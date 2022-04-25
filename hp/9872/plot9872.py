#!/usr/bin/env python3
# A HP9872 emulator for use with MAME IEEE-488 remotizer
# Copyright (C) 2022 F. Ulivi <fulivi at big "G" mail>
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

# Parts of this file come from hp2xx HPGL converter (https://www.gnu.org/software/hp2xx/),
# especially the font definition.

import rem488
import sys
import itertools
import re
import io
import math

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
        self.rest = ""

    def parse(self , s):
        tot = self.rest + s
        while len(tot) >= 3:
            tot_strip = tot.lstrip()
            if len(tot_strip) < 3:
                break
            else:
                cmd = tot_strip[ :2 ].upper()
                arg = tot_strip[ 2: ]
                # LB command is terminated by ^C
                if cmd == "LB":
                    # Search for terminating ETX
                    idx = arg.find(ETX)
                    if idx >= 0:
                        yield ParsedCmd("LB" , [ ParsedString(arg[ :idx ]) ])
                        tot = arg[ idx+1: ]
                    else:
                        break
                else:
                    # Find terminating ; or \n
                    idx = arg.find(";")
                    if idx >= 0:
                        idx2 = arg.find("\n")
                        if idx2 >= 0:
                            idx = min(idx , idx2)
                    else:
                        idx = arg.find("\n")
                    if idx < 0:
                        break
                    if cmd == "SM":
                        # SM is terminated by ; or \n like other commands but it has
                        # 0 or 1 character as parameter
                        if idx < 2:
                            yield ParsedCmd(cmd , [ ParsedString(arg[ :idx ]) ])
                        else:
                            yield ParsedCmd(cmd , [ BadArg(arg[ :idx ]) ])
                    elif cmd.isalpha():
                        # Split arguments
                        pieces = arg[ :idx ].split(",")
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
                        yield ParsedCmd(cmd , args)
                    else:
                        yield ParseError(cmd)
                    tot = arg[ idx+1: ]
        self.rest = tot

class WrongNumArgs(Exception):
    pass

class InvalidArg(Exception):
    pass

class InvalidChar(Exception):
    pass

class PosOverflow(Exception):
    pass

class Point:
    def __init__(self , x , y):
        self.x = x
        self.y = y

    def dup(self):
        return Point(self.x , self.y)

    def __eq__(self , other):
        return self.x == other.x and self.y == other.y

    # Distance between self and other
    def dist(self , other):
        return math.dist([ self.x , self.y ] , [ other.x , other.y ])

    def __str__(self):
        return "({},{})".format(self.x , self.y)

