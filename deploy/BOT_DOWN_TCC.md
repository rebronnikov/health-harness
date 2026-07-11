# Bot down: macOS TCC blocks launchd from `~/Documents`

**Resolution:** Moving the checkout outside `~/Documents` restored polling.
The virtual environment was recreated, `.env` and `data/` were carried over,
and the LaunchAgent was reinstalled from the new checkout.

_(Original report below.)_

**Impact:** The Telegram bot does not poll, so nothing sent to it is logged. All code
(including the new food/macros feature) is committed and verified — it will work the moment
the bot process runs again.

---

## TL;DR

The checkout was under `~/Documents`. macOS **TCC** (privacy
protection) denies the **launchd-spawned** bot process access to anything under `~/Documents`.
So launchd can't read the working dir, `.env`, the venv, or write its logs — the agent dies
before Python starts. Running the bot by hand works because an interactive shell session has
Documents access that the launchd context does not.

The fix is to get the bot **out of TCC's way** — either grant Full Disk Access to its Python
binary, or move the project to a non-protected folder. Nothing in the code is wrong.

---

## Symptom (what you'll see)

```
$ scripts/bot.sh status
    state = spawn scheduled
    last exit code = 78: EX_CONFIG      # EX_CONFIG, with NO traceback in the log
```
- `runs` keeps climbing (KeepAlive retries every 10s); the bot never stays up.
- `data/logs/bot.err.log` shows no *fresh* entries — its last line records a
  network error. (That crash was incidental; the TCC denial is why it never recovered.)
- `pgrep -fl health.main` → nothing.

## Root cause

`exit 78` + **no output** = launchd couldn't even open `StandardErrorPath`
(`data/logs/bot.err.log` inside the checkout) because it was under `~/Documents`.
Re-pointing the logs to `/tmp` surfaced the underlying error:

```
/bin/bash: ~/Documents/example-checkout/scripts/run-bot.sh: Operation not permitted
shell-init: error retrieving current directory: getcwd: cannot access parent directories:
            Operation not permitted
```

`Operation not permitted` (EPERM) on `~/Documents` = a TCC denial against the launchd process.

## Things that look like the cause but AREN'T (don't chase these again)

- **Not the code.** Foreground run with launchd's *exact* minimal env starts cleanly:
  ```
  env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
      ~/Documents/example-checkout/.venv/bin/python -m health.main
  # -> "health bot starting: polling Telegram" / "Application started"
  ```
- **Not code-signing / the Homebrew-python symlink (LWCR).** Launching via `/bin/bash` instead
  of the venv python failed *identically* — the failure is the path access, not the binary.
- **Not a stale plist / launchd cache.** A full `bootout` + `bootstrap` (`bot.sh install`) did
  not help. Installed and committed plists match.
- **Not the log directory perms.** `data/logs/` exists and is writable by your shell; the block
  is TCC on the launchd context, not Unix permissions.

## Why it worked before and broke now

It initially worked after installation from a shell session with access to Documents.
A reboot / security-state change after the crash dropped that grant for the
launchd context, and TCC has denied it ever since.

---

## Fix options (pick one when you return to this)

### Option A — Grant Full Disk Access (fastest, keep project in place)
1. **System Settings → Privacy & Security → Full Disk Access**.
2. Click **+**, then **⌘⇧G** and paste the bot's real Python binary:
   ```
   /opt/homebrew/Cellar/python@3.12/3.12.13/Frameworks/Python.framework/Versions/3.12/bin/python3.12
   ```
   (That's what `.venv/bin/python` resolves to — confirm with
   `readlink -f .venv/bin/python` in case Homebrew bumped the patch version.)
3. Toggle it **ON**.
4. `scripts/bot.sh install` (re-bootstrap), then verify (below).
- Caveat: TCC attribution for a launchd-run CLI binary can be finicky; if it still denies,
  prefer Option B. Granting FDA to a shared `python3.12` also widens access for any tool using
  that binary — acceptable on a personal machine, but worth knowing.

### Option B — Relocate out of `~/Documents` (most robust, survives OS updates)
Move the checkout to a folder outside the protected directories. Steps:
1. `bot.sh uninstall`.
2. Copy the tree to the new path. **Recreate the venv** — its paths are absolute and won't
   survive a move: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
   Carry over `.env` and `data/` (the JSONL system-of-record + sqlite).
3. Update the hardcoded checkout paths in **`scripts/bot.sh`** (`REPO=`)
   and **`deploy/local.health-bot.plist`** (ProgramArguments, WorkingDirectory, log paths).
4. `bot.sh install` from the new location, then verify.
- `~/Library/Application Support/...` also works but is awkward to `cd` into; a top-level
  `~/Projects` or `~/Developer` dir is simpler.

## Verify after fixing
```
scripts/bot.sh status      # state = running, a real pid, NO exit 78
pgrep -fl health.main      # one process
scripts/bot.sh logs        # fresh "health bot starting: polling Telegram" with today's date
```
Then send a Telegram message (e.g. "for breakfast I had 60g of oats" after saving oats) and
confirm a reply + a row in `data/food_log.jsonl`.

## Current state (already done)
- Deployment experiment fully reverted: `deploy/local.health-bot.plist` is back to the
  original (direct venv-python invocation, logs under `data/logs/`); installed copy matches.
- The food feature is implemented and verified; see
