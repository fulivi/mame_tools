#!/usr/bin/env python3
# A HP9872 emulator for use with MAME IEEE-488 remotizer
# Copyright (C) 2022-2023 F. Ulivi <fulivi at big "G" mail>
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

# This version of emulator is based on the reverse-engineering of 9872 firmware.
# See https://github.com/fulivi/hp9872_re

import rem488
import itertools
import re
import io
import math
from copy import deepcopy

# LB terminator
ETX="\x03"

# Physical limits
MIN_X_PHY = 0
MAX_X_PHY = 16000
MIN_Y_PHY = 0
MAX_Y_PHY = 11400

# Default P1/P2
DEF_X_P1 = 520
DEF_Y_P1 = 380
DEF_X_P2 = 15720
DEF_Y_P2 = 10380

# Reset position
RST_X = 16000
RST_Y = 0

# "Impossible" pen position
NO_X_PEN = 65535
NO_Y_PEN = 65535

# Limit of integer arguments
MIN_INT_NO_SC = -32767
MAX_INT_NO_SC = 32767
MIN_INT_SC = -16383
MAX_INT_SC = 16383
ABS_MAX_INT = 32767

# Limit of decimal arguments
MIN_DEC = -127.999
MAX_DEC = 127.999
MAX_ABS_DEC = 127

# Rounding of decimal arguments
DEC_ROUND = 1.0 / 256.0

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
        s = io.StringIO()
        print("cmd={}".format(self.cmd) , file = s , end = "")
        if self.args:
            print(", args" , file = s , end = "")
            sep = ":"
            for a in self.args:
                print("{}{}".format(sep , str(a)) , file = s , end = "")
                sep = ","
        return s.getvalue()

class ParsedIntArg:
    def __init__(self , value):
        self.value = value

    def __str__(self):
        return "int={}".format(self.value)

class ParsedFixArg:
    def __init__(self , value):
        self.value = value

    def __str__(self):
        return "fix={}".format(self.value)

class ParsedString:
    def __init__(self , value):
        self.value = value

    def __str__(self):
        return "string=" + self.value

class BadArg:
    def __init__(self , s):
        self.failed_parse = s

    def __str__(self):
        return "Bad argument:" + self.failed_parse

class Parser:
    def __init__(self):
        self.int_arg_re = re.compile(r"[+-]?\d+$")
        self.fix_arg_re = re.compile(r"[+-]?\d*\.\d+$")
        # 0 Start
        # 1 1st letter of cmd parsed
        # 2 2nd letter of cmd parsed, accumulating parameters (if any)
        # 3 Accumulating string for LB cmd
        # 4 Waiting for [\n;] in SM cmd
        # 5 Waiting for \n in SM cmd
        self.state = 0

    def parse(self , s):
        for c in s:
            if self.state < 3 and (c == ' ' or c == '\r'):
                continue
            elif self.state == 0:
                if c == '\n' or c == ';':
                    # NOP
                    continue
                else:
                    self.cmd = c.upper()
                    self.state = 1
            elif self.state == 1:
                if c == '\n' or c == ';':
                    yield ParsedCmd(self.cmd, [])
                    self.state = 0
                else:
                    self.cmd += c.upper()
                    self.param = ""
                    if self.cmd == "LB":
                        self.state = 3
                    else:
                        self.state = 2
            elif self.state == 2:
                if c == '\n' or c == ';':
                    # Split arguments
                    pieces = self.param.split(",")
                    pieces = [ p.strip() for p in pieces ]
                    args = []
                    if len(pieces) > 1 or pieces[ 0 ]:
                        for p in pieces:
                            p_strip = p.strip()
                            if p_strip:
                                mo = self.fix_arg_re.match(p_strip)
                                if mo:
                                    x = float(mo.group())
                                    x = int(x / DEC_ROUND) * DEC_ROUND
                                    args.append(ParsedFixArg(x))
                                else:
                                    mo = self.int_arg_re.match(p_strip)
                                    if mo:
                                        x = int(mo.group())
                                        args.append(ParsedIntArg(x))
                                    else:
                                        args.append(ParseError(p_strip))
                            else:
                                # Empty argument
                                args.append(BadArg(""))
                    yield ParsedCmd(self.cmd , args)
                    self.state = 0
                elif self.cmd == "SM":
                    self.param = c
                    self.state = 4
                else:
                    self.param += c
            elif self.state == 3:
                if c == ETX:
                    yield ParsedCmd("LB" , [ ParsedString(self.param) ])
                    self.state = 0
                else:
                    self.param += c
            elif self.state == 4:
                if c == '\n' or c == ';':
                    yield ParsedCmd(self.cmd , [ ParsedString(self.param) ])
                    self.state = 0
                elif c == '\r':
                    self.state = 5
                else:
                    yield ParsedCmd(self.cmd , [ InvalidArg() ])
                    self.state = 0
            else:
                if c == '\n':
                    yield ParsedCmd(self.cmd , [ ParsedString(self.param) ])
                else:
                    yield ParsedCmd(self.cmd , [ InvalidArg() ])
                self.state = 0

class WrongNumArgs(Exception):
    pass

class InvalidArg(Exception):
    pass

class InvalidChar(Exception):
    pass

class UnknownCharSet(Exception):
    pass

class PosOverflow(Exception):
    pass

class Point:
    def __init__(self , x , y):
        self.x = x
        self.y = y

    def __eq__(self , other):
        return self.x == other.x and self.y == other.y

    def __add__(self , other):
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self , other):
        return Point(self.x - other.x, self.y - other.y)

    # Distance between self and other
    def dist(self , other):
        return math.dist([ self.x , self.y ] , [ other.x , other.y ])

    def __str__(self):
        return "({},{})".format(self.x , self.y)

class Segment:
    def __init__(self , p1 , p2):
        self.p1 = deepcopy(p1)
        self.p2 = deepcopy(p2)

    def null_len(self):
        return self.p1 == self.p2

    def length(self):
        return self.p1.dist(self.p2)

    def __str__(self):
        return "{}-{}".format(str(self.p1) , str(self.p2))

class Rectangle:
    def __init__(self , pll , pur):
        # Point at lower left corner
        self.pll = pll
        # Point at upper right corner
        self.pur = pur

    def contains(self , pt):
        return self.pll.x <= pt.x <= self.pur.x and self.pll.y <= pt.y <= self.pur.y

    def __contains__(self , pt):
        return self.pll.x <= pt.x <= self.pur.x and self.pll.y <= pt.y <= self.pur.y

    def __str__(self):
        return "{}-{}".format(str(self.pll) , str(self.pur))

    # Liang-Barsky algorithm
    # Return segment s clipped by the rectangle. If s is entirely outside rectangle, return None
    def clip_segment(self , s):
        p1 = -(s.p2.x - s.p1.x)
        p2 = -p1
        p3 = -(s.p2.y - s.p1.y)
        p4 = -p3
        q1 = s.p1.x - self.pll.x
        q2 = self.pur.x - s.p1.x
        q3 = s.p1.y - self.pll.y
        q4 = self.pur.y - s.p1.y

        if (p1 == 0 and q1 < 0) or (p2 == 0 and q2 < 0) or (p3 == 0 and q3 < 0) or (p4 == 0 and q4 < 0):
            # Parallel to sides of rectangle and outside of it
            return None

        posarr = [ 1 ]
        negarr = [ 0 ]
        if p1 != 0:
            r1 = q1 / p1
            r2 = q2 / p2
            if p1 < 0:
                negarr.append(r1)
                posarr.append(r2)
            else:
                negarr.append(r2)
                posarr.append(r1)
        if p3 != 0:
            r3 = q3 / p3
            r4 = q4 / p4
            if p3 < 0:
                negarr.append(r3)
                posarr.append(r4)
            else:
                negarr.append(r4)
                posarr.append(r3)
        rn1 = max(negarr)
        rn2 = min(posarr)

        if rn1 > rn2:
            # Entirely outside of rectangle
            return None

        xn1 = int(s.p1.x + p2 * rn1)
        yn1 = int(s.p1.y + p4 * rn1)
        xn2 = int(s.p1.x + p2 * rn2)
        yn2 = int(s.p1.y + p4 * rn2)

        return Segment(Point(xn1 , yn1) , Point(xn2 , yn2))

def group_pairs(iterable):
    args = [ iter(iterable) ] * 2
    return itertools.zip_longest(*args)

