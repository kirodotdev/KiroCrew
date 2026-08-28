"""``agent.acp_backend`` is writable from the dashboard, and only to real backends.

The Developer > Agent Backend switch writes this field over
``PATCH /api/config/kirocrew``, so it has to be in ``_EDITABLE_CONFIG`` at all —
before this it was absent and every save came back "field not editable".

The load-bearing test here is the PARITY one. ``_EDITABLE_CONFIG`` duplicates the
selectable-backend list as a literal because importing ``kiro_crew.acp.types`` at
module scope would execute the ``kiro_crew.acp`` package init (client + runtime)
while this dict is being built — the import cycle
``config.loader._normalize_acp_backend`` defers for as well. A literal that nobody
checks is a literal that drifts: widening ``ACP_BACKENDS_SELECTABLE`` without
touching the handler would leave the new backend rejected by the dashboard with a
misleading "invalid value", and NARROWING it would let the dashboard write a value
the loader then silently coerces back. Both directions fail here instead.
"""

from kiro_crew.acp.types import ACP_BACKENDS_SELECTABLE
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

FIELD = "agent.acp_backend"


def test_acp_backend_is_editable_from_the_dashboard():
    assert FIELD in _EDITABLE_CONFIG, f"{FIELD} must be PATCH-able or the switch cannot save"
    assert _EDITABLE_CONFIG[FIELD]["type"] == "enum"


def test_editable_values_equal_the_selectable_backends():
    """The allowlist and the set AcpProvider can serve must be the same set."""
    assert set(_EDITABLE_CONFIG[FIELD]["values"]) == set(ACP_BACKENDS_SELECTABLE)


def test_the_default_backend_is_accepted_by_its_own_allowlist():
    """The shipped default must be writable, or the switch cannot be reset."""
    assert KiroCrewConfig().agent.acp_backend in _EDITABLE_CONFIG[FIELD]["values"]


def test_the_field_schema_enum_matches_the_allowlist():
    """GET /api/config/schema drives which options the UI enables.

    The tab renders every known backend but disables any value the schema does not
    advertise, so a schema enum that disagreed with the PATCH allowlist would show
    an option that is enabled and then refused (or hide one that works).
    """
    meta = KiroCrewConfig().agent.__dataclass_fields__["acp_backend"].metadata
    assert set(meta["enum"]) == set(_EDITABLE_CONFIG[FIELD]["values"])
