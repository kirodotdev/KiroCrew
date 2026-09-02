"""The ``agent.acp_backend`` axis has one comparison, and it lives in agent_sdk.

Four places used to spell ``backend == ACP_BACKEND_CLAUDE`` independently:
``AcpProvider.is_claude_backend``, ``providers.acp.provider_label``,
``session._is_claude_backend``, and ``AcpClient._is_claude``. Nothing asserted
they agreed. Three now delegate to
:func:`kiro_crew.agent_sdk.backend_identity.is_claude_backend_name`; the client
keeps its own for a layering reason this module pins so the exception stays
deliberate.

These tests also pin the two properties that make the delegation safe: the
helper takes the backend NAME (not a provider), and reading the backend STRING
keeps the predicates mock-safe in a way reading the ``is_claude_backend``
property is not.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
from kiro_crew.subprocess_utf8 import UTF8_TEXT

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: Sites that must ask the helper rather than compare the constant themselves.
DELEGATING = (
    "providers/acp.py",
    "session.py",
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("claude", True),
        ("kas", False),
        ("", False),
        (None, False),
        ("Claude", False),
        ("claude ", False),
        ("claude_code", False),
    ],
)
def test_truth_table(value: str | None, expected: bool) -> None:
    """Only the exact backend id is the claude harness.

    ``"claude_code"`` is False on purpose: that is the ``agent.provider`` value
    on the OTHER axis, and a caller that passes it here has confused the two.
    """
    assert is_claude_backend_name(value) is expected


def test_a_failed_lookup_reads_as_not_claude() -> None:
    """The empty string must answer False, because empty IS the kiro backend.

    This is the whole reason the helper takes a name and the lookup stays at the
    call site. ``getattr(provider, "backend", "")`` returns ``""`` on a miss, and
    ``ACP_BACKEND_KIRO`` is also ``""`` -- so a missed lookup is indistinguishable
    from a real kiro answer. Comparing to claude is safe under that failure mode;
    a kiro helper would not be, which is why none is offered.
    """
    assert ACP_BACKEND_KIRO == ""
    assert is_claude_backend_name(ACP_BACKEND_KIRO) is False
    assert is_claude_backend_name(getattr(object(), "backend", "")) is False


def test_module_reaches_only_the_leaf_constants() -> None:
    """The helper may import ``acp_backends`` and nothing else from the package.

    ``acp_backends`` is a documented leaf. An import of ``kiro_crew.acp`` or
    ``kiro_crew.providers`` here would put the consolidation point inside the
    very roots the boundary gate forbids, so every consumer that routed through
    it would gain a forbidden edge instead of losing one.
    """
    tree = ast.parse((SRC / "agent_sdk" / "backend_identity.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert [m for m in imported if m.startswith("kiro_crew")] == ["kiro_crew.acp_backends"]


def test_importing_this_module_does_not_load_acp_or_providers() -> None:
    """Neither forbidden root may load when the helper is imported.

    The static gate cannot catch a regression: a new eager edge added inside the
    exempt ``agent_sdk/`` tree keeps the gate green while every SDK consumer
    starts paying the ACP package -- or worse, closes a providers/session cycle.

    Runs in a subprocess because ``sys.modules`` is process-wide and the suite
    has already imported most of the package by now.
    """
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC.parent)!r})\n"
        "from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name\n"
        "assert is_claude_backend_name('claude') is True\n"
        "loaded = {\n"
        "    'acp': 'kiro_crew.acp' in sys.modules,\n"
        "    'providers': 'kiro_crew.providers' in sys.modules,\n"
        "    'acp_backends': 'kiro_crew.acp_backends' in sys.modules,\n"
        "}\n"
        "print(repr(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        cwd=str(SRC.parent.parent),
        **UTF8_TEXT,
    )
    assert result.returncode == 0, f"probe failed: {result.stderr[-2000:]}"
    loaded = ast.literal_eval(result.stdout.strip().splitlines()[-1])

    assert loaded["acp"] is False, "importing the helper loaded kiro_crew.acp"
    assert loaded["providers"] is False, "importing the helper loaded kiro_crew.providers"
    # Asserted, not tolerated: the helper reads its constant from here.
    assert loaded["acp_backends"] is True


def _constant_comparisons(path: Path) -> list[int]:
    """Lines comparing something against the claude backend constant by name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not all(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        for operand in [node.left, *node.comparators]:
            if isinstance(operand, ast.Name) and operand.id == "ACP_BACKEND_CLAUDE":
                hits.append(node.lineno)
                break
            if isinstance(operand, ast.Constant) and operand.value == ACP_BACKEND_CLAUDE:
                hits.append(node.lineno)
                break
    return hits


@pytest.mark.parametrize("rel", DELEGATING)
def test_delegating_files_do_not_compare_the_constant(rel: str) -> None:
    """Ratchet: these files must not re-grow their own comparison."""
    hits = _constant_comparisons(SRC / rel)
    assert hits == [], (
        f"{rel} compares the claude backend constant directly at line(s) {hits}; "
        f"call is_claude_backend_name() so the four predicates cannot drift"
    )


@pytest.mark.parametrize("rel", DELEGATING)
def test_delegating_files_ask_the_helper(rel: str) -> None:
    """Non-vacuity guard: deleting the call would satisfy the ratchet above.

    Counts CALLS from the AST rather than mentions in the text. A substring
    check passes on the surviving ``import`` line alone, which a mutation run
    confirmed -- so it would have stayed green with every call site rewritten to
    read the property instead.
    """
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "is_claude_backend_name"
    ]
    assert calls, f"{rel} imports the helper but never calls it"


