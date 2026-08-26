"""Tests for Design Tweak project registry, dev-server, /thread, and folder-picker routes.

Covers lines ~3230-3790 of server.py: the /projects listing and registration routes,
/dev-server/start and /dev-server/stop, /thread, /pick-folder, and main().
"""

from __future__ import annotations

import io
import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import server


@pytest.fixture()
def isolated_queue(tmp_path, monkeypatch):
    data = (tmp_path / 'data').resolve()
    queue = data / 'queue'
    handled = data / 'handled'
    queue.mkdir(parents=True)
    handled.mkdir(parents=True)
    monkeypatch.setattr(server, 'DATA_DIR', data)
    monkeypatch.setattr(server, 'QUEUE_DIR', queue)
    monkeypatch.setattr(server, 'HANDLED_DIR', handled)
    monkeypatch.setattr(server, '_ROOT', '')
    monkeypatch.setattr(server, '_TARGET', '')
    monkeypatch.setitem(server._CFG, 'projects', [])
    return queue


# ---------------------------------------------------------------------------
# Helpers — build a Handler without opening a real socket
# ---------------------------------------------------------------------------

class _Recorded:
    """Captures the _json response without writing to a socket."""

    def __init__(self):
        self.code: int | None = None
        self.payload: Any = None


def _make_handler(
    method: str,
    path: str,
    body: dict | None = None,
) -> tuple[server.Handler, _Recorded]:
    """Construct a Handler via __new__ (no socket) and wire up routing."""
    h = server.Handler.__new__(server.Handler)
    h.path = path
    h.headers = {"Content-Length": "0", "X-KiroCrew-Proxy": ""}
    rec = _Recorded()

    raw = b""
    if body is not None:
        raw = json.dumps(body).encode()
        h.headers = {
            "Content-Length": str(len(raw)),
            "X-KiroCrew-Proxy": "",
        }
    h.rfile = io.BytesIO(raw)
    h._cached_body = raw

    # Override _json to capture instead of writing to wfile
    def _capture_json(code, payload):
        rec.code = code
        rec.payload = payload

    h._json = _capture_json  # type: ignore[assignment]

    # Override _authorized to bypass HMAC verification in tests
    h._authorized = lambda *a, **kw: True  # type: ignore[assignment]

    # Stub send_response/send_header/end_headers/wfile for _send_raw
    h.send_response = lambda *a: None  # type: ignore[assignment]
    h.send_header = lambda *a: None  # type: ignore[assignment]
    h.end_headers = lambda: None  # type: ignore[assignment]
    h.wfile = io.BytesIO()

    return h, rec


def _get(path: str) -> tuple[server.Handler, _Recorded]:
    return _make_handler("GET", path)


def _post(path: str, body: dict | None = None) -> tuple[server.Handler, _Recorded]:
    return _make_handler("POST", path, body)


# ---------------------------------------------------------------------------
# /projects (GET) — project listing
# ---------------------------------------------------------------------------

class TestProjectsList:
    """The /projects GET endpoint returns the registry with live classification."""

    def test_empty_projects_returns_list(self, isolated_queue, monkeypatch):
        """If no projects are registered, production shows an empty panel instead
        of crashing on a missing key."""
        monkeypatch.setattr(server, '_CFG', {'projects': [], 'activeId': ''})
        h, rec = _get("/projects")
        h._h_projects_list()
        assert rec.code == 200
        assert rec.payload["projects"] == []
        assert rec.payload["activeId"] == ""
        assert rec.payload["serving"] is False

    def test_project_with_valid_root(self, isolated_queue, tmp_path, monkeypatch):
        """A registered project with an existing directory appears in the listing
        with classification fields; failure here means the panel never loads."""
        proj_dir = tmp_path / "my-app"
        proj_dir.mkdir()
        projects = [{"id": "abc123", "path": str(proj_dir), "name": "my-app"}]
        monkeypatch.setattr(server, '_CFG', {'projects': projects, 'activeId': ''})
        # Stub helpers that reach into FS/network
        monkeypatch.setattr(server, '_classify_project', lambda root: {
            "needsDevServer": False, "devCommand": "", "unbundledEntry": "", "hasEntry": True
        })
        monkeypatch.setattr(server, '_dev_proc_alive', lambda pid: False)
        monkeypatch.setattr(server, '_DEV_PROCS', {})
        h, rec = _get("/projects")
        h._h_projects_list()
        assert rec.code == 200
        assert len(rec.payload["projects"]) == 1
        assert rec.payload["projects"][0]["id"] == "abc123"
        assert rec.payload["projects"][0]["hasEntry"] is True

    def test_project_with_invalid_root_gets_fallback(self, isolated_queue, monkeypatch):
        """If the project folder was deleted after registration, the listing returns
        fallback classification rather than 500-ing — a broken path must degrade
        gracefully or the entire panel is bricked."""
        projects = [{"id": "gone1", "path": "/nonexistent/path", "name": "gone"}]
        monkeypatch.setattr(server, '_CFG', {'projects': projects, 'activeId': ''})
        monkeypatch.setattr(server, '_dev_proc_alive', lambda pid: False)
        monkeypatch.setattr(server, '_DEV_PROCS', {})
        h, rec = _get("/projects")
        h._h_projects_list()
        assert rec.code == 200
        row = rec.payload["projects"][0]
        assert row["needsDevServer"] is False
        assert row["hasEntry"] is False

    def test_active_project_serving_flag(self, isolated_queue, tmp_path, monkeypatch):
        """When _ROOT matches the active project's path, serving=True tells the
        frontend to show the 'live' badge."""
        proj_dir = tmp_path / "live-app"
        proj_dir.mkdir()
        real_path = str(proj_dir.resolve())
        projects = [{"id": "live1", "path": real_path, "name": "live-app"}]
        monkeypatch.setattr(server, '_CFG', {'projects': projects, 'activeId': 'live1'})
        monkeypatch.setattr(server, '_ROOT', real_path)
        monkeypatch.setattr(server, '_classify_project', lambda root: {
            "needsDevServer": False, "devCommand": "", "unbundledEntry": "", "hasEntry": True
        })
        monkeypatch.setattr(server, '_dev_proc_alive', lambda pid: False)
        monkeypatch.setattr(server, '_DEV_PROCS', {})
        h, rec = _get("/projects")
        h._h_projects_list()
        assert rec.code == 200
        assert rec.payload["serving"] is True


