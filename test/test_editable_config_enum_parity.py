"""Every Settings enum offers exactly the values the config schema declares.

``dashboard/handlers/core_config_schema.py`` hand-maintains a ``values`` list for each
``_EDITABLE_CONFIG`` entry of ``type: enum``. The config schema
(``config/sections.py`` ``_meta(enum=...)``, exported as ``SCHEMA_REGISTRY``)
declares the same set for the CLI and the loader. When the two lists differ, a
value the CLI admits cannot be picked in Settings, or Settings offers a value
the loader degrades back to the default.

The walk covers every editable enum, so a new key is checked the day it lands.
A key whose schema entry carries no enum at all is listed in
``_SCHEMA_DECLARES_NO_ENUM`` with the reason, and that list is pinned exactly:
a new editable enum without a schema enum fails here until it is either given
one or named below, and an entry that gains a schema enum must leave the list.
"""

from __future__ import annotations

import pytest

from kiro_crew.config.schema import SCHEMA_REGISTRY
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

# Editable enum keys whose schema entry has no ``enum`` to compare against.
_SCHEMA_DECLARES_NO_ENUM: dict[str, str] = {
    # The selectable backends are registered at boot, after the schema is
    # built; a frozen schema enum would strip a registered backend from
    # config.json. Settings reads the live set through ``values_fn``.
    "agent.acp_backend": "live set from the backend registry",
    # ``agent.role_efforts`` is one dict-typed schema entry; its per-role
    # keys are dashboard paths only. Both offer "" plus ``EFFORT_LEVELS``.
    "agent.role_efforts.background": "per-role key of a dict entry",
    "agent.role_efforts.subagent": "per-role key of a dict entry",
    # Onboarding profile slugs: the schema types them as plain strings.
    "dashboard.user_role": "plain string in the schema",
    "dashboard.user_technical_level": "plain string in the schema",
    # The schema types it as a string; Settings offers ``JUDGE_PROVIDERS``.
    "decisions.nudge_wake.provider": "plain string in the schema",
}


def _editable_enum_paths() -> list[str]:
    return sorted(path for path, spec in _EDITABLE_CONFIG.items() if spec.get("type") == "enum")


def _schema_enums() -> dict[str, list | None]:
    return {entry.path: entry.enum_values for entry in SCHEMA_REGISTRY}


def _compared_paths() -> list[str]:
    return [p for p in _editable_enum_paths() if p not in _SCHEMA_DECLARES_NO_ENUM]


def test_the_walk_finds_editable_enums_to_compare() -> None:
    """An empty walk would pass every parity check below without checking one."""
    assert len(_compared_paths()) >= 5


@pytest.mark.parametrize("path", _compared_paths())
def test_settings_offers_the_schema_enum(path: str) -> None:
    schema = _schema_enums()
    assert path in schema, f"{path} is editable in Settings but absent from the config schema"
    declared = schema[path]
    hint = f"{path} has no schema enum; give it one or name it in _SCHEMA_DECLARES_NO_ENUM"
    assert declared is not None, hint
    assert _EDITABLE_CONFIG[path]["values"] == declared


def test_the_no_enum_list_names_exactly_the_keys_without_a_schema_enum() -> None:
    """Pins the exception list both ways: nothing missing, nothing stale."""
    schema = _schema_enums()
    without_enum = {p for p in _editable_enum_paths() if schema.get(p) is None}
    assert without_enum == set(_SCHEMA_DECLARES_NO_ENUM)
