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

import ast
import inspect
import json
import textwrap

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import (
    OPENCODE_ACP_SUBCMD,
    OPENCODE_BIN,
    OPENCODE_INSTALL_COMMAND,
    PROTOCOL_VERSION_OPENCODE,
    AcpClient,
    _opencode_agent_permissions,
    _opencode_uniform_permission,
    _resolve_opencode_bin,
    _scrub_observed,
)
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
        """
        binary = tmp_path / "opencode"
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(acp_client.platform_compat, "is_executable_file", lambda _path: True)
        monkeypatch.setenv(_ENV_BIN, str(binary))
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: "/never/reached")
        resolved, _searched = _resolve_opencode_bin()
        assert resolved == acp_client._normalize_exe_casing(str(binary)) or str(binary)

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
        resolved, _searched = _resolve_opencode_bin()
        assert resolved == "/opt/mise/opencode"

    def test_mise_is_consulted_before_path(self, monkeypatch):
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: "/opt/mise/opencode")
        monkeypatch.setattr(acp_client.shutil, "which", lambda *_a, **_kw: "/usr/bin/opencode")
        resolved, _searched = _resolve_opencode_bin()
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
        resolved, _searched = _resolve_opencode_bin()
        expected = acp_client._normalize_exe_casing("/usr/bin/opencode") or "/usr/bin/opencode"
        assert resolved == expected
        assert resolved.replace("\\", "/").endswith("/opencode")

    def test_absent_reports_the_path_it_searched(self, monkeypatch):
        """The searched path travels WITH the answer, so the message cannot describe
        a different environment than the one the search ran in."""
        monkeypatch.setattr(acp_client, "_mise_which", lambda _tool: None)
        monkeypatch.setattr(acp_client.shutil, "which", lambda *_a, **_kw: None)
        monkeypatch.setenv("PATH", "/first:/second")
        resolved, searched = _resolve_opencode_bin()
        assert resolved is None
        assert "/first" in searched

    def test_there_is_no_adapter_package_in_the_argv(self):
        """The argv is the binary and its own subcommand: no node, no entry script."""
        assert OPENCODE_ACP_SUBCMD == "acp"
        assert OPENCODE_BIN == "opencode"
        assert "npm" in OPENCODE_INSTALL_COMMAND


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
    body = inspect.getsource(AcpClient._initialize_session)
    assert "_PROTOCOL_VERSION_BY_BACKEND.get(" in body
    assert "PROTOCOL_VERSION_OPENCODE if" not in body


# ── The routing seed ─────────────────────────────────────────────────────────


class TestRoutingSeed:
    """The setting travels in the environment, and it never eats operator config."""

    def _client(self, tmp_path):
        return AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)

    def test_the_seed_carries_the_declared_setting(self, tmp_path):
        seed = json.loads(self._client(tmp_path)._opencode_routing_config())
        assert seed == {"permission": "ask"}

    def test_an_operators_other_keys_survive(self, monkeypatch, tmp_path):
        """Merged over the ambient value, not substituted for it.

        An operator who set this variable did so to configure the harness; dropping
        their model or provider block to deliver one permission key would break the
        session in service of securing it.
        """
        monkeypatch.setenv(
            _ENV_CONFIG, json.dumps({"model": "ollama/qwen3:8b", "permission": "allow"})
        )
        seed = json.loads(self._client(tmp_path)._opencode_routing_config())
        assert seed["model"] == "ollama/qwen3:8b"
        assert seed["permission"] == "ask", "Crew's permission setting must win the merge"

    @pytest.mark.parametrize("ambient", ["not json at all", '"a string"', "[1, 2]"])
    def test_an_unusable_ambient_value_still_yields_the_seed(self, monkeypatch, tmp_path, ambient):
        """A value that is not a JSON object cannot be merged, so the seed stands alone.

        Failing closed the other way -- refusing to seed -- would let a malformed
        environment variable disable the host gate.
        """
        monkeypatch.setenv(_ENV_CONFIG, ambient)
        seed = json.loads(self._client(tmp_path)._opencode_routing_config())
        assert seed == {"permission": "ask"}

    def test_nothing_is_written_into_the_work_dir(self, tmp_path):
        """The whole reason the seed is an env value: a session leaves no trace in a
        checked-out repository, so there is no ownership to arbitrate and no file to
        restore on teardown."""
        before = sorted(p.name for p in tmp_path.iterdir())
        self._client(tmp_path)._opencode_routing_config()
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

    def test_a_missing_binary_is_an_issue(self, tmp_path):
        issue, remedy = self._client(tmp_path)._verify_opencode_routing(
            [str(tmp_path / "not-there"), "debug", "config"], '{"permission": "ask"}'
        )
        assert issue
        assert "debug config" in remedy, "an exec failure gets the harness remedy"

    def test_a_non_zero_exit_is_an_issue(self, tmp_path, monkeypatch):
        class _Completed:
            returncode = 3
            stdout = ""

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._client(tmp_path)._verify_opencode_routing(_ARGV, "{}")
        assert "exit 3" in issue
        assert "debug config" in remedy

    def test_an_unparseable_document_is_an_issue(self, tmp_path, monkeypatch):
        class _Completed:
            returncode = 0
            stdout = "no json here"

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._client(tmp_path)._verify_opencode_routing(_ARGV, "{}")
        assert "parsed" in issue
        assert "debug config" in remedy

    def test_a_banner_before_the_document_is_tolerated(self, tmp_path, monkeypatch):
        """The harness prints a banner first, so the object is found, not assumed."""

        class _Completed:
            returncode = 0
            stdout = 'opencode 1.18.30\n{"permission": {"*": "ask"}}\n'

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        assert self._client(tmp_path)._verify_opencode_routing(_ARGV, "{}") == ("", "")

    def test_a_permissive_resolved_value_is_refused(self, tmp_path, monkeypatch):
        """The case the whole mechanism exists for: the harness's own default asks
        for nothing, so a session that resolves to it would present a gate that
        gates nothing."""

        class _Completed:
            returncode = 0
            stdout = '{"permission": {"*": "allow"}}'

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._client(tmp_path)._verify_opencode_routing(_ARGV, "{}")
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

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, remedy = self._client(tmp_path)._verify_opencode_routing(_ARGV, "{}")
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

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        issue, _remedy = self._client(tmp_path)._verify_opencode_routing(_ARGV, "{}")
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

        monkeypatch.setattr(acp_client.subprocess_mod, "run", lambda *_a, **_kw: _Completed())
        assert self._client(tmp_path)._verify_opencode_routing(_ARGV, "{}") == ("", "")

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

        monkeypatch.setattr(acp_client.subprocess_mod, "run", _fake_run)
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-should-not-travel")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-travel")
        monkeypatch.setenv("KIRO_API_KEY", "should-not-travel")
        self._client(tmp_path)._verify_opencode_routing(_ARGV, '{"permission": "ask"}')

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

        monkeypatch.setattr(acp_client.subprocess_mod, "run", _fake_run)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_OPENCODE,
            extra_env={
                "XDG_CONFIG_HOME": str(tmp_path / "elsewhere"),
                "SLACK_BOT_TOKEN": "xoxb-overlay-must-not-bypass-the-scrub",
            },
        )
        client._verify_opencode_routing(_ARGV, '{"permission": "ask"}')

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

        monkeypatch.setattr(acp_client.subprocess_mod, "run", _fake_run)
        self._client(tmp_path)._verify_opencode_routing(_ARGV, '{"permission": "ask"}')
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


def test_the_routing_read_back_runs_off_the_event_loop() -> None:
    """The read-back spawns a child, so calling it inline would stall the gateway.

    Measured at ~2.3s on a loaded desktop: on the loop that is every dashboard tab
    frozen for the duration of a session start. Pinned structurally rather than by
    timing, because a timing test would pass on a fast host.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(AcpClient._spawn)))
    offloaded = False
    bare_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "to_thread":
            for arg in node.args:
                if isinstance(arg, ast.Attribute) and arg.attr == "_verify_opencode_routing":
                    offloaded = True
        elif isinstance(func, ast.Attribute) and func.attr == "_verify_opencode_routing":
            bare_calls += 1
    assert offloaded, "the routing read-back must be handed to asyncio.to_thread"
    assert bare_calls == 0, "the routing read-back must not be called inline on the loop"


