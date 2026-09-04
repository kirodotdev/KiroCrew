"""Regression: dashboard handlers must not interpolate raw exception text into
the client-visible ``error`` field.

The dashboard renders the ``error`` field of a 4xx/5xx JSON body verbatim into a
localized UI, so raw driver/exception text (which can carry filesystem paths, SQL
fragments, or hostnames) must never reach the client, and the prose is
untranslatable besides. The fix at each site is the subtraction PR #5600 shipped
in ``knowledge.import_bundle``: a generic fixed ``error`` message plus a
machine-readable ``code``, with the exception detail going to the server log.

Follow-up from the First Principles review on PR #5600. See issue #5644.

These are source-level guards rather than end-to-end handler drives: the eight
sites live in six handlers with six different app-setup requirements, and the
property under test -- "no raw exception object is formatted into the ``error``
field" -- is a property of the source at each site, so asserting it on the source
is both precise and stable across refactors of the surrounding harness. Each
assertion fails on the pre-fix code (which read ``f"...{exc}"``) and passes after.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from kiro_crew.dashboard.handlers import (
    diagnostics,
    mcp,
    memory,
    tailnet_mobile,
    themes,
    usage,
    worktree,
)


def _error_field_values(func) -> list[ast.expr]:
    """Return the AST node for every ``"error": <value>`` pair in ``func``.

    Walks the function body for dict literals and collects the value node keyed
    by the string literal ``"error"`` -- that is the field the dashboard renders
    verbatim.
    """
    tree = ast.parse(inspect.getsource(func).lstrip())
    values: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, val in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "error":
                    values.append(val)
    return values


def _formats_an_exception(value: ast.expr) -> bool:
    """True if an ``"error"`` value is an f-string interpolating the exception.

    A plain string constant is safe, and so is an f-string that interpolates a
    validated request value or a fixed limit constant (``{name!r}``,
    ``{_MAX_STUB_BATCH}``) -- those carry no internal detail. What must never
    reach the client is the caught exception itself, conventionally bound as
    ``exc``, ``e`` or ``err``. This mirrors the reviewer's own grep on PR #5600
    (``error.*\\{(exc|e|err)\\}``) but is stricter: it also unwraps a ``BoolOp``
    or conditional so ``{exc.strerror or exc}`` is caught, a spelling that grep
    (and a naive matcher) misses. Flag a ``FormattedValue`` whose interpolated
    expression is one of those names, an attribute/call rooted at one
    (``{exc.strerror}``), or such a name anywhere inside a boolean/conditional.
    """
    _EXC_NAMES = {"exc", "e", "err"}

    def _roots_at_exception(expr: ast.expr) -> bool:
        # Peel attribute/call/subscript wrappers down to the root name.
        while isinstance(expr, (ast.Attribute, ast.Call, ast.Subscript)):
            if isinstance(expr, ast.Attribute):
                expr = expr.value
            elif isinstance(expr, ast.Call):
                expr = expr.func
            else:
                expr = expr.value
        if isinstance(expr, ast.Name):
            return expr.id in _EXC_NAMES
        # `exc.strerror or exc`, `exc or ""`, `exc if cond else ""` -- any operand
        # that is exception-rooted makes the whole f-string a leak.
        if isinstance(expr, ast.BoolOp):
            return any(_roots_at_exception(v) for v in expr.values)
        if isinstance(expr, ast.IfExp):
            return _roots_at_exception(expr.body) or _roots_at_exception(expr.orelse)
        return False

    if not isinstance(value, ast.JoinedStr):
        return False
    for part in value.values:
        if isinstance(part, ast.FormattedValue) and _roots_at_exception(part.value):
            return True
    return False


# (handler function, at least one machine code that must appear in its source)
_FIXED_SITES = [
    (diagnostics.api_diagnostics_collect, "collection_failed"),
    (mcp.api_mcp_gateway_enable, "mcp_apply_failed"),
    (mcp.api_mcp_gateway_set_stub, "config_lock_failed"),
    (memory.api_memory_embedding_model, "vector_store_unavailable"),
    (tailnet_mobile.api_tailnet_mobile_qr, "encode_failed"),
    (themes.api_theme_detail, "invalid_installed_theme"),
    (usage._parse_sessions, "sessions_dir_unreadable"),
    (worktree._create_worktree_sync, "worktree_mkdir_failed"),
]


@pytest.mark.parametrize(
    "func,code",
    _FIXED_SITES,
    ids=[f.__name__ for f, _ in _FIXED_SITES],
)
def test_error_field_carries_no_interpolated_exception(func, code) -> None:
    """No ``"error"`` value in a fixed handler interpolates the caught exception."""
    leaking = [v for v in _error_field_values(func) if _formats_an_exception(v)]
    assert not leaking, (
        f"{func.__name__} still interpolates the caught exception into the "
        f'client-visible "error" field ({len(leaking)} site(s)); use a generic '
        f'fixed message plus a machine "code" and log the detail server-side'
    )
    # The generic body must still carry a machine-readable code so the client can
    # branch on the failure without parsing prose.
    has_code = code in inspect.getsource(func)
    assert has_code, f'{func.__name__} must expose the machine code "{code}" on its error body'
