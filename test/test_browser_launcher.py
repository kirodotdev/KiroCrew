"""The Browser panel's address bar on the non-native transport.

``browser_cli.launcher`` opens an OWNER-typed URL in the gateway host's Playwright
CLI browser, and ``POST /api/browser/open`` is its one caller. These tests pin the
contract the panel and the security model rest on:

* the auth class -- owner ok, internal-secret (agent) callers refused,
  unauthenticated refused -- because an agent reaching the endpoint would skip the
  shell approval ladder the capability model routes browsing through;
* URL validation, independent of the panel's own;
* argv construction: ``goto`` first, ``open`` only when the CLI says no browser is
  up, never a sandbox flag anywhere in argv or the environment;
* the CLI's own error text surfacing verbatim, with the sandbox remedy appended;
* the session naming that keeps the human's browser out of the orphan sweep, and
  the shutdown close that owns its lifetime instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.browser_cli import launcher

#: The real reveal and owner tag, captured before the autouse fixture below pins them.
REAL_REVEAL = launcher._reveal
REAL_OWNER_TAG = launcher.owner_tag

CLI = "/opt/node/bin/playwright-cli"
URL = "https://www.google.com/"
#: This gateway's owner tag under test; a sibling gateway's differs.
OWNER = "0a1b2c"
SIBLING = "f9e8d7"
#: The gateway-owned socket root and daemon registry the launcher and the ``show``
#: child share.
SOCKET_ROOT = "/pw/ui/s"
DAEMON_ROOT = "/pw/ui/d"
DAEMON_ENV = "PWTEST_DAEMON_SESSION_DIR"


#: What ``playwright-cli --json list`` answers for one session in each state the
#: launcher distinguishes. Shapes measured on 0.1.18 (``collectList``).
def list_json(session: str, status: str | None) -> str:
    browsers = [] if status is None else [{"name": session, "status": status}]
    return json.dumps({"browsers": browsers})


#: Node's uncaught-error preamble that precedes every daemon failure: the frame,
#: the source line, the caret.
NODE_PREAMBLE = (
    "/opt/node/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/tools/"
    "cli-client/session.js:169\n"
    "    const rejectWithPid = (reject, message) => reject(Object.assign(new Error("
    "`Daemon pid=${child.pid}: ${message}`), { daemonPid: child.pid }));\n"
    "                                                                    ^\n\n"
)
SANDBOX_STDERR = (
    "\n╔════════════════════════════╗\n"
    "║ Update available for @playwright/cli: 0.1.18 → 0.1.19 ║\n"
    "╚════════════════════════════╝\n\n"
    + NODE_PREAMBLE
    + "Error: Daemon pid=1467353: Daemon process exited with code 1\n"
    "[TargetClosedError2: Target page, context or browser has been closed\n"
    "Browser logs:\n"
    "Chromium sandboxing failed!\n"
    "Call log:\n"
    "\x1b[2m  - <launching> /home/u/.cache/ms-playwright/chromium_headless_shell-1237/"
    "chrome-headless-shell --disable-field-trial-config --user-data-dir=/x\x1b[22m\n"
    "\x1b[2m  - <launched> pid=1467368\x1b[22m\n"
    "\x1b[2m  - [pid=1467368][err] [0909/101519.013904:FATAL:zygote_host_impl_linux.cc(129)] "
    "No usable sandbox! If you want to live dangerously and need an immediate workaround, "
    "you can try using --no-sandbox.\x1b[22m\n"
    "\x1b[2m  - [pid=1467368] <gracefully close start>\x1b[22m\n"
    "] {\n"
    "  log: [\n"
    "    '  - <launching> /home/u/.cache/ms-playwright/chromium_headless_shell-1237/'\n"
    "  ]\n"
    "}\n"
)
#: Measured against a live gateway whose launch config names a bogus executable:
#: the message line carries the object-dump opener on its tail.
MISSING_EXECUTABLE_STDERR = (
    NODE_PREAMBLE + "Error: Daemon pid=1615098: Daemon process exited with code 1\n"
    "[PlaywrightError: Failed to launch chromium because executable doesn't exist at "
    "/nonexistent/chrome] {\n"
    "  log: []\n"
    "}\n"
)


@pytest.fixture(autouse=True)
def _fresh_launcher_state(monkeypatch: pytest.MonkeyPatch):
    """Module state is per-gateway; a test must not see another test's sessions.

    The socket root and the installed-source probe are pinned too: both read the
    host (the data home, the installed CLI's bundle), and the launcher's contract
    is about what it does WITH their answers.
    """
    monkeypatch.setattr(launcher, "_opened", set())
    monkeypatch.setattr(launcher, "_session_locks", {})
    monkeypatch.setattr(launcher, "cli_path", lambda: CLI)
    monkeypatch.setattr(launcher, "cli_command", lambda cli=None: [cli] if cli else None)
    monkeypatch.setattr(launcher, "owner_tag", lambda: OWNER)
    monkeypatch.setattr(
        launcher,
        "ui_socket_env",
        lambda env: {launcher.SOCKETS_ENV: SOCKET_ROOT, DAEMON_ENV: DAEMON_ROOT},
    )
    monkeypatch.setattr(launcher, "cli_dashboard_socket_supported", lambda: True)
    # No reveal socket exists in tests; the helper must stay silent about that.
    monkeypatch.setattr(launcher, "_reveal", lambda session, env: None)


def _name(session_key: str) -> str:
    """What :func:`launcher.session_name` answers under the pinned owner tag."""
    return f"panel-{OWNER}-" + hashlib.sha256(session_key.encode()).hexdigest()[:8]


#: ``list`` answers for the session ``open_url("chat-1")`` addresses.
SESSION_1 = _name("chat-1")
LIST_OPEN = (0, list_json(SESSION_1, "open"), "")
LIST_CLOSED = (0, list_json(SESSION_1, "closed"), "")
LIST_ABSENT = (0, list_json(SESSION_1, None), "")
LIST_BROKEN = (1, "", "Error: something about the registry")
OK = (0, "### Page URL: https://www.google.com/\n", "")


class TestSessionName:
    def test_is_owner_tag_then_slot_digest_and_deterministic(self):
        name = launcher.session_name("chat-133-1788945816")
        assert name.startswith(f"{launcher.SESSION_PREFIX}{OWNER}-")
        leaf = name[len(launcher.owner_prefix()) :]
        assert len(leaf) == 8 and all(c in "0123456789abcdef" for c in leaf)
        assert name == launcher.session_name("chat-133-1788945816")
        assert name != launcher.session_name("chat-134-1788945817")

    def test_owner_tag_is_this_gateway_s_data_home(self, monkeypatch: pytest.MonkeyPatch):
        """Two gateways sharing the CLI's registry (same HOME and cwd) are told
        apart by their data homes, so neither can produce the other's names."""
        monkeypatch.setattr(launcher, "config_dir", lambda: Path("/home/u/.kiro/crew"))
        live = REAL_OWNER_TAG()
        monkeypatch.setattr(launcher, "config_dir", lambda: Path("/tmp/pods/p1/.kiro/crew"))
        pod = REAL_OWNER_TAG()
        assert len(live) == 6 and all(c in "0123456789abcdef" for c in live)
        assert live != pod
        monkeypatch.setattr(launcher, "config_dir", lambda: Path("/home/u/.kiro/crew"))
        assert REAL_OWNER_TAG() == live

    def test_is_structurally_excluded_from_the_orphan_sweep(self):
        """A generated ``kc-<8hex>`` daemon is reclaimed as soon as no live process
        carries its session variable -- and the only process that ever carries the
        panel's is the short-lived CLI invocation. So the panel's name must NOT
        read as generated to either the sweep or the launch module."""
        from kiro_crew.browser_cli import launch
        from kiro_crew.session_pid import _is_generated_browser_session

        name = launcher.session_name("chat-1")
        assert not name.startswith("kc-")
        assert not _is_generated_browser_session(name.encode())
        assert launch._session_leaf(name) == ""


class TestReclaimStranded:
    """Startup closes what a previous life of THIS gateway left running -- and
    nothing a sibling gateway or an operator owns."""

    def test_an_inherited_agent_registry_never_captures_the_panel_s_sessions(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A gateway started from inside an agent's shell inherits that agent's
        daemon registry. The launcher's env must pin the gateway-owned one
        instead, for the open that registers the daemon AND for the sweep that
        looks for it a gateway life later -- otherwise a crash strands a
        logged-in browser the sweep can never find."""
        monkeypatch.setattr(
            launcher, "cli_env", lambda: {"PATH": "/opt/node/bin", DAEMON_ENV: "/agent/a1b2c3d4/d"}
        )
        monkeypatch.setattr(
            launcher,
            "ui_socket_env",
            lambda env: {launcher.SOCKETS_ENV: SOCKET_ROOT, DAEMON_ENV: DAEMON_ROOT},
        )
        own = _name("chat-a")
        fake, calls = _runs([LIST_CLOSED, OK, (0, list_json(own, "open"), ""), (0, "closed", "")])
        with patch.object(launcher, "_run_cli", fake):
            assert launcher.open_url(URL, "chat-1").ok
            launcher._opened.clear()  # a new gateway life: nothing recorded
            assert launcher.reclaim_stranded() == 1
        assert verbs(calls) == ["list", "open", "list", "close"]
        for _argv, env, _timeout in calls:
            assert env[DAEMON_ENV] == DAEMON_ROOT
            assert env[launcher.SOCKETS_ENV] == SOCKET_ROOT

    def test_closes_own_stranded_sessions_only(self):
        own_a = _name("chat-a")
        own_b = _name("chat-b")
        listing = json.dumps(
            {
                "browsers": [
                    {"name": own_a, "status": "open"},
                    {"name": own_b, "status": "closed"},  # not up: nothing to close
                    {"name": f"panel-{SIBLING}-11112222", "status": "open"},  # a pod's
                    {"name": "kc-deadbeef", "status": "open"},  # an agent's (sweep's job)
                    {"name": "my-browser", "status": "open"},  # the operator's own
                ]
            }
        )
        fake, calls = _runs([(0, listing, ""), (0, "closed", "")])
        with patch.object(launcher, "_run_cli", fake):
            assert launcher.reclaim_stranded() == 1
        assert verbs(calls) == ["list", "close"]
        assert calls[1][0] == [CLI, f"-s={own_a}", "close"]
        assert calls[1][1][launcher.SESSION_ENV] == own_a

    def test_a_session_this_life_already_opened_is_kept(self):
        """A launch that beat the sweep to its slot keeps its page."""
        own = _name("chat-a")
        launcher._opened.add(own)
        fake, calls = _runs([(0, list_json(own, "open"), "")])
        with patch.object(launcher, "_run_cli", fake):
            assert launcher.reclaim_stranded() == 0
        assert verbs(calls) == ["list"]

    def test_the_close_runs_under_the_slot_s_lock(self):
        """A launch mid-``goto`` for the same slot holds the slot lock and has not
        recorded the session yet; the close must wait for it, not race it."""
        own = _name("chat-a")
        held_during_close: list[bool] = []

        def fake(argv, env, timeout):
            if argv[-1] == "close":
                held_during_close.append(launcher._session_lock(own).locked())
                return (0, "closed", "")
            return (0, list_json(own, "open"), "")

        with patch.object(launcher, "_run_cli", fake):
            assert launcher.reclaim_stranded() == 1
        assert held_during_close == [True]
        assert not launcher._session_lock(own).locked()

    def test_an_unreadable_list_closes_nothing(self):
        fake, calls = _runs([LIST_BROKEN])
        with patch.object(launcher, "_run_cli", fake):
            assert launcher.reclaim_stranded() == 0
        assert verbs(calls) == ["list"]

    def test_without_the_cli_it_spawns_nothing(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(launcher, "cli_path", lambda: None)
        fake, calls = _runs([])
        with patch.object(launcher, "_run_cli", fake):
            assert launcher.reclaim_stranded() == 0
        assert calls == []

    def test_a_path_only_shim_is_warned_once_and_never_executed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from kiro_crew.browser_cli import install

        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        shim_dir = tmp_path / "path-bin"
        shim_dir.mkdir(parents=True)
        shim = shim_dir / (f"{install.CLI_BIN}.cmd" if os.name == "nt" else install.CLI_BIN)
        shim.write_text(
            "@exit /b 99\r\n" if os.name == "nt" else "#!/bin/sh\nexit 99\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        if os.name == "nt":
            monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", str(shim_dir))
        monkeypatch.setattr(install, "config_dir", lambda: crew)
        monkeypatch.setattr(install, "_system_cli_candidates", lambda: (), raising=False)
        monkeypatch.setattr(install, "_agent_writable_roots", lambda: (), raising=False)
        monkeypatch.setattr(install, "_warned_cli_refusals", set(), raising=False)
        monkeypatch.setattr(launcher, "cli_path", install.cli_path)
        fake, calls = _runs([])

        with (
            caplog.at_level(logging.WARNING, logger=install.__name__),
            patch.object(launcher, "_run_cli", fake),
        ):
            assert launcher.reclaim_stranded() == 0

        assert calls == []
        warnings = [
            record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert repr(str(shim.resolve())) in warnings[0]
        assert "PATH" in warnings[0]


class TestValidateUrl:
    @pytest.mark.parametrize(
        "raw",
        ["https://www.google.com/", "http://example.com:8443/path", "  https://a.b  "],
    )
    def test_accepts_http_and_https_with_a_host(self, raw: str):
        assert launcher.validate_url(raw) == raw.strip()

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "google.com",  # the panel normalizes; the endpoint wants a scheme
            "javascript:alert(1)",
            "file:///etc/passwd",
            "ftp://example.com/",
            "data:text/html,hi",
            "https://",
            "https:///path-only",
            "https://user:secret@example.com/",  # would publish the secret via /proc
            "https://user@example.com/",
            "https://example.com/?token=SECRET",  # query would leak via argv
            "https://example.com/#access_token=SECRET",  # fragment would leak via argv
            "https://example.com/?a=1",
            "https://example.com/#frag",
            "https://exa mple.com/",
            "https://example.com/a\nb",
            "https://" + "a" * 9000 + ".com/",
        ],
    )
    def test_refuses_everything_else(self, raw: str):
        assert launcher.validate_url(raw) is None


def _runs(script: list[tuple[int, str, str]]):
    """A ``_run_cli`` stand-in that replays *script* and records every call."""
    calls: list[tuple[list[str], dict[str, str], float]] = []
    outcomes = list(script)

    def fake(argv, env, timeout):
        calls.append((argv, env, timeout))
        return outcomes.pop(0)

    return fake, calls


def verbs(calls) -> list[str]:
    """The CLI verb of each recorded invocation (``list``, ``goto``, ``open``, ``close``)."""
    return ["list" if argv[-1] == "list" else argv[2] for argv, _env, _timeout in calls]


class TestOpenUrlArgv:
    def test_goto_when_list_says_the_browser_is_open(self):
        fake, calls = _runs([LIST_OPEN, OK])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.ok is True and result.error is None
        assert [c[0] for c in calls] == [
            [CLI, "--json", "list"],
            [CLI, f"-s={result.session}", "goto", URL],
        ]
        assert launcher._opened == {result.session}

    @pytest.mark.parametrize("listed", [LIST_CLOSED, LIST_ABSENT], ids=["closed", "absent"])
    def test_open_when_list_says_no_browser_is_up(self, listed):
        fake, calls = _runs([listed, OK])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.ok is True
        assert verbs(calls) == ["list", "open"]
        assert calls[1][0] == [CLI, f"-s={result.session}", "open", URL]
        # ``open`` gets the longer budget: it launches Chromium before navigating.
        assert calls[1][2] > launcher._GOTO_TIMEOUT_S

    @pytest.mark.parametrize(
        "listed",
        [LIST_BROKEN, (0, "not json", ""), (0, json.dumps({"browsers": "?"}), "")],
        ids=["nonzero", "unparseable", "wrong-shape"],
    )
    def test_an_unreadable_list_never_escalates_to_open(self, listed):
        """An ``open`` on a live session discards its tabs, so the only answer
        that may trigger one is a positive "not open" from the CLI."""
        fake, calls = _runs([listed, (1, "", "Error: Browser 'x' is not open.")])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.ok is False
        assert verbs(calls) == ["list", "goto"]
        assert "is not open" in (result.error or "")

    def test_a_goto_failure_is_reported_not_retried_with_open(self):
        fake, calls = _runs(
            [LIST_OPEN, (1, "", "Error: page.goto: net::ERR_NAME_NOT_RESOLVED at https://x/")]
        )
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url("https://x/", "chat-1")
        assert result.ok is False
        assert "ERR_NAME_NOT_RESOLVED" in (result.error or "")
        assert verbs(calls) == ["list", "goto"]
        assert launcher._opened == set()

    def test_no_sandbox_flag_is_ever_injected(self, monkeypatch: pytest.MonkeyPatch):
        """The browser sandbox is a security boundary the launcher never drops:
        no ``--no-sandbox`` on argv, no ``chromiumSandbox`` in the environment,
        and no config file written -- the operator's own PLAYWRIGHT_MCP_CONFIG is
        the only way to accept that trade-off, and it is inherited as-is."""
        monkeypatch.setenv("PLAYWRIGHT_MCP_CONFIG", "/operator/own/config.json")
        fake, calls = _runs([LIST_ABSENT, OK])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        for argv, env, _timeout in calls:
            assert not any("sandbox" in a.lower() for a in argv), argv
            # The keys the CLI reads to configure the launch are inherited
            # untouched (the operator's own config wins) and nothing that names
            # the Chromium sandbox flag is added anywhere in the environment.
            assert env["PLAYWRIGHT_MCP_CONFIG"] == "/operator/own/config.json"
            assert not any(k.startswith("PLAYWRIGHT_MCP_") for k in env if k not in os.environ)
            assert not any(
                "chromiumsandbox" in v.lower() or "--no-sandbox" in v.lower() for v in env.values()
            )
            # The daemon's exec-time environ and its argv name the same session,
            # and it lives under the gateway-owned socket root the view shares.
            assert env["PLAYWRIGHT_CLI_SESSION"] == result.session
            assert env[launcher.SOCKETS_ENV] == SOCKET_ROOT

    def test_missing_cli_is_an_honest_error(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(launcher, "cli_path", lambda: None)
        fake, calls = _runs([])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.ok is False and "not installed" in (result.error or "")
        assert calls == []

    def test_windows_batch_wrapper_never_receives_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node = r"C:\Program Files\nodejs\node.exe"
        entry = r"C:\Program Files\nodejs\node_modules\@playwright\cli\playwright-cli.js"
        monkeypatch.setattr(launcher, "cli_path", lambda: r"C:\managed\playwright-cli.cmd")
        monkeypatch.setattr(
            launcher, "cli_command", lambda _path=None: [node, entry], raising=False
        )
        fake, calls = _runs([LIST_OPEN, OK])

        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url("https://example.com/a&whoami", "chat-1")

        assert result.ok is True
        assert calls[1][0] == [
            node,
            entry,
            f"-s={result.session}",
            "goto",
            "https://example.com/a&whoami",
        ]
        assert not any(part.casefold().endswith((".cmd", ".bat")) for part in calls[1][0])

    def test_posix_shebang_wrapper_never_receives_the_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node = "/managed/playwright-cli/gateway-node"
        entry = "/managed/playwright-cli/lib/node_modules/@playwright/cli/playwright-cli.js"
        wrapper = "/managed/playwright-cli/managed-bin/playwright-cli"
        monkeypatch.setattr(launcher, "cli_path", lambda: wrapper)
        monkeypatch.setattr(launcher, "cli_command", lambda _path=None: [node, entry])
        fake, calls = _runs([LIST_OPEN, OK])

        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url("https://example.com/path", "chat-1")

        assert result.ok is True
        assert calls[1][0] == [
            node,
            entry,
            f"-s={result.session}",
            "goto",
            "https://example.com/path",
        ]
        assert wrapper not in calls[1][0]

    def test_a_timed_out_open_is_still_recorded_for_shutdown(self):
        """The CLI was killed waiting, but the detached daemon it started may be
        up; it must be on the shutdown list, not left for a retry."""
        timed_out = (launcher._TIMEOUT_RC, "", "playwright-cli did not finish within 90s")
        fake, _calls = _runs([LIST_ABSENT, timed_out])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.ok is False and "did not finish" in (result.error or "")
        assert launcher._opened == {result.session}

    def test_a_failed_first_navigation_is_still_recorded_for_shutdown(self):
        """``open`` is "start the daemon, then goto". The installed CLI reports a
        failed first navigation with exit code 1 and LEAVES the browser running
        (only a thrown error stops the daemon), so the session must be on the
        shutdown list even though the launch is reported as failed."""
        fake, calls = _runs(
            [LIST_ABSENT, (1, "", "Error: page.goto: net::ERR_NAME_NOT_RESOLVED at https://x/")]
        )
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url("https://x/", "chat-2")
        assert result.ok is False
        assert "ERR_NAME_NOT_RESOLVED" in (result.error or "")
        assert verbs(calls) == ["list", "open"]
        assert launcher._opened == {result.session}


class TestErrorText:
    def test_sandbox_failure_keeps_the_cli_words_and_appends_the_remedy(self):
        fake, _calls = _runs([LIST_ABSENT, (1, "", SANDBOX_STDERR)])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.ok is False
        error = result.error or ""
        # The CLI's own diagnosis, verbatim ...
        assert "Chromium sandboxing failed!" in error
        assert "No usable sandbox!" in error
        assert "Daemon process exited with code 1" in error
        # ... without the noise it is wrapped in ...
        assert "Update available" not in error
        assert "--disable-field-trial-config" not in error
        assert "\x1b[" not in error
        assert "log: [" not in error
        assert "session.js:169" not in error
        assert "rejectWithPid" not in error
        assert "^" not in error
        # ... and the spec's remedy, after it, naming the operator's escape hatch.
        assert error.endswith(launcher.SANDBOX_REMEDY)
        assert "PLAYWRIGHT_MCP_CONFIG" in launcher.SANDBOX_REMEDY
        assert "never disables the sandbox" in launcher.SANDBOX_REMEDY

    def test_missing_executable_failure_is_the_cli_message_alone(self):
        """The message line ends in the object-dump opener, which must not
        swallow the message with it."""
        fake, _calls = _runs([LIST_ABSENT, (1, "", MISSING_EXECUTABLE_STDERR)])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.error == (
            "Error: Daemon pid=1615098: Daemon process exited with code 1\n"
            "[PlaywrightError: Failed to launch chromium because executable doesn't exist at "
            "/nonexistent/chrome]"
        )

    def test_remedy_is_not_appended_to_unrelated_failures(self):
        fake, _calls = _runs(
            [LIST_ABSENT, (1, "", "Error: Chromium distribution 'chrome' is not found at /opt/x")]
        )
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.error == "Error: Chromium distribution 'chrome' is not found at /opt/x"

    def test_silent_failure_names_the_exit_status(self):
        fake, _calls = _runs([LIST_OPEN, (3, "", "")])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert result.error == "playwright-cli exited with status 3"

    def test_credentials_in_cli_output_are_redacted_before_they_reach_the_panel(self):
        leaky = "Error: registry https://user:hunter2@npm.example/ refused\n"
        fake, _calls = _runs([LIST_OPEN, (1, "", leaky)])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert "hunter2" not in (result.error or "")

    def test_distilled_text_is_capped(self):
        fake, _calls = _runs([LIST_OPEN, (1, "", "x" * 10_000)])
        with patch.object(launcher, "_run_cli", fake):
            result = launcher.open_url(URL, "chat-1")
        assert len(result.error or "") <= launcher._ERROR_CAP + len(launcher.SANDBOX_REMEDY) + 2


class TestCloseAll:
    def test_closes_exactly_the_sessions_this_gateway_opened(self):
        a_name = launcher.session_name("chat-a")
        b_name = launcher.session_name("chat-b")
        fake, calls = _runs(
            [
                (0, list_json(a_name, "open"), ""),
                OK,
                (0, list_json(b_name, "open"), ""),
                OK,
                (0, "closed", ""),
                (0, "closed", ""),
            ]
        )
        with patch.object(launcher, "_run_cli", fake):
            a = launcher.open_url(URL, "chat-a").session
            b = launcher.open_url(URL, "chat-b").session
            launcher.close_all()
        closes = [c[0] for c in calls if c[0][-1] == "close"]
        assert sorted(closes) == sorted([[CLI, f"-s={a}", "close"], [CLI, f"-s={b}", "close"]])
        # Never the CLI's global forms, which would take an operator's browser down.
        assert not any("close-all" in c[0] or "kill-all" in c[0] for c in calls)
        assert launcher._opened == set()

    def test_nothing_opened_spawns_nothing(self):
        fake, calls = _runs([])
        with patch.object(launcher, "_run_cli", fake):
            launcher.close_all()
        assert calls == []


class TestReveal:
    def test_socket_path_is_the_shared_root_or_nothing(self, monkeypatch: pytest.MonkeyPatch):
        # The POSIX branch, on any host: Windows is its own case below.
        monkeypatch.setattr(launcher.platform_compat, "IS_WINDOWS", False)
        # The root the gateway set for both children; never a re-derived default.
        assert launcher._dashboard_socket_path({launcher.SOCKETS_ENV: "/pw/root"}) == os.path.join(
            "/pw/root", "dashboard", "app.sock"
        )
        assert launcher._dashboard_socket_path({}) is None
        assert launcher._dashboard_socket_path({"TMPDIR": "/tmp", "USER": "u"}) is None
        # An installed CLI whose bundle does not carry the layout: no reveal.
        monkeypatch.setattr(launcher, "cli_dashboard_socket_supported", lambda: False)
        assert launcher._dashboard_socket_path({launcher.SOCKETS_ENV: "/pw/root"}) is None

    def test_windows_skips_the_reveal_silently(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """The dashboard listens on a named pipe there and the human picks the
        session from the sidebar; no path, and no warning about a lost layout."""
        monkeypatch.setattr(launcher.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(launcher, "_layout_warned", False)
        with caplog.at_level(logging.WARNING, logger=launcher.__name__):
            assert launcher._dashboard_socket_path({launcher.SOCKETS_ENV: "/pw/root"}) is None
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_a_lost_layout_is_reported_once_at_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """An upstream rename must be visible in the gateway log, not a silent
        loss of the auto-attach -- and said once, not on every launch."""
        monkeypatch.setattr(launcher.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(launcher, "cli_dashboard_socket_supported", lambda: False)
        monkeypatch.setattr(launcher, "_layout_warned", False)
        with caplog.at_level(logging.WARNING, logger=launcher.__name__):
            assert launcher._dashboard_socket_path({launcher.SOCKETS_ENV: "/pw/root"}) is None
            assert launcher._dashboard_socket_path({launcher.SOCKETS_ENV: "/pw/root"}) is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "dashboard socket layout" in warnings[0].getMessage()

    @pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX sockets only")
    def test_sends_one_reveal_line_to_the_running_dashboard(self, request: pytest.FixtureRequest):
        # tempfile's base is the run-scoped short root the conftest redirects to;
        # pytest's own tmp_path is too long for a sun_path (103 bytes on Linux).
        root = Path(tempfile.mkdtemp(prefix="pw-"))
        # Strict cleanup, registered the moment the directory exists: a socket
        # file left behind would be a real leak, not one to ignore.
        request.addfinalizer(lambda: shutil.rmtree(root))
        (root / "dashboard").mkdir(parents=True)
        sock_path = str(root / "dashboard" / "app.sock")
        received: list[bytes] = []
        ready = threading.Event()

        def serve() -> None:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
                srv.bind(sock_path)
                srv.listen(1)
                srv.settimeout(5)
                ready.set()
                try:
                    conn, _ = srv.accept()
                    with conn:
                        received.append(conn.recv(4096))
                        conn.sendall(b'{"pid": 4242}\n')
                except (socket.timeout, TimeoutError):
                    return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        # Joined unconditionally: a failed reveal must not leave the listener blocked in accept().
        request.addfinalizer(lambda: thread.join(6))
        assert ready.wait(5)
        assert REAL_REVEAL("panel-0a1b2c-1234abcd", {launcher.SOCKETS_ENV: str(root)}) is True
        thread.join(5)
        assert json.loads(received[0].decode().strip()) == {"sessionName": "panel-0a1b2c-1234abcd"}

    def test_a_missing_dashboard_is_silently_skipped(self, tmp_path: Path):
        # No listener, no socket file: the reveal must not raise and must not
        # spawn anything (the CLI's own `show -s=` would pop an app window here).
        # It reports the skip, so the panel can tell the reader which session to pick.
        assert REAL_REVEAL("panel-0a1b2c-1234abcd", {launcher.SOCKETS_ENV: str(tmp_path)}) is False

    def test_no_socket_path_reports_no_attach(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(launcher, "_dashboard_socket_path", lambda env: None)
        assert REAL_REVEAL("panel-0a1b2c-1234abcd", {}) is False

    def test_reveal_runs_only_after_a_successful_launch(self):
        seen: list[str] = []
        with (
            patch.object(launcher, "_reveal", lambda session, env: seen.append(session)),
            patch.object(launcher, "_run_cli", _runs([LIST_OPEN, (1, "", "boom")])[0]),
        ):
            launcher.open_url(URL, "chat-1")
        assert seen == []
        with (
            patch.object(launcher, "_reveal", lambda session, env: seen.append(session) or True),
            patch.object(launcher, "_run_cli", _runs([LIST_OPEN, OK])[0]),
        ):
            result = launcher.open_url(URL, "chat-1")
        assert seen == [result.session]

    def test_the_answer_says_whether_the_view_attached(self):
        """A skipped attach is the one case where the reader lands on the session
        grid with no page, so the result -- and its wire shape -- carry it."""
        with (
            patch.object(launcher, "_reveal", lambda session, env: True),
            patch.object(launcher, "_run_cli", _runs([LIST_OPEN, OK])[0]),
        ):
            attached = launcher.open_url(URL, "chat-1")
        assert attached.ok is True and attached.attached is True
        assert attached.as_dict()["attached"] is True
        with (
            patch.object(launcher, "_reveal", lambda session, env: False),
            patch.object(launcher, "_run_cli", _runs([LIST_OPEN, OK])[0]),
        ):
            skipped = launcher.open_url(URL, "chat-1")
        assert skipped.ok is True and skipped.attached is False
        assert skipped.as_dict() == {
            "ok": True,
            "session": skipped.session,
            "error": None,
            "attached": False,
        }
        # A failed launch never attached anything.
        with (
            patch.object(launcher, "_reveal", lambda session, env: True),
            patch.object(launcher, "_run_cli", _runs([LIST_OPEN, (1, "", "boom")])[0]),
        ):
            failed = launcher.open_url(URL, "chat-1")
        assert failed.ok is False and failed.attached is False


# ── The route ───────────────────────────────────────────────────────────────


VIEW_RUNNING = {"status": "running", "url": "http://127.0.0.1:45613", "port": 45613, "reason": None}


@pytest.fixture()
def mock_sel():
    m = MagicMock()
    m.log_api_access = MagicMock()
    with patch("kiro_crew.dashboard.handlers.sel", return_value=m):
        yield m


@pytest.fixture()
def launch_mocks(tmp_path: Path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dashboard": {"browser_view_port": 45613}}), encoding="utf-8")
    opened: list[tuple[str, str]] = []
    ensured: list[int | None] = []

    def fake_open(url: str, session_key: str):
        opened.append((url, session_key))
        return launcher.LaunchResult(True, launcher.session_name(session_key), None, attached=False)

    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg),
        patch("kiro_crew.browser_cli.view.ensure_running", side_effect=ensured.append),
        patch("kiro_crew.browser_cli.view.status", return_value=VIEW_RUNNING),
        patch("kiro_crew.browser_cli.launcher.open_url", side_effect=fake_open) as open_mock,
    ):
        yield {"opened": opened, "ensured": ensured, "open_mock": open_mock}


def _app(*, owner: bool = True, internal: bool = False) -> web.Application:
    from kiro_crew.dashboard.handlers.messaging import api_browser_open

    app = web.Application()
    app.router.add_post("/api/browser/open", api_browser_open)
    if internal:

        @web.middleware
        async def _internal(request: web.Request, handler):
            # What token_auth publishes for a loopback X-Internal-Secret caller:
            # the grant marker, and NO dashboard-user claim.
            request["internal_auth"] = True
            return await handler(request)

        app.middlewares.append(_internal)
    if owner:
        as_owner(app)
    else:
        from dashboard_owner_helpers import NoConfiguredOwner

        app["state"] = NoConfiguredOwner()
    return app


async def _post(app: web.Application, body: object, **kwargs):
    async with TestClient(TestServer(app)) as client:
        if isinstance(body, (bytes, str)):
            resp = await client.post("/api/browser/open", data=body, **kwargs)
        else:
            resp = await client.post("/api/browser/open", json=body, **kwargs)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_owner_opens_the_url_and_gets_the_view_status(mock_sel, launch_mocks):
    status, payload = await _post(_app(), {"url": URL, "session_key": "chat-7"})
    assert status == 200
    assert payload["ok"] is True
    assert "url" not in payload  # the caller's own input is not echoed
    assert payload["session"] == launcher.session_name("chat-7")
    assert payload["error"] is None
    assert payload["view"] == VIEW_RUNNING
    # Whether the framed dashboard attached to the session travels with the
    # answer: false is the one case where the panel must say which session to pick.
    assert payload["attached"] is False
    assert launch_mocks["opened"] == [(URL, "chat-7")]
    # The view is brought up FIRST, with the configured pin, so the human has
    # something to look at and the reveal has a dashboard to talk to.
    assert launch_mocks["ensured"] == [45613]


@pytest.mark.asyncio
async def test_internal_secret_caller_is_refused(mock_sel, launch_mocks):
    """An agent (loopback + X-Internal-Secret) must not drive the panel's browser:
    that would bypass the shell approval ladder the capability model routes
    agent browsing through."""
    status, payload = await _post(
        _app(owner=False, internal=True), {"url": URL, "session_key": "c"}
    )
    assert status == 403
    assert launch_mocks["open_mock"].call_count == 0
    assert launch_mocks["ensured"] == []


@pytest.mark.asyncio
async def test_internal_secret_is_refused_even_alongside_owner_claims(mock_sel, launch_mocks):
    """Belt and braces: if the route were ever put on an internal-path list, the
    handler's own check still refuses the proven internal caller."""
    status, payload = await _post(_app(owner=True, internal=True), {"url": URL, "session_key": "c"})
    assert status == 403
    assert payload["code"] == "internal_caller"
    assert launch_mocks["open_mock"].call_count == 0


@pytest.mark.asyncio
async def test_unauthenticated_caller_is_refused(mock_sel, launch_mocks):
    status, _payload = await _post(_app(owner=False), {"url": URL, "session_key": "c"})
    assert status == 403
    assert launch_mocks["open_mock"].call_count == 0


@pytest.mark.asyncio
async def test_non_owner_dashboard_user_and_app_token_are_refused(mock_sel, launch_mocks):
    for headers in ({"X-Test-User": "someone-else"}, {"X-Test-App": "an-app"}):
        status, _payload = await _post(_app(), {"url": URL, "session_key": "c"}, headers=headers)
        assert status == 403, headers
    assert launch_mocks["open_mock"].call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body, code",
    [
        ({"url": "javascript:alert(1)", "session_key": "c"}, "invalid_url"),
        ({"url": "google.com", "session_key": "c"}, "invalid_url"),
        ({"url": "https://u:p@example.com/", "session_key": "c"}, "invalid_url"),
        ({"url": URL}, "invalid_request"),
        ({"url": URL, "session_key": "   "}, "invalid_request"),
        ({"url": 5, "session_key": "c"}, "invalid_request"),
        ([1], "invalid_json"),
        (b"not json", "invalid_json"),
    ],
)
async def test_bad_requests_are_400_and_spawn_nothing(mock_sel, launch_mocks, body, code):
    status, payload = await _post(_app(), body)
    assert status == 400
    assert payload["code"] == code
    assert launch_mocks["open_mock"].call_count == 0
    assert launch_mocks["ensured"] == []


@pytest.mark.asyncio
async def test_cli_failure_surfaces_verbatim_with_200_and_ok_false(mock_sel, launch_mocks):
    """A launch that failed is an answer, not an HTTP error: the panel renders
    the CLI's own words, exactly as the view routes report a failed start."""
    text = "Chromium sandboxing failed!\nNo usable sandbox!\n\n" + launcher.SANDBOX_REMEDY
    launch_mocks["open_mock"].side_effect = lambda url, key: launcher.LaunchResult(
        False, launcher.session_name(key), text
    )
    status, payload = await _post(_app(), {"url": URL, "session_key": "c"})
    assert status == 200
    assert payload["ok"] is False
    assert payload["error"] == text
    assert payload["view"] == VIEW_RUNNING
