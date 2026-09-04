"""``kirocrew computer`` — the human-facing CLI, with ``call`` as the new surface.

``call`` is a debug/repro harness over the ten MCP tools, not an eleventh tool. Two
properties carry the weight here and both are asserted structurally rather than by
inspection:

* **It is FULLY GATED.** Every call goes through ``tools.dispatch_tool``, which is
  the same ordered chokepoint the agent traverses — so the primary enable, the
  fail-closed governance gate, the app denylist and the secure-field floors all
  apply. Two tests prove the routing (the disabled refusal and the blocked-app
  refusal come out of the CLI unchanged) rather than trusting the call site.
* **It never mints ``approval_recorded=True``.** That flag means "the Plane-A
  prompt was already enforced on this leg", and the CLI has no prompt. The AST test
  in ``test_computer_use_gate.py`` pins the package-wide producer set; here we pin
  the CLI's own call shape.

``--calls`` exists because ``element_index`` is only meaningful relative to the
``computer_get_state`` that produced it, and that mapping lives in a per-process
cache. The batch test asserts exactly that: an index minted in entry 1 resolves in
entry 2 of the same invocation.

No ctypes anywhere: the suite-wide ``_fake_computer_use_backend`` fixture
(``conftest.py``) has already registered ``FakeComputerUseBackend`` process-wide.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from kiro_crew.computer_use import backend as cu_backend
from kiro_crew.computer_use import cli as cu_cli
from kiro_crew.computer_use import index as cu_index
from kiro_crew.computer_use import service as cu_service
from kiro_crew.computer_use.types import (
    ERROR_PREFIX,
    REFUSAL_DISABLED,
    TOOL_CLICK,
    TOOL_END_TURN,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
)
from kiro_crew.testing.fake_computer_use import FAKE_FILES_APP

# ── Fixtures ──


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect KIROCREW_HOME so the keystone read lands in a tmp dir."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_forged_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's real env leak an identity into these tests."""
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)


@pytest.fixture
def cli_opt_in(home: Path) -> Path:
    """Grant the keystone ``allow_cli_diagnostics`` opt-in.

    Required by ``apps``/``call`` on any invocation without an HMAC-verified
    identity — which is every unsigned one, including a test's. This is the
    operator's consent, and it lives in the keystone precisely because a process
    cannot prove it is attended.
    """
    state = home / "computer_use.json"
    body = json.loads(state.read_text()) if state.exists() else {}
    body["allow_cli_diagnostics"] = True
    state.write_text(json.dumps(body), encoding="utf-8")
    return state


@pytest.fixture
def enabled(home: Path) -> Path:
    """Primary enable ON, plus the CLI diagnostics opt-in.

    Both, because they answer different questions: ``enabled`` is "may computer use
    run at all", ``allow_cli_diagnostics`` is "may an invocation whose caller cannot
    be authenticated run it". A human using the CLI sets both;
    ``TestUnprovenCallerIsRefused`` covers the case where only the first is set.
    """
    state = home / "computer_use.json"
    state.write_text(json.dumps({"enabled": True, "allow_cli_diagnostics": True}), encoding="utf-8")
    return state


@pytest.fixture(autouse=True)
def fresh_singletons():
    """Drop the shared service/index between tests.

    The snapshot cache is process-wide and TTL'd, so a leaked entry from one test
    would make another test's "no state for …" assertion pass for the wrong
    reason (or fail outright).
    """
    cu_service.reset_shared_service()
    cu_index.reset_shared_index()
    cu_backend.reset_shared_backend()
    yield
    cu_service.reset_shared_service()
    cu_index.reset_shared_index()
    cu_backend.reset_shared_backend()


