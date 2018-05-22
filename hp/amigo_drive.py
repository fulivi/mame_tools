#!/usr/bin/env python3
# An high-level emulator of HP Amigo drives for use with MAME IEEE-488 remotizer
# Copyright (C) 2018 F. Ulivi <fulivi at big "G" mail>
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

import sys
import socket
import collections
import threading
import functools
import argparse

debug_print = None

class ConnectionClosed(Exception):
    pass

MSGS = "DEJKQRSXY"

class Remote488MsgIO:
    def my_th(self):
        state = 0
        while True:
            try:
                ins = self.sock.recv(4096)
            except ConnectionError:
                break
            if not ins:
                break
            for b in ins:
                c = chr(b)
                if state == 0:
                    if c in MSGS:
                        msg_type = c
                        state = 1
                    elif not c.isspace():
                        state = 5
                elif state == 1:
                    if c == ':':
                        state = 2
                    else:
                        state = 5
                elif state == 2:
                    try:
                        data = int(c , 16)
                        state = 3
                    except ValueError:
                        state = 5
                elif state == 3:
                    try:
                        data = (data << 4) | int(c , 16)
                        state = 4
                    except ValueError:
                        state = 5
                elif state == 4:
                    if c.isspace() or c == ',' or c == ';':
                        state = 0
                        if msg_type == 'J':
                            with self.lock:
                                self.sock.sendall(b'K:00\n')
                        else:
                            self.q.append((msg_type , data))
                            with self.cv:
                                self.cv.notify()
                    else:
                        state = 5
                else:
                    if c.isspace()  or c == ',' or c == ';':
                        state = 0
        self.q.append(ConnectionClosed())
        with self.cv:
            self.cv.notify()

    def __init__(self , sock):
        self.sock = sock
        self.cv = threading.Condition()
        self.lock = threading.RLock()
        self.q = collections.deque()
        self.th = threading.Thread(target = self.my_th)
        self.th.start()

    def has_msg(self ):
        return len(self.q) > 0

    def get_msg(self ):
        m = self.q.popleft()
        if isinstance(m , ConnectionClosed):
            raise m
        else:
            return m

    def send_msg(self, msg_type , msg_data):
        b = bytes("{}:{:02x},".format(msg_type , msg_data) , encoding = "ascii")
        with self.lock:
            self.sock.sendall(b)

    def send_data(self, data , eoi_at_end = False):
        last_dab = len(data)
        add_eoi = eoi_at_end and last_dab > 0
        if add_eoi:
            last_dab -= 1
        with self.lock:
            #print("T {}".format(data))
            for b in data[ :last_dab ]:
                self.send_msg('D' , b)
            if add_eoi:
                self.send_msg('E' , data[ last_dab ])

    def send_pp_state(self, pp_state):
        self.send_msg('P' , pp_state)

CMDS = {
    0x01 : "GTL",
    0x04 : "SDC",
    0x05 : "PPC",
    0x08 : "GET",
    0x09 : "TCT",
    0x11 : "LLO",
    0x14 : "DCL",
    0x15 : "PPU",
    0x18 : "SPE",
    0x19 : "SPD",
    0x1f : "CFE",
    0x3f : "UNL",
    0x5f : "UNT"
}

def decode_cmd(byte):
    code = byte & 0x7f
    parity = (byte >> 4) ^ (byte & 0x0f)
    parity = (parity >> 2) ^ (parity & 0x03)
    parity = (parity >> 1) ^ (parity & 0x01)
    par_msg = " (E)" if parity == 0 else " (O)"
    if code in CMDS:
        s = CMDS[ code ]
    elif (code & 0x60) == 0x20:
        s = "LA {:02x}".format(code & 0x1f)
    elif (code & 0x60) == 0x40:
        s = "TA {:02x}".format(code & 0x1f)
    elif (code & 0x60) == 0x60:
        s = "SA {:02x}".format(code & 0x1f)
    else:
        s = "???"
    return s + par_msg

