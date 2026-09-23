"""The tool gate reports routing honestly and refuses what it enforces.

Two axes that must not be conflated, and each test names which one it is on:

* **Truth** -- what :func:`routing_verdict` SAYS about a harness. Every harness
  gets an honest verdict, including the ones this core does not enforce.
* **Enforcement** -- whether a non-ROUTED verdict REFUSES. Scoped to the
  mechanisms this core implements end to end: ``SESSION_CONFIG``, which checks the
  advertised option, and ``VERIFIED_SEEDED_SETTINGS``, which reads back the setting
  it wrote.

Collapsing them is the failure this file exists to prevent: upgrading an
unenforced harness to ``ROUTED`` so it stops refusing would make the picker and
the doctor row assert a guarantee nothing performs.

Every test is revert-verified: with the corresponding guard removed they fail.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

from kiro_crew import acp_tool_gate as gate
from kiro_crew import platform_compat
from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_ROUTING,
    ACP_BACKENDS_KNOWN,
    Routing,
    permission_config_for,
    routing_for,
)
from kiro_crew.agent_sdk.tool_gate import PI_GATE_ARTIFACT_LEAF
from kiro_crew.sandbox import (
    _CREW_CHILD_WITHHELD_LEAVES,
    _CREW_READONLY_LEAVES,
    _CREW_SANDBOX_VISIBLE_LEAVES,
    crew_host_runtime_leaves,
)
from kiro_crew.security import crew_home_prefixes, sensitive_home_dirs

AGENT_SPEC_BACKENDS = (ACP_BACKEND_KIRO, ACP_BACKEND_KAS)

#: Production routing table, not a hand-maintained id list: a newly enforced
#: harness must pick up the Windows copy with its own ``label_for`` name.
ENFORCED_BACKENDS = tuple(
    backend for backend, routing in ACP_BACKEND_ROUTING.items() if routing in gate.ENFORCED_ROUTINGS
)

_GENERIC_SANDBOX_REMEDY = "Set agent.sandbox to 'standard' or 'strict'"


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


def test_only_implemented_mechanisms_are_enforced() -> None:
    """The enforced set is a mechanism list, not a harness allowlist.

    Scoping by mechanism is what makes widening it require IMPLEMENTING one; an
    id-based allowlist could be widened by editing a literal. Every member carries an
    observation: SESSION_CONFIG checks the advertised option before the first
    prompt, VERIFIED_SEEDED_SETTINGS reads back the setting it wrote, and
    VERIFIED_GATE_EXTENSION reads the harness's own command registry back for the
    gate it loaded. Plain SEEDED_SETTINGS is absent for exactly that reason -- it
    writes without reading.
    """
    assert gate.ENFORCED_ROUTINGS == frozenset(
        {
            Routing.SESSION_CONFIG,
            Routing.VERIFIED_SEEDED_SETTINGS,
            Routing.VERIFIED_GATE_EXTENSION,
        }
    )
    assert gate.is_enforced(ACP_BACKEND_CODEX) is True
    assert gate.is_enforced(ACP_BACKEND_OPENCODE) is True
    assert gate.is_enforced(ACP_BACKEND_PI) is True
    # deepseek is NOT enforced, and it is the case that shows enforcement following the
    # MECHANISM rather than the harness: it has a permission setting Crew can pin and
    # read back, and that setting governs model-initiated escalations rather than tool
    # calls. Its sandbox decides those itself. So its routing is ``UNVERIFIED``, which
    # is outside the enforced set by construction -- there is no observation to
    # enforce.
    assert gate.is_enforced(ACP_BACKEND_DEEPSEEK) is False
    assert routing_for(ACP_BACKEND_DEEPSEEK) is Routing.UNVERIFIED
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
    # The two declared exclusions, subtracted rather than special-cased: the
    # adapter's own token, and Crew's own runtime artifacts and ceilings, whose
    # sandbox disposition ``sandbox`` owns (``_host_runtime_targets`` below).
    excused = own | set(_host_runtime_leaves())
    for leaf in sensitive_home_dirs():
        if leaf in excused:
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


def test_aws_stays_masked_with_only_config_reexposed() -> None:
    """The cc tier's Bedrock posture, on every platform.

    A codex configured for Bedrock resolves its credentials through
    ``~/.aws/config`` (``credential_process``). Masking the directory made every
    session fail at start with ``failed to load AWS credentials``, surfaced as
    ``Authentication required``. The fix re-exposes exactly that file, the way
    ``_CC_EXPOSE_FILES`` does for Claude Code, and keeps ``~/.aws/credentials``
    and ``~/.aws/sso/cache`` hidden. Revert-verified: dropping the expose leaf
    fails the second assertion; excluding ``.aws`` from the mask fails the first.
    """
    masked = set(gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))
    home = os.path.expanduser("~")
    assert os.path.join(home, ".aws") in masked, (
        "the whole-directory hide is what keeps ~/.aws/credentials and the SSO "
        "cache away from the self-approving child"
    )
    hidden = gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX)
    assert gate.adapter_expose_files(ACP_BACKEND_CODEX, hidden) == (
        os.path.join(home, ".aws", "config"),
    )
    assert (
        ".aws" in sensitive_home_dirs()
    ), "the agent's own file tools must still be fenced from ~/.aws"


def test_every_exposed_leaf_sits_under_a_masked_dir() -> None:
    """A re-exposure that is not inside the mask is a grant, not a narrowing.

    The Seatbelt builder ignores such a file (nothing to carve it out of), so
    the auth path would silently fail on macOS; the Linux launcher would copy a
    file over its own live source. Pin containment at the table.
    """
    masked = set(gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))
    for exposed in gate.adapter_expose_files(
        ACP_BACKEND_CODEX, gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX)
    ):
        assert any(exposed.startswith(m + os.sep) for m in masked), exposed


@pytest.mark.parametrize("backend", (*AGENT_SPEC_BACKENDS, ACP_BACKEND_CLAUDE))
def test_unenforced_harness_gets_no_expose_files(backend) -> None:
    """The first-class path keeps byte-identical sandbox arguments here too."""
    assert gate.adapter_expose_files(backend, ()) == ()


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
    """An enforced harness whose spawn path skips the preflight starts unmasked.

    The preflight (refuse-then-mask) is invoked from inside each adapter's OWN spawn
    path rather than from a gate on a shared one, so the kiro construction path
    gains no conditional and no awaited step in service of an adapter
    (harness-parity H13). The cost of that placement is that a new enforced harness
    needs its own call: forgetting one would spawn it with no mask and no refusal.

    An enforced harness reaches its spawn through ONE of two cores, so this counts
    both. A harness on the shared runtime resolves its own plan in its
    ``HarnessAdapter``, so its call lives there; a per-session harness has an arm in
    ``AcpClient._spawn``. Counting only the client core would read a runtime harness
    as missing its preflight while it has one, and -- worse in the other direction --
    would let a harness moved onto the runtime lose its call silently, because the
    arm it was counted by is deleted in the same change that moves it.
    """
    import ast
    import inspect
    import textwrap

    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.harness import harness_for
    from kiro_crew.acp_backends import ACP_BACKEND_ROUTING, ACP_BACKENDS_ACP_RUNTIME

    enforced = {
        backend
        for backend, routing in ACP_BACKEND_ROUTING.items()
        if routing in gate.ENFORCED_ROUTINGS
    }
    assert enforced, "the gate enforces no mechanism; this ratchet would be vacuous"

    def _preflight_calls(fn: object) -> int:
        """References to the preflight, however it is spelled.

        A bare name in the client core; ``client_mod._sandbox_preflight`` on a
        harness, which reaches it through the module rather than importing it. An
        ``ast.Name``-only count reads the second as zero and reports a harness that
        does hold the mask as one that does not.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        total = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "_sandbox_preflight":
                total += 1
            elif isinstance(node, ast.Attribute) and node.attr == "_sandbox_preflight":
                total += 1
        return total

    on_runtime = {b for b in enforced if b in ACP_BACKENDS_ACP_RUNTIME}
    on_client = enforced - on_runtime
    assert on_runtime, "no enforced harness runs on the shared runtime; check the sets"
    assert on_client, "no enforced harness runs on AcpClient; check the sets"

    # A runtime harness resolves its plan itself, so the call is reachable from its
    # own spawn seam. Followed one level into the module helper the seam delegates
    # to, which is where these harnesses put the refuse-then-mask pair.
    for backend in sorted(on_runtime):
        adapter = harness_for(backend)
        module = sys.modules[type(adapter).__module__]
        calls = _preflight_calls(type(adapter).resolve_spawn)
        for name in ("resolve_spawn_masks",):
            helper = getattr(module, name, None)
            if helper is not None:
                calls += _preflight_calls(helper)
        assert calls == 1, (
            f"enforced harness {backend!r} runs on the shared runtime and reaches "
            f"_sandbox_preflight {calls} time(s); exactly one is the contract -- zero "
            "spawns with no mask and no refusal, two enforces twice"
        )

    # A per-session harness keeps its arm in the client core, one call each.
    client_calls = _preflight_calls(AcpClient._spawn)
    assert client_calls == len(on_client), (
        f"{len(on_client)} enforced harness(es) on AcpClient {sorted(on_client)!r} "
        f"but {client_calls} _sandbox_preflight call site(s) in AcpClient._spawn: "
        "every enforced harness must invoke the preflight inside its own spawn arm"
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
    own_tail = os.path.join(*own.split("/"))
    assert not any(
        entry.endswith(own_tail) for entry in masked
    ), "the adapter's own OAuth token was masked, which would break its auth"
    # Matched on the whole leaf, not its final segment: two harnesses name their
    # token ``auth.json``, so a basename check would report codex's own token
    # unmasked while it was really seeing a SIBLING harness's token -- and would
    # equally pass if the exclusion had let codex read that sibling's file.
    for other, leaves in gate.ADAPTER_OWN_CREDENTIAL_LEAVES.items():
        if other == ACP_BACKEND_CODEX:
            continue
        for leaf in leaves:
            tail = os.path.join(*leaf.split("/"))
            assert any(entry.endswith(tail) for entry in masked), (
                f"{other!r}'s credential store is not denied to the codex child; the "
                "exclusion must spare only the harness's OWN token"
            )


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


def _pin_no_sandbox_backend(monkeypatch: pytest.MonkeyPatch, *, opted_in: bool = False) -> None:
    """Pin the probes ``credential_mask_applies`` reads; never the operator config."""
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "none")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: opted_in)


