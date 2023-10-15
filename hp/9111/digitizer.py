#!/usr/bin/env python3
# A HP9111 emulator for use with MAME IEEE-488 remotizer
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

import rem488
import functools
import re
import sys
import threading
import struct
from PyQt6 import QtCore

class ParseError:
    def __init__(self , s):
        self.s = s

    def __str__(self):
        return "Parse Error:" + self.s

class ParsedCmd:
    def __init__(self , cmd , args):
        self.cmd = cmd
        self.args = args

    def __str__(self):
        return "cmd={}, args={}".format(self.cmd , self.args)

class Parser:
    def __init__(self ):
        self.cmd_re = re.compile(r"[a-z][a-z]" , re.I)
        self.arg_re = re.compile(r"-?\d+")
        self.rest = ""

    def parse_arg(self , piece):
        piece_strip = piece.strip()
        if piece_strip:
            mo = self.arg_re.match(piece_strip)
            if mo:
                arg = int(mo.group())
                return arg
            else:
                return ParseError(piece)
        else:
            return None

    def parse_cmd(self , piece):
        mo = self.cmd_re.match(piece)
        if mo:
            cmd = mo.group().upper()
            pieces = piece[ mo.end(): ].split(",")
            args = [ self.parse_arg(p) for p in pieces ]
            # Collapse a no-argument condition into an empty list
            if args == [ None ]:
                args = []
            return ParsedCmd(cmd , args)
        else:
            return ParseError(piece)

    def parse(self , s):
        tot = self.rest + s
        # First split on semicolons
        pieces = tot.split(";")
        # Then split on \n
        pieces = functools.reduce(lambda a , b: a + b.split("\n") , pieces , [])
        if len(pieces) < 2:
            self.rest = tot
        else:
            for p in pieces[ :-1 ]:
                p_strip = p.lstrip()
                if p_strip:
                    yield self.parse_cmd(p_strip)
            self.rest = pieces[ -1 ]

class TooManyArgs(Exception):
    pass

class InvalidArg(Exception):
    pass

class SuspendCmd(Exception):
    pass