class Font:
    # Each codepoint is a 3-element tuple
    # [0]       Offset to center character for SM cmd
    # [1]       True if character has auto-backspace
    # [2]       List of pen movements. Each element is a 3-element tuple:
    #   [0]     Pen up (False) or pen down (True)
    #   [1]     X coord delta (1 character width = 48)
    #   [2]     Y coord delta (1 character height = 32)
    FONT = [
        # 00
        ((-1, -16), False, [(False, 0, 0), (True, 2, 0), (True, 0, 2), (True, -2, 0), (True, 0, -2), (False, 1, 11), (True, 0, 21)]),
        # 01
        ((-16, -31), False, [(False, 10, 26), (True, 0, 10), (False, 12, 0), (True, 0, -10)]),
        # 02
        ((-16, -16), False, [(False, 4, 0), (True, 12, 32), (False, 12, 0), (True, -12, -32), (False, 16, 12), (True, -32, 0), (False, 0, 8), (True, 32, 0)]),
        # 03
        ((-16, -16), False, [(False, 0, 6), (True, 2, -3), (True, 5, -2), (True, 18, 0), (True, 5, 2), (True, 2, 3), (True, 0, 5), (True, -2, 3), (True, -5, 2), (True, -18, 1), (True, -5, 2), (True, -2, 3), (True, 0, 4), (True, 2, 3), (True, 5, 2), (True, 17, 0), (True, 5, -2), (True, 2, -3), (False, -15, 10), (True, 0, -40)]),
        # 04
        ((-16, -16), False, [(False, 0, 0), (True, 32, 32), (False, -22, 0), (True, -6, 0), (True, -3, -1), (True, -1, -2), (True, 0, -5), (True, 1, -2), (True, 3, -1), (True, 6, 0), (True, 3, 1), (True, 1, 2), (True, 0, 5), (True, -1, 2), (True, -3, 1), (False, 12, -21), (True, -3, -1), (True, -1, -2), (True, 0, -5), (True, 1, -2), (True, 3, -1), (True, 6, 0), (True, 3, 1), (True, 1, 2), (True, 0, 5), (True, -1, 2), (True, -3, 1), (True, -6, 0)]),
        # 05
        ((-16, -16), False, [(False, 8, 19), (True, -6, -3), (True, -2, -4), (True, 0, -5), (True, 2, -4), (True, 4, -2), (True, 5, -1), (True, 10, 0), (True, 5, 1), (True, 4, 2), (True, 2, 4), (True, 0, 6), (False, -3, 12), (True, 0, 3), (True, -2, 3), (True, -5, 1), (True, -11, 0), (True, -5, -1), (True, -2, -3), (True, 0, -4), (True, 2, -3), (True, 26, -21)]),
        # 06
        ((-16, -30), False, [(False, 15, 27), (True, 2, 1), (True, 1, 1), (True, 0, 5), (True, -3, 0), (True, 0, -2), (True, 3, 0)]),
        # 07
        ((-28, -16), False, [(False, 32, -4), (True, -4, 4), (True, -3, 5), (True, -1, 5), (True, 0, 12), (True, 1, 5), (True, 3, 5), (True, 4, 4)]),
        # 08
        ((-4, -16), False, [(False, 0, -4), (True, 4, 4), (True, 3, 5), (True, 1, 5), (True, 0, 12), (True, -1, 5), (True, -3, 5), (True, -4, 4)]),
        # 09
        ((-16, -16), False, [(False, 4, 4), (True, 24, 24), (False, 4, -12), (True, -32, 0), (False, 4, 12), (True, 24, -24)]),
        # 0a
        ((-16, -16), False, [(False, 16, 4), (True, 0, 24), (False, -16, -12), (True, 32, 0)]),
        # 0b
        ((-1, 1), False, [(False, 3, 0), (True, -3, 0), (True, 0, 2), (True, 3, 0), (True, 0, -5), (True, -1, -1), (True, -2, -1)]),
        # 0c
        ((-16, -16), False, [(False, 0, 16), (True, 32, 0)]),
        # 0d
        ((-16, -1), False, [(False, 15, 0), (True, 0, 2), (True, 3, 0), (True, 0, -2), (True, -3, 0)]),
        # 0e
        ((-16, -16), False, [(False, 0, -4), (True, 32, 40)]),
        # 0f
        ((-16, -16), False, [(False, 14, 0), (True, -7, 2), (True, -3, 3), (True, -3, 6), (True, 0, 10), (True, 3, 6), (True, 3, 3), (True, 6, 2), (True, 6, 0), (True, 6, -2), (True, 3, -3), (True, 3, -6), (True, 0, -10), (True, -3, -6), (True, -3, -3), (True, -6, -2), (True, -5, 0)]),
        # 10
        ((-18, -16), False, [(False, 8, 20), (True, 12, 12), (True, 0, -32), (False, -12, 0), (True, 20, 0)]),
        # 11
        ((-16, -16), False, [(False, 1, 26), (True, 2, 4), (True, 6, 2), (True, 15, 0), (True, 6, -2), (True, 2, -4), (True, 0, -5), (True, -2, -4), (True, -6, -2), (True, -16, -2), (True, -5, -3), (True, -3, -10), (True, 32, 0)]),
        # 12
        ((-16, -16), False, [(False, 1, 27), (True, 2, 3), (True, 5, 2), (True, 16, 0), (True, 5, -2), (True, 2, -4), (True, 0, -3), (True, -2, -4), (True, -4, -2), (True, -15, 0), (False, 15, 0), (True, 5, -2), (True, 2, -4), (True, 0, -5), (True, -2, -4), (True, -5, -2), (True, -18, 0), (True, -5, 2), (True, -2, 4)]),
        # 13
        ((-16, -16), False, [(False, 32, 8), (True, -32, 0), (True, 28, 24), (True, 0, -32)]),
        # 14
        ((-16, -16), False, [(False, 0, 5), (True, 2, -3), (True, 6, -2), (True, 16, 0), (True, 6, 2), (True, 2, 4), (True, 0, 9), (True, -2, 4), (True, -6, 2), (True, -15, 0), (True, -6, -2), (True, -3, -3), (True, 0, 16), (True, 32, 0)]),
        # 15
        ((-16, -16), False, [(False, 0, 12), (True, 2, 4), (True, 5, 2), (True, 18, 0), (True, 5, -2), (True, 2, -4), (True, 0, -6), (True, -2, -4), (True, -5, -2), (True, -18, 0), (True, -5, 2), (True, -2, 4), (True, 0, 20), (True, 2, 4), (True, 5, 2), (True, 18, 0), (True, 5, -2), (True, 2, -4)]),
        # 16
        ((-16, -16), False, [(False, 0, 32), (True, 32, 0), (True, -24, -32)]),
        # 17
        ((-16, -16), False, [(False, 7, 17), (True, -4, 2), (True, -2, 4), (True, 0, 4), (True, 2, 3), (True, 5, 2), (True, 16, 0), (True, 5, -2), (True, 2, -4), (True, 0, -3), (True, -2, -4), (True, -4, -2), (True, -18, 0), (True, -5, -2), (True, -2, -4), (True, 0, -5), (True, 2, -4), (True, 5, -2), (True, 18, 0), (True, 5, 2), (True, 2, 4), (True, 0, 5), (True, -2, 4), (True, -5, 2)]),
        # 18
        ((-16, -16), False, [(False, 0, 6), (True, 2, -4), (True, 5, -2), (True, 18, 0), (True, 5, 2), (True, 2, 4), (True, 0, 20), (True, -2, 4), (True, -5, 2), (True, -18, 0), (True, -5, -2), (True, -2, -4), (True, 0, -6), (True, 2, -4), (True, 5, -2), (True, 18, 0), (True, 5, 2), (True, 2, 4)]),
        # 19
        ((-1, -12), False, [(False, 0, 0), (True, 0, 2), (True, 3, 0), (True, 0, -2), (True, -3, 0), (False, 0, 22), (True, 0, 2), (True, 3, 0), (True, 0, -2), (True, -3, 0)]),
        # 1a
        ((-1, -9), False, [(False, 3, 0), (True, -3, 0), (True, 0, 2), (True, 3, 0), (True, 0, -5), (True, -1, -1), (True, -2, -1), (False, 0, 27), (True, 0, 2), (True, 3, 0), (True, 0, -2), (True, -3, 0)]),
        # 1b
        ((-16, -16), False, [(False, 32, 4), (True, -32, 12), (True, 32, 12)]),
        # 1c
        ((-16, -16), False, [(False, 0, 12), (True, 32, 0), (False, -32, 8), (True, 32, 0)]),
        # 1d
        ((-16, -16), False, [(False, 0, 4), (True, 32, 12), (True, -32, 12)]),
        # 1e
        ((-16, -16), False, [(False, 0, 27), (True, 2, 3), (True, 5, 2), (True, 18, 0), (True, 5, -2), (True, 2, -3), (True, 0, -6), (True, -2, -3), (True, -5, -2), (True, -8, -1), (True, -2, -2), (True, -1, -3), (False, -1, -8), (True, 3, 0), (True, 0, -2), (True, -3, 0), (True, 0, 2)]),
        # 1f
        ((-16, -16), False, [(False, 28, 0), (True, -18, 0), (True, -6, 1), (True, -3, 3), (True, -1, 4), (True, 0, 16), (True, 1, 4), (True, 3, 3), (True, 6, 1), (True, 12, 0), (True, 6, -1), (True, 3, -3), (True, 1, -4), (True, 0, -11), (True, -3, -3), (True, -4, -1), (True, -9, 0), (True, -4, 1), (True, -3, 3), (True, 0, 6), (True, 3, 3), (True, 4, 1), (True, 9, 0), (True, 4, -1), (True, 3, -3)]),
        # 20
        ((-16, -16), False, [(False, 0, 0), (True, 16, 32), (True, 16, -32), (False, -28, 8), (True, 24, 0)]),
        # 21
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (True, 24, 0), (True, 5, -2), (True, 2, -3), (True, 0, -5), (True, -2, -3), (True, -5, -2), (True, 6, -2), (True, 2, -4), (True, 0, -5), (True, -2, -4), (True, -6, -2), (True, -24, 0), (False, 0, 17), (True, 24, 0)]),
        # 22
        ((-16, -16), False, [(False, 31, 7), (True, -1, -3), (True, -3, -3), (True, -7, -1), (True, -8, 0), (True, -7, 1), (True, -3, 3), (True, -2, 6), (True, 0, 12), (True, 2, 6), (True, 3, 3), (True, 7, 1), (True, 8, 0), (True, 7, -1), (True, 3, -3), (True, 1, -3)]),
        # 23
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (True, 20, 0), (True, 7, -1), (True, 3, -3), (True, 2, -6), (True, 0, -12), (True, -2, -6), (True, -3, -3), (True, -7, -1), (True, -20, 0)]),
        # 24
        ((-16, -16), False, [(False, 32, 0), (True, -32, 0), (True, 0, 32), (True, 32, 0), (False, -32, -15), (True, 26, 0)]),
        # 25
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (True, 32, 0), (False, -32, -15), (True, 24, 0)]),
        # 26
        ((-16, -16), False, [(False, 30, 28), (True, -3, 3), (True, -7, 1), (True, -8, 0), (True, -7, -1), (True, -3, -3), (True, -2, -6), (True, 0, -12), (True, 2, -6), (True, 3, -3), (True, 7, -1), (True, 8, 0), (True, 7, 1), (True, 3, 3), (True, 2, 6), (True, 0, 5), (True, -16, 0)]),
        # 27
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (False, 0, -15), (True, 32, 0), (False, 0, 15), (True, 0, -32)]),
        # 28
        ((-16, -16), False, [(False, 4, 32), (True, 24, 0), (False, -12, 0), (True, 0, -32), (False, -12, 0), (True, 24, 0)]),
        # 29
        ((-16, -16), False, [(False, 12, 32), (True, 20, 0), (False, -8, 0), (True, 0, -22), (True, -1, -6), (True, -3, -3), (True, -5, -1), (True, -6, 0), (True, -5, 1), (True, -3, 3), (True, -1, 6)]),
        # 2a
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (False, 0, -15), (True, 10, 0), (False, 20, 15), (True, -20, -15), (True, 22, -17)]),
        # 2b
        ((-16, -16), False, [(False, 0, 32), (True, 0, -32), (True, 32, 0)]),
        # 2c
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (True, 16, -24), (True, 16, 24), (True, 0, -32)]),
        # 2d
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (True, 32, -32), (True, 0, 32)]),
        # 2e
        ((-16, -16), False, [(False, 12, 0), (True, -7, 1), (True, -3, 3), (True, -2, 6), (True, 0, 12), (True, 2, 6), (True, 3, 3), (True, 7, 1), (True, 8, 0), (True, 7, -1), (True, 3, -3), (True, 2, -6), (True, 0, -12), (True, -2, -6), (True, -3, -3), (True, -7, -1), (True, -8, 0)]),
        # 2f
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (True, 25, 0), (True, 5, -2), (True, 2, -3), (True, 0, -8), (True, -2, -3), (True, -5, -2), (True, -25, 0)]),
        # 30
        ((-16, -16), False, [(False, 12, 0), (True, -7, 1), (True, -3, 3), (True, -2, 6), (True, 0, 12), (True, 2, 6), (True, 3, 3), (True, 7, 1), (True, 8, 0), (True, 7, -1), (True, 3, -3), (True, 2, -6), (True, 0, -12), (True, -2, -6), (True, -3, -3), (True, -7, -1), (True, -8, 0), (False, 8, 13), (True, 12, -13)]),
        # 31
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (True, 25, 0), (True, 5, -2), (True, 2, -3), (True, 0, -8), (True, -2, -3), (True, -5, -2), (True, -25, 0), (False, 25, 0), (True, 5, -2), (True, 2, -3), (True, 0, -9)]),
        # 32
        ((-16, -16), False, [(False, 0, 5), (True, 2, -3), (True, 5, -2), (True, 18, 0), (True, 5, 2), (True, 2, 3), (True, 0, 6), (True, -2, 3), (True, -5, 2), (True, -18, 1), (True, -5, 2), (True, -2, 3), (True, 0, 5), (True, 2, 3), (True, 5, 2), (True, 17, 0), (True, 5, -2), (True, 2, -3)]),
        # 33
        ((-16, -16), False, [(False, 0, 32), (True, 32, 0), (False, -16, 0), (True, 0, -32)]),
        # 34
        ((-16, -16), False, [(False, 0, 32), (True, 0, -23), (True, 2, -5), (True, 3, -3), (True, 7, -1), (True, 8, 0), (True, 7, 1), (True, 3, 3), (True, 2, 5), (True, 0, 23)]),
        # 35
        ((-16, -16), False, [(False, 0, 32), (True, 16, -32), (True, 16, 32)]),
        # 36
        ((-16, -16), False, [(False, 0, 32), (True, 4, -32), (True, 12, 24), (True, 12, -24), (True, 4, 32)]),
        # 37
        ((-16, -16), False, [(False, 0, 0), (True, 31, 32), (False, -30, 0), (True, 31, -32)]),
        # 38
        ((-15, -16), False, [(False, 15, 0), (True, 0, 14), (False, -16, 18), (True, 16, -18), (True, 16, 18)]),
        # 39
        ((-16, -16), False, [(False, 1, 32), (True, 30, 0), (True, -31, -32), (True, 32, 0)]),
        # 3a
        ((-27, -16), False, [(False, 32, -4), (True, -10, 0), (True, 0, 40), (True, 10, 0)]),
        # 3b
        ((-16, -16), False, [(False, 0, 36), (True, 32, -40)]),
        # 3c
        ((-5, -16), False, [(False, 0, -4), (True, 10, 0), (True, 0, 40), (True, -10, 0)]),
        # 3d
        ((-14, -33), False, [(False, 4, 30), (True, 10, 6), (True, 10, -6)]),
        # 3e
        ((-24, 6), False, [(False, 0, -6), (True, 48, 0)]),
        # 3f
        ((-14, -34), False, [(False, 8, 38), (True, 12, -8)]),
        # 40
        ((-14, -12), False, [(False, 2, 22), (True, 2, 1), (True, 6, 1), (True, 8, 0), (True, 6, -1), (True, 3, -3), (True, 1, -4), (True, 0, -16), (False, 0, 10), (True, -3, 3), (True, -4, 1), (True, -14, 0), (True, -4, -1), (True, -3, -3), (True, 0, -6), (True, 3, -3), (True, 4, -1), (True, 14, 0), (True, 4, 1), (True, 3, 3)]),
        # 41
        ((-14, -16), False, [(False, 0, 0), (True, 0, 32), (False, 0, -14), (True, 3, 4), (True, 6, 2), (True, 10, 0), (True, 6, -2), (True, 3, -5), (True, 0, -10), (True, -3, -5), (True, -6, -2), (True, -10, 0), (True, -6, 2), (True, -3, 4)]),
        # 42
        ((-14, -12), False, [(False, 28, 18), (True, -3, 4), (True, -6, 2), (True, -10, 0), (True, -6, -2), (True, -3, -5), (True, 0, -10), (True, 3, -5), (True, 6, -2), (True, 10, 0), (True, 6, 2), (True, 3, 4)]),
        # 43
        ((-16, -16), False, [(False, 28, 6), (True, -3, -4), (True, -6, -2), (True, -10, 0), (True, -6, 2), (True, -3, 5), (True, 0, 10), (True, 3, 5), (True, 6, 2), (True, 10, 0), (True, 6, -2), (True, 3, -4), (False, 0, 14), (True, 0, -32)]),
        # 44
        ((-14, -12), False, [(False, 0, 13), (True, 28, 0), (True, 0, 4), (True, -3, 5), (True, -6, 2), (True, -10, 0), (True, -6, -2), (True, -3, -5), (True, 0, -10), (True, 3, -5), (True, 6, -2), (True, 12, 0), (True, 6, 3)]),
        # 45
        ((-12, -16), False, [(False, 12, 0), (True, 0, 28), (True, 2, 3), (True, 5, 1), (True, 5, 0), (False, -19, -12), (True, 19, 0)]),
        # 46
        ((-14, -8), False, [(False, 1, -6), (True, 6, -2), (True, 12, 0), (True, 6, 2), (True, 3, 4), (True, 0, 26), (False, 0, -6), (True, -3, 4), (True, -6, 2), (True, -10, 0), (True, -6, -2), (True, -3, -5), (True, 0, -8), (True, 3, -5), (True, 6, -2), (True, 10, 0), (True, 6, 2), (True, 3, 5)]),
        # 47
        ((-14, -16), False, [(False, 0, 0), (True, 0, 32), (False, 0, -14), (True, 3, 4), (True, 6, 2), (True, 10, 0), (True, 6, -2), (True, 3, -4), (True, 0, -18)]),
        # 48
        ((-16, -16), False, [(False, 14, 32), (True, 0, -2), (False, -8, -8), (True, 12, 0), (True, 0, -22), (False, -12, 0), (True, 20, 0)]),
        # 49
        ((-9, -12), False, [(False, 6, -8), (True, 5, 0), (True, 5, 1), (True, 2, 3), (True, 0, 26), (True, -12, 0), (False, 8, 8), (True, 0, 2)]),
        # 4a
        ((-14, -16), False, [(False, 0, 0), (True, 0, 32), (False, 26, -8), (True, -20, -11), (True, -6, 0), (False, 6, 0), (True, 22, -13)]),
        # 4b
        ((-16, -16), False, [(False, 6, 32), (True, 12, 0), (True, 0, -32), (False, -12, 0), (True, 20, 0)]),
        # 4c
        ((-16, -12), False, [(False, 0, 0), (True, 0, 24), (False, 0, -6), (True, 2, 4), (True, 4, 2), (True, 4, 0), (True, 4, -2), (True, 2, -4), (True, 0, -18), (False, 0, 18), (True, 2, 4), (True, 4, 2), (True, 4, 0), (True, 4, -2), (True, 2, -4), (True, 0, -18)]),
        # 4d
        ((-14, -12), False, [(False, 0, 0), (True, 0, 24), (False, 0, -6), (True, 3, 4), (True, 6, 2), (True, 10, 0), (True, 6, -2), (True, 3, -4), (True, 0, -18)]),
        # 4e
        ((-14, -12), False, [(False, 9, 0), (True, -6, 2), (True, -3, 5), (True, 0, 10), (True, 3, 5), (True, 6, 2), (True, 10, 0), (True, 6, -2), (True, 3, -5), (True, 0, -10), (True, -3, -5), (True, -6, -2), (True, -10, 0)]),
        # 4f
        ((-14, -8), False, [(False, 0, -8), (True, 0, 32), (False, 0, -6), (True, 3, 4), (True, 6, 2), (True, 10, 0), (True, 6, -2), (True, 3, -5), (True, 0, -10), (True, -3, -5), (True, -6, -2), (True, -10, 0), (True, -6, 2), (True, -3, 4)]),
        # 50
        ((-14, -8), False, [(False, 28, 18), (True, -3, 4), (True, -6, 2), (True, -10, 0), (True, -6, -2), (True, -3, -5), (True, 0, -10), (True, 3, -5), (True, 6, -2), (True, 10, 0), (True, 6, 2), (True, 3, 4), (False, 0, 18), (True, 0, -32)]),
        # 51
        ((-14, -12), False, [(False, 0, 0), (True, 0, 24), (False, 0, -8), (True, 4, 6), (True, 6, 2), (True, 9, 0), (True, 6, -2), (True, 3, -5)]),
        # 52
        ((-14, -12), False, [(False, 0, 4), (True, 3, -3), (True, 6, -1), (True, 10, 0), (True, 6, 1), (True, 3, 3), (True, 0, 5), (True, -3, 3), (True, -6, 1), (True, -10, 0), (True, -6, 1), (True, -3, 2), (True, 0, 5), (True, 3, 2), (True, 6, 1), (True, 10, 0), (True, 6, -1), (True, 3, -2)]),
        # 53
        ((-14, -15), False, [(False, 0, 22), (True, 24, 0), (False, -16, 10), (True, 0, -28), (True, 3, -3), (True, 4, -1), (True, 9, 0), (True, 4, 2)]),
        # 54
        ((-14, -12), False, [(False, 0, 24), (True, 0, -18), (True, 3, -4), (True, 6, -2), (True, 10, 0), (True, 6, 2), (True, 3, 4), (False, 0, 18), (True, 0, -24)]),
        # 55
        ((-14, -12), False, [(False, 0, 24), (True, 14, -24), (True, 14, 24)]),
        # 56
        ((-16, -12), False, [(False, 0, 24), (True, 5, -24), (True, 11, 18), (True, 11, -18), (True, 5, 24)]),
        # 57
        ((-14, -12), False, [(False, 0, 0), (True, 27, 24), (False, -26, 0), (True, 27, -24)]),
        # 58
        ((-14, -8), False, [(False, 5, -8), (True, 4, 0), (True, 4, 3), (True, 3, 5), (True, 12, 24), (False, -28, 0), (True, 16, -24)]),
        # 59
        ((-14, -12), False, [(False, 1, 24), (True, 26, 0), (True, -27, -24), (True, 28, 0)]),
        # 5a
        ((-26, -16), False, [(False, 32, 36), (True, -3, 0), (True, -2, -1), (True, -1, -2), (True, 0, -12), (True, -1, -2), (True, -2, -2), (True, -3, -1), (True, 3, -1), (True, 2, -2), (True, 1, -2), (True, 0, -12), (True, 1, -2), (True, 2, -1), (True, 3, 0)]),
        # 5b
        ((0, -16), False, [(False, 0, -4), (True, 0, 40)]),
        # 5c
        ((-6, -16), False, [(False, 0, 36), (True, 3, 0), (True, 2, -1), (True, 1, -2), (True, 0, -12), (True, 1, -2), (True, 2, -2), (True, 3, -1), (True, -3, -1), (True, -2, -2), (True, -1, -2), (True, 0, -12), (True, -1, -2), (True, -2, -1), (True, -3, 0)]),
        # 5d
        ((-14, -32), False, [(False, 0, 30), (True, 4, 4), (True, 3, 1), (True, 3, 0), (True, 2, -1), (True, 4, -3), (True, 2, -1), (True, 3, 0), (True, 3, 1), (True, 4, 4)]),
        # 5e
        None,
        # 5f
        ((-16, -34), False, [(False, 16, 38), (True, 0, -8)]),
        # 60
        ((-16, -16), False, [(False, 0, 16), (True, 8, 0), (True, 8, -16), (True, 8, 32), (True, 8, 0)]),
        # 61
        ((-16, -16), False, [(False, 16, 0), (True, 0, 32), (False, -16, -12), (True, 16, 12), (True, 16, -12)]),
        # 62
        ((24, 6), True, [(False, 0, -6), (True, -48, 0)]),
        # 63
        ((32, -34), True, [(False, -26, 30), (True, -12, 8)]),
        # 64
        ((-16, -13), False, [(False, 0, 18), (True, 5, 5), (True, 2, 1), (True, 3, 0), (True, 12, -4), (True, 3, 0), (True, 2, 1), (True, 5, 5), (False, -24, -2), (True, 0, -24), (False, 16, 20), (True, 0, -20)]),
        # 65
        ((-16, -16), False, [(False, 0, 0), (True, 0, 32), (False, 0, -16), (True, 32, 0)]),
        # 66
        ((-16, -16), False, [(False, 0, 16), (True, 32, 0), (False, -9, 3), (True, 9, -3), (True, -9, -3)]),
        # 67
        ((34, -32), True, [(False, -20, 35), (True, -4, -4), (True, -3, -1), (True, -3, 0), (True, -2, 1), (True, -4, 3), (True, -2, 1), (True, -3, 0), (True, -3, -1), (True, -4, -4)]),
        # 68
        ((-16, -16), False, [(False, 8, 20), (True, 16, 0), (False, 8, 8), (True, -3, 3), (True, -3, 1), (True, -4, 0), (True, -3, -1), (True, -2, -2), (True, -1, -4), (True, 0, -17), (True, -1, -4), (True, -3, -3), (True, -3, -1), (True, -4, 0), (True, -3, 1), (True, -2, 2), (True, 0, 2), (True, 2, 2), (True, 3, 1), (True, 4, 0), (True, 3, -1), (True, 8, -6), (True, 3, -1), (True, 4, 0), (True, 4, 2), (True, 1, 2)]),
        # 69
        ((34, -34), True, [(False, -28, 38), (True, -12, -8)]),
        # 6a
        ((-14, -8), False, [(False, 13, -8), (True, 3, 3), (True, -4, 5), (False, 16, 6), (True, -3, -4), (True, -6, -2), (True, -10, 0), (True, -6, 2), (True, -3, 5), (True, 0, 10), (True, 3, 5), (True, 6, 2), (True, 10, 0), (True, 6, -2), (True, 3, -4)]),
        # 6b
        ((34, -33), True, [(False, -24, 30), (True, -10, 6), (True, -10, -6)]),
        # 6c
        ((32, -39), True, [(False, -24, 38), (True, 0, 2), (True, -3, 0), (True, 0, -2), (True, 3, 0), (False, -13, 2), (True, -3, 0), (True, 0, -2), (True, 3, 0), (True, 0, 2)]),
        # 6d
        ((32, -41), True, [(False, -27, 40), (True, 0, 3), (True, -3, 2), (True, -4, 0), (True, -3, -2), (True, 0, -3), (True, 3, -2), (True, 4, 0), (True, 3, 2)]),
        # 6e
        ((34, -31), True, [(False, -26, 30), (True, 0, 2), (True, -3, 0), (True, 0, -2), (True, 3, 0), (False, -13, 2), (True, -3, 0), (True, 0, -2), (True, 3, 0), (True, 0, 2)]),
        # 6f
        ((-16, -16), False, [(False, 0, 0), (True, 32, 32), (False, -5, -1), (True, -7, 1), (True, -8, 0), (True, -7, -1), (True, -3, -3), (True, -2, -6), (True, 0, -12), (True, 2, -6), (True, 3, -3), (True, 7, -1), (True, 8, 0), (True, 7, 1), (True, 3, 3), (True, 2, 6), (True, 0, 12), (True, -2, 6), (True, -3, 3)]),
        # 70
        ((-16, -16), False, [(False, 0, 0), (True, 14, 32), (True, 18, 0), (False, -15, 0), (True, 0, -32), (True, 15, 0), (False, -25, 16), (True, 25, 0)]),
        # 71
        ((-14, -12), False, [(False, 0, 0), (True, 28, 24), (False, -9, 0), (True, -10, 0), (True, -6, -2), (True, -3, -5), (True, 0, -10), (True, 3, -5), (True, 6, -2), (True, 10, 0), (True, 6, 2), (True, 3, 5), (True, 0, 10), (True, -3, 5), (True, -6, 2)]),
        # 72
        ((-16, -12), False, [(False, 16, 4), (True, -2, -3), (True, -3, -1), (True, -6, 0), (True, -3, 1), (True, -2, 3), (True, 0, 4), (True, 3, 3), (True, 6, 2), (True, 7, 1), (True, 16, 0), (True, 0, 6), (True, -2, 3), (True, -3, 1), (True, -6, 0), (True, -3, -1), (True, -2, -3), (False, -16, 2), (True, 4, 2), (True, 7, 0), (True, 3, -1), (True, 2, -3), (True, 0, -16), (True, 2, -3), (True, 3, -1), (True, 7, 0), (True, 4, 2)]),
        # 73
        ((34, -33), True, [(False, -29, 32), (True, 0, 3), (True, -3, 2), (True, -4, 0), (True, -3, -2), (True, 0, -3), (True, 3, -2), (True, 4, 0), (True, 3, 2)]),
        # 74
        ((-16, -8), False, [(False, 16, 24), (True, 0, -2), (True, 3, 0), (True, 0, 2), (True, -3, 0), (False, 2, -10), (True, -1, -3), (True, -2, -2), (True, -8, -1), (True, -5, -2), (True, -2, -3), (True, 0, -6), (True, 2, -3), (True, 5, -2), (True, 18, 0), (True, 5, 2), (True, 2, 3)]),
        # 75
        ((-31, -8), False, [(False, 31, -8), (True, 0, 21), (False, -1, 9), (True, 0, 2), (True, 2, 0), (True, 0, -2), (True, -2, 0)]),
        # 76
        ((56, -42), True, [(False, -16, 46), (True, -17, -7), (True, -4, -1), (True, -6, 0), (True, -4, 1), (True, -18, 6), (True, -4, 1), (True, -6, 0), (True, -4, -1), (True, -17, -7)]),
        # 77
        ((32, -41), True, [(False, -16, 44), (True, -5, -5), (True, -2, -1), (True, -3, 0), (True, -3, 1), (True, -6, 4), (True, -3, 1), (True, -3, 0), (True, -2, -1), (True, -5, -5)]),
        # 78
        ((56, -34), True, [(False, -16, 38), (True, -17, -7), (True, -4, -1), (True, -6, 0), (True, -4, 1), (True, -18, 6), (True, -4, 1), (True, -6, 0), (True, -4, -1), (True, -17, -7)]),
    ]

    XLATE = [
        # Charset 1
        {
            0x27: 0x5f,
            0x5c: 0x60,
            0x5e: 0x61,
            0x5f: 0x62,
            0x60: 0x63,
            0x7b: 0x64,
            0x7c: 0x65,
            0x7d: 0x66,
            0x7e: 0x67
        },
        # Charset 2
        {
            0x23: 0x68,
            0x27: 0x69,
            0x5c: 0x6a,
            0x5e: 0x6b,
            0x5f: 0x62,
            0x60: 0x63,
            0x7b: 0x6c,
            0x7c: 0x6d,
            0x7d: 0x6e,
            0x7e: 0x5f
        },
        # Charset 3
        {
            0x23: 0x68,
            0x5b: 0x6f,
            0x5c: 0x70,
            0x5d: 0x71,
            0x5e: 0x72,
            0x5f: 0x62,
            0x7b: 0x6c,
            0x7c: 0x6d,
            0x7d: 0x6e,
            0x7e: 0x73
        },
        # Charset 4
        {
            0x23: 0x74,
            0x27: 0x69,
            0x5c: 0x75,
            0x5e: 0x6b,
            0x5f: 0x62,
            0x7b: 0x76,
            0x7c: 0x77,
            0x7d: 0x78,
            0x7e: 0x67
        }
    ]


    def __init__(self):
        pass

    def translate_code(self, charset, char):
        code = None
        if 1 <= charset <= 4:
            d = self.XLATE[ charset - 1 ]
            code = d.get(char)
        if code is None and 0x21 <= char <= 0x7e:
            code = char - 0x21
        return code

    def get_glyph(self , code):
        return self.FONT[ code ]

