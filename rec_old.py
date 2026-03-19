# WINDOWS 11 SYSTEM AUDIO RECORDER (FULL + STABLE)
# tray + hotkeys + wav/mp3 + samplerate + Qt overlay
# Python 3.10+

import sys
import os
import time
import datetime
import threading
import warnings
import json


import numpy as np
import soundcard as sc
from pydub import AudioSegment
import pystray
from PIL import Image, ImageDraw
from soundcard.mediafoundation import SoundcardRuntimeWarning

from PySide6 import QtWidgets, QtCore

# ================== GLOBAL STATE ==================

samplerate = 48000          # selected in menu
actual_samplerate = 48000   # fixed per recording
format_mp3 = True           # False = WAV

recording = []
is_recording = False
level = 0.0

overlay = None

OVERLAY_POS_FILE = "overlay_pos.json"


# ================== SYSTEM ==================

if sys.platform != "win32":
    sys.exit(0)

warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)

# ================== TRAY ICON ==================

def create_icon(color, level=0.0):
    img = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.ellipse((8, 8, 56, 56), fill=color)

    bar = int(min(max(level * 40, 2), 40))
    draw.rectangle((28, 52 - bar, 36, 52), fill=(0, 255, 0))

    return img

icon_idle = create_icon((120, 120, 120))
icon_rec  = create_icon((220, 30, 30))

# ================== AUDIO THREAD ==================

def record_loopback():
    global recording, is_recording, level, actual_samplerate

    speaker = sc.default_speaker()
    mic = sc.get_microphone(speaker.name, include_loopback=True)

    while True:
        if not is_recording:
            time.sleep(0.05)
            continue

        actual_samplerate = samplerate
        recording = []

        with mic.recorder(samplerate=actual_samplerate) as recorder:
            while is_recording:
                data = recorder.record(numframes=2048)
                recording.append(data.copy())
                level = float(np.sqrt(np.mean(data ** 2)))
                tray.icon = create_icon((220, 30, 30), level)

# ================== CONTROLS ==================

def start_recording(icon=None, item=None):
    global is_recording
    is_recording = True
    tray.icon = icon_rec


def stop_recording(icon=None, item=None):
    global is_recording

    if not is_recording or not recording:
        return

    is_recording = False
    tray.icon = icon_idle

    audio_np = np.concatenate(recording, axis=0)
    audio_int16 = np.int16(audio_np * 32767)

    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = f"system_{ts}"

    audio = AudioSegment(
        audio_int16.tobytes(),
        frame_rate=actual_samplerate,
        sample_width=2,
        channels=audio_np.shape[1]
    )

    # MP3 policy
    if format_mp3 and actual_samplerate >= 16000:
        audio.export(base + ".mp3", format="mp3")
    else:
        audio.export(base + ".wav", format="wav")


def toggle_recording():
    if is_recording:
        stop_recording()
    else:
        start_recording()


def quit_app(icon, item):
    tray.stop()
    os._exit(0)

# ================== SETTINGS (MENU) ==================

def set_mp3(icon, item):
    global format_mp3
    if samplerate < 16000:
        return False
    format_mp3 = True
    return False


def set_wav(icon, item):
    global format_mp3
    format_mp3 = False
    return False


def set_rate(rate):
    global samplerate
    samplerate = rate
    return False

# ================== QT OVERLAY ==================

class Overlay(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            QtCore.Qt.WindowStaysOnTopHint |
            QtCore.Qt.FramelessWindowHint |
            QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)

        self.resize(220, 90)

        self.label_status = QtWidgets.QLabel("● STOP")
        self.label_status.setStyleSheet("color: gray; font-size: 18px;")

        self.level_bar = QtWidgets.QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(12)

        self.label_time = QtWidgets.QLabel("00:00")
        self.label_time.setStyleSheet("color: white; font-size: 14px;")

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.label_status)
        layout.addWidget(self.level_bar)
        layout.addWidget(self.label_time)
        layout.setContentsMargins(12, 10, 12, 10)

        self.start_time = None
        self.drag_pos = None

        self.load_position()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_ui)
        self.timer.start(100)

    # ---------- UI UPDATE ----------

    def update_ui(self):
        global is_recording, level

        if is_recording:
            self.label_status.setText("● REC")
            self.label_status.setStyleSheet("color: red; font-size: 18px;")

            if self.start_time is None:
                self.start_time = time.time()

            elapsed = int(time.time() - self.start_time)
            self.label_time.setText(f"{elapsed//60:02d}:{elapsed%60:02d}")
        else:
            self.label_status.setText("● STOP")
            self.label_status.setStyleSheet("color: gray; font-size: 18px;")
            self.start_time = None
            self.label_time.setText("00:00")

        self.level_bar.setValue(int(min(level * 100, 100)))

    # ---------- DRAG ----------

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_pos and event.buttons() & QtCore.Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        self.save_position()

    # ---------- POSITION SAVE / LOAD ----------

    def save_position(self):
        try:
            with open(OVERLAY_POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.x(), "y": self.y()}, f)
        except Exception:
            pass

    def load_position(self):
        try:
            if os.path.exists(OVERLAY_POS_FILE):
                with open(OVERLAY_POS_FILE, "r", encoding="utf-8") as f:
                    pos = json.load(f)
                    self.move(pos["x"], pos["y"])
            else:
                screen = QtWidgets.QApplication.primaryScreen().geometry()
                self.move(
                    screen.width() - self.width() - 20,
                    screen.height() - self.height() - 60
                )
        except Exception:
            pass


def start_overlay():
    global overlay
    app = QtWidgets.QApplication([])
    overlay = Overlay()
    overlay.show()
    app.exec()

def toggle_overlay():
    if overlay:
        overlay.setVisible(not overlay.isVisible())

# ================== MAIN ==================

def main():
    import keyboard

    keyboard.add_hotkey("ctrl+alt+r", toggle_recording)
    keyboard.add_hotkey("ctrl+alt+o", toggle_overlay)

    audio_thread = threading.Thread(target=record_loopback, daemon=True)
    audio_thread.start()

    overlay_thread = threading.Thread(target=start_overlay, daemon=True)
    overlay_thread.start()

    menu = pystray.Menu(
        pystray.MenuItem("▶ Start", start_recording),
        pystray.MenuItem("■ Stop", stop_recording),
        pystray.Menu.SEPARATOR,

        pystray.MenuItem("MP3", set_mp3, checked=lambda i: format_mp3),
        pystray.MenuItem("WAV", set_wav, checked=lambda i: not format_mp3),
        pystray.Menu.SEPARATOR,

        pystray.MenuItem("9600 Hz",  lambda i,j: set_rate(9600),  checked=lambda i: samplerate == 9600),
        pystray.MenuItem("16000 Hz", lambda i,j: set_rate(16000), checked=lambda i: samplerate == 16000),
        pystray.MenuItem("24000 Hz", lambda i,j: set_rate(24000), checked=lambda i: samplerate == 24000),
        pystray.MenuItem("44100 Hz", lambda i,j: set_rate(44100), checked=lambda i: samplerate == 44100),
        pystray.MenuItem("48000 Hz", lambda i,j: set_rate(48000), checked=lambda i: samplerate == 48000),
        pystray.MenuItem("96000 Hz", lambda i,j: set_rate(96000), checked=lambda i: samplerate == 96000),

        pystray.Menu.SEPARATOR,
        pystray.MenuItem("✖ Exit", quit_app)
    )

    global tray
    tray = pystray.Icon("Recorder", icon_idle, "System Audio Recorder", menu)
    tray.run()

if __name__ == "__main__":
    main()
