"""The ``kirocrew-computer`` MCP server — a THIN SHIM (``mcp_computer.py``).

The stdio process implements nothing: it resolves the session STRICTLY, forwards over
loopback with the internal-secret handshake, and relays text. Governance, capture and
SEL auditing happen in the GATEWAY, because the PreToolUse gate
(``hooks._governance_denial``) fails OPEN by policy. The fail-CLOSED gate is the
keystone enable at the top of ``computer_use/tools.py``'s chokepoint.

Two halves are tested here: the shim (``_list_tools`` / ``_call_tool`` with HTTP
stubbed) and the gateway dispatcher (``dispatch_tool`` against
``FakeComputerUseBackend``). Pointer verbs and the action header are in
``test_mcp_computer_actions.py``; sessions in ``test_mcp_computer_sessions.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from mcp_computer_helpers import fake_backend_fixture  # noqa: F401
from mcp_computer_helpers import home_fixture  # noqa: F401
from mcp_computer_helpers import keystone_fixture  # noqa: F401
from mcp_computer_helpers import _SESSION, _dispatch, _enable, _get_state

from kiro_crew import mcp_computer, mcp_core
from kiro_crew.computer_use import index as cu_index
from kiro_crew.computer_use import tools as cu_tools
from kiro_crew.computer_use.types import (
    ALL_TOOLS,
    ERROR_PREFIX,
    MUTATING_TOOLS,
    READ_ONLY_TOOLS,
    SECURE_PLACEHOLDER,
    TOOL_CLICK,
    TOOL_DRAG,
    TOOL_END_TURN,
    TOOL_GET_STATE,
    TOOL_LIST_APPS,
    TOOL_PERFORM_ACTION,
    TOOL_PRESS_KEY,
    TOOL_SCROLL,
    TOOL_SET_VALUE,
    TOOL_TYPE_TEXT,
)
from kiro_crew.testing.fake_computer_use import (
    FAKE_CREDENTIAL_FIXTURE,
    FAKE_FILES_APP,
    FAKE_LOGIN_APP,
    FAKE_SECRET_VALUE,
    FakeComputerUseBackend,
)
from kiro_crew.validation import MCP_COMPUTER_SCHEMAS, ValidationError


@pytest.fixture
def ceiling(home: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a governance POLICY ceiling for the duration of one test.

    Mirrors ``test_computer_use_gate.py::_install_ceiling``. ``_PROFILES_DIR`` is
    pinned under the tmp home so a developer's own ``~/.kiro/crew/profiles`` cannot
    decide the outcome.
    """
    import dataclasses

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform import context as ctx_mod
    from kiro_crew.platform import governance_profiles as gp
    from kiro_crew.platform.bootstrap import build_default_context
    from kiro_crew.platform.governance import parse_policy

    profiles = home / "profiles"
    profiles.mkdir(exist_ok=True)
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()

    def _install(computer_use: dict) -> None:
        base = build_default_context(KiroCrewConfig.load())
        body = {"version": 1, "boot": {"fail_closed": True}, "computer_use": computer_use}
        ctx_mod.set_context(dataclasses.replace(base, governance=parse_policy(body)))

    yield _install
    ctx_mod.reset_context()
    gp.reset_store()


# ── tools/list visibility ──


def test_tools_list_is_empty_when_the_keystone_says_disabled(home: Path):
    """A disabled feature is INVISIBLE, not nine tools that always refuse.

    kiro-cli caches ``tools/list`` once per session, so advertising them would
    spend context on capabilities the model can never use and invite retry loops.
    """
    assert mcp_computer._list_tools() == []


def test_tools_list_is_empty_when_the_keystone_is_corrupt(keystone: Path):
    """Fail-CLOSED: an unreadable primary enable means "not proven on"."""
    keystone.write_text("{not json", encoding="utf-8")
    assert mcp_computer._list_tools() == []


def test_tools_list_is_empty_for_a_truthy_non_true_enabled(keystone: Path):
    """``"enabled": "false"`` (a truthy string) must not enable desktop control.

    The only spelling that enables the feature is a real JSON ``true``.
    """
    keystone.write_text(json.dumps({"enabled": "false"}), encoding="utf-8")
    assert mcp_computer._list_tools() == []
    keystone.write_text(json.dumps({"enabled": 1}), encoding="utf-8")
    assert mcp_computer._list_tools() == []


def test_tools_list_advertises_every_tool_when_enabled(keystone: Path):
    """Enabled: exactly the shipped tools, each with a name and an input schema.

    Counted from ``ALL_TOOLS`` rather than against a literal, so adding a tool means
    editing the ONE registration tuple — the parity tests below then force the schema
    and the class-table rows to follow.
    """
    _enable(keystone)
    listed = mcp_computer._list_tools()
    assert [t["name"] for t in listed] == list(ALL_TOOLS)
    assert len(listed) == len(ALL_TOOLS)
    for tool in listed:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_disabled_shim_records_zero_gateway_calls(home: Path, monkeypatch: pytest.MonkeyPatch):
    """A call while disabled must not reach the wire at all.

    ``_list_tools`` hides the tools, but kiro-cli caches the list for the life of a
    session — so a session that started while the feature was on keeps offering
    them after the operator turns it off. That re-check is what this asserts.
    """
    posted: list[Any] = []
    monkeypatch.setattr(mcp_computer, "_invoke", lambda *a, **k: posted.append(a) or {"text": "ok"})
    for tool in ALL_TOOLS:
        result = mcp_computer._call_tool_inner(tool, {})
        assert result.startswith(ERROR_PREFIX)
        assert "Settings" in result, "the refusal must tell the operator where to enable it"
    assert posted == [], "no gateway call may be made while the feature is disabled"


