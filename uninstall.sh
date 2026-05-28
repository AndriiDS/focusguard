#!/bin/bash
# FocusGuard uninstaller. Restores /etc/hosts.
# Run with: sudo ./uninstall.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Please run with sudo:"
    echo "    sudo ./uninstall.sh"
    exit 1
fi

echo "Uninstalling FocusGuard..."

# Second factor: require a valid authenticator code before tearing down
if [ -f /usr/local/etc/focusguard/totp.secret ]; then
    if ! /usr/local/bin/focusguard verify-2fa; then
        echo "Wrong code. Uninstall aborted."
        exit 1
    fi
fi

# Stop the daemon
launchctl unload /Library/LaunchDaemons/com.focusguard.daemon.plist 2>/dev/null || true
rm -f /Library/LaunchDaemons/com.focusguard.daemon.plist

# Stop the per-user agent
CONSOLE_UID="$(id -u "$(stat -f '%Su' /dev/console)")"
launchctl bootout "gui/$CONSOLE_UID/com.focusguard.agent" 2>/dev/null || true
rm -f /Library/LaunchAgents/com.focusguard.agent.plist

# Restore hosts file from backup if we have one
if [ -f /etc/hosts.focusguard-backup ]; then
    cp /etc/hosts.focusguard-backup /etc/hosts
    rm -f /etc/hosts.focusguard-backup
    dscacheutil -flushcache 2>/dev/null || true
    killall -HUP mDNSResponder 2>/dev/null || true
    echo "  • /etc/hosts restored"
else
    # No backup — at least strip our managed block in-place
    sed -i.bak '/# FOCUSGUARD_START/,/# FOCUSGUARD_END/d' /etc/hosts || true
    rm -f /etc/hosts.bak
    dscacheutil -flushcache 2>/dev/null || true
    killall -HUP mDNSResponder 2>/dev/null || true
    echo "  • managed block removed from /etc/hosts"
fi

rm -f  /usr/local/bin/focusguard
rm -f  /usr/local/bin/fg-axurl
rm -rf /usr/local/etc/focusguard
rm -rf /usr/local/var/focusguard

echo "✓ Uninstalled."
