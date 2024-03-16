#!/usr/bin/env python3
# A GUI based emulator of HP Amigo drives for use with MAME IEEE-488 remotizer
# Copyright (C) 2020-2024 F. Ulivi <fulivi at big "G" mail>
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

from PyQt6 import QtCore
import rem488
import threading
import struct
from collections import namedtuple

UnitSpec = namedtuple("UnitSpec", [ "geometry", "fixed" , "ignore_fmt", "unit_desc", "vol_il" ])
DriveModel = namedtuple("DriveModel" , [ "name", "id", "protocol", "cont_desc", "unit_specs" ])

class CHSOutOfRange(Exception):
    def __init__(self, chs , max_chs):
        self.chs = chs
        self.max_chs = chs

    def __str__(self):
        return "({},{},{}) out of range, max = ({},{},{})".format(self.chs[ 0 ] , self.chs[ 1 ] , self.chs[ 2 ] , self.max_chs[ 0 ] - 1 ,  self.max_chs[ 1 ] - 1 ,  self.max_chs[ 2 ] - 1)

class LBAOutOfRange(Exception):
    def __init__(self, lba , max_lba):
        self.lba = lba
        self.max_lba = lba

    def __str__(self):
        return "LBA {} out of range, max = {}".format(self.lba , self.max_lba)

class Geometry:
    def __init__(self, chs):
        self.max_chs = chs
        self.max_lba = chs[ 0 ] * chs[ 1 ] * chs[ 2 ]

    def chs_to_lba(self, chs):
        c, h, s = self.max_chs
        if 0 <= chs[ 0 ] < c and\
           0 <= chs[ 1 ] < h and\
           0 <= chs[ 2 ] < s:
            return (chs[ 0 ] * h + chs[ 1 ]) * s + chs[ 2 ]
        else:
            raise CHSOutOfRange(chs , self.max_chs)

    def lba_to_chs(self, lba):
        if lba < 0 or lba >= self.max_lba:
            raise LBAOutOfRange(lba , self.max_lba)
        tmp , s = divmod(lba , self.max_chs[ 2 ])
        c , h = divmod(tmp , self.max_chs[ 1 ])
        return (c , h , s)

#                             geometry      fixed  ignore_fmt unit_desc vol_il
UNIT9885 = UnitSpec(Geometry(( 77, 2, 30)), False, False,     None,     None)
UNIT9134 = UnitSpec(Geometry((306, 4, 31)), True,  True,      None,     None)
UINT82901= UnitSpec(Geometry(( 33, 2, 16)), False, False,     None,     None)
UNIT9122 = UnitSpec(Geometry(( 77, 2, 16)), False, False,\
                    b"\x01\x09\x12\x21\x01\x00\x01\x00\x17\x00\x00\x2d\x11\x94\x20\xd0\x0f\x00\x01",\
                    2 )

DRIVE_MODELS = [
    #          name     id           protocol cont_desc
    DriveModel("9895" , b"\x00\x81", "Amigo", None, [ UNIT9885, UNIT9885 ]),
    DriveModel("9134b", b"\x01\x0a", "Amigo", None, [ UNIT9134 ]),
    DriveModel("82901", b"\x01\x04", "Amigo", None, [ UINT82901, UINT82901 ]),
    #          name     id           protocol cont_desc
    DriveModel("9122d", b"\x02\x22", "SS/80", b"\x80\x03\x00\x64\x05", [ UNIT9122, UNIT9122 ])
]

class AmigoUnitState:
    def __init__(self, geometry, ignore_fmt):
        self.geometry = geometry
        self.bps = 256
        self.ignore_fmt = ignore_fmt
        self.current_lba = 0
        self.rd_counter = 0
        self.wr_counter = 0
        self.a_bit = False
        self.c_bit = False
        self.image = None
        self.read_only = False
        self.unload_image()

    def load_image(self , image_file):
        try:
            self.rd_counter = 0
            self.wr_counter = 0
            self.image = open(image_file , "rb" if self.read_only else "r+b")
            self.f_bit = True
            self.ss = 0
            self.tttt = 6
            return 0
        except OSError as e:
            self.image = None
            self.unload_image()
            return e.errno

    def unload_image(self):
        if self.image:
            self.image.close()
            self.image = None
        self.f_bit = False
        self.ss = 3
        self.tttt = 0

    def set_read_only(self , read_only):
        if self.image == None:
            self.read_only = read_only

    def is_ready(self):
        return self.image != None

    def set_current_chs(self, chs):
        try:
            self.current_lba = self.geometry.chs_to_lba(chs)
        except CHSOutOfRange:
            self.current_lba = self.geometry.max_lba
            raise

    def is_lba_ok(self):
        return 0 <= self.current_lba < self.geometry.max_lba

    def get_current_chs(self):
        return self.geometry.lba_to_chs(self.current_lba)

    def write_img(self, data):
        if self.is_ready() and not self.read_only:
            self.image.seek(256 * self.current_lba)
            self.image.write(data)
            self.current_lba += 1
            self.wr_counter += 1

    def read_img(self):
        if self.is_ready():
            self.image.seek(256 * self.current_lba)
            data = bytearray(self.image.read(256))
            if len(data) < 256:
                data.extend(bytes(256 - len(data)))
            self.current_lba += 1
            self.rd_counter += 1
        else:
            data = bytearray(256)
        return data

    def format_img(self, filler):
        if self.is_ready() and not self.read_only:
            self.image.seek(0)
            fill = bytes([ filler ] * 256)
            for x in range(self.geometry.max_lba):
                self.image.write(fill)
            self.wr_counter += self.geometry.max_lba