# ──────────────────────────────────────────────────────────────────────────
# A. Argument parsing — the shape, never the per-field types
# ──────────────────────────────────────────────────────────────────────────
class TestParseCalls:
    """``_parse_calls`` turns argv into ``(tool, args)`` and refuses ambiguity."""

    def test_single_tool_with_no_arguments(self):
        assert cu_cli._parse_calls([TOOL_LIST_APPS]) == [(TOOL_LIST_APPS, {})]

    def test_key_value_arguments_are_json_coerced_when_they_can_be(self):
        """``element_index=3`` MUST arrive as an int — the schema rejects "3"."""
        calls = cu_cli._parse_calls([TOOL_CLICK, "app=Finder", "element_index=3"])
        assert calls == [(TOOL_CLICK, {"app": "Finder", "element_index": 3})]

    def test_bools_and_floats_coerce_and_plain_words_stay_strings(self):
        calls = cu_cli._parse_calls(
            [TOOL_GET_STATE, "app=Fake Files", "screenshot=false", "text_limit=120"]
        )
        args = calls[0][1]
        assert args["screenshot"] is False
        assert args["text_limit"] == 120
        # Not valid JSON, so it stays the string the user typed.
        assert args["app"] == "Fake Files"

    def test_a_json_string_literal_is_kept_as_text_not_unwrapped(self):
        """A value that already decodes to ``str`` gains nothing from the trip.

        Keeping it as text is what makes ``text=null`` the four-character string
        it looks like at a prompt rather than a ``None`` the schema cannot type.
        """
        assert cu_cli._coerce("null") == "null"
        assert cu_cli._coerce('"Finder"') == '"Finder"'
        assert cu_cli._coerce("[1, 2]") == "[1, 2]"
        assert cu_cli._coerce("hello there") == "hello there"

    @pytest.mark.parametrize(
        "argv, fragment",
        [
            ([], "needs a tool name"),
            (["--json"], "expected a tool name"),
            ([TOOL_CLICK, "app"], "expected key=value"),
            ([TOOL_CLICK, "=Finder"], "expected key=value"),
            ([TOOL_CLICK, "app=A", "app=B"], "given twice"),
            ([TOOL_CLICK, "--weird"], "unknown flag"),
            (["--calls"], "needs a JSON array"),
            (["--calls", "[]", "[]"], "exactly one JSON array"),
            ([TOOL_CLICK, "--calls", "[]"], "not both"),
            (["--calls", "{"], "not valid JSON"),
            (["--calls", "{}"], "must be a JSON array"),
            (["--calls", "[]"], "array is empty"),
            (["--calls", "[3]"], "must be a JSON object"),
            (["--calls", '[{"args": {}}]'], "non-empty string 'tool'"),
            (["--calls", '[{"tool": ""}]'], "non-empty string 'tool'"),
            (["--calls", '[{"tool": "x", "args": 3}]'], "'args' must be a JSON object"),
            (["--calls", '[{"tool": "x", "arg": {}}]'], "unknown key(s): arg"),
        ],
    )
    def test_malformed_input_raises_a_user_facing_sentence(self, argv, fragment):
        with pytest.raises(ValueError) as excinfo:
            cu_cli._parse_calls(argv)
        assert fragment in str(excinfo.value)

    def test_a_misspelled_args_key_is_refused_not_silently_dropped(self):
        """``arg``/``arguments`` must not run the tool with NO arguments.

        For a real desktop action a silently-argumentless call is a DIFFERENT
        call, not a no-op — so the typo is an error rather than an omission.
        """
        with pytest.raises(ValueError):
            cu_cli._parse_calls(["--calls", '[{"tool": "computer_click", "arguments": {}}]'])

    def test_batch_omitting_args_defaults_to_empty(self):
        calls = cu_cli._parse_calls(["--calls", f'[{{"tool": "{TOOL_END_TURN}"}}]'])
        assert calls == [(TOOL_END_TURN, {})]

    def test_batch_null_args_is_treated_as_empty(self):
        calls = cu_cli._parse_calls(["--calls", f'[{{"tool": "{TOOL_END_TURN}", "args": null}}]'])
        assert calls == [(TOOL_END_TURN, {})]

    def test_batch_preserves_order(self):
        raw = json.dumps(
            [
                {"tool": TOOL_GET_STATE, "args": {"app": "Fake Files"}},
                {"tool": TOOL_END_TURN},
            ]
        )
        calls = cu_cli._parse_calls(["--calls", raw])
        assert [name for name, _ in calls] == [TOOL_GET_STATE, TOOL_END_TURN]


