#!/usr/bin/env python3
# A tool to read/list the content of HP IPC filesystem images
# Copyright (C) 2018 F. Ulivi <fulivi at big "G" mail>
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
import argparse
import struct
import itertools
import time
import os
import os.path
import copy

SECTOR_SIZE=256
SEC_HEADER=0
BLOCK_SIZE=1024
SECTORS_IN_BLOCK=BLOCK_SIZE//SECTOR_SIZE
ROOT_INODE=2
PATH_SEP="/"
ROOT_DIR="/"

class MyException(Exception):
    pass

class ReadFailureSec(MyException):
    def __init__(self, sec):
        self.sec = sec

    def __str__(self ):
        return "Can't read sector {}".format(self.sec)

class ReadFailureBlk(MyException):
    def __init__(self, blk):
        self.blk = blk

    def __str__(self ):
        return "Can't read block {}".format(self.blk)

class FormatError(MyException):
    def __init__(self, msg):
        self.msg = msg

    def __str__(self ):
        return self.msg

class NotDirectory(MyException):
    def __init__(self ):
        pass

    def __str__(self ):
        return "Not a directory"

class WrongInode(MyException):
    def __init__(self, inode):
        self.inode = inode

    def __str__(self ):
        return "Inode {} doesn't exist".format(self.inode)

class NotFound(MyException):
    def __init__(self, filename, path):
        self.filename = filename
        self.path = path

    def __str__(self ):
        return "File/dir {} (in path {}) doesn't exist".format(self.filename , self.path)

def convert_str(b):
    idx = b.find(0)
    if idx >= 0:
        b = b[ :idx ]
    return b.decode(encoding = "ascii")

def format_perms(mask):
    return "{}{}{}".format('r' if (mask & 4) != 0 else '-' ,
                           'w' if (mask & 2) != 0 else '-' ,
                           'x' if (mask & 1) != 0 else '-')

def format_time(tm):
    return time.strftime('%Y-%m-%d %H:%M:%S' , time.gmtime(tm))

class Directory:
    def __init__(self ):
        self.dicti = {}
        self.entries = []

FILETYPES = {
    0x8000 : '-',
    0x4000 : 'd',
    0x2000 : 'c',
    0x6000 : 'b',
    0x1000 : 'p'
}

class INode:
    def __init__(self, byte_repr , mnt_image):
        self.mnt_image = mnt_image
        tmp = struct.unpack(">HHHHL40slll" , byte_repr)
        self.di_mode = tmp[ 0 ]
        self.di_nlink = tmp[ 1 ]
        self.di_uid = tmp[ 2 ]
        self.di_gid = tmp[ 3 ]
        self.di_size = tmp[ 4 ]
        self.di_atime = tmp[ 6 ]
        self.di_mtime = tmp[ 7 ]
        self.di_ctime = tmp[ 8 ]
        self.block_list = self.decode_block_list(tmp[ 5 ])

    def decode_block_list(self, byte_repr):
        l = []
        for i in range(13):
            s = byte_repr[ (i*3):(i*3+3) ]
            blk = (s[ 0 ] << 16) | (s[ 1 ] << 8) | s[ 2 ]
            l.append(blk)
        l2 = []
        for i , b in enumerate(l):
            if b == 0:
                break
            if i < 10:
                l2.append(b)
            else:
                l2.extend(self.indirect_blk_list(b , i - 10))
        return l2

    def indirect_blk_list(self, blk, level):
        if level < 0:
            return [ blk ]
        else:
            b = self.mnt_image.read_block(blk)
            l = []
            for e in itertools.takewhile(lambda x: x != 0 , map(lambda x: x[ 0 ] , struct.iter_unpack(">L" , b))):
                l.extend(self.indirect_blk_list(e , level - 1))
            return l

    def get_data(self ):
        accum = bytes()
        for b in self.block_list:
            blk = self.mnt_image.read_block(b)
            accum += blk
        return accum[ :self.di_size ]

    def get_file_type(self ):
        tp = self.di_mode & 0xf000
        if tp in FILETYPES:
            return FILETYPES[ tp ]
        else:
            return '?'

    def get_directory(self ):
        if self.get_file_type() != 'd':
            raise NotDirectory()
        d = Directory()
        dd = self.get_data()
        for i in range(0 , len(dd) , 16):
            inode , filename = struct.unpack(">H14s" , dd[ i:i+16 ])
            if inode != 0:
                s = convert_str(filename)
                d.entries.append((inode , s))
                if s != '.' and s != '..':
                    d.dicti[ s ] = inode
        return d

