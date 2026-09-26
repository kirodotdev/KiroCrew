"""Tests for knowledge add_source local_file support and get_config endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.dashboard.handlers.knowledge import (
    _folder_picker_available,
    _run_folder_dialog,
    add_source,
    get_config,
    import_bundle,
    ingest_text,
    pick_folder,
)
from kiro_crew.knowledge.store import KnowledgeStore


@pytest.fixture()
def store(tmp_path):
    s = KnowledgeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def _make_app(store, pipeline=None):
    """Create minimal app with knowledge routes for testing."""
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    if pipeline:
        app["knowledge_pipeline"] = pipeline
    app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=None))
    app.router.add_get("/api/knowledge/config", get_config)
    app.router.add_post("/api/knowledge/sources", add_source)
    return app


class TestGetConfig:
    @pytest.mark.asyncio
    async def test_returns_enabled_and_formats(self, store):
        async with TestClient(TestServer(_make_app(store, pipeline=MagicMock()))) as client:
            resp = await client.get("/api/knowledge/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["enabled"] is True
            assert ".md" in data["supported_formats"]
            assert ".py" in data["supported_formats"]
            assert "" not in data["supported_formats"]


class TestAddSourceLocalFile:
    @pytest.mark.asyncio
    async def test_rejects_relative_path(self, store):
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": "relative/path.md"
            })
            assert resp.status == 400
            data = await resp.json()
            assert "absolute path" in data["error"]
            # Machine-readable code so the frontend can translate (AGENTS.md:
            # new non-2xx JSON bodies must carry a `code`).
            assert data["code"] == "uri_not_absolute"

    @pytest.mark.asyncio
    async def test_rejects_extended_length_and_unc_prefixes(self, store):
        r"""``\\?\C:\...`` survives Path.resolve() un-normalized, so
        is_sensitive_path() does NOT match it against the credential paths that
        the plain ``C:\...`` form hits — admitting one would route around the
        sensitive-path floor and let ``.ssh/id_rsa`` be ingested. Neither the
        extended-length nor the UNC prefix is something the file picker
        produces, so both are refused before the absoluteness gate."""
        blocked = [
            "\\\\?\\C:\\Users\\me\\.ssh\\id_rsa",  # Win32 extended-length
            "\\\\localhost\\C$\\Users\\me\\.aws\\credentials",  # UNC
            "//localhost/C$/Users/me/.aws/credentials",  # UNC, forward slashes
            # Windows accepts mixed slash flavours as a device-path prefix, so
            # the check has to match "first two chars are any slash", not
            # literal "\\" / "//":
            "\\/?\\C:\\Users\\me\\.ssh\\id_rsa",  # mixed \/
            "/\\?\\C:\\Users\\me\\.ssh\\id_rsa",  # mixed /\
        ]
        async with TestClient(TestServer(_make_app(store))) as client:
            for uri in blocked:
                resp = await client.post("/api/knowledge/sources", json={
                    "name": "x", "source_type": "local_file", "uri": uri
                })
                assert resp.status == 400, (uri, resp.status)
                data = await resp.json()
                assert data["code"] == "uri_unsupported_prefix", (uri, data)

    @pytest.mark.asyncio
    async def test_accepts_windows_drive_path(self, store, tmp_path):
        # The absoluteness gate must use Path.is_absolute(), not a leading-"/"
        # test — a Windows absolute path (C:\...) never starts with "/", so the
        # old check made single-file ingest 100% unusable on Windows.
        from pathlib import PureWindowsPath

        # A drive-letter path is absolute under Windows path semantics.
        assert PureWindowsPath("C:\\Users\\me\\notes\\design.md").is_absolute()
        # And the handler's own gate accepts a real absolute path on this host:
        test_file = tmp_path / "win.md"
        test_file.write_text("# Win")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "win.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp.status == 201

    @pytest.mark.asyncio
    async def test_rejects_sensitive_path(self, store, tmp_path):
        # Create a symlink to a sensitive path
        sensitive = str(Path.home() / ".ssh" / "config")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": sensitive
            })
            assert resp.status == 403
            data = await resp.json()
            assert "restricted" in data["error"]

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_file(self, store, tmp_path):
        # A platform-absolute path that does not exist: under tmp_path so it is
        # absolute on both POSIX ("/...") and Windows ("C:\..."). A hardcoded
        # "/tmp/..." is NOT absolute on Windows and would trip the 400
        # absoluteness gate before reaching the not-found check.
        missing = str(tmp_path / "nonexistent_xyz_12345.md")
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": missing
            })
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rejects_directory(self, store, tmp_path):
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "test", "source_type": "local_file", "uri": str(tmp_path)
            })
            assert resp.status == 404
            data = await resp.json()
            assert "file not found" in data["error"]

    @pytest.mark.asyncio
    async def test_accepts_valid_file(self, store, tmp_path):
        test_file = tmp_path / "hello.md"
        test_file.write_text("# Hello")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "hello.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp.status == 201
            data = await resp.json()
            assert "id" in data

    @pytest.mark.asyncio
    async def test_duplicate_returns_409(self, store, tmp_path):
        test_file = tmp_path / "dup.md"
        test_file.write_text("content")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp1 = await client.post("/api/knowledge/sources", json={
                "name": "dup.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp1.status == 201
            resp2 = await client.post("/api/knowledge/sources", json={
                "name": "dup.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp2.status == 409

    @pytest.mark.asyncio
    async def test_resolves_symlinks(self, store, tmp_path):
        real_file = tmp_path / "real.md"
        real_file.write_text("content")
        link = tmp_path / "link.md"
        link.symlink_to(real_file)
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "link.md", "source_type": "local_file", "uri": str(link)
            })
            assert resp.status == 201
            # Stored URI should be the resolved path
            source = store.get_source_by_uri(str(real_file.resolve()))
            assert source is not None

    @pytest.mark.asyncio
    async def test_symlink_to_sensitive_blocked(self, store, tmp_path):
        # Create symlink pointing to sensitive location
        link = tmp_path / "innocent.md"
        sensitive_target = Path.home() / ".aws" / "credentials"
        link.symlink_to(sensitive_target)
        async with TestClient(TestServer(_make_app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "innocent.md", "source_type": "local_file", "uri": str(link)
            })
            # Either 403 (sensitive) or 404 (doesn't exist) depending on whether file exists
            assert resp.status in (403, 404)

    @pytest.mark.asyncio
    async def test_triggers_immediate_ingestion(self, store, tmp_path):
        test_file = tmp_path / "ingest.md"
        test_file.write_text("# Ingest me")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "ingest.md", "source_type": "local_file", "uri": str(test_file)
            })
            assert resp.status == 201
            # The task claims 'syncing' off the loop before it ingests, so reaching
            # ingest_file costs a worker-thread hop. Poll rather than sleep a fixed
            # span, which races that on a loaded runner.
            import asyncio
            for _ in range(200):
                if pipeline.ingest_file.called:
                    break
                await asyncio.sleep(0.01)
            pipeline.ingest_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_path_traversal_blocked(self, store, tmp_path):
        # Create a file, then try to access it via ../.. traversal
        test_file = tmp_path / "safe.md"
        test_file.write_text("safe")
        # Construct a traversal path that resolves to the same file
        traversal = str(tmp_path / "subdir" / ".." / "safe.md")
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock()
        async with TestClient(TestServer(_make_app(store, pipeline=pipeline))) as client:
            resp = await client.post("/api/knowledge/sources", json={
                "name": "safe.md", "source_type": "local_file", "uri": traversal
            })
            # Should succeed but store the resolved canonical path
            assert resp.status == 201
            source = store.get_source_by_uri(str(test_file.resolve()))
            assert source is not None


def _make_pick_app(store, local_only=True):
    app = web.Application()
    state = MagicMock()
    state.knowledge_store = store
    app["state"] = state
    app["local_only"] = local_only
    app.router.add_post("/api/knowledge/pick-folder", pick_folder)
    app.router.add_get("/api/knowledge/config", get_config)
    return app


def _fake_request(local_only=True):
    return SimpleNamespace(app={"local_only": local_only})


def _trusted(monkeypatch, path="/usr/bin/osascript"):
    """Answer the trusted-binary lookup without touching this host's filesystem.

    Every darwin-simulating picker test needs it. The real lookup probes the
    system directories, and the machine running these tests has no osascript in
    them, so an unpatched probe would send every test down the unavailable arm.
    Pass ``None`` to exercise that arm deliberately.
    """
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.knowledge.platform_compat.trusted_system_bin",
        lambda name: path,
    )


class TestFolderPickerAvailable:
    def test_available_on_mac_local(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        _trusted(monkeypatch)
        assert _folder_picker_available(_fake_request(local_only=True)) is True

    def test_unavailable_when_osascript_is_not_trusted(self, monkeypatch):
        """A macOS host whose osascript does not resolve out of the system
        directories offers no picker, so the UI hides the button instead of
        showing one whose only possible answer is a refusal."""
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        _trusted(monkeypatch, None)
        assert _folder_picker_available(_fake_request(local_only=True)) is False

    def test_unavailable_off_mac(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "linux")
        assert _folder_picker_available(_fake_request(local_only=True)) is False

    def test_unavailable_when_remote(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        assert _folder_picker_available(_fake_request(local_only=False)) is False

    def test_fail_closed_when_local_only_unset(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        assert _folder_picker_available(SimpleNamespace(app={})) is False


class TestRunFolderDialog:
    def test_picked_returns_path(self, monkeypatch):
        completed = MagicMock(returncode=0, stdout="/home/user/notes\n")
        seen: dict[str, list[str]] = {}

        def run(cmd, *a, **k):
            seen["cmd"] = cmd
            return completed

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge.subprocess.run", run,
        )
        _trusted(monkeypatch)
        assert _run_folder_dialog() == "/home/user/notes"
        # The resolved absolute path, never the bare name a planted shim answers.
        assert seen["cmd"][0] == "/usr/bin/osascript"

    def test_untrusted_binary_never_spawns(self, monkeypatch):
        """An osascript that does not resolve out of the trusted directories is a
        refusal: no process starts, and the caller reads it as a failed launch."""
        def boom(*a, **k):
            raise AssertionError("spawned a dialog with an untrusted binary")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge.subprocess.run", boom,
        )
        _trusted(monkeypatch, None)
        assert _run_folder_dialog() is None

    def test_cancel_returns_none(self, monkeypatch):
        completed = MagicMock(returncode=1, stdout="")
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge.subprocess.run",
            lambda *a, **k: completed,
        )
        _trusted(monkeypatch)
        assert _run_folder_dialog() is None

    def test_launch_failure_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge.subprocess.run", boom,
        )
        _trusted(monkeypatch)
        assert _run_folder_dialog() is None


class TestPickFolderHandler:
    @pytest.mark.asyncio
    async def test_blocked_when_not_local_only(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        async with TestClient(TestServer(_make_pick_app(store, local_only=False))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_blocked_when_not_mac(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "linux")
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_blocked_when_osascript_is_not_trusted(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        _trusted(monkeypatch, None)
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_returns_picked_path(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        _trusted(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge._run_folder_dialog",
            lambda: "/home/user/notes",
        )
        async with TestClient(TestServer(_make_pick_app(store))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 200
            assert (await resp.json())["path"] == "/home/user/notes"

    @pytest.mark.asyncio
    async def test_returns_null_on_cancel(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        _trusted(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge._run_folder_dialog",
            lambda: None,
        )
        async with TestClient(TestServer(_make_pick_app(store))) as client:
            resp = await client.post("/api/knowledge/pick-folder")
            assert resp.status == 200
            assert (await resp.json())["path"] is None


class TestConfigFolderPickerFlag:
    @pytest.mark.asyncio
    async def test_reports_true_on_mac_local(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        _trusted(monkeypatch)
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.get("/api/knowledge/config")
            assert (await resp.json())["folder_picker"] is True

    @pytest.mark.asyncio
    async def test_reports_false_off_mac(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "win32")
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.get("/api/knowledge/config")
            assert (await resp.json())["folder_picker"] is False


def _grant_bedrock(
    *, profile: str = "team-a", region: str = "us-east-1", account: str = "111122223333"
):
    """Record a REAL Bedrock KB grant in the isolated consent store.

    The attestation the insert records pins the account the grant confirms, so
    the bedrock_kb tests need a readable grant, not only a stubbed
    ``is_granted``; the real ``is_granted`` then passes for the body's target.
    """
    from kiro_crew import aws_consent

    return aws_consent.record_grant(
        aws_consent.SERVICE_BEDROCK_KB,
        profile=profile,
        region=region,
        account=account,
        arn=f"arn:aws:iam::{account}:user/x",
        granted_at="2026-09-06T00:00:00+00:00",
    )


class TestAddSourceBedrockGrantRecheck:
    """The locked insert re-reads the LIVE grant for the exact target and
    refuses a grantless registration: ``is_granted`` returns ``(granted,
    reason)``, so the guard must test the boolean, not the tuple."""

    # The body the dashboard sends (SourcesList.tsx): a derived logical uri
    # plus kb_ids / region / profile in properties.
    _BODY = {
        "name": "kb",
        "source_type": "bedrock_kb",
        "uri": "bedrock-kb://us-east-1/ABCDEFGHIJ",
        "properties": {"kb_ids": "ABCDEFGHIJ", "region": "us-east-1", "profile": "team-a"},
    }

    @pytest.fixture(autouse=True)
    def _granted(self):
        _grant_bedrock()

    @staticmethod
    def _app(store, *, owner: str = ""):
        # A registered connector whose validation passes, so the request
        # reaches the locked insert (an unregistered type stops at the
        # https:// rule and never gets there). Owner-shaped: the bedrock_kb
        # branch is owner-gated before validation, and ``as_owner`` leaves a
        # fixture's own ``state`` alone, so the MagicMock state needs an
        # explicit ``owner_id`` (a MagicMock attribute is a truthy, unmatched
        # owner and would refuse the default ``local-app`` caller).
        app = _make_app(store)
        app["state"].owner_id = owner
        connector = MagicMock(validate_config=MagicMock(return_value=(True, None)))
        app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=connector))
        return as_owner(app)

    @pytest.mark.asyncio
    async def test_no_grant_for_the_target_refuses_and_persists_nothing(self, store, monkeypatch):
        from kiro_crew import aws_consent

        monkeypatch.setattr(aws_consent, "read_grant", lambda service: None)
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "bedrock_kb_grant_changed"
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM sources WHERE source_type = 'bedrock_kb'"
            ).fetchone()[0]
            == 0
        )

    @pytest.mark.asyncio
    async def test_matching_grant_lets_the_insert_through(self, store, monkeypatch):
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 201

    @pytest.mark.asyncio
    async def test_target_conflict_refusal_redacts_the_held_rows_name(self, store, monkeypatch):
        """The 409 (the consent POST's status for the same code) names the
        registered source that holds the other target, and that name comes
        from an agent-writable row the handler's own bound never saw. A
        planted credential or exfil URL in it reaches the operator as the
        redaction tag, not the value. Found in review."""
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        token = "aB3" * 70
        planted = (
            "Team KB AKIAIOSFODNN7EXAMPLE https://collect.attacker.example/?token="
            + token
            + "&host=corp-laptop"
        )
        store.db.execute(
            "INSERT INTO sources (name, source_type, uri, properties, created_at, updated_at) "
            "VALUES (?, 'bedrock_kb', 'bedrock-kb://eu-west-1/KBTEST0001', ?, "
            "'2026-09-22T00:00:00', '2026-09-22T00:00:00')",
            (
                planted,
                json.dumps({"kb_ids": "KBTEST0001", "region": "eu-west-1", "profile": "team-b"}),
            ),
        )
        store.db.commit()
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 409, await resp.text()
            data = await resp.json()
        assert data["code"] == "bedrock_kb_target_conflict"
        assert "AKIAIOSFODNN7EXAMPLE" not in data["error"]
        assert token not in data["error"] and "corp-laptop" not in data["error"]
        assert "[REDACTED: credential]" in data["error"]
        assert "[REDACTED: suspicious URL to collect.attacker.example]" in data["error"]
        assert "(team-b, eu-west-1)" in data["error"]

    @staticmethod
    def _app_reconfirming_during_validation(store, *, account: str):
        """The owner-shaped app whose connector validation, in the seconds it
        runs outside the lock, sees the consent POST land a same-target
        confirmation for ``account`` -- the race the locked insert must see."""
        app = TestAddSourceBedrockGrantRecheck._app(store)

        def _validate_then_reconfirm(config):
            _grant_bedrock(account=account)
            return True, None

        connector = MagicMock(validate_config=MagicMock(side_effect=_validate_then_reconfirm))
        app["knowledge_sync"] = MagicMock(get_connector=MagicMock(return_value=connector))
        return app

    @pytest.mark.asyncio
    async def test_a_same_target_reconfirmation_for_another_account_during_validation_is_refused(
        self, store
    ):
        """Same target is not the same account. Validation probed the KB under
        the account the grant confirmed when it started; a confirmation for
        another account with the same (profile, region) passes the target
        equality check, and the attestation would pin the new account -- a KB
        never validated there. Refused with nothing persisted. Found in review."""
        from kiro_crew import aws_consent

        app = self._app_reconfirming_during_validation(store, account="999988887777")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "bedrock_kb_grant_changed"
        assert store.db.execute(
            "SELECT COUNT(*) FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchone()[0] == 0
        assert aws_consent.attested_sources() == {}

    @pytest.mark.asyncio
    async def test_a_same_target_same_account_reconfirmation_during_validation_still_lands(
        self, store
    ):
        """The documented choice holds: retrieval demands target equality, not
        grant identity. A fresh grant for the same target AND the same account
        is the account validation ran under, so the insert goes through and
        the attestation pins that account."""
        from kiro_crew import aws_consent

        app = self._app_reconfirming_during_validation(store, account="111122223333")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 201, await resp.text()
            sid = (await resp.json())["id"]
        attested = aws_consent.attested_sources()
        assert set(attested) == {sid}
        assert attested[sid]["account"] == "111122223333"

    @pytest.mark.asyncio
    async def test_the_insert_attests_the_row_and_delete_revokes_it(self, store, monkeypatch):
        """The row alone proves nothing (knowledge.db is agent-writable
        in-sandbox), so the owner-gated insert records the row in the sealed
        consent store, pinned to the values it stored; the paid enumeration
        searches only such rows. Deleting the row drops the attestation, so a
        later row minted under the same id cannot inherit it."""
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers.knowledge import delete_source

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 201
            sid = (await resp.json())["id"]
            row = store.db.execute(
                "SELECT uri, properties FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            attested = aws_consent.attested_sources()
            assert set(attested) == {sid}
            assert aws_consent.source_matches_attestation(
                attested, sid, row["uri"], json.loads(row["properties"])
            )
            # The attestation pins the stored triple: a row rewritten to name
            # another KB does not match it.
            assert not aws_consent.source_matches_attestation(
                attested, sid, row["uri"],
                {**json.loads(row["properties"]), "kb_ids": "ABCDEFGHIJ,KLMNOPQRST"},
            )

            resp = await client.delete(f"/api/knowledge/sources/{sid}")
            assert resp.status == 200
            assert aws_consent.attested_sources() == {}

    @pytest.mark.asyncio
    async def test_an_unreadable_consent_store_refuses_the_delete_and_leaves_the_row(
        self, store, monkeypatch
    ):
        """The revoke must not read an unreadable store as "nothing attested":
        the row would go while the attestation stays on disk, for a row
        re-minted under the same id and values to inherit. The revoke raises,
        the route answers its coded 500, the row and the file's bytes stay.
        Found in review."""
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers.knowledge import delete_source

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 201
            sid = (await resp.json())["id"]
            path = aws_consent.aws_consent_path()
            path.write_text("{not json", encoding="utf-8")
            resp = await client.delete(f"/api/knowledge/sources/{sid}")
            assert resp.status == 500, await resp.text()
            assert (await resp.json())["code"] == "bedrock_kb_attestation_revoke_failed"
        assert store.db.execute(
            "SELECT COUNT(*) FROM sources WHERE id = ?", (sid,)
        ).fetchone()[0] == 1
        assert path.read_text(encoding="utf-8") == "{not json"

    @pytest.mark.asyncio
    async def test_the_row_retains_only_the_keys_the_connector_reads(self, store, monkeypatch):
        """``properties`` is retained and re-parsed at every search, so the
        insert projects it onto the read set: extra fields in the body, flat
        or nested, never reach the row."""
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        body = {
            **self._BODY,
            "properties": {
                **self._BODY["properties"],
                "note": "x" * 5000,
                "nested": {"a": [1, 2, 3]},
                "sync_status": "active",
            },
        }
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=body)
            assert resp.status == 201
        row = store.db.execute(
            "SELECT properties FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchone()
        assert json.loads(row["properties"]) == self._BODY["properties"]

    @pytest.mark.asyncio
    async def test_the_row_retains_the_validated_values_not_the_raw_spelling(
        self, store, monkeypatch
    ):
        """Every gate strips or parses before it judges, so whitespace padding
        (and an ARN spelling of a KB id) passes them all. The row must hold the
        values the gates checked -- stripped region/profile, canonical bare
        ids -- so its size follows the gates' bounds instead of the request
        body cap, and its spelling is the one every later compare uses."""
        from kiro_crew import aws_consent

        seen: list[tuple[str, str]] = []

        def _granted(service, *, profile, region):
            seen.append((profile, region))
            return True, ""

        monkeypatch.setattr(aws_consent, "is_granted", _granted)
        pad = " " * 5000
        body = {
            **self._BODY,
            "properties": {
                "kb_ids": (
                    f"{pad}arn:aws:bedrock:us-east-1:123456789012:knowledge-base/ABCDEFGHIJ{pad},"
                    f" KLMNOPQRST{pad}"
                ),
                "region": f"{pad}us-east-1{pad}",
                "profile": f"{pad}team-a{pad}",
            },
        }
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=body)
            assert resp.status == 201
        row = store.db.execute(
            "SELECT properties FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchone()
        assert json.loads(row["properties"]) == {
            "kb_ids": "ABCDEFGHIJ,KLMNOPQRST",
            "region": "us-east-1",
            "profile": "team-a",
        }
        # The grant equality judged exactly the values the row now holds.
        assert seen == [("team-a", "us-east-1")]

    @pytest.mark.asyncio
    async def test_the_row_is_keyed_by_the_server_derived_handle_not_the_callers(
        self, store, monkeypatch
    ):
        """``sources.uri`` is the UNIQUE dedup key, so it is derived from the
        validated config on the server. A caller-chosen handle (oversized, or
        simply unique per request) never reaches the row, and a second add of
        the same knowledge base under another handle dedupes against the first
        instead of minting a row -- the row count is one per real KB."""
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        first = {**self._BODY, "uri": "bedrock-kb://us-east-1/" + "x" * 5000}
        second = {**self._BODY, "uri": "bedrock-kb://us-east-1/another-unique-handle"}
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=first)
            assert resp.status == 201
            resp = await client.post("/api/knowledge/sources", json=second)
            assert resp.status == 409
            assert (await resp.json())["code"] == "source_exists"
            # No uri at all is fine too: the server owns the handle.
            without = {k: v for k, v in self._BODY.items() if k != "uri"}
            resp = await client.post("/api/knowledge/sources", json=without)
            assert resp.status == 409
        rows = store.db.execute(
            "SELECT uri FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchall()
        assert [r["uri"] for r in rows] == ["bedrock-kb://us-east-1/ABCDEFGHIJ"]

    @pytest.mark.asyncio
    async def test_the_name_is_bounded_at_add_time_like_the_rename_path(
        self, store, monkeypatch
    ):
        """``name`` is a column every source type retains and every source-list
        response carries. The rename endpoint refuses more than
        ``_MAX_SOURCE_NAME_LEN`` characters; the add path must not admit what a
        rename would refuse, or a valid request lands an oversized row the
        list serves back on every read."""
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers.knowledge import _MAX_SOURCE_NAME_LEN

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        too_long = {**self._BODY, "name": "n" * (_MAX_SOURCE_NAME_LEN + 1)}
        at_ceiling = {**self._BODY, "name": "n" * _MAX_SOURCE_NAME_LEN}
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=too_long)
            assert resp.status == 400
            assert (await resp.json())["code"] == "name_too_long"
            assert store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
            resp = await client.post("/api/knowledge/sources", json=at_ceiling)
            assert resp.status == 201
        row = store.db.execute(
            "SELECT name FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchone()
        assert row["name"] == "n" * _MAX_SOURCE_NAME_LEN

    @pytest.mark.asyncio
    async def test_a_retained_field_outside_its_bound_is_refused_with_a_code(self, store):
        """The connector bounds every field a row retains; here the registered
        connector's validation is a mock that passes anything, so the
        retention bound is the gate that answers, with a coded 400 and no
        row."""
        for bad in (
            {**self._BODY, "properties": {**self._BODY["properties"], "profile": "p" * 129}},
            {**self._BODY, "properties": {**self._BODY["properties"], "region": "r" * 65}},
        ):
            async with TestClient(TestServer(self._app(store))) as client:
                resp = await client.post("/api/knowledge/sources", json=bad)
                assert resp.status == 400
                assert (await resp.json())["code"] == "bedrock_kb_config_invalid"
        assert store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_a_failed_attestation_undoes_the_insert(self, store, monkeypatch):
        """The row insert and the attestation are two writes. A row nothing
        attests is dark for good (the enumeration skips it every search), so
        when the attestation write fails the insert is undone and the add
        reports the failure instead of a half-registered success."""
        from kiro_crew import aws_consent

        def boom(source_id, uri, properties):
            raise OSError("consent store not writable")

        monkeypatch.setattr(aws_consent, "record_source_attestation", boom)
        async with TestClient(TestServer(self._app(store))) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 500
            assert (await resp.json())["code"] == "bedrock_kb_attestation_failed"
        assert store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert aws_consent.attested_sources() == {}

    @pytest.mark.asyncio
    async def test_delete_revokes_before_the_row_goes(self, store, monkeypatch):
        """Order of the two delete writes: the attestation first, so a failure
        between them leaves a dark row (skipped, audited, removable by a
        retry), never an attestation without a row that a later row minted
        under the same id could inherit. A failed revoke deletes nothing."""
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers.knowledge import delete_source

        app = self._app(store)
        app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/knowledge/sources", json=self._BODY)
            assert resp.status == 201
            sid = (await resp.json())["id"]

            def boom(source_id):
                raise OSError("consent store not writable")

            monkeypatch.setattr(aws_consent, "revoke_source_attestation", boom)
            resp = await client.delete(f"/api/knowledge/sources/{sid}")
            assert resp.status == 500
            assert (await resp.json())["code"] == "bedrock_kb_attestation_revoke_failed"
            assert (
                store.db.execute("SELECT COUNT(*) FROM sources WHERE id = ?", (sid,)).fetchone()[0]
                == 1
            )
            assert set(aws_consent.attested_sources()) == {sid}

            monkeypatch.undo()
            order: list[str] = []
            real_revoke = aws_consent.revoke_source_attestation
            real_cascade = store.delete_source_cascade
            monkeypatch.setattr(
                aws_consent,
                "revoke_source_attestation",
                lambda source_id: (order.append("revoke"), real_revoke(source_id))[1],
            )
            monkeypatch.setattr(
                store,
                "delete_source_cascade",
                lambda source_id: (order.append("delete"), real_cascade(source_id))[1],
            )
            resp = await client.delete(f"/api/knowledge/sources/{sid}")
            assert resp.status == 200
            assert order == ["revoke", "delete"]
            assert aws_consent.attested_sources() == {}
            assert store.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0


class TestAddSourceBedrockIsOwnerOnly:
    """Registering a Bedrock source is an OWNER action, like the grant it rides.

    ``add_source`` is open to any authenticated dashboard caller, which is fine
    for a local folder. A ``bedrock_kb`` source validates by probing the KB with
    the owner's AWS credentials and then registers a KB the gateway queries on
    the owner's account, so the same two caller classes the consent endpoint
    shuts out are refused here, BEFORE validation: an allow-listed messaging
    user (``app == ""``, caller is not the owner) and an app token. Found in
    review.
    """

    _BODY = TestAddSourceBedrockGrantRecheck._BODY

    @pytest.fixture(autouse=True)
    def _granted(self):
        _grant_bedrock()

    @staticmethod
    def _app(store):
        return TestAddSourceBedrockGrantRecheck._app(store, owner="owner-1")

    @staticmethod
    def _assert_refused_before_validation(app, store, resp_status, payload):
        assert resp_status == 403
        assert payload["code"] == "owner_only"
        connector = app["knowledge_sync"].get_connector.return_value
        connector.validate_config.assert_not_called()
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM sources WHERE source_type = 'bedrock_kb'"
            ).fetchone()[0]
            == 0
        )

    @pytest.mark.asyncio
    async def test_non_owner_messaging_user_is_refused(self, store, monkeypatch):
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json=self._BODY,
                headers={"X-Test-User": "slack-guest"},
            )
            self._assert_refused_before_validation(app, store, resp.status, await resp.json())

    @pytest.mark.asyncio
    async def test_app_token_is_refused(self, store, monkeypatch):
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json=self._BODY,
                headers={"X-Test-User": "owner-1", "X-Test-App": "some-app"},
            )
            self._assert_refused_before_validation(app, store, resp.status, await resp.json())

    @pytest.mark.asyncio
    async def test_the_owner_still_gets_through(self, store, monkeypatch):
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json=self._BODY,
                headers={"X-Test-User": "owner-1"},
            )
            assert resp.status == 201
        app["knowledge_sync"].get_connector.return_value.validate_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_deleting_a_bedrock_source_is_owner_only_too(self, store, tmp_path):
        """The mirror of the insert gate: removing the owner's registration
        (and the approval recorded with it) is the owner's call. A non-owner
        DELETE is refused before either write, so the row stays attested; a
        local folder row is not gated, as before."""
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers.knowledge import delete_source

        folder = tmp_path / "notes"
        folder.mkdir()
        app = self._app(store)
        app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources", json=self._BODY, headers={"X-Test-User": "owner-1"}
            )
            assert resp.status == 201
            sid = (await resp.json())["id"]
            resp = await client.post(
                "/api/knowledge/sources",
                json={"name": "notes", "source_type": "local_folder", "uri": str(folder)},
                headers={"X-Test-User": "slack-guest"},
            )
            assert resp.status == 201
            folder_sid = (await resp.json())["id"]

            resp = await client.delete(
                f"/api/knowledge/sources/{sid}", headers={"X-Test-User": "slack-guest"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "owner_only"
            assert (
                store.db.execute("SELECT COUNT(*) FROM sources WHERE id = ?", (sid,)).fetchone()[0]
                == 1
            )
            assert set(aws_consent.attested_sources()) == {sid}

            resp = await client.delete(
                f"/api/knowledge/sources/{folder_sid}", headers={"X-Test-User": "slack-guest"}
            )
            assert resp.status == 200

            resp = await client.delete(
                f"/api/knowledge/sources/{sid}", headers={"X-Test-User": "owner-1"}
            )
            assert resp.status == 200
            assert aws_consent.attested_sources() == {}

    @pytest.mark.asyncio
    async def test_a_relabelled_registered_row_is_still_owner_gated_and_revoked(self, store):
        """The delete gate keys on the sealed attestation, not on the row's
        ``source_type``: that column lives in the agent-writable knowledge.db,
        so an in-sandbox agent can re-label a registered row as a local one.
        A delete that trusted the label would skip the owner gate and the
        revoke, leaving the attestation for a row re-minted under the same id
        and values to inherit (review finding). Re-labelled, the row still
        refuses a non-owner, and the owner's delete still revokes."""
        from kiro_crew import aws_consent
        from kiro_crew.dashboard.handlers.knowledge import delete_source

        app = self._app(store)
        app.router.add_delete("/api/knowledge/sources/{id}", delete_source)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources", json=self._BODY, headers={"X-Test-User": "owner-1"}
            )
            assert resp.status == 201
            sid = (await resp.json())["id"]
            assert set(aws_consent.attested_sources()) == {sid}
            # A direct write to the row, as an in-sandbox agent could make.
            store.db.execute("UPDATE sources SET source_type = 'local_folder' WHERE id = ?", (sid,))
            store.db.commit()

            resp = await client.delete(
                f"/api/knowledge/sources/{sid}", headers={"X-Test-User": "slack-guest"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "owner_only"
            assert set(aws_consent.attested_sources()) == {sid}

            resp = await client.delete(
                f"/api/knowledge/sources/{sid}", headers={"X-Test-User": "owner-1"}
            )
            assert resp.status == 200
            assert aws_consent.has_source_attestation(sid) is False
            assert aws_consent.attested_sources() == {}

    @pytest.mark.asyncio
    async def test_local_sources_are_not_gated(self, store, tmp_path):
        """The gate is scoped to bedrock_kb: a local folder from a non-owner
        keeps today's behaviour (no owner check on ``add_source`` itself)."""
        folder = tmp_path / "notes"
        folder.mkdir()
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={"name": "notes", "source_type": "local_folder", "uri": str(folder)},
                headers={"X-Test-User": "slack-guest"},
            )
            assert resp.status == 201, await resp.text()


class TestAddSourceBodyShapeGate:
    """Every field ``add_source`` reads is type-checked ONCE, up front.

    A caller-shaped body is a shape question answered before any field is
    used, so no later line dereferences a wrong-typed value. The Bedrock
    connector ignores ``url`` during validation, so a JSON object in ``uri``
    would otherwise pass validation and reach ``str.startswith`` as a 500.
    """

    _BODY = TestAddSourceBedrockGrantRecheck._BODY

    @pytest.fixture(autouse=True)
    def _granted(self):
        _grant_bedrock()

    @staticmethod
    def _app(store):
        return TestAddSourceBedrockGrantRecheck._app(store, owner="owner-1")

    @pytest.mark.asyncio
    async def test_non_string_uri_is_a_400_before_validation(self, store, monkeypatch):
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={**self._BODY, "uri": {"scheme": "bedrock-kb"}},
                headers={"X-Test-User": "owner-1"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "uri must be a string"
        app["knowledge_sync"].get_connector.return_value.validate_config.assert_not_called()
        assert (
            store.db.execute(
                "SELECT COUNT(*) FROM sources WHERE source_type = 'bedrock_kb'"
            ).fetchone()[0]
            == 0
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("field", "value", "error"),
        [
            ("name", 7, "name must be a string"),
            ("source_type", ["bedrock_kb"], "source_type must be a string"),
            ("namespace", {"x": 1}, "namespace must be a string"),
            ("properties", "kb_ids=ABCDEFGHIJ", "properties must be an object"),
        ],
    )
    async def test_every_read_field_is_gated(self, store, field, value, error):
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={**self._BODY, field: value},
                headers={"X-Test-User": "owner-1"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == error
        app["knowledge_sync"].get_connector.return_value.validate_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_text_fields_read_as_absent(self, store, monkeypatch):
        """``null`` is not a shape violation: each text field has a default,
        so a client that serializes an unset field as null keeps working."""
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/sources",
                json={**self._BODY, "name": None, "namespace": None},
                headers={"X-Test-User": "owner-1"},
            )
            assert resp.status == 201, await resp.text()


class TestBundlesNeverCarryBedrockSources:
    """The bundle routes are the OTHER write path into ``sources``, and
    ``/api/knowledge/import`` is open to every authenticated caller. A
    ``bedrock_kb`` row in a bundle would register a KB with no consent, no
    validation and no target check, against whatever grant the owner already
    holds -- the exact registration ``add_source`` gates to the owner. So the
    store refuses such rows at import and leaves them out of exports (a live
    account binding with no items is not knowledge). Found in review."""

    _ROW = {
        "id": "src-bedrock-1",
        "name": "kb",
        "source_type": "bedrock_kb",
        "uri": "bedrock-kb://us-east-1/ABCDEFGHIJ",
        # A bundle row is a serialized SELECT * row: properties travel as text.
        "properties": json.dumps({"kb_ids": "ABCDEFGHIJ", "region": "us-east-1", "profile": "team-a"}),
        "sync_status": "active",
    }

    @staticmethod
    def _bundle(*sources: dict) -> dict:
        return {
            "items": [],
            "entities": [],
            "relations": [],
            "sources": list(sources),
            "source_locations": [],
            "mentions": [],
        }

    @staticmethod
    def _bedrock_rows(store) -> int:
        return store.db.execute(
            "SELECT COUNT(*) FROM sources WHERE source_type = 'bedrock_kb'"
        ).fetchone()[0]

    @staticmethod
    def _app(store):
        # The owner-shaped app the add-source tests use, plus the import route
        # (the minimal app registers only the routes under test).
        app = TestAddSourceBedrockGrantRecheck._app(store, owner="owner-1")
        app.router.add_post("/api/knowledge/import", import_bundle)
        return app

    @pytest.mark.asyncio
    async def test_non_owner_import_registers_no_bedrock_source(self, store, monkeypatch):
        from kiro_crew import aws_consent

        # A grant for the crafted row's exact target already exists: the state
        # of any deployment whose owner uses Bedrock, and the one in which an
        # imported row would start billing retrievals.
        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        local = {
            "id": "src-local-1", "name": "notes", "source_type": "local_file",
            "uri": "/tmp/notes.md", "properties": "{}", "sync_status": "active",
        }
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/import",
                json=self._bundle(self._ROW, local),
                headers={"X-Test-User": "slack-guest"},
            )
            assert resp.status == 200, await resp.text()
            result = await resp.json()
        # The refused row is counted (the SEL line carries the count), the
        # rest of the bundle lands, and nothing bedrock-shaped exists to search.
        assert result["sources_refused"] == 1
        assert self._bedrock_rows(store) == 0
        assert store.db.execute(
            "SELECT COUNT(*) FROM sources WHERE id = 'src-local-1'"
        ).fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_the_owner_is_refused_the_same_way(self, store, monkeypatch):
        """Not a caller check: the bundle is the wrong path for an account
        binding whoever posts it -- the owner registers a KB through the
        add-source form, where validation and the target check run."""
        from kiro_crew import aws_consent

        monkeypatch.setattr(
            aws_consent, "is_granted", lambda service, *, profile, region: (True, "")
        )
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/import",
                json=self._bundle(self._ROW),
                headers={"X-Test-User": "owner-1"},
            )
            assert resp.status == 200, await resp.text()
            assert (await resp.json())["sources_refused"] == 1
        assert self._bedrock_rows(store) == 0

    def test_export_leaves_a_registered_bedrock_source_out(self, store):
        store.add_source(
            name="kb", source_type="bedrock_kb", uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        store.add_source(name="notes", source_type="local_file", uri="/tmp/notes.md", properties={})
        assert self._bedrock_rows(store) == 1
        exported = store.export_all()["sources"]
        assert [s["source_type"] for s in exported] == ["local_file"]
        # And a round trip of that export changes nothing about the binding:
        # the row stays exactly as the owner registered it, refused count 0.
        assert store.import_bundle(self._bundle(*exported))["sources_refused"] == 0
        assert self._bedrock_rows(store) == 1

    def test_no_item_can_come_to_name_a_bedrock_source(self, store, tmp_path):
        """'Holds no items' is enforced at the writer, not asserted: the export
        drops the row, so an item naming it would be a dangling reference that
        rolls a CLEAN store's import back on the FK. Both ways an item comes to
        name a source are refused -- the insert, and a location attaching an
        existing item (the dedup path). Found in review."""
        from kiro_crew.knowledge.store import LiveSourceHoldsNoItems

        bedrock_id = store.add_source(
            name="kb", source_type="bedrock_kb", uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local_id = store.add_source(
            name="notes", source_type="local_file", uri="/tmp/notes.md", properties={})
        with pytest.raises(LiveSourceHoldsNoItems):
            store.add_item(title="planted", content="local text", item_type="note",
                           source_id=bedrock_id)
        item_id = store.add_item(title="fine", content="local text", item_type="note",
                                 source_id=local_id)
        with pytest.raises(LiveSourceHoldsNoItems):
            store.add_source_location(item_id, bedrock_id)
        # The refused writes left nothing behind, and the store still writes.
        assert self._items_under(store, bedrock_id) == 0
        assert self._items_under(store, local_id) == 1
        assert store.db.execute(
            "SELECT COUNT(*) FROM source_locations WHERE source_id = ?", (bedrock_id,)
        ).fetchone()[0] == 0
        # The export of this store names the dropped row nowhere, so a clean
        # store -- one that never saw the bedrock row -- imports it whole.
        bundle = store.export_all()
        assert bedrock_id not in {i["source_id"] for i in bundle["items"]}
        assert bedrock_id not in {loc["source_id"] for loc in bundle["source_locations"]}
        clean = KnowledgeStore(str(tmp_path / "clean.db"))
        try:
            result = clean.import_bundle(bundle)
        finally:
            clean.close()
        assert result["items_imported"] == 1
        assert result["items_refused"] == 0
        assert result["sources_refused"] == 0

    def test_export_leaves_out_what_a_direct_write_planted_under_a_bedrock_source(
        self, store, tmp_path
    ):
        """The gateway's writers cannot put an item under a live source, but
        knowledge.db is agent-writable in-sandbox, so a row planted by a
        direct write bypasses them. The export drops the source row, so such
        an item -- and the location, mention and relation riding on it -- would
        ride out dangling and roll a clean store's import back on the FK. They
        are left out, the same set the import refuses. Found in review."""
        bedrock_id = store.add_source(
            name="kb", source_type="bedrock_kb", uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local_id = store.add_source(
            name="notes", source_type="local_file", uri="/tmp/notes.md", properties={})
        kept = store.add_item(title="fine", content="local text", item_type="note",
                              source_id=local_id)
        now = "2026-09-22T00:00:00"
        db = store.db
        db.execute(
            "INSERT INTO items (id, title, content, item_type, source_id, created_at, updated_at) "
            "VALUES ('planted', 'planted', 'local text', 'note', ?, ?, ?)",
            (bedrock_id, now, now))
        db.execute(
            "INSERT INTO entities (id, name, entity_type, created_at, updated_at) "
            "VALUES ('ent-1', 'Thing', 'concept', ?, ?)", (now, now))
        db.execute(
            "INSERT INTO entity_relations (id, source_id, target_id, relation_type, "
            "source_item_id, created_at) VALUES ('rel-planted', 'ent-1', 'ent-1', 'self', "
            "'planted', ?)", (now,))
        db.execute(
            "INSERT INTO entity_relations (id, source_id, target_id, relation_type, "
            "source_item_id, created_at) VALUES ('rel-kept', 'ent-1', 'ent-1', 'self', ?, ?)",
            (kept, now))
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-planted', 'planted', ?, ?)", (bedrock_id, now))
        db.execute(
            "INSERT INTO mentions (item_id, entity_id, created_at) "
            "VALUES ('planted', 'ent-1', ?)", (now,))
        db.commit()
        bundle = store.export_all()
        assert {i["id"] for i in bundle["items"]} == {kept}
        assert {r["id"] for r in bundle["relations"]} == {"rel-kept"}
        assert bundle["source_locations"] == []
        assert bundle["mentions"] == []
        assert [s["source_type"] for s in bundle["sources"]] == ["local_file"]
        clean = KnowledgeStore(str(tmp_path / "clean.db"))
        try:
            result = clean.import_bundle(bundle)
        finally:
            clean.close()
        assert result["items_imported"] == 1
        assert result["relations_rebuilt"] == 1
        assert result["items_refused"] == 0

    def test_a_planted_bedrock_holder_never_becomes_an_item_owner(self, store, tmp_path):
        """Deleting a source hands each surviving item to another holder. A
        ``source_locations`` row a direct write planted under a live source must
        not make that source the new owner: the item would survive live and be
        left out of every bundle, so the next restore loses it (review finding).
        On both deletion paths the new owner comes from the holders that may
        take ownership; an item whose only other holder is the live source goes
        with its owner, and the planted row goes with it."""
        bedrock_id = store.add_source(
            name="kb", source_type="bedrock_kb", uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local_id = store.add_source(
            name="notes", source_type="local_file", uri="/tmp/notes.md", properties={})
        other_id = store.add_source(
            name="more", source_type="local_file", uri="/tmp/more.md", properties={})
        shared = store.add_item(title="shared", content="shared text", item_type="note",
                                source_id=local_id)
        lonely = store.add_item(title="lonely", content="lonely text", item_type="note",
                                source_id=local_id)
        now = "2026-09-24T00:00:00"
        db = store.db
        # The planted rows first, so an unfiltered "first other holder" would be
        # the live source.
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-shared-kb', ?, ?, ?)", (shared, bedrock_id, now))
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-lonely-kb', ?, ?, ?)", (lonely, bedrock_id, now))
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-shared-other', ?, ?, ?)", (shared, other_id, now))
        db.commit()

        store.delete_source_cascade(local_id)
        assert db.execute(
            "SELECT source_id FROM items WHERE id = ?", (shared,)).fetchone()["source_id"] == other_id
        assert db.execute("SELECT COUNT(*) FROM items WHERE id = ?", (lonely,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM source_locations WHERE item_id = ?", (lonely,)
        ).fetchone()[0] == 0

        # The item-level path (a source dropping its copy of a document).
        lonely2 = store.add_item(title="lonely2", content="lonely text 2", item_type="note",
                                 source_id=other_id)
        db.execute(
            "INSERT INTO source_locations (id, item_id, source_id, created_at) "
            "VALUES ('loc-lonely2-kb', ?, ?, ?)", (lonely2, bedrock_id, now))
        db.commit()
        store.delete_items_batch([lonely2], owner_source_id=other_id)
        assert db.execute("SELECT COUNT(*) FROM items WHERE id = ?", (lonely2,)).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM source_locations WHERE item_id = ?", (lonely2,)
        ).fetchone()[0] == 0

        bundle = store.export_all()
        assert {i["id"] for i in bundle["items"]} == {shared}
        # The deleted owner is gone and the live source is left out: one source.
        assert [s["id"] for s in bundle["sources"]] == [other_id]
        clean = KnowledgeStore(str(tmp_path / "clean.db"))
        try:
            result = clean.import_bundle(bundle)
        finally:
            clean.close()
        assert result["items_imported"] == 1
        assert result["items_refused"] == 0

    @pytest.mark.asyncio
    async def test_ingest_text_under_a_bedrock_source_is_refused_before_the_pipeline_runs(
        self, store
    ):
        """The agent's ingest-text route takes any existing source id, and a
        bedrock id is readable off the sources list. The refusal is a coded
        409 at the route, before the body is read, so the caller learns why
        instead of getting the store's refusal as a 500 after extraction."""
        bedrock_id = store.add_source(
            name="kb", source_type="bedrock_kb", uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        pipeline = MagicMock()
        pipeline.ingest_file = AsyncMock(return_value="job-1")
        app = _make_app(store, pipeline=pipeline)
        app.router.add_post("/api/knowledge/sources/{id}/ingest-text", ingest_text)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                f"/api/knowledge/sources/{bedrock_id}/ingest-text",
                json={"text": "local text", "name": "planted"},
            )
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "live_source_holds_no_items"
        pipeline.ingest_file.assert_not_called()
        assert self._items_under(store, bedrock_id) == 0

    @staticmethod
    def _item(item_id: str, source_id: str) -> dict:
        return {
            "id": item_id, "title": "planted", "content": "local text",
            "item_type": "note", "source_id": source_id, "chunk_index": 0,
            "namespace": "default", "summary": None, "tags": "[]",
            "embedding": None, "embedding_sig": None, "status": "active",
        }

    @staticmethod
    def _items_under(store, source_id: str) -> int:
        return store.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id = ?", (source_id,)
        ).fetchone()[0]

    @pytest.mark.asyncio
    async def test_an_item_naming_a_registered_bedrock_source_is_refused(self, store):
        """A bedrock source holds no items -- it is queried live. Its id is
        readable by any authenticated caller off the sources list, so a bundle
        that plants an item under it must not land local content under a
        live-only source. Found in review."""
        bedrock_id = store.add_source(
            name="kb", source_type="bedrock_kb", uri=self._ROW["uri"],
            properties=json.loads(self._ROW["properties"]),
        )
        local = {
            "id": "src-local-1", "name": "notes", "source_type": "local_file",
            "uri": "/tmp/notes.md", "properties": "{}", "sync_status": "active",
        }
        bundle = self._bundle(local)
        bundle["items"] = [
            self._item("item-planted", bedrock_id),
            self._item("item-fine", "src-local-1"),
        ]
        bundle["source_locations"] = [{
            "id": "loc-1", "item_id": "item-planted", "source_id": bedrock_id,
            "chunk_range": None, "section_title": None, "anchor": None,
        }]
        app = self._app(store)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/knowledge/import", json=bundle,
                headers={"X-Test-User": "slack-guest"},
            )
            assert resp.status == 200, await resp.text()
            result = await resp.json()
        # The planted item is refused and counted; the rest of the bundle lands.
        assert result["items_refused"] == 1
        assert result["items_imported"] == 1
        assert self._items_under(store, bedrock_id) == 0
        assert self._items_under(store, "src-local-1") == 1
        assert store.db.execute(
            "SELECT COUNT(*) FROM source_locations WHERE source_id = ?", (bedrock_id,)
        ).fetchone()[0] == 0

    def test_items_riding_on_a_refused_bundle_row_are_refused_with_it(self, store):
        """The refused row's dependents -- items, their locations and mentions,
        relations they evidence -- are dropped with the row instead of tripping
        the FK and rolling the whole bundle back."""
        bundle = self._bundle(self._ROW)
        bundle["items"] = [self._item("item-planted", self._ROW["id"])]
        bundle["entities"] = [{
            "id": "ent-1", "name": "Thing", "entity_type": "concept",
            "description": None, "aliases": "[]",
        }]
        bundle["relations"] = [{
            "id": "rel-1", "source_id": "ent-1", "target_id": "ent-1",
            "relation_type": "self", "description": None, "weight": 1.0,
            "source_item_id": "item-planted",
        }]
        bundle["source_locations"] = [{
            "id": "loc-1", "item_id": "item-planted", "source_id": self._ROW["id"],
            "chunk_range": None, "section_title": None, "anchor": None,
        }]
        bundle["mentions"] = [{"item_id": "item-planted", "entity_id": "ent-1", "context": None}]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 1
        assert result["items_imported"] == 0
        assert result["relations_rebuilt"] == 0
        # The bundle still committed: the entity that depended on nothing landed.
        assert result["entities_created"] == 1
        assert self._bedrock_rows(store) == 0
        assert self._items_under(store, self._ROW["id"]) == 0
        assert store.db.execute("SELECT COUNT(*) FROM mentions").fetchone()[0] == 0

    def test_numeric_ids_in_a_crafted_bundle_are_refused_the_same_way(self, store):
        """The validator does not type ids, so a crafted bundle may carry a
        number where the exporter wrote a string; the membership checks compare
        as text on both sides, else the item slips past to the FK and rolls the
        whole bundle back. Found in review."""
        row = {**self._ROW, "id": 4242}
        bundle = self._bundle(row)
        bundle["items"] = [self._item("item-planted", 4242)]
        bundle["source_locations"] = [{
            "id": "loc-1", "item_id": "item-planted", "source_id": 4242,
            "chunk_range": None, "section_title": None, "anchor": None,
        }]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 1
        assert result["items_imported"] == 0
        assert self._bedrock_rows(store) == 0

    def test_a_refused_item_named_none_drops_no_id_less_dependents(self, store):
        """``str(None)`` is the text "None", which a bundle can also spell as
        an item id. A refused item with no id (or one literally called "None")
        must not take down a relation that names no evidencing item: that
        relation is evidence-free, not evidence of a refused item, and was
        imported before the refusal code existed. Found in review."""
        bundle = self._bundle(self._ROW)
        id_less = {**self._item("unused", self._ROW["id"])}
        del id_less["id"]
        bundle["items"] = [id_less, self._item("None", self._ROW["id"])]
        bundle["entities"] = [{
            "id": "ent-1", "name": "Thing", "entity_type": "concept",
            "description": None, "aliases": "[]",
        }]
        bundle["relations"] = [
            {
                "id": "rel-free", "source_id": "ent-1", "target_id": "ent-1",
                "relation_type": "self", "description": None, "weight": 1.0,
                "source_item_id": None,
            },
            {
                "id": "rel-planted", "source_id": "ent-1", "target_id": "ent-1",
                "relation_type": "self", "description": None, "weight": 1.0,
                "source_item_id": "None",
            },
        ]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 2
        assert result["items_imported"] == 0
        # The evidence-free relation landed; the one evidenced by the refused
        # item called "None" did not.
        assert result["relations_rebuilt"] == 1
        rows = store.db.execute("SELECT id FROM entity_relations").fetchall()
        assert [row[0] for row in rows] == ["rel-free"]

    def test_an_id_less_refused_row_does_not_refuse_a_source_called_none(self, store):
        """The source-side twin: a refused ``bedrock_kb`` row with no id must
        not put the text "None" in the refused set, or a LOCAL source that a
        bundle happens to call "None" loses every item under it. Found in
        review."""
        id_less = {k: v for k, v in self._ROW.items() if k != "id"}
        local = {
            "id": "None", "name": "notes", "source_type": "local_file",
            "uri": "/tmp/notes.md", "properties": "{}", "sync_status": "active",
        }
        bundle = self._bundle(id_less, local)
        bundle["items"] = [self._item("item-fine", "None")]
        result = store.import_bundle(bundle)
        assert result["sources_refused"] == 1
        assert result["items_refused"] == 0
        assert result["items_imported"] == 1
        assert self._items_under(store, "None") == 1
        assert self._bedrock_rows(store) == 0
