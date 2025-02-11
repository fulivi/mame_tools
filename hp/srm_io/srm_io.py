#!/usr/bin/env python3
# A file server for SRM network
# Copyright (C) 2025 F. Ulivi <fulivi at big "G" mail>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see
# <http://www.gnu.org/licenses/>.

import argparse
import asyncio
import copy
import errno
import io
import os
import pathlib
import re
import stat
import struct
import time

VERSION="1.0"

VOL_NAME="SERVER"

SRM_ERRNO_SOFTWARE_BUG = 31000
SRM_ERRNO_INVALID_FILE_ID = 31011
SRM_ERRNO_VOLUME_IO_ERROR = 31013
SRM_ERRNO_FILE_PATHNAME_MISSING = 31014
SRM_ERRNO_FILE_UNOPENED = 31019
SRM_ERRNO_ACCESS_TO_FILE_NOT_ALLOWED = 31023
SRM_ERRNO_INSUFFICIENT_DISK_SPACE = 31028
SRM_ERRNO_DUPLICATE_FILENAMES = 31029
SRM_ERRNO_FILE_NOT_FOUND = 31032
SRM_ERRNO_FILE_NOT_DIRECTORY = 31034
SRM_ERRNO_DIRECTORY_NOT_EMPTY = 31035
SRM_ERRNO_VOLUME_NOT_FOUND = 31036
SRM_ERRNO_RENAME_ACROSS_VOLUMES = 31043
SRM_ERRNO_EOF_ENCOUNTERED = 31045

class Abort:
    def __init__(self):
        pass

    def __str__(self):
        return "Abort"

    def encode(self):
        return b"\xff"

class BadPacket:
    def __init__(self, msg, payload = None):
        self.msg = msg
        self.payload = payload

    def __str__(self ):
        if self.payload is None:
            return f"Bad packet: {self.msg}"
        else:
            return f"Bad packet: {self.msg} payload={len(self.payload)} bytes"

class RawPacket:
    def __init__(self, msg, calc_crc, crc_ok, bit_count):
        self.msg = msg
        self.calc_crc = calc_crc
        self.crc_ok = crc_ok
        self.bit_count = bit_count

    def __str__(self):
        return f"Msg len={len(self.msg)} crc_ok={self.crc_ok} bit_count={self.bit_count}"

class Packet:
    def __init__(self):
        pass

    def set_common(self, sa, da, ctrl, level):
        self.sa = sa
        self.da = da
        self.ctrl = ctrl
        self.level = level

    def decode(raw):
        # Checks
        if not raw.crc_ok:
            s = io.StringIO()
            print(f"Wrong CRC ({raw.calc_crc:04x}), bit_count={raw.bit_count} ", file=s, end="")
            for b in raw.msg:
                print(f"{b:02x} ", file=s, end="")
            return BadPacket(s.getvalue())
        elif (raw.bit_count % 8) != 0:
            return BadPacket("Size not integral number of bytes")
        else:
            n_bytes = raw.bit_count // 8
            if n_bytes < 8:
                return BadPacket(f"Too short ({n_bytes} bytes)")
            l = struct.unpack("<H", raw.msg[ 2:4 ])[ 0 ]
            if l != len(raw.msg):
                return BadPacket(f"Inconsistent length ({l} != {len(raw.msg)}")
            sa = raw.msg[ 1 ]
            if sa >= 64:
                return BadPacket(f"Invalid SA ({sa})")
            ctrl = raw.msg[ 5 ]
            # I frame
            if (ctrl & 0x11) == 0x10:
                nr = (ctrl & 0xe0) >> 5
                ns = (ctrl & 0x0e) >> 1
                return IPacket(sa, raw.msg[ 0 ], ctrl, raw.msg[ 4 ], nr, ns, raw.msg[ 6:l-2 ])
            elif (ctrl & 0x1f) == 0x11:
                # RR frame
                nr = (ctrl & 0xe0) >> 5
                return RRPacket(sa, raw.msg[ 0 ], ctrl, raw.msg[ 4 ], nr)
            elif ctrl == 0x3f:
                # SABM
                return SABMPacket(sa, raw.msg[ 0 ], ctrl, raw.msg[ 4 ])
            elif ctrl == 0x73:
                # UA
                return UAPacket(sa, raw.msg[ 0 ], ctrl, raw.msg[ 4 ])
            elif ctrl == 0x1b:
                # RC
                return RCPacket(sa, raw.msg[ 0 ], ctrl, raw.msg[ 4 ], raw.msg[ 6:l-2 ])
            else:
                return BadPacket(f"Unknown type {sa:2} {raw.msg[ 0 ]:2} {raw.msg[ 4 ]} {ctrl:02x}", raw.msg[ 6:l-2 ])

    def encode(self, level, ctrl, payload = None):
        if payload is None:
            length = 8
        else:
            length = 8 + len(payload)
        hdr = struct.pack("<BBHBB", self.da, self.sa, length, level, ctrl)
        if payload is None:
            return hdr
        else:
            return hdr + payload

class IPacket(Packet):
    def __init__(self, sa, da, ctrl, level, nr, ns, payload):
        super().set_common(sa, da, ctrl, level)
        self.nr = nr
        self.ns = ns
        self.payload = payload
        if ctrl == 0:
            self.ctrl = 0x10 | (self.nr << 5) | (self.ns << 1)

    def encode(self):
        return super().encode(self.level, self.ctrl, self.payload)

    def __str__(self ):
        return f"{self.sa:2} {self.da:2} {self.level} {self.ctrl:02x} I     N(R)={self.nr} N(S)={self.ns} payload={len(self.payload)} bytes"

