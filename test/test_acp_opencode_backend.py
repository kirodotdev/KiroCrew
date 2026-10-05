"""The opencode spawn path: how it is found, how it is made to ask, and how that is proven.

Three things are pinned here, and they fail apart:

* **Resolution.** A binary that serves ACP itself takes the plain-binary ladder --
  explicit override, mise, PATH -- and reports the path it searched when absent.
* **The routing seed.** The permission setting travels in the child's environment,
  merged over whatever the operator already put there, so nothing is written into a
  checked-out repository.
* **The read-back.** What makes this harness's routing VERIFIED rather than
  declared: the harness's own resolved configuration is read back and compared, off
  the event loop, before the first prompt.

The read-back's refusal vocabulary is exercised through
``acp_tool_gate.seeded_setting_issue``, which owns it, rather than by asserting on
message wording at the call site.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading

import pytest

from kiro_crew import acp_tool_gate
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import (
    OPENCODE_BIN,
    OPENCODE_INSTALL_COMMAND,
    PROTOCOL_VERSION_OPENCODE,
    AcpClient,
    _opencode_agent_permissions,
    _opencode_uniform_permission,
    _resolve_self_served_bin,
)
from kiro_crew.acp.harness import opencode as opencode_mod
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE, Routing, routing_for
from kiro_crew.acp_tool_gate import seeded_setting_issue

#: A read-back argv as the spawn arm hands it over: already sandbox-wrapped there,
#: so these tests pass one through unchanged rather than reconstructing it.
_ARGV = ["/opt/opencode", "debug", "config"]

_ENV_BIN = "OPENCODE_BIN"
_ENV_CONFIG = "OPENCODE_CONFIG_CONTENT"


@pytest.fixture(autouse=True)
def _no_ambient_opencode_env(monkeypatch):
    """Neither override may leak in from the developer's own shell."""
    monkeypatch.delenv(_ENV_BIN, raising=False)
    monkeypatch.delenv(_ENV_CONFIG, raising=False)


# ── Resolution ───────────────────────────────────────────────────────────────


class TestResolutionLadder:
    """Three rungs, and the harness's own spelling for the override variable."""

    def test_an_executable_override_wins(self, monkeypatch, tmp_path):
        """The override outranks the two rungs below it.

        Executability is STUBBED rather than created on disk: what a file has to be
        for the host to call it runnable differs per platform (a mode bit here, a
        PATHEXT suffix there), and this test is about rung ORDER. The next test
        covers the case where the override is not runnable.

        The mise stub answers ``/never/reached`` so a dropped override rung shows up
        as that value in ``resolved``. Bind the expected answer -- the resolver's own
        casing normalization, with the same ``or`` fallback it applies -- to a
        variable before comparing, as ``test_path_is_the_last_rung`` does: written
        inline, ``assert resolved == norm(x) or x`` parses as
        ``(resolved == norm(x)) or x``, and a non-empty tmp_path string on the right
        of ``or`` makes the whole assert unfalsifiable, so the rung this test exists
        for would be pinned by nothing.
        """
        binary = tmp_path / "opencode"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(acp_client.platform_compat, "is_executable_file", lambda _path: True)
        monkeypatch.setenv(_ENV_BIN, str(binary))
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: "/never/reached")
        resolved, _searched = _resolve_self_served_bin(ACP_BACKEND_OPENCODE)
        expected = acp_client._normalize_exe_casing(str(binary)) or str(binary)
        assert resolved == expected

    def test_a_non_executable_override_falls_through(self, monkeypatch, tmp_path):
        """An override naming something unrunnable must not shadow a working install.

        Pointing the variable at a directory or a text file is a typo, and treating
        it as the answer would report the harness present and then fail at spawn
        with an exec error instead of the ladder's own message.
        """
        not_a_binary = tmp_path / "notes.txt"
        not_a_binary.write_text("hello", encoding="utf-8")
        monkeypatch.setattr(acp_client.platform_compat, "is_executable_file", lambda _path: False)
        monkeypatch.setenv(_ENV_BIN, str(not_a_binary))
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: "/opt/mise/opencode")
        resolved, _searched = _resolve_self_served_bin(ACP_BACKEND_OPENCODE)
        assert resolved == "/opt/mise/opencode"

    def test_mise_is_consulted_before_path(self, monkeypatch):
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: "/opt/mise/opencode")
        monkeypatch.setattr(acp_client.shutil, "which", lambda *_a, **_kw: "/usr/bin/opencode")
        resolved, _searched = _resolve_self_served_bin(ACP_BACKEND_OPENCODE)
        assert resolved == "/opt/mise/opencode"

    def test_path_is_the_last_rung(self, monkeypatch):
        """With no override and no mise answer, PATH decides.

        Compared against the resolver's own casing normalization rather than the
        literal string handed to the stub: on Windows that normalization resolves a
        bare POSIX-looking path against the current drive, and pinning the literal
        would assert a spelling this code deliberately does not promise.
        """
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
        monkeypatch.setattr(acp_client.shutil, "which", lambda *_a, **_kw: "/usr/bin/opencode")
        resolved, _searched = _resolve_self_served_bin(ACP_BACKEND_OPENCODE)
        expected = acp_client._normalize_exe_casing("/usr/bin/opencode") or "/usr/bin/opencode"
        assert resolved == expected
        assert resolved.replace("\\", "/").endswith("/opencode")

    def test_absent_reports_the_path_it_searched(self, monkeypatch):
        """The searched path travels WITH the answer, so the message cannot describe
        a different environment than the one the search ran in."""
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
        monkeypatch.setattr(acp_client.shutil, "which", lambda *_a, **_kw: None)
        monkeypatch.setenv("PATH", "/first:/second")
        resolved, searched = _resolve_self_served_bin(ACP_BACKEND_OPENCODE)
        assert resolved is None
        assert "/first" in searched

    def test_there_is_no_adapter_package_in_the_argv(self):
        """The argv is the binary and its own subcommand: no node, no entry script."""
        from kiro_crew.agent_sdk.backends import launch_for

        record = launch_for(ACP_BACKEND_OPENCODE)
        assert record.acp_args == ("acp",)
        assert record.binary == "opencode"
        assert "npm" in record.install_command
        # The two names this module still binds, because its routing remedy spells
        # them in prose. They must not drift from the row they are read out of.
        assert OPENCODE_BIN == record.binary
        assert OPENCODE_INSTALL_COMMAND == record.install_command


