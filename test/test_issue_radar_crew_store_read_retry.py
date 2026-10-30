"""Issue Radar crew-store reads ride out a Windows sharing violation.

``read_settings``, ``list_crews`` and ``read_crew`` fold any ``OSError`` into a
default, a skipped record or ``None``. On Windows a read that meets the store's own
``atomic_write`` replace fails with ``PermissionError`` for a moment, so a bare read
would hand crews the default 48 h claim TTL, drop a live crew from the roster, or
call a present crew unknown. The reads go through ``read_bytes_with_retry``.

The contention is simulated: ``platform_compat.IS_WINDOWS`` is forced on and the
helper's single-read primitive fails with ``PermissionError`` a set number of
times, so every runner proves the Windows arm without a real lock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_crew_log,
)

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs

OWNER, REPO = "acme", "widgets"


@pytest.fixture
def contended(monkeypatch):
    """Make the next ``n`` reads of any path fail as a Windows sharing violation."""
    monkeypatch.setattr(atomic_write_module, "_REPLACE_BACKOFF_SECONDS", 0)
    real = atomic_write_module._read_bytes
    remaining = {"n": 0}

    def _read(target, max_bytes):
        if remaining["n"] > 0:
            remaining["n"] -= 1
            raise PermissionError(32, "The process cannot access the file", str(target))
        return real(target, max_bytes)

    monkeypatch.setattr(atomic_write_module, "_read_bytes", _read)

    def _arm(n: int) -> None:
        # Set here, after the test's own writes: the store's file lock reads the
        # same flag, and only the read under test should take the Windows arm.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        remaining["n"] = n

    return _arm


def test_settings_read_during_a_replace_returns_the_stored_values(tmp_path: Path, contended):
    cs.write_settings(
        OWNER, REPO, {"claim_ttl_hours": 72, "needs_human_label": "needs: human"}, tmp_path
    )
    contended(2)

    got = cs.read_settings(OWNER, REPO, tmp_path)

    assert got["claim_ttl_hours"] == 72
    assert got["needs_human_label"] == "needs: human"


def test_crew_read_during_a_replace_finds_the_crew(tmp_path: Path, contended):
    crew = cs.create_crew(OWNER, REPO, {"name": "Andromeda"}, tmp_path)
    contended(2)

    got = cs.read_crew(OWNER, REPO, crew["id"], tmp_path)

    assert got is not None and got["name"] == "Andromeda"


def test_roster_read_during_a_replace_keeps_the_crew(tmp_path: Path, contended):
    cs.create_crew(OWNER, REPO, {"name": "Andromeda"}, tmp_path)
    contended(2)

    assert [c["name"] for c in cs.list_crews(OWNER, REPO, tmp_path)] == ["Andromeda"]


def test_a_damaged_settings_file_still_degrades_to_the_defaults(tmp_path: Path):
    cs.settings_path(OWNER, REPO, tmp_path).write_text("{not json", encoding="utf-8")

    assert cs.read_settings(OWNER, REPO, tmp_path) == dict(cs.DEFAULT_SETTINGS)


def test_a_damaged_crew_record_is_still_unknown(tmp_path: Path):
    crew = cs.create_crew(OWNER, REPO, {"name": "Andromeda"}, tmp_path)
    cs.crew_path(OWNER, REPO, crew["id"], tmp_path).write_bytes(b"\xff\xfe not utf-8")

    assert cs.read_crew(OWNER, REPO, crew["id"], tmp_path) is None
    assert cs.list_crews(OWNER, REPO, tmp_path) == []


def test_a_settings_read_that_outlasts_the_budget_still_returns_a_value(tmp_path: Path, contended):
    """The retry is bounded; past it the old fold still answers, never raises."""
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 72}, tmp_path)
    contended(atomic_write_module._REPLACE_MAX_ATTEMPTS + 1)

    assert cs.read_settings(OWNER, REPO, tmp_path) == dict(cs.DEFAULT_SETTINGS)


def test_the_stored_settings_document_is_plain_json(tmp_path: Path):
    """Pins the format the reader decodes: UTF-8 JSON written by ``write_settings``."""
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 72}, tmp_path)
    raw = cs.settings_path(OWNER, REPO, tmp_path).read_bytes()

    assert json.loads(raw.decode("utf-8"))["claim_ttl_hours"] == 72
