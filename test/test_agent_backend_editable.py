"""``agent.acp_backend`` is writable from the dashboard, and only to real backends.

The Settings > Agent Harness switch writes it over ``PATCH /api/config/kirocrew``.
An edition extends the backend registry at boot (``register_selectable_backend``), so
each surface must resolve the set per request from ``agent_sdk.backends``.
"""

from typing import Any, Dict, List

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
    ACP_BACKEND_STRANDS,
)
from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import _supply_live_enum
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

FIELD = "agent.acp_backend"

#: Known ids the public baseline deliberately does not offer, each entry carrying its
#: reason in ``test_baseline_ships_every_known_backend``. An entry is a reasoned
#: exclusion rather than a defect, and it earns its place by naming what the id fails.
#: Empty: an id whose tool calls bypass the PreToolUse gate stays out until its
#: routing is verified (e.g. ``Routing.VERIFIED_GATE_EXTENSION``).
NOT_SHIPPED_SELECTABLE: frozenset = frozenset()


@pytest.fixture
def restore_registry():
    """Snapshot/restore the module-global selectable sets around a mutation.

    Both sets, since ``register_selectable_backend`` writes both. Reached through
    ``agent_sdk.backends``, which defines them; the shim re-exports public names only.
    """
    baseline_before = set(acp_backends._baseline)
    before = set(acp_backends._selectable)
    yield
    acp_backends._baseline.clear()
    acp_backends._baseline.update(baseline_before)
    acp_backends._selectable.clear()
    acp_backends._selectable.update(before)


def test_acp_backend_is_editable_from_the_dashboard():
    assert FIELD in _EDITABLE_CONFIG, f"{FIELD} must be PATCH-able or the switch cannot save"
    assert _EDITABLE_CONFIG[FIELD]["type"] == "enum"


def test_the_allowlist_resolves_the_set_and_never_carries_a_literal():
    """A static ``values`` list cannot see boot registration.

    A literal would fail a registered backend's PATCH with "invalid value".
    """
    spec = _EDITABLE_CONFIG[FIELD]
    assert "values" not in spec, "a frozen list here is exactly the drift being removed"
    assert callable(spec["values_fn"])
    # The config-file vocabulary: Kiro CLI is "kiro", and "" stores "no choice".
    assert set(spec["values_fn"]()) == {
        acp_backends.backend_to_stored(b) for b in acp_backends.selectable_backends()
    } | {""}


def test_the_allowlist_speaks_the_stored_vocabulary():
    """A Kiro CLI pick is written as "kiro"; "" is "no choice", never Kiro CLI."""
    allowed = _EDITABLE_CONFIG[FIELD]["values_fn"]()
    assert acp_backends.STORED_BACKEND_KIRO in allowed
    assert acp_backends.STORED_BACKEND_KIRO == acp_backends.POLICY_ID_KIRO


def test_the_default_backend_is_accepted_by_its_own_allowlist():
    """The shipped default must be writable, or the switch cannot be reset."""
    assert KiroCrewConfig().agent.acp_backend in _EDITABLE_CONFIG[FIELD]["values_fn"]()


def test_a_registered_backend_reaches_the_allowlist(restore_registry):
    """An edition registering a backend must not need a core edit to be writable.

    Every known backend is in the baseline, so the unregistered state is constructed;
    the point is that the allowlist resolves the registry per call.
    """
    acp_backends._baseline.discard(ACP_BACKEND_CLAUDE)
    acp_backends._selectable.discard(ACP_BACKEND_CLAUDE)
    assert ACP_BACKEND_CLAUDE not in _EDITABLE_CONFIG[FIELD]["values_fn"]()

    acp_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)

    assert ACP_BACKEND_CLAUDE in _EDITABLE_CONFIG[FIELD]["values_fn"]()


def test_registering_an_unknown_backend_is_refused(restore_registry):
    """A dashboard option that cannot start a session is worse than an absent one.

    Matched on the unknown-id message: an unknown id is also ``UNVERIFIED``
    (``routing_for`` fails closed), so a bare ``ValueError`` would pass without this guard.
    """
    with pytest.raises(ValueError, match="unknown ACP backend"):
        acp_backends.register_selectable_backend("byo-harness")
    assert "byo-harness" not in acp_backends.selectable_backends()


def test_the_schema_endpoint_serves_the_same_set_as_the_allowlist():
    """GET /api/config/schema drives which options the UI enables.

    A disagreeing enum would show an option that is enabled and then refused.
    """
    entry: Dict[str, Any] = {"path": FIELD, "enumValues": None}
    _supply_live_enum(entry)
    # The PATCH also takes "" ("no choice"), which is not an option to render.
    assert entry["enumValues"] == sorted(set(_EDITABLE_CONFIG[FIELD]["values_fn"]()) - {""})
    assert acp_backends.STORED_BACKEND_KIRO in entry["enumValues"]
    assert "" not in entry["enumValues"]


def test_the_schema_endpoint_leaves_other_fields_alone():
    """The binding is one path, not a blanket rewrite of every enum."""
    entry: Dict[str, Any] = {"path": "agent.provider", "enumValues": ["acp"]}
    _supply_live_enum(entry)
    assert entry["enumValues"] == ["acp"]


def test_a_registered_backend_reaches_the_schema_endpoint(restore_registry):
    """The other half of the same guarantee: the UI lights it up with no FE change."""
    acp_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)
    entry: Dict[str, Any] = {"path": FIELD, "enumValues": None}
    _supply_live_enum(entry)
    assert ACP_BACKEND_CLAUDE in entry["enumValues"]


def test_the_field_declares_no_static_enum():
    """The field metadata carries no static enum either; the set is resolved live."""
    meta = KiroCrewConfig().agent.__dataclass_fields__["acp_backend"].metadata
    assert meta.get("enum") is None


def test_baseline_ships_every_known_backend():
    """The public build's capability, stated once so a NARROWING is deliberate.

    An id may sit outside the baseline only by being named in
    ``NOT_SHIPPED_SELECTABLE`` with its reason. A missing install is the install
    probe's job, not a reason to drop the switch.
    """
    baseline: List[str] = sorted(acp_backends.BASELINE_SELECTABLE_BACKENDS)
    assert baseline == sorted(
        [
            ACP_BACKEND_KIRO,
            ACP_BACKEND_CLAUDE,
            ACP_BACKEND_KAS,
            ACP_BACKEND_CODEX,
            ACP_BACKEND_OPENCODE,
            ACP_BACKEND_PI,
            ACP_BACKEND_GOOSE,
            ACP_BACKEND_DEEPSEEK,
            ACP_BACKEND_COPILOT,
            ACP_BACKEND_STRANDS,
        ]
    )
    assert baseline == sorted(acp_backends.ACP_BACKENDS_KNOWN - NOT_SHIPPED_SELECTABLE)