def test_the_sandbox_floor_is_checked_before_any_child_is_started() -> None:
    """The preflight runs BEFORE the read-back, not after.

    On a host where the credential mask cannot be applied the session is refused
    anyway, so ordering decides whether a foreign binary starts first and is then
    told the session is off. Checked by position in the arm, because both calls
    succeed in isolation and only their order carries the property.
    """
    body = inspect.getsource(AcpClient._spawn).split("elif self._is_opencode:", 1)[1]
    body = body.split("        else:", 1)[0]
    preflight_at = body.find("_sandbox_preflight")
    readback_at = body.find("_verify_opencode_routing")
    assert preflight_at != -1 and readback_at != -1
    assert preflight_at < readback_at, (
        "the sandbox-floor preflight must precede the read-back, or a refused "
        "session still spawns a harness child first"
    )


def test_the_read_back_child_is_sandbox_wrapped_with_the_adapter_mask() -> None:
    """The read-back runs the harness's OWN binary, so it gets the session's sandbox.

    This harness resolves its configuration by reading the work dir, and that
    resolution can load a project's plugins -- so an unwrapped read-back would run
    third-party code with the credential homes the mask exists to deny it, moments
    before the masked session spawn. The mask is resolved by the preflight above,
    which is what makes wrapping possible at this point at all.
    """
    body = inspect.getsource(AcpClient._spawn).split("elif self._is_opencode:", 1)[1]
    body = body.split("        else:", 1)[0]
    wrap_at = body.find("wrap_argv_async")
    readback_at = body.find("_verify_opencode_routing")
    assert wrap_at != -1, "the read-back argv must go through the sandbox wrapper"
    assert wrap_at < readback_at, "the wrap must happen before the child is spawned"
    assert (
        "extra_hidden_dirs=adapter_hidden_dirs" in body
    ), "the read-back must carry the same credential mask as the session spawn"
    assert (
        "readback_cleanup" in body
    ), "the wrapper's launcher artifact must be removed once the child has exited"


