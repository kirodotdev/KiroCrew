"""Backend tests for the imported-cookies feature.

Covers the three parser shapes and their normalisation (including the strict
``secure``/``httpOnly`` boolean parsing), expiry dropping and the size/count
limits, the owner-only 0600 storageState write, the engine-only launch config,
the value-free summary, the socket-shaped live-session enumeration, the
``cookie-set`` request the gateway writes to a daemon socket (and that no
process is spawned for it), the best-effort injection and clear and their
value-free failure reporting, the
``SessionWatcher`` (new sockets, re-import, reset, bounds, thread lifecycle),
and the three HTTP handlers (status, import, clear) through the aiohttp client
fixture including the non-owner 403, the restricted-session 403, and the shared
lock that serialises an overlapping import and clear.

No real ``playwright-cli`` is ever spawned or connected to: the daemon socket
is faked at ``_connect`` (one test drives a real ``socketpair``), and
``cli_command``/``cli_env``/``subprocess.run`` are faked for ``list`` and
``cookie-clear``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
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
from kiro_crew.testing.wait import async_wait_until, until_parked

#: Injection speaks to the daemon over an ``AF_UNIX`` stream socket; where the
#: platform has none (Windows CPython), :func:`mod._inject_into_session` answers
#: ``unsupported platform`` and the watcher never starts, so the tests that drive
#: a real injection have no subject there.
needs_af_unix = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="cookie injection needs AF_UNIX sockets"
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    # The module-level watcher is shared across tests; start each from a clean
    # record and never leave a thread running.
    mod._WATCHER.stop()
    mod._WATCHER.reset()
    yield h
    mod._WATCHER.stop()
    mod._WATCHER.reset()


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

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (True, True),
            (False, False),
            (None, False),
            ("true", True),
            ("TRUE", True),
            (" True ", True),
            ("1", True),
            ("false", False),
            ("False", False),
            ("0", False),
        ],
    )
    def test_boolean_flags_accept_booleans_and_their_string_spellings(
        self, home: Path, raw: object, expected: bool
    ) -> None:
        # Cookie-Editor sometimes exports "secure": "true" as a STRING; bool("false")
        # would be True, so the strings are parsed, not coerced.
        text = json.dumps([{"name": "n", "domain": "d.com", "secure": raw, "httpOnly": raw}])
        cookie = mod.parse_cookie_import(text)[0]
        assert cookie["secure"] is expected
        assert cookie["httpOnly"] is expected

    @pytest.mark.parametrize("raw", ["yes", "no", "", "2", 1, 0, 1.5, [], {}])
    @pytest.mark.parametrize("field", ["secure", "httpOnly"])
    def test_other_boolean_spellings_are_rejected(
        self, home: Path, raw: object, field: str
    ) -> None:
        text = json.dumps([{"name": "n", "domain": "d.com", field: raw}])
        with pytest.raises(mod.CookieImportError, match=f"invalid {field} value"):
            mod.parse_cookie_import(text)

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


# ── One config: the agent's launch config never names the masked file ──


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

    def test_no_gateway_side_config_or_prewarm_exists(self, home: Path) -> None:
        """Design A, invariants 1 and 2: the gateway never starts a daemon for an
        agent session and writes no config naming the hidden file. A revival of
        either would reopen the sandbox escape the redesign closed."""
        for name in (
            "prewarm_session",
            "_run_prewarm",
            "gateway_config",
            "gateway_config_path",
            "write_gateway_config",
            "_GATEWAY_CONFIG_FILE",
            "hot_load_into_live_sessions",
        ):
            assert not hasattr(mod, name), name
        mod.save_storage_state(
            mod.parse_cookie_import(json.dumps([{"name": "n", "domain": "d.com"}]))
        )
        leaves = sorted(p.name for p in home.iterdir())
        assert leaves == [mod.STORAGE_STATE_DIR]
        assert [p.name for p in (home / mod.STORAGE_STATE_DIR).iterdir()] == [
            mod.STORAGE_STATE_FILE
        ]


# ── The state file is masked from every agent sandbox ──


def test_storage_state_leaf_is_hidden_and_tool_gated() -> None:
    from kiro_crew import sandbox, security

    assert mod.STORAGE_STATE_DIR in sandbox._CREW_HIDDEN_LEAVES
    assert mod.STORAGE_STATE_DIR in security._CREW_SECRET_LEAVES
    inside = f"~/.kiro/crew/{mod.STORAGE_STATE_DIR}/{mod.STORAGE_STATE_FILE}"
    assert security.is_sensitive_path(inside) is True


def test_storage_state_dir_is_precreated_so_its_mask_always_mounts() -> None:
    """A mask over an absent name is skipped at spawn, so a sandbox started before
    the first import would see the cookies written later. The directory is made
    before every spawn instead, and the file lives inside it."""
    from kiro_crew import sandbox

    assert mod.STORAGE_STATE_DIR in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert mod.storage_state_path().parent.name == mod.STORAGE_STATE_DIR


# ── Live-session enumeration by lifecycle-root shape ──


def _fake_socket(home: Path, leaf: str, name: str = "0123456789abcdef") -> Path:
    cli = home / "pw" / leaf / "s" / "cli"
    cli.mkdir(parents=True, exist_ok=True)
    sock = cli / f"{name}.sock"
    sock.write_bytes(b"")
    return sock


def _key(sock: Path) -> mod.SocketKey:
    st = sock.stat()
    return (str(sock), st.st_ino, st.st_ctime_ns)


class TestLiveSessions:
    def test_sockets_under_generated_roots_are_sessions(self, home: Path) -> None:
        a = _fake_socket(home, "aaaa1111")
        b = _fake_socket(home, "bbbb2222")
        (home / "pw" / "cccc3333" / "s" / "cli").mkdir(parents=True)  # no socket
        (home / "pw" / "ui" / "s" / "cli").mkdir(parents=True)  # the gateway's own root
        (home / "pw" / "ui" / "s" / "cli" / "x-panel.sock").write_bytes(b"")
        sockets = mod._live_sockets()
        assert sockets == {"kc-aaaa1111": _key(a), "kc-bbbb2222": _key(b)}
        sessions = mod._live_sessions()
        assert sorted(sessions) == ["kc-aaaa1111", "kc-bbbb2222"]
        env = sessions["kc-aaaa1111"]
        assert env[launch_mod.SESSION_ENV] == "kc-aaaa1111"
        assert env[launch_mod.SOCKETS_ENV] == str(home / "pw" / "aaaa1111" / "s")
        assert env[launch_mod.DAEMON_DIR_ENV] == str(home / "pw" / "aaaa1111" / "d")

    def test_socket_key_is_path_inode_and_ctime(self, home: Path) -> None:
        """A daemon restart makes a NEW socket file; the key must change with it
        so the watcher injects into the new daemon rather than trusting the path."""
        sock = _fake_socket(home, "aaaa1111")
        first = mod._live_sockets()["kc-aaaa1111"]
        # Swap a NEW file in under the same path while the old one still exists,
        # so the two inodes are distinct by construction.
        fresh = sock.with_name("fresh.tmp")
        fresh.write_bytes(b"")
        os.replace(fresh, sock)
        second = mod._live_sockets()["kc-aaaa1111"]
        assert first[0] == second[0]
        assert first != second

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


# ── cookie-set request: what actually crosses the daemon socket ──


class TestCookieSetArgs:
    def test_full_cookie(self) -> None:
        cookie = {
            "name": "sid",
            "value": "-abc=def",
            "domain": ".example.com",
            "path": "/",
            "expires": 1900000000.0,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Strict",
        }
        # The object the CLI client builds from its argv with minimist and sends
        # as ``params.args``: the command and its two positionals under ``_``,
        # every option a key the command's strict schema declares.
        assert mod._cookie_set_args(cookie) == {
            "_": ["cookie-set", "sid", "-abc=def"],
            "domain": ".example.com",
            "path": "/",
            "sameSite": "Strict",
            "expires": 1900000000,
            "httpOnly": True,
            "secure": True,
        }

    def test_session_cookie_omits_expires_and_flags(self) -> None:
        cookie = {"name": "n", "value": "v", "domain": "d.com", "path": "/x", "expires": -1.0}
        assert mod._cookie_set_args(cookie) == {
            "_": ["cookie-set", "n", "v"],
            "domain": "d.com",
            "path": "/x",
            "sameSite": "Lax",
        }

    def test_fractional_expiry_is_kept(self) -> None:
        cookie = {"name": "n", "value": "v", "domain": "d.com", "expires": 1900000000.5}
        assert mod._cookie_set_args(cookie)["expires"] == 1900000000.5


# ── injection over the daemon socket: best effort, value-free, never raise ──


def _import_cookies(home: Path, *names: str) -> list[dict]:
    text = json.dumps(
        [{"name": n, "value": f"secret-{n}", "domain": "d.com", "secure": True} for n in names]
    )
    cookies = mod.parse_cookie_import(text)
    mod.save_storage_state(cookies)
    return cookies


class _FakeSock:
    """One fake control-socket connection, answering each request line in turn."""

    def __init__(self, daemon: "_FakeDaemon", path: str) -> None:
        self.daemon = daemon
        self.path = path
        self.out = bytearray()
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        assert timeout > 0

    def sendall(self, data: bytes) -> None:
        message = json.loads(data.decode("utf-8"))
        self.daemon.messages.append((self.path, message))
        mode = self.daemon.modes.get(self.path, "ok")
        if mode == "timeout":
            raise TimeoutError("timed out")
        value = message["params"]["args"]["_"][2]
        if mode == "error":
            reply = {"id": message["id"], "error": f"cookie {value} rejected"}
        elif mode == "is_error":
            reply = {"id": message["id"], "result": {"isError": True, "text": value}}
        elif mode == "wrong_id":
            reply = {"id": message["id"] + 7, "result": {"isError": False, "text": ""}}
        elif mode == "garbage":
            self.out += b"not json\n"
            return
        else:
            # The daemon's reply echoes the cookie it was handed.
            reply = {"id": message["id"], "result": {"isError": False, "text": value}}
        self.out += json.dumps(reply).encode("utf-8") + b"\n"

    def recv(self, size: int) -> bytes:
        chunk, self.out = bytes(self.out[:size]), self.out[size:]
        return chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_FakeSock":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _FakeDaemon:
    """Stands in for every daemon control socket (patched over ``mod._connect``).

    ``modes`` maps a socket path to how its daemon answers: ``ok`` (default),
    ``error``, ``is_error``, ``wrong_id``, ``garbage``, ``timeout`` (the reply
    never comes), ``refused`` (the connect fails) or ``connect_timeout``.
    """

    def __init__(self, modes: dict[str, str] | None = None) -> None:
        self.modes = modes or {}
        self.messages: list[tuple[str, dict]] = []
        self.connections: list[_FakeSock] = []

    def __call__(self, path: str, timeout: float) -> _FakeSock:
        mode = self.modes.get(path, "ok")
        if mode == "refused":
            raise ConnectionRefusedError("refused")
        if mode == "connect_timeout":
            raise TimeoutError("timed out")
        sock = _FakeSock(self, path)
        self.connections.append(sock)
        return sock

    def path_messages(self, path: Path | str) -> list[dict]:
        return [m for p, m in self.messages if p == str(path)]


def _no_spawn(*args: object, **kwargs: object) -> None:
    raise AssertionError("cookie injection must not spawn a process")


class TestInjectIntoLiveSessions:
    def test_no_file_and_no_sessions_return_notes(self, home: Path) -> None:
        assert "no imported cookies" in mod.inject_into_live_sessions()["note"]
        _import_cookies(home, "a")
        result = mod.inject_into_live_sessions()
        assert result["loaded"] == [] and result["failed"] == {}
        assert "new sessions" in result["note"]

    @needs_af_unix
    def test_one_request_per_cookie_per_session_over_its_own_socket(self, home: Path) -> None:
        _import_cookies(home, "a", "b")
        a = _fake_socket(home, "aaaa1111")
        b = _fake_socket(home, "bbbb2222")
        daemon = _FakeDaemon({str(b): "error"})
        with (
            patch.object(mod, "_connect", side_effect=daemon),
            patch("subprocess.run", side_effect=_no_spawn),
            patch("subprocess.Popen", side_effect=_no_spawn),
        ):
            result = mod.inject_into_live_sessions()
        assert result["loaded"] == ["kc-aaaa1111"]
        assert result["failed"] == {"kc-bbbb2222": "the browser refused the cookie"}
        assert "note" not in result
        a_messages = daemon.path_messages(a)
        assert [m["id"] for m in a_messages] == [1, 2]
        for message in a_messages:
            assert message["method"] == "run"
            args = message["params"]["args"]
            assert args["_"][0] == "cookie-set"
            assert args["secure"] is True
            assert args["domain"] == "d.com"
        assert {m["params"]["args"]["_"][1] for m in a_messages} == {"a", "b"}
        assert {m["params"]["args"]["_"][2] for m in a_messages} == {"secret-a", "secret-b"}
        # One connection per session, closed afterwards.
        assert len(daemon.connections) == 2 and all(c.closed for c in daemon.connections)
        # The failing session stopped at its FIRST cookie.
        assert len(daemon.path_messages(b)) == 1

    @needs_af_unix
    @pytest.mark.parametrize(
        ("mode", "reason"),
        [
            ("error", "the browser refused the cookie"),
            ("is_error", "the browser refused the cookie"),
            ("wrong_id", "malformed reply"),
            ("garbage", "malformed reply"),
            ("timeout", "timed out"),
            ("connect_timeout", "timed out"),
            ("refused", "ConnectionRefusedError"),
        ],
    )
    def test_failure_reasons_are_fixed_and_never_carry_a_value(
        self, home: Path, mode: str, reason: str
    ) -> None:
        """The daemon's reply echoes the cookie; a reason that quoted it (or the
        request) could put a value in the HTTP response or a log line."""
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        with patch.object(mod, "_connect", side_effect=_FakeDaemon({str(sock): mode})):
            result = mod.inject_into_live_sessions()
        assert result["loaded"] == []
        assert result["failed"] == {"kc-aaaa1111": reason}
        assert "secret" not in json.dumps(result)
        assert "new sessions" in result["note"]

    @needs_af_unix
    def test_a_trickled_reply_cannot_outlive_the_exchange_deadline(self, home: Path) -> None:
        """A peer that sends a byte at a time with no newline gets a fresh
        per-receive timeout unless the absolute deadline is re-applied before every
        receive; with it, the exchange ends at the deadline with a fixed reason."""
        _import_cookies(home, "a")
        clock = [1000.0]
        timeouts: list[float] = []

        class _Trickle:
            def settimeout(self, timeout: float) -> None:
                timeouts.append(timeout)

            def sendall(self, data: bytes) -> None:
                pass

            def recv(self, size: int) -> bytes:
                clock[0] += 10.0  # each byte lands just inside a per-call timeout
                return b"x"

            def __enter__(self) -> "_Trickle":
                return self

            def __exit__(self, *exc: object) -> None:
                pass

        fake_time = MagicMock(wraps=time)
        fake_time.monotonic = lambda: clock[0]
        with (
            patch.object(mod, "time", fake_time),
            patch.object(mod, "_connect", return_value=_Trickle()),
        ):
            reason = mod._inject_into_session("/unused.sock", mod._load_cookies() or [])
        assert reason == "timed out"
        # Two receives fit in the 15 s budget; every timeout handed out shrinks.
        assert timeouts == sorted(timeouts, reverse=True)
        assert all(t <= mod._CLI_TIMEOUT_S for t in timeouts)
        assert clock[0] - 1000.0 <= mod._CLI_TIMEOUT_S + 10.0

    def test_a_missing_socket_file_is_a_value_free_failure(self, home: Path) -> None:
        """The real connect against a socket path nothing listens on."""
        _import_cookies(home, "a")
        if not hasattr(__import__("socket"), "AF_UNIX"):
            pytest.skip("no AF_UNIX on this platform")
        reason = mod._inject_into_session(str(home / "absent.sock"), mod._load_cookies() or [])
        assert reason in {"FileNotFoundError", "ConnectionRefusedError", "OSError"}

    @pytest.mark.skipif(IS_WINDOWS, reason="AF_UNIX socketpair")
    def test_real_socket_round_trip_speaks_the_daemon_protocol(self, home: Path) -> None:
        """A real stream socket on both ends: requests are newline-delimited JSON,
        replies may arrive split across reads, and the exchange completes."""
        import socket

        _import_cookies(home, "a", "b")
        ours, theirs = socket.socketpair()
        received: list[dict] = []

        def daemon() -> None:
            buf = b""
            with theirs:
                while len(received) < 2:
                    data = theirs.recv(4096)
                    if not data:
                        return
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        message = json.loads(line)
                        received.append(message)
                        reply = json.dumps(
                            {"id": message["id"], "result": {"isError": False, "text": "ok"}}
                        ).encode()
                        # Split the reply across two writes to exercise buffering.
                        theirs.sendall(reply[:5])
                        theirs.sendall(reply[5:] + b"\n")

        thread = threading.Thread(target=daemon, daemon=True)
        thread.start()
        with patch.object(mod, "_connect", return_value=ours):
            assert mod._inject_into_session("/unused.sock", mod._load_cookies() or []) == ""
        thread.join(5)
        assert [m["id"] for m in received] == [1, 2]
        assert {m["params"]["args"]["_"][1] for m in received} == {"a", "b"}

    @needs_af_unix
    def test_records_reached_sockets_with_the_watcher(self, home: Path) -> None:
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        daemon = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=daemon):
            assert mod.inject_into_live_sessions()["loaded"] == ["kc-aaaa1111"]
            assert mod._WATCHER.injected() == {_key(sock)}
            # The watcher's next tick has nothing left to do for that socket.
            before = len(daemon.messages)
            mod._WATCHER.tick()
            assert len(daemon.messages) == before


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

    def test_resets_the_watcher_record(self, home: Path) -> None:
        mod._WATCHER.mark_injected(("/x.sock", 1, 2))
        with patch.object(mod, "cli_command", return_value=None):
            mod.clear_live_sessions()
        assert mod._WATCHER.injected() == frozenset()

    def test_never_raises(self, home: Path) -> None:
        with (
            patch.object(mod, "cli_command", return_value=["node", "cli.js"]),
            patch.object(mod, "cli_env", return_value={}),
            patch.object(mod, "_live_sessions", return_value={"kc-aaaa1111": {}}),
            patch("subprocess.run", side_effect=OSError("nope")),
        ):
            assert "kc-aaaa1111" in mod.clear_live_sessions()["failed"]


# ── SessionWatcher: cookies for daemons that appear after the import ──


@needs_af_unix
class TestSessionWatcher:
    def test_no_file_means_no_work_and_a_clean_record(self, home: Path) -> None:
        watcher = mod.SessionWatcher()
        watcher.mark_injected(("/x.sock", 1, 2))
        _fake_socket(home, "aaaa1111")
        daemon = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=daemon):
            assert watcher.tick() == 0
        assert daemon.messages == []
        assert watcher.injected() == frozenset()

    def test_injects_new_sockets_once_and_skips_them_afterwards(self, home: Path) -> None:
        _import_cookies(home, "a", "b")
        a = _fake_socket(home, "aaaa1111")
        watcher = mod.SessionWatcher()
        fake = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=fake):
            assert watcher.tick() == 1
            assert len(fake.path_messages(a)) == 2  # one cookie-set per cookie
            assert fake.messages[0][0] == str(home / "pw" / "aaaa1111" / "s" / "cli" / a.name)
            assert watcher.injected() == {_key(a)}
            assert watcher.tick() == 0
            assert len(fake.messages) == 2
            # A second session appears: only it is injected.
            b = _fake_socket(home, "bbbb2222")
            assert watcher.tick() == 1
            assert len(fake.path_messages(b)) == 2
            assert len(fake.path_messages(a)) == 2
            assert watcher.injected() == {_key(a), _key(b)}

    def test_a_new_daemon_socket_for_the_same_session_is_injected_again(self, home: Path) -> None:
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        watcher = mod.SessionWatcher()
        fake = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=fake):
            assert watcher.tick() == 1
            sock.unlink()
            _fake_socket(home, "aaaa1111", name="fedcba9876543210")
            assert watcher.tick() == 1
        assert len(fake.messages) == 2

    def test_reimport_changes_the_file_and_reinjects_everything(self, home: Path) -> None:
        _import_cookies(home, "a")
        _fake_socket(home, "aaaa1111")
        watcher = mod.SessionWatcher()
        fake = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=fake):
            assert watcher.tick() == 1
            assert watcher.tick() == 0
            _import_cookies(home, "a", "b", "c")  # a new size changes the signature
            assert watcher.tick() == 1
        assert len(fake.messages) == 1 + 3

    def test_reset_forgets_the_record(self, home: Path) -> None:
        _import_cookies(home, "a")
        _fake_socket(home, "aaaa1111")
        watcher = mod.SessionWatcher()
        fake = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=fake):
            assert watcher.tick() == 1
            watcher.reset()
            assert watcher.injected() == frozenset()
            assert watcher.tick() == 1
        assert len(fake.messages) == 2

    def test_failed_sockets_are_retried_a_bounded_number_of_times(self, home: Path) -> None:
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        watcher = mod.SessionWatcher()
        fake = _FakeDaemon({str(sock): "error"})
        with patch.object(mod, "_connect", side_effect=fake):
            for _ in range(mod.WATCH_MAX_ATTEMPTS + 3):
                assert watcher.tick() == 0
        assert len(fake.messages) == mod.WATCH_MAX_ATTEMPTS
        assert watcher.injected() == frozenset()

    def test_a_tick_is_bounded_to_a_few_sessions(self, home: Path) -> None:
        _import_cookies(home, "a")
        leaves = [f"{i:08x}" for i in range(mod.WATCH_MAX_SESSIONS_PER_TICK + 3)]
        for leaf in leaves:
            _fake_socket(home, leaf)
        watcher = mod.SessionWatcher()
        fake = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=fake):
            assert watcher.tick() == mod.WATCH_MAX_SESSIONS_PER_TICK
            assert watcher.tick() == 3
            assert watcher.tick() == 0
        assert len(watcher.injected()) == len(leaves)

    def test_gone_sockets_leave_the_record(self, home: Path) -> None:
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        watcher = mod.SessionWatcher()
        with patch.object(mod, "_connect", side_effect=_FakeDaemon()):
            watcher.tick()
            assert watcher.injected() == {_key(sock)}
            sock.unlink()
            _fake_socket(home, "bbbb2222")
            watcher.tick()
        assert all(
            key[0].endswith("bbbb2222/s/cli/0123456789abcdef.sock") for key in watcher.injected()
        )

    def test_a_tick_reads_and_injects_under_the_transaction_lock(self, home: Path) -> None:
        """A clear holds the same lock across delete and ``cookie-clear``, so a tick
        can never inject a set it loaded before the clear after the clear returned."""
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        held: list[bool] = []
        daemon = _FakeDaemon()

        def connect(path: str, timeout: float) -> _FakeSock:
            held.append(mod.transaction().locked())
            return daemon(path, timeout)

        watcher = mod.SessionWatcher()
        with patch.object(mod, "_connect", side_effect=connect):
            assert watcher.tick() == 1
        assert held == [True]
        assert not mod.transaction().locked()
        assert daemon.path_messages(sock)

    def test_a_tick_never_raises(self, home: Path) -> None:
        _import_cookies(home, "a")
        _fake_socket(home, "aaaa1111")
        watcher = mod.SessionWatcher()
        with patch.object(mod, "_live_sockets", side_effect=RuntimeError("boom")):
            assert watcher.tick() == 0

    def test_thread_lifecycle_is_idempotent_and_off_the_loop(self, home: Path) -> None:
        _import_cookies(home, "a")
        _fake_socket(home, "aaaa1111")
        ticked = threading.Event()
        watcher = mod.SessionWatcher(interval=0.01)
        fake = _FakeDaemon()

        def run(path, timeout):
            ticked.set()
            return fake(path, timeout)

        with patch.object(mod, "_connect", side_effect=run):
            watcher.start()
            first = watcher._thread
            watcher.start()  # idempotent
            assert watcher._thread is first
            assert watcher.running
            assert first is not None and first.daemon is True
            assert first.name == "browser-cookie-watcher"
            assert ticked.wait(5)
            watcher.stop()
            watcher.stop()  # idempotent
            assert not watcher.running
        assert len(fake.messages) == 1

    def test_module_singleton_start_stop(self, home: Path) -> None:
        mod.start_session_watcher()
        assert not mod._WATCHER.running  # nothing imported: no thread
        _import_cookies(home, "a")
        mod.start_session_watcher()
        assert mod._WATCHER.running
        mod.stop_session_watcher()
        assert not mod._WATCHER.running
        mod.stop_session_watcher()


def test_no_af_unix_means_no_watcher_and_a_fixed_reason(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without AF_UNIX nothing can reach a daemon: the import still saves, the
    watcher never starts, and the live outcome is a fixed, value-free reason."""
    monkeypatch.delattr(mod.socket, "AF_UNIX", raising=False)
    _import_cookies(home, "a")
    assert mod.storage_state_path().is_file()
    mod.start_session_watcher()
    assert not mod._WATCHER.running
    assert mod._inject_into_session("/unused.sock", mod._load_cookies() or []) == (
        "unsupported platform"
    )


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
        assert Path(body["config_path"]).parts[-2:] == (
            mod.STORAGE_STATE_DIR,
            mod.STORAGE_STATE_FILE,
        )

        with patch(
            "kiro_crew.browser_cli.cookies.inject_into_live_sessions",
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
    live = {"cleared": ["kc-aaaa1111"], "failed": {"kc-bbbb2222": "not open"}}
    held: list[bool] = []

    def clear_locked() -> dict:
        held.append(mod.transaction().locked())
        return live

    async with TestClient(TestServer(app)) as client:
        with patch(
            "kiro_crew.browser_cli.cookies.clear_live_sessions", side_effect=clear_locked
        ) as clear_live:
            resp = await client.delete("/api/browser/cookies")
        assert resp.status == 200
        # Live sessions are cleared too, and the outcome is reported, not hidden.
        assert (await resp.json()) == {"ok": True, "present": False, "live": live}
        clear_live.assert_called_once_with()
    assert not mod.storage_state_path().exists()
    # Delete and cookie-clear run under the lock a watcher tick takes.
    assert held == [True]


@pytest.mark.asyncio
async def test_import_injects_live_sessions_and_reports_the_note(app, home: Path) -> None:
    hot = {"loaded": [], "failed": {"kc-aaaa1111": "exit status 1"}, "note": "nothing reached"}
    held: list[bool] = []

    def inject_locked() -> dict:
        held.append(mod.transaction().locked())
        return hot

    async with TestClient(TestServer(app)) as client:
        with patch(
            "kiro_crew.browser_cli.cookies.inject_into_live_sessions", side_effect=inject_locked
        ) as inject:
            resp = await client.post(
                "/api/browser/cookies",
                json={"content": json.dumps([{"name": "n", "value": "v", "domain": "d.com"}])},
            )
        assert resp.status == 200
        assert (await resp.json())["hot_load"] == hot
        # Injection runs AFTER the file is written, from the stored state.
        inject.assert_called_once_with()
    assert held == [True]  # save and inject run under the watcher's lock
    assert mod.storage_state_path().exists()
    # The first import starts the watcher, so later sessions get the cookies.
    assert mod._WATCHER.running is hasattr(socket, "AF_UNIX")
    # Nothing but the state file appears under the data home: no config the
    # agent or its daemon reads ever names it.
    agent = launch_mod.launch_config_path()
    assert not agent.exists() or "storageState" not in agent.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_import_and_clear_are_serialised_by_one_lock(app, home: Path) -> None:
    """An overlapping POST and DELETE must not interleave at the to_thread
    boundary: with the lock, the DELETE's delete+clear runs only after the POST's
    save+inject has finished (or the reverse), so a cleared state can never be
    left active in the live sessions."""
    from kiro_crew.dashboard.handlers import messaging

    # A lock left by another test's loop must not be reused on this one.
    messaging._cookie_lock = None
    events: list[str] = []
    import_started = threading.Event()
    release_import = threading.Event()

    def slow_inject():
        events.append("import:inject:start")
        import_started.set()
        assert release_import.wait(5)
        events.append("import:inject:end")
        return {"loaded": [], "failed": {}}

    def clear_live():
        events.append("clear:live")
        return {"cleared": [], "failed": {}}

    async with TestClient(TestServer(app)) as client:
        with (
            patch(
                "kiro_crew.browser_cli.cookies.inject_into_live_sessions", side_effect=slow_inject
            ),
            patch("kiro_crew.browser_cli.cookies.clear_live_sessions", side_effect=clear_live),
        ):
            post = asyncio.ensure_future(
                client.post(
                    "/api/browser/cookies",
                    json={"content": json.dumps([{"name": "n", "value": "v", "domain": "d.com"}])},
                )
            )
            # Wait until the import is inside its transaction, then fire the clear.
            await async_wait_until(import_started.is_set, describe=lambda: events)
            delete = asyncio.ensure_future(client.delete("/api/browser/cookies"))
            # The DELETE is parked on the lock the import holds, so the clear has
            # NOT run.
            await until_parked(messaging._get_cookie_lock())
            assert events == ["import:inject:start"]
            assert mod.storage_state_path().exists()
            release_import.set()
            post_resp, delete_resp = await asyncio.gather(post, delete)
    assert post_resp.status == 200 and delete_resp.status == 200
    assert events == ["import:inject:start", "import:inject:end", "clear:live"]
    assert not mod.storage_state_path().exists()


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
