"""The crew container's own configuration must stay in step with the gateway.

The container serves only the customer's HTTP turn, so it disables every channel and
strips every channel credential, and it writes its own sandbox posture into the config.
Those name lists are compared against ``builtin_channel_descriptors()`` and
``AgentConfig``'s ``sandbox*`` fields, so a new channel or knob fails here until the
container decides what to do with it.

``FORCED_AGENT_SETTINGS`` is the baseline, held universally protective.
``INTERNAL_ONLY_AGENT_SETTINGS`` is the Fargate lane's exception, with its own additive
rules, so the baseline rule never has to bend.

The container source is read via AST, never imported:
``test_spawn_audit.py::test_container_image_assets_are_not_imported`` pins that.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = (
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "container"
    / "supervisor"
    / "backend.py"
)
#: The supervisor entry point, which is where ``verify_sandbox`` lives. Read for the
#: same reason as its sibling: this tree is a docker build context the gateway must
#: never import.
MAIN_SRC = BACKEND_SRC.with_name("__main__.py")


def _literal(name: str) -> Any:
    """The value assigned to a module-level constant, read from the source.

    ``ast.literal_eval`` on the assigned node, so only a literal is accepted -- a
    constant computed at import time would raise here rather than be silently read as
    empty, which is the failure mode that would make this whole file vacuous.
    """
    tree = ast.parse(BACKEND_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        value = getattr(node, "value", None)
        assert value is not None, f"{name} has no assigned value"
        if isinstance(value, ast.Call):  # frozenset({...}) and friends
            assert len(value.args) == 1, f"{name} is a call this reader cannot evaluate"
            return ast.literal_eval(value.args[0])
        return ast.literal_eval(value)
    raise AssertionError(f"{name} is not assigned at module level in {BACKEND_SRC}")


@pytest.fixture(scope="module")
def registry():
    """The gateway's own channel roster, with each channel's credential variables."""
    from kiro_crew.channels import builtin_channel_descriptors
    from kiro_crew.messaging.registry import governed_members

    descriptors = tuple(builtin_channel_descriptors())
    assert descriptors, "the channel registry is empty; every assertion here would be vacuous"
    return {
        "members": set(governed_members(descriptors)),
        "credentials": {name for d in descriptors for name in d.credentials},
    }


@pytest.fixture(scope="module")
def sandbox_keys() -> set[str]:
    """Every `agent` setting the gateway has whose name begins with ``sandbox``.

    By prefix, not a fixed list, so a new knob on `AgentConfig` fails these tests until
    the container decides what to write for it.
    """
    import dataclasses

    from kiro_crew.config.sections import AgentConfig

    keys = {f.name for f in dataclasses.fields(AgentConfig) if f.name.startswith("sandbox")}
    assert keys, "no sandbox settings found on AgentConfig; this test would be vacuous"
    return keys


def test_the_container_disables_every_channel_the_gateway_can_start(registry) -> None:
    sections = set(_literal("CHANNEL_SECTIONS"))
    missing = sorted(registry["members"] - sections)
    assert not missing, (
        "the crew container does not disable these transports, so a crew in a container "
        f"can come up on them: {missing}. Add them to CHANNEL_SECTIONS in {BACKEND_SRC}."
    )


def test_the_container_names_no_channel_the_gateway_does_not_have(registry) -> None:
    """The other direction, because a stale name is a false claim of coverage.

    A section the gateway does not have is written into the config and ignored, which
    makes the list read as broader than its reach.
    """
    sections = set(_literal("CHANNEL_SECTIONS"))
    unknown = sorted(sections - registry["members"])
    assert not unknown, f"these are not channels the gateway can start: {unknown}"


def test_the_container_strips_every_channel_credential_the_gateway_reads(registry) -> None:
    stripped = set(_literal("CHANNEL_CRED_ENV"))
    missing = sorted(registry["credentials"] - stripped)
    assert not missing, (
        "these channel credentials would reach the container's backend, and through it "
        f"the auto-approving model worker: {missing}. Add them to CHANNEL_CRED_ENV in "
        f"{BACKEND_SRC}."
    )


def test_the_container_strips_no_variable_the_gateway_never_reads(registry) -> None:
    """A name nothing reads strips nothing while making the list look complete."""
    stripped = set(_literal("CHANNEL_CRED_ENV"))
    unknown = sorted(stripped - registry["credentials"])
    assert not unknown, (
        f"these variables are in no channel's credential set, so removing them protects "
        f"nothing: {unknown}"
    )


def test_the_container_forces_every_sandbox_setting_the_gateway_reads(sandbox_keys) -> None:
    """Each one changes how sandboxed the model subprocess is, so the container states it.

    `config.json` arrives from outside the container, so any setting left to it lets a
    supplied file weaken a protection or refuse a fallback the deployment needs.
    """
    forced = set(_literal("FORCED_AGENT_SETTINGS"))
    missing = sorted(sandbox_keys - forced)
    assert not missing, (
        "the crew container does not write these sandbox settings, so a config file "
        f"supplied to the task decides them: {missing}. Add them to "
        f"FORCED_AGENT_SETTINGS in {BACKEND_SRC}."
    )


def test_no_sandbox_setting_is_forced_to_a_permissive_value(sandbox_keys) -> None:
    """Writing the key is half of it; the value has to be the protective one.

    Derived, not a per-key table: a table here could be edited alongside a relaxed value
    and leave CI green. Every boolean ``sandbox*`` setting must be ``False``; the mode must
    not be ``JAIL_MODE_OFF`` and must be one the schema declares; a list-valued setting
    only narrows what the agent may write, so it must equal ``AgentConfig``'s default.
    """
    from kiro_crew.config.sections import JAIL_MODE_OFF, AgentConfig

    declared = AgentConfig()
    forced = dict(_literal("FORCED_AGENT_SETTINGS"))
    checked = 0
    for key in sorted(sandbox_keys):
        assert key in forced, f"{key} is not written by the container at all"
        value = forced[key]
        if isinstance(getattr(declared, key), bool):
            assert value is False, (
                f"{key} is forced to {value!r}. Every boolean sandbox setting is a way to "
                "run with less isolation than the host offers, and this container writes "
                "the protective value for all of them."
            )
        elif isinstance(getattr(declared, key), list):
            # A list-valued sandbox setting only ever NARROWS what the agent may write
            # (``sandbox_readonly_paths`` and its exceptions), so no value of it is less
            # isolated than the default. The container states that default.
            assert value == getattr(declared, key), (
                f"{key} is forced to {value!r}, not the gateway's own default "
                f"{getattr(declared, key)!r}"
            )
        else:
            assert value != JAIL_MODE_OFF, (
                f"{key} is forced to the gateway's disabling value {value!r}, which skips "
                "OS-level isolation on a host that could have provided it."
            )
            assert value in _schema_enum(
                key
            ), f"{key} is forced to {value!r}, which is not a mode the gateway declares"
        checked += 1
    assert checked == len(sandbox_keys), "a sandbox setting was skipped rather than judged"


def _schema_enum(field_name: str) -> tuple:
    """The values ``AgentConfig`` declares for *field_name*, from its field metadata.

    No declared enum yields an empty tuple, so the check fails closed.
    """
    import dataclasses

    from kiro_crew.config.sections import AgentConfig

    for f in dataclasses.fields(AgentConfig):
        if f.name == field_name:
            return tuple(f.metadata.get("enum", ()) or ())
    return ()


def test_the_permissive_value_rule_has_something_to_stand_on(sandbox_keys) -> None:
    """Non-vacuity: the rule above must have a boolean to require and a mode to refuse.

    Without a disabling mode or a boolean sandbox setting, the rule would pass everything.
    """
    import dataclasses

    from kiro_crew.config.sections import JAIL_MODE_OFF, AgentConfig

    declared = AgentConfig()
    booleans = {
        f.name
        for f in dataclasses.fields(AgentConfig)
        if f.name in sandbox_keys and isinstance(getattr(declared, f.name), bool)
    }
    assert booleans, "no boolean sandbox setting: the False requirement would be vacuous"
    assert JAIL_MODE_OFF, "the gateway declares no disabling mode: the refusal cannot anchor"
    assert _schema_enum("sandbox"), "the mode declares no enum: the membership check is vacuous"
    assert JAIL_MODE_OFF in _schema_enum(
        "sandbox"
    ), "the disabling value is not in the mode's own enum, so refusing it proves nothing"


def test_the_guard_asserts_the_backend_environment_carries_no_credential() -> None:
    """The invariant ``build_backend_env`` maintains, checked where it is consumed.

    ``verify_sandbox`` refuses a credential in the spawn environment whatever the sandbox
    verdict. Both credential names are required, or the sibling path stays open.
    """
    guard = ast.parse(MAIN_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(guard):
        if isinstance(node, ast.FunctionDef) and node.name == "verify_sandbox":
            break
    else:
        raise AssertionError(f"{MAIN_SRC} no longer defines verify_sandbox")
    names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    assert {"ENV_KIRO_IDENTITY", "ENV_KIRO_API_KEY"} <= names, (
        "verify_sandbox no longer reads both credential names, so a credential "
        f"reintroduced into the backend's environment would pass unremarked: {names}"
    )
    # And it must read a HANDED-IN environment rather than build its own, or the
    # assertion would be the builder confirming itself.
    args = {a.arg for a in node.args.kwonlyargs} | {a.arg for a in node.args.args}
    assert "env" in args, f"verify_sandbox takes no env to read: {sorted(args)}"


def test_the_container_forces_no_agent_setting_the_gateway_does_not_have() -> None:
    """A key the gateway does not read is written and ignored, which reads as coverage."""
    import dataclasses

    from kiro_crew.config.sections import AgentConfig

    known = {f.name for f in dataclasses.fields(AgentConfig)}
    forced = set(_literal("FORCED_AGENT_SETTINGS"))
    unknown = sorted(forced - known)
    assert not unknown, f"these are not settings on AgentConfig: {unknown}"


# ── The internal-only exception, and why it is stated as a SECOND rule ──
#
# The Fargate lane is internal-only (RFC section 7): the operator runs their own crews and
# bears the risk, so an unsandboxed model subprocess is accepted there. The ratchet above
# is not bent for it; the exception has its own additive rules: only override a key the
# baseline decides, only real sandbox keys, unreachable without the deployment's claim.


def _function_def(source: Path, name: str) -> ast.FunctionDef:
    """The named module-level function, read from *source* rather than imported."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{source} no longer defines {name}")