# ──────────────────────────────────────────────────────────────────────────
# B. Execution — one process, one snapshot cache
# ──────────────────────────────────────────────────────────────────────────
class TestRunCalls:
    """``run_calls`` is the programmatic form of ``call --calls``."""

    def test_a_batch_shares_the_element_index_cache(self, enabled):
        """The whole reason ``--calls`` exists.

        Entry 1 mints indices; entry 2 resolves one of them. Two separate CLI
        invocations could not do this — the cache is per-process — so this is the
        property the batch form buys.
        """
        replies = cu_cli.run_calls(
            [
                (TOOL_GET_STATE, {"app": FAKE_FILES_APP.name}),
                # Element 3 is the fixture's pressable "Back" button.
                (TOOL_CLICK, {"app": FAKE_FILES_APP.name, "element_index": 3}),
            ]
        )
        assert not replies[0].startswith(ERROR_PREFIX), replies[0]
        assert not replies[1].startswith(ERROR_PREFIX), replies[1]

    def test_acting_without_a_snapshot_is_refused_as_it_is_for_the_agent(self, enabled):
        reply = cu_cli.run_calls([(TOOL_CLICK, {"app": FAKE_FILES_APP.name, "element_index": 4})])[
            0
        ]
        assert reply.startswith(ERROR_PREFIX)
        assert "computer_get_state" in reply

    def test_a_batch_does_not_abort_at_the_first_error(self, enabled):
        """A reproduction is more useful whole than truncated."""
        replies = cu_cli.run_calls(
            [
                (TOOL_CLICK, {"app": FAKE_FILES_APP.name, "element_index": 9}),
                (TOOL_LIST_APPS, {}),
            ]
        )
        assert replies[0].startswith(ERROR_PREFIX)
        assert not replies[1].startswith(ERROR_PREFIX)

    def test_the_primary_enable_gates_the_cli_exactly_like_the_agent(self, cli_opt_in):
        """Opt-in granted, primary enable absent -> the same refusal the MCP path returns."""
        reply = cu_cli.run_calls([(TOOL_LIST_APPS, {})])[0]
        assert reply == f"{ERROR_PREFIX}{REFUSAL_DISABLED}"

    def test_an_unknown_tool_is_refused_by_the_chokepoint_not_by_the_cli(self, enabled):
        """The CLI does not keep its own tool list to drift from the schemas."""
        reply = cu_cli.run_calls([("computer_teleport", {})])[0]
        assert reply.startswith(ERROR_PREFIX)

    def test_bad_arguments_surface_the_schema_refusal(self, enabled):
        reply = cu_cli.run_calls([(TOOL_GET_STATE, {"nope": 1})])[0]
        assert reply.startswith(ERROR_PREFIX)