class RRPacket(Packet):
    def __init__(self, sa, da, ctrl, level, nr):
        super().set_common(sa, da, ctrl, level)
        self.nr = nr
        if ctrl == 0:
            self.ctrl = 0x11 | (self.nr << 5)
        if level == 0:
            self.level = 2

    def __str__(self):
        return f"{self.sa:2} {self.da:2} {self.level} {self.ctrl:02x} RR    N(R)={self.nr}"

    def encode(self):
        return super().encode(self.level, self.ctrl)

class SABMPacket(Packet):
    def __init__(self, sa, da, ctrl = 0, level = 0):
        super().set_common(sa, da, ctrl, level)
        if ctrl == 0:
            self.ctrl = 0x3f
        if level == 0:
            self.level = 2

    def __str__(self):
        return f"{self.sa:2} {self.da:2} {self.level} {self.ctrl:02x} SABM  "

    def encode(self):
        return super().encode(self.level, self.ctrl)

class UAPacket(Packet):
    def __init__(self, sa, da, ctrl = 0, level = 0):
        super().set_common(sa, da, ctrl, level)
        if ctrl == 0:
            self.ctrl = 0x73
        if level == 0:
            self.level = 2

    def __str__(self):
        return f"{self.sa:2} {self.da:2} {self.level} {self.ctrl:02x} UA    "

    def encode(self):
        return super().encode(self.level, self.ctrl)

class RCPacket(Packet):
    def __init__(self, sa, da, ctrl, level, payload):
        super().set_common(sa, da, ctrl, level)
        self.payload = payload
        if ctrl == 0:
            self.ctrl = 0x1b

    def encode(self):
        return super().encode(self.level, self.ctrl, self.payload)

    def __str__(self ):
        return f"{self.sa:2} {self.da:2} {self.level} {self.ctrl:02x} RC    payload={len(self.payload)} bytes"

class RCRPacket(Packet):
    def __init__(self, sa, da, ctrl, level, payload):
        super().set_common(sa, da, ctrl, level)
        self.payload = payload
        if ctrl == 0:
            self.ctrl = 0x5b

    def encode(self):
        return super().encode(self.level, self.ctrl, self.payload)

    def __str__(self ):
        return f"{self.sa:2} {self.da:2} {self.level} {self.ctrl:02x} RCR   payload={len(self.payload)} bytes"

SDLC_POLY=0x8408

def update_crc(crc, bit):
    if (crc ^ bit) & 1:
        crc = (crc >> 1) ^ SDLC_POLY
    else:
        crc >>= 1
    return crc & 0xffff