def test_the_internal_only_loosening_names_only_real_sandbox_settings(sandbox_keys) -> None:
    """The exception may only relax a sandbox knob the gateway actually reads.

    Catches a typo that loosens nothing, and a non-sandbox setting (such as `acp_backend`,
    which decides who owns the model credential) riding the sandbox exception.
    """
    loosened = set(_literal("INTERNAL_ONLY_AGENT_SETTINGS"))
    outside = sorted(loosened - sandbox_keys)
    assert not outside, (
        "the internal-only exception names settings that are not sandbox settings: "
        f"{outside}. The boundary accepts an unsandboxed model subprocess; it does not "
        "license any other posture change, and a typo here loosens nothing while "
        "reading as though it did."
    )


def test_the_internal_only_loosening_can_only_override_a_stated_protective_value(
    sandbox_keys,
) -> None:
    """Every loosened key must ALSO be in the baseline, so the ratchet above covers it.

    A key only here would have no protective value stated for the universal rule to judge.
    """
    baseline = set(_literal("FORCED_AGENT_SETTINGS"))
    loosened = set(_literal("INTERNAL_ONLY_AGENT_SETTINGS"))
    orphans = sorted(loosened - baseline)
    assert not orphans, (
        f"these are loosened without the baseline stating a protective value: {orphans}. "
        "Add them to FORCED_AGENT_SETTINGS first, so the universal rule above judges "
        "their default and this constant is only ever an override of it."
    )
    # It must stay an exception: loosening every sandbox key is no posture at all.
    assert loosened < sandbox_keys, (
        f"the internal-only exception loosens every sandbox setting ({sorted(loosened)}). "
        "That is not an exception to the protective posture, it is the absence of one."
    )