def test_the_handshake_is_the_spec_dialect():
    """An integer ``protocolVersion``, captured off this harness's own wire.

    kiro-cli's dialect is a date string, so a harness put on the wrong one fails
    ``initialize`` outright. Looked up from a per-harness TABLE, so the shared
    handshake evaluates no adapter conditional and an unknown id keeps kiro's.
    """
    assert PROTOCOL_VERSION_OPENCODE == 1
    table = acp_client._PROTOCOL_VERSION_BY_BACKEND
    assert table[ACP_BACKEND_OPENCODE] == PROTOCOL_VERSION_OPENCODE
    assert table.get("", acp_client.PROTOCOL_VERSION) == acp_client.PROTOCOL_VERSION
    # The params are spelled once, in _initialize_params, which both the session
    # handshake and the entitlement probe's handshake read.
    assert "self._initialize_params()" in inspect.getsource(AcpClient._initialize_session)
    body = inspect.getsource(AcpClient._initialize_params)
    assert "_PROTOCOL_VERSION_BY_BACKEND.get(" in body
    assert "PROTOCOL_VERSION_OPENCODE if" not in body


# ── The routing seed ─────────────────────────────────────────────────────────


class TestRoutingSeed:
    """The setting travels in the environment, and it never eats operator config."""

    @staticmethod
    def _seed() -> str:
        return opencode_mod._opencode_routing_config(ACP_BACKEND_OPENCODE)

    def test_the_seed_carries_the_declared_setting(self):
        seed = json.loads(self._seed())
        assert seed == {"permission": "ask"}

    def test_an_operators_other_keys_survive(self, monkeypatch):
        """Merged over the ambient value, not substituted for it.

        An operator who set this variable did so to configure the harness; dropping
        their model or provider block to deliver one permission key would break the
        session in service of securing it.
        """
        monkeypatch.setenv(
            _ENV_CONFIG, json.dumps({"model": "ollama/qwen3:8b", "permission": "allow"})
        )
        seed = json.loads(self._seed())
        assert seed["model"] == "ollama/qwen3:8b"
        assert seed["permission"] == "ask", "Crew's permission setting must win the merge"

    @pytest.mark.parametrize("ambient", ["not json at all", '"a string"', "[1, 2]"])
    def test_an_unusable_ambient_value_still_yields_the_seed(self, monkeypatch, ambient):
        """A value that is not a JSON object cannot be merged, so the seed stands alone.

        Failing closed the other way -- refusing to seed -- would let a malformed
        environment variable disable the host gate.
        """
        monkeypatch.setenv(_ENV_CONFIG, ambient)
        seed = json.loads(self._seed())
        assert seed == {"permission": "ask"}

    def test_nothing_is_written_into_the_work_dir(self, monkeypatch, tmp_path):
        """The whole reason the seed is an env value: a session leaves no trace in a
        checked-out repository, so there is no ownership to arbitrate and no file to
        restore on teardown. Run from inside the directory, so a relative write would
        land where it is looked for."""
        (tmp_path / "repo-file.txt").write_text("x", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        before = sorted(p.name for p in tmp_path.iterdir())
        self._seed()
        assert sorted(p.name for p in tmp_path.iterdir()) == before


# ── The read-back ────────────────────────────────────────────────────────────


class TestObservedPermissionNormalization:
    """The harness normalizes a bare value into a rule map, so shapes are compared."""

    def test_a_bare_value_is_itself(self):
        assert _opencode_uniform_permission("ask") == "ask"

    def test_a_uniform_map_is_its_value(self):
        assert _opencode_uniform_permission({"*": "ask"}) == "ask"
        assert _opencode_uniform_permission({"*": "ask", "bash": "ask"}) == "ask"

    def test_a_mixed_map_is_not_reduced(self):
        """One tool left permissive is one tool whose calls never reach the gate.

        Returned as its own spelling rather than collapsed, so the refusal can name
        what was seen instead of reporting a value that is not in the file.
        """
        observed = _opencode_uniform_permission({"*": "ask", "webfetch": "allow"})
        assert observed != "ask"
        assert "webfetch" in str(observed)

    @pytest.mark.parametrize("raw", [None, {}, 42, ["ask"]])
    def test_anything_else_is_absent(self, raw):
        assert _opencode_uniform_permission(raw) is None


class TestTheIssueVocabulary:
    """What the read-back's answer means, decided by the gate rather than the driver."""

    def test_the_required_value_is_no_issue(self):
        assert seeded_setting_issue(ACP_BACKEND_OPENCODE, "ask") == ""

    def test_an_absent_setting_is_an_issue(self):
        assert "does not carry" in seeded_setting_issue(ACP_BACKEND_OPENCODE, None)

    def test_a_permissive_setting_is_an_issue_naming_what_was_seen(self):
        issue = seeded_setting_issue(ACP_BACKEND_OPENCODE, "allow")
        assert "allow" in issue and "ask" in issue

    def test_a_harness_on_another_mechanism_has_no_issue(self):
        """Scoped to the mechanism: a SESSION_CONFIG harness is checked elsewhere."""
        assert seeded_setting_issue("codex", None) == ""


class TestTheReadBackReportsFailureRatherThanAssuming:
    """A read-back that could not run must never read as "in force"."""

    def _client(self, tmp_path):
        return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)

    @staticmethod
    def _verify(client, argv, config_content):
        return opencode_mod._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, argv, config_content
        )

    def test_a_missing_binary_is_an_issue(self, tmp_path):
        issue, remedy = self._verify(
            self._client(tmp_path),
            [str(tmp_path / "not-there"), "debug", "config"],
            '{"permission": "ask"}',
        )
        assert issue
        assert "debug config" in remedy, "an exec failure gets the harness remedy"

    def test_a_non_zero_exit_is_an_issue(self, tmp_path, monkeypatch):
        class _Completed:
            returncode = 3
            stdout = ""
            stderr = ""

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._verify(self._client(tmp_path), _ARGV, "{}")
        assert "exit 3" in issue
        assert "debug config" in remedy

    def test_the_mcp_servers_of_the_harnesss_own_config_are_recorded(self, tmp_path, monkeypatch):
        """The read-back names the servers opencode mounts itself, for hook matching."""

        class _Completed:
            returncode = 0
            stdout = (
                'banner\n{"permission": "ask", "mcp": {"docs.server": {"type": "local"},'
                ' "kirocrew-core": {"type": "local"}}}'
            )
            stderr = ""

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        client = self._client(tmp_path)
        assert client._opencode_config_mcp_servers == ()
        issue, _remedy = opencode_mod._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, _ARGV, "{}"
        )
        assert issue == ""
        assert client._opencode_config_mcp_servers == ("docs.server",)

    def test_only_the_names_opencode_rewrites_are_kept(self):
        from kiro_crew.acp.client import _opencode_config_mcp_server_names

        resolved = {"mcp": {"docs.server": {}, "kirocrew-core": {}, "a_b": {}}}
        assert _opencode_config_mcp_server_names(resolved) == (("docs.server",), "")

    @pytest.mark.parametrize("shape", ["count", "length"])
    def test_a_config_past_the_bounds_refuses_the_session(self, shape, tmp_path, monkeypatch):
        """Bounded by refusing, never by truncating: a dropped name would miss its deny."""
        from kiro_crew.acp.harness_tool_names import (
            MAX_HARNESS_CONFIG_MCP_SERVERS,
            MAX_HARNESS_TOOL_NAME_LEN,
        )

        if shape == "count":
            servers = {f"s.{i}": {} for i in range(MAX_HARNESS_CONFIG_MCP_SERVERS + 1)}
        else:
            servers = {"x" * (MAX_HARNESS_TOOL_NAME_LEN + 1): {}}
        # Names opencode writes as they are do not count toward the bound.
        plain = {f"s{i}": {} for i in range(MAX_HARNESS_CONFIG_MCP_SERVERS + 1)}

        def _completed(mcp):
            class _Completed:
                returncode = 0
                stdout = json.dumps({"permission": "ask", "mcp": mcp})
                stderr = ""

            return _Completed()

        client = self._client(tmp_path)
        monkeypatch.setattr(
            opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _completed(plain)
        )
        assert opencode_mod._verify_opencode_routing(client, ACP_BACKEND_OPENCODE, _ARGV, "{}") == (
            "",
            "",
        )
        monkeypatch.setattr(
            opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _completed(servers)
        )
        issue, remedy = opencode_mod._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, _ARGV, "{}"
        )
        assert "MCP server" in issue and "opencode's own config" in remedy

    def test_the_childs_own_reason_reaches_the_refusal(self, tmp_path, monkeypatch):
        """The child's own reason reaches the refusal, as on the pi read-back."""

        class _Completed:
            returncode = 1
            stdout = ""
            stderr = "Error: cannot parse config at line 3\n"

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = self._verify(self._client(tmp_path), _ARGV, "{}")
        assert "exit 1" in issue
        assert "its configuration could not be parsed" in issue
        # The fault, not the child's sentence: the line number is the harness's to
        # repeat when the operator runs it, and quoting it would put child bytes back
        # into a message that reaches the dashboard and the chat card.
        assert "line 3" not in issue

    def test_a_secret_in_the_childs_stderr_is_not_republished(self, tmp_path, monkeypatch):
        # A 40-char run of the base64 alphabet: the AWS secret-key shape. Not real.
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"

        class _Completed:
            returncode = 1
            stdout = ""
            stderr = f"auth failed for key={secret}\n"

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = self._verify(self._client(tmp_path), _ARGV, "{}")
        assert secret not in issue
        assert issue.endswith("recognises)"), issue

    def test_an_unparseable_document_is_an_issue(self, tmp_path, monkeypatch):
        class _Completed:
            returncode = 0
            stdout = "no json here"

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._verify(self._client(tmp_path), _ARGV, "{}")
        assert "parsed" in issue
        assert "debug config" in remedy

    def test_a_banner_before_the_document_is_tolerated(self, tmp_path, monkeypatch):
        """The harness prints a banner first, so the object is found, not assumed."""

        class _Completed:
            returncode = 0
            stdout = 'opencode 1.18.30\n{"permission": {"*": "ask"}}\n'

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        assert self._verify(self._client(tmp_path), _ARGV, "{}") == ("", "")

    def test_a_permissive_resolved_value_is_refused(self, tmp_path, monkeypatch):
        """The case the whole mechanism exists for: the harness's own default asks
        for nothing, so a session that resolves to it would present a gate that
        gates nothing."""

        class _Completed:
            returncode = 0
            stdout = '{"permission": {"*": "allow"}}'

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._verify(self._client(tmp_path), _ARGV, "{}")
        assert "allow" in issue
        # A CONFIG problem gets the gate's remedy, which names the override that
        # outranks the seed -- not the reinstall advice an exec failure gets. The
        # two remedies name different actions, and only one of them can clear the
        # refusal that produced it.
        assert "higher-precedence" in remedy
        assert "debug config" not in remedy

    def test_a_permissive_agent_level_override_is_refused_by_name(self, tmp_path, monkeypatch):
        """``agent.<name>.permission`` replaces the top-level value for that agent, and
        the seed writes only the top-level key -- so a top-level ``ask`` with a
        permissive agent beneath it would pass the top-level check and run that
        agent's tools past the host gate. Observed live: the resolved document
        keeps each agent's own value under ``agent`` with the seed in force above."""

        class _Completed:
            returncode = 0
            stdout = (
                '{"permission": {"*": "ask"}, "agent": {'
                '"plan": {"permission": {"*": "ask"}, "options": {}}, '
                '"build": {"permission": {"*": "allow"}, "options": {}}}}'
            )

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._verify(self._client(tmp_path), _ARGV, "{}")
        assert "'build'" in issue and "allow" in issue
        assert "plan" not in issue, "an agent that asks is not what the refusal names"
        assert "higher-precedence" in remedy

    def test_a_partial_agent_map_with_one_permissive_tool_is_refused(self, tmp_path, monkeypatch):
        """A per-agent map need not be complete: ``{"bash": "allow"}`` merges over
        the top-level rules for that agent, and that one tool is enough."""

        class _Completed:
            returncode = 0
            stdout = (
                '{"permission": {"*": "ask"}, '
                '"agent": {"plan": {"permission": {"bash": "allow"}, "options": {}}}}'
            )

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = self._verify(self._client(tmp_path), _ARGV, "{}")
        assert "'plan'" in issue and "allow" in issue

    def test_agents_that_ask_or_inherit_are_in_force(self, tmp_path, monkeypatch):
        """An agent with no permission of its own inherits the checked top-level value;
        one that spells ``ask`` itself is equally in force. Neither refuses."""

        class _Completed:
            returncode = 0
            stdout = (
                '{"permission": {"*": "ask"}, "agent": {'
                '"plan": {"permission": {"*": "ask"}, "options": {}}, '
                '"build": {"options": {}}}}'
            )

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        assert self._verify(self._client(tmp_path), _ARGV, "{}") == ("", "")

    def test_the_child_environment_is_scrubbed_like_the_spawns(self, tmp_path, monkeypatch):
        """The read-back child is a FOREIGN harness binary, so it gets the same scrub.

        It runs BEFORE the session spawn that would scrub, so inheriting the
        gateway's environment verbatim would hand a third-party binary every channel
        token, cloud secret and agent socket the spawn path exists to strip -- a few
        lines ahead of the code that strips them. The sandbox wrap the caller applies
        confines the child's filesystem reads; it does not empty its environment,
        which is why both controls are needed.
        """
        seen: dict = {}

        class _Completed:
            returncode = 0
            stdout = '{"permission": "ask"}'

        def _fake_run(argv, **kwargs):
            seen["env"] = kwargs["env"]
            return _Completed()

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", _fake_run)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-travel")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-travel")
        monkeypatch.setenv("KIRO_API_KEY", "should-not-travel")
        self._verify(self._client(tmp_path), _ARGV, '{"permission": "ask"}')

        env = seen["env"]
        for leaked in ("SLACK_BOT_TOKEN", "AWS_SECRET_ACCESS_KEY", "KIRO_API_KEY"):
            assert leaked not in env, f"{leaked} reached the harness's read-back child"
        assert env[_ENV_CONFIG] == '{"permission": "ask"}', "the seed itself must survive"
        assert env.get("PATH"), "the child still needs a PATH to resolve its own tools"

    def test_the_child_sees_the_session_overlay_the_spawn_applies(self, tmp_path, monkeypatch):
        """The read-back resolves config in the SAME environment the session will run in.

        This harness reads its config LOCATION from the environment (``XDG_CONFIG_HOME``,
        ``OPENCODE_CONFIG``), and the spawn applies a per-session overlay -- a cron
        job's ``env`` among its sources -- on top of the gateway's. A read-back that
        skipped the overlay would resolve a different set of config files than the
        session it vouches for, so a permissive value reachable only through the
        overlay would pass verification unseen. The overlay is applied BEFORE the
        scrub, so it cannot smuggle back what the scrub strips either.
        """
        seen: dict = {}

        class _Completed:
            returncode = 0
            stdout = '{"permission": "ask"}'

        def _fake_run(argv, **kwargs):
            seen["env"] = kwargs["env"]
            return _Completed()

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", _fake_run)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_OPENCODE,
            extra_env={
                "XDG_CONFIG_HOME": str(tmp_path / "elsewhere"),
                "SLACK_BOT_TOKEN": "xoxb-overlay-must-not-bypass-the-scrub",
            },
        )
        opencode_mod._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, _ARGV, '{"permission": "ask"}'
        )

        env = seen["env"]
        assert env["XDG_CONFIG_HOME"] == str(tmp_path / "elsewhere")
        assert "SLACK_BOT_TOKEN" not in env

    def test_the_child_gets_the_seed_and_a_bounded_timeout(self, tmp_path, monkeypatch):
        """The read-back must observe the environment the SESSION will run in.

        Reading back without the seed applied would measure the operator's config
        and then start a child with Crew's -- a verdict about a different process
        than the one being spawned.
        """
        seen: dict = {}

        class _Completed:
            returncode = 0
            stdout = '{"permission": "ask"}'

        def _fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _Completed()

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", _fake_run)
        self._verify(self._client(tmp_path), _ARGV, '{"permission": "ask"}')
        # Used VERBATIM: the caller hands over an argv that has already been through
        # the sandbox wrapper, so anything rebuilt here would run unwrapped.
        assert seen["argv"] == _ARGV
        assert seen["kwargs"]["env"][_ENV_CONFIG] == '{"permission": "ask"}'
        assert seen["kwargs"]["timeout"] > 0
        assert seen["kwargs"]["cwd"] == str(tmp_path)
        assert "shell" not in seen["kwargs"], "the read-back must never go through a shell"
        assert seen["kwargs"]["encoding"] == "utf-8"


