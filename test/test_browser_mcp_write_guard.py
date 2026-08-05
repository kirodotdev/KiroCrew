"""Guards for the write discipline on kiro's global ``mcp.json``.

Every writer of ``~/.kiro/settings/mcp.json`` must serialize on the shared
``mcp.lock`` sidecar, refuse to overwrite a user-authored server under the
canonical Playwright key, and create the file when a fresh install has none.
The dashboard browser-config endpoint used to call the low-level
``patch_mcp_*`` primitives directly, which have none of those three properties:

* no lock -> a concurrent app-bridge / dashboard-MCP write is clobbered and
  that writer's server entries are lost;
* no guard -> a hand-authored direct ``playwright-mcp`` entry is silently
  overwritten (data loss, no concurrency required);
* no create -> a cold install silently no-ops while reporting success.

These tests pin all three, plus a structural tripwire so the primitives cannot
be called unlocked from a new site again.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

import kiro_crew.browser.setup as setup_mod
from kiro_crew.dashboard.handlers.messaging import api_browser_config_save
from kiro_crew.mcp_utils import mcp_server_alias

_CANONICAL = mcp_server_alias("@playwright/mcp")
_REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
_PRIMITIVES = {"patch_mcp_extension", "patch_mcp_headless"}


def _mcp_json(home: Path) -> Path:
    return home / ".kiro" / "settings" / "mcp.json"


def _write_mcp(home: Path, servers: dict) -> Path:
    path = _mcp_json(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")
    return path


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HOME and the KiroCrew data home at a throwaway tree."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    data_home = tmp_path / "data"
    data_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(setup_mod, "config_dir", lambda: data_home)
    monkeypatch.setattr(setup_mod, "_kirocrew_bin", lambda: "kirocrew")
    return home


def _save(body: dict, data_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Drive the real PUT /api/browser/config handler with ``body``."""
    import kiro_crew.config.loader as loader_mod

    monkeypatch.setattr(loader_mod, "config_dir", lambda: data_home)
    req = make_mocked_request("PUT", "/api/browser/config")

    async def _json():
        return body

    monkeypatch.setattr(req, "json", _json, raising=False)
    return asyncio.run(api_browser_config_save(req))


def _try_lock_noblock(lock_path: Path) -> bool:
    """True iff an exclusive advisory lock can be taken right now.

    Used to prove a write happened while the caller held the sidecar lock,
    without any sleep-based timing. POSIX-only: ``fcntl.flock`` is per-fd, so a
    second fd in this same process is refused while the first fd holds LOCK_EX.
    """
    import fcntl

    with open(lock_path, "a+") as probe:
        try:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        return True


needs_posix = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="fcntl non-blocking probe is POSIX-only"
)


class TestDashboardSaveGuardsUserConfig:
    def test_keeps_user_authored_direct_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A user hand-authored their OWN direct (non-proxy) Playwright server
        # under the canonical key. Saving browser settings from the dashboard
        # must not overwrite it — authorship is by launch target, not key name.
        home = _isolate(tmp_path, monkeypatch)
        direct = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        path = _write_mcp(home, {_CANONICAL: dict(direct)})
        before = path.read_text(encoding="utf-8")

        resp = _save({"extension_mode": False, "token": ""}, tmp_path / "data", monkeypatch)

        assert path.read_text(encoding="utf-8") == before
        assert json.loads(resp.text)["mcp_status"] == "kept-user-entry"

    def test_refreshes_own_proxy_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # The guard must not freeze KiroCrew's OWN entry — a proxy spec under the
        # canonical key is ours and is still rewritten.
        home = _isolate(tmp_path, monkeypatch)
        stale = {"command": "kirocrew", "args": ["mcp-playwright-proxy", "--config", "/stale"]}
        path = _write_mcp(home, {_CANONICAL: dict(stale), "other-mcp": {"command": "foo"}})

        resp = _save({"extension_mode": False, "token": ""}, tmp_path / "data", monkeypatch)

        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert servers[_CANONICAL]["args"] != stale["args"]
        assert servers["other-mcp"] == {"command": "foo"}, "unrelated server must survive"
        assert json.loads(resp.text)["mcp_status"] == "registered"


