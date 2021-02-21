#!/usr/bin/env python3
# An utility to interface Hercules & MAME with BiSync lines
# Copyright (C) 2021 F. Ulivi <fulivi at big "G" mail>
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

import codecs
import socket
import select
import sys
import asyncio
import io

DUMP=0

CH_SYN=b"\x32"
CH_STX=b"\x02"
CH_ETX=b"\x03"
CH_ETB=b"\x26"
CH_ENQ=b"\x2d"
CH_DLE=b"\x10"
CH_ACK=b"\x2e"
CH_NAK=b"\x3d"
CH_EOT=b"\x37"
CH_PAD=b"\xff"
CH_ACK0 = b"\x70"
CH_ACK1 = b"\x61"
CH_WACK = b"\x6b"
CH_RVI = b"\x7c"

B_DLE = 0x10
B_ACK0 = 0x70
B_ACK1 = 0x61
B_WACK = 0x6b
B_RVI = 0x7c
B_ENQ = 0x2d
B_SYN = 0x32
B_STX = 0x02
B_ETX = 0x03
B_ETB = 0x26
B_NAK = 0x3d
B_SOH = 0x01
B_IUS = 0x1f
B_EOT = 0x37
B_PAD = 0xff

def dump(data , out):
    for i , b in enumerate(data):
        if (i % 16) == 0:
            print("\n{:04x} ".format(i) , end='' , file = out)
        print("{:02x} ".format(b) , end='' , file = out)
    print(file = out)

class Message:
    pass

class MsgNAK(Message):
    def __init__(self):
        pass

    def encode(self):
        return (CH_NAK , CH_PAD)

    def __str__(self):
        return "NAK"

class MsgEOT(Message):
    def __init__(self):
        pass

    def encode(self):
        return (CH_EOT , CH_PAD)

    def __str__(self):
        return "EOT"

class MsgDLEEOT(Message):
    def __init__(self):
        pass

    def encode(self):
        return (CH_DLE + CH_EOT , CH_PAD)

    def __str__(self):
        return "DLE-EOT"

class MsgEnq(Message):
    def __init__(self , poll_data):
        self.poll_data = poll_data

    def encode(self):
        return (self.poll_data + CH_ENQ , CH_PAD)

    def __str__(self):
        return "ENQ"

class MsgText(Message):
    def __init__(self , text , transparent , first):
        self.text = text
        self.transparent = transparent
        self.first = first

    def encode(self):
        crc = CRC16()
        if self.transparent:
            enc_text = bytearray()
            enc_text.append(B_DLE)
            enc_text.append(self.text[ 0 ])
            if not self.first:
                crc.add_byte(B_DLE)
                crc.add_byte(self.text[ 0 ])
            for b in self.text[ 1:-1 ]:
                if b == B_DLE:
                    enc_text.append(B_DLE)
                enc_text.append(b)
                crc.add_byte(b)
            enc_text.append(B_DLE)
            enc_text.append(self.text[ -1 ])
            crc.add_byte(self.text[ -1 ])
        else:
            enc_text = self.text
            if not self.first:
                crc.add_byte(self.text[ 0 ])
            for b in self.text[ 1: ]:
                crc.add_byte(b)
        if self.text[ -1 ] == B_ENQ:
            return (enc_text , CH_PAD)
        else:
            return (enc_text , crc.get_crc())

    def __str__(self):
        s = "TEXT, len={}, T={}, F={}, end={:02x}".format(len(self.text) , int(self.transparent) , int(self.first) , self.text[ -1 ])
        if DUMP:
            dmp = io.StringIO()
            dump(self.text , dmp)
            asc = self.text.decode(encoding="cp500", errors="replace")
            print("As text: >{}<".format(asc) , file = dmp)
            s += dmp.getvalue()
            dmp.close()
        return s

class MsgAck(Message):
    def __init__(self , seq):
        self.seq = seq

    def encode(self):
        return (CH_DLE + (CH_ACK1 if self.seq else CH_ACK0) , CH_PAD)

    def __str__(self):
        return "ACK" + str(self.seq)

class MsgWAck(Message):
    def __init__(self):
        pass

    def encode(self):
        return (CH_DLE + CH_WACK , CH_PAD)

    def __str__(self):
        return "WACK"

class MsgRVI(Message):
    def __init__(self):
        pass

    def encode(self):
        return (CH_DLE + CH_RVI , CH_PAD)

    def __str__(self):
        return "RVI"