class SDLC_IO:
    FLAG=0x7e
    CRC_RESIDUAL=0xf0b8
    CRC_XOR_IN=0xffff
    CRC_XOR_OUT=0xffff
    BCAST_ADDR=0xff

    def __init__(self, my_addr, reader, writer):
        self.my_addr = my_addr
        self.reader = reader
        self.writer = writer
        # 0     Waiting for flag
        # 1     Shifting flag out
        # 2     Receiving 1st byte
        # 3     Receiving following bytes
        self.rx_sync_fsm = 0
        self.rx_sync_sr = 0
        self.rx_sr = 0
        self.rx_one_cnt = 0
        self.rx_bit = 0
        self.rx_bit_limit = 0

    def enter_hunt_mode(self):
        self.rx_sync_fsm = 0

    async def rx_fsm_bit(self, bit):
        flag_matched = self.rx_sync_sr == self.FLAG
        sync_sr_out = self.rx_sync_sr & 1
        self.rx_sync_sr >>= 1
        if bit:
            self.rx_sync_sr |= 0x80
        zero_deleted = False
        if sync_sr_out:
            self.rx_sr = (self.rx_sr >> 1) | 0x80
            if self.rx_one_cnt < 7:
                self.rx_one_cnt += 1
                if self.rx_one_cnt == 7:
                    yield Abort()
                    self.enter_hunt_mode()
        elif self.rx_one_cnt == 5:
            self.rx_one_cnt = 0
            zero_deleted = True
        else:
            self.rx_sr >>= 1
            self.rx_one_cnt = 0
        if self.rx_sync_fsm == 0 or self.rx_sync_fsm == 1:
            if flag_matched:
                self.rx_sync_fsm = 1
                self.rx_bit = 0
                self.rx_bit_limit = 7
            elif self.rx_sync_fsm == 1:
                self.rx_bit += 1
                if self.rx_bit == self.rx_bit_limit:
                    self.rx_sync_fsm = 2
                    self.rx_crc = self.CRC_XOR_IN
                    self.rx_bit = 0
                    self.rx_bit_limit = 8
                    self.rx_accum = bytearray()
        elif not zero_deleted and (self.rx_sync_fsm == 2 or self.rx_sync_fsm == 3):
            self.rx_bit += 1
            if self.rx_bit == self.rx_bit_limit:
                self.rx_bit = 0
            if flag_matched:
                if self.rx_sync_fsm == 3:
                    # frame ends
                    tot_bits = 8 * len(self.rx_accum)
                    if self.rx_bit != 1:
                        self.rx_accum.append(self.rx_sr)
                    tot_bits += (self.rx_bit - 1) % 8
                    yield RawPacket(self.rx_accum, self.rx_crc, self.rx_crc == self.CRC_RESIDUAL, tot_bits)
                self.rx_sync_fsm = 1
                self.rx_bit = 0
                self.rx_bit_limit = 7
            else:
                p_crc = self.rx_crc
                self.rx_crc = update_crc(self.rx_crc, sync_sr_out)
                if self.rx_bit == 0:
                    # Check address
                    if self.rx_sync_fsm == 2 and self.rx_sr != self.BCAST_ADDR and self.rx_sr != self.my_addr:
                        self.enter_hunt_mode()
                    else:
                        self.rx_accum.append(self.rx_sr)
                        self.rx_bit_limit = 8
                        self.rx_sync_fsm = 3

    async def rx_fsm(self, byt):
        for n in range(8):
            bit = byt & 1
            byt >>= 1
            async for msg in self.rx_fsm_bit(bit):
                yield msg

    async def get_rx_msg(self):
        while True:
            rx_byte = await self.reader.read(1)
            if not rx_byte:
                break
            async for msg in self.rx_fsm(rx_byte[ 0 ]):
                yield msg

    def tx_bit(self, bit):
        self.tx_sr >>= 1
        if bit:
            self.tx_sr |= 0x80
        self.tx_bit_cnt += 1
        if self.tx_bit_cnt == 8:
            self.tx_accum.append(self.tx_sr)
            self.tx_bit_cnt = 0

    def tx_byte(self, b, stuffing):
        for _ in range(8):
            bit = b & 1
            b >>= 1
            self.tx_bit(bit)
            if bit and stuffing:
                self.tx_one_cnt += 1
                if self.tx_one_cnt == 5:
                    self.tx_one_cnt = 0
                    self.tx_bit(0)
            else:
                self.tx_one_cnt = 0

    async def tx(self, pkt):
        if verb_level >= 2:
            print(f"<{str(pkt)}")
        raw = pkt.encode()
        self.tx_accum = bytearray()
        self.tx_sr = 0
        self.tx_bit_cnt = 0
        # Flags before packet
        for _ in range(5):
            self.tx_byte(self.FLAG, False)
        crc = self.CRC_XOR_IN
        for b in raw:
            self.tx_byte(b, True)
            for _ in range(8):
                bit = b & 1
                b >>= 1
                crc = update_crc(crc, bit)
        crc ^= self.CRC_XOR_OUT
        self.tx_byte(crc & 0xff, True)
        self.tx_byte((crc >> 8) & 0xff, True)
        # Trailing flags
        for _ in range(70):
            self.tx_byte(self.FLAG, False)
        # 4 aborts to ensure line is detected as idle when packet ends
        raw = Abort().encode()
        for _ in range(4):
            for b in raw:
                self.tx_byte(b, False)
        self.writer.write(self.tx_accum)
        await self.writer.drain()

class MyException(Exception):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return f"Error: {self.msg}"

class InvalidFileID(Exception):
    def __init__(self, _id):
        self._id = _id

class FailedRequest(Exception):
    def __init__(self, err_code):
        self.err_code = err_code

def dump(data):
    for i in range(0, len(data), 16):
        print(f"{i:03x} ", end="")
        for j in range(i, min(i + 16, len(data))):
            print(f"{data[ j ]:02x} ", end="")
        print()

def decode_str(b):
    try:
        idx = b.index(32)
    except ValueError:
        idx = len(b)
    return b[ :idx ].decode("ascii", "ignore")

def encode_str(b, pad_to = 16):
    pad = pad_to - len(b)
    if pad > 0:
        b += " " * pad
    else:
        b = b[ :pad_to ]
    return b.encode("ascii", "replace")

def encode_id_time(xid, t):
    tm = time.localtime(t)
    y = tm.tm_year - 1900
    if y >= 100:
        y -= 100
    date = (tm.tm_mon << 12) | (tm.tm_mday << 7) | y
    seconds = (tm.tm_hour * 60 + tm.tm_min) * 60 + tm.tm_sec
    return struct.pack(">HHL", xid, date, seconds)

class VolumeHeader:
    def __init__(self, driv_name, catorg, dap, a1, ha, unit, vol, vol_name):
        self.driv_name = driv_name
        self.catorg = catorg
        self.dap = dap
        self.a1 = a1
        self.ha = ha
        self.unit = unit
        self.vol = vol
        self.vol_name = vol_name

    def decode(vh):
        # len(vh) >= 72
        driv_name = decode_str(vh[ 4:20 ])
        catorg = decode_str(vh[ 20:36 ])
        dap, a1, ha, unit, vol = struct.unpack(">LLLLL", vh[ 36:56 ])
        vol_name = decode_str(vh[ 56:72 ])
        return VolumeHeader(driv_name, catorg, dap, a1, ha, unit, vol, vol_name)

    def is_handled(self):
        return (self.dap and self.a1 == 0 or self.a1 == 8) or (not self.dap and self.vol_name == VOL_NAME)

    def __str__(self):
        return f"addr {self.a1}, haddr {self.ha}, unit {self.unit}, volume {self.vol}, driver {self.driv_name} catorg {self.catorg} vname {self.vol_name} present {self.dap}"

class DEntry:
    def __init__(self, lif_name, sets, path, st_mode, st_uid, st_gid, st_mtime, st_ctime):
        self.lif_name = lif_name
        self.sets = sets
        self.path = path
        self.st_mode = st_mode
        self.st_uid = st_uid
        self.st_gid = st_gid
        self.st_mtime = st_mtime
        self.st_ctime = st_ctime