class TestDashboardSaveColdInstall:
    def test_creates_mcp_json_when_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        # No ~/.kiro/settings/mcp.json yet. The endpoint used to return ok:true
        # having written nothing, leaving the Browser panel silently unwired.
        home = _isolate(tmp_path, monkeypatch)
        path = _mcp_json(home)
        assert not path.exists()

        resp = _save({"extension_mode": False, "token": ""}, tmp_path / "data", monkeypatch)

        assert path.exists(), "cold install must create the config, not no-op"
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert "mcp-playwright-proxy" in servers[_CANONICAL]["args"]
        assert json.loads(resp.text)["mcp_status"] == "registered"


class TestWritesHappenUnderTheLock:
    @needs_posix
    def test_dashboard_save_holds_lock_during_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        home = _isolate(tmp_path, monkeypatch)
        _write_mcp(home, {})
        lock_path = _mcp_json(home).with_suffix(".lock")
        observed: list[bool] = []
        real = setup_mod._patch_mcp_for_mode_unlocked

        def _spy():
            # Lock must NOT be acquirable here — the caller is holding it.
            observed.append(_try_lock_noblock(lock_path))
            real()

        monkeypatch.setattr(setup_mod, "_patch_mcp_for_mode_unlocked", _spy)
        _save({"extension_mode": False, "token": ""}, tmp_path / "data", monkeypatch)

        assert observed == [False], "write ran without holding the shared mcp.json lock"

    @needs_posix
    def test_public_primitives_take_the_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Any remaining caller of the public wrappers is correct by construction.
        home = _isolate(tmp_path, monkeypatch)
        _write_mcp(home, {})
        lock_path = _mcp_json(home).with_suffix(".lock")
        observed: list[bool] = []

        monkeypatch.setattr(
            setup_mod,
            "_patch_mcp_headless_unlocked",
            lambda: observed.append(_try_lock_noblock(lock_path)),
        )
        setup_mod.patch_mcp_headless()

        monkeypatch.setattr(
            setup_mod,
            "_patch_mcp_extension_unlocked",
            lambda _t: observed.append(_try_lock_noblock(lock_path)),
        )
        setup_mod.patch_mcp_extension("tok-123")

        assert observed == [False, False]

    @needs_posix
    def test_boot_migration_holds_lock_across_read_and_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The migration decides from a read then writes; both must be one
        # critical section or the write is computed from a stale snapshot.
        home = _isolate(tmp_path, monkeypatch)
        legacy = {"command": "npx", "args": ["@playwright/mcp@latest"]}
        _write_mcp(home, {"npm:@playwright/mcp": legacy})
        lock_path = _mcp_json(home).with_suffix(".lock")
        observed: list[bool] = []

        monkeypatch.setattr(setup_mod, "has_playwright_extension", lambda: False)
        monkeypatch.setattr(
            setup_mod,
            "_patch_mcp_headless_unlocked",
            lambda: observed.append(_try_lock_noblock(lock_path)),
        )
        setup_mod._migrate_owned_kiro_registration()

        assert observed == [False], "boot migration wrote without the shared lock"

    def test_migration_does_not_create_config_on_bare_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Taking the lock creates the settings dir + sidecar as a side effect, so
        # the migration keeps its pre-lock bail: it never adds Playwright (nor
        # kiro's settings tree) where none exists.
        home = _isolate(tmp_path, monkeypatch)
        setup_mod._migrate_owned_kiro_registration()
        assert not _mcp_json(home).exists()
        assert not (home / ".kiro" / "settings").exists()