def _assert_windows_sandbox_refusal(exc: BaseException, backend: str) -> None:
    """The Windows copy names this harness and does not recommend a sandbox toggle."""
    msg = str(exc)
    assert type(exc) is gate.ToolGateUnroutable
    assert gate.label_for(backend) in msg
    assert "native Windows" in msg
    assert "no supported OS sandbox backend" in msg
    assert "Kiro CLI" in msg
    assert "Settings → Agent Backend" in msg
    assert "new session" in msg
    assert _GENERIC_SANDBOX_REMEDY not in msg
    assert "sandbox_allow_unsandboxed_exec" not in msg


@pytest.mark.parametrize("mode", ["auto", "standard", "strict", "off"])
@pytest.mark.parametrize("opted_in", [True, False])
def test_windows_refuses_opencode_for_every_sandbox_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, opted_in: bool
) -> None:
    """Native Windows has no Crew sandbox backend, so OpenCode cannot start.

    Changing agent.sandbox cannot create one, and the unsandboxed-exec opt-in is
    not a recovery path. Revert-verified: the generic refusal recommends
    standard/strict and fails the copy assertions below.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _pin_no_sandbox_backend(monkeypatch, opted_in=opted_in)
    with pytest.raises(gate.ToolGateUnroutable) as excinfo:
        gate.enforce_sandbox_floor(ACP_BACKEND_OPENCODE, mode)
    _assert_windows_sandbox_refusal(excinfo.value, ACP_BACKEND_OPENCODE)
    assert "OpenCode" in str(excinfo.value)


@pytest.mark.parametrize("backend", ENFORCED_BACKENDS)
def test_windows_refusal_uses_the_enforced_harness_label(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """Every enforced harness gets its own display name, not the OpenCode example."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _pin_no_sandbox_backend(monkeypatch)
    with pytest.raises(gate.ToolGateUnroutable) as excinfo:
        gate.enforce_sandbox_floor(backend, "standard")
    _assert_windows_sandbox_refusal(excinfo.value, backend)


