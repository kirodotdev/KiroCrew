"""External kiro-cli logout / account switch must invalidate running state.

Covers the three properties that together let a signed-out account keep
answering: the fingerprint must ignore token rotation, an ordinary status poll
must re-probe when the account changes, and a turn must retire the children that
still hold the old credential.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew import kiro_prerequisite as kp
from kiro_crew.session_allocation import (
    FOREGROUND_COLD_START_RESERVE as _COLD_START_RESERVE_FOR_TEST,
)
from kiro_crew.session_allocation import MAX_CONCURRENT_COLD_STARTS as _MAX_COLD_STARTS_FOR_TEST


@pytest.fixture(autouse=True)
def _private_sel_root_per_test(sel_private_root):
    """Every test in this module gets its OWN SEL root.

    ``identity_fingerprint`` is audit-or-deny: it returns "absent" unless a
    CRITICAL SEL event lands first. On the event-loop thread the chain-lock
    acquire is a single non-blocking attempt that refuses rather than stall the
    loop -- correct product behaviour -- so on the worker's SHARED SEL root an
    async test asserting a NON-EMPTY fingerprint is racing writers it never
    created (another test still flushing, another xdist worker on the same
    path). It then reads "" and fails on a property it never meant to test.
    ``sel_private_root`` removes the concurrent writer: a fresh per-test,
    per-worker directory nothing else writes.
    """
    yield


def _simulate_unsearchable_directory(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """Answer for every path inside ``directory`` as the OS does when the gateway may not look in it.

    A stub rather than ``chmod(0)``, so the scenario also runs as root and on Windows, where a
    mode-0 directory blocks nothing. ``Path.exists`` and ``Path.is_symlink`` answer through
    ``os.path`` as CPython 3.14's do; 3.12 and 3.13 raise there instead.
    """

    denied_directory = os.path.normcase(os.path.abspath(os.fsdecode(directory)))

    def is_strictly_inside(candidate: object) -> bool:
        try:
            candidate_path = os.fspath(candidate)
        except TypeError:
            return False
        try:
            candidate_name = os.path.normcase(os.path.abspath(os.fsdecode(candidate_path)))
            return (
                candidate_name != denied_directory
                and os.path.commonpath((candidate_name, denied_directory)) == denied_directory
            )
        except (OSError, TypeError, ValueError):
            return False

    real_stat = os.stat
    real_lstat = os.lstat
    real_exists = os.path.exists
    real_lexists = os.path.lexists
    real_islink = os.path.islink
    real_open_identity_db_readonly = kp._open_identity_db_readonly

    def permission_denied(candidate: object) -> PermissionError:
        return PermissionError(
            errno.EACCES,
            os.strerror(errno.EACCES),
            os.fspath(candidate),
        )

    def stat(candidate: object, *args: object, **kwargs: object) -> os.stat_result:
        if is_strictly_inside(candidate):
            raise permission_denied(candidate)
        return real_stat(candidate, *args, **kwargs)

    def lstat(candidate: object, *args: object, **kwargs: object) -> os.stat_result:
        if is_strictly_inside(candidate):
            raise permission_denied(candidate)
        return real_lstat(candidate, *args, **kwargs)

    def exists(candidate: object) -> bool:
        return False if is_strictly_inside(candidate) else real_exists(candidate)

    def lexists(candidate: object) -> bool:
        return False if is_strictly_inside(candidate) else real_lexists(candidate)

    def islink(candidate: object) -> bool:
        return False if is_strictly_inside(candidate) else real_islink(candidate)

    def path_exists(self: Path, *, follow_symlinks: bool = True) -> bool:
        return os.path.exists(self) if follow_symlinks else os.path.lexists(self)

    def open_identity_db_readonly(path: Path):
        if is_strictly_inside(path):
            return None
        return real_open_identity_db_readonly(path)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os.path, "exists", exists)
    monkeypatch.setattr(os.path, "lexists", lexists)
    monkeypatch.setattr(os.path, "islink", islink)
    monkeypatch.setattr(Path, "exists", path_exists)
    monkeypatch.setattr(Path, "is_symlink", lambda self: os.path.islink(self))
    monkeypatch.setattr(kp, "_open_identity_db_readonly", open_identity_db_readonly)


def _write_store(
    path: Path,
    *,
    token_value: str = "access-token-v1",
    start_url: str = "https://company.awsapps.com/start",
    profile: str = "arn:aws:codewhisperer:us-east-1:1111:profile/COMPANY",
    auth_keys: tuple[str, ...] = ("kirocli:odic:device-registration", "kirocli:odic:token"),
    client_id: str = "client-registration-aaa",
    region: str = "us-east-1",
    state_rows: bool = True,
) -> None:
    """Write a minimal store shaped like kiro-cli's real one.

    ``state_rows=False`` models a Builder ID login: no Identity Center marker rows
    and no CodeWhisperer profile, so the identity has to come from the credential
    blob's stable claims instead.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    with con:
        con.execute("CREATE TABLE IF NOT EXISTS auth_kv (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("DELETE FROM auth_kv")
        con.execute("DELETE FROM state")
        for key in auth_keys:
            if key.endswith(":device-registration"):
                blob = {
                    "client_id": client_id,
                    "client_secret": "SECRET-must-never-be-fingerprinted",
                    "client_secret_expires_at": "2099-01-01T00:00:00Z",
                    "oauth_flow": "device_code",
                    "region": region,
                    "scopes": ["codewhisperer:completions", "codewhisperer:analysis"],
                }
            else:
                blob = {
                    "access_token": token_value,
                    "refresh_token": f"refresh-of-{token_value}",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "oauth_flow": "device_code",
                    "region": region,
                    "scopes": ["codewhisperer:completions", "codewhisperer:analysis"],
                    "start_url": start_url,
                }
            con.execute("INSERT INTO auth_kv (key, value) VALUES (?, ?)", (key, json.dumps(blob)))
        if state_rows:
            con.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)", ("auth.idc.start-url", start_url)
            )
            con.execute("INSERT INTO state (key, value) VALUES (?, ?)", ("auth.idc.region", region))
            con.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)",
                ("api.codewhisperer.profile", profile),
            )
        # Unrelated local state must not participate in the fingerprint.
        con.execute("INSERT INTO state (key, value) VALUES (?, ?)", ("telemetry.client-id", "abc"))
    con.close()


def _expire_identity_cache(service: "kp.KiroPrerequisiteService") -> None:
    """Drop the reader's real-time cache.

    The fingerprint is cached for a few seconds so a dashboard poll storm cannot
    turn into one SQLite read and one SEL audit event per poll. A test that
    rewrites the store and immediately re-reads is outside that design, so it
    expires the cache explicitly rather than sleeping.
    """

    service._identity_cache_at = 0.0


class TestIdentityFingerprint:
    def test_token_rotation_does_not_change_the_fingerprint(self, tmp_path: Path) -> None:
        """A refresh replaces the token value; the account has not changed.

        This is the property that makes the check safe to run on the turn path.
        If the token value were an input, every refresh would read as an account
        change and retire healthy sessions roughly hourly.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(db, token_value="access-token-v1")
        before = kp.identity_fingerprint(db)
        _write_store(db, token_value="a-completely-different-token-v2")
        assert kp.identity_fingerprint(db) == before
        assert before != ""

    def test_unrelated_local_state_does_not_change_the_fingerprint(self, tmp_path: Path) -> None:
        db = tmp_path / "data.sqlite3"
        _write_store(db)
        before = kp.identity_fingerprint(db)
        con = sqlite3.connect(str(db))
        with con:
            con.execute("UPDATE state SET value='zzz' WHERE key='telemetry.client-id'")
        con.close()
        assert kp.identity_fingerprint(db) == before

    def test_account_switch_changes_the_fingerprint(self, tmp_path: Path) -> None:
        db = tmp_path / "data.sqlite3"
        _write_store(db)
        company = kp.identity_fingerprint(db)
        _write_store(
            db,
            start_url="https://personal.awsapps.com/start",
            profile="arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
        )
        assert kp.identity_fingerprint(db) != company

    def test_auth_kind_switch_changes_the_fingerprint(self, tmp_path: Path) -> None:
        """Same state rows, different credential kind, is still a different login."""

        db = tmp_path / "data.sqlite3"
        _write_store(db, auth_keys=("kirocli:odic:token",))
        odic = kp.identity_fingerprint(db)
        _write_store(db, auth_keys=("kirocli:social:token",))
        assert kp.identity_fingerprint(db) != odic

    def test_logout_reads_as_absent(self, tmp_path: Path) -> None:
        db = tmp_path / "data.sqlite3"
        _write_store(db)
        assert kp.identity_fingerprint(db) != ""
        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        assert kp.identity_fingerprint(db) == ""

    def test_missing_and_symlinked_stores_read_as_absent(self, tmp_path: Path) -> None:
        assert kp.identity_fingerprint(tmp_path / "nope.sqlite3") == ""
        real = tmp_path / "real.sqlite3"
        _write_store(real)
        link = tmp_path / "link.sqlite3"
        link.symlink_to(real)
        # A symlink could redirect the read; the sanctioned reader refuses it.
        assert kp.identity_fingerprint(link) == ""

    def test_a_claimless_row_contributes_nothing(self, tmp_path: Path) -> None:
        """A social login has no SSO start_url, so its row carries no claim.

        Recording the key NAME alone would make account A and account B under
        `kirocli:social:token` fingerprint identically, and the child
        authenticated as A would never be retired. Contributing nothing lets the
        store come out ABSENT, which is never reconciled and re-sweeps each turn:
        "cannot distinguish" reported as "cannot confirm", not as "unchanged".
        """

        db = tmp_path / "data.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        with con:
            con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT)")
            # Social token: a rotating access token and nothing identifying.
            con.execute(
                "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
                ("kirocli:social:token", json.dumps({"access_token": "account-A"})),
            )
        con.close()
        assert kp.identity_fingerprint(db) == ""

        con = sqlite3.connect(str(db))
        with con:
            con.execute(
                "UPDATE auth_kv SET value=? WHERE key='kirocli:social:token'",
                (json.dumps({"access_token": "account-B"}),),
            )
        con.close()
        # Still absent -- and absent is never accepted as a reconciled baseline.
        assert kp.identity_fingerprint(db) == ""

    def test_a_claimful_row_still_records_its_key(self, tmp_path: Path) -> None:
        """The skip must not drop rows that DO identify an account."""

        db = tmp_path / "data.sqlite3"
        _write_store(db, state_rows=False)
        assert kp.identity_fingerprint(db) != ""

    def test_fingerprint_carries_no_credential_value(self, tmp_path: Path) -> None:
        db = tmp_path / "data.sqlite3"
        secret = "super-secret-token-value"
        _write_store(db, token_value=secret, start_url="https://company.awsapps.com/start")
        fingerprint = kp.identity_fingerprint(db)
        assert secret not in fingerprint
        assert "company.awsapps.com" not in fingerprint
        assert "SECRET-must-never-be-fingerprinted" not in fingerprint

    def test_a_profile_change_alone_is_detected(self, tmp_path: Path) -> None:
        """Two Identity Center accounts can share a start_url and registration.

        What separates them is the CodeWhisperer profile ARN in `state`, so those
        rows carry identity the credential blob does not.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(db, profile="arn:aws:codewhisperer:us-east-1:1111:profile/TEAM_A")
        team_a = kp.identity_fingerprint(db)
        _write_store(db, profile="arn:aws:codewhisperer:us-east-1:2222:profile/TEAM_B")
        assert kp.identity_fingerprint(db) != team_a

    def test_a_profile_less_account_switch_is_detected(self, tmp_path: Path) -> None:
        """Builder ID -> Builder ID: identical key names, no state rows at all.

        With only key names and `state` rows participating, these two logins were
        indistinguishable and the stale child kept answering as the first account.
        The stable claims inside the credential blob are what separate them.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(
            db,
            state_rows=False,
            start_url="https://view.awsapps.com/start",
            client_id="registration-for-account-A",
        )
        account_a = kp.identity_fingerprint(db)
        _write_store(
            db,
            state_rows=False,
            start_url="https://view.awsapps.com/start",
            client_id="registration-for-account-B",
        )
        assert kp.identity_fingerprint(db) != account_a
        assert account_a != ""

    def test_a_start_url_change_alone_is_detected(self, tmp_path: Path) -> None:
        """Even with no state rows and the same registration."""

        db = tmp_path / "data.sqlite3"
        _write_store(db, state_rows=False, start_url="https://a.awsapps.com/start")
        before = kp.identity_fingerprint(db)
        _write_store(db, state_rows=False, start_url="https://b.awsapps.com/start")
        assert kp.identity_fingerprint(db) != before

    def test_rotating_blob_fields_do_not_move_the_fingerprint(self, tmp_path: Path) -> None:
        """access_token, refresh_token and expires_at all rotate on refresh."""

        db = tmp_path / "data.sqlite3"
        _write_store(db, token_value="v1")
        before = kp.identity_fingerprint(db)
        con = sqlite3.connect(str(db))
        with con:
            row = con.execute("SELECT value FROM auth_kv WHERE key='kirocli:odic:token'").fetchone()
            blob = json.loads(row[0])
            blob["access_token"] = "rotated-access"
            blob["refresh_token"] = "rotated-refresh"
            blob["expires_at"] = "2100-06-06T00:00:00Z"
            con.execute(
                "UPDATE auth_kv SET value=? WHERE key='kirocli:odic:token'", (json.dumps(blob),)
            )
        con.close()
        assert kp.identity_fingerprint(db) == before

    def test_scope_reordering_is_not_an_account_change(self, tmp_path: Path) -> None:
        db = tmp_path / "data.sqlite3"
        _write_store(db)
        before = kp.identity_fingerprint(db)
        con = sqlite3.connect(str(db))
        with con:
            row = con.execute("SELECT value FROM auth_kv WHERE key='kirocli:odic:token'").fetchone()
            blob = json.loads(row[0])
            blob["scopes"] = list(reversed(blob["scopes"]))
            con.execute(
                "UPDATE auth_kv SET value=? WHERE key='kirocli:odic:token'", (json.dumps(blob),)
            )
        con.close()
        assert kp.identity_fingerprint(db) == before

    @staticmethod
    def _set_oauth_flow(db: Path, flow: str) -> None:
        con = sqlite3.connect(str(db))
        with con:
            for key, value in con.execute("SELECT key, value FROM auth_kv").fetchall():
                blob = json.loads(value)
                blob["oauth_flow"] = flow
                con.execute("UPDATE auth_kv SET value=? WHERE key=?", (json.dumps(blob), key))
        con.close()

    def test_oauth_flow_casing_is_not_an_account_change(self, tmp_path: Path) -> None:
        """kiro-cli's refreshers write the same flow as `PKCE` or `Pkce`.

        A refresh that only re-spells the flow must not read as a new account,
        or every refresh retires healthy sessions.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(db)
        self._set_oauth_flow(db, "PKCE")
        before = kp.identity_fingerprint(db)
        self._set_oauth_flow(db, "Pkce")
        assert kp.identity_fingerprint(db) == before
        self._set_oauth_flow(db, "pkce")
        assert kp.identity_fingerprint(db) == before

    def test_an_oauth_flow_change_is_still_detected(self, tmp_path: Path) -> None:
        """Case-folding must not hide a genuine change of sign-in flow."""

        db = tmp_path / "data.sqlite3"
        _write_store(db)
        self._set_oauth_flow(db, "PKCE")
        before = kp.identity_fingerprint(db)
        self._set_oauth_flow(db, "device_code")
        assert kp.identity_fingerprint(db) != before

    def test_start_url_and_client_id_stay_case_sensitive(self, tmp_path: Path) -> None:
        """Only the flow label is case-folded; identifiers keep exact comparison."""

        db = tmp_path / "data.sqlite3"
        _write_store(db, state_rows=False, client_id="client-registration-aaa")
        before = kp.identity_fingerprint(db)
        _write_store(db, state_rows=False, client_id="CLIENT-REGISTRATION-AAA")
        assert kp.identity_fingerprint(db) != before
        _write_store(db, state_rows=False, start_url="https://COMPANY.awsapps.com/start")
        assert kp.identity_fingerprint(db) != before

    def test_an_unknown_blob_field_never_joins_the_fingerprint(self, tmp_path: Path) -> None:
        """Allowlist, not denylist: a field a future kiro-cli adds stays out.

        Otherwise a new secret could enter the digest, or a new rotating field
        could report an account change on every refresh.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(db)
        before = kp.identity_fingerprint(db)
        con = sqlite3.connect(str(db))
        with con:
            row = con.execute("SELECT value FROM auth_kv WHERE key='kirocli:odic:token'").fetchone()
            blob = json.loads(row[0])
            blob["some_future_secret"] = "leak-me"
            blob["some_future_counter"] = "42"
            con.execute(
                "UPDATE auth_kv SET value=? WHERE key='kirocli:odic:token'", (json.dumps(blob),)
            )
        con.close()
        assert kp.identity_fingerprint(db) == before

    def test_the_read_is_audited_and_fails_closed(self, tmp_path: Path, monkeypatch) -> None:
        """This file holds live credential material.

        An unauditable read must return "absent" -- which errs toward retiring the
        children -- rather than hand back an unaudited answer.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(db)
        assert kp.identity_fingerprint(db) != ""

        calls: list[tuple[str, str]] = []

        def _refuse(read_id: str, outcome: str) -> bool:
            calls.append((read_id, outcome))
            return False

        monkeypatch.setattr(kp.hooks, "emit_internal_read_audit", _refuse)
        assert kp.identity_fingerprint(db) == ""
        assert calls and calls[0][0] == "kiro_prerequisite.identity_fingerprint"

    def test_the_audit_id_is_registered(self) -> None:
        """An unregistered id is refused by the hook, which would fail every read."""

        from kiro_crew import hooks

        assert kp._IDENTITY_FINGERPRINT_READ_ID in hooks._AUDIT_ONLY_READ_IDS

    def test_an_unauditable_read_never_opens_the_store(self, tmp_path: Path, monkeypatch) -> None:
        """The gate is BEFORE the read, not a discard afterwards.

        Discarding after the fact would still have pulled credential material into
        the process with no audit trail; refusing up front means the file is never
        opened at all.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(db)
        opened: list[Path] = []
        real_open = kp._open_identity_db_readonly

        def _tracked(path: Path):
            opened.append(path)
            return real_open(path)

        monkeypatch.setattr(kp, "_open_identity_db_readonly", _tracked)
        monkeypatch.setattr(kp.hooks, "emit_internal_read_audit", lambda *_: False)

        assert kp.identity_fingerprint(db) == ""
        assert opened == [], "the store was opened despite an unavailable audit"

    def test_a_failed_terminal_audit_discards_the_result(self, tmp_path: Path, monkeypatch) -> None:
        """The read happened; if its outcome cannot be audited, discard the answer.

        Failing only on the pre-read audit would leave a path where the store was
        read, the SEL write failed, and unaudited identity data still drove
        retirement.
        """

        db = tmp_path / "data.sqlite3"
        _write_store(db)

        def _fail_only_success(read_id: str, outcome: str) -> bool:
            return outcome != "success"

        monkeypatch.setattr(kp.hooks, "emit_internal_read_audit", _fail_only_success)
        assert kp.identity_fingerprint(db) == ""


class TestFingerprintCaching:
    """The cache serves polling only; anything acting on the answer reads fresh."""

    @pytest.mark.asyncio
    async def test_polling_reuses_a_cached_read(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        reads: list[int] = []
        real = kp.identity_fingerprint

        def _counted(path, **kwargs):
            reads.append(1)
            return real(path, **kwargs)

        monkeypatched = pytest.MonkeyPatch()
        monkeypatched.setattr(kp, "identity_fingerprint", _counted)
        try:
            await service.current_identity_fingerprint()
            await service.current_identity_fingerprint()
            await service.current_identity_fingerprint()
        finally:
            monkeypatched.undo()

        assert len(reads) == 1, "a poll storm must collapse onto one read"

    @pytest.mark.asyncio
    async def test_a_logout_inside_the_cache_window_is_still_detected(self, tmp_path: Path) -> None:
        """The window GPT identified.

        A fingerprint read seconds earlier, then a logout, then a turn. Serving the
        cached value would let the child authenticated as the logged-out account
        take that turn.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        # Warm the cache and reconcile, as a status poll plus a first turn would.
        _, live = await service.identity_changed_since_sessions()
        service.note_sessions_reconciled(live)
        await service.current_identity_fingerprint()  # poll, populates the cache

        # Logout, well inside the cache window -- no cache poke here on purpose.
        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()

        changed, live_now = await service.identity_changed_since_sessions()
        assert changed is True, "the cached pre-logout value was served to a turn"
        assert live_now == ""


