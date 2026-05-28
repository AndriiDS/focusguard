#!/usr/bin/env python3
"""
FocusGuard - personal website blocker for macOS.

Blocks websites system-wide (any browser) via /etc/hosts. Supports:
  - Daily time limits (with reset at midnight)
  - Scheduled night-block windows (e.g. 22:30 - 08:00)
  - Password-protected temporary unlock

Runs as a LaunchDaemon (root) so it can edit /etc/hosts directly.
Activity detection is fed by a per-user LaunchAgent (fg-axurl) that writes the
frontmost window's app/url/title to active.txt, since the Accessibility grant
is only honoured inside the GUI login session.
"""

import argparse
import base64
import copy
import datetime
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import subprocess
import sys
import time
from pathlib import Path

# --- paths ---
CONFIG_DIR  = Path("/usr/local/etc/focusguard")
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_DIR   = Path("/usr/local/var/focusguard")
STATE_PATH  = STATE_DIR / "state.json"
ACTIVE_PATH = STATE_DIR / "active.txt"
TOTP_PATH   = CONFIG_DIR / "totp.secret"
LOG_PATH    = STATE_DIR / "focusguard.log"
HOSTS_PATH   = Path("/etc/hosts")
HOSTS_BACKUP = Path("/etc/hosts.focusguard-backup")

MARKER_START = "# FOCUSGUARD_START - managed block, do not edit"
MARKER_END   = "# FOCUSGUARD_END"

DEFAULT_CONFIG = {
    "night_block": {"start": "22:30", "end": "08:00"},
    "sites": {
        "youtube": {
            "domains": [
                "youtube.com", "www.youtube.com", "m.youtube.com",
                "youtu.be", "www.youtu.be",
                "youtube-nocookie.com", "www.youtube-nocookie.com",
            ],
            "url_patterns": [
                "youtube.com", "youtu.be", "youtube-nocookie",
                "googlevideo", "duck://player",
            ],
            "daily_limit_minutes": 60,
            "night_block": True,
        },
        "spotify": {
            "domains": ["open.spotify.com", "spotify.com", "www.spotify.com"],
            "url_patterns": ["open.spotify.com", "spotify.com"],
            "apps": ["Spotify"],
            "daily_limit_minutes": 0,
            "night_block": True,
        },
    },
}

TICK_SECONDS = 60
ACTIVE_MAX_AGE = 120  # ignore active.txt if the agent stopped refreshing it
STATE_DEFAULT = {"date": "", "usage": {}, "unlock_until": 0}

# ---------- utilities ----------

def setup_logging():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

def load_json(path, default):
    return copy.deepcopy(default) if not path.exists() else json.loads(path.read_text())

