#!/usr/bin/env python3
# A GUI based emulator of HP Amigo drives for use with MAME IEEE-488 remotizer
# Copyright (C) 2020-2022 F. Ulivi <fulivi at big "G" mail>
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

"""Python3 module to interface with MAME IEEE-488 remotizer
"""

import threading
import socket
import collections
import socketserver

class RemotizerEvent:
    pass

CONNECTION_OK = 0
CONNECTION_CLOSED = 1
CONNECTION_ERROR = 2

class RemotizerConnection(RemotizerEvent):
    def __init__(self , status , msg):
        self.status = status
        self.msg = msg

    msgs = {
        CONNECTION_OK : "Connected",
        CONNECTION_CLOSED : "Disconnected",
        CONNECTION_ERROR : "Connection failed"
    }

    def __str__(self):
        s = self.msgs[ self.status ]
        if self.msg:
            s += " ({})".format(self.msg)
        return s

class RemotizerMsg(RemotizerEvent):
    def __init__(self, msg_type, msg_data):
        self.msg_type = msg_type
        self.msg_data = msg_data

    def __str__(self):
        return "{}:{:02x}".format(self.msg_type , self.msg_data)

class RemotizerCP(RemotizerEvent):
    def __init__(self):
        pass

    def __str__(self):
        return "CP"

class RemotizerCPReached(RemotizerEvent):
    def __init__(self, flushed):
        self.flushed = flushed

    def __str__(self):
        return "CP reached({})".format(self.flushed)

class RemotizerDevClear(RemotizerEvent):
    def __init__(self):
        pass

    def __str__(self):
        return "Device Clear"

class RemotizerData(RemotizerEvent):
    def __init__(self , sec_addr , data , end):
        self.sec_addr = sec_addr
        self.data = data
        self.end = end

    def __str__(self):
        s = "Data len={} ".format(len(self.data))
        if self.sec_addr != None:
            s += "SA={:02x}".format(self.sec_addr)
        if self.end:
            s += " (END)"
        return s

class RemotizerTalk(RemotizerEvent):
    def __init__(self , sec_addr):
        self.sec_addr = sec_addr

    def __str__(self):
        s = "Talk "
        if self.sec_addr != None:
            s += "SA={:02x}".format(self.sec_addr)
        return s

class RemotizerIdentify(RemotizerEvent):
    def __init__(self):
        pass

    def __str__(self):
        return "ID"

class RemotizerAddressed(RemotizerEvent):
    def __init__(self , addressed):
        self.addressed = addressed

    def __str__(self):
        return "Addressed={}".format(self.addressed)

class RemotizerSerialPoll(RemotizerEvent):
    def __init__(self):
        pass

    def __str__(self):
        return "Serial Poll"

class RemotizerSPAS(RemotizerEvent):
    def __init__(self , enabled):
        self.enabled = enabled

    def __str__(self):
        return "SPAS={}".format(self.enabled)

# Debug masks
DBG_ENQUEUED = 1
DBG_CMD = 2
DBG_IN_MSG = 4
DBG_OUT_MSG = 8
DBG_SP = 16
DBG_ALL = 0x1f

