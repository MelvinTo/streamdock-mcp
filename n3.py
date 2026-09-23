#!/usr/bin/env python3
"""n3 - command-line control for the Mirabox Stream Dock N3 (no StreamDock app needed).

Device: HOTSPOTEKUSB "HID DEMO", USB 0x6603:0x1002, firmware V3.293N3_PXL.02.010.
Talks straight to the vendor HID interface (usage page 0xFFA0) via hidapi.

Wire protocol (recovered from Mirabox's libtransport and verified on the device):
  output report : 1024 bytes  (hidapi write of 1025 bytes, leading 0x00 report id)
  input  report : 512 bytes   "ACK\\0\\0OK\\0\\0\\0" + code@9 + state@10   (key/knob events)
  GET_REPORT(0) : returns the firmware version string

  commands (zero padded to 1024 bytes):
    CRT\\0\\0LIG\\0\\0 <pct>               key/screen brightness 0-100      (byte 10)
    CRT\\0\\0CLE\\0\\0\\0\\0 <key|0xFF>      clear one key image / all        (byte 11)
    CRT\\0\\0STP                         refresh: show everything queued
    CRT\\0\\0DIS                         wake screen
    CRT\\0\\0HAN                         sleep screen
    CRT\\0\\0CONNECT                     heartbeat (vendor sends every 10 s)
    CRT\\0\\0CLE\\0\\0DC                   "software disconnected" notice
    CRT\\0\\0BAT <u32be size> <key>      key JPEG follows in 1024-byte chunks
    CRT\\0\\0LOG <u32be size> 0x01       screen JPEG follows in 1024-byte chunks

Layout (from the vendor SDK): keys 1-6 have 64x64 LCDs (hardware codes 1-6),
keys 7-9 are plain buttons (0x25, 0x30, 0x31), three knobs (press 0x33/0x34/0x35,
rotate 0x90/0x91, 0x60/0x61, 0x50/0x51), and a 320x240 screen.  Images are
sent rotated 90 degrees clockwise, exactly like the vendor SDK does.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time

from typing import Iterable, Optional

try:
    import hid  # pip install hidapi
except ImportError:  # pragma: no cover
    sys.stderr.write("missing dependency: pip3 install --user hidapi pillow\n")
    raise

VID, PID = 0x6603, 0x1002
REPORT_OUT = 1024
REPORT_IN = 512

KEY_SIZE = (64, 64)        # per-key LCD, JPEG
SCREEN_SIZE = (320, 240)   # side screen, JPEG
IMAGE_ROTATION = -90       # confirmed on device: tiles must be pre-rotated 90° clockwise (upright input shows rotated CCW)

# hardware code -> logical key number
KEY_CODES = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 0x25: 7, 0x30: 8, 0x31: 9}
IMAGE_KEYS = range(1, 7)   # only these keys have a display
KNOB_PRESS = {0x33: 1, 0x34: 2, 0x35: 3}
KNOB_ROTATE = {0x90: (1, "left"), 0x91: (1, "right"),
               0x60: (2, "left"), 0x61: (2, "right"),
               0x50: (3, "left"), 0x51: (3, "right")}
KNOB_POSITION = {1: "bottom-left", 2: "bottom-right", 3: "top"}

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


# --------------------------------------------------------------------------- transport
class N3Error(RuntimeError):
    pass


class N3IOError(N3Error):
    """Transport-level failure (device gone, write failed); safe to reopen and retry."""


def enumerate_devices() -> list[dict]:
    """All attached N3 vendor interfaces (one per device)."""
    seen, out = set(), []
    for d in hid.enumerate(VID, PID):
        if d.get("interface_number") != 0:
            continue
        if d["path"] in seen:
            continue
        seen.add(d["path"])
        out.append(d)
    return out


def _open_non_exclusive():
    """Match the StreamDock app: open the HID device without seizing it (macOS only)."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        lib = ctypes.CDLL(hid.__file__)
        lib.hid_darwin_set_open_exclusive(ctypes.c_int(0))
    except Exception:
        pass


