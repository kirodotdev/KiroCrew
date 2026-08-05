"""GATE group B (static half) — sandbox & determinism invariants for workflow scripts.

These assert that ``kiro_crew.workflows.validate.validate`` rejects the dangerous
constructs an LLM-authored workflow script must never contain:

* B1 — no ``import``, no ``open``/``eval``/``exec``/``compile``/``__import__``/...
* B2 — no dunder access (``().__class__``, ``__builtins__``, ...) + an adversarial
       escape corpus that must be rejected wholesale
* B3 (static half) — ``time``/``random``/``uuid`` cannot be imported (the runtime
       half — they're absent from the exec namespace — is tested in the runner suite)

Plus the authoring-shape rules (pure-literal META, ``async def workflow(ctx)``).

If a case here goes from RED to GREEN because a check was loosened, that is a
sandbox regression — see ``docs/system-specs/modules/workflow-gates.md`` group B. New escape
ideas append to ``ADVERSARIAL_CORPUS`` (the intervention flywheel).
"""

from __future__ import annotations

import pytest

from kiro_crew.workflows.validate import SAFE_BUILTINS, validate

# A minimal valid script we mutate per-case so failures isolate to the construct.
GOOD = 'META = {"name": "ok", "description": "d"}\n' "async def workflow(ctx):\n" "    return {}\n"


def _bad(snippet_body: str) -> str:
    """Wrap a body snippet into an otherwise-valid script."""
    return (
        'META = {"name": "x", "description": "d"}\n'
        "async def workflow(ctx):\n"
        f"    {snippet_body}\n"
        "    return {}\n"
    )


def test_good_script_validates() -> None:
    res = validate(GOOD)
    assert res.ok, res.errors
    assert res.meta == {"name": "ok", "description": "d"}


# --------------------------------------------------------------------------- #
# B1 — no imports / no dangerous builtins
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "script",
    [
        "import os\n" + GOOD,
        "import time\n" + GOOD,
        "from os import system\n" + GOOD,
        "from random import random\n" + GOOD,
        _bad("import sys"),
    ],
)
def test_b1_imports_rejected(script: str) -> None:
    assert validate(script).ok is False


@pytest.mark.parametrize(
    "name",
    [
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "globals",
        "locals",
        "getattr",
        "setattr",
        "vars",
        "input",
        "__builtins__",
    ],
)
def test_b1_forbidden_builtins_rejected(name: str) -> None:
    res = validate(_bad(f"x = {name}"))
    assert res.ok is False
    assert any(name in e for e in res.errors)


# --------------------------------------------------------------------------- #
# B2 — no dunder access + adversarial escape corpus
# --------------------------------------------------------------------------- #

ADVERSARIAL_CORPUS = [
    "x = ().__class__",
    "x = ().__class__.__bases__",
    "x = ().__class__.__bases__[0].__subclasses__()",
    "x = (1).__class__.__mro__",
    "x = ctx.__class__.__init__.__globals__",
    "x = ''.__class__.__mro__[1].__subclasses__()",
    "x = type(ctx).__dict__",
    # Exposing Exception (so try/except works) must NOT open the classic
    # Exception-subclasses introspection escape: dunder access is still blocked.
    "x = Exception.__subclasses__()",
    "x = Exception.__class__.__bases__",
    "x = ValueError.__init__.__globals__",
]


@pytest.mark.parametrize("body", ADVERSARIAL_CORPUS)
def test_b2_adversarial_escapes_rejected(body: str) -> None:
    """Every known escape attempt must be statically rejected."""
    assert validate(_bad(body)).ok is False


@pytest.mark.parametrize("attr", ["__class__", "__globals__", "__bases__", "__dict__", "__mro__"])
def test_b2_dunder_attribute_rejected(attr: str) -> None:
    res = validate(_bad(f"y = ctx.{attr}"))
    assert res.ok is False
    assert any(attr in e for e in res.errors)


# --------------------------------------------------------------------------- #
# B3 (static half) — determinism modules cannot be imported
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mod", ["time", "random", "uuid", "datetime", "secrets"])
def test_b3_determinism_modules_rejected(mod: str) -> None:
    res = validate(f"import {mod}\n" + GOOD)
    assert res.ok is False
    assert any(mod in e for e in res.errors)


def test_b3_safe_builtins_exclude_nondeterminism_and_io() -> None:
    """The allow-list the runner injects must not leak time/random/io/introspection."""
    for forbidden in ("open", "eval", "exec", "__import__", "input", "getattr", "type"):
        assert forbidden not in SAFE_BUILTINS


# --------------------------------------------------------------------------- #
# Exception types are usable (try/except / raise) + undefined-name lint
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("exc", ["Exception", "ValueError", "KeyError", "RuntimeError", "TypeError"])
def test_common_exceptions_are_available(exc: str) -> None:
    """A workflow may catch/raise common exceptions — they must be in the sandbox
    (regression: agent scripts using ``except Exception`` died with NameError)."""
    assert exc in SAFE_BUILTINS
    script = (
        'META = {"name": "x"}\n'
        "async def workflow(ctx):\n"
        "    try:\n"
        "        r = await ctx.agent('go')\n"
        f"    except {exc} as e:\n"
        "        ctx.log(str(e)); r = None\n"
        "    return {'r': r}\n"
    )
    assert validate(script).ok, validate(script).errors


def test_try_except_exception_validates_end_to_end() -> None:
    script = (
        'META = {"name": "x"}\n'
        "async def workflow(ctx):\n"
        "    try:\n"
        "        raise ValueError('boom')\n"
        "    except Exception as e:\n"
        "        return {'caught': str(e)}\n"
    )
    assert validate(script).ok


