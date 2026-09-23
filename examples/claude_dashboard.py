#!/usr/bin/env python3
"""Claude/Codex dashboard for the Stream Dock N3 (a port of a StreamDock "scene").

Keys (3 x 2 LCD grid):
  1  app launcher (icon from the installed app; press opens it)
  2  Codex weekly quota  (% used, from ~/.codex/sessions/*.jsonl rate_limits events)
  3  active Claude Code sessions (herdr agent list + fresh ~/.claude/projects transcripts)
     press: bring the herdr terminal window to the front
  4  Claude 5-hour quota   (from ~/.claude/rate-limits.json, written by the status-line hook)
  5  Claude weekly quota
  6  ping latency to a target, 15 s rolling average

Knobs: top knob = system volume (press = mute toggle); bottom-left knob = key brightness.

Runs against `n3 daemon`. Install as a login agent with --install (macOS).
Optional overrides in ~/.config/n3/dashboard.json, e.g.
  {"ping_target": "192.168.1.1", "launcher_app": "Slack", "herdr_window": "herdr", "terminal_app": "Ghostty"}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import n3  # noqa: E402
from PIL import Image, ImageColor, ImageDraw  # noqa: E402

CONFIG_FILE = os.path.expanduser("~/.config/n3/dashboard.json")
CFG = {
    "ping_target": "1.1.1.1",
    "launcher_app": "Slack",
    "launcher_key": 1,
    "terminal_app": "Ghostty",
    "herdr_window": "herdr",
    "quota_file": os.path.expanduser("~/.claude/rate-limits.json"),
    "codex_sessions": os.path.expanduser("~/.codex/sessions"),
    "projects_dir": os.path.expanduser("~/.claude/projects"),
    "herdr_bin": os.path.expanduser("~/.local/bin/herdr"),
}
try:
    CFG.update(json.load(open(CONFIG_FILE)))
except Exception:
    pass

STALE_MS = 6 * 3600 * 1000
ACTIVE_WINDOW_S = 60
PING_WINDOW_S = 15
GREEN, AMBER, RED, GREY, INK = "#46a758", "#f5a623", "#e5484d", "#5a5a5a", "#f5f0ea"
LAUNCHD_LABEL = "local.n3-dashboard"
LOG_PATH = os.path.expanduser("~/Library/Logs/n3-dashboard.log")


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------------ rendering (64x64)
def tile(digit, sub, color, digit_color=INK, unit=""):
    w, h = n3.KEY_SIZE
    im = Image.new("RGB", (w, h), "#000000")
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=10, fill="#1a1a1e")
    s = str(digit)
    size = 30 if len(s) <= 2 else 22 if len(s) == 3 else 17
    font = n3._font(size)
    unit_font = n3._font(max(9, int(size * 0.45)))
    tw = d.textbbox((0, 0), s, font=font)[2]
    uw = d.textbbox((0, 0), unit, font=unit_font)[2] if unit else 0
    x = (w - tw - uw) // 2
    d.text((x, 10), s, fill=ImageColor.getrgb(digit_color), font=font)
    if unit:
        d.text((x + tw + 1, 10 + size - int(size * 0.45)), unit, fill=ImageColor.getrgb(color), font=unit_font)
    sf = n3._font(9)
    while sf and d.textbbox((0, 0), sub, font=sf)[2] > w - 4 and len(sub) > 3:
        sub = sub[:-2]
    sw = d.textbbox((0, 0), sub, font=sf)[2]
    d.text(((w - sw) // 2, h - 14), sub, fill=ImageColor.getrgb(color), font=sf)
    return im


def launcher_tile(app):
    path = os.path.expanduser(f"~/.config/n3/icons/{app}.png")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        res = f"/Applications/{app}.app/Contents/Resources"
        icns = None
        try:
            info = json.loads(subprocess.run(["plutil", "-convert", "json", "-o", "-",
                                              f"/Applications/{app}.app/Contents/Info.plist"],
                                             capture_output=True, text=True).stdout)
            name = info.get("CFBundleIconFile", "")
            icns = os.path.join(res, name if name.endswith(".icns") else name + ".icns")
        except Exception:
            pass
        if not icns or not os.path.exists(icns):
            cands = [f for f in os.listdir(res) if f.endswith(".icns")] if os.path.isdir(res) else []
            icns = os.path.join(res, cands[0]) if cands else None
        if icns:
            subprocess.run(["sips", "-s", "format", "png", icns, "--out", path], capture_output=True)
    if os.path.exists(path):
        return n3.load_image(path, n3.KEY_SIZE, "#1a1a1e")
    return n3.render_text(app, n3.KEY_SIZE, bg="#1a1a1e")


# ------------------------------------------------------------------ data sources
def read_quota(window):
    try:
        raw = json.load(open(CFG["quota_file"]))
    except Exception:
        return None
    w = (raw.get("rate_limits") or {}).get(window)
    if not w or w.get("used_percentage") is None:
        return None
    resets = float(w.get("resets_at") or 0)
    if resets and resets < 1e12:
        resets *= 1000
    updated = float(raw.get("updated_at") or 0) * 1000
    return {"used": float(w["used_percentage"]), "resets_at": resets,
            "stale": time.time() * 1000 - updated > STALE_MS}


def codex_quota():
    files = []
    for dp, _, fs in os.walk(CFG["codex_sessions"]):
        for f in fs:
            if f.endswith(".jsonl"):
                p = os.path.join(dp, f)
                try:
                    files.append((os.stat(p).st_mtime * 1000, p))
                except OSError:
                    pass
    for mtime, p in sorted(files, reverse=True)[:20]:
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as fh:
                fh.seek(max(0, size - 1024 * 1024))
                lines = fh.read().decode("utf-8", "replace").split("\n")
        except OSError:
            continue
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except Exception:
                continue
            lim = (ev.get("payload") or {}).get("rate_limits")
            if not lim or lim.get("limit_id") != "codex":
                continue
            try:
                updated = time.mktime(time.strptime(ev["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")) * 1000
            except Exception:
                updated = mtime
            out = {}
            for win in (lim.get("primary"), lim.get("secondary")):
                if not win or win.get("used_percent") is None:
                    continue
                key = {300: "five_hour", 10080: "seven_day"}.get(int(win.get("window_minutes") or 0))
                if not key:
                    continue
                resets = float(win.get("resets_at") or 0)
                if resets and resets < 1e12:
                    resets *= 1000
                out[key] = {"used": float(win["used_percent"]), "resets_at": resets,
                            "stale": time.time() * 1000 - updated > STALE_MS}
            return out
    return {}


def quota_tile(q, label):
    if not q:
        return tile("—", f"{label} no data", GREY, "#8a8a8a")
    pct = round(q["used"])
    color = RED if pct >= 90 else AMBER if pct >= 70 else GREEN
    sub = label
    left = q["resets_at"] - time.time() * 1000
    if q["resets_at"] and left > 0:
        hrs = round(left / 3.6e6)
        sub += f" {round(hrs / 24)}d" if hrs >= 48 else f" {hrs}h"
    if q["stale"]:
        sub += " stale"
        color, ink = "#8a8a8a", "#8a8a8a"
    else:
        ink = INK
    return tile(pct, sub, color, ink, "%")


def session_counts():
    tracked, working, blocked = set(), 0, 0
    for binp in (CFG["herdr_bin"], "herdr"):
        try:
            out = subprocess.run([binp, "agent", "list"], capture_output=True, text=True, timeout=3).stdout
            agents = (json.loads(out).get("result") or {}).get("agents") or []
        except Exception:
            continue
        for a in agents:
            if a.get("agent") != "claude":
                continue
            sid = (a.get("agent_session") or {}).get("value")
            if not sid:
                continue
            tracked.add(sid)
            st = a.get("agent_status")
            working += st == "working"
            blocked += st == "blocked"
        break
    now = time.time()
    try:
        for d in os.scandir(CFG["projects_dir"]):
            if not d.is_dir():
                continue
            for f in os.scandir(d.path):
                if f.name.endswith(".jsonl") and now - f.stat().st_mtime < ACTIVE_WINDOW_S:
                    if f.name[:-6] not in tracked:
                        working += 1
    except OSError:
        pass
    return working, blocked


def counter_tile(working, blocked):
    if blocked > 0:
        return tile(blocked, "blocked", RED, RED)
    return tile(working, "working" if working else "idle", GREEN if working else GREY)


def ping_once(target):
    try:
        out = subprocess.run(["/sbin/ping", "-c", "1", "-W", "900", "-n", target],
                             capture_output=True, text=True, timeout=2).stdout
        m = re.search(r"time=([\d.]+) ms", out)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def latency_tile(samples, target):
    ok = [s for _, s in samples if s is not None]
    if not samples:
        return tile("…", target, GREY)
    if not ok:
        return tile("✕", f"{target} timeout", RED, RED)
    avg = sum(ok) / len(ok)
    digit = f"{avg:.1f}" if avg < 10 else str(round(avg))
    lost = len(samples) - len(ok)
    color = GREEN if avg < 50 else AMBER if avg < 150 else RED
    return tile(digit, f"ms {lost} lost" if lost else f"ms {target}", color)


# ------------------------------------------------------------------ actions
def osa(script):
    subprocess.Popen(["/usr/bin/osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def focus_herdr():
    osa(f'''tell application "System Events"
  tell process "{CFG["terminal_app"]}"
    set frontmost to true
    repeat with w in windows
      if title of w contains "{CFG["herdr_window"]}" then
        perform action "AXRaise" of w
        exit repeat
      end if
    end repeat
  end tell
end tell''')


def on_event(ev):
    t, a = ev.get("type"), ev.get("action")
    if t == "key" and a == "press":
        k = ev.get("key")
        if k == CFG["launcher_key"]:
            subprocess.Popen(["open", "-a", CFG["launcher_app"]])
        elif k == 3:
            focus_herdr()
    elif t == "knob":
        kn = ev.get("knob")
        if kn == 3:  # top knob: volume
            if a == "left":
                osa("set volume output volume ((output volume of (get volume settings)) - 5)")
            elif a == "right":
                osa("set volume output volume ((output volume of (get volume settings)) + 5)")
            elif a == "press":
                osa("set volume output muted (not (output muted of (get volume settings)))")
        elif kn == 1 and a in ("left", "right"):  # bottom-left knob: brightness
            subprocess.Popen([sys.executable, os.path.join(os.path.dirname(n3.__file__), "n3.py"),
                              "brightness", "-10" if a == "left" else "+10"], stdout=subprocess.DEVNULL)


def event_loop():
    while True:
        try:
            dev = n3.open_device()
            for ev in dev.events():
                try:
                    on_event(ev)
                except Exception as e:
                    log(f"action error: {e}")
        except Exception as e:
            log(f"event stream lost: {e}; retrying")
        time.sleep(2)


# ------------------------------------------------------------------ main loop
def main():
    if "--install" in sys.argv:
        return install()
    if "--uninstall" in sys.argv:
        return uninstall()
    threading.Thread(target=event_loop, daemon=True).start()
    last = {}
    samples = []
    next_counts = next_quota = 0.0
    tiles = {}
    dev = None
    while True:
        now = time.time()
        if CFG["launcher_key"] not in tiles:
            tiles[CFG["launcher_key"]] = launcher_tile(CFG["launcher_app"])
        ms = ping_once(CFG["ping_target"])
        samples.append((now, ms))
        samples = [s for s in samples if now - s[0] <= PING_WINDOW_S]
        tiles[6] = latency_tile(samples, CFG["ping_target"])
        if now >= next_counts:
            tiles[3] = counter_tile(*session_counts())
            next_counts = now + 4
        if now >= next_quota:
            cq = codex_quota()
            tiles[2] = quota_tile(cq.get("seven_day"), "codex 7d")
            tiles[4] = quota_tile(read_quota("five_hour"), "5 hours")
            tiles[5] = quota_tile(read_quota("seven_day"), "weekly")
            next_quota = now + 30
        try:
            if dev is None:
                dev = n3.open_device()
            changed = False
            for k, im in tiles.items():
                jpeg = n3.encode_jpeg(im, n3.KEY_SIZE)
                h = hashlib.md5(jpeg).hexdigest()
                if last.get(k) != h:
                    dev.set_key_jpeg(k, jpeg)
                    last[k] = h
                    changed = True
            if changed:
                dev.refresh()
        except Exception as e:
            log(f"device error: {e}")
            dev = None
            last.clear()
        time.sleep(max(0.0, 1.0 - (time.time() - now)))


def install():
    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string><string>{os.path.abspath(__file__)}</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{os.environ.get("PATH", "/usr/bin:/bin")}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>{LOG_PATH}</string><key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict></plist>
"""
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", plist_path], capture_output=True)
    open(plist_path, "w").write(plist)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist_path], capture_output=True, text=True)
    print("installed" if r.returncode == 0 else f"launchctl failed: {r.stderr.strip()}", plist_path)


def uninstall():
    plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", plist_path], capture_output=True)
    if os.path.exists(plist_path):
        os.unlink(plist_path)
    print("removed")


if __name__ == "__main__":
    main()