@pytest.mark.parametrize("mode", ["off", "standard"])
def test_kiro_cli_is_unaffected_on_windows(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """Platform messaging is reached only after the enforced-harness guard.

    Kiro CLI is not an enforced harness, so a simulated Windows host with
    sandbox off still starts it. Revert-verified: moving the Windows branch
    above ``is_enforced`` refuses the first-class path.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _pin_no_sandbox_backend(monkeypatch)
    gate.enforce_sandbox_floor(ACP_BACKEND_KIRO, mode)


def test_windows_still_passes_when_the_mask_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows copy is refusal text, not a second admission rule.

    A working backend whose governance floor raises ``off`` still starts on a
    simulated Windows host, matching the non-Windows governed-floor path.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: "standard")
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: "namespace")
    gate.enforce_sandbox_floor(ACP_BACKEND_OPENCODE, "off")


@pytest.mark.parametrize("mode,detect", [("off", "namespace"), ("standard", "none")])
def test_non_windows_keeps_the_generic_sandbox_refusal(
    monkeypatch: pytest.MonkeyPatch, mode: str, detect: str
) -> None:
    """A host that CAN have a backend still gets the set-standard-or-strict remedy."""
    from kiro_crew import sandbox

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(sandbox, "_governance_sandbox_floor", lambda: None)
    monkeypatch.setattr(sandbox, "detect_backend", lambda **_: detect)
    monkeypatch.setattr(sandbox, "_inside_kirocrew_sandbox", lambda: False)
    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: False)
    with pytest.raises(gate.ToolGateUnroutable) as excinfo:
        gate.enforce_sandbox_floor(ACP_BACKEND_OPENCODE, mode)
    msg = str(excinfo.value)
    assert "OpenCode" in msg
    assert "native Windows" not in msg
    assert _GENERIC_SANDBOX_REMEDY in msg


def test_sandbox_preflight_retains_the_windows_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spawn-path translation keeps the platform-aware remedy.

    ``acp/client.py::_sandbox_preflight`` wraps ``ToolGateUnroutable`` in
    ``AcpToolGateUnroutable``. A translation that dropped the message would send
    the operator back to the generic sandbox advice that cannot succeed on
    native Windows. Uses the real preflight; does not start a child.
    """
    from kiro_crew.acp import client as acp_client
    from kiro_crew.acp.client import AcpToolGateUnroutable

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _pin_no_sandbox_backend(monkeypatch)
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        acp_client._sandbox_preflight(ACP_BACKEND_OPENCODE, "standard")
    msg = str(excinfo.value)
    assert "OpenCode" in msg
    assert "native Windows" in msg
    assert "Kiro CLI" in msg
    assert "Settings → Agent Backend" in msg
    assert _GENERIC_SANDBOX_REMEDY not in msg
    assert "sandbox_allow_unsandboxed_exec" not in msg


# ── Ratchet: the mask must not hide what the child has to reach ───────────────
#
# The class this pins was found on pi and is not pi's: the mask projects the whole
# READ-GATE floor, and the floor covers a Crew runtime artifact to keep the AGENT'S
# FILE TOOLS out of it, not to keep the CHILD out. Handed to a sandbox as a deny
# list, the same entry hid the directory the pi child had to exec its gate launcher
# out of -- the child saw an empty dir and every pi session was refused with exit
# 126. The audit that followed found the same collision on every ENFORCED harness,
# over every further leaf ``sandbox`` itself declares the child must reach. No count is
# written down here: the set is derived, a literal would be one more thing to drift,
# and ``test_the_declared_child_reachable_set_is_not_empty`` is what keeps it honest.
#
# So this is a per-BACKEND sweep over the whole roster, not a pi regression test: a
# new harness inherits the mask by declaring an enforced routing, and inherits the
# collision with it unless something fails here first.


def _host_runtime_leaves() -> tuple[str, ...]:
    """Crew-home-relative leaves, each spelled with every data-home prefix.

    The projection ``adapter_hidden_credential_dirs`` performs, repeated here from
    the same two sources rather than from its output: reading the answer off the
    thing under test would pass on any answer at all.
    """
    return tuple(
        f"{prefix}/{leaf}" for prefix in crew_home_prefixes() for leaf in crew_host_runtime_leaves()
    )


def _host_runtime_targets() -> tuple[str, ...]:
    """Every ANCHOR the mask projects a crew leaf under, not just ``expanduser("~")``.

    ``sandbox_credential_targets`` emits the logical home AND the resolved one,
    because a host whose home is a symlink (``/home/u`` -> ``/local/home/u``) reaches
    the same file by two spellings and a deny list denies only what it is handed. A
    sweep that checked one spelling would report a leaf excluded while the other
    spelling stayed masked -- and on a developer host the two differ, so the weaker
    sweep passes exactly where the bug would live.
    """
    logical = os.path.expanduser("~")
    anchors = {logical, os.path.realpath(logical)}
    return tuple(
        os.path.join(anchor, *leaf.split("/"))
        for anchor in sorted(anchors)
        for leaf in _host_runtime_leaves()
    )


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN))
def test_no_backends_mask_hides_a_path_its_child_must_reach(backend) -> None:
    """For EVERY harness in the roster, over every artifact ``sandbox`` declares.

    Parametrized on the production roster, so a harness added later is swept with
    no edit here. An unenforced harness gets an empty mask and passes trivially --
    that is the posture, not a gap, and the enforced rows below are what carry the
    assertion.
    """
    masked = set(gate.adapter_hidden_credential_dirs(backend))
    for target in _host_runtime_targets():
        assert target not in masked, (
            f"{backend or 'kiro'}'s credential mask hides {target}, which "
            "sandbox.crew_host_runtime_leaves() declares the child must be able to "
            "read. This is the class that refused every pi session: the child sees "
            "an empty directory and fails where the artifact should be."
        )


