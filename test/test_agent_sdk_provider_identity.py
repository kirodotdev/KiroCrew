"""GATE — the ``agent.provider`` identity question has exactly one spelling.

``"claude_code"`` means four unrelated things in this codebase, and only one of
them is a provider identity check. This test pins that separation so a future
sweep cannot quietly merge them:

1. **The provider-identity axis** — ``agent.provider``, asked through
   :func:`kiro_crew.agent_sdk.provider_identity.is_claude_code`. The eleven
   branches that used to spell the literal inline now route through it, and
   :func:`test_no_converted_file_compares_the_literal` keeps them there.
2. **The session-map provider label** — ``PROVIDER_LABEL_CLAUDE``. Equal in
   value, different in job. Pinned equal by
   :func:`test_config_value_and_session_map_label_agree` so a divergence fails
   loudly instead of silently coupling two vocabularies.
3. **A model-registry index key** — inside ``model_registry``, the string names
   a namespace, not a provider. Deliberately NOT converted; a model's context
   window is a property of the model, not of the provider serving it.
4. **An onboarding import-source id** — the Claude Code desktop app whose
   settings are imported. Pinned by
   :func:`test_onboarding_import_source_id_is_left_alone` so a consolidation
   sweep has to read the reason before changing it.

The ratchet reads the AST rather than the file text, so a docstring that quotes
the old spelling neither satisfies nor trips it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.agent_sdk.provider_identity import (
    PROVIDER_ACP,
    PROVIDER_CLAUDE_CODE,
    is_claude_code,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: Files whose provider-identity branches were converted. Each must ask the
#: predicate and must not compare the literal.
CONVERTED = (
    "context.py",
    "cli_doctor.py",
    "knowledge/llm_pool.py",
    "dashboard/chat_handlers.py",
    "dashboard/chat_runner.py",
    "dashboard/handlers/agents.py",
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("claude_code", True),
        ("acp", False),
        ("kas", False),
        ("", False),
        (None, False),
        ("Claude_Code", False),
        ("claude", False),
    ],
)
def test_predicate_truth_table(value: str | None, expected: bool) -> None:
    """Only the exact id is Claude Code.

    ``"claude"`` is explicitly False: that is the ``agent.acp_backend`` value
    for the same seam, on a different axis. Accepting it here would let a
    caller on the wrong axis get a plausible-looking answer.
    """
    assert is_claude_code(value) is expected


def test_absent_config_is_not_claude_code() -> None:
    """A missing value means the default provider, not an unknown one."""
    assert is_claude_code(None) is False
    assert is_claude_code("") is False
    assert is_claude_code(PROVIDER_ACP) is False


def test_config_value_and_session_map_label_agree() -> None:
    """The config value and the session-map label must stay equal.

    They are two vocabularies that happen to share a spelling. This asserts the
    equality instead of importing one from the other, so if a future change
    moves one of them the failure names both rather than silently rewriting
    session-map entries.
    """
    from kiro_crew.acp.types import PROVIDER_LABEL_CLAUDE

    assert PROVIDER_CLAUDE_CODE == PROVIDER_LABEL_CLAUDE


def test_module_is_dependency_free() -> None:
    """No ``kiro_crew`` imports, so this module cannot be the one that closes a cycle.

    Consumers include ``context.py`` and ``chat_runner.py``. If this module ever
    imports back into the package, one of those import graphs closes a loop.

    This pins the module's OWN imports only. Importing it still executes the
    ``agent_sdk`` package ``__init__``, which loads ``backend_install`` and
    ``drivers.acp``; that chain is safe because it is import-light, not because
    naming the submodule avoids it.
    """
    tree = ast.parse((SRC / "agent_sdk" / "provider_identity.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert [m for m in imported if m.startswith("kiro_crew")] == []


def test_importing_this_module_does_not_load_the_acp_package() -> None:
    """Importing the predicate must not pull ``kiro_crew.acp`` into the process.

    Importing this submodule DOES execute the ``agent_sdk`` package ``__init__``,
    so ``backend_install`` and ``drivers.acp`` load -- that is expected and is
    asserted here so the cost is visible rather than surprising. What must NOT
    load is ``kiro_crew.acp`` itself, which stays out only because
    ``drivers/acp.py`` defers those imports into function bodies.

    The static boundary gate cannot catch a regression here: if the driver
    stopped deferring, the new edge would sit INSIDE the exempt ``agent_sdk/``
    tree and the gate would stay green while every consumer of the SDK began
    loading the ACP package at import time.

    Runs in a subprocess because ``sys.modules`` is process-wide -- by the time
    this test executes, the suite has already imported half the package.
    """
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC.parent)!r})\n"
        "from kiro_crew.agent_sdk.provider_identity import is_claude_code\n"
        "assert is_claude_code('claude_code') is True\n"
        "loaded = {\n"
        "    'backend_install': 'kiro_crew.agent_sdk.backend_install' in sys.modules,\n"
        "    'drivers_acp': 'kiro_crew.agent_sdk.drivers.acp' in sys.modules,\n"
        "    'acp': 'kiro_crew.acp' in sys.modules,\n"
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

    assert loaded["acp"] is False, (
        "importing agent_sdk.provider_identity loaded kiro_crew.acp; "
        "drivers/acp.py must keep deferring its ACP imports into function bodies, "
        "or every SDK consumer pays the ACP package at import time"
    )
    # Asserted, not merely tolerated: these DO load, and the module docstring
    # says so. If a future change makes them lazy, update the docstring too.
    assert loaded["backend_install"] is True
    assert loaded["drivers_acp"] is True


def _literal_comparisons(path: Path) -> list[int]:
    """Line numbers where code compares something against ``"claude_code"``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not all(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue
        operands = [node.left, *node.comparators]
        for operand in operands:
            if isinstance(operand, ast.Constant) and operand.value == PROVIDER_CLAUDE_CODE:
                hits.append(node.lineno)
                break
    return hits


@pytest.mark.parametrize("rel", CONVERTED)
def test_no_converted_file_compares_the_literal(rel: str) -> None:
    """The converted branches must keep asking the predicate.

    Reads comparisons out of the AST, so the surviving prose mentions in
    ``context.py`` and ``chat_runner.py`` are correctly ignored -- and so are
    the ``model_registry`` namespace arguments, which are call arguments rather
    than comparisons.
    """
    hits = _literal_comparisons(SRC / rel)
    assert hits == [], (
        f'{rel} compares the raw "{PROVIDER_CLAUDE_CODE}" literal at line(s) '
        f"{hits}; ask is_claude_code() instead so provider-specific logic stays "
        f"findable in one place"
    )


@pytest.mark.parametrize("rel", CONVERTED)
def test_converted_files_ask_the_predicate(rel: str) -> None:
    """Non-vacuity guard for the ratchet above.

    Without this, deleting the branch (or renaming the file) would make the
    ratchet pass while the provider-specific logic went somewhere unwatched.
    """
    body = (SRC / rel).read_text(encoding="utf-8")
    assert "is_claude_code" in body, f"{rel} no longer asks is_claude_code()"


def test_onboarding_import_source_id_is_left_alone() -> None:
    """``onboarding_import`` keeps its own ``"claude_code"``, on purpose.

    There it identifies the Claude Code desktop app whose settings are being
    imported (``~/.claude``, ``CLAUDE_CONFIG_DIR``) -- a foreign config tree on
    disk, not the provider this process runs on. Routing it through the
    predicate would assert a provider identity that has nothing to do with the
    question being asked.
    """
    body = (SRC / "onboarding_import.py").read_text(encoding="utf-8")
    assert '"claude_code"' in body
    assert "is_claude_code" not in body


def test_model_registry_keeps_its_index_key() -> None:
    """``model_registry`` keeps the string as a namespace key, on purpose.

    The module states the reason where the constant is defined: *"Named for the
    registry key, NOT because windows are claude_code-only."* A model's context
    window belongs to the model, not to the provider serving it, so these are
    not provider checks and must not become any.
    """
    body = (SRC / "model_registry.py").read_text(encoding="utf-8")
    assert '_WINDOW_INDEX = "claude_code"' in body
    assert "is_claude_code" not in body
