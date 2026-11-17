"""A vault read in the gateway writes one audit row: name, caller, purpose, never the value."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.config.loader_builders import _build_dashboard_config
from kiro_crew.secrets import read_audit
from kiro_crew.secrets.vault import SecretVault

_VALUE = "value-9f3e1c7a-not-a-name"


class _Log:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[dict] = []
        self.fail = fail

    def log_api_access(self, **row: object) -> None:
        if self.fail:
            raise OSError("audit log unwritable")
        self.rows.append(row)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _rig(_floor_monkeypatch, tmp_path):
    log = _Log()
    clock = _Clock()
    state = {"on": True}

    def _snapshot():
        return SimpleNamespace(dashboard=SimpleNamespace(audit_secret_reads=state["on"]))

    _floor_monkeypatch.setattr(live, "snapshot", _snapshot)
    _floor_monkeypatch.setattr(read_audit, "_sel", lambda: log)
    _floor_monkeypatch.setattr(read_audit, "_clock", clock)
    _floor_monkeypatch.setattr(read_audit, "_seen", {})
    _floor_monkeypatch.setattr(read_audit, "_loaded", None, raising=False)
    vault = SecretVault(tmp_path)
    vault.set_sync("API_TOKEN", _VALUE)
    vault.set_sync("OTHER", _VALUE + "-2")
    return SimpleNamespace(vault=vault, log=log, clock=clock, state=state)


def _read_api_token(vault: SecretVault):
    return vault.get("API_TOKEN")


def test_a_read_writes_one_row_with_name_caller_and_purpose(_rig) -> None:
    assert _read_api_token(_rig.vault).reveal() == _VALUE
    assert _rig.log.rows == [
        {
            "caller": __name__,
            "operation": "secret_read",
            "outcome": "ok",
            "source": "gateway",
            "resources": "secret=API_TOKEN purpose=_read_api_token",
        }
    ]
    assert "9f3e1c" not in str(_rig.log.rows)


def test_every_read_method_audits_found_names_only(_rig) -> None:
    v = _rig.vault
    v.get("MISSING")
    v.get_with_revision("API_TOKEN")
    v.get_many(["OTHER", "MISSING"])
    v.get_items_many(["API_TOKEN", "OTHER"])
    v.get_items_readable(["OTHER"])
    names = [row["resources"].split()[0] for row in _rig.log.rows]
    # One row per (name, caller, purpose): this test function is one purpose.
    assert names == ["secret=API_TOKEN", "secret=OTHER"]


def test_repeat_reads_share_a_row_that_counts_them(_rig) -> None:
    for _ in range(5):
        _read_api_token(_rig.vault)
    assert len(_rig.log.rows) == 1
    _rig.clock.now += read_audit.WINDOW_SECS - 1
    _read_api_token(_rig.vault)
    assert len(_rig.log.rows) == 1
    _rig.clock.now += 1
    _read_api_token(_rig.vault)
    assert len(_rig.log.rows) == 2
    assert _rig.log.rows[1]["resources"] == ("secret=API_TOKEN purpose=_read_api_token repeats=5")


def test_tracked_triples_stay_bounded(_rig, _floor_monkeypatch) -> None:
    _floor_monkeypatch.setattr(read_audit, "MAX_TRACKED", 2)
    for name in ("A", "B", "C"):
        read_audit.note_reads((name,))
    assert len(read_audit._seen) == 2
    assert len(_rig.log.rows) == 3


def test_a_forgotten_triple_writes_its_absorbed_reads_first(_rig, _floor_monkeypatch) -> None:
    _floor_monkeypatch.setattr(read_audit, "MAX_TRACKED", 2)
    for name in ("A", "A", "A", "B", "C"):
        read_audit.note_reads((name,))
    secrets = [row["resources"].split()[0] for row in _rig.log.rows]
    assert secrets == ["secret=A", "secret=B", "secret=A", "secret=C"]
    # The forgotten triple's row names the same caller and purpose as its first row.
    first, forgotten = _rig.log.rows[0], _rig.log.rows[2]
    assert forgotten["caller"] == first["caller"]
    assert forgotten["resources"] == first["resources"] + " repeats=2"
    assert len(read_audit._seen) == 2


def test_a_long_name_is_kept_as_a_checksum_and_a_clipped_name(_rig) -> None:
    head = "N" * 200_000
    read_audit.note_reads((head + "A", head + "B", head + "A"))
    # Two names, two triples, though they share their first 200,000 characters.
    assert len(_rig.log.rows) == 2
    assert len(read_audit._seen) == 2
    # What the tracker keeps does not grow with the name.
    assert len(repr(read_audit._seen)) < 2_000
    for row in _rig.log.rows:
        secret = row["resources"].split()[0]
        assert secret.startswith("secret=" + "N" * read_audit.MAX_NAME_CHARS + "...#")
        assert len(secret) < read_audit.MAX_NAME_CHARS + 30
    assert _rig.log.rows[0]["resources"] != _rig.log.rows[1]["resources"]


def test_a_bare_method_on_a_worker_thread_never_names_the_thread_pool(_rig) -> None:
    asyncio.run(asyncio.to_thread(_rig.vault.get, "API_TOKEN"))
    assert [(row["caller"], row["resources"]) for row in _rig.log.rows] == [
        ("unknown", "secret=API_TOKEN purpose=unknown")
    ]


def test_a_function_handed_to_a_worker_thread_is_the_caller(_rig) -> None:
    asyncio.run(asyncio.to_thread(_read_api_token, _rig.vault))
    assert [(row["caller"], row["resources"]) for row in _rig.log.rows] == [
        (__name__, "secret=API_TOKEN purpose=_read_api_token")
    ]


def test_the_oauth_rollback_read_names_its_handler(_rig) -> None:
    from kiro_crew.dashboard.handlers import connections

    rolled = asyncio.run(
        connections._rollback_secret(
            _rig.vault, "API_TOKEN", slug="demo", written="not-the-value", previous=None
        )
    )
    assert rolled is False
    assert [(row["caller"], row["resources"]) for row in _rig.log.rows] == [
        (
            "kiro_crew.dashboard.handlers.connections",
            "secret=API_TOKEN purpose=_rollback_secret.<locals>.<lambda>",
        )
    ]


def test_off_writes_nothing(_rig, _floor_monkeypatch) -> None:
    _rig.state["on"] = False
    _read_api_token(_rig.vault)
    _floor_monkeypatch.setattr(live, "snapshot", lambda: None)
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", lambda: _config(False))
    _read_api_token(_rig.vault)
    assert _rig.log.rows == []


def _config(on: bool) -> SimpleNamespace:
    return SimpleNamespace(dashboard=SimpleNamespace(audit_secret_reads=on))


def test_with_no_live_snapshot_the_loaded_config_decides(_rig, _floor_monkeypatch) -> None:
    loads: list[bool] = []
    state = {"on": True}

    def _load():
        loads.append(state["on"])
        return _config(state["on"])

    _floor_monkeypatch.setattr(live, "snapshot", lambda: None)
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", _load)
    _read_api_token(_rig.vault)
    _rig.vault.get("OTHER")
    assert [row["resources"].split()[0] for row in _rig.log.rows] == [
        "secret=API_TOKEN",
        "secret=OTHER",
    ]
    # One disk load serves every read inside the reuse window.
    assert loads == [True]
    state["on"] = False
    _rig.clock.now += read_audit.CONFIG_TTL_SECS - 1
    read_audit.note_reads(("THIRD",))
    assert len(_rig.log.rows) == 3
    _rig.clock.now += 1
    read_audit.note_reads(("FOURTH",))
    assert len(_rig.log.rows) == 3
    assert loads == [True, False]


def test_a_config_load_that_fails_reads_as_off_once_per_window(_rig, _floor_monkeypatch) -> None:
    calls: list[int] = []

    def _load():
        calls.append(1)
        raise OSError("config unreadable")

    _floor_monkeypatch.setattr(live, "snapshot", lambda: None)
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", _load)
    assert _read_api_token(_rig.vault).reveal() == _VALUE
    assert _rig.vault.get_many(["OTHER"])["OTHER"].reveal() == _VALUE + "-2"
    assert _rig.log.rows == []
    assert len(calls) == 1


def test_a_vault_read_made_by_the_config_load_is_skipped(_rig, _floor_monkeypatch) -> None:
    def _load():
        _rig.vault.get("OTHER")
        return _config(True)

    _floor_monkeypatch.setattr(live, "snapshot", lambda: None)
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", _load)
    _read_api_token(_rig.vault)
    assert [row["resources"].split()[0] for row in _rig.log.rows] == ["secret=API_TOKEN"]


def test_no_gateway_code_hands_a_bare_vault_method_to_a_worker_thread() -> None:
    # Such a read names `unknown`; the call site wraps it in a function of its own.
    bare = re.compile(r"to_thread\(\s*[\w.]*vault[\w.]*\.get\w*\s*,")
    root = Path(read_audit.__file__).resolve().parents[1]
    hits = [
        str(path.relative_to(root))
        for path in sorted(root.rglob("*.py"))
        if path.name != "read_audit.py" and bare.search(path.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_a_failing_audit_never_fails_the_read(_rig) -> None:
    _rig.log.fail = True
    assert _read_api_token(_rig.vault).reveal() == _VALUE
    assert _rig.vault.get_many(["OTHER"])["OTHER"].reveal() == _VALUE + "-2"


def test_setting_defaults_off_and_reads_from_config() -> None:
    assert _build_dashboard_config(set(), {}).audit_secret_reads is False
    assert _build_dashboard_config(set(), {"audit_secret_reads": True}).audit_secret_reads is True
