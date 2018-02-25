#!/usr/bin/env python3
# A proof-of-concept HLE of HP9895 drive for use with MAME IEEE-488 remotizer
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

class ConnectionClosed(Exception):
    pass

MSGS = "DEJKQRS"

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
                    if c.isspace():
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
                    if c.isspace():
                        state = 0
        self.q.append(ConnectionClosed())
        with self.cv:
            self.cv.notify()
        
    def __init__(self , sock , cv):
        self.sock = sock
        self.cv = cv
        self.lock = threading.RLock()
        self.q = collections.deque()
        self.th = threading.Thread(target = self.my_th)
        self.th.start()

    def has_msg(self ):
        return len(self.q) > 0

    def get_msg(self ):
        return self.q.popleft()
    
    def send_msg(self, msg_type , msg_data):
        b = bytearray("{}:{:02x}\n".format(msg_type , msg_data) , encoding = "ascii")
        with self.lock:
            self.sock.sendall(b)

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

def get_cmd(io , my_addr , debug_print = False):
    state = 0
    signals = 0x1f
    pp_state = True
    mta = (my_addr & 0x1f) | 0x40
    mla = (my_addr & 0x1f) | 0x20
    msa = (my_addr & 0x1f) | 0x60
    dab_cnt = 0
    while True:
        with io.cv:
            io.cv.wait_for(lambda : io.has_msg())
        m = io.get_msg()
        if isinstance(m , ConnectionClosed):
            return
        else:
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
                print("{}:{:02x} {}".format(msg_type , msg_data , s))
            consumed = False
            while not consumed:
                consumed = True
                if msg_type == 'R':
                    signals &= ~msg_data
                elif msg_type == 'S':
                    signals |= msg_data
                elif msg_type == 'Q':
                    pass
                is_cmd = (signals & 1) == 0 and msg_type == 'D'
                if debug_print:
                    print("S={} {}".format(state , is_cmd))
                if is_cmd:
                    msg_data &= 0x7f
                if state == 0:
                    # Wait for UNT, MTA, MLA
                    if is_cmd:
                        if msg_data == 0x5f:
                            state = 1
                        elif msg_data == mta:
                            state = 2
                        elif msg_data == mla:
                            state = 3
                elif state == 1:
                    # Wait for UNT + MSA
                    if is_cmd and msg_data == msa:
                        c = IdentifyCmd()
                        state = 6
                    else:
                        consumed = False
                        state = 0
                elif state == 2:
                    # Wait for MTA + SA
                    if is_cmd and (msg_data & 0x60) == 0x60:
                        if pp_state:
                            yield ParallelPoll(False)
                            pp_state = False
                        state = 7
                        c = TalkCmd(msg_data & 0x1f)
                    else:
                        consumed = False
                        state = 0
                elif state == 3:
                    # Wait for MLA + SA
                    if is_cmd and (msg_data & 0x60) == 0x60:
                        if pp_state:
                            yield ParallelPoll(False)
                            pp_state = False
                        state = 5
                        sec_addr = msg_data & 0x1f
                        params = bytearray()
                    else:
                        consumed = False
                        state = 0
                elif state == 4:
                    # Wait for new cmd
                    if is_cmd:
                        state = 0
                        consumed = False
                        if not pp_state:
                            yield ParallelPoll(True)
                            pp_state = True
                elif state == 5:
                    # Wait for MLA + SA + Parameters
                    if is_cmd:
                        if (msg_data & 0x60) != 0x40 or msg_data == mta:
                            state = 0
                            consumed = False
                            if not pp_state:
                                yield ParallelPoll(True)
                                pp_state = True
                    elif msg_type == 'D':
                        params.append(msg_data)
                    elif msg_type == 'E':
                        params.append(msg_data)
                        state = 4
                        yield ListenCmd(sec_addr , params)
                elif state == 6:
                    # Wait for ATN to be deasserted
                    if (signals & 1) != 0:
                        state = 4
                        yield c
                    elif msg_type == 'D' or msg_type == 'E':
                        state = 0
                        consumed = False
                elif state == 7:
                    # Wait for ATN to be deasserted, ignore other listener addresses
                    if (signals & 1) != 0:
                        state = 4
                        yield c
                    elif is_cmd and (msg_data & 0x60) == 0x20 and msg_data != mla:
                        pass
                    elif msg_type == 'D' or msg_type == 'E':
                        state = 0
                        consumed = False