class RemotizerIO:
    def _enqueue(self , obj):
        if self.debug and self.debug_mask & DBG_ENQUEUED:
            print("Q:{}".format(str(obj)) , file = self.debug)
        with self.cv:
            self.q.append(obj)
            self.cv.notify()

    def _init_488(self):
        # 0: idle
        # 1: TADS (got MTA)
        # 2: LADS (got MLA)
        # 3: SPAS
        self.hpib_state = 0
        # 0: NONE
        # 1: PACS
        # 2: TPAS
        # 3: LPAS
        # 4: UNT
        self.sa_state = 0
        # 0: NPRS
        # 1: SRQS
        # 2: APRS
        self.sr_state = 0
        self.rsv_state = False
        self.srq_state = None
        self.wait_sb_cp = False
        self.spms = False
        self.signals = 0x1f
        self.addressed = False
        self.next_event = None
        self.pp_state = None
        self.accum = bytearray()
        self._sr_fsm()

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

    def _print_cmd(self , byte , out):
        code = byte & 0x7f
        parity = (byte >> 4) ^ (byte & 0x0f)
        parity = (parity >> 2) ^ (parity & 0x03)
        parity = (parity >> 1) ^ (parity & 0x01)
        par_msg = "(E)" if parity == 0 else "(O)"
        if code in self.CMDS:
            s = self.CMDS[ code ]
        elif (code & 0x60) == 0x20:
            s = "LA {:02x}".format(code & 0x1f)
        elif (code & 0x60) == 0x40:
            s = "TA {:02x}".format(code & 0x1f)
        elif (code & 0x60) == 0x60:
            s = "SA {:02x}".format(code & 0x1f)
        else:
            s = "???"
        print(s , par_msg , file = out)

    def _sr_fsm(self):
        with self.sr_lock:
            save = self.sr_state
            if self.sr_state == 0:
                # NPRS state
                if self.rsv_state and self.hpib_state != 3:
                    self.sr_state = 1
            elif self.sr_state == 1:
                # SRQS state
                if self.hpib_state == 3:
                    self.sr_state = 2
                elif not self.rsv_state:
                    self.sr_state = 0
            else:
                # APRS state
                if self.hpib_state != 3 and not self.rsv_state:
                    self.sr_state = 0
            if save != self.sr_state and self.debug and self.debug_mask & DBG_SP:
                print("SR {}->{}".format(save , self.sr_state) , file = self.debug)
            # Send SRQ signal
            srq = self.sr_state == 1
            if self.srq_state != srq:
                if self.debug and self.debug_mask & DBG_SP:
                    print("SRQ {}".format(srq) , file = self.debug)
                self.srq_state = srq
                self.send_msg('R' if self.srq_state else 'S' , 8)

    def _send_status_byte(self):
        b = self.status_byte
        if self.sr_state == 2:
            b |= 0x40
        self.send_msg('D' , b)
        self.send_checkpoint()
        self.wait_sb_cp = True

    def _set_addressed(self , addressed):
        if addressed != self.addressed:
            self.addressed = addressed
            self._enqueue(RemotizerAddressed(addressed))

    def _flush_accum(self):
        if self.accum:
            self._enqueue(RemotizerData(self.sec_addr , self.accum , False))
            self.accum = bytearray()

    def _fsm488_D(self , msg_data):
        if (self.signals & 1) == 0:
            # Command byte (ATN is asserted)
            if self.debug and self.debug_mask & DBG_CMD:
                self._print_cmd(msg_data , self.debug)
            msg_data &= 0x7f
            is_pcg = (msg_data & 0x60) != 0x60
            # Commands not implemented:
            # 01        Go To Local
            # 08        Group Execute Trigger
            # 09        Take Control
            # 11        Local Lock-Out
            # 1f        CFE
            if is_pcg:
                self.sa_state = 0

            if (msg_data == 0x04 and self.hpib_state == 2) or msg_data == 0x14:
                # Selected Device Clear or Device Clear
                self._enqueue(RemotizerDevClear())
            elif msg_data == 0x05 and self.hpib_state == 2:
                # PPC
                self.sa_state = 1
            elif msg_data == 0x15:
                # PPU
                # TODO:
                pass
            elif msg_data == 0x18:
                # SPE
                self.spms = True
            elif msg_data == 0x19:
                # SPD
                self.spms = False
            elif msg_data == self.mla:
                # MLA
                # -> LADS
                self.hpib_state = 2
                # -> LPAS
                self.sa_state = 3
                self.next_event = None
                self._flush_accum()
                self.sec_addr = None
                if not self.has_sa:
                    self._set_addressed(True)
            elif msg_data == 0x3f and self.hpib_state == 2:
                # UNL
                # -> idle
                self.hpib_state = 0
                self._flush_accum()
                self._set_addressed(False)
            elif msg_data == self.mta:
                # MTA
                # -> TADS
                self.hpib_state = 1
                # -> TPAS
                self.sa_state = 2
                self._flush_accum()
                self.next_event = RemotizerTalk(None)
                if not self.has_sa:
                    self._set_addressed(True)
            elif (msg_data & 0x60) == 0x40:
                # OTA or UNT
                if self.hpib_state == 1:
                    self.hpib_state = 0
                    self.next_event = None
                    self._set_addressed(False)
                if msg_data == 0x5f:
                    # -> UNT
                    self.sa_state = 4
            elif not is_pcg:
                # Secondary address
                if self.sa_state == 1:
                    # PPE / PPD
                    # TODO:
                    pass
                elif self.sa_state == 2:
                    # MTA + SA
                    self.next_event = RemotizerTalk(msg_data & 0x1f)
                    self._set_addressed(True)
                elif self.sa_state == 3:
                    # MLA + SA
                    self.sec_addr = msg_data & 0x1f
                    self._set_addressed(True)
                elif self.sa_state == 4 and msg_data == self.msa:
                    # UNT + SA
                    self.next_event = RemotizerIdentify()
        elif self.hpib_state == 2:
            # DAB
            self.accum.append(msg_data)
            if len(self.accum) == 256:
                self._flush_accum()

    def _fsm488_E(self , msg_data):
        if self.hpib_state == 2 and (self.signals & 1) != 0:
            self.accum.append(msg_data)
            self._enqueue(RemotizerData(self.sec_addr , self.accum , True))
            self.accum = bytearray()

    def _fsm488_J(self , msg_data):
        with self.lock:
            self.conn.sendall(b"K:00\n")

    def _fsm488_R(self , msg_data):
        # Reset/assert signals
        save = self.signals
        self.signals &= ~msg_data
        if save & 1 and not self.signals & 1 and self.hpib_state == 3:
            # ATN asserted & SPAS
            # -> TADS
            self.hpib_state = 1
            self._sr_fsm()
            self._enqueue(RemotizerSPAS(False))

    def _fsm488_S(self , msg_data):
        # Set/de-assert signals
        save = self.signals
        self.signals |= msg_data
        if not save & 1 and self.signals & 1:
            # ATN de-asserted
            if self.hpib_state == 1 and self.spms:
                # -> SPAS
                self.hpib_state = 3
                self.next_event = None
                self._enqueue(RemotizerSPAS(True))
                with self.sr_lock:
                    self._sr_fsm()
                    # Send status byte
                    self._send_status_byte()
            if self.next_event != None:
                self._enqueue(self.next_event)
                self.next_event = None

    def _fsm488_X(self , msg_data):
        self._flush_accum()
        if self.auto_cp:
            # Send back automatic "checkpoint reached"
            self.send_msg('Y' , 0)
        else:
            # Else defer to module user
            self._enqueue(RemotizerCP())

    def _fsm488_Y(self , msg_data):
        # Checkpoint reached at the far end
        if self.wait_sb_cp:
            # Checkpoint on status byte
            self.wait_sb_cp = False
            if not msg_data:
                self._enqueue(RemotizerSerialPoll())
            # Ideally, the device should keep sending the status byte if
            # the controller keeps accepting it. But:
            # 1. it's very uncommon for the controller to accept more than 1 byte
            # 2. the repeated sending of the byte introduces a nice race condition
            #    between sender and controller when it asserts ATN to stop the device
            # For these reasons we send the status byte just once.
            #    if self.hpib_state == 3:
            #        self._send_status_byte()
        else:
            self._enqueue(RemotizerCPReached(msg_data != 0))

    MSGS = {
        "D" : _fsm488_D,
        "E" : _fsm488_E,
        "J" : _fsm488_J,
        "R" : _fsm488_R,
        "S" : _fsm488_S,
        "X" : _fsm488_X,
        "Y" : _fsm488_Y,
    }

    def _parse_msgs(self , gen):
        parser_state = 0
        for ins in gen:
            for b in ins:
                c = chr(b)
                if parser_state == 0:
                    msg_type = c
                    fsm_fn = self.MSGS.get(c)
                    if fsm_fn != None:
                        parser_state = 1
                    elif not c.isspace():
                        parser_state = 5
                elif parser_state == 1:
                    if c == ':':
                        parser_state = 2
                    else:
                        parser_state = 5
                elif parser_state == 2:
                    try:
                        data = int(c , 16)
                        parser_state = 3
                    except ValueError:
                        parser_state = 5
                elif parser_state == 3:
                    try:
                        data = (data << 4) + int(c , 16)
                        parser_state = 4
                    except ValueError:
                        parser_state = 5
                elif parser_state == 4:
                    if c.isspace() or c == ',' or c == ';':
                        parser_state = 0
                        yield msg_type , fsm_fn , data
                    else:
                        parser_state = 5
                else:
                    if c.isspace() or c == ',' or c == ';':
                        parser_state = 0

    def _rem_recv(self , conn):
        while True:
            try:
                ins = conn.recv(4096)
                if len(ins) == 0:
                    break
                else:
                    yield ins
            except ConnectionError:
                break

    def _conn_close(self ):
        if self.state == 2:
            with self.lock:
                self.conn.close()
            self._enqueue(RemotizerConnection(CONNECTION_CLOSED , None))
            if self.keep_open:
                self.state = 1
            else:
                self.state = 3

    def __my_th(self):
        while True:
            if self.state == 0:
                try:
                    self.io = socket.socket()
                    self.io.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self.io.bind(('0.0.0.0' , self.port))
                    self.state = 1
                except ConnectionError as e:
                    self.state = 3
                    self._enqueue(RemotizerConnection(CONNECTION_ERROR , str(e)))
            elif self.state == 1:
                try:
                    self.io.listen(1)
                    #with self.lock:
                    self.conn , addr = self.io.accept()
                    self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    self._enqueue(RemotizerConnection(CONNECTION_OK , str(addr)))
                    self.state = 2
                    self._init_488()
                except ConnectionError as e:
                    self.state = 3
                    self._enqueue(RemotizerConnection(CONNECTION_ERROR , str(e)))
            elif self.state == 2:
                for msg_type , fsm_fn , data in self._parse_msgs(self._rem_recv(self.conn)):
                    if self.debug and self.debug_mask & DBG_IN_MSG:
                        print("{}:{:02x}<".format(msg_type , data) , file = self.debug)
                        #self._enqueue(RemotizerMsg(msg_type , data))
                    fsm_fn(self , data)
                self._conn_close()
            else:
                return

    def __init__(self , port , has_sa , keep_open = True , auto_cp = True , * , debug = None , debug_mask = DBG_ALL):
        self.debug = debug
        self.debug_mask = debug_mask
        self.port = port
        self.has_sa = has_sa
        self.keep_open = keep_open
        self.auto_cp = auto_cp
        # 0     Create socket
        # 1     Waiting for connection
        # 2     Connected
        # 3     Connection failed
        self.state = 0
        # Set default address to 0
        self.set_address(0)
        # No default PP response
        self.pp_mask = 0
        # This mutex serializes sending of msgs to socket
        self.lock = threading.RLock()
        self.conn = None
        # This mutex protects the SR FSM
        self.sr_lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.q = collections.deque()
        self._init_488()
        self.status_byte = 0
        self.th = threading.Thread(target = self.__my_th)
        self.th.daemon = True
        self.th.start()

    def set_address(self , address):
        # A mutex here wouldn't hurt..
        if self.state != 2:
            self.hpib_addr = address & 0x1f
            self.mta = self.hpib_addr | 0x40
            self.mla = self.hpib_addr | 0x20
            self.msa = self.hpib_addr | 0x60

    def has_events(self):
        return len(self.q) > 0

    # Events that are returned by this function:
    # RemotizerConnection   Status of remotizer connection
    # RemotizerCP           Request for checkpoint received
    # RemotizerCPReached    Checkpoint received at remote end
    # RemotizerDevClear     SDC or DCL received
    # RemotizerData         Listened data
    # RemotizerTalk         Enabled to talk
    # RemotizerIdentify     Identify sequence received
    # RemotizerAddressed    Addressed or unaddressed
    # RemotizerSerialPoll   Serial poll received
    # RemotizerSPAS         SPAS state on/off
    def get_event(self , timeout = None):
        with self.cv:
            if self.cv.wait_for(lambda : self.has_events() , timeout):
                return self.q.popleft()
            else:
                return None

    def send_msg(self, msg_type , msg_data):
        b = bytes("{}:{:02x},".format(msg_type , msg_data) , encoding = "ascii")
        if self.debug and self.debug_mask & DBG_OUT_MSG:
            print("{}:{:02x}>".format(msg_type , msg_data) , file = self.debug)
        try:
            with self.lock:
                if self.conn:
                    self.conn.sendall(b)
        except ConnectionError:
            pass
        except OSError:
            pass

    def talk_data(self, data , eoi_at_end = False):
        last_dab = len(data)
        add_eoi = eoi_at_end and last_dab > 0
        if add_eoi:
            last_dab -= 1
        with self.lock:
            for b in data[ :last_dab ]:
                self.send_msg('D' , b)
            if add_eoi:
                self.send_msg('E' , data[ last_dab ])

    def set_pp_response(self , pp_mask):
        self.pp_mask = pp_mask

    def send_pp_state(self , state):
        new_state = self.pp_mask if state else 0
        if new_state != self.pp_state:
            self.pp_state = new_state
            self.send_msg("P" , self.pp_state)

    def send_checkpoint(self):
        self.send_msg("X" , 0)

    def send_checkpoint_reached(self , flushed = False):
        self.send_msg("Y" , int(flushed))

    def set_rsv_state(self , rsv):
        with self.sr_lock:
            self.rsv_state = rsv
            self._sr_fsm()

    def set_status_byte(self , b):
        self.status_byte = b & 0xbf

    def force_data(self , data):
        self._enqueue(RemotizerData(None , data , False))
