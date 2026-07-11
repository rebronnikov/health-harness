"""Entrypoint: wire the Telegram adapter to the harness and run the polling loop.

    python -m health.main
"""
from __future__ import annotations

import logging

from health.config import require_telegram_config
from health.store.connection import init_db


def main() -> None:
    # Configure logging first so config failures and startup are captured.
    # basicConfig writes to stderr -> launchd's StandardErrorPath when run as a service.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every getUpdates poll at INFO with the full URL (which embeds the
    # bot token) — ~once per 10s. Quiet it: keeps logs small and off-disk-secret.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("health.main")

    require_telegram_config()                    # refuse to run without an allowlist
    init_db()                                    # ensure data/ + create feature tables
    # Imported here (not at top) so config/env is loaded and validated first.
    from health.adapters.telegram_adapter import build_app

    app = build_app()
    log.info("health bot starting: polling Telegram (allowed_updates=['message'])")
    app.run_polling(allowed_updates=["message"])  # long-polling; blocks


if __name__ == "__main__":
    main()
