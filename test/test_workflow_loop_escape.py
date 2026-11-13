"""GATE B — the workflow validator must deny the coroutine-driver / event-loop reach.

A workflow script is ``exec``-ed inside the gateway's own event loop with a
restricted ``__builtins__``. The static validator (``FORBIDDEN_ATTRS``) already
blocks frame/generator introspection, but it omitted the coroutine *drivers*
(``send``/``throw``) and the ``Future`` -> loop accessor (``get_loop``), so a
script could step a ctx coroutine by hand, take the pending Future it suspended
on, and reach the real loop's process/executor methods — all through public,
non-underscore attribute names the denylist never listed.

These tests pin the denied names and prove a normal workflow script is still
accepted (the hardening adds no refusal any legitimate script would hit:
``ctx`` exposes ``send_slack``/``send_message``, never a bare ``send``).
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.validate import FORBIDDEN_ATTRS, validate

# The attribute names this hardening adds, each a step in the demonstrated reach.
_DENIED_LOOP_ATTRS = (
    "send",
    "throw",
    "get_loop",
    "subprocess_shell",
    "subprocess_exec",
    "run_in_executor",
)


@pytest.mark.parametrize("attr", _DENIED_LOOP_ATTRS)
def test_name_is_in_forbidden_attrs(attr: str) -> None:
    assert attr in FORBIDDEN_ATTRS, f"{attr!r} must be denied by the workflow validator"


@pytest.mark.parametrize("attr", _DENIED_LOOP_ATTRS)
def test_reference_is_rejected(attr: str) -> None:
    """A script referencing the attribute by name is refused, with a reason."""
    source = (
        'META = {"name": "probe"}\n'
        "async def workflow(ctx):\n"
        f"    thing = ctx.args\n"
        f"    return thing.{attr}\n"
    )
    result = validate(source)
    assert result.ok is False, f"validate() accepted a script using .{attr}"
    assert any(attr in e for e in result.errors), result.errors


def test_drive_then_get_loop_chain_is_rejected() -> None:
    """The two-step reach (hand-drive a coroutine, then .get_loop) is refused."""
    source = (
        'META = {"name": "probe"}\n'
        "async def workflow(ctx):\n"
        "    coro = ctx.agent('x')\n"
        "    pending = coro.send(None)\n"
        "    return pending.get_loop()\n"
    )
    result = validate(source)
    assert result.ok is False
    # Broken at both points: the driver and the loop accessor.
    joined = " ".join(result.errors)
    assert "send" in joined and "get_loop" in joined, result.errors


def test_get_loop_in_format_string_is_rejected() -> None:
    """The format-string reflection path shares the same predicate, so the loop
    accessor is denied inside a ``str.format`` field too (no new gap)."""
    source = (
        'META = {"name": "probe"}\n'
        "async def workflow(ctx):\n"
        "    return '{0.get_loop}'.format(ctx)\n"
    )
    result = validate(source)
    assert result.ok is False
    assert any("get_loop" in e for e in result.errors), result.errors


def test_legitimate_workflow_still_validates() -> None:
    """No new refusal for normal scripts: a workflow that awaits agents, groups
    phases, logs, and calls the ctx messaging surface is still accepted.

    ``send_slack``/``send_message`` are distinct names and must NOT be caught by
    the bare-``send`` denylist; a local variable named around them is fine.
    """
    source = (
        'META = {"name": "digest", "description": "summarise then notify"}\n'
        "async def workflow(ctx):\n"
        "    with ctx.phase('gather'):\n"
        "        ctx.log('starting')\n"
        "        summary = await ctx.agent('summarise the inbox')\n"
        "    results = await ctx.parallel([])\n"
        "    if summary:\n"
        "        await ctx.send_slack(summary)\n"
        "        await ctx.send_message(summary)\n"
        "    return {'summary': summary, 'results': results}\n"
    )
    result = validate(source)
    assert result.ok is True, result.errors
