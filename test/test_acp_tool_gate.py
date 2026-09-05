"""The tool gate reports routing honestly and refuses what it enforces.

Two axes that must not be conflated, and each test names which one it is on:

* **Truth** -- what :func:`routing_verdict` SAYS about a harness. Every harness
  gets an honest verdict, including the ones this core does not enforce.
* **Enforcement** -- whether a non-ROUTED verdict REFUSES. Scoped to the
  mechanisms this core implements end to end, which today is ``SESSION_CONFIG``
  only.

Collapsing them is the failure this file exists to prevent: upgrading an
unenforced harness to ``ROUTED`` so it stops refusing would make the picker and
the doctor row assert a guarantee nothing performs.

Every test is revert-verified: with the corresponding guard removed they fail.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from kiro_crew import acp_tool_gate as gate
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    Routing,
    permission_config_for,
    routing_for,
)
from kiro_crew.security import sensitive_home_dirs

AGENT_SPEC_BACKENDS = (ACP_BACKEND_KIRO, ACP_BACKEND_KAS)


# ── Truth: what the verdict says ─────────────────────────────────────────────


@pytest.mark.parametrize("backend", AGENT_SPEC_BACKENDS)
def test_agent_spec_is_routed_by_construction(backend) -> None:
    """kiro and KAS ask because the spawn names an agent; there is nothing to probe."""
    verdict, reason = gate.routing_verdict(backend)
    assert verdict is gate.Verdict.ROUTED
    assert "names an agent" in reason


def test_codex_verdict_names_the_option_it_promises() -> None:
    """The SESSION_CONFIG verdict is a promise, so it must say what was promised.

    A bare ROUTED here would be unfalsifiable: the reason string is what lets a
    reader check the promise against the apply.
    """
    verdict, reason = gate.routing_verdict(ACP_BACKEND_CODEX)
    assert verdict is gate.Verdict.ROUTED
    assert "mode=read-only" in reason


def test_claude_is_indeterminate_not_routed() -> None:
    """The unenforced harness is told truthfully, never upgraded to ROUTED.

    This core does not write claude's permission settings, so no read-back can
    establish the guarantee. Reporting ROUTED to avoid a refusal would put a claim
    in the doctor row that nothing performs.
    """
    verdict, _reason = gate.routing_verdict(ACP_BACKEND_CLAUDE)
    assert verdict is gate.Verdict.INDETERMINATE


def test_unknown_backend_fails_closed() -> None:
    """An id the routing table does not name never inherits a neighbour's mechanism."""
    assert routing_for("no-such-harness") is Routing.UNVERIFIED
    verdict, _reason = gate.routing_verdict("no-such-harness")
    assert verdict is gate.Verdict.INDETERMINATE


# ── Enforcement scope ────────────────────────────────────────────────────────


def test_only_session_config_is_enforced() -> None:
    """The enforced set is a mechanism list, not a harness allowlist.

    Scoping by mechanism is what makes widening it require IMPLEMENTING one; an
    id-based allowlist could be widened by editing a literal.
    """
    assert gate.ENFORCED_ROUTINGS == frozenset({Routing.SESSION_CONFIG})
    assert gate.is_enforced(ACP_BACKEND_CODEX) is True
    assert gate.is_enforced(ACP_BACKEND_CLAUDE) is False
    for backend in AGENT_SPEC_BACKENDS:
        assert gate.is_enforced(backend) is False


def test_unenforced_harness_does_not_refuse() -> None:
    """An INDETERMINATE verdict on an unenforced mechanism starts the session.

    The point of the scoping: this core must not refuse a shipped harness over a
    guarantee it never attempted to establish.
    """
    gate.enforce_runtime_routing(
        ACP_BACKEND_CLAUDE,
        "this core does not seed its settings",
        verdict=gate.Verdict.INDETERMINATE,
    )


# ── Refusal: there is no opt-out ─────────────────────────────────────────────


def test_enforced_harness_always_refuses() -> None:
    """A routing failure on an enforced mechanism raises before the first prompt."""
    with pytest.raises(gate.ToolGateUnroutable) as excinfo:
        gate.enforce_runtime_routing(
            ACP_BACKEND_CODEX,
            "session/new did not advertise config option 'mode'",
        )
    message = str(excinfo.value)
    assert "denied-command rules" in message
    assert "sensitive-path block" in message
    assert "governance ceiling" in message