def test_the_declared_child_reachable_set_is_not_empty() -> None:
    """Vacuity guard: an empty declaration would make the sweep above pass on anything.

    Two separate ways it could empty out -- the sandbox list going away, or the
    prefix projection returning nothing -- so both ends are asserted.
    """
    assert crew_host_runtime_leaves(), "sandbox declares no child-reachable crew leaves"
    assert crew_home_prefixes(), "no crew data-home prefixes to project the leaves through"
    assert len(_host_runtime_targets()) >= 2 * len(crew_host_runtime_leaves())


@pytest.mark.parametrize("backend", ENFORCED_BACKENDS)
@pytest.mark.parametrize(
    "leaf",
    (
        # The browser launcher an agent browser command must exec. The governance,
        # policy and consent documents are NOT here: each is an input to an
        # authorization decision, so they are withheld and the withheld-leaf test
        # pins them instead.
        "playwright-cli",
    ),
)
def test_named_child_reachable_leaves_stay_out_of_every_enforced_mask(backend, leaf) -> None:
    """The leaves with a named in-sandbox reader, pinned one by one.

    Named as well as derived for the reason the credential leaves above are: the
    derivation would keep passing if a leaf were dropped from
    ``crew_host_runtime_leaves()`` as well, and these are the ones whose loss has a
    concrete consequence a reader can check -- a 403 on every internal call, an
    unresolvable session identity, a composition abort, an unexecutable launcher.
    """
    masked = set(gate.adapter_hidden_credential_dirs(backend))
    home = os.path.expanduser("~")
    for prefix in crew_home_prefixes():
        target = os.path.join(home, *f"{prefix}/{leaf}".split("/"))
        assert target not in masked, f"{backend} hides {target} from its own child"