# ── schema parity ──


@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_every_tool_has_a_validation_schema(tool: str):
    """**Parity, per tool.** Every advertised name is a ``MCP_COMPUTER_SCHEMAS`` key.

    Mandatory, not tidiness: an unregistered tool's arguments would pass RAW
    through validation, and a ``ValidationError`` raised deeper inside a handler
    escapes the stdio loop and kills the server.
    """
    assert tool in MCP_COMPUTER_SCHEMAS
    assert MCP_COMPUTER_SCHEMAS[tool].tool_name == tool


def test_schemas_contain_no_extra_tools():
    """And no schema exists for a tool the server does not advertise."""
    assert set(MCP_COMPUTER_SCHEMAS) == set(ALL_TOOLS)


def test_advertised_schema_matches_the_validated_fields(keystone: Path):
    """Advertised properties are a subset of what validation accepts.

    The schemas are the ENFORCEMENT point and the advertisement is documentation,
    so a mismatch is not a hole — but it produces a confusing "unknown field"
    error for a parameter the model was told about, which is worth pinning.
    """
    _enable(keystone)
    for tool in mcp_computer._list_tools():
        advertised = set(tool["inputSchema"].get("properties") or {})
        validated = {field.name for field in MCP_COMPUTER_SCHEMAS[tool["name"]].fields}
        assert advertised <= validated, f"{tool['name']} advertises un-validated fields"


def test_every_REQUIRED_validated_field_is_advertised_as_required(keystone: Path):
    """The other direction, which is not merely cosmetic.

    A ``required`` list looser than the validator's teaches the model a call shape
    that is always refused, and it retries the same rejected call.
    """
    _enable(keystone)
    for tool in mcp_computer._list_tools():
        advertised_required = set(tool["inputSchema"].get("required") or ())
        validated_required = {
            field.name for field in MCP_COMPUTER_SCHEMAS[tool["name"]].fields if field.required
        }
        missing = validated_required - advertised_required
        assert not missing, (
            f"{tool['name']} does not advertise required field(s) {sorted(missing)} that "
            "validation always demands — every conforming call would be refused"
        )


@pytest.mark.parametrize("tool", sorted(cu_tools._ELEMENT_REQUIRED_TOOLS))
def test_element_required_tools_demand_the_index_in_the_validator_too(tool: str):
    """**The third layer.** ``MCP_COMPUTER_SCHEMAS`` must require what the chokepoint does.

    The parity tests above compare the advertised schema with the validator; this pins
    the validator against ``tools._ELEMENT_REQUIRED_TOOLS``. Only ``computer_click``
    may take coordinates instead of an index.
    """
    required = {field.name for field in MCP_COMPUTER_SCHEMAS[tool].fields if field.required}
    assert "element_index" in required, (
        f"{tool} is in _ELEMENT_REQUIRED_TOOLS but its validation schema does not "
        "require element_index — an indexless call would pass validation and be "
        "refused one layer later, and an unnamed target has no role/subrole for the "
        "always-on secure-field refusal to inspect"
    )


def test_unknown_tool_is_rejected_with_no_raw_passthrough():
    """There is no raw pass-through fallback for an unregistered tool name.

    Unlike the cron/core servers: a computer-use tool reaching a handler with
    unvalidated arguments could synthesize input into a live window.
    """
    with pytest.raises(ValidationError) as excinfo:
        mcp_computer._validate_args("computer_teleport", {})
    assert "computer_teleport" in str(excinfo.value)


# ── argument validation ──


@pytest.mark.parametrize(
    "index",
    [
        # ``isinstance(True, int)`` is True in Python, so a bool would otherwise pass
        # an int field AND its range check, and address element 1, some other widget.
        pytest.param(True, id="bool"),
        # A float index is not an index; refuse rather than truncate.
        pytest.param(3.7, id="float"),
    ],
)
def test_element_index_of_a_non_int_type_is_rejected(index):
    with pytest.raises(ValidationError):
        mcp_computer._validate_args(TOOL_CLICK, {"app": "Finder", "element_index": index})


def test_element_index_out_of_range_is_rejected():
    with pytest.raises(ValidationError):
        mcp_computer._validate_args(TOOL_CLICK, {"app": "Finder", "element_index": -1})
    with pytest.raises(ValidationError):
        mcp_computer._validate_args(TOOL_CLICK, {"app": "Finder", "element_index": 10**9})


