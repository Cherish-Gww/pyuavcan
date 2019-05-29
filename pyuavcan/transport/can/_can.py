#
# Copyright (c) 2019 UAVCAN Development Team
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import annotations
import typing
import asyncio
import logging
import pyuavcan.transport
from . import _session, media as _media, _frame, _can_id


_logger = logging.getLogger(__name__)


class CANTransport(pyuavcan.transport.Transport):
    def __init__(self,
                 media: _media.Media,
                 loop:  typing.Optional[asyncio.AbstractEventLoop] = None):
        self._media = media
        self._local_node_id: typing.Optional[int] = None
        self._started = False
        self._media_lock = asyncio.Lock(loop=loop)
        self._loop = loop if loop is not None else asyncio.get_event_loop()

        # Lookup performance for the output registry is not important because it's only used for loopback frames.
        # Hence we don't trade-off memory for speed here.
        self._output_registry: typing.Dict[typing.Tuple[pyuavcan.transport.DataSpecifier, typing.Optional[int]],
                                           _session.OutputSession] = {}  # None for broadcast

        # Input lookup must be fast, so we use constant-complexity static lookup table.
        self._input_dispatch_table: typing.List[typing.Optional[_session.InputSession]] = [
            None for _ in range(_INPUT_DISPATCH_TABLE_SIZE + 1)
        ]

        self._media.set_received_frames_handler(self._on_frames_received)   # Effectively starts the transport.

    @property
    def protocol_parameters(self) -> pyuavcan.transport.ProtocolParameters:
        sft_payload_capacity = self._media.max_data_field_length - 1
        return pyuavcan.transport.ProtocolParameters(
            transfer_id_modulo=_frame.TRANSFER_ID_MODULO,
            node_id_set_cardinality=_can_id.CANID.NODE_ID_MASK + 1,
            single_frame_transfer_payload_capacity_bytes=sft_payload_capacity
        )

    @property
    def local_node_id(self) -> typing.Optional[int]:
        return self._local_node_id

    async def set_local_node_id(self, node_id: int) -> None:
        if self._local_node_id is None:
            if 0 <= node_id <= _can_id.CANID.NODE_ID_MASK:
                self._local_node_id = int(node_id)
                await self._media.enable_automatic_retransmission()
                await self._reconfigure_acceptance_filters()
            else:
                raise ValueError(f'Invalid node ID for CAN: {node_id}')
        else:
            raise pyuavcan.transport.InvalidTransportConfigurationError('Node ID can be assigned only once')

    async def close(self) -> None:
        await self._media.close()

    async def get_statistics(self) -> pyuavcan.transport.Statistics:
        raise NotImplementedError

    async def get_broadcast_output(self,
                                   data_specifier:   pyuavcan.transport.DataSpecifier,
                                   payload_metadata: pyuavcan.transport.PayloadMetadata) \
            -> _session.BroadcastOutputSession:
        def factory(finalizer: typing.Callable[[], None]) -> _session.BroadcastOutputSession:
            metadata = pyuavcan.transport.SessionMetadata(data_specifier, payload_metadata)
            return _session.BroadcastOutputSession(metadata=metadata,
                                                   transport=self,
                                                   media_lock=self._media_lock,
                                                   finalizer=finalizer)
        out = self._get_output(data_specifier, None, factory)
        assert isinstance(out, _session.BroadcastOutputSession)
        return out

    async def get_unicast_output(self,
                                 data_specifier:      pyuavcan.transport.DataSpecifier,
                                 payload_metadata:    pyuavcan.transport.PayloadMetadata,
                                 destination_node_id: int) -> _session.UnicastOutputSession:
        def factory(finalizer: typing.Callable[[], None]) -> _session.UnicastOutputSession:
            metadata = pyuavcan.transport.SessionMetadata(data_specifier, payload_metadata)
            return _session.UnicastOutputSession(destination_node_id=destination_node_id,
                                                 metadata=metadata,
                                                 transport=self,
                                                 media_lock=self._media_lock,
                                                 finalizer=finalizer)
        out = self._get_output(data_specifier, destination_node_id, factory)
        assert isinstance(out, _session.UnicastOutputSession)
        return out

    async def get_promiscuous_input(self,
                                    data_specifier:   pyuavcan.transport.DataSpecifier,
                                    payload_metadata: pyuavcan.transport.PayloadMetadata) \
            -> _session.PromiscuousInputSession:
        def factory(finalizer: typing.Callable[[], None]) -> _session.PromiscuousInputSession:
            metadata = pyuavcan.transport.SessionMetadata(data_specifier, payload_metadata)
            return _session.PromiscuousInputSession(metadata=metadata, loop=self._loop, finalizer=finalizer)
        out = self._get_input(data_specifier, None, factory)
        assert isinstance(out, _session.PromiscuousInputSession)
        return out

    async def get_selective_input(self,
                                  data_specifier:   pyuavcan.transport.DataSpecifier,
                                  payload_metadata: pyuavcan.transport.PayloadMetadata,
                                  source_node_id:   int) -> _session.SelectiveInputSession:
        def factory(finalizer: typing.Callable[[], None]) -> _session.SelectiveInputSession:
            metadata = pyuavcan.transport.SessionMetadata(data_specifier, payload_metadata)
            return _session.SelectiveInputSession(source_node_id=source_node_id,
                                                  metadata=metadata,
                                                  loop=self._loop,
                                                  finalizer=finalizer)
        out = self._get_input(data_specifier, source_node_id, factory)
        assert isinstance(out, _session.SelectiveInputSession)
        return out

    @property
    def media(self) -> _media.Media:
        return self._media

    def _get_output(self,
                    data_specifier:      pyuavcan.transport.DataSpecifier,
                    destination_node_id: typing.Optional[int],
                    factory:             typing.Callable[[typing.Callable[[], None]], _session.OutputSession]) \
            -> _session.OutputSession:
        def finalizer() -> None:
            try:
                del self._output_registry[key]
            except LookupError:
                pass

        key = data_specifier, destination_node_id
        try:
            return self._output_registry[key]
        except KeyError:
            session = factory(finalizer)
            self._output_registry[key] = session
            return session

    def _get_input(self,
                   data_specifier: pyuavcan.transport.DataSpecifier,
                   source_node_id: typing.Optional[int],
                   factory:        typing.Callable[[typing.Callable[[], None]], _session.InputSession]) \
            -> _session.InputSession:
        def finalizer() -> None:
            self._input_dispatch_table[index] = None

        index = _compute_input_dispatch_table_index(data_specifier, source_node_id)
        session = self._input_dispatch_table[index]
        if session is None:
            session = factory(finalizer)
            self._input_dispatch_table[index] = session
        return session

    def _on_frames_received(self, frames: typing.Iterable[_media.TimestampedDataFrame]) -> None:
        for raw_frame in frames:
            try:
                cid = _can_id.CANID.try_parse(raw_frame.identifier)
                if cid is not None:                                             # Ignore non-UAVCAN CAN frames
                    ufr = _frame.TimestampedUAVCANFrame.try_parse(raw_frame)
                    if ufr is not None:                                         # Ignore non-UAVCAN CAN frames
                        if not ufr.loopback:
                            self._handle_received_frame(cid, ufr)
                        else:
                            self._handle_loopback_frame(cid, ufr)
            except Exception as ex:
                _logger.exception(f'Unhandled exception while processing input CAN frame {raw_frame}: {ex}')

    def _handle_received_frame(self, can_id: _can_id.CANID, frame: _frame.TimestampedUAVCANFrame) -> None:
        assert not frame.loopback
        data_spec = can_id.to_input_data_specifier()
        if isinstance(can_id, _can_id.ServiceCANID):
            exact_source_node_id: typing.Optional[int] = can_id.source_node_id
        elif isinstance(can_id, _can_id.MessageCANID):
            exact_source_node_id = can_id.source_node_id
        else:
            assert False

        for nid in {exact_source_node_id, None}:
            index = _compute_input_dispatch_table_index(data_spec, nid)
            session = self._input_dispatch_table[index]
            if session is not None:                                     # Ignore UAVCAN frames we don't care about
                session.push_frame(can_id, frame)

    def _handle_loopback_frame(self, can_id: _can_id.CANID, frame: _frame.TimestampedUAVCANFrame) -> None:
        assert frame.loopback
        data_spec = can_id.to_output_data_specifier()
        if isinstance(can_id, _can_id.ServiceCANID):
            dest_nid: typing.Optional[int] = can_id.destination_node_id
        else:
            assert not hasattr(can_id, 'destination_node_id')
            dest_nid = None

        try:
            session = self._output_registry[(data_spec, dest_nid)]
        except KeyError:
            _logger.info('No matching output session for loopback frame: %s; '
                         'parsed CAN ID: %s; data specifier: %s; destination node ID: %s. '
                         'Either the session has just been closed or the media driver is misbehaving.',
                         frame, can_id, data_spec, dest_nid, self._media)
        else:
            session.handle_loopback_frame(frame)

    async def _reconfigure_acceptance_filters(self) -> None:
        pass

    def __str__(self) -> str:
        raise NotImplementedError