@pytest.mark.parametrize("backend", ENFORCED_BACKENDS)
@pytest.mark.parametrize(
    "leaf",
    (
        ".aws",
        ".ssh",
        ".gnupg",
        ".netrc",
        ".git-credentials",
        # Crew's own SECRETS, as opposed to its runtime artifacts. The exclusion
        # above must not reach these: they have no in-sandbox reader, and the whole
        # point of the mask is that a self-approving child cannot open them.
        ".kiro/crew/.env",
        ".kiro/crew/token_signing.key",
        ".kiro/crew/.vault",
        ".kirocrew/.env",
        ".kirocrew/token_signing.key",
        ".kirocrew/.vault",
    ),
)
def test_the_exclusion_does_not_widen_into_a_credential(backend, leaf) -> None:
    """The other direction: what the mask still has to deny, after the subtraction.

    Without this the sweep above could be satisfied by excluding everything. Both
    crew data-home spellings are named for the crew secrets, because the projection
    that excused the runtime leaves walks the same two prefixes.
    """
    masked = set(gate.adapter_hidden_credential_dirs(backend))
    target = os.path.join(os.path.expanduser("~"), *leaf.split("/"))
    assert target in masked, f"{backend}'s mask no longer denies {target}"


def test_the_child_reachable_set_survives_a_relocated_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """``KIROCREW_HOME`` moves the artifacts, and the exclusion has to move with them.

    The mask re-anchors every crew leaf under the override (that is what makes it
    deny a relocated secret at all), so an exclusion that only spelled the two
    default prefixes would hide the launcher and the gateway credential again on
    exactly the managed hosts that set the override -- the failure back in a
    configuration nobody runs locally.

    Asserted in both directions on the same mask: the runtime artifacts are absent
    under the relocated root, and a crew SECRET is still present under it. One
    without the other passes if the whole re-anchor stopped happening.
    """
    relocated = tmp_path / "relocated-crew"
    monkeypatch.setenv("KIROCREW_HOME", str(relocated))
    masked = set(gate.adapter_hidden_credential_dirs(ACP_BACKEND_CODEX))

    assert any(
        str(relocated) in entry for entry in masked
    ), "the relocated data home is not re-anchored at all; the rest proves nothing"

    for leaf in crew_host_runtime_leaves():
        target = os.path.join(str(relocated), *leaf.split("/"))
        assert target not in masked, (
            f"a relocated data home re-masked {target}, which sandbox declares the "
            "child must reach -- the exclusion must follow the override the mask does"
        )

    for secret in (".env", "token_signing.key"):
        assert os.path.join(str(relocated), secret) in masked, (
            f"{secret} is readable under a relocated data home; the exclusion has "
            "widened past Crew's runtime artifacts into its secrets"
        )


