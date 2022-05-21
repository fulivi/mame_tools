# -*- coding: utf-8 -*-

from PyQt5 import QtCore, QtGui, QtWidgets
from pendialog import Ui_PenDialog

class Pen_dialog(QtWidgets.QDialog):
    def __init__(self , parent , pen_settings , defaults):
        QtWidgets.QDialog.__init__(self , parent)
        self.ui = Ui_PenDialog()
        self.ui.setupUi(self)
        self.color_pbs = {}
        for idx in range(0 , 8):
            self.color_pbs[ self.get_color_pb(idx) ] = idx
        self.set_pen_settings(pen_settings)
        self.defaults = defaults

    def get_color_pb(self , pen):
        return self.ui.__dict__[ f"color{pen + 1}" ]

    def get_size_spin(self , pen):
        return self.ui.__dict__[ f"size{pen + 1}" ]

    def set_pen_settings(self , pen_settings):
        self.colors = [s[ 0 ] for s in pen_settings]
        for idx in range(0 , 8):
            self.set_color(idx)
            self.get_size_spin(idx).setValue(pen_settings[ idx ][ 1 ])

    def get_pen_settings(self):
        s = [ (self.colors[ idx ] , self.get_size_spin(idx).value()) for idx in range(0 , 8) ]
        return s

    def set_color(self , idx):
        pb = self.get_color_pb(idx)
        color = self.colors[ idx ]
        pb.setStyleSheet(f"background-color: rgb({color.red()},{color.green()},{color.blue()})")

    def editColor(self):
        sender = self.sender()
        pen = self.color_pbs.get(sender , None)
        new_color = QtWidgets.QColorDialog.getColor(self.colors[ pen ] , self , f"Choose color for pen {pen + 1}")
        if new_color.isValid():
            self.colors[ pen ] = new_color
            self.set_color(pen)

    def setDefaults(self):
        self.set_pen_settings(self.defaults)