def test_no_local_opt_out_exists() -> None:
    """A security control with a local off-switch is not a control.

    An earlier revision carried ``agent.acp_backend_allow_ungated_tools``: a local
    config bool that started the session with the compensating control off. That is
    the shape the central governance ceiling exists to forbid, since a managed
    fleet could allow the harness while a user's own config disabled the gate.
    Pinned as an ABSENCE so it cannot be reintroduced as a convenience.
    """
    import inspect

    assert not hasattr(gate, "OPT_OUT_KEY")
    params = inspect.signature(gate.enforce_runtime_routing).parameters
    assert "allow_ungated" not in params
    source = inspect.getsource(gate)
    assert "acp_backend_allow_ungated_tools" not in source.split('"""')[0]


def test_refusal_carries_the_remedy() -> None:
    """The message names the concrete change, not just the problem."""
    with pytest.raises(gate.ToolGateUnroutable) as excinfo:
        gate.enforce_runtime_routing(
            ACP_BACKEND_CODEX,
            "not advertised",
            remedy=gate.remediation_for(ACP_BACKEND_CODEX),
        )
    assert "adapter that advertises" in str(excinfo.value)


def test_indeterminate_refuses_alongside_bypassed() -> None:
    """ "Cannot tell" must not be treated as "probably fine".

    A guarantee that lapses whenever evidence is missing is not a guarantee, so
    both verdicts refuse identically. Only the wording differs.
    """
    for verdict in (gate.Verdict.BYPASSED, gate.Verdict.INDETERMINATE):
        with pytest.raises(gate.ToolGateUnroutable):
            gate.enforce_runtime_routing(ACP_BACKEND_CODEX, "reason", verdict=verdict)


# ── The OS-boundary mask compensating for unrouted reads ─────────────────────


def test_mask_covers_the_whole_read_gate_floor() -> None:
    """The compensation must cover what the control it compensates for covers.

    Codex's passive reads never reach ``HookManager.on_tool_call``, so the floor
    cannot see them and this mask is the only thing standing in. An enumerated
    subset left ``.claude/.credentials.json``, ``.netrc``, ``.git-credentials``,
    ``.pypirc`` and ``.npmrc`` readable by the child while the floor called them
    never-readable. Derived, so the two cannot drift.
    """
    masked = set(gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))
    home = os.path.expanduser("~")
    own = set(gate.ADAPTER_OWN_CREDENTIAL_LEAVES[ACP_BACKEND_CODEX])
    for leaf in sensitive_home_dirs():
        if leaf in own:
            continue
        assert os.path.join(home, *leaf.split("/")) in masked, (
            f"{leaf} is on the read-gate floor but readable by the codex child; "
            "the mask must cover the floor it compensates for"
        )


@pytest.mark.parametrize(
    "leaf",
    (
        ".claude/.credentials.json",
        ".netrc",
        ".git-credentials",
        ".pypirc",
        ".npmrc",
        ".aws",
        ".ssh",
    ),
)
def test_named_credential_leaves_are_masked(leaf) -> None:
    """The specific leaves an enumerated mask missed, pinned by name.

    Named individually as well as by derivation: the derivation test would keep
    passing if a leaf were dropped from the FLOOR too, and these are the ones a
    codex agent driven by untrusted content would go after.
    """
    masked = set(gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))
    home = os.path.expanduser("~")
    assert os.path.join(home, *leaf.split("/")) in masked


def test_the_harness_keeps_its_own_token_readable() -> None:
    """The adapter must read its own credential to authenticate.

    Excluding it is safe because the two controls cover different readers: the
    floor still blocks the AGENT's file tools from this leaf, while the mask only
    governs what the adapter's child process can open.
    """
    masked = set(gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))
    own = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
    assert own not in masked
    assert ".codex/auth.json" in sensitive_home_dirs(), (
        "the agent's own file tools must still be fenced from the token even "
        "though the adapter child may read it"
    )


@pytest.mark.parametrize("backend", (*AGENT_SPEC_BACKENDS, ACP_BACKEND_CLAUDE))
def test_unenforced_harness_gets_no_mask(backend) -> None:
    """The first-class path keeps byte-identical sandbox arguments.

    An adapter-driven change must not alter what the Kiro spawn is handed.
    """
    assert gate.adapter_hidden_credential_dirs(backend) == ()


def test_mask_paths_are_absolute() -> None:
    """``wrap_argv`` abspaths what it is handed, so a bare leaf would deny nothing.

    A relative ``.aws`` would resolve against the CWD and silently mask an
    unrelated path (or nothing), which fails OPEN.
    """
    for path in gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX):
        assert os.path.isabs(path)


