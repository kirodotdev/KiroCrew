from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_byo import provider_catalog as catalog


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "src" / "kiro_byo" / "catalog" / "provider_catalog.v1.json"


def test_provider_catalog_is_versioned_external_data() -> None:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert len(data["presets"]) >= 10
    assert len(data["routing_rules"]) >= 20
    assert len(data["model_rules"]) >= 30
    # Every score triple carries an explanation/source field in data.
    assert all((row.get("source") or {}).get("note") for row in data["routing_rules"])

    source = (ROOT / "src" / "kiro_byo" / "provider_catalog.py").read_text(encoding="utf-8")
    assert 'if preset == "' not in source
    assert "_load_provider_catalog" in source


def test_bad_catalog_fails_early_with_precise_path(tmp_path: Path) -> None:
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    data["routing_rules"][0]["routing"]["speed"] = "fast"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(catalog.ProviderCatalogError) as excinfo:
        catalog._load_provider_catalog(path)
    message = str(excinfo.value)
    assert "provider_catalog.routing_rules[0].routing.speed" in message
    assert "expected numeric score" in message


def test_data_matcher_preserves_family_rules() -> None:
    assert catalog.official_model_profile("zhipu", "glm-5.2-202610")["reasoning"]["mode"] == "levels"
    assert catalog.official_model_profile("qwen", "qwen3-30b-a3b-thinking-2507")["reasoning"]["mode"] == "fixed"
    assert catalog.official_model_profile("qwen", "qwen3-32b")["reasoning"]["mode"] == "toggle"
    assert catalog.official_routing_profile("gemini", "gemini-3.8-flash-preview")["routing"]["tiers"] == ["fast", "balanced"]