class Segment:
    def __init__(self , p1 , p2):
        self.p1 = p1
        self.p2 = p2

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
    CHARSET0 = [
        # 20
        [ ],
        # 21
        [ (False,2,0),(True,2,1),(False,2,2),(True,2,6) ],
        # 22
        [ (False,1,5),(True,1,6),(False,3,5),(True,3,6) ],
        # 23
        [ (False,1,0),(True,1,6),(False,3,0),(True,3,6),(False,0,2),(True,4,2),(False,0,4),(True,4,4) ],
        # 24
        [ (False,2,0),(True,2,6),(False,4,5),(True,1,5),(True,0,4),(True,1,3),(True,3,3),(True,4,2),(True,3,1),(True,0,1) ],
        # 25
        [ (False,0,0),(True,4,6),(False,1,5),(True,1,4),(True,2,4),(True,2,5),(True,1,5),(False,2,2),(True,2,1),(True,3,1),(True,3,2),(True,2,2) ],
        # 26
        [ (False,4,0),(True,0,4),(True,0,5),(True,1,6),(True,2,5),(True,2,4),(True,0,2),(True,0,1),(True,1,0),(True,2,0),(True,4,2) ],
        # 27
        [ (False,2,5),(True,3,6) ],
        # 28
        [ (False,4,6),(True,2,4),(True,2,2),(True,4,0) ],
        # 29
        [ (False,0,0),(True,2,2),(True,2,4),(True,0,6) ],
        # 2a
        [ (False,-1,1),(True,5,5),(False,5,1),(True,-1,5),(False,2,6),(True,2,0) ],
        # 2b
        [ (False,2,1),(True,2,5),(False,0,3),(True,4,3) ],
        # 2c
        [ (False,2,0),(True,1,0),(True,1,1),(True,2,1),(True,2,-1),(True,1,-2) ],
        # 2d
        [ (False,0,3),(True,4,3) ],
        # 2e
        [ (False,2,0),(True,1,0),(True,1,1),(True,2,1),(True,2,0) ],
        # 2f
        [ (True,5,6) ],
        # 30
        [ (False,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,5),(True,3,6),(True,1,6),(True,0,5),(True,0,1) ],
        # 31
        [ (False,1,0),(True,3,0),(False,2,0),(True,2,6),(True,1,5) ],
        # 32
        [ (False,0,5),(True,1,6),(True,3,6),(True,4,5),(True,4,4),(True,0,1),(True,0,0),(True,4,0) ],
        # 33
        [ (False,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,2),(True,3,3),(True,2,3),(True,4,6),(True,0,6) ],
        # 34
        [ (False,3,6),(True,0,3),(True,0,2),(True,4,2),(False,3,3),(True,3,0) ],
        # 35
        [ (False,4,6),(True,0,6),(True,0,4),(True,3,4),(True,4,3),(True,4,1),(True,3,0),(True,1,0),(True,0,1) ],
        # 36
        [ (False,4,6),(True,2,6),(True,0,4),(True,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,2),(True,3,3),(True,0,3) ],
        # 37
        [ (False,0,6),(True,4,6),(True,4,5),(True,0,2),(True,0,0) ],
        # 38
        [ (False,3,3),(True,4,4),(True,4,5),(True,3,6),(True,1,6),(True,0,5),(True,0,4),(True,1,3),(True,3,3),(True,4,2),(True,4,1),(True,3,0),(True,1,0),(True,0,1),(True,0,2),(True,1,3) ],
        # 39
        [ (False,1,0),(True,2,0),(True,4,2),(True,4,5),(True,3,6),(True,1,6),(True,0,5),(True,0,4),(True,1,3),(True,4,3) ],
        # 3a
        [ (False,1,3),(True,1,4),(True,2,4),(True,2,3),(True,1,3),(False,1,1),(True,2,1),(True,2,0),(True,1,0),(True,1,1) ],
        # 3b
        [ (False,1,2),(True,1,3),(True,2,3),(True,2,2),(True,1,2),(False,2,0),(True,1,0),(True,1,1),(True,2,1),(True,2,-1),(True,1,-2) ],
        # 3c
        [ (False,3,6),(True,0,3),(True,3,0) ],
        # 3d
        [ (False,0,4),(True,4,4),(False,0,2),(True,4,2) ],
        # 3e
        [ (False,0,6),(True,3,3),(True,0,0) ],
        # 3f
        [ (False,0,5),(True,1,6),(True,3,6),(True,4,5),(True,4,4),(True,3,3),(True,2,3),(True,2,2),(False,2,1),(True,2,0) ],
        # 40
        [ (False,3,-1),(True,1,-1),(True,0,0),(True,0,4),(True,1,6),(True,3,6),(True,4,5),(True,4,2),(True,3,1),(True,2,2),(True,2,3),(True,3,4),(True,4,4) ],
        # 41
        [ (False,0,0),(True,0,5),(True,1,6),(True,3,6),(True,4,5),(True,4,0),(False,0,2),(True,4,2) ],
        # 42
        [ (False,0,0),(True,0,6),(True,3,6),(True,4,5),(True,4,4),(True,3,3),(True,0,3),(False,0,0),(True,3,0),(True,4,1),(True,4,2),(True,3,3) ],
        # 43
        [ (False,4,1),(True,3,0),(True,1,0),(True,0,1),(True,0,5),(True,1,6),(True,3,6),(True,4,5) ],
        # 44
        [ (False,0,0),(True,0,6),(True,3,6),(True,4,5),(True,4,1),(True,3,0),(True,0,0) ],
        # 45
        [ (False,4,0),(True,0,0),(True,0,6),(True,4,6),(False,0,3),(True,3,3) ],
        # 46
        [ (False,0,0),(True,0,6),(True,4,6),(False,0,3),(True,3,3) ],
        # 47
        [ (False,4,5),(True,3,6),(True,1,6),(True,0,5),(True,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,3),(True,1,3) ],
        # 48
        [ (False,0,0),(True,0,6),(False,4,0),(True,4,6),(False,0,3),(True,4,3) ],
        # 49
        [ (False,0,0),(True,4,0),(False,2,0),(True,2,6),(False,0,6),(True,4,6) ],
        # 4a
        [ (False,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,6),(True,0,6) ],
        # 4b
        [ (False,0,0),(True,0,6),(False,0,3),(True,1,3),(True,4,0),(False,1,3),(True,4,6) ],
        # 4c
        [ (False,0,6),(True,0,0),(True,4,0) ],
        # 4d
        [ (False,0,0),(True,0,6),(True,2,4),(True,4,6),(True,4,0) ],
        # 4e
        [ (False,0,0),(True,0,6),(True,4,0),(True,4,6) ],
        # 4f
        [ (False,1,0),(True,0,1),(True,0,5),(True,1,6),(True,3,6),(True,4,5),(True,4,1),(True,3,0),(True,1,0) ],
        # 50
        [ (False,0,0),(True,0,6),(True,3,6),(True,4,5),(True,4,4),(True,3,3),(True,0,3) ],
        # 51
        [ (False,1,0),(True,0,1),(True,0,5),(True,1,6),(True,3,6),(True,4,5),(True,4,2),(True,2,0),(True,1,0),(False,2,2),(True,4,0) ],
        # 52
        [ (False,0,0),(True,0,6),(True,3,6),(True,4,5),(True,4,4),(True,3,3),(True,0,3),(True,1,3),(True,4,0) ],
        # 53
        [ (False,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,2),(True,3,3),(True,1,3),(True,0,4),(True,0,5),(True,1,6),(True,3,6),(True,4,5) ],
        # 54
        [ (False,2,0),(True,2,6),(True,0,6),(True,4,6) ],
        # 55
        [ (False,0,6),(True,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,6) ],
        # 56
        [ (False,0,6),(True,0,4),(True,2,0),(True,4,4),(True,4,6) ],
        # 57
        [ (False,0,6),(True,0,0),(True,2,3),(True,4,0),(True,4,6) ],
        # 58
        [ (False,0,0),(True,4,6),(False,4,0),(True,0,6) ],
        # 59
        [ (False,0,6),(True,0,5),(True,2,2),(True,2,0),(False,2,2),(True,4,5),(True,4,6) ],
        # 5a
        [ (False,0,6),(True,4,6),(True,0,0),(True,4,0) ],
        # 5b
        [ (False,4,0),(True,2,0),(True,2,6),(True,4,6) ],
        # 5c
        [ (False,0,6),(True,4,0) ],
        # 5d
        [ (False,0,0),(True,2,0),(True,2,6),(True,0,6) ],
        # 5e
        [ (False,0,4),(True,2,6),(True,4,4) ],
        # 5f
        [ (False,0,-1),(True,4,-1) ],
        # 60
        [ (False,1,7),(True,3,4) ],
        # 61
        [ (False,4,0),(True,1,0),(True,0,1),(True,0,3),(True,1,4),(True,3,4),(True,3,0) ],
        # 62
        [ (False,0,0),(True,3,0),(True,4,1),(True,4,3),(True,3,4),(True,1,4),(False,1,6),(True,1,0) ],
        # 63
        [ (False,4,1),(True,3,0),(True,2,0),(True,1,1),(True,1,3),(True,2,4),(True,3,4),(True,4,3) ],
        # 64
        [ (False,3,6),(True,3,0),(True,1,0),(True,0,1),(True,0,3),(True,1,4),(True,3,4),(False,3,0),(True,4,0) ],
        # 65
        [ (False,0,2),(True,3,2),(True,4,3),(True,3,4),(True,1,4),(True,0,3),(True,0,1),(True,1,0),(True,4,0) ],
        # 66
        [ (False,2,0),(True,2,5),(True,3,6),(True,4,6),(False,1,3),(True,3,3) ],
        # 67
        [ (False,0,-2),(True,2,-2),(True,3,-1),(True,3,4),(True,1,4),(True,0,3),(True,0,1),(True,1,0),(True,3,0) ],
        # 68
        [ (False,0,6),(True,0,0),(False,0,4),(True,2,4),(True,3,3),(True,3,0) ],
        # 69
        [ (False,2,6),(True,2,5),(False,1,4),(True,2,4),(True,2,0),(False,1,0),(True,3,0) ],
        # 6a
        [ (False,2,6),(True,2,5),(False,1,4),(True,2,4),(True,2,-1),(True,1,-2),(True,0,-2) ],
        # 6b
        [ (False,0,0),(True,0,6),(False,3,0),(True,0,2),(True,3,4) ],
        # 6c
        [ (False,1,6),(True,2,6),(True,2,0),(False,1,0),(True,3,0) ],
        # 6d
        [ (False,0,0),(True,0,4),(False,0,3),(True,1,4),(True,2,3),(True,2,0),(False,2,3),(True,3,4),(True,4,3),(True,4,0) ],
        # 6e
        [ (False,0,0),(True,0,4),(False,0,3),(True,1,4),(True,2,4),(True,3,3),(True,3,0) ],
        # 6f
        [ (False,1,0),(True,0,1),(True,0,3),(True,1,4),(True,2,4),(True,3,3),(True,3,1),(True,2,0),(True,1,0) ],
        # 70
        [ (False,0,-2),(True,0,4),(True,2,4),(True,3,3),(True,3,1),(True,2,0),(True,0,0) ],
        # 71
        [ (False,3,0),(True,1,0),(True,0,1),(True,0,3),(True,1,4),(True,3,4),(True,3,-2) ],
        # 72
        [ (False,0,4),(True,0,0),(False,0,2),(True,2,4),(True,3,4) ],
        # 73
        [ (False,3,4),(True,1,4),(True,0,3),(True,1,2),(True,2,2),(True,3,1),(True,2,0),(True,0,0) ],
        # 74
        [ (False,1,6),(True,1,0),(True,3,0),(False,0,4),(True,3,4) ],
        # 75
        [ (False,0,4),(True,0,1),(True,1,0),(True,3,0),(True,3,4) ],
        # 76
        [ (False,0,4),(True,0,2),(True,2,0),(True,4,2),(True,4,4) ],
        # 77
        [ (False,0,4),(True,0,1),(True,1,0),(True,2,1),(True,2,3),(False,2,1),(True,3,0),(True,4,1),(True,4,4) ],
        # 78
        [ (False,0,4),(True,4,0),(False,0,0),(True,4,4) ],
        # 79
        [ (False,0,-2),(True,4,2),(True,4,4),(False,0,4),(True,0,2),(True,2,0) ],
        # 7a
        [ (False,0,4),(True,3,4),(True,0,0),(True,3,0) ],
        # 7b
        [ (False,3,7),(True,2,7),(True,1,6),(True,1,4),(True,0,3),(True,1,2),(True,1,0),(True,2,-1),(True,3,-1) ],
        # 7c
        [ (False,2,7),(True,2,-1) ],
        # 7d
        [ (False,1,7),(True,2,7),(True,3,6),(True,3,4),(True,4,3),(True,3,2),(True,3,0),(True,2,-1),(True,1,-1) ],
        # 7e
        [ (False,0,5),(True,1,6),(True,3,4),(True,4,5) ]
    ]

    # Differences of charsets 1-4 wrt charset 0
    # Key is:
    # [0]   Charset no.
    # [1]   Character code
    DIFFS = {
        (1 , 0x5c): [ (False,0,2),(True,1,2),(True,2,0),(True,3,5),(True,4,5) ],
        (1 , 0x5e): [ (False,2,6),(True,2,0),(False,0,4),(True,2,6),(True,4,4) ],
        (1 , 0x7b): [ (False,0,5),(True,1,6),(True,3,4),(True,4,5),(False,1,0),(True,1,6),(False,3,0),(True,3,4) ],
        (1 , 0x7c): [ (False,2,7),(True,2,-1),(False,0,3),(True,4,3) ],
        (1 , 0x7d): [ (False,0,3),(True,4,3),(False,3,2),(True,4,3),(True,3,4) ],
        (2 , 0x23): [ (False,4,5),(True,4,6),(True,3,6),(True,2,4),(True,2,1),(True,1,0),(True,0,0),(True,0,1),(True,1,2),(True,3,0),(True,4,1),(False,1,4),(True,3,4),(False,1,3),(True,3,3) ],
        (2 , 0x27): [ (False,1,6),(True,3,9) ],
        (2 , 0x5c): [ (False,4,1),(True,3,0),(True,2,0),(True,1,1),(True,1,3),(True,2,4),(True,3,4),(True,4,3),(False,2,0),(True,1,-1) ],
        (2 , 0x7b): [ (False,1,8),(True,1,7),(False,3,8),(True,3,7) ],
        (2 , 0x7c): [ (False,2,6),(True,1,7),(True,2,8),(True,3,7),(True,2,6) ],
        (2 , 0x7d): [ (False,1,6),(True,1,5),(False,3,6),(True,3,5) ],
        (2 , 0x7e): [ (False,3,5),(True,3,6) ],
        (3 , 0x23): [ (False,4,5),(True,4,6),(True,3,6),(True,2,4),(True,2,1),(True,1,0),(True,0,0),(True,0,1),(True,1,2),(True,3,0),(True,4,1),(False,1,4),(True,3,4),(False,1,3),(True,3,3) ],
        (3 , 0x5b): [ (False,0,1),(True,1,0),(True,3,0),(True,4,1),(True,4,5),(True,3,6),(True,1,6),(True,0,5),(True,0,1),(True,4,5) ],
        (3 , 0x5c): [ (False,4,6),(True,2,6),(True,2,0),(True,4,0),(False,4,3),(True,1,3),(False,2,6),(True,0,0) ],
        (3 , 0x5d): [ (False,1,0),(True,0,1),(True,0,3),(True,1,4),(True,2,4),(True,3,3),(True,3,1),(True,2,0),(True,1,0),(False,0,0),(True,3,4) ],
        (3 , 0x5e): [ (False,4,1),(True,3,0),(True,2,1),(True,2,3),(True,3,4),(True,4,3),(True,3,2),(True,1,2),(True,0,1),(True,1,0),(True,2,1),(False,2,3),(True,1,4),(True,0,3) ],
        (3 , 0x7b): [ (False,1,8),(True,1,7),(False,3,8),(True,3,7) ],
        (3 , 0x7c): [ (False,2,6),(True,1,7),(True,2,8),(True,3,7),(True,2,6) ],
        (3 , 0x7d): [ (False,1,6),(True,1,5),(False,3,6),(True,3,5) ],
        (3 , 0x7e): [ (False,2,4),(True,1,5),(True,2,6),(True,3,5),(True,2,4) ],
        (4 , 0x23): [ (False,2,6),(True,2,5),(False,2,4),(True,2,3),(True,1,3),(True,0,2),(True,0,1),(True,1,0),(True,3,0),(True,4,1) ],
        (4 , 0x27): [ (False,1,6),(True,3,9) ],
        (4 , 0x5c): [ (False,2,6),(True,2,5),(False,2,4),(True,2,0) ],
        (4 , 0x7b): [ (False,-1,7),(True,1,8),(True,4,7),(True,6,8) ],
        (4 , 0x7c): [ (False,0,7),(True,1,8),(True,3,6),(True,4,7) ],
        (4 , 0x7d): [ (False,-1,6),(True,1,7),(True,4,6),(True,6,6) ],
        (4 , 0x7e): [ (False,0,6),(True,1,7),(True,3,5),(True,4,6) ]
    }

    # Chars with auto-backspace
    AUTOBS = frozenset([
        (1 , 0x5f) , (1 , 0x60) , (1 , 0x7e) ,
        (2 , 0x27) , (2 , 0x5e) , (2 , 0x5f) , (2 , 0x60) , (2 , 0x7b) , (2 , 0x7c) , (2 , 0x7d) ,
        (3 , 0x5f) , (3 , 0x7b) , (3 , 0x7c) , (3 , 0x7d) , (3 , 0x7e) ,
        (4 , 0x27) , (4 , 0x5e) , (4 , 0x5f) , (4 , 0x7b) , (4 , 0x7c) , (4 , 0x7d) , (4 , 0x7e)])

    def __init__(self):
        pass

    def get_char(self , charset , char):
        if char < 0x20 or char > 0x7e:
            return None
        k = (charset , char)
        d = self.DIFFS.get(k)
        if d != None:
            return d
        else:
            return self.CHARSET0[ char - 0x20 ]

    def has_auto_backspace(self , charset , char):
        k = (charset , char)
        return k in self.AUTOBS

