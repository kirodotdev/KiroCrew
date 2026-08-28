"""``agent.acp_backend`` is writable from the dashboard, and only to real backends.

The Developer > Agent Backend switch writes this field over
``PATCH /api/config/kirocrew``, so it has to be in ``_EDITABLE_CONFIG`` at all —
before this it was absent and every save came back "field not editable".

The load-bearing tests here used to be PARITY ones: three unrelated places each
kept a literal copy of the selectable-backend list, and these tests stood in for a
code owner. They no longer can. The set is a REGISTRY an edition extends at boot
(``register_selectable_backend``), which no import-time literal can see. So what is
pinned now is that each surface RESOLVES the set at request time from the one
owner, ``acp_backends``, rather than carrying its own answer.
"""

from typing import Any, Dict, List

import pytest

from kiro_crew import acp_backends
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.agents import _supply_live_enum
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

FIELD = "agent.acp_backend"


@pytest.fixture
def restore_registry():
    """Snapshot/restore the module-global selectable set around a mutation."""
    before = set(acp_backends._selectable)
    yield
    acp_backends._selectable.clear()
    acp_backends._selectable.update(before)


def test_acp_backend_is_editable_from_the_dashboard():
    assert FIELD in _EDITABLE_CONFIG, f"{FIELD} must be PATCH-able or the switch cannot save"
    assert _EDITABLE_CONFIG[FIELD]["type"] == "enum"


def test_the_allowlist_resolves_the_set_and_never_carries_a_literal():
    """A static ``values`` list cannot see boot registration.

    This is the drift fix itself: the old literal made a registered backend fail
    the PATCH with a misleading "invalid value", which is what the dashboard
    surfaced as an option that was "not enabled in this build" on a build that had
    it.
    """
    spec = _EDITABLE_CONFIG[FIELD]
    assert "values" not in spec, "a frozen list here is exactly the drift being removed"
    assert callable(spec["values_fn"])
    assert set(spec["values_fn"]()) == set(acp_backends.selectable_backends())


def test_the_default_backend_is_accepted_by_its_own_allowlist():
    """The shipped default must be writable, or the switch cannot be reset."""
    assert KiroCrewConfig().agent.acp_backend in _EDITABLE_CONFIG[FIELD]["values_fn"]()


def test_a_registered_backend_reaches_the_allowlist(restore_registry):
    """An edition registering a backend must not need a core edit to be writable."""
    assert ACP_BACKEND_CLAUDE not in _EDITABLE_CONFIG[FIELD]["values_fn"]()
    acp_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)
    assert ACP_BACKEND_CLAUDE in _EDITABLE_CONFIG[FIELD]["values_fn"]()


def test_registering_an_unknown_backend_is_refused(restore_registry):
    """A dashboard option that cannot start a session is worse than an absent one."""
    with pytest.raises(ValueError):
        acp_backends.register_selectable_backend("byo-harness")
    assert "byo-harness" not in acp_backends.selectable_backends()


def test_the_schema_endpoint_serves_the_same_set_as_the_allowlist():
    """GET /api/config/schema drives which options the UI enables.

    The tab renders every known backend but disables any value the schema does not
    advertise, so a schema enum that disagreed with the PATCH allowlist would show
    an option that is enabled and then refused (or hide one that works).
    """
    entry: Dict[str, Any] = {"path": FIELD, "enumValues": None}
    _supply_live_enum(entry)
    assert entry["enumValues"] == _EDITABLE_CONFIG[FIELD]["values_fn"]()


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
    """The frozen copy is gone from the field metadata too.

    Kept as its own assertion here (and not only in ``test_harness_parity``)
    because this file is where someone re-adding ``enum=["", "kas"]`` to make the
    schema "self-describing" would look for permission.
    """
    meta = KiroCrewConfig().agent.__dataclass_fields__["acp_backend"].metadata
    assert meta.get("enum") is None


def test_baseline_ships_kiro_and_kas_only():
    """The public build's capability, stated once so a widening is deliberate."""
    baseline: List[str] = sorted(acp_backends.BASELINE_SELECTABLE_BACKENDS)
    assert baseline == sorted([ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