class TestApiKeyIdentity:
    """A host authenticated by KIRO_API_KEY must not read as signed out.

    kiro-cli keeps no identity row for an API key in its store, so a
    store-only fingerprint came out ABSENT on every read. Absent is never
    reconciled as a baseline, so every dashboard send retired idle sessions
    and cancelled their running subagents although the CLI was authenticated.
    """

    _KEY_1 = "test-api-key-value-one-0123456789"
    _KEY_2 = "test-api-key-value-two-9876543210"

    @staticmethod
    def _write_empty_store(home: Path) -> Path:
        """A store shaped like an API-key host's: the schema, no identity rows."""

        db = kp.kiro_identity_store_path("linux", home, {})
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db))
        with con:
            con.execute("CREATE TABLE IF NOT EXISTS auth_kv (key TEXT PRIMARY KEY, value BLOB)")
        con.close()
        return db

    @staticmethod
    def _service(home: Path, environ: dict[str, str]) -> "kp.KiroPrerequisiteService":
        return kp.KiroPrerequisiteService(home=home, environ=environ, platform_name="linux")

    def test_the_store_alone_reads_as_signed_out(self, tmp_path: Path) -> None:
        """The repro: the store reader cannot see the key at all."""

        db = self._write_empty_store(tmp_path)
        assert kp.identity_fingerprint(db) == ""

    @pytest.mark.asyncio
    async def test_an_api_key_host_fingerprints_non_empty_and_stable(self, tmp_path: Path) -> None:
        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})

        first = await service.current_identity_fingerprint(allow_cached=False)
        second = await service.current_identity_fingerprint(allow_cached=False)

        assert first != ""
        assert first == second

    @pytest.mark.asyncio
    async def test_a_key_read_from_the_data_home_env_file_counts(self, tmp_path: Path) -> None:
        """Post-scrub Docker moves the key into the data home's .env; the
        fingerprint must find it there, as the whoami probe does."""

        self._write_empty_store(tmp_path)
        env_file = tmp_path / ".kiro" / "crew" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(f"KIRO_API_KEY={self._KEY_1}\n")
        from_file = await self._service(tmp_path, {}).current_identity_fingerprint(
            allow_cached=False
        )
        from_environ = await self._service(
            tmp_path, {"KIRO_API_KEY": self._KEY_1}
        ).current_identity_fingerprint(allow_cached=False)

        assert from_file != ""
        assert from_file == from_environ

    @pytest.mark.asyncio
    async def test_a_key_change_changes_the_fingerprint(self, tmp_path: Path) -> None:
        self._write_empty_store(tmp_path)
        environ = {"KIRO_API_KEY": self._KEY_1}
        service = self._service(tmp_path, environ)

        before = await service.current_identity_fingerprint(allow_cached=False)
        environ["KIRO_API_KEY"] = self._KEY_2
        after = await service.current_identity_fingerprint(allow_cached=False)

        assert before != "" and after != ""
        assert before != after

    @pytest.mark.asyncio
    async def test_no_key_and_no_store_identity_is_still_absent(self, tmp_path: Path) -> None:
        """The unchanged contract: nothing anywhere still reads as \"\"."""

        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {})

        assert await service.current_identity_fingerprint(allow_cached=False) == ""

    @pytest.mark.asyncio
    async def test_store_login_plus_key_differs_from_either_alone(self, tmp_path: Path) -> None:
        """The key is its own component beside the store's, so no precedence
        between them has to be assumed: adding or removing either reads as a
        change."""

        store_home = tmp_path / "store"
        _write_store(kp.kiro_identity_store_path("linux", store_home, {}))
        key_home = tmp_path / "key"
        self._write_empty_store(key_home)

        store_only = await self._service(store_home, {}).current_identity_fingerprint(
            allow_cached=False
        )
        both = await self._service(
            store_home, {"KIRO_API_KEY": self._KEY_1}
        ).current_identity_fingerprint(allow_cached=False)
        key_only = await self._service(
            key_home, {"KIRO_API_KEY": self._KEY_1}
        ).current_identity_fingerprint(allow_cached=False)

        assert "" not in (store_only, both, key_only)
        assert len({store_only, both, key_only}) == 3
        # A host without a key keeps the store-only fingerprint byte-for-byte.
        assert store_only == kp.identity_fingerprint(
            kp.kiro_identity_store_path("linux", store_home, {})
        )

    @pytest.mark.asyncio
    async def test_a_store_account_switch_is_seen_while_a_key_is_set(self, tmp_path: Path) -> None:
        """A harness that strips the key (KAS) authenticates from the store, so a
        store account switch must still prove a mismatch and arm the latch on a
        host that also carries a key."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})
        assert await service.seed_sessions_baseline() is True
        before = await service.current_identity_fingerprint(allow_cached=False)

        _write_store(db, start_url="https://other.awsapps.com/start", client_id="other-client")
        after = await service.current_identity_fingerprint(allow_cached=False)

        assert after != before
        assert kp.identity_stamp_mismatch(before, after) is True
        assert service._interim_identity_observed is True

    @pytest.mark.asyncio
    async def test_a_store_read_blip_is_not_proof_of_a_new_account_on_a_key_host(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A store read that fails (here: the database is not openable) must not
        present as a DIFFERENT nonempty identity: the interim latch and the
        spawn-stamp mismatch treat that as proof of a new account and would
        retire healthy sessions with their running children."""

        _write_store(kp.kiro_identity_store_path("linux", tmp_path, {}))
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})
        assert await service.seed_sessions_baseline() is True
        healthy = await service.current_identity_fingerprint(allow_cached=False)

        with monkeypatch.context() as patched:
            patched.setattr(kp, "_open_identity_db_readonly", lambda _path: None)
            blipped = await service.current_identity_fingerprint(allow_cached=False)

        assert kp.identity_stamp_mismatch(healthy, blipped) is False
        assert service._interim_identity_observed is False
        # Once the store reads again nothing is left over to force a sweep.
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure", ["audit_unavailable", "unreadable", "sqlite_error"], ids=str
    )
    async def test_a_key_never_stands_in_for_an_unknowable_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
    ) -> None:
        """A child can still authenticate from the store (a harness that strips
        the key), so a store read that could not be audited or completed must
        not let a key-only fingerprint become the baseline -- a later store
        account switch would then compare equal. Such a read stays absent and
        the seed is refused, exactly as before the key was counted."""

        _write_store(kp.kiro_identity_store_path("linux", tmp_path, {}))
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})
        if failure == "audit_unavailable":
            monkeypatch.setattr(kp.hooks, "emit_internal_read_audit", lambda *_a, **_k: False)
        elif failure == "unreadable":
            monkeypatch.setattr(kp, "_open_identity_db_readonly", lambda _path: None)
        else:

            class _BrokenConnection:
                def execute(self, *_a: object) -> object:
                    raise sqlite3.OperationalError("database is locked")

                def close(self) -> None:
                    pass

            monkeypatch.setattr(kp, "_open_identity_db_readonly", lambda _p: _BrokenConnection())

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert await service.seed_sessions_baseline() is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("with_sso_row", [False, True], ids=["social-only", "social-plus-sso"])
    async def test_a_key_never_stands_in_for_an_unidentifiable_store_login(
        self, tmp_path: Path, with_sso_row: bool
    ) -> None:
        """A social login (GitHub, Google) carries no stable claim, so the store
        reader skips its row. A KAS child still authenticates as that login, so
        a switch between two social accounts must not compare equal under an
        unchanged key: the key is withheld and the fingerprint is exactly what
        it was before the key was counted."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        if with_sso_row:
            _write_store(db, state_rows=False)
        else:
            self._write_empty_store(tmp_path)
        con = sqlite3.connect(str(db))
        with con:
            con.execute(
                "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
                ("kirocli:social:token", json.dumps({"access_token": "social-a"})),
            )
        con.close()
        keyed = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})
        keyless = self._service(tmp_path, {})

        with_key = await keyed.current_identity_fingerprint(allow_cached=False)
        assert with_key == await keyless.current_identity_fingerprint(allow_cached=False)
        if not with_sso_row:
            assert with_key == ""
            assert await keyed.seed_sessions_baseline() is False

    @pytest.mark.asyncio
    async def test_a_key_never_stands_in_for_a_relocated_store(self, tmp_path: Path) -> None:
        """With the store relocated the gateway cannot see the store a child
        may authenticate from, so the key must not make that read definitive."""

        service = self._service(
            tmp_path,
            {"KIRO_API_KEY": self._KEY_1, "XDG_DATA_HOME": str(tmp_path / "elsewhere")},
        )
        assert kp.identity_store_is_relocated("linux", tmp_path, service._environ)
        assert await service.current_identity_fingerprint(allow_cached=False) == ""

    @pytest.mark.asyncio
    async def test_an_empty_store_proves_nobody_is_signed_in(self, tmp_path: Path) -> None:
        """No credential row, no key, no vault identity: a sign-out, not a guess."""

        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {})

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is True

    @pytest.mark.asyncio
    async def test_a_social_login_reads_absent_without_proving_a_sign_out(
        self, tmp_path: Path
    ) -> None:
        """Its row carries no stable claim, so someone is signed in behind an empty
        fingerprint: a caller that takes this for a sign-out recycles healthy work."""

        self._write_empty_store(tmp_path)
        con = sqlite3.connect(str(kp.kiro_identity_store_path("linux", tmp_path, {})))
        with con:
            con.execute(
                "INSERT INTO auth_kv (key, value) VALUES (?, ?)",
                ("kirocli:social:token", json.dumps({"access_token": "social-a"})),
            )
        con.close()
        service = self._service(tmp_path, {})

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_relocated_store_reads_absent_without_proving_a_sign_out(
        self, tmp_path: Path
    ) -> None:
        service = self._service(tmp_path, {"XDG_DATA_HOME": str(tmp_path / "elsewhere")})

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_signed_in_store_is_no_absence(self, tmp_path: Path) -> None:
        _write_store(kp.kiro_identity_store_path("linux", tmp_path, {}))
        service = self._service(tmp_path, {})

        assert await service.current_identity_fingerprint(allow_cached=False) != ""
        assert service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_an_env_file_that_will_not_read_proves_no_sign_out(self, tmp_path: Path) -> None:
        """The key reader reports an unreadable .env as holding no key, so an empty
        store beside one is no sign-out: the key may be in it."""

        self._write_empty_store(tmp_path)
        # A directory where the .env belongs: no platform will read it as a file.
        (tmp_path / ".kiro" / "crew" / ".env").mkdir(parents=True)
        service = self._service(tmp_path, {})

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_failed_env_read_is_no_proof_even_when_a_reread_would_find_the_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The proof comes from the key claim's own read: a later read that succeeds
        cannot vouch for the one that failed, because the key may be in the file."""

        from kiro_crew.config import loader

        self._write_empty_store(tmp_path)
        env_file = tmp_path / ".kiro" / "crew" / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(f"KIRO_API_KEY={self._KEY_1}\n")
        real_read = loader.read_env_text
        reads: list[Path] = []

        def _fails_once(ep: Path, encoding: str | None = None) -> str:
            reads.append(ep)
            if len(reads) == 1:
                raise PermissionError("transient")
            return real_read(ep, encoding)

        monkeypatch.setattr(loader, "read_env_text", _fails_once)
        service = self._service(tmp_path, {})

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_vault_that_will_not_read_proves_no_sign_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token store reads a corrupt vault as empty; the proof must not."""

        from kiro_crew.auth import bridge
        from kiro_crew.auth.store import TokenStore

        vault = TokenStore(tmp_path / "vault-home")
        entries = tmp_path / "vault-home" / "kas" / ".vault" / "secrets.enc"
        entries.parent.mkdir(parents=True)
        entries.write_text("not a vault")
        monkeypatch.setattr(bridge, "default_token_store", lambda: vault)
        # The reader the fingerprint uses takes this vault for an empty one.
        assert vault.resolve() is None
        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {})

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_store_the_gateway_may_not_look_at_proves_no_sign_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``os.path.lexists`` answers False for a missing store and for one it may not see."""

        store_dir = self._write_empty_store(tmp_path).parent
        service = self._service(tmp_path, {})
        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is True

        _simulate_unsearchable_directory(monkeypatch, store_dir)
        denied_service = self._service(tmp_path, {})
        assert await denied_service.current_identity_fingerprint(allow_cached=False) == ""
        assert denied_service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_windows_store_the_gateway_may_not_look_at_proves_no_sign_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows chooses between two store anchors; skipping one it cannot see is no proof."""

        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        roaming.parent.mkdir(parents=True)
        con = sqlite3.connect(str(roaming))
        with con:
            con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value BLOB)")
        con.close()
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="win32")
        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is True

        _simulate_unsearchable_directory(monkeypatch, roaming.parent)
        denied_service = kp.KiroPrerequisiteService(
            home=tmp_path, environ={}, platform_name="win32"
        )
        # The store choice reads the missing Local anchor, which says "no store file".
        assert await denied_service.current_identity_fingerprint(allow_cached=False) == ""
        assert denied_service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_two_windows_stores_prove_no_sign_out(self, tmp_path: Path) -> None:
        """With both stores present the choice rests on write times that may not read."""

        def write_empty(path: Path) -> None:
            path.parent.mkdir(parents=True)
            con = sqlite3.connect(str(path))
            with con:
                con.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value BLOB)")
            con.close()

        write_empty(tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3")
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="win32")
        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is True

        write_empty(tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3")
        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_vault_the_gateway_may_not_look_at_proves_no_sign_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a missing entry map is an empty vault, not one the gateway may not see."""

        from kiro_crew.auth import bridge
        from kiro_crew.auth.store import TokenStore

        vault = TokenStore(tmp_path / "vault-home")
        vault_dir = tmp_path / "vault-home" / "kas" / ".vault"
        vault_dir.mkdir(parents=True)
        (vault_dir / "secrets.enc").write_text('{"version": 1, "backend": "file", "entries": {}}')
        monkeypatch.setattr(bridge, "default_token_store", lambda: vault)
        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {})
        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is True

        _simulate_unsearchable_directory(monkeypatch, vault_dir)
        denied_service = self._service(tmp_path, {})
        assert await denied_service.current_identity_fingerprint(allow_cached=False) == ""
        assert denied_service.identity_absence_is_definitive is False

    @pytest.mark.asyncio
    async def test_a_finished_sweep_covers_one_unbroken_sign_out(self, tmp_path: Path) -> None:
        """Any other reading in between makes the next sign-out a new one to sweep."""

        self._write_empty_store(tmp_path)
        environ: dict[str, str] = {}
        service = self._service(tmp_path, environ)

        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_already_swept is False
        service.note_absence_swept(service.identity_absence_run)
        await service.current_identity_fingerprint(allow_cached=False)
        assert service.identity_absence_already_swept is True

        environ["KIRO_API_KEY"] = self._KEY_1
        assert await service.current_identity_fingerprint(allow_cached=False) != ""
        assert service.identity_absence_already_swept is False
        del environ["KIRO_API_KEY"]
        assert await service.current_identity_fingerprint(allow_cached=False) == ""
        assert service.identity_absence_is_definitive is True
        assert service.identity_absence_already_swept is False

        newer = service.identity_absence_run
        service.note_absence_swept(newer)
        # A slower sweep that began under the earlier sign-out finishes last.
        service.note_absence_swept(newer - 1)
        assert service.identity_absence_already_swept is True

    @pytest.mark.asyncio
    async def test_a_key_host_with_no_store_file_at_all_fingerprints(self, tmp_path: Path) -> None:
        """A container that never ran `kiro-cli login` has no store file; that
        is a definitive "nobody signed in there", so the key alone counts."""

        assert not kp.kiro_identity_store_path("linux", tmp_path, {}).exists()
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})

        assert await service.current_identity_fingerprint(allow_cached=False) != ""
        assert await service.seed_sessions_baseline() is True

    @pytest.mark.asyncio
    async def test_the_key_value_never_appears_in_the_fingerprint(self, tmp_path: Path) -> None:
        _write_store(kp.kiro_identity_store_path("linux", tmp_path / "both", {}))
        self._write_empty_store(tmp_path / "key")
        for home in (tmp_path / "both", tmp_path / "key"):
            fingerprint = await self._service(
                home, {"KIRO_API_KEY": self._KEY_1}
            ).current_identity_fingerprint(allow_cached=False)
            assert fingerprint != ""
            assert self._KEY_1 not in fingerprint
            # Not even a recognisable fragment of it.
            assert self._KEY_1[:12] not in fingerprint

    @pytest.mark.asyncio
    async def test_a_seeded_api_key_host_reports_unchanged(self, tmp_path: Path) -> None:
        """The reported bug end to end: the boot seed succeeds, so a turn no
        longer reads as an identity change and nothing is retired."""

        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})

        assert await service.seed_sessions_baseline() is True

        changed, live = await service.identity_changed_since_sessions()
        assert changed is False
        assert live != ""

    @pytest.mark.asyncio
    async def test_a_per_session_key_overlay_is_never_stamped(self, tmp_path: Path) -> None:
        """A cron ``env`` block can hand a child its OWN ``KIRO_API_KEY``. The
        gateway read fingerprints the ambient key, so stamping it would certify a
        possibly different account as live and the sweep would spare it."""

        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})
        live = await service.current_identity_fingerprint(allow_cached=False)
        assert live != ""

        async def reader() -> str:
            return live

        for overlay in ({"KIRO_API_KEY": "kiro_other_key"}, {"kiro_api_key": ""}):
            provider = SimpleNamespace(spawn_identity="", _extra_env=overlay)
            await kp.stamp_spawn_identity(reader, provider, pre_spawn=live)
            assert provider.spawn_identity == ""
            assert kp.spawned_under(provider, live) is False

        # The same overlay on the shared runtime a demuxed session rides.
        runtime = SimpleNamespace(spawn_identity="", _extra_env={"KIRO_API_KEY": "x"})
        demuxed = SimpleNamespace(_runtime=runtime)
        await kp.stamp_spawn_identity(reader, runtime, pre_spawn=live)
        assert runtime.spawn_identity == ""
        assert kp.spawned_under(demuxed, live) is False

        # A real ``AcpProvider`` keeps the overlay on the client it wraps, and
        # that client's shared runtime may carry it instead.
        wrapped = [
            SimpleNamespace(
                spawn_identity="", _client=SimpleNamespace(_extra_env={"KIRO_API_KEY": "b"})
            ),
            SimpleNamespace(
                spawn_identity="", client=SimpleNamespace(_extra_env={"KIRO_API_KEY": "b"})
            ),
            SimpleNamespace(
                spawn_identity="",
                _client=SimpleNamespace(
                    _extra_env={}, _runtime=SimpleNamespace(_extra_env={"KIRO_API_KEY": "b"})
                ),
            ),
        ]
        for provider in wrapped:
            await kp.stamp_spawn_identity(reader, provider, pre_spawn=live)
            assert provider.spawn_identity == ""
            assert kp.spawned_under(provider, live) is False

    @pytest.mark.asyncio
    async def test_a_child_without_a_key_overlay_is_still_stamped(self, tmp_path: Path) -> None:
        self._write_empty_store(tmp_path)
        service = self._service(tmp_path, {"KIRO_API_KEY": self._KEY_1})
        live = await service.current_identity_fingerprint(allow_cached=False)

        async def reader() -> str:
            return live

        provider = SimpleNamespace(
            spawn_identity="",
            _extra_env={"OTHER": "1"},
            _client=SimpleNamespace(
                _extra_env={"OTHER": "1"}, _runtime=SimpleNamespace(_extra_env={})
            ),
        )
        await kp.stamp_spawn_identity(reader, provider, pre_spawn=live)
        assert provider.spawn_identity == live
        assert kp.spawned_under(provider, live) is True

    def test_a_stale_stamp_with_a_key_overlay_is_not_spared(self) -> None:
        """Defence in depth: even a holder that somehow carries a stamp is not
        spared while its overlay names the key."""

        holder = SimpleNamespace(spawn_identity="fp", _extra_env={"KIRO_API_KEY": "b"})
        assert kp.spawned_under(holder, "fp") is False

    def test_the_overlay_guard_sees_the_real_acp_classes(self) -> None:
        """Couples the guard to the REAL provider/client/runtime shapes, so a
        rename of the attributes it reads fails here instead of failing open."""

        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.providers.acp import AcpProvider

        overlay = {"KIRO_API_KEY": "b"}
        assert kp.overlay_sets_api_key(AcpProvider(extra_env=overlay)) is True
        assert kp.overlay_sets_api_key(AcpRuntime(extra_env=overlay)) is True
        assert kp.overlay_sets_api_key(AcpProvider(extra_env={"OTHER": "1"})) is False
        assert kp.overlay_sets_api_key(AcpRuntime()) is False

    def test_a_key_rotation_alone_is_not_proof_of_a_wrong_account(self) -> None:
        """KAS strips the key and authenticates from the store, so a key
        rotation must not condemn its children; store and vault still prove."""

        sep, vault = kp._API_KEY_FINGERPRINT_SEP, kp._CREW_VAULT_FINGERPRINT_SEP
        assert kp.identity_stamp_mismatch(f"s1{sep}k1{vault}v1", f"s1{sep}k2{vault}v1") is False
        assert kp.identity_stamp_mismatch(f"s1{sep}k1", f"s2{sep}k1") is True
        assert kp.identity_stamp_mismatch(f"s1{sep}k1{vault}v1", f"s1{sep}k1{vault}v2") is True

    def test_a_key_rotation_spares_a_key_stripping_child_only(self) -> None:
        """A Crew-owned KAS relay (and every foreign backend) has the key
        stripped at spawn, so a key rotation alone must not un-spare it -- that
        would retire its idle parent and cancel running children. A kiro-cli
        child and a cli-owned KAS relay ARE handed the key, so they keep the
        whole-fingerprint spare."""

        from kiro_crew.acp.runtime import AcpRuntime
        from kiro_crew.providers.acp import AcpProvider

        sep, vault = kp._API_KEY_FINGERPRINT_SEP, kp._CREW_VAULT_FINGERPRINT_SEP
        stamp, rotated = f"s1{sep}k1{vault}v1", f"s1{sep}k2{vault}v1"

        def holder(backend: object, **runtime: object) -> SimpleNamespace:
            return SimpleNamespace(
                spawn_identity=stamp,
                _runtime=SimpleNamespace(acp_backend=backend, **runtime),
            )

        crew_owned_kas = holder("kas", _kas_host_auth=True)
        assert kp.spawned_under(crew_owned_kas, rotated) is True
        assert kp.spawned_under(holder("claude"), rotated) is True
        assert kp.spawned_under(holder(""), rotated) is False
        # A cli-owned KAS relay authenticated with the key, so a rotation un-spares it.
        assert kp.spawned_under(holder("kas", _kas_host_auth=False), rotated) is False
        # A KAS holder whose auth owner cannot be read keeps the stricter spare.
        assert kp.spawned_under(holder("kas"), rotated) is False
        # An unreadable backend keeps the stricter spare.
        assert kp.spawned_under(SimpleNamespace(spawn_identity=stamp), rotated) is False
        # A store or vault change still un-spares a key-stripping child.
        assert kp.spawned_under(crew_owned_kas, f"s2{sep}k2{vault}v1") is False
        assert kp.spawned_under(crew_owned_kas, f"s1{sep}k2{vault}v2") is False

        # The backend and auth owner are read off the real classes.
        crew_owned = AcpRuntime(acp_backend="kas")
        crew_owned._kas_host_auth = True
        assert kp.receives_kiro_cli_api_key(crew_owned) is False
        assert kp.receives_kiro_cli_api_key(AcpRuntime(acp_backend="kas")) is True
        assert kp.receives_kiro_cli_api_key(AcpRuntime()) is True
        assert kp.receives_kiro_cli_api_key(AcpProvider(acp_backend="kas")) is True
        assert kp.receives_kiro_cli_api_key(AcpProvider()) is True


