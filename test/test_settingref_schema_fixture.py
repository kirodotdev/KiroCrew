"""Cross-layer drift guard for SettingRef schema keys and environment names.

Every generated configKey must exist in the backend schema. The Decisions
control additionally pins its literal key, TypeScript constant and point name.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from kiro_crew.config.schema import SCHEMA_REGISTRY

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = ROOT / "website" / "src" / "test" / "fixtures" / "settingref-schema.json"
ENV_VARS_FIXTURE_PATH = ROOT / "website" / "src" / "test" / "fixtures" / "settingref-env-vars.json"
SETTINGS_REGISTRY_PATH = (
    ROOT / "website" / "src" / "components" / "commandPalette" / "settingsRegistry.gen.ts"
)
BACKEND_SRC_ROOT = ROOT / "src" / "kiro_crew"
CONFIG_KEY_RE = re.compile(r'"configKey"\s*:\s*"([^"]+)"')
DECISIONS_READER_PATH = ROOT / "website" / "src" / "pages" / "settings" / "decisionsPreview.ts"
FEATURE_PREVIEWS_PATH = (
    ROOT / "website" / "src" / "pages" / "settings" / "FeaturePreviewsSection.tsx"
)
TS_CONST_RE = r"export const {name} = '([^']+)'"


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
    assert (
        SETTINGS_REGISTRY_PATH.exists()
    ), f"settingsRegistry.gen.ts not found: {SETTINGS_REGISTRY_PATH}"
    return CONFIG_KEY_RE.findall(SETTINGS_REGISTRY_PATH.read_text(encoding="utf-8"))


class TestSettingRefSchemaFixtureDrift:
    """Every key referenced by frontend SettingRef must exist in the backend."""

    def test_fixture_keys_exist_in_registry(self, fixture_entries, registry_index):
        missing = [
            entry["path"] for entry in fixture_entries if entry["path"] not in registry_index
        ]
        assert not missing, (
            "Frontend SettingRef fixture references keys missing from backend "
            f"SCHEMA_REGISTRY: {missing}"
        )

    def test_fixture_types_match_registry(self, fixture_entries, registry_index):
        mismatches = []
        for entry in fixture_entries:
            backend = registry_index.get(entry["path"])
            if backend is None:
                continue  # covered by the key-presence test
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
        assert generated_config_keys, "No configKey entries found in settingsRegistry.gen.ts"

    def test_all_config_keys_exist_in_schema_registry(self, generated_config_keys, registry_index):
        missing = [key for key in generated_config_keys if key not in registry_index]
        assert not missing, (
            "settingsRegistry.gen.ts configKey(s) missing from backend "
            f"SCHEMA_REGISTRY — typo or backend rename? Missing: {missing}"
        )


def _ts_const(source: str, name: str) -> str:
    """Read one exported TypeScript string literal."""
    match = re.search(TS_CONST_RE.format(name=name), source)
    assert match, f"{name} not found as a string constant in decisionsPreview.ts"
    return match.group(1)


class TestDecisionsSettingCrossLayer:
    """Keep the card, its one config path and the backend schema in agreement.

    Consent is NOT a config path: it is the keystone ``decisions_consent.json``
    behind ``/api/decisions/consent``. So the schema must carry the bucket and
    NOT an ``enabled``, the card must write the consent route and carry no
    ``configKey``, and the point name must be one the backend ships.
    """

    def test_the_bucket_exists_in_schema_registry_and_enabled_does_not(self, registry_index):
        bucket = registry_index.get("decisions.bucket")
        assert bucket is not None, "decisions.bucket missing from SCHEMA_REGISTRY"
        assert bucket.type == "integer", f"decisions.bucket is {bucket.type}"
        assert "decisions.enabled" not in registry_index, (
            "decisions.enabled is back in SCHEMA_REGISTRY: consent lives on the "
            "keystone decisions_consent.json, never in the agent-writable config.json"
        )

    def test_frontend_constant_matches_the_schema_path(self, registry_index):
        source = DECISIONS_READER_PATH.read_text(encoding="utf-8")
        value = _ts_const(source, "DECISIONS_BUCKET_PATH")
        assert value == "decisions.bucket"
        assert value in registry_index, "DECISIONS_BUCKET_PATH names no SCHEMA_REGISTRY path"
        assert "DECISIONS_ENABLED_PATH" not in source, (
            "the reader spells a config path for consent again; the switch writes the "
            "keystone through /api/decisions/consent"
        )

    def test_the_toggle_writes_the_consent_route_and_carries_no_config_key(self):
        card = FEATURE_PREVIEWS_PATH.read_text(encoding="utf-8")
        assert "api.saveDecisionsConsent(" in card
        assert (
            'configKey="decisions.enabled"' not in card
        ), "a configKey naming decisions.enabled would name a path nothing reads"

    def test_the_consent_routes_are_registered(self):
        from kiro_crew.dashboard import handlers

        assert callable(handlers.api_decisions_consent_get)
        assert callable(handlers.api_decisions_consent_put)

    def test_the_frontend_and_backend_name_the_same_points(self):
        """Both directions, because each one breaks a different surface.

        A point the FRONTEND names but the backend does not ship is a reader
        waiting for a record nothing writes. A point the BACKEND ships but the
        frontend cannot name draws nothing at all -- ``decisionRecord.ts``
        dispatches on the record's own ``point`` and returns null for one it does
        not know, so that direction fails silently and looks exactly like a healthy
        release stamping no record.

        The frontend side is DISCOVERED, not listed: every ``DECISIONS_*_POINT``
        constant the reader exports. So shipping a third point is one constant in
        ``decisionsPreview.ts`` and no edit here -- which also keeps two branches
        each adding a point from colliding on this file.

        The ANNOTATING point is the one name held here rather than discovered: it
        draws a badge on the tool card instead of a line in the strip, so the
        reader exports no constant for it. The backend-minus-frontend difference
        therefore has to come out as exactly that one name, never as a point whose
        record no surface draws.

        Discovery is asserted non-empty and anchored on the skills point, because a
        scan that silently matched nothing would pass while measuring nothing.
        """
        from kiro_crew.decisions.gate import DECISION_POINT_NAMES
        from kiro_crew.decisions.points.tool_risk import POINT as ANNOTATION_POINT

        source = DECISIONS_READER_PATH.read_text(encoding="utf-8")
        names = re.findall(r"export const (DECISIONS_\w+_POINT)\b", source)
        assert names, (
            "no DECISIONS_*_POINT constant found in decisionsPreview.ts -- the "
            "discovery pattern no longer matches how the reader spells a point, so "
            "this assertion would compare nothing"
        )
        frontend = {_ts_const(source, name) for name in names}
        assert (
            "skills.select" in frontend
        ), f"the reader stopped naming the skills point (found {sorted(frontend)})"
        backend = set(DECISION_POINT_NAMES)
        assert frontend <= backend, (
            f"the reader names {sorted(frontend - backend)} which the backend does "
            "not ship: a strip line waiting for a record nothing writes"
        )
        assert backend - frontend == {ANNOTATION_POINT}, (
            f"the backend ships {sorted(backend - frontend - {ANNOTATION_POINT})} with "
            f"no reader constant; only the annotating point {ANNOTATION_POINT!r} draws "
            "outside the strip, and a point is an egress path -- name it in "
            "decisionsPreview.ts so some surface shows what was sent"
        )


def _scan_backend_source_for_literal(name: str) -> bool:
    """Search backend Python files for a literal environment-variable name."""
    for dirpath, _dirs, files in os.walk(BACKEND_SRC_ROOT):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            if name in (Path(dirpath) / fname).read_text(encoding="utf-8"):
                return True
    return False


@pytest.fixture()
def env_vars_fixture() -> list[str]:
    assert ENV_VARS_FIXTURE_PATH.exists(), f"Env vars fixture not found: {ENV_VARS_FIXTURE_PATH}"
    return json.loads(ENV_VARS_FIXTURE_PATH.read_text(encoding="utf-8"))


class TestSettingRefEnvVarsDrift:
    """Every listed environment name must exist in backend source."""

    def test_fixture_not_empty(self, env_vars_fixture):
        assert env_vars_fixture, "settingref-env-vars.json is empty"

    def test_all_env_vars_found_in_backend_source(self, env_vars_fixture):
        missing = [name for name in env_vars_fixture if not _scan_backend_source_for_literal(name)]
        assert not missing, (
            "settingref-env-vars.json lists env vars not found in "
            f"src/kiro_crew/**/*.py: {missing}. Remove stale names or fix their spelling."
        )