def test_required_arguments_are_enforced():
    with pytest.raises(ValidationError):
        mcp_computer._validate_args(TOOL_TYPE_TEXT, {"app": "Finder"})
    with pytest.raises(ValidationError):
        mcp_computer._validate_args(TOOL_SET_VALUE, {"app": "Finder", "element_index": 0})
    # ``computer_click``'s target is NOT schema-required: it accepts either an
    # element index or coordinates, which is a CROSS-FIELD rule the field-by-field
    # validator has no vocabulary for. It is enforced at the dispatch chokepoint
    # instead (``policy.check_click_target``) — see
    # ``TestClickTargeting::test_neither_target_form_is_refused``.
    assert mcp_computer._validate_args(TOOL_CLICK, {"app": "Finder"}) == {"app": "Finder"}
    for missing in ("from_x", "from_y", "to_x", "to_y"):
        args = {"app": "Finder", "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4}
        args.pop(missing)
        with pytest.raises(ValidationError):
            mcp_computer._validate_args(TOOL_DRAG, args)


def test_scroll_direction_is_an_enum():
    with pytest.raises(ValidationError):
        mcp_computer._validate_args(
            TOOL_SCROLL, {"app": "Finder", "element_index": 0, "direction": "sideways"}
        )
    cleaned = mcp_computer._validate_args(
        TOOL_SCROLL, {"app": "Finder", "element_index": 0, "direction": "down"}
    )
    assert cleaned["direction"] == "down"


def test_oversized_text_is_rejected():
    """The type-text cap is enforced at the schema, before anything is typed."""
    from kiro_crew.computer_use.types import MAX_TYPE_TEXT_LEN

    with pytest.raises(ValidationError):
        mcp_computer._validate_args(
            TOOL_TYPE_TEXT, {"app": "Finder", "text": "x" * (MAX_TYPE_TEXT_LEN + 1)}
        )


# ── strict session identity ──


def test_shim_uses_the_strict_session_resolver_not_the_lenient_one():
    """Strict identity only, routed through the shared reflexive-tool gate.

    The lenient resolver walks ``/proc`` ancestors over ``session_pid_<pid>.txt``,
    which ``mcp_core`` itself documents as "agent-writable and therefore
    forgeable" — a forged file could resolve a looser attended profile. Asserted
    over the module's source so a future edit cannot quietly swap the resolver.
    """
    src = inspect.getsource(mcp_computer)
    assert "require_strict_session_key" in src
    assert "_resolve_session_key_strict" in src
    # The lenient name must not appear as a CALL. (It is a prefix of the strict
    # name, so compare call forms rather than substrings.)
    assert "_resolve_session_key()" not in src


def test_an_unresolved_session_key_PROCEEDS_with_an_empty_identity(
    keystone: Path, monkeypatch: pytest.MonkeyPatch
):
    """**An unresolved identity is NOT a refusal**; it proceeds, empty.

    A GUI-launched kiro-cli on macOS has neither accepted identity source, so a refusal
    would make the feature unusable there. Only audit attribution is lost; the key is a
    per-process placeholder (see ``TestUnresolvedSessionsAreNamespaced``).
    """
    _enable(keystone)
    posted: list[Any] = []
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    monkeypatch.setattr(mcp_computer, "_invoke", lambda *a, **k: posted.append(a) or {"text": "ok"})
    result = mcp_computer._call_tool_inner(TOOL_LIST_APPS, {})
    assert not result.startswith(ERROR_PREFIX), result
    assert result == "ok"
    # It reached the wire, carrying the unresolved placeholder rather than a guessed
    # identity — and NOT an empty string, which would alias every such session.
    assert posted
    assert posted[0][0].startswith(mcp_computer.UNRESOLVED_SESSION_PREFIX)


def test_the_shim_carries_no_identity_refusal_at_all(keystone: Path):
    """Guard the removal itself, so it cannot creep back as a "small" safety net.

    An ``if not session_key: return refusal`` line breaks macOS. Asserted as an absence
    because the test above also passes with an unreachable refusal.
    """
    src = inspect.getsource(mcp_computer)
    assert "could not be identified" not in src
    assert "ERR_NO_SESSION" not in src


def test_resolved_session_key_is_forwarded_in_body_and_header(
    keystone: Path, monkeypatch: pytest.MonkeyPatch
):
    """The gateway learns the calling surface from the shim's resolved key."""
    _enable(keystone)
    seen: dict[str, Any] = {}

    def _invoke(session_key: str, name: str, args: dict) -> dict:
        seen.update({"session_key": session_key, "name": name, "args": args})
        return {"text": "App=…"}

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: _SESSION)
    monkeypatch.setattr(mcp_computer, "_invoke", _invoke)
    assert mcp_computer._call_tool_inner(TOOL_LIST_APPS, {}) == "App=…"
    assert seen["session_key"] == _SESSION
    assert seen["name"] == TOOL_LIST_APPS


def test_non_latin1_session_key_is_refused_with_an_actionable_message(
    keystone: Path, monkeypatch: pytest.MonkeyPatch
):
    """An em-dash in a tab title cannot go in an HTTP header.

    ``http.client`` encodes header values as latin-1, so this would otherwise
    surface as a raw ``UnicodeEncodeError`` instead of "rename the tab".
    """
    _enable(keystone)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "dashboard:tab—1")
    monkeypatch.setattr(mcp_computer, "_invoke", lambda *a, **k: {"text": "ok"})
    result = mcp_computer._call_tool_inner(TOOL_LIST_APPS, {})
    assert result.startswith(ERROR_PREFIX)
    assert "latin-1" in result


# ── no native code in the shim ──


def test_shim_module_does_not_import_ctypes():
    """The shim must load NO framework and touch NO accessibility API.

    A ctypes fault is not catchable in Python, so keeping the native graph out of
    the process kiro-cli talks to is what stops a driver fault from taking the MCP
    server down mid-session. Asserted three ways: no bound attribute, no import
    statement, and no native computer-use module imported at module scope.
    """
    assert not hasattr(mcp_computer, "ctypes")
    src = inspect.getsource(mcp_computer)
    assert "import ctypes" not in src
    for native in ("macos_ffi", "macos_driver", "snapshot_macos", "capture_macos", "apps_macos"):
        assert native not in src, f"the shim must not reference {native}"


