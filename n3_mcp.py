#!/usr/bin/env python3
"""MCP server (stdio, JSON-RPC) exposing the Stream Dock N3 to Claude and other agents.

Zero dependencies beyond n3.py (hidapi + pillow).  Keeps the device open for the
life of the server so key/knob events are buffered and can be awaited by tools.

Register (project-level .mcp.json is already provided) or globally:
    claude mcp add n3 -- python3 "$PWD/n3_mcp.py"   # from the repo directory
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import n3  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "n3-streamdock", "version": "0.1.0"}


# --------------------------------------------------------------------------- device holder
class Device:
    """Lazily opened N3 with a background reader that buffers events."""

    def __init__(self):
        self.dev: n3.N3 | None = None
        self.events: deque = deque(maxlen=500)
        self.cv = threading.Condition()
        self.reader: threading.Thread | None = None
        self.serial = os.environ.get("N3_SERIAL") or None

    def get(self):
        if self.dev is None:
            self.dev = n3.open_device(serial=self.serial, heartbeat=True)  # daemon Client if running, else direct
            self.reader = threading.Thread(target=self._read_loop, daemon=True)
            self.reader.start()
        return self.dev

    def _read_loop(self):
        dev = self.dev
        try:
            for ev in dev.events():
                if self.dev is not dev:
                    return
                with self.cv:
                    self.events.append(ev)
                    self.cv.notify_all()
        except Exception:
            pass
        if self.dev is dev:
            self.drop()

    def drop(self):
        d, self.dev = self.dev, None
        if d:
            try:
                d.close()
            except Exception:
                pass

    def call(self, fn):
        """Run fn(dev); on a transport error reopen once and retry."""
        try:
            return fn(self.get())
        except (OSError, n3.N3IOError) as e:
            self.drop()
            try:
                return fn(self.get())
            except Exception as e2:
                raise n3.N3Error(f"{e2} (first error: {e})")

    def wait(self, timeout: float, kinds: set[str] | None = None) -> list[dict]:
        """Return buffered events (drained) or block up to timeout for the first one."""
        self.get()
        deadline = time.time() + timeout
        with self.cv:
            while True:
                got = [e for e in self.events if not kinds or e["type"] in kinds]
                if got:
                    self.events.clear()
                    return got
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self.cv.wait(remaining)


D = Device()


# --------------------------------------------------------------------------- tools
def _spec_schema(extra: dict | None = None) -> dict:
    props = {
        "text": {"type": "string", "description": "text to draw; use \\n for line breaks; auto-sized"},
        "image": {"type": "string", "description": "absolute path to a PNG/JPEG to show (letterboxed)"},
        "bg": {"type": "string", "description": "background colour, e.g. '#1e88e5' or 'navy' (default #202020)"},
        "fg": {"type": "string", "description": "text colour (default white)"},
        "size": {"type": "integer", "description": "font size in px (default: auto fit)"},
    }
    props.update(extra or {})
    return props


TOOLS = [
    {
        "name": "n3_info",
        "description": "Describe the attached Stream Dock N3: serial, firmware, key/knob layout. Call first if unsure the device is present.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "n3_set_key",
        "description": "Draw text and/or an image on one LCD key (1-6). Keys 7-9 are plain buttons without a display.",
        "inputSchema": {"type": "object", "required": ["key"],
                        "properties": _spec_schema({"key": {"type": "integer", "minimum": 1, "maximum": 6}})},
    },
    {
        "name": "n3_set_keys",
        "description": "Batch update: map of key number -> {text,image,bg,fg,size} or null to clear that key. Optionally also brightness and screen. Faster than many n3_set_key calls (single refresh).",
        "inputSchema": {"type": "object", "properties": {
            "keys": {"type": "object", "description": "e.g. {\"1\": {\"text\": \"Build\", \"bg\": \"#1e88e5\"}, \"2\": null}",
                     "additionalProperties": {"anyOf": [{"type": "object", "properties": _spec_schema()}, {"type": "null"}]}},
            "screen": {"type": "object", "properties": _spec_schema(), "description": "optional screen content"},
            "brightness": {"type": "integer", "minimum": 0, "maximum": 100},
            "clear": {"type": "boolean", "description": "clear all keys first"},
        }},
    },
    {
        "name": "n3_set_screen",
        "description": "Draw text and/or an image on the N3's 320x240 side screen.",
        "inputSchema": {"type": "object", "properties": _spec_schema()},
    },
    {
        "name": "n3_brightness",
        "description": "Set key and screen brightness (0-100).",
        "inputSchema": {"type": "object", "required": ["level"],
                        "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}}},
    },
    {
        "name": "n3_clear",
        "description": "Clear key images. With no keys given, clears all six.",
        "inputSchema": {"type": "object", "properties": {
            "keys": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 6}}}},
    },
    {
        "name": "n3_power",
        "description": "Wake or sleep the displays.",
        "inputSchema": {"type": "object", "required": ["state"],
                        "properties": {"state": {"type": "string", "enum": ["wake", "sleep"]}}},
    },
    {
        "name": "n3_wait_event",
        "description": "Wait up to `timeout` seconds for the user to press a key/knob or turn a knob. Returns buffered events immediately if any occurred since the last call. Events: {type:'key', key:1-9, action:'press'|'release'} or {type:'knob', knob:1-3, position, action:'press'|'release'|'left'|'right'}.",
        "inputSchema": {"type": "object", "properties": {
            "timeout": {"type": "number", "default": 30, "description": "seconds to wait (max 600)"},
            "presses_only": {"type": "boolean", "default": True, "description": "drop 'release' events"}}},
    },
    {
        "name": "n3_events",
        "description": "Return (and clear) events buffered since the last call, without waiting.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _text(s: str) -> dict:
    return {"content": [{"type": "text", "text": s}]}


def tool_call(name: str, args: dict) -> dict:
    if name == "n3_info":
        def f(d):
            return {"serial": d.serial, "firmware": d.firmware_version(),
                    "lcd_keys": "1-6 (64x64)", "buttons": "7-9 (no display)",
                    "knobs": n3.KNOB_POSITION, "screen": "320x240"}
        return _text(json.dumps(D.call(f)))

    if name == "n3_set_key":
        key = int(args["key"])
        spec = {k: v for k, v in args.items() if k != "key" and v is not None}
        if not spec:
            raise ValueError("give text, image or bg")
        D.call(lambda d: (d.set_key_jpeg(key, n3.encode_jpeg(n3.build_key_image(spec), n3.KEY_SIZE)), d.refresh()))
        return _text(f"key {key} updated")

    if name == "n3_set_keys":
        layout = {k: v for k, v in args.items() if v is not None}
        D.call(lambda d: n3.apply_layout(d, layout))
        return _text("layout applied: " + ", ".join(sorted((layout.get("keys") or {}).keys()))
                     + (" + screen" if layout.get("screen") else ""))

    if name == "n3_set_screen":
        spec = {k: v for k, v in args.items() if v is not None}
        if not spec:
            raise ValueError("give text, image or bg")
        D.call(lambda d: d.set_screen_image(n3.build_screen_image(spec)))
        return _text("screen updated")

    if name == "n3_brightness":
        level = max(0, min(100, int(args["level"])))
        D.call(lambda d: d.brightness(level))
        n3._remember_brightness(level)
        return _text(f"brightness {level}")

    if name == "n3_clear":
        keys = args.get("keys") or []

        def f(d):
            if keys:
                for k in keys:
                    d.clear_key(int(k))
            else:
                d.clear_all()
            d.refresh()
        D.call(f)
        return _text("cleared " + (", ".join(map(str, keys)) if keys else "all keys"))

    if name == "n3_power":
        D.call(lambda d: d.wake() if args["state"] == "wake" else d.sleep())
        return _text(args["state"])

    if name == "n3_wait_event":
        timeout = min(float(args.get("timeout", 30)), 600)
        presses_only = args.get("presses_only", True)
        deadline = time.time() + timeout
        while True:
            evs = D.wait(max(0.0, deadline - time.time()))
            if presses_only:
                evs = [e for e in evs if e.get("action") != "release"]
            if evs or time.time() >= deadline:
                return _text(json.dumps(evs))

    if name == "n3_events":
        return _text(json.dumps(D.wait(0)))

    raise ValueError(f"unknown tool {name}")


# --------------------------------------------------------------------------- JSON-RPC over stdio
def send(msg: dict):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def handle(req: dict):
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO,
            "instructions": "Stream Dock N3 control. Keys 1-6 have 64x64 screens, keys 7-9 are buttons, knobs 1-3 (bottom-left, bottom-right, top), plus a 320x240 side screen. Use n3_set_keys for a whole layout, n3_wait_event to react to the user."}})
    elif method == "notifications/initialized" or method.startswith("notifications/"):
        return
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        try:
            res = tool_call(params.get("name"), params.get("arguments") or {})
            send({"jsonrpc": "2.0", "id": mid, "result": res})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "result": {"isError": True,
                  "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        threading.Thread(target=handle, args=(req,), daemon=True).start() if req.get("method") == "tools/call" \
            and (req.get("params") or {}).get("name") == "n3_wait_event" else handle(req)
    D.drop()


if __name__ == "__main__":
    main()
