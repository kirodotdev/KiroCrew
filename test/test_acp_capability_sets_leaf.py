"""The capability sets must be askable without importing ``kiro_crew.acp``.

They used to be defined in ``kiro_crew.acp.types``. Importing anything under
``kiro_crew.acp`` executes that package's ``__init__`` (client + runtime), and
``kiro_crew.acp`` is a FORBIDDEN_ROOT for the agent-SDK boundary gate -- so every
consumer outside the ACP layer that asked a capability question had to add a
forbidden edge, against a baseline that may only shrink. Onboarding a new harness
means teaching outside consumers (readiness, prerequisite, MCP wiring) to ask these
sets, so that cost was about to be paid repeatedly.

Definitions now live in the leaf ``kiro_crew.acp_backends`` and are re-exported from
``acp.types`` for existing importers. These tests pin both halves: the leaf stays
reachable without the ACP package, and the re-export keeps working.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_IDENTITY_STORE,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    model_registry_namespace,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

CAPABILITY_SETS = (
    "ACP_BACKENDS_ACP_RUNTIME",
    "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION",
    "ACP_BACKENDS_COMPACT",
    "ACP_BACKENDS_INTERNAL_SANDBOX",
    "ACP_BACKENDS_KIRO_IDENTITY_STORE",
    "ACP_BACKENDS_SEED_LOCAL_SETTINGS",
    "ACP_BACKENDS_SESSION_SHARING",
    "ACP_BACKENDS_STEER",
)


@pytest.mark.parametrize("name", CAPABILITY_SETS)
def test_defined_in_the_leaf_not_in_the_acp_package(name: str) -> None:
    """The definition must sit in ``acp_backends``, never back in ``acp.types``.

    A future edit that moves one back would compile and pass every other test while
    silently re-imposing a forbidden edge on each consumer that reads it.
    """
    leaf = (SRC / "acp_backends.py").read_text(encoding="utf-8")
    types_mod = (SRC / "acp" / "types.py").read_text(encoding="utf-8")

    assert f"\n{name} = frozenset(" in leaf, f"{name} is not defined in acp_backends.py"
    assert f"\n{name} = frozenset(" not in types_mod, (
        f"{name} is defined in acp/types.py again; define it in the leaf "
        f"acp_backends.py so a consumer can read it without importing kiro_crew.acp"
    )


@pytest.mark.parametrize("name", CAPABILITY_SETS)
def test_the_re_export_is_the_same_object(name: str) -> None:
    """Existing ``from kiro_crew.acp.types import ...`` call sites keep working.

    Identity, not equality: a copy would drift the moment one side is edited.
    """
    import kiro_crew.acp_backends as leaf
    from kiro_crew.acp import types as acp_types

    assert getattr(acp_types, name) is getattr(leaf, name), (
        f"acp.types.{name} is a different object than acp_backends.{name}; "
        f"re-export it rather than redefining it"
    )


def test_reading_a_capability_set_does_not_load_the_acp_package() -> None:
    """The whole point of the move, and the reason it needs a test.

    The boundary gate counts import edges; it cannot see that this one is now
    avoidable. If the sets drifted back behind ``kiro_crew.acp``, every consumer
    would silently start paying that package again -- and the gate would stay green
    because those consumers are already in its baseline.

    Runs in a subprocess: by the time this test executes, the suite has imported
    most of the package already, so an in-process ``sys.modules`` check proves
    nothing.
    """
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC.parent)!r})\n"
        "from kiro_crew.acp_backends import ACP_BACKENDS_STEER, ACP_BACKENDS_ACP_RUNTIME\n"
        "assert isinstance(ACP_BACKENDS_STEER, frozenset)\n"
        "print(repr({'acp': 'kiro_crew.acp' in sys.modules,\n"
        "            'providers': 'kiro_crew.providers' in sys.modules}))\n"
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
        "reading a capability set loaded kiro_crew.acp; the sets must stay in the "
        "leaf so an outside consumer can ask without a forbidden-root import"
    )
    assert loaded["providers"] is False, "reading a capability set loaded kiro_crew.providers"


def test_membership_is_unchanged_by_the_move() -> None:
    """Pin the actual members, so the move cannot quietly grant a capability.

    Opting a harness in is a deliberate edit with evidence (harness-parity H5/H6);
    a relocation is not the place for it.
    """
    assert ACP_BACKENDS_SESSION_SHARING == frozenset({ACP_BACKEND_KIRO})
    assert ACP_BACKENDS_COMPACT == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE})
    assert ACP_BACKENDS_INTERNAL_SANDBOX == frozenset({ACP_BACKEND_KIRO})
    assert ACP_BACKENDS_STEER == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})
    assert ACP_BACKENDS_ACP_RUNTIME == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})
    assert ACP_BACKENDS_KIRO_IDENTITY_STORE == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})
    # The provider-advertised-model seams: claude only today. A future adapter
    # with the same served-vs-stored spelling gap (or its own settings seed) opts
    # in here — a deliberate edit this pin forces to be seen.
    assert ACP_BACKENDS_ADVERTISED_MODEL_SELECTION == frozenset({ACP_BACKEND_CLAUDE})
    assert ACP_BACKENDS_SEED_LOCAL_SETTINGS == frozenset({ACP_BACKEND_CLAUDE})


def test_model_registry_namespace_maps_every_known_backend() -> None:
    """The namespace is a registry index selector, mapped for every backend so a
    future ADVERTISED_MODEL_SELECTION member already has an entry. Non-claude ids
    live in the ``acp`` namespace; only claude uses ``claude_code``."""
    assert model_registry_namespace(ACP_BACKEND_CLAUDE) == "claude_code"
    assert model_registry_namespace(ACP_BACKEND_KIRO) == "acp"
    assert model_registry_namespace(ACP_BACKEND_KAS) == "acp"
    assert model_registry_namespace(ACP_BACKEND_CODEX) == "acp"
    # An unknown/unregistered backend defaults to the kiro namespace, never crashes.
    assert model_registry_namespace("something-new") == "acp"


def test_acp_runtime_is_a_superset_of_session_sharing() -> None:
    """The documented relationship between the two sets, asserted rather than described.

    Running on AcpRuntime is necessary for session sharing but not sufficient: KAS
    runs there yet is excluded from sharing until keep-aware teardown lands. A future
    edit that adds a harness to sharing without adding it to the runtime set would
    describe a backend that multiplexes sessions without a multiplexer.
    """
    assert ACP_BACKENDS_SESSION_SHARING <= ACP_BACKENDS_ACP_RUNTIME
    assert ACP_BACKEND_KAS in ACP_BACKENDS_ACP_RUNTIME
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_SESSION_SHARING
