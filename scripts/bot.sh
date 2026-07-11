#!/usr/bin/env bash
#
# Manage the Health Telegram bot as a per-user launchd LaunchAgent.
#
#   ./scripts/bot.sh install     copy plist, (re)load, and start now
#   ./scripts/bot.sh uninstall   stop and remove the LaunchAgent
#   ./scripts/bot.sh restart     kill + restart the running bot
#   ./scripts/bot.sh status      show state / pid / last exit code
#   ./scripts/bot.sh logs        tail the error log (Ctrl-C to stop)
#
# The bot polls Telegram only while the Mac is awake and you're logged in.
# A sleeping/closed-lid Mac will not poll; it reconnects on wake and Telegram
# holds undelivered messages for ~24h. Re-run `install` after editing the plist
# (launchd caches it at bootstrap; restart alone won't re-read it).
set -euo pipefail

LABEL="local.health-bot"
REPO="~/Projects/health-harness"
SRC_PLIST="$REPO/deploy/$LABEL.plist"
DST_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"
LOG_DIR="$REPO/data/logs"

case "${1:-}" in
  install)
    mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
    cp "$SRC_PLIST" "$DST_PLIST"
    chmod 644 "$DST_PLIST"
    launchctl bootout "$TARGET" 2>/dev/null || true   # drop any prior copy first
    launchctl bootstrap "$DOMAIN" "$DST_PLIST"
    launchctl enable "$TARGET"
    launchctl kickstart -p "$TARGET"
    echo "installed + started $LABEL"
    ;;
  uninstall)
    launchctl bootout "$TARGET" 2>/dev/null || true
    rm -f "$DST_PLIST"
    echo "uninstalled $LABEL"
    ;;
  restart)
    launchctl kickstart -k "$TARGET"
    echo "restarted $LABEL"
    ;;
  status)
    launchctl print "$TARGET" | grep -E "state|pid|last exit code|program =" || true
    ;;
  logs)
    tail -n 50 -f "$LOG_DIR/bot.err.log"
    ;;
  *)
    echo "usage: $0 {install|uninstall|restart|status|logs}" >&2
    exit 2
    ;;
esac
