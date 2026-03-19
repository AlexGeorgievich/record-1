import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import warnings
import wave
import tkinter as tk

import numpy as np


_ORIGINAL_NUMPY_FROMSTRING = np.fromstring


def _compat_fromstring(data, dtype=float, count=-1, sep="", like=None):
    if sep == "":
        try:
            return np.frombuffer(memoryview(data), dtype=dtype, count=count).copy()
        except TypeError:
            pass
    return _ORIGINAL_NUMPY_FROMSTRING(
        data, dtype=dtype, count=count, sep=sep, like=like
    )


np.fromstring = _compat_fromstring

import pystray
import soundcard as sc
from PIL import Image, ImageDraw
from soundcard.mediafoundation import SoundcardRuntimeWarning


SAMPLERATE = 48000
ACTUAL_SAMPLERATE = 48000
FORMAT_MP3 = True
TARGET_PEAK = 0.95
MAX_GAIN = 8.0

RECORDING = []
IS_RECORDING = False
LEVEL = 0.0

TRAY = None
OVERLAY = None
TRAY_THREAD = None
KEYBOARD = None
EXIT_EVENT = threading.Event()
OVERLAY_VISIBLE = True
TK_ROOT = None
HELP_REQUEST = threading.Event()

OVERLAY_POS_FILE = "overlay_pos.json"
HELP_TEXT = """System Audio Recorder

Commands:
  Ctrl+Alt+R  Start/stop recording
  Ctrl+Alt+O  Show/hide status overlay
  Ctrl+Alt+Q  Quit application

Tray menu:
  Start       Start recording
  Stop        Stop recording and save file
  MP3/WAV     Select output format
  9600-96000  Select sample rate
  Help        Show this help
  Exit        Quit application
"""


if sys.platform != "win32":
    sys.exit("This recorder only supports Windows.")

warnings.filterwarnings("ignore", category=SoundcardRuntimeWarning)


def configure_console_encoding():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except ValueError:
            pass


def sanitize_level(value):
    if not np.isfinite(value):
        return 0.0
    return float(max(value, 0.0))


configure_console_encoding()