class DriveState:
    def __init__(self ):
        self.dsj = 2
        self.current_chs = (0 , 0 , 0)
        self.stat1 = 0
        self.stat2 = 0
        self.a_bit = False
        self.f_bit = True
        self.tttt = 6
        self.pp_enabled = True
        self.pp_state = False
        self.buffer_ = bytearray(256)
        self.status = bytearray(4)

    def set_pp(self, new_pp_state , io):
        new_state = self.pp_enabled and new_pp_state
        if new_state != self.pp_state:
            io.send_msg('P' , 0x80 if new_state else 0)
            self.pp_state = new_state

    def is_dsj_ok(self ):
        return self.dsj != 2

    def clear_dsj(self ):
        if self.dsj != 2:
            self.dsj = 0

    def get_current_lba(self ):
        return self.current_chs[ 2 ] + (self.current_chs[ 1 ] + self.current_chs[ 0 ] * 2) * 30

    def inc_chs(self ):
        c , h , s = self.current_chs
        s += 1
        if s >= 30:
            s = 0
            h += 1
            if h >= 2:
                h = 0
                c += 1
        self.current_chs = (c , h , s)
        
def main():
    parser = argparse.ArgumentParser(description="Barebone emulation of HP9895")
    parser.add_argument('-p' , '--port' , default = 1234 , type = int , help = "TCP port of MAME remotizer (defaults to 1234)")
    parser.add_argument('img_file' , type = argparse.FileType('r+b') , help = "Image file")
    args = parser.parse_args()
    
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
        cv = threading.Condition()
        intf = Remote488MsgIO(sock_io , cv)
        state = DriveState()
        inp = args.img_file
        for c in get_cmd(intf , 0):
            if isinstance(c , IdentifyCmd):
                intf.send_msg('D' , 0x00)
                intf.send_msg('E' , 0x81)
            elif isinstance(c , ParallelPoll):
                state.set_pp(c.state , intf)
            elif isinstance(c , ListenCmd):
                print(str(c))
                state.pp_enabled = True
                if state.is_dsj_ok():
                    if c.sec_addr == 8 and len(c.params) == 2 and c.params[ 0 ] == 3 and c.params[ 1 ] == 0:
                        # Request status
                        state.status[ 0 ] = state.stat1
                        state.status[ 1 ] = 0
                        state.status[ 2 ] = state.tttt << 1
                        res = 0
                        if state.a_bit:
                            res |= 0x80
                        if state.f_bit:
                            res |= 0x08
                        state.status[ 3 ] = res
                        state.a_bit = False
                        state.f_bit = False
                        state.stat1 = 0
                        state.clear_dsj()
                    elif c.sec_addr == 8 and len(c.params) == 6 and c.params[ 0 ] == 2 and c.params[ 1 ] == 0:
                        # Seek
                        state.current_chs = (c.params[ 3 ] , c.params[ 4 ] , c.params[ 5 ])
                        state.clear_dsj()
                        state.a_bit = True
                    elif c.sec_addr == 0x0a and len(c.params) == 2 and c.params[ 0 ] == 5 and c.params[ 1 ] == 0:
                        # Buffered read
                        print("RD ({}:{}:{})".format(state.current_chs[ 0 ] , state.current_chs[ 1 ] , state.current_chs[ 2 ]))
                        inp.seek(state.get_current_lba() * 256)
                        state.buffer_ = inp.read(256)
                        state.clear_dsj()
                        state.inc_chs()
                    elif c.sec_addr == 0x09 and len(c.params) == 2 and c.params[ 0 ] == 8 and c.params[ 1 ] == 0:
                        # Buffered write
                        pass
                    elif c.sec_addr == 0x00 and len(c.params) == 256:
                        # Receive data (actual write)
                        print("WR ({}:{}:{})".format(state.current_chs[ 0 ] , state.current_chs[ 1 ] , state.current_chs[ 2 ]))
                        inp.seek(state.get_current_lba() * 256)
                        inp.write(c.params)
                        state.buffer_ = c.params
                        state.clear_dsj()
                        state.inc_chs()
                    else:
                        print("Unknown Listen SA={:02x}".format(c.sec_addr))
                state.set_pp(True , intf)
            elif isinstance(c , TalkCmd):
                print(str(c))
                state.pp_enabled = True
                if c.sec_addr == 0:
                    if state.is_dsj_ok():
                        # Send data
                        for b in state.buffer_:
                            intf.send_msg('D' , b)
                        state.clear_dsj()
                elif c.sec_addr == 8:
                    print("Status = {:02x}:{:02x}:{:02x}:{:02x}".format(state.status[ 0 ] , state.status[ 1 ] , state.status[ 2 ] , state.status[ 3 ]))
                    intf.send_msg('D' , state.status[ 0 ])
                    intf.send_msg('D' , state.status[ 1 ])
                    intf.send_msg('D' , state.status[ 2 ])
                    intf.send_msg('D' , state.status[ 3 ])
                elif c.sec_addr == 0x10:
                    print("DSJ = {:02x}".format(state.dsj))
                    intf.send_msg('E' , state.dsj)
                    if state.dsj == 2:
                        state.dsj = 0
                    state.pp_enabled = False
                else:
                    print("Unknown Talk SA={:02x}".format(c.sec_addr))
                state.set_pp(True , intf)
                    
if __name__ == '__main__':
    main()