def _compute_input_dispatch_table_index(data_specifier: pyuavcan.transport.DataSpecifier,
                                        source_node_id: typing.Optional[int]) -> int:
    """
    Time-memory trade-off: the input dispatch table is tens of megabytes large, but the lookup is very fast and O(1).
    """
    assert source_node_id is None or source_node_id < _NUM_NODE_IDS

    if isinstance(data_specifier, pyuavcan.transport.MessageDataSpecifier):
        dim1 = data_specifier.subject_id
    elif isinstance(data_specifier, pyuavcan.transport.ServiceDataSpecifier):
        if data_specifier.role == data_specifier.Role.CLIENT:
            dim1 = data_specifier.service_id + _NUM_SUBJECTS
        elif data_specifier.role == data_specifier.Role.SERVER:
            dim1 = data_specifier.service_id + _NUM_SUBJECTS + _NUM_SERVICES
        else:
            assert False
    else:
        assert False

    dim2_cardinality = _NUM_NODE_IDS + 1
    dim2 = source_node_id if source_node_id is not None else _NUM_NODE_IDS

    point = dim1 * dim2_cardinality + dim2

    assert 0 <= point < _INPUT_DISPATCH_TABLE_SIZE
    return point


_NUM_SUBJECTS = _can_id.MessageCANID.SUBJECT_ID_MASK + 1
_NUM_SERVICES = _can_id.ServiceCANID.SERVICE_ID_MASK + 1
_NUM_NODE_IDS = _can_id.CANID.NODE_ID_MASK + 1

# Services multiplied by two to account for requests and responses.
# One added to nodes to allow promiscuous inputs which don't care about source node ID.
_INPUT_DISPATCH_TABLE_SIZE = (_NUM_SUBJECTS + _NUM_SERVICES * 2) * (_NUM_NODE_IDS + 1)


def _unittest_can_compute_input_dispatch_table_index() -> None:
    values: typing.Set[int] = set()
    for node_id in (*range(_NUM_NODE_IDS), None):
        for subj in range(_NUM_SUBJECTS):
            out = _compute_input_dispatch_table_index(pyuavcan.transport.MessageDataSpecifier(subj), node_id)
            assert out not in values
            values.add(out)
            assert out < _INPUT_DISPATCH_TABLE_SIZE

        for serv in range(_NUM_SERVICES):
            for role in pyuavcan.transport.ServiceDataSpecifier.Role:
                out = _compute_input_dispatch_table_index(pyuavcan.transport.ServiceDataSpecifier(serv, role), node_id)
                assert out not in values
                values.add(out)
                assert out < _INPUT_DISPATCH_TABLE_SIZE

    assert len(values) == _INPUT_DISPATCH_TABLE_SIZE
