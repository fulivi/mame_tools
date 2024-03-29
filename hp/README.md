Tools for MAME emulation of HP systems
======================================

## hp disk

The "HP disk" tool is described in its own README file (see `hp_disk` directory).

## 9111

"9111" is an emulator of HP9111A graphic tablet for MAME-emulated systems (see `9111` directory).

## 9872

"9872" is an emulator of HP9872C plotter for MAME-emulated systems (see `9872` directory).

## mux

"mux" is a tool acting as a virtual bus cable for MAME IEEE-488 Remotizer. It allows the connection of more than 1 external device to MAME-emulated systems (see `mux` directory).

## RJE

This directory contains files & tools for BiSync communication between MAME & Hercules (see [here](https://fulivi.github.io/mame_tools/rje_9845.html) for instructions).

## hp9825t_artwork

This directory contains artwork files for HP9825T.

## amigo_drive

This tool emulates an HP drive implementing the so-called "Amigo" protocol. It connects to a HP system running in MAME through the IEEE-488 remotizer. The following drives are emulated.

| Model name | Units per drive | Raw capacity per unit |
| -----------|-----------------|-------------------|
| 9134b      | 1               | 9.26 MiB |
| 9895       | 2               | 1.13 MiB |

This tool comes in two version: one written in Python 3 and one in C++14 (for performance).

### Compilation

The CMake tool is needed to compile the C++ version of amigo_drive (see [CMake home page](https://cmake.org/)).

A C++14 compiler that is recognized by CMake is also needed. I tested compilation with GCC 6 on Debian Linux.

Follow these steps to compile.

   1. `cd hp/build`
   2. `cmake -DCMAKE_BUILD_TYPE=Release ../amigo_drive_cpp/`
   3. `make install/strip`
   4. The `amigo_drive` executable is installed in `hp` directory (one level up from `build`).

### Usage

The command line syntax of amigo_drive is as follows:

`amigo_drive[.py] [-p ` _port_`] [-d ` _debug_file_`] ` _model_ `[ ` _image file for unit #0_  _image file for unit #1_`..]`

The `-p` and `-d` switches are available in Python version only. Image files can be less than the number of units in the drive. Units without an image file are reported "not ready" to host.

The amigo_drive tool is to be launched before running MAME.

#### Usage on HP9845

Use these options on the MAME command line:

`-slot0 98034_hpib -rom1 massd -slot0:98034_hpib:ieee_rem remote488 -bitb1 socket.localhost:1234`

When emulating an HP9895 drive, the first unit is referred to as `:H7` and the second one as `:H7,0,1`.

#### Usage on HP85

Use these options on the MAME command line:

`-slot1 82937_hpib -rom1 mass -slot1:82937_hpib:ieee_rem remote488 -bitb1 socket.localhost:1234`

When emulating an HP9895 drive, the first unit is referred to as `:D700` and the second one as `:D701`.

#### Usage on HP64000

Use these options on the MAME command line:

`-ieee_rem remote488 -bitb socket.localhost:1234`

The 9134b model is to be selected in amigo_drive. In order to boot from HD the "System source" DIP switch should be set to "Sys bus".

It is advisable to enable the "Long HPIB timeout" cheat on HP64000 emulation as the real system has a very tight (< 10 ms) timeout on DSJ command. This timeout is sometimes missed due to scheduling variations and other factors when using amigo_drive. In order to enable the cheat these options should be added to command line:

`-cheat -cheatpath ` _path to "hp" directory_

Once the emulation has started, bring up the UI and activate the cheat.

The cheat is not strictly necessary as the system will usually recover from missed DSJs. However, disk I/O slows down considerably when access is retried.

#### Usage on HP IPC

Use these options on the MAME command line:

`-ieee_rem remote488 -bitb socket.localhost:1234`

You can directly use the raw images of IPC disks that are available around on the Internet. Set amigo_drive to emulate a HP9895 to load any of these. The IPC OS doesn't check for the correct size so images for the internal floppy drive can be loaded into an external 9895.

If you want to create an image from scratch you need to format it before files can be stored there, just as with real disks. Follow these steps:

   1. Create a 0-sized file as image and mount it in amigo_drive (any drive model is ok),
   2. Start IPC emulation and load "Utilities" disk in internal floppy,
   3. Select "format_disc" command in PAM,
   4. Press F4 to set target drive to "D000",
   5. (Optionally) Press F3 to set the image name,
   6. Press F1 to start the format operation.

#### Usage on HP 9000/3xx

Use these options on the MAME command line:

`-ieee_rem remote488 -bitb socket.localhost:1234`

## mini_9895

This tool emulates drive #0 of a HP9895 dual-floppy drive. It's meant to be interfaced with MAME emulation of HP9845 or HP85 systems through the IEEE-488 remotizer.

It has a lot of limitations that you need to be aware of:

   - It only emulates read & write operations. Formatting and auxiliary commands are not supported.
   - There is no error-checking at all. In most cases an error condition will crash the tool.
   - It only supports drive #0 (and not drive #1).
   - HPIB address of drive is fixed at 0.
   - Image size is not checked (it must be either 1152000 or 1182720 bytes if cylinders are 75 or 77, respectively).

### Usage

Follow these steps to use this tool with MAME.

   1. Start MAME with these options on HP9845:

      `-slot0 98034_hpib -rom1 massd -slot0:98034_hpib:ieee_rem remote488 -bitb1 socket.localhost:1234`

      On HP85, add these options:

      `-slot1 82937_hpib -rom1 mass -slot1:82937_hpib:ieee_rem remote488 -bitb1 socket.localhost:1234`

   2. Before emulation begins, start mini_9895 as follows:

      `mini_9895.py` _image_file_

      Where _image_file_ points to the image file.

**Never directly use valuable image files: use a copy instead. Don't trust the tool not to damage the images.**

For your convenience, empty formatted images are available in `empty_images.zip`.