# ── session_config_issue: the other half of the promise ───────────────────────


def test_advertised_option_and_value_is_no_issue() -> None:
    option_id, value = permission_config_for(ACP_BACKEND_CODEX)
    advertised = [{"id": option_id, "options": [{"value": value}, {"value": "agent"}]}]
    assert gate.session_config_issue(ACP_BACKEND_CODEX, advertised) == ""


@pytest.mark.parametrize(
    "config_options,expected_fragment",
    [
        (None, "did not advertise configOptions"),
        ([], "did not advertise config option"),
        ([{"id": "unrelated", "options": [{"value": "x"}]}], "did not advertise config option"),
        ([{"id": "mode", "options": "not-a-list"}], "has no values"),
        ([{"id": "mode", "options": [{"value": "agent"}]}], "does not advertise required value"),
    ],
)
def test_missing_or_wrong_advertisement_is_an_issue(config_options, expected_fragment) -> None:
    """Each shape fails closed with its own reason.

    Permission routing is stricter than optional model/effort config: a missing
    option cannot be shrugged off as lazy advertising, because the first prompt
    would then run ungated.
    """
    issue = gate.session_config_issue(ACP_BACKEND_CODEX, config_options)
    assert issue, "a missing or wrong advertisement must not read as satisfied"
    assert expected_fragment in issue


@pytest.mark.parametrize("backend", (*AGENT_SPEC_BACKENDS, ACP_BACKEND_CLAUDE))
def test_non_session_config_harness_has_no_config_issue(backend) -> None:
    """The check is scoped to the mechanism it belongs to."""
    assert gate.session_config_issue(backend, None) == ""


# ── the promise and the apply cannot drift ───────────────────────────────────


def test_every_session_config_harness_names_its_option() -> None:
    """A harness declaring the mechanism without an option would report a false ROUTED.

    ``routing_verdict`` builds its ROUTED reason from the option; a harness that
    declared SESSION_CONFIG and named none would promise something the apply could
    never perform, so the verdict degrades to INDETERMINATE instead. This pins that
    no shipped harness is in that state.
    """
    from kiro_crew.acp_backends import ACP_BACKEND_ROUTING

    for backend, routing in ACP_BACKEND_ROUTING.items():
        if routing is not Routing.SESSION_CONFIG:
            continue
        option_id, value = permission_config_for(backend)
        assert option_id and value, (
            f"{backend!r} declares SESSION_CONFIG routing but names no config "
            "option, so routing_verdict would promise an apply that cannot run"
        )


def test_every_enforced_harness_declares_its_own_credential() -> None:
    """An enforced harness with no named token store would be masked out of its own auth.

    ``adapter_hidden_credential_dirs`` denies the whole floor minus the harness's
    own leaf, so a harness absent from that table gets its token masked and cannot
    authenticate. Fails here rather than as an opaque auth error on first use.
    """
    from kiro_crew.acp_backends import ACP_BACKEND_ROUTING

    for backend, routing in ACP_BACKEND_ROUTING.items():
        if routing not in gate.ENFORCED_ROUTINGS:
            continue
        assert backend in gate.ADAPTER_OWN_CREDENTIAL_LEAVES, (
            f"{backend!r} is enforced, so the mask denies it the whole floor; it "
            "must name its own credential leaf or it cannot authenticate"
        )


def test_every_enforced_harness_reaches_the_spawn_preflight() -> None:
    """An enforced harness whose spawn arm skips the preflight would start unmasked.

    The preflight (refuse-then-mask) is invoked from inside each adapter's OWN arm
    of ``AcpClient._spawn`` rather than from a gate on the shared path, so the kiro
    construction path gains no conditional and no awaited step in service of an
    adapter (harness-parity H13). The cost of that placement is that a new enforced
    harness needs its own call: forgetting one would spawn it with no mask and no
    refusal. This pins one preflight call site per enforced harness, so the
    omission fails here instead of silently shipping an unmasked adapter.
    """
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp_backends import ACP_BACKEND_ROUTING

    enforced = {
        backend
        for backend, routing in ACP_BACKEND_ROUTING.items()
        if routing in gate.ENFORCED_ROUTINGS
    }
    assert enforced, "the gate enforces no mechanism; this ratchet would be vacuous"

    spawn_tree = ast.parse(textwrap.dedent(inspect.getsource(AcpClient._spawn)))
    call_sites = sum(
        1
        for node in ast.walk(spawn_tree)
        if isinstance(node, ast.Name) and node.id == "_sandbox_preflight"
    )
    assert call_sites == len(enforced), (
        f"{len(enforced)} enforced harness(es) {sorted(enforced)!r} but "
        f"{call_sites} _sandbox_preflight call site(s) in AcpClient._spawn: every "
        "enforced harness must invoke the preflight inside its own spawn arm"
    )