class TestDoesNotBlockTheEventLoop:
    def test_registration_runs_off_the_loop_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The lock acquire is blocking; taking it on the loop thread would stall
        # every other dashboard request for the duration.
        home = _isolate(tmp_path, monkeypatch)
        _write_mcp(home, {})
        threads: list[int] = []
        real = setup_mod._patch_mcp_for_mode_unlocked

        def _spy():
            threads.append(threading.get_ident())
            real()

        monkeypatch.setattr(setup_mod, "_patch_mcp_for_mode_unlocked", _spy)

        loop_thread: list[int] = []

        async def _drive():
            loop_thread.append(threading.get_ident())
            req = make_mocked_request("PUT", "/api/browser/config")

            async def _json():
                return {"extension_mode": False, "token": ""}

            monkeypatch.setattr(req, "json", _json, raising=False)
            import kiro_crew.config.loader as loader_mod

            monkeypatch.setattr(loader_mod, "config_dir", lambda: tmp_path / "data")
            return await api_browser_config_save(req)

        asyncio.run(_drive())

        assert threads and threads[0] != loop_thread[0], "blocking write ran on the event loop"


class TestPrimitivesAreNotCalledUnlocked:
    def test_no_module_outside_browser_setup_calls_the_primitives(self):
        """Tripwire: the lock-free write path must stay inside browser/setup.py.

        ``patch_mcp_extension`` / ``patch_mcp_headless`` are the only entry points
        that mutate kiro's global mcp.json without the caller supplying a lock.
        They are now self-locking wrappers, but a new call site elsewhere is still
        a smell: it means someone re-derived the extension-vs-headless dispatch
        instead of calling ``register_playwright_proxy``, and so also skipped the
        user-entry guard and the create-when-absent path. Fail closed and make the
        author route through ``register_playwright_proxy`` instead.
        """
        offenders: list[str] = []
        own = (_REPO_SRC / "browser" / "setup.py").resolve()
        for path in _REPO_SRC.rglob("*.py"):
            if path.resolve() == own:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (
                    fn.id
                    if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None
                )
                if name in _PRIMITIVES:
                    rel = path.relative_to(_REPO_SRC)
                    offenders.append(f"{rel}:{node.lineno} calls {name}()")
        assert not offenders, (
            "route these through register_playwright_proxy() (lock + user-entry "
            "guard + create-when-absent):\n  " + "\n  ".join(offenders)
        )

    def test_tripwire_detects_a_planted_call(self):
        # Confirms the AST walk above actually matches both call shapes, so a
        # green result means "no offenders", not "the matcher is broken".
        src = "import x\ndef f():\n    patch_mcp_headless()\n    x.patch_mcp_extension('t')\n"
        tree = ast.parse(src)
        found = {
            (
                n.func.id
                if isinstance(n.func, ast.Name)
                else n.func.attr if isinstance(n.func, ast.Attribute) else None
            )
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert _PRIMITIVES <= found


class TestFailureIsReportedNotRaised:
    def test_registration_failure_still_saves_the_preference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The mode flag is persisted before mcp.json is touched, so an
        # mcp.json-level failure must not 500 — that would tell the user nothing
        # was saved when the preference in fact was.
        _isolate(tmp_path, monkeypatch)
        data_home = tmp_path / "data"

        def _boom():
            raise OSError("disk full")

        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers.messaging.register_playwright_proxy", _boom
        )
        resp = _save({"extension_mode": False, "token": ""}, data_home, monkeypatch)

        body = json.loads(resp.text)
        assert body["ok"] is True
        assert body["mcp_status"] == "registration-failed"
        assert not (data_home / "playwright-extension-mode").exists()


def test_lock_probe_is_meaningful(tmp_path: Path):
    """The probe must report True when nothing holds the lock.

    Without this, every ``observed == [False]`` assertion above could pass for
    the wrong reason (a probe that always fails).
    """
    if sys.platform.startswith("win"):
        pytest.skip("POSIX-only probe")
    lock_path = tmp_path / "mcp.lock"
    lock_path.touch()
    assert _try_lock_noblock(lock_path) is True
    with open(lock_path, "r+") as held:
        import fcntl

        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        try:
            assert _try_lock_noblock(lock_path) is False
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