class Plotter:
    # Line type patterns
    LT_PATTERNS = {
        # Line type 1
        1: [ 0 , 100 ],
        # Line type 2
        2: [ 50 , 50 ],
        # Line type 3
        3: [ 70 , 30 ],
        # Line type 4
        4: [ 80 , 10 ,  0 , 10 ],
        # Line type 5
        5: [ 70 , 10 , 10 , 10 ],
        # Line type 6
        6: [ 50 , 10,  10 , 10 , 10 , 10 ]
    }
    # Solid line type
    LT_SOLID = -1
    # Two-point line type
    LT_2_POINTS = 0

    def __init__(self , io):
        self.io = io
        self.parser = Parser()
        self.output = None
        self.font = Font()
        self.pen_no = 1
        self.initialize()

    def clear_status(self):
        self.pp_accum = 0
        self.srq_accum = 0
        self.io.set_rsv_state(False)
        self.io.set_pp_state(False)
        self.status = 0
        self.set_in_masks = [223, 0, 0]
        # Set "ready for data"
        self.set_status_1(0x10)

    def set_defaults(self):
        # Clear scaling mode
        # IM;
        # SR;
        # DC;
        # DR;
        # IW;
        # LT;
        # CA;
        # CS;
        # SS;
        # SL;
        # SM;
        # TL;
        # VS;
        # VN;
        # AP;
        # Pattern length = 4.0
        self.scaling = None
        self.set_in_masks = [223, 0, 0]
        self.text_size = Point(0.0075, 0.015)
        self.text_size_rel = True
        self.text_dir = Point(1.0, 0.0)
        self.text_dir_rel = True
        self.text_drawing = False
        self.window = Rectangle(Point(MIN_X_PHY , MIN_Y_PHY) , Point(MAX_X_PHY , MAX_Y_PHY))
        self.line_type = self.LT_SOLID
        self.line_type_pct = 4
        self.text_sets = [ 0, 0 ]
        self.text_cur_set = 0
        self.text_slant = 0.0
        self.text_symbol = None
        self.neg_tick = 0.005
        self.pos_tick = 0.005
        self.compute_text_dir()
        # Variables defined to avoid NameError's, value is not important
        self.text_char_width = 1
        self.text_char_height = 1
        self.char_offset = Point(0, 0)
        self.update_text_size()

    def initialize(self):
        self.clear_status()
        self.set_error(0)
        self.P1 = Point(DEF_X_P1 , DEF_Y_P1)
        self.P2 = Point(DEF_X_P2 , DEF_Y_P2)
        self.set_defaults()
        self.set_status_1(8)
        # Stored pen positions:
        # pen           Pen position in PU, within mech. limits
        # last_pen      Commanded pen position in PU. Clipped with current window.
        # pen_zone      Status of last_pen
        # scaled_pen    Commanded pen position in UU. Can be in nearby zone.
        # fe06/fe07
        self.pen = Point(RST_X, RST_Y)
        # fe3f/fe40
        self.last_pen = Point(RST_X, RST_Y)
        # fef2/fef3
        self.scaled_pen = Point(RST_X, RST_Y)
        self.set_pen_zone(self.P_IN_WINDOW)
        # Position where last segment/point ended
        self.last_pen_draw = Point(NO_X_PEN , NO_Y_PEN)
        # Position where pen last went down
        self.last_pen_down = Point(NO_X_PEN , NO_Y_PEN)
        # pen_down      fe4d Actual up/dn state of pen
        # cmd_pen_down  fe30 Commanded up/dn state of pen
        self.cmd_pen_down = False
        # Ensure pen status update
        self.pen_down = True
        self.set_pen_down(False)

    def is_drawing(self):
        return self.pen_down and self.pen_no != 0

    def set_pen_down(self , state):
        if not self.pen_down and state:
            self.last_pen_down = self.last_pen
        elif self.is_drawing() and not state and self.last_pen == self.last_pen_down:
            # Draw a single point (null length segment) when these conditions are true:
            # - Pen is raised
            # - Pen did not move from where it was lowered
            # - Pen did move from where the last drawn segment ended
            self.draw_point(self.last_pen)
        self.pen_down = state

    P_IN_WINDOW = 0
    P_NEARBY = 1
    P_FARAWAY = 2

    def set_pen_zone(self , zone):
        self.pen_zone = zone
        if zone == self.P_FARAWAY:
            self.set_pen_down(False)
        self.io.set_ol_led(zone)

    def update_pen_zone(self):
        self.set_pen_zone(self.P_IN_WINDOW if self.last_pen in self.window else self.P_NEARBY)

    def update_pen_zone_and_up(self):
        # 02b4
        self.last_pen = deepcopy(self.pen)
        self.update_pen_zone()
        self.cmd_pen_up()

    def check_scaled_coord(self, coord):
        if not MIN_INT_SC <= coord <= MAX_INT_SC:
            raise PosOverflow()

    def inverse_scale_and_update(self):
        # 05a3
        try:
            p2p1_diff_x = self.P2.x - self.P1.x
            if p2p1_diff_x >= 0:
                x = self.scaling.pll.x + self.scale_coord(self.last_pen.x - self.P1.x, p2p1_diff_x, self.scaling.pur.x - self.scaling.pll.x)
            else:
                x = self.scaling.pur.x + self.scale_coord(self.last_pen.x - self.P2.x, -p2p1_diff_x, self.scaling.pll.x - self.scaling.pur.x)
            self.check_scaled_coord(x)
            p2p1_diff_y = self.P2.y - self.P1.y
            if p2p1_diff_y >= 0:
                y = self.scaling.pll.y + self.scale_coord(self.last_pen.y - self.P1.y, p2p1_diff_y, self.scaling.pur.y - self.scaling.pll.y)
            else:
                y = self.scaling.pur.y + self.scale_coord(self.last_pen.y - self.P2.y, -p2p1_diff_y, self.scaling.pll.y - self.scaling.pur.y)
            self.check_scaled_coord(y)
            self.scaled_pen = Point(x, y)
        except PosOverflow:
            self.set_pen_zone(self.P_FARAWAY)

    def segment_output(self , s):
        if not s.null_len() or s.p1 != self.last_pen_draw:
            s.pen_no = self.pen_no
            self.io.draw_segment(s)
            self.last_pen_draw = deepcopy(s.p2)

    def draw_to_point(self , dest, pen_down):
        # 08d7
        s = Segment(self.last_pen, dest)
        s_clip = self.window.clip_segment(s)
        if s_clip:
            if pen_down and self.pen_no != 0:
                self.segment_output(deepcopy(s_clip))
            self.pen = s_clip.p2
            self.last_pen = dest
            zone = self.P_IN_WINDOW if s_clip.p2 == dest else self.P_NEARBY
            self.set_pen_zone(zone)
            self.set_pen_down(pen_down if zone == self.P_IN_WINDOW else False)
        else:
            # Segment is entirely outside the window
            # Do not move pen
            self.last_pen = dest
            self.set_pen_zone(self.P_NEARBY)

    def draw_to_point_sym(self, dest, pen_down):
        self.draw_to_point(dest, pen_down)
        if self.text_symbol is not None:
            # Draw symbol if SM mode enabled
            glyph = self.font.get_glyph(self.text_symbol)
            # Center cell
            self.pos_in_cell = Point(glyph[ 0 ][ 0 ] * 2, glyph[ 0 ][ 1 ] *  2)
            self.text_ref_point = deepcopy(self.last_pen)
            self.char_offset = Point(0, 0)
            self.draw_char(glyph)
            self.pos_in_cell = Point(0, 0)
            # Reposition pen at starting point
            self.draw_to_point_char(False)

    def draw_pattern_line_sym(self , dest):
        if self.line_type == self.LT_SOLID or not self.cmd_pen_down:
            self.draw_to_point_sym(dest, self.cmd_pen_down)
        elif self.line_type == self.LT_2_POINTS:
            # Two dots (0)
            self.draw_to_point_sym(dest, False)
            if self.pen_zone == self.P_IN_WINDOW and self.pen_no != 0:
                self.draw_point(dest)
        else:
            # Generic pattern 1..6
            if dest == self.last_pen:
                self.draw_to_point_sym(dest, True)
            else:
                pat = self.LT_PATTERNS[ self.line_type ]
                while True:
                    if self.line_pat_rem == 0:
                        self.line_pat_idx += 1
                        if self.line_pat_idx >= len(pat):
                            self.line_pat_idx = 0
                        self.line_pat_rem = int((self.line_type_pct * self.P1.dist(self.P2) * pat[ self.line_pat_idx ]) / 10000)
                    rem = int(self.last_pen.dist(dest))
                    if rem <= self.line_pat_rem:
                        self.line_pat_rem -= rem
                        self.draw_to_point_sym(dest, (self.line_pat_idx & 1) == 0)
                        break
                    else:
                        p = self.line_pat_rem / rem
                        delta = dest - self.last_pen
                        pdest = Point(self.last_pen.x + int(p * delta.x), self.last_pen.y + int(p * delta.y))
                        self.draw_to_point(pdest, (self.line_pat_idx & 1) == 0)
                        self.line_pat_rem = 0

    def draw_point(self , p):
        self.segment_output(Segment(p , p))

    def scale_to_p1p2(self, pt):
        pt.x *= abs(self.P1.x - self.P2.x)
        pt.y *= abs(self.P1.y - self.P2.y)

    def update_text_size(self):
        tmp = deepcopy(self.text_size)
        if self.text_size_rel:
            self.scale_to_p1p2(tmp)
        prev_width = self.text_char_width
        self.text_char_width = int(tmp.x * 1.5)
        prev_height = self.text_char_height
        self.text_char_height = int(tmp.y * 2)
        self.char_offset.x = self.char_offset.x * prev_width
        if self.text_char_width != 0:
            self.char_offset.x /= self.text_char_width
        self.char_offset.y = self.char_offset.y * prev_height
        if self.text_char_height != 0:
            self.char_offset.y /= self.text_char_height

    def compute_text_dir(self):
        tmp = deepcopy(self.text_dir)
        if self.text_dir_rel:
            self.scale_to_p1p2(tmp)
        l = math.sqrt(tmp.x * tmp.x + tmp.y * tmp.y)
        if l < 1.0e-3:
            self.text_direction = Point(0, 0)
        else:
            self.text_direction = Point(tmp.x / l, tmp.y / l)

    def start_text_drawing(self):
        # 14f5
        if not self.text_drawing:
            self.text_drawing = True
            self.text_ref_point = deepcopy(self.last_pen)
            self.char_offset = Point(0, 0)

    def check_overflow(self, coord):
        if coord < -32768:
            coord = -32768
            self.set_error(6)
        elif coord > 32767:
            coord = 32767
            self.set_error(6)
        return coord

    def clamped_scaling(self, coord, scale):
        tmp = int(coord * scale)
        return self.check_overflow(tmp)

    def rotate_text_point(self, pt):
        tmpx = self.check_overflow(int(pt.x * self.text_direction.y))
        tmpy = self.check_overflow(int(pt.y * self.text_direction.x))
        return self.check_overflow(tmpx + tmpy)

    def draw_to_point_char(self, pen_down):
        # 15a1
        if self.pen_zone == self.P_FARAWAY:
            return
        tmp = Point(self.pos_in_cell.x / 96.0, self.pos_in_cell.y / 128.0)
        tmp.x += self.char_offset.x
        tmp.x = self.clamped_scaling(tmp.x, self.text_char_width)
        tmp.x += int(self.text_slant * self.clamped_scaling(tmp.y, self.text_char_height))
        tmp.x = self.check_overflow(tmp.x)
        tmp.y += self.char_offset.y
        tmp.y = self.clamped_scaling(tmp.y, self.text_char_height)
        destx = self.check_overflow(self.text_ref_point.x + self.rotate_text_point(Point(-tmp.y, tmp.x)))
        desty = self.check_overflow(self.text_ref_point.y + self.rotate_text_point(Point(tmp.x, tmp.y)))
        self.draw_to_point(Point(destx, desty), pen_down)

    def draw_char(self, glyph):
        # 152b
        for pen, dx, dy in glyph[ 2 ]:
            self.pos_in_cell.x += 2 * dx
            self.pos_in_cell.y += 2 * dy
            self.draw_to_point_char(pen)

    def set_error(self , err_no):
        if err_no == 0:
            # Clear error
            self.io.set_error_led(False)
            self.err_no = 0
            self.set_status_0(0x20)
        elif ((1 << (err_no - 1)) & self.set_in_masks[ 0 ]) != 0 and self.err_no == 0:
            self.io.set_error_led(True)
            self.err_no = err_no
            self.set_status_1(0x20)

    def set_status_1(self , mask):
        self.status |= mask
        self.io.set_status_byte(self.status & 0x3f)
        tmp = self.status & self.set_in_masks[ 2 ]
        if tmp:
            self.pp_accum |= tmp
            self.io.set_pp_state(True)
        tmp = self.status & self.set_in_masks[ 1 ]
        if tmp:
            self.srq_accum |= tmp
            self.io.set_rsv_state(True)

    def set_status_0(self , mask):
        mask = ~mask;
        self.status &= mask
        self.io.set_status_byte(self.status & 0x3f)
        self.pp_accum &= mask
        if not self.pp_accum:
            self.io.set_pp_state(False)
        self.srq_accum &= mask
        if not self.srq_accum:
            self.io.set_rsv_state(False)

    def check_args(self , args , arg_type , min_args , max_args):
        l = len(args)
        if min_args <= l and (max_args == None or l <= max_args):
            new_args = []
            for a in args:
                if type(a) == arg_type:
                    new_args.append(a)
                elif arg_type == ParsedFixArg and type(a) == ParsedIntArg and abs(a.value) <= MAX_ABS_DEC:
                    new_args.append(ParsedFixArg(float(a.value)))
                else:
                    raise InvalidArg()
            return new_args
        else:
            raise WrongNumArgs()

    def get_args(self , args , arg_type , min_args , max_args):
        return [ p.value for p in self.check_args(args , arg_type , min_args , max_args) ]

    def check_no_arg(self , args):
        if args:
            raise WrongNumArgs()

    # CA: Select alternate character set
    # 0 or 1 int parameter
    def cmd_CA(self , args):
        args = self.get_args(args , ParsedIntArg , 0 , 1)
        if args:
            if 0 <= args[ 0 ] <= 4:
                self.text_sets[ 1 ] = args[ 0 ]
            else:
                raise UnknownCharSet()
        else:
            self.text_sets[ 1 ] = 0

    def zero_char_offset_x(self):
        # 14de
        self.char_offset = Point(0, self.char_offset.y)

    def move_char_offset_x(self, delta_x):
        # 14ed
        x = self.char_offset.x + delta_x
        x = max(min(x, 32767), -32768)
        self.char_offset = Point(x, self.char_offset.y)

    def move_char_offset_y(self, delta_y):
        # 14e5
        y = self.char_offset.y + delta_y
        y = max(min(y, 32767), -32768)
        self.char_offset = Point(self.char_offset.x, y)

    def zero_pos_in_cell_and_draw(self, pen_down):
        # 1453
        self.pos_in_cell = Point(0, 0)
        self.draw_to_point_char(pen_down)

    def move_char_offset_y_and_draw(self, delta_y):
        # 1450
        self.move_char_offset_y(delta_y)
        self.zero_pos_in_cell_and_draw(False)

    def carriage_return(self):
        # 144e
        self.zero_char_offset_x()
        self.zero_pos_in_cell_and_draw(False)

    def move_to_next_char(self):
        # 1441
        self.move_char_offset_x(1)
        self.zero_pos_in_cell_and_draw(False)

    # CP: Character positioning
    # 0 to 2 dec parameters
    def cmd_CP(self , args):
        try:
            self.start_text_drawing()
            args = self.get_args(args , ParsedFixArg , 0 , 2)
            if not args:
                # CRLF
                self.zero_char_offset_x()
                self.move_char_offset_y_and_draw(-1)
            elif len(args) == 2:
                if MIN_DEC <= args[ 0 ] <= MAX_DEC and \
                   MIN_DEC <= args[ 1 ] <= MAX_DEC:
                    self.move_char_offset_x(args[ 0 ])
                    self.move_char_offset_y(args[ 1 ])
                    self.zero_pos_in_cell_and_draw(self.cmd_pen_down)
                else:
                    raise InvalidArg()
            else:
                raise WrongNumArgs()
        finally:
            if self.scaling is not None:
                self.inverse_scale_and_update()

    # CS: Select standard character set
    # 0 or 1 int parameter
    def cmd_CS(self , args):
        args = self.get_args(args , ParsedIntArg , 0 , 1)
        if args:
            if 0 <= args[ 0 ] <= 4:
                self.text_sets[ 0 ] = args[ 0 ]
            else:
                raise UnknownCharSet()
        else:
            self.text_sets [ 0 ] = 0

    # DF: Set defaults
    # No parameters
    def cmd_DF(self , args):
        self.check_no_arg(args)
        self.set_defaults()

    # DI: Set absolute direction
    # 0 or 2 dec parameters
    def cmd_DI(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_dir = Point(1.0, 0.0)
            self.text_dir_rel = False
            self.text_drawing = False
            self.compute_text_dir()
        elif len(args) == 2:
            if MIN_DEC <= args[ 0 ] <= MAX_DEC and \
               MIN_DEC <= args[ 1 ] <= MAX_DEC and (args[ 0 ] != 0 or args[ 1 ] != 0):
                self.text_dir = Point(args[ 0 ], args[ 1 ])
                self.text_dir_rel = False
                self.text_drawing = False
                self.compute_text_dir()
            else:
                raise InvalidArg()
        else:
            raise WrongNumArgs()

    # DR: Set relative direction
    # 0 or 2 dec parameters
    def cmd_DR(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_dir = Point(1.0, 0.0)
            self.text_dir_rel = True
            self.text_drawing = False
            self.compute_text_dir()
        elif len(args) == 2:
            if MIN_DEC <= args[ 0 ] <= MAX_DEC and \
               MIN_DEC <= args[ 1 ] <= MAX_DEC and (args[ 0 ] != 0 or args[ 1 ] != 0):
                self.text_dir = Point(args[ 0 ], args[ 1 ])
                self.text_dir_rel = True
                self.text_drawing = False
                self.compute_text_dir()
            else:
                raise InvalidArg()
        else:
            raise WrongNumArgs()

    # IM: Set input mask
    # 0 to 3 int parameters
    def cmd_IM(self , args):
        args = self.get_args(args , ParsedIntArg , 0 , 3)
        if not args:
            self.set_in_masks = [223, 0, 0]
        else:
            for i , m in enumerate(args):
                if not 0 <= m <= 255:
                    raise InvalidArg()
                self.set_in_masks[ i ] = m

    # IN: Initialize
    # No parameters
    def cmd_IN(self , args):
        self.check_no_arg(args)
        self.initialize()

    # IP: Input P1/P2 points
    # 0 or 4 int parameters
    def cmd_IP(self , args):
        if not args:
            self.P1 = Point(DEF_X_P1 , DEF_Y_P1)
            self.P2 = Point(DEF_X_P2 , DEF_Y_P2)
        else:
            p1x , p1y , p2x , p2y = self.get_args(args , ParsedIntArg , 4 , 4)
            if MIN_X_PHY <= p1x <= MAX_X_PHY and \
                MIN_X_PHY <= p2x <= MAX_X_PHY and \
                MIN_Y_PHY <= p1y <= MAX_Y_PHY and \
                MIN_Y_PHY <= p2y <= MAX_Y_PHY:
                self.P1 = Point(p1x, p1y)
                self.P2 = Point(p2x, p2y)
            else:
                raise InvalidArg()
        self.set_status_1(0x02)
        self.update_text_size()
        self.compute_text_dir()
        if self.scaling is not None:
            self.update_pen_zone_and_up()
            self.inverse_scale_and_update()

    # IW: Set window
    # 0 or 4 int parameters
    def cmd_IW(self , args):
        if not args:
            self.window = Rectangle(Point(MIN_X_PHY , MIN_Y_PHY) , Point(MAX_X_PHY , MAX_Y_PHY))
        else:
            xmin, ymin, xmax, ymax = self.get_args(args , ParsedIntArg , 4 , 4)
            xmin = max(xmin, 0) & 0xfffe
            ymin = max(ymin, 0) & 0xfffe
            xmax = min(xmax, MAX_X_PHY)
            ymax = min(ymax, MAX_Y_PHY)
            if xmin > xmax or ymin > ymax:
                raise InvalidArg()
            self.window = Rectangle(Point(xmin , ymin) , Point(xmax , ymax))
        self.update_pen_zone()
        if self.pen_zone == self.P_NEARBY:
            self.set_pen_down(False)

    # LB: Label
    # 1 string parameter
    def cmd_LB(self , args):
        s = args[ 0 ].value
        if len(s) > 0:
            self.start_text_drawing()
            for c in s:
                ordc = ord(c)
                if ordc >= 0x21:
                    code = self.font.translate_code(self.text_sets[ self.text_cur_set ], ordc)
                    if code is None:
                        self.set_error(4)
                    else:
                        self.pos_in_cell = Point(0, 0)
                        glyph = self.font.get_glyph(code)
                        self.draw_char(glyph)
                        if not glyph[ 1 ]:
                            # no auto-backspace
                            self.move_char_offset_x(1)
                        self.set_pen_down(False)
                elif ordc == 0x20:
                    # Space
                    self.move_to_next_char()
                elif 0x11 <= ordc <= 0x14 or ordc == 0x0c or ordc == 0x09 or ordc == 0x07:
                    # DC1..DC4 (11..14), FF (0c), HT (09), BEL (07) = NOP
                    pass
                elif ordc == 0x0f:
                    # SI (0f) = Select std charset
                    self.text_cur_set = 0
                elif ordc == 0x0e:
                    # SO (0e) = Select alt charset
                    self.text_cur_set = 1
                elif ordc == 0x0d:
                    # CR (0d) = carriage return
                    self.carriage_return()
                elif ordc == 0x0b:
                    # VT (0b) = Move 1 line up
                    self.move_char_offset_y_and_draw(1)
                elif ordc == 0x0a:
                    # LF (0a) = Move 1 line down
                    self.move_char_offset_y_and_draw(-1)
                elif ordc == 0x08:
                    # BS (08) = backspace
                    self.move_char_offset_x(-1)
                    self.zero_pos_in_cell_and_draw(False)
                else:
                    self.set_error(4)
            self.zero_pos_in_cell_and_draw(False)
            self.set_pen_down(self.cmd_pen_down)
        if self.scaling is not None:
            self.inverse_scale_and_update()

    # LT: Set line type
    # 0 to 2 dec parameters
    def cmd_LT(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if args:
            if args[ 0 ] < 0 or args[ 0 ] >= 7:
                raise InvalidArg()
            self.line_type = int(args[ 0 ])
            if len(args) == 2:
                if args[ 1 ] <= 0 or args[ 1 ] > MAX_DEC:
                    raise InvalidArg()
                self.line_type_pct = args[ 1 ]
            self.line_pat_idx = -1
            self.line_pat_rem = 0
        else:
            self.line_type = self.LT_SOLID
            self.set_pen_down(self.cmd_pen_down)

    # OA: Output actual position
    # No parameters
    def cmd_OA(self , args):
        self.check_no_arg(args)
        return f"{int(self.pen.x)},{int(self.pen.y)},{int(self.pen_down)}\r\n"

    # OC: Output commanded position
    # No parameters
    def cmd_OC(self , args):
        self.check_no_arg(args)
        if self.scaling is None:
            return f"{int(self.last_pen.x)},{int(self.last_pen.y)},{int(self.cmd_pen_down)}\r\n"
        elif self.pen_zone == self.P_FARAWAY:
            return f"{MAX_INT_NO_SC},{MAX_INT_NO_SC},{int(self.cmd_pen_down)}\r\n"
        else:
            return f"{int(self.scaled_pen.x)},{int(self.scaled_pen.y)},{int(self.cmd_pen_down)}\r\n"

    # OE: Output error
    # No parameters
    def cmd_OE(self , args):
        self.check_no_arg(args)
        save = self.err_no
        self.set_error(0)
        return f"{save}\r\n"

    # OF: Output factors
    # No parameters
    def cmd_OF(self , args):
        self.check_no_arg(args)
        return "40,40\r\n"

    # OI: Output identification
    # No parameters
    def cmd_OI(self , args):
        self.check_no_arg(args)
        return "9872C\r\n"

    # OO: Output options
    # No parameters
    def cmd_OO(self , args):
        self.check_no_arg(args)
        return "2,1,0,0,0,0,0,0\r\n"

    # OP: Output P1/P2 points
    # No parameters
    def cmd_OP(self , args):
        self.check_no_arg(args)
        p2 = deepcopy(self.P2)
        if p2.x == self.P1.x:
            p2.x += 1
        if p2.y == self.P1.y:
            p2.y += 1
        self.set_status_0(2)
        return f"{self.P1.x},{self.P1.y},{p2.x},{p2.y}\r\n"

    # OS: Output status
    # No parameters
    def cmd_OS(self , args):
        self.check_no_arg(args)
        save = self.status
        self.set_status_0(8)
        return f"{save}\r\n"

    def scale_coord(self, coord_m_min, in_range, out_range):
        if in_range == 0:
            raise PosOverflow()
        return round((out_range * coord_m_min) / in_range)

    def plot(self, args, absolute):
        points = self.get_args(args , ParsedIntArg , 0 , None)
        for px , py in group_pairs(points):
            self.text_drawing = False
            if py == None:
                # odd number of parameters
                raise WrongNumArgs()
            dest = None
            if abs(px) <= MAX_INT_NO_SC and abs(py) <= MAX_INT_NO_SC:
                if self.scaling is None:
                    if absolute:
                        dest = Point(px, py)
                    else:
                        px += self.last_pen.x
                        py += self.last_pen.y
                        if MIN_INT_NO_SC <= px <= MAX_INT_NO_SC and\
                           MIN_INT_NO_SC <= py <= MAX_INT_NO_SC:
                            dest = Point(px, py)
                else:
                    try:
                        self.check_scaled_coord(px)
                        self.check_scaled_coord(py)
                        if not absolute:
                            px += self.scaled_pen.x
                            self.check_scaled_coord(px)
                            py += self.scaled_pen.y
                            self.check_scaled_coord(py)
                        max_m_min_x = self.scaling.pur.x - self.scaling.pll.x
                        p2p1_diff_x = self.P2.x - self.P1.x
                        if p2p1_diff_x >= 0:
                            x = self.P1.x + self.scale_coord(px - self.scaling.pll.x, max_m_min_x, p2p1_diff_x)
                        else:
                            x = self.P2.x + self.scale_coord(px - self.scaling.pur.x, max_m_min_x, p2p1_diff_x)
                        self.check_scaled_coord(x)
                        max_m_min_y = self.scaling.pur.y - self.scaling.pll.y
                        p2p1_diff_y = self.P2.y - self.P1.y
                        if p2p1_diff_y >= 0:
                            y = self.P1.y + self.scale_coord(py - self.scaling.pll.y, max_m_min_y, p2p1_diff_y)
                        else:
                            y = self.P2.y + self.scale_coord(py - self.scaling.pur.y, max_m_min_y, p2p1_diff_y)
                        self.check_scaled_coord(y)
                        dest = Point(x, y)
                        self.scaled_pen = Point(px, py)
                    except PosOverflow:
                        # dest is already None
                        pass
            if dest is not None:
                if self.pen_zone != self.P_FARAWAY:
                    self.draw_pattern_line_sym(dest)
                else:
                    self.draw_to_point(dest, False)
            else:
                self.set_pen_zone(self.P_FARAWAY)

    # PA: Plot absolute
    # 0 to n int parameters
    def cmd_PA(self , args):
        self.plot(args, True)

    # PD: Pen down
    # No parameters
    def cmd_PD(self , args):
        self.check_no_arg(args)
        if not self.cmd_pen_down:
            self.cmd_pen_down = True
            self.set_status_1(0x01)
            if self.pen_zone == self.P_IN_WINDOW:
                self.set_pen_down(True)

    # PR: Plot relative
    # 0 to n int parameters
    def cmd_PR(self , args):
        if self.pen_zone == self.P_FARAWAY:
            return
        self.plot(args, False)

    def cmd_pen_up(self):
        # 02ee
        self.cmd_pen_down = False
        self.set_status_0(0x01)
        if self.pen_zone != self.P_FARAWAY:
            self.set_pen_down(False)

    # PU: Pen up
    # No parameters
    def cmd_PU(self , args):
        self.check_no_arg(args)
        if self.cmd_pen_down:
            self.cmd_pen_up()

    # SA: Select alternate set
    # No parameters
    def cmd_SA(self , args):
        self.check_no_arg(args)
        self.text_cur_set = 1

    # SC: Scale
    # 0 or 4 int parameters
    def cmd_SC(self , args):
        if not args:
            if self.scaling is not None:
                self.scaling = None
                self.update_pen_zone_and_up()
        else:
            xmin , xmax , ymin , ymax = self.get_args(args , ParsedIntArg , 4 , 4)
            if MIN_INT_SC <= xmin <= MAX_INT_SC and \
              MIN_INT_SC <= xmax <= MAX_INT_SC and \
              MIN_INT_SC <= ymin <= MAX_INT_SC and \
              MIN_INT_SC <= ymax <= MAX_INT_SC and \
              xmin < xmax and ymin < ymax:
                self.scaling = Rectangle(Point(xmin , ymin) , Point(xmax , ymax))
                self.update_pen_zone_and_up()
                self.inverse_scale_and_update()
            else:
                raise InvalidArg()

    # SI: Set absolute character size
    # 0 or 2 dec parameters
    def cmd_SI(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_size_rel = False
            self.text_size = Point(114, 150)
            self.update_text_size()
        elif len(args) == 2:
            if 0 < args[ 0 ] < (10485 / 256) and 0 < args[ 1 ] < (10485 / 256):
                self.text_size_rel = False
                self.text_size = Point(int(args[ 0 ] * 400), int(args[ 1 ] * 400))
                self.update_text_size()
            else:
                raise InvalidArg()
        else:
            raise WrongNumArgs()

    # SL: Set slant
    # 0 or 1 dec parameter
    def cmd_SL(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 1)
        if not args:
            self.text_slant = 0.0
        elif MIN_DEC <= args[ 0 ] <= MAX_DEC:
            self.text_slant = args[ 0 ]
        else:
            raise InvalidArg()

    # SM: Symbol mode
    # 0 or 1 single-character string
    def cmd_SM(self , args):
        self.text_symbol = None
        if not args:
            return
        elif isinstance(args[ 0 ], InvalidArg):
            raise WrongNumArgs()
        else:
            self.text_symbol = self.font.translate_code(self.text_sets[ self.text_cur_set ], ord(args[ 0 ].value))
            if self.text_symbol is None:
                raise InvalidArg()

    # SP: Select pen
    # 0 or 1 int parameter
    def cmd_SP(self , args):
        args = self.get_args(args , ParsedIntArg , 0 , 1)
        if args:
            pen_no = args[ 0 ]
            if 0 <= pen_no <= 8:
                self.pen_no = pen_no
        else:
            self.pen_no = 0

    # SR: Set relative character size
    # 0 or 2 dec parameters
    def cmd_SR(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_size_rel = True
            self.text_size = Point(0.0075, 0.015)
            self.update_text_size()
        elif len(args) == 2:
            if 0 < args[ 0 ] <= MAX_DEC and 0 < args[ 1 ] <= MAX_DEC:
                self.text_size_rel = True
                self.text_size = Point(args[ 0 ] / 100, args[ 1 ] / 100)
                self.update_text_size()
            else:
                raise InvalidArg()
        else:
            raise WrongNumArgs()

    # SS: Select standard set
    # No parameters
    def cmd_SS(self , args):
        self.check_no_arg(args)
        self.text_cur_set = 0

    # TL: Set tick length
    # 0 to 2 dec parameters
    def cmd_TL(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.neg_tick = 0.005
            self.pos_tick = 0.005
        elif len(args) == 1 and 0 <= args[ 0 ] <= MAX_DEC:
            self.pos_tick = args[ 0 ] / 100.0
            self.neg_tick = 0
        elif len(args) == 2 and 0 <= args[ 0 ] <= MAX_DEC and \
          0 <= args[ 1 ] <= MAX_DEC:
            self.pos_tick = args[ 0 ] / 100.0
            self.neg_tick = args[ 1 ] / 100.0
        else:
            raise InvalidArg()

    # UC: Defined user character
    # 0 to n int parameters
    def cmd_UC(self , args):
        try:
            self.start_text_drawing()
            points = self.get_args(args , ParsedIntArg , 0 , None)
            if not points:
                self.carriage_return()
            else:
                self.zero_pos_in_cell_and_draw(False)
                pen = False
                it = iter(points)
                while (pt := next(it, None)) is not None:
                    if pt >= 99:
                        pen = True
                    elif pt <= -99:
                        pen = False
                    else:
                        tmp = pt
                        pt = next(it, None)
                        if pt is None:
                            raise WrongNumArgs()
                        elif abs(pt) > 98:
                            raise InvalidArg()
                        else:
                            self.pos_in_cell.x += tmp * 16
                            self.pos_in_cell.y += pt * 8
                            self.draw_to_point_char(pen)
                self.move_to_next_char()
        finally:
            if self.scaling is not None:
                self.inverse_scale_and_update()

    def draw_tick(self, off):
        save = deepcopy(self.last_pen)
        self.draw_to_point(off + self.last_pen, True)
        self.draw_to_point(save, True)

    # XT: Draw X ticks
    # No parameters
    def cmd_XT(self , args):
        self.check_no_arg(args)
        if self.pen_zone != self.P_FARAWAY:
            self.draw_tick(Point(0, self.pos_tick * abs(self.P2.y - self.P1.y)))
            self.draw_tick(Point(0, -self.neg_tick * abs(self.P2.y - self.P1.y)))

    # YT: Draw Y ticks
    # No parameters
    def cmd_YT(self , args):
        self.check_no_arg(args)
        if self.pen_zone != self.P_FARAWAY:
            self.draw_tick(Point(self.pos_tick * abs(self.P2.x - self.P1.x), 0))
            self.draw_tick(Point(-self.neg_tick * abs(self.P2.x - self.P1.x), 0))

    def ev_dev_clear(self , ev):
        #TODO:
        # DEV CLEAR
        pass

    def ev_listen_data(self , ev):
        # Listen data
        # EOI signal is ignored when receiving data
        # Bit 7 is masked out of each byte
        s = str(bytes([x & 0x7f for x in ev.data]) , encoding = "ascii" , errors = "replace")
        for p in self.parser.parse(s):
            cmd_fn = "cmd_" + p.cmd
            d = self.__class__.__dict__
            cmd_m = d.get(cmd_fn)
            if cmd_m:
                try:
                    res = cmd_m(self , p.args)
                    if res:
                        self.output = res.encode("ascii" , "ignore")
                except WrongNumArgs:
                    # Wrong number of parameters
                    self.set_error(2)
                except InvalidArg:
                    # Bad parameter
                    self.set_error(3)
                except InvalidChar:
                    # Illegal character
                    self.set_error(4)
                except UnknownCharSet:
                    # Unknown character set
                    self.set_error(5)
                except PosOverflow:
                    # Position overflow
                    self.set_error(6)
            else:
                # Unknown command
                self.set_error(1)

    def ev_talk(self , ev):
        if self.output:
            self.io.set_talk_data(self.output)
            self.output = None

    EV_FNS = {
        rem488.RemotizerDevClear  : ev_dev_clear,
        rem488.RemotizerData      : ev_listen_data,
        rem488.RemotizerTalk      : ev_talk
        }

    def io_event(self , ev):
        fn = self.EV_FNS.get(ev.__class__ , None)
        if fn:
            fn(self , ev)