def test_mask_reanchors_a_relocated_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A credential moved by an env override is still denied to the child.

    Regression: the mask projected every leaf under ``Path.home()``, so an operator
    who relocated a store with ``CLAUDE_CONFIG_DIR`` (or ``KIROCREW_HOME``) kept the
    LIVE secret readable while the sandbox was handed a home-rooted path that denied
    nothing. The mask now shares the read gate's anchor rules. With the delegation
    reverted to a home-only projection this fails.
    """
    # tmp_path, not a hardcoded POSIX path: the assertion is about the OVERRIDE
    # being honoured, and "/tmp/..." is not a path Windows resolves to itself.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "relocated-claude"))
    # No cache reset needed: ``_resolved_root_key`` re-reads the environment on
    # every call, which is what makes the mask honour a late override at all.
    masked = gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX)

    assert any(
        "relocated-claude" in entry for entry in masked
    ), "the relocated claude credential store is not denied to the enforced child"


def test_mask_still_exposes_the_adapters_own_token() -> None:
    """The harness must keep reading its OWN token or it cannot authenticate.

    The deliberate asymmetry: this mask fences the CHILD, while the read gate still
    fences the same leaf for the agent's own file tools, so the two controls cover
    different readers.
    """
    masked = gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX)
    own = gate.ADAPTER_OWN_CREDENTIAL_LEAVES[ACP_BACKEND_CODEX][0]
    basename = own.split("/")[-1]
    assert not any(
        entry.endswith(basename) for entry in masked
    ), "the adapter's own OAuth token was masked, which would break its auth"


def test_sandbox_off_refuses_an_enforced_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the sandbox off the mask is never applied, so the session must refuse.

    ``wrap_argv`` returns from its ``mode == "off"`` branch before it applies
    ``extra_hidden_dirs``. Because ACP v1 cannot force a prompt for a passive read,
    an enforced adapter started that way has NO compensating control and is strictly
    weaker than the gate-routed harnesses beside it. Revert-verified: without the
    guard this raises nothing.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor(ACP_BACKEND_CODEX, "off")


def test_sandbox_off_is_allowed_for_an_unenforced_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first-class path keeps byte-identical spawn behaviour.

    The guard must not refuse a harness this core does not enforce -- it never
    attempted the guarantee, so refusing would break the Kiro path over a promise
    it does not make.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    gate.enforce_sandbox_floor(ACP_BACKEND_KIRO, "off")


def test_governed_floor_keeps_an_enforced_adapter_startable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ceiling that raises ``off`` must not trigger the refusal.

    The guard is keyed on the EFFECTIVE tier, so a governed host whose
    ``sandbox.min_level`` floor overrides the config value still gets the mask and
    must start. Keying it on the raw config value instead fails this.

    ``detect_backend`` is pinned to a PRESENT backend deliberately. A governed host
    that can actually satisfy its own floor has one, and without the pin this test
    silently depended on the CI host having none -- which made it read as "a
    no-backend host must stay startable", a claim it never meant and which
    ``test_no_backend_refuses_under_a_governance_floor_too`` now contradicts.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "standard")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "namespace")
    gate.enforce_sandbox_floor(ACP_BACKEND_CODEX, "off")


def test_no_backend_refuses_under_a_governance_floor_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No backend refuses even when a governance floor forbids unsandboxed execution.

    An earlier revision answered from the floor here, reasoning that a floor
    mandating isolation makes ``wrap_argv``'s unwrapped return unreachable, so
    nothing could start unmasked. That held only for as long as the floor did: a
    ceiling LOOSENED between this preflight and the spawn drops the very refusal
    that made the answer safe, and the floor is mutable config read here just like
    the opt-in was. With no backend nothing can carry the mask at all, so the
    verdict must not be derived from policy in either direction.

    Revert-verified: returning ``_floor_mandates_sandbox(floor)`` passes this
    through.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "strict")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "none")
    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor(ACP_BACKEND_CODEX, "standard")


def test_env_root_override_is_read_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A trailing space is a legal path character and must not be stripped.

    ``_valid_override_home`` takes ``KIROCREW_HOME`` raw and ``config_dir`` mkdirs
    whatever it names, so stripping here anchored the sensitive-target set on
    ``<root>`` while the process actually ran out of ``"<root> "`` -- leaving the real
    ``.env``, signing keys and governance files outside the floor. Revert-verified:
    restoring ``.strip()`` fails this.
    """
    from kiro_crew import security

    # Compare against the SAME resolution the helper performs on the raw value,
    # rather than a POSIX literal: the contract under test is "the value is not
    # stripped", and asserting an absolute spelling instead tests the platform's
    # path semantics (Windows resolves a bare "/tmp/..." onto the current drive).
    raw = str(tmp_path / "crew-home") + " "
    monkeypatch.setenv("KIROCREW_HOME", raw)
    expected = str(pathlib.Path(raw).expanduser().resolve())
    assert security._resolved_env_root("KIROCREW_HOME") == expected
    monkeypatch.setenv("KIROCREW_HOME", "")
    assert security._resolved_env_root("KIROCREW_HOME") is None


