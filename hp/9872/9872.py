#!/usr/bin/env python3
# A HP9872 emulator for use with MAME IEEE-488 remotizer
# Copyright (C) 2022 F. Ulivi <fulivi at big "G" mail>
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
from PyQt5 import QtCore, QtGui, QtWidgets , QtSvg
import plot9872
import rem488
import resources

from pen_dialog import Pen_dialog

DEFAULT_PORT = 1234
DEFAULT_ADDR = 5

class Platen(QtWidgets.QWidget):
    # *****************
    # **** Signals ****
    # *****************
    #
    # Set file for HPGL logging
    set_log_file = QtCore.pyqtSignal(QtCore.QFile)
    # Unset log file
    unset_log_file = QtCore.pyqtSignal()
    # Send HPGL playback data
    playback_data = QtCore.pyqtSignal(bytes)

    DEFAULT_PENS = [
        # 1
        (QtGui.QColor(QtCore.Qt.GlobalColor.black) , 20),
        # 2
        (QtGui.QColor(QtCore.Qt.GlobalColor.red) , 20),
        # 3
        (QtGui.QColor(QtCore.Qt.GlobalColor.green) , 20),
        # 4
        (QtGui.QColor(QtCore.Qt.GlobalColor.blue) , 20),
        # 5
        (QtGui.QColor(QtCore.Qt.GlobalColor.cyan) , 20),
        # 6
        (QtGui.QColor(QtCore.Qt.GlobalColor.magenta) , 20),
        # 7
        (QtGui.QColor(QtCore.Qt.GlobalColor.yellow) , 20),
        # 8
        (QtGui.QColor(QtCore.Qt.GlobalColor.lightGray) , 20)
    ]

    def __init__(self , parent , main_win):
        QtWidgets.QWidget.__init__(self , parent)
        self.main_win = main_win
        # Each element defines a segment as a tuple:
        # [0]   p1.x
        # [1]   p1.y
        # [2]   p2.x
        # [3]   p2.y
        # [4]   color
        # [5]   pen size
        self.segments = []
        self.xform = None
        self.menu = QtWidgets.QMenu(self)
        a1 = QtWidgets.QAction("Save.." , self.menu)
        self.menu.addAction(a1)
        a1.triggered.connect(self.menu_save)
        a2 = QtWidgets.QAction("Clear" , self.menu)
        self.menu.addAction(a2)
        a2.triggered.connect(self.menu_clear)
        self.log_action = QtWidgets.QAction("Log HPGL.." , self.menu)
        self.menu.addAction(self.log_action)
        self.log_action.triggered.connect(self.menu_log)
        self.log_action.setCheckable(True)
        self.log_file = None
        self.pbk_action = QtWidgets.QAction("HPGL playback.." , self.menu)
        self.menu.addAction(self.pbk_action)
        self.pbk_action.triggered.connect(self.menu_pbk)
        self.pen_action = QtWidgets.QAction("Set pens.." , self.menu)
        self.menu.addAction(self.pen_action)
        self.pen_action.triggered.connect(self.menu_pen)
        self.pens = self.DEFAULT_PENS

    def mousePressEvent(self , event):
        if event.button() == QtCore.Qt.RightButton:
            self.menu.popup(self.mapToGlobal(event.pos()))

    def menu_save(self , action):
        filename = QtWidgets.QFileDialog.getSaveFileName(self , "Save SVG" , "" , "SVG files (*.svg)")
        f = filename[ 0 ]
        if f:
            gen = QtSvg.QSvgGenerator()
            gen.setFileName(f)
            gen.setSize(QtCore.QSize(plot9872.MAX_X_PHY , plot9872.MAX_Y_PHY))
            gen.setViewBox(QtCore.QRect(0 , 0 , plot9872.MAX_X_PHY , plot9872.MAX_Y_PHY))
            # 40 points / mm
            gen.setResolution(1016)
            gen.setTitle("HP9872 simulator output")
            gen.setDescription("")
            painter = QtGui.QPainter()
            painter.begin(gen)
            # Flip image vertically (y grows upwards in plotter)
            xf = QtGui.QTransform(1 , 0 , 0 , -1 , 0 , plot9872.MAX_Y_PHY)
            painter.setTransform(xf)
            self.paint(painter)
            painter.end()
            print("Done!")

    def menu_clear(self , action):
        self.segments.clear()
        self.update()

    def menu_log(self , action):
        if self.log_file is None:
            filename = QtWidgets.QFileDialog.getSaveFileName(self , "Log HPGL" , "" , "Log files (*.log)")
            f = filename[ 0 ]
            if f:
                self.log_file = QtCore.QFile(f)
                if not self.log_file.open(QtCore.QIODevice.WriteOnly):
                    QtWidgets.QMessageBox.critical(self , "Error" , "Can't open log file")
                    self.log_file = None
                else:
                    self.set_log_file.emit(self.log_file)
        else:
            self.unset_log_file.emit()
            self.log_file.close()
            self.log_file = None
        self.log_action.setChecked(self.log_file is not None)
        self.pbk_action.setEnabled(self.log_file is None)

    def menu_pbk(self , action):
        filename = QtWidgets.QFileDialog.getOpenFileName(self , "HPGL playback" , "" , "Log files (*.log);;All files (*.*)")
        f = filename[ 0 ]
        if f:
            pbk_file = QtCore.QFile(f)
            if not pbk_file.open(QtCore.QIODevice.ReadOnly):
                QtWidgets.QMessageBox.critical(self , "Error" , "Can't open file")
            else:
                pbk_data = pbk_file.readAll().data()
                self.playback_data.emit(pbk_data)

    def menu_pen(self , action):
        dlg = Pen_dialog(self.main_win , self.pens , self.DEFAULT_PENS)
        rt = dlg.exec()
        if rt == QtWidgets.QDialog.Accepted:
            self.pens = dlg.get_pen_settings()

    # SLOT
    def add_segment(self , s):
        color , size = self.pens[ s.pen_no - 1 ]
        self.segments.append((s.p1.x , s.p1.y , s.p2.x , s.p2.y , color , size))
        self.update()

    def set_transform(self):
        sp = self.size()
        m11 = sp.width() / plot9872.MAX_X_PHY
        m12 = 0
        m21 = 0
        m22 = -sp.height() / plot9872.MAX_Y_PHY
        dx = 0
        dy = sp.height()
        self.xform = QtGui.QTransform(m11 , m12 , m21 , m22 , dx , dy)

    def resizeEvent(self , event):
        QtWidgets.QWidget.resizeEvent(self , event)
        self.set_transform()

    def paint(self , painter):
        pen = QtGui.QPen()
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        brush = QtGui.QBrush(QtCore.Qt.BrushStyle.SolidPattern)
        no_pen = QtGui.QPen(QtCore.Qt.PenStyle.NoPen)
        no_brush = QtGui.QBrush()
        for p1x , p1y , p2x , p2y , color , size in self.segments:
            if p1x == p2x and p1y == p2y:
                # Dot
                brush.setColor(color)
                painter.setBrush(brush)
                painter.setPen(no_pen)
                painter.drawEllipse(p1x - size // 2 , p1y - size // 2 , size , size)
            else:
                # Segment
                painter.setBrush(no_brush)
                pen.setColor(color)
                pen.setWidth(size)
                painter.setPen(pen)
                painter.drawLine(p1x , p1y , p2x , p2y)

    def paintEvent(self , event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        if self.xform:
            painter.setTransform(self.xform)
        pen = QtGui.QPen(QtCore.Qt.GlobalColor.gray)
        pen.setWidth(0)
        painter.setPen(pen)
        painter.drawRect(0 , 0 , plot9872.MAX_X_PHY , plot9872.MAX_Y_PHY)
        self.paint(painter)

    def load_settings(self , settings):
        settings.beginReadArray("pens")
        try:
            pens = []
            for idx in range(0 , 8):
                settings.setArrayIndex(idx)
                v = settings.value("color")
                if not isinstance(v , QtGui.QColor):
                    return
                color = v
                v = settings.value("size")
                if v is None:
                    return
                try:
                    size = int(v)
                except ValueError:
                    return
                if not 1 <= size <= 40:
                    return
                pens.append((color , size))
            self.pens = pens
        finally:
            settings.endArray()

    def save_settings(self , settings):
        settings.beginWriteArray("pens")
        for idx , pen in enumerate(self.pens):
            settings.setArrayIndex(idx)
            color , size = pen
            settings.setValue("color" , color)
            settings.setValue("size" , size)
        settings.endArray()

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

class My_io(QtCore.QThread):
    status_connect = QtCore.pyqtSignal(int , str)
    add_segment = QtCore.pyqtSignal(plot9872.Segment)
    error_led = QtCore.pyqtSignal(bool)
    ol_led = QtCore.pyqtSignal(int)

    def __init__(self):
        QtCore.QThread.__init__(self)
        self.plt = plot9872.Plotter(self)
        self.lock = QtCore.QMutex()
        self.log_file = None

    def set_talk_data(self , data):
        print("Talk:{}".format(data))

    # Set parallel poll state
    def set_pp_state(self , state):
        print("PP:{}".format(state))

    # Set RSV state
    def set_rsv_state(self , state):
        print("RSV:{}".format(state))

    # Set status byte
    def set_status_byte(self , b):
        print("SB:{:02x}".format(b))

    # Draw a segment
    def draw_segment(self , segment):
        print("Seg:{} Pen={}".format(str(segment) , segment.pen_no))
        self.add_segment.emit(segment)

    # Set ERROR LED state
    def set_error_led(self , state):
        self.error_led.emit(state)

    # Set "OUT OF LIMITS" LED state
    # state == 0    OFF
    # state == 1    ON
    # state == 2    Blinking
    def set_ol_led(self , state):
        self.ol_led.emit(state)

    def set_log_file(self , f):
        self.lock.lock()
        try:
            self.log_file = f
        finally:
            self.lock.unlock()

    def unset_log_file(self):
        self.lock.lock()
        try:
            self.log_file = None
        finally:
            self.lock.unlock()

    def log_hpgl(self , data):
        self.lock.lock()
        try:
            if self.log_file is not None:
                self.log_file.writeData(data)
        finally:
            self.lock.unlock()

    # SLOT
    def playback_data(self , data):
        self.plt.io_event(rem488.RemotizerData(None , data , False))

    def run(self):
        try:
            while True:
                i = input("? ")
                i_s = i.lstrip()
                if not i_s or not i_s[ 0 ].isalpha():
                    print("???")
                else:
                    c = i_s[ 0 ].upper()
                    if c == "C":
                        ev = rem488.RemotizerDevClear()
                    elif c == "D":
                        sn = bytearray()
                        state = 0
                        for idx in range(1 , len(i_s)):
                            ordc = ord(i_s[ idx ])
                            if state == 0:
                                if ordc == 24:
                                    state = 1
                                else:
                                    sn.append(ordc)
                            else:
                                if 0x41 <= ordc <= 0x5f:
                                    sn.append(ordc - 0x40)
                                state = 0
                        ev = rem488.RemotizerData(None , sn , False)
                        self.log_hpgl(sn)
                    elif c == "S":
                        ev = rem488.RemotizerSerialPoll()
                    elif c == "F":
                        try:
                            with open(i_s[ 1: ] , "rb") as inp:
                                data = inp.read()
                                ev = rem488.RemotizerData(None , data , False)
                                self.log_hpgl(sn)
                        except IOError:
                            print("I/O error")
                            continue
                    else:
                        print("???")
                        continue
                    self.plt.io_event(ev)
                    if c == "D":
                        ev = rem488.RemotizerTalk(None)
                        self.plt.io_event(ev)

        except EOFError:
            pass

class Rem488_io(QtCore.QThread):
    # *****************
    # **** Signals ****
    # *****************
    #
    # Report connection status
    # 1st parameter is one of rem488.CONNECTION_*
    status_connect = QtCore.pyqtSignal(int , str)
    # Add a segment to drawing
    add_segment = QtCore.pyqtSignal(plot9872.Segment)
    # ERROR LED state
    error_led = QtCore.pyqtSignal(bool)
    # "OUT OF LIMITS" LED state
    # 0 OFF
    # 1 ON
    # 2 Blinking
    ol_led = QtCore.pyqtSignal(int)

    def __init__(self , port , addr):
        QtCore.QThread.__init__(self)
        self.rem = rem488.RemotizerIO(port , False , True , False)
        self.rem.set_address(addr)
        if addr < 8:
            self.rem.set_pp_response(0x80 >> addr)
        self.plt = plot9872.Plotter(self)
        self.lock = QtCore.QMutex()
        self.log_file = None

    def set_talk_data(self , data):
        self.rem.talk_data(data)

    # Set parallel poll state
    def set_pp_state(self , state):
        self.rem.send_pp_state(state)

    # Set RSV state
    def set_rsv_state(self , state):
        self.rem.set_rsv_state(state)

    # Set status byte
    def set_status_byte(self , b):
        self.rem.set_status_byte(b)

    # Draw a segment
    def draw_segment(self , segment):
        print("Seg:{} Pen={}".format(str(segment) , segment.pen_no))
        self.add_segment.emit(segment)

    # Set ERROR LED state
    def set_error_led(self , state):
        self.error_led.emit(state)

    # Set "OUT OF LIMITS" LED state
    def set_ol_led(self , state):
        self.ol_led.emit(state)

    # SLOT
    def set_log_file(self , f):
        self.lock.lock()
        try:
            self.log_file = f
        finally:
            self.lock.unlock()

    # SLOT
    def unset_log_file(self):
        self.lock.lock()
        try:
            self.log_file = None
        finally:
            self.lock.unlock()

    def log_hpgl(self , data):
        self.lock.lock()
        try:
            if self.log_file is not None:
                self.log_file.writeData(data)
        finally:
            self.lock.unlock()

    # SLOT
    def playback_data(self , data):
        self.rem.force_data(data)

    def run(self):
        while True:
            ev = self.rem.get_event()
            if isinstance(ev , rem488.RemotizerConnection):
                self.status_connect.emit(ev.status , ev.msg)
            elif isinstance(ev , rem488.RemotizerCP):
                self.rem.send_checkpoint_reached()
            else:
                self.plt.io_event(ev)
                if isinstance(ev , rem488.RemotizerData):
                    self.log_hpgl(ev.data)

class Playback(QtCore.QThread):
    def __init__(self , rem , in_file):
        pass

    def run(self):
        pass

class MyMainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        QtWidgets.QWidget.__init__(self , None)
        self.widget = QtWidgets.QWidget(self)
        self.platen = Platen(self.widget , self)
        self.layout = FixedARLayout(self.widget , 1.4)
        self.layout.addWidget(self.platen)
        self.widget.setLayout(self.layout)
        self.setCentralWidget(self.widget)
        self.statusbar = QtWidgets.QStatusBar(self)
        self.setStatusBar(self.statusbar)
        self.yellow_led_on = QtGui.QPixmap(":/led-yellow-on.bmp")
        self.yellow_led_off = QtGui.QPixmap(":/led-yellow-off.bmp")
        self.label_ol = QtWidgets.QLabel(self.statusbar)
        self.label_ol.setText("OUT OF LIMIT")
        self.led_ol = QtWidgets.QLabel(self.statusbar)
        self.led_ol.setPixmap(self.yellow_led_off)
        self.led_ol_state = 0
        self.ol_timer = QtCore.QTimer(self)
        self.ol_timer.timeout.connect(self.ol_timer_timeout)
        self.label_error = QtWidgets.QLabel(self.statusbar)
        self.label_error.setText("ERROR")
        self.led_error = QtWidgets.QLabel(self.statusbar)
        self.led_error.setPixmap(self.yellow_led_off)
        self.statusbar.addPermanentWidget(self.label_ol)
        self.statusbar.addPermanentWidget(self.led_ol)
        self.statusbar.addPermanentWidget(self.label_error)
        self.statusbar.addPermanentWidget(self.led_error)
        self.conn_msg = ""

    # SLOT
    def conn_status(self , status , msg):
        if status == rem488.CONNECTION_OK:
            self.conn_msg = "Connected {}".format(msg)
        elif status == rem488.CONNECTION_CLOSED:
            self.conn_msg = "Disconnected"
        else:
            self.conn_msg = "Connection failure"
        self.statusbar.showMessage(self.conn_msg)

    # SLOT
    def error_led(self , state):
        self.led_error.setPixmap(self.yellow_led_on if state else self.yellow_led_off)

    # SLOT
    def ol_led(self , state):
        if self.led_ol_state != state:
            self.led_ol_state = state
            if state == 0:
                self.led_ol.setPixmap(self.yellow_led_off)
                self.ol_timer.stop()
            elif state == 1:
                self.led_ol.setPixmap(self.yellow_led_on)
                self.ol_timer.stop()
            elif state == 2:
                self.blink = True
                self.led_ol.setPixmap(self.yellow_led_on)
                self.ol_timer.start(500)

    # SLOT
    def ol_timer_timeout(self):
        self.blink = not self.blink
        self.led_ol.setPixmap(self.yellow_led_on if self.blink else self.yellow_led_off)

    def closeEvent(self , event):
        self.save_settings()
        event.accept()

    def load_settings(self):
        settings = QtCore.QSettings("9872")
        settings.beginGroup("MainWindow")
        geo = settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        settings.endGroup()
        self.platen.load_settings(settings)

    def save_settings(self):
        settings = QtCore.QSettings("9872")
        settings.beginGroup("MainWindow")
        settings.setValue("geometry" , self.saveGeometry())
        settings.endGroup()
        self.platen.save_settings(settings)

def get_options(app):
    parser = QtCore.QCommandLineParser()
    parser.addHelpOption()
    port_opt = QtCore.QCommandLineOption([ "p" , "port" ] , "Set TCP port fo remote488." , "TCP port" , str(DEFAULT_PORT))
    parser.addOption(port_opt)
    addr_opt = QtCore.QCommandLineOption([ "a" , "addr" ] , "Set HPIB address" , "address" , str(DEFAULT_ADDR))
    parser.addOption(addr_opt)
    parser.process(app)
    try:
        port = int(parser.value(port_opt))
    except ValueError:
        print("Invalid port, using default")
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        print("Invalid port ({}), using default".format(port))
        port = DEFAULT_PORT
    try:
        addr = int(parser.value(addr_opt))
    except ValueError:
        print("Invalid address, using default")
        addr = DEFAULT_ADDR
    if not 0 <= addr <= 30:
        print("Invalid address ({}), using default".format(addr))
        addr = DEFAULT_ADDR
    return port , addr

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("HP9872 emulator")
    app.setApplicationVersion("1.1")
    port , address = get_options(app)
    #my_io = My_io()
    my_io = Rem488_io(port , address)
    myapp = MyMainWindow()
    my_io.add_segment.connect(myapp.platen.add_segment)
    my_io.status_connect.connect(myapp.conn_status)
    my_io.error_led.connect(myapp.error_led)
    my_io.ol_led.connect(myapp.ol_led)
    myapp.platen.set_log_file.connect(my_io.set_log_file)
    myapp.platen.unset_log_file.connect(my_io.unset_log_file)
    myapp.platen.playback_data.connect(my_io.playback_data)
    my_io.start()
    myapp.load_settings()
    myapp.show()
    sys.exit(app.exec_())