# ── The other half of the ruling: the leaves the child is DENIED ─────────────
#
# The audit that produced the sweep above also found that the pre-fix mask hid two
# DIFFERENT kinds of thing, and only one of them should come back. A ceiling or a
# consent record holds no secret and its in-sandbox reader needs it. A live
# credential is exactly what this mask exists to keep from a harness that
# self-approves its own passive reads, and the fact that the first-class path is
# looser (``AGENTS.md`` carries ``sel_hmac.key`` as a VISIBLE residual with no OS
# fence) is not a licence to widen that residual to every enforced harness.
#
# So these five stay masked, and the features whose in-sandbox readers need them stay
# broken under an enforced harness until those readers move behind the gateway. They
# are broken TODAY, so withholding them is not a regression -- excluding them would
# have been a new exposure.


def _granted_per_backend(leaf: str, backend: str) -> bool:
    """Whether a WITHHELD leaf is handed to *backend* alone by a narrower grant.

    Read off the production rule rather than restated: ``adapter_hidden_credential_dirs``
    excludes the gate-artifact leaf only for the backend whose child execs the launcher
    there. Restating the pair here would let the test and the mask drift into agreeing
    about different things.
    """
    return leaf == PI_GATE_ARTIFACT_LEAF and backend == ACP_BACKEND_PI


