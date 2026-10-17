"""External kiro-cli logout / account switch must invalidate running state.

Three properties together stop a signed-out account from answering: the
fingerprint must ignore token rotation, an ordinary status poll must re-probe when
the account changes, and a turn must retire the children that still hold the old
credential. This module pins what the fingerprint reads. The baseline, latch and
turn gate are in ``test_external_logout_detection_baseline.py``, retirement in
``test_external_logout_detection_retirement.py``, and spawn-time identity in
``test_external_logout_detection_spawn_identity.py``.
"""

from __future__ import annotations

import errno
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from external_logout_detection_helpers import _private_sel_root_per_test  # noqa: F401
from external_logout_detection_helpers import _write_store

from kiro_crew import kiro_prerequisite as kp


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
