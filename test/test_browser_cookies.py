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
is faked at ``_connect`` (one test drives a real ``socketpair``), for the
``cookie-set`` of an import and the ``cookie-clear`` of a clear alike.
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
from typing import Any, Iterator
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
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    h = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(h))
    # A host where the launch puts agent sockets under the scanned roots: a
    # generated session name and a CLI that honours the lifecycle hooks. The
    # tests that pin the other hosts override these.
    monkeypatch.delenv(launch_mod.SESSION_ENV, raising=False)
    monkeypatch.delenv(launch_mod.SOCKETS_ENV, raising=False)
    monkeypatch.delenv(launch_mod.DAEMON_DIR_ENV, raising=False)
    monkeypatch.setattr(
        launch_mod, "cli_lifecycle_env_support", lambda: (launch_mod.SeamSupport.SUPPORTED, "")
    )
    # Keep ordinary cases independent of pytest's long temporary-directory names.
    monkeypatch.setattr(launch_mod, "_UNIX_SOCKET_PATH_MAX_BYTES", 10_000)
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

    def test_netscape_cookie_with_an_empty_value_is_kept(self, home: Path) -> None:
        """An empty value is a trailing tab, which curl and the export extensions
        write; stripping the whole line would leave six columns and drop it."""
        text = (
            ".example.com\tTRUE\t/\tTRUE\t0\tflag\t\r\n"
            "#HttpOnly_.example.com\tTRUE\t/\tTRUE\t0\tmarker\t\n"
        )
        cookies = mod.parse_cookie_import(text)
        assert [(c["name"], c["value"]) for c in cookies] == [("flag", ""), ("marker", "")]
        assert cookies[1]["httpOnly"] is True


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
        summary = mod.storage_state_summary()
        assert summary is not None
        assert summary["earliest_expiry"] is None

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


def _make_unix_socket(path: Path) -> Path:
    """Leave a real (unlistened) ``AF_UNIX`` socket file at *path*.

    Discovery accepts only socket inodes, so a plain file cannot stand in. The
    socket is bound by its bare name from inside its directory, because
    ``tmp_path`` can exceed the AF_UNIX path budget.
    """
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no AF_UNIX on this platform")
    cwd = os.getcwd()
    os.chdir(path.parent)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.bind(path.name)
    finally:
        os.chdir(cwd)
    return path


