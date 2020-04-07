#!/usr/bin/env python3
# A GUI based emulator of HP Amigo drives for use with MAME IEEE-488 remotizer
# Copyright (C) 2020 F. Ulivi <fulivi at big "G" mail>
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

from PyQt5 import QtCore
import rem488
import threading
from collections import namedtuple

DriveModel = namedtuple("DriveModel" , [ "name" , "id" , "geometry" , "units" , "fixed" , "ignore_fmt" , "protocol" , "extra_data" ])

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

class FixedDriveData(DriveModel):
    def __new__(cls , *args):
        ins = super().__new__(cls , *args)
        # cache max_lba attribute as it's fixed
        ins.max_lba = ins.geometry[ 0 ] * ins.geometry[ 1 ] * ins.geometry[ 2 ]
        return ins

    def chs_to_lba(self, chs):
        if chs[ 0 ] < 0 or chs[ 0 ] >= self.geometry[ 0 ] or \
           chs[ 1 ] < 0 or chs[ 1 ] >= self.geometry[ 1 ] or \
           chs[ 2 ] < 0 or chs[ 2 ] >= self.geometry[ 2 ]:
            raise CHSOutOfRange(chs , self.geometry)
        return (chs[ 0 ] * self.geometry[ 1 ] + chs[ 1 ]) * self.geometry[ 2 ] + chs[ 2 ]

    def lba_to_chs(self, lba):
        if lba < 0 or lba > self.max_lba:
            raise LBAOutOfRange(lba , self.max_lba)
        tmp , s = divmod(lba , self.geometry[ 2 ])
        c , h = divmod(tmp , self.geometry[ 1 ])
        return (c , h , s)

DRIVE_MODELS = [
    #              name     id            geometry      units  fixed ignore_fmt protocol extra_data
    FixedDriveData("9895" , b"\x00\x81" , ( 77 , 2 , 30) , 2 , False , False , "Amigo" , None),
    FixedDriveData("9134b", b"\x01\x0a" , (306 , 4 , 31) , 1 , True  , True  , "Amigo" , None),
    FixedDriveData("82901", b"\x01\x04" , ( 33 , 2 , 16) , 2 , False , False , "Amigo" , None),
]

class AmigoUnitState:
    def __init__(self , fixed_data):
        self.fixed_data = fixed_data
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
            self.current_lba = self.fixed_data.chs_to_lba(chs)
        except CHSOutOfRange:
            self.current_lba = self.fixed_data.max_lba
            raise

    def is_lba_ok(self):
        return self.current_lba >= 0 and self.current_lba < self.fixed_data.max_lba

    def get_current_chs(self):
        return self.fixed_data.lba_to_chs(self.current_lba)

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
            for x in range(self.fixed_data.max_lba):
                self.image.write(fill)
            self.wr_counter += self.fixed_data.max_lba

