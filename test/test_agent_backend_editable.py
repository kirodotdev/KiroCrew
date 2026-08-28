"""``agent.acp_backend`` is writable from the dashboard, and only to real backends.

The Developer > Agent Backend switch writes this field over
``PATCH /api/config/kirocrew``, so it has to be in ``_EDITABLE_CONFIG`` at all —
before this it was absent and every save came back "field not editable".

The load-bearing test here is the PARITY one. Both the editable-config validator
and the schema metadata resolve the selectable-backend set lazily, avoiding an
import cycle while keeping one source of truth. Widening or narrowing
``ACP_BACKENDS_SELECTABLE`` must therefore change both surfaces together.
"""

from kiro_crew.acp.types import ACP_BACKENDS_SELECTABLE
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

FIELD = "agent.acp_backend"


def test_acp_backend_is_editable_from_the_dashboard():
    assert FIELD in _EDITABLE_CONFIG, f"{FIELD} must be PATCH-able or the switch cannot save"
    assert _EDITABLE_CONFIG[FIELD]["type"] == "str"
    assert callable(_EDITABLE_CONFIG[FIELD]["values_fn"])


def test_editable_values_equal_the_selectable_backends():
    """The allowlist and the set AcpProvider can serve must be the same set."""
    assert set(_EDITABLE_CONFIG[FIELD]["values_fn"]()) == set(ACP_BACKENDS_SELECTABLE)


def test_the_default_backend_is_accepted_by_its_own_allowlist():
    """The shipped default must be writable, or the switch cannot be reset."""
    assert KiroCrewConfig().agent.acp_backend in _EDITABLE_CONFIG[FIELD]["values_fn"]()


def test_the_field_schema_enum_matches_the_allowlist():
    """GET /api/config/schema drives which options the UI enables.

    The tab renders every known backend but disables any value the schema does not
    advertise, so a schema enum that disagreed with the PATCH allowlist would show
    an option that is enabled and then refused (or hide one that works).
    """
    meta = KiroCrewConfig().agent.__dataclass_fields__["acp_backend"].metadata
    assert callable(meta["enum"])
    assert set(meta["enum"]()) == set(_EDITABLE_CONFIG[FIELD]["values_fn"]())