def _fake_socket(home: Path, leaf: str, name: str = "0123456789abcdef") -> Path:
    cli = home / "pw" / leaf / "s" / "cli"
    cli.mkdir(parents=True, exist_ok=True)
    return _make_unix_socket(cli / f"{name}.sock")


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

    def test_every_named_browser_of_a_session_is_its_own_entry(self, home: Path) -> None:
        """An agent that opens a second named browser (``-s=<name>``) gets a second
        socket under the same session root. Both are returned under distinct
        names, so an import or a clear cannot reach only the newer one."""
        first = _fake_socket(home, "aaaa1111", "first")
        second = _fake_socket(home, "aaaa1111", "second")
        only = _fake_socket(home, "bbbb2222")
        assert mod._live_sockets() == {
            "kc-aaaa1111/first": _key(first),
            "kc-aaaa1111/second": _key(second),
            "kc-bbbb2222": _key(only),
        }

    def test_one_stem_under_two_roots_is_two_entries(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session started before and after the operator root was set holds a
        socket of the same stem under each root; neither may overwrite the other."""
        operator = tmp_path / "operator-root"
        monkeypatch.setenv(launch_mod.SOCKETS_ENV, str(operator))
        default = _fake_socket(home, "aaaa1111")
        cli = launch_mod.socket_dir("kc-aaaa1111", operator) / "cli"
        cli.mkdir(parents=True)
        moved = _make_unix_socket(cli / default.name)
        assert sorted(mod._live_sockets().values()) == sorted([_key(default), _key(moved)])

    def test_socket_key_is_path_inode_and_ctime(self, home: Path) -> None:
        """A daemon restart makes a NEW socket file; the key must change with it
        so the watcher injects into the new daemon rather than trusting the path."""
        sock = _fake_socket(home, "aaaa1111")
        first = mod._live_sockets()["kc-aaaa1111"]
        # Swap a NEW file in under the same path while the old one still exists,
        # so the two inodes are distinct by construction.
        fresh = _make_unix_socket(sock.with_name("fresh.tmp"))
        os.replace(fresh, sock)
        second = mod._live_sockets()["kc-aaaa1111"]
        assert first[0] == second[0]
        assert first != second

    def test_an_operator_socket_root_is_scanned_as_the_launch_resolves_it(
        self, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The agent launch honours an absolute foreign ``SOCKETS_ENV`` as the BASE
        of every session's socket root, so a session started under it lives at
        ``<operator>/<8hex>/s``; discovery must look there as well as under the
        default root, or an import and the watcher never reach that browser."""
        operator = tmp_path / "operator-root"
        monkeypatch.setenv(launch_mod.SOCKETS_ENV, str(operator))
        default = _fake_socket(home, "aaaa1111")
        cli = launch_mod.socket_dir("kc-bbbb2222", operator) / "cli"
        cli.mkdir(parents=True)
        custom = _make_unix_socket(cli / "0123456789abcdef.sock")
        assert mod._live_sockets() == {"kc-aaaa1111": _key(default), "kc-bbbb2222": _key(custom)}

    @pytest.mark.skipif(IS_WINDOWS, reason="symlinks and AF_UNIX socket files")
    def test_a_symlink_or_plain_file_is_never_a_session_socket(
        self, home: Path, tmp_path: Path
    ) -> None:
        """The session roots are writable by the agent: a symlink planted at
        ``<8hex>/s/cli/a.sock`` pointing at another browser's socket (the owner's
        panel browser) must not be followed, or an import or clear writes into
        that browser. A plain file named ``*.sock`` is no socket either."""
        real = _fake_socket(home, "aaaa1111")
        target = _make_unix_socket(tmp_path / "panel.sock")
        cli = home / "pw" / "bbbb2222" / "s" / "cli"
        cli.mkdir(parents=True)
        (cli / "a.sock").symlink_to(target)
        plain = home / "pw" / "cccc3333" / "s" / "cli"
        plain.mkdir(parents=True)
        (plain / "a.sock").write_bytes(b"")
        assert mod._live_sockets() == {"kc-aaaa1111": _key(real)}

    @pytest.mark.parametrize("link_at", ["session", "s", "cli"])
    def test_a_symlinked_directory_on_the_way_is_never_followed(
        self, home: Path, tmp_path: Path, link_at: str
    ) -> None:
        """The directories above the socket are agent-writable as well: a
        session, ``s`` or ``cli`` directory that is a symlink into another tree
        holding a real socket must not lead the walk there."""
        real = _fake_socket(home, "aaaa1111")
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "s" / "cli").mkdir(parents=True)
        _make_unix_socket(elsewhere / "s" / "cli" / "a.sock")
        session = home / "pw" / "bbbb2222"
        if link_at == "session":
            session.symlink_to(elsewhere)
        elif link_at == "s":
            session.mkdir(parents=True)
            (session / "s").symlink_to(elsewhere / "s")
        else:
            (session / "s").mkdir(parents=True)
            (session / "s" / "cli").symlink_to(elsewhere / "s" / "cli")
        assert mod._live_sockets() == {"kc-aaaa1111": _key(real)}

    def test_an_inherited_session_root_is_not_an_operator_root(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One of our own ``<root>/<8hex>/s`` roots arriving by inheritance is not
        an operator base (the launch regenerates it), so it adds no root."""
        monkeypatch.setenv(launch_mod.SOCKETS_ENV, str(home / "pw" / "aaaa1111" / "s"))
        assert mod._lifecycle_roots() == [home / "pw"]
        monkeypatch.setenv(launch_mod.SOCKETS_ENV, "relative/root")
        assert mod._lifecycle_roots() == [home / "pw"]


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
        positionals = message["params"]["args"]["_"]
        value = positionals[2] if len(positionals) > 2 else ""
        if mode == "refuse_a":
            # Refuse only the cookie named "a" (Chromium refusing SameSite=None
            # without Secure), answer every other request.
            mode = "error" if positionals[1:2] == ["a"] else "ok"
        if mode == "deep":
            # Below the byte cap, deeper than the JSON parser recurses.
            self.out += b"[" * 100_000 + b"]" * 100_000 + b"\n"
            return
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
    ``error``, ``is_error``, ``refuse_a`` (refuses only cookie ``a``),
    ``wrong_id``, ``garbage``, ``deep`` (a reply nested past the parser's
    recursion limit), ``timeout`` (the reply never comes), ``refused`` (the
    connect fails: a socket file that outlived its daemon), ``gone`` (the file
    vanished before the connect), ``connect_oserror``, ``connect_timeout`` or
    ``accept_and_stall`` (calls ``on_connect``, then answers normally).
    """

    def __init__(self, modes: dict[str, str] | None = None) -> None:
        self.modes = modes or {}
        self.on_connect: Any = lambda path, timeout: None
        self.messages: list[tuple[str, dict]] = []
        self.connections: list[_FakeSock] = []

    def __call__(self, path: str, timeout: float) -> _FakeSock:
        mode = self.modes.get(path, "ok")
        if mode == "refused":
            raise ConnectionRefusedError("refused")
        if mode == "connect_timeout":
            raise TimeoutError("timed out")
        if mode == "gone":
            raise FileNotFoundError("gone")
        if mode == "connect_oserror":
            raise PermissionError("denied")
        if mode == "accept_and_stall":
            self.on_connect(path, timeout)
        sock = _FakeSock(self, path)
        self.connections.append(sock)
        return sock

    def path_messages(self, path: Path | str) -> list[dict]:
        return [m for p, m in self.messages if p == str(path)]


def _no_spawn(*args: object, **kwargs: object) -> None:
    raise AssertionError("cookie injection must not spawn a process")


class TestInjectIntoLiveSessions:
    @pytest.mark.parametrize("reachable", [True, False])
    @pytest.mark.parametrize(
        ("cookies", "reasons"),
        [
            ([], {}),
            ([{}], {}),
            ([{}], {"kc-a": ""}),
            ([{}], {"kc-a": "timed out"}),
            ([{}], {"kc-a": "", "kc-b": "timed out"}),
            ([{}], {"kc-a": mod._NOT_LIVE}),
        ],
        ids=["no_import", "no_sessions", "loaded", "failed", "mixed", "stale"],
    )
    def test_deliverable_on_every_return_path(
        self, home: Path, reachable: bool, cookies: list[dict[str, Any]], reasons: dict[str, str]
    ) -> None:
        sockets = {name: (f"/{name}.sock", i, i) for i, name in enumerate(reasons)}
        with (
            patch.object(mod, "delivery_supported", return_value=True),
            patch.object(mod, "_new_sessions_reachable", return_value=reachable),
            patch.object(mod, "_load_cookies", return_value=cookies),
            patch.object(mod, "_live_sockets", return_value=sockets),
            patch.object(mod, "_inject_into_session", side_effect=list(reasons.values())),
        ):
            result = mod.inject_into_live_sessions()
        assert result["deliverable"] is reachable
        assert result["loaded"] == [name for name, reason in reasons.items() if not reason]
        assert result["failed"] == {
            name: reason for name, reason in reasons.items() if reason and reason != mod._NOT_LIVE
        }
        if cookies and not result["loaded"]:
            assert (mod._UNREACHABLE_NOTE in result["note"]) is (not reachable)

    @needs_af_unix
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
        assert result["failed"] == {"kc-bbbb2222": "the browser refused 2 of 2 cookies"}
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
        # A refusal is counted, not a stop: the refusing session was still sent
        # every cookie.
        assert len(daemon.path_messages(b)) == 2

    @needs_af_unix
    def test_one_refused_cookie_does_not_keep_the_rest_out(self, home: Path) -> None:
        _import_cookies(home, "a", "b", "c")
        sock = _fake_socket(home, "aaaa1111")
        daemon = _FakeDaemon({str(sock): "refuse_a"})
        with patch.object(mod, "_connect", side_effect=daemon):
            result = mod.inject_into_live_sessions()
        sent = [m["params"]["args"]["_"][1] for m in daemon.path_messages(sock)]
        assert sorted(sent) == ["a", "b", "c"]
        assert result["failed"] == {"kc-aaaa1111": "the browser refused 1 of 3 cookies"}
        assert "secret" not in json.dumps(result)

    @needs_af_unix
    def test_a_reply_nested_past_the_parser_limit_is_a_malformed_reply(self, home: Path) -> None:
        """The control socket is the agent's own; a reply it crafts to recurse the
        JSON parser out must come back as a fixed reason, never as a raise out of
        a function the import handler relies on never raising."""
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        with patch.object(mod, "_connect", side_effect=_FakeDaemon({str(sock): "deep"})):
            result = mod.inject_into_live_sessions()
        assert result["failed"] == {"kc-aaaa1111": "malformed reply"}

    @needs_af_unix
    @pytest.mark.parametrize(
        ("mode", "reason"),
        [
            ("error", "the browser refused 1 of 1 cookies"),
            ("is_error", "the browser refused 1 of 1 cookies"),
            ("wrong_id", "malformed reply"),
            ("garbage", "malformed reply"),
            ("timeout", "timed out"),
            ("connect_timeout", "timed out"),
            ("connect_oserror", "PermissionError"),
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
        import tempfile

        # A short directory: tmp_path can exceed the AF_UNIX path budget.
        with tempfile.TemporaryDirectory(prefix="kcs", dir="/tmp") as short:
            absent = str(Path(short) / "absent.sock")
            reason = mod._inject_into_session(absent, mod._load_cookies() or [])
            assert reason == mod._NOT_LIVE
            # A socket file whose daemon is gone (bound, nothing listening).
            stale = _make_unix_socket(Path(short) / "stale.sock")
            assert mod._inject_into_session(str(stale), mod._load_cookies() or []) == mod._NOT_LIVE

    @needs_af_unix
    def test_a_stale_socket_left_by_a_dead_daemon_is_not_a_failure(self, home: Path) -> None:
        """A socket file outlives its daemon. The real connect is refused, and
        that browser does not exist: it is neither loaded nor failed, so the UI
        does not say an open browser refused the cookies."""
        _import_cookies(home, "a")
        stale = _fake_socket(home, "aaaa1111")
        live = _fake_socket(home, "bbbb2222")
        gone = _fake_socket(home, "cccc3333")
        daemon = _FakeDaemon({str(stale): "refused", str(gone): "gone"})
        with patch.object(mod, "_connect", side_effect=daemon):
            result = mod.inject_into_live_sessions()
        assert result == {"loaded": ["kc-bbbb2222"], "failed": {}, "deliverable": True}
        assert mod._WATCHER.injected() == {_key(live)}

    @needs_af_unix
    def test_only_stale_sockets_read_as_no_live_sessions(self, home: Path) -> None:
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        with patch.object(mod, "_connect", side_effect=_FakeDaemon({str(sock): "refused"})):
            result = mod.inject_into_live_sessions()
        assert result["loaded"] == [] and result["failed"] == {}
        assert result["note"].startswith("no live browser sessions")
        assert "new sessions automatically" in result["note"]

    @needs_af_unix
    def test_the_fan_out_has_one_overall_deadline(self, home: Path) -> None:
        """Agent-planted listeners that accept and stall must not hold the lock for
        the per-socket bound times their number: one deadline covers the whole
        import, and a socket it leaves unreached is a fixed, value-free failure."""
        _import_cookies(home, "a")
        leaves = ("aaaa1111", "bbbb2222", "cccc3333", "dddd4444")
        socks = [_fake_socket(home, leaf) for leaf in leaves]
        clock = [1000.0]
        handed: list[float] = []
        daemon = _FakeDaemon({str(s): "accept_and_stall" for s in socks})

        def stall(path: str, timeout: float) -> None:
            handed.append(timeout)
            clock[0] += 14.0

        daemon.on_connect = stall
        fake_time = MagicMock(wraps=time)
        fake_time.monotonic = lambda: clock[0]
        with (
            patch.object(mod, "_FANOUT_DEADLINE_S", 30.0),
            patch.object(mod, "time", fake_time),
            patch.object(mod, "_connect", side_effect=daemon),
        ):
            result = mod.inject_into_live_sessions()
        assert result["loaded"] == ["kc-aaaa1111", "kc-bbbb2222"]
        # The third socket gets only what is left of the overall budget and runs
        # out inside it; the fourth is never reached.
        assert handed == [15.0, 15.0, 2.0]
        assert result["failed"] == {"kc-cccc3333": "timed out", "kc-dddd4444": mod._NOT_REACHED}
        assert len(daemon.connections) == 3
        assert "secret" not in json.dumps(result)

    @needs_af_unix
    def test_new_session_deliverability_uses_launch_path_validation(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(launch_mod, "_UNIX_SOCKET_PATH_MAX_BYTES", 103)
        monkeypatch.setattr(launch_mod.platform_compat, "IS_WINDOWS", False)
        long_home = home / ("x" * 100)
        monkeypatch.setenv("KIROCREW_HOME", str(long_home))
        with patch.object(mod, "_load_cookies", return_value=[]):
            assert mod.inject_into_live_sessions()["deliverable"] is False
        assert not long_home.exists()

    @needs_af_unix
    @pytest.mark.parametrize("override", [launch_mod.SOCKETS_ENV, launch_mod.DAEMON_DIR_ENV])
    def test_relative_lifecycle_override_means_no_delivery_promise(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, override: str
    ) -> None:
        _import_cookies(home, "a")
        monkeypatch.setenv(override, "relative/root")
        assert mod.inject_into_live_sessions()["deliverable"] is False

    def test_supported_new_session_reach_check_is_read_only(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(launch_mod, "peek_data_home", lambda: home)
        with (
            patch.object(launch_mod, "config_dir", side_effect=AssertionError("must not maintain")),
            patch.object(Path, "mkdir", side_effect=AssertionError("must not create")),
            patch.object(launch_mod.platform_compat, "make_owner_only_dir") as prepare,
            patch.object(launch_mod.platform_compat, "restrict_dir_to_owner") as restrict,
            patch.object(launch_mod.logger, "warning") as warning,
        ):
            assert mod._new_sessions_reachable() is True
        prepare.assert_not_called()
        restrict.assert_not_called()
        warning.assert_not_called()
        assert not home.exists()

    @needs_af_unix
    def test_an_operator_fixed_session_name_means_no_delivery_promise(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With an operator-chosen session name the launch adds no socket root, so
        new agent sessions are never under the scanned roots: the note must not
        promise that the cookies apply to them."""
        _import_cookies(home, "a")
        monkeypatch.setenv(launch_mod.SESSION_ENV, "shared")
        result = mod.inject_into_live_sessions()
        assert result["note"].endswith(mod._UNREACHABLE_NOTE)
        assert "automatically" not in result["note"]

    @needs_af_unix
    @pytest.mark.parametrize("support", ["UNVERIFIED", "UNSUPPORTED"])
    def test_a_cli_without_the_lifecycle_hooks_means_no_delivery_promise(
        self, home: Path, monkeypatch: pytest.MonkeyPatch, support: str
    ) -> None:
        _import_cookies(home, "a")
        sock = _fake_socket(home, "aaaa1111")
        monkeypatch.setattr(
            launch_mod, "cli_lifecycle_env_support", lambda: (launch_mod.SeamSupport[support], "x")
        )
        with patch.object(mod, "_connect", side_effect=_FakeDaemon({str(sock): "error"})):
            result = mod.inject_into_live_sessions()
        assert result["note"].endswith(mod._UNREACHABLE_NOTE)
        assert "automatically" not in result["note"]

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
    def test_injects_into_every_named_browser_of_a_session(self, home: Path) -> None:
        _import_cookies(home, "a")
        older = _fake_socket(home, "aaaa1111", "older")
        newer = _fake_socket(home, "aaaa1111", "newer")
        daemon = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=daemon):
            result = mod.inject_into_live_sessions()
        assert sorted(result["loaded"]) == ["kc-aaaa1111/newer", "kc-aaaa1111/older"]
        assert len(daemon.path_messages(older)) == 1
        assert len(daemon.path_messages(newer)) == 1
        assert mod._WATCHER.injected() == {_key(older), _key(newer)}

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
    def test_no_sessions_carry_a_note(self, home: Path) -> None:
        result = mod.clear_live_sessions()
        assert result["cleared"] == [] and result["failed"] == {} and "note" in result

    @needs_af_unix
    def test_clears_over_the_same_sockets_an_import_reaches(self, home: Path) -> None:
        """``cookie-clear`` goes over each session's control socket, to exactly the
        sessions :func:`mod._live_sockets` finds, with no process spawned."""
        a = _fake_socket(home, "aaaa1111")
        b = _fake_socket(home, "bbbb2222")
        daemon = _FakeDaemon({str(b): "error"})
        with (
            patch.object(mod, "_connect", side_effect=daemon),
            patch("subprocess.run", side_effect=_no_spawn),
            patch("subprocess.Popen", side_effect=_no_spawn),
        ):
            result = mod.clear_live_sessions()
        assert result == {
            "cleared": ["kc-aaaa1111"],
            "failed": {"kc-bbbb2222": "the browser refused to clear its cookies"},
        }
        for path in (a, b):
            [message] = daemon.path_messages(path)
            assert message["method"] == "run"
            assert message["params"]["args"] == {"_": ["cookie-clear"]}

    @needs_af_unix
    def test_clears_every_named_browser_of_a_session(self, home: Path) -> None:
        """A session with an older and a newer named browser: ``cookie-clear`` goes
        to both, so the older one is not left signed in."""
        older = _fake_socket(home, "aaaa1111", "older")
        newer = _fake_socket(home, "aaaa1111", "newer")
        daemon = _FakeDaemon()
        with patch.object(mod, "_connect", side_effect=daemon):
            result = mod.clear_live_sessions()
        assert sorted(result["cleared"]) == ["kc-aaaa1111/newer", "kc-aaaa1111/older"]
        assert result["failed"] == {}
        for path in (older, newer):
            [message] = daemon.path_messages(path)
            assert message["params"]["args"] == {"_": ["cookie-clear"]}

    def test_resets_the_watcher_record(self, home: Path) -> None:
        mod._WATCHER.mark_injected(("/x.sock", 1, 2))
        mod.clear_live_sessions()
        assert mod._WATCHER.injected() == frozenset()

    @needs_af_unix
    def test_never_raises(self, home: Path) -> None:
        sock = _fake_socket(home, "aaaa1111")
        with patch.object(mod, "_connect", side_effect=_FakeDaemon({str(sock): "timeout"})):
            assert mod.clear_live_sessions()["failed"] == {"kc-aaaa1111": "timed out"}

    @needs_af_unix
    def test_a_stale_socket_is_not_reported_as_still_signed_in(self, home: Path) -> None:
        stale = _fake_socket(home, "aaaa1111")
        live = _fake_socket(home, "bbbb2222")
        daemon = _FakeDaemon({str(stale): "refused"})
        with patch.object(mod, "_connect", side_effect=daemon):
            assert mod.clear_live_sessions() == {"cleared": ["kc-bbbb2222"], "failed": {}}
        both_gone = _FakeDaemon({str(stale): "refused", str(live): "gone"})
        with patch.object(mod, "_connect", side_effect=both_gone):
            only_stale = mod.clear_live_sessions()
        assert only_stale["cleared"] == [] and only_stale["failed"] == {}
        assert only_stale["note"] == "no live browser sessions"

    @needs_af_unix
    def test_the_clear_fan_out_has_one_overall_deadline(self, home: Path) -> None:
        leaves = ("aaaa1111", "bbbb2222", "cccc3333", "dddd4444")
        socks = [_fake_socket(home, leaf) for leaf in leaves]
        clock = [1000.0]
        daemon = _FakeDaemon({str(s): "accept_and_stall" for s in socks})

        def stall(path: str, timeout: float) -> None:
            clock[0] += 14.0

        daemon.on_connect = stall
        fake_time = MagicMock(wraps=time)
        fake_time.monotonic = lambda: clock[0]
        with (
            patch.object(mod, "_FANOUT_DEADLINE_S", 30.0),
            patch.object(mod, "time", fake_time),
            patch.object(mod, "_connect", side_effect=daemon),
        ):
            result = mod.clear_live_sessions()
        assert result == {
            "cleared": ["kc-aaaa1111", "kc-bbbb2222"],
            "failed": {"kc-cccc3333": "timed out", "kc-dddd4444": mod._NOT_REACHED},
        }
        assert len(daemon.connections) == 3


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

    def test_a_tick_has_one_deadline_and_does_not_spend_untried_attempts(
        self, home: Path, manual_clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _import_cookies(home, "a")
        socks = [_fake_socket(home, f"{i:08x}") for i in range(4)]
        sockets = {f"kc-{i:08x}": _key(sock) for i, sock in enumerate(socks)}
        watcher = mod.SessionWatcher()
        handed: list[float] = []
        manual_clock.install(monkeypatch, mod)
        monkeypatch.setattr(mod, "_FANOUT_DEADLINE_S", 30.0)

        def stall(path: str, cookies, timeout: float) -> str:
            assert mod.transaction().locked()
            handed.append(timeout)
            manual_clock.advance(min(14.0, timeout))
            return "timed out"

        with (
            patch.object(mod, "_live_sockets", return_value=sockets),
            patch.object(mod, "_inject_into_session", side_effect=stall) as inject,
        ):
            assert watcher.tick() == 0
        assert handed == [30.0, 16.0, 2.0]
        assert inject.call_count == 3
        assert watcher._attempts == {_key(sock): 1 for sock in socks[:3]}
        assert _key(socks[3]) not in watcher._attempts
        assert not mod.transaction().locked()

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


def test_no_af_unix_means_no_delivery_and_a_fixed_reason(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without AF_UNIX nothing can reach a daemon: delivery is reported as
    unsupported, the watcher never starts, and every live outcome is a fixed,
    value-free reason rather than a promise that new sessions get the cookies."""
    monkeypatch.delattr(mod.socket, "AF_UNIX", raising=False)
    assert mod.delivery_supported() is False
    _import_cookies(home, "a")
    mod.start_session_watcher()
    assert not mod._WATCHER.running
    assert mod._inject_into_session("/unused.sock", mod._load_cookies() or []) == (
        "unsupported platform"
    )
    injected = mod.inject_into_live_sessions()
    assert injected["deliverable"] is False
    assert injected["loaded"] == [] and "new sessions" not in injected["note"]
    assert "cannot reach" in mod.clear_live_sessions()["note"]


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
    # The handlers' own behaviour, on every platform: a host without AF_UNIX
    # refuses the import outright, which its own test below pins.
    with patch.object(mod, "delivery_supported", return_value=True):
        yield application


@pytest.mark.asyncio
async def test_status_absent_then_present(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/browser/cookies")
        assert resp.status == 200
        body = await resp.json()
        assert body["present"] is False
        assert body["summary"] is None
        # No host path in the answer: nothing renders it.
        assert set(body) == {"present", "summary"}

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
async def test_import_on_a_host_without_af_unix_fails_and_stores_nothing(app, home: Path) -> None:
    """No browser on this host could ever receive the cookies, so the import must
    not answer with a success: it is refused, and nothing is written."""
    async with TestClient(TestServer(app)) as client:
        with patch.object(mod, "delivery_supported", return_value=False):
            resp = await client.post(
                "/api/browser/cookies",
                json={"content": json.dumps([{"name": "n", "value": "v", "domain": "d.com"}])},
            )
        assert resp.status == 501
        body = await resp.json()
        assert body["code"] == "unsupported_platform"
        assert "nothing was imported" in body["error"]
    assert not mod.storage_state_path().exists()
    assert not mod._WATCHER.running


@pytest.mark.asyncio
@pytest.mark.parametrize("reachable", [True, False])
async def test_import_reports_deliverable_from_injection(app, home: Path, reachable: bool) -> None:
    content = json.dumps([{"name": "n", "domain": "d.com"}])
    async with TestClient(TestServer(app)) as client:
        with patch.object(mod, "_new_sessions_reachable", return_value=reachable):
            resp = await client.post("/api/browser/cookies", json={"content": content})
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True and body["hot_load"]["loaded"] == []
    assert body["hot_load"]["deliverable"] is reachable
    assert (mod._UNREACHABLE_NOTE in body["hot_load"]["note"]) is (not reachable)
    assert body["summary"]["cookie_count"] == 1 and mod.storage_state_path().exists()


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
async def test_import_json_escaped_export_reaches_parser(app, home: Path) -> None:
    content = "\t" * (19 * 1024 * 1024 // 10) + "invalid-export"
    payload = json.dumps({"content": content})
    assert len(content.encode()) < mod.MAX_IMPORT_BYTES < len(payload.encode())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/browser/cookies",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "invalid_cookies"
    assert not mod.storage_state_path().exists()


@pytest.mark.asyncio
async def test_import_content_over_limit_is_refused_by_parser(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/browser/cookies", json={"content": "x" * (mod.MAX_IMPORT_BYTES + 1)}
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["code"] == "invalid_cookies"
        assert "2 MiB" in body["error"]
    assert not mod.storage_state_path().exists()


@pytest.mark.asyncio
async def test_import_oversize_is_413(app, home: Path) -> None:
    async with TestClient(TestServer(app)) as client:
        payload = json.dumps({"content": "x" * (2 * mod.MAX_IMPORT_BYTES + 1025)})
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


@pytest.mark.asyncio
async def test_import_audit_caps_the_domains_it_names(app, home: Path, mock_sel) -> None:
    """An import of thousands of domains must not become one unbounded SEL
    resources string: the first ``_AUDIT_MAX_DOMAINS`` are named, the rest
    counted."""
    cap = 20
    cookies = [{"name": "n", "value": "v", "domain": f"d{i:03d}.example"} for i in range(cap + 5)]
    # Sorted first in the summary, so it is among the named ones.
    cookies.append({"name": "n", "value": "v", "domain": "a\n.forged\x1b\u2028.example"})
    content = json.dumps(cookies)
    async with TestClient(TestServer(app)) as client:
        with patch(
            "kiro_crew.browser_cli.cookies.inject_into_live_sessions",
            return_value={"loaded": [], "failed": {}},
        ):
            resp = await client.post("/api/browser/cookies", json={"content": content})
        assert resp.status == 200
    [audit] = [
        c.kwargs
        for c in mock_sel.log_api_access.call_args_list
        if c.kwargs.get("operation") == "browser_cookies_import"
        and " cookies=" in c.kwargs.get("resources", "")
    ]
    resources = audit["resources"]
    assert resources.endswith(",+6 more")
    assert resources.count(".example") == cap
    assert "a.forged.example," in resources
    assert all(ch.isprintable() for ch in resources)
