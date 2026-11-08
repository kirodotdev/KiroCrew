"""The capability sets must be askable without importing ``kiro_crew.acp``.

``kiro_crew.acp`` is a FORBIDDEN_ROOT for the agent-SDK boundary gate, so the sets are
defined in the leaf :mod:`kiro_crew.agent_sdk.backends` and re-exported from
``acp.types``. ``kiro_crew.acp_backends`` is a pure re-export shim; a definition in it
would be reachable without crossing the boundary.
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
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_SEED_LOCAL_SETTINGS,
    ACP_BACKENDS_SESSION_EVICTION,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE,
    ACP_BACKENDS_STEER,
    model_registry_namespace,
)
from kiro_crew.agent_sdk.host_auth import backends_retired_by_host_logout
from kiro_crew.subprocess_utf8 import UTF8_TEXT

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: The sets whose definition home is the leaf. The kiro-identity-store membership is
#: deliberately absent: it is ``host_auth.backends_retired_by_host_logout()``,
#: projected from each harness's own auth declaration rather than opted into here. It
#: is a FUNCTION and not an ``ACP_BACKENDS_*`` set because that naming is vocabulary,
#: whose home is the leaf -- and it cannot live in the leaf either, which supplies the
#: backend ids that table is keyed by. Its home, its value and its
#: re-export identity are pinned in ``test_agent_sdk_host_auth.py``; the value pin
#: below stays here too, because the harness membership is still this module's subject.
CAPABILITY_SETS = (
    "ACP_BACKENDS_ACP_RUNTIME",
    "ACP_BACKENDS_ADVERTISED_MODEL_SELECTION",
    "ACP_BACKENDS_COMPACT",
    "ACP_BACKENDS_INTERNAL_SANDBOX",
    "ACP_BACKENDS_SEED_LOCAL_SETTINGS",
    "ACP_BACKENDS_SESSION_EVICTION",
    "ACP_BACKENDS_SESSION_SHARING",
    "ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE",
    "ACP_BACKENDS_STEER",
)


@pytest.mark.parametrize("name", CAPABILITY_SETS)
def test_defined_in_the_leaf_not_in_the_acp_package(name: str) -> None:
    """The definition must sit in ``agent_sdk.backends``, never back in ``acp.types``.

    Moving one back passes every other test but adds a forbidden edge. The third
    assertion keeps sets out of the ``acp_backends`` shim too.
    """
    leaf = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            SRC / "agent_sdk" / "backends.py",
            *sorted(SRC.glob("agent_sdk/backends_*.py")),
        )
    )
    types_mod = (SRC / "acp" / "types.py").read_text(encoding="utf-8")
    shim = (SRC / "acp_backends.py").read_text(encoding="utf-8")

    assert f"\n{name} = frozenset(" in leaf, f"{name} is not defined in agent_sdk/backends.py"
    assert f"\n{name} = frozenset(" not in types_mod, (
        f"{name} is defined in acp/types.py again; define it in the leaf "
        f"agent_sdk/backends.py so a consumer can read it without importing kiro_crew.acp"
    )
    assert f"\n{name} = frozenset(" not in shim, (
        f"{name} is defined in the acp_backends shim; that file must only re-export "
        f"from agent_sdk/backends.py, or the boundary has a second front door"
    )


@pytest.mark.parametrize("name", CAPABILITY_SETS)
def test_the_re_export_is_the_same_object(name: str) -> None:
    """Existing ``from kiro_crew.acp.types import ...`` call sites keep working.

    Identity, not equality: a copy would drift the moment one side is edited.
    """
    import kiro_crew.acp_backends as shim
    import kiro_crew.agent_sdk.backends as leaf
    from kiro_crew.acp import types as acp_types

    assert getattr(acp_types, name) is getattr(leaf, name), (
        f"acp.types.{name} is a different object than agent_sdk.backends.{name}; "
        f"re-export it rather than redefining it"
    )
    assert getattr(shim, name) is getattr(leaf, name), (
        f"acp_backends.{name} is a different object than agent_sdk.backends.{name}; "
        f"the shim must re-export, never copy"
    )


def test_reading_a_capability_set_does_not_load_the_acp_package() -> None:
    """Reading a set must not import ``kiro_crew.acp``.

    The boundary gate stays green here because those consumers are in its baseline.
    Runs in a subprocess, since the suite has already imported most of the package.
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
    # kiro and codex are in all three; KAS is on the runtime and in eviction, and
    # out of sharing. No two of these sets may be derived from another, and KAS is
    # the case that shows it: eviction and sharing both read its teardown verb and
    # reach opposite conclusions, because ``_kiro/session/delete`` frees the session
    # (eviction) by REMOVING the record a continuation would load (no sharing).
    # codex's ``session/close`` frees the session and leaves the record, so it is in
    # both.
    assert ACP_BACKENDS_SESSION_SHARING == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_CODEX})
    # The three harnesses added on captured evidence are named here too, so a
    # relocation still cannot grant the capability quietly -- the pin moves with
    # the deliberate edit rather than being loosened to accommodate it.
    assert ACP_BACKENDS_COMPACT == frozenset(
        {
            ACP_BACKEND_KIRO,
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_CODEX,
            ACP_BACKEND_OPENCODE,
            ACP_BACKEND_GOOSE,
            ACP_BACKEND_KAS,
            ACP_BACKEND_PI,
            ACP_BACKEND_COPILOT,
        }
    )
    assert ACP_BACKENDS_INTERNAL_SANDBOX == frozenset({ACP_BACKEND_KIRO})
    assert ACP_BACKENDS_STEER == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})
    assert ACP_BACKENDS_ACP_RUNTIME == frozenset(
        {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX}
    )
    assert ACP_BACKENDS_SESSION_EVICTION == frozenset(
        {ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_CODEX}
    )
    # The spec's own servers reach kiro-cli from disk and KAS as a projected agent
    # definition; codex mounts exactly the array it is sent, so it is judged by it.
    assert ACP_BACKENDS_SPEC_SERVERS_OFF_WIRE == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})
    assert backends_retired_by_host_logout() == frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS})
    # claude for the spelling fold; codex, opencode, pi and deepseek because only the
    # captured select holds ids the adapter accepts (``provider/model`` pairs, or
    # deepseek's JSON ``[provider, model]``). The settings seed stays claude-only.
    assert ACP_BACKENDS_ADVERTISED_MODEL_SELECTION == frozenset(
        {
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_CODEX,
            ACP_BACKEND_OPENCODE,
            ACP_BACKEND_PI,
            ACP_BACKEND_GOOSE,
            ACP_BACKEND_DEEPSEEK,
            ACP_BACKEND_COPILOT,
        }
    )
    assert ACP_BACKENDS_SEED_LOCAL_SETTINGS == frozenset({ACP_BACKEND_CLAUDE})


