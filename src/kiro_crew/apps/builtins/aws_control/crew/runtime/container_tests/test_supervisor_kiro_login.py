"""Tests for ``container.supervisor.kiro_login`` -- kiro-cli's own login check.

``kiro-cli acp`` validates its own credential store before it offers an ACP
handshake, so a container whose vault is correctly seeded still exits ``rc=1`` "You
are not logged in" and answers every dashboard turn with a 503. The subject writes
one non-secret sentinel row into that store so the check passes while the vault stays
the only place a credential lives.

These tests pin the row shape against a RECORDED REAL ROW
(``kiro_cli_login_row.json``) rather than against the subject's own idea of the
format, and they pin that the row carries no credential.

What they cannot do is run the binary: it is not in a CI image. So the shape is held
from two sides -- the recorded row here, and the measurements written up in the
subject's module docstring, each taken by writing a row and asking the shipped binary
whether the store then reads as signed in.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from container import common
from container.supervisor import kiro_login as kiro_login_mod
from container.supervisor.kiro_login import (
    AUTH_TABLE,
    SELECT_SQL,
    SENTINEL_ACCESS_TOKEN,
    SENTINEL_ROW_KEY,
    seed_kiro_cli_login,
    sentinel_row,
    store_path,
)

#: kiro-cli's own migration creates this table with no ``IF NOT EXISTS``, which is why
#: the subject never creates it and why this fake stands in for the binary.
_KIRO_SCHEMA = f"CREATE TABLE {AUTH_TABLE} (key TEXT PRIMARY KEY, value TEXT)"

#: A REAL row, read out of the store a signed-in kiro-cli wrote (table ``auth_kv``,
#: key ``kirocli:odic:token``). The two token values are replaced with placeholders;
#: NOTHING else is altered -- the field names, the expiry rendering, the ``oauth_flow``
#: spelling, the region and the scopes list are exactly as kiro-cli wrote them, and
#: those are what this fixture is here to pin.
#:
#: The same 7-name field list, the same ``auth_kv`` statements and the same
#: ``data.sqlite3`` filename are in the string table of the binary the crew image pins
#: (``runtime/Dockerfile``, whose tarball sha256 is recorded there).
#:
#: Inline rather than a data file because a non-.py member under ``crew/runtime/`` owes
#: the image payload a packaging rule (``test_crew_runtime_payload``), and a test
#: fixture is not image payload.
_REAL_ROW: dict[str, object] = {
    "access_token": "REDACTED-ACCESS-TOKEN",
    "expires_at": "2026-09-28T07:56:22.155309005Z",
    "oauth_flow": "DeviceCode",
    "refresh_token": "REDACTED-REFRESH-TOKEN",
    "region": "us-east-1",
    "scopes": [
        "codewhisperer:completions",
        "codewhisperer:analysis",
        "codewhisperer:conversations",
    ],
    "start_url": "https://d-906679a6f2.awsapps.com/start",
}

#: A plausible binary path, built rather than written as a literal so the test carries
#: no absolute POSIX path.
_FAKE_BINARY = str(Path("usr", "local", "bin", "kiro-cli"))


def fake_resolve() -> str:
    return _FAKE_BINARY


class _Completed:
    """The one field the subject reads off a finished subprocess."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def accepting_run(argv, **kw):
    """A binary that reports the store as signed in."""
    return _Completed(0)


def refusing_run(argv, **kw):
    """A binary that does not accept the store."""
    return _Completed(1)


