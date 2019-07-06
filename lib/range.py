"""
Dwarf - Copyright (C) 2019 Giovanni Rocca (iGio90)

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>
"""
import json

from lib import utils
from lib.hook import Hook


class Range(object):
    # dump memory from target proc
    SOURCE_TARGET = 0

    def __init__(self, source, dwarf):
        super().__init__()

        self.source = source
        self.dwarf = dwarf

        self.base = 0
        self.size = 0
        self.tail = 0
        self.data = bytes()

        self.start_address = 0
        self.start_offset = 0

    def invalidate(self):
        self.base = 0
        self.size = 0
        self.tail = 0
        self.data = bytes()

        self.start_address = 0
        self.start_offset = 0

    def init_with_address(self, address, length=0, base=0, require_data=True):
        self.start_address = utils.parse_ptr(address)

        if self.base > 0:
            if self.base < self.start_address < self.tail:
                self.start_offset = self.start_address - self.base
                return -1

        if self.source == Range.SOURCE_TARGET:
            try:
                _range = self.dwarf.dwarf_api('getRange', self.start_address)
            except Exception as e:
                return 1
            if _range is None or len(_range) == 0:
                return 1

            # setup range fields
            self.base = int(_range['base'], 16)
            if base > 0:
                self.base = base
            self.size = _range['size']
            if 0 < length < self.size:
                self.size = length
            self.tail = self.base + self.size
            self.start_offset = self.start_address - self.base

            if require_data:
                # read data
                self.data = self.dwarf.read_memory(self.base, self.size)

                # check if we have hooks in range and patch data
                hooks = json.loads(self.dwarf._script.exports.hooks())
                for key in list(hooks.keys()):
                    hook = hooks[key]
                    if utils.parse_ptr(hook['nativePtr']) != 1 and hook['bytes']:
                        hook_address = utils.parse_ptr(hook['nativePtr'])
                        if hook_address % 2 != 0:
                            hook_address -= 1
                        if self.base < hook_address < self.tail:
                            offset = hook_address - self.base
                            # patch bytes
                            self.patch_bytes(hook['bytes'], offset)
        if self.data is None:
            self.data = bytes()
            return 1
        if len(self.data) == 0:
            return 1
        return 0

    def patch_bytes(self, _bytes, offset):
        data_bt = bytearray(self.data)
        org_bytes = bytes.fromhex(_bytes)
        data_bt[offset:offset+len(org_bytes)] = org_bytes
        self.data = bytes(data_bt)

    def set_start_offset(self, offset):
        self.start_offset = offset
        self.start_address = self.base + offset
