#!/usr/bin/env bash
#
# Manage the health bot as a per-user launchd LaunchAgent (macOS).
#
#   ./scripts/bot.sh install     render the plist for THIS checkout, (re)load, start
#   ./scripts/bot.sh uninstall   stop and remove the LaunchAgent
#   ./scripts/bot.sh restart     kill + restart the running bot (needed after code edits)
#   ./scripts/bot.sh status      show state / pid / last exit code
#   ./scripts/bot.sh logs        tail the error log (Ctrl-C to stop)
#
# Paths: launchd requires absolute paths and cannot expand $HOME, so `install`
# renders deploy/health-bot.plist.template with this checkout's location (derived
# from this script's own path) and its .venv python. Set LABEL to run more than
# one instance; the default is `local.health-bot`.
#
# The bot polls Telegram only while the Mac is awake and you're logged in.
# A sleeping/closed-lid Mac will not poll; it reconnects on wake and Telegram
# holds undelivered messages for ~24h. Re-run `install` after editing the
# template (launchd caches the plist at bootstrap; restart alone won't re-read it).
set -euo pipefail

LABEL="${LABEL:-local.health-bot}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
TEMPLATE="$REPO/deploy/health-bot.plist.template"
DST_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
TARGET="$DOMAIN/$LABEL"
LOG_DIR="$REPO/data/logs"

render() {
  sed -e "s|__LABEL__|$LABEL|g" -e "s|__REPO__|$REPO|g" -e "s|__PYTHON__|$PYTHON|g" "$TEMPLATE"
}

case "${1:-}" in
  install)
    [ -x "$PYTHON" ] || { echo "no python at $PYTHON — create the venv first" >&2; exit 1; }
    mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
    render > "$DST_PLIST"
    chmod 644 "$DST_PLIST"
    plutil -lint "$DST_PLIST" > /dev/null
    launchctl bootout "$TARGET" 2>/dev/null || true   # drop any prior copy first
    launchctl bootstrap "$DOMAIN" "$DST_PLIST"
    launchctl enable "$TARGET"
    launchctl kickstart -p "$TARGET"
    echo "installed + started $LABEL from $REPO"
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
  render)
    render                                            # print the plist; never installs
    ;;
  *)
    echo "usage: $0 {install|uninstall|restart|status|logs|render}" >&2
    exit 2
    ;;
esac