@pytest.mark.parametrize("backend", ENFORCED_BACKENDS)
@pytest.mark.parametrize("leaf", _CREW_CHILD_WITHHELD_LEAVES)
def test_a_withheld_leaf_leaves_the_mask_only_by_a_narrower_grant(backend, leaf) -> None:
    """Each withheld leaf, per enforced harness, under both data-home spellings.

    Most of them carry a live credential or a capability and no child may read them:

    * ``run`` holds ``run/gateway-<port>.secret``, which
      ``config.loader.read_local_secret`` resolves BEFORE the shared
      ``.local_secret`` -- so exposing the directory hands over the same credential
      masking the shared file was meant to withhold;
    * ``trust`` and ``sel_hmac.key`` hold the SEL audit key;
    * ``.local_secret`` is the dashboard internal-API bearer secret;
    * ``crons.json`` carries the session key a job runs under;
    * ``member-memory-bindings`` stores raw session keys rather than digests.

    The gate-artifact leaf is the exception, and asserted in BOTH directions here
    rather than skipped: the one harness whose child execs the launcher must reach it,
    and every OTHER enforced harness must not. A skip would leave the second half
    unpinned, which is the half that keeps the narrower grant narrow.
    """
    masked = set(gate.adapter_hidden_credential_dirs(backend))
    readable = set(crew_host_runtime_leaves())
    floor = set(sensitive_home_dirs())
    home = os.path.expanduser("~")
    granted = _granted_per_backend(leaf, backend)

    # What this change actually controls is the EXCLUSION, so that is asserted for
    # every withheld leaf without exception: a withheld leaf must never be published
    # as child-readable, whatever the floor does.
    assert leaf not in readable, f"{leaf!r} is withheld yet published as child-readable"

    for prefix in crew_home_prefixes():
        spelling = f"{prefix}/{leaf}"
        target = os.path.join(home, *spelling.split("/"))
        if granted:
            assert target not in masked, (
                f"{backend} must be able to reach {target}: its child execs Crew's "
                "launcher out of that leaf, and a masked directory is what refused "
                "every one of its sessions before."
            )
        elif spelling in floor:
            assert target in masked, (
                f"{backend}'s credential mask no longer denies {target}. Either the "
                "leaf carries a live credential the child-reachable exclusion must "
                "never reach, or it belongs to one other harness and this one has no "
                "business reading it."
            )
        else:
            # The mask is a projection of the READ-GATE floor, so it can only deny a
            # leaf the floor names. A leaf the floor covers for WRITE alone is absent
            # from the projection, and asserting a denial here would be asserting
            # something this mask cannot perform for any backend. Withholding it still
            # matters -- the exclusion check above is unconditional -- so the leaf is
            # kept out of the child-readable set rather than quietly reclassified.
            assert target not in masked, (
                f"{target} is absent from the read-gate floor, so the mask cannot "
                "deny it; if it appears here the floor changed and this leaf's "
                "classification needs re-deciding rather than re-asserting"
            )


def test_the_withheld_leaves_are_really_on_the_lists_they_subtract_from() -> None:
    """A typo here would withhold nothing and read exactly like a deliberate choice.

    Every withheld entry must be a leaf one of the two source lists actually declares,
    or the subtraction is a no-op: the leaf was never in the union, the exclusion never
    covered it, and this file's own assertions would still pass because the mask denies
    it for the ordinary reason. Pinned so the withhold list cannot rot into decoration.
    """
    declared = set(_CREW_SANDBOX_VISIBLE_LEAVES) | set(_CREW_READONLY_LEAVES)
    for leaf in _CREW_CHILD_WITHHELD_LEAVES:
        assert leaf in declared, (
            f"{leaf!r} is withheld from the child-reachable set but no sandbox list "
            "declares it, so the withhold subtracts nothing"
        )
    assert not set(_CREW_CHILD_WITHHELD_LEAVES) & set(crew_host_runtime_leaves()), (
        "a leaf is both withheld and published as child-reachable; the two sets must "
        "not overlap or the mask's contents depend on iteration order"
    )