def test_shim_does_not_import_the_dispatcher():
    """The shim forwards; it does not dispatch.

    Importing ``computer_use.tools`` here would pull the service and (on macOS) the
    native driver into the sidecar, defeating the whole thin-shim architecture.
    """
    src = inspect.getsource(mcp_computer)
    assert "computer_use.tools" not in src
    assert "dispatch_tool" not in src


# ── transport behaviour ──


def test_transport_failure_is_reported_as_an_actionable_error(
    keystone: Path, monkeypatch: pytest.MonkeyPatch
):
    """An unreachable gateway must be diagnosable, not an opaque internal error."""
    _enable(keystone)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: _SESSION)
    monkeypatch.setattr(
        mcp_computer,
        "_invoke",
        lambda *a, **k: {"error": mcp_computer.ERR_GATEWAY_UNREACHABLE.format(detail="refused")},
    )
    result = mcp_computer._call_tool_inner(TOOL_LIST_APPS, {})
    assert result.startswith(ERROR_PREFIX)
    assert "gateway" in result


def test_gateway_refusal_text_is_relayed_verbatim(keystone: Path, monkeypatch: pytest.MonkeyPatch):
    """A gateway refusal is a TOOL RESULT, relayed unchanged.

    The gateway answers 200 with ``{"text": "Error: …"}`` for both success and
    refusal, because a refusal the model can read and explain beats a transport
    failure it cannot reason about.
    """
    _enable(keystone)
    refusal = f"{ERROR_PREFIX}Blocked by governance policy: capability disabled"
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: _SESSION)
    monkeypatch.setattr(mcp_computer, "_invoke", lambda *a, **k: {"text": refusal})
    assert mcp_computer._call_tool_inner(TOOL_CLICK, {}) == refusal


def test_empty_gateway_body_is_an_error_not_a_silent_success(
    keystone: Path, monkeypatch: pytest.MonkeyPatch
):
    """A body with neither text nor error must not read as success."""
    _enable(keystone)
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: _SESSION)
    monkeypatch.setattr(mcp_computer, "_invoke", lambda *a, **k: {})
    result = mcp_computer._call_tool_inner(TOOL_LIST_APPS, {})
    assert result.startswith(ERROR_PREFIX)


@pytest.mark.asyncio
async def test_gateway_dispatch_runs_on_the_bounded_subprocess_pool(
    monkeypatch: pytest.MonkeyPatch, home: Path
):
    """The gateway's dispatch leg uses ``subprocess_executor``, NOT the default pool.

    A hung target app parks the worker for the driver's whole timeout; on the default
    pool a few of those starve the gateway and the loop's ``getaddrinfo``. Pinned by
    recording the executor, since both pools return the same answer.
    """
    from kiro_crew.dashboard.handlers import computer_use as cu_api
    from kiro_crew.executors import subprocess_executor

    loop = asyncio.get_running_loop()
    seen: list[object] = []

    class _RecordingLoop:
        """Delegates to the real loop, recording each executor it is handed."""

        def run_in_executor(self, executor, fn, *args):
            seen.append(executor)
            return loop.run_in_executor(executor, fn, *args)

    monkeypatch.setattr("kiro_crew.computer_use.tools.dispatch_tool", lambda *a, **k: "ok")
    monkeypatch.setattr(cu_api.asyncio, "get_running_loop", _RecordingLoop)

    request = _InvokeRequest({"tool": TOOL_LIST_APPS, "args": {}, "session_key": _SESSION})
    resp = await cu_api.api_computer_use_invoke(request)

    assert json.loads(resp.body)["text"] == "ok"
    assert seen == [subprocess_executor()], "the dispatch must not land on the default pool"


class _InvokeRequest(dict):
    """The minimum ``api_computer_use_invoke`` touches: ``.json()``, path, mapping.

    Pre-authorized: the secret check runs against the real middleware in
    ``test_computer_use_api.py``.
    """

    path = "/api/computer-use/invoke"

    def __init__(self, body: dict) -> None:
        super().__init__(internal_auth=True)
        self._body = body

    async def json(self) -> dict:
        return self._body


def test_server_identity_is_slash_free_and_stable():
    """The server key must contain no ``/`` — kiro-cli splits ``@server`` refs on it."""
    assert mcp_computer.SERVER_NAME == "kirocrew-computer"
    assert "/" not in mcp_computer.SERVER_NAME
    assert mcp_computer.SERVER_VERSION