class BusCmd:
    pass

class IdentifyCmd(BusCmd):
    def __str__(self ):
        return "IDENTIFY"

class CPReachedCmd(BusCmd):
    def __init__(self, flushed):
        self.flushed = flushed

    def __str__(self ):
        return "CP F={}".format(self.flushed)

class TalkCmd(BusCmd):
    def __init__(self , sec_addr):
        self.sec_addr = sec_addr

    def __str__(self ):
        return "TALK    {:02x}".format(self.sec_addr)

class ListenCmd(BusCmd):
    def __init__(self, sec_addr , params):
        self.sec_addr = sec_addr
        self.params = params

    def __str__(self ):
        p = functools.reduce(lambda x , y: x + "{:02x} ".format(y), self.params , "")
        return "LISTEN  {:02x} {}".format(self.sec_addr , p)

class ParallelPoll(BusCmd):
    def __init__(self, state):
        self.state = state

    def __str__(self ):
        return "PP      {}".format(self.state)

class DeviceClear(BusCmd):
    def __init__(self):
        pass

    def __str__(self):
        return "CLEAR"

class UnknownModel(Exception):
    pass

class CHSOutOfRange(Exception):
    def __init__(self, chs , max_chs):
        self.chs = chs
        self.max_chs = chs

    def __str__(self ):
        return "({},{},{}) out of range, max = ({},{},{})".format(self.chs[ 0 ] , self.chs[ 1 ] , self.chs[ 2 ] , self.max_chs[ 0 ] - 1 ,  self.max_chs[ 1 ] - 1 ,  self.max_chs[ 2 ] - 1)

class LBAOutOfRange(Exception):
    def __init__(self, lba , max_lba):
        self.lba = lba
        self.max_lba = lba

    def __str__(self ):
        return "LBA {} out of range, max = {}".format(self.lba , self.max_lba)

class FixedDriveData:
    # [ 0 ]     Identify sequence
    # [ 1 ]     (C , H , S) tuple: total number of physical cylinders, heads & sectors
    # [ 2 ]     Number of units per drive
    # [ 3 ]     Ignore data on format cmd
    MODELS = {
        '9895'  : ( b"\x00\x81" , ( 77 , 2 , 30) , 2 , False ),
        '9134b' : ( b"\x01\x0a" , (306 , 4 , 31) , 1 , True  )
    }

    def __init__(self, model):
        if model not in FixedDriveData.MODELS:
            raise UnknownModel()
        rec = FixedDriveData.MODELS[ model ]
        self.id_seq = rec[ 0 ]
        self.max_chs = rec[ 1 ]
        self.max_unit = rec[ 2 ]
        self.ignore_fmt = rec[ 3 ]
        self.max_lba = self.max_chs[ 0 ] * self.max_chs[ 1 ] * self.max_chs[ 2 ]

    def chs_to_lba(self, chs):
        if chs[ 0 ] < 0 or chs[ 0 ] >= self.max_chs[ 0 ] or \
           chs[ 1 ] < 0 or chs[ 1 ] >= self.max_chs[ 1 ] or \
           chs[ 2 ] < 0 or chs[ 2 ] >= self.max_chs[ 2 ]:
            raise CHSOutOfRange(chs , self.max_chs)
        return (chs[ 0 ] * self.max_chs[ 1 ] + chs[ 1 ]) * self.max_chs[ 2 ] + chs[ 2 ]

    def lba_to_chs(self, lba):
        if lba < 0 or lba > self.max_lba:
            raise LBAOutOfRange(lba , self.max_lba)
        tmp , s = divmod(lba , self.max_chs[ 2 ])
        c , h = divmod(tmp , self.max_chs[ 1 ])
        return (c , h , s)