class N3:
    """One open Stream Dock N3."""

    def __init__(self, serial: Optional[str] = None, heartbeat: bool = False):
        devs = enumerate_devices()
        if serial:
            devs = [d for d in devs if d.get("serial_number") == serial]
        if not devs:
            raise N3IOError("no Stream Dock N3 found" + (f" with serial {serial}" if serial else "")
                          + " (is the StreamDock app still holding it? quit it first)")
        self.info = devs[0]
        self.serial = self.info.get("serial_number", "")
        self._h = hid.device()
        _open_non_exclusive()
        try:
            self._h.open_path(self.info["path"])
        except OSError as e:
            raise N3IOError(f"cannot open device: {e}. Quit the StreamDock app if it is running.") from e
        self._h.set_nonblocking(1)
        self._lock = threading.Lock()
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        if heartbeat:
            self.start_heartbeat()

    # -- lifecycle
    def close(self, notify_disconnect: bool = False):
        self.stop_heartbeat()
        if notify_disconnect:
            try:
                self.disconnect_notice()
            except Exception:
                pass
        try:
            self._h.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- raw
    def write(self, payload: bytes):
        if len(payload) > REPORT_OUT:
            raise ValueError("payload longer than one report")
        pkt = bytearray(1 + REPORT_OUT)
        pkt[1:1 + len(payload)] = payload
        with self._lock:
            n = self._h.write(bytes(pkt))
        if n < 0:
            raise N3IOError(f"hid write failed: {self._h.error()}")
        return n

    def cmd(self, tag: bytes, tail: bytes = b""):
        return self.write(b"CRT\x00\x00" + tag + tail)

    def read(self, timeout_ms: int = 0) -> Optional[bytes]:
        with self._lock:
            data = self._h.read(REPORT_IN, timeout_ms) if timeout_ms else self._h.read(REPORT_IN)
        return bytes(data) if data else None

    # -- simple commands
    def firmware_version(self) -> str:
        with self._lock:
            r = bytes(self._h.get_input_report(0, 64))
        return r.lstrip(b"\x00").split(b"\x00", 1)[0].decode("ascii", "replace")

    def brightness(self, pct: int):
        pct = max(0, min(100, int(pct)))
        self.cmd(b"LIG", b"\x00\x00" + bytes([pct]))

    def wake(self):
        self.cmd(b"DIS")

    def sleep(self):
        self.cmd(b"HAN")

    def refresh(self):
        self.cmd(b"STP")

    def heartbeat(self):
        self.cmd(b"CONNECT")

    def disconnect_notice(self):
        self.cmd(b"CLE", b"\x00\x00DC")

    def device_config(self, cfg: bytes = b"\x1f\x11\x00\x11\x00\x11\x00"):
        """QUCMD: device configuration. The StreamDock app sends exactly this on connect."""
        self.cmd(b"QUCMD", cfg)

    def connect(self, brightness: Optional[int] = None):
        """Replay the app's connect handshake: wake, brightness, QUCMD, brightness again."""
        self.wake()
        if brightness is not None:
            self.brightness(brightness)
        self.device_config()
        if brightness is not None:
            self.brightness(brightness)

    def clear_key(self, key: int):
        self.cmd(b"CLE", b"\x00\x00\x00\x00" + bytes([int(key)]))

    def clear_all(self):
        self.cmd(b"CLE", b"\x00\x00\x00\x00\xff")

    # -- images
    def _stream(self, tag: bytes, data: bytes, last: int):
        self.cmd(tag, struct.pack(">I", len(data)) + bytes([last]))
        for i in range(0, len(data), REPORT_OUT):
            self.write(data[i:i + REPORT_OUT])

    def set_key_jpeg(self, key: int, jpeg: bytes):
        if key not in IMAGE_KEYS:
            raise N3Error(f"key {key} has no display (only keys 1-6 do)")
        self._stream(b"BAT", jpeg, key)

    def set_screen_jpeg(self, jpeg: bytes):
        self._stream(b"LOG", jpeg, 0x01)

    def set_key_image(self, key: int, image, refresh: bool = True):
        self.set_key_jpeg(key, encode_jpeg(image, KEY_SIZE))
        if refresh:
            self.refresh()

    def set_screen_image(self, image, refresh: bool = True):
        self.set_screen_jpeg(encode_jpeg(image, SCREEN_SIZE))
        if refresh:
            self.refresh()

    # -- heartbeat thread (keeps the device in "connected" mode during long sessions)
    def start_heartbeat(self, interval: float = 10.0):
        if self._hb_thread:
            return

        def loop():
            while not self._hb_stop.wait(interval):
                try:
                    self.heartbeat()
                except Exception:
                    return
        self._hb_stop.clear()
        self._hb_thread = threading.Thread(target=loop, daemon=True)
        self._hb_thread.start()

    def stop_heartbeat(self):
        self._hb_stop.set()
        self._hb_thread = None

    # -- events
    @staticmethod
    def decode(pkt: bytes) -> Optional[dict]:
        """Turn an input report into an event dict, or None if it is not one."""
        if len(pkt) < 11 or pkt[:3] != b"ACK" or pkt[5:7] != b"OK":
            return None
        code, state = pkt[9], pkt[10]
        if code == 0xFF:
            return None  # write acknowledgement on some firmware
        if code in KEY_CODES:
            return {"type": "key", "key": KEY_CODES[code],
                    "action": "press" if state == 0x01 else "release"}
        if code in KNOB_PRESS:
            k = KNOB_PRESS[code]
            return {"type": "knob", "knob": k, "position": KNOB_POSITION[k],
                    "action": "press" if state == 0x01 else "release"}
        if code in KNOB_ROTATE:
            k, d = KNOB_ROTATE[code]
            return {"type": "knob", "knob": k, "position": KNOB_POSITION[k], "action": d}
        return {"type": "unknown", "code": code, "state": state, "raw": pkt[:16].hex()}

    def events(self, timeout: Optional[float] = None, count: Optional[int] = None,
               raw: bool = False) -> Iterable[dict]:
        """Yield decoded events until timeout (seconds) or count events."""
        deadline = time.time() + timeout if timeout else None
        n = 0
        while True:
            if deadline and time.time() >= deadline:
                return
            pkt = self.read(timeout_ms=50)
            if not pkt:
                continue
            ev = self.decode(pkt)
            if ev is None:
                if not raw:
                    continue
                ev = {"type": "raw", "raw": pkt[:32].hex()}
            ev["ts"] = round(time.time(), 3)
            yield ev
            n += 1
            if count and n >= count:
                return