class File(DEntry):
    def __init__(self, lif_name, sets, path, st_mode, st_uid, st_gid, st_mtime, st_ctime, st_size, lif_type, boot_address):
        super().__init__(lif_name, sets, path, st_mode, st_uid, st_gid, st_mtime, st_ctime)
        self.st_size = st_size
        self.lif_type = lif_type
        self.boot_address = boot_address

    def get_lif_type(self):
        return self.lif_type - (65536 if self.lif_type & 0x8000 else 0)

    def compose_file_name(name, lif_type, boot_address):
        return f"{name}.{boot_address:08x}.{lif_type:04x}"

class Directory(DEntry):
    def __init__(self, lif_name, sets, path, st_mode, st_uid, st_gid, st_mtime, st_ctime):
        super().__init__(lif_name, sets, path, st_mode, st_uid, st_gid, st_mtime, st_ctime)

    def get_lif_type(self):
        return 3

def key_by_t_name(z):
    'Sort by type and name'
    if z.is_file():
        r = z.name.lower().split('.')
        if len(r) > 2:
            r = r[:-2]
        r = 1,r
    elif z.is_dir():
        r = 0,z.name.lower()
    else:
        return 99,z.name.lower()
    return r

class FS:
    def __init__(self, top_dir):
        self.top_dir = top_dir
        # <LIF filename>.<boot address>.<lif type>
        self.re_filename = re.compile(r"(.{1,16})\.([0-9a-f]{8})\.([0-9a-f]{4})")
        # Open files
        # Key: file_id
        # Val: (a File or Directory obj, open file or None)
        self.open_files = {}
        self.next_file_id = 1

    def decode_filename_sets(self, file_header, file_name_sets, start_idx = 0):
        num_sets, wd, pt = struct.unpack(">LLL", file_header[ 0:12 ])
        if not 0 <= num_sets <= 7:
            raise MyException(f"num_sets out of range ({num_sets})")
        min_len = 36 * (num_sets + start_idx)
        if len(file_name_sets) < min_len:
            raise MyException(f"file_name_sets too short ({len(file_name_sets)} < {min_len})")
        if pt == 0 or wd == 0:
            # Start at root
            sets = []
        else:
            # Start at working directory
            try:
                d, _ = self.file_id_to_file(wd)
                if not isinstance(d, Directory):
                    raise FailedRequest(SRM_ERRNO_FILE_NOT_DIRECTORY)
                sets = copy.copy(d.sets)
            except InvalidFileID:
                raise FailedRequest(SRM_ERRNO_INVALID_FILE_ID)
        for i in range(start_idx, start_idx + num_sets):
            idx = 36 * i
            s = decode_str(file_name_sets[ idx:idx+16 ])
            pos = s.find("<")
            if pos >= 0:
                s = s[ :pos ]
            pos = s.find(">")
            if pos >= 0:
                s = s[ :pos ]
            if not s:
                raise MyException(f"Empty file name in set")
            sets.append(s)
        return sets, num_sets

    def sets_to_path(self, sets):
        p = pathlib.Path(self.top_dir)
        for s in sets:
            p /= s
        return p

    def split_sets(self, sets):
        if sets:
            return sets[ :-1 ], sets[ -1 ]
        else:
            return None, None

    def cat_dir(self, path):
        c = []
        try:
            with os.scandir(path) as it:
                for e in sorted(it,key=key_by_t_name):
                    if e.is_file() and (mo := self.re_filename.fullmatch(e.name)):
                        st = e.stat()
                        c.append(File(mo.group(1), None, path / e.name, st.st_mode, st.st_uid, st.st_gid, st.st_mtime, st.st_ctime, st.st_size, int(mo.group(3), 16), int(mo.group(2), 16)))
                    elif e.is_dir() and len(e.name) <= 16:
                        st = e.stat()
                        c.append(Directory(e.name, None, path / e.name, st.st_mode, st.st_uid, st.st_gid, st.st_mtime, st.st_ctime))
        except OSError:
            pass
        return c

    def find(self, sets):
        sets_up, sets_last = self.split_sets(sets)
        path = self.sets_to_path(sets)
        try:
            s = os.stat(path)
            if stat.S_ISDIR(s.st_mode):
                return Directory("" if sets_up is None else sets_last, sets, path, s.st_mode, s.st_uid, s.st_gid, s.st_mtime, s.st_ctime)
        except OSError as e:
            if e.errno == errno.ENOENT:
                try:
                    if sets_up is not None:
                        path = self.sets_to_path(sets_up)
                    with os.scandir(path) as it:
                        for e in it:
                            if e.is_file() and (mo := self.re_filename.fullmatch(e.name)) and mo.group(1) == sets_last:
                                st = e.stat()
                                return File(mo.group(1), sets, path / e.name, st.st_mode, st.st_uid, st.st_gid, st.st_mtime, st.st_ctime, st.st_size, int(mo.group(3), 16), int(mo.group(2), 16))
                except OSError as e:
                    pass
        return None

    def sets_to_file_path(self, sets, lif_type, boot_addr):
        sets_up, sets_last = self.split_sets(sets)
        if sets_up is None:
            raise FailedRequest(SRM_ERRNO_FILE_PATHNAME_MISSING)
        path = self.sets_to_path(sets_up)
        path /= File.compose_file_name(sets_last, lif_type, boot_addr)
        return path

    def get_new_file_id(self, file_or_dir, open_file):
        file_id = self.next_file_id
        self.next_file_id += 1
        self.open_files[ file_id ] = (file_or_dir, open_file)
        return file_id

    def file_id_to_file(self, file_id):
        try:
            return self.open_files[ file_id ]
        except KeyError:
            raise InvalidFileID(file_id)

    def del_file_id(self, file_id):
        del self.open_files[ file_id ]

    def open_file(self, file_):
        f = open(file_.path, "r+b")
        return self.get_new_file_id(file_, f)

    def encode_file_info(self, file_or_dir):
        # file_name
        enc = bytearray(encode_str(file_or_dir.lif_name))
        if isinstance(file_or_dir, File):
            # REGULAR FILE
            # open_flag
            # share_code
            # file_code
            # record_mode
            # max_record_size
            # max_file_size
            enc.extend(struct.pack(">LLlLLL", 0, 0, file_or_dir.get_lif_type(), 0, 256, 0xffffffff))
        else:
            # DIRECTORY
            # open_flag
            # share_code
            # file_code
            # record_mode
            # max_record_size
            # max_file_size
            enc.extend(struct.pack(">LLlLLL", 0, 1, file_or_dir.get_lif_type(), 1, 1, 0xffffffff))
        # creation_date
        enc.extend(encode_id_time(file_or_dir.st_uid, file_or_dir.st_ctime))
        # last_access_date
        enc.extend(encode_id_time(file_or_dir.st_gid, file_or_dir.st_mtime))
        # capabilities
        # perm
        enc.extend(struct.pack(">hH", -1, file_or_dir.st_mode & 0x1ff))
        if isinstance(file_or_dir, File):
            # REGULAR FILE
            # logical_eof
            # physical_size
            enc.extend(struct.pack(">LL", file_or_dir.st_size, file_or_dir.st_size))
        else:
            # DIRECTORY
            # logical_eof
            # physical_size
            enc.extend(struct.pack(">LL", 1024, 1024))
        return enc