class MountedImage:
    def __init__(self, image):
        self.image = image
        hdr = self.read_sector(SEC_HEADER)
        hdr_u = struct.unpack(">H6sLHHLHH" , hdr[ :24 ])
        self.hdr_mlfi = hdr_u[ 0 ]
        self.hdr_vollbl = hdr_u[ 1 ]
        self.hdr_l_dstart = hdr_u[ 2 ]
        self.hdr_hack_3000 = hdr_u[ 3 ]
        self.hdr_l_dirlen = hdr_u[ 5 ]
        self.hdr_version = hdr_u[ 6 ]
        s = self.read_sector(self.hdr_l_dstart)
        name , file_type , superblk = struct.unpack(">10sHL" , s[ 0:16 ])
        if file_type != 0xe942:
            raise FormatError("Wrong type of superblock file ({:x})".format(file_type))
        s = self.read_sector(superblk)
        inode_end_sec = struct.unpack(">L" , s[ :4 ])[ 0 ] * SECTORS_IN_BLOCK
        inode_sec = superblk + SECTORS_IN_BLOCK
        self.inodes = []
        for inode_sec in range(inode_sec , inode_end_sec):
            s = self.read_sector(inode_sec)
            for i in range(4):
                raw_inode = s[ (i*64):(i*64+64) ]
                inode = INode(raw_inode , self)
                self.inodes.append(inode)

    def read_sector(self, sec):
        self.image.seek(sec * SECTOR_SIZE)
        s = self.image.read(SECTOR_SIZE)
        if len(s) != SECTOR_SIZE:
            raise ReadFailureSec(sec)
        return s

    def read_block(self, blk):
        self.image.seek(blk * BLOCK_SIZE)
        b = self.image.read(BLOCK_SIZE)
        if len(b) != BLOCK_SIZE:
            raise ReadFailureBlk(blk)
        return b

    def get_inode(self, inode):
        if inode < 2 or inode > len(self.inodes):
            raise WrongInode(inode)
        return self.inodes[ inode - 1 ]

    def path_to_inode(self, path):
        inode = ROOT_INODE
        fn = path.strip(PATH_SEP)
        if fn:
            for dname in fn.split(PATH_SEP):
                d = self.get_inode(inode).get_directory()
                try:
                    inode = d.dicti[ dname ]
                except KeyError:
                    raise NotFound(dname , path)
        return self.get_inode(inode)

    def format_direntry(self, e):
        inode = self.get_inode(e[ 0 ])
        filetype = inode.get_file_type()
        uperms = format_perms(inode.di_mode >> 6)
        gperms = format_perms(inode.di_mode >> 3)
        operms = format_perms(inode.di_mode)

        return "{}{}{}{} {:3d} {:3d} {:6d} {} {}".format(filetype , uperms , gperms , operms ,
                                                         inode.di_uid , inode.di_gid , inode.di_size ,
                                                         format_time(inode.di_mtime) ,
                                                         e[ 1 ])

    def _ls(self, inode , accum_path , recursive):
        if recursive:
            print("{}:".format(accum_path))
        d = inode.get_directory()
        for e in d.entries:
            print(self.format_direntry(e))
        if recursive:
            print()
            for e in d.entries:
                dir_inode = self.get_inode(e[ 0 ])
                if dir_inode.get_file_type() == 'd' and e[ 1 ] != "." and e[ 1 ] != "..":
                    new_accum_path = copy.copy(accum_path)
                    if not new_accum_path.endswith(PATH_SEP):
                        new_accum_path += PATH_SEP
                    new_accum_path += e[ 1 ]
                    self._ls(dir_inode , new_accum_path , True)

    def ls_directory(self, path , recursive):
        if not path:
            path = ROOT_DIR
        inode = self.path_to_inode(path)
        self._ls(inode , path , recursive)

    def burst(self, path, dest):
        inode = self.path_to_inode(path)
        filetype = inode.get_file_type()
        if filetype == 'd':
            if path:
                newdir = os.path.join(dest , path)
            else:
                newdir = dest
            os.mkdir(newdir)
            d = inode.get_directory()
            for e in d.entries:
                dir_inode = self.get_inode(e[ 0 ])
                if dir_inode.get_file_type() != 'd' or (e[ 1 ] != "." and e[ 1 ] != ".."):
                    buf = os.path.join(path , e[ 1 ])
                    self.burst(buf , dest)
        elif filetype == '-':
            filename = os.path.join(dest , path)
            out = open(filename , "wb")
            data = inode.get_data()
            out.write(data)
            out.close()
            print("cp {} {}".format(path , filename))
        else:
            print("File {} skipped: type = {}".format(path , filetype))

def main():
    parser = argparse.ArgumentParser(description="Tool to inspect HP IPC filesystem images")
    parser.add_argument('-R' , '--recursive' , action='store_true' , help = "List directories recursively")
    parser.add_argument('cmd' , nargs=1 , help = "Command" , choices=["ls" , "cat" , "burst"])
    parser.add_argument('img_file' , nargs=1 , type = argparse.FileType('rb') , help = "Image file")
    parser.add_argument('path' , nargs='?' , help = "Path (required for cat & burst, optional for ls)")
    args = parser.parse_args()
    cmd = args.cmd[ 0 ]

    try:
        mi = MountedImage(args.img_file[ 0 ])
        if cmd == "ls":
            mi.ls_directory(args.path , args.recursive)
        elif cmd == 'cat':
            if not args.path:
                print("path missing")
                parser.print_usage()
            else:
                inode = mi.path_to_inode(args.path)
                if inode.get_file_type() == "-":
                    data = inode.get_data()
                    sys.stdout.buffer.write(data)
                else:
                    print("{} is not a regular file".format(args.path))
        elif cmd == 'burst':
            if not args.path:
                print("path missing")
                parser.print_usage()
            else:
                mi.burst("" , args.path)
    except MyException as e:
        print(str(e))

if __name__ == '__main__':
    main()