# ── Placement ────────────────────────────────────────────────────────────────


class TestAgentLevelPermissionWalk:
    """The per-agent overrides the seed cannot reach, reduced the way the top-level is."""

    def test_only_agents_carrying_the_setting_are_returned(self):
        resolved = {
            "agent": {
                "build": {"permission": {"*": "allow"}},
                "plan": {"options": {}},
                "legacy": {"permission": "ask", "mode": "primary"},
            }
        }
        assert _opencode_agent_permissions(resolved, "permission") == [
            ("build", "allow"),
            ("legacy", "ask"),
        ]

    def test_a_mixed_agent_map_keeps_its_spelling(self):
        resolved = {"agent": {"plan": {"permission": {"*": "ask", "bash": "allow"}}}}
        [(name, observed)] = _opencode_agent_permissions(resolved, "permission")
        assert name == "plan" and observed != "ask" and "bash" in str(observed)

    @pytest.mark.parametrize("agents", [None, {}, [], "build", {"build": "allow"}])
    def test_nothing_walkable_is_empty(self, agents):
        assert _opencode_agent_permissions({"agent": agents}, "permission") == []


class _OpencodeLaunchRig:
    """``OpencodeLaunch.resolve_spawn`` driven against fakes at the adapter's own seams.

    The resolution, the session's MCP warm, the sandbox preflight, the sandbox wrap,
    the read-back child and its cleanup each answer a fixed value and record, in
    order, that they ran, on which thread and with what. What a test asserts is what
    the launch DID with them.
    """

    BIN = "/opt/bin/opencode"
    HIDDEN = ("/opt/creds/.aws",)
    SANDBOX = ["/opt/run/kirocrew_sandbox_launcher"]
    CLEANUP = "/opt/run/kirocrew_sandbox_readback"
    MODE = "standard"

    def __init__(self, monkeypatch, tmp_path, *, routing=("", "")):
        self.events: list[tuple] = []
        self.loop_thread: int | None = None
        self.plan = None
        self.client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        self.adapter = opencode_mod.OpencodeLaunch()
        self._tmp_path = tmp_path

        def _record(name, *args):
            self.events.append((name, threading.get_ident(), args))

        async def _launch(backend):
            _record("resolve", backend)
            return self.BIN, [self.BIN, "acp"], "opencode acp", "opencode"

        async def _prepare_session_mcp():
            _record("session_mcp")

        async def _preflight(preflight, backend, mode):
            _record("preflight", preflight, backend, mode)
            return self.HIDDEN

        async def _wrap(argv, **kwargs):
            _record("wrap", list(argv), kwargs)
            return [*self.SANDBOX, *argv], self.CLEANUP

        def _verify(session, backend, argv, config_content):
            _record("verify", list(argv), config_content, session, backend)
            return routing

        def _unlink(path):
            _record("unlink", path)

        monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _launch)
        monkeypatch.setattr(self.client, "_prepare_session_mcp", _prepare_session_mcp)
        monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _preflight)
        monkeypatch.setattr(opencode_mod, "wrap_argv_async", _wrap)
        monkeypatch.setattr(opencode_mod, "_verify_opencode_routing", _verify)
        monkeypatch.setattr(launch_mod, "_unlink_readback_launcher", _unlink)

    def run(self):
        """Resolve the plan on a fresh loop, noting the thread the loop runs on."""

        async def _go():
            self.loop_thread = threading.get_ident()
            return await self.adapter.resolve_spawn(
                SpawnContext(
                    agent="kirocrew",
                    work_dir=self._tmp_path,
                    model=None,
                    environ={},
                    home=self._tmp_path,
                    sandbox_mode=self.MODE,
                    session=self.client,
                )
            )

        self.plan = asyncio.run(_go())
        return self.plan

    def names(self) -> list[str]:
        return [name for name, _thread, _args in self.events]

    def _event(self, name: str) -> tuple:
        hits = [event for event in self.events if event[0] == name]
        assert len(hits) == 1, f"{name} ran {len(hits)} times: {self.names()}"
        return hits[0]

    def args_of(self, name: str) -> tuple:
        return self._event(name)[2]

    def ran_off_the_loop(self, name: str) -> bool:
        assert self.loop_thread is not None
        return self._event(name)[1] != self.loop_thread


