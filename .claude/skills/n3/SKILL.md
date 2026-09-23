---
name: n3
description: Control the Mirabox Stream Dock N3 (6 LCD keys, 3 buttons, 3 knobs, 320x240 screen) from the command line or via the n3 MCP tools, without the StreamDock app. Use whenever the user mentions the N3, Stream Dock, Mirabox, showing something on the keys/screen, key brightness, or reacting to key presses or knob turns.
---

# Stream Dock N3 control

Everything lives in this repo (the directory containing `n3.py`; `n3` on PATH is a symlink to it):

- `n3.py` — library + CLI (pure Python: `hidapi` + `pillow`, no vendor binaries).
- `n3_mcp.py` — MCP server (registered in `.mcp.json` as `n3`) exposing tools
  `n3_info`, `n3_set_key`, `n3_set_keys`, `n3_set_screen`, `n3_brightness`,
  `n3_clear`, `n3_power`, `n3_wait_event`, `n3_events`.

Prefer the MCP tools when they are loaded (they keep the device open and buffer
events). Fall back to the CLI otherwise.

## Daemon (normally already running)

`n3 daemon` is installed as launchd agent `local.n3d`. It owns the HID handle,
sends the 10 s heartbeat and a 1 s keepalive image refresh (without it this firmware
stops repainting after idle until a USB replug), reconnects on replug, replays the last
layout and streams events; the CLI and MCP route through `~/.config/n3/n3d.sock`
automatically. If the deck stops refreshing: `n3 daemon --status`, then
`n3 daemon --install` to (re)start it; log at `~/Library/Logs/n3d.log`.

## Prerequisites

- The StreamDock.app must NOT be running; it holds the HID device exclusively.
  Quit it with `osascript -e 'quit app "StreamDock"'`. Never relaunch it unless asked.
- `pip3 install --user hidapi pillow` (already installed for the system python3).

## Layout facts

| Part | Numbers | Display | Notes |
|------|---------|---------|-------|
| LCD keys | 1–6 | 64×64 JPEG | left→right, top→bottom |
| Buttons | 7–9 | none | press/release only |
| Knobs | 1 bottom-left, 2 bottom-right, 3 top | none | press/release, left/right rotate |
| Screen | – | 320×240 JPEG | text or image |

Images/text are auto-fitted; the library rotates them for the panel. Keep key
text to 1–2 short words or 2 lines; the auto-fit shrinks the font until it fits.

## CLI cheat sheet

```bash
# `n3` is n3.py on PATH; run from anywhere. Example layouts live in this repo's examples/.
n3 info                              # serial, firmware, layout
n3 brightness 70          # or +10 / -10 relative to last set level
n3 key 1 --text "Build" --bg "#1e88e5"
n3 key 2 3 --text "Hi\nthere" --fg yellow
n3 key 4 --image /path/icon.png      # letterboxed on --bg
n3 screen --text "CI: green" --bg navy
n3 clear            # all keys        | n3 clear 2 5
n3 layout examples/status.json       # whole layout in one go (or - for stdin)
n3 listen --json --timeout 30        # JSON lines of events
n3 watch examples/keymap.json        # run shell commands on events
n3 wake | sleep | refresh
```

Layout JSON: `{"brightness": 80, "clear": true, "keys": {"1": {"text","image","bg","fg","size"}, "2": null}, "screen": {...}}`.
`null` clears a key. One `layout` call is much faster than several `key` calls.

Events (from `listen --json`, `watch`, or `n3_wait_event`):
`{"type":"key","key":3,"action":"press"}`, `{"type":"knob","knob":3,"position":"top","action":"left"}`.
Keymap names: `key:N`, `key:N:release`, `knob:N:press|left|right`.

## Conventions for an agent driving the device

- Batch: build the whole state and send it with `n3_set_keys` / `layout`, then stop.
- Status colours: green `#2e7d32` ok, red `#c62828` failed, amber `#f9a825` running, grey `#455a64` idle, blue `#1565c0` info.
- To ask the user something physically, put the options on keys and call
  `n3_wait_event` (see the `n3-menu` skill).
- Long waits: `n3_wait_event` accepts up to 600 s; events that happened while
  you were busy are buffered and returned immediately.
- Do not send the "disconnect" notice or relaunch StreamDock.app; images persist
  on the device after the CLI exits.
- Firmware acknowledges nothing on writes; silence is success. Errors surface as
  exceptions (device missing / held by another app).

## Protocol notes (only if you need to extend `n3.py`)

Output reports 1024 B, input 512 B, GET_REPORT(0) = firmware string.
`CRT..LIG..<pct>@10`, `CRT..CLE....<key|FF>@11`, `CRT..STP` refresh, `CRT..DIS` wake,
`CRT..HAN` sleep, `CRT..CONNECT` heartbeat, `CRT..BAT <u32be len> <key>` + JPEG chunks,
`CRT..LOG <u32be len> 01` + JPEG chunks. Full comment block at the top of `n3.py`.