def create_icon(color, level=0.0):
    img = Image.new("RGB", (64, 64), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    bar = int(min(max(sanitize_level(level) * 40, 2), 40))
    draw.rectangle((28, 52 - bar, 36, 52), fill=(0, 255, 0))
    return img


ICON_IDLE = create_icon((120, 120, 120))
ICON_REC = create_icon((220, 30, 30))


def update_tray_icon(level=None):
    if TRAY is None:
        return

    if level is None:
        TRAY.icon = ICON_REC if IS_RECORDING else ICON_IDLE
        return

    TRAY.icon = create_icon((220, 30, 30), level)


def center_window(window, width, height):
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    pos_x = int((screen_width - width) / 2)
    pos_y = int((screen_height - height) / 2)
    window.geometry(f"{width}x{height}+{pos_x}+{pos_y}")


def build_help_dialog(dialog, on_close=None):
    dialog.title("System Audio Recorder Help")
    dialog.attributes("-topmost", True)
    dialog.resizable(False, False)
    dialog.configure(bg="#f3f3f3")

    container = tk.Frame(dialog, bg="#f3f3f3", padx=18, pady=16)
    container.pack(fill="both", expand=True)

    title = tk.Label(
        container,
        text="System Audio Recorder",
        fg="#111111",
        bg="#f3f3f3",
        font=("Segoe UI", 13, "bold"),
    )
    title.pack(anchor="w", pady=(0, 8))

    label = tk.Label(
        container,
        text=HELP_TEXT,
        justify="left",
        anchor="w",
        fg="#202020",
        bg="#f3f3f3",
        font=("Segoe UI", 11),
    )
    label.pack(anchor="w")

    close_cmd = on_close if on_close is not None else dialog.destroy
    button = tk.Button(
        container,
        text="OK",
        width=12,
        command=close_cmd,
        bg="#e6e6e6",
        fg="#111111",
        activebackground="#d8d8d8",
        activeforeground="#111111",
    )
    button.pack(anchor="e", pady=(14, 0))

    dialog.protocol("WM_DELETE_WINDOW", close_cmd)
    dialog.update_idletasks()
    center_window(dialog, 460, 336)


def show_help(icon=None, item=None):
    if TK_ROOT is None or OVERLAY is None:
        temp_root = tk.Tk()
        temp_root.withdraw()
        dialog = tk.Toplevel(temp_root)
        build_help_dialog(dialog)
        dialog.grab_set()
        dialog.focus_force()
        temp_root.wait_window(dialog)
        temp_root.destroy()
        return

    HELP_REQUEST.set()


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def write_wav(path, audio_int16, sample_rate, channels):
    with wave.open(path, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_int16.tobytes())


def apply_output_gain(audio_np):
    peak = float(np.max(np.abs(audio_np))) if audio_np.size else 0.0
    if peak <= 0.0:
        return audio_np

    gain = min(TARGET_PEAK / peak, MAX_GAIN)
    if gain <= 1.0:
        return audio_np

    return np.clip(audio_np * gain, -1.0, 1.0)


def export_audio(base_path, audio_int16, sample_rate, channels):
    wav_path = base_path + ".wav"
    write_wav(wav_path, audio_int16, sample_rate, channels)

    if not FORMAT_MP3:
        return wav_path

    if sample_rate < 16000:
        return wav_path

    if not ffmpeg_available():
        return wav_path

    mp3_path = base_path + ".mp3"
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            wav_path,
            mp3_path,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return wav_path

    try:
        os.remove(wav_path)
    except OSError:
        pass

    return mp3_path


def record_loopback():
    global ACTUAL_SAMPLERATE, LEVEL, RECORDING

    speaker = sc.default_speaker()
    mic = sc.get_microphone(speaker.name, include_loopback=True)

    while not EXIT_EVENT.is_set():
        if not IS_RECORDING:
            time.sleep(0.05)
            continue

        ACTUAL_SAMPLERATE = SAMPLERATE
        RECORDING = []

        with mic.recorder(samplerate=ACTUAL_SAMPLERATE) as recorder:
            while IS_RECORDING and not EXIT_EVENT.is_set():
                data = recorder.record(numframes=2048)
                RECORDING.append(data.copy())
                if data.size == 0:
                    LEVEL = 0.0
                else:
                    rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64))))
                    LEVEL = sanitize_level(rms)
                update_tray_icon(LEVEL)


def start_recording(icon=None, item=None):
    global IS_RECORDING

    if IS_RECORDING:
        return

    IS_RECORDING = True
    update_tray_icon()


def stop_recording(icon=None, item=None):
    global IS_RECORDING, LEVEL

    if not IS_RECORDING or not RECORDING:
        IS_RECORDING = False
        LEVEL = 0.0
        update_tray_icon()
        return

    IS_RECORDING = False
    LEVEL = 0.0
    update_tray_icon()

    audio_np = np.concatenate(RECORDING, axis=0)
    audio_np = apply_output_gain(audio_np)
    audio_int16 = np.int16(np.clip(audio_np, -1.0, 1.0) * 32767)

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base_path = f"system_{timestamp}"
    channels = 1 if audio_np.ndim == 1 else audio_np.shape[1]
    export_audio(base_path, audio_int16, ACTUAL_SAMPLERATE, channels)


def toggle_recording():
    if IS_RECORDING:
        stop_recording()
    else:
        start_recording()


def quit_app(icon, item):
    global IS_RECORDING, LEVEL

    EXIT_EVENT.set()
    IS_RECORDING = False
    LEVEL = 0.0

    if KEYBOARD is not None:
        try:
            KEYBOARD.unhook_all_hotkeys()
        except Exception:
            pass

    if TK_ROOT is not None:
        try:
            if OVERLAY is not None:
                TK_ROOT.after(0, OVERLAY.close)
            TK_ROOT.after(0, TK_ROOT.quit)
        except Exception:
            pass

    if TRAY is not None:
        try:
            TRAY.visible = False
        except Exception:
            pass
        try:
            TRAY.stop()
        except Exception:
            pass