def test_the_routing_read_back_runs_off_the_event_loop(monkeypatch, tmp_path) -> None:
    """The read-back spawns a child, so calling it inline would stall the gateway.

    Measured at ~2.3s on a loaded desktop: on the loop that is every dashboard tab
    frozen for the duration of a session start. Observed by the thread the read-back
    ran on, not by timing, because a timing test would pass on a fast host.
    """
    rig = _OpencodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    assert rig.ran_off_the_loop("verify"), "the routing read-back ran on the event loop"


def test_the_sandbox_floor_is_checked_before_any_child_is_started(monkeypatch, tmp_path) -> None:
    """The preflight runs BEFORE the read-back, not after.

    On a host where the credential mask cannot be applied the session is refused
    anyway, so ordering decides whether a foreign binary starts first and is then
    told the session is off. Both calls succeed in isolation and only their order
    carries the property, so the recorded order is what is asserted.
    """
    rig = _OpencodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    names = rig.names()
    assert names.index("preflight") < names.index("wrap") < names.index("verify"), (
        "the sandbox-floor preflight must precede the read-back, or a refused "
        "session still spawns a harness child first"
    )
    preflight, backend, mode = rig.args_of("preflight")
    assert preflight is launch_mod._sandbox_preflight
    assert (backend, mode) == (ACP_BACKEND_OPENCODE, rig.MODE)


