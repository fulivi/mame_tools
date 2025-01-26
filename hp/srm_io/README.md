srm_io
======

`srm_io` is a SRM file server for MAME-emulated HP desktop computers.
SRM (Shared Resource Management) was a network technology from HP that predated Ethernet LANs.
It allowed multiple desktop computers to remotely access files, printers and plotters.

See [here](https://www.hp9845.net/9845/tutorials/networks/index.html) for more information.

At a physical level, network nodes communicate with a variant of SDLC protocol on a logical bus. In the first version of SRM released by HP, the bus is implemented
by connecting in a star topology to a central multiplexer with synchronous RS422 serial lines. The multiplexer arbitrates access to bus.
It guarantees that one node at most is sending a packet at any time and it forwards packets to all the nodes except the one transmitting.
In the second implementation of the bus, all nodes are directly connected to bus in a way that closely resembles Ethernet 10base2. Each node uses a "T" splitter to connect to a single coax cable.
In both versions nodes on the bus are identified by an unique address, in 0-63 range.

MAME emulation implements the first version. It emulates the 98629 SRM card and 98028 multiplexer. I/O happens through a "bitbanger" interface through which raw physical-level bits are exchanged.

`srm_io` is a Python3 script that implements the SRM file sharing service. It interfaces to emulated
system's bitbanger through a TCP socket.

`srm_io` allows the emulated systems to access files and directories below a given point in the filesystem tree of the host machine where `srm_io` runs.

Only directories and regular files are considered when exporting to SRM. Directories are mapped 1:1 (as long as the name doesn't exceed 16 characters in length).

Files are only exported if their name on the host filesystem has this form:

    <LIF name>.<boot address>.<LIF type>

  * `<LIF name>` is the name by which the file is visible on SRM

  * `<boot address>` is a 32-bit number (8 hex digits) that is exported as boot address metadata of the file

  * `<LIF type>` is a 16-bit number (4 hex digits) that encodes the LIF type of the file. This one too is exported in metadata.

`<boot address>` and `<LIF type>` fields are not visible in the exported file name.

For example, file "THING" having boot address = `0x12345678` and LIF type = `0xe950` is stored in the host machine with this name:

`THING.12345678.e950`

**WARNING**

`srm_io` doesn't implement the slightest bit of a security policy. Use at your own risk. Never run it as root.

## Instructions ##

### `srm_io` side ###

The Python script needs to be started before MAME, as it opens a server (listening) socket. It is invoked in this way:

    python3 srm_io.py [--port <PORT>] [--addr <ADDR>] [<top directory>]

  * `<PORT>` defines the TCP port where to listen. By default port 1235 is used.

  * `<ADDR>` defines the SDLC address of the SRM server. By default it has address 0.

  * `<top directory>` specifies the top directory of the exported filesystem. It's "SRM" by default.

### MAME side ###

The following options are to be added to command line when invoking MAME (assuming slot0 is free for 98629 card and port 1235 is used to interface to `srm_io`):

    -slot0 98629 -bitb socket.localhost:1235

## Acknowledgments ##

I'd like to thank Sven Schnelle for his work on SRM protocol reverse engineering. He developed [LANSRM](https://github.com/svenschnelle/lansrm) which has the same function as `srm_io` but uses standard UDP packets.
Needless to say, I thoroughly studied LANSRM to write `srm_io`.

## Change history

+ 1.0: first release
