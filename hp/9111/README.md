9111
====

"9111" is an emulator of HP9111A graphic tablet. It connects to MAME through the IEEE-488 remotizer.

## Installation

9111 is composed of the following files:

+ `9111.py`
+ `digitizer.py`
+ `resources.py`

These files must be placed in the same directory. The emulator also needs `rem488.py` from HP disk emulator. You can either copy it over to 9111 directory or set the `PYTHONPATH` environment variable to point to HP disk directory.

An optional file (`backgrounds.json`) lists additional backgrounds to be loaded beside the default one. See below for a detailed description.

9111 is written in Python 3. It requires the [PyQt6 library](https://www.riverbankcomputing.com/software/pyqt/download) which can be installed with `pip install PyQt6`.

## Usage

9111 is run by launching `9111.py` in the Python 3 interpreter. It takes an optional parameter to set the TCP port where it listens for connection (`-p port`). By default port 1234 is used.

9111 is to be run before starting MAME. It waits for a connection from MAME remotizer on TCP port specified by `-p` command line option.

The GUI layout is quite simple: most of the window is occupied by the digitizer plate. At the bottom right corner the 3 LED indicators are simulated. Interaction with the digitizer happens through the mouse: a left button click simulates pen pressure on the plate, a right button click brings up the popup menu for background selection.

The default background is embedded in the `resources.py` file. Additional backgrounds can be specified by using the `backgrounds.json` JSON file. The top-level structure in the file must be an array where each element is a new background. Each background is specified by a JSON object having the following fields:

+ `name`: a string holding the name of the background (what is shown in the popup menu). This name must be unique and it must be different from "default".
+ `file`: a string with the image file to be loaded
+ `ll`: an object with `x` & `y` fields holding the coordinates of the lower left point of the main digitizing area (the one bounded by the big rectangle)
+ `ur`: an object with the x/y coordinates of the upper right point of the digitizing area

Background images are always displayed with 1.2 aspect ratio.

This is an example of `backgrounds.json` file:

    [
        {
            "name" : "draw",
            "file" : "menu_draw_large.bmp",
            "ll": { "x": 52, "y": 786 },
            "ur": { "x": 971, "y": 119 }
        },
        {
            "name" : "draw 9845",
            "file" : "draw_9845_large.png",
            "ll": { "x": 0, "y": 721 },
            "ur": { "x": 919, "y": 54 }
        },
        {
            "name" : "editor",
            "file" : "menu_editor_large.bmp",
            "ll": { "x": 52, "y": 786 },
            "ur": { "x": 971, "y": 119 }
        }
    ]

### HP85

Add these options when invoking MAME:

`-slot1 82937_hpib -slot1:82937_hpib:ieee_rem remote488 -bitb1 socket.localhost:1234`

### HP86B

Add these options when invoking MAME:

`-slot1:hpib:ieee_rem remote488 -bitb socket.localhost:1234`

### HP9845B/C/T

Add these options when invoking MAME:

`-slot0 98034_hpib -slot0:98034_hpib:ieee_rem remote488 -bitb1 socket.localhost:1234`

## Acknowledgements

I'd like to thank A. Kueckes for his support, for letting me peek at the source code for his version of 9111 emulator (see [DPS](https://hp9845.net/9845/projects/9111/)) and, last but certainly not least, for letting me use some of his icons and background images.

## Change history

+ 1.0: first release

+ 1.1: ported to Qt6
