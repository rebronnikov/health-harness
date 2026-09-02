# Running it for real

The bot is a long-polling Telegram process. It runs anywhere Python 3.11+ runs; the
shipped service files are for macOS `launchd`.

## 1. Configure

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env            # then fill in the three values below
```

| value | what it is |
|---|---|
| `ANTHROPIC_API_KEY` | the routing model's key |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `ALLOWED_CHAT_ID` | **the security boundary.** The one chat the bot answers; every other chat is dropped silently, so the bot's existence is never confirmed to a stranger. The bot refuses to start without it. Send your bot a message, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`. |

Optional: `MODEL`, `DATA_DIR`, and the `TARGET_*` daily targets — see `.env.example`.

Seed the catalog (or save foods by message later): `.venv/bin/python -m health.seed_catalog`.

## 2. Run

Foreground, for a first look:

```bash
.venv/bin/python -m health.main
```

As a resident macOS LaunchAgent:

```bash
./scripts/bot.sh install     # render the plist for THIS checkout, load, start
./scripts/bot.sh status      # state / pid / last exit
./scripts/bot.sh logs        # tail data/logs/bot.err.log
./scripts/bot.sh restart     # after editing code — launchd runs a resident copy
```

`install` renders `deploy/health-bot.plist.template` with the checkout's own path and
`.venv` python, because launchd needs absolute paths and cannot expand `$HOME`. The
label is `local.health-bot` (override with `LABEL=`). `KeepAlive` relaunches only after
a crash, so `./scripts/bot.sh uninstall` — or a plain `launchctl bootout` — stays
stopped, which is what the rebuild step below relies on.

**Don't put the checkout under `~/Documents`, `~/Desktop` or `~/Downloads`.** macOS
privacy protection (TCC) denies a launchd-spawned process access to those folders, and
the agent dies before Python starts with `exit 78` and an empty log. The full diagnosis,
including the things that look like the cause but aren't, is in
[`incidents/macos-tcc.md`](incidents/macos-tcc.md).

## 3. Operate

- **Logs:** `data/logs/bot.err.log` carries all application logging (stderr). The
  `httpx` logger is silenced to WARNING because Telegram's `getUpdates` URLs embed the
  bot token. The log deliberately carries no message text.
- **What actually happened in a conversation:** `data/messages/<date>.jsonl` — every
  inbound message, every tool call with its result or error, every web lookup, every
  reply, and one `usage` line per API call.
- **What it costs:** `.venv/bin/python -m health.report_usage --days 7` totals the
  `usage` lines: tokens, the cache read/write split, calls per message, and a cost.
- **Rebuilding the projection** after editing the JSONL by hand: stop the bot cleanly
  (`./scripts/bot.sh uninstall`), `python -m health.rebuild_db`, then
  `./scripts/bot.sh install`. The SQLite file is disposable; the JSONL is not.
- **Backups:** `data/` is the whole state — the JSONL system of record, the projection,
  the transcript and the logs. It is gitignored. Back it up as a directory.

## What's not here

Single user by construction. Auth is the chat-id allowlist and nothing more. No
migrations beyond rebuild-from-JSONL. Deployment files are macOS-only. Pending estimates,
conversation history and the numbered listing behind "delete 2" are in-process and lost
on restart (by design; the user just re-sends).