class TestStorePathSelection:
    def test_linux_path_is_kiro_cli_not_amazon_q(self, tmp_path: Path) -> None:
        path = kp.kiro_identity_store_path("linux", tmp_path, {})
        assert path == tmp_path / ".local" / "share" / "kiro-cli" / "data.sqlite3"

    def test_linux_path_ignores_a_redirected_xdg_data_home(self, tmp_path: Path) -> None:
        """A redirected data home would land outside the agent-write fence.

        The fence that makes this store unwritable by agent file tools is anchored
        at the fixed path, so honouring the variable would let an agent author the
        rows this reader trusts -- forging an identity that keeps matching, so the
        children signed in as the previous account are never retired.
        """

        path = kp.kiro_identity_store_path(
            "linux", tmp_path, {"XDG_DATA_HOME": str(tmp_path / "forged")}
        )
        assert "forged" not in str(path)
        assert path == tmp_path / ".local" / "share" / "kiro-cli" / "data.sqlite3"

    def test_no_platform_consults_the_environment(self, tmp_path: Path) -> None:
        """Same rule on every platform, so one branch cannot drift from another."""

        hostile = {
            "XDG_DATA_HOME": str(tmp_path / "forged"),
            "APPDATA": str(tmp_path / "forged"),
            "LOCALAPPDATA": str(tmp_path / "forged"),
            "HOME": str(tmp_path / "forged"),
        }
        for platform_name in ("linux", "darwin", "win32"):
            path = kp.kiro_identity_store_path(platform_name, tmp_path, hostile)
            assert "forged" not in str(path), platform_name
            assert str(path).startswith(str(tmp_path)), platform_name

    def test_darwin_path(self, tmp_path: Path) -> None:
        assert kp.kiro_identity_store_path("darwin", tmp_path, {}) == (
            tmp_path / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"
        )

    def test_windows_defaults_to_local_when_no_store_exists(self, tmp_path: Path) -> None:
        """Current kiro-cli writes under AppData/Local; that is the anchor.

        Anchoring at the legacy Roaming location would make every fingerprint
        "absent" on a current host, so a logout would look identical to a
        signed-in state and no child would ever be retired there.
        """

        path = kp.kiro_identity_store_path("win32", tmp_path, {})
        # Anchor on the tail BELOW the home we passed, never on global parts: on
        # Windows CI tmp_path itself lives under AppData\Local\Temp, so a bare
        # `"Roaming" not in path.parts` asserts something about the fixture's
        # prefix rather than about which directory this function chose.
        assert path.relative_to(tmp_path).parts == (
            "AppData",
            "Local",
            "kiro-cli",
            "data.sqlite3",
        )

    def test_windows_current_layout_resolves_local(self, tmp_path: Path) -> None:
        local = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        local.parent.mkdir(parents=True)
        local.touch()
        assert kp.kiro_identity_store_path("win32", tmp_path, {}) == local

    def test_windows_legacy_roaming_only_host_falls_back(self, tmp_path: Path) -> None:
        """Older kiro-cli layouts kept the store under Roaming; keep reading them."""

        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        roaming.parent.mkdir(parents=True)
        roaming.touch()
        assert kp.kiro_identity_store_path("win32", tmp_path, {}) == roaming

    def test_windows_both_present_reads_the_most_recently_written(self, tmp_path: Path) -> None:
        """With both layouts present, the live store is the one being written.

        An upgraded host carries a stale Roaming leftover next to its live
        Local store; a downgraded host writes Roaming next to a stale Local
        leftover. Preferring either fixed side would read the leftover on the
        other shape -- a confident fingerprint of an account nobody is signed
        into -- so recency decides. Both paths are inside the agent-write
        fence, so the timestamp is as trustworthy as the rows themselves.
        """

        local = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        for db in (local, roaming):
            db.parent.mkdir(parents=True)
            db.touch()

        os.utime(local, (1_000_000, 1_000_000))
        os.utime(roaming, (2_000_000, 2_000_000))
        assert kp.kiro_identity_store_path("win32", tmp_path, {}) == roaming

        os.utime(local, (3_000_000, 3_000_000))
        assert kp.kiro_identity_store_path("win32", tmp_path, {}) == local

        # Equal timestamps prefer Local, the current layout.
        os.utime(roaming, (3_000_000, 3_000_000))
        assert kp.kiro_identity_store_path("win32", tmp_path, {}) == local

    def test_windows_recency_counts_the_wal_sidecar(self, tmp_path: Path) -> None:
        """A commit in WAL mode advances the -wal file, not the main file.

        An actively-written store can have a frozen main-file mtime until the
        next checkpoint, so recency compares the newest of (db, db-wal) per
        side -- otherwise the live side loses the tie-break to a stale main
        file that merely got touched later.
        """

        local = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        roaming = tmp_path / "AppData" / "Roaming" / "kiro-cli" / "data.sqlite3"
        for db in (local, roaming):
            db.parent.mkdir(parents=True)
            db.touch()
        wal = roaming.with_name(roaming.name + "-wal")
        wal.touch()

        # Roaming main file is old, but its WAL carries the newest write.
        os.utime(roaming, (1_000_000, 1_000_000))
        os.utime(local, (2_000_000, 2_000_000))
        os.utime(wal, (3_000_000, 3_000_000))
        assert kp.kiro_identity_store_path("win32", tmp_path, {}) == roaming

    def test_windows_path_ignores_a_redirected_appdata(self, tmp_path: Path) -> None:
        """Fixed anchor, not %APPDATA%.

        The fence that makes this store unwritable by agent file tools is
        home-anchored, so an env-redirected path would land outside it where the
        contents are forgeable.
        """

        path = kp.kiro_identity_store_path(
            "win32",
            tmp_path,
            {"APPDATA": str(tmp_path / "evil"), "LOCALAPPDATA": str(tmp_path / "evil")},
        )
        assert "evil" not in str(path)

    def test_staging_mappings_are_untouched_by_this_change(self, tmp_path: Path) -> None:
        """Sign-in STAGING keeps its own behaviour; only the fingerprint moved."""

        mappings = kp._auth_store_mappings("linux", tmp_path, {})
        sources = {str(m.source) for m in mappings}
        assert any("kiro-cli" in s for s in sources)
        assert any("amazon-q" in s for s in sources)
        assert any(".aws/sso/cache" in s.replace("\\", "/") for s in sources)


class _FakeSemaphore:
    def __init__(self, locked: bool) -> None:
        self._locked = locked

    def locked(self) -> bool:
        return self._locked