class CRC16:
    def __init__(self):
        # x^15 is stored in LSB
        self.crc = 0

    def clear(self):
        self.crc = 0

    def add_byte(self , b):
        for bit_n in range(8):
            bit = b & 1
            b >>= 1
            if (self.crc & 1) ^ bit:
                self.crc = (self.crc >> 1) ^ 0xa001
            else:
                self.crc >>= 1

    def get_crc(self):
        return bytes([ self.crc & 0xff , (self.crc >> 8) & 0xff ])

class Sync_IO:
    def __init__(self , syn , reader , writer , hercules):
        self.syn = syn
        self.reader = reader
        self.writer = writer
        self.hercules = hercules
        # 0     Hunt for sync
        # 1     Synchronized, idle
        # 2     Non-transparent packet started
        # 3     Transparent packet started
        # 4     After DLE in transparent packet
        # 5     After initial DLE
        # 6     Wait for PAD
        # 7     After DLE in header
        self.enter_hunt()
        self.accum = 0

    def enter_hunt(self):
        if self.hercules:
            self.state = 1
        else:
            self.state = 0
            self.bit_cnt = 8
        self.bcc_bytes = 0
        self.poll_data = bytearray()
        self.in_text = False

    def msg_done(self , msg):
        self.next_msg = msg
        if self.hercules:
            self.enter_hunt()
            return True
        else:
            self.state = 6
            return False

    def wait_bcc(self):
        if self.hercules:
            if self.msg_ended:
                self.enter_hunt()
            else:
                self.crc = CRC16()
            return True
        else:
            self.bcc_bytes = 2
            return False

    async def rx_fsm(self , byt):
        if DUMP:
            print("{} S {} B {:02x}".format('H' if self.hercules else 'M' , self.state , byt))
        if self.bcc_bytes > 0:
            # Accumulate BCC bytes
            self.crc.add_byte(byt)
            self.bcc_bytes -= 1
            if self.bcc_bytes == 0:
                if self.crc.crc != 0:
                    print("Wrong CRC! {:04x}".format(self.crc.crc))
                yield self.next_msg
                if self.msg_ended:
                    self.enter_hunt()
                else:
                    self.crc = CRC16()
        elif self.state == 1:
            # Idle
            if byt == B_NAK:
                if self.msg_done(MsgNAK()):
                    yield self.next_msg
            elif byt == B_STX or byt == B_SOH:
                self.state = 2
                self.crc = CRC16()
                self.text = bytearray([ byt ])
                self.in_text = byt == B_STX
                self.first = True
            elif byt == B_DLE:
                self.state = 5
            elif byt == self.syn or byt == B_PAD:
                pass
            elif byt == B_EOT:
                if self.msg_done(MsgEOT()):
                    yield self.next_msg
            elif byt == B_ENQ:
                if self.msg_done(MsgEnq(self.poll_data)):
                    yield self.next_msg
            else:
                self.poll_data.append(byt)
        elif self.state == 2:
            # In non-transparent text
            if byt == B_STX:
                self.in_text = True
            elif byt == B_DLE:
                if not self.in_text:
                    self.state = 7
                    self.crc.add_byte(byt)
                    return
            elif byt == self.syn:
                return
            self.text.append(byt)
            self.crc.add_byte(byt)
            if byt == B_ETX or byt == B_ETB or byt == B_IUS:
                self.next_msg = MsgText(self.text , False , self.first)
                if byt == B_IUS:
                    self.in_text = False
                    self.msg_ended = False
                    self.first = False
                    self.text = bytearray()
                else:
                    self.msg_ended = True
                if self.wait_bcc():
                    yield self.next_msg
            elif byt == B_ENQ:
                print("ENQ discards text!")
                yield MsgText(self.text , False , False)
                self.enter_hunt()
        elif self.state == 3:
            # In transparent text
            if byt == B_DLE:
                self.state = 4
            else:
                self.text.append(byt)
                self.crc.add_byte(byt)
        elif self.state == 4:
            # After DLE in transparent text
            if byt == B_SYN:
                return
            else:
                self.text.append(byt)
                self.crc.add_byte(byt)
                self.state = 3
                if byt == B_ETX or byt == B_ETB or byt == B_IUS:
                    self.next_msg = MsgText(self.text , True , self.first)
                    if byt == B_IUS:
                        self.msg_ended = False
                        self.first = False
                        self.state = 2
                    else:
                        self.msg_ended = True
                    if self.wait_bcc():
                        yield self.next_msg
                elif byt == B_ENQ:
                    print("ENQ discards text!")
                    yield MsgText(self.text , True , False)
                    self.enter_hunt()
        elif self.state == 5:
            # After initial DLE
            if byt == B_STX or byt == B_SOH:
                self.state = 3
                self.crc = CRC16()
                self.text = bytearray([ byt ])
                self.first = True
            elif byt == B_EOT:
                if self.msg_done(MsgDLEEOT()):
                    yield self.next_msg
            elif byt == B_ACK0 or byt == B_ACK1:
                if self.msg_done(MsgAck(int(byt == B_ACK1))):
                    yield self.next_msg
            elif byt == B_WACK:
                if self.msg_done(MsgWAck()):
                    yield self.next_msg
            elif byt == B_RVI:
                if self.msg_done(MsgRVI()):
                    yield self.next_msg
            else:
                print("Unexpected {:02x} after DLE".format(byt))
                self.enter_hunt()
        elif self.state == 6:
            # Waiting for PAD
            if byt == B_PAD:
                yield self.next_msg
            else:
                print("PAD expected, {:02x} received!".format(byt))
            self.enter_hunt()
        elif self.state == 7:
            if byt == B_STX or byt == B_SOH:
                self.state = 3
                self.crc.add_byte(byt)
                self.text = bytearray([ byt ])
            else:
                print("Unexpected byte {:02x}".format(byt))
                self.enter_hunt()

    async def get_rx_msg(self):
        while True:
            rx_byte = await self.reader.read(1)
            if len(rx_byte) == 0:
                break
            rx_byte = rx_byte[ 0 ]
            if self.hercules:
                async for msg in self.rx_fsm(rx_byte):
                    yield msg
            else:
                # print("RX {:02x}".format(rx_byte))
                for bit in range(8):
                    self.accum = (self.accum >> 1) & 0x7fff
                    if rx_byte & (1 << bit):
                        self.accum |= 0x8000
                    self.bit_cnt -= 1
                    if self.bit_cnt == 0:
                        self.bit_cnt = 8
                        if self.state == 0:
                            # Hunt mode
                            if self.accum == (self.syn << 8) | self.syn:
                                # got sync
                                self.state = 1
                                self.bit_cnt = 16
                                print("Synched")
                            else:
                                self.bit_cnt = 1
                        else:
                            byt = self.accum & 0xff
                            async for msg in self.rx_fsm(byt):
                                yield msg

    async def tx_msg(self , msg):
        enc_msg , trailer = msg.encode()
        if not self.hercules:
            self.writer.write(CH_SYN * 2)
        if DUMP:
            print("Msg body")
            dump(enc_msg , sys.stdout)
        self.writer.write(enc_msg)
        if not self.hercules:
            if DUMP:
                print("Msg trailer")
                dump(trailer , sys.stdout)
            self.writer.write(trailer)
        await self.writer.drain()

