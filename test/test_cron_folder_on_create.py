"""Filing a cron into a Schedule-page folder at CREATE time.

Covers the three creation surfaces that previously could not carry a folder:
the MCP ``cron_add``/``cron_update`` tools (``folder`` argument), the CLI
(``kirocrew cron add --folder``), and the app manifest (``CronEntry.folder``),
plus the shared read-only resolver they build on.

The resolver contract under test: ``cron_folders.json`` is OWNED by the
dashboard (its state rewrites the file wholesale), so non-dashboard surfaces
resolve read-only — only the MCP path may create a missing folder, and it does
so through ``POST /api/cron-folders`` (the dashboard's own endpoint) rather
than by appending to the file.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.apps.manifest import CronEntry
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import load_cron_folders, lookup_cron_folder_id


def _write_folders(entries) -> None:
    (config_dir() / "cron_folders.json").write_text(json.dumps(entries), encoding="utf-8")


@pytest.fixture()
def folders_on_disk(tmp_path, monkeypatch):
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    config_dir().mkdir(parents=True, exist_ok=True)
    _write_folders(
        [
            {"id": "aaaa1111", "name": "Veille", "order": 0},
            {"id": "bbbb2222", "name": "Apps", "order": 1},
        ]
    )
    return home


# ---------------------------------------------------------------------------
# Read-only lookup (shared by CLI and app-cron registration)
# ---------------------------------------------------------------------------


class TestLookupCronFolderId:
    def test_empty_ref_is_ungrouped_not_an_error(self, folders_on_disk):
        assert lookup_cron_folder_id("") == ("", None, False)
        assert lookup_cron_folder_id("   ") == ("", None, False)

    def test_resolves_exact_id(self, folders_on_disk):
        assert lookup_cron_folder_id("aaaa1111") == ("aaaa1111", None, False)

    def test_resolves_name_case_insensitively(self, folders_on_disk):
        assert lookup_cron_folder_id("veille") == ("aaaa1111", None, False)
        assert lookup_cron_folder_id("VEILLE") == ("aaaa1111", None, False)

    def test_unknown_ref_is_an_error_never_a_create(self, folders_on_disk):
        found = lookup_cron_folder_id("Nope")
        assert found.folder_id == ""
        assert found.error is not None and "not found" in found.error
        # Read-only: the file is untouched.
        assert len(load_cron_folders()) == 2

    def test_unknown_ref_is_flagged_missing_so_a_creator_can_act(self, folders_on_disk):
        # The flag is the ONLY signal a caller with a create path may key off;
        # without it the create leg would have to match on the message prose.
        assert lookup_cron_folder_id("Nope").missing is True

    def test_ambiguous_name_is_refused(self, folders_on_disk):
        _write_folders(
            [
                {"id": "aaaa1111", "name": "Veille", "order": 0},
                {"id": "cccc3333", "name": "veille", "order": 1},
            ]
        )
        found = lookup_cron_folder_id("Veille")
        assert found.folder_id == ""
        assert found.error is not None and "pass the folder id" in found.error

    def test_ambiguous_name_is_not_missing_so_it_never_becomes_a_create(self, folders_on_disk):
        # An ambiguity must stay a refusal for every caller: creating here would
        # add a THIRD folder sharing the name and make the next lookup worse.
        _write_folders(
            [
                {"id": "aaaa1111", "name": "Veille", "order": 0},
                {"id": "cccc3333", "name": "veille", "order": 1},
            ]
        )
        assert lookup_cron_folder_id("Veille").missing is False

    def test_malformed_file_degrades_to_no_folders(self, folders_on_disk):
        (config_dir() / "cron_folders.json").write_text("{}", encoding="utf-8")
        assert load_cron_folders() == []
        found = lookup_cron_folder_id("Veille")
        assert found.folder_id == "" and found.error is not None

    def test_unreadable_store_is_not_missing_so_it_never_becomes_a_create(self, folders_on_disk):
        # The folder set is UNKNOWN, not empty: creating here would add a folder
        # the store may already hold, and the dashboard's next wholesale save
        # decides which version survives.
        (config_dir() / "cron_folders.json").write_text("{}", encoding="utf-8")
        found = lookup_cron_folder_id("Veille")
        assert found.missing is False
        assert found.error is not None and "unreadable" in found.error

    def test_unparseable_store_is_not_missing_either(self, folders_on_disk):
        (config_dir() / "cron_folders.json").write_text("not json at all", encoding="utf-8")
        assert lookup_cron_folder_id("Veille").missing is False

    def test_absent_file_is_a_readable_empty_store_so_a_miss_is_creatable(
        self, tmp_path, monkeypatch
    ):
        # No file at all is genuinely "no folders yet", unlike a corrupt one.
        home = tmp_path / "empty-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        assert lookup_cron_folder_id("Veille").missing is True

    def test_missing_file_degrades_to_no_folders(self, tmp_path, monkeypatch):
        home = tmp_path / "empty-home"
        home.mkdir()
        monkeypatch.setenv("KIROCREW_HOME", str(home))
        assert load_cron_folders() == []

    def test_malformed_entries_are_skipped(self, folders_on_disk):
        _write_folders(
            [
                {"id": "aaaa1111", "name": "Veille", "order": 0},
                {"id": "", "name": "NoId", "order": 1},
                "not-a-dict",
                {"name": "NoIdKey", "order": 2},
            ]
        )
        assert [f["id"] for f in load_cron_folders()] == ["aaaa1111"]


# ---------------------------------------------------------------------------
# MCP resolver (resolve locally, create through the dashboard endpoint)
# ---------------------------------------------------------------------------


class TestMcpResolveCronFolder:
    def test_existing_name_resolves_without_http(self, folders_on_disk, monkeypatch):
        from kiro_crew import mcp_cron

        def _boom(*a, **k):  # the create endpoint must not be hit
            raise AssertionError("POST issued for an existing folder")

        monkeypatch.setattr(mcp_cron, "_post", _boom)
        assert mcp_cron._resolve_cron_folder("Veille", session_key="") == ("aaaa1111", None)
        assert mcp_cron._resolve_cron_folder("bbbb2222", session_key="") == ("bbbb2222", None)
        assert mcp_cron._resolve_cron_folder("", session_key="") == ("", None)

    def test_missing_name_is_created_via_dashboard_endpoint(self, folders_on_disk, monkeypatch):
        from kiro_crew import mcp_cron

        calls: list[tuple[str, dict]] = []

        def _fake_post(path, body=None, **kwargs):
            calls.append((path, body or {}))
            return {"id": "dddd4444", "name": body["name"], "order": 2}

        monkeypatch.setattr(mcp_cron, "_post", _fake_post)
        fid, err = mcp_cron._resolve_cron_folder("Fresh", session_key="dashboard:x")
        assert (fid, err) == ("dddd4444", None)
        assert calls == [("/api/cron-folders", {"name": "Fresh"})]

    def test_create_failure_is_reported_not_swallowed(self, folders_on_disk, monkeypatch):
        from kiro_crew import mcp_cron

        monkeypatch.setattr(mcp_cron, "_post", lambda *a, **k: {"error": "gateway unreachable"})
        fid, err = mcp_cron._resolve_cron_folder("Fresh", session_key="")
        assert fid == ""
        assert err is not None and "gateway unreachable" in err

    def test_id_shaped_miss_is_refused_not_created(self, folders_on_disk, monkeypatch):
        from kiro_crew import mcp_cron

        def _boom(*a, **k):
            raise AssertionError("POST issued for an id-shaped reference")

        monkeypatch.setattr(mcp_cron, "_post", _boom)
        fid, err = mcp_cron._resolve_cron_folder("deadbeef", session_key="")
        assert fid == ""
        assert err is not None and "minted" in err

    def test_ambiguous_name_is_refused(self, folders_on_disk, monkeypatch):
        from kiro_crew import mcp_cron

        _write_folders(
            [
                {"id": "aaaa1111", "name": "Veille", "order": 0},
                {"id": "cccc3333", "name": "veille", "order": 1},
            ]
        )
        fid, err = mcp_cron._resolve_cron_folder("Veille", session_key="")
        assert fid == ""
        assert err is not None and "pass the folder id" in err


# ---------------------------------------------------------------------------
# Manifest field
# ---------------------------------------------------------------------------


class TestCronEntryFolder:
    def test_round_trips_through_to_dict_from_dict(self):
        entry = CronEntry(name="sweep", every=300, agent="bg", message="go", folder="Radar")
        d = entry.to_dict()
        assert d["folder"] == "Radar"
        assert CronEntry.from_dict(d).folder == "Radar"

    def test_defaults_to_ungrouped_and_is_omitted_when_empty(self):
        entry = CronEntry(name="sweep", every=300)
        assert entry.folder == ""
        assert "folder" not in entry.to_dict()

    def test_non_string_folder_degrades_to_empty(self):
        entry = CronEntry.from_dict({"name": "sweep", "every": 300, "folder": 42})
        assert entry.folder == ""

    def test_defs_from_manifest_carry_folder(self):
        from kiro_crew.apps.bridges import _cron_defs_from_manifest
        from kiro_crew.apps.manifest import AppManifest

        manifest = AppManifest.from_dict(
            {
                "name": "radar",
                "version": "1.0.0",
                "displayName": "Radar",
                "description": "x",
                "author": "t",
                "crons": [
                    {
                        "name": "sweep",
                        "every": 300,
                        "agent": "bg",
                        "message": "go",
                        "folder": "Radar",
                    },
                ],
            }
        )
        defs, registered = _cron_defs_from_manifest("radar", manifest)
        assert registered == ["radar/sweep"]
        assert defs[0]["folder"] == "Radar"


# ---------------------------------------------------------------------------
# App-cron registration (resolve existing, degrade to ungrouped on a miss)
# ---------------------------------------------------------------------------


class TestRegistrationFilesJobIntoFolder:
    def _register(self, home, defs):
        """Persist ``defs`` as an installed app's cron defs and register them."""
        from kiro_crew.apps.bridges import _app_crons_path, register_app_crons_with_service
        from kiro_crew.cron import CronService

        path = _app_crons_path("test-app")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(defs), encoding="utf-8")
        # A minimal installed-app root so the trust gate resolves the app.
        app_root = home / "apps" / "test-app"
        app_root.mkdir(parents=True, exist_ok=True)
        (app_root / "app.json").write_text(
            json.dumps(
                {
                    "name": "test-app",
                    "version": "1.0.0",
                    "displayName": "Test App",
                    "description": "x",
                    "author": "t",
                }
            ),
            encoding="utf-8",
        )

        async def _go():
            svc = CronService(base_dir=home / "crons")
            await svc.start()
            try:
                await register_app_crons_with_service("test-app", svc)
                return svc.list_jobs(include_disabled=True)
            finally:
                await svc.stop()

        return asyncio.run(_go())

    def test_existing_folder_name_is_resolved_to_its_id(self, folders_on_disk, monkeypatch):
        monkeypatch.setattr("kiro_crew.apps.bridges._registration_denied", lambda *a, **k: False)
        jobs = self._register(
            folders_on_disk,
            [
                {
                    "name": "test-app/sweep",
                    "every": 300,
                    "agent": "bg",
                    "message": "go",
                    "app": "test-app",
                    "folder": "Veille",
                }
            ],
        )
        assert [j.folder_id for j in jobs if j.name == "test-app/sweep"] == ["aaaa1111"]

    def test_unknown_folder_registers_ungrouped(self, folders_on_disk, monkeypatch):
        monkeypatch.setattr("kiro_crew.apps.bridges._registration_denied", lambda *a, **k: False)
        jobs = self._register(
            folders_on_disk,
            [
                {
                    "name": "test-app/sweep",
                    "every": 300,
                    "agent": "bg",
                    "message": "go",
                    "app": "test-app",
                    "folder": "Missing",
                }
            ],
        )
        # The job matters more than its grouping: registered, unfiled.
        assert [j.folder_id for j in jobs if j.name == "test-app/sweep"] == [""]