class _FakeProvider:
    def __init__(self, backend: str, *, sid: str = "") -> None:
        self.backend = backend
        self.client = SimpleNamespace(_session_id=sid, backend=backend)
        self.cwd = ""
        self.shutdown_calls = 0

    @property
    def uses_kiro_identity_store(self) -> bool:
        from kiro_crew.acp.types import backends_retired_by_host_logout

        return self.backend in backends_retired_by_host_logout()

    def is_process_alive(self) -> bool:
        return True

    def is_alive(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _DeadProvider(_FakeProvider):
    """A provider whose child has already exited.

    Drives the dead-provider removal branch in ``_get_or_create_impl``, the one
    path that pops a session without going through ``_evict_stale_session``.
    """

    def is_process_alive(self) -> bool:
        return False

    def is_alive(self) -> bool:
        return False


class _FakeRuntime:
    """Stands in for AcpRuntime in the retirement sweep."""

    def __init__(
        self, backend: str = "", *, active: bool = False, initializing: bool = False
    ) -> None:
        self._acp_backend = backend
        self._active = active
        self._initializing = initializing
        self.killed = 0
        self.kill_reasons: list[str] = []

    @property
    def uses_kiro_identity_store(self) -> bool:
        from kiro_crew.acp.types import backends_retired_by_host_logout

        return self._acp_backend in backends_retired_by_host_logout()

    def has_active_sessions(self) -> bool:
        return self._active

    def has_active_or_initializing_sessions(self) -> bool:
        return self._active or self._initializing

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        # Mirrors the real signature: a double that refuses ``reason`` turns an
        # attributed kill into a swallowed TypeError, which the sweep logs as a
        # failed teardown instead of performing one.
        self.killed += 1
        self.kill_reasons.append(reason)


class TestStoreRelocation:
    """A redirected store must report absent, not read a leftover default DB."""

    def test_xdg_relocation_is_detected(self, tmp_path: Path) -> None:
        assert kp.identity_store_is_relocated(
            "linux", tmp_path, {"XDG_DATA_HOME": str(tmp_path / "elsewhere")}
        )

    def test_xdg_set_to_the_default_is_not_a_relocation(self, tmp_path: Path) -> None:
        assert not kp.identity_store_is_relocated(
            "linux", tmp_path, {"XDG_DATA_HOME": str(tmp_path / ".local" / "share")}
        )

    def test_unset_and_blank_are_not_relocations(self, tmp_path: Path) -> None:
        assert not kp.identity_store_is_relocated("linux", tmp_path, {})
        assert not kp.identity_store_is_relocated("linux", tmp_path, {"XDG_DATA_HOME": "   "})

    def test_windows_appdata_relocation_is_detected(self, tmp_path: Path) -> None:
        assert kp.identity_store_is_relocated(
            "win32", tmp_path, {"APPDATA": str(tmp_path / "elsewhere")}
        )
        assert not kp.identity_store_is_relocated(
            "win32", tmp_path, {"APPDATA": str(tmp_path / "AppData" / "Roaming")}
        )

    def test_either_appdata_redirect_relocates_regardless_of_stores(self, tmp_path: Path) -> None:
        """A fixed-anchor DB under redirection cannot be attributed to a live writer.

        The CLI resolves its data dir from LOCALAPPDATA (current layout) or
        APPDATA (legacy layout), and which generation is writing cannot be
        observed. Once either variable is redirected, a database at a fixed
        anchor may be a leftover of either layout, and reading a leftover
        yields a confident fingerprint of an account nobody is signed into --
        so the guard refuses to guess, whatever stores exist. Absent is the
        module's safe side, and this matches the pre-change posture for
        Group-Policy Roaming redirection (the anchor then lived under
        Roaming), so redirected enterprise hosts lose nothing they had.
        """

        local = tmp_path / "AppData" / "Local" / "kiro-cli" / "data.sqlite3"
        local.parent.mkdir(parents=True)
        local.touch()
        # Even with a healthy Local store, either redirect relocates.
        assert kp.identity_store_is_relocated(
            "win32", tmp_path, {"APPDATA": str(tmp_path / "elsewhere")}
        )
        assert kp.identity_store_is_relocated(
            "win32", tmp_path, {"LOCALAPPDATA": str(tmp_path / "elsewhere")}
        )
        # Both variables at their defaults is never a relocation.
        assert not kp.identity_store_is_relocated(
            "win32",
            tmp_path,
            {
                "APPDATA": str(tmp_path / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(tmp_path / "AppData" / "Local"),
            },
        )

    def test_localappdata_relocation_is_detected(self, tmp_path: Path) -> None:
        """LOCALAPPDATA moves the local app-data home where the identity now lives."""

        assert kp.identity_store_is_relocated(
            "win32", tmp_path, {"LOCALAPPDATA": str(tmp_path / "elsewhere")}
        )
        assert not kp.identity_store_is_relocated(
            "win32", tmp_path, {"LOCALAPPDATA": str(tmp_path / "AppData" / "Local")}
        )

    @pytest.mark.asyncio
    async def test_a_leftover_default_store_is_not_read_when_relocated(
        self, tmp_path: Path
    ) -> None:
        """The failure mode: a stale DB at the default path would pin an old account.

        Reading it yields a confident fingerprint of an account nobody is signed
        into, so a logout in the REAL store changes nothing we can see and the
        old-account child is reused -- worse than reporting "cannot tell".
        """

        # A leftover database at the default location, with a real identity in it.
        leftover = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(leftover)
        assert kp.identity_fingerprint(leftover) != ""

        service = kp.KiroPrerequisiteService(
            home=tmp_path,
            environ={"XDG_DATA_HOME": str(tmp_path / "elsewhere")},
            platform_name="linux",
        )
        assert await service.current_identity_fingerprint(allow_cached=False) == ""

    @pytest.mark.asyncio
    async def test_the_default_store_is_still_read_when_not_relocated(self, tmp_path: Path) -> None:
        """The refusal must not disable the ordinary case."""

        _write_store(kp.kiro_identity_store_path("linux", tmp_path, {}))
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.current_identity_fingerprint(allow_cached=False) != ""


class TestProviderMembership:
    def test_kiro_backend_is_a_member(self) -> None:
        from kiro_crew.session import _provider_uses_kiro_identity_store

        assert _provider_uses_kiro_identity_store(_FakeProvider(""))

    def test_unknown_backend_fails_closed(self) -> None:
        """An object that declares nothing must be left running, not recycled."""

        from kiro_crew.session import _provider_uses_kiro_identity_store

        assert not _provider_uses_kiro_identity_store(object())
        assert not _provider_uses_kiro_identity_store(_FakeProvider("claude"))

    def test_the_capability_is_declared_on_the_provider_abc(self) -> None:
        """harness-parity H14: the session layer reads a declared capability.

        Probing private shapes (``_client``, ``_acp_backend``) would silently
        misclassify an adapted provider. The base declares it with a safe default
        so a harness that never states the claim cannot inherit it.
        """

        from kiro_crew.providers.base import LLMProvider

        assert "uses_kiro_identity_store" in vars(LLMProvider)
        assert LLMProvider.uses_kiro_identity_store.fget(object()) is False  # type: ignore[attr-defined]

    def test_a_non_declaring_provider_is_not_swept(self) -> None:
        from kiro_crew.session import _provider_uses_kiro_identity_store

        class _Bare:
            pass

        assert _provider_uses_kiro_identity_store(_Bare()) is False


class TestIdentityChangePredicate:
    @pytest.mark.asyncio
    async def test_no_change_before_the_first_probe(self, tmp_path: Path) -> None:
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.identity_changed_since_probe() is False

    @pytest.mark.asyncio
    async def test_change_detected_against_the_recorded_identity(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        # Stand in for a completed probe: the latch was written while the store
        # named this account.
        service._stamp_probe(await service.current_identity_fingerprint())
        assert await service.identity_changed_since_probe() is False
        _write_store(db, start_url="https://personal.awsapps.com/start")
        _expire_identity_cache(service)
        assert await service.identity_changed_since_probe() is True

    @pytest.mark.asyncio
    async def test_a_logout_before_the_first_turn_is_still_detected(self, tmp_path: Path) -> None:
        """A child can exist before any turn (eager spawn / warm pool)."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live == ""

    @pytest.mark.asyncio
    async def test_an_unset_baseline_reports_changed(self, tmp_path: Path) -> None:
        """ "We do not know" must not resolve to "the children match".

        Readiness is probed a few seconds AFTER boot while a session can be
        spawned eagerly before it. A logout landing in that gap would otherwise be
        adopted as the starting point, and the pre-logout child would keep
        answering as the previous account with nothing left to detect it.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        # No probe has run and nothing has been reconciled.
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live != ""

    @pytest.mark.asyncio
    async def test_a_logout_before_the_delayed_probe_is_detected(self, tmp_path: Path) -> None:
        """The eager-spawn-before-probe window GPT identified.

        Child spawns under account A, the terminal logs out, and only THEN does
        the delayed boot probe run. Seeding the baseline from that probe would
        record the post-logout identity and strand the child.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        # Logout happens BEFORE the probe.
        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()

        # The delayed probe now runs and sees the signed-out store.
        service._stamp_probe(await service.current_identity_fingerprint())

        # The pre-logout child must still be swept.
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True

    @pytest.mark.asyncio
    async def test_probes_never_move_the_session_baseline(self, tmp_path: Path) -> None:
        """Only a completed sweep advances it; a probe never does."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        assert service._session_identity is None

        _write_store(db, start_url="https://personal.awsapps.com/start")
        service._stamp_probe(await service.current_identity_fingerprint())
        assert service._session_identity is None

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        service.note_sessions_reconciled(live)
        changed_after, _ = await service.identity_changed_since_sessions()
        assert changed_after is False

    @pytest.mark.asyncio
    async def test_assume_ready_never_reports_a_change(self, tmp_path: Path) -> None:
        service = kp.KiroPrerequisiteService(
            home=tmp_path, environ={}, platform_name="linux", assume_ready=True
        )
        service._stamp_probe("something")
        assert await service.identity_changed_since_probe() is False


class TestBaselinesAreIndependent:
    """The status consumer must not be able to consume the retirement signal."""

    @pytest.mark.asyncio
    async def test_a_status_reprobe_does_not_hide_the_change_from_retirement(
        self, tmp_path: Path
    ) -> None:
        """The defect this pair of baselines exists to prevent.

        With ONE shared baseline: logout -> a status poll re-probes and stamps the
        new identity -> the next turn sees no change -> the stale child is never
        retired. The dashboard polls every few seconds and turns are minutes
        apart, so the poll essentially always wins that race.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        # Reconcile the retirement baseline explicitly -- an unset one now reports
        # changed, so the sweep has to have happened before this scenario starts.
        _, live0 = await service.identity_changed_since_sessions()
        service.note_sessions_reconciled(live0)
        assert await service.identity_changed_since_probe() is False
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

        # The account changes.
        _write_store(db, start_url="https://personal.awsapps.com/start")
        _expire_identity_cache(service)

        # The status consumer observes it FIRST and advances its own baseline,
        # exactly as an ordinary poll's re-probe does.
        assert await service.identity_changed_since_probe() is True
        service._stamp_probe(await service.current_identity_fingerprint())
        assert await service.identity_changed_since_probe() is False

        # Retirement must STILL see the change.
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live != ""

    @pytest.mark.asyncio
    async def test_the_session_baseline_advances_only_when_told(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        await service.identity_changed_since_sessions()  # adopt

        _write_store(db, start_url="https://personal.awsapps.com/start")
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        # Re-asking without reconciling keeps reporting the change, so a failed
        # retirement is retried rather than silently recorded as handled.
        changed_again, _ = await service.identity_changed_since_sessions()
        assert changed_again is True

        service.note_sessions_reconciled(live)
        changed_after, _ = await service.identity_changed_since_sessions()
        assert changed_after is False

    @pytest.mark.asyncio
    async def test_an_unreconciled_baseline_never_reads_as_matching(self, tmp_path: Path) -> None:
        """Repeated asks keep reporting changed until a sweep reconciles.

        Replaces an earlier "first call adopts" behaviour, which was the hole GPT
        found: adopting on first read trusts children that may predate the read.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        first, live = await service.identity_changed_since_sessions()
        second, _ = await service.identity_changed_since_sessions()
        assert first is True
        assert second is True

        service.note_sessions_reconciled(live)
        after, _ = await service.identity_changed_since_sessions()
        assert after is False


class TestLatchNarrowingPolicy:
    """Narrowing readiness is right for a sign-out and wrong for a switch."""

    class _State:
        def __init__(self, service: object, sessions: object) -> None:
            self.kiro_prerequisite_service = service
            self.sessions = sessions

    class _Sessions:
        def __init__(self, complete: bool = True) -> None:
            self._complete = complete
            self.calls = 0

        async def retire_kiro_identity_sessions(self, fingerprint: str = ""):
            self.calls += 1
            return ([], self._complete)

    @pytest.mark.asyncio
    async def test_a_switch_to_a_valid_account_does_not_narrow_readiness(
        self, tmp_path: Path
    ) -> None:
        """The stuck-readiness sequence.

        A status poll observes the switch FIRST and stamps the new identity. If the
        turn path then narrowed unconditionally, readiness would go false while the
        fingerprints now MATCH -- so no ordinary poll re-probes and the card sits
        at "not signed in" until someone presses Check again.
        """

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        # Switch to another VALID account, then let a poll observe it first.
        _write_store(db, start_url="https://personal.awsapps.com/start")
        service._stamp_probe(await service.current_identity_fingerprint())
        service._status = type(service._status)(  # type: ignore[misc]
            **{**vars(service._status), "authenticated": True, "ready": True}
        )

        state = self._State(service, self._Sessions())
        await chat_runner._retire_sessions_on_identity_change(state)

        assert service._status.ready is True, "readiness was narrowed on a valid switch"

    @pytest.mark.asyncio
    async def test_an_actual_sign_out_does_narrow_readiness(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())
        service._status = type(service._status)(  # type: ignore[misc]
            **{**vars(service._status), "authenticated": True, "ready": True}
        )

        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        _expire_identity_cache(service)

        state = self._State(service, self._Sessions())
        await chat_runner._retire_sessions_on_identity_change(state)

        assert service._status.ready is False
        assert service._status.authenticated is False

    @pytest.mark.asyncio
    async def test_an_incomplete_sweep_leaves_the_change_pending(self, tmp_path: Path) -> None:
        """A skipped holder must not be recorded as reconciled."""

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=False)
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 1

        # Still pending, so the next turn tries again.
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 2

    @pytest.mark.asyncio
    async def test_an_incomplete_sweep_warns_and_names_what_it_waits_on(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO sits below the default level, so the holdout was invisible."""

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=False)
        sessions.identity_sweep_waiting_on = ("channel:c1:m1 (channel member)",)  # type: ignore[attr-defined]
        state = self._State(service, sessions)

        with caplog.at_level(logging.INFO, logger="kiro_crew.dashboard.chat_runner"):
            await chat_runner._retire_sessions_on_identity_change(state)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, caplog.text
        assert "incomplete" in warnings[0].getMessage()
        assert "channel:c1:m1 (channel member)" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_complete_sweep_reconciles_once(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service._stamp_probe(await service.current_identity_fingerprint())

        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=True)
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_store_is_never_reconciled(self, tmp_path: Path) -> None:
        """ "Cannot tell" must not become the accepted steady state.

        Reconciling an empty fingerprint would make every LATER account switch
        compare equal to "" and go undetected, while children keep running. Staying
        unreconciled re-sweeps each turn, bounding how long a child can outlive the
        account it loaded to one turn.
        """

        from kiro_crew.dashboard import chat_runner

        # No store on disk at all: the fingerprint is absent.
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        sessions = self._Sessions(complete=True)
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)

        # Every turn re-sweeps rather than accepting the unreadable state.
        assert sessions.calls == 3
        assert service._session_identity is None


class TestReturnToBaselineAfterIncompleteSweep:
    """A switch BACK to the reconciled account must still retire interim holders.

    The baseline advances only on a COMPLETE sweep. So after an A->B sweep left a
    busy session behind, returning to A compares equal to the baseline and the
    identity check would see no change -- while successors that registered under B
    keep answering on B's credential, with no auth failure to report it.
    """

    class _State:
        def __init__(self, service: object, sessions: object) -> None:
            self.kiro_prerequisite_service = service
            self.sessions = sessions

    class _Sessions:
        """Mirrors the lifecycle service's pending-fingerprint bookkeeping."""

        def __init__(self, complete: bool = True) -> None:
            self._complete = complete
            self.calls = 0
            self.swept_with: list[str] = []
            self.pending_identity_sweep_fingerprint = ""

        async def retire_kiro_identity_sessions(self, fingerprint: str = ""):
            self.calls += 1
            self.swept_with.append(fingerprint)
            # Kept while a sweep stays incomplete; cleared the moment one completes.
            self.pending_identity_sweep_fingerprint = "" if self._complete else fingerprint
            return ([], self._complete)

    @pytest.mark.asyncio
    async def test_returning_to_the_baseline_account_still_sweeps(self, tmp_path: Path) -> None:
        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        fp_a = await service.current_identity_fingerprint()
        service._stamp_probe(fp_a)
        # Children already match A.
        service.note_sessions_reconciled(fp_a)

        # A -> B, and the sweep cannot finish (a busy session survives it).
        _write_store(db, start_url="https://personal.awsapps.com/start")
        sessions = self._Sessions(complete=False)
        state = self._State(service, sessions)
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 1
        # Incomplete, so the baseline stayed at A while B is outstanding.
        assert service._session_identity == fp_a
        pending_b = sessions.pending_identity_sweep_fingerprint
        assert pending_b and pending_b != fp_a

        # Back to A. The baseline now MATCHES the live account, but TWO
        # independent triggers still reach the B successors: the gate's own
        # fresh read observed B and latched (the interim-identity latch), and
        # the incomplete sweep recorded B as the pending fingerprint. Either
        # alone forces the retry; both are pinned here.
        _write_store(db)
        assert (await service.identity_changed_since_sessions())[
            0
        ] is True, "the observed interim account must latch and report changed"

        # Isolate the pending-fingerprint retry: with the latch cleared (as if
        # the observation had never happened), the identity predicate reports
        # no change -- and the B holders must STILL be swept via the pending
        # fingerprint alone.
        service._interim_identity_observed = False
        assert (await service.identity_changed_since_sessions())[0] is False

        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 2, "the B holders were left serving under B"
        # The retry is captured afresh under the account now in use, not B's.
        assert sessions.swept_with[-1] != pending_b

    @pytest.mark.asyncio
    async def test_an_incomplete_sweep_retries_even_for_the_live_account(
        self, tmp_path: Path
    ) -> None:
        """Completion is the stop condition, not a fingerprint match.

        This trigger sweeps with the LIVE fingerprint while the baseline equals it,
        so the pending fingerprint an incomplete one records is the live account's.
        Stopping on that equality would abandon the retry with holders still to
        retire; only a sweep that COMPLETES clears the pending fingerprint.
        """

        from kiro_crew.dashboard import chat_runner

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        fp_a = await service.current_identity_fingerprint()
        service._stamp_probe(fp_a)
        service.note_sessions_reconciled(fp_a)

        # An outstanding sweep FOR the live account, still incomplete.
        sessions = self._Sessions(complete=False)
        sessions.pending_identity_sweep_fingerprint = fp_a
        state = self._State(service, sessions)

        await chat_runner._retire_sessions_on_identity_change(state)
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 2, "an incomplete sweep stopped retrying"

        # A sweep that completes clears the pending fingerprint, which stops it.
        sessions._complete = True
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 3
        assert sessions.pending_identity_sweep_fingerprint == ""
        await chat_runner._retire_sessions_on_identity_change(state)
        assert sessions.calls == 3, "it kept sweeping after completion"


class TestRetirementCoverage:
    """Every holder of a kiro child must be reachable by retirement."""

    @staticmethod
    def _manager():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        # pool_size 0 so construction never pre-spawns; the pool is populated
        # explicitly by the test that cares about it.
        return SessionManager(KiroCrewConfig())

    @staticmethod
    def _stored_sid(smap, key: str) -> str:
        """Read the stored sid WITHOUT ``get``'s transcript-file check.

        ``SessionMap.get`` stats ``<sid>.json`` and prunes an entry whose
        transcript is missing, which would answer "no pointer" for a reason this
        test is not about. The question here is only whether the retirement
        dropped the pointer, so read the entry.
        """

        from kiro_crew.session_map import canonical_key

        entry = smap._session_map._data.get(canonical_key(key)) or {}
        return entry.get("sid", "")

    @staticmethod
    def _session(provider: object, *, busy: bool = False):
        from kiro_crew.session import _Session

        sess = _Session(provider=provider)  # type: ignore[arg-type]
        if busy:
            # Retirement reads semaphore.locked(); drain the permit to say "busy".
            sess.semaphore._value = 0  # type: ignore[attr-defined]
        return sess

    @pytest.mark.asyncio
    async def test_idle_kiro_sessions_are_retired_and_others_left_alone(self) -> None:
        smap = self._manager()
        kiro = _FakeProvider("")
        claude = _FakeProvider("claude")
        smap._sessions["kiro-key"] = self._session(kiro)
        smap._sessions["claude-key"] = self._session(claude)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == ["kiro-key"]
        assert complete is True
        assert "kiro-key" not in smap._sessions
        assert "claude-key" in smap._sessions
        assert kiro.shutdown_calls == 1
        assert claude.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_a_busy_kiro_session_is_not_retired(self) -> None:
        smap = self._manager()
        busy = _FakeProvider("")
        smap._sessions["busy"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        # A skipped session means the change is NOT reconciled.
        assert complete is False
        assert "busy" in smap._sessions
        assert busy.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_selection_and_unregistration_are_one_atomic_step(self) -> None:
        """A session busy at DECISION time is never unregistered or shut down.

        The TOCTOU GPT flagged is closed by doing the idle check and the
        unregistration in a single lock hold. The acquire path takes the
        semaphore OUTSIDE the lock and then validates registration INSIDE it, so
        the two orderings are both safe: acquire-first is visible here as
        ``locked()`` and skipped, while pop-first is caught by that validation
        (covered by the next test).
        """

        smap = self._manager()
        idle = _FakeProvider("")
        busy = _FakeProvider("")
        smap._sessions["idle"] = self._session(idle)
        smap._sessions["busy"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == ["idle"]
        assert complete is False  # the busy one was left running
        assert "busy" in smap._sessions
        assert busy.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_a_turn_can_never_stream_on_a_retired_provider(self) -> None:
        """The other half of the race: a turn that acquires AFTER the pop.

        It re-validates registration under the same lock, finds the entry gone,
        releases its semaphore and reports invalid -- so the caller cold starts a
        replacement on the current account instead of streaming on the retired
        child. Without that, retirement would be racing every in-flight acquire.
        """

        smap = self._manager()
        provider = _FakeProvider("")
        sess = self._session(provider)
        smap._sessions["key"] = sess

        await smap.retire_kiro_identity_sessions()
        assert "key" not in smap._sessions

        # A turn that had not yet acquired when the sweep ran now tries to.
        still_valid = await smap._reacquire_and_validate("key", sess)

        assert still_valid is False
        # The contract is that an invalid result has ALREADY released the
        # semaphore; a leaked permit would deadlock the key forever.
        assert not sess.semaphore.locked()

    @pytest.mark.asyncio
    async def test_pooled_kiro_providers_are_discarded(self) -> None:
        """A warm provider spawned pre-change would otherwise be handed to a
        brand-new session, running it as the previous account."""

        smap = self._manager()
        pooled_kiro = _FakeProvider("")
        pooled_other = _FakeProvider("claude")
        smap._warm_pool.put_nowait((pooled_kiro, 0.0))
        smap._warm_pool.put_nowait((pooled_other, 0.0))

        await smap.retire_kiro_identity_sessions()

        assert pooled_kiro.shutdown_calls == 1
        assert pooled_other.shutdown_calls == 0
        # The non-kiro entry is put back, not dropped on the floor.
        assert smap._warm_pool.qsize() == 1
        survivor, _ = smap._warm_pool.get_nowait()
        assert survivor is pooled_other

    @pytest.mark.asyncio
    async def test_kiro_subagent_runtimes_are_retired(self) -> None:
        smap = self._manager()

        kiro_runtime = _FakeRuntime("")
        other_runtime = _FakeRuntime("claude")
        smap._subagent_runtimes["parent-kiro"] = kiro_runtime  # type: ignore[assignment]
        smap._subagent_runtimes["parent-other"] = other_runtime  # type: ignore[assignment]

        await smap.retire_kiro_identity_sessions()

        assert kiro_runtime.killed == 1
        assert kiro_runtime.kill_reasons == ["subagent runtime released"]
        assert other_runtime.killed == 0
        assert "parent-other" in smap._subagent_runtimes

    @pytest.mark.asyncio
    async def test_the_shared_background_runtime_is_retired(self) -> None:
        """One process serves all background work and outlives every session, so
        the session sweep cannot reach it."""

        smap = self._manager()

        bg = _FakeRuntime("")
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert bg.killed == 1
        assert smap._bg_runtime is None
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_non_kiro_background_runtime_is_left_alone(self) -> None:
        smap = self._manager()

        bg = _FakeRuntime("claude")
        smap._bg_runtime = bg  # type: ignore[assignment]

        await smap.retire_kiro_identity_sessions()

        assert bg.killed == 0
        assert smap._bg_runtime is bg

    @pytest.mark.asyncio
    async def test_an_active_background_runtime_is_spared_and_reported_incomplete(self) -> None:
        """One process serves every background caller, so killing it mid-flight
        drops work belonging to callers unrelated to the account change.

        Same principle as a busy session: spare it, report the sweep incomplete so
        the change stays pending, and retire it once it drains.
        """

        smap = self._manager()
        bg = _FakeRuntime("", active=True)
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert bg.killed == 0
        assert smap._bg_runtime is bg
        assert complete is False

    @pytest.mark.asyncio
    async def test_an_active_subagent_runtime_is_spared_and_reported_incomplete(self) -> None:
        smap = self._manager()
        busy_runtime = _FakeRuntime("", active=True)
        smap._subagent_runtimes["parent"] = busy_runtime  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert busy_runtime.killed == 0
        assert "parent" in smap._subagent_runtimes
        assert complete is False

    # -- The live-account spare ---------------------------------------------
    #
    # A holder whose spawn stamp EQUALS the live fingerprint provably
    # authenticated as the live account. Retiring it buys nothing and costs
    # its in-flight children, and counting it against completeness keeps a
    # sweep on a busy host from finishing: every turn re-sweeps and every
    # idle parent's subagents die as "provider shutdown".

    LIVE = "cli-live"

    @pytest.mark.asyncio
    async def test_an_idle_session_stamped_with_the_live_account_is_spared(self) -> None:
        smap = self._manager()
        spared = _FakeProvider("")
        spared.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        unstamped = _FakeProvider("")
        other = _FakeProvider("")
        other.spawn_identity = "cli-other"  # type: ignore[attr-defined]
        smap._sessions["spared"] = self._session(spared)
        smap._sessions["unstamped"] = self._session(unstamped)
        smap._sessions["other"] = self._session(other)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert sorted(retired) == ["other", "unstamped"]
        assert complete is True
        assert "spared" in smap._sessions
        assert spared.shutdown_calls == 0
        assert not smap._sessions["spared"].retire_on_identity_change
        assert unstamped.shutdown_calls == 1
        assert other.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_a_busy_session_stamped_with_the_live_account_does_not_block_completion(
        self,
    ) -> None:
        """This is the livelock: a busy live-account holder kept every sweep
        incomplete, so the baseline never advanced and the next turn swept
        again. It is not holding the previous account, so it is not a reason
        to retry."""

        smap = self._manager()
        busy = _FakeProvider("")
        busy.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._sessions["busy-live"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is True
        assert "busy-live" in smap._sessions
        assert not smap._sessions["busy-live"].retire_on_identity_change
        assert smap.pending_identity_sweep_fingerprint == ""

    @pytest.mark.asyncio
    async def test_a_demuxed_session_is_spared_through_its_runtime_stamp(self) -> None:
        """A demuxed session's provider carries no stamp of its own; the shared
        runtime it rides does."""

        smap = self._manager()
        provider = _FakeProvider("")
        provider._runtime = SimpleNamespace(spawn_identity=self.LIVE)  # type: ignore[attr-defined]
        smap._sessions["demuxed"] = self._session(provider)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is True
        assert provider.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_the_spare_needs_the_whole_fingerprint_to_match(self) -> None:
        """Sparing is stricter than the mismatch gate: a stamp that agrees on
        the CLI component but lost or differs on the vault component is not
        proof of the live account, so it is retired like any other."""

        smap = self._manager()
        partial = _FakeProvider("")
        partial.spawn_identity = "cli-live"  # type: ignore[attr-defined]
        smap._sessions["partial"] = self._session(partial)
        live = kp._combine_identity_fingerprints("cli-live", "vault-x")

        retired, _ = await smap.retire_kiro_identity_sessions(fingerprint=live)

        assert retired == ["partial"]
        assert partial.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_store_spares_nothing(self) -> None:
        """An empty live fingerprint means "cannot tell"; a stamped holder is
        then retired exactly as before the spare existed (the sign-out path
        sweeps with no fingerprint on purpose)."""

        smap = self._manager()
        stamped = _FakeProvider("")
        stamped.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._sessions["stamped"] = self._session(stamped)

        retired, _ = await smap.retire_kiro_identity_sessions(fingerprint="")

        assert retired == ["stamped"]
        assert stamped.shutdown_calls == 1

    @pytest.mark.asyncio
    async def test_a_live_stamped_subagent_runtime_is_spared_even_when_busy(self) -> None:
        smap = self._manager()
        live_runtime = _FakeRuntime("", active=True)
        live_runtime.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        old_runtime = _FakeRuntime("")
        smap._subagent_runtimes["parent-live"] = live_runtime  # type: ignore[assignment]
        smap._subagent_runtimes["parent-old"] = old_runtime  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert live_runtime.killed == 0
        assert "parent-live" in smap._subagent_runtimes
        assert old_runtime.killed == 1
        assert "parent-old" not in smap._subagent_runtimes
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_release_pinned_to_a_stale_runtime_spares_its_replacement(self) -> None:
        """The sweep decides from a snapshot, then waits for the per-parent
        lock. A respawn holding that lock installs a live-stamped replacement
        under the same key before letting go; the release must not pop it."""

        smap = self._manager()
        stale = _FakeRuntime("")
        replacement = _FakeRuntime("", active=True)
        replacement.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._subagent_runtimes["parent"] = replacement  # type: ignore[assignment]
        smap._subagent_runtime_locks["parent"] = asyncio.Lock()

        released = await smap.release_subagent_runtime("parent", expected=stale)

        assert released is False
        assert replacement.killed == 0
        assert smap._subagent_runtimes["parent"] is replacement
        assert "parent" in smap._subagent_runtime_locks

    @pytest.mark.asyncio
    async def test_a_release_pinned_to_the_registered_runtime_still_kills_it(self) -> None:
        smap = self._manager()
        runtime = _FakeRuntime("")
        smap._subagent_runtimes["parent"] = runtime  # type: ignore[assignment]

        released = await smap.release_subagent_runtime("parent", expected=runtime)

        assert released is True
        assert runtime.killed == 1
        assert "parent" not in smap._subagent_runtimes

    @pytest.mark.asyncio
    async def test_a_replacement_installed_mid_sweep_survives_and_the_sweep_completes(
        self,
    ) -> None:
        """Same interleaving through the sweep itself: the idle probe on the
        stale runtime is the suspension the respawn wins, so a swap there
        models a replacement landing between the snapshot and the release."""

        smap = self._manager()
        replacement = _FakeRuntime("", active=True)
        replacement.spawn_identity = self.LIVE  # type: ignore[attr-defined]

        class _StaleRuntime(_FakeRuntime):
            def has_active_or_initializing_sessions(self) -> bool:
                smap._subagent_runtimes["parent"] = replacement  # type: ignore[assignment]
                return False

        stale = _StaleRuntime("")
        smap._subagent_runtimes["parent"] = stale  # type: ignore[assignment]

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert stale.killed == 0
        assert replacement.killed == 0
        assert smap._subagent_runtimes["parent"] is replacement
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_live_stamped_background_runtime_is_spared_even_when_busy(self) -> None:
        smap = self._manager()
        bg = _FakeRuntime("", active=True)
        bg.spawn_identity = self.LIVE  # type: ignore[attr-defined]
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert bg.killed == 0
        assert smap._bg_runtime is bg
        assert complete is True

    # -- Channel members ----------------------------------------------------
    #
    # A channel member holds its lease for the channel's whole life, so the
    # semaphore test reads it as busy even while it only listens.

    def _channel_member(self, smap, key: str, stamp: str):
        provider = _FakeProvider("")
        if stamp:
            provider.spawn_identity = stamp  # type: ignore[attr-defined]
        smap._sessions[key] = self._session(provider, busy=True)
        assert smap.mark_lifecycle_lease(key), "precondition: the lease was not declared"
        return provider

    @pytest.mark.asyncio
    async def test_a_live_stamped_channel_member_does_not_block_completion(self) -> None:
        smap = self._manager()
        member = self._channel_member(smap, "channel:c1:m1", self.LIVE)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is True
        assert member.shutdown_calls == 0
        assert not smap._sessions["channel:c1:m1"].retire_on_identity_change
        assert smap.pending_identity_sweep_fingerprint == ""
        assert smap.identity_sweep_waiting_on == ()

    @pytest.mark.asyncio
    async def test_an_old_account_channel_member_is_named_as_the_holdout(self) -> None:
        smap = self._manager()
        member = self._channel_member(smap, "channel:c1:m1", "cli-old")
        busy = _FakeProvider("")
        smap._sessions["chat-busy"] = self._session(busy, busy=True)

        retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)

        assert retired == []
        assert complete is False
        assert member.shutdown_calls == 0
        assert smap.identity_sweep_waiting_on == (
            "channel:c1:m1 (channel member)",
            "chat-busy (busy)",
        )

        # Once the holdouts are gone the next sweep completes and clears the list.
        del smap._sessions["channel:c1:m1"]
        del smap._sessions["chat-busy"]
        _, complete = await smap.retire_kiro_identity_sessions(fingerprint=self.LIVE)
        assert complete is True
        assert smap.identity_sweep_waiting_on == ()

    @pytest.mark.asyncio
    async def test_a_provider_mid_start_makes_the_sweep_incomplete(self) -> None:
        """A provider between start() and registration is in none of the maps.

        It already holds whatever the store said when it spawned, so reconciling
        while it is in flight would advance the baseline over it and leave it
        reusable under the previous account once it registers.
        """

        smap = self._manager()
        smap._starting_pids.add(4242)

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is False

    @pytest.mark.asyncio
    async def test_no_in_flight_start_allows_a_complete_sweep(self) -> None:
        smap = self._manager()
        assert not smap._starting_pids

        _, complete = await smap.retire_kiro_identity_sessions()

        assert complete is True

    @pytest.mark.asyncio
    async def test_an_in_flight_runtime_spawn_makes_the_sweep_incomplete(self) -> None:
        """`get_subagent_runtime` holds the per-parent lock across its spawn.

        A runtime being created right now is in no map at all, while it already
        holds whatever the store said when it started. Reconciling would advance
        the baseline over it and leave later subagents running as the previous
        account.
        """

        smap = self._manager()
        lock = asyncio.Lock()
        await lock.acquire()
        smap._subagent_runtime_locks["parent"] = lock

        _, complete = await smap.retire_kiro_identity_sessions()
        assert complete is False

        lock.release()
        _, complete_after = await smap.retire_kiro_identity_sessions()
        assert complete_after is True

    @pytest.mark.asyncio
    async def test_an_idle_runtime_lock_does_not_block_reconciliation(self) -> None:
        """A lock that merely EXISTS is not a spawn in flight."""

        smap = self._manager()
        smap._subagent_runtime_locks["parent"] = asyncio.Lock()

        _, complete = await smap.retire_kiro_identity_sessions()
        assert complete is True

    @pytest.mark.asyncio
    async def test_a_busy_session_is_marked_so_its_next_turn_cannot_reuse_it(self) -> None:
        """Skipping a busy session protected its turn but not the NEXT one.

        `get_or_create` would simply wait for that turn's semaphore and hand the
        same old-account provider to the following turn. Marking it makes the
        post-semaphore re-validate report invalid, so the caller's existing
        stale-provider path evicts and cold starts -- no blocking, no refusal.
        """

        smap = self._manager()
        busy = _FakeProvider("")
        sess = self._session(busy, busy=True)
        smap._sessions["busy"] = sess

        _, complete = await smap.retire_kiro_identity_sessions()

        assert complete is False
        assert sess.retire_on_identity_change is True
        assert busy.shutdown_calls == 0, "the in-flight turn must not be killed"

        # The next turn releases and re-validates: the mark makes it invalid.
        sess.semaphore.release()
        still_valid = await smap._reacquire_and_validate("busy", sess)
        assert still_valid is False
        assert not sess.semaphore.locked(), "an invalid result must release the permit"

    @pytest.mark.asyncio
    async def test_an_unmarked_session_still_validates_normally(self) -> None:
        """The mark must not make every re-validate fail."""

        smap = self._manager()
        provider = _FakeProvider("claude")
        sess = self._session(provider)
        smap._sessions["ok"] = sess

        assert sess.retire_on_identity_change is False
        still_valid = await smap._reacquire_and_validate("ok", sess)
        assert still_valid is True
        smap.release("ok")

    @pytest.mark.asyncio
    async def test_a_runtime_surviving_the_sweep_reports_incomplete(self) -> None:
        """Post-condition, not a window enumeration.

        A companion spawn that COMPLETES between the runtime snapshot and the final
        lock check is in neither -- its lock is released and it was not in the
        snapshot. Asserting that no kiro-backed runtime is LEFT catches anything
        installed while we swept, whatever the timing.
        """

        smap = self._manager()
        survivor = _FakeRuntime("")
        smap._subagent_runtimes["parent"] = survivor  # type: ignore[assignment]

        # Simulate a release that does not actually remove it (equivalently: a
        # runtime installed after the snapshot was taken).
        async def _noop_release(key: str) -> None:
            return None

        smap.release_subagent_runtime = _noop_release  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert "parent" in smap._subagent_runtimes
        assert complete is False, "a surviving kiro-backed runtime must not reconcile"

    @pytest.mark.asyncio
    async def test_a_non_kiro_runtime_surviving_is_fine(self) -> None:
        """The post-condition must only count runtimes this sweep owns."""

        smap = self._manager()
        smap._subagent_runtimes["parent"] = _FakeRuntime("claude")  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert complete is True

    @pytest.mark.asyncio
    async def test_an_initializing_session_protects_a_runtime(self) -> None:
        """`create_session` registers its queue OUTSIDE the runtime lock.

        So a session whose `session/new` is in flight is invisible to
        `has_active_sessions()`, and killing the runtime under it surfaces as
        `AcpRuntimeDead` on work the user never connected to an account change.
        The stale-runtime recycle path tolerates that window because a respawn
        loop backstops it; this sweep has no such backstop, so it must not.
        """

        smap = self._manager()
        initializing = _FakeRuntime("", initializing=True)
        smap._subagent_runtimes["parent"] = initializing  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert initializing.killed == 0
        assert "parent" in smap._subagent_runtimes
        assert complete is False

    @pytest.mark.asyncio
    async def test_an_initializing_session_protects_the_background_runtime(self) -> None:
        smap = self._manager()
        bg = _FakeRuntime("", initializing=True)
        smap._bg_runtime = bg  # type: ignore[assignment]

        _, complete = await smap.retire_kiro_identity_sessions()

        assert bg.killed == 0
        assert smap._bg_runtime is bg
        assert complete is False

    @pytest.mark.asyncio
    async def test_the_runtime_predicate_counts_inits_in_flight(self) -> None:
        """Pins the real AcpRuntime property, not just the test double."""

        from kiro_crew.acp.runtime import AcpRuntime

        runtime = AcpRuntime.__new__(AcpRuntime)
        runtime._session_queues = {}  # type: ignore[attr-defined]
        runtime._session_inits_in_flight = 0  # type: ignore[attr-defined]
        assert runtime.has_active_sessions() is False
        assert runtime.has_active_or_initializing_sessions() is False

        runtime._session_inits_in_flight = 1  # type: ignore[attr-defined]
        # The old predicate still reports idle -- that is the window.
        assert runtime.has_active_sessions() is False
        assert runtime.has_active_or_initializing_sessions() is True

    @pytest.mark.asyncio
    async def test_two_concurrent_sweeps_do_not_deadlock(self) -> None:
        """Two sweeps at once are serialized, and both complete.

        Each sweep takes every cold-start permit through ``PrioritySemaphore.drain``,
        which is not re-entrant: a second drain started while one is pending raises.
        ``_identity_sweep_lock`` is what lets the second sweep wait for the first
        instead. Two concurrent sweeps are the common boot case, not an exotic one:
        with ``_session_identity`` unset, every in-flight turn sees a change at once.
        """
        from kiro_crew.start_priority import StartPriority

        smap = self._manager()
        # Every permit held -- background its full width, a person the reserve -- so
        # both sweeps are waiting before any permit is free.
        holds = [StartPriority.BACKGROUND] * _MAX_COLD_STARTS_FOR_TEST + [
            StartPriority.FOREGROUND
        ] * _COLD_START_RESERVE_FOR_TEST
        for priority in holds:
            await asyncio.wait_for(smap._start_sem.acquire(priority), timeout=5.0)

        first = asyncio.create_task(smap.retire_kiro_identity_sessions())
        second = asyncio.create_task(smap.retire_kiro_identity_sessions())
        await asyncio.sleep(0.05)
        assert not first.done() and not second.done()

        for priority in holds:
            smap._start_sem.release(priority)
            await asyncio.sleep(0)

        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)
        assert all(complete for _, complete in results)
        # Every permit returned, so later cold starts are unaffected.
        assert smap._start_sem._value == smap._start_sem._limit

    @pytest.mark.asyncio
    async def test_the_barrier_waits_for_an_in_flight_cold_start(self) -> None:
        """The scan must be authoritative, so it WAITS for every permit.

        A partial barrier is not enough: reporting "incomplete" defers the baseline
        but does not stop the current turn, so an eager session spawned under the
        previous account would still win registration and serve it.
        """

        from kiro_crew.start_priority import StartPriority

        smap = self._manager()
        await asyncio.wait_for(smap._start_sem.acquire(StartPriority.BACKGROUND), timeout=5.0)

        task = asyncio.create_task(smap.retire_kiro_identity_sessions())
        await asyncio.sleep(0.05)
        assert not task.done(), "the sweep scanned while a cold start was in flight"

        smap._start_sem.release(StartPriority.BACKGROUND)
        retired, complete = await asyncio.wait_for(task, timeout=2.0)
        assert complete is True
        assert retired == []

    @pytest.mark.asyncio
    async def test_the_barrier_releases_every_permit_it_took(self) -> None:
        """A leaked permit would shrink cold-start concurrency for the process."""

        smap = self._manager()
        before = smap._start_sem._value
        await smap.retire_kiro_identity_sessions()
        assert smap._start_sem._value == before

    @pytest.mark.asyncio
    async def test_permits_are_released_even_when_the_scan_raises(self) -> None:
        """The release must be in a `finally`, or one failure degrades the process."""

        smap = self._manager()
        before = smap._start_sem._value

        class _Boom(Exception):
            pass

        async def _explode() -> bool:
            raise _Boom()

        smap._retire_kiro_warm_pool = _explode  # type: ignore[assignment]
        with pytest.raises(_Boom):
            await smap.retire_kiro_identity_sessions()
        assert smap._start_sem._value == before

    @pytest.mark.asyncio
    async def test_the_pool_drain_holds_the_fill_lock(self) -> None:
        """An in-flight fill must not land a pre-change child behind the sweep.

        Without the lock the sweep reads an empty queue, the outstanding spawn
        completes, and a provider authenticated as the old account is enqueued for
        a later session to claim.
        """

        smap = self._manager()
        observed: list[bool] = []

        original = smap._retire_kiro_warm_pool

        async def watched() -> bool:
            observed.append(smap._pool_fill_lock.locked())
            return await original()

        smap._retire_kiro_warm_pool = watched  # type: ignore[assignment]

        # Hold the fill lock as an in-flight fill would, and confirm the drain
        # cannot proceed until it is released.
        await smap._pool_fill_lock.acquire()
        task = asyncio.create_task(smap.retire_kiro_identity_sessions())
        await asyncio.sleep(0.05)
        assert not task.done(), "drain proceeded while a fill held the lock"
        smap._pool_fill_lock.release()
        await task

        assert observed, "the drain never ran"

    @pytest.mark.asyncio
    async def test_a_retired_session_loses_its_native_conversation_pointer(self) -> None:
        """The successor must not ``session/load`` the previous account's history.

        An extended-thinking model's stored thinking blocks carry a provider
        signature bound to the conversation they were minted in. Reload one under
        a different account and the provider rejects the whole request with
        "Invalid `signature` in `thinking` block ... bound to a different
        conversation", and it repeats on every later turn because the cold start
        keeps resuming the same sid. Clearing the pointer is what makes the
        replacement child start a NEW native conversation.
        """

        smap = self._manager()
        kiro = _FakeProvider("")
        claude = _FakeProvider("claude")
        smap._sessions["kiro-key"] = self._session(kiro)
        smap._sessions["claude-key"] = self._session(claude)
        smap._session_map.set("kiro-key", "sid-old-account")
        smap._session_map.set("claude-key", "sid-untouched")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == ["kiro-key"]
        assert complete is True
        assert self._stored_sid(smap, "kiro-key") == ""
        # Only the pointer is dropped: the conversation stays recoverable.
        assert smap._session_map.get_discarded_sid("kiro-key") == "sid-old-account"
        # A provider that does not read the kiro identity store is untouched.
        assert self._stored_sid(smap, "claude-key") == "sid-untouched"

    @pytest.mark.asyncio
    async def test_a_busy_session_loses_its_pointer_during_the_sweep(self) -> None:
        """The running child keeps its in-process sid, but the map drops it now."""

        smap = self._manager()
        busy = _FakeProvider("")
        session = self._session(busy, busy=True)
        smap._sessions["busy"] = session
        smap._session_map.set("busy", "sid-old-account")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is False
        assert session.retire_on_identity_change is True
        assert self._stored_sid(smap, "busy") == ""
        assert smap._session_map.get_discarded_sid("busy") == "sid-old-account"
        assert "busy" in smap._sessions
        assert busy.shutdown_calls == 0

    @pytest.mark.asyncio
    async def test_retry_sweep_retires_the_successor_too(self) -> None:
        """A retry retires every Kiro session, successors included.

        Registration order cannot say which account a session authenticated
        under: a cold start that began before the switch registers after it, so
        sparing "whatever registered later" hands the old account a session the
        sweep was asked to retire. Retiring the successor costs it a fresh native
        conversation; its previous pointer stays recoverable.
        """

        smap = self._manager()
        old = _FakeProvider("")
        old_session = self._session(old, busy=True)
        smap._sessions["shared"] = old_session
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-old-account")

        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is False
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-old-account"

        old_session.semaphore.release()
        await smap._evict_stale_session("shared", old_session)
        successor = self._session(_FakeProvider("", sid="sid-new-account"))
        smap._sessions["shared"] = successor
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-new-account")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-b")

        assert retired == ["shared"]
        assert complete is True
        assert "shared" not in smap._sessions
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-new-account"

    @pytest.mark.asyncio
    async def test_retry_sweep_still_clears_a_marked_busy_holder(self) -> None:
        """A busy holder keeps its mark and loses its pointer on every retry."""

        smap = self._manager()
        old = _FakeProvider("")
        old_session = self._session(old, busy=True)
        smap._sessions["shared"] = old_session
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-old-account")

        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is False

        # Model the marked generation trying to republish before the retry.
        smap._session_map.set("shared", "sid-old-account")
        _, complete = await smap.retire_kiro_identity_sessions("fp-b")

        assert complete is False
        assert old_session.retire_on_identity_change is True
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-old-account"

    @pytest.mark.asyncio
    async def test_complete_sweep_releases_the_pending_marker(self) -> None:
        """A completed sweep clears the marker, so a later change is not a retry."""

        smap = self._manager()
        first = self._session(_FakeProvider(""))
        smap._sessions["shared"] = first
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-first-account")

        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is True

        second = self._session(_FakeProvider(""))
        smap._sessions["shared"] = second
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-second-account")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-c")

        assert retired == ["shared"]
        assert complete is True
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-second-account"

    @pytest.mark.asyncio
    async def test_a_second_switch_during_an_incomplete_sweep_retires_the_successor(
        self,
    ) -> None:
        """A -> B with a busy A-session, then B -> C before it goes idle.

        The consumer baseline is still A, so the sweep re-fires. The successor
        registered under B holds a B-account conversation that account C would
        reject exactly like the original bug, so it is retired rather than spared
        for having registered later.
        """

        smap = self._manager()
        busy_a = self._session(_FakeProvider(""), busy=True)
        smap._sessions["busy"] = busy_a
        smap._advance_session_generation("busy")
        smap._session_map.set("busy", "sid-account-a")
        smap._sessions["shared"] = self._session(_FakeProvider(""))
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-account-a")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert retired == ["shared"]
        assert complete is False

        successor_b = self._session(_FakeProvider("", sid="sid-account-b"))
        smap._sessions["shared"] = successor_b
        smap._advance_session_generation("shared")
        smap._session_map.set("shared", "sid-account-b")

        retired, complete = await smap.retire_kiro_identity_sessions("fp-c")

        assert retired == ["shared"]
        assert complete is False
        assert "shared" not in smap._sessions
        assert self._stored_sid(smap, "shared") == ""
        assert smap._session_map.get_discarded_sid("shared") == "sid-account-b"
        assert busy_a.retire_on_identity_change is True
        assert self._stored_sid(smap, "busy") == ""

    @pytest.mark.asyncio
    async def test_a_sweep_back_to_the_baseline_retires_the_interim_holders(self) -> None:
        """A -> B incomplete, back to A, then a genuine A -> B.

        The return to A is itself swept, because an outstanding change is its own
        trigger and the reconciled baseline says nothing about it. A session
        started under A in between therefore loses its A-account pointer at the
        later genuine A -> B, instead of surviving as something the sweep decided
        was already on the new account.
        """

        smap = self._manager()
        busy_a = self._session(_FakeProvider(""), busy=True)
        smap._sessions["busy"] = busy_a
        smap._advance_session_generation("busy")
        smap._session_map.set("busy", "sid-account-a")

        # A -> B, left incomplete by the busy A-session: fence armed for B.
        _, complete = await smap.retire_kiro_identity_sessions("fp-b")
        assert complete is False

        # Back to A. The consumer baseline never advanced, so this fires only
        # because an outstanding sweep is a trigger in its own right.
        await smap.retire_kiro_identity_sessions("fp-a")

        # A session started under A AFTER that return.
        later = self._session(_FakeProvider("", sid="sid-account-a"))
        smap._sessions["later"] = later
        smap._advance_session_generation("later")
        smap._session_map.set("later", "sid-account-a")

        # The genuine A -> B. `later` holds an A conversation and must lose it.
        retired, _ = await smap.retire_kiro_identity_sessions("fp-b")

        assert "later" in retired, "an A-account holder escaped the sweep"
        assert self._stored_sid(smap, "later") == ""
        assert smap._session_map.get_discarded_sid("later") == "sid-account-a"

    @pytest.mark.asyncio
    async def test_an_older_sweep_does_not_retire_a_newer_sweeps_fence(self) -> None:
        """A -> B whose shutdowns straddle a B -> C sweep.

        The shutdown loop runs outside ``identity_sweep_lock``, so a second
        account change can acquire that lock and recapture the fence while the
        first sweep is still awaiting a child's exit. The first sweep's own work
        succeeded while the pending change belongs to C: clearing the fence
        would drop the successor generations C recorded, and reporting complete
        would let the caller reconcile its baseline with C's busy holder still
        serving turns on B's credential -- and with no pending fingerprint left
        for the next turn to retry from.
        """

        released = asyncio.Event()
        entered = asyncio.Event()

        class _SlowProvider(_FakeProvider):
            async def shutdown(self) -> None:
                self.shutdown_calls += 1
                entered.set()
                await released.wait()

        smap = self._manager()
        smap._sessions["doomed"] = self._session(_SlowProvider(""))
        smap._advance_session_generation("doomed")
        smap._session_map.set("doomed", "sid-account-a")

        first = asyncio.create_task(smap.retire_kiro_identity_sessions("fp-b"))
        await asyncio.wait_for(entered.wait(), timeout=5.0)

        # A busy successor registers under B while that shutdown is pending, then
        # the account moves on to C.
        busy_b = self._session(_SlowProvider(""), busy=True)
        smap._sessions["busy"] = busy_b
        smap._advance_session_generation("busy")
        smap._session_map.set("busy", "sid-account-b")

        _, second_complete = await smap.retire_kiro_identity_sessions("fp-c")
        assert second_complete is False
        assert smap.pending_identity_sweep_fingerprint == "fp-c"

        released.set()
        retired, first_complete = await asyncio.wait_for(first, timeout=5.0)

        assert retired == ["doomed"]
        assert first_complete is False, "an older sweep claimed a newer change was handled"
        assert smap.pending_identity_sweep_fingerprint == "fp-c"
        assert busy_b.retire_on_identity_change is True

    @pytest.mark.asyncio
    async def test_an_ordinary_stale_eviction_keeps_the_conversation(self) -> None:
        """Only an identity change invalidates the sid.

        A dead or unresponsive provider is replaced under the SAME account, where
        resuming the native conversation is the behaviour that preserves it.
        """

        smap = self._manager()
        dead = _FakeProvider("")
        session = self._session(dead)
        smap._sessions["stale"] = session
        smap._session_map.set("stale", "sid-same-account")

        await smap._evict_stale_session("stale", session)

        assert self._stored_sid(smap, "stale") == "sid-same-account"
        assert smap._session_map.get_discarded_sid("stale") == ""

    @pytest.mark.asyncio
    async def test_a_marked_session_whose_process_died_first_still_loses_its_pointer(
        self,
    ) -> None:
        """A child that dies after the sweep cannot restore its old pointer."""

        smap = self._manager()
        dead = _DeadProvider("")
        session = self._session(dead, busy=True)
        smap._sessions["marked"] = session
        smap._session_map.set("marked", "sid-old-account")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is False
        assert session.retire_on_identity_change is True
        assert self._stored_sid(smap, "marked") == ""

        # No factory: the cold start after the removal raises, which keeps this
        # test on the removal itself rather than on provider construction.
        smap._provider_factory = None
        with pytest.raises(RuntimeError, match="No provider factory"):
            await smap.get_or_create("marked")

        assert "marked" not in smap._sessions
        assert self._stored_sid(smap, "marked") == ""
        assert smap._session_map.get_discarded_sid("marked") == "sid-old-account"

    @pytest.mark.asyncio
    async def test_an_unmarked_dead_session_keeps_its_conversation(self) -> None:
        """A dead provider outside the changed identity store keeps its pointer."""

        smap = self._manager()
        dead = _DeadProvider("claude")
        smap._sessions["stale"] = self._session(dead)
        smap._session_map.set("stale", "sid-same-account")

        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is True
        assert self._stored_sid(smap, "stale") == "sid-same-account"

        smap._provider_factory = None
        with pytest.raises(RuntimeError, match="No provider factory"):
            await smap.get_or_create("stale")

        assert self._stored_sid(smap, "stale") == "sid-same-account"
        assert smap._session_map.get_discarded_sid("stale") == ""

    @pytest.mark.asyncio
    async def test_shutdown_skips_marked_sid_but_persists_unmarked_sid(self, monkeypatch) -> None:
        """Shutdown must not undo the sweep's old-account pointer clear."""

        smap = self._manager()
        marked = _FakeProvider("", sid="sid-old-account")
        marked_session = self._session(marked, busy=True)
        smap._sessions["marked"] = marked_session
        smap._session_map.set("marked", "sid-old-account")

        await smap.retire_kiro_identity_sessions()
        assert marked_session.retire_on_identity_change is True
        assert self._stored_sid(smap, "marked") == ""

        unmarked = _FakeProvider("", sid="sid-current-account")
        smap._sessions["unmarked"] = self._session(unmarked)
        monkeypatch.setattr("kiro_crew.session._load_acp_provider_type", lambda: _FakeProvider)

        await smap.close_all()

        assert self._stored_sid(smap, "marked") == ""
        assert smap._session_map.get_discarded_sid("marked") == "sid-old-account"
        assert self._stored_sid(smap, "unmarked") == "sid-current-account"

    def test_marked_replay_settlement_consumes_lease_without_republishing_sid(self) -> None:
        """A landed replay cannot restore the identity sweep's discarded sid."""

        from kiro_crew.providers.acp import AcpProvider

        smap = self._manager()
        provider = object.__new__(AcpProvider)
        provider._client = SimpleNamespace(
            _session_id="sid-old-account-replayed",
            _work_dir="/old-workspace",
            backend="kiro",
        )
        session = self._session(provider)
        session.provider_switch_replay = True
        session.retire_on_identity_change = True
        smap._sessions["marked"] = session
        smap._session_map.set("marked", "sid-old-account")
        smap._session_map.clear_sid("marked")

        assert smap.commit_provider_switch_replay_sid("marked") is True
        assert session.provider_switch_replay is False
        assert self._stored_sid(smap, "marked") == ""
        assert smap._session_map.get_discarded_sid("marked") == "sid-old-account"

    def test_every_sid_writer_is_registration_or_identity_fenced(self) -> None:
        """A future non-registration writer must respect the identity marker."""

        import ast

        source_root = Path(__file__).parents[1] / "src" / "kiro_crew"
        registration_paths = {
            ("session_allocation.py", "seed_conversation"),
            ("session_allocation.py", "_get_or_create_impl"),
        }
        writers: list[str] = []
        offenders: list[str] = []

        def is_session_map_set(node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_session_map"
            )

        def has_prior_identity_guard(
            call: ast.Call,
            parents: dict[ast.AST, ast.AST],
        ) -> bool:
            child: ast.AST = call
            while child in parents:
                parent = parents[child]
                for _, value in ast.iter_fields(parent):
                    if not isinstance(value, list) or child not in value:
                        continue
                    for sibling in value[: value.index(child)]:
                        if not isinstance(sibling, ast.If):
                            continue
                        tests_identity_marker = (
                            isinstance(sibling.test, ast.Attribute)
                            and sibling.test.attr == "retire_on_identity_change"
                        )
                        exits_writer_path = bool(sibling.body) and isinstance(
                            sibling.body[-1], (ast.Continue, ast.Return)
                        )
                        if tests_identity_marker and exits_writer_path:
                            return True
                child = parent
            return False

        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            parents = {
                child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
            }
            for call in (node for node in ast.walk(tree) if is_session_map_set(node)):
                assert isinstance(call, ast.Call)
                function: ast.AST = call
                while function in parents and not isinstance(
                    function, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    function = parents[function]
                function_name = getattr(function, "name", "<module>")
                relative_path = path.relative_to(source_root).as_posix()
                location = f"{relative_path}:{call.lineno}:{function_name}"
                writers.append(location)
                if (relative_path, function_name) in registration_paths:
                    continue
                if not has_prior_identity_guard(call, parents):
                    offenders.append(location)

        assert len(writers) >= 7, f"session-map writer scan found only {writers}"
        assert (
            not offenders
        ), "session-map sid writer lacks a retire_on_identity_change fence: " + ", ".join(offenders)


class TestWarmPoolIdentityFence:
    """A pooled process that authenticated as the previous account is unusable.

    Pool claims take no cold-start permit, so the sweep's permit barrier cannot
    hold one back, and the pool teardown cannot run inside that barrier without
    deadlocking on the fill lock. The claim-time age check is the fence.
    """

    @staticmethod
    def _manager():
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        return SessionManager(KiroCrewConfig())

    @staticmethod
    def _record_discards(smap) -> list:
        discarded: list = []

        async def _discard(provider, context: str) -> None:
            discarded.append((provider, context))

        smap._discard_pool_provider = _discard  # type: ignore[method-assign]
        smap._schedule_replenish = lambda: None  # type: ignore[method-assign]
        return discarded

    @pytest.mark.asyncio
    async def test_a_provider_queued_before_the_switch_is_discarded_not_claimed(self) -> None:
        smap = self._manager()
        discarded = self._record_discards(smap)
        stale = _FakeProvider("")
        smap._warm_pool.put_nowait((stale, time.monotonic()))

        # The sweep stamps the pool before any key becomes claimable.
        smap._mark_identity_epoch()

        assert await smap._drain_and_claim(None) is None
        assert [p for p, _ in discarded] == [stale]

    @pytest.mark.asyncio
    async def test_a_pool_start_spanning_the_switch_is_discarded(self) -> None:
        """The enqueue timestamp records when startup began, not when it ended."""

        smap = self._manager()
        discarded = self._record_discards(smap)
        started = asyncio.Event()
        finish_start = asyncio.Event()

        class _StraddlingProvider(_FakeProvider):
            async def start(self) -> None:
                started.set()
                await finish_start.wait()

        provider = _StraddlingProvider("")
        smap._pool._pool_size = 1
        smap._provider_factory = lambda *args, **kwargs: provider

        fill = asyncio.create_task(smap._fill_warm_pool())
        await started.wait()
        smap._mark_identity_epoch()
        finish_start.set()
        await fill

        assert await smap._drain_and_claim(None) is None
        assert [item for item, _ in discarded] == [provider]

    @pytest.mark.asyncio
    async def test_a_provider_spawned_after_the_switch_is_still_claimable(self) -> None:
        """The fence is an age check, not a pool-wide off switch.

        The replenish that follows a sweep spawns under the NEW account, and
        refusing those would leave the pool permanently useless.
        """

        smap = self._manager()
        discarded = self._record_discards(smap)
        smap._mark_identity_epoch()
        fresh = _FakeProvider("")
        # Strictly after the epoch: the stamp is taken before this spawn.
        smap._warm_pool.put_nowait((fresh, smap._pool._pool_identity_epoch + 0.001))

        assert await smap._drain_and_claim(None) is fresh
        assert discarded == []

    @pytest.mark.asyncio
    async def test_a_provider_on_another_identity_store_is_not_fenced(self) -> None:
        """Only providers that read the Kiro identity store are affected."""

        smap = self._manager()
        discarded = self._record_discards(smap)
        other = _FakeProvider("claude")
        assert other.uses_kiro_identity_store is False
        smap._warm_pool.put_nowait((other, time.monotonic()))

        smap._mark_identity_epoch()

        assert await smap._drain_and_claim(None) is other
        assert discarded == []

    @pytest.mark.asyncio
    async def test_an_untouched_pool_claims_normally(self) -> None:
        """With no sweep having run there is no epoch, so nothing is fenced."""

        smap = self._manager()
        discarded = self._record_discards(smap)
        provider = _FakeProvider("")
        smap._warm_pool.put_nowait((provider, time.monotonic()))

        assert smap._pool._pool_identity_epoch == 0.0
        assert await smap._drain_and_claim(None) is provider
        assert discarded == []

    @pytest.mark.asyncio
    async def test_the_sweep_stamps_the_pool_even_when_it_retires_nothing(self) -> None:
        """The stamp is the sweep's first act, not a consequence of a retirement.

        A sweep that finds no session to pop still has a pool full of previous
        account processes, and the claim path is what must learn about it.
        """

        smap = self._manager()
        retired, complete = await smap.retire_kiro_identity_sessions()

        assert retired == []
        assert complete is True
        assert smap._pool._pool_identity_epoch > 0.0


class TestBootSeededSessionBaseline:
    """Seeding at boot replaces the once-per-lifetime unset-baseline sweep.

    The unset baseline deliberately reads as changed (an unknown baseline must
    not resolve to "the children match"), which was designed to cost one sweep
    per gateway lifetime. On a live gateway that sweep's completion
    precondition -- nothing busy, nothing mid-start, no runtime surviving -- is
    routinely unsatisfiable: the sweep retires idle sessions, dashboard slots
    eagerly respawn them, the in-flight starts keep the NEXT sweep incomplete,
    and the baseline never advances. Every turn then re-triggers retirement,
    recycling healthy just-spawned children forever.

    Seeding the baseline at startup, BEFORE anything can spawn a kiro-backed
    child, removes the boot sweep without weakening the guarantee: every child
    necessarily postdates the seed read, so the account it authenticated under
    is the seeded one or a later one -- and a later one compares unequal, which
    is exactly the change the sweep exists to catch.
    """

    @pytest.mark.asyncio
    async def test_a_seeded_baseline_reports_unchanged(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        assert await service.seed_sessions_baseline() is True

        changed, live = await service.identity_changed_since_sessions()
        assert changed is False
        assert live != ""

    @pytest.mark.asyncio
    async def test_an_unreadable_store_is_never_seeded(self, tmp_path: Path) -> None:
        """No store means no seed: the fail-safe unset-baseline sweep survives.

        Seeding "" would make "cannot tell" the accepted steady state -- every
        later account switch would compare equal to "" and go undetected. The
        unset baseline instead re-sweeps each turn, bounding how long a child
        can outlive the account it loaded.
        """

        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        assert await service.seed_sessions_baseline() is False

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live == ""

    @pytest.mark.asyncio
    async def test_a_hung_store_read_refuses_the_seed_within_the_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stalled store must not stall gateway boot.

        The seed's await sits on the startup path ahead of the listeners
        binding, so a hung store (locked SQLite, stalled network filesystem)
        must cost boot at most the deadline -- and the refusal must fall back
        to the existing fail-safe unset-baseline sweep, never to a guessed
        baseline.
        """

        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        async def _hang(*, allow_cached: bool = True) -> str:
            await asyncio.sleep(30)
            return "never-reached"

        monkeypatch.setattr(service, "current_identity_fingerprint", _hang)
        monkeypatch.setattr(kp, "_SEED_BASELINE_TIMEOUT_SECS", 0.05)

        assert await service.seed_sessions_baseline() is False

    @pytest.mark.asyncio
    async def test_a_switch_after_seeding_is_still_detected(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        await service.seed_sessions_baseline()

        _write_store(
            db,
            start_url="https://personal.awsapps.com/start",
            profile="arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
        )
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live != ""

    @pytest.mark.asyncio
    async def test_a_sign_out_after_seeding_is_still_detected(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        await service.seed_sessions_baseline()

        con = sqlite3.connect(str(db))
        with con:
            con.execute("DELETE FROM auth_kv")
            con.execute("DELETE FROM state")
        con.close()
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        assert live == ""

    @pytest.mark.asyncio
    async def test_seeding_never_overwrites_a_recorded_baseline(self, tmp_path: Path) -> None:
        """A pending change must not be masked by a late seed call."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        service.note_sessions_reconciled("fingerprint-of-a-previous-account")

        assert await service.seed_sessions_baseline() is False

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True

    @pytest.mark.asyncio
    async def test_a_second_seed_is_a_noop(self, tmp_path: Path) -> None:
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        assert await service.seed_sessions_baseline() is True
        assert await service.seed_sessions_baseline() is False

    @pytest.mark.asyncio
    async def test_assume_ready_seeding_is_a_noop(self, tmp_path: Path) -> None:
        service = kp.KiroPrerequisiteService(
            home=tmp_path, environ={}, platform_name="linux", assume_ready=True
        )

        assert await service.seed_sessions_baseline() is False

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

    @pytest.mark.asyncio
    async def test_a_seeded_first_turn_never_marks_a_busy_session(self, tmp_path: Path) -> None:
        """The end-to-end repro of the retire/respawn loop, fixed at its root.

        Pre-fix: turn 1 reads the unset baseline as changed, the sweep marks the
        busy session ``retire_on_identity_change`` (guaranteeing a recycle at its
        next turn) and reports incomplete, so the baseline never advances and
        every later turn repeats it. Post-fix: the seeded baseline compares
        equal, no sweep fires, the busy session is untouched.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        await service.seed_sessions_baseline()

        smap = SessionManager(KiroCrewConfig())
        busy = _Session(provider=_FakeProvider(""))  # type: ignore[arg-type]
        busy.semaphore._value = 0  # type: ignore[attr-defined]
        smap._sessions["busy"] = busy

        # The turn path's decision, exactly as _retire_sessions_on_identity_change
        # takes it: no change and no pending sweep means no retirement call.
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False
        assert getattr(smap, "pending_identity_sweep_fingerprint", "") == ""
        assert busy.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_an_unseeded_busy_gateway_re_sweeps_every_turn(self, tmp_path: Path) -> None:
        """Characterizes the defect the seed removes (and the fail-safe kept).

        With no seed and a perpetually-busy session, the sweep can never
        complete, the baseline never advances, and every turn re-triggers
        retirement. This remains the DESIRED behaviour for the one case the
        seed refuses: a store that cannot be fingerprinted.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")

        smap = SessionManager(KiroCrewConfig())
        busy = _Session(provider=_FakeProvider(""))  # type: ignore[arg-type]
        busy.semaphore._value = 0  # type: ignore[attr-defined]
        smap._sessions["busy"] = busy

        for _turn in range(3):
            changed, live = await service.identity_changed_since_sessions()
            assert changed is True
            retired, complete = await smap.retire_kiro_identity_sessions(fingerprint=live)
            assert retired == []
            assert complete is False
            # The incomplete sweep never advances the baseline (mirrors the
            # caller's `if complete and live` gate), so the loop repeats.

        assert busy.retire_on_identity_change is True

    def test_both_startup_sites_seed_before_serving(self) -> None:
        """Every place that constructs the service must seed it immediately.

        A construction site without a seed re-introduces the boot sweep for
        that entrypoint. Counted rather than AST-walked: the construction is
        spelled identically at both sites.
        """

        source = (
            Path(__file__).parents[1] / "src" / "kiro_crew" / "dashboard" / "server.py"
        ).read_text(encoding="utf-8")
        constructions = source.count("KiroPrerequisiteService,")
        seeds = source.count("seed_sessions_baseline()")
        assert constructions >= 2, "expected both startup sites to construct the service"
        assert seeds >= constructions, (
            f"{constructions} construction site(s) but only {seeds} seed call(s): "
            "a site that skips seed_sessions_baseline re-introduces the boot sweep loop"
        )


class TestInterimIdentityLatch:
    """An observed A->B->A round trip must still trigger the retirement sweep.

    The baseline comparison alone is blind to a round trip: seed A -> switch
    to B (a child spawns under B on a non-sweep path) -> switch back to A
    compares equal to the baseline, and the B-authenticated child serves the
    next turn. The latch records any fresh read that observed a DIFFERENT
    signed-in account and reports changed until a sweep completes, whatever
    the store says by then.

    The latch's own hazard is the mirror image: a sticky flag armed by a
    TRANSIENT read failure would force retire-until-complete sweeps on a
    healthy host -- re-creating the perpetual recycle loop the seeded
    baseline exists to remove. Half these tests therefore pin the refusals:
    component LOSS (an unreadable CLI store, a vanished vault component)
    never latches; only a component that appears or changes does, because
    reads lose components under failure, they do not gain them.
    """

    _PERSONAL = {
        "start_url": "https://personal.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
    }

    async def _seeded_service(self, tmp_path: Path) -> "kp.KiroPrerequisiteService":
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True
        return service

    @pytest.mark.asyncio
    async def test_a_round_trip_observed_by_a_poll_still_sweeps(self, tmp_path: Path) -> None:
        """Switch A->B, a poll observes B, switch back to A: changed reports True."""

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        # Any fresh read is an observer -- here, the status surface's poll.
        observed = await service.current_identity_fingerprint(allow_cached=False)
        assert observed != ""

        _write_store(db)  # back to the seeded account
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True, "the observed interim account must force a sweep"
        assert live != ""

    @pytest.mark.asyncio
    async def test_a_complete_sweep_clears_the_latch_and_turns_go_quiet(
        self, tmp_path: Path
    ) -> None:
        """Reconciliation resolves the observation; later turns must NOT re-sweep.

        The loop guard: if the latch survived reconciliation, every turn on a
        healthy host would retire healthy children forever -- the exact
        pathology the seeded baseline removes.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        _write_store(db)
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        service.note_sessions_reconciled(live)

        for _ in range(3):
            _expire_identity_cache(service)
            changed, _ = await service.identity_changed_since_sessions()
            assert changed is False, "a reconciled latch must not keep forcing sweeps"

    @pytest.mark.asyncio
    async def test_an_unreadable_cli_store_blip_never_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A transient read failure is not an interim account.

        An unreadable store reads as absent; latching on it would make one
        blip sticky until a fully-quiescent sweep, which a live gateway may
        never produce. The blip itself still reports changed (non-sticky,
        existing behaviour) for as long as it persists -- but once the store
        reads fine again, turns must be quiet.
        """

        service = await self._seeded_service(tmp_path)

        real_fingerprint = kp.identity_fingerprint

        def _blip(path: object, **_kwargs: object) -> str:
            raise OSError("database is locked")

        monkeypatch.setattr(kp, "identity_fingerprint", _blip)
        _expire_identity_cache(service)
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True  # non-sticky: absent differs from the baseline
        assert live == ""

        monkeypatch.setattr(kp, "identity_fingerprint", real_fingerprint)
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False, "a recovered blip must not leave a sticky latch"

    @pytest.mark.asyncio
    async def test_a_vanished_vault_component_never_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vault component lost while the CLI account matches: refused.

        An unreadable vault reads as empty, indistinguishable from a vault
        sign-out, so a sticky latch on component loss would arm on a blip.
        """

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        service = await self._seeded_service(tmp_path)

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True  # non-sticky: the combined digest differs

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False, "a recovered vault must not leave a sticky latch"

    @pytest.mark.asyncio
    async def test_a_vault_identity_round_trip_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vault component that APPEARS is a real interim identity, not a blip.

        Reads lose components under failure; they do not gain them. A Crew
        sign-in that lands and is reverted between two turns leaves children
        holding its credential, exactly like a CLI round trip.
        """

        service = await self._seeded_service(tmp_path)

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-INTERIM")
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, "an observed interim vault identity must force a sweep"

    @pytest.mark.asyncio
    async def test_a_vault_only_round_trip_latches_with_no_cli_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A vault-only gateway must still catch a vault A->B->A round trip.

        With no CLI store at all, the CLI component is permanently absent --
        but a vault component that CHANGES to a different nonempty value
        cannot be a read blip (reads lose components under failure; they do
        not gain or alter them). Refusing to latch whenever the CLI is absent
        would make the entire latch inert on vault-only hosts, leaving
        B-authenticated children alive after the round trip.
        """

        # No _write_store: the CLI store never exists on this host.
        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-B")
        _expire_identity_cache(service)
        # A status poll observes the interim vault account.
        observed = await service.current_identity_fingerprint(allow_cached=False)
        assert observed != ""

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, "a vault-only interim account must force a sweep"

    @pytest.mark.asyncio
    async def test_a_vault_only_blip_never_latches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vault-only host, vault read blips to empty: refused, no sticky latch.

        The loop guard for the vault-only arm: an empty vault under an absent
        CLI is indistinguishable from a transient vault read failure, so it
        must not arm the sticky flag -- only a nonempty, DIFFERENT vault does.
        Once the vault reads fine again, turns must be quiet.
        """

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True  # non-sticky: the combined digest differs

        monkeypatch.setattr(kp, "_crew_vault_fingerprint", lambda: "vault-A")
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False, "a recovered vault-only blip must not leave a sticky latch"

    @pytest.mark.asyncio
    async def test_an_unobserved_round_trip_remains_undetected(self, tmp_path: Path) -> None:
        """Pins the SERVICE-level boundary: the baseline stays blind by design.

        A round trip with NO fresh read during the interim window is invisible
        to a gateway-wide observer by construction, and this test pins that
        the baseline comparison honestly reports unchanged. The CHILD is no
        longer exposed by it: its spawn recorded the interim account
        (``read_spawn_identity`` -> ``stamp_spawn_identity``), and the turn
        gate's ``flag_identity_stamp_mismatches`` retires it from that record
        -- see ``TestSpawnIdentityStamp``. What remains open is a full round
        trip completing strictly between the two bracket reads of a single
        spawn, documented on ``stamp_spawn_identity``.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _write_store(db)  # round trip completes with no read in between
        _expire_identity_cache(service)

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is False

    @pytest.mark.asyncio
    async def test_a_stable_identity_never_latches_across_repeated_reads(
        self, tmp_path: Path
    ) -> None:
        """The healthy-host invariant: no observation, no latch, no sweeps."""

        service = await self._seeded_service(tmp_path)

        for _ in range(5):
            _expire_identity_cache(service)
            await service.current_identity_fingerprint(allow_cached=False)
            changed, _ = await service.identity_changed_since_sessions()
            assert changed is False


class TestSpawnIdentityStamp:
    """Each kiro-backed child records the account it spawned under.

    The gateway-wide baseline and the interim latch share one blind spot: an
    A->B->A round trip that completes with NO fresh read in between leaves
    both comparing A to A while a child that spawned during the interim still
    holds B's credential. The stamp closes it per child: the spawn records the
    account the store held at that moment, and the turn gate's
    ``flag_identity_stamp_mismatches`` retires any session whose record
    PROVABLY differs from the live read.

    "Provably" is the loop guard, mirrored from the latch: a component
    participates only when nonempty on BOTH sides, because reads lose
    components under failure and never gain or alter them -- so a transient
    read failure (at spawn or at the gate) can never flag a healthy child,
    and an unstamped child keeps exactly the pre-stamping protections.
    """

    _PERSONAL = {
        "start_url": "https://personal.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
    }

    def test_mismatch_component_rules(self) -> None:
        """The pure comparator: only a both-sides-nonempty difference flags."""

        mismatch = kp.identity_stamp_mismatch
        # Unknown on either side is never a mismatch.
        assert mismatch("", "") is False
        assert mismatch("", "cli-a") is False
        assert mismatch("cli-b", "") is False
        # Equality is never a mismatch.
        assert mismatch("cli-a", "cli-a") is False
        assert mismatch("cli-a+crew:v1", "cli-a+crew:v1") is False
        # A CLI component that differs on both sides flags.
        assert mismatch("cli-b", "cli-a") is True
        assert mismatch("cli-b+crew:v1", "cli-a+crew:v1") is True
        # A vault component that differs on both sides flags.
        assert mismatch("cli-a+crew:v2", "cli-a+crew:v1") is True
        assert mismatch("+crew:v2", "+crew:v1") is True
        # Component LOSS is indistinguishable from a read blip: never flags.
        assert mismatch("cli-a+crew:v1", "cli-a") is False
        assert mismatch("cli-a", "cli-a+crew:v1") is False
        assert mismatch("+crew:v1", "cli-a") is False

    @pytest.mark.asyncio
    async def test_a_child_from_an_unobserved_round_trip_is_flagged(self, tmp_path: Path) -> None:
        """The end-to-end scenario the stamp exists for.

        Seed A -> a child spawns while the store holds B (its spawn records
        B) -> the store returns to A before ANY other read. The baseline
        comparison reports unchanged -- and the child is still flagged for
        recycle, from its own record.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        # The interim window: the store switches to B and a child spawns.
        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        provider = _FakeProvider("")
        pre_spawn = await kp.pre_spawn_identity(service.read_spawn_identity)
        await kp.stamp_spawn_identity(service.read_spawn_identity, provider, pre_spawn=pre_spawn)
        interim_stamp = getattr(provider, "spawn_identity", "")
        assert interim_stamp != "", "the spawn must have recorded the interim account"

        smap = SessionManager(KiroCrewConfig())
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["victim"] = sess

        # The round trip completes; the live read now matches the baseline.
        _write_store(db)
        _expire_identity_cache(service)
        live = await service.current_identity_fingerprint(allow_cached=False)
        assert live != interim_stamp

        flagged = await smap.flag_identity_stamp_mismatches(live)
        assert flagged == ["victim"]
        assert sess.retire_on_identity_change is True

    @pytest.mark.asyncio
    async def test_an_unstamped_child_is_never_flagged(self, tmp_path: Path) -> None:
        """No stamp means no verdict: the pre-stamping protections apply."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        sess = _Session(provider=_FakeProvider(""))  # type: ignore[arg-type]
        smap._sessions["plain"] = sess

        assert await smap.flag_identity_stamp_mismatches("live-fp") == []
        assert sess.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_a_matching_stamp_never_flags_and_an_empty_live_never_flags(
        self, tmp_path: Path
    ) -> None:
        """The healthy-host invariant and the gate-side blip guard.

        A stamped child whose account still matches must never be recycled --
        flagging here every turn would be the respawn churn the seeded
        baseline removed -- and an unreadable live read proves nothing.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        provider = _FakeProvider("")
        provider.spawn_identity = "same-fp"  # type: ignore[attr-defined]
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["healthy"] = sess

        for _ in range(3):
            assert await smap.flag_identity_stamp_mismatches("same-fp") == []
        assert await smap.flag_identity_stamp_mismatches("") == []
        assert sess.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_a_non_kiro_child_is_never_flagged(self, tmp_path: Path) -> None:
        """The store predicate still gates: a claude child never compares."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        provider = _FakeProvider("claude")
        provider.spawn_identity = "cli-b"  # type: ignore[attr-defined]
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["claude"] = sess

        assert await smap.flag_identity_stamp_mismatches("cli-a") == []
        assert sess.retire_on_identity_change is False

    @pytest.mark.asyncio
    async def test_a_shared_runtime_stamp_is_read_through_the_fallback(
        self, tmp_path: Path
    ) -> None:
        """A demuxed session's provider inherits its runtime's spawn record."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        smap = SessionManager(KiroCrewConfig())
        provider = _FakeProvider("")
        provider._runtime = SimpleNamespace(spawn_identity="cli-b")  # type: ignore[attr-defined]
        sess = _Session(provider=provider)  # type: ignore[arg-type]
        smap._sessions["demuxed"] = sess

        assert await smap.flag_identity_stamp_mismatches("cli-a") == ["demuxed"]
        assert sess.retire_on_identity_change is True

    @pytest.mark.asyncio
    async def test_the_first_stamp_wins(self) -> None:
        """A warm-pool provider keeps its fill-time record across a claim.

        Re-stamping at claim time would relabel the child with whatever the
        store holds THEN -- exactly the wrong account for a provider that
        spawned during an interim window.
        """

        provider = _FakeProvider("")

        async def _fill_read() -> str:
            return "fill-time-fp"

        async def _claim_read() -> str:
            return "claim-time-fp"

        await kp.stamp_spawn_identity(_fill_read, provider, pre_spawn="fill-time-fp")
        await kp.stamp_spawn_identity(_claim_read, provider, pre_spawn="claim-time-fp")
        assert provider.spawn_identity == "fill-time-fp"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_stamping_refuses_failures_and_empty_reads(self) -> None:
        """A failed or empty spawn read leaves the child unstamped, never raises.

        Absent and unknown must stay the same observable: recording "" would
        be a claim, and the flag machinery skips unstamped children entirely.
        """

        provider = _FakeProvider("")

        await kp.stamp_spawn_identity(None, provider, pre_spawn="cli-a")
        assert getattr(provider, "spawn_identity", "") == ""

        async def _empty() -> str:
            return ""

        await kp.stamp_spawn_identity(_empty, provider, pre_spawn="cli-a")
        assert getattr(provider, "spawn_identity", "") == ""

        async def _boom() -> str:
            raise RuntimeError("store exploded")

        await kp.stamp_spawn_identity(_boom, provider, pre_spawn="cli-a")
        assert getattr(provider, "spawn_identity", "") == ""

    @pytest.mark.asyncio
    async def test_a_switch_across_the_spawn_window_refuses_the_stamp(self) -> None:
        """Disagreeing bracket reads mean the child's account is unprovable.

        The child reads its credential between the pre-spawn and post-spawn
        reads. When they disagree, the store switched inside the window --
        stamping the post value would label a possibly-B child as A, which is
        exactly the mislabelling the reviewer's F2 names. Refusal keeps the
        child on the pre-stamping protections, and the differing read has
        already fed the interim latch through the ordinary read path.
        """

        provider = _FakeProvider("")

        async def _post_read() -> str:
            return "cli-a"

        await kp.stamp_spawn_identity(_post_read, provider, pre_spawn="cli-b")
        assert getattr(provider, "spawn_identity", "") == ""

    @pytest.mark.asyncio
    async def test_a_missing_pre_read_refuses_the_stamp(self) -> None:
        """No pre-spawn read, no agreement, no stamp.

        A post-only read is the single-sample race F2 names; a site that
        skips the pre-read must degrade to the fail-safe unstamped state,
        never to a guess.
        """

        provider = _FakeProvider("")

        async def _post_read() -> str:
            return "cli-a"

        await kp.stamp_spawn_identity(_post_read, provider, pre_spawn="")
        assert getattr(provider, "spawn_identity", "") == ""

    @pytest.mark.asyncio
    async def test_agreeing_bracket_reads_stamp_the_child(self) -> None:
        """The healthy path: both reads name the same account and it sticks."""

        provider = _FakeProvider("")

        async def _read() -> str:
            return "cli-a"

        pre = await kp.pre_spawn_identity(_read)
        await kp.stamp_spawn_identity(_read, provider, pre_spawn=pre)
        assert provider.spawn_identity == "cli-a"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_a_failed_pre_read_returns_empty_and_never_raises(self) -> None:
        """``pre_spawn_identity`` is best-effort: failures become the absent value."""

        async def _boom() -> str:
            raise RuntimeError("store exploded")

        assert await kp.pre_spawn_identity(None) == ""
        assert await kp.pre_spawn_identity(_boom) == ""

    @pytest.mark.asyncio
    async def test_a_spawn_read_also_feeds_the_interim_latch(self, tmp_path: Path) -> None:
        """Defense in depth: the spawn's own read is an interim observer.

        Stamping flows through the ordinary read path, so a spawn that lands
        during the interim window arms the latch exactly as a status poll
        would -- the round trip is then caught globally as well as per child.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await kp.stamp_spawn_identity(service.read_spawn_identity, _FakeProvider(""))

        _write_store(db)  # the round trip completes
        _expire_identity_cache(service)
        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, "the spawn-time observation must force a sweep"

    def test_both_startup_sites_wire_the_spawn_reader(self) -> None:
        """Every seeded startup site must also wire the stamp reader.

        A site that seeds but does not wire silently reverts that entrypoint
        to baseline-only protection. Counted like the seed-call scan: the
        wiring is spelled identically at both sites.
        """

        source = (
            Path(__file__).parents[1] / "src" / "kiro_crew" / "dashboard" / "server.py"
        ).read_text(encoding="utf-8")
        seeds = source.count("seed_sessions_baseline()")
        wirings = source.count("spawn_identity_reader = ")
        assert seeds >= 2, "expected both startup sites to seed"
        assert wirings >= seeds, (
            f"{seeds} seed site(s) but only {wirings} spawn_identity_reader "
            "wiring(s): an unwired site leaves every child unstamped there"
        )

    def test_every_spawn_site_stamps(self) -> None:
        """Each provider start path must record the spawn account.

        Counted per file: a start site that skips stamping reverts its spawns
        to baseline-only protection without any test failing at runtime,
        because unstamped children are (by design) silently skipped.
        """

        src = Path(__file__).parents[1] / "src" / "kiro_crew"
        expected = {
            # shared run runtime + cold start + direct companion runtime
            "session_allocation.py": 3,
            # bg create + recycle replacement + bg replacement runtime
            "session_background.py": 3,
            "session_pool.py": 1,  # warm pool fill
        }
        pre_reads = {
            "session_allocation.py": 3,
            # the bg replacement runtime re-brackets its fallback spawn, so it
            # carries one more pre-read than stamp calls
            "session_background.py": 4,
            "session_pool.py": 1,
        }
        for name, count in expected.items():
            source = (src / name).read_text(encoding="utf-8")
            found = source.count("await stamp_spawn_identity(")
            assert found >= count, (
                f"{name}: expected at least {count} stamp_spawn_identity "
                f"call(s) after provider starts, found {found}"
            )
            brackets = source.count("await pre_spawn_identity(")
            assert brackets >= pre_reads[name], (
                f"{name}: expected at least {pre_reads[name]} pre_spawn_identity bracket "
                f"read(s) before provider starts, found {brackets} -- a site "
                "without one leaves its children unstamped (fail-safe, but "
                "silently reduced coverage)"
            )

    @staticmethod
    def _fake_runtime(stamp: str, *, busy: bool = False, kiro: bool = True) -> "SimpleNamespace":
        """A registry double declaring the store capability like AcpRuntime."""

        killed: list[str] = []

        async def _kill(*, expected: bool = False, reason: str = "") -> None:
            killed.append(reason)

        ns = SimpleNamespace(
            uses_kiro_identity_store=kiro,
            spawn_identity=stamp,
            has_active_or_initializing_sessions=lambda: busy,
            kill=_kill,
            killed=killed,
            pid=_UNALLOCATABLE_PID,
        )
        ns.is_alive = lambda: not killed
        return ns

    @pytest.mark.asyncio
    async def test_a_mismatched_bg_runtime_is_parked_at_the_gate(self) -> None:
        """F1/F3: the background runtime's stamp is compared, and the slot is
        freed WITHOUT a same-pass kill.

        It spawned while the store held B; the store now names A. It never
        appears in ``_sessions``, so only the registry pass can reach it --
        and even when its busy probe answers idle, the kill is deferred to
        the drain reap: the probe races a ``get_bg_session`` claim that was
        handed the runtime but has not yet opened its init scope, so killing
        on the pass that proved the mismatch would abort that claim.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        runtime = self._fake_runtime("cli-b")
        smap._bg_runtime = runtime  # type: ignore[assignment]

        flagged = await smap.flag_identity_stamp_mismatches("cli-a")
        assert flagged == ["background-runtime"]
        assert runtime.killed == [], "the mismatch pass must never kill same-pass"
        assert smap._bg_runtime is None
        assert runtime in smap._draining_bg_runtimes

    @pytest.mark.asyncio
    async def test_a_mismatched_subagent_runtime_is_parked_at_the_gate(self) -> None:
        """F1/F3: a wrong-account companion runtime is displaced, never
        killed on the pass that proved the mismatch.

        Every session demuxed onto it would inherit the stale credential, so
        the registry itself must be swept -- but an idle-looking runtime may
        be one a concurrent claim was just handed (``create_session`` opens
        its init scope only at entry), so the kill belongs to a later drain
        reap, after the park grace.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        runtime = self._fake_runtime("cli-b")
        smap._subagent_runtimes["parent"] = runtime  # type: ignore[index]

        flagged = await smap.flag_identity_stamp_mismatches("cli-a")
        assert flagged == ["subagent-runtime:parent"]
        assert runtime.killed == [], "the mismatch pass must never kill same-pass"
        assert "parent" not in smap._subagent_runtimes
        assert runtime in smap._draining_subagent_runtimes

    @pytest.mark.asyncio
    async def test_a_busy_mismatched_runtime_is_displaced_never_killed(self) -> None:
        """A busy wrong-account runtime leaves the claimable slots but keeps running.

        Killing live work is the defect this whole change removes -- but
        leaving the runtime registered kept it CLAIMABLE: the registry and the
        ``_bg`` slot are exactly what new acquisitions are handed, so a busy
        B-stamped runtime would serve fresh sessions B's credentials for its
        whole drain. Displacement takes both properties at once: the runtime
        is popped from the claimable slot (a replacement spawns under the
        live account) and parked, unkilled, until its work drains.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        bg = self._fake_runtime("cli-b", busy=True)
        sub = self._fake_runtime("cli-b", busy=True)
        healthy = self._fake_runtime("cli-a", busy=True)
        smap._bg_runtime = bg  # type: ignore[assignment]
        smap._subagent_runtimes["parent"] = sub  # type: ignore[index]
        smap._subagent_runtimes["healthy"] = healthy  # type: ignore[index]

        flagged = await smap.flag_identity_stamp_mismatches("cli-a")
        assert sorted(flagged) == ["background-runtime", "subagent-runtime:parent"]
        # Never killed mid-turn: the work is preserved.
        assert bg.killed == [] and sub.killed == []
        # Unclaimable: both slots are free for a live-account respawn.
        assert smap._bg_runtime is None
        assert "parent" not in smap._subagent_runtimes
        # Parked to drain instead.
        assert sub in smap._draining_subagent_runtimes
        assert bg in smap._draining_bg_runtimes
        # A busy runtime with a MATCHING stamp is untouched.
        assert smap._subagent_runtimes["healthy"] is healthy
        assert healthy.killed == []

    @pytest.mark.asyncio
    async def test_a_parked_displaced_runtime_is_reaped_once_it_drains(self) -> None:
        """The park is a drain, not a leak: idle parked runtimes are killed.

        While busy the parked runtime survives every pass (probes fail toward
        busy). Once idle it must ALSO outlive the park grace before the reap
        may end it -- the busy probe can read a just-claimed runtime as idle
        for the sub-second stretch before the claim opens its init scope --
        and only a pass that finds it idle past the grace kills it and drops
        it from the drain list.
        """

        import time as _time

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.kiro_prerequisite import IDENTITY_PARK_GRACE_SECS
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        bg = self._fake_runtime("cli-b", busy=True)
        sub = self._fake_runtime("cli-b", busy=True)
        smap._bg_runtime = bg  # type: ignore[assignment]
        smap._subagent_runtimes["parent"] = sub  # type: ignore[index]

        await smap.flag_identity_stamp_mismatches("cli-a")
        # Still busy: parked runtimes survive the next pass unkilled.
        await smap.flag_identity_stamp_mismatches("cli-a")
        assert bg.killed == [] and sub.killed == []

        sub.has_active_or_initializing_sessions = lambda: False
        bg.has_active_or_initializing_sessions = lambda: False
        # Idle but freshly parked: the grace refuses the kill.
        await smap.flag_identity_stamp_mismatches("cli-a")
        assert bg.killed == [] and sub.killed == []
        assert sub in smap._draining_subagent_runtimes
        assert bg in smap._draining_bg_runtimes

        # Age the park marks past the grace: the next pass reaps both.
        aged = _time.monotonic() - IDENTITY_PARK_GRACE_SECS - 1.0
        sub._identity_parked_at = aged
        bg._identity_parked_at = aged
        await smap.flag_identity_stamp_mismatches("cli-a")
        assert sub.killed == ["drained identity displacement teardown"]
        assert bg.killed == ["drained displacement teardown"]
        assert smap._draining_subagent_runtimes == []
        assert smap._draining_bg_runtimes == []

    @pytest.mark.asyncio
    async def test_matching_unstamped_or_foreign_runtimes_are_never_retired(self) -> None:
        """The healthy-host invariant extends to the registries.

        A matching stamp, no stamp at all (fail-safe skip), and a non-kiro
        runtime must all survive every pass -- retiring any of them per turn
        would be the respawn churn this PR exists to remove.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        healthy = self._fake_runtime("cli-a")
        unstamped = self._fake_runtime("")
        foreign = self._fake_runtime("cli-b", kiro=False)
        smap._bg_runtime = healthy  # type: ignore[assignment]
        smap._subagent_runtimes["plain"] = unstamped  # type: ignore[index]
        smap._subagent_runtimes["claude"] = foreign  # type: ignore[index]

        for _ in range(3):
            assert await smap.flag_identity_stamp_mismatches("cli-a") == []
        assert healthy.killed == [] and unstamped.killed == [] and foreign.killed == []
        assert smap._bg_runtime is healthy

    @pytest.mark.asyncio
    async def test_a_direct_companion_runtime_is_stamped_at_spawn(self) -> None:
        """F1: ``get_subagent_runtime``'s raw runtime spawn records the account.

        Without the stamp, the registry pass compares an empty string and
        silently skips this runtime for its whole lifetime -- every subagent
        session demuxed onto it would inherit a credential nothing can audit.
        """

        from unittest import mock

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())

        async def reader() -> str:
            return "cli-a"

        smap.spawn_identity_reader = reader  # type: ignore[attr-defined]

        class _FakeRuntime:
            def __init__(self, agent: str | None = None, **kwargs: object) -> None:
                self.agent = agent

            async def spawn(self, start_priority=None) -> None:
                return None

            def is_alive(self) -> bool:
                return True

        class _FakeDead(Exception):
            pass

        with mock.patch(
            "kiro_crew.session._load_bg_runtime_types",
            return_value=(_FakeRuntime, _FakeDead),
        ):
            runtime = await smap.get_subagent_runtime("parent")

        assert getattr(runtime, "spawn_identity", "") == "cli-a"
        assert smap._subagent_runtimes["parent"] is runtime

    @pytest.mark.asyncio
    async def test_ensure_background_holds_the_permit_through_registration(self) -> None:
        """F2: the sweep's permit barrier covers registration, not just start.

        The identity sweep drains every cold-start permit as its quiescence
        barrier. If ``_ensure_background`` released its permit before the
        provider was registered, a concurrent sweep could reconcile a store
        change while the (possibly wrong-account) provider was invisible to
        it, and the provider would register afterward -- past the barrier.
        Registration must therefore happen while the permit is still held.
        """

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import BACKGROUND_KEY, SessionManager

        class _Prov:
            cwd = "."

            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

        smap = SessionManager(
            KiroCrewConfig(), provider_factory=lambda key, agent=None, cwd=None: _Prov()
        )
        # One permit total, so ``locked()`` is True exactly while it is held.
        from kiro_crew.start_priority import PrioritySemaphore

        smap._start_sem = PrioritySemaphore(1)

        held_at_registration: list[bool] = []
        original = smap._advance_session_generation

        def probe(key: str) -> None:
            held_at_registration.append(smap._start_sem.locked())
            original(key)

        smap._advance_session_generation = probe  # type: ignore[method-assign]

        await smap._ensure_background()

        assert BACKGROUND_KEY in smap._sessions
        assert held_at_registration, "registration never happened"
        assert held_at_registration[0] is True, (
            "the cold-start permit was released before the background provider "
            "was registered -- the identity sweep's barrier cannot cover the "
            "started-but-unregistered window"
        )
        await smap.close_all()

    @pytest.mark.asyncio
    async def test_a_cancelled_stamp_read_tears_down_the_companion_runtime(self) -> None:
        """A cancellation during the stamp read must not leak the spawned runtime.

        The stamp read is a real suspension point between a successful spawn
        and registration. Pre-guard, a cancellation landing there escaped with
        the runtime alive but absent from ``_subagent_runtimes`` -- unmanaged
        until the orphan sweep's grace window expired. The guard kills it on
        the way out and re-raises.
        """

        from unittest import mock

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())

        calls: list[int] = []

        async def cancelling_reader() -> str:
            # The first read is the pre-spawn bracket (before spawn -- a
            # cancellation there never leaks anything); the second is the
            # post-start stamp read, the window under test.
            calls.append(1)
            if len(calls) >= 2:
                raise asyncio.CancelledError()
            return "cli-a"

        smap.spawn_identity_reader = cancelling_reader  # type: ignore[attr-defined]

        killed: list[str] = []

        class _FakeRuntime:
            def __init__(self, agent: str | None = None, **kwargs: object) -> None:
                self.agent = agent

            async def spawn(self, start_priority=None) -> None:
                return None

            def is_alive(self) -> bool:
                return True

            async def kill(self, expected: bool = False, reason: str = "") -> None:
                killed.append(reason)

        class _FakeDead(Exception):
            pass

        with mock.patch(
            "kiro_crew.session._load_bg_runtime_types",
            return_value=(_FakeRuntime, _FakeDead),
        ):
            with pytest.raises(asyncio.CancelledError):
                await smap.get_subagent_runtime("parent")

        assert killed, (
            "the spawned runtime was not torn down when the stamp read was "
            "cancelled -- it leaks unmanaged until the orphan sweep's grace "
            "window expires"
        )
        assert "parent" not in smap._subagent_runtimes

    @pytest.mark.asyncio
    async def test_a_cancelled_stamp_read_kills_the_background_provider(self) -> None:
        """The background spawn's stamp window is covered by a kill guard too."""

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import BACKGROUND_KEY, SessionManager

        class _Prov:
            cwd = "."

            async def start(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

        smap = SessionManager(
            KiroCrewConfig(), provider_factory=lambda key, agent=None, cwd=None: _Prov()
        )

        calls: list[int] = []

        async def cancelling_reader() -> str:
            # First read = pre-spawn bracket (a cancel there escapes before
            # start and leaks nothing); second = the post-start stamp read.
            calls.append(1)
            if len(calls) >= 2:
                raise asyncio.CancelledError()
            return "cli-a"

        smap.spawn_identity_reader = cancelling_reader  # type: ignore[attr-defined]

        hard_killed: list[object] = []
        smap._dispatch_hard_kill = hard_killed.append  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await smap._ensure_background()

        assert hard_killed, (
            "the started background provider was not killed when the stamp "
            "read was cancelled before registration"
        )
        assert BACKGROUND_KEY not in smap._sessions
        await smap.close_all()

    def test_every_stamp_await_is_guarded_against_cancellation(self) -> None:
        """Source scan: no stamp call may sit unguarded between start and registry.

        ``stamp_spawn_identity`` awaits an uncached store read (a multi-second
        suspension point) after the child's process already started but before
        it is registered anywhere a sweep can see. Every call site must
        therefore either sit inside a ``try`` whose handler tears the child
        down (kill/discard + raise) or be covered by an enclosing ``finally``
        that discards the child (the warm-pool fill pattern). This scan pins
        the guard textually: each ``await stamp_spawn_identity(`` must be
        preceded within a few lines by a ``try:`` opener that pairs with a
        teardown handler, so a refactor that hoists a stamp back out of its
        guard fails here loudly.
        """

        import re
        from pathlib import Path

        import kiro_crew

        src_root = Path(kiro_crew.__file__).parent
        guarded = 0
        for rel in ("session_allocation.py", "session_background.py", "session_pool.py"):
            text = (src_root / rel).read_text(encoding="utf-8")
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if "await stamp_spawn_identity(" not in line:
                    continue
                guarded += 1
                window_before = "\n".join(lines[max(0, i - 6) : i + 1])
                window_after = "\n".join(lines[i : i + 40])
                has_try_guard = re.search(r"^\s*try:\s*$", window_before, re.M) and re.search(
                    r"except BaseException:", window_after
                )
                has_finally_discard = re.search(
                    r"finally:\s*\n\s*if provider is not None:", window_after
                )
                assert has_try_guard or has_finally_discard, (
                    f"{rel}:{i + 1}: `await stamp_spawn_identity(` is not covered by a "
                    "cancellation teardown guard (try/except BaseException that kills the "
                    "child, or an enclosing finally that discards it)"
                )
        assert guarded >= 6, f"expected at least 6 stamp sites, scanned {guarded}"

    @pytest.mark.asyncio
    async def test_the_stamp_read_runs_under_the_pid_shield(self) -> None:
        """F2: the child's PID is shielded from the orphan sweep during the stamp.

        The stamp read is a bounded suspension point between the child's
        process start and its registration anywhere the sweep's active-PID
        union can see (``_starting_pids``, the session map, a runtime
        registry, the warm pool). On hosts whose PID probe has no age grace a
        sweep tick landing in that await would kill the healthy child -- so
        the shield must be up BEFORE the read suspends, and released once
        registration is visible.
        """

        from unittest import mock

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        observed: list[set[int]] = []

        async def reader() -> str:
            observed.append(set(smap._starting_pids))
            return "cli-a"

        smap.spawn_identity_reader = reader  # type: ignore[attr-defined]

        class _FakeRuntime:
            pid = _UNALLOCATABLE_PID

            def __init__(self, agent: str | None = None, **kwargs: object) -> None:
                self.agent = agent

            async def spawn(self, start_priority=None) -> None:
                return None

            def is_alive(self) -> bool:
                return True

        class _FakeDead(Exception):
            pass

        with mock.patch(
            "kiro_crew.session._load_bg_runtime_types",
            return_value=(_FakeRuntime, _FakeDead),
        ):
            runtime = await smap.get_subagent_runtime("parent")

        assert len(observed) == 2, "expected a pre-spawn and a stamp read"
        assert _UNALLOCATABLE_PID not in observed[0], "no shield before the process exists"
        assert _UNALLOCATABLE_PID in observed[1], (
            "the stamp read must run under the start-to-registration PID "
            "shield, or an orphan-sweep tick landing in the await kills the "
            "healthy child"
        )
        assert (
            _UNALLOCATABLE_PID not in smap._starting_pids
        ), "the shield must be released once the registry owns the runtime"
        assert smap._subagent_runtimes["parent"] is runtime

    def test_every_stamp_await_is_shielded_from_the_orphan_sweep(self) -> None:
        """Source scan: each stamp await sits under the PID shield.

        Every ``await stamp_spawn_identity(`` must be preceded, within its
        site's shield block, by a ``_starting_pids.add(`` -- the facade's
        start-to-registration guard the orphan sweep unions into its active
        set. A site that stamps unshielded re-opens the Windows kill window
        the shield exists for, so a refactor that drops one fails here
        loudly.
        """

        from pathlib import Path

        import kiro_crew

        src_root = Path(kiro_crew.__file__).parent
        scanned = 0
        for rel in ("session_allocation.py", "session_background.py", "session_pool.py"):
            lines = (src_root / rel).read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if "await stamp_spawn_identity(" not in line:
                    continue
                scanned += 1
                window_before = "\n".join(lines[max(0, i - 25) : i + 1])
                assert "_starting_pids.add(" in window_before, (
                    f"{rel}:{i + 1}: `await stamp_spawn_identity(` is not preceded by a "
                    "`_starting_pids.add(` shield -- the read suspends after start() "
                    "published the PID but before registration is visible to the "
                    "orphan sweep"
                )
        assert scanned >= 6, f"expected at least 6 stamp sites, scanned {scanned}"


class TestGenerationScopedLatchClear:
    """Reconciliation must not erase an observation NEWER than its own sweep.

    The sweep's initiating read covers every observation up to that moment.
    But the sweep releases its permits before the gate reconciles, so a
    status poll can observe ANOTHER account (a store flap) while the sweep
    runs -- and a child spawned in that window whose bracket reads disagree
    is unstamped, invisible to the stamp gate. An unconditional latch clear
    at reconcile would erase the only record that account existed; the
    generation scope keeps the latch armed so the next turn sweeps again.
    """

    _PERSONAL = {
        "start_url": "https://personal.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
    }
    _THIRD = {
        "start_url": "https://third.awsapps.com/start",
        "profile": "arn:aws:codewhisperer:us-east-1:3333:profile/THIRD",
    }

    async def _seeded_service(self, tmp_path: Path) -> "kp.KiroPrerequisiteService":
        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True
        return service

    @pytest.mark.asyncio
    async def test_an_observation_during_the_sweep_survives_reconcile(self, tmp_path: Path) -> None:
        """The finding's scenario: A->B sweep, C observed mid-sweep, store back to B.

        The reconcile that completes the A->B sweep must NOT clear the latch
        the C observation armed: the sweep compared children against B, never
        against C, and clearing would leave a C-authenticated child serving
        the next B turn with nothing left to catch it.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        # The gate's initiating read observes the switch to B.
        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        changed, live = await service.identity_changed_since_sessions()
        assert changed is True and live != ""
        # What chat_runner captures right after the read.
        observed_gen = service.identity_observation_generation

        # While the sweep runs: a poll observes a flap to a THIRD account.
        _write_store(db, **self._THIRD)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)

        # The store returns to B before the sweep reconciles.
        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        service.note_sessions_reconciled(live, observations_before=observed_gen)

        changed, _ = await service.identity_changed_since_sessions()
        assert changed is True, (
            "the C observation postdates the sweep's read; reconciling the "
            "A->B sweep must keep the latch armed so the next turn sweeps"
        )

    @pytest.mark.asyncio
    async def test_reconcile_with_no_newer_observation_clears_the_latch(
        self, tmp_path: Path
    ) -> None:
        """The loop guard: the ordinary reconcile still quiets later turns.

        When nothing was observed after the gate's read, the sweep covered
        every observation and the latch MUST clear -- a latch that survived
        this path would force retire-until-complete sweeps on a healthy host,
        the exact pathology the seeded baseline removes.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        _write_store(db)  # back to the seeded account
        _expire_identity_cache(service)

        changed, live = await service.identity_changed_since_sessions()
        assert changed is True
        observed_gen = service.identity_observation_generation
        service.note_sessions_reconciled(live, observations_before=observed_gen)

        for _ in range(3):
            _expire_identity_cache(service)
            changed, _ = await service.identity_changed_since_sessions()
            assert changed is False, "a covered observation must not keep forcing sweeps"

    @pytest.mark.asyncio
    async def test_a_repeat_observation_while_armed_still_bumps_the_generation(
        self, tmp_path: Path
    ) -> None:
        """An armed latch must not swallow later observations' recency.

        If arming short-circuited while already armed, an account observed
        DURING a sweep would carry the pre-sweep generation and the reconcile
        would clear it as covered -- the same erasure through a side door.
        """

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        service = await self._seeded_service(tmp_path)

        _write_store(db, **self._PERSONAL)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        gen_after_first = service.identity_observation_generation
        assert gen_after_first > 0

        _write_store(db, **self._THIRD)
        _expire_identity_cache(service)
        await service.current_identity_fingerprint(allow_cached=False)
        assert service.identity_observation_generation > gen_after_first, (
            "an observation landing while the latch is armed must still "
            "postdate a sweep that read the store before it"
        )

    def test_the_turn_gate_passes_the_captured_generation(self) -> None:
        """Source scan: chat_runner captures the generation and hands it back.

        ``observations_before=None`` clears unconditionally (the legacy test
        shape), so the production gate forgetting to pass its capture would
        silently reopen the erasure. Pin both halves of the handshake.
        """

        from pathlib import Path as _Path

        import kiro_crew.dashboard.chat_runner as chat_runner_module

        text = _Path(chat_runner_module.__file__).read_text(encoding="utf-8")
        assert 'getattr(service, "identity_observation_generation", None)' in text, (
            "the turn gate must capture the observation generation right "
            "after its identity_changed_since_sessions read"
        )
        assert "note_sessions_reconciled(live, observations_before=observed_gen)" in text, (
            "the turn gate must hand its captured generation back at "
            "reconcile, or observations landing during the sweep are erased"
        )


class TestFlaggedSessionResumePointer:
    """A stamp-flagged session must lose its durable resume pointer.

    The eviction that recycles a flagged session replaces the process, but
    ``get_or_create``'s re-entry reads ``resume_sid`` from the session map --
    and ``close_all`` skips retire-flagged sessions by design, so a stale
    pointer would survive a gateway restart too. Either way the replacement
    child, authenticated under the CURRENT account, would ``session/load``
    the flagged account's conversation, whose signed thinking blocks its
    provider rejects wholesale.
    """

    @pytest.mark.asyncio
    async def test_a_flagged_session_loses_its_resume_pointer(self, tmp_path: Path) -> None:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        db = kp.kiro_identity_store_path("linux", tmp_path, {})
        _write_store(db)
        service = kp.KiroPrerequisiteService(home=tmp_path, environ={}, platform_name="linux")
        assert await service.seed_sessions_baseline() is True

        _write_store(
            db,
            start_url="https://personal.awsapps.com/start",
            profile="arn:aws:codewhisperer:us-east-1:2222:profile/PERSONAL",
        )
        _expire_identity_cache(service)
        victim_provider = _FakeProvider("")
        pre_spawn = await kp.pre_spawn_identity(service.read_spawn_identity)
        await kp.stamp_spawn_identity(
            service.read_spawn_identity, victim_provider, pre_spawn=pre_spawn
        )
        assert getattr(victim_provider, "spawn_identity", "") != ""

        _write_store(db)
        _expire_identity_cache(service)
        live = await service.current_identity_fingerprint(allow_cached=False)

        smap = SessionManager(KiroCrewConfig())
        smap._sessions["victim"] = _Session(provider=victim_provider)  # type: ignore[arg-type]
        keeper_provider = _FakeProvider("")
        keeper_provider.spawn_identity = live  # matches the live account
        smap._sessions["keeper"] = _Session(provider=keeper_provider)  # type: ignore[arg-type]

        cleared: list[str] = []
        smap._session_map.clear_sid = cleared.append  # type: ignore[method-assign]

        flagged = await smap.flag_identity_stamp_mismatches(live)
        assert flagged == ["victim"]
        assert cleared == ["victim"], (
            "the flagged session's resume pointer must be dropped at flag "
            "time, mirroring the sweep's clear_sid over invalidated keys"
        )


class TestDrainReapConcurrentPark:
    """A runtime parked WHILE the drain reap kills another must survive.

    The reap awaits each kill, and a concurrent turn gate can displace a new
    runtime onto the drain list during that suspension. The list is the only
    reference the parked runtime has left -- it is already popped from
    ``_subagent_runtimes`` -- so dropping it orphans a live child: invisible
    to ``close_all`` and to the PID shield alike.
    """

    @staticmethod
    def _runtime(*, busy: bool, parked_at: float | None = None) -> "SimpleNamespace":
        killed: list[str] = []

        async def _kill(*, expected: bool = False, reason: str = "") -> None:
            killed.append(reason)

        ns = SimpleNamespace(
            uses_kiro_identity_store=True,
            spawn_identity="cli-b",
            has_active_or_initializing_sessions=lambda: busy,
            kill=_kill,
            killed=killed,
            pid=_UNALLOCATABLE_PID,
        )
        ns.is_alive = lambda: not killed
        if parked_at is not None:
            ns._identity_parked_at = parked_at
        return ns

    @pytest.mark.asyncio
    async def test_a_runtime_parked_during_the_reap_kill_survives(self) -> None:
        import time as _time

        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.kiro_prerequisite import IDENTITY_PARK_GRACE_SECS
        from kiro_crew.session import SessionManager

        smap = SessionManager(KiroCrewConfig())
        aged = _time.monotonic() - IDENTITY_PARK_GRACE_SECS - 1.0
        drained = self._runtime(busy=False, parked_at=aged)
        late_park = self._runtime(busy=True, parked_at=_time.monotonic())

        original_kill = drained.kill

        async def _kill_and_park(*, expected: bool = False, reason: str = "") -> None:
            # While this kill's await is suspended, a CONCURRENT reap pass
            # finishes: its own rebuild drops the entry this pass is killing
            # and carries the runtime a turn gate just displaced. Any rebuild
            # this pass then writes from its pre-await traversal erases that
            # park; targeted removal of what this pass killed cannot.
            smap._draining_subagent_runtimes[:] = [late_park]
            await original_kill(expected=expected, reason=reason)

        drained.kill = _kill_and_park
        smap._draining_subagent_runtimes.append(drained)

        await smap.flag_identity_stamp_mismatches("cli-a")

        assert drained.killed, "the drained runtime past its grace is reaped"
        assert late_park in smap._draining_subagent_runtimes, (
            "a runtime parked during the reap's kill await must stay on the "
            "drain list -- it is that runtime's only remaining reference"
        )