async def receiver(rx):
    try:
        return await rx.__anext__()
    except StopAsyncIteration:
        return None

async def serve_mame(m_rd , m_wr):
    try:
        print("Connected!")
        mame_io = Sync_IO(B_SYN , m_rd , m_wr , False)
        mame_rx = mame_io.get_rx_msg()
        print("Connecting on Hercules side..")
        h_rd , h_wr = await asyncio.open_connection("localhost" , 2703)
        print("Connected!")
        hercules_io = Sync_IO(B_SYN , h_rd , h_wr , True)
        hercules_rx = hercules_io.get_rx_msg()
        rx_mame = asyncio.create_task(receiver(mame_rx))
        rx_hercules = asyncio.create_task(receiver(hercules_rx))
        while True:
            d , _ = await asyncio.wait({ rx_mame , rx_hercules } , timeout = 1.0 , return_when = asyncio.FIRST_COMPLETED)
            if d:
                for t in d:
                    exc = t.exception()
                    if exc:
                        raise exc
                    else:
                        msg = t.result()
                        if not msg:
                            print("Disconnected!")
                            h_wr.close()
                            await h_wr.wait_closed()
                            return
                        elif t == rx_mame:
                            print("MAME-> : {}".format(str(msg)))
                            await hercules_io.tx_msg(msg)
                            rx_mame = asyncio.create_task(receiver(mame_rx))
                        else:
                            print("HERC-> : {}".format(str(msg)))
                            await mame_io.tx_msg(msg)
                            rx_hercules = asyncio.create_task(receiver(hercules_rx))
            else:
                print("T/O")
                m_wr.write(CH_SYN * 2 + CH_PAD)
                await m_wr.drain()
    except ConnectionError:
        print("Connection error")

async def main():
    print("Connecting on MAME side..")
    server = await asyncio.start_server(serve_mame, '0.0.0.0', 2780)

    async with server:
        await server.serve_forever()

if __name__ == '__main__':
    asyncio.run(main())
