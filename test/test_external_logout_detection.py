"""External kiro-cli logout / account switch must invalidate running state.

Covers the three properties that together let a signed-out account keep
answering: the fingerprint must ignore token rotation, an ordinary status poll
must re-probe when the account changes, and a turn must retire the children that
still hold the old credential.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest

from kiro_crew import kiro_prerequisite as kp
from kiro_crew.session import _MAX_CONCURRENT_COLD_STARTS as _MAX_COLD_STARTS_FOR_TEST


@pytest.fixture(autouse=True)
def _private_sel_root_per_test(sel_private_root):
    """Every test in this module gets its OWN SEL root (issue #7029).

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

        def _counted(path):
            reads.append(1)
            return real(path)

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
    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.shutdown_calls = 0

    @property
    def uses_kiro_identity_store(self) -> bool:
        from kiro_crew.acp.types import ACP_BACKENDS_KIRO_IDENTITY_STORE

        return self.backend in ACP_BACKENDS_KIRO_IDENTITY_STORE

    def is_process_alive(self) -> bool:
        return True

    def is_alive(self) -> bool:
        return True

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeRuntime:
    """Stands in for AcpRuntime in the retirement sweep."""

    def __init__(
        self, backend: str = "", *, active: bool = False, initializing: bool = False
    ) -> None:
        self._acp_backend = backend
        self._active = active
        self._initializing = initializing
        self.killed = 0

    @property
    def uses_kiro_identity_store(self) -> bool:
        from kiro_crew.acp.types import ACP_BACKENDS_KIRO_IDENTITY_STORE

        return self._acp_backend in ACP_BACKENDS_KIRO_IDENTITY_STORE

    def has_active_sessions(self) -> bool:
        return self._active

    def has_active_or_initializing_sessions(self) -> bool:
        return self._active or self._initializing

    async def kill(self, *, expected: bool = False) -> None:
        self.killed += 1


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

        async def retire_kiro_identity_sessions(self):
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
        """The hold-and-wait deadlock two unserialized sweeps would reach.

        Each sweep drains all four cold-start permits one at a time. With a third
        party holding one (a warm-pool fill or eager spawn at boot -- routine),
        sweep A can hold 3 waiting for its 4th while sweep B holds 1 waiting for
        its 2nd: four taken, none free, and neither releases until it reaches four.
        The releases live in a `finally` that never runs, and the wait is un-timed,
        so both turns hang forever AND every later cold start blocks on a drained
        semaphore.

        Two concurrent sweeps are the common boot case, not an exotic one: with
        `_session_identity` unset, every in-flight turn sees a change at once.
        """

        smap = self._manager()
        # Hold ALL permits first, so both sweeps are queued as waiters before any
        # permit is free. This is what makes them interleave: `acquire()` has a
        # non-yielding fast path, so a lone sweep would otherwise grab every free
        # permit atomically and never give a peer the chance to take one.
        for _ in range(_MAX_COLD_STARTS_FOR_TEST):
            await smap._start_sem.acquire()

        first = asyncio.create_task(smap.retire_kiro_identity_sessions())
        second = asyncio.create_task(smap.retire_kiro_identity_sessions())
        await asyncio.sleep(0.05)
        assert not first.done() and not second.done()

        # Hand the permits back one at a time. Unserialized, the two sweeps
        # alternate as FIFO waiters -- each takes one and re-queues behind the
        # other -- until all four are split between them with none free and neither
        # at its required four. Their `finally` releases never run, so both hang.
        for _ in range(_MAX_COLD_STARTS_FOR_TEST):
            smap._start_sem.release()
            await asyncio.sleep(0)

        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)
        assert all(complete for _, complete in results)
        # Every permit returned, so later cold starts are unaffected.
        assert smap._start_sem._value == _MAX_COLD_STARTS_FOR_TEST

    @pytest.mark.asyncio
    async def test_the_barrier_waits_for_an_in_flight_cold_start(self) -> None:
        """The scan must be authoritative, so it WAITS for every permit.

        A partial barrier is not enough: reporting "incomplete" defers the baseline
        but does not stop the current turn, so an eager session spawned under the
        previous account would still win registration and serve it.
        """

        smap = self._manager()
        await smap._start_sem.acquire()

        task = asyncio.create_task(smap.retire_kiro_identity_sessions())
        await asyncio.sleep(0.05)
        assert not task.done(), "the sweep scanned while a cold start was in flight"

        smap._start_sem.release()
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