def test_the_internal_only_loosening_is_unreachable_without_the_deployments_claim() -> None:
    """The constant must be applied ONLY under the flag, read from the source.

    Every read inside ``build_backend_config`` must sit in an ``if`` naming
    ``internal_only``; a default of ``False`` alone does not prove the branch is guarded.
    ``container_tests/test_supervisor_backend.py`` checks the resulting values.
    """
    fn = _function_def(BACKEND_SRC, "build_backend_config")
    args = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "internal_only" in args, (
        "build_backend_config takes no internal_only parameter, so the loosened posture "
        f"cannot be conditioned on the deployment's claim: {sorted(args)}"
    )

    def _reads_constant(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Name) and n.id == "INTERNAL_ONLY_AGENT_SETTINGS"
            for n in ast.walk(node)
        )

    guarded = [
        branch
        for branch in ast.walk(fn)
        if isinstance(branch, ast.If)
        and _reads_constant(branch)
        and any(isinstance(n, ast.Name) and n.id == "internal_only" for n in ast.walk(branch.test))
    ]
    assert guarded, (
        "INTERNAL_ONLY_AGENT_SETTINGS is not applied inside an `if` that tests "
        "internal_only, so the loosened sandbox posture is reachable without the "
        "deployment claiming the internal-only boundary. Every container would get it, "
        "including one on a local host."
    )
    # Nowhere else in the function, or an unguarded second site would decide.
    reads_outside = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Name)
        and node.id == "INTERNAL_ONLY_AGENT_SETTINGS"
        and not any(node in ast.walk(branch) for branch in guarded)
    ]
    assert not reads_outside, (
        "INTERNAL_ONLY_AGENT_SETTINGS is read outside the guarded branch as well, so "
        "the guard is not the only path to the loosened posture."
    )


def test_the_sandbox_guard_refuses_or_accepts_on_the_deployments_claim() -> None:
    """``verify_sandbox`` must read the claim, and its refusal must name the setting.

    Unread, the claim would change only a config file and Fargate would still refuse. The
    refusal must name ``SMC_INTERNAL_ONLY``: Fargate has no host with user namespaces, so
    "move to one" is no advice there.
    """
    fn = _function_def(MAIN_SRC, "verify_sandbox")
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "internal_only" in attrs, (
        "verify_sandbox does not read settings.internal_only, so the deployment's claim "
        f"cannot decide whether it refuses: {sorted(attrs)}"
    )
    text = " ".join(
        node.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )
    assert "SMC_INTERNAL_ONLY" in text, (
        "no message in verify_sandbox names SMC_INTERNAL_ONLY, so an operator refused on "
        "a host that cannot sandbox is told to move to one that can -- which in the "
        "Fargate lane does not exist."
    )