def test_call_tool_emits_a_sel_invocation_on_validation_failure(
    keystone: Path, monkeypatch: pytest.MonkeyPatch
):
    """A rejected call is still audited, as a FAILED outcome.

    ``call_tool_with_logging`` classifies a leading ``"Error:"`` as failed, which is
    why the prefix is load-bearing rather than cosmetic.
    """
    _enable(keystone)
    with patch("kiro_crew.mcp_shared.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        result = mcp_computer._call_tool(TOOL_CLICK, {"app": "Finder", "element_index": True})
    assert result.startswith(ERROR_PREFIX)
    assert recorder.log_tool_invocation.called
    assert recorder.log_tool_invocation.call_args.kwargs["outcome"] == "failed"


# ── the gateway dispatcher: the ordered chokepoint ──


def test_dispatcher_refuses_while_the_keystone_says_disabled(
    home: Path, fake_backend: FakeComputerUseBackend
):
    """Step 1 of the chokepoint. The driver is never reached.

    An empty call journal is the assertion that matters: a disabled feature must
    not even enumerate the operator's windows.
    """
    result = _dispatch(TOOL_LIST_APPS)
    assert result.startswith(ERROR_PREFIX)
    assert fake_backend.calls == []


def test_action_without_a_prior_get_state_hard_fails(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """**Hard fail, never a lazy re-snapshot**, and the message names the fix.

    A lazy re-walk would let the model act on a tree it was never shown — exactly
    the failure element indices exist to prevent.
    """
    _enable(keystone)
    result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=3)
    assert result.startswith(ERROR_PREFIX)
    assert "no state for" in result
    assert TOOL_GET_STATE in result
    assert "click" not in [name for name, _ in fake_backend.calls]


def test_ttl_expiry_names_the_age(
    keystone: Path, fake_backend: FakeComputerUseBackend, monkeypatch: pytest.MonkeyPatch
):
    """An expired snapshot is refused with its real age.

    The number is what makes the refusal actionable — "call get_state again"
    without it reads like a generic failure the model may retry blindly.
    """
    _enable(keystone)
    from kiro_crew.computer_use.types import SNAPSHOT_TTL_SECS

    clock = {"now": 1000.0}
    monkeypatch.setattr(cu_index.time, "monotonic", lambda: clock["now"])
    _get_state()
    clock["now"] += SNAPSHOT_TTL_SECS + 214
    result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=3)
    assert result.startswith(ERROR_PREFIX)
    assert "old" in result
    assert TOOL_GET_STATE in result


def test_fingerprint_drift_names_both_identities(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """Drift refuses and names BOTH the old and the new element.

    Without both, the model cannot tell whether the UI merely re-laid out or it was
    about to click something genuinely different — so it would just retry.
    """
    _enable(keystone)
    state = _get_state()
    assert "Save" in state
    # Retitle the element under the cached index: the pre-action re-walk must see
    # a different fingerprint at that index and refuse.
    save_index = next(
        rec.index
        for rec in cu_index.get_shared_index()
        .get(FAKE_FILES_APP.window_key, session_key=_SESSION)
        .elements
        if rec.title == "Save"
    )
    fake_backend.restage_title(FAKE_FILES_APP.key, save_index, "Delete")
    result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=save_index)
    assert result.startswith(ERROR_PREFIX)
    assert "changed since" in result
    assert "Save" in result and "Delete" in result


def test_drift_drops_the_cache_so_a_retry_still_refuses(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """After drift the model MUST re-read; a second attempt cannot slip through.

    Leaving the stale snapshot installed would let a retry resolve the same stale
    index again, and caching the FRESH walk would hand the model indices from a
    tree it has never seen.
    """
    _enable(keystone)
    _get_state()
    index = cu_index.get_shared_index()
    save_index = next(
        rec.index
        for rec in index.get(FAKE_FILES_APP.window_key, session_key=_SESSION).elements
        if rec.title == "Save"
    )
    fake_backend.restage_title(FAKE_FILES_APP.key, save_index, "Delete")
    assert _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=save_index).startswith(
        ERROR_PREFIX
    )
    second = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=save_index)
    assert second.startswith(ERROR_PREFIX)
    assert "no state for" in second


def test_unknown_element_index_is_refused(keystone: Path, fake_backend: FakeComputerUseBackend):
    """An index outside the shown tree is refused, never clamped."""
    _enable(keystone)
    _get_state()
    result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=4999)
    assert result.startswith(ERROR_PREFIX)
    assert "4999" in result


def test_end_turn_invalidates_every_cached_snapshot(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """The explicit early release: after it, every index is refused."""
    _enable(keystone)
    _get_state()
    assert len(cu_index.get_shared_index()) == 1
    text = _dispatch(TOOL_END_TURN)
    assert not text.startswith(ERROR_PREFIX)
    assert len(cu_index.get_shared_index()) == 0
    assert _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=3).startswith(ERROR_PREFIX)