class Plotter:
    # Line type patterns
    LT_PATTERNS = {
        # Line typ 1
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
        self.srq_pending = False
        self.io.set_rsv_state(False)
        self.set_in_masks = (223 , 0 , 0)

    def set_defaults(self):
        self.clear_status()
        self.window = Rectangle(Point(MIN_X_PHY , MIN_Y_PHY) , Point(MAX_X_PHY , MAX_Y_PHY))
        self.line_type = self.LT_SOLID
        self.line_type_pct = 4
        self.update_pen_zone()
        self.scaling = None
        self.text_dir_x = 1
        self.text_dir_y = 0
        self.text_dir_rel = True
        self.text_slant = 0.0
        self.text_size_x = 0.0075
        self.text_size_y = 0.015
        self.text_size_rel = True
        # [0]   Standard set
        # [1]   Alternate set
        self.text_sets = [ 0 , 0 ]
        self.text_cur_set = 0
        self.text_symbol = None
        self.compute_text_vars()
        self.neg_tick = 0.005
        self.pos_tick = 0.005

    def compute_text_vars(self):
        x = self.text_dir_x
        y = self.text_dir_y
        if self.text_dir_rel:
            x *= abs(self.P1.x - self.P2.x)
            y *= abs(self.P1.y - self.P2.y)
        self.text_dir = math.atan2(y , x)
        cdir = math.cos(self.text_dir)
        sdir = math.sin(self.text_dir)
        w = self.text_size_x
        h = self.text_size_y
        if self.text_size_rel:
            w *= abs(self.P1.x - self.P2.x)
            h *= abs(self.P1.y - self.P2.y)
        self.text_xx = w * cdir / 4
        self.text_yx = w * sdir / 4
        self.text_xy = h * (self.text_slant * cdir - sdir) / 8
        self.text_yy = h * (self.text_slant * sdir + cdir) / 8
        s = w * 1.5
        self.text_char_dx = s * cdir
        self.text_char_dy = s * sdir
        l = h * 2
        self.text_line_dx = l * sdir
        self.text_line_dy = -l * cdir
        self.text_center_off_x = int(-self.text_char_dx / 3 + self.text_line_dx * 3 / 16)
        self.text_center_off_y = int(-self.text_char_dy / 3 + self.text_line_dy * 3 / 16)

    def initialize(self):
        self.clear_status()
        self.status = 0
        self.set_status(0x18)
        self.set_error(0)
        self.P1 = Point(DEF_X_P1 , DEF_Y_P1)
        self.P2 = Point(DEF_X_P2 , DEF_Y_P2)
        # Stored pen positions:
        # pen           Current pen position in plotter units. Unclipped, can be in nearby zone.
        # last_pen      Current pen position in plotter units. Clipped with current window.
        # last_cmd_pen  Commanded pen position, optionally scaled. Can be in nearby zone.
        self.pen = Point(0 , 0)
        self.last_pen = Point(0 , 0)
        self.last_cmd_pen = Point(0 , 0)
        self.set_pen_zone(self.P_IN_WINDOW)
        self.text_cr_point = Point(0 , 0)
        # Position where last segment/point ended
        self.last_pen_draw = Point(NO_X_PEN , NO_Y_PEN)
        # Position where pen last went down
        self.last_pen_down = Point(NO_X_PEN , NO_Y_PEN)
        # Ensure pen status update
        self.pen_down = True
        self.set_pen_down(False)
        self.set_defaults()

    def is_drawing(self):
        return self.pen_down and self.pen_no != 0

    def set_pen_down(self , state):
        if not self.pen_down and state:
            self.set_status_1(0x01)
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
        if self.pen_zone != self.P_FARAWAY:
            self.set_pen_zone(self.P_IN_WINDOW if self.window.contains(self.pen) else self.P_NEARBY)

    def scale_factors(self):
        if self.scaling:
            self.scale_xf = (self.P2.x - self.P1.x) / (self.scaling.pur.x - self.scaling.pll.x)
            self.scale_yf = (self.P2.y - self.P1.y) / (self.scaling.pur.y - self.scaling.pll.y)
            self.scale_ux = self.P1.x - int(self.scale_xf * self.scaling.pll.x)
            self.scale_uy = self.P1.y - int(self.scale_yf * self.scaling.pll.y)

    def scale(self , p):
        if self.scaling:
            xsc = int(self.scale_xf * p.x) + self.scale_ux
            ysc = int(self.scale_yf * p.y) + self.scale_uy
            return Point(xsc , ysc)
        else:
            return p

    def inverse_scale(self , p):
        if self.scaling:
            xisc = int((p.x - self.scale_ux) / self.scale_xf)
            yisc = int((p.y - self.scale_uy) / self.scale_yf)
            return Point(xisc , yisc)
        else:
            return p

    # Scale point p (if scaling is enabled) and classify its position
    # Returns z , psc
    # z is zone of p (In window, nearby or faraway)
    # psc is p scaled
    def get_pt_zone(self , p , relative = False):
        if self.scaling:
            if p.x < MIN_INT_SC or p.x > MAX_INT_SC or \
              p.y < MIN_INT_SC or p.y > MAX_INT_SC:
                return self.P_FARAWAY , p
            else:
                if relative:
                    p.x += self.last_cmd_pen.x
                    p.y += self.last_cmd_pen.y
                    if p.x < MIN_INT_SC or p.x > MAX_INT_SC or \
                      p.y < MIN_INT_SC or p.y > MAX_INT_SC:
                        return self.P_FARAWAY , p
                self.last_cmd_pen = p.dup()
                psc = self.scale(p)
                if psc.x < MIN_INT_SC or psc.x > MAX_INT_SC or \
                  psc.y < MIN_INT_SC or psc.y > MAX_INT_SC:
                    return self.P_FARAWAY , psc
                elif self.window.contains(psc):
                    return self.P_IN_WINDOW , psc
                else:
                    return self.P_NEARBY , psc
        else:
            if p.x < MIN_INT_NO_SC or p.x > MAX_INT_NO_SC or \
              p.y < MIN_INT_NO_SC or p.y > MAX_INT_NO_SC:
                return self.P_FARAWAY , p
            else:
                if relative:
                    p.x += self.last_cmd_pen.x
                    p.y += self.last_cmd_pen.y
                    if p.x < MIN_INT_NO_SC or p.x > MAX_INT_NO_SC or \
                      p.y < MIN_INT_NO_SC or p.y > MAX_INT_NO_SC:
                        return self.P_FARAWAY , p
                self.last_cmd_pen = p.dup()
                if self.window.contains(p):
                    return self.P_IN_WINDOW , p
                else:
                    return self.P_NEARBY , p

    def segment_output(self , s):
        if not s.null_len() or s.p1 != self.last_pen_draw:
            s.pen_no = self.pen_no
            self.io.draw_segment(s)
            self.last_pen_draw = s.p2.dup()

    def draw_segment(self , s , force_solid = False):
        if self.is_drawing():
            # Line type
            if self.line_type == self.LT_SOLID or force_solid:
                # Solid
                self.segment_output(s)
            elif self.line_type == self.LT_2_POINTS:
                # Two dots (0)
                self.draw_point(s.p1)
                self.draw_point(s.p2)
            else:
                # Generic pattern 1..6
                pat = self.LT_PATTERNS[ self.line_type ]
                one_pct = (self.P1.dist(self.P2) * self.line_type_pct) / 10000
                seg_len = s.length()
                seg_used = 0
                dx = s.p2.x - s.p1.x
                dy = s.p2.y - s.p1.y
                while seg_used < seg_len:
                    pat_rem_pct = pat[ self.line_pat_idx ] - self.line_pat_used
                    pat_rem = pat_rem_pct * one_pct
                    run = min(pat_rem , seg_len - seg_used)
                    if (self.line_pat_idx & 1) == 0:
                        # Draw
                        seg_frac = seg_used / seg_len
                        new_seg_p1 = Point(int(s.p1.x + dx * seg_frac) , int(s.p1.y + dy * seg_frac))
                        seg_used += run
                        seg_frac = seg_used / seg_len
                        new_seg_p2 = Point(int(s.p1.x + dx * seg_frac) , int(s.p1.y + dy * seg_frac))
                        self.segment_output(Segment(new_seg_p1 , new_seg_p2))
                    else:
                        # Gap
                        seg_used += run
                    pat_used_pct = self.line_pat_used + run / one_pct
                    if pat_used_pct < pat[ self.line_pat_idx ]:
                        self.line_pat_used = pat_used_pct
                    else:
                        self.line_pat_used = 0
                        self.line_pat_idx = (self.line_pat_idx + 1) % len(pat)
        self.last_pen = s.p2

    def draw_clipped_segment(self , s , force_solid = False):
        s_clip = self.window.clip_segment(s)
        if s_clip:
            self.draw_segment(s_clip , force_solid)
        if self.text_symbol:
            pt = s.p2.dup()
            pt.x += self.text_center_off_x
            pt.y += self.text_center_off_y
            self.draw_char(pt , self.text_symbol_charset , self.text_symbol)

    def draw_always_clipped_segment(self , s):
        # pen_no != 0 must be checked outside this fn
        s_clip = self.window.clip_segment(s)
        if s_clip:
            self.segment_output(s_clip)
            self.last_pen = s_clip.p2

    def draw_point(self , p):
        if self.is_drawing():
            self.segment_output(Segment(p.dup() , p.dup()))
        self.last_pen = p

    def char_grid_to_pt(self , p , x , y):
        px = self.text_xx * x + self.text_xy * y + p.x
        py = self.text_yx * x + self.text_yy * y + p.y
        return Point(px , py)

    def draw_char(self , p , charset , c):
        if self.pen_no != 0:
            shape = self.font.get_char(charset , c)
            prev_pt = p.dup()
            if shape is not None:
                for d , x , y in shape:
                    next_pt = self.char_grid_to_pt(p , x , y)
                    if d:
                        self.draw_always_clipped_segment(Segment(prev_pt , next_pt))
                    prev_pt = next_pt

    def adjust_pen_after_char(self):
        # Adjust pen zone and last_cmd_pen according to final pen position
        # after plotting characters
        if self.scaling:
            if not MIN_INT_SC <= self.pen.x <= MAX_INT_SC or \
              not MIN_INT_SC <= self.pen.y <= MAX_INT_SC:
                z = self.P_FARAWAY
            elif self.window.contains(self.pen):
                z = self.P_IN_WINDOW
            else:
                z = self.P_NEARBY
            if z != self.P_FARAWAY:
                new_pen = self.inverse_scale(self.pen)
                if not MIN_INT_SC <= new_pen.x <= MAX_INT_SC or \
                  not MIN_INT_SC <= new_pen.y <= MAX_INT_SC:
                    z = self.P_FARAWAY
                else:
                    self.last_cmd_pen = new_pen
        else:
            z , _ = self.get_pt_zone(self.pen)
        self.set_pen_zone(z)
        if z == self.P_FARAWAY:
            raise PosOverflow()

    def set_error(self , err_no):
        if err_no == 0:
            # Clear error
            self.io.set_error_led(False)
            self.err_no = 0
            self.set_status_0(0x20)
        elif (1 << (err_no - 1)) & self.set_in_masks[ 0 ]:
            self.io.set_error_led(True)
            self.err_no = err_no
            self.set_status_1(0x20)

    def update_status(self , prev_status):
        self.io.set_status_byte(self.status & 0xbf)
        to_1 = ~prev_status & self.status
        if to_1 & self.set_in_masks[ 1 ]:
            self.io.set_rsv_state(True)
            self.srq_pending = True
        elif self.srq_pending and (self.status & self.set_in_masks[ 1 ]) == 0:
            self.io.set_rsv_state(False)
            self.srq_pending = False
        self.io.set_pp_state(self.status & self.set_in_masks[ 2 ])

    def set_status(self , status):
        if self.status != status:
            save = self.status
            self.status = status
            self.update_status(save)

    def set_status_1(self , mask):
        self.set_status(self.status | mask)

    def set_status_0(self , mask):
        self.set_status(self.status & ~mask)

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
                raise InvalidArg()
        else:
            self.text_sets[ 1 ] = 0
        return None

    # CP: Character plot
    # 0 to 2 dec parameters
    def cmd_CP(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        save_pen = self.pen.dup()
        if not args:
            # CRLF
            if self.pen_zone != self.P_FARAWAY:
                self.text_cr_point.x += self.text_line_dx
                self.text_cr_point.y += self.text_line_dy
                self.pen = self.text_cr_point.dup()
                # It's assumed here that line type is always solid
                self.draw_clipped_segment(Segment(save_pen , self.pen) , True)
                self.adjust_pen_after_char()
        elif len(args) == 2 and MIN_DEC <= args[ 0 ] <= MAX_DEC and \
          MIN_DEC <= args[ 1 ] <= MAX_DEC:
            if self.pen_zone != self.P_FARAWAY:
                self.pen.x += args[ 0 ] * self.text_char_dx - args[ 1 ] * self.text_line_dx
                self.pen.y += args[ 0 ] * self.text_char_dy - args[ 1 ] * self.text_line_dy
                self.text_cr_point = self.pen.dup()
                self.draw_clipped_segment(Segment(save_pen , self.pen) , True)
                self.adjust_pen_after_char()
        else:
            raise InvalidArg()
        return None

    # CS: Select standard character set
    # 0 or 1 int parameter
    def cmd_CS(self , args):
        args = self.get_args(args , ParsedIntArg , 0 , 1)
        if args:
            if 0 <= args[ 0 ] <= 4:
                self.text_sets[ 0 ] = args[ 0 ]
            else:
                raise InvalidArg()
        else:
            self.text_sets [ 0 ] = 0
        return None

    # DF: Set defaults
    # No parameters
    def cmd_DF(self , args):
        self.check_no_arg(args)
        self.set_defaults()
        return None

    # DI: Set absolute direction
    # 0 or 2 dec parameters
    def cmd_DI(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_dir_x = 1.0
            self.text_dir_y = 0.0
            self.text_dir_rel = False
            self.compute_text_vars()
            self.text_cr_point = self.pen.dup()
        elif len(args) == 2 and MIN_DEC <= args[ 0 ] <= MAX_DEC and \
          MIN_DEC <= args[ 1 ] <= MAX_DEC and (args[ 0 ] != 0 or args[ 1 ] != 0):
            self.text_dir_x = args[ 0 ]
            self.text_dir_y = args[ 1 ]
            self.text_dir_rel = False
            self.compute_text_vars()
            self.text_cr_point = self.pen.dup()
        else:
            raise InvalidArg()
        return None

    # DR: Set relative direction
    # 0 or 2 dec parameters
    def cmd_DR(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_dir_x = 1.0
            self.text_dir_y = 0.0
            self.text_dir_rel = True
            self.compute_text_vars()
            self.text_cr_point = self.pen.dup()
        elif len(args) == 2 and MIN_DEC <= args[ 0 ] <= MAX_DEC and \
          MIN_DEC <= args[ 1 ] <= MAX_DEC and (args[ 0 ] != 0 or args[ 1 ] != 0):
            self.text_dir_x = args[ 0 ]
            self.text_dir_y = args[ 1 ]
            self.text_dir_rel = True
            self.compute_text_vars()
            self.text_cr_point = self.pen.dup()
        else:
            raise InvalidArg()
        return None

    # IM: Set input mask
    # 0 to 3 int parameters
    def cmd_IM(self , args):
        args = self.get_args(args , ParsedIntArg , 0 , 3)
        if not args:
            self.set_in_masks = (223 , 0 , 0)
        else:
            im = list(self.set_in_masks)
            for i , m in enumerate(args):
                if not 0 <= m <= 255:
                    raise InvalidArg()
                im[ i ] = m
            self.set_in_masks = tuple(im)
        self.update_status(self.status)
        return None

    # IN: Initialize
    # No parameters
    def cmd_IN(self , args):
        self.check_no_arg(args)
        self.initialize()
        return None

    # IP: Input P1/P2 points
    # 0 or 4 int parameters
    def cmd_IP(self , args):
        if not args:
            self.P1 = Point(DEF_X_P1 , DEF_Y_P1)
            self.P2 = Point(DEF_X_P2 , DEF_Y_P2)
            self.compute_text_vars()
        else:
            llx , lly , urx , ury = self.get_args(args , ParsedIntArg , 4 , 4)
            if MIN_X_PHY <= llx <= MAX_X_PHY and \
                MIN_X_PHY <= urx <= MAX_X_PHY and \
                MIN_Y_PHY <= lly <= MAX_Y_PHY and \
                MIN_Y_PHY <= ury <= MAX_Y_PHY and \
                llx != urx and lly != ury:
                self.P1 = Point(min(llx , urx) , min(lly , ury))
                self.P2 = Point(max(llx , urx) , max(lly , ury))
                self.compute_text_vars()
            else:
                raise InvalidArg()
        self.set_status_1(0x02)
        self.scale_factors()
        return None

    # IW: Set window
    # 0 or 4 int parameters
    def cmd_IW(self , args):
        if not args:
            self.window = Rectangle(Point(MIN_X_PHY , MIN_Y_PHY) , Point(MAX_X_PHY , MAX_Y_PHY))
        else:
            llx , lly , urx , ury = self.get_args(args , ParsedIntArg , 4 , 4)
            if abs(llx) > ABS_MAX_INT or abs(lly) > ABS_MAX_INT or abs(urx) > ABS_MAX_INT or abs(ury) > ABS_MAX_INT:
                raise InvalidArg()
            llx = min(max(MIN_X_PHY , llx) , MAX_X_PHY)
            urx = min(max(MIN_X_PHY , urx) , MAX_X_PHY)
            lly = min(max(MIN_Y_PHY , lly) , MAX_Y_PHY)
            ury = min(max(MIN_Y_PHY , ury) , MAX_Y_PHY)
            if llx >= urx or lly >= ury:
                raise InvalidArg()
            self.window = Rectangle(Point(llx , lly) , Point(urx , ury))
        self.update_pen_zone()
        return None

    # LB: Label
    # 1 string parameter
    def cmd_LB(self , args):
        if self.pen_zone == self.P_FARAWAY:
            return None
        s = args[ 0 ].value
        invalid_chars = False
        for c in s:
            ordc = ord(c)
            # NOP codes: BEL, HT, FF, DCx
            if ordc == 0x07 or ordc == 0x09 or ordc == 0x0c or 0x11 <= ordc <= 0x14:
                pass
            # BS
            elif ordc == 0x08:
                self.pen = self.char_grid_to_pt(self.pen , -6 , 0)
            # LF
            elif ordc == 0x0a:
                self.text_cr_point.x += self.text_line_dx
                self.text_cr_point.y += self.text_line_dy
                self.pen.x += self.text_line_dx
                self.pen.y += self.text_line_dy
            # VT
            elif ordc == 0x0b:
                self.text_cr_point.x -= self.text_line_dx
                self.text_cr_point.y -= self.text_line_dy
                self.pen.x -= self.text_line_dx
                self.pen.y -= self.text_line_dy
            # CR
            elif ordc == 0x0d:
                self.pen = self.text_cr_point.dup()
            # SO
            elif ordc == 0x0e:
                self.text_cur_set = 1
            # SI
            elif ordc == 0x0f:
                self.text_cur_set = 0
            elif 0x20 <= ordc <= 0x7e:
                charset = self.text_sets[ self.text_cur_set ]
                if self.font.has_auto_backspace(charset , ordc):
                    self.pen = self.char_grid_to_pt(self.pen , -6 , 0)
                self.draw_char(self.pen , charset , ordc)
                self.pen = self.char_grid_to_pt(self.pen , 6 , 0)
            else:
                invalid_chars = True
        self.adjust_pen_after_char()
        if invalid_chars:
            raise InvalidChar()
        return None

    # LT: Set line type
    # 0 to 2 dec parameters
    def cmd_LT(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if args:
            if args[ 0 ] < 0 or args[ 0 ] >= 7:
                raise InvalidArg()
            if len(args) == 2 and (args[ 1 ] <= 0 or args[ 1 ] > MAX_DEC):
                raise InvalidArg()
            self.line_type = int(args[ 0 ])
            self.line_type_pct = 4 if len(args) < 2 else args[ 1 ]
            self.line_pat_idx = 0
            self.line_pat_used = 0
        else:
            self.line_type = self.LT_SOLID
            self.line_type_pct = 4
        return None

    # OA: Output actual position
    # No parameters
    def cmd_OA(self , args):
        self.check_no_arg(args)
        return "{},{},{}\r\n".format(int(self.last_pen.x) , int(self.last_pen.y) , int(self.pen_down))

    # OC: Output commanded position
    # No parameters
    def cmd_OC(self , args):
        self.check_no_arg(args)
        if self.pen_zone == self.P_FARAWAY:
            return "{},{},0\r\n".format(MAX_INT_NO_SC , MAX_INT_NO_SC)
        else:
            return "{},{},{}\r\n".format(int(self.last_cmd_pen.x) , int(self.last_cmd_pen.y) , int(self.pen_down))

    # OE: Output error
    # No parameters
    def cmd_OE(self , args):
        self.check_no_arg(args)
        save = self.err_no
        self.set_error(0)
        return "{}\r\n".format(save)

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
        return "{},{},{},{}\r\n".format(self.P1.x , self.P1.y , self.P2.x , self.P2.y)

    # OS: Output status
    # No parameters
    def cmd_OS(self , args):
        self.check_no_arg(args)
        save = self.status & 0xbf
        if self.srq_pending:
            save |= 0x40
        self.set_status_0(8)
        return "{}\r\n".format(save)

    # PA: Plot absolute
    # 0 to n int parameters
    def cmd_PA(self , args):
        points = self.get_args(args , ParsedIntArg , 0 , None)
        for px , py in group_pairs(points):
            if py == None:
                # odd number of parameters
                raise WrongNumArgs()
            pt = Point(px , py)
            z , psc = self.get_pt_zone(pt)
            if self.pen_zone == self.P_FARAWAY:
                if z != self.P_FARAWAY:
                    self.pen = psc
                    if z == self.P_IN_WINDOW:
                        self.last_pen = psc
            elif z == self.P_IN_WINDOW or z == self.P_NEARBY:
                s = Segment(self.pen , psc)
                self.draw_clipped_segment(s)
                self.pen = psc
            self.set_pen_zone(z)
        self.text_cr_point = self.pen.dup()
        return None

    # PD: Pen down
    # No parameters
    def cmd_PD(self , args):
        self.check_no_arg(args)
        if self.pen_zone != self.P_FARAWAY:
            self.set_pen_down(True)
        return None

    # PR: Plot relative
    # 0 to n int parameters
    def cmd_PR(self , args):
        points = self.get_args(args , ParsedIntArg , 0 , None)
        for px , py in group_pairs(points):
            if py == None:
                # odd number of parameters
                raise WrongNumArgs()
            pt = Point(px , py)
            if self.pen_zone != self.P_FARAWAY:
                z , psc = self.get_pt_zone(pt , True)
                if z == self.P_IN_WINDOW or z == self.P_NEARBY:
                    s = Segment(self.pen , psc)
                    self.draw_clipped_segment(s)
                    self.pen = psc
                self.set_pen_zone(z)
        self.text_cr_point = self.pen.dup()
        return None

    # PU: Pen up
    # No parameters
    def cmd_PU(self , args):
        self.check_no_arg(args)
        self.set_pen_down(False)
        return None

    # SA: Select alternate set
    # No parameters
    def cmd_SA(self , args):
        self.check_no_arg(args)
        self.text_cur_set = 1
        return None

    # SC: Scale
    # 0 or 4 int parameters
    def cmd_SC(self , args):
        if not args:
            self.scaling = None
        else:
            xmin , xmax , ymin , ymax = self.get_args(args , ParsedIntArg , 4 , 4)
            if MIN_INT_SC <= xmin <= MAX_INT_SC and \
              MIN_INT_SC <= xmax <= MAX_INT_SC and \
              MIN_INT_SC <= ymin <= MAX_INT_SC and \
              MIN_INT_SC <= ymax <= MAX_INT_SC and \
              xmin < xmax and ymin < ymax:
                self.scaling = Rectangle(Point(xmin , ymin) , Point(xmax , ymax))
                self.scale_factors()
            else:
                raise InvalidArg()
        return None

    # SI: Set absolute character size
    # 0 or 2 dec parameters
    def cmd_SI(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_size_rel = False
            self.text_size_x = 114
            self.text_size_y = 150
            self.compute_text_vars()
        elif len(args) == 2 and 0 < args[ 0 ] <= MAX_DEC and 0 < args[ 1 ] <= MAX_DEC:
            self.text_size_rel = False
            self.text_size_x = args[ 0 ] * 400
            self.text_size_y = args[ 1 ] * 400
            self.compute_text_vars()
        else:
            raise InvalidArg()
        return None

    # SL: Set slant
    # 0 or 1 dec parameter
    def cmd_SL(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 1)
        if not args:
            self.text_slant = 0.0
            self.compute_text_vars()
        elif MIN_DEC <= args[ 0 ] <= MAX_DEC:
            self.text_slant = args[ 0 ]
            self.compute_text_vars()
        else:
            raise InvalidArg()
        return None

    # SM: Symbol mode
    # 0 or 1 single-character string
    def cmd_SM(self , args):
        args = self.get_args(args , ParsedString , 1 , 1)
        if not args[ 0 ]:
            self.text_symbol = None
        else:
            ordc = ord(args[ 0 ])
            if 0x21 <= ordc <= 0x7e:
                self.text_symbol = ordc
                self.text_symbol_charset = self.text_sets[ self.text_cur_set ]
            else:
                raise InvalidChar()
        return None

    # SP: Select pen
    # 0 or 1 int parameter
    def cmd_SP(self , args):
        args = self.get_args(args , ParsedIntArg , 0 , 1)
        if args:
            pen_no = args[ 0 ]
            if 0 <= pen_no <= 8:
                self.pen_no = pen_no
            else:
                raise InvalidArg()
        else:
            self.pen_no = 0
        return None

    # SR: Set relative character size
    # 0 or 2 dec parameters
    def cmd_SR(self , args):
        args = self.get_args(args , ParsedFixArg , 0 , 2)
        if not args:
            self.text_size_rel = True
            self.text_size_x = 0.0075
            self.text_size_y = 0.015
            self.compute_text_vars()
        elif len(args) == 2 and 0 < args[ 0 ] <= MAX_DEC and 0 < args[ 1 ] <= MAX_DEC:
            self.text_size_rel = True
            self.text_size_x = args[ 0 ] / 100
            self.text_size_y = args[ 1 ] / 100
            self.compute_text_vars()
        else:
            raise InvalidArg()
        return None

    # SS: Select standard set
    # No parameters
    def cmd_SS(self , args):
        self.check_no_arg(args)
        self.text_cur_set = 0
        return None

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
        return None

    # UC: Defined user character
    # 0 to n int parameters
    def cmd_UC(self , args):
        points = self.get_args(args , ParsedIntArg , 0 , None)
        if self.pen_zone != self.P_FARAWAY:
            i = iter(points)
            draw = False
            prev_pt = self.pen.dup()
            x = 0
            y = 0
            for dx in i:
                if dx == -99:
                    draw = False
                elif dx == 99:
                    draw = True
                elif -98 <= dx <= 98:
                    try:
                        dy = next(i)
                    except StopIteration:
                        raise InvalidArg()
                    if -98 <= dy <= 98:
                        x += dx
                        y += dy
                        next_pt = self.char_grid_to_pt(self.pen , x , y)
                        if draw and self.pen_no != 0:
                            self.draw_always_clipped_segment(Segment(prev_pt , next_pt))
                        prev_pt = next_pt
                    else:
                        raise InvalidArg()
                else:
                    raise InvalidArg()
            self.pen = self.char_grid_to_pt(self.pen , 6 , 0)
            self.adjust_pen_after_char()
        return None

    # XT: Draw X ticks
    # No parameters
    def cmd_XT(self , args):
        self.check_no_arg(args)
        if self.pen_zone != self.P_FARAWAY and self.pen_no != 0:
            p1 = self.pen.dup()
            p2 = self.pen.dup()
            save_last_pen = self.last_pen
            p1.y -= self.neg_tick * abs(self.P2.y - self.P1.y)
            p2.y += self.pos_tick * abs(self.P2.y - self.P1.y)
            self.draw_always_clipped_segment(Segment(p1 , p2))
            self.last_pen = save_last_pen
        return None

    # YT: Draw Y ticks
    # No parameters
    def cmd_YT(self , args):
        self.check_no_arg(args)
        if self.pen_zone != self.P_FARAWAY and self.pen_no != 0:
            p1 = self.pen.dup()
            p2 = self.pen.dup()
            save_last_pen = self.last_pen
            p1.x -= self.neg_tick * abs(self.P2.x - self.P1.x)
            p2.x += self.pos_tick * abs(self.P2.x - self.P1.x)
            self.draw_always_clipped_segment(Segment(p1 , p2))
            self.last_pen = save_last_pen
        return None

    def ev_dev_clear(self , ev):
        #TODO:
        # DEV CLEAR
        pass

    def ev_listen_data(self , ev):
        # Listen data
        s = str(ev.data , encoding = "ascii" , errors = "replace")
        for p in self.parser.parse(s):
            if isinstance(p , ParsedCmd):
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
                    except PosOverflow:
                        # Position overflow
                        self.set_error(6)
                else:
                    # Unknown command
                    self.set_error(1)
            else:
                # Parse error
                self.set_error(4)

    def ev_talk(self , ev):
        if self.output:
            self.io.set_talk_data(self.output)
            self.output = None

    def ev_serial_poll(self , ev):
        self.io.set_rsv_state(False)
        self.srq_pending = False

    EV_FNS = {
        rem488.RemotizerDevClear  : ev_dev_clear,
        rem488.RemotizerData      : ev_listen_data,
        rem488.RemotizerTalk      : ev_talk,
        rem488.RemotizerSerialPoll: ev_serial_poll
        }

    def io_event(self , ev):
        fn = self.EV_FNS.get(ev.__class__ , None)
        if fn:
            fn(self , ev)