def fake_kiro_cli(path: Path, *, accepts: bool = True):
    """Stand in for the binary: creates its own schema, then answers the check."""
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(list(argv))
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with sqlite3.connect(path) as conn:
                conn.execute(_KIRO_SCHEMA)
        return _Completed(0 if accepts else 1)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def existing_store(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """An env pointing at a store the binary has already created."""
    env = {"HOME": str(tmp_path / "home")}
    db = store_path(env)
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute(_KIRO_SCHEMA)
    return env, db


# --- where the store is ----------------------------------------------------


def test_store_path_prefers_xdg_data_home(tmp_path):
    """The binary reads XDG_DATA_HOME first, so a task that sets it must be followed."""
    xdg = tmp_path / "xdg"
    p = store_path({"XDG_DATA_HOME": str(xdg), "HOME": str(tmp_path / "home")})
    assert p == xdg / "kiro-cli" / "data.sqlite3"


def test_store_path_falls_back_to_home_dot_local_share(tmp_path):
    """With no XDG_DATA_HOME the binary keeps its store under the home directory."""
    home = tmp_path / "home"
    assert store_path({"HOME": str(home)}) == home / ".local" / "share" / "kiro-cli" / (
        "data.sqlite3"
    )


def test_store_path_refuses_when_neither_is_set():
    """A guessed path would look seeded and still fail every turn."""
    with pytest.raises(common.ConfigError) as err:
        store_path({})
    assert "XDG_DATA_HOME" in str(err.value)


# --- the row shape, against the recorded real row --------------------------


def test_row_carries_exactly_the_field_names_a_real_kiro_cli_wrote():
    """The pin that matters: the KEYS, taken from a row kiro-cli itself wrote.

    Not a subset check, and exact in both directions. A row missing one of these keys
    reads as not signed in even when the value would have been null, which is the
    failure this module exists to avoid; an EXTRA key stops the document
    deserialising into the struct the binary declares.
    """
    key, value = sentinel_row()
    assert key == SENTINEL_ROW_KEY == "kirocli:odic:token"
    assert set(json.loads(value)) == set(_REAL_ROW)


def test_row_renders_expiry_the_way_the_real_row_does():
    """RFC3339, UTC, Z-suffixed, with a fractional part -- as recorded."""
    rendered = json.loads(sentinel_row()[1])["expires_at"]
    recorded = str(_REAL_ROW["expires_at"])
    assert rendered.endswith("Z") and recorded.endswith("Z")
    assert "." in rendered and "." in recorded


def test_row_spells_oauth_flow_as_the_real_row_does():
    """A required key that takes no null; the value is the binary's own spelling."""
    assert json.loads(sentinel_row()[1])["oauth_flow"] == _REAL_ROW["oauth_flow"]


def test_the_expiry_is_far_enough_out_to_survive_the_task():
    """The binary re-reads the store on every spawn, so a lapsing row restores the bug.

    Its own refresh margin measures between 60 and 200 seconds and an expired row is
    rejected outright, so the sentinel's expiry is fixed and far out rather than
    tracking anything that moves.
    """
    from datetime import datetime, timedelta, timezone

    expires = datetime.strptime(
        json.loads(sentinel_row()[1])["expires_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
    ).replace(tzinfo=timezone.utc)
    assert expires - datetime.now(timezone.utc) > timedelta(days=3650)


# --- the row carries no credential -----------------------------------------


def test_the_row_carries_no_refresh_token_and_no_account_detail():
    """The store is visible to a spawned shell, so the row holds nothing worth reading.

    Every optional field is null rather than filled from anywhere: a value here would
    be readable by an auto-approved worker acting on untrusted prompt content, which
    is the same route ``build_backend_env`` closes on the environment side.
    """
    document = json.loads(sentinel_row()[1])
    assert document["refresh_token"] is None
    assert document["region"] is None
    assert document["start_url"] is None
    assert document["scopes"] is None


def test_the_access_token_is_a_labelled_placeholder():
    """Whoever finds this row must be able to tell it is not a token."""
    document = json.loads(sentinel_row()[1])
    assert document["access_token"] == SENTINEL_ACCESS_TOKEN
    assert "not-a-credential" in document["access_token"]


def test_the_row_is_a_constant_and_reads_nothing(tmp_path, monkeypatch):
    """Two calls under different vault and environment states give the same bytes.

    The property that makes the row safe: it cannot carry an identity, an account or
    a credential, because it is not built from any.
    """
    first = sentinel_row()
    monkeypatch.setenv("KIRO_IDENTITY", json.dumps({"access_token": "atk-secret"}))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert sentinel_row() == first


def test_no_vault_value_can_reach_the_store(tmp_path, monkeypatch):
    """End to end: a seeded store holds the sentinel and nothing from the delivery."""
    from container.common import Settings
    from container.supervisor.backend import seed_model_identity

    data_home = tmp_path / "data"
    settings = Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home / "config",
        crew_name="test-crew",
        backup_bucket=None,
        backup_prefix="",
    )
    delivered = json.dumps(
        {
            "access_token": "atk-do-not-copy-me",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "provider": "BuilderId",
            "identity": "builder_id",
            "refresh_token": "rtk-do-not-copy-me",
            "profile_arn": "arn:aws:profile/test",
        }
    )
    assert seed_model_identity(settings, source={"KIRO_IDENTITY": delivered}) is True
    env, db = existing_store(tmp_path)
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)

    seed_kiro_cli_login(env=env, run=accepting_run)

    with sqlite3.connect(db) as conn:
        rows = conn.execute(f"SELECT key, value FROM {AUTH_TABLE}").fetchall()
    assert [key for key, _ in rows] == [SENTINEL_ROW_KEY]
    blob = rows[0][1]
    assert "do-not-copy-me" not in blob
    assert "arn:aws:profile" not in blob