def test_no_backend_with_opt_in_refuses_an_enforced_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``off`` tier is NOT proof the mask will be applied.

    On a host with no sandbox backend (Docker/CI where ``unshare(CLONE_NEWUSER)`` is
    blocked) and ``sandbox_allow_unsandboxed_exec`` opted in, ``wrap_argv`` returns the
    argv unwrapped, so ``extra_hidden_dirs`` never lands. The guard must refuse that
    too. Revert-verified: keying it on ``effective_sandbox_mode(mode) != "off"``
    passes this configuration straight through.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "none")
    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor(ACP_BACKEND_CODEX, "standard")


def test_no_backend_refuses_even_without_the_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sandbox backend refuses whether or not unsandboxed exec is opted in.

    An earlier revision answered "the mask applies" here, on the grounds that
    ``wrap_argv`` would raise ``SandboxUnavailableError`` anyway with better
    host-specific remedy text, so guarding twice only blurred the diagnostic. That
    made the verdict depend on ``_allow_unsandboxed_exec()`` -- MUTABLE config, read
    at preflight and acted on at the spawn. Flipping it on in that window left a
    session that had already passed the guard taking wrap_argv's unwrapped path with
    the credential paths readable, so the verdict must not consult it at all.

    Revert-verified: restoring the opt-in read makes this pass through instead.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "none")
    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor(ACP_BACKEND_CODEX, "standard")


def test_no_backend_verdict_ignores_the_mutable_opt_in() -> None:
    """The no-backend verdict is identical for both opt-in states.

    Pins the absence of the read itself rather than one configuration's outcome: a
    future edit that reintroduces ``_allow_unsandboxed_exec()`` on this branch makes
    the two calls disagree and fails here.
    """
    import unittest.mock as _mock

    from kiro_crew import sandbox

    verdicts = set()
    for opted_in in (True, False):
        with (
            _mock.patch.object(sandbox, "_governance_sandbox_floor", lambda: None),
            _mock.patch.object(sandbox, "detect_backend", lambda **_: "none"),
            _mock.patch.object(sandbox, "_inside_kirocrew_sandbox", lambda: False),
            _mock.patch.object(sandbox, "_allow_unsandboxed_exec", lambda: opted_in),
        ):
            verdicts.add(sandbox.credential_mask_applies("standard"))
    assert verdicts == {False}, (
        "credential_mask_applies must answer False for a host with no sandbox "
        f"backend regardless of the unsandboxed-exec opt-in; got {verdicts!r}"
    )


def test_nested_sandbox_passthrough_refuses_an_enforced_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside an existing Kiro Crew sandbox the mask is never applied either.

    A nested re-wrap is impossible by design (Linux seccomp denies the unshare, macOS
    Seatbelt refuses sandbox_apply), so wrap_argv passes the argv through and
    ``extra_hidden_dirs`` never lands. The outer sandbox is not a substitute: the
    standard tier deliberately leaves ``~/.aws`` / ``~/.ssh`` / ``~/.kube`` readable
    for kiro-cli's sake, which is exactly what this mask exists to close for an
    enforced adapter. Revert-verified: without the nested branch the backend probe
    returns a real backend and the guard passes this configuration through.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: True)
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    with pytest.raises(gate.ToolGateUnroutable):
        gate.enforce_sandbox_floor(ACP_BACKEND_CODEX, "standard")
