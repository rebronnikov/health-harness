"""Registry wiring: the shared assembly the harness and store depend on. Cheap
guards that catch a mis-wired feature (dup tool name, tool with no executor).
"""
import sqlite3

import pytest

from health.core import registry


def test_tool_names_are_unique():
    names = [t["name"] for t in registry.all_tools()]
    assert len(names) == len(set(names))


def test_every_tool_has_an_executor():
    tool_names = {t["name"] for t in registry.all_tools()}
    assert tool_names <= set(registry.all_executors())          # each routed tool is dispatchable


def test_feature_by_domain_resolves_and_raises():
    assert registry.feature_by_domain("steps").name == "steps"
    with pytest.raises(KeyError):
        registry.feature_by_domain("no_such_domain")


def test_ingesting_features_are_the_domain_projectors():
    domains = {f.domain for f in registry.ingesting_features()}
    assert domains == {"steps", "weight", "measurements", "food_catalog", "food_log",
                       "corrections"}


def test_corrections_replays_last():
    """rebuild_db replays domains in FEATURES order, and a correction must land after the
    rows it corrects. Registry order is load-bearing, not cosmetic — see corrections.py."""
    assert registry.ingesting_features()[-1].domain == "corrections"


def test_init_schema_is_idempotent():
    conn = sqlite3.connect(":memory:")
    registry.init_schema(conn)
    registry.init_schema(conn)                                  # all DDL is IF NOT EXISTS
    conn.close()