class UnitState:
    def __init__(self , fixed_data , image):
        self.fixed_data = fixed_data
        self.current_lba = 0
        self.a_bit = False
        self.w_bit = False
        self.f_bit = True
        self.c_bit = False
        self.ss = 0
        self.tttt = 6
        self.image = image
        if not self.is_ready():
            self.ss = 3
            self.f_bit = False

    def is_ready(self ):
        return self.image != None

    def set_current_chs(self, chs):
        try:
            self.current_lba = self.fixed_data.chs_to_lba(chs)
        except CHSOutOfRange:
            self.current_lba = self.fixed_data.max_lba
            raise

    def is_lba_ok(self ):
        return self.current_lba >= 0 and self.current_lba < self.fixed_data.max_lba

    def get_current_chs(self ):
        return self.fixed_data.lba_to_chs(self.current_lba)

    def write_img(self, data):
        if self.is_ready():
            self.image.seek(256 * self.current_lba)
            self.image.write(data)
            self.current_lba += 1

    def read_img(self ):
        if self.is_ready():
            self.image.seek(256 * self.current_lba)
            data = bytearray(self.image.read(256))
            if len(data) < 256:
                data.extend(bytes(256 - len(data)))
            self.current_lba += 1
        else:
            data = bytearray(256)
        return data

    def format_img(self, filler):
        if self.is_ready():
            self.image.seek(0)
            fill = bytes([ filler ] * 256)
            for x in range(self.fixed_data.max_lba):
                self.image.write(fill)