# ---------------------------------------------------------------------------
# Schemas and CLI flag
# ---------------------------------------------------------------------------


class TestSurfacesAcceptFolder:
    def test_cron_add_schema_accepts_folder(self):
        from kiro_crew.validation import CRON_ADD_SCHEMA, validate_tool_args

        cleaned = validate_tool_args(
            {"name": "j", "message": "m", "every": 300, "folder": "Veille"}, CRON_ADD_SCHEMA
        )
        assert cleaned["folder"] == "Veille"

    def test_cron_update_schema_accepts_folder(self):
        from kiro_crew.validation import MCP_CRON_SCHEMAS, validate_tool_args

        cleaned = validate_tool_args(
            {"job_id": "abcd1234", "folder": ""}, MCP_CRON_SCHEMAS["cron_update"]
        )
        assert cleaned["folder"] == ""

    def test_cli_add_files_job_into_existing_folder(self, folders_on_disk, capsys):
        import argparse

        from kiro_crew.cli_commands import _cron_dispatch
        from kiro_crew.cron import CronService

        _cron_dispatch(
            argparse.Namespace(
                cron_action="add",
                name="veille-job",
                message="go",
                every=300,
                cron_expr=None,
                channel=None,
                approval_mode="",
                agent="",
                silent=False,
                folder="Veille",
            )
        )
        svc = CronService(base_dir=config_dir())
        assert [j.folder_id for j in svc.list_jobs() if j.name == "veille-job"] == ["aaaa1111"]

    def test_cli_add_refuses_unknown_folder_without_creating_a_job(self, folders_on_disk, capsys):
        import argparse

        from kiro_crew.cli_commands import _cron_dispatch
        from kiro_crew.cron import CronService

        with pytest.raises(SystemExit):
            _cron_dispatch(
                argparse.Namespace(
                    cron_action="add",
                    name="orphan",
                    message="go",
                    every=300,
                    cron_expr=None,
                    channel=None,
                    approval_mode="",
                    agent="",
                    silent=False,
                    folder="Missing",
                )
            )
        assert "not found" in capsys.readouterr().err
        svc = CronService(base_dir=config_dir())
        assert not any(j.name == "orphan" for j in svc.list_jobs(include_disabled=True))

    def test_mcp_cron_add_tool_declares_folder(self):
        from kiro_crew.mcp_cron import _list_tools

        by_name = {t["name"]: t for t in _list_tools()}
        assert "folder" in by_name["cron_add"]["inputSchema"]["properties"]
        assert "folder" in by_name["cron_update"]["inputSchema"]["properties"]
