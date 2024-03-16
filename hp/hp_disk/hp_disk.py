#!/usr/bin/env python3
# A GUI based emulator of HP Amigo drives for use with MAME IEEE-488 remotizer
# Copyright (C) 2020-2024 F. Ulivi <fulivi at big "G" mail>
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
import os.path
from PyQt6 import QtCore, QtGui, QtWidgets

from main import Ui_MainWindow

import hp_disk_protocol
import rem488

DEFAULT_PORT = 1234

class MyMainWindow(QtWidgets.QMainWindow):
    N_UNITS = 2

    def __init__(self , io):
        QtWidgets.QWidget.__init__(self , None)
        self.io = io
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)
        self.unit_filenames = [ "" ] * self.N_UNITS
        self.unit_file = self.instances_to_list("file")
        self.unit_load = self.instances_to_list("load")
        for n in range(self.N_UNITS):
            f = (lambda n: lambda : self.load_image(n))(n)
            self.unit_load[ n ].clicked.connect(f)
        self.unit_loaded = [ False ] * self.N_UNITS
        self.unit_readonly = self.instances_to_list("readonly")
        for n in range(self.N_UNITS):
            f = (lambda n: lambda state: self.set_read_only(n , state))(n)
            self.unit_readonly[ n ].clicked.connect(f)
        self.rd_counter = [ 0 ] * self.N_UNITS
        self.wr_counter = [ 0 ] * self.N_UNITS
        self.unit_read = self.instances_to_list("read")
        self.unit_write = self.instances_to_list("write")
        self.unit_lba = self.instances_to_list("lba")
        self.unit_cyl = self.instances_to_list("cyl")
        self.unit_head = self.instances_to_list("head")
        self.unit_sec = self.instances_to_list("sec")
        self.unit_capacity = self.instances_to_list("capacity")
        self.unit_geometry = self.instances_to_list("geometry")
        self.unit_bps = self.instances_to_list("bps")
        for model in hp_disk_protocol.DRIVE_MODELS:
            self.ui.drive_model.addItem(model.name)
        self.set_connected_state(False)
        self.io.status_connect.connect(self.conn_status)
        self.io.rd_counter.connect(self.inc_rd_counter)
        self.io.wr_counter.connect(self.inc_wr_counter)
        self.io.curr_pos.connect(self.set_current_pos)
        self.act_timers = []
        for n in range(self.N_UNITS):
            timer = QtCore.QTimer()
            f = (lambda n: lambda : self.act_timer_to(n))(n)
            timer.timeout.connect(f)
            timer.setSingleShot(True)
            self.act_timers.append(timer)
        for n in range(self.N_UNITS):
            self.clear_status(n)
        self.io.set_address(0)
        self.load_settings()

    def instances_to_list(self , suffix):
        l = [ self.ui.__dict__[ f"unit{n}_{suffix}" ] for n in range(self.N_UNITS) ]
        return l

    # SLOT
    def set_model(self , model_idx):
        model = hp_disk_protocol.DRIVE_MODELS[ model_idx ]
        self.ui.drive_protocol.setText(model.protocol)
        self.io.set_model(model_idx)
        for n in range(self.N_UNITS):
            self.clear_status(n)
            if n < len(model.unit_specs):
                self.ui.drives.setTabEnabled(n, True)
                geometry = self.io.drive.units[ n ].geometry
                g = geometry.max_chs
                bps = self.io.drive.units[ n ].bps
                size = geometry.max_lba * bps
                self.unit_geometry[ n ].setText(f"{g[ 0 ]}x{g[ 1 ]}x{g[ 2 ]}")
                self.unit_capacity[ n ].setText(f"{(size + 1023) // 1024} k")
                self.unit_bps[ n ].setText(f"{bps}")
            else:
                self.ui.drives.setTabEnabled(n, False)

    # SLOT
    def set_address(self , addr):
        if not self.connected:
            self.io.set_address(addr)

    # SLOT
    def conn_status(self , status , msg):
        if status == rem488.CONNECTION_OK:
            self.ui.statusbar.showMessage(f"Connected {msg}")
            self.set_connected_state(True)
        elif status == rem488.CONNECTION_CLOSED:
            self.ui.statusbar.showMessage("Disconnected")
            self.set_connected_state(False)
        else:
            self.ui.statusbar.showMessage("Connection failure")
            self.set_connected_state(False)

    def load_file(self , unit , filename):
        status = self.io.load_image(unit , filename)
        if status == 0:
            self.unit_filenames[ unit ] = filename
            self.unit_file[ unit ].setText(os.path.basename(filename))
            self.unit_load[ unit ].setText("Unload")
            self.unit_loaded[ unit ] = True
            self.unit_readonly[ unit ].setEnabled(False)
        else:
            self.clear_image_file(unit)
        return status

    def load_image(self , unit):
        if self.unit_loaded[ unit ]:
            self.io.load_image(unit , None)
            self.clear_image_file(unit)
        else:
            f = QtWidgets.QFileDialog.getOpenFileName(self , f"Select image file for unit {unit}")
            if f[ 0 ]:
                status = self.load_file(unit , f[ 0 ])
                if status != 0:
                    QtWidgets.QMessageBox.critical(self , "Error" , f"Can't open file {f[ 0 ]} (err={status})")
        self.clear_counters(unit)
        self.clear_pos(unit)

    def clear_status(self , unit):
        self.clear_image_file(unit)
        self.clear_counters(unit)
        self.clear_pos(unit)

    def clear_image_file(self , unit):
        self.unit_filenames[ unit ] = ""
        self.unit_file[ unit ].setText("")
        self.unit_load[ unit ].setText("Load")
        self.unit_loaded[ unit ] = False
        self.unit_readonly[ unit ].setEnabled(True)

    def set_read_only(self , unit , state):
        self.io.set_read_only(unit , state)

    def set_connected_state(self , connected):
        self.connected = connected
        self.ui.drive_model.setEnabled(not connected)
        self.ui.drive_addr.setEnabled(not connected)

    def clear_counters(self , unit):
        self.rd_counter[ unit ] = 0
        self.wr_counter[ unit ] = 0
        self.unit_read[ unit ].setNum(0)
        self.unit_write[ unit ].setNum(0)

    # SLOT
    def inc_rd_counter(self , unit , delta):
        if delta > 0:
            self.rd_counter[ unit ] += delta
            self.unit_read[ unit ].setNum(self.rd_counter[ unit ])
            self.ui.drives.tabBar().setTabTextColor(unit, QtCore.Qt.GlobalColor.green)
            self.act_timers[ unit ].start(100)

    # SLOT
    def inc_wr_counter(self , unit , delta):
        if delta > 0:
            self.wr_counter[ unit ] += delta
            self.unit_write[ unit ].setNum(self.wr_counter[ unit ])
            self.ui.drives.tabBar().setTabTextColor(unit, QtCore.Qt.GlobalColor.red)
            self.act_timers[ unit ].start(100)

    # SLOT
    def act_timer_to(self, unit):
        self.ui.drives.tabBar().setTabTextColor(unit, QtCore.Qt.GlobalColor.black)

    def clear_pos(self , unit):
        self.unit_lba[ unit ].setNum(0)
        self.unit_cyl[ unit ].setNum(0)
        self.unit_head[ unit ].setNum(0)
        self.unit_sec[ unit ].setNum(0)

    # SLOT
    def set_current_pos(self , unit , lba , cyl , head , sec):
        self.unit_lba[ unit ].setNum(lba)
        self.unit_cyl[ unit ].setNum(cyl)
        self.unit_head[ unit ].setNum(head)
        self.unit_sec[ unit ].setNum(sec)

    def load_settings(self):
        settings = QtCore.QSettings("hp_disk")
        settings.beginGroup("MainWindow")
        geo = settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        settings.endGroup()
        settings.beginGroup("Drive")
        x = settings.value("model" , 0 , int)
        self.ui.drive_model.setCurrentIndex(x)
        x = settings.value("address" , 0 , int)
        self.ui.drive_addr.setValue(x)
        settings.endGroup()
        n = min(settings.beginReadArray("Unit") , self.N_UNITS , self.io.get_unit_count())
        read_only = self.instances_to_list("readonly")
        for i in range(n):
            settings.setArrayIndex(i)
            ro = settings.value("readonly" , False , bool)
            read_only[ i ].setChecked(ro)
            self.set_read_only(i , ro)
            filename = settings.value("file")
            if filename:
                self.load_file(i , filename)
        settings.endArray()

    def save_settings(self):
        settings = QtCore.QSettings("hp_disk")
        settings.beginGroup("MainWindow")
        settings.setValue("geometry" , self.saveGeometry())
        settings.endGroup()
        settings.beginGroup("Drive")
        settings.setValue("model" , self.ui.drive_model.currentIndex())
        settings.setValue("address" , self.ui.drive_addr.value())
        settings.endGroup()
        settings.beginWriteArray("Unit" , self.N_UNITS)
        read_only = self.instances_to_list("readonly")
        read_only_values = [ ro.isChecked() for ro in read_only ]
        for i in range(self.N_UNITS):
            settings.setArrayIndex(i)
            settings.setValue("file" , self.unit_filenames[ i ])
            settings.setValue("readonly" , read_only_values[ i ])
        settings.endArray()

    def closeEvent(self , event):
        self.save_settings()
        event.accept()

def get_options(app):
    parser = QtCore.QCommandLineParser()
    parser.addHelpOption()
    port_opt = QtCore.QCommandLineOption([ "p" , "port" ] , "Set TCP port fo remote488." , "TCP port" , str(DEFAULT_PORT))
    parser.addOption(port_opt)
    parser.process(app)
    try:
        port = int(parser.value(port_opt))
    except ValueError:
        print("Invalid port, using default")
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        print("Invalid port ({}), using default".format(port))
        port = DEFAULT_PORT
    return port

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("HP disk emulator")
    app.setApplicationVersion("2.0")
    port = get_options(app)
    iot = hp_disk_protocol.IOThread(port)
    myapp = MyMainWindow(iot)
    iot.start()
    myapp.show()
    sys.exit(app.exec())
