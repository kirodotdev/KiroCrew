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


class TestFolderPickerAvailable:
    def test_available_on_mac_local(self, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
        assert _folder_picker_available(_fake_request(local_only=True)) is True

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
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge.subprocess.run",
            lambda *a, **k: completed,
        )
        assert _run_folder_dialog() == "/home/user/notes"

    def test_cancel_returns_none(self, monkeypatch):
        completed = MagicMock(returncode=1, stdout="")
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge.subprocess.run",
            lambda *a, **k: completed,
        )
        assert _run_folder_dialog() is None

    def test_launch_failure_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.knowledge.subprocess.run", boom,
        )
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
    async def test_returns_picked_path(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "darwin")
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
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.get("/api/knowledge/config")
            assert (await resp.json())["folder_picker"] is True

    @pytest.mark.asyncio
    async def test_reports_false_off_mac(self, store, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.handlers.knowledge.sys.platform", "win32")
        async with TestClient(TestServer(_make_pick_app(store, local_only=True))) as client:
            resp = await client.get("/api/knowledge/config")
            assert (await resp.json())["folder_picker"] is False


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