class DriveState:
    def __init__(self , io , fixed_data , hpib_addr , images):
        self.io = io
        self.fixed_data = fixed_data
        self.hpib_addr = hpib_addr
        self.dsj = 2
        self.stat1 = 0
        self.pp_enabled = True
        self.pp_state = False
        self.buffer_ = bytearray(256)
        self.status = bytearray(4)
        self.units = []
        for u in range(self.fixed_data.max_unit):
            img = images.pop(0) if images else None
            self.units.append(UnitState(self.fixed_data , img))
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

    def send_pp_state(self):
        self.io.send_pp_state(0x80 if self.pp_state else 0)

    def set_pp(self, new_pp_state):
        new_state = self.pp_enabled and new_pp_state
        if new_state != self.pp_state:
            self.pp_state = new_state
            self.send_pp_state()

    def is_dsj_ok(self ):
        return self.dsj != 2

    def select_unit(self, unit):
        if unit < self.fixed_data.max_unit:
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
        self.io.send_data(b"\x01" , True)

    def send_checkpoint(self ):
        self.io.send_msg('X' , 0)

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

    def clear_dsj(self ):
        if self.dsj != 2:
            self.dsj = 0

    def clear_errors(self ):
        self.stat1 = 0
        self.dsj = 0

    def get_cmd(self):
        # 0: No primary
        # 1: PACS (got PPC command)
        # 2: TPAS (got MTA)
        # 3: LPAS (got MLA)
        # 4: got UNT
        sa_state = 0
        # 0: idle
        # 1: MTA + SA
        # 2: MLA + SA, collecting parameters
        state = 0
        signals = 0x1f
        pp_state = True
        talker = False
        listener = False
        mta = (self.hpib_addr & 0x1f) | 0x40
        mla = (self.hpib_addr & 0x1f) | 0x20
        msa = (self.hpib_addr & 0x1f) | 0x60
        dab_cnt = 0
        while True:
            with self.io.cv:
                self.io.cv.wait_for(lambda : self.io.has_msg())
            try:
                m = self.io.get_msg()
            except ConnectionClosed:
                return
            msg_type , msg_data = m
            if debug_print:
                s = ""
                if (signals & 1) == 0 and msg_type == 'D':
                    s = decode_cmd(msg_data)
                elif msg_type == 'D' or msg_type == 'E':
                    dab_cnt += 1
                    s = "{:3d}".format(dab_cnt)
                else:
                    dab_cnt = 0
                print("{}:{:02x} {}".format(msg_type , msg_data , s) , file = debug_print)
            if msg_type == 'R':
                signals &= ~msg_data
            elif msg_type == 'S':
                signals |= msg_data
            elif msg_type == 'Q':
                continue
            elif msg_type == 'X':
                self.io.send_msg('Y' , 0)
                continue
            elif msg_type == 'Y':
                yield CPReachedCmd(msg_data != 0)
                continue
            is_cmd = (signals & 1) == 0 and msg_type == 'D'
            if is_cmd:
                msg_data &= 0x7f
                is_pcg = (msg_data & 0x60) != 0x60
            else:
                is_pcg = False
            if debug_print:
                print("S={} SA={} {} {}".format(state , sa_state , is_cmd , is_pcg) , file = debug_print)
            if is_cmd:
                if is_pcg:
                    sa_state = 0
                if msg_data == 0x05 and listener:
                    # PPC
                    sa_state = 1
                    state = 0
                elif msg_data == 0x15:
                    # PPU
                    # TODO:
                    pass
                elif listener and msg_data == 0x3f:
                    # UNL
                    listener = False
                    state = 0
                    if not pp_state:
                        yield ParallelPoll(True)
                        pp_state = True
                elif msg_data == 0x5f:
                    # UNT
                    talker = False
                    state = 0
                    sa_state = 4
                    if not pp_state:
                        yield ParallelPoll(True)
                        pp_state = True
                elif msg_data == mla:
                    # MLA
                    listener = True
                    sa_state = 3
                    state = 0
                elif msg_data == mta:
                    # MTA
                    talker = True
                    sa_state = 2
                    state = 0
                elif talker and (msg_data & 0x60) == 0x40:
                    # OTA
                    talker = False
                    state = 0
                    if not pp_state:
                        yield ParallelPoll(True)
                        pp_state = True
                elif (listener and msg_data == 0x04) or msg_data == 0x14:
                    state = 0
                    yield DeviceClear()
                elif not is_pcg:
                    # Secondary address
                    if sa_state == 1:
                        # PPE / PPD
                        # TODO:
                        pass
                    elif sa_state == 2:
                        # MTA + SA
                        if pp_state:
                            yield ParallelPoll(False)
                            pp_state = False
                        state = 1
                        c = TalkCmd(msg_data & 0x1f)
                    elif sa_state == 3:
                        # MLA + SA
                        if pp_state:
                            yield ParallelPoll(False)
                            pp_state = False
                        state = 2
                        sec_addr = msg_data & 0x1f
                        params = bytearray()
                    elif sa_state == 4:
                        # UNT + SA
                        if msg_data == msa:
                            c = IdentifyCmd()
                            state = 1
            if state == 1:
                # Wait for ATN to be deasserted
                if (signals & 1) != 0:
                    state = 0
                    yield c
            elif state == 2:
                # Wait for parameters after MLA + SA
                if listener and not is_cmd:
                    if msg_type == 'D' or msg_type == 'E':
                        params.append(msg_data)
                        if msg_type == 'E':
                            state = 0
                            yield ListenCmd(sec_addr , params)
                        elif len(params) == 256:
                            yield ListenCmd(sec_addr , params)
                            params = bytearray()

    # Commands
    def cmd_rx_data(self, c):
        if self.cmd_seq_state != 3 and self.cmd_seq_state != 6:
            self.set_seq_error(False)
        elif self.lba_out_of_range():
            self.set_seq_state(0)
        else:
            unit = self.units[ self.current_unit ]
            chs = unit.get_current_chs()
            print("WR {} ({},{},{})".format(unit.current_lba , chs[ 0 ] , chs[ 1 ] , chs[ 2 ]))
            self.buffer_[ :len(c.params) ] = c.params
            unit.write_img(self.buffer_)
            self.clear_errors()
            if self.cmd_seq_state == 3:
                self.set_seq_state(0)

    def cmd_seek(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.params[ 1 ])
            if unit:
                self.set_error(0x1f)
                unit.a_bit = True
                try:
                    chs = (c.params[ 2 ] * 256 + c.params[ 3 ] , c.params[ 4 ] , c.params[ 5 ])
                    print("Seek ({},{},{})".format(chs[ 0 ] , chs[ 1 ] , chs[ 2 ]))
                    unit.set_current_chs(chs)
                    self.clear_dsj()
                except CHSOutOfRange:
                    unit.c_bit = True

    def cmd_req_status(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            if c.params[ 1 ] < self.fixed_data.max_unit:
                self.current_unit = c.params[ 1 ]
                unit = self.units[ self.current_unit ]
                self.status[ 0 ] = self.stat1
                self.status[ 1 ] = self.current_unit
                self.status[ 2 ] = unit.tttt << 1
                if unit.c_bit or unit.ss != 0:
                    self.status[ 2 ] |= 0x80
                res = unit.ss
                if unit.a_bit:
                    res |= 0x80
                if unit.w_bit:
                    res |= 0x40
                if unit.f_bit:
                    res |= 0x08
                if unit.c_bit:
                    res |= 0x04
                self.status[ 3 ] = res
            else:
                # Invalid unit number
                self.status[ 0 ] = 0x17
                self.status[ 1 ] = c.params[ 1 ]
                self.status[ 2 ] = 0
                self.status[ 3 ] = 0
                unit = self.units[ self.current_unit ]
            #print("Status = {:02x}:{:02x}:{:02x}:{:02x}".format(self.status[ 0 ] , self.status[ 1 ] , self.status[ 2 ] , self.status[ 3 ]))
            unit.a_bit = False
            unit.f_bit = False
            unit.c_bit = False
            self.clear_errors()
            self.set_seq_state(1)

    def cmd_verify(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.params[ 1 ])
            if unit:
                sec_count = c.params[ 2 ] * 256 + c.params[ 3 ]
                print("Verify {} sectors".format(sec_count))
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
            unit = self.select_unit_check_f(c.params[ 1 ])
            if unit:
                self.set_error(0x1f)
                unit.a_bit = True
                try:
                    chs = (c.params[ 2 ] * 256 + c.params[ 3 ] , c.params[ 4 ] , c.params[ 5 ])
                    print("Set addr. rec. ({},{},{})".format(chs[ 0 ] , chs[ 1 ] , chs[ 2 ]))
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
            print("Log. address = ({},{},{})".format(chs[ 0 ] , chs[ 1 ] , chs[ 2 ]))
            self.clear_errors()
            self.set_seq_state(1)

    def cmd_end(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            # Not entirely correct
            self.clear_errors()
            self.pp_enabled = False

    def cmd_write(self, c, seq_state):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.params[ 1 ])
            if unit and not self.dsj1_holdoff() and not self.lba_out_of_range():
                self.set_seq_state(seq_state)

    def cmd_buff_wr(self, c):
        self.cmd_write(c , 3)

    def cmd_unbuff_wr(self, c):
        self.cmd_write(c , 6)

    def cmd_read(self, c):
        if self.require_seq_state(0 , False) and self.is_dsj_ok():
            unit = self.select_unit_check_f(c.params[ 1 ])
            if unit and not self.dsj1_holdoff() and not self.lba_out_of_range():
                chs = unit.get_current_chs()
                print("RD {} ({},{},{})".format(unit.current_lba , chs[ 0 ] , chs[ 1 ] , chs[ 2 ]))
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
            unit = self.select_unit_check_f(c.params[ 1 ])
            if unit:
                if not self.fixed_data.ignore_fmt or (c.params[ 2 ] & 0x80) != 0:
                    unit.format_img(0xff if self.fixed_data.ignore_fmt else c.params[ 4 ])
                unit.current_lba = 0
                self.clear_errors()

    def cmd_tx_data(self, c):
        if self.require_seq_state(2 , True):
            self.io.send_data(self.buffer_)
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
                    print("RD {} ({},{},{})".format(unit.current_lba , chs[ 0 ] , chs[ 1 ] , chs[ 2 ]))
                    self.buffer_ = unit.read_img()
                    self.io.send_data(self.buffer_)
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
            self.io.send_data(self.status)
            # Add a 0x01 byte with EOI
            self.send_end_byte()
            self.send_checkpoint()
            self.set_seq_state(0)

    def cmd_dsj(self, c):
        if self.require_seq_state(0 , True):
            print("DSJ={}".format(self.dsj))
            self.io.send_data([ self.dsj ] , True)
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
        self.io.send_data(self.fixed_data.id_seq , True)

    def cmd_parallel_poll(self, c):
        self.set_pp(c.state)

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

    def get_decoded_cmd(self):
        for c in self.get_cmd():
            if isinstance(c , ListenCmd):
                len_params = len(c.params)
                # Match receive data cmd
                if len_params > 0 and c.sec_addr == 0:
                    c.cmd = ("Receive data" , DriveState.cmd_rx_data)
                elif c.sec_addr == 0x10 and len_params == 1:
                    c.cmd = ("Amigo clear" , DriveState.cmd_amigo_clear)
                else:
                    key_tuple = (c.sec_addr , len_params , c.params[ 0 ] if (len_params > 0) else None)
                    try:
                        c.cmd = DriveState.LISTEN_CMDS[ key_tuple ]
                    except KeyError:
                        c.cmd = ("Unknown" , DriveState.cmd_unknown_listen)
            elif isinstance(c , TalkCmd):
                try:
                    c.cmd = DriveState.TALK_CMDS[ c.sec_addr ]
                except KeyError:
                    c.cmd = ("Unknown" , DriveState.cmd_unknown_talk)
            elif isinstance(c , IdentifyCmd):
                c.cmd = (str(c) , DriveState.cmd_identify)
            elif isinstance(c , ParallelPoll):
                c.cmd = (str(c) , DriveState.cmd_parallel_poll)
            elif isinstance(c , DeviceClear):
                c.cmd = (str(c) , DriveState.cmd_dev_clear)
            elif isinstance(c , CPReachedCmd):
                c.cmd = (str(c) , DriveState.cmd_cp_reached)
            else:
                c.cmd = None
            yield c

    def exec_cmd(self, c):
        is_tl_cmd = isinstance(c , ListenCmd) or isinstance(c , TalkCmd)
        if is_tl_cmd:
            self.pp_enabled = True
        c.cmd[ 1 ](self , c)
        if is_tl_cmd:
            self.set_pp(True)

def main():
    parser = argparse.ArgumentParser(description="Emulation of Amigo drives")
    parser.add_argument('-p' , '--port' , default = 1234 , type = int , help = "TCP port of MAME remotizer (defaults to 1234)")
    parser.add_argument('-d' , '--dbg' , type = argparse.FileType('wt') , help = "File for debug output")
    parser.add_argument('model' , nargs=1 , help = "Drive model")
    parser.add_argument('img_file' , nargs='*' , type = argparse.FileType('r+b') , help = "Image file(s)")
    args = parser.parse_args()

    try:
        fixed = FixedDriveData(args.model[ 0 ])
    except UnknownModel:
        print("Model {} unknown\n\nAvailable models:".format(args.model[ 0 ]))
        for m in FixedDriveData.MODELS.keys():
            print(m)
        sys.exit(1)

    global debug_print
    debug_print = args.dbg

    io = socket.socket()
    try:
        io.connect(("localhost" , args.port))
        sock_io = io
    except ConnectionError:
        print("Client connection unsuccessful, start as server..")
        io.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        io.bind(('0.0.0.0' , args.port))
        io.listen(1)
        conn , addr = io.accept()
        io.close()
        print("Connection from {}".format(addr))
        sock_io = conn
    sock_io.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with sock_io:
        intf = Remote488MsgIO(sock_io)
        state = DriveState(intf , fixed , 0 , args.img_file)
        for c in state.get_decoded_cmd():
            if debug_print:
                print(str(c) , file = debug_print)
            print(c.cmd[ 0 ])
            state.exec_cmd(c)
    if debug_print:
        debug_print.close()

if __name__ == '__main__':
    main()
