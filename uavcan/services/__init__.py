#
# Copyright (C) 2014-2015  UAVCAN Development Team  <uavcan.org>
#
# This software is distributed under the terms of the MIT License.
#
# Author: Ben Dyer <ben_dyer@mac.com>
#         Pavel Kirienko <pavel.kirienko@zubax.com>
#

from __future__ import division, absolute_import, print_function, unicode_literals
import os
from logging import getLogger

import uavcan
import uavcan.node


logger = getLogger(__name__)


class FileGetInfoService(uavcan.node.Service):
    def __init__(self, *args, **kwargs):
        super(FileGetInfoService, self).__init__(*args, **kwargs)
        self.base_path = kwargs.get("path")

    def on_request(self):
        logger.debug("[#{0:03d}:uavcan.protocol.file.GetInfo] {1!r}"
                     .format(self.transfer.source_node_id, self.request.path.path.decode()))
        try:
            vpath = self.request.path.path.decode()
            with open(os.path.join(self.base_path, vpath), "rb") as fw:
                data = fw.read()
                self.response.error.value = self.response.error.OK
                self.response.size = len(data)
                self.response.crc64 = \
                    uavcan.dsdl.signature.compute_signature(data)
                self.response.entry_type.flags = \
                    (self.response.entry_type.FLAG_FILE |
                     self.response.entry_type.FLAG_READABLE)
        except Exception:
            logger.exception("[#{0:03d}:uavcan.protocol.file.GetInfo] error")
            self.response.error.value = self.response.error.UNKNOWN_ERROR
            self.response.size = 0
            self.response.crc64 = 0
            self.response.entry_type.flags = 0


class FileReadService(uavcan.node.Service):
    def __init__(self, *args, **kwargs):
        super(FileReadService, self).__init__(*args, **kwargs)
        self.base_path = kwargs.get("path")

    def on_request(self):
        logger.debug("[#{0:03d}:uavcan.protocol.file.Read] {1!r} @ offset {2:d}"
                     .format(self.transfer.source_node_id, self.request.path.path.decode(), self.request.offset))
        try:
            vpath = self.request.path.path.decode()
            with open(os.path.join(self.base_path, vpath), "rb") as fw:
                fw.seek(self.request.offset)
                for byte in fw.read(256):
                    self.response.data.append(ord(byte))
                self.response.error.value = self.response.error.OK
        except Exception:
            logger.exception("[#{0:03d}:uavcan.protocol.file.Read] error")
            self.response.error.value = self.response.error.UNKNOWN_ERROR