# ---------------------------------------------------------------------------
# /projects (POST) — register a project
# ---------------------------------------------------------------------------

class TestProjectsAdd:
    """POST /projects registers a folder, with _valid_root as the gate."""

    def test_register_valid_folder(self, isolated_queue, tmp_path, monkeypatch):
        """A real directory is accepted and persisted; failure means the user
        cannot add projects at all."""
        proj_dir = tmp_path / "webapp"
        proj_dir.mkdir()
        monkeypatch.setattr(server, '_detect_dev_servers', lambda root: [])
        h, rec = _post("/projects", {"path": str(proj_dir)})
        h._h_projects_add()
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert rec.payload["project"]["path"] == str(proj_dir.resolve())
        # Was also appended to _CFG
        assert len(server._CFG["projects"]) == 1

    def test_refuses_sensitive_path_ssh(self, isolated_queue, monkeypatch):
        """_valid_root refuses paths containing .ssh; without this, the preview
        would serve private keys over HTTP."""
        h, rec = _post("/projects", {"path": "/home/user/.ssh"})
        h._h_projects_add()
        assert rec.code == 400
        assert "not a readable folder" in rec.payload["error"]

    def test_refuses_sensitive_path_aws(self, isolated_queue, monkeypatch):
        """_valid_root refuses paths containing .aws; without this, credentials
        become fetchable via the preview server."""
        h, rec = _post("/projects", {"path": "/home/user/.aws"})
        h._h_projects_add()
        assert rec.code == 400
        assert "not a readable folder" in rec.payload["error"]

    def test_refuses_non_directory(self, isolated_queue, tmp_path, monkeypatch):
        """_valid_root rejects a file path; without this guard a file could be
        registered and the listing would crash on iteration."""
        f = tmp_path / "file.txt"
        f.write_text("hi")
        h, rec = _post("/projects", {"path": str(f)})
        h._h_projects_add()
        assert rec.code == 400
        assert "not a readable folder" in rec.payload["error"]

    def test_refuses_path_containing_credential_store(
        self, isolated_queue, tmp_path, monkeypatch
    ):
        """A path that CONTAINS a sensitive subtree (e.g. home directory that
        has .ssh inside it) is also refused — otherwise the preview server
        exposes nested credentials."""
        # Monkeypatch path_contains_sensitive to return True for this path
        monkeypatch.setattr(
            server, 'path_contains_sensitive', lambda p: True
        )
        proj_dir = tmp_path / "home-folder"
        proj_dir.mkdir()
        h, rec = _post("/projects", {"path": str(proj_dir)})
        h._h_projects_add()
        assert rec.code == 400
        assert "not a readable folder" in rec.payload["error"]

    def test_duplicate_project_returns_existing(self, isolated_queue, tmp_path, monkeypatch):
        """Registering the same folder twice returns the existing entry rather
        than creating a duplicate; without this the registry grows unboundedly."""
        proj_dir = tmp_path / "dup"
        proj_dir.mkdir()
        existing = {"id": "dup1", "path": str(proj_dir.resolve()), "name": "dup"}
        monkeypatch.setattr(server, '_CFG', {'projects': [existing], 'activeId': ''})
        monkeypatch.setattr(server, '_detect_dev_servers', lambda root: [])
        h, rec = _post("/projects", {"path": str(proj_dir)})
        h._h_projects_add()
        assert rec.code == 200
        assert rec.payload["existing"] is True
        assert rec.payload["project"]["id"] == "dup1"

    def test_invalid_preview_url_rejected(self, isolated_queue, tmp_path, monkeypatch):
        """A non-loopback previewUrl is rejected; without this an attacker could
        SSRF to internal services via the proxy."""
        proj_dir = tmp_path / "app"
        proj_dir.mkdir()
        h, rec = _post("/projects", {
            "path": str(proj_dir),
            "previewUrl": "http://evil.com:3000"
        })
        h._h_projects_add()
        assert rec.code == 400
        assert "localhost" in rec.payload["error"] or "127.0.0.1" in rec.payload["error"]


# ---------------------------------------------------------------------------
# /projects/select, /projects/remove, /projects/preview-url
# ---------------------------------------------------------------------------