def test_the_read_back_child_is_sandbox_wrapped_with_the_adapter_mask(
    monkeypatch, tmp_path
) -> None:
    """The read-back runs the harness's OWN binary, so it gets the session's sandbox.

    This harness resolves its configuration by reading the work dir, and that
    resolution can load a project's plugins -- so an unwrapped read-back would run
    third-party code with the credential homes the mask exists to deny it, moments
    before the masked session spawn.
    """
    rig = _OpencodeLaunchRig(monkeypatch, tmp_path)
    plan = rig.run()
    wrapped, kwargs = rig.args_of("wrap")
    expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_OPENCODE, rig.HIDDEN)
    assert wrapped == [rig.BIN, *acp_client._OPENCODE_CONFIG_READBACK_ARGS]
    assert kwargs["mode"] == rig.MODE
    assert kwargs["extra_hidden_dirs"] == rig.HIDDEN, "the read-back lost the session's mask"
    assert kwargs["extra_expose_files"] == expose
    assert kwargs["strip_python_env"] is True
    assert rig.args_of("verify")[0] == [*rig.SANDBOX, *wrapped], "the read-back ran unwrapped"
    names = rig.names()
    assert rig.args_of("unlink") == (rig.CLEANUP,), "the wrapper's launcher artifact leaked"
    assert names.index("verify") < names.index("unlink")
    assert plan.extra_hidden_dirs == rig.HIDDEN
    assert plan.extra_expose_files == expose


