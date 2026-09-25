"""The crew container's own configuration must stay in step with the gateway.

The container that a remote crew runs as serves exactly one caller: the customer's
HTTP turn, through its front process. It has nobody to reach on a messaging channel,
so it disables every transport before the backend starts -- by name in a config file
it owns, and by refusing to pass a channel credential into the launch environment.
It also states its agent posture there rather than inheriting one: which ACP backend
owns the model credential, and what to do on a host that cannot sandbox the model
subprocess. The gateway reads all of that from the same file, so the container writes
it.

All of that is lists of names, and a list of names is only as good as its last
update. This file is what keeps them updated: it compares them against the
gateway's own definitions -- ``builtin_channel_descriptors()`` for the channels and
their credentials, ``AgentConfig``'s ``sandbox*`` fields for the sandbox settings. A
channel or a sandbox knob added there reds this test until the container decides what
to do about it, which is the difference between an isolation claim and an isolation
that holds.

The container's source is READ, never imported. ``crew/runtime/`` is a docker build
context whose modules import each other as top-level ``container.*``, and
``test_spawn_audit.py::test_container_image_assets_are_not_imported`` pins that the
gateway never imports the tree. So the constants are pulled out of the file's AST.
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

    The rule is deliberately by PREFIX rather than a list of the three that exist
    today. A sandbox knob added to `AgentConfig` then reds this test until the
    container decides what to write for it, which is the only way a container that
    states its sandbox posture stays truthful as the gateway grows more ways to be
    less sandboxed.
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

    The gateway reads its sandbox mode and its fallback flags from `config.json`,
    which arrives in the task from outside the container's code. Any of them left to
    what a supplied file says makes the posture a property of that file rather than of
    this container -- in either direction: a file could weaken a protection, or refuse
    a fallback the deployment depends on.
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

    Stated UNIVERSALLY, over whatever ``sandbox*`` settings the gateway has, and not as
    a table of per-key expectations in this file. A table would sit beside the values it
    guards: relaxing a setting in ``FORCED_AGENT_SETTINGS`` and editing its row here
    would leave CI green, which is the one thing this test exists to prevent. A ratchet
    may only tighten, so the expectation has to come from somewhere the person relaxing
    the setting is not already editing.

    So each expectation is derived rather than declared:

    * Every boolean ``sandbox*`` setting must be ``False``. Each one is a way to proceed
      with less isolation than the host could provide, so a knob added to ``AgentConfig``
      is covered the moment it exists, with nothing to add here.
    * The non-boolean mode must not be the gateway's own disabling value, read from
      ``JAIL_MODE_OFF`` rather than spelled here, and must be one the schema declares --
      so a typo naming no real mode is caught as well.

    Which settings are booleans is read from ``AgentConfig``'s own defaults, so this does
    not need to know their names either.
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

    Read from the schema so a mode the gateway adds or renames is reflected without an
    edit here. Falls back to refusing rather than to accepting: a field with no declared
    enum yields an empty tuple, and the assertion above then fails, which is the right
    direction for a security ratchet that cannot find its own reference.
    """
    import dataclasses

    from kiro_crew.config.sections import AgentConfig

    for f in dataclasses.fields(AgentConfig):
        if f.name == field_name:
            return tuple(f.metadata.get("enum", ()) or ())
    return ()


def test_the_permissive_value_rule_has_something_to_stand_on(sandbox_keys) -> None:
    """Non-vacuity: the rule above must have a boolean to require and a mode to refuse.

    A ratchet that cannot fail is the defect this PR exists to fix, so the rule's own
    anchors are checked rather than assumed. If the gateway stopped declaring a disabling
    mode, or stopped having boolean sandbox settings, the rule would pass everything and
    nothing else would say so.
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

    Popping the credential is something the container controls, so a value in the
    environment handed to the spawn is a broken invariant rather than a property of the
    host -- and ``verify_sandbox`` refuses on it whatever the sandbox verdict is. Read
    from the guard's own source, so deleting that check reds here.

    Both credential shapes are required: covering one and not its sibling would leave
    the same path open beside the guard.
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
