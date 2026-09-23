---
name: n3-brightness
description: Adjust the Stream Dock N3's key and screen brightness, or wake/sleep its displays. Use when the user says the deck is too dark, too bright, dim, off, asleep, or asks to set brightness to a level or percentage.
---

# N3 brightness

One brightness value (0–100) drives the six LCD keys and the side screen.
Brightness survives CLI exit; images stay as they are.

## Do this

```bash
n3 brightness 100        # "too dark" -> go straight to 100, then wake
n3 wake                  # displays asleep/blank -> wake (also after long idle)
n3 brightness 30         # dim for night / "too bright"
n3 brightness +20        # relative to the last level set by this tool (remembered in ~/.config/n3/brightness)
n3 brightness -20
n3 sleep                 # displays off, images kept, `n3 wake` restores
```

MCP equivalents: `n3_brightness {level}` and `n3_power {state: wake|sleep}`.

## Rules of thumb

- "Too dark" → set 100 and wake. 100 is the firmware maximum. If it is still
  dark or not refreshing, check `n3 daemon --status` (the heartbeat daemon must
  be running) and then unplug/replug the device.
- "Too bright" → 30. Night / late hours → 20. Normal → 80.
- Steps requested as "a bit brighter/darker" → move by 20.
- The firmware does not acknowledge the command; the CLI prints the level it
  set. `n3 info` failing means the StreamDock app still owns the device.
- Make it a knob if asked: in a `n3 watch` keymap add
  `"knob:3:left": "n3 brightness -10", "knob:3:right": "n3 brightness +10"`.
