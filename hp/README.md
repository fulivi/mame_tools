Tools for MAME emulation of HP systems
======================================

## mini_9895

This tool emulates drive #0 of a HP9895 dual-floppy drive. It's meant to be interfaced with MAME emulation of HP9845 or HP85 systems through the IEEE-488 remotizer.

It has a lot of limitations that you need to be aware of:

   - It only emulates read & write operations. Formatting and auxiliary commands are not supported.
   - There is no error-checking at all. In most cases an error condition will crash the tool.
   - It only supports drive #0 (and not drive #1).
   - HPIB address of drive is fixed at 0.
   - Image size is not checked (it must be either 1152000 or 1182720 bytes if cylinders are 75 or 77, respectively).

**Remotizer will be available in MAME when PR #3241 is merged.** See the [PR status](https://github.com/mamedev/mame/pull/3241).

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