# ──────────────────────────────────────────────────────────────────────────
# C. The command surface (stdout, exit codes, dispatch)
# ──────────────────────────────────────────────────────────────────────────
class TestCommandSurface:
    def test_call_prints_the_reply_and_exits_zero_on_success(self, enabled, capsys):
        cu_cli.run_computer(["call", TOOL_LIST_APPS])
        out = capsys.readouterr().out
        assert FAKE_FILES_APP.name in out

    def test_call_exits_nonzero_when_a_reply_is_an_error(self, cli_opt_in, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cu_cli.run_computer(["call", TOOL_LIST_APPS])
        assert excinfo.value.code == cu_cli._EXIT_PROBLEM
        assert REFUSAL_DISABLED in capsys.readouterr().out

    def test_a_single_reply_is_printed_without_a_header(self, enabled, capsys):
        """So it stays pasteable — a header would corrupt a copied tree."""
        cu_cli.run_computer(["call", TOOL_LIST_APPS])
        assert "──" not in capsys.readouterr().out

    def test_a_batch_labels_each_step(self, enabled, capsys):
        raw = json.dumps([{"tool": TOOL_LIST_APPS}, {"tool": TOOL_END_TURN}])
        cu_cli.run_computer(["call", "--calls", raw])
        out = capsys.readouterr().out
        assert f"1/2 {TOOL_LIST_APPS}" in out
        assert f"2/2 {TOOL_END_TURN}" in out

    def test_json_output_is_an_array_of_tool_and_text(self, enabled, capsys):
        cu_cli.run_computer(["call", TOOL_LIST_APPS, "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert isinstance(payload, list) and len(payload) == 1
        assert payload[0]["tool"] == TOOL_LIST_APPS
        assert isinstance(payload[0]["text"], str)

    def test_json_output_still_exits_nonzero_on_a_refusal(self, cli_opt_in, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cu_cli.run_computer(["call", TOOL_LIST_APPS, "--json"])
        assert excinfo.value.code == cu_cli._EXIT_PROBLEM
        assert json.loads(capsys.readouterr().out)[0]["text"].startswith(ERROR_PREFIX)

    def test_a_parse_error_prints_the_usage_and_exits_nonzero(self, enabled, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cu_cli.run_computer(["call"])
        assert excinfo.value.code == cu_cli._EXIT_PROBLEM
        err = capsys.readouterr().err
        assert "needs a tool name" in err
        assert "Commands:" in err

    def test_call_is_advertised_in_the_usage_text(self, capsys):
        cu_cli.run_computer([])
        out = capsys.readouterr().out
        assert "call <tool>" in out
        assert "--calls" in out

    def test_an_unknown_subcommand_is_still_refused(self, capsys):
        with pytest.raises(SystemExit):
            cu_cli.run_computer(["frobnicate"])
        assert "Unknown command" in capsys.readouterr().err


# ──────────────────────────────────────────────────────────────────────────
# D. The security properties of the new verb, asserted structurally
# ──────────────────────────────────────────────────────────────────────────
class TestCallIsNotABypass:
    def test_every_cli_call_goes_through_the_gated_dispatcher(self):
        """Structural, not behavioural: pin that the CLI calls the CHOKEPOINT.

        A future edit reaching into ``service`` directly (to "skip the overhead",
        say) would keep every behavioural test above green while dropping
        governance, the denylist and the observation ceiling on the floor. So this
        asserts over the AST: no function in this module may call a
        ``service.get_shared_service()`` mutator, and the only tool execution path
        is ``tools.dispatch_tool``.
        """
        source = inspect.getsource(cu_cli)
        tree = ast.parse(source)
        dispatch_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "dispatch_tool":
                dispatch_calls += 1
        assert dispatch_calls >= 1, "the CLI must execute tools via tools.dispatch_tool"
        # NOTHING may read the desktop through the service any more. ``list_apps``
        # used to be allowed here as "a diagnostic in the operator's own terminal",
        # but the agent can run this command with bash — so it was an ungated read
        # of every window TITLE that worked with the feature disabled, in an
        # unattended session, and under a policy banning computer use (reviewer
        # finding). Only ``doctor``'s two capability probes remain, and neither
        # returns any desktop content.
        service_attrs = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Attribute)
            and node.func.value.func.attr == "get_shared_service"
        }
        assert service_attrs <= {"status", "probe_permissions"}, service_attrs

    def test_the_cli_never_mints_the_approval_recorded_flag(self):
        """There is no prompt on this leg, so the flag would be a weaker path."""
        tree = ast.parse(inspect.getsource(cu_cli))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                assert kw.arg != "approval_recorded", "the CLI must not claim an approval"

    def test_the_apps_command_is_refused_while_the_feature_is_disabled(self, cli_opt_in, capsys):
        """It reads window titles, so it is gated exactly like every other read.

        The CLI opt-in is granted here so the refusal under test is the PRIMARY
        enable's, not the diagnostics guard's — the two are independent.
        """
        cu_cli.run_computer(["apps"])
        assert REFUSAL_DISABLED in capsys.readouterr().out

    def test_the_apps_command_still_lists_apps_when_enabled(self, enabled, capsys):
        cu_cli.run_computer(["apps"])
        assert FAKE_FILES_APP.name in capsys.readouterr().out