def check_volume_handled(vol_header):
    vh = VolumeHeader.decode(vol_header)
    if not vh.is_handled():
        raise FailedRequest(SRM_ERRNO_VOLUME_NOT_FOUND)

def handle_catalog(pkt):
    max_num_files, file_index = struct.unpack(">LL", pkt.payload[ 11:19 ])
    check_volume_handled(pkt.payload[ 23:95 ])
    sets, _ = filesystem.decode_filename_sets(pkt.payload[ 95:123 ], pkt.payload[ 127: ])
    if verb_level >= 1:
        print(f"path={sets},max={max_num_files},idx={file_index}")
    d = filesystem.find(sets)
    if d is None:
        raise FailedRequest(SRM_ERRNO_FILE_NOT_FOUND)
    elif isinstance(d, File):
        # A reg. file
        if verb_level >= 1:
            print(f"FILE:{d.lif_name}")
        response = bytearray(struct.pack(">LL", 0, 1))
        response.extend(filesystem.encode_file_info(d))
    else:
        # It's a directory
        cat_info = bytearray()
        if file_index == 0:
            file_index = 1
        file_index -= 1
        last = file_index + min(max_num_files, 8)
        num_files = 0
        for idx, e in enumerate(filesystem.cat_dir(d.path)):
            if file_index <= idx < last:
                cat_info.extend(filesystem.encode_file_info(e))
                num_files += 1
        if verb_level >= 1:
            print(f"{num_files} file(s) returned")
        response = bytearray(struct.pack(">LL", 0, num_files))
        response.extend(cat_info)
    return 0, response

def handle_open(pkt):
    check_volume_handled(pkt.payload[ 11:83 ])
    open_type = struct.unpack(">L", pkt.payload[ 127:131 ])[ 0 ]
    sets, _ = filesystem.decode_filename_sets(pkt.payload[ 83:111 ], pkt.payload[ 131: ])
    if verb_level >= 1:
        print(f"path={sets},ot={open_type}")
    f = filesystem.find(sets)
    if f is None:
        raise FailedRequest(SRM_ERRNO_FILE_NOT_FOUND)
    elif isinstance(f, Directory):
        # It's a directory
        # file_id
        # record_mode
        # max_record_size
        # max_file_size
        # file_code
        # open_logical_eof
        # share_bits
        # sec_ext_size
        # boot_start_address
        file_id = filesystem.get_new_file_id(f, None)
        response = struct.pack(">LLLLlLLLL", file_id, 1, 256, 0, f.get_lif_type(), 0, 0xffffffff, 0, 0);
        if verb_level >= 1:
            print(f"DIR OPENED, id={file_id}")
        return 0, response
    else:
        # It's a plain file
        if open_type == 1 or open_type == 2:
            raise FailedRequest(SRM_ERRNO_FILE_NOT_DIRECTORY)
        file_id = filesystem.open_file(f)
        # file_id
        # record_mode
        # max_record_size
        # max_file_size
        # file_code
        # open_logical_eof
        # share_bits
        # sec_ext_size
        # boot_start_address
        response = struct.pack(">LLLLlLLLL", file_id, 0, 256, 0xffffffff, f.get_lif_type(), f.st_size, 0xffffffff, f.st_size, f.boot_address);
        if verb_level >= 1:
            print(f"FILE OPENED, id={file_id}")
        return 0, response

