"""Self-checking safety net: prove the suite is writing to a throwaway location,
never the owner's real data/. If a future change breaks the conftest isolation, THIS
fails loudly instead of the suite silently mutating live health data.
"""
from pathlib import Path

import health
from health import config
from health.store import connection, msg_log, writer
from health.store.connection import get_conn

REAL_DATA_DIR = Path(health.__file__).resolve().parent.parent / "data"


def test_session_config_paths_are_not_the_real_data_dir():
    # Set from env in conftest BEFORE the first health import (the primary guarantee).
    assert config.DATA_DIR != REAL_DATA_DIR
    assert REAL_DATA_DIR not in config.DB_PATH.parents


def test_per_test_writes_target_a_tmp_dir(tmp_path):
    assert writer.DATA_DIR == tmp_path                          # isolate fixture redirected it
    assert msg_log.DATA_DIR == tmp_path                         # ...including the transcript
    assert connection.DB_PATH.parent == tmp_path
    assert REAL_DATA_DIR not in writer.DATA_DIR.parents


def test_live_connection_file_is_under_tmp(tmp_path):
    files = [r[2] for r in get_conn().execute("PRAGMA database_list")]  # (seq, name, file)
    main_db = Path(files[0])
    assert main_db.parent == tmp_path
    assert REAL_DATA_DIR not in main_db.parents
