"""Environment, paths, and version constants.

Importing this module is side-effect-light: it loads `.env` and resolves paths,
but does NOT assert that Telegram credentials are present — offline tools like
`rebuild_db` must be runnable without a bot token or chat id. The bot entrypoint
calls `require_telegram_config()` at startup to enforce the allowlist invariant.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# --- Paths (data/ is a sibling of the health/ package, already gitignored) ---
_REPO_ROOT = Path(__file__).resolve().parent.parent

# Load .env from the repo root by absolute path so it is found regardless of cwd
# (e.g. under launchd, where cwd is "/"). Idempotent; a no-op if .env is absent.
load_dotenv(_REPO_ROOT / ".env")

DATA_DIR = Path(os.environ.get("DATA_DIR", _REPO_ROOT / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "health.sqlite"))

# --- Daily targets ---
# Example defaults for a moderately active adult on a high-protein plan; override
# any of them with TARGET_KCAL / TARGET_PROTEIN_G / TARGET_FIBRE_G / TARGET_CARB_G /
# TARGET_FAT_G in the environment (or `.env`). kcal and fat are ceilings, protein
# and fibre are floors, carbs are tracked — that semantics lives in
# `features/targets.py`; only the numbers live here, because this is already the
# one module that reads the environment.
#
# Why env-with-defaults rather than a targets.toml: five integers that never change
# at runtime don't justify a parser, a missing-file branch and a second config
# surface. If a sixth setting ever needs structure, that is the moment to add a file.
DEFAULT_TARGETS = {"kcal": 2200, "protein_g": 150, "fibre_g": 28, "carb_g": 250, "fat_g": 70}


def _targets() -> dict[str, int]:
    out = {}
    for key, default in DEFAULT_TARGETS.items():
        raw = os.environ.get(f"TARGET_{key.upper()}")
        out[key] = int(raw) if raw else default
    return out


TARGETS = _targets()

# --- Model + provenance versions ---
# The routing model. Sonnet is enough for routing + extraction, which is all the model
# does here (invariant 1: it never computes). Override with MODEL in the environment;
# note that prompt caches are model-scoped, so a change costs one cold prefix.
MODEL = os.environ.get("MODEL", "claude-sonnet-5")
MODEL_VERSION = MODEL              # provenance: which model parsed the message
PARSER_VERSION = "v1"              # provenance: ingest-pipeline version

# --- Telegram (validated lazily, not at import) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def _allowed_chat_id() -> int | None:
    raw = os.environ.get("ALLOWED_CHAT_ID")
    return int(raw) if raw else None


ALLOWED_CHAT_ID = _allowed_chat_id()


def require_telegram_config() -> None:
    """Refuse to run the bot without an explicit allowlist.

    A bot with no allowlist would process anyone's messages — a health-data leak.
    Called by main.py at startup, not at import (so rebuild_db/tests don't need it).
    """
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set; refusing to start the bot.")
    if ALLOWED_CHAT_ID is None:
        raise RuntimeError(
            "ALLOWED_CHAT_ID is not set; refusing to start the bot without an allowlist."
        )
