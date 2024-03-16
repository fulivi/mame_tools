#!/usr/bin/env python3
# A multiplexer for MAME IEEE-488 remotizer
# Copyright (C) 2022-2024 F. Ulivi <fulivi at big "G" mail>
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
import asyncio
import argparse

SIGNAL_MASK=0x0f

class Connected:
    def __init__(self , rem_id):
        self.rem_id = rem_id

    def __str__(self):
        return f"Port {self.rem_id.port} connected"

class Disconnected:
    def __init__(self , rem_id , e):
        self.rem_id = rem_id
        self.e = e

    def __str__(self):
        return f"Port {self.rem_id.port} disconnected, exception = {self.e!s}"

class RemotizerMsg:
    def __init__(self , rem_id , msg_type , msg_data):
        self.rem_id = rem_id
        self.msg_type = msg_type
        self.msg_data = msg_data
        self.encoded = self.encode()

    def encode(self):
        return bytes(f"{self.msg_type}:{self.msg_data:02x}\n" , encoding = "ascii")

    def __str__(self):
        return f"Msg {self.msg_type}:{self.msg_data:02x}"

class NullRead(Exception):
    def __str__(self):
        return "Null read exception"

class Rem488Port:
    SERVER_MSGS = frozenset([ ord(x) for x in "DEJQRSXY" ])
    NON_SERVER_MSGS = frozenset([  ord(x) for x in "DEKPRSXY" ])

    def __init__(self , is_server , port , q_out):
        self.is_server = is_server
        self.port = port
        self.allowed_inp_msgs = self.SERVER_MSGS if is_server else self.NON_SERVER_MSGS
        self.q_in = asyncio.Queue()
        self.q_out = q_out
        if self.is_server:
            self.tk = asyncio.create_task(self.server_task())
        else:
            self.tk = asyncio.create_task(self.client_task())

    async def server_task(self):
        server = await asyncio.start_server(self.conn , "127.0.0.1" , self.port , backlog = 1)
        await server.serve_forever()

    async def client_task(self):
        while True:
            while True:
                try:
                    rd , wr = await asyncio.open_connection("127.0.0.1" , self.port)
                    break
                except ConnectionError as e:
                    await asyncio.sleep(1)
            await self.conn(rd , wr)

    async def conn(self , rd , wr):
        self.tk_rd = asyncio.create_task(self.rd_task(rd))
        self.tk_wr = asyncio.create_task(self.wr_task(wr))
        await self.q_out.put(Connected(self))
        if self.tk_rd.cancelled():
            e_rd = None
        else:
            try:
                await self.tk_rd
                e_rd = None
            except Exception as e:
                e_rd = e
        if self.tk_wr.cancelled():
            e_wr = None
        else:
            try:
                await self.tk_wr
                e_wr = None
            except Exception as e:
                e_wr = e
        e = e_rd if e_rd is not None else e_wr
        await self.q_out.put(Disconnected(self , e))

    async def rd_task(self , rd):
        parser_state = 0
        while True:
            try:
                inp = await rd.read(4)
                if len(inp) == 0:
                    self.tk_wr.cancel()
                    raise NullRead()
            except ConnectionError as e:
                self.tk_wr.cancel()
                raise e
            for b in inp:
                c = chr(b)
                if parser_state == 0:
                    msg_type = c
                    if b in self.allowed_inp_msgs:
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
                        await self.q_out.put(RemotizerMsg(self , msg_type , data))
                    else:
                        parser_state = 5
                else:
                    if c.isspace() or c == ',' or c == ';':
                        parser_state = 0

    async def wr_task(self , wr):
        try:
            while True:
                m = await self.q_in.get()
                if isinstance(m , RemotizerMsg):
                    wr.write(m.encoded)
                    await wr.drain()
        except ConnectionError as e:
            self.tk_rd.cancel()
            raise e
        except asyncio.CancelledError:
            raise

    async def send(self , msg):
        if log_level > 1:
            print(f"{msg!s} > {self.port}")
        await self.q_in.put(msg)

async def align_signals(connected , signals , to_skip = None):
    new_signals = SIGNAL_MASK
    for p in connected:
        new_signals &= p.signals
    to_set = new_signals & ~signals;
    to_clear = ~new_signals & signals;
    for p in connected:
        if p is not to_skip:
            if to_set:
                await p.send(RemotizerMsg(None , "S" , to_set))
            if to_clear:
                await p.send(RemotizerMsg(None , "R" , to_clear))
    return new_signals

def get_global_pp(connected):
    pp = 0
    for r in connected:
        pp |= r.pp
    return pp

