"""Backend tests for the imported-cookies feature.

Covers the three parser shapes and their normalisation, expiry dropping and the
size/count limits, the owner-only 0600 storageState write, the conditional
``storageState`` key in the launch config, the value-free summary, the
best-effort hot-load's failure handling, and the three HTTP handlers (status,
import, clear) through the aiohttp client fixture including the non-owner 403 and
the restricted-session 403.

No real ``playwright-cli`` is ever spawned: ``cli_command``/``cli_env`` and
``subprocess.run`` are faked at the module boundary.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.browser_cli import cookies as mod
from kiro_crew.browser_cli import launch as launch_mod
from kiro_crew.platform_compat import IS_WINDOWS


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    # The host running the tests may itself be an agent with the CLI config
    # variable set; prewarm_session treats a foreign value as an operator choice.
    monkeypatch.delenv(launch_mod.CONFIG_ENV, raising=False)
    return h


# ── parse_cookie_import: the three shapes ──


class TestParseShapes:
    def test_playwright_storage_state(self, home: Path) -> None:
        text = json.dumps(
            {
                "cookies": [{"name": "sid", "value": "v", "domain": "example.com", "path": "/"}],
                "origins": [],
            }
        )
        cookies = mod.parse_cookie_import(text)
        assert len(cookies) == 1
        assert cookies[0]["name"] == "sid"
        assert cookies[0]["domain"] == "example.com"

    def test_bare_json_array_extension_export(self, home: Path) -> None:
        text = json.dumps(
            [
                {
                    "name": "auth",
                    "value": "tok",
                    "domain": ".example.com",
                    "path": "/app",
                    "expirationDate": 9999999999,
                    "sameSite": "no_restriction",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        )
        cookies = mod.parse_cookie_import(text)
        assert cookies[0]["expires"] == 9999999999.0
        assert cookies[0]["sameSite"] == "None"
        assert cookies[0]["secure"] is True
        assert cookies[0]["httpOnly"] is True

    def test_netscape_cookies_txt(self, home: Path) -> None:
        text = (
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t9999999999\tsid\tsecret\n"
            "example.org\tFALSE\t/\tFALSE\t0\tplain\tv\n"
        )
        cookies = mod.parse_cookie_import(text)
        assert len(cookies) == 2
        http_only = next(c for c in cookies if c["name"] == "sid")
        assert http_only["httpOnly"] is True
        assert http_only["secure"] is True
        assert http_only["domain"] == ".example.com"
        plain = next(c for c in cookies if c["name"] == "plain")
        # `0` expiry means a session cookie -> normalised to -1.
        assert plain["expires"] == -1.0


class TestNormalisation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Strict", "Strict"),
            ("lax", "Lax"),
            ("no_restriction", "None"),
            ("none", "None"),
            ("unspecified", "Lax"),
            ("garbage", "Lax"),
            (None, "Lax"),
        ],
    )
    def test_same_site(self, home: Path, raw: object, expected: str) -> None:
        text = json.dumps([{"name": "n", "domain": "d.com", "sameSite": raw}])
        assert mod.parse_cookie_import(text)[0]["sameSite"] == expected

    def test_missing_expiry_is_session_cookie(self, home: Path) -> None:
        text = json.dumps([{"name": "n", "domain": "d.com"}])
        assert mod.parse_cookie_import(text)[0]["expires"] == -1.0

    @pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "Infinity", "NaN"])
    def test_non_finite_expiry_string_is_session_cookie(self, home: Path, raw: str) -> None:
        # A non-finite float would be written verbatim into the storage state and
        # make it unreadable to the daemon's strict JSON parser.
        text = json.dumps([{"name": "n", "domain": "d.com", "expirationDate": raw}])
        assert mod.parse_cookie_import(text)[0]["expires"] == -1.0

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_json_constants_are_rejected(self, home: Path, constant: str) -> None:
        text = f'[{{"name": "n", "domain": "d.com", "expirationDate": {constant}}}]'
        with pytest.raises(mod.CookieImportError, match="invalid JSON constant"):
            mod.parse_cookie_import(text)

    def test_deeply_nested_json_is_a_user_error(self, home: Path) -> None:
        # json.loads recurses per nesting level; the handler must see a
        # CookieImportError (400), never a RecursionError (500).
        depth = 100_000
        text = "[" * depth + "]" * depth
        with pytest.raises(mod.CookieImportError, match="nested too deeply|not valid JSON"):
            mod.parse_cookie_import(text)

    def test_expired_cookie_dropped(self, home: Path) -> None:
        past = time.time() - 3600
        text = json.dumps(
            [
                {"name": "old", "domain": "d.com", "expirationDate": past},
                {"name": "live", "domain": "d.com"},
            ]
        )
        cookies = mod.parse_cookie_import(text)
        assert [c["name"] for c in cookies] == ["live"]


class TestLimits:
    def test_over_size_limit_rejected(self, home: Path) -> None:
        big = "x" * (mod.MAX_IMPORT_BYTES + 1)
        with pytest.raises(mod.CookieImportError, match="too large"):
            mod.parse_cookie_import(big)

    def test_over_cookie_count_rejected(self, home: Path) -> None:
        many = [{"name": f"n{i}", "domain": "d.com"} for i in range(mod.MAX_COOKIES + 1)]
        with pytest.raises(mod.CookieImportError, match="too many"):
            mod.parse_cookie_import(json.dumps(many))

    def test_missing_name_rejected(self, home: Path) -> None:
        with pytest.raises(mod.CookieImportError, match="name"):
            mod.parse_cookie_import(json.dumps([{"domain": "d.com"}]))

    def test_missing_domain_rejected(self, home: Path) -> None:
        with pytest.raises(mod.CookieImportError, match="domain"):
            mod.parse_cookie_import(json.dumps([{"name": "n"}]))

    def test_empty_rejected(self, home: Path) -> None:
        with pytest.raises(mod.CookieImportError, match="empty"):
            mod.parse_cookie_import("   ")

    def test_all_expired_rejected(self, home: Path) -> None:
        text = json.dumps([{"name": "old", "domain": "d.com", "expirationDate": 1.0}])
        with pytest.raises(mod.CookieImportError, match="no unexpired"):
            mod.parse_cookie_import(text)

    def test_unrecognisable_rejected(self, home: Path) -> None:
        with pytest.raises(mod.CookieImportError):
            mod.parse_cookie_import("this is not json or a cookies.txt")


# ── save / summary / clear ──


class TestSaveAndSummary:
    def test_save_writes_owner_only_storage_state(self, home: Path) -> None:
        cookies = mod.parse_cookie_import(
            json.dumps([{"name": "n", "value": "v", "domain": "d.com"}])
        )
        path = mod.save_storage_state(cookies)
        assert path == mod.storage_state_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"cookies": cookies, "origins": []}
        if not IS_WINDOWS:
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_summary_has_no_values(self, home: Path) -> None:
        cookies = mod.parse_cookie_import(
            json.dumps(
                [
                    {"name": "a", "value": "SECRET", "domain": ".example.com"},
                    {
                        "name": "b",
                        "value": "TOKEN",
                        "domain": "other.com",
                        "expirationDate": 9999999999,
                    },
                ]
            )
        )
        mod.save_storage_state(cookies)
        summary = mod.storage_state_summary()
        assert summary is not None
        assert summary["cookie_count"] == 2
        # Leading dot stripped, sorted, distinct.
        assert summary["domains"] == ["example.com", "other.com"]
        assert summary["earliest_expiry"] == 9999999999.0
        assert "imported_at" in summary
        blob = json.dumps(summary)
        assert "SECRET" not in blob and "TOKEN" not in blob

    def test_summary_none_when_absent(self, home: Path) -> None:
        assert mod.storage_state_summary() is None

    def test_summary_session_only_earliest_is_none(self, home: Path) -> None:
        cookies = mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}]))
        mod.save_storage_state(cookies)
        assert mod.storage_state_summary()["earliest_expiry"] is None

    def test_clear_removes_file(self, home: Path) -> None:
        cookies = mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}]))
        mod.save_storage_state(cookies)
        assert mod.clear_storage_state() is True
        assert not mod.storage_state_path().exists()
        assert mod.clear_storage_state() is False


# ── Two configs: the agent's never names the masked file, the gateway's does ──


class TestLaunchConfig:
    def test_agent_config_is_engine_only_without_file(self, home: Path) -> None:
        assert launch_mod.desired_config() == {"browser": {"browserName": "chromium"}}

    def test_agent_config_never_names_the_state_even_when_present(self, home: Path) -> None:
        """The agent's daemon runs inside the sandbox that masks the file: naming it
        there would make every browse fail on ENOENT, so the key must never appear."""
        mod.save_storage_state(
            mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}]))
        )
        assert launch_mod.desired_config() == {"browser": {"browserName": "chromium"}}
        path = launch_mod.write_config()
        assert path is not None
        assert "storageState" not in path.read_text(encoding="utf-8")

    def test_gateway_config_has_key_exactly_when_file_exists(self, home: Path) -> None:
        assert mod.gateway_config() == launch_mod.desired_config()
        mod.save_storage_state(
            mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}]))
        )
        browser = mod.gateway_config()["browser"]
        assert isinstance(browser, dict)
        assert browser["contextOptions"] == {"storageState": str(mod.storage_state_path())}
        assert browser["browserName"] == "chromium"

    def test_gateway_config_is_a_separate_file_that_converges(self, home: Path) -> None:
        mod.save_storage_state(
            mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}]))
        )
        path = mod.write_gateway_config()
        assert path is not None
        assert path != launch_mod.launch_config_path()
        assert "storageState" in path.read_text(encoding="utf-8")
        mod.clear_storage_state()
        mod.write_gateway_config()
        assert "storageState" not in path.read_text(encoding="utf-8")


# ── The state file is masked from every agent sandbox ──


def test_storage_state_leaf_is_hidden_and_tool_gated() -> None:
    from kiro_crew import sandbox, security

    assert mod.STORAGE_STATE_FILE in sandbox._CREW_HIDDEN_LEAVES
    assert mod.STORAGE_STATE_FILE in security._CREW_SECRET_LEAVES
    assert security.is_sensitive_path(f"~/.kiro/crew/{mod.STORAGE_STATE_FILE}") is True
    # The gateway config only carries a PATH, never a value: it stays visible.
    assert mod._GATEWAY_CONFIG_FILE not in sandbox._CREW_HIDDEN_LEAVES


# ── Live-session enumeration by lifecycle-root shape ──


def _fake_socket(home: Path, leaf: str) -> None:
    cli = home / "pw" / leaf / "s" / "cli"
    cli.mkdir(parents=True)
    (cli / f"0123456789abcdef-kc-{leaf}.sock").write_bytes(b"")


class TestLiveSessions:
    def test_sockets_under_generated_roots_are_sessions(self, home: Path) -> None:
        _fake_socket(home, "aaaa1111")
        _fake_socket(home, "bbbb2222")
        (home / "pw" / "cccc3333" / "s" / "cli").mkdir(parents=True)  # no socket
        (home / "pw" / "ui" / "s" / "cli").mkdir(parents=True)  # the gateway's own root
        (home / "pw" / "ui" / "s" / "cli" / "x-panel.sock").write_bytes(b"")
        sessions = mod._live_sessions()
        assert sorted(sessions) == ["kc-aaaa1111", "kc-bbbb2222"]
        env = sessions["kc-aaaa1111"]
        assert env[launch_mod.SESSION_ENV] == "kc-aaaa1111"
        assert env[launch_mod.SOCKETS_ENV] == str(home / "pw" / "aaaa1111" / "s")
        assert env[launch_mod.DAEMON_DIR_ENV] == str(home / "pw" / "aaaa1111" / "d")

    def test_falls_back_to_list_when_no_root_holds_a_socket(self, home: Path) -> None:
        with patch.object(mod, "_live_session_names", return_value=["kc-dddd4444"]):
            assert mod._live_sessions() == {"kc-dddd4444": {}}

    def test_live_session_names_filters_to_prefix(self, home: Path) -> None:
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "kc-aaaa1111 running\npanel-1 running\nkc-bbbb2222\n"
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={}),
            patch("subprocess.run", return_value=proc),
        ):
            assert mod._live_session_names() == ["kc-aaaa1111", "kc-bbbb2222"]


# ── hot_load / clear_live_sessions: best effort, truthful, never raise ──


class TestHotLoad:
    def test_no_cli_returns_note(self, home: Path) -> None:
        with patch.object(mod, "cli_command", return_value=None):
            result = mod.hot_load_into_live_sessions(mod.storage_state_path())
        assert result == {
            "loaded": [],
            "failed": {},
            "note": "playwright-cli is not installed",
        }

    def test_no_sessions_returns_note(self, home: Path) -> None:
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "_live_sessions", return_value={}),
        ):
            result = mod.hot_load_into_live_sessions(mod.storage_state_path())
        assert result["loaded"] == []
        assert result["failed"] == {}
        assert "note" in result

    def test_loads_with_each_sessions_env_and_reports_failures(self, home: Path) -> None:
        path = mod.storage_state_path()
        seen: dict[str, dict[str, str]] = {}

        def fake_run(argv, **kwargs):
            name = next(a for a in argv if a.startswith("-s=")).removeprefix("-s=")
            seen[name] = kwargs["env"]
            assert argv[-2:] == ["state-load", str(path)]
            proc = MagicMock()
            proc.returncode = 0 if name == "kc-aaaa1111" else 1
            proc.stdout = ""
            proc.stderr = "Error: ENOENT: no such file or directory"
            return proc

        sessions = {
            "kc-aaaa1111": {launch_mod.SOCKETS_ENV: "/s/a"},
            "kc-bbbb2222": {launch_mod.SOCKETS_ENV: "/s/b"},
        }
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={"PATH": "/bin"}),
            patch.object(mod, "_live_sessions", return_value=sessions),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = mod.hot_load_into_live_sessions(path)
        assert result["loaded"] == ["kc-aaaa1111"]
        assert "ENOENT" in result["failed"]["kc-bbbb2222"]
        assert "note" not in result
        assert seen["kc-aaaa1111"] == {"PATH": "/bin", launch_mod.SOCKETS_ENV: "/s/a"}
        assert seen["kc-bbbb2222"][launch_mod.SOCKETS_ENV] == "/s/b"

    def test_nothing_loaded_carries_a_note(self, home: Path) -> None:
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = ""
        proc.stderr = "boom"
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={}),
            patch.object(mod, "_live_sessions", return_value={"kc-aaaa1111": {}}),
            patch("subprocess.run", return_value=proc),
        ):
            result = mod.hot_load_into_live_sessions(mod.storage_state_path())
        assert result["loaded"] == []
        assert result["failed"] == {"kc-aaaa1111": "boom"}
        assert "new sessions" in result["note"]

    def test_never_raises_on_subprocess_error(self, home: Path) -> None:
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={}),
            patch.object(mod, "_live_sessions", return_value={"kc-aaaa1111": {}}),
            patch("subprocess.run", side_effect=OSError("nope")),
        ):
            result = mod.hot_load_into_live_sessions(mod.storage_state_path())
        assert result["loaded"] == []
        assert "kc-aaaa1111" in result["failed"]


class TestClearLiveSessions:
    def test_no_cli_and_no_sessions_carry_notes(self, home: Path) -> None:
        with patch.object(mod, "cli_command", return_value=None):
            assert mod.clear_live_sessions()["note"] == "playwright-cli is not installed"
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "_live_sessions", return_value={}),
        ):
            result = mod.clear_live_sessions()
        assert result["cleared"] == [] and result["failed"] == {} and "note" in result

    def test_runs_cookie_clear_per_session_and_reports(self, home: Path) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            proc = MagicMock()
            proc.returncode = 0 if "-s=kc-aaaa1111" in argv else 1
            proc.stdout = ""
            proc.stderr = "The browser 'kc-bbbb2222' is not open"
            return proc

        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={}),
            patch.object(
                mod, "_live_sessions", return_value={"kc-aaaa1111": {}, "kc-bbbb2222": {}}
            ),
            patch("subprocess.run", side_effect=fake_run),
        ):
            result = mod.clear_live_sessions()
        assert result == {
            "cleared": ["kc-aaaa1111"],
            "failed": {"kc-bbbb2222": "The browser 'kc-bbbb2222' is not open"},
        }
        assert all(argv[-1] == "cookie-clear" for argv in calls)

    def test_never_raises(self, home: Path) -> None:
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={}),
            patch.object(mod, "_live_sessions", return_value={"kc-aaaa1111": {}}),
            patch("subprocess.run", side_effect=OSError("nope")),
        ):
            assert "kc-aaaa1111" in mod.clear_live_sessions()["failed"]


# ── prewarm_session: gateway-owned daemon, fire-and-forget ──


_AGENT_ENV = {
    launch_mod.SESSION_ENV: "kc-deadbeef",
    launch_mod.SOCKETS_ENV: "/data/pw/deadbeef/s",
    launch_mod.DAEMON_DIR_ENV: "/data/pw/deadbeef/d",
}


def _import_one(home: Path) -> None:
    mod.save_storage_state(mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}])))


class TestPrewarm:
    def test_noop_without_state_file(self, home: Path) -> None:
        with patch.object(mod.threading, "Thread") as thread:
            assert mod.prewarm_session("kc-deadbeef", _AGENT_ENV) is False
        thread.assert_not_called()

    def test_noop_for_non_generated_name_or_missing_lifecycle_env(self, home: Path) -> None:
        _import_one(home)
        with patch.object(mod.threading, "Thread") as thread:
            assert mod.prewarm_session("chrome", _AGENT_ENV) is False
            assert (
                mod.prewarm_session("kc-deadbeef", {launch_mod.SESSION_ENV: "kc-deadbeef"}) is False
            )
        thread.assert_not_called()

    def test_operator_config_wins(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _import_one(home)
        monkeypatch.setenv(launch_mod.CONFIG_ENV, "/etc/theirs.json")
        with patch.object(mod.threading, "Thread") as thread:
            assert mod.prewarm_session("kc-deadbeef", _AGENT_ENV) is False
        thread.assert_not_called()

    def test_spawns_open_with_gateway_config_and_agent_lifecycle_env(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _import_one(home)
        monkeypatch.setenv(launch_mod.CONFIG_ENV, str(launch_mod.launch_config_path()))
        runs: list[tuple[list[str], dict[str, str]]] = []

        def fake_run(argv, **kwargs):
            runs.append((argv, kwargs["env"]))
            proc = MagicMock()
            proc.returncode = 0
            proc.stdout = proc.stderr = ""
            return proc

        started: list[threading.Thread] = []
        real_thread = threading.Thread

        def capture(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            started.append(thread)
            return thread

        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={"PATH": "/bin", "HOME": str(home)}),
            patch("subprocess.run", side_effect=fake_run),
            patch.object(mod.threading, "Thread", side_effect=capture),
        ):
            assert mod.prewarm_session("kc-deadbeef", _AGENT_ENV) is True
            for thread in started:
                thread.join(timeout=5)
        assert len(runs) == 1
        argv, env = runs[0]
        assert argv == ["node", "cli.js", "-s=kc-deadbeef", "open", "about:blank"]
        assert env[launch_mod.CONFIG_ENV] == str(mod.gateway_config_path())
        assert env[launch_mod.SESSION_ENV] == "kc-deadbeef"
        assert env[launch_mod.SOCKETS_ENV] == "/data/pw/deadbeef/s"
        assert env[launch_mod.DAEMON_DIR_ENV] == "/data/pw/deadbeef/d"
        assert env["KIROCREW_SPAWNED"] == "1"
        assert env["PATH"] == "/bin"
        # The config the daemon reads names the masked file; the agent's does not.
        gateway = json.loads(mod.gateway_config_path().read_text(encoding="utf-8"))
        assert gateway["browser"]["contextOptions"]["storageState"] == str(mod.storage_state_path())
        assert started[0].daemon is True

    def test_never_raises_and_never_blocks(self, home: Path) -> None:
        _import_one(home)
        gate = threading.Event()

        def slow_run(argv, **kwargs):
            gate.wait(5)
            raise OSError("nope")

        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={}),
            patch("subprocess.run", side_effect=slow_run),
        ):
            start = time.monotonic()
            assert mod.prewarm_session("kc-deadbeef", _AGENT_ENV) is True
            assert time.monotonic() - start < 2.0
            gate.set()
        with patch.object(mod, "cli_command", side_effect=RuntimeError("boom")):
            assert mod.prewarm_session("kc-deadbeef", _AGENT_ENV) is False


# ── HTTP handlers ──


@pytest.fixture()
def mock_sel():
    try:
        import kiro_crew.dashboard.handlers  # noqa: F401
    except ImportError:
        pytest.skip("dashboard handler deps not available locally")
    m = MagicMock()
    m.log_api_access = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


def _make_state(restricted: bool = False) -> MagicMock:
    state = MagicMock()
    # Empty owner_id -> the standalone-local path, where as_owner's default
    # ``local-app`` caller reads as the owner (the same shape NoConfiguredOwner
    # gives). A MagicMock owner_id would stringify to a non-empty value and deny.
    state.owner_id = ""
    state._restricted_keys = {"dashboard:guest"} if restricted else set()
    state._slots = {}
    # inherited_session_memory_mode reads these; keep them concrete so it does
    # not iterate a MagicMock (which would raise) before the _restricted_keys
    # check runs.
    state.context_builder = None
    state.subagents = None
    return state


@pytest.fixture()
def app(home: Path, mock_sel):
    from kiro_crew.dashboard.handlers import messaging

    application = web.Application()
    application.router.add_get("/api/browser/cookies", messaging.api_browser_cookies_get)
    application.router.add_post("/api/browser/cookies", messaging.api_browser_cookies_import)
    application.router.add_delete("/api/browser/cookies", messaging.api_browser_cookies_clear)
    as_owner(application)
    # as_owner installs NoConfiguredOwner only when there is no state; give a real
    # enough state so the restricted-session predicate has its maps.
    application["state"] = _make_state()
    return application


@pytest.mark.asyncio
async def test_status_absent_then_present(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/browser/cookies")
        assert resp.status == 200
        body = await resp.json()
        assert body["present"] is False
        assert body["summary"] is None
        assert body["config_path"].endswith("browser-storage-state.json")

        with patch(
            "kiro_crew.browser_cli.cookies.hot_load_into_live_sessions",
            return_value={"loaded": [], "failed": {}},
        ):
            imp = await client.post(
                "/api/browser/cookies",
                json={"content": json.dumps([{"name": "n", "value": "v", "domain": "d.com"}])},
            )
        assert imp.status == 200
        imp_body = await imp.json()
        assert imp_body["ok"] is True
        assert imp_body["summary"]["cookie_count"] == 1

        resp2 = await client.get("/api/browser/cookies")
        assert (await resp2.json())["present"] is True


@pytest.mark.asyncio
async def test_import_malformed_is_400(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/browser/cookies", json={"content": "not-a-cookie"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_cookies"


@pytest.mark.asyncio
async def test_import_missing_content_is_400(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/browser/cookies", json={"filename": "x"})
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_content"


@pytest.mark.asyncio
async def test_import_oversize_is_413(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        payload = json.dumps({"content": "x" * (mod.MAX_IMPORT_BYTES + 100)})
        resp = await client.post(
            "/api/browser/cookies",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 413


@pytest.mark.asyncio
async def test_clear_removes(app, home: Path) -> None:
    mod.save_storage_state(mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}])))
    mod.write_gateway_config()
    assert "storageState" in mod.gateway_config_path().read_text(encoding="utf-8")
    live = {"cleared": ["kc-aaaa1111"], "failed": {"kc-bbbb2222": "not open"}}
    async with TestClient(TestServer(app)) as client:
        with patch(
            "kiro_crew.browser_cli.cookies.clear_live_sessions", return_value=live
        ) as clear_live:
            resp = await client.delete("/api/browser/cookies")
        assert resp.status == 200
        # Live sessions are cleared too, and the outcome is reported, not hidden.
        assert (await resp.json()) == {"ok": True, "present": False, "live": live}
        clear_live.assert_called_once_with()
    assert not mod.storage_state_path().exists()
    assert "storageState" not in mod.gateway_config_path().read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_import_reports_hot_load_note_and_writes_gateway_config(app, home: Path) -> None:
    hot = {"loaded": [], "failed": {"kc-aaaa1111": "ENOENT"}, "note": "cannot load"}
    async with TestClient(TestServer(app)) as client:
        with patch("kiro_crew.browser_cli.cookies.hot_load_into_live_sessions", return_value=hot):
            resp = await client.post(
                "/api/browser/cookies",
                json={"content": json.dumps([{"name": "n", "value": "v", "domain": "d.com"}])},
            )
        assert resp.status == 200
        assert (await resp.json())["hot_load"] == hot
    gateway = json.loads(mod.gateway_config_path().read_text(encoding="utf-8"))
    assert gateway["browser"]["contextOptions"]["storageState"] == str(mod.storage_state_path())
    # The AGENT's config was not touched into naming the masked file.
    agent = launch_mod.launch_config_path()
    assert not agent.exists() or "storageState" not in agent.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_non_owner_forbidden(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        headers = {"X-Test-User": "someone-else"}
        assert (await client.get("/api/browser/cookies", headers=headers)).status == 403
        assert (
            await client.post("/api/browser/cookies", json={"content": "x"}, headers=headers)
        ).status == 403
        assert (await client.delete("/api/browser/cookies", headers=headers)).status == 403


@pytest.mark.asyncio
async def test_restricted_session_forbidden(home: Path, mock_sel) -> None:
    from kiro_crew.dashboard.handlers import messaging

    application = web.Application()
    application.router.add_get("/api/browser/cookies", messaging.api_browser_cookies_get)
    application.router.add_post("/api/browser/cookies", messaging.api_browser_cookies_import)
    application.router.add_delete("/api/browser/cookies", messaging.api_browser_cookies_clear)
    as_owner(application)
    application["state"] = _make_state(restricted=True)
    headers = {"X-Session-Key": "dashboard:guest"}
    async with TestClient(TestServer(application)) as client:
        # The status read names the sites a credential unlocks, so it is refused
        # on the SAME body shape as the mutations (the panel hides on one code).
        status = await client.get("/api/browser/cookies", headers=headers)
        assert status.status == 403
        assert (await status.json())["code"] == "restricted_session"
        imp = await client.post(
            "/api/browser/cookies",
            json={"content": json.dumps([{"name": "n", "domain": "d.com"}])},
            headers=headers,
        )
        assert imp.status == 403
        assert (await imp.json())["code"] == "restricted_session"
        clr = await client.delete("/api/browser/cookies", headers=headers)
        assert clr.status == 403