class Digitizer(QtCore.QObject):
    # *****************
    # **** Signals ****
    # *****************
    #
    # LED status
    # LED order: DIGITIZE, MENU, ERROR
    led_state = QtCore.pyqtSignal(bool , bool , bool)
    # Beep note
    # Params: note [0..48] , duration (ms), amplitude [0..5]
    play_note = QtCore.pyqtSignal(int , int , int)
    # Start/stop internal timer
    __start_timer = QtCore.pyqtSignal(int)
    __stop_timer = QtCore.pyqtSignal()

    def __init__(self , rem , parent = None):
        QtCore.QObject.__init__(self , parent)
        self.rem = rem
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.timer_to)
        self.__start_timer.connect(self.timer.start)
        self.__stop_timer.connect(self.timer.stop)
        self.leds = [ False ] * 3
        self.parser = Parser()
        self.cmd_gen = None
        self.suspended = False
        self.output = None
        self.pen_lock = threading.RLock()
        self.cursor_pos = (0 , 0)
        self.tmp_pos = (0 , 0)
        self.digitized_pos = (0 , 0)
        self.digitized_press = None
        self.pen_press = False
        self.tmp_press = False
        self.pen_prox = False
        self.tmp_prox = False
        self.sel_sk = 0
        # 0     Idle
        # 1     Waiting for bin data to be available
        # 2     Bin data sent, waiting for CP
        self.fsm_bin_data = 0
        self.srq_pending = False
        self.spas_enabled = False

    # Led indexes
    LED_DIGITIZE = 0
    LED_MENU = 1
    LED_ERROR = 2

    # FSM states
    ST_START = 0
    ST_SELF_TEST = 1
    ST_IDLE = 2
    ST_SN_NO_DGTZ = 3
    ST_SN_DGTZ = 4
    ST_SF_NO_DGTZ = 5
    ST_SF_DGTZ = 6
    ST_SG = 7
    ST_DP = 8

    def set_defaults(self):
        self.srq_pending = False
        self.rem.set_rsv_state(False)
        self.set_in_masks = (7 , 0 , 0)
        self.set_status_0(0x2ac)
        self.set_error(0)
        self.set_data_rate = 60
        self.set_sk_on = True
        self.sel_sk = 0
        self.fsm = self.ST_IDLE
        self.digitized_press = None
        # Switch mode
        # 0     Switch normal
        # 1     Switch follow
        self.set_switch_mode = 0
        self.set_beep = (12 , 150 , 4)
        self.set_led(self.LED_DIGITIZE , False)
        self.set_led(self.LED_MENU , False)
        self.set_led(self.LED_ERROR , False)

    def initialize(self):
        # Force status update
        self.status = 8
        self.set_defaults()
        self.set_p1 = (400 , 400)
        self.set_p2 = (11632 , 8340)
        self.beep_list = []
        self.fsm = self.ST_START
        self.set_led(self.LED_DIGITIZE , True)
        self.set_led(self.LED_MENU , True)
        self.set_led(self.LED_ERROR , True)
        # Hello tone
        self.start_beep([ (12 , 150 , 5) , (16 , 150 , 5) , (19 , 150 , 5) ])

    # SLOT
    def pen_position(self , x , y , press):
        with self.pen_lock:
            self.tmp_pos = (x , y)
            self.tmp_press = press

    # SLOT
    def pen_proximity(self , prox):
        with self.pen_lock:
            self.tmp_prox = prox

    def start_beep(self , b):
        self.beep_list.extend(b)
        if self.beep_list:
            self.beep_single(self.beep_list[ 0 ])

    def beep_single(self , b):
        note , dur , amp = b
        self.set_status_0(0x10)
        self.play_note.emit(note , dur , amp)
        self.__stop_timer.emit()

    def set_error(self , err_no):
        if err_no == 0:
            # Clear error
            self.set_led(self.LED_ERROR , False)
            self.err_no = 0
            self.set_status_0(0x20)
        elif (1 << (err_no - 1)) & self.set_in_masks[ 0 ]:
            self.set_led(self.LED_ERROR , True)
            self.err_no = err_no
            self.set_status_1(0x20)
            # TODO: Play ERROR tone (36,50,3),34,32,30

    def update_status(self , prev_status):
        self.rem.set_status_byte(self.status & 0xbf)
        to_1 = ~prev_status & self.status
        if to_1 & self.set_in_masks[ 1 ]:
            #print("RSV=1 {:03x} {:03x} {:03x}".format(prev_status , self.status , self.set_in_masks[ 1 ]))
            self.rem.set_rsv_state(True)
            self.srq_pending = True
        elif self.srq_pending and (self.status & self.set_in_masks[ 1 ]) == 0:
            #print("RSV=0 {:03x} {:03x} {:03x}".format(prev_status , self.status , self.set_in_masks[ 1 ]))
            self.rem.set_rsv_state(False)
            self.srq_pending = False
        self.rem.send_pp_state(self.status & self.set_in_masks[ 2 ])

    def set_status(self , status):
        if self.status != status:
            save = self.status
            self.status = status
            self.update_status(save)

    def set_status_1(self , mask):
        self.set_status(self.status | mask)

    def set_status_0(self , mask):
        self.set_status(self.status & ~mask)

    def ev_serial_poll(self , ev):
        #print("RSV=0 {:03x} {:03x}".format(self.status , self.set_in_masks[ 1 ]))
        self.rem.set_rsv_state(False)
        self.srq_pending = False

    def set_led(self , led_no , state):
        if self.leds[ led_no ] != state:
            self.leds[ led_no ] = state
            self.led_state.emit(self.leds[ 0 ] , self.leds[ 1 ] , self.leds[ 2 ])

    def start_sampling(self):
        if self.fsm != self.ST_START:
            ms = round(1000.0 / self.set_data_rate)
            self.__start_timer.emit(ms)

    def is_in_main_area(self ):
        return self.pen_prox and (not self.set_sk_on or self.cursor_pos[ 1 ] < 8832)

    def is_in_menu(self ):
        return self.pen_prox and self.set_sk_on and self.cursor_pos[ 1 ] >= 8832

    def clicked_sk(self):
        if self.set_sk_on and (8832 <= self.cursor_pos[ 1 ] <= 9544):
            x = self.cursor_pos[ 0 ]
            if -92 <= x < 2752:
                first = 1
                off = x + 92
            elif 3032 <= x < 5876:
                first = 5
                off = x - 3032
            elif 6156 <= x < 9000:
                first = 9
                off = x - 6156
            elif 9280 <= x < 12124:
                first = 13
                off = x - 9280
            else:
                return 0
            return first + (off // 711)
        else:
            return 0

    def clear_sk(self):
        self.sel_sk = 0
        self.set_status_0(0x80)
        self.set_led(self.LED_MENU , False)

    def clear_digitize(self):
        self.set_status_0(4)
        self.digitized_press = None

    def start_sf(self):
        if self.pen_prox and self.pen_press:
            self.fsm = self.ST_SF_DGTZ
        else:
            self.fsm = self.ST_SF_NO_DGTZ

    def normalize_args(self , args , std_arg_no):
        l = len(args)
        if l > std_arg_no:
            raise TooManyArgs()
        else:
            extra = [ None ] * (std_arg_no - l)
            args.extend(extra)

    # BP: BEEP
    def cmd_BP(self , args):
        self.normalize_args(args , 3)
        note , dur , amp = args
        if note == None:
            note = self.set_beep[ 0 ]
        elif note < 0 or note > 255:
            raise InvalidArg()
        else:
            note = min(note , 48)
        if dur == None:
            dur = self.set_beep[ 1 ]
        elif dur < 1 or dur > 32767:
            raise InvalidArg()
        if amp == None:
            amp = self.set_beep[ 2 ]
        elif amp < 0 or amp > 5:
            raise InvalidArg()
        self.set_beep = (note , dur , amp)
        self.start_beep([ self.set_beep ])
        raise SuspendCmd()

    # CN: Continuous sampling mode
    def cmd_CN(self , args):
        self.normalize_args(args , 2)
        if self.set_switch_mode == 0:
            self.fsm = self.ST_SN_NO_DGTZ
        else:
            self.start_sf()
        self.set_led(self.LED_DIGITIZE , True)
        return None

    # CR: Cursor rate
    def cmd_CR(self , args):
        self.normalize_args(args , 1)
        rate = args[ 0 ]
        if rate == None:
            rate = 60
        elif rate < 1 or rate > 60:
            raise InvalidArg()
        if rate != self.set_data_rate:
            self.set_data_rate = rate
            self.start_sampling()
        return None

    # DC: Digitizer clear
    def cmd_DC(self , args):
        self.normalize_args(args , 0)
        self.fsm = self.ST_IDLE
        self.set_led(self.LED_DIGITIZE , False)
        self.clear_digitize()
        return None

    # DF: Set defaults
    def cmd_DF(self , args):
        self.normalize_args(args , 0)
        self.set_defaults()
        self.set_status_1(0x10)
        return None

    # DP: Digitize point
    def cmd_DP(self , args):
        self.normalize_args(args , 0)
        if self.fsm == self.ST_IDLE:
            self.fsm = self.ST_DP
            self.set_led(self.LED_DIGITIZE , True)
        return None

    # IM: Set input mask
    def cmd_IM(self , args):
        if len(args) == 0:
            self.set_in_masks = (7 , 0 , 0)
        else:
            self.normalize_args(args , 3)
            im0 , im1 , im2 = args
            if im0 == None:
                im0 = self.set_in_masks[ 0 ]
            elif im0 < 0 or im0 > 32767:
                raise InvalidArg()
            elif im0 & ~0x47:
                im0 = 7
            if im1 == None:
                im1 = self.set_in_masks[ 1 ]
            elif im1 < 0 or im1 > 32767:
                raise InvalidArg()
            elif im1 & ~0x3bc:
                im1 = 0
            if im2 == None:
                im2 = self.set_in_masks[ 2 ]
            elif im2 < 0 or im2 > 32767:
                raise InvalidArg()
            elif im2 & ~0x3bc:
                im2 = 0
            self.set_in_masks = (im0 , im1 , im2)
        self.update_status(self.status)
        return None

    # IN: Initialize
    def cmd_IN(self , args):
        self.normalize_args(args , 0)
        self.initialize()
        return None

    # IP: Input points
    def cmd_IP(self , args):
        if len(args) == 0:
            self.set_p1 = (400 , 400)
            self.set_p2 = (11632 , 8340)
        else:
            self.normalize_args(args , 4)
            p1x , p1y , p2x , p2y = args
            if p1x == None:
                p1x = self.set_p1[ 0 ]
            elif p1x < -999999 or p1x > 999999:
                raise InvalidArg()
            if p1y == None:
                p1y = self.set_p1[ 1 ]
            elif p1y < -999999 or p1y > 999999:
                raise InvalidArg()
            if p2x == None:
                p2x = self.set_p2[ 0 ]
            elif p2x < -999999 or p2x > 999999:
                raise InvalidArg()
            if p2y == None:
                p2y = self.set_p2[ 1 ]
            elif p2y < -999999 or p2y > 999999:
                raise InvalidArg()
            self.set_p1 = (p1x , p1y)
            self.set_p2 = (p2x , p2y)
        return None

    # OA: Output actual stylus pos
    def cmd_OA(self , args):
        return self.cmd_OC(args)

    # OC: Output cursor
    def cmd_OC(self , args):
        self.normalize_args(args , 0)
        save = self.status
        self.set_status_0(0x200)
        return "{},{},{},{:02d},{:04d},{}\r\n".format(self.cursor_pos[ 0 ] , self.cursor_pos[ 1 ] , int(self.pen_press) , self.sel_sk , save , self.err_no)

    # OD: Output digitized point
    def cmd_OD(self , args):
        self.normalize_args(args , 0)
        # Apparently OD also clears the cursor status bit. This behaviour is undocumented
        # but it's needed by the tracking function in the enhanced graphic ROM.
        self.set_status_0(0x204)
        if self.fsm <= self.ST_IDLE:
            self.set_error(1)
            return "0,0,-1\r\n"
        elif self.digitized_press == None:
            # TODO:
            print("OD with no point!")
            return "0,0,-1\r\n"
        else:
            return "{},{},{}\r\n".format(self.digitized_pos[ 0 ] , self.digitized_pos[ 1 ] , int(self.digitized_press))

    # OE: Output error
    def cmd_OE(self , args):
        self.normalize_args(args , 0)
        save = self.err_no
        self.set_error(0)
        return "{}\r\n".format(save)

    # OF: Output factor
    def cmd_OF(self , args):
        self.normalize_args(args , 0)
        return "40,40\r\n"

    # OI: Output identification
    def cmd_OI(self , args):
        self.normalize_args(args , 0)
        return "9111A\r\n"

    # OK: Output key
    def cmd_OK(self , args):
        self.normalize_args(args , 0)
        # See "OD" command. This one, too, seems to clear cursor bit.
        self.set_status_0(0x280)
        if self.sel_sk:
            return "{}\r\n".format(1 << (self.sel_sk - 1))
        else:
            return "0\r\n"

    # OP: Output points
    def cmd_OP(self , args):
        self.normalize_args(args , 0)
        return "{},{},{},{}\r\n".format(self.set_p1[ 0 ] , self.set_p1[ 1 ] , self.set_p2[ 0 ] , self.set_p2[ 1 ])

    # OR: Output resolution
    def cmd_OR(self , args):
        self.normalize_args(args , 0)
        return ".025,.025\r\n"

    # OS: Output status
    def cmd_OS(self , args):
        self.normalize_args(args , 0)
        save = self.status
        self.set_status_0(8)
        return "{}\r\n".format(save)

    # RC: Read cursor
    def cmd_RC(self , args):
        return self.cmd_OC(args)

    # RS: Read softkey
    def cmd_RS(self , args):
        self.normalize_args(args , 1)
        en = args[ 0 ]
        if en == None:
            en = 1
        elif en < 0 or en > 1:
            raise InvalidArg()
        self.set_sk_on = en != 0
        save = self.sel_sk
        self.clear_sk()
        return "{}\r\n".format(save)

    # SF: Switch follow mode
    def cmd_SF(self , args):
        self.normalize_args(args , 0)
        self.set_switch_mode = 1
        if self.fsm == self.ST_SN_NO_DGTZ or self.fsm == self.ST_SN_DGTZ:
            self.start_sf()
        return None

    # SG: Single-sample mode
    def cmd_SG(self , args):
        self.normalize_args(args , 0)
        self.fsm = self.ST_SG
        self.clear_digitize()
        self.set_led(self.LED_DIGITIZE , True)
        return None

    # SK: Set Key
    def cmd_SK(self , args):
        self.normalize_args(args , 1)
        self.clear_sk()
        return None

    # SN: Switch normal mode
    def cmd_SN(self , args):
        self.normalize_args(args , 0)
        self.set_switch_mode = 0
        if self.fsm == self.ST_SF_NO_DGTZ or self.fsm == self.ST_SF_DGTZ:
            self.fsm = self.ST_SN_NO_DGTZ
        return None

    NOPS = [ "AN" , "AT" , "AV" , "CC" , "DD" , "DR" , "IW" , "LB" , "LT" , "PA" , "PC" , "PD" , "PG" , "PU" , "RV" , "SL" , "SP" , "SR" ]

    def exec_cmd(self):
        for cmd in self.cmd_gen:
            self.output = None
            if isinstance(cmd , ParsedCmd):
                if cmd.cmd in self.NOPS:
                    error = 0
                else:
                    cmd_fn = "cmd_" + cmd.cmd
                    d = self.__class__.__dict__
                    cmd_m = d.get(cmd_fn)
                    if cmd_m:
                        try:
                            res = cmd_m(self , cmd.args)
                            if res:
                                self.output = res.encode("ascii" , "ignore")
                            error = 0
                        except SuspendCmd:
                            self.set_error(0)
                            self.suspended = True
                            return
                        except TooManyArgs:
                            error = 2
                        except InvalidArg:
                            error = 3
                    else:
                        print("unknown cmd {}".format(cmd.cmd))
                        error = 1
            elif isinstance(cmd , ParseError):
                error = 1
            self.set_error(error)
        self.cmd_gen = None

    def ev_listen_data(self , ev):
        if self.is_busy():
            return

        if self.cmd_gen:
            # Suspended commands present
            self.exec_cmd()

        if not self.cmd_gen:
            s = str(ev.data , encoding = "ascii" , errors = "replace")
            if ev.end:
                s += ";"
            self.cmd_gen = self.parser.parse(s)
            self.exec_cmd()

    def send_binary_data(self):
        output = struct.pack(">hhH" , self.cursor_pos[ 0 ] , self.cursor_pos[ 1 ] , self.status)
        self.rem.talk_data(output , True)
        self.rem.send_checkpoint()
        self.fsm_bin_data = 2

    def ev_talk(self , ev):
        if not self.output:
            # Binary data
            if self.status & 0x200:
                self.send_binary_data()
            else:
                self.fsm_bin_data = 1
        else:
            self.rem.talk_data(self.output , True)
            self.rem.send_checkpoint()
            self.output = None
            self.fsm_bin_data = 0

    def ev_addressed(self , ev):
        if not ev.addressed:
            self.fsm_bin_data = 0

    def ev_spas(self , ev):
        self.spas_enabled = ev.enabled

    def ev_cp_reached(self , ev):
        if ev.flushed:
            self.fsm_bin_data = 0
        elif self.fsm_bin_data == 2:
            self.set_status_0(0x200)
            self.fsm_bin_data = 1

    def ev_dev_clear(self , ev):
        # DEV CLEAR, do nothing ATM
        pass

    EV_FNS = {
        rem488.RemotizerCPReached : ev_cp_reached,
        rem488.RemotizerDevClear  : ev_dev_clear,
        rem488.RemotizerData      : ev_listen_data,
        rem488.RemotizerTalk      : ev_talk,
        rem488.RemotizerSerialPoll: ev_serial_poll,
        rem488.RemotizerAddressed : ev_addressed,
        rem488.RemotizerSPAS      : ev_spas
        }

    def ev_dispatch(self , ev):
        fn = self.EV_FNS.get(ev.__class__ , None)
        if fn:
            fn(self , ev)

    def timer_to(self):
        # Fetch new sample
        with self.pen_lock:
            # Do nothing when SPAS enabled
            if self.spas_enabled:
                return
            self.cursor_pos = self.tmp_pos
            click = not self.pen_press and self.tmp_press and self.pen_prox and self.tmp_prox
            self.pen_press = self.tmp_press
            new_status = self.status
            if self.pen_press:
                new_status |= 0x400
            else:
                new_status &= ~0x400
            self.pen_prox = self.tmp_prox
            if self.pen_prox:
                new_status |= 0x300
            else:
                new_status &= ~0x100
            if self.is_in_menu() and click:
                sk = self.clicked_sk()
                if sk:
                    if self.sel_sk == sk:
                        new_status &= ~0x80
                        self.set_led(self.LED_MENU , False)
                        self.sel_sk = 0
                    else:
                        new_status |= 0x80
                        self.set_led(self.LED_MENU , True)
                        self.sel_sk = sk
            if self.is_in_main_area():
                take_press = None
                if click:
                    if self.fsm == self.ST_SN_NO_DGTZ:
                        self.fsm = self.ST_SN_DGTZ
                        take_press = True
                    elif self.fsm == self.ST_SN_DGTZ:
                        self.fsm = self.ST_SN_NO_DGTZ
                        take_press = False
                    elif self.fsm == self.ST_SF_NO_DGTZ:
                        self.fsm = self.ST_SF_DGTZ
                        take_press = True
                    elif self.fsm == self.ST_SG:
                        take_press = True
                    elif self.fsm == self.ST_DP:
                        self.fsm = self.ST_IDLE
                        take_press = True
                else:
                    if self.fsm == self.ST_SF_DGTZ:
                        take_press = self.pen_press
                        if not take_press:
                            self.fsm = self.ST_SF_NO_DGTZ
                    elif self.fsm == self.ST_SN_DGTZ:
                        take_press = True
                if take_press != None:
                    self.set_led(self.LED_DIGITIZE , False)
                    self.digitized_pos = self.cursor_pos
                    self.digitized_press = take_press
                    new_status |= 4
            self.set_status(new_status)
            if self.fsm_bin_data == 1 and self.status & 0x200:
                self.send_binary_data()

    # SLOT
    def note_ended(self):
        if self.beep_list:
            self.beep_list.pop(0)
            if self.beep_list:
                self.beep_single(self.beep_list[ 0 ])
            else:
                # Beeping ends
                self.set_status_1(0x10)
                self.suspended = False
                if self.cmd_gen:
                    # Resume executing suspended commands
                    self.exec_cmd()
                if not self.suspended:
                    if self.fsm == self.ST_START:
                        self.fsm = self.ST_IDLE
                        self.set_led(self.LED_DIGITIZE , False)
                        self.set_led(self.LED_MENU , False)
                        self.set_led(self.LED_ERROR , False)
                        self.set_status_1(8)
                    self.start_sampling()

    def is_busy(self):
        return len(self.beep_list) > 0 or self.suspended

class DigitizerIO(QtCore.QThread):
    # *****************
    # **** Signals ****
    # *****************
    #
    # Report connection status
    # 1st parameter is one of rem488.CONNECTION_*
    status_connect = QtCore.pyqtSignal(int , str)
    # LED status
    # LED order: DIGITIZE, MENU, ERROR
    led_state = QtCore.pyqtSignal(bool , bool , bool)
    # Play note
    play_note = QtCore.pyqtSignal(int , int , int)

    def __init__(self , port):
        QtCore.QThread.__init__(self)
        #self.rem = rem488.RemotizerIO(port , False , True , False , debug = sys.stdout , debug_mask = rem488.DBG_CMD | rem488.DBG_IN_MSG | rem488.DBG_OUT_MSG)
        self.rem = rem488.RemotizerIO(port , False , True , False)
        self.rem.set_address(6)
        self.rem.set_pp_response(0x02)

    def run(self):
        self.digitizer = Digitizer(self.rem)
        self.digitizer.led_state.connect(self.led_state)
        self.digitizer.play_note.connect(self.play_note)
        self.digitizer.initialize()
        evd = self.eventDispatcher()
        il = 0
        while True:
            run_evd = True
            if self.digitizer.is_busy():
                self.msleep(10)
            elif il < 5:
                ev = self.rem.get_event(0.01)
                if ev != None:
                    il += 1
                    run_evd = False
                    #print("EV:",ev)
                    if isinstance(ev , rem488.RemotizerConnection):
                        self.status_connect.emit(ev.status , ev.msg)
                    elif isinstance(ev , rem488.RemotizerCP):
                        self.rem.send_checkpoint_reached()
                    else:
                        self.digitizer.ev_dispatch(ev)
            if run_evd:
                il = 0
                evd.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents)

    def pen_position(self , x , y , press):
        self.digitizer.pen_position(x , y , press)
    def pen_proximity(self , prox):
        self.digitizer.pen_proximity(prox)
    def note_ended(self):
        self.digitizer.note_ended()
