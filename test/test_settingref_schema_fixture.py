"""Cross-layer drift guard: ensures every key in the frontend SettingRef
schema fixture (shared JSON) exists in the real backend SCHEMA_REGISTRY with
matching ``path`` and ``type``.

Additionally, every ``configKey`` in the generated settingsRegistry.gen.ts
that maps a UI control to a backend config entry is asserted to exist in
SCHEMA_REGISTRY — catching typos or backend renames that would make a
SettingRef chip inert at runtime.

ENV-KEY drift guard: ensures every env var name in settingref-env-vars.json
actually appears as a literal string in the backend source (src/kiro_crew/).
A typo'd call-site var would fail vitest (not in fixture); a stale fixture
entry would fail this pytest (not in source).

If a backend rename or type change breaks a frontend <SettingRef> call site,
this test will fail — alerting the developer before the change ships.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from kiro_crew.config.schema import SCHEMA_REGISTRY

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "website"
    / "src"
    / "test"
    / "fixtures"
    / "settingref-schema.json"
)

ENV_VARS_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "website"
    / "src"
    / "test"
    / "fixtures"
    / "settingref-env-vars.json"
)

SETTINGS_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent
    / "website"
    / "src"
    / "components"
    / "commandPalette"
    / "settingsRegistry.gen.ts"
)

BACKEND_SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

CONFIG_KEY_RE = re.compile(r'"configKey"\s*:\s*"([^"]+)"')


@pytest.fixture()
def fixture_entries() -> list[dict]:
    """Load the shared JSON fixture used by both vitest and this test."""
    assert FIXTURE_PATH.exists(), f"Fixture not found: {FIXTURE_PATH}"
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def registry_index() -> dict[str, object]:
    """Build a lookup {path: ConfigEntry} from the real backend registry."""
    return {entry.path: entry for entry in SCHEMA_REGISTRY}


@pytest.fixture()
def generated_config_keys() -> list[str]:
    """Extract all configKey values from settingsRegistry.gen.ts."""
    assert SETTINGS_REGISTRY_PATH.exists(), (
        f"settingsRegistry.gen.ts not found: {SETTINGS_REGISTRY_PATH}"
    )
    content = SETTINGS_REGISTRY_PATH.read_text(encoding="utf-8")
    return CONFIG_KEY_RE.findall(content)


class TestSettingRefSchemaFixtureDrift:
    """Every key referenced by frontend SettingRef must exist in the backend."""

    def test_fixture_keys_exist_in_registry(self, fixture_entries, registry_index):
        missing = [e["path"] for e in fixture_entries if e["path"] not in registry_index]
        assert not missing, (
            f"Frontend SettingRef fixture references keys missing from backend "
            f"SCHEMA_REGISTRY: {missing}"
        )

    def test_fixture_types_match_registry(self, fixture_entries, registry_index):
        mismatches = []
        for entry in fixture_entries:
            backend = registry_index.get(entry["path"])
            if backend is None:
                continue  # covered by test above
            if backend.type != entry["type"]:
                mismatches.append(
                    f"{entry['path']}: fixture={entry['type']} backend={backend.type}"
                )
        assert (
            not mismatches
        ), f"Type mismatch between fixture and backend SCHEMA_REGISTRY: {mismatches}"


class TestSettingsRegistryGenConfigKeyDrift:
    """Every configKey in settingsRegistry.gen.ts must exist in the backend."""

    def test_generated_config_keys_found(self, generated_config_keys):
        """At least one configKey exists in the generated file."""
        assert len(generated_config_keys) > 0, (
            "No configKey entries found in settingsRegistry.gen.ts"
        )

    def test_all_config_keys_exist_in_schema_registry(
        self, generated_config_keys, registry_index
    ):
        missing = [k for k in generated_config_keys if k not in registry_index]
        assert not missing, (
            f"settingsRegistry.gen.ts configKey(s) missing from backend "
            f"SCHEMA_REGISTRY — typo or backend rename? Missing: {missing}"
        )


def _scan_backend_source_for_literal(name: str) -> bool:
    """Recursively search src/kiro_crew/**/*.py for the literal env var name."""
    for dirpath, _dirs, files in os.walk(BACKEND_SRC_ROOT):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            content = fpath.read_text(encoding="utf-8")
            if name in content:
                return True
    return False


@pytest.fixture()
def env_vars_fixture() -> list[str]:
    """Load the shared JSON fixture of known env var names."""
    assert ENV_VARS_FIXTURE_PATH.exists(), (
        f"Env vars fixture not found: {ENV_VARS_FIXTURE_PATH}"
    )
    return json.loads(ENV_VARS_FIXTURE_PATH.read_text(encoding="utf-8"))


class TestSettingRefEnvVarsDrift:
    """Every env var in settingref-env-vars.json must exist in backend source."""

    def test_fixture_not_empty(self, env_vars_fixture):
        assert len(env_vars_fixture) > 0, "settingref-env-vars.json is empty"

    def test_all_env_vars_found_in_backend_source(self, env_vars_fixture):
        missing = [
            name for name in env_vars_fixture
            if not _scan_backend_source_for_literal(name)
        ]
        assert not missing, (
            f"settingref-env-vars.json lists env vars not found in "
            f"src/kiro_crew/**/*.py: {missing}. Either the var was removed "
            f"from the backend (remove from fixture) or it has a typo."
        )