async def main(ports):
    q_in = asyncio.Queue()
    rems = []
    for s , p in ports:
        rems.append(Rem488Port(s , p , q_in))
        if log_level > 0:
            print(f"{'Server' if s else 'Client'} port {p} created")
    connected = set()
    checkpoint_sender = None
    checkpoint_receivers = set()
    q_delayed = None
    signals = SIGNAL_MASK
    while True:
        if q_delayed is None or checkpoint_sender is not None:
            e = await q_in.get()
        elif q_delayed.empty():
            q_delayed = None
            continue
        else:
            e = await q_delayed.get()
        p = e.rem_id
        if isinstance(e , Connected):
            if log_level > 0:
                print(str(e))
            connected.add(p)
            p.signals = SIGNAL_MASK
            tmp = signals & SIGNAL_MASK
            if tmp:
                await p.send(RemotizerMsg(None , "S" , tmp))
            tmp = ~signals & SIGNAL_MASK
            if tmp:
                await p.send(RemotizerMsg(None , "R" , tmp))
            p.pp = 0
        elif isinstance(e , Disconnected):
            if log_level > 0:
                print(str(e))
            connected.remove(p)
            signals = await align_signals(connected , signals)
            if p in checkpoint_receivers:
                checkpoint_receivers.remove(p)
                if not checkpoint_receivers:
                    await checkpoint_sender.send(RemotizerMsg(None , "Y" , int(checkpoint_flush)))
                    checkpoint_sender = None
        elif isinstance(e , RemotizerMsg):
            if log_level > 1:
                print(f"{e!s} < {p.port}")
            if e.msg_type == "J":
                # Reply to ping
                await p.send(RemotizerMsg(None , "K" , 0))
            elif checkpoint_sender is None:
                # Normal processing (not waiting for CP)
                if e.msg_type == "D" or e.msg_type == "E":
                    # Send data to every connected port but do not loop back into sender
                    for r in connected:
                        if r is not p:
                            await r.send(e)
                elif e.msg_type == "X":
                    # Propagate checkpoint request
                    checkpoint_receivers.clear()
                    for r in connected:
                        if r is not p:
                            await r.send(e)
                            checkpoint_receivers.add(r)
                    if not checkpoint_receivers:
                        await p.send(RemotizerMsg(None , "Y" , 0))
                    else:
                        checkpoint_sender = p
                        checkpoint_flush = False
                elif e.msg_type == "R":
                    p.signals &= ~e.msg_data
                    signals = await align_signals(connected , signals , p)
                elif e.msg_type == "S":
                    p.signals |= e.msg_data
                    signals = await align_signals(connected , signals , p)
                elif e.msg_type == "Q":
                    pp = get_global_pp(connected)
                    await p.send(RemotizerMsg(None , "P" , pp))
                elif e.msg_type == "P":
                    p.pp = e.msg_data
                    pp = get_global_pp(connected)
                    m = RemotizerMsg(None , "P" , pp)
                    for r in connected:
                        if r is not p:
                            await r.send(m)
            else:
                # Waiting for CP
                if e.msg_type == "Y":
                    if p in checkpoint_receivers:
                        if e.msg_data != 0:
                            checkpoint_flush = True
                        checkpoint_receivers.remove(p)
                        if not checkpoint_receivers:
                            await checkpoint_sender.send(RemotizerMsg(None , "Y" , int(checkpoint_flush)))
                            checkpoint_sender = None
                else:
                    if q_delayed is None:
                        q_delayed = asyncio.Queue()
                    await q_delayed.put(e)

def port(arg):
    if len(arg) < 3 or arg[ 1 ] != ":":
        raise ValueError()
    mode_s = arg[ 0 ].upper()
    if mode_s != "C" and mode_s != "S":
        raise ValueError()
    port_s = arg[ 2: ]
    port = int(port_s)
    if port < 1 or port > 65535:
        raise ValueError()
    return mode_s == "S" , port

def parse_cl():
    parser = argparse.ArgumentParser(description = "A multiplexer for IEEE-488 Remotizer")
    parser.add_argument('--verbose' , '-v' , help = "Increase verbosity level" , action='count' , default=0)
    parser.add_argument("port" , nargs="+" , help="Port specification having the form [cs]:xxxx" , type = port)
    args = parser.parse_args()
    global log_level
    log_level = args.verbose
    # Check for duplicated port numbers
    accum = set()
    for _ , p in args.port:
        if p in accum:
            print(f"Port {p} used more than once")
            sys.exit(1)
        accum.add(p)
    return args.port

if __name__ == '__main__':
    ports = parse_cl()
    asyncio.run(main(ports))