def test_model_registry_namespace_maps_every_known_backend() -> None:
    """The namespace is a registry index selector, and the same key selects the
    advertised-model cache bucket. The kiro family lives in the ``acp`` namespace;
    claude uses ``claude_code``; codex has its own, because codex's served ids are not
    kiro's."""
    assert model_registry_namespace(ACP_BACKEND_CLAUDE) == "claude_code"
    assert model_registry_namespace(ACP_BACKEND_KIRO) == "acp"
    assert model_registry_namespace(ACP_BACKEND_KAS) == "acp"
    assert model_registry_namespace(ACP_BACKEND_CODEX) == "codex"
    # An unknown/unregistered backend defaults to the kiro namespace, never crashes.
    assert model_registry_namespace("something-new") == "acp"


def test_a_harness_that_persists_a_catalog_owns_its_own_namespace() -> None:
    """A member of ADVERTISED_MODEL_SELECTION must not share the kiro bucket.

    These harnesses persist captured models under this key; sharing ``acp`` would
    replace kiro-cli's catalog. Derived from the set, so a new member is covered, and
    no two members may share a bucket.
    """
    kiro_namespace = model_registry_namespace(ACP_BACKEND_KIRO)
    seen: dict = {}
    for backend in sorted(ACP_BACKENDS_ADVERTISED_MODEL_SELECTION):
        namespace = model_registry_namespace(backend)
        assert namespace != kiro_namespace, (
            f"{backend!r} persists an advertised catalog but shares the kiro namespace "
            f"{namespace!r}, so its models overwrite kiro-cli's in the picker -- give it "
            "its own key in _MODEL_REGISTRY_NAMESPACE_BY_BACKEND"
        )
        assert namespace not in seen, (
            f"{backend!r} and {seen[namespace]!r} both persist advertised catalogs into "
            f"namespace {namespace!r}, so whichever starts last wins"
        )
        seen[namespace] = backend
    assert seen, "no harness persists an advertised catalog, so this ratchet is vacuous"


def test_acp_runtime_is_a_superset_of_session_sharing() -> None:
    """The documented relationship between the two sets, asserted rather than described.

    AcpRuntime is necessary, not sufficient: KAS runs there but is excluded, since its
    teardown removes the record a continuation would load.
    """
    assert ACP_BACKENDS_SESSION_SHARING <= ACP_BACKENDS_ACP_RUNTIME
    assert ACP_BACKEND_KAS in ACP_BACKENDS_ACP_RUNTIME
    assert ACP_BACKEND_KAS not in ACP_BACKENDS_SESSION_SHARING
    # Sharing is a PROPER subset, and KAS is now the only member of the runtime that
    # demonstrates it -- so the assertion is kept rather than dropped when codex
    # moved across, or nothing would hold the two sets apart.
    assert ACP_BACKENDS_SESSION_SHARING != ACP_BACKENDS_ACP_RUNTIME
    # codex is in ALL THREE, and the pair below is the fact that puts it in sharing
    # without softening its teardown: the session is evicted, and the record the
    # harness owns is what a ``session/load`` restores.
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_ACP_RUNTIME
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_SHARING
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_SESSION_EVICTION
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    # KAS is in eviction too, so eviction cannot be what sharing is read off.
    assert ACP_BACKEND_KAS in ACP_BACKENDS_SESSION_EVICTION