def handle_close(pkt):
    file_id = struct.unpack(">L", pkt.payload[ 11:15 ])[ 0 ]
    if verb_level >= 1:
        print(f"id={file_id}")
    try:
        file_or_dir, f = filesystem.file_id_to_file(file_id)
    except InvalidFileID:
        raise FailedRequest(SRM_ERRNO_INVALID_FILE_ID)
    if f is not None:
        f.close()
    filesystem.del_file_id(file_id)
    return 0, None

def handle_create(pkt):
    check_volume_handled(pkt.payload[ 11:83 ])
    file_type = struct.unpack(">L", pkt.payload[ 111:115 ])[ 0 ]
    sets, _ = filesystem.decode_filename_sets(pkt.payload[ 83:111 ], pkt.payload[ 151: ])
    if verb_level >= 1:
        print(f"path={sets},file_type={file_type}")
    if file_type == 3:
        # Create a directory
        os.mkdir(filesystem.sets_to_path(sets))
    else:
        # Create a file
        lif_type = file_type & 0xffff
        boot_addr = struct.unpack(">L", pkt.payload[ 139:143 ])[ 0 ]
        path = filesystem.sets_to_file_path(sets, lif_type, boot_addr)
        f = open(path, "wb")
        f.close()
    return 0, None

def get_file_from_id(file_id):
    try:
        file_or_dir, f = filesystem.file_id_to_file(file_id)
    except InvalidFileID:
        raise FailedRequest(SRM_ERRNO_FILE_UNOPENED)
    if isinstance(file_or_dir, Directory):
        raise FailedRequest(SRM_ERRNO_FILE_NOT_FOUND)
    return file_or_dir, f

def handle_write(pkt):
    file_id, access_code = struct.unpack(">LL", pkt.payload[ 15:23 ])
    requested, offset = struct.unpack(">LL", pkt.payload[ 31:39 ])
    if verb_level >= 1:
        print(f"id={file_id},ac={access_code},req={requested},off={offset}")
    file_or_dir, f = get_file_from_id(file_id)
    if access_code == 0:
        f.seek(offset, os.SEEK_SET)
    requested = min(len(pkt.payload) - 47, requested)
    written = f.write(pkt.payload[ 47:47+requested ])
    if verb_level >= 1:
        print(f"written={written}")
    return 0, struct.pack(">L", written)

def handle_position(pkt):
    file_id = struct.unpack(">L", pkt.payload[ 15:19 ])[ 0 ]
    position_type, offset = struct.unpack(">Hl", pkt.payload[ 21:27 ])
    if verb_level >= 1:
        print(f"id={file_id},pt={position_type},off={offset}")
    file_or_dir, f = get_file_from_id(file_id)
    f.seek(offset, os.SEEK_SET if position_type == 0 else os.SEEK_CUR)
    return 0, None

def handle_read(pkt):
    file_id, access_code = struct.unpack(">LL", pkt.payload[ 15:23 ])
    requested, offset = struct.unpack(">LL", pkt.payload[ 31:39 ])
    if verb_level >= 1:
        print(f"id={file_id},ac={access_code},req={requested},off={offset}")
    file_or_dir, f = get_file_from_id(file_id)
    if access_code == 0:
        f.seek(offset, os.SEEK_SET)
    requested = min(512, requested)
    data = f.read(requested)
    if verb_level >= 1:
        print(f"read={len(data)}")
    return 0 if len(data) == requested else SRM_ERRNO_EOF_ENCOUNTERED, struct.pack(">LLLLL", len(data), 0, 0, 0, 0) + data

def handle_seteof(pkt):
    file_id, position_type, offset = struct.unpack(">LLl", pkt.payload[ 15:27 ])
    if verb_level >= 1:
        print(f"id={file_id},pt={position_type},off={offset}")
    file_or_dir, f = get_file_from_id(file_id)
    whence = os.SEEK_SET if position_type == 0 else os.SEEK_CUR
    pos = f.seek(offset, whence)
    f.truncate(pos)
    return 0, None

def handle_fileinfo(pkt):
    file_id = struct.unpack(">L", pkt.payload[ 15:19 ])[ 0 ]
    if verb_level >= 1:
        print(f"id={file_id}")
    try:
        file_or_dir, _ = filesystem.file_id_to_file(file_id)
    except InvalidFileID:
        raise FailedRequest(SRM_ERRNO_INVALID_FILE_ID)
    if verb_level >= 1:
        print(f"sets={file_or_dir.sets}")
    return 0, struct.pack(">L", 0) + filesystem.encode_file_info(file_or_dir)

def handle_purgelink(pkt):
    check_volume_handled(pkt.payload[ 11:83 ])
    sets, _ = filesystem.decode_filename_sets(pkt.payload[ 83:111 ], pkt.payload[ 111: ])
    if verb_level >= 1:
        print(f"path={sets}")
    f = filesystem.find(sets)
    if f is None:
        raise FailedRequest(SRM_ERRNO_FILE_NOT_FOUND)
    elif isinstance(f, Directory):
        os.rmdir(f.path)
    else:
        os.remove(f.path)
    return 0, None