@pytest.mark.parametrize("name", ["json", "math", "os", "lenght", "datetime", "requests"])
def test_undefined_names_rejected(name: str) -> None:
    """Names not in the sandbox would NameError at runtime — caught statically so
    the authoring loop regenerates instead of launching a doomed run."""
    script = (
        'META = {"name": "x"}\n'
        "async def workflow(ctx):\n"
        f"    return {name}.whatever([1, 2])\n"
    )
    res = validate(script)
    assert res.ok is False
    assert any(name in e and "undefined name" in e for e in res.errors)


def test_module_level_helper_is_allowed() -> None:
    """A module-level helper def/var referenced from workflow() is NOT flagged."""
    script = (
        'META = {"name": "x"}\n'
        "PHASES = ['a', 'b']\n"
        "def pick(items):\n"
        "    return items[0]\n"
        "async def workflow(ctx):\n"
        "    ctx.phase(pick(PHASES))\n"
        "    return {}\n"
    )
    assert validate(script).ok, validate(script).errors


# --------------------------------------------------------------------------- #
# Authoring shape
# --------------------------------------------------------------------------- #


def test_meta_must_be_pure_literal() -> None:
    # A call inside META is not a pure literal.
    script = 'META = {"name": str(1)}\nasync def workflow(ctx):\n    return {}\n'
    assert validate(script).ok is False


def test_meta_required() -> None:
    assert validate("async def workflow(ctx):\n    return {}\n").ok is False


def test_entrypoint_must_be_async() -> None:
    script = 'META = {"name": "x"}\ndef workflow(ctx):\n    return {}\n'
    res = validate(script)
    assert res.ok is False
    assert any("async" in e for e in res.errors)


def test_entrypoint_must_take_ctx() -> None:
    script = 'META = {"name": "x"}\nasync def workflow():\n    return {}\n'
    assert validate(script).ok is False


def test_entrypoint_required() -> None:
    assert validate('META = {"name": "x"}\n').ok is False


def test_oversize_script_rejected() -> None:
    big = GOOD + ("# pad\n" * 100000)
    assert validate(big, max_bytes=1024).ok is False


def test_syntax_error_is_reported_not_raised() -> None:
    res = validate("async def workflow(ctx)\n  return {}\n")  # missing colon
    assert res.ok is False
    assert any("syntax" in e.lower() for e in res.errors)


# --------------------------------------------------------------------------- #
# GATE group B (DSL-contract half) — two authoring-bug classes that shipped
# runtime crashes in generated scripts (see the core-group bug thread):
#   * awaiting a SYNCHRONOUS ctx method (ctx.phase/log/nudge) → "can't await NoneType"
#   * dereferencing an awaited nullable ctx result inline → "'NoneType' has no
#     attribute 'get'" / "not subscriptable" when the agent returns None
# validate() must reject both statically so authoring regenerates before launch.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["phase", "log", "nudge"])
def test_awaiting_sync_ctx_method_rejected(method: str) -> None:
    res = validate(_bad(f'await ctx.{method}("x")'))
    assert res.ok is False
    assert any(f"ctx.{method}" in e and "synchronous" in e for e in res.errors), res.errors


def test_sync_ctx_method_without_await_ok() -> None:
    # The correct form (no await) must still validate.
    assert validate(_bad('ctx.phase("read")')).ok, "sync ctx.phase() must be allowed"
    assert validate(_bad('ctx.log("hi")')).ok


@pytest.mark.parametrize(
    "expr",
    [
        '(await ctx.agent("cr")).get("cr_url", "X")',      # attribute .get on await agent
        '(await ctx.parallel([ctx.agent("a")]))[0]',        # subscript on await parallel
        '(await ctx.pipeline([1], f)).get("k")',            # attribute on await pipeline
        '(await ctx.agent("x")).result',                    # plain attribute on await agent
    ],
)
def test_unguarded_none_deref_of_awaited_result_rejected(expr: str) -> None:
    res = validate(_bad(f"y = {expr}"))
    assert res.ok is False
    assert any("None-guard" in e for e in res.errors), res.errors


def test_guarded_agent_result_ok() -> None:
    # Binding first + guarding is the correct pattern and must NOT be flagged.
    script = (
        'META = {"name": "x", "description": "d"}\n'
        "async def workflow(ctx):\n"
        '    cr = await ctx.agent("cut cr")\n'
        '    url = cr.get("cr_url", "UNKNOWN") if isinstance(cr, dict) else "UNKNOWN"\n'
        "    reads = await ctx.parallel([ctx.agent('a')])\n"
        "    first = reads[0] if reads and reads[0] else {}\n"
        '    return {"url": url, "first": first}\n'
    )
    res = validate(script)
    assert res.ok, res.errors


def test_bound_then_subscript_not_flagged() -> None:
    # Subscripting a *variable* (not an inline await) is legal — only the inline
    # unguarded await-deref is the bug we catch.
    script = (
        'META = {"name": "x", "description": "d"}\n'
        "async def workflow(ctx):\n"
        "    reads = await ctx.parallel([ctx.agent('a')])\n"
        '    x = reads[0]\n'
        '    return {"x": x}\n'
    )
    assert validate(script).ok, validate(script).errors


def test_bare_await_agent_ok() -> None:
    # Awaiting an async ctx method without dereferencing is fine.
    assert validate(_bad('out = await ctx.agent("do")')).ok