def test_the_read_back_vouches_for_the_seed_the_session_carries(monkeypatch, tmp_path) -> None:
    """The seed the read-back verified is exactly the one the child is started with."""
    rig = _OpencodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    seed = rig.args_of("verify")[1]
    assert json.loads(seed) == {"permission": "ask"}
    env: dict[str, str] = {}
    rig.adapter.apply_spawn_env(env)
    assert env == {_ENV_CONFIG: seed}
    # And the session's MCP array is warmed before any child of this harness starts.
    assert rig.names().index("session_mcp") < rig.names().index("wrap")


def test_a_routing_refusal_is_translated_to_the_acp_layer_type(monkeypatch, tmp_path) -> None:
    """``ensure_ready`` catches ``AcpToolGateUnroutable``, so the raw type escapes it.

    An untranslated refusal matches neither of that method's handlers: the failure
    surfaces untyped AND ``_cleanup_failed_live_spawn`` never runs, leaving the
    spawn it just refused unreaped.
    """
    rig = _OpencodeLaunchRig(monkeypatch, tmp_path, routing=("permissive", "fix it"))
    with pytest.raises(acp_client.AcpToolGateUnroutable) as excinfo:
        rig.run()
    assert type(excinfo.value) is acp_client.AcpToolGateUnroutable
    assert excinfo.value.__suppress_context__, "the gate module's exception leaks as context"
    assert "unlink" in rig.names(), "the read-back's launcher must be reclaimed on refusal too"


def test_a_successful_load_is_adopted_without_a_modes_block() -> None:
    """OpenCode returns no ``modes`` on any result, so the kiro-shaped gate must not apply.

    Every result frame captured off this harness carries ``configOptions`` and never
    ``modes``. Gating adoption on ``modes`` alone would send session/load, get a
    success, fall through to session/new anyway, and discard the conversation the
    harness had just restored -- on every reopened slot, silently.
    """
    from kiro_crew.acp_backends import ACP_BACKENDS_LOAD_WITHOUT_MODES

    assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_LOAD_WITHOUT_MODES
    body = inspect.getsource(AcpClient._initialize_session)
    assert (
        '"modes" in load_resp or self.backend in ACP_BACKENDS_LOAD_WITHOUT_MODES' in body
    ), "a successful opencode load must be adopted even though it carries no modes"