def test_a_routing_refusal_is_translated_to_the_acp_layer_type() -> None:
    """``ensure_ready`` catches ``AcpToolGateUnroutable``, so the raw type escapes it.

    An untranslated refusal matches neither of that method's handlers: the failure
    surfaces untyped AND ``_cleanup_failed_live_spawn`` never runs, leaving the
    spawn it just refused unreaped. The three sibling call sites all translate.
    """
    body = inspect.getsource(AcpClient._spawn).split("elif self._is_opencode:", 1)[1]
    body = body.split("        else:", 1)[0]
    assert "except acp_tool_gate.ToolGateUnroutable" in body
    assert "raise AcpToolGateUnroutable" in body


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
    assert (
        "self._is_opencode" not in body
    ), "no opencode identity test may sit on the shared init path (harness-parity H13)"


def test_the_read_back_environment_is_built_from_the_spawns_sources() -> None:
    """The read-back vouches for the session's environment, so it is built from the
    same overlay and passed through the same resolver and scrub as the spawn's.
    A pin on the source, because the two live thousands of lines apart and a
    future edit to one would otherwise leave the other vouching for a different
    process.
    """
    body = inspect.getsource(AcpClient._verify_opencode_routing)
    assert "{**os.environ, **self._extra_env}" in body
    assert "_resolve_spawn_env(" in body
    assert "scrub_agent_subprocess_env(" in body


def test_the_observed_value_is_redacted_before_it_reaches_a_refusal() -> None:
    """The refusal text carries a value out of the operator's own config.

    It reaches the dashboard and the chat card, so it goes through the same two
    scrubs every other backend-sourced string in this module does.
    """
    scrub = inspect.getsource(_scrub_observed)
    assert "redact_exfiltration_urls" in scrub
    assert "redact_credentials" in scrub
    body = inspect.getsource(AcpClient._verify_opencode_routing)
    # Both the top-level value and every agent-level one (value AND agent name, which
    # is operator-spelled too) go through it before they reach the refusal text.
    assert body.count("_scrub_observed(") >= 3


def test_the_spawn_arm_refuses_before_the_first_prompt() -> None:
    """The refusal has to be reached from the spawn arm itself.

    ``enforce_runtime_routing`` is what turns the read-back's issue into a refused
    session. A read-back whose answer nothing acted on would report the harness
    routed while it ran its own default -- the one silent-bypass shape this
    mechanism exists to close.
    """
    source = inspect.getsource(AcpClient._spawn)
    arm = source.split("elif self._is_opencode:", 1)
    assert len(arm) == 2, "the opencode spawn arm is gone"
    body = arm[1].split("        else:", 1)[0]
    assert "_verify_opencode_routing" in body
    assert "enforce_runtime_routing" in body
    assert "_sandbox_preflight" in body, "an enforced harness must reach the preflight"


def test_the_backend_declares_the_verified_mechanism() -> None:
    """Named here so a change of mechanism cannot pass as a refactor."""
    assert routing_for(ACP_BACKEND_OPENCODE) is Routing.VERIFIED_SEEDED_SETTINGS
