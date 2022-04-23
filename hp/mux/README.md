Mux
===

"Mux" is a multiplexer for the MAME IEEE-488 Remotizer. It acts as a "virtual bus cable" when more than two nodes are connected on the bus.
It is typically used to interface a MAME-emulated system (e.g. a HP9845) with IEEE-488 peripherals such as disk drives, graphic tablets and plotters.

Without "mux" a MAME system can only be connected to a single external device.

## Installation

Mux is composed of just `mux.py`. It can be run in-place and has no other dependencies other than a python 3 run-time.

## Usage

Mux connects together multiple IEEE-488 nodes. Each node is connected to mux with either a server or client TCP connection.

MAME Remotizer connects to external device(s) by opening a client connection: in this case mux should have a server port listening for the incoming connection.
Emulated peripherals on the other hand opens a server connection so a client connection should be opened to them.

Mux can open as many server and client ports as needed. The only requirement is that every port number is different from the others.

Command line usage of mux is as follows:

`mux.py [sc]:nnnn ...`

Ports are specified by "s" for server and "c" for client, followed by a colon and the port number.

For example:

`mux.py s:1232 c:1234 c:1235 c:1236`

In this case mux opens a server port (1232) and three client ports (1234, 1235 and 1236).

Mux can be exited by simply interrupting the process (usually by pressing Ctrl-C).

## Change history

+ 1.0: first release