def test_a_resumed_session_is_not_gated_on_a_kiro_transcript() -> None:
    """This harness keeps its own sessions, so a kiro file check would never pass.

    Its ``initialize`` result advertises ``loadSession: true`` and its ids are its
    own, so gating the resume on a kiro transcript path silently starts every
    reopened slot fresh -- the opposite of what the host contract records.
    """
    from kiro_crew.acp_backends import ACP_BACKENDS_HARNESS_OWNED_SESSIONS

    assert ACP_BACKEND_OPENCODE in ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    body = inspect.getsource(AcpClient._initialize_session)
    assert (
        "self.backend in ACP_BACKENDS_HARNESS_OWNED_SESSIONS" in body
    ), "the resume pre-check must be a membership test, not a chain of identities"
    # The resume gate itself must carry no identity test. The ONE identity branch
    # this path legitimately holds is the per-harness MCP-array splice, which every
    # member of ACP_BACKENDS_SESSION_MCP_ARRAY has and which
    # test_harness_parity.test_each_mcp_seam_is_spliced_only_for_its_own_harness
    # requires to be gated -- an ungated splice would hand an opencode session
    # claude's or codex's entries. So this asserts the branch is only ever that
    # splice, rather than that the name is absent: a blanket absence check passed
    # only while this harness had no MCP channel at all, and would have had to be
    # deleted rather than narrowed the moment it got one.
    stray = [
        line.strip()
        for line in body.splitlines()
        if "self._is_opencode" in line and "_opencode_session_mcp_servers()" not in line
    ]
    assert not stray, (
        "an opencode identity test that is not the MCP-array splice sits on the shared "
        f"init path (harness-parity H13): {stray}"
    )


def test_the_read_back_environment_is_built_from_the_spawns_sources(tmp_path, monkeypatch) -> None:
    """The read-back vouches for the session's environment, so it is built the same way.

    Overlay first, then the session's own credential repair, then the scrub: a
    resolver that reintroduces a denied variable must still be scrubbed, and the
    repair must see the overlay the session runs under.
    """
    seen: dict = {}

    def _resolve(env, *, kiro_api_key):
        seen["resolved_from"] = dict(env)
        seen["kiro_api_key"] = kiro_api_key
        return {**env, "SLACK_BOT_TOKEN": "xoxb-a-resolver-reintroduced-this"}

    class _Completed:
        returncode = 0
        stdout = '{"permission": "ask"}'

    def _fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return _Completed()

    monkeypatch.setattr(acp_client, "_resolve_spawn_env", _resolve)
    monkeypatch.setattr(opencode_mod.subprocess_mod, "run", _fake_run)
    # The gateway's own environment is the base the overlay lands on: an ambient config
    # location the session inherits must reach the read-back too.
    monkeypatch.setenv("OPENCODE_CONFIG", str(tmp_path / "ambient.json"))
    client = AcpClient(
        work_dir=tmp_path,
        acp_backend=ACP_BACKEND_OPENCODE,
        extra_env={"XDG_CONFIG_HOME": str(tmp_path / "overlay")},
    )
    opencode_mod._verify_opencode_routing(client, ACP_BACKEND_OPENCODE, _ARGV, "{}")
    assert seen["resolved_from"]["XDG_CONFIG_HOME"] == str(tmp_path / "overlay")
    assert seen["resolved_from"]["OPENCODE_CONFIG"] == str(tmp_path / "ambient.json")
    assert seen["kiro_api_key"] is False, "the read-back child must never carry KIRO_API_KEY"
    assert "SLACK_BOT_TOKEN" not in seen["env"], "the scrub ran before the resolver"


_GITHUB_TOKEN_SHAPE = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
_EXFIL_URL = "https://evil.example.com/collect?data=aGVsbG8gd29ybGQgdGhpcyBpcyBhIHNlY3JldA=="


@pytest.mark.parametrize(
    "document",
    [
        {"permission": f"allow {_GITHUB_TOKEN_SHAPE} {_EXFIL_URL}"},
        {
            "permission": {"*": "ask"},
            "agent": {"build": {"permission": f"allow {_GITHUB_TOKEN_SHAPE} {_EXFIL_URL}"}},
        },
        {
            "permission": {"*": "ask"},
            "agent": {f"x {_GITHUB_TOKEN_SHAPE} {_EXFIL_URL}": {"permission": "allow"}},
        },
    ],
    ids=["top-level-value", "agent-value", "agent-name"],
)
def test_the_observed_value_is_redacted_before_it_reaches_a_refusal(
    tmp_path, monkeypatch, document
) -> None:
    """The refusal text carries a value out of the operator's own config.

    It reaches the dashboard and the chat card, so a credential or an exfiltration URL
    spelled anywhere the refusal quotes -- the top-level value, an agent's value, an
    agent's NAME -- goes through both scrubs before it is published.
    """

    class _Completed:
        returncode = 0
        stdout = json.dumps(document)

    monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    issue, _remedy = opencode_mod._verify_opencode_routing(
        client, ACP_BACKEND_OPENCODE, _ARGV, "{}"
    )
    assert issue, "a permissive document must be refused"
    assert _GITHUB_TOKEN_SHAPE not in issue
    assert "evil.example.com/collect" not in issue
    assert "[REDACTED" in issue


