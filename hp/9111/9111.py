#!/usr/bin/env python3
# A HP9111 emulator for use with MAME IEEE-488 remotizer
# Copyright (C) 2020 F. Ulivi <fulivi at big "G" mail>
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
from PyQt5 import QtCore, QtGui, QtWidgets, QtMultimedia
import resources
import digitizer
import rem488
import math
import struct
import threading
import json

class Plate(QtWidgets.QWidget):
    # *****************
    # **** Signals ****
    # *****************
    #
    # Report pen position in DU
    # Params: x, y, pen pressed
    pen_position = QtCore.pyqtSignal(int , int , bool)
    # Report pen entering/leaving plate
    pen_proximity = QtCore.pyqtSignal(bool)

    def __init__(self , parent):
        QtWidgets.QWidget.__init__(self , parent)
        self.setMouseTracking(True)
        self.image = QtGui.QImage()
        self.inside = False
        self.current_bg = None

    def set_backgrounds(self , backgrounds):
        self.backgrounds = backgrounds
        self.menu = QtWidgets.QMenu(self)
        names = list(backgrounds.keys())
        names.sort()
        self.bg_action_names = {}
        self.bg_ag = QtWidgets.QActionGroup(self.menu)
        self.bg_ag.setExclusive(True)
        for n in names:
            a = QtWidgets.QAction(n , self.menu)
            self.bg_action_names[ a ] = n
            a.setCheckable(True)
            self.bg_ag.addAction(a)
            self.menu.addAction(a)
        self.bg_ag.triggered.connect(self.menu_sel)

    def set_calibration(self , ll , ur):
        self.ll = ll
        self.ur = ur
        self.set_transform()

    def set_transform(self):
        # Set self.xform to be a transform mapping from position in the widget
        # to position in digitizer units
        si = self.image.size()
        sp = self.size()
        su_x = 12032.0 / (self.ur.x() - self.ll.x())
        su_y = 8740.0 / (self.ll.y() - self.ur.y())
        m11 = su_x * float(si.width()) / float(sp.width())
        m21 = 0
        dx = -self.ll.x() * su_x
        m22 = -su_y * float(si.height()) / float(sp.height())
        m12 = 0
        dy = self.ll.y() * su_y
        self.xform = QtGui.QTransform(m11 , m12 , m21 , m22 , dx , dy)

    def resizeEvent(self , event):
        QtWidgets.QWidget.resizeEvent(self , event)
        if not self.image.isNull():
            self.set_transform()

    def point_to_du(self , p):
        return None if self.image.isNull() else self.xform.map(p)

    def is_in_platen(self , du):
        return du != None and (-124 <= du.x() <= 12156) and (-124 <= du.y() <= 9668)

    def paintEvent(self , event):
        painter = QtGui.QPainter(self)
        painter.drawImage(QtCore.QRect(0 , 0 , self.width() , self.height()) , self.image)

    def emit_pen_state(self , du , press):
        inside = self.is_in_platen(du)
        if inside and not self.inside:
            self.inside = True
            self.pen_proximity.emit(True)
        elif not inside and self.inside:
            self.inside = False
            self.pen_proximity.emit(False)
        if self.inside:
            self.pen_position.emit(du.x() , du.y() , press)

    def mousePressEvent(self , event):
        if event.button() == QtCore.Qt.LeftButton:
            du = self.point_to_du(event.pos())
            self.emit_pen_state(du , True)
            super().mousePressEvent(event)
        elif event.button() == QtCore.Qt.RightButton:
            self.menu.popup(self.mapToGlobal(event.pos()))

    def mouseReleaseEvent(self , event):
        if event.button() == QtCore.Qt.LeftButton:
            du = self.point_to_du(event.pos())
            self.emit_pen_state(du , False)

    def enterEvent(self , event):
        du = self.point_to_du(event.pos())
        self.emit_pen_state(du , False)

    def leaveEvent(self , event):
        if self.inside:
            self.inside = False
            self.pen_proximity.emit(False)

    def mouseMoveEvent(self , event):
        du = self.point_to_du(event.pos())
        self.emit_pen_state(du , event.buttons() & QtCore.Qt.LeftButton)

    def sizeHint(self):
        return self.image.size()

    def load_background(self , name):
        if name in self.backgrounds:
            self.current_bg = name
            filename , ll , ur = self.backgrounds[ name ]
            if self.image.load(filename):
                self.set_calibration(QtCore.QPoint(ll[ 0 ] , ll[ 1 ]) , QtCore.QPoint(ur[ 0 ] , ur[ 1 ]))
                # set menu action corresponding to "name" checked
                for a , n in self.bg_action_names.items():
                    if n == name:
                        a.setChecked(True)
                        break
            else:
                QtWidgets.QMessageBox.critical(self , "Error" , "Can't load background from {}".format(filename))
            self.update()
        else:
            print(name , "not in backgrounds!")

    def get_current_background(self):
        return self.current_bg

    def menu_sel(self , action):
        name = self.bg_action_names.get(action)
        if name:
            self.load_background(name)
        else:
            print("? Action not in bg_action_names ?")