def test_secure_element_refuses_set_value_and_type_text(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """A secure target refuses input.

    The fixture's role is the INNOCUOUS ``AXTextField``; only its subrole reveals
    it, which is exactly the shape a role-only check misses — and it has a
    READABLE value, so writing into it (then reading back) is a credential path.
    """
    _enable(keystone)
    _get_state(FAKE_LOGIN_APP.name)
    secure_index = next(
        rec.index
        for rec in cu_index.get_shared_index()
        .get(FAKE_LOGIN_APP.window_key, session_key=_SESSION)
        .elements
        if rec.secure
    )
    for tool, args in (
        (TOOL_SET_VALUE, {"value": "hunter2"}),
        (TOOL_TYPE_TEXT, {"text": "hunter2"}),
    ):
        result = _dispatch(tool, app=FAKE_LOGIN_APP.name, element_index=secure_index, **args)
        assert result.startswith(ERROR_PREFIX)
        assert "secure" in result


def test_secure_value_never_appears_in_any_result(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """The secure field's value bytes are NEVER rendered — placeholder only."""
    _enable(keystone)
    state = _get_state(FAKE_LOGIN_APP.name)
    assert FAKE_SECRET_VALUE not in state
    assert SECURE_PLACEHOLDER in state


def test_secure_window_gets_no_screenshot(keystone: Path, fake_backend: FakeComputerUseBackend):
    """Whole-window suppression when any node is secure.

    A password field's rendered glyphs are a credential even after the tree
    redacted its value, and there is no reliable way to blank a sub-rectangle of an
    already-encoded JPEG — so a partial redaction that missed would be worse than
    none. The note is explicit so the model does not retry.
    """
    _enable(keystone)
    state = _get_state(FAKE_LOGIN_APP.name)
    assert "Screenshot:" not in state
    assert "suppressed" in state.lower()


def test_sensitive_text_is_refused(keystone: Path, fake_backend: FakeComputerUseBackend):
    """The text scan is a SECOND layer behind refusing terminals wholesale."""
    _enable(keystone)
    _get_state()
    result = _dispatch(TOOL_TYPE_TEXT, app=FAKE_FILES_APP.name, text="cat ~/.aws/credentials")
    assert result.startswith(ERROR_PREFIX)


def test_unknown_key_spec_is_refused_before_any_keystroke(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """A key we cannot map must not be silently dropped or approximated.

    Sending a DIFFERENT keystroke than the caller asked for, into a live
    application, is worse than refusing.
    """
    _enable(keystone)
    result = _dispatch(TOOL_PRESS_KEY, app=FAKE_FILES_APP.name, key="hyper+q")
    assert result.startswith(ERROR_PREFIX)
    assert "press_key" not in [name for name, _ in fake_backend.calls]


def test_unknown_tool_is_refused_by_the_dispatcher(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """An unregistered tool is refused before validation, not passed through."""
    _enable(keystone)
    result = cu_tools.dispatch_tool("computer_teleport", {}, session_key=_SESSION)
    assert result.startswith(ERROR_PREFIX)
    assert "computer_teleport" in result


def test_unresolvable_app_is_refused_with_the_discovery_hint(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    _enable(keystone)
    result = _dispatch(TOOL_GET_STATE, app="Photoshop")
    assert result.startswith(ERROR_PREFIX)
    assert TOOL_LIST_APPS in result


def test_dispatcher_never_raises_on_a_driver_failure(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """A driver refusal becomes an ``Error:`` string, never an exception.

    An exception escaping the dispatcher would surface in the gateway's request
    handler as a 5xx the shim cannot relay as a tool result.
    """
    _enable(keystone)
    fake_backend.force_error = "the accessibility API refused"
    result = _dispatch(TOOL_LIST_APPS)
    assert result.startswith(ERROR_PREFIX)
    assert "refused" in result


def test_no_result_ever_carries_an_image_content_block(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """**Text only, by construction.**

    ``validation.build_tool_response`` is the transport's single exit and emits only
    ``{"type": "text"}``, so a screenshot can only travel as a path.
    """
    from kiro_crew.validation import build_tool_response

    _enable(keystone)
    outputs = [_get_state(), _dispatch(TOOL_LIST_APPS), _dispatch(TOOL_END_TURN)]
    for text in outputs:
        response = build_tool_response(text)
        assert [block["type"] for block in response["content"]] == ["text"]
        assert "isError" not in response
        assert all(set(block) == {"type", "text"} for block in response["content"])


def test_get_state_relays_a_path_not_bytes(keystone: Path, fake_backend: FakeComputerUseBackend):
    """Only the screenshot PATH reaches the model; the bytes never do."""
    import base64

    from kiro_crew.testing.fake_computer_use import FAKE_JPEG_BYTES

    _enable(keystone)
    state = _get_state()
    assert "Screenshot:" in state
    assert base64.b64encode(FAKE_JPEG_BYTES).decode() not in state
    assert "\xff\xd8" not in state


def test_action_result_includes_the_refreshed_tree(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """Every mutator returns the post-action tree, so the model acts on fresh indices."""
    _enable(keystone)
    _get_state()
    save_index = next(
        rec.index
        for rec in cu_index.get_shared_index()
        .get(FAKE_FILES_APP.window_key, session_key=_SESSION)
        .elements
        if rec.title == "Save"
    )
    result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=save_index)
    assert not result.startswith(ERROR_PREFIX)
    assert "Refreshed state:" in result
    assert f"App={FAKE_FILES_APP.label}" in result


def test_mutator_result_carries_no_screenshot(keystone: Path, fake_backend: FakeComputerUseBackend):
    """A mutator's refreshed view is structural only — no pixels.

    A mutator declares no observation channel at the gate (so a fleet denying
    screenshots keeps the ability to click), and capturing a frame the ceiling
    might forbid only to discard it would be both slower and a needless brush with
    the ceiling.
    """
    _enable(keystone)
    _get_state()
    save_index = next(
        rec.index
        for rec in cu_index.get_shared_index()
        .get(FAKE_FILES_APP.window_key, session_key=_SESSION)
        .elements
        if rec.title == "Save"
    )
    result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=save_index)
    assert "Screenshot:" not in result


# ── redaction ──


def test_drift_refusal_is_redacted_like_every_other_result(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """A fingerprint-drift refusal goes through the SAME egress pass as a result.

    The refusal quotes both the cached and the fresh element's titles verbatim, so it
    must pass ``policy.redact_result`` like a rendered tree.
    """
    _enable(keystone)
    _get_state()
    save_index = next(
        rec.index
        for rec in cu_index.get_shared_index()
        .get(FAKE_FILES_APP.window_key, session_key=_SESSION)
        .elements
        if rec.title == "Save"
    )
    # The element DRIFTS into holding a credential-shaped literal. The refusal
    # names the new identity verbatim, so this is the leak path.
    fake_backend.restage_title(
        FAKE_FILES_APP.key, save_index, f"token {FAKE_CREDENTIAL_FIXTURE} pasted"
    )
    result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=save_index)
    assert result.startswith(ERROR_PREFIX)
    assert "changed since" in result, "the refusal must still be the actionable drift message"
    assert FAKE_CREDENTIAL_FIXTURE not in result


def test_driver_failure_refusal_is_redacted_too(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """Every typed refusal is redacted, not only the drift one.

    A ``ComputerUseError`` carries whatever the driver put in ``DriverResult.text``,
    which on macOS quotes the app label and the failed action — the same class of
    desktop-derived text.
    """
    _enable(keystone)
    fake_backend.force_error = f"the accessibility API refused near {FAKE_CREDENTIAL_FIXTURE}"
    result = _dispatch(TOOL_LIST_APPS)
    assert result.startswith(ERROR_PREFIX)
    assert FAKE_CREDENTIAL_FIXTURE not in result


def test_credential_shaped_fixture_comes_back_masked(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """An AKIA-shaped literal in a tree node is REDACTED on the way out.

    This is the primary egress control for tree text, which is arbitrary user content.
    The fixture is the PUBLIC documentation key, so it is safe on disk.
    """
    _enable(keystone)
    state = _get_state()
    assert FAKE_CREDENTIAL_FIXTURE not in state
    # And the surrounding node still renders, so redaction narrowed the value
    # rather than dropping the element.
    assert "deploy notes" in state


def test_exfiltration_shaped_url_comes_back_masked(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """A long-query exfil-shaped URL in a title is redacted too."""
    _enable(keystone)
    state = _get_state()
    assert "evil.example.com/collect?data=aaaaaaaa" not in state


# ── SEL audit ──


def test_sel_records_a_tool_invocation_on_success(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """EVERY permitted call is audited, not only refusals.

    The operator's record of what the agent did to their desktop is the whole
    point of an audit trail for this surface — a permitted ``set_value`` in an
    authenticated app is exactly the event a later investigation needs.
    """
    _enable(keystone)
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        assert not _dispatch(TOOL_LIST_APPS).startswith(ERROR_PREFIX)
    assert recorder.log_tool_invocation.called
    kwargs = recorder.log_tool_invocation.call_args.kwargs
    assert kwargs["tool_name"] == TOOL_LIST_APPS
    assert kwargs["outcome"] == "allowed"
    assert kwargs["session_key"] == _SESSION


def test_sel_records_a_PRE_GATE_refusal(keystone: Path, fake_backend: FakeComputerUseBackend):
    """A refusal that returns before the gate still records itself.

    Validation errors, unknown tools, stale indices and bad keys never reach the gate,
    and a burst of them is exactly what an investigation wants to see.
    """
    _enable(keystone)
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        # No snapshot yet, so the cached-element lookup refuses before the gate.
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=3)
    assert result.startswith(ERROR_PREFIX)
    assert recorder.log_tool_invocation.called
    kwargs = recorder.log_tool_invocation.call_args.kwargs
    assert kwargs["outcome"] == "refused"
    assert kwargs["tool_name"] == TOOL_CLICK
    assert kwargs["session_key"] == _SESSION


def test_a_refusal_audit_carries_NO_desktop_detail(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """The refusal TEXT can quote a window title; the audit line must not.

    This event fires before the observation ceiling has been applied to that text, so
    it records the fact and the tool name only. "Redacted credentials" is a weaker
    guarantee than "never included".
    """
    _enable(keystone)
    _get_state()
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=9999)
    kwargs = recorder.log_tool_invocation.call_args.kwargs
    assert kwargs["outcome"] == "refused"
    assert kwargs["resources"] == ""


def test_a_validation_refusal_is_audited_too(keystone: Path, fake_backend: FakeComputerUseBackend):
    """A bad argument never reaches the gate, so it is audited as refused."""
    _enable(keystone)
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, x="bad", y=1)
    assert result.startswith(ERROR_PREFIX)
    assert recorder.log_tool_invocation.call_args.kwargs["outcome"] == "refused"


def test_an_audit_failure_never_turns_a_refusal_into_a_crash(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    _enable(keystone)
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        recorder.log_tool_invocation.side_effect = RuntimeError("sel is down")
        sel_factory.return_value = recorder
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, element_index=3)
    assert result.startswith(ERROR_PREFIX)


def test_every_refusal_exit_goes_through_an_AUDITED_helper():
    """Structural: no refusal may return ``Error: …`` inline.

    A site that returns ``f"{ERROR_PREFIX}{…}"`` directly produces no SEL record, so
    only the two helpers that audit first may build that string.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cu_tools))
    audited = {"_refusal", "_static_refusal"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name in audited:
            continue
        for inner in ast.walk(node):
            # ``return f"{ERROR_PREFIX}…"`` — an f-string whose first piece is that name.
            if not isinstance(inner, ast.Return) or not isinstance(inner.value, ast.JoinedStr):
                continue
            for part in inner.value.values:
                if (
                    isinstance(part, ast.FormattedValue)
                    and isinstance(part.value, ast.Name)
                    and part.value.id == "ERROR_PREFIX"
                ):
                    offenders.append(node.name)
    assert not offenders, (
        f"{sorted(set(offenders))} build a refusal inline; route it through "
        "_static_refusal (static prose) or _refusal (text quoting the desktop) so it "
        "is audited"
    )


def test_a_static_pre_gate_refusal_is_audited(keystone: Path, fake_backend: FakeComputerUseBackend):
    """A malformed pointer request is refused with static prose and still audited."""
    _enable(keystone)
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        # ``x`` with no ``y`` is not a target, so the request shape is refused.
        result = _dispatch(TOOL_CLICK, app=FAKE_FILES_APP.name, x=10)
    assert result.startswith(ERROR_PREFIX)
    assert recorder.log_tool_invocation.call_args.kwargs["outcome"] == "refused"


def test_a_disabled_feature_refusal_is_audited(home: Path):
    """Even the earliest exit records the attempt."""
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        assert _dispatch(TOOL_LIST_APPS).startswith(ERROR_PREFIX)
    assert recorder.log_tool_invocation.call_args.kwargs["outcome"] == "refused"


def test_an_unknown_tool_refusal_is_audited(keystone: Path):
    _enable(keystone)
    with patch("kiro_crew.computer_use.tools.sel") as sel_factory:
        recorder = MagicMock()
        sel_factory.return_value = recorder
        assert _dispatch("computer_teleport").startswith(ERROR_PREFIX)
    assert recorder.log_tool_invocation.called


def test_sel_records_the_resolved_identity_not_the_agents_claim(
    keystone: Path, fake_backend: FakeComputerUseBackend, sel_private_root: Path
):
    """The audited target is the identity the driver RESOLVED.

    Auditing the model's ``app`` string would record what it asked for rather than
    what was actually touched — useless in an investigation, and the same reason
    the gate is fed the resolved identity.
    """
    _enable(keystone)
    # Query by a loose fragment; the stored audit row must carry the resolved label.
    _dispatch(TOOL_GET_STATE, app="fake files")
    log = sel_private_root / "security_events.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["resources"] == FAKE_FILES_APP.label
    assert not [r for r in rows if "fake files" in r["resources"]], rows


def test_denial_text_does_not_disclose_the_ceiling(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """A refusal must not hand the agent the policy back one query at a time."""
    _enable(keystone)
    result = cu_tools.dispatch_tool(TOOL_LIST_APPS, {}, session_key="cron:nightly")
    for leak in ("security_policy.json", "profiles/", "allow", "deny_all"):
        assert leak not in result


# ── tool coverage ──


def test_read_only_and_mutating_sets_partition_the_tools():
    """The classification the PreToolUse gate keys on must be total and disjoint.

    ``computer_end_turn`` is neither: it drops KiroCrew's OWN cache and touches no
    other application, so it is control-plane.
    """
    assert READ_ONLY_TOOLS & MUTATING_TOOLS == frozenset()
    assert READ_ONLY_TOOLS | MUTATING_TOOLS | {TOOL_END_TURN} == set(ALL_TOOLS)
    assert READ_ONLY_TOOLS == {TOOL_LIST_APPS, TOOL_GET_STATE}


@pytest.mark.parametrize("tool", sorted(MUTATING_TOOLS))
def test_every_mutating_tool_is_reachable_and_gated(
    tool: str, keystone: Path, fake_backend: FakeComputerUseBackend
):
    """Every mutator refuses while the feature is off — none has its own path.

    Enumerated so a tenth tool added without routing through the chokepoint fails
    here rather than shipping ungoverned.
    """
    result = cu_tools.dispatch_tool(tool, {}, session_key=_SESSION)
    assert result.startswith(ERROR_PREFIX)
    assert fake_backend.calls == []


@pytest.mark.parametrize("tool", sorted(ALL_TOOLS))
def test_every_tool_is_classified_by_the_governance_action_table(tool: str):
    """A tool absent from the class table classifies ``("mutate",)`` — fail-closed.

    Fail-closed in both directions: it can never satisfy an ``@observe``
    allow-list, AND it is caught by an ``@mutate`` deny. But a READ tool added
    without a row would then needlessly prompt, which is what this enumeration
    catches.
    """
    from kiro_crew.platform.governance import (
        CU_CLASS_CONTROL,
        CU_CLASS_MUTATE,
        CU_CLASS_OBSERVE,
        computer_use_action_classes,
    )

    classes = computer_use_action_classes(tool)
    assert classes, f"{tool} has no action classes"
    if tool in READ_ONLY_TOOLS:
        assert CU_CLASS_OBSERVE in classes
    elif tool in MUTATING_TOOLS:
        assert CU_CLASS_MUTATE in classes
    else:
        assert CU_CLASS_CONTROL in classes


def test_perform_action_is_reachable_for_an_advertised_action(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    """The happy path for the named-action verb, so the refusals above mean something."""
    _enable(keystone)
    _get_state()
    save_index = next(
        rec.index
        for rec in cu_index.get_shared_index()
        .get(FAKE_FILES_APP.window_key, session_key=_SESSION)
        .elements
        if rec.title == "Save"
    )
    result = _dispatch(
        TOOL_PERFORM_ACTION, app=FAKE_FILES_APP.name, element_index=save_index, action="AXShowMenu"
    )
    assert not result.startswith(ERROR_PREFIX)
    assert (
        "perform_action",
        {"app": FAKE_FILES_APP.key, "index": save_index, "action": "AXShowMenu"},
    ) in fake_backend.calls


def test_scroll_is_reachable_with_a_page_count(
    keystone: Path, fake_backend: FakeComputerUseBackend
):
    _enable(keystone)
    _get_state()
    result = _dispatch(
        TOOL_SCROLL, app=FAKE_FILES_APP.name, element_index=0, direction="down", pages=2
    )
    assert not result.startswith(ERROR_PREFIX)
    scrolls = [args for name, args in fake_backend.calls if name == "scroll"]
    assert scrolls and scrolls[0]["direction"] == "down"
    assert scrolls[0]["pages"] == pytest.approx(2.0)