class AmigoDriveState:
    def __init__(self , io , fixed_data):
        self.io = io
        self.io.disable_unlisten_sa()
        self.fixed_data = fixed_data
        self.dsj = 2
        self.stat1 = 0
        self.pp_enabled = True
        self.pp_state = False
        self.buffer_ = bytearray(256)
        self.status = bytearray(4)
        self.units = [ AmigoUnitState(u.geometry, u.ignore_fmt) for u in fixed_data.unit_specs ]
        self.n_units = len(self.units)
        self.current_unit = 0
        self.failed_unit = 0
        # 0     Idle
        # 1     Wait for send addr/status
        # 2     Wait for send data
        # 3     Wait for receive data
        # 4     Wait for device clear
        # 5     Wait for CP in unbuffered reading
        # 6     Wait for receive data in unbuffered writing
        self.set_seq_state(0)

    def set_pp(self, new_pp_state):
        new_state = self.pp_enabled and new_pp_state
        if new_state != self.pp_state:
            self.pp_state = new_state
            self.io.send_pp_state(self.pp_state)

    def is_dsj_ok(self):
        return self.dsj != 2

    def select_unit(self, unit):
        if unit < self.n_units:
            self.current_unit = unit
            return self.units[ self.current_unit ]
        else:
            self.set_error(0x17)
            return None

    def select_unit_check_f(self, unit):
        unit = self.select_unit(unit)
        if unit and (unit.f_bit or not unit.is_ready()):
            self.set_error(0x13)
            unit = None
        return unit

    def set_error(self, new_stat1):
        self.stat1 = new_stat1
        self.failed_unit = self.current_unit
        if self.dsj != 2:
            self.dsj = 1

    def send_end_byte(self):
        self.io.talk_data(b"\x01" , True)

    def send_checkpoint(self):
        self.io.send_checkpoint()

    def set_seq_state(self, state):
        self.cmd_seq_state = state

    def set_seq_error(self , talker):
        self.set_seq_state(0)
        if self.dsj == 0:
            # I/O error
            self.set_error(10)
        if talker:
            self.send_end_byte()

    def require_seq_state(self, req_state, talker):
        if self.cmd_seq_state != req_state and ((self.cmd_seq_state != 5 and self.cmd_seq_state != 6) or req_state != 0):
            self.set_seq_error(talker)
            return False
        else:
            self.cmd_seq_state = req_state
            return True

    def dsj1_holdoff(self):
        return self.dsj == 1 and self.stat1 != 1 and self.stat1 != 10

    def lba_out_of_range(self):
        unit = self.units[ self.current_unit ]
        if not unit.is_lba_ok():
            unit.a_bit = True
            unit.c_bit = True
            self.set_error(0x1f)
            return True
        else:
            return False

    def clear_dsj(self):
        if self.dsj != 2:
            self.dsj = 0

    def clear_errors(self):
        self.stat1 = 0
        self.dsj = 0

    def check_write_ok(self):
        unit = self.units[ self.current_unit ]
        if unit.read_only:
            self.set_error(0x13)
            return False
        else:
            return True

    # Commands
    def cmd_rx_data(self, c):
        if self.cmd_seq_state != 3 and self.cmd_seq_state != 6:
            self.set_seq_error(False)
        elif self.lba_out_of_range():
            self.set_seq_state(0)
        else:
            unit = self.units[ self.current_unit ]
            chs = unit.get_current_chs()
            self.buffer_[ :len(c.data) ] = c.data
            unit.write_img(self.buffer_)
            self.clear_errors()
            if self.cmd_seq_state == 3:
                self.set_seq_state(0)

    def cmd_seek(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.data[ 1 ])
            if unit:
                self.set_error(0x1f)
                unit.a_bit = True
                try:
                    chs = (c.data[ 2 ] * 256 + c.data[ 3 ] , c.data[ 4 ] , c.data[ 5 ])
                    unit.set_current_chs(chs)
                    self.clear_dsj()
                except CHSOutOfRange:
                    unit.c_bit = True

    def cmd_req_status(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            if c.data[ 1 ] < self.n_units:
                self.status[ 0 ] = self.stat1
                self.status[ 1 ] = self.failed_unit
                self.current_unit = c.data[ 1 ]
                unit = self.units[ self.current_unit ]
                self.status[ 2 ] = unit.tttt << 1
                if unit.c_bit or unit.ss != 0:
                    self.status[ 2 ] |= 0x80
                res = unit.ss
                if unit.a_bit:
                    res |= 0x80
                if unit.read_only:
                    res |= 0x40
                if unit.f_bit:
                    res |= 0x08
                if unit.c_bit:
                    res |= 0x04
                self.status[ 3 ] = res
            else:
                # Invalid unit number
                self.status[ 0 ] = 0x17
                self.status[ 1 ] = c.data[ 1 ]
                self.status[ 2 ] = 0
                self.status[ 3 ] = 0
                unit = self.units[ self.current_unit ]
            unit.a_bit = False
            unit.f_bit = False
            unit.c_bit = False
            self.clear_errors()
            self.set_seq_state(1)

    def cmd_verify(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.data[ 1 ])
            if unit:
                sec_count = c.data[ 2 ] * 256 + c.data[ 3 ]
                if sec_count == 0:
                    # Verify to end of disk
                    unit.current_lba = unit.geometry.max_lba
                else:
                    new_lba = min(unit.geometry.max_lba , unit.current_lba + sec_count)
                    unit.current_lba = new_lba
                self.clear_errors()

    def cmd_initialize(self, c):
        # TODO:
        pass

    def cmd_set_addr_rec(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.data[ 1 ])
            if unit:
                self.set_error(0x1f)
                unit.a_bit = True
                try:
                    chs = (c.data[ 2 ] * 256 + c.data[ 3 ] , c.data[ 4 ] , c.data[ 5 ])
                    unit.set_current_chs(chs)
                    self.clear_dsj()
                except CHSOutOfRange:
                    unit.c_bit = True

    def cmd_download(self, c):
        self.set_seq_error(False)

    def cmd_req_log_addr(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            chs = self.units[ self.current_unit ].get_current_chs()
            self.status[ 0 ] = chs[ 0 ] // 256
            self.status[ 1 ] = chs[ 0 ] % 256
            self.status[ 2 ] = chs[ 1 ]
            self.status[ 3 ] = chs[ 2 ]
            self.clear_errors()
            self.set_seq_state(1)

    def cmd_end(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            # Not entirely correct
            self.clear_errors()
            self.pp_enabled = False

    def cmd_write(self, c, seq_state):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.data[ 1 ])
            if unit and not self.dsj1_holdoff() and not self.lba_out_of_range() and self.check_write_ok():
                self.set_seq_state(seq_state)

    def cmd_buff_wr(self, c):
        self.cmd_write(c , 3)

    def cmd_unbuff_wr(self, c):
        self.cmd_write(c , 6)

    def cmd_read(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.data[ 1 ])
            if unit and not self.dsj1_holdoff() and not self.lba_out_of_range():
                chs = unit.get_current_chs()
                self.buffer_ = unit.read_img()
                self.clear_errors()
                self.set_seq_state(2)

    def cmd_buff_rd(self, c):
        self.unbuffered = False
        self.cmd_read(c)

    def cmd_unbuff_rd(self, c):
        self.unbuffered = True
        self.cmd_read(c)

    def cmd_format(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.data[ 1 ])
            if unit and self.check_write_ok():
                if not unit.ignore_fmt or (c.data[ 2 ] & 0x80) != 0:
                    unit.format_img(0xff if unit.ignore_fmt else c.data[ 4 ])
                unit.current_lba = 0
                self.clear_errors()

    def cmd_tx_data(self, c):
        if self.require_seq_state(2 , True):
            self.io.talk_data(self.buffer_)
            self.send_checkpoint()
            if self.unbuffered:
                self.set_seq_state(5)
                self.pp_enabled = False
            else:
                self.set_seq_state(0)

    def cmd_cp_reached(self, c):
        if self.cmd_seq_state == 5:
            if c.flushed:
                self.set_seq_state(0)
                self.pp_enabled = True
            else:
                unit = self.units[ self.current_unit ]
                if unit.is_lba_ok():
                    chs = unit.get_current_chs()
                    self.buffer_ = unit.read_img()
                    self.io.talk_data(self.buffer_)
                    self.send_checkpoint()
                    self.pp_enabled = False
                else:
                    self.send_end_byte()
                    self.send_checkpoint()
                    self.set_seq_state(0)
                    self.pp_enabled = True
            self.set_pp(True)

    def cmd_tx_status(self, c):
        if self.require_seq_state(1 , True):
            self.io.talk_data(self.status)
            # Add a 0x01 byte with EOI
            self.send_end_byte()
            self.send_checkpoint()
            self.set_seq_state(0)

    def cmd_dsj(self, c):
        if self.require_seq_state(0 , True):
            self.io.talk_data([ self.dsj ] , True)
            self.send_checkpoint()
            if self.dsj == 2:
                self.dsj = 0
        self.pp_enabled = False
        self.set_seq_state(0)

    def cmd_amigo_clear(self, c):
        if self.require_seq_state(0 , False):
            self.set_seq_state(4)
            self.pp_enabled = False

    def cmd_dev_clear(self, c):
        self.set_seq_state(0)
        self.clear_errors()
        for u in self.units:
            u.a_bit = False
            u.f_bit = False
            u.c_bit = False
            u.current_lba = 0
        self.current_unit = 0
        self.pp_enabled = True
        self.set_pp(True)

    def cmd_identify(self, c):
        self.io.talk_data(self.fixed_data.id , True)

    def cmd_parallel_poll(self, c):
        self.set_pp(not c.addressed)

    def cmd_unknown_listen(self, c):
        self.set_error(10)
        self.set_seq_state(0)

    def cmd_unknown_talk(self, c):
        self.send_end_byte()
        self.set_error(10)
        self.set_seq_state(0)

    # Listen command decoding table
    # Fields in key tuple:
    # [ 0 ]     Secondary address
    # [ 1 ]     Length of parameters
    # [ 2 ]     Opcode
    LISTEN_CMDS = {
        (   8 ,   6 ,    2) : cmd_seek,
        (   8 ,   2 ,    3) : cmd_req_status,
        (   8 ,   2 ,    5) : cmd_unbuff_rd,
        (   8 ,   4 ,    7) : cmd_verify,
        (   8 ,   2 ,    8) : cmd_unbuff_wr,
        (   8 ,   2 , 0x0b) : cmd_initialize,
        (   8 ,   6 , 0x0c) : cmd_set_addr_rec,
        (   8 ,   2 , 0x14) : cmd_req_log_addr,
        (   8 ,   2 , 0x15) : cmd_end,
        (   9 ,   2 ,    8) : cmd_buff_wr,
        (0x0a ,   2 ,    3) : cmd_req_status,
        (0x0a ,   2 ,    5) : cmd_buff_rd,
        (0x0a ,   2 , 0x14) : cmd_req_log_addr,
        (0x0b ,   2 ,    5) : cmd_buff_rd,
        (0x0c ,   5 , 0x18) : cmd_format
    }

    # Talk command decoding table
    # Secondary address is key
    TALK_CMDS = {
           0 : cmd_tx_data,
           8 : cmd_tx_status,
        0x10 : cmd_dsj
    }

    def process_ev(self, ev):
        if isinstance(ev , rem488.RemotizerData):
            len_params = len(ev.data)
            # Match receive data cmd
            if len_params > 0 and ev.sec_addr == 0:
                cmd = AmigoDriveState.cmd_rx_data
            elif ev.sec_addr == 0x10 and len_params == 1:
                cmd = AmigoDriveState.cmd_amigo_clear
            else:
                key_tuple = (ev.sec_addr , len_params , ev.data[ 0 ] if (len_params > 0) else None)
                cmd = AmigoDriveState.LISTEN_CMDS.get(key_tuple, AmigoDriveState.cmd_unknown_listen)
        elif isinstance(ev , rem488.RemotizerTalk):
            cmd = AmigoDriveState.TALK_CMDS.get(ev.sec_addr, AmigoDriveState.cmd_unknown_talk)
        elif isinstance(ev , rem488.RemotizerIdentify):
            cmd = AmigoDriveState.cmd_identify
        elif isinstance(ev , rem488.RemotizerAddressed):
            cmd = AmigoDriveState.cmd_parallel_poll
        elif isinstance(ev , rem488.RemotizerDevClear):
            cmd = AmigoDriveState.cmd_dev_clear
        elif isinstance(ev , rem488.RemotizerCPReached):
            cmd = AmigoDriveState.cmd_cp_reached
        else:
            return
        is_tl_cmd = isinstance(ev , rem488.RemotizerData) or isinstance(ev , rem488.RemotizerTalk)
        if is_tl_cmd:
            self.pp_enabled = True
        cmd(self, ev)
        if is_tl_cmd:
            self.set_pp(True)

class SS80BaseUnitState:
    def __init__(self):
        # dec_state
        # 0     --
        # 1     cmd locate & read: reading
        # 2     cmd locate & write: writing
        # 3     describe
        # 4     download (N/U)
        # 5     request status
        # 6     read loopback
        # 7     write loopback
        # 8     validate key
        # 9     set format options
        # 10    cmd ends w/o pp enable
        # 11    idle
        pass

    def clear_status(self):
        self.status_bits = bytearray(8)
        self.parameter = bytearray(10)
        self.qstat = 0

    def clear_status_mask(self):
        self.clear_status()
        self.mask_bits = bytearray(8)
        self.target_length = 0xffff_ffff

    def clear_unit(self):
        if self.test_bit(self.status_bits, 24):
            # Clear error 30
            self.clear_status_bit(30)
            self.qstat = 1
        else:
            self.clear_status()
        self.mask_bits = bytearray(8)
        self.target_length = 0xffff_ffff
        self.dec_state = 10

    def cancel(self):
        self.clear_status_bit(10)
        self.clear_status_bit(12)
        if self.qstat != 2:
            if any(self.status_bits):
                self.qstat = 1
            else:
                self.qstat = 0
        self.dec_state = 11

    def mask_idx(self, bit_no):
        return bit_no // 8, 0x80 >> (bit_no % 8)

    def test_bit(self, bits, bit_no):
        idx, mask = self.mask_idx(bit_no)
        return (bits[ idx ] & mask) != 0

    def set_status_bit(self, bit_no):
        if not self.test_bit(self.mask_bits, bit_no) and\
           (bit_no != 10 or not any(self.status_bits[ 0:6 ])):
                idx, mask = self.mask_idx(bit_no)
                self.status_bits[ idx ] |= mask
                if bit_no == 30:
                    self.qstat = 2
                    self.holdoff = True
                elif self.qstat != 2:
                    self.qstat = 1
        self.dec_state = 11

    def clear_status_bit(self, bit_no):
        idx, mask = self.mask_idx(bit_no)
        self.status_bits[ idx ] &= ~mask

class SS80UnitState(SS80BaseUnitState):
    def __init__(self, geometry, unit_desc, vol_il):
        super().__init__()
        self.geometry = geometry
        self.unit_desc = unit_desc
        self.bps = struct.unpack(">H", unit_desc[ 4:6 ])[ 0 ]
        self.vol_il = vol_il
        # AKA target address
        self.current_lba = 0
        self.rd_counter = 0
        self.wr_counter = 0
        self.image = None
        self.new = False
        self.read_only = False
        self.unload_image()

    def get_current_chs(self):
        return self.geometry.lba_to_chs(self.current_lba)

    def load_image(self, image_file):
        try:
            self.rd_counter = 0
            self.wr_counter = 0
            self.image = open(image_file , "rb" if self.read_only else "r+b")
            if not self.test_bit(self.status_bits, 30):
                self.new = True
            return 0
        except OSError as e:
            self.image = None
            self.unload_image()
            return e.errno

    def unload_image(self):
        if self.image:
            self.image.close()
            self.image = None
        self.new = False

    def set_read_only(self, read_only):
        if self.image is None:
            self.read_only = read_only

    def is_ready(self):
        return self.image is not None

    def write_img(self, data):
        if self.is_ready() and not self.read_only:
            self.image.seek(self.bps * self.current_lba)
            self.image.write(data)
            self.current_lba += 1
            if self.current_lba == self.geometry.max_lba:
                self.current_lba = 0
            self.wr_counter += 1

    def read_img(self):
        if self.is_ready():
            self.image.seek(self.bps * self.current_lba)
            data = bytearray(self.image.read(self.bps))
            if len(data) < self.bps:
                data.extend(bytes(self.bps - len(data)))
            self.current_lba += 1
            if self.current_lba == self.geometry.max_lba:
                self.current_lba = 0
            self.rd_counter += 1
        else:
            # Should never get here
            data = None
        return data

    def format_img(self):
        self.image.seek(0)
        fill = bytes(self.bps)
        for x in range(self.geometry.max_lba):
            self.image.write(fill)
        self.wr_counter += self.geometry.max_lba

class SS80DriveState:
    def __init__(self, io, fixed_data):
        self.io = io
        self.io.set_unlisten_sa([ 0x0e, 0x12 ])
        self.fixed_data = fixed_data
        self.units = [ SS80UnitState(u.geometry, u.unit_desc, u.vol_il) for u in fixed_data.unit_specs ]
        self.n_units = len(self.units)
        self.unit15 = SS80BaseUnitState()
        self.srq_enabled = False
        self.unit15.clear_status_mask()
        self.unit15.set_status_bit(30)
        for u in self.units:
            u.clear_status_mask()
            u.set_status_bit(30)
        self.select_unit(0)
        self.cp_reached_handler = None
        self.pp_enabled = True
        self.pp_state = False
        self.set_pp(True)

    def device_clear(self):
        self.unit15.clear_unit()
        for u in self.units:
            u.clear_unit()
            u.current_lba = 0
        self.select_unit(0)

    def set_pp(self, new_pp_state):
        new_state = self.pp_enabled and new_pp_state
        if new_state != self.pp_state:
            self.pp_state = new_state
            self.io.send_pp_state(self.pp_state)
            if not new_state:
                self.io.set_rsv_state(False)
            elif self.srq_enabled:
                self.io.set_rsv_state(True)

    def cmd_identify(self):
        self.io.talk_data(self.fixed_data.id , True)

    def cmd_parallel_poll(self, ev):
        self.set_pp(not ev.addressed)

    class Error(Exception):
        def __init__(self, err_no):
            self.err_no = err_no

    def select_unit(self, u):
        if u == 15:
            self.c_unit = self.unit15
            self.c_unit_no = 15
        elif u < self.n_units:
            self.c_unit = self.units[ u ]
            self.c_unit_no = u
        else:
            raise self.Error(6)

    def check_listen_data(self, ev):
        if len(ev.data) > 50 or not ev.end:
            raise self.Error(12)

    def check_not_unit15(self):
        if self.c_unit is self.unit15:
            raise self.Error(5)

    def collect_bytes(self, gen, n):
        res = bytearray()
        try:
            for x in range(n):
                res.append(next(gen))
            return res
        except StopIteration:
            raise self.Error(9)

    def check_end_seq(self, gen, n):
        if n > 0:
            res = self.collect_bytes(gen, n)
        else:
            res = None
        try:
            # This "next" should fail (sequence should stop here)
            b = next(gen)
        except StopIteration:
            return res
        raise self.Error(9)

    def not_unit15_end_seq(self, gen, n):
        self.check_not_unit15()
        return self.check_end_seq(gen, n)

    def check_new_not_ready(self):
        if not self.c_unit.is_ready():
            # Not ready
            raise self.Error(35)
        if self.c_unit.new:
            self.c_unit.new = False
            # Power fail
            raise self.Error(30)

    def check_not_read_only(self):
        if self.c_unit.read_only:
            raise self.Error(36)

    def dec_cmd_locate_read(self, gen):
        self.not_unit15_end_seq(gen, 0)
        self.check_new_not_ready()
        self.c_unit.dec_state = 11 if self.c_unit.target_length == 0 else 1

    def dec_cmd_locate_write(self, gen):
        self.not_unit15_end_seq(gen, 0)
        self.check_new_not_ready()
        self.check_not_read_only()
        if self.c_unit.target_length != 0:
            self.c_unit.dec_state = 2
            self.c_unit.first_0e = True
            self.len_op = self.c_unit.target_length
            self.c_unit.accum_0e = bytearray()
        else:
            self.c_unit.dec_state = 11

    def cmd_write(self, ev):
        if self.c_unit.first_0e:
            self.c_unit.first_0e = False
            self.check_new_not_ready()
            self.check_not_read_only()
        self.c_unit.accum_0e.extend(ev.data)
        mv = memoryview(self.c_unit.accum_0e)
        idx = 0
        rem = len(mv)
        while True:
            if self.len_op > self.c_unit.bps:
                min_len = self.c_unit.bps
                exp_end = False
            else:
                min_len = self.len_op
                exp_end = True
            if not ev.end and not ev.unlistened and rem < min_len:
                if idx > 0:
                    self.c_unit.accum_0e = self.c_unit.accum_0e[ idx: ]
                self.pp_enabled = False
                break
            taken = min(rem, self.c_unit.bps)
            if taken != min_len:
                raise self.Error(12)
            eoi_end = ev.end and rem <= self.c_unit.bps
            if exp_end and not eoi_end:
                raise self.Error(12)
            if taken == self.c_unit.bps:
                self.c_unit.write_img(mv[ idx:(idx+taken) ])
            else:
                data = bytearray(mv[ idx: ])
                fill = bytearray(self.c_unit.bps - taken)
                data.extend(fill)
                self.c_unit.write_img(data)
            idx += taken
            rem -= taken
            if self.c_unit.current_lba == 0:
                if exp_end or self.c_unit.target_length == 0xffff_ffff:
                    self.c_unit.dec_state = 11
                    break
                else:
                    raise self.Error(44)
            elif exp_end:
                self.c_unit.dec_state = 11
                break
            else:
                self.len_op -= self.c_unit.bps

    def dec_cmd_locate_verify(self, gen):
        self.not_unit15_end_seq(gen, 0)
        self.check_new_not_ready()
        if self.c_unit.target_length == 0xffff_ffff:
            self.c_unit.current_lba = 0
        elif self.c_unit.target_length != 0:
            sects = (self.c_unit.target_length + self.c_unit.bps - 1) // self.c_unit.bps
            max_sects = self.c_unit.geometry.max_lba - self.c_unit.current_lba
            if sects >= max_sects:
                self.c_unit.current_lba = 0
            else:
                self.c_unit.current_lba += sects
            if sects > max_sects:
                raise self.Error(44)
        self.c_unit.dec_state = 11

    def dec_cmd_spare_block(self, gen):
        dummy = self.not_unit15_end_seq(gen, 1)
        self.check_new_not_ready()
        raise self.Error(34)

    def dec_cmd_request_status(self, gen):
        self.check_end_seq(gen, 0)
        # In real hw errors 17, 24, 41, 58, 59 would be checked here (these errors
        # have something that's not current target address in parameter field)
        self.c_unit.parameter[ 0 ] = 0
        self.c_unit.parameter[ 1 ] = 0
        addr = 0 if self.c_unit is self.unit15 else self.c_unit.current_lba
        self.c_unit.parameter[ 2:6 ] = struct.pack(">L", addr)
        self.c_unit.dec_state = 5

    def dec_cmd_release(self, gen):
        self.check_end_seq(gen, 0)

    def dec_cmd_release_denied(self, gen):
        self.check_end_seq(gen, 0)

    CMDS = {
        0x00: dec_cmd_locate_read,
        0x02: dec_cmd_locate_write,
        0x04: dec_cmd_locate_verify,
        0x06: dec_cmd_spare_block,
        0x0d: dec_cmd_request_status,
        0x0e: dec_cmd_release,
        0x0f: dec_cmd_release_denied
    }

    def decode_cmd_0x(self, gen, b):
        fn = self.CMDS.get(b)
        if fn:
            fn(self, gen)
        else:
            raise self.Error(5)

    def cmd_validate_key(self, ev):
        self.check_new_not_ready()
        if len(ev.data) != 12 or not ev.end:
            raise self.Error(12)
        else:
            # Key is always valid :)
            self.c_unit.dec_state = 11

    def cmd_set_format_options(self, ev):
        raise self.Error(8)

    def decode_cmd_3x(self, gen, b):
        if b == 0x31:
            cmd = self.collect_bytes(gen, 2)
            if cmd[ 0 ] == 0xf1 and cmd[ 1 ] == 0x02:
                # VALIDATE KEY
                self.not_unit15_end_seq(gen, 0)
                self.check_new_not_ready()
                self.c_unit.dec_state = 8
            elif cmd[ 0 ] == 0xf3 and cmd[ 1 ] == 0x5f:
                # SET FORMAT OPTIONS
                self.not_unit15_end_seq(gen, 0)
                self.c_unit.dec_state = 9
            else:
                raise self.Error(8)
        elif b == 0x33:
            # INITIATE DIAGNOSTIC
            code = self.check_end_seq(gen, 3)
            if code[ 0 ] != 0 or code[ 1 ] != 1 or code[ 2 ] != 0:
                raise self.Error(8)
            self.c_unit.dec_state = 11
        elif b == 0x35:
            # DESCRIBE
            self.check_end_seq(gen, 0)
            self.c_unit.dec_state = 3
        elif b == 0x37:
            # INITIALIZE MEDIA
            params = self.not_unit15_end_seq(gen, 2)
            try:
                self.check_new_not_ready()
                self.check_not_read_only()
                self.c_unit.format_img()
            finally:
                self.c_unit.dec_state = 11
        else:
            raise self.Error(5)

    def listen_05(self, ev):
        d = iter(ev.data)
        try:
            b = next(d)
            if b == 0x34:
                # 34: NOP
                b = next(d)
            if (b & 0xf0) == 0x20:
                # 2x: select unit x
                self.select_unit(b & 0x0f)
                b = next(d)
            if self.c_unit.qstat == 2 and self.c_unit.holdoff:
                raise StopIteration()
            if self.c_unit.dec_state != 10:
                raise self.Error(10)
            for i in range(8):
                if b == 0x34:
                    # 34: NOP
                    b = next(d)
                if (b & 0xf8) == 0x40:
                    # 4x: volume
                    if b != 0x40:
                        raise self.Error(6)
                    else:
                        b = next(d)
                if b == 0x34:
                    b = next(d)
                if b == 0x10:
                    # 10: set address
                    self.check_not_unit15()
                    addr = self.collect_bytes(d, 6)
                    if addr[ 0 ] != 0 or addr[ 1 ] != 0:
                        raise self.Error(7)
                    dec_addr = struct.unpack(">L", addr[ 2:6 ])[ 0 ]
                    if dec_addr >= self.c_unit.geometry.max_lba:
                        raise self.Error(7)
                    self.c_unit.current_lba = dec_addr
                    b = next(d)
                if b == 0x34:
                    b = next(d)
                if b == 0x18:
                    # 18: set length
                    l = self.collect_bytes(d, 4)
                    dec_l = struct.unpack(">L", l)[ 0 ]
                    self.c_unit.target_length = dec_l
                    b = next(d)
                if b == 0x34:
                    b = next(d)
                if (b & 0xf0) == 0x00:
                    self.decode_cmd_0x(d, b)
                    return
                if b == 0x39:
                    dummy = self.collect_bytes(d, 2)
                    b = next(d)
                if b == 0x3b:
                    dummy = self.collect_bytes(d, 1)
                    b = next(d)
                if b == 0x3e:
                    mask = self.collect_bytes(d, 8)
                    if mask[ 2 ] != 0 or mask[ 3 ] != 0:
                        raise self.Error(8)
                    self.c_unit.mask_bits = mask
                    b = next(d)
                if b == 0x48:
                    mode = self.collect_bytes(d, 1)
                    if mode[ 0 ] != 0:
                        raise self.Error(8)
                    b = next(d)
            if (b & 0xf0) == 0x00:
                self.decode_cmd_0x(d, b)
                return
            if (b & 0xf0) == 0x30:
                self.decode_cmd_3x(d, b)
                return
            raise self.Error(5)
        except StopIteration:
            self.c_unit.dec_state = 11

    def listen_0e(self, ev):
        if self.c_unit.dec_state == 2:
            self.cmd_write(ev)
        elif self.c_unit.dec_state == 8:
            self.cmd_validate_key(ev)
        elif self.c_unit.dec_state == 9:
            self.cmd_set_format_options(ev)
        else:
            raise self.Error(10)

    def cmd_write_loopback(self, ev):
        # No EPPR!
        self.pp_enabled = False
        if len(ev.data) > self.len_op:
            raise self.Error(12)
        if ev.end != (len(ev.data) == self.len_op):
            raise self.Error(12)
        for idx, b in enumerate(ev.data, self.next_loop):
            if b != (idx % 256):
                raise self.Error(2)
        self.next_loop = (self.next_loop + len(ev.data)) % 256
        self.len_op -= len(ev.data)
        if ev.end:
            self.c_unit.dec_state = 10

    def cmd_ch_independent_clear(self):
        if self.c_unit is self.unit15:
            self.device_clear()
        else:
            self.c_unit.clear_unit()
            self.c_unit.current_lba = 0

    def cmd_cancel(self):
        self.c_unit.cancel()

    def listen_12(self, ev):
        d = iter(ev.data)
        try:
            b = next(d)
            if (b & 0xf0) == 0x20:
                # 2x: select unit x
                self.select_unit(b & 0x0f)
                b = next(d)
                if b == 8:
                    self.cmd_ch_independent_clear()
                elif b == 9:
                    self.cmd_cancel()
                else:
                    raise self.Error(5)
            elif b == 0x01:
                # 01: HPIB parity checking
                # No EPPR
                self.pp_enabled = False
                param = self.collect_bytes(d, 1)
                self.srq_enabled = (param[ 0 ] & 2) != 0
            elif b == 0x02:
                # 02: read loopback
                # No EPPR
                self.pp_enabled = False
                l1 = self.check_end_seq(d, 4)
                l2 = struct.unpack(">L", l1)[ 0 ]
                if l2 == 0:
                    raise self.Error(8)
                self.len_op = l2
                self.c_unit.dec_state = 6
            elif b == 0x03:
                # 03: write loopback
                # No EPPR
                self.pp_enabled = False
                l1 = self.check_end_seq(d, 4)
                l2 = struct.unpack(">L", l1)[ 0 ]
                if l2 == 0:
                    raise self.Error(8)
                self.len_op = l2
                self.next_loop = 0xff
                self.c_unit.dec_state = 7
            elif b == 0x08:
                self.cmd_ch_independent_clear()
            elif b == 0x09:
                self.cmd_cancel()
            else:
               raise self.Error(5)
        except StopIteration:
            raise self.Error(5)

    def process_listen(self, ev):
        try:
            if ev.sec_addr == 0x05:
                self.check_listen_data(ev)
                self.listen_05(ev)
            elif ev.sec_addr == 0x0e:
                self.listen_0e(ev)
            elif ev.sec_addr == 0x10:
                self.check_listen_data(ev)
                if len(ev.data) != 1:
                    self.c_unit.set_status_bit(9)
                # No EPPR!
                self.pp_enabled = False
            elif ev.sec_addr == 0x12:
                if self.c_unit.dec_state == 7:
                    self.cmd_write_loopback(ev)
                else:
                    self.check_listen_data(ev)
                    self.listen_12(ev)
            else:
                self.check_listen_data(ev)
                raise self.Error(10)
        except self.Error as e:
            self.c_unit.set_status_bit(e.err_no)

    def send_end_byte(self):
        self.io.talk_data(b"\x01" , True)

    def talk_and_set_cp(self, data, eoi, cp_handler):
        self.io.talk_data(data, eoi)
        self.io.send_checkpoint()
        self.cp_reached_handler = cp_handler

    def cmd_read(self):
        try:
            self.check_new_not_ready()
        except self.Error:
            self.send_end_byte()
            raise
        self.len_op = self.c_unit.target_length
        self.read_n_talk()

    def read_n_talk(self):
        data = self.c_unit.read_img()
        if self.c_unit.current_lba == 0:
            reached_end = True
        else:
            reached_end = False
        if self.c_unit.target_length != 0xffff_ffff:
            if self.len_op <= self.c_unit.bps:
                self.talk_and_set_cp(data[ :self.len_op ], True, SS80DriveState.generic_talk_cp)
            else:
                self.talk_and_set_cp(data, reached_end, SS80DriveState.cmd_read_cp3 if reached_end else SS80DriveState.cmd_read_cp2)
        else:
            self.talk_and_set_cp(data, reached_end, SS80DriveState.generic_talk_cp if reached_end else SS80DriveState.cmd_read_cp2)
        self.pp_enabled = False

    def generic_talk_cp(self, ev):
        if ev.flushed:
            self.c_unit.set_status_bit(12)
        else:
            self.c_unit.dec_state = 11

    def cmd_read_cp2(self, ev):
        if ev.flushed:
            self.c_unit.set_status_bit(12)
        else:
            self.len_op -= self.c_unit.bps
            self.read_n_talk()

    def cmd_read_cp3(self, ev):
        if ev.flushed:
            self.c_unit.set_status_bit(12)
        else:
            # Reached end of volume
            self.c_unit.set_status_bit(44)

    def cmd_describe(self):
        # First controller description
        out = bytearray(self.fixed_data.cont_desc)
        if self.c_unit is self.unit15:
            for u in self.units:
                self.describe_unit(u, out)
        else:
            self.describe_unit(self.c_unit, out)
        self.talk_and_set_cp(out, True, SS80DriveState.generic_talk_cp)
        self.pp_enabled = False

    def describe_unit(self, unit, out):
        out.extend(unit.unit_desc)
        c, h, s = unit.geometry.max_chs
        vol = struct.pack(">xHBHxxLB", c - 1, h - 1, s - 1, unit.geometry.max_lba - 1 if unit.is_ready() else 0, unit.vol_il)
        out.extend(vol)

    def cmd_request_status(self):
        out = bytearray([ self.c_unit_no, 0xff ])
        out.extend(self.c_unit.status_bits)
        out.extend(self.c_unit.parameter)
        self.talk_and_set_cp(out, True, SS80DriveState.cmd_request_status_cp)
        self.pp_enabled = False

    def cmd_request_status_cp(self, ev):
        if ev.flushed:
            self.c_unit.set_status_bit(12)
        else:
            self.c_unit.clear_status()
            self.c_unit.dec_state = 11

    def cmd_qstat(self):
        self.talk_and_set_cp(bytes([ self.c_unit.qstat ]), True, SS80DriveState.cmd_qstat_cp)
        # No EPPR!
        self.pp_enabled = False

    def cmd_qstat_cp(self, ev):
        if ev.flushed:
            self.c_unit.set_status_bit(12)
        else:
            self.c_unit.dec_state = 10
            self.c_unit.holdoff = False
        # No EPPR!
        self.pp_enabled = False

    def cmd_read_loopback(self):
        data = bytes([ (x + 0xff) & 0xff for x in range(256) ])
        l = min(self.len_op, 256)
        reached_end = l <= 256
        self.talk_and_set_cp(data[ :l ], reached_end, SS80DriveState.cmd_read_loopback_cp2 if reached_end else SS80DriveState.cmd_read_loopback_cp1)
        # No EPPR!
        self.pp_enabled = False

    def cmd_read_loopback_cp1(self, ev):
        # No EPPR!
        self.pp_enabled = False
        if ev.flushed:
            self.c_unit.set_status_bit(12)
        else:
            self.len_op -= 256
            self.cmd_read_loopback()

    def cmd_read_loopback_cp2(self, ev):
        # No EPPR!
        self.pp_enabled = False
        if ev.flushed:
            self.c_unit.set_status_bit(12)
        else:
            self.c_unit.dec_state = 10

    def process_talk(self, ev):
        try:
            if ev.sec_addr == 0x0e:
                if self.c_unit.dec_state == 1:
                    self.cmd_read()
                elif self.c_unit.dec_state == 3:
                    self.cmd_describe()
                elif self.c_unit.dec_state == 5:
                    self.cmd_request_status()
                else:
                    self.send_end_byte()
                    self.c_unit.set_status_bit(10)
            elif ev.sec_addr == 0x10:
                self.cmd_qstat()
            elif ev.sec_addr == 0x12 and self.c_unit.dec_state == 6:
                self.cmd_read_loopback()
            else:
                self.send_end_byte()
                self.c_unit.set_status_bit(10)
        except self.Error as e:
            self.c_unit.set_status_bit(e.err_no)

    def process_ev(self, ev):
        if isinstance(ev, rem488.RemotizerIdentify):
            self.cmd_identify()
        elif isinstance(ev, rem488.RemotizerAddressed):
            self.cmd_parallel_poll(ev)
        elif isinstance(ev, rem488.RemotizerData):
            self.pp_enabled = True
            self.process_listen(ev)
            self.set_pp(True)
        elif isinstance(ev, rem488.RemotizerTalk):
            self.pp_enabled = True
            self.process_talk(ev)
            self.set_pp(True)
        elif isinstance(ev, rem488.RemotizerCPReached) and self.cp_reached_handler is not None:
            tmp = self.cp_reached_handler
            self.cp_reached_handler = None
            self.pp_enabled = True
            tmp(self, ev)
            self.set_pp(True)
        elif isinstance(ev, rem488.RemotizerDevClear):
            self.set_pp(False)
            self.device_clear()
            self.pp_enabled = True
            self.set_pp(True)

PROTOS = {
    "Amigo": AmigoDriveState,
    "SS/80": SS80DriveState
}

class IOThread(QtCore.QThread):
    # *****************
    # **** Signals ****
    # *****************
    #
    # Report connection status
    # 1st parameter is one of rem488.CONNECTION_*
    status_connect = QtCore.pyqtSignal(int , str)
    # Report Read counter
    rd_counter = QtCore.pyqtSignal(int , int)
    # Report Write counter
    wr_counter = QtCore.pyqtSignal(int , int)
    # Report current position
    # Params: unit# , LBA , Cylinder , Head , Sector
    curr_pos = QtCore.pyqtSignal(int , int , int , int , int)

    def __init__(self, port):
        QtCore.QThread.__init__(self)
        self.lock = threading.RLock()
        self.rem = rem488.RemotizerIO(port , True)
        self.model_index = -1
        self.drive = None

    def run(self):
        while True:
            ev = self.rem.get_event()
            if isinstance(ev , rem488.RemotizerConnection):
                self.status_connect.emit(ev.status , ev.msg)
            elif self.drive:
                self.drive.process_ev(ev)
                for unit_no , unit in enumerate(self.drive.units):
                    if unit.rd_counter:
                        self.rd_counter.emit(unit_no , unit.rd_counter)
                        unit.rd_counter = 0
                    if unit.wr_counter:
                        self.wr_counter.emit(unit_no , unit.wr_counter)
                        unit.wr_counter = 0
                    if self.current_lba[ unit_no ] != unit.current_lba:
                        self.current_lba[ unit_no ] = unit.current_lba
                        c , h , s = unit.get_current_chs()
                        self.curr_pos.emit(unit_no , unit.current_lba , c , h , s)

    # Set model (by index into DRIVE_MODELS)
    def set_model(self , index):
        if self.drive == None or self.model_index != index:
            with self.lock:
                if index >= 0 and index < len(DRIVE_MODELS):
                    if self.drive is not None:
                        for unit in self.drive.units:
                            unit.unload_image()
                    self.model_index = index
                    fixed_data = DRIVE_MODELS[ index ]
                    self.drive = PROTOS[ fixed_data.protocol ](self.rem , fixed_data)
                    self.current_lba = [ 0 ] * self.drive.n_units

    # Load/unload an image file
    def load_image(self , unit , image_file):
        if self.drive:
            with self.lock:
                if image_file:
                    return self.drive.units[ unit ].load_image(image_file)
                else:
                    self.drive.units[ unit ].unload_image()
                    return 0

    # Set HPIB address
    def set_address(self , addr):
        with self.lock:
            self.rem.set_address(addr)
            self.rem.set_pp_response(0x80 >> addr)

    # Set read-only state
    def set_read_only(self , unit , read_only):
        with self.lock:
            if self.drive:
                self.drive.units[ unit ].set_read_only(read_only)

    def get_unit_count(self):
        return self.drive.n_units if self.drive else 0