def save_json(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.chmod(mode)
    tmp.replace(path)

def load_config():  return load_json(CONFIG_PATH, DEFAULT_CONFIG)
def save_config(c): save_json(CONFIG_PATH, c)
def load_state():   return load_json(STATE_PATH, STATE_DEFAULT)
def save_state(s):  save_json(STATE_PATH, s)

# ---------- TOTP second factor (RFC 6238) ----------

def load_totp():
    if not TOTP_PATH.exists():
        return None
    return TOTP_PATH.read_text().strip()

def save_totp(secret: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOTP_PATH.write_text(secret)
    os.chmod(TOTP_PATH, 0o600)

def totp_now(secret: str, t=None, step=30, digits=6) -> str:
    counter = (t or int(time.time())) // step
    h = hmac.new(base64.b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0f
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % 10**digits).zfill(digits)

def verify_totp(secret: str, code: str) -> bool:
    now = int(time.time())
    return any(secrets.compare_digest(totp_now(secret, now + offset), code.strip())
               for offset in (-30, 0, 30))

def in_night_window(night, now: datetime.datetime) -> bool:
    if not night:
        return False
    sh, sm = map(int, night["start"].split(":"))
    eh, em = map(int, night["end"].split(":"))
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    end = eh * 60 + em
    if start > end:        # wraps midnight (e.g. 22:30 -> 08:00)
        return cur >= start or cur < end
    return start <= cur < end

# ---------- active-window detection ----------
# A per-user LaunchAgent runs fg-axurl inside the GUI login session (where the
# Accessibility grant is honoured) and writes "app||url||title" to active.txt.
# The root daemon cannot read AX itself, so it reads that file here.

def read_active_url():
    if not ACTIVE_PATH.exists():
        return "", "", ""
    if time.time() - ACTIVE_PATH.stat().st_mtime > ACTIVE_MAX_AGE:
        return "", "", ""
    app, url, title = (ACTIVE_PATH.read_text().strip().split("||") + ["", "", ""])[:3]
    return app, url, title

def detect_active_site(config):
    """Return (site_key, frontmost_app) for the monitored site in front, else (None, app)."""
    app, url, title = read_active_url()
    haystack = (url + " " + title).lower()
    for site_key, site in config["sites"].items():
        if any(p.lower() in haystack for p in site.get("url_patterns", [])):
            return site_key, app
    return None, app

# ---------- /etc/hosts management ----------

def kill_app(name):
    if subprocess.run(["pkill", "-ix", name]).returncode == 0:
        logging.info(f"Killed app: {name}")

def write_hosts(content: str):
    HOSTS_PATH.write_text(content)
    subprocess.run(["dscacheutil", "-flushcache"], check=False)
    subprocess.run(["killall", "-HUP", "mDNSResponder"], check=False)

def update_hosts(blocked_domains) -> bool:
    """Update the managed block in /etc/hosts. Returns True if anything changed."""
    current = HOSTS_PATH.read_text()

    # Strip any existing managed block
    if MARKER_START in current and MARKER_END in current:
        before, _, rest = current.partition(MARKER_START)
        _, _, after = rest.partition(MARKER_END)
        stripped = before.rstrip() + "\n"
        if after.strip():
            stripped += after.lstrip()
    else:
        stripped = current

    # Build new block
    new_block = ""
    if blocked_domains:
        new_block = "\n" + MARKER_START + "\n"
        for d in blocked_domains:
            new_block += f"127.0.0.1 {d}\n"
            new_block += f"::1       {d}\n"
        new_block += MARKER_END + "\n"

    new_content = stripped.rstrip() + new_block
    if not new_content.endswith("\n"):
        new_content += "\n"

    if new_content == current:
        return False

    if not HOSTS_BACKUP.exists():
        HOSTS_BACKUP.write_text(current)

    write_hosts(new_content)
    return True

# ---------- commands ----------

def cmd_tick(_args):
    """One scheduler tick. Called by launchd every 60 seconds."""
    setup_logging()
    config = load_config()
    state = load_state()
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Reset usage at midnight
    if state.get("date") != today:
        state["date"] = today
        state["usage"] = {}
        logging.info(f"New day: {today}")

    # Meter whichever monitored site is in the frontmost window (incl. duck://player)
    active, active_app = detect_active_site(config)
    if active:
        state["usage"][active] = state["usage"].get(active, 0) + TICK_SECONDS
        logging.info(f"Active: {active} (today={state['usage'][active]}s)")

    # If temporarily unlocked, clear blocks and bail
    if state.get("unlock_until", 0) > time.time():
        if update_hosts([]):
            logging.info("Cleared hosts (unlock active)")
        save_state(state)
        return

    # Compute what should be blocked, per site
    blocked_domains = set()
    blocked_apps = set()
    blocked_sites = set()
    night = in_night_window(config.get("night_block"), now)
    for site_key, site in config["sites"].items():
        limit = site.get("daily_limit_minutes", 0) * 60
        over_limit = limit > 0 and state["usage"].get(site_key, 0) >= limit
        night_blocked = night and site.get("night_block", False)
        if over_limit or night_blocked:
            blocked_domains.update(site["domains"])
            blocked_apps.update(site.get("apps", []))
            blocked_sites.add(site_key)

    if update_hosts(sorted(blocked_domains)):
        logging.info(f"Updated hosts. Blocking: {sorted(blocked_domains)}")

    for app in blocked_apps:
        kill_app(app)

    # Active-tab enforcement: hosts can't block embedded CDN streams (e.g.
    # duck://player from googlevideo), so if a blocked site is the frontmost
    # tab, quit the browser showing it. Reopening just gets quit again.
    if active in blocked_sites and active_app:
        kill_app(active_app)
        logging.info(f"Quit {active_app} (blocked {active} was frontmost)")

    save_state(state)

def _fmt_mins(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"

def cmd_status(_args):
    config = load_config()
    state = load_state()
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    same_day = state.get("date") == today

    print("FocusGuard")
    print("=" * 50)

    until = state.get("unlock_until", 0)
    if until > time.time():
        when = datetime.datetime.fromtimestamp(until).strftime("%H:%M:%S")
        print(f"⚠️  UNLOCKED until {when}")

    night_cfg = config.get("night_block")
    if night_cfg:
        in_night = in_night_window(night_cfg, now)
        flag = "🌙 ACTIVE" if in_night else "💤 inactive"
        print(f"Night window {night_cfg['start']}–{night_cfg['end']}: {flag}")

    print(f"Date: {today}")
    print()

    active, _ = detect_active_site(config)
    for key, site in config["sites"].items():
        used = state.get("usage", {}).get(key, 0) if same_day else 0
        limit = site.get("daily_limit_minutes", 0) * 60
        nb = "night-blocked" if site.get("night_block") else ""
        live = " ▶️ active now" if key == active else ""
        if limit > 0:
            remaining = max(0, limit - used)
            mark = "🔴" if used >= limit else "🟢"
            extra = f", {nb}" if nb else ""
            print(f"  {mark} {key}: {_fmt_mins(used)} / {_fmt_mins(limit)} "
                  f"used ({_fmt_mins(remaining)} left{extra}){live}")
        else:
            extra = f" ({nb})" if nb else ""
            print(f"  ⚪ {key}: no daily limit{extra}{live}")

def authenticate():
    """Require the 2FA code (sudo is the first factor). Exits on failure."""
    secret = load_totp()
    if not secret:
        print("2FA not set up. Run: sudo focusguard setup-2fa")
        sys.exit(1)
    code = getpass.getpass("Authenticator code: ")
    if not verify_totp(secret, code):
        print("Wrong code.")
        sys.exit(1)

def cmd_unlock(args):
    authenticate()
    state = load_state()
    state["unlock_until"] = time.time() + args.minutes * 60
    save_state(state)
    update_hosts([])
    print(f"Unlocked for {args.minutes} minutes.")

def cmd_lock(_args):
    state = load_state()
    state["unlock_until"] = 0
    save_state(state)
    # Re-evaluate immediately
    cmd_tick(None)
    print("Locked.")

def cmd_init_config(_args):
    if CONFIG_PATH.exists():
        print(f"Config already exists at {CONFIG_PATH}")
        return
    save_config(copy.deepcopy(DEFAULT_CONFIG))
    print(f"Wrote default config to {CONFIG_PATH}")

def cmd_set_limit(args):
    config = load_config()
    if args.site not in config["sites"]:
        print(f"Unknown site '{args.site}'. Known: {', '.join(config['sites'])}")
        sys.exit(1)
    authenticate()
    config["sites"][args.site]["daily_limit_minutes"] = args.minutes
    save_config(config)
    print(f"{args.site} daily limit set to {args.minutes} min (effect within ~60s).")

def cmd_set_night(args):
    config = load_config()
    authenticate()
    config["night_block"] = {"start": args.start, "end": args.end}
    save_config(config)
    print(f"Night block set to {args.start}–{args.end} (effect within ~60s).")

def cmd_setup_2fa(_args):
    existing = load_totp()
    if existing:
        code = getpass.getpass("Current authenticator code (to re-seed): ")
        if not verify_totp(existing, code):
            print("Wrong code. Re-seed aborted.")
            sys.exit(1)
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    save_totp(secret)
    uri = f"otpauth://totp/FocusGuard?secret={secret}&issuer=FocusGuard"
    print("Second factor enabled.")
    print()
    print("Add this to your authenticator app (manual / 'enter setup key'):")
    print(f"  Account: FocusGuard")
    print(f"  Key:     {secret}")
    print(f"  URI:     {uri}")
    print()
    print(f"Current code: {totp_now(secret)}  (verify it matches your app)")

def cmd_verify_2fa(args):
    secret = load_totp()
    if not secret:
        sys.exit(0)  # no factor configured: nothing to verify
    code = args.code if args.code else getpass.getpass("Authenticator code: ")
    sys.exit(0 if verify_totp(secret, code) else 1)

def cmd_selftest(_args):
    sec = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238 vector
    assert totp_now(sec, t=59, digits=8) == "94287082"
    assert verify_totp(sec, totp_now(sec))
    assert not verify_totp(sec, "000000")
    at = lambda h, m: datetime.datetime(2026, 1, 1, h, m)
    wrap = {"start": "22:30", "end": "08:00"}
    assert in_night_window(wrap, at(23, 0)) and in_night_window(wrap, at(2, 0))
    assert not in_night_window(wrap, at(8, 0)) and not in_night_window(wrap, at(22, 29))
    day = {"start": "09:00", "end": "17:00"}
    assert in_night_window(day, at(12, 0)) and not in_night_window(day, at(8, 0))
    print("selftest ok")

# ---------- main ----------

def hhmm(s: str) -> str:
    datetime.datetime.strptime(s, "%H:%M")
    return s

def non_negative(s: str) -> int:
    n = int(s)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n

def main():
    p = argparse.ArgumentParser(prog="focusguard")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick",        help="Run one daemon tick (used by launchd)")
    sub.add_parser("status",      help="Show usage and current block state")
    sub.add_parser("lock",        help="Re-lock immediately (cancel any unlock)")
    sub.add_parser("init-config", help="Write the default config if missing")
    sub.add_parser("selftest",    help="Run built-in logic checks (TOTP, night window)")
    sub.add_parser("setup-2fa",   help="Enable/re-seed the authenticator second factor")
    u = sub.add_parser("unlock",  help="Temporarily unlock all blocks")
    u.add_argument("minutes", type=int, help="Minutes to unlock for")
    v = sub.add_parser("verify-2fa", help="Verify a 2FA code (exit 0/1); used by uninstall")
    v.add_argument("code", nargs="?", help="Code to verify (prompted if omitted)")
    sl = sub.add_parser("set-limit", help="Change a site's daily limit (needs 2FA code)")
    sl.add_argument("site", help="Site key, e.g. youtube")
    sl.add_argument("minutes", type=non_negative, help="Daily limit in minutes (0 = no limit)")
    sn = sub.add_parser("set-night", help="Change the night-block window (needs 2FA code)")
    sn.add_argument("start", type=hhmm, help="Start time HH:MM, e.g. 22:30")
    sn.add_argument("end",   type=hhmm, help="End time HH:MM, e.g. 08:00")

    args = p.parse_args()

    needs_root = {"tick", "lock", "unlock", "init-config", "setup-2fa",
                  "verify-2fa", "set-limit", "set-night"}
    if args.cmd in needs_root and os.geteuid() != 0:
        print(f"Run with sudo: sudo focusguard {args.cmd}")
        sys.exit(1)

    handler = globals()[f"cmd_{args.cmd.replace('-', '_')}"]
    handler(args)

if __name__ == "__main__":
    main()