# --- the seed, end to end --------------------------------------------------


def test_seed_writes_a_row_the_binarys_own_select_finds(tmp_path, monkeypatch):
    """The whole point: after this, kiro-cli's pre-handshake check has something."""
    env = {"HOME": str(tmp_path / "home")}
    db = store_path(env)
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)

    written = seed_kiro_cli_login(env=env, run=fake_kiro_cli(db))

    assert written == db
    with sqlite3.connect(db) as conn:
        row = conn.execute(SELECT_SQL, (SENTINEL_ROW_KEY,)).fetchone()
    assert row is not None
    assert json.loads(row[0])["access_token"] == SENTINEL_ACCESS_TOKEN


def test_the_schema_call_is_skipped_when_the_store_already_exists(tmp_path, monkeypatch):
    """Two binary calls at most, and the first is only for a schema that is absent.

    With the file already there the only call is the acceptance probe, so a warm start
    pays one subprocess rather than two.
    """
    env, _ = existing_store(tmp_path)
    calls = []

    def run(argv, **kw):
        calls.append(list(argv))
        return _Completed(0)

    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    seed_kiro_cli_login(env=env, run=run)
    assert calls == [[_FAKE_BINARY, *kiro_login_mod.LOGIN_CHECK_ARGV]]


def test_the_binary_is_asked_to_accept_the_store_after_the_write(tmp_path, monkeypatch):
    """The check that makes the row shape a verified claim, not a remembered one.

    A cold start runs the binary twice: once so it lays down its own schema, once after
    the row is written to answer whether it accepts it.
    """
    env = {"HOME": str(tmp_path / "home")}
    db = store_path(env)
    run = fake_kiro_cli(db)
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)

    seed_kiro_cli_login(env=env, run=run)

    assert run.calls == [[_FAKE_BINARY, *kiro_login_mod.LOGIN_CHECK_ARGV]] * 2


def test_a_store_the_binary_refuses_is_a_startup_refusal(tmp_path, monkeypatch):
    """Reading the row back cannot fail, so this is what catches a moved contract.

    A KIRO_VERSION bump that changes the key spelling, the field set or the expiry rule
    would otherwise leave the seed reporting success while the relay refuses every turn.
    """
    env, _ = existing_store(tmp_path)
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    with pytest.raises(common.ConfigError) as err:
        seed_kiro_cli_login(env=env, run=refusing_run)
    assert "does not accept" in str(err.value)
    assert "pinned version moved" in str(err.value)