def test_acp_client_keeps_its_own_comparison() -> None:
    """``AcpClient._is_claude`` is the documented exception, not an oversight.

    ``agent_sdk`` sits above ``kiro_crew.acp``, so routing the client through the
    helper would invert the layering and drag the SDK package -- and the
    backend-install registry its ``__init__`` builds -- into every client import.

    Pinned so a future sweep that "finishes the job" has to read this reason
    first. If the layering ever changes, delete this test deliberately.
    """
    body = (SRC / "acp" / "client.py").read_text(encoding="utf-8")
    assert "is_claude_backend_name" not in body, (
        "acp/client.py now imports the agent_sdk helper; that inverts the "
        "acp -> agent_sdk layering. If this is intended, remove this test."
    )
    assert _constant_comparisons(SRC / "acp" / "client.py"), (
        "acp/client.py no longer compares the constant itself; if its check "
        "moved, this test is stale"
    )


class _FakeClient:
    """Minimal stand-in for the object an ``AcpProvider`` holds as ``client``."""

    def __init__(self, backend: str) -> None:
        self.backend = backend


def _provider_with_backend(backend: str):
    """An ``AcpProvider`` carrying *backend*, without running its ``__init__``.

    ``__init__`` spawns a real ``AcpClient``; the two predicates under test read
    only ``client.backend``, so bypassing construction keeps this a unit test.
    """
    from kiro_crew.providers.acp import AcpProvider

    provider = object.__new__(AcpProvider)
    provider._client = _FakeClient(backend)  # type: ignore[attr-defined]
    return provider


@pytest.mark.parametrize(
    "backend,expected",
    [(ACP_BACKEND_CLAUDE, True), (ACP_BACKEND_KAS, False), (ACP_BACKEND_KIRO, False)],
)
def test_the_property_and_the_session_predicate_agree(backend: str, expected: bool) -> None:
    """Both surviving predicates must answer the same for the same backend.

    They are kept as separate implementations on purpose -- the session one gates
    on ``isinstance`` and defers its import across the providers/session cycle --
    so nothing but a test can hold them equal.
    """
    from kiro_crew.providers.acp import is_claude_backend
    from kiro_crew.session import _is_claude_backend

    provider = _provider_with_backend(backend)
    assert provider.is_claude_backend is expected
    assert is_claude_backend(provider) is expected
    assert _is_claude_backend(provider) is expected


def test_a_spec_mock_is_not_every_backend_at_once() -> None:
    """The string read must stay mock-safe; this is why it is not a property read.

    A ``MagicMock(spec=...)`` constrains attribute NAMES but not their values, so
    a spec'd provider's ``is_claude_backend`` property reads truthy. The session
    predicate reads ``client.backend`` instead and is therefore unfooled. If it
    is ever "simplified" to read the property, this test fails and explains why
    the simplification is wrong.
    """
    from kiro_crew.providers.acp import AcpProvider
    from kiro_crew.session import _is_claude_backend

    spec_mock = MagicMock(spec=AcpProvider)
    assert bool(spec_mock.is_claude_backend) is True, "premise changed: spec mock is not truthy"
    assert _is_claude_backend(spec_mock) is False


def test_non_acp_objects_are_not_the_claude_backend() -> None:
    """A provider this axis knows nothing about answers False, not an error."""
    from kiro_crew.providers.acp import is_claude_backend
    from kiro_crew.session import _is_claude_backend

    assert is_claude_backend(object()) is False
    assert _is_claude_backend(object()) is False
    assert _is_claude_backend(None) is False
