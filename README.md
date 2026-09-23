# n3 — Stream Dock N3 without the StreamDock app

Independent, community project. Not affiliated with or endorsed by Mirabox / Hotspot Technology.

Pure-Python control of the Mirabox Stream Dock N3 (USB `6603:1002`) over HID.
No vendor binaries, no Qt app, no plugins: a CLI for humans and scripts, and an
MCP server plus Claude Code skills so an AI agent can drive the deck directly.

```
n3.py        library + CLI
n3_mcp.py    MCP server (stdio JSON-RPC) — tools n3_info, n3_set_key(s), n3_set_screen,
             n3_brightness, n3_clear, n3_power, n3_wait_event, n3_events
.mcp.json    registers the server for Claude Code when run from this directory
.claude/skills/n3        how to use the CLI/MCP, layout, conventions
.claude/skills/n3-menu   ask the user questions through the physical keys
examples/    keymap.json (watch mode), status.json (layout),
             claude_dashboard.py (live tiles: Claude/Codex quotas, active sessions, ping,
             app launcher, volume/brightness knobs; `--install` runs it as a login agent)
```

## Setup

```bash
pip3 install --user hidapi pillow          # package name is 'hidapi' (imports as 'hid'); Python 3.9+
osascript -e 'quit app "StreamDock"'       # the app holds the device exclusively
python3 n3.py info
```

Optional launcher: `ln -s "$PWD/n3.py" ~/bin/n3` (or anywhere on your PATH).

Global MCP registration (instead of the per-project `.mcp.json`):

```bash
claude mcp add n3 -- python3 "$PWD/n3_mcp.py"     # run from this directory
```

## Background daemon (recommended)

Firmware V3.293N3 stops repainting after a period of silence and only recovers on a USB
replug. The StreamDock app never goes quiet (it re-uploads a key image + refresh every
second), so the daemon does the same (`--keepalive 1.0`), plus the 10 s `CONNECT` heartbeat.
`n3 daemon` owns the device, sends the heartbeat, reconnects on unplug/replug,
replays the last layout, and fans out key/knob events. The CLI and the MCP
server automatically talk to it over `~/.config/n3/n3d.sock` when it is running
(macOS opens HID devices exclusively, so the daemon must be the single owner).

```bash
n3 daemon --install     # macOS launchd agent local.n3d, starts at login, log ~/Library/Logs/n3d.log
n3 daemon --status
n3 daemon --uninstall
n3 daemon               # or run it in the foreground yourself
```

## CLI

```bash
python3 n3.py info [--json]
python3 n3.py brightness 0-100 | +10 | -10   # relative steps use the last level set
python3 n3.py key 1 [2 3 ...] --text "Build\nOK" --bg "#2e7d32" --fg white [--image icon.png] [--size 18]
python3 n3.py screen --text "CI: green" --bg navy [--image bg.png]
python3 n3.py clear [keys... | all]
python3 n3.py layout file.json | -          # whole state in one call
python3 n3.py listen [--json] [--timeout S] [--count N]
python3 n3.py watch examples/keymap.json    # run shell commands on key/knob events
python3 n3.py wake | sleep | refresh
python3 n3.py raw <hex> [--read S]          # protocol experiments
```

Layout JSON:

```json
{"brightness": 80, "clear": true,
 "keys": {"1": {"text": "Build", "bg": "#1e88e5"}, "2": {"image": "/path/icon.png"}, "3": null},
 "screen": {"text": "main @ 4f2a1c", "bg": "#1a237e"}}
```

Events are JSON lines: `{"type":"key","key":3,"action":"press"}`,
`{"type":"knob","knob":3,"position":"top","action":"left"}`.

## Platform notes

- macOS is what this was developed and tested on (Apple Silicon, macOS 26). `n3 daemon --install` uses launchd.
- Linux should work with `hidapi` and a udev rule granting access to `6603:1002` (e.g. `SUBSYSTEM=="hidraw", ATTRS{idVendor}=="6603", ATTRS{idProduct}=="1002", MODE="0666"`); run `n3 daemon` under your own service manager. Untested.
- Windows: untested.
- Only the N3 variant reporting firmware `V3.293N3_PXL...` (the app calls it "MBox-N3 ") has been verified. Other Stream Dock models use different sizes and product IDs; see Mirabox's public Device SDK for their tables.

## Device layout

| Part | Ids | Display |
|------|-----|---------|
| LCD keys | 1–6 | 64×64 |
| Buttons | 7–9 | none |
| Knobs | 1 bottom-left, 2 bottom-right, 3 top | none |
| Screen | – | 320×240 |

## Protocol

Recovered from Mirabox's `libtransport` (their public Device SDK) and verified
against firmware `V3.293N3_PXL.02.010`. Output reports are 1024 bytes, input
reports 512 bytes, and GET_REPORT(0) returns the firmware string. Command frames
(`CRT\0\0` prefix, zero padded):

| Frame | Meaning |
|-------|---------|
| `LIG \0\0 <pct>` | brightness (byte 10) |
| `CLE \0\0\0\0 <key or FF>` | clear key / all (byte 11) |
| `STP` | refresh (show queued images) |
| `DIS` / `HAN` | wake / sleep |
| `CONNECT` | heartbeat, every 10 s while a client is attached |
| `CLE\0\0DC` | "software disconnected" notice |
| `BAT <u32be len> <key>` + JPEG in 1024-byte chunks | key image |
| `LOG <u32be len> 01` + JPEG in 1024-byte chunks | screen image |

Input events: `ACK\0\0OK\0\0\0 <code> <state>`; codes 1–6 keys, `25/30/31`
buttons 7–9, `33/34/35` knob press, `90/91 60/61 50/51` knob rotate.
The firmware sends no acknowledgement for writes.
