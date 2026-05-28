#!/bin/bash
# FocusGuard installer.
# Run with: sudo ./install.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Please run with sudo:"
    echo "    sudo ./install.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing FocusGuard..."

# 1. Install main binary
install -m 755 "$SCRIPT_DIR/focusguard.py" /usr/local/bin/focusguard

# 1b. Build and install the Accessibility URL helper (reads the active tab)
swiftc -O "$SCRIPT_DIR/fg-axurl.swift" -o "$SCRIPT_DIR/fg-axurl"
install -m 755 "$SCRIPT_DIR/fg-axurl" /usr/local/bin/fg-axurl

CONSOLE_USER="$(stat -f '%Su' /dev/console)"
CONSOLE_UID="$(id -u "$CONSOLE_USER")"

# 2. Create directories and the agent's active-tab file (user-writable)
mkdir -p /usr/local/etc/focusguard
mkdir -p /usr/local/var/focusguard
touch /usr/local/var/focusguard/active.txt
chown "$CONSOLE_USER" /usr/local/var/focusguard/active.txt

# 3. Init default config (only if absent)
/usr/local/bin/focusguard init-config

# 4. Second factor (authenticator app on a spare phone). This is the gate for
#    unlock / set-limit / set-night / uninstall, on top of sudo.
echo
read -r -p "Set up a 2FA authenticator code now? (recommended) [Y/n] " ANS
if [ "$ANS" != "n" ] && [ "$ANS" != "N" ]; then
    /usr/local/bin/focusguard setup-2fa
fi

# 5. Install the LaunchDaemon
install -m 644 -o root -g wheel \
    "$SCRIPT_DIR/com.focusguard.daemon.plist" \
    /Library/LaunchDaemons/com.focusguard.daemon.plist

# 6. Load it (unload first in case of reinstall)
launchctl unload /Library/LaunchDaemons/com.focusguard.daemon.plist 2>/dev/null || true
launchctl load   /Library/LaunchDaemons/com.focusguard.daemon.plist

# 7. Install the per-user LaunchAgent (reads the active tab inside the GUI session)
install -m 644 -o root -g wheel \
    "$SCRIPT_DIR/com.focusguard.agent.plist" \
    /Library/LaunchAgents/com.focusguard.agent.plist
launchctl bootout   "gui/$CONSOLE_UID/com.focusguard.agent" 2>/dev/null || true
launchctl bootstrap "gui/$CONSOLE_UID" /Library/LaunchAgents/com.focusguard.agent.plist

# 8. Trigger the one-time Accessibility permission prompt for the helper
echo
echo "Requesting Accessibility permission for the usage tracker..."
launchctl asuser "$CONSOLE_UID" /usr/local/bin/fg-axurl --prompt 2>/dev/null || true

echo
echo "✓ FocusGuard installed."
echo
echo "Default config (edit /usr/local/etc/focusguard/config.json to change):"
echo "  • YouTube: 1 hour/day, also blocked 22:30–08:00"
echo "  • Spotify (web player): blocked 22:30–08:00"
echo
echo "Commands:"
echo "  focusguard status              # see usage and what's blocked"
echo "  sudo focusguard unlock 30      # unlock for 30 min (needs 2FA code)"
echo "  sudo focusguard lock           # re-lock immediately"
echo
echo "⚠️  ONE-TIME PERMISSION SETUP"
echo "Usage tracking reads your active browser tab via the Accessibility API."
echo "Grant it under:"
echo "    System Settings → Privacy & Security → Accessibility"
echo "and enable /usr/local/bin/fg-axurl (add it with '+' if not listed)."
echo
echo "Without this, the daily-limit tracking can't see YouTube."
echo "The night-block feature works regardless."