def hotkey_quit():
    quit_app(None, None)


def set_mp3(icon, item):
    global FORMAT_MP3

    if SAMPLERATE < 16000:
        return False

    FORMAT_MP3 = True
    return False


def set_wav(icon, item):
    global FORMAT_MP3
    FORMAT_MP3 = False
    return False


def set_rate(rate):
    global SAMPLERATE
    SAMPLERATE = rate
    return False


class OverlayWindow:
    def __init__(self, root):
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.88)
        self.window.configure(bg="#f2f2f2")

        self.status_var = tk.StringVar(value="STOP")
        self.time_var = tk.StringVar(value="00:00")
        self.help_window = None

        container = tk.Frame(
            self.window,
            bg="#f2f2f2",
            padx=10,
            pady=8,
            highlightthickness=2,
            highlightbackground="#b8b8b8",
            highlightcolor="#b8b8b8",
        )
        container.pack(fill="both", expand=True)

        top_row = tk.Frame(container, bg="#f2f2f2")
        top_row.pack(anchor="w", fill="x")

        self.indicator = tk.Canvas(
            top_row,
            width=24,
            height=24,
            bg="#f2f2f2",
            highlightthickness=0,
        )
        self.indicator.pack(side="left", padx=(0, 8))
        self.indicator_circle = self.indicator.create_oval(
            3, 3, 21, 21, fill="#4a4a4a", outline=""
        )

        self.label_status = tk.Label(
            top_row,
            textvariable=self.status_var,
            fg="#707070",
            bg="#f2f2f2",
            font=("Segoe UI", 12, "bold"),
        )
        self.label_status.pack(side="left")

        self.label_time = tk.Label(
            top_row,
            textvariable=self.time_var,
            fg="#111111",
            bg="#f2f2f2",
            font=("Consolas", 13, "bold"),
        )
        self.label_time.pack(side="left", padx=(10, 0))

        self.start_time = None
        self.drag_offset_x = 0
        self.drag_offset_y = 0

        self.window.bind("<ButtonPress-1>", self.on_press)
        self.window.bind("<B1-Motion>", self.on_drag)
        self.window.bind("<ButtonRelease-1>", self.on_release)

        self.load_position()
        self.update_ui()

    def close(self):
        self.save_position()
        self.window.destroy()

    def on_press(self, event):
        self.drag_offset_x = event.x
        self.drag_offset_y = event.y

    def on_drag(self, event):
        x = self.window.winfo_pointerx() - self.drag_offset_x
        y = self.window.winfo_pointery() - self.drag_offset_y
        self.window.geometry(f"+{x}+{y}")

    def on_release(self, event):
        self.save_position()

    def save_position(self):
        try:
            with open(OVERLAY_POS_FILE, "w", encoding="utf-8") as file:
                json.dump(
                    {"x": self.window.winfo_x(), "y": self.window.winfo_y()},
                    file,
                )
        except OSError:
            pass

    def load_position(self):
        try:
            if os.path.exists(OVERLAY_POS_FILE):
                with open(OVERLAY_POS_FILE, "r", encoding="utf-8") as file:
                    pos = json.load(file)
                self.window.geometry(f"+{pos['x']}+{pos['y']}")
                return
        except (OSError, KeyError, ValueError):
            pass

        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        self.window.geometry(f"+{screen_width - 180}+{screen_height - 110}")

    def show_help_dialog(self):
        if self.help_window is not None and self.help_window.winfo_exists():
            self.help_window.lift()
            self.help_window.focus_force()
            return

        self.help_window = tk.Toplevel(self.root)
        build_help_dialog(self.help_window, on_close=self.close_help_dialog)
        self.help_window.protocol("WM_DELETE_WINDOW", self.close_help_dialog)
        self.help_window.grab_set()
        self.help_window.focus_force()

    def close_help_dialog(self):
        if self.help_window is None:
            return
        try:
            self.help_window.grab_release()
        except Exception:
            pass
        self.help_window.destroy()
        self.help_window = None

    def update_ui(self):
        if EXIT_EVENT.is_set():
            return

        if HELP_REQUEST.is_set():
            HELP_REQUEST.clear()
            self.show_help_dialog()

        visible = bool(OVERLAY_VISIBLE)

        if visible:
            self.window.deiconify()
        else:
            self.window.withdraw()

        if IS_RECORDING:
            self.status_var.set("REC")
            self.label_status.configure(fg="#ff4d4d")
            blink_on = int(time.time() * 2) % 2 == 0
            self.indicator.itemconfig(
                self.indicator_circle,
                fill="#ff1744" if blink_on else "#c2183a",
            )
            if self.start_time is None:
                self.start_time = time.time()
            elapsed = int(time.time() - self.start_time)
            self.time_var.set(f"{elapsed // 60:02d}:{elapsed % 60:02d}")
        else:
            self.status_var.set("STOP")
            self.label_status.configure(fg="#707070")
            self.indicator.itemconfig(self.indicator_circle, fill="#4a4a4a")
            self.start_time = None
            self.time_var.set("00:00")
        self.root.after(100, self.update_ui)


