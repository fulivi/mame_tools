Tools for HP IPC emulation
==========================

## ipc_utils

This Python 3 tool is a re-implementation of [ipc_utils](http://www.coho.org/~pete/IPC/ipc_utils.html) by Peter Johnson. Unlike it, the Python version makes no assumption on word size or endianness of host machine.

The purpose of this tool is listing/reading the content of IPC filesystem images. It can handle the images of internal floppy drive or external disks as long as they are formatted in the IPC-standard HFS.

### Usage

Command line invocation of ipc_utils is as follows:

`ipc_utils.py [-R] ` _cmd_ _image file_ `[ ` _path_ ` ]`

#### "ls" command

This command, like its Unix namesake, lists the content of the filesystem as files & directories. The optional `-R` switch enables recursive descent into sub-directories. The optional _path_ argument can be used to list only a specific directory.

Example:

`ipc_utils.py -R ls image_file.img`

#### "cat" command

This command extracts the content of a file and dumps it to stdout. The _path_ argument is not optional. It must refer to a regular file.

Example:

`ipc_utils.py cat image_file.img README`

#### "burst" command

This command extracts the whole content of an image, replicating the tree structure. The _path_ argument is not optional. It's the name of the destination directory of the extracted tree. This directory must not exist when burst command is invoked.

Example:

`ipc_utils.py burst image_file.img output_dir`