def handle_createlink(pkt):
    check_volume_handled(pkt.payload[ 11:83 ])
    sets_old, n_sets = filesystem.decode_filename_sets(pkt.payload[ 83:111 ], pkt.payload[ 143: ])
    sets_new, _ = filesystem.decode_filename_sets(pkt.payload[ 111:139 ], pkt.payload[ 143: ], n_sets)
    purge_old = struct.unpack(">L", pkt.payload[ 139:143 ])[ 0 ]
    if verb_level >= 1:
        print(f"old path={sets_old},new_path={sets_new},purge={purge_old}")
    f = filesystem.find(sets_old)
    if f is None:
        raise FailedRequest(SRM_ERRNO_FILE_NOT_FOUND)
    else:
        path_old = f.path
        if isinstance(f, Directory):
            path_new = filesystem.sets_to_path(sets_new)
        else:
            path_new = filesystem.sets_to_file_path(sets_new, f.lif_type, f.boot_address)
        if purge_old:
            os.rename(path_old, path_new)
        else:
            os.link(path_old, path_new)
        return 0, None

def handle_changeprotect(pkt):
    # Do nothing, successfully (tm)
    return 0, None

def handle_copyfile(pkt):
    file_id1, off1, file_id2, off2, req = struct.unpack(">LLLLL", pkt.payload[ 11:31 ])
    if verb_level >= 1:
        print(f"id1,off1={file_id1},{off1} id2,off2={file_id2},{off2} req={req}")
    _, f1 = get_file_from_id(file_id1)
    _, f2 = get_file_from_id(file_id2)
    f1.seek(off1, os.SEEK_SET)
    f2.seek(off2, os.SEEK_SET)
    tot_moved = 0
    while req > 0:
        moved = os.sendfile(f2.fileno(), f1.fileno(), None, req)
        if moved <= 0:
            break
        req -= moved
        tot_moved += moved
    if verb_level >= 1:
        print(f"copied={tot_moved}")
    return 0, struct.pack(">L", tot_moved)

def handle_volstatus(pkt):
    vh = VolumeHeader.decode(pkt.payload[ 11:83 ])
    if verb_level >= 1:
        print(f"VOLUME STATUS {str(vh)}")
    if vh.is_handled():
        # srmux = 1
        # exist = 1
        # interleave = 0
        response = struct.pack(">HBBL", 0, 1, 1, 1048576)
        # volume_name
        response += encode_str(VOL_NAME)
        return 0, response
    else:
        raise FailedRequest(SRM_ERRNO_VOLUME_NOT_FOUND)

def handle_reset(pkt):
    dump(pkt.payload)
    # Do nothing..
    # TODO: close?
    return None

def handle_areyoualive(pkt):
    return 0x01000000, None

# 72 srm_volume_header
# 28 srm_file_header
# Key: request code
# Value:
# [0]   Name of request
# [1]   Handler function
# [2]   Minimum length of payload
# [3]   Length of null reply in case of errors
HANDLERS={
    1:    ("WRITE",        handle_write,         47,  4),
    2:    ("POSITION",     handle_position,      27,  0),
    3:    ("READ",         handle_read,          39,  20),
    4:    ("SETEOF",       handle_seteof,        27,  0),
    10:   ("FILEINFO",     handle_fileinfo,      19,  72),
    13:   ("CLOSE",        handle_close,         55,  0),
    14:   ("OPEN",         handle_open,          131, 36),
    15:   ("PURGELINK",    handle_purgelink,     111, 0),
    16:   ("CATALOG",      handle_catalog,       127, 8),
    17:   ("CREATE",       handle_create,        151, 0),
    18:   ("CREATELINK",   handle_createlink,    143, 0),
    19:   ("CHANGEPROTECT",handle_changeprotect, 115, 0),
    22:   ("VOLSTATUS",    handle_volstatus,     83,  24),
    30:   ("COPYFILE",     handle_copyfile,      31,  4),
    1000: ("RESET",        handle_reset,         0,   0),
    1001: ("AREYOUALIVE",  handle_areyoualive,   0,   0)
}

def encode_response(request, sequence_no, status, payload):
    if payload is None:
        payload = b""
    length = 16 + len(payload)
    hdr = struct.pack(">BHlLl", 0, length, -request, sequence_no, status)
    return hdr + payload

ERROR_MAP={
    errno.ENOSPC: SRM_ERRNO_INSUFFICIENT_DISK_SPACE,
    errno.EEXIST: SRM_ERRNO_DUPLICATE_FILENAMES,
    errno.EXDEV: SRM_ERRNO_RENAME_ACROSS_VOLUMES,
    errno.ENOENT: SRM_ERRNO_FILE_NOT_FOUND,
    errno.EPERM: SRM_ERRNO_ACCESS_TO_FILE_NOT_ALLOWED,
    errno.EACCES: SRM_ERRNO_ACCESS_TO_FILE_NOT_ALLOWED,
    errno.EISDIR: SRM_ERRNO_FILE_NOT_FOUND,
    errno.ENOTDIR: SRM_ERRNO_FILE_NOT_FOUND,
    errno.EIO: SRM_ERRNO_VOLUME_IO_ERROR,
    errno.EINVAL: SRM_ERRNO_VOLUME_IO_ERROR,
    errno.ENOTEMPTY: SRM_ERRNO_DIRECTORY_NOT_EMPTY
}