class TestProjectsSelectRemovePreviewUrl:
    """Select, remove, and preview-url mutation routes."""

    def test_select_sets_active(self, isolated_queue, tmp_path, monkeypatch):
        """Selecting a project makes it active and sets _ROOT; failure means
        the preview panel never connects to a project."""
        proj_dir = tmp_path / "sel-app"
        proj_dir.mkdir()
        proj = {"id": "sel1", "path": str(proj_dir.resolve()), "name": "sel-app"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_save_cfg', lambda c: None)
        h, rec = _post("/projects/select", {"id": "sel1"})
        h._h_projects_select()
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert server._CFG["activeId"] == "sel1"

    def test_select_nonexistent_returns_404(self, isolated_queue, monkeypatch):
        """Selecting a missing project id returns 404 rather than crashing."""
        monkeypatch.setattr(server, '_CFG', {'projects': [], 'activeId': ''})
        h, rec = _post("/projects/select", {"id": "nope"})
        h._h_projects_select()
        assert rec.code == 404

    def test_remove_drops_project(self, isolated_queue, tmp_path, monkeypatch):
        """Removing a project drops it from the registry; failure means projects
        cannot be unregistered and the list grows forever."""
        proj = {"id": "rm1", "path": "/tmp/rm", "name": "rm"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_save_cfg', lambda c: None)
        h, rec = _post("/projects/remove", {"id": "rm1"})
        h._h_projects_remove()
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert len(server._CFG["projects"]) == 0

    def test_remove_active_clears_root(self, isolated_queue, tmp_path, monkeypatch):
        """Removing the active project also clears _ROOT; otherwise the server
        keeps serving a deregistered folder."""
        proj = {"id": "act1", "path": "/tmp/act", "name": "act"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': 'act1'})
        monkeypatch.setattr(server, '_ROOT', '/tmp/act')
        monkeypatch.setattr(server, '_save_cfg', lambda c: None)
        h, rec = _post("/projects/remove", {"id": "act1"})
        h._h_projects_remove()
        assert rec.code == 200
        assert server._CFG["activeId"] == ""
        assert server._ROOT == ""

    def test_preview_url_rejects_non_loopback(self, isolated_queue, monkeypatch):
        """Setting a non-loopback preview URL is rejected; without this the
        proxy could connect to arbitrary hosts (SSRF)."""
        proj = {"id": "pu1", "path": "/tmp/pu", "name": "pu"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        h, rec = _post("/projects/preview-url", {
            "id": "pu1",
            "previewUrl": "https://attacker.com"
        })
        h._h_projects_preview_url()
        assert rec.code == 400
        assert "localhost" in rec.payload["error"] or "127.0.0.1" in rec.payload["error"]

    def test_preview_url_accepts_localhost(self, isolated_queue, monkeypatch):
        """A valid loopback URL is persisted; failure means framework projects
        cannot use their own dev server for HMR."""
        proj = {"id": "pu2", "path": "/tmp/pu2", "name": "pu2"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_save_cfg', lambda c: None)
        h, rec = _post("/projects/preview-url", {
            "id": "pu2",
            "previewUrl": "http://localhost:5173"
        })
        h._h_projects_preview_url()
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert proj["previewUrl"] == "http://localhost:5173"

    def test_preview_url_clear(self, isolated_queue, monkeypatch):
        """Sending empty previewUrl clears it, reverting to static serving;
        failure means there is no way to disconnect a stopped dev server."""
        proj = {"id": "pu3", "path": "/tmp/pu3", "name": "pu3",
                "previewUrl": "http://localhost:3000"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_save_cfg', lambda c: None)
        h, rec = _post("/projects/preview-url", {"id": "pu3", "previewUrl": ""})
        h._h_projects_preview_url()
        assert rec.code == 200
        assert "previewUrl" not in proj


# ---------------------------------------------------------------------------
# /dev-server/start and /dev-server/stop
# ---------------------------------------------------------------------------

class TestDevServer:
    """Dev-server lifecycle routes must never start real processes in tests."""

    def test_start_missing_project_returns_404(self, isolated_queue, monkeypatch):
        """Starting a dev server for an unregistered project returns 404;
        failure here means arbitrary folder paths could be executed."""
        monkeypatch.setattr(server, '_CFG', {'projects': [], 'activeId': ''})
        h, rec = _get("/dev-server/start?id=nope")
        h._h_dev_server_start({"id": ["nope"]})
        assert rec.code == 404

    def test_start_unreadable_folder_returns_400(self, isolated_queue, monkeypatch):
        """If the project folder was deleted, start returns 400 rather than
        attempting to spawn a process in a missing directory."""
        proj = {"id": "ds1", "path": "/no/such/folder", "name": "ds"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        h, rec = _get("/dev-server/start?id=ds1")
        h._h_dev_server_start({"id": ["ds1"]})
        assert rec.code == 400
        assert "no longer readable" in rec.payload["error"]

    def test_start_adopts_running_server(self, isolated_queue, tmp_path, monkeypatch):
        """If a dev server is already running for this folder, it is adopted
        rather than spawning a second one; failure means port conflicts."""
        proj_dir = tmp_path / "running"
        proj_dir.mkdir()
        proj = {"id": "ds2", "path": str(proj_dir), "name": "running"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(
            server, '_auto_dev_server', lambda root: "http://localhost:3000"
        )
        monkeypatch.setattr(
            server, '_front_with_proxy',
            lambda pid, url: "http://127.0.0.1:7800/proxy/"
        )
        h, rec = _get("/dev-server/start?id=ds2")
        h._h_dev_server_start({"id": ["ds2"]})
        assert rec.code == 200
        assert rec.payload["adopted"] is True
        assert rec.payload["url"] == "http://127.0.0.1:7800/proxy/"

    def test_start_spawns_dev_proc(self, isolated_queue, tmp_path, monkeypatch):
        """When no running server is found, _start_dev_proc is called; failure
        means the Start button in the UI does nothing."""
        proj_dir = tmp_path / "fresh"
        proj_dir.mkdir()
        proj = {"id": "ds3", "path": str(proj_dir), "name": "fresh"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_auto_dev_server', lambda root: "")
        monkeypatch.setattr(
            server, '_start_dev_proc',
            lambda pid, root: {"ok": True, "url": "http://localhost:5173"}
        )
        h, rec = _get("/dev-server/start?id=ds3")
        h._h_dev_server_start({"id": ["ds3"]})
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert rec.payload["project"]["id"] == "ds3"

    def test_start_dev_proc_failure_reported(self, isolated_queue, tmp_path, monkeypatch):
        """A failed spawn returns the error text at status 200 (the error IS the
        answer); failure means the UI shows a raw 500 instead of a message."""
        proj_dir = tmp_path / "fail"
        proj_dir.mkdir()
        proj = {"id": "ds4", "path": str(proj_dir), "name": "fail"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_auto_dev_server', lambda root: "")
        monkeypatch.setattr(
            server, '_start_dev_proc',
            lambda pid, root: {"ok": False, "error": "no package.json"}
        )
        h, rec = _get("/dev-server/start?id=ds4")
        h._h_dev_server_start({"id": ["ds4"]})
        assert rec.code == 200
        assert rec.payload["ok"] is False
        assert "no package.json" in rec.payload["error"]

    def test_stop_missing_project_returns_404(self, isolated_queue, monkeypatch):
        """Stopping a dev server for a nonexistent project returns 404."""
        monkeypatch.setattr(server, '_CFG', {'projects': [], 'activeId': ''})
        h, rec = _get("/dev-server/stop?id=nope")
        h._h_dev_server_stop({"id": ["nope"]})
        assert rec.code == 404

    def test_stop_succeeds(self, isolated_queue, monkeypatch):
        """Stopping a registered project's dev server returns ok and saves config;
        failure means the Stop button does not actually terminate anything."""
        proj = {"id": "ds5", "path": "/tmp/stop", "name": "stop",
                "previewUrl": "http://localhost:3000"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_stop_dev_proc', lambda pid: True)
        monkeypatch.setattr(server, '_save_cfg', lambda c: None)
        h, rec = _get("/dev-server/stop?id=ds5")
        h._h_dev_server_stop({"id": ["ds5"]})
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert rec.payload["stopped"] is True
        # previewUrl should be popped from proj
        assert "previewUrl" not in proj

    def test_stop_saves_the_registry_under_the_lock(self, isolated_queue, monkeypatch):
        """The registry write must be serialized, the process teardown must not be.

        `_save_cfg` is a whole-file atomic replace, so two concurrent replacements
        do not interleave -- the loser is overwritten and a project added or
        removed by the racing request vanishes on the next load. This was the only
        `_save_cfg(_CFG)` in the file outside `_QUEUE_LOCK`.

        The other half is just as load-bearing: `_stop_dev_proc` escalates
        SIGTERM -> SIGKILL and waits on the child, so holding the lock across it
        would stall every queue operation for the length of that teardown. This
        pins BOTH -- saved while held, stopped while not.
        """
        proj = {"id": "ds6", "path": "/tmp/stop6", "name": "stop6",
                "previewUrl": "http://localhost:3000"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})

        # A depth-tracking stand-in rather than the real lock. `_QUEUE_LOCK` is an
        # RLock, which has no `.locked()`, and being reentrant it grants a
        # same-thread `acquire(blocking=False)` even while held -- so neither can
        # answer "is it held right now". Substituting the context manager can.
        class _Probe:
            def __init__(self):
                self.depth = 0

            def __enter__(self):
                self.depth += 1
                return self

            def __exit__(self, *exc):
                self.depth -= 1
                return False

        probe = _Probe()
        monkeypatch.setattr(server, '_QUEUE_LOCK', probe)

        held_during_stop = []
        held_during_save = []
        monkeypatch.setattr(
            server, '_stop_dev_proc',
            lambda pid: (held_during_stop.append(probe.depth), True)[1],
        )
        monkeypatch.setattr(
            server, '_save_cfg', lambda cfg: held_during_save.append(probe.depth),
        )
        h, rec = _get("/dev-server/stop?id=ds6")
        h._h_dev_server_stop({"id": ["ds6"]})

        assert rec.code == 200
        assert held_during_save == [1], "registry write must hold _QUEUE_LOCK"
        assert held_during_stop == [0], "process teardown must NOT hold the lock"
        assert probe.depth == 0, "the lock must be released"

    def test_stop_does_not_resurrect_a_concurrently_removed_project(
        self, isolated_queue, monkeypatch
    ):
        """A project removed during the teardown window must not be written back.

        `proj` is resolved BEFORE `_stop_dev_proc`, which can take as long as a
        SIGKILL escalation. Mutating that pre-lock dict afterwards would persist a
        registry the project is no longer part of, so the handler re-resolves
        inside the lock and saves nothing when it is gone.
        """
        proj = {"id": "ds7", "path": "/tmp/stop7", "name": "stop7",
                "previewUrl": "http://localhost:3000"}
        cfg = {'projects': [proj], 'activeId': ''}
        monkeypatch.setattr(server, '_CFG', cfg)

        saves = []
        # The removal lands while the dev process is being torn down.
        monkeypatch.setattr(
            server, '_stop_dev_proc',
            lambda pid: (cfg["projects"].clear(), True)[1],
        )
        monkeypatch.setattr(server, '_save_cfg', lambda c: saves.append(c))
        h, rec = _get("/dev-server/stop?id=ds7")
        h._h_dev_server_stop({"id": ["ds7"]})

        assert rec.code == 200
        assert rec.payload["stopped"] is True
        # Nothing persisted, and the removal stands.
        assert saves == []
        assert cfg["projects"] == []


# ---------------------------------------------------------------------------
# /thread — append progress to a comment thread
# ---------------------------------------------------------------------------

class TestThread:
    """The /thread route appends agent progress notes to request threads."""

    def _make_request_file(self, queue_dir, rid, comments=None):
        """Write a request JSON to the queue directory."""
        req = {
            "id": rid,
            "state": "sent",
            "sentAt": "2025-01-01T00:00:00Z",
            "number": 1,
            "comments": comments or [],
        }
        fp = queue_dir / f"{rid}.json"
        fp.write_text(json.dumps(req), encoding="utf-8")
        return fp

    def test_rejects_missing_id(self, isolated_queue, monkeypatch):
        """An empty id is rejected with 400; without this, requests could
        target arbitrary queue files via path traversal."""
        h, rec = _post("/thread?id=", {"text": "hi", "role": "agent"})
        h._h_thread({"id": [""], "cid": [""]})
        assert rec.code == 400
        assert "valid id required" in rec.payload["error"]

    def test_rejects_invalid_id_format(self, isolated_queue, monkeypatch):
        """An id with special characters (path traversal attempt) is rejected;
        without this, ../../../etc/passwd could be read."""
        h, rec = _post("/thread?id=../etc", {"text": "hi", "role": "agent"})
        h._h_thread({"id": ["../etc"], "cid": [""]})
        assert rec.code == 400
        assert "valid id required" in rec.payload["error"]

    def test_rejects_invalid_cid_format(self, isolated_queue, monkeypatch):
        """An invalid cid is rejected; without this, comment targeting could
        break on malformed identifiers."""
        h, rec = _post("/thread?id=abc&cid=../x", {"text": "hi", "role": "agent"})
        h._h_thread({"id": ["abc"], "cid": ["../x"]})
        assert rec.code == 400
        assert "invalid cid" in rec.payload["error"]

    def test_request_not_found(self, isolated_queue, monkeypatch):
        """Threading to a non-existent request returns 404; without this, the
        agent gets a 500 and retries indefinitely."""
        h, rec = _post("/thread?id=noexist", {"text": "progress", "role": "agent"})
        h._h_thread({"id": ["noexist"], "cid": [""]})
        assert rec.code == 404
        assert "not found" in rec.payload["error"]

    def test_appends_to_request_thread(self, isolated_queue, monkeypatch):
        """A valid thread post appends to the request's thread array; failure
        means agent progress notes are silently lost."""
        rid = "req-thread-1"
        self._make_request_file(isolated_queue, rid)
        h, rec = _post(f"/thread?id={rid}", {"text": "working on it", "role": "agent"})
        h._h_thread({"id": [rid], "cid": [""]})
        assert rec.code == 200
        assert rec.payload["ok"] is True
        # Verify persisted
        fp = isolated_queue / f"{rid}.json"
        saved = json.loads(fp.read_text())
        assert len(saved["thread"]) == 1
        assert saved["thread"][0]["text"] == "working on it"
        assert saved["thread"][0]["role"] == "agent"

    def test_targets_specific_comment(self, isolated_queue, monkeypatch):
        """With a cid, the thread entry lands on that comment, not the request;
        failure means per-comment progress is lost and the UI shows nothing."""
        rid = "req-thread-2"
        cid = "comment-a"
        self._make_request_file(
            isolated_queue, rid,
            comments=[{"cid": cid, "text": "fix button", "status": "new"}]
        )
        h, rec = _post(
            f"/thread?id={rid}&cid={cid}",
            {"text": "fixed", "role": "agent", "status": "done"}
        )
        h._h_thread({"id": [rid], "cid": [cid]})
        assert rec.code == 200
        assert rec.payload["ok"] is True
        fp = isolated_queue / f"{rid}.json"
        saved = json.loads(fp.read_text())
        comment = saved["comments"][0]
        assert len(comment["thread"]) == 1
        assert comment["thread"][0]["text"] == "fixed"
        assert comment["status"] == "done"

    def test_missing_comment_returns_404(self, isolated_queue, monkeypatch):
        """Targeting a cid not in the request returns 404; without this the
        agent silently discards the progress note."""
        rid = "req-thread-3"
        self._make_request_file(isolated_queue, rid, comments=[])
        h, rec = _post(
            f"/thread?id={rid}&cid=nosuch",
            {"text": "hello", "role": "agent"}
        )
        h._h_thread({"id": [rid], "cid": ["nosuch"]})
        assert rec.code == 404
        assert "comment nosuch not in request" in rec.payload["error"]

    def test_requires_text_or_status(self, isolated_queue, monkeypatch):
        """Sending neither text nor status is rejected; without this, empty
        thread entries pollute the queue and confuse the panel."""
        rid = "req-thread-4"
        self._make_request_file(isolated_queue, rid)
        h, rec = _post(f"/thread?id={rid}", {"role": "agent"})
        h._h_thread({"id": [rid], "cid": [""]})
        assert rec.code == 400
        assert "text or status required" in rec.payload["error"]

    def test_normalizes_invalid_role(self, isolated_queue, monkeypatch):
        """An unrecognized role is normalized to 'agent' rather than stored
        verbatim; without this the panel's role-based styling breaks."""
        rid = "req-thread-5"
        self._make_request_file(isolated_queue, rid)
        h, rec = _post(f"/thread?id={rid}", {"text": "hi", "role": "hacker"})
        h._h_thread({"id": [rid], "cid": [""]})
        assert rec.code == 200
        fp = isolated_queue / f"{rid}.json"
        saved = json.loads(fp.read_text())
        assert saved["thread"][0]["role"] == "agent"

    def test_draft_promoted_to_sent_on_agent_activity(
        self, isolated_queue, monkeypatch
    ):
        """A draft request is promoted to 'sent' when an agent posts a thread
        note; without this, worked requests appear unsent in the panel."""
        rid = "req-thread-6"
        fp = isolated_queue / f"{rid}.json"
        fp.write_text(json.dumps({
            "id": rid, "state": "draft", "number": 1, "comments": []
        }))
        h, rec = _post(f"/thread?id={rid}", {"text": "on it", "role": "agent"})
        h._h_thread({"id": [rid], "cid": [""]})
        assert rec.code == 200
        saved = json.loads(fp.read_text())
        assert saved["state"] == "sent"
        assert saved.get("sentAt")


# ---------------------------------------------------------------------------
# /pick-folder — native macOS folder chooser
# ---------------------------------------------------------------------------

class TestPickFolder:
    """The folder picker must never run real osascript and must gate on darwin."""

    def test_non_darwin_returns_501(self, isolated_queue, monkeypatch):
        """Off macOS the picker returns a structured error rather than trying to
        spawn osascript; failure means Linux/Windows users see a raw crash."""
        monkeypatch.setattr(server._sys, 'platform', 'linux')
        h, rec = _post("/pick-folder")
        h._h_pick_folder()
        assert rec.code == 501
        assert "macOS-only" in rec.payload["error"]

    def test_picker_unavailable(self, isolated_queue, monkeypatch):
        """If trusted_system_bin returns None (osascript not at expected path),
        the handler returns a structured error, not a crash."""
        monkeypatch.setattr(server._sys, 'platform', 'darwin')
        monkeypatch.setattr(server, 'trusted_system_bin', lambda name: None)
        # Release the lock if test isolation left it acquired
        if server._PICK_LOCK.locked():
            server._PICK_LOCK.release()
        h, rec = _post("/pick-folder")
        h._h_pick_folder()
        assert rec.code == 501
        assert rec.payload["code"] == "picker_unavailable"

    def test_picker_timeout(self, isolated_queue, monkeypatch):
        """A timed-out picker returns 408 rather than hanging; failure means the
        backend thread is blocked forever by a stuck dialog."""
        monkeypatch.setattr(server._sys, 'platform', 'darwin')
        monkeypatch.setattr(server, 'trusted_system_bin', lambda name: '/usr/bin/osascript')

        def _timeout_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=180)

        monkeypatch.setattr(subprocess, 'run', _timeout_run)
        if server._PICK_LOCK.locked():
            server._PICK_LOCK.release()
        h, rec = _post("/pick-folder")
        h._h_pick_folder()
        assert rec.code == 408
        assert "timed out" in rec.payload["error"]

    def test_picker_canceled(self, isolated_queue, monkeypatch):
        """A user-cancelled dialog returns ok=False/canceled=True; failure means
        cancellation is reported as an error and the UI shows an alert."""
        monkeypatch.setattr(server._sys, 'platform', 'darwin')
        monkeypatch.setattr(server, 'trusted_system_bin', lambda name: '/usr/bin/osascript')

        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="User canceled (-128)"
        )
        monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: result)
        if server._PICK_LOCK.locked():
            server._PICK_LOCK.release()
        h, rec = _post("/pick-folder")
        h._h_pick_folder()
        assert rec.code == 200
        assert rec.payload["canceled"] is True

    def test_picker_returns_path(self, isolated_queue, monkeypatch):
        """A successful pick returns the chosen path; failure means the folder
        registration flow is completely broken."""
        monkeypatch.setattr(server._sys, 'platform', 'darwin')
        monkeypatch.setattr(server, 'trusted_system_bin', lambda name: '/usr/bin/osascript')

        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="/Users/me/projects/app\n", stderr=""
        )
        monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: result)
        if server._PICK_LOCK.locked():
            server._PICK_LOCK.release()
        h, rec = _post("/pick-folder")
        h._h_pick_folder()
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert rec.payload["path"] == "/Users/me/projects/app"

    def test_picker_concurrent_lock(self, isolated_queue, monkeypatch):
        """Only one picker can be open at a time; a second attempt returns 409;
        failure means two dialogs stack invisibly and the user is confused."""
        monkeypatch.setattr(server._sys, 'platform', 'darwin')
        # Acquire the lock to simulate a picker already running
        server._PICK_LOCK.acquire()
        try:
            h, rec = _post("/pick-folder")
            h._h_pick_folder()
            assert rec.code == 409
            assert "already open" in rec.payload["error"]
        finally:
            server._PICK_LOCK.release()

    def test_picker_oserror(self, isolated_queue, monkeypatch):
        """An OSError from subprocess is caught and reported; failure means the
        backend crashes on a permission-denied spawn."""
        monkeypatch.setattr(server._sys, 'platform', 'darwin')
        monkeypatch.setattr(server, 'trusted_system_bin', lambda name: '/usr/bin/osascript')

        def _raise_os_error(*args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(subprocess, 'run', _raise_os_error)
        if server._PICK_LOCK.locked():
            server._PICK_LOCK.release()
        h, rec = _post("/pick-folder")
        h._h_pick_folder()
        assert rec.code == 500
        assert "permission denied" in rec.payload["error"]


# ---------------------------------------------------------------------------
# /source (POST) — set source for preview
# ---------------------------------------------------------------------------

class TestSetSource:
    """The /source route sets _ROOT or _TARGET for the preview proxy."""

    def test_clear_source(self, isolated_queue, monkeypatch):
        """Empty value clears both _ROOT and _TARGET; failure means there is no
        way to disconnect a preview."""
        monkeypatch.setattr(server, '_ROOT', '/old/root')
        monkeypatch.setattr(server, '_TARGET', 'http://localhost:3000')
        h, rec = _post("/source", {"value": ""})
        h._h_set_source()
        assert rec.code == 200
        assert rec.payload["mode"] == "cleared"
        assert server._ROOT == ""
        assert server._TARGET == ""

    def test_set_url_target(self, isolated_queue, monkeypatch):
        """A loopback URL sets _TARGET for reverse-proxying; failure means
        framework projects cannot use HMR."""
        monkeypatch.setattr(server, '_ROOT', '')
        monkeypatch.setattr(server, '_TARGET', '')
        h, rec = _post("/source", {"value": "http://127.0.0.1:5173"})
        h._h_set_source()
        assert rec.code == 200
        assert rec.payload["mode"] == "url"
        assert server._TARGET == "http://127.0.0.1:5173"

    def test_set_url_rejects_non_loopback(self, isolated_queue, monkeypatch):
        """A non-loopback URL is rejected (SSRF); failure means the proxy can
        be used to reach internal services."""
        h, rec = _post("/source", {"value": "http://internal.corp:8080"})
        h._h_set_source()
        assert rec.code == 400
        assert "localhost" in rec.payload["error"] or "127.0.0.1" in rec.payload["error"]

    def test_set_folder_source(self, isolated_queue, tmp_path, monkeypatch):
        """A valid directory sets _ROOT for static serving; failure means the
        user cannot preview from a local folder."""
        d = tmp_path / "static-site"
        d.mkdir()
        h, rec = _post("/source", {"value": str(d)})
        h._h_set_source()
        assert rec.code == 200
        assert rec.payload["mode"] == "folder"
        assert server._ROOT == str(d.resolve())

    def test_set_folder_rejects_invalid(self, isolated_queue, monkeypatch):
        """An invalid folder path returns 400; failure means a nonexistent path
        could be set as _ROOT and all subsequent requests 500."""
        h, rec = _post("/source", {"value": "/no/such/dir"})
        h._h_set_source()
        assert rec.code == 400
        assert "not a readable folder" in rec.payload["error"]


# ---------------------------------------------------------------------------
# main() — bootstrap tail (does not run serve_forever)
# ---------------------------------------------------------------------------

class TestMain:
    """The main() entry point creates directories and binds the server."""

    def test_main_creates_directories(self, tmp_path, monkeypatch):
        """main() creates QUEUE_DIR and HANDLED_DIR at startup; failure means
        the first request 500s because the queue path does not exist."""
        q = tmp_path / "q"
        h = tmp_path / "h"
        monkeypatch.setattr(server, 'QUEUE_DIR', q)
        monkeypatch.setattr(server, 'HANDLED_DIR', h)
        # Patch ThreadingHTTPServer to not actually listen
        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = KeyboardInterrupt
        monkeypatch.setattr(
            server, 'ThreadingHTTPServer', lambda addr, handler: mock_server
        )
        server.main()
        assert q.is_dir()
        assert h.is_dir()
        mock_server.server_close.assert_called_once()

    def test_main_returns_zero(self, tmp_path, monkeypatch):
        """main() returns 0 after KeyboardInterrupt; failure means the process
        exits with an error code on normal shutdown."""
        monkeypatch.setattr(server, 'QUEUE_DIR', tmp_path / "q")
        monkeypatch.setattr(server, 'HANDLED_DIR', tmp_path / "h")
        mock_server = MagicMock()
        mock_server.serve_forever.side_effect = KeyboardInterrupt
        monkeypatch.setattr(
            server, 'ThreadingHTTPServer', lambda addr, handler: mock_server
        )
        assert server.main() == 0


# ---------------------------------------------------------------------------
# /projects listing — deeper proxy/dev branches
# ---------------------------------------------------------------------------

class TestProjectsListProxyBranches:
    """Cover the live-proxy and persisted-URL branches in _h_projects_list."""

    def test_live_proxy_url_used(self, isolated_queue, tmp_path, monkeypatch):
        """When _DEV_PROCS has a live proxyUrl for a project, the listing uses
        it instead of the persisted URL; failure means the injecting proxy is
        bypassed and select-to-edit silently breaks."""
        proj_dir = tmp_path / "proxied"
        proj_dir.mkdir()
        proj = {"id": "px1", "path": str(proj_dir), "name": "proxied"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_DEV_PROCS', {
            "px1": {"proxyUrl": "http://127.0.0.1:7800/proxy/", "url": "http://localhost:3000"}
        })
        monkeypatch.setattr(server, '_classify_project', lambda root: {
            "needsDevServer": False, "devCommand": "", "unbundledEntry": "", "hasEntry": True
        })
        monkeypatch.setattr(server, '_dev_proc_alive', lambda pid: True)
        h, rec = _get("/projects")
        h._h_projects_list()
        assert rec.code == 200
        row = rec.payload["projects"][0]
        assert row["previewUrl"] == "http://127.0.0.1:7800/proxy/"
        assert row["devUrl"] == "http://localhost:3000"

    def test_persisted_dev_url_fronted_with_proxy(
        self, isolated_queue, tmp_path, monkeypatch
    ):
        """A persisted loopback previewUrl that passes _valid_target is routed
        through _front_with_proxy; failure means the injecting overlay never
        loads for framework projects that persist their dev URL."""
        proj_dir = tmp_path / "fronted"
        proj_dir.mkdir()
        proj = {
            "id": "fr1", "path": str(proj_dir), "name": "fronted",
            "previewUrl": "http://localhost:5173"
        }
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_DEV_PROCS', {})
        monkeypatch.setattr(server, '_classify_project', lambda root: {
            "needsDevServer": False, "devCommand": "", "unbundledEntry": "", "hasEntry": True
        })
        monkeypatch.setattr(server, '_dev_proc_alive', lambda pid: False)
        monkeypatch.setattr(
            server, '_front_with_proxy',
            lambda pid, url: "http://127.0.0.1:7801/proxy/"
        )
        h, rec = _get("/projects")
        h._h_projects_list()
        assert rec.code == 200
        row = rec.payload["projects"][0]
        assert row["previewUrl"] == "http://127.0.0.1:7801/proxy/"
        assert row["devUrl"] == "http://localhost:5173"


# ---------------------------------------------------------------------------
# /detect-dev-server — server detection route
# ---------------------------------------------------------------------------

class TestDetectDevServer:
    """The /detect-dev-server route finds running dev servers for a project."""

    def test_by_project_id(self, isolated_queue, tmp_path, monkeypatch):
        """Detection by project id resolves the folder and probes; failure means
        the Detect button in the UI is broken."""
        proj_dir = tmp_path / "detect"
        proj_dir.mkdir()
        proj = {"id": "det1", "path": str(proj_dir), "name": "detect"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_detect_dev_servers', lambda root: [
            {"url": "http://localhost:3000", "servesHtml": True}
        ])
        h, rec = _get("/detect-dev-server?id=det1")
        h._h_detect_dev_server({"id": ["det1"], "path": [""]})
        assert rec.code == 200
        assert rec.payload["ok"] is True
        assert rec.payload["suggested"] == "http://localhost:3000"

    def test_unknown_project_returns_404(self, isolated_queue, monkeypatch):
        """Detection for an unregistered project id returns 404."""
        monkeypatch.setattr(server, '_CFG', {'projects': [], 'activeId': ''})
        h, rec = _get("/detect-dev-server?id=nope")
        h._h_detect_dev_server({"id": ["nope"], "path": [""]})
        assert rec.code == 404

    def test_invalid_path_returns_400(self, isolated_queue, monkeypatch):
        """Detection for an unreadable path returns 400; failure means a 500
        instead of a user-facing error."""
        monkeypatch.setattr(server, '_CFG', {'projects': [], 'activeId': ''})
        h, rec = _get("/detect-dev-server?path=/no/such")
        h._h_detect_dev_server({"id": [""], "path": ["/no/such"]})
        assert rec.code == 400

    def test_ambiguous_candidates_no_suggestion(
        self, isolated_queue, tmp_path, monkeypatch
    ):
        """When multiple servers serve HTML, no single suggestion is offered;
        failure means the wrong dev server could be auto-selected."""
        proj_dir = tmp_path / "multi"
        proj_dir.mkdir()
        proj = {"id": "m1", "path": str(proj_dir), "name": "multi"}
        monkeypatch.setattr(server, '_CFG', {'projects': [proj], 'activeId': ''})
        monkeypatch.setattr(server, '_detect_dev_servers', lambda root: [
            {"url": "http://localhost:3000", "servesHtml": True},
            {"url": "http://localhost:5173", "servesHtml": True},
        ])
        h, rec = _get("/detect-dev-server?id=m1")
        h._h_detect_dev_server({"id": ["m1"], "path": [""]})
        assert rec.code == 200
        assert rec.payload["suggested"] == ""


# ---------------------------------------------------------------------------
# /projects (POST) — duplicate with updated previewUrl
# ---------------------------------------------------------------------------

class TestProjectsAddPreviewUrlUpdate:
    """Cover the duplicate-project-with-updated-previewUrl branch."""

    def test_duplicate_updates_preview_url(self, isolated_queue, tmp_path, monkeypatch):
        """Re-registering an existing project with a different previewUrl updates
        it; failure means a changed dev server port requires deregister+reregister."""
        proj_dir = tmp_path / "upd"
        proj_dir.mkdir()
        existing = {
            "id": "upd1", "path": str(proj_dir.resolve()), "name": "upd",
            "previewUrl": "http://localhost:3000"
        }
        monkeypatch.setattr(server, '_CFG', {'projects': [existing], 'activeId': ''})
        monkeypatch.setattr(server, '_save_cfg', lambda c: None)
        monkeypatch.setattr(server, '_detect_dev_servers', lambda root: [])
        h, rec = _post("/projects", {
            "path": str(proj_dir),
            "previewUrl": "http://localhost:5173"
        })
        h._h_projects_add()
        assert rec.code == 200
        assert rec.payload["existing"] is True
        assert rec.payload.get("updated") == "previewUrl"
        assert existing["previewUrl"] == "http://localhost:5173"


# ---------------------------------------------------------------------------
# /thread — done status fan-out
# ---------------------------------------------------------------------------

class TestThreadDoneFanout:
    """Cover the request-level done→all-comments fan-out branch."""

    def _make_request_file(self, queue_dir, rid, comments=None):
        req = {
            "id": rid,
            "state": "sent",
            "sentAt": "2025-01-01T00:00:00Z",
            "number": 1,
            "comments": comments or [],
        }
        fp = queue_dir / f"{rid}.json"
        fp.write_text(json.dumps(req), encoding="utf-8")
        return fp

    def test_done_fans_out_to_all_comments(self, isolated_queue, monkeypatch):
        """Setting status=done at the request level marks ALL comments done;
        failure means an agent that reports once for the batch leaves
        sub-items stuck as 'new' in the panel."""
        rid = "req-fan-1"
        self._make_request_file(
            isolated_queue, rid,
            comments=[
                {"cid": "c1", "text": "fix A", "status": "new"},
                {"cid": "c2", "text": "fix B", "status": "new"},
            ]
        )
        h, rec = _post(
            f"/thread?id={rid}",
            {"text": "all done", "role": "agent", "status": "done"}
        )
        h._h_thread({"id": [rid], "cid": [""]})
        assert rec.code == 200
        fp = isolated_queue / f"{rid}.json"
        saved = json.loads(fp.read_text())
        for c in saved["comments"]:
            assert c["status"] == "done"