class AmigoDriveState:
    def __init__(self , io , fixed_data):
        self.io = io
        self.fixed_data = fixed_data
        self.dsj = 2
        self.stat1 = 0
        self.pp_enabled = True
        self.pp_state = False
        self.buffer_ = bytearray(256)
        self.status = bytearray(4)
        self.units = []
        for u in range(self.fixed_data.units):
            self.units.append(AmigoUnitState(self.fixed_data))
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
        if unit < self.fixed_data.units:
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
            if c.data[ 1 ] < self.fixed_data.units:
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
                    unit.current_lba = self.fixed_data.max_lba
                else:
                    new_lba = min(self.fixed_data.max_lba , unit.current_lba + sec_count)
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
                if not self.fixed_data.ignore_fmt or (c.data[ 2 ] & 0x80) != 0:
                    unit.format_img(0xff if self.fixed_data.ignore_fmt else c.data[ 4 ])
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
        (   8 ,   6 ,    2) : ("Seek"                 , cmd_seek),
        (   8 ,   2 ,    3) : ("Request status"       , cmd_req_status),
        (   8 ,   2 ,    5) : ("Unbuffered read"      , cmd_unbuff_rd),
        (   8 ,   4 ,    7) : ("Verify"               , cmd_verify),
        (   8 ,   2 ,    8) : ("Unbuffered write"     , cmd_unbuff_wr),
        (   8 ,   2 , 0x0b) : ("Initialize"           , cmd_initialize),
        (   8 ,   6 , 0x0c) : ("Set address record"   , cmd_set_addr_rec),
        (   8 ,   2 , 0x14) : ("Request log. address" , cmd_req_log_addr),
        (   8 ,   2 , 0x15) : ("End"                  , cmd_end),
        (   9 ,   2 ,    8) : ("Buffered write"       , cmd_buff_wr),
        (0x0a ,   2 ,    3) : ("Request status"       , cmd_req_status),
        (0x0a ,   2 ,    5) : ("Buffered read"        , cmd_buff_rd),
        (0x0a ,   2 , 0x14) : ("Request log. address" , cmd_req_log_addr),
        (0x0b ,   2 ,    5) : ("Buffered read/verify" , cmd_buff_rd),
        (0x0c ,   5 , 0x18) : ("Format"               , cmd_format)
    }

    # Talk command decoding table
    # Secondary address is key
    TALK_CMDS = {
           0 : ("Send data"        , cmd_tx_data),
           8 : ("Send addr/status" , cmd_tx_status),
        0x10 : ("DSJ"              , cmd_dsj)
    }

    def get_decoded_cmd(self , c):
        if isinstance(c , rem488.RemotizerData):
            len_params = len(c.data)
            # Match receive data cmd
            if len_params > 0 and c.sec_addr == 0:
                c.cmd = ("Receive data" , AmigoDriveState.cmd_rx_data)
            elif c.sec_addr == 0x10 and len_params == 1:
                c.cmd = ("Amigo clear" , AmigoDriveState.cmd_amigo_clear)
            else:
                key_tuple = (c.sec_addr , len_params , c.data[ 0 ] if (len_params > 0) else None)
                try:
                    c.cmd = AmigoDriveState.LISTEN_CMDS[ key_tuple ]
                except KeyError:
                    c.cmd = ("Unknown" , AmigoDriveState.cmd_unknown_listen)
        elif isinstance(c , rem488.RemotizerTalk):
            try:
                c.cmd = AmigoDriveState.TALK_CMDS[ c.sec_addr ]
            except KeyError:
                c.cmd = ("Unknown" , AmigoDriveState.cmd_unknown_talk)
        elif isinstance(c , rem488.RemotizerIdentify):
            c.cmd = (str(c) , AmigoDriveState.cmd_identify)
        elif isinstance(c , rem488.RemotizerAddressed):
            c.cmd = (str(c) , AmigoDriveState.cmd_parallel_poll)
        elif isinstance(c , rem488.RemotizerDevClear):
            c.cmd = (str(c) , AmigoDriveState.cmd_dev_clear)
        elif isinstance(c , rem488.RemotizerCPReached):
            c.cmd = (str(c) , AmigoDriveState.cmd_cp_reached)
        else:
            c.cmd = None
        return c

    def exec_cmd(self, c):
        if c.cmd == None:
            return
        is_tl_cmd = isinstance(c , rem488.RemotizerData) or isinstance(c , rem488.RemotizerTalk)
        if is_tl_cmd:
            self.pp_enabled = True
        c.cmd[ 1 ](self , c)
        if is_tl_cmd:
            self.set_pp(True)

class IOThread(QtCore.QThread):
    # *****************
    # **** Signals ****
    # *****************
    #
    # Report connection status
    # 1st parameter is one of rem488.CONNECTION_*
    status_connect = QtCore.pyqtSignal(int , str)
    # Report "active" state
    active = QtCore.pyqtSignal()
    # Report Read counter
    rd_counter = QtCore.pyqtSignal(int , int)
    # Report Write counter
    wr_counter = QtCore.pyqtSignal(int , int)
    # Report current position
    # Params: unit# , LBA , Cylinder , Head , Sector
    curr_pos = QtCore.pyqtSignal(int , int , int , int , int)

    def __init__(self):
        QtCore.QThread.__init__(self)
        self.lock = threading.RLock()
        self.rem = rem488.RemotizerIO(1234 , True)
        self.model_index = -1
        self.drive = None
        self.active_state = False

    def run(self):
        while True:
            ev = self.rem.get_event()
            if isinstance(ev , rem488.RemotizerConnection):
                self.status_connect.emit(ev.status , ev.msg)
            elif self.drive:
                c = self.drive.get_decoded_cmd(ev)
                if c.cmd and not self.active_state:
                    self.active_state = True
                    self.active.emit()
                self.drive.exec_cmd(c)
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
                    self.model_index = index
                    fixed_data = DRIVE_MODELS[ index ]
                    self.drive = AmigoDriveState(self.rem , fixed_data)
                    self.current_lba = [ 0 ] * fixed_data.units

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

    def clear_active(self):
        self.active_state = False

    def get_unit_count(self):
        return self.drive.fixed_data.units if self.drive else 0
