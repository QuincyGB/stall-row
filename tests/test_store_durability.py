"""Recheck store durability: location resolution (explicit env wins,
volume mount auto-detected, plain default otherwise), the measured
durability flag flowing into /health, and a simulated restart —
close + reopen the store at the same path and the pack balance and
watch baselines must be intact.

The VM has no /data mount and no STALLROW_RECHECK_DB in its env, so
the default-mode tests exercise the real ephemeral path; the volume
tests fake the filesystem probes bound in app.config."""

import logging
import os
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import app.config as config
import app.main as main
from app.config import load_settings, resolve_recheck_db
from app.services import recheck

WEB_URL = "https://93.184.216.34/story"


def _clean_env(monkeypatch):
    monkeypatch.delenv("STALLROW_RECHECK_DB", raising=False)


# --- resolution ---------------------------------------------------------------

def test_explicit_env_wins(monkeypatch, tmp_path):
    target = str(tmp_path / "custom" / "store.db")
    monkeypatch.setenv("STALLROW_RECHECK_DB", target)
    path, persistent = resolve_recheck_db()
    assert path == target
    # tmp_path is a plain directory, not a mount — honestly ephemeral.
    assert persistent is False


def test_explicit_env_under_a_mount_is_persistent(monkeypatch, tmp_path):
    target = str(tmp_path / "store.db")
    monkeypatch.setenv("STALLROW_RECHECK_DB", target)
    # Pretend the directory holding the db is a mounted volume.
    monkeypatch.setattr(config, "_ismount", lambda p: p == str(tmp_path))
    path, persistent = resolve_recheck_db()
    assert path == target
    assert persistent is True


def test_volume_mount_autodetected(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setattr(config, "_isdir", lambda p: p == "/data")
    monkeypatch.setattr(config, "_ismount", lambda p: p == "/data")
    monkeypatch.setattr(config, "_writable", lambda p, mode: True)
    path, persistent = resolve_recheck_db()
    assert path == "/data/recheck.db"
    assert persistent is True


def test_plain_data_dir_is_not_a_volume(monkeypatch):
    # A /data that merely exists (part of an image, say) is on the
    # ephemeral container disk — it must NOT be treated as durable.
    _clean_env(monkeypatch)
    monkeypatch.setattr(config, "_isdir", lambda p: p == "/data")
    monkeypatch.setattr(config, "_ismount", lambda p: False)
    monkeypatch.setattr(config, "_writable", lambda p, mode: True)
    path, persistent = resolve_recheck_db()
    assert path == "recheck.db"
    assert persistent is False


def test_default_is_ephemeral(monkeypatch):
    _clean_env(monkeypatch)
    path, persistent = resolve_recheck_db()
    assert path == "recheck.db"
    assert persistent is False


def test_load_settings_carries_the_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("STALLROW_RECHECK_DB", str(tmp_path / "s.db"))
    s = load_settings()
    assert s.recheck_db_path == str(tmp_path / "s.db")
    assert s.recheck_db_persistent is False


# --- restart survival -----------------------------------------------------------

def test_pack_and_baseline_survive_a_simulated_restart(tmp_path):
    db = str(tmp_path / "nested" / "recheck.db")  # parent auto-created
    store = recheck.WatchStore(db, persistent=True)
    store.save("watch-1", "web", {"status_code": 200, "body_sha256": "abc"},
               "2026-10-25T00:00:00Z")
    pack, created = store.mint_pack("pay-restart", WEB_URL)
    assert created
    store.reserve_credit(pack["pack_id"], WEB_URL)
    store.close()

    reopened = recheck.WatchStore(db, persistent=True)
    try:
        got = reopened.get_pack(pack["pack_id"])
        assert got is not None
        assert got["credits_total"] == 250
        assert got["credits_remaining"] == 249
        baseline = reopened.get("watch-1")
        assert baseline is not None
        assert baseline["fingerprint"]["body_sha256"] == "abc"
    finally:
        reopened.close()


def test_fresh_store_logs_that_it_starts_empty(tmp_path, caplog):
    db = str(tmp_path / "fresh.db")
    with caplog.at_level(logging.WARNING, logger="stall-row.recheck"):
        store = recheck.WatchStore(db)
    store.close()
    assert "starting EMPTY" in caplog.text
    # ...and reopening the same file does NOT claim a fresh start.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="stall-row.recheck"):
        store = recheck.WatchStore(db)
    store.close()
    assert "starting EMPTY" not in caplog.text


# --- /health honesty ------------------------------------------------------------

def test_health_reports_ephemeral_by_default():
    client = TestClient(main.app, raise_server_exceptions=False)
    health = client.get("/health").json()
    assert health["ok"] is True
    assert health["routes"] == 25
    # This environment has no volume and no env override, so the
    # measured mode must be the honest one.
    assert health["storage"]["recheck_store"] == "ephemeral"


def test_health_reports_persistent_when_measured_so(monkeypatch):
    durable = replace(main.settings, recheck_db_persistent=True,
                      recheck_db_path="/data/recheck.db")
    monkeypatch.setattr(main, "settings", durable)
    client = TestClient(main.app, raise_server_exceptions=False)
    health = client.get("/health").json()
    assert health["storage"] == {
        "recheck_store": "persistent",
        "recheck_db_path": "/data/recheck.db",
    }