# On input to handler function:
# pkt.payload starts at offset 6 (1st byte of message_length field)
# pkt.payload[ 11: ] is the part after user_sequencing_field
# On ouput "response" starts at offset 21 (1st byte after status field)
def process_req(pkt):
    if len(pkt.payload) < 11:
        print(f"Request packet too short ({len(pkt.payload)})")
        return None
    else:
        request, sequence_no = struct.unpack(">LL", pkt.payload[ 3:11 ])
        if request not in HANDLERS:
            print(f"Unknown request {request}, level={pkt.level}")
            dump(pkt.payload)
            return encode_response(request, sequence_no, SRM_ERRNO_VOLUME_IO_ERROR, None)
        else:
            try:
                req = HANDLERS[ request ]
                if verb_level >= 1:
                    print(req[ 0 ])
                if len(pkt.payload) < req[ 2 ]:
                    print(f"Payload too short ({len(pkt.payload)} < {req[ 2 ]})")
                    return None
                try:
                    response = req[ 1 ](pkt)
                    if response is None:
                        return None
                    else:
                        status, payload = response
                        return encode_response(request, sequence_no, status, payload)
                except OSError as e:
                    raise FailedRequest(ERROR_MAP.get(e.errno, SRM_ERRNO_SOFTWARE_BUG))
            except MyException as e:
                print(str(e))
                return None
            except FailedRequest as e:
                print(f"Failed, err_code={e.err_code}")
                dump(pkt.payload)
                return encode_response(request, sequence_no, e.err_code, bytes(req[ 3 ]))

async def serve_srm(m_rd , m_wr):
    try:
        print("Connected!")
        connected_addr = None
        nr = 0
        ns = 0
        # 0     Idle
        # 1     Waiting for UA
        # 2     Waiting for RR
        fsm = 0
        sdlc = SDLC_IO(my_addr, m_rd, m_wr)
        park = None
        async for msg in sdlc.get_rx_msg():
            while True:
                if isinstance(msg, RawPacket):
                    pkt = Packet.decode(msg)
                    if verb_level >= 2:
                        print(f">{str(pkt)}")
                    if isinstance(pkt, IPacket):
                        if fsm == 0:
                            if pkt.sa == connected_addr:
                                if pkt.ns == nr:
                                    nr = (nr + 1) % 8
                                    rr = RRPacket(my_addr, pkt.sa, 0, 0, nr)
                                    await sdlc.tx(rr)
                                    response = process_req(pkt)
                                    if response is not None:
                                        tmp = ns
                                        ns = (ns + 1) % 8
                                        r_pkt = IPacket(my_addr, pkt.sa, 0, 7, nr, tmp, response)
                                        await sdlc.tx(r_pkt)
                                        fsm = 2
                                # TODO: Check for repeated I
                                else:
                                    # NAK
                                    print(f"NAK: exp N(S)={nr}, act N(S)={pkt.ns}")
                                    rr = RRPacket(my_addr, pkt.sa, 0, 0, nr)
                                    await sdlc.tx(rr)
                            else:
                                # Not connected, send SABM and wait for UA
                                sabm = SABMPacket(my_addr, pkt.sa)
                                await sdlc.tx(sabm)
                                fsm = 1
                                wait_addr = pkt.sa
                        elif fsm == 1 or fsm == 2:
                            park = msg
                    elif isinstance(pkt, UAPacket):
                        if pkt.sa == wait_addr and fsm == 1:
                            connected_addr = pkt.sa
                        else:
                            print("Unexpected UA packet")
                        fsm = 0
                        if park:
                            msg = park
                            park = None
                            continue
                    elif isinstance(pkt, SABMPacket):
                        connected_addr= pkt.sa
                        nr = 0
                        ns = 0
                        ua = UAPacket(my_addr, pkt.sa)
                        await sdlc.tx(ua)
                        fsm = 0
                        if park:
                            msg = park
                            park = None
                            continue
                    elif isinstance(pkt, RRPacket):
                        if fsm == 2 and pkt.sa == connected_addr:
                            if pkt.nr != ns:
                                print(f"Mismatch between expected N(R) ({ns}) and received N(R) ({pkt.nr})")
                        else:
                            print("Unexpected RR packet")
                        fsm = 0
                        if park:
                            msg = park
                            park = None
                            continue
                    elif isinstance(pkt, RCPacket):
                        response = process_req(pkt)
                        if response is not None:
                            r_pkt = RCRPacket(my_addr, pkt.sa, 0, 5, response)
                            await sdlc.tx(r_pkt)
                    elif isinstance(pkt, BadPacket) and pkt.payload is not None:
                        dump(pkt.payload)
                break

        print("Gone!")
    except ConnectionError:
        print("Connection error")

async def main(port, address, top_dir):
    global my_addr
    my_addr = address
    global filesystem
    filesystem = FS(top_dir)

    print(f"{port=},{address=},{top_dir=}")
    print("Connecting..")
    server = await asyncio.start_server(serve_srm, '127.0.0.1', port)

    async with server:
        await server.serve_forever()

def listen_port(arg):
    port = int(arg)
    if not 1 <= port <= 65535:
        raise ValueError()
    return port

def sdlc_address(arg):
    addr = int(arg)
    if not 0 <= addr <= 63:
        raise ValueError()
    return addr

def parse_cl():
    parser = argparse.ArgumentParser(description = "A server for SRM file I/O")
    parser.add_argument("--port", help="TCP port where to listen (default: %(default)s)", default = 1235, type = listen_port)
    parser.add_argument("--addr", help="SDLC address of server (default: %(default)s)", default = 0, type = sdlc_address)
    parser.add_argument("top", nargs='?', help="Top directory (default: %(default)s)", default = "SRM")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument('--verbose', '-v', action='count', default=0)
    args = parser.parse_args()
    global verb_level
    verb_level = args.verbose
    return args.port, args.addr, args.top

if __name__ == '__main__':
    try:
        port, address, top_dir = parse_cl()
        asyncio.run(main(port, address, top_dir))
    except KeyboardInterrupt:
        print("Interrupted!")