def test_the_read_back_quotes_through_the_scrub_its_session_hands_it(tmp_path) -> None:
    """The scrub is the session's, accepted rather than looked up: whatever the session
    applies to a harness-reported value is what every quoted value in a refusal went
    through -- the top-level value, an agent's value and an agent's name alike."""

    class _Completed:
        returncode = 0
        stdout = json.dumps(
            {"permission": {"*": "ask"}, "agent": {"build": {"permission": "allow"}}}
        )

    seen: list[object] = []

    class _Session:
        _spawn_work_dir = str(tmp_path)
        _extra_env: dict[str, str] = {}
        _opencode_config_mcp_servers: tuple[str, ...] = ()

        @staticmethod
        def _scrub_observed(value):
            seen.append(value)
            return "<scrubbed>" if value == "build" else value

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = opencode_mod._verify_opencode_routing(
            _Session(), ACP_BACKEND_OPENCODE, _ARGV, "{}"
        )

    assert seen == ["ask", "allow", "build"], seen
    assert "'<scrubbed>' overrides it" in issue
    assert "build" not in issue


@pytest.mark.parametrize(
    "routing",
    [
        ("the resolved configuration could not be parsed", "rerun it"),
        ("the session's permission resolves to 'allow'", "remove the override"),
    ],
)
def test_the_spawn_arm_refuses_before_the_first_prompt(monkeypatch, tmp_path, routing) -> None:
    """The refusal has to be reached from the launch itself.

    ``enforce_runtime_routing`` is what turns the read-back's issue into a refused
    session. A read-back whose answer nothing acted on would report the harness
    routed while it ran its own default -- the one silent-bypass shape this
    mechanism exists to close. And an enforced harness reaches the preflight first.
    """
    rig = _OpencodeLaunchRig(monkeypatch, tmp_path, routing=routing)
    with pytest.raises(acp_client.AcpToolGateUnroutable) as excinfo:
        rig.run()
    assert routing[1] in str(excinfo.value)
    assert rig.plan is None
    assert "preflight" in rig.names(), "an enforced harness must reach the preflight"


def test_the_backend_declares_the_verified_mechanism() -> None:
    """Named here so a change of mechanism cannot pass as a refactor."""
    assert routing_for(ACP_BACKEND_OPENCODE) is Routing.VERIFIED_SEEDED_SETTINGS


# ── A per-tool rule from a lower config source ───────────────────────────────


class TestATrailingAskRuleOutranksTheRulesBeforeIt:
    """OpenCode checks rules in order and the LAST match wins; ``"*"`` matches all.

    The seed's ``"*": "ask"`` is merged in AFTER a lower source's per-tool keys,
    because the harness merges sources key by key. Measured on opencode 1.18.30 and
    1.18.32: with ``{"bash": "allow"}`` in the operator's global ``opencode.json`` the
    resolved map is ``{"bash": "allow", "*": "ask"}`` and ``bash`` asks before it
    runs. Main refused that map, so every session on such a host was refused.
    """

    def test_a_trailing_ask_after_an_allowed_tool_is_ask(self):
        assert _opencode_uniform_permission({"bash": "allow", "*": "ask"}) == "ask"

    def test_a_trailing_ask_after_a_pattern_map_is_ask(self):
        raw = {"bash": {"git *": "allow", "*": "ask"}, "edit": "allow", "*": "ask"}
        assert _opencode_uniform_permission(raw) == "ask"

    def test_order_matters_a_leading_ask_is_outranked(self):
        """``{"*": "ask", "bash": "allow"}`` lets ``bash`` win: still refused."""
        observed = _opencode_uniform_permission({"*": "ask", "bash": "allow"})
        assert observed != "ask"
        assert "bash" in str(observed)

    def test_a_trailing_allow_is_not_ask(self):
        observed = _opencode_uniform_permission({"bash": "ask", "*": "allow"})
        assert observed != "ask"

    @pytest.mark.parametrize(
        "raw",
        [
            {"webfetch": "deny", "*": "ask"},
            {"bash": "allow", "webfetch": "deny", "*": "ask"},
            {"bash": {"pwd": "deny", "git *": "allow"}, "*": "ask"},
        ],
    )
    def test_a_deny_the_trailing_ask_would_outrank_stays_refused(self, raw):
        """The trailing ``"*"`` would turn the operator's ``deny`` into a prompt, and
        an auto-approving session would then run the call they switched off. Measured
        live: ``bash: {"pwd": "deny"}`` with ``"*": "ask"`` after it asks and runs
        ``pwd``. So a map carrying any deny is not read as ``ask``."""
        assert _opencode_uniform_permission(raw) != "ask"

    def test_the_read_back_accepts_the_merged_shape(self, tmp_path, monkeypatch):
        class _Completed:
            returncode = 0
            stdout = '{"permission": {"bash": "allow", "edit": "allow", "*": "ask"}}'

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        assert opencode_mod._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, _ARGV, '{"permission": "ask"}'
        ) == ("", "")

    def test_an_agent_map_follows_the_same_rule(self, tmp_path, monkeypatch):
        """The agent-level check shares the reducer, so the order rule holds there."""

        class _Completed:
            returncode = 0
            stdout = (
                '{"permission": {"*": "ask"}, "agent": {'
                '"build": {"permission": {"*": "ask", "bash": "allow"}, "options": {}}}}'
            )

        monkeypatch.setattr(opencode_mod.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        issue, _remedy = opencode_mod._verify_opencode_routing(
            client, ACP_BACKEND_OPENCODE, _ARGV, "{}"
        )
        assert "'build'" in issue
