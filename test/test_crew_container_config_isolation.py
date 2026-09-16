"""The crew container's own configuration must stay in step with the gateway.

The container that a remote crew runs as serves exactly one caller: the customer's
HTTP turn, through its front process. It has nobody to reach on a messaging channel,
so it disables every transport before the backend starts -- by name in a config file
it owns, and by refusing to pass a channel credential into the launch environment.
It also refuses to run the model subprocess unsandboxed, and the gateway reads its
sandbox mode and fallback flags from that same file, so the container writes those
too rather than inheriting whatever arrives.

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
from typing import Any, NamedTuple

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


#: The value a sandbox knob has to be forced to, keyed by the type it is DECLARED as.
#:
#: Every one of these settings is a way to be less sandboxed, so the value the
#: container writes is the one that grants nothing: ``False`` for a flag that would
#: enable a fallback, ``""`` for a string that would name an alternative. Keyed by
#: type rather than by key name so the next non-boolean knob is covered on the day it
#: is added, instead of standing this assertion off against a field whose safe value
#: was never spellable as ``False``.
#:
#: A type that is NOT in here gets no guess: the test fails and names the field. That
#: keeps the human decision where it belongs for a knob whose safe value is not its
#: type's empty one -- an int where ``0`` means "unlimited", say, which the empty-value
#: rule would wave through as safe while it removed a ceiling.
SAFE_FORCED_VALUE: dict[type, object] = {bool: False, str: ""}


class _Knob(NamedTuple):
    """One ``sandbox*`` setting as `AgentConfig` declares it.

    ``choices`` is the field's ``enum`` metadata, empty for a free-form field. It is
    carried because it is what separates a MODE from an opt-out, and the two have
    opposite safe values -- see
    :func:`test_the_forced_sandbox_values_are_the_sandboxed_ones`.
    """

    declared: type
    choices: tuple[str, ...]


@pytest.fixture(scope="module")
def sandbox_fields() -> dict[str, _Knob]:
    """Every `agent` setting whose name begins with ``sandbox``, as declared.

    The rule is deliberately by PREFIX rather than a list of the ones that exist
    today. A sandbox knob added to `AgentConfig` then reds this test until the
    container decides what to write for it, which is the only way a container that
    refuses to run unsandboxed stays true as the gateway grows more ways to not be.

    The declaration travels with the name because the safe value depends on it. The
    type is resolved with ``get_type_hints`` rather than read off ``field.type``,
    which under `AgentConfig`'s ``from __future__ import annotations`` is the SOURCE
    TEXT ``"bool"`` and would match no entry in :data:`SAFE_FORCED_VALUE`.
    """
    import dataclasses
    import typing

    from kiro_crew.config.sections import AgentConfig

    hints = typing.get_type_hints(AgentConfig)
    fields = {
        f.name: _Knob(hints[f.name], tuple(f.metadata.get("enum", ())))
        for f in dataclasses.fields(AgentConfig)
        if f.name.startswith("sandbox")
    }
    assert fields, "no sandbox settings found on AgentConfig; this test would be vacuous"
    return fields


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


def test_the_container_forces_every_sandbox_setting_the_gateway_reads(sandbox_fields) -> None:
    """Each one is a way to be less sandboxed, and the container refuses to be.

    The supervisor refuses to start where the model subprocess cannot be sandboxed and
    offers no unsandboxed posture. The gateway reads its sandbox mode and its fallback
    flags from `config.json`, which the container writes -- so any of them left to what
    a supplied file says is a way to defeat that refusal without tripping it.
    """
    forced = set(_literal("FORCED_AGENT_SETTINGS"))
    missing = sorted(sandbox_fields.keys() - forced)
    assert not missing, (
        "the crew container does not write these sandbox settings, so a config file "
        f"supplied to the task decides them: {missing}. Add them to "
        f"FORCED_AGENT_SETTINGS in {BACKEND_SRC}."
    )


def test_the_forced_sandbox_values_are_the_sandboxed_ones(sandbox_fields) -> None:
    """Writing the key is half of it; the value has to be the safe one.

    Checked against the values rather than against the schema defaults, because a
    default is what this container is declining to rely on.

    ``sandbox`` is the one knob whose safe value is not empty: it names the MODE, and
    the sandboxed mode is ``auto``, not the absence of a mode. Every other one is a way
    to opt OUT of that mode, so the value that opts out of nothing is its type's empty
    value -- ``False`` for a flag, ``""`` for a name. See :data:`SAFE_FORCED_VALUE` for
    why that is keyed by type rather than by field.

    A second MODE-shaped knob would break that rule, so the shape is checked rather
    than assumed: a field declaring ``enum`` choices has no empty value to fall back
    to, and the empty-value rule would quietly accept a value its own schema rejects.
    Such a field reds here instead, and is named by hand as ``sandbox`` is.
    """
    forced = dict(_literal("FORCED_AGENT_SETTINGS"))
    mode = sandbox_fields["sandbox"]
    assert forced.get("sandbox") == "auto", forced.get("sandbox")
    # Non-vacuity for the line above: if the mode enum is ever respelled, writing the
    # stale "auto" would otherwise keep passing while the container emitted a value
    # `agent.sandbox` no longer accepts.
    assert "auto" in mode.choices, f"sandbox no longer offers 'auto'; it offers {mode.choices}"
    for key in sorted(sandbox_fields.keys() - {"sandbox"}):
        knob = sandbox_fields[key]
        assert knob.declared in SAFE_FORCED_VALUE, (
            f"{key} is declared as {knob.declared!r}, a type with no entry in "
            f"SAFE_FORCED_VALUE. Decide what value of that type leaves the container "
            f"fully sandboxed, force it in {BACKEND_SRC}, and add the type here."
        )
        safe = SAFE_FORCED_VALUE[knob.declared]
        assert not knob.choices or safe in knob.choices, (
            f"{key} is a mode: it offers {list(knob.choices)}, and the empty value "
            f"{safe!r} is not among them. Its safe value is a named member, not an "
            f"absence, so pick the member that leaves the container fully sandboxed, "
            f"force it in {BACKEND_SRC}, and assert it by name here as `sandbox` is."
        )
        actual = forced.get(key)
        # The type is compared as well as the value, and with ``type(...) is`` rather
        # than ``isinstance``: ``0 == False`` and ``False`` is an ``int`` subclass, so
        # an equality-only or isinstance check would accept a value of an entirely
        # different shape as the safe one. An absent key arrives here as ``None``,
        # which fails on the type and is reported with the same message.
        assert (
            type(actual) is knob.declared and actual == safe
        ), f"{key} is forced to {actual!r}, not {safe!r}"


def test_the_container_forces_no_agent_setting_the_gateway_does_not_have() -> None:
    """A key the gateway does not read is written and ignored, which reads as coverage."""
    import dataclasses

    from kiro_crew.config.sections import AgentConfig

    known = {f.name for f in dataclasses.fields(AgentConfig)}
    forced = set(_literal("FORCED_AGENT_SETTINGS"))
    unknown = sorted(forced - known)
    assert not unknown, f"these are not settings on AgentConfig: {unknown}"