def start_overlay():
    global OVERLAY, TK_ROOT

    TK_ROOT = tk.Tk()
    TK_ROOT.withdraw()
    OVERLAY = OverlayWindow(TK_ROOT)
    TK_ROOT.mainloop()


def toggle_overlay():
    global OVERLAY_VISIBLE
    OVERLAY_VISIBLE = not OVERLAY_VISIBLE


def run_tray():
    menu = pystray.Menu(
        pystray.MenuItem("Start", start_recording),
        pystray.MenuItem("Stop", stop_recording),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("MP3", set_mp3, checked=lambda item: FORMAT_MP3),
        pystray.MenuItem("WAV", set_wav, checked=lambda item: not FORMAT_MP3),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "9600 Hz",
            lambda icon, item: set_rate(9600),
            checked=lambda item: SAMPLERATE == 9600,
        ),
        pystray.MenuItem(
            "16000 Hz",
            lambda icon, item: set_rate(16000),
            checked=lambda item: SAMPLERATE == 16000,
        ),
        pystray.MenuItem(
            "24000 Hz",
            lambda icon, item: set_rate(24000),
            checked=lambda item: SAMPLERATE == 24000,
        ),
        pystray.MenuItem(
            "44100 Hz",
            lambda icon, item: set_rate(44100),
            checked=lambda item: SAMPLERATE == 44100,
        ),
        pystray.MenuItem(
            "48000 Hz",
            lambda icon, item: set_rate(48000),
            checked=lambda item: SAMPLERATE == 48000,
        ),
        pystray.MenuItem(
            "96000 Hz",
            lambda icon, item: set_rate(96000),
            checked=lambda item: SAMPLERATE == 96000,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Help", show_help),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", quit_app),
    )

    global TRAY
    TRAY = pystray.Icon("Recorder", ICON_IDLE, "System Audio Recorder", menu)
    TRAY.run()


def main():
    import keyboard

    global KEYBOARD
    KEYBOARD = keyboard

    if any(arg in ("-h", "--help", "help") for arg in sys.argv[1:]):
        show_help()
        return

    keyboard.add_hotkey("ctrl+alt+r", toggle_recording)
    keyboard.add_hotkey("ctrl+alt+o", toggle_overlay)
    keyboard.add_hotkey("ctrl+alt+q", hotkey_quit)

    audio_thread = threading.Thread(target=record_loopback, daemon=True)
    audio_thread.start()

    global TRAY_THREAD
    TRAY_THREAD = threading.Thread(target=run_tray, daemon=True)
    TRAY_THREAD.start()

    start_overlay()
    time.sleep(0.2)
    os._exit(0)


if __name__ == "__main__":
    main()