class FixedARLayout(QtWidgets.QLayout):
    def __init__(self , parent , fixed_ar):
        QtWidgets.QLayout.__init__(self , parent)
        self.fixed_ar = fixed_ar
        self.item_list = []

    def addItem(self , item):
        self.item_list.append(item)

    def itemAt(self , idx):
        return self.item_list[ idx ]

    def takeAt(self , idx):
        return self.item_list.pop(idx)

    def count(self):
        return len(self.item_list)

    def sizeHint(self):
        return self.item_list[ 0 ].sizeHint() if self.item_list else QtCore.QSize(400 , int(400 // self.fixed_ar))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self , w):
        return int(w / self.fixed_ar)

    def minimumSize(self):
        return QtCore.QSize(400 , int(400 // self.fixed_ar))

    def setGeometry(self , r):
        QtWidgets.QLayout.setGeometry(self , r)
        new_w = r.width()
        new_h = int(new_w // self.fixed_ar)
        if new_h > r.height():
            new_h = r.height()
            new_w = round(new_h * self.fixed_ar)
        pos_x = (r.width() - new_w) // 2
        pos_y = (r.height() - new_h) // 2
        new_r = QtCore.QRect(pos_x , pos_y , new_w , new_h)
        self.item_list[ 0 ].setGeometry(new_r)

class BGJsonError(Exception):
    def __init__(self , msg):
        self.msg = msg

    def __str__(self):
        return "BG JSON error: {}".format(self.msg)

# Each value in the "out" dict (key = name):
# [ 0 ] Filename
# [ 1 ] (x , y) of LL point
# [ 2 ] (x , y) of UR point
def parse_bg_json(inp , out):
    try:
        tutto = json.load(inp)
    except json.decoder.JSONDecodeError:
        raise BGJsonError("Can't parse")
    if not isinstance(tutto , list):
        raise BGJsonError("Top level value is not an array")
    for x in tutto:
        if not isinstance(x , dict):
            raise BGJsonError("An element of top-level array is not an object")
        name = x.get("name")
        if not name or not isinstance(name , str):
            raise BGJsonError("'name' missing/empty/of wrong type")
        filename = x.get("file")
        if not filename or not isinstance(filename , str):
            raise BGJsonError("'file' missing/empty/of wrong type")
        if not QtCore.QFileInfo(filename).isNativePath():
            raise BGJsonError("'{}' is a resource name".format(filename))
        ll = x.get("ll")
        if not ll or not isinstance(ll , dict):
            raise BGJsonError("'ll' missing/empty/of wrong type")
        ur = x.get("ur")
        if not ur or not isinstance(ur , dict):
            raise BGJsonError("'ur' missing/empty/of wrong type")
        ll_x = ll.get("x")
        if ll_x == None or not isinstance(ll_x , int):
            raise BGJsonError("'ll.x' missing/of wrong type")
        ll_y = ll.get("y")
        if ll_y == None or not isinstance(ll_y , int):
            raise BGJsonError("'ll.y' missing/of wrong type")
        ur_x = ur.get("x")
        if ur_x == None or not isinstance(ur_x , int):
            raise BGJsonError("'ur.x' missing/of wrong type")
        ur_y = ur.get("y")
        if ur_y == None or not isinstance(ur_y , int):
            raise BGJsonError("'ur.y' missing/of wrong type")
        if ll_x >= ur_x or ll_y <= ur_y:
            raise BGJsonError("Inconsistent ll/ur")
        if name in out:
            raise BGJsonError("Duplicated name ({})".format(name))
        out[ name ] = (filename , (ll_x , ll_y) , (ur_x , ur_y))

class MyMainWindow(QtWidgets.QMainWindow):
    # *****************
    # **** Signals ****
    # *****************
    #
    # Note ended
    note_ended = QtCore.pyqtSignal()

    def __init__(self):
        QtWidgets.QWidget.__init__(self , None)
        self.widget = QtWidgets.QWidget(self)
        self.plate = Plate(self.widget)
        self.layout = FixedARLayout(self.widget , 1.2)
        self.layout.addWidget(self.plate)
        self.widget.setLayout(self.layout)
        self.setCentralWidget(self.widget)
        self.statusbar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self.statusbar)
        self.label_digitize = QtWidgets.QLabel(self.statusbar)
        self.label_digitize.setText("DIGITIZE")
        self.green_led_on = QtGui.QPixmap(":/led-green-on.bmp")
        self.green_led_off = QtGui.QPixmap(":/led-green-off.bmp")
        self.yellow_led_on = QtGui.QPixmap(":/led-yellow-on.bmp")
        self.yellow_led_off = QtGui.QPixmap(":/led-yellow-off.bmp")
        self.led_digitize = QtWidgets.QLabel(self.statusbar)
        self.led_digitize.setPixmap(self.green_led_off)
        self.label_menu = QtWidgets.QLabel(self.statusbar)
        self.label_menu.setText("MENU")
        self.led_menu = QtWidgets.QLabel(self.statusbar)
        self.led_menu.setPixmap(self.yellow_led_off)
        self.label_error = QtWidgets.QLabel(self.statusbar)
        self.label_error.setText("ERROR")
        self.led_error = QtWidgets.QLabel(self.statusbar)
        self.led_error.setPixmap(self.yellow_led_off)
        self.statusbar.addPermanentWidget(self.label_digitize)
        self.statusbar.addPermanentWidget(self.led_digitize)
        self.statusbar.addPermanentWidget(self.label_menu)
        self.statusbar.addPermanentWidget(self.led_menu)
        self.statusbar.addPermanentWidget(self.label_error)
        self.statusbar.addPermanentWidget(self.led_error)
        self.conn_msg = ""
        self.adi = QtMultimedia.QAudioDeviceInfo.defaultOutputDevice()
        self.tmr = QtCore.QTimer(self)
        self.tmr.timeout.connect(self.push_audio)
        af = QtMultimedia.QAudioFormat()
        af.setByteOrder(QtMultimedia.QAudioFormat.LittleEndian)
        af.setChannelCount(1)
        af.setCodec("audio/pcm")
        af.setSampleRate(11025)
        af.setSampleSize(16)
        af.setSampleType(QtMultimedia.QAudioFormat.SignedInt)
        self.audio_lock = threading.RLock()
        self.audio_running = False
        self.out = QtMultimedia.QAudioOutput(self.adi , af , self)
        self.out.stateChanged.connect(self.stateChanged)
        # 40 ms of audio buffer
        self.out.setBufferSize(round(11025.0 * 0.04 * 2))
        self.audio_io = self.out.start()

    def closeEvent(self , event):
        self.save_settings()
        event.accept()

    def update_statusbar(self):
        self.statusbar.showMessage(self.conn_msg)

    def stateChanged(self , s):
        with self.audio_lock:
            if s == QtMultimedia.QAudio.IdleState and self.audio_running and self.samples == 0:
                self.audio_running = False
                self.tmr.stop()

    def push_audio(self):
        if self.out == QtMultimedia.QAudio.StoppedState:
            return
        if not self.audio_running:
            return

        chunks = self.out.bytesFree() // self.out.periodSize()
        if not chunks:
            return
        if self.samples > 0:
            b = bytearray()
            for i in range(chunks):
                chunk_samples = self.out.periodSize() // 2
                while chunk_samples > 0:
                    with self.audio_lock:
                        if self.samples:
                            n_samples = min(self.samples , chunk_samples)
                            b.extend(self.get_samples(n_samples))
                            self.samples -= n_samples
                            chunk_samples -= n_samples
                            if not self.samples:
                                self.note_ended.emit()
                        else:
                            # Pad rest of chunk with silence
                            b.extend(bytes(chunk_samples * 2))
                            chunk_samples = 0
            self.audio_io.write(b)

    # SLOT
    def conn_status(self , status , msg):
        if status == rem488.CONNECTION_OK:
            self.conn_msg = "Connected {}".format(msg)
        elif status == rem488.CONNECTION_CLOSED:
            self.conn_msg = "Disconnected"
        else:
            self.conn_msg = "Connection failure"
        self.update_statusbar()

    # SLOT
    def led_state(self , led_digitize , led_menu , led_error):
        self.led_digitize.setPixmap(self.green_led_on if led_digitize else self.green_led_off)
        self.led_menu.setPixmap(self.yellow_led_on if led_menu else self.yellow_led_off)
        self.led_error.setPixmap(self.yellow_led_on if led_error else self.yellow_led_off)

    # Audio levels
    # 0         Muted
    # 1         -26.1 dB
    # 2         -14.1 dB
    # 3         -8.15 dB
    # 4         -2.07 dB
    # 5         0 dB
    AMPS = [ 0 , 1615 , 6421 , 12809 , 25815 , 32760 ]

    # SLOT
    def play_note(self , note , duration , amplitude):
        freq = (2 ** (note / 12.0)) * 130.81
        with self.audio_lock:
            self.two_om = 2.0 * math.pi * freq / 11025.0
            self.peak = self.AMPS[ amplitude ]
            self.samples = round(duration * 11.025)
            if not self.audio_running:
                self.audio_running = True
                self.ph = 0
                self.tmr.start(10)

    def get_samples(self , count):
        b = bytearray()
        for n in range(count):
            s = round(self.peak * math.sin(self.ph))
            b.extend(struct.pack("<h" , s))
            self.ph += self.two_om
            if self.ph >= 2.0 * math.pi:
                self.ph -= 2.0 * math.pi
        return b

    def load_backgrounds(self):
        self.backgrounds = { "default": ( ":/menu_standard_large.png" , (55 , 786) , (969 , 122)) }
        try:
            with open("backgrounds.json" , "rt") as inp:
                parse_bg_json(inp , self.backgrounds)
        except OSError as e:
            if not isinstance(e , FileNotFoundError):
                QtWidgets.QMessageBox.critical(self , "Error" , "Can't read BG file: {}".format(str(e)))
        except BGJsonError as e:
            QtWidgets.QMessageBox.critical(self , "Error" , str(e))
        self.plate.set_backgrounds(self.backgrounds)

    def load_settings(self):
        settings = QtCore.QSettings("9111")
        settings.beginGroup("MainWindow")
        geo = settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        settings.endGroup()
        settings.beginGroup("Background")
        bg = settings.value("name" , "default" , str)
        if bg:
            self.plate.load_background(bg)
        settings.endGroup()

    def save_settings(self):
        settings = QtCore.QSettings("9111")
        settings.beginGroup("MainWindow")
        settings.setValue("geometry" , self.saveGeometry())
        settings.endGroup()
        settings.beginGroup("Background")
        settings.setValue("name" , self.plate.get_current_background())
        settings.endGroup()

def get_tcp_port(app):
    parser = QtCore.QCommandLineParser()
    parser.addHelpOption()
    default_port = 1234
    port_opt = QtCore.QCommandLineOption([ "p" , "port" ] , "Set TCP port fo remote488." , "TCP port" , str(default_port))
    parser.addOption(port_opt)
    parser.process(app)
    try:
        port = int(parser.value(port_opt))
    except ValueError:
        print("Invalid port, using default")
        port = default_port
    if 1 <= port <= 65535:
        return port
    else:
        print("Invalid port ({}), using default".format(port))
        return default_port

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("HP9111 emulator")
    app.setApplicationVersion("1.0")
    port = get_tcp_port(app)
    digitizer = digitizer.DigitizerIO(port)
    myapp = MyMainWindow()
    myapp.plate.pen_position.connect(digitizer.pen_position)
    myapp.plate.pen_proximity.connect(digitizer.pen_proximity)
    myapp.note_ended.connect(digitizer.note_ended)
    digitizer.status_connect.connect(myapp.conn_status)
    digitizer.led_state.connect(myapp.led_state)
    digitizer.play_note.connect(myapp.play_note)
    digitizer.start()
    myapp.load_backgrounds()
    myapp.load_settings()
    myapp.show()
    sys.exit(app.exec_())