def test_a_binary_that_cannot_be_run_is_a_startup_refusal(tmp_path, monkeypatch):
    """The relay runs this same check before every handshake."""
    env, _ = existing_store(tmp_path)

    def exploding_run(argv, **kw):
        raise OSError("exec format error")

    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    with pytest.raises(common.ConfigError) as err:
        seed_kiro_cli_login(env=env, run=exploding_run)
    assert "could not run kiro-cli" in str(err.value)


def test_seed_refuses_when_kiro_cli_is_not_on_path(tmp_path, monkeypatch):
    """A misbuilt image fails at startup, not on the first turn."""
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", lambda: None)
    with pytest.raises(common.ConfigError) as err:
        seed_kiro_cli_login(env={"HOME": str(tmp_path / "home")}, run=accepting_run)
    assert "could not be resolved" in str(err.value)


def test_seed_refuses_when_the_store_has_no_auth_table(tmp_path, monkeypatch):
    """Fail closed rather than fall through to the 503 this seed removes.

    A store file that exists without ``auth_kv`` means this image's kiro-cli keeps its
    credential somewhere the measurement did not find, and a silent success there
    would be a container that starts and then refuses every turn.
    """
    env = {"HOME": str(tmp_path / "home")}
    db = store_path(env)
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    with pytest.raises(common.ConfigError) as err:
        seed_kiro_cli_login(env=env, run=accepting_run)
    assert AUTH_TABLE in str(err.value)


def test_seed_refuses_when_the_binary_leaves_no_store_behind(tmp_path, monkeypatch):
    """The store is located the way kiro-cli locates it; a miss means a wrong path."""
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    with pytest.raises(common.ConfigError) as err:
        seed_kiro_cli_login(env={"HOME": str(tmp_path / "home")}, run=accepting_run)
    assert "did not create" in str(err.value)


def test_seed_refuses_when_the_store_does_not_keep_the_row(tmp_path, monkeypatch):
    """A write that landed is not a row the reader accepts.

    Driven with a sqlite trigger that rewrites the value on insert, which is the
    cheapest way to make the store accept the write and then hold something else.
    """
    env, db = existing_store(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"CREATE TRIGGER mangle AFTER INSERT ON {AUTH_TABLE} "
            f"BEGIN UPDATE {AUTH_TABLE} SET value = 'dropped' WHERE key = NEW.key; END"
        )
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    with pytest.raises(common.ConfigError) as err:
        seed_kiro_cli_login(env=env, run=accepting_run)
    assert "did not keep" in str(err.value)


def test_seed_replaces_an_earlier_row_rather_than_failing_on_it(tmp_path, monkeypatch):
    """A persistent volume can carry a row from an earlier task; it must not win."""
    env, db = existing_store(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            kiro_login_mod.UPSERT_SQL,
            (SENTINEL_ROW_KEY, json.dumps({"access_token": "atk-stale"})),
        )
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    seed_kiro_cli_login(env=env, run=accepting_run)
    with sqlite3.connect(db) as conn:
        row = conn.execute(SELECT_SQL, (SENTINEL_ROW_KEY,)).fetchone()
    assert json.loads(row[0])["access_token"] == SENTINEL_ACCESS_TOKEN


def test_a_row_under_another_key_is_removed_too(tmp_path, monkeypatch):
    """Two reasons, one statement.

    A row an earlier task wrote under a different identity's key would sign this task
    in as whoever that was; and left in place it would also let the binary's acceptance
    verdict be about that row rather than about this one.
    """
    env, db = existing_store(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            kiro_login_mod.UPSERT_SQL,
            ("kirocli:social:token", json.dumps({"access_token": "atk-previous-tenant"})),
        )
    monkeypatch.setattr(kiro_login_mod, "_resolve_binary", fake_resolve)
    seed_kiro_cli_login(env=env, run=accepting_run)
    with sqlite3.connect(db) as conn:
        rows = conn.execute(f"SELECT key FROM {AUTH_TABLE}").fetchall()
    assert [key for (key,) in rows] == [SENTINEL_ROW_KEY]
