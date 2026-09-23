---
name: n3-menu
description: Ask the user a question or get an approval through the Stream Dock N3's physical keys instead of the terminal — show options on the LCD keys, wait for a press, act on the answer. Use when the user wants to approve/choose/confirm from the device, wants "a button for" something, or when a long task should notify them on the deck and wait for a key.
---

# Physical menus on the N3

Pattern: render choices on keys 1–6, wait for a press, map the key back to the
choice, then restore or clear the keys. Requires the `n3` MCP server (preferred)
or the CLI (`n3` on PATH, i.e. `n3.py` in this repo). See the `n3` skill for basics.

## Steps (MCP)

1. `n3_set_keys` with one option per key, colour-coded, and a short prompt on the
   screen. Leave unused keys `null` so stale labels never get pressed by mistake.
   ```json
   {"clear": true,
    "keys": {"1": {"text": "Yes", "bg": "#2e7d32"},
             "2": {"text": "No",  "bg": "#c62828"},
             "3": {"text": "Skip","bg": "#455a64"}},
    "screen": {"text": "Deploy to prod?", "bg": "#1a237e"}}
   ```
2. `n3_events` once to discard anything buffered before the prompt was shown.
3. `n3_wait_event` with a sensible `timeout` (60–600 s). Ignore events whose
   `key` is not one of the offered keys and wait again with the remaining time.
4. Acknowledge on the device (e.g. screen "Deploying…") and proceed.
5. When done, either show the outcome (green/red) or `n3_clear`.

Knobs can be used as a scroll/adjust input: `knob 3` (top) left/right for a
value, `knob:3:press` to confirm. Show the current value on the screen after
every turn.

## Steps (CLI)

```bash
echo '{"clear":true,"keys":{"1":{"text":"Yes","bg":"#2e7d32"},"2":{"text":"No","bg":"#c62828"}},"screen":{"text":"Deploy?","bg":"#1a237e"}}' | n3 layout -
n3 listen --json --count 1 --timeout 300   # prints the first event as JSON
```
(`listen` prints presses and releases; act on `"action":"press"`.)

## Notifying without a question

For "tell me when it's done": `n3_set_screen` with the result and a green/red
key, then `n3_wait_event` if you want the user to dismiss it; otherwise leave it.