# --------------------------------------------------------------------------- images
def _pil():
    try:
        from PIL import Image, ImageColor, ImageDraw, ImageFont  # noqa
        return Image, ImageColor, ImageDraw, ImageFont
    except ImportError:  # pragma: no cover
        raise N3Error("missing dependency: pip3 install --user pillow")


def _font(size: int):
    _, _, _, ImageFont = _pil()
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def encode_jpeg(image, size: tuple[int, int], quality: int = 100) -> bytes:
    """Fit a PIL image to `size`, rotate for the panel, return JPEG bytes."""
    Image, _, _, _ = _pil()
    im = image.convert("RGB")
    if im.size != size:
        im = im.resize(size, Image.LANCZOS)
    if IMAGE_ROTATION:
        im = im.rotate(IMAGE_ROTATION, expand=True)
    buf = io.BytesIO()
    # match the StreamDock app byte-for-byte in flavour: baseline JFIF, quality 100 (all-ones
    # quantisation tables), 4:2:0 chroma subsampling, no optimisation/progressive
    im.save(buf, "JPEG", quality=quality, subsampling=2, optimize=False, progressive=False)
    return buf.getvalue()


def load_image(path: str, size: tuple[int, int], bg: str = "black"):
    """Open a file, letterbox it onto a `bg` canvas of `size`."""
    Image, ImageColor, _, _ = _pil()
    src = Image.open(path).convert("RGBA")
    src.thumbnail(size, Image.LANCZOS)
    canvas = Image.new("RGBA", size, ImageColor.getrgb(bg))
    canvas.paste(src, ((size[0] - src.width) // 2, (size[1] - src.height) // 2), src)
    return canvas.convert("RGB")


def render_text(text: str, size: tuple[int, int], bg: str = "#202020", fg: str = "white",
                font_size: Optional[int] = None, image: Optional[str] = None):
    """Render (multi-line) text centred on a canvas, optionally over an image."""
    Image, ImageColor, ImageDraw, _ = _pil()
    if image:
        canvas = load_image(image, size, bg)
    else:
        canvas = Image.new("RGB", size, ImageColor.getrgb(bg))
    lines = [ln for ln in text.split("\\n")] if "\\n" in text else text.split("\n")
    draw = ImageDraw.Draw(canvas)
    w, h = size
    fs = font_size or max(10, min(w, h) // 3)
    while fs > 8:
        font = _font(fs)
        widths = [draw.textbbox((0, 0), ln, font=font)[2] for ln in lines]
        line_h = fs + 2
        if max(widths) <= w - 6 and line_h * len(lines) <= h - 4:
            break
        fs -= 1
    font = _font(fs)
    line_h = fs + 2
    y = (h - line_h * len(lines)) // 2
    for ln in lines:
        tw = draw.textbbox((0, 0), ln, font=font)[2]
        draw.text(((w - tw) // 2, y), ln, fill=ImageColor.getrgb(fg), font=font)
        y += line_h
    return canvas


def build_key_image(spec: dict):
    """spec: {text, image, bg, fg, size}  -> PIL image sized for a key."""
    return _build(spec, KEY_SIZE)


def build_screen_image(spec: dict):
    return _build(spec, SCREEN_SIZE)


def _build(spec: dict, size):
    bg = spec.get("bg", "#202020")
    if spec.get("text"):
        return render_text(spec["text"], size, bg=bg, fg=spec.get("fg", "white"),
                           font_size=spec.get("size"), image=spec.get("image"))
    if spec.get("image"):
        return load_image(spec["image"], size, bg)
    Image, ImageColor, _, _ = _pil()
    return Image.new("RGB", size, ImageColor.getrgb(bg))


def apply_layout(dev, layout: dict):
    """layout = {"brightness": 80, "clear": true, "screen": {...}, "keys": {"1": {...}, ...}}"""
    if layout.get("clear"):
        dev.clear_all()
    if "brightness" in layout:
        dev.brightness(layout["brightness"])
        _remember_brightness(int(layout["brightness"]))
    for k, spec in (layout.get("keys") or {}).items():
        key = int(k)
        if spec is None or spec == "clear":
            dev.clear_key(key)
        else:
            dev.set_key_jpeg(key, encode_jpeg(build_key_image(spec), KEY_SIZE))
    if "screen" in layout:
        spec = layout["screen"]
        if spec:
            dev.set_screen_jpeg(encode_jpeg(build_screen_image(spec), SCREEN_SIZE))
    dev.refresh()


# --------------------------------------------------------------------------- daemon + client
SOCK_PATH = os.path.expanduser("~/.config/n3/n3d.sock")
LAUNCHD_LABEL = "local.n3d"
LAUNCHD_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
LOG_PATH = os.path.expanduser("~/Library/Logs/n3d.log")


def _log(msg: str):
    sys.stderr.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


class Client:
    """Talks to a running `n3 daemon` over its Unix socket; mirrors the N3 API."""

    def __init__(self, serial: Optional[str] = None, heartbeat: bool = False, timeout: float = 5.0):
        import socket
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        try:
            self._sock.connect(SOCK_PATH)
        except OSError as e:
            raise N3IOError(f"daemon not reachable at {SOCK_PATH}: {e}") from e
        self._f = self._sock.makefile("rwb")
        self._lock = threading.Lock()
        info = self._rpc([["info"]])
        self.info = info
        self.serial = info.get("serial", "")

    def _rpc(self, ops):
        with self._lock:
            self._f.write((json.dumps({"ops": ops}) + "\n").encode())
            self._f.flush()
            line = self._f.readline()
        if not line:
            raise N3IOError("daemon closed the connection")
        r = json.loads(line)
        if not r.get("ok"):
            raise N3Error(r.get("error", "daemon error"))
        return r.get("result")

    def close(self, notify_disconnect: bool = False):
        try:
            self._sock.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def firmware_version(self):
        return self._rpc([["info"]]).get("firmware", "")

    def write(self, payload: bytes):
        return self._rpc([["raw", payload.hex()]])

    def brightness(self, pct: int):
        self._rpc([["brightness", int(pct)]])

    def wake(self):
        self._rpc([["wake"]])

    def sleep(self):
        self._rpc([["sleep"]])

    def refresh(self):
        self._rpc([["refresh"]])

    def clear_key(self, key: int):
        self._rpc([["clear_key", int(key)]])

    def clear_all(self):
        self._rpc([["clear_all"]])

    def set_key_jpeg(self, key: int, jpeg: bytes):
        import base64
        self._rpc([["key_jpeg", int(key), base64.b64encode(jpeg).decode()]])

    def set_screen_jpeg(self, jpeg: bytes):
        import base64
        self._rpc([["screen_jpeg", base64.b64encode(jpeg).decode()]])

    def set_key_image(self, key: int, image, refresh: bool = True):
        self.set_key_jpeg(key, encode_jpeg(image, KEY_SIZE))
        if refresh:
            self.refresh()

    def set_screen_image(self, image, refresh: bool = True):
        self.set_screen_jpeg(encode_jpeg(image, SCREEN_SIZE))
        if refresh:
            self.refresh()

    def connect(self, brightness: Optional[int] = None):
        self._rpc([["connect", brightness]])

    def device_config(self, cfg: bytes = b"\x1f\x11\x00\x11\x00\x11\x00"):
        self._rpc([["raw", (b"CRT\x00\x00QUCMD" + cfg).hex()]])

    def start_heartbeat(self, interval: float = 10.0):
        pass  # the daemon does it

    def stop_heartbeat(self):
        pass

    def events(self, timeout: Optional[float] = None, count: Optional[int] = None,
               raw: bool = False) -> Iterable[dict]:
        import socket
        sub = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sub.connect(SOCK_PATH)
        f = sub.makefile("rwb")
        f.write(b'{"subscribe": true}\n')
        f.flush()
        deadline = time.time() + timeout if timeout else None
        n = 0
        try:
            while True:
                if deadline:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return
                    sub.settimeout(remaining)
                try:
                    line = f.readline()
                except socket.timeout:
                    return
                if not line:
                    return
                ev = json.loads(line)
                if ev.get("type") == "raw" and not raw:
                    continue
                yield ev
                n += 1
                if count and n >= count:
                    return
        finally:
            try:
                sub.close()
            except Exception:
                pass


def daemon_running() -> bool:
    return os.path.exists(SOCK_PATH)


def open_device(serial: Optional[str] = None, heartbeat: bool = False):
    """Prefer the daemon (it owns the HID handle); fall back to opening the device directly."""
    if daemon_running():
        try:
            return Client(serial=serial, heartbeat=heartbeat)
        except N3IOError:
            try:
                os.unlink(SOCK_PATH)  # stale socket
            except OSError:
                pass
    dev = N3(serial=serial, heartbeat=heartbeat)
    dev.connect()  # no daemon: replay the app's handshake on every open
    return dev


class Daemon:
    """Owns the device: heartbeat, reconnect, event fan-out, last-state replay."""

    def __init__(self, serial: Optional[str] = None, heartbeat: float = 10.0):
        self.serial = serial
        self.hb_interval = heartbeat
        self.dev: Optional[N3] = None
        self.lock = threading.RLock()
        self.subs: list = []
        self.subs_lock = threading.Lock()
        self.state = {"brightness": None, "keys": {}, "screen": None}
        self.stop = threading.Event()
        self.last_write = 0.0
        # Firmware V3.293N3 stops repainting after a period of silence and only recovers on a USB
        # reset. The StreamDock app never goes quiet (it re-uploads a key image + refresh every
        # second), so we do the same: keepalive re-sends a stored key image whenever idle.
        self.keepalive = 1.0
        self._ka_index = 0

    # -- device management
    def _ensure(self) -> N3:
        with self.lock:
            if self.dev is None:
                self.dev = N3(serial=self.serial)
                _log(f"device opened serial={self.dev.serial} fw={self.dev.firmware_version()}")
                self._replay()
            return self.dev

    def _drop(self, why: str):
        with self.lock:
            if self.dev is not None:
                _log(f"device dropped: {why}")
                try:
                    self.dev.close()
                except Exception:
                    pass
                self.dev = None

    def _replay(self):
        d = self.dev
        try:
            d.connect(self.state["brightness"] if self.state["brightness"] is not None else 100)
            for k, jpeg in self.state["keys"].items():
                d.set_key_jpeg(k, jpeg)
            if self.state["screen"]:
                d.set_screen_jpeg(self.state["screen"])
            d.refresh()
        except Exception as e:
            _log(f"replay failed: {e}")

    def _device_loop(self):
        last_hb = 0.0
        while not self.stop.is_set():
            try:
                d = self._ensure()
            except N3IOError as e:
                _log(f"waiting for device: {e}")
                self.stop.wait(3.0)
                continue
            try:
                now = time.time()
                if now - last_hb >= self.hb_interval:
                    with self.lock:
                        d.heartbeat()
                    last_hb = now
                if self.keepalive and now - self.last_write >= self.keepalive:
                    with self.lock:
                        self._keepalive_tick(d)
                        self.last_write = time.time()
                pkt = d.read(timeout_ms=100)
            except (OSError, N3IOError) as e:
                self._drop(str(e))
                continue
            except Exception as e:
                self._drop(f"read error: {e}")
                continue
            if not pkt:
                continue
            ev = N3.decode(pkt)
            if ev is None:
                ev = {"type": "raw", "raw": pkt[:32].hex()}
            ev["ts"] = round(time.time(), 3)
            self._publish(ev)

    def _keepalive_tick(self, d: N3):
        keys = sorted(self.state["keys"])
        if keys:
            k = keys[self._ka_index % len(keys)]
            self._ka_index += 1
            d.set_key_jpeg(k, self.state["keys"][k])
        d.refresh()

    def _publish(self, ev: dict):
        line = (json.dumps(ev) + "\n").encode()
        with self.subs_lock:
            dead = []
            for f in self.subs:
                try:
                    f.write(line)
                    f.flush()
                except Exception:
                    dead.append(f)
            for f in dead:
                self.subs.remove(f)

    # -- request handling
    def apply(self, ops) -> dict:
        import base64
        result = {}
        with self.lock:
            d = self._ensure()
            try:
                self.last_write = time.time()
                for op in ops:
                    name, args = op[0], op[1:]
                    if name == "info":
                        result = {"serial": d.serial, "firmware": d.firmware_version(), "daemon": True,
                                  "pid": os.getpid()}
                    elif name == "brightness":
                        d.brightness(args[0])
                        self.state["brightness"] = max(0, min(100, int(args[0])))
                    elif name == "connect":
                        d.connect(args[0] if args and args[0] is not None else self.state["brightness"])
                    elif name == "wake":
                        d.wake()
                    elif name == "sleep":
                        d.sleep()
                    elif name == "refresh":
                        d.refresh()
                    elif name == "clear_key":
                        d.clear_key(args[0])
                        self.state["keys"].pop(int(args[0]), None)
                    elif name == "clear_all":
                        d.clear_all()
                        self.state["keys"].clear()
                    elif name == "key_jpeg":
                        jpeg = base64.b64decode(args[1])
                        d.set_key_jpeg(int(args[0]), jpeg)
                        self.state["keys"][int(args[0])] = jpeg
                    elif name == "screen_jpeg":
                        jpeg = base64.b64decode(args[0])
                        d.set_screen_jpeg(jpeg)
                        self.state["screen"] = jpeg
                    elif name == "raw":
                        d.write(bytes.fromhex(args[0]))
                    else:
                        raise N3Error(f"unknown op {name}")
            except (OSError, N3IOError) as e:
                self._drop(str(e))
                raise
        return result

    def _serve_conn(self, conn):
        f = conn.makefile("rwb")
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if req.get("subscribe"):
                    with self.subs_lock:
                        self.subs.append(f)
                    return  # connection now owned by the publisher
                try:
                    res = self.apply(req.get("ops") or [])
                    f.write((json.dumps({"ok": True, "result": res}) + "\n").encode())
                except Exception as e:
                    f.write((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
                f.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.subs_lock:
                if f in self.subs:
                    return
            try:
                conn.close()
            except Exception:
                pass

    def run(self):
        import socket
        import signal
        os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
        if os.path.exists(SOCK_PATH):
            try:
                Client(timeout=1.0).close()
                raise SystemExit(f"another n3 daemon is already serving {SOCK_PATH}")
            except N3IOError:
                os.unlink(SOCK_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        srv.listen(16)

        def shutdown(*_):
            self.stop.set()
            try:
                srv.close()
            except Exception:
                pass
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        threading.Thread(target=self._device_loop, daemon=True).start()
        _log(f"n3 daemon listening on {SOCK_PATH}")
        try:
            while not self.stop.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        finally:
            self.stop.set()
            try:
                os.unlink(SOCK_PATH)
            except OSError:
                pass
            self._drop("shutdown")
            _log("n3 daemon stopped")


def launchd_install():
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string>
    <string>{os.path.abspath(__file__)}</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>{LOG_PATH}</string>
  <key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict></plist>
"""
    os.makedirs(os.path.dirname(LAUNCHD_PLIST), exist_ok=True)
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", LAUNCHD_PLIST], capture_output=True)
    open(LAUNCHD_PLIST, "w").write(plist)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", LAUNCHD_PLIST], capture_output=True, text=True)
    if r.returncode != 0:
        raise N3Error(f"launchctl bootstrap failed: {r.stderr.strip()}")
    print(f"installed {LAUNCHD_PLIST}; log: {LOG_PATH}")


def launchd_uninstall():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", LAUNCHD_PLIST], capture_output=True)
    if os.path.exists(LAUNCHD_PLIST):
        os.unlink(LAUNCHD_PLIST)
    print("daemon stopped and launchd agent removed")


def cmd_daemon(a):
    if a.install:
        return launchd_install()
    if a.uninstall:
        return launchd_uninstall()
    if a.status:
        if daemon_running():
            try:
                with Client(timeout=2.0) as c:
                    print(json.dumps(c.info))
                    return
            except N3IOError as e:
                print(f"socket present but daemon not answering: {e}")
                return
        print("daemon not running")
        return
    dm = Daemon(serial=a.serial, heartbeat=a.heartbeat)
    dm.keepalive = a.keepalive
    dm.run()


# --------------------------------------------------------------------------- CLI
def _event_name(ev: dict) -> str:
    if ev["type"] == "key":
        return f"key:{ev['key']}:{ev['action']}"
    if ev["type"] == "knob":
        return f"knob:{ev['knob']}:{ev['action']}"
    return f"unknown:{ev.get('code')}"


def cmd_info(a):
    devs = enumerate_devices()
    out = []
    for d in devs:
        rec = {"serial": d.get("serial_number"), "product": d.get("product_string"),
               "vendor_id": hex(VID), "product_id": hex(PID), "path": d["path"].decode()}
        try:
            with open_device(serial=d.get("serial_number")) as dev:
                rec["firmware"] = dev.firmware_version()
                rec["daemon"] = isinstance(dev, Client)
        except N3Error as e:
            rec["error"] = str(e)
        rec["layout"] = {"lcd_keys": "1-6 (64x64)", "buttons": "7-9", "knobs": KNOB_POSITION,
                         "screen": "320x240"}
        out.append(rec)
    if a.json:
        print(json.dumps(out, indent=2))
    elif not out:
        print("no Stream Dock N3 found")
    else:
        for r in out:
            print(f"Stream Dock N3  serial={r['serial']}  firmware={r.get('firmware', r.get('error'))}")
            print("  keys 1-6: 64x64 LCD keys   keys 7-9: buttons   knobs 1-3: "
                  + ", ".join(f"{k}={v}" for k, v in KNOB_POSITION.items()) + "   screen: 320x240")


def _open(a):
    return open_device(serial=a.serial)


BRIGHTNESS_FILE = os.path.expanduser("~/.config/n3/brightness")


def _remembered_brightness(default: int = 80) -> int:
    try:
        return int(open(BRIGHTNESS_FILE).read().strip())
    except Exception:
        return default


def _remember_brightness(level: int):
    try:
        os.makedirs(os.path.dirname(BRIGHTNESS_FILE), exist_ok=True)
        open(BRIGHTNESS_FILE, "w").write(str(level))
    except Exception:
        pass


def cmd_brightness(a):
    v = a.level.strip()
    if v[0] in "+-":
        level = _remembered_brightness() + int(v)
    else:
        level = int(v)
    level = max(0, min(100, level))
    with _open(a) as d:
        d.brightness(level)
    _remember_brightness(level)
    print(level)


def cmd_simple(a):
    with _open(a) as d:
        getattr(d, a.op)()


def cmd_connect(a):
    with _open(a) as d:
        d.connect(a.brightness)
        d.refresh()


def cmd_clear(a):
    with _open(a) as d:
        if not a.keys or "all" in a.keys:
            d.clear_all()
        else:
            for k in a.keys:
                d.clear_key(int(k))
        d.refresh()


def _spec_from_args(a) -> dict:
    spec = {}
    if a.text:
        spec["text"] = a.text
    if a.image:
        spec["image"] = a.image
    if a.bg:
        spec["bg"] = a.bg
    if a.fg:
        spec["fg"] = a.fg
    if getattr(a, "size", None):
        spec["size"] = a.size
    if not spec:
        raise SystemExit("give --text and/or --image (or --bg for a solid colour)")
    return spec


def cmd_key(a):
    spec = _spec_from_args(a)
    with _open(a) as d:
        for k in a.key:
            d.set_key_jpeg(int(k), encode_jpeg(build_key_image(spec), KEY_SIZE))
        d.refresh()


def cmd_screen(a):
    spec = _spec_from_args(a)
    with _open(a) as d:
        d.set_screen_image(build_screen_image(spec))


def cmd_layout(a):
    text = sys.stdin.read() if a.file == "-" else open(a.file).read()
    layout = json.loads(text)
    with _open(a) as d:
        apply_layout(d, layout)


def cmd_listen(a):
    with open_device(serial=a.serial, heartbeat=True) as d:
        if not a.json:
            print("listening for key/knob events (ctrl-c to stop)...", file=sys.stderr)
        try:
            for ev in d.events(timeout=a.timeout, count=a.count, raw=a.raw):
                if a.json or ev.get("type") == "raw":
                    print(json.dumps(ev), flush=True)
                else:
                    print(_event_name(ev), flush=True)
        except KeyboardInterrupt:
            pass


def cmd_watch(a):
    """Run shell commands from a keymap on events.
    keymap.json: {"key:1:press": "open -a Safari", "knob:3:left": "...", "layout": {...optional...}}"""
    keymap = json.load(open(a.file))
    with open_device(serial=a.serial, heartbeat=True) as d:
        if keymap.get("layout"):
            apply_layout(d, keymap["layout"])
        print(f"watching {a.file} (ctrl-c to stop)", file=sys.stderr)
        try:
            for ev in d.events():
                name = _event_name(ev)
                cmd = keymap.get(name)
                if cmd is None and ev.get("action") == "press":
                    cmd = keymap.get(name.rsplit(":", 1)[0])  # "key:1" == press
                if cmd:
                    print(f"{name} -> {cmd}", file=sys.stderr, flush=True)
                    subprocess.Popen(cmd, shell=True, env={**os.environ, "N3_EVENT": json.dumps(ev)})
        except KeyboardInterrupt:
            pass


def cmd_raw(a):
    payload = bytes.fromhex(a.hex.replace(" ", ""))
    with _open(a) as d:
        d.write(payload)
        if a.read:
            for ev in d.events(timeout=a.read):
                print(json.dumps(ev))


def main(argv=None):
    p = argparse.ArgumentParser(prog="n3", description="Control a Mirabox Stream Dock N3 from the command line.")
    p.add_argument("--serial", help="pick a device by serial when several are attached")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("info", help="list devices, firmware and layout"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_info)
    s = sp.add_parser("brightness", help="set brightness 0-100, or a relative step like +10 / -10"); s.add_argument("level"); s.set_defaults(fn=cmd_brightness)
    for op, hlp in (("wake", "wake the screens"), ("sleep", "put the screens to sleep"), ("refresh", "redraw pending images")):
        s = sp.add_parser(op, help=hlp); s.set_defaults(fn=cmd_simple, op=op)
    s = sp.add_parser("connect", help="send the app's connect handshake (wake, brightness, QUCMD config)"); s.add_argument("--brightness", type=int, default=100); s.set_defaults(fn=cmd_connect)
    s = sp.add_parser("clear", help="clear key images (default: all)"); s.add_argument("keys", nargs="*"); s.set_defaults(fn=cmd_clear)

    for name, fn, hlp in (("key", cmd_key, "draw text and/or an image on LCD keys 1-6"),
                          ("screen", cmd_screen, "draw text and/or an image on the 320x240 screen")):
        s = sp.add_parser(name, help=hlp)
        if name == "key":
            s.add_argument("key", nargs="+", help="key number(s) 1-6")
        s.add_argument("--text", "-t", help="text; use \\n for line breaks")
        s.add_argument("--image", "-i", help="PNG/JPEG file to show (letterboxed)")
        s.add_argument("--bg", help="background colour, e.g. '#1e88e5' or 'navy'")
        s.add_argument("--fg", help="text colour")
        s.add_argument("--size", type=int, help="font size in px (auto-fit by default)")
        s.set_defaults(fn=fn)

    s = sp.add_parser("layout", help="apply a whole layout from JSON (file or - for stdin)"); s.add_argument("file"); s.set_defaults(fn=cmd_layout)
    s = sp.add_parser("listen", help="print key/knob events"); s.add_argument("--timeout", type=float); s.add_argument("--count", type=int); s.add_argument("--json", action="store_true"); s.add_argument("--raw", action="store_true", help="also print undecodable input reports (debug)"); s.set_defaults(fn=cmd_listen)
    s = sp.add_parser("watch", help="run shell commands from a keymap JSON on events"); s.add_argument("file"); s.set_defaults(fn=cmd_watch)
    s = sp.add_parser("daemon", help="own the device in the background: heartbeat, reconnect, event fan-out (CLI/MCP route through it)")
    s.add_argument("--install", action="store_true", help="install + start as a launchd agent (runs at login)")
    s.add_argument("--uninstall", action="store_true", help="stop and remove the launchd agent")
    s.add_argument("--status", action="store_true")
    s.add_argument("--heartbeat", type=float, default=10.0, help="seconds between heartbeats")
    s.add_argument("--keepalive", type=float, default=1.0, help="seconds of silence before re-sending a key image + refresh (0 = off)")
    s.set_defaults(fn=cmd_daemon)
    s = sp.add_parser("raw", help="send raw payload bytes (hex) for protocol experiments"); s.add_argument("hex"); s.add_argument("--read", type=float, help="then print events for N seconds"); s.set_defaults(fn=cmd_raw)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except N3Error as e:
        print(f"n3: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
