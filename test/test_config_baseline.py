"""Unit tests for scripts/generate_config_baseline.py.

Verifies the baseline generator produces valid JSON with the expected
structure and entry count.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from kiro_crew.config.schema import SCHEMA_REGISTRY

# Each test spawns a real child interpreter (subprocess.run([sys.executable, ...]));
# pin the module to a dedicated xdist worker so concurrent cold-starts under -n auto
# don't starve each other / blow the 30s timeout. Requires --dist loadgroup.
pytestmark = pytest.mark.xdist_group(name="subprocess_spawn")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "generate_config_baseline.py")


def _run_generator(tmp_path: str) -> dict:
    """Run the baseline generator and return the parsed JSON output."""
    env = os.environ.copy()
    out_path = os.path.join(str(tmp_path), "config-baseline.json")
    env["KIROCREW_BASELINE_OUTPUT"] = out_path
    result = subprocess.run(
        [sys.executable, _SCRIPT_PATH],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"Script failed:\n{result.stderr}"
    assert os.path.exists(out_path), "config-baseline.json was not created"

    with open(out_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestBaselineGenerator:
    """Unit tests for the baseline generator script."""

    def test_output_has_required_top_level_keys(self, tmp_path: str) -> None:
        """Output JSON has generatedBy and entries keys."""
        data = _run_generator(tmp_path)

        assert "generatedBy" in data
        assert "entries" in data

        assert data["generatedBy"] == "scripts/generate_config_baseline.py"
        assert "generatedAt" not in data  # removed to avoid merge conflicts
        assert isinstance(data["entries"], list)

    def test_entries_count_matches_registry(self, tmp_path: str) -> None:
        """entries array contains expected number of ConfigEntry dicts."""
        data = _run_generator(tmp_path)

        assert len(data["entries"]) == len(
            SCHEMA_REGISTRY
        ), f"Expected {len(SCHEMA_REGISTRY)} entries, got {len(data['entries'])}"

    def test_entries_have_expected_fields(self, tmp_path: str) -> None:
        """Each entry dict has all expected ConfigEntry fields."""
        data = _run_generator(tmp_path)

        required_keys = {
            "path",
            "kind",
            "type",
            "required",
            "deprecated",
            "sensitive",
            "tags",
            "label",
            "help",
            "hasChildren",
            "enumValues",
            "defaultValue",
        }
        # ``nullable`` is only emitted when True (Optional[X] dict/list values);
        # it's a valid extra key but never required.
        optional_keys = {"nullable"}

        for entry_dict in data["entries"]:
            keys = set(entry_dict.keys())
            assert required_keys <= keys, (
                f"Entry {entry_dict.get('path', '?')!r} missing keys: " f"{required_keys - keys}"
            )
            unexpected = keys - required_keys - optional_keys
            assert not unexpected, (
                f"Entry {entry_dict.get('path', '?')!r} has unexpected keys: " f"{unexpected}"
            )

    def test_script_is_runnable_and_produces_valid_json(self, tmp_path: str) -> None:
        """Script is executable via python and produces valid JSON."""
        out_path = os.path.join(str(tmp_path), "config-baseline.json")
        env = os.environ.copy()
        env["KIROCREW_BASELINE_OUTPUT"] = out_path
        result = subprocess.run(
            [sys.executable, _SCRIPT_PATH],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, f"Script failed:\n{result.stderr}"

        with open(out_path, encoding="utf-8") as f:
            data = json.load(f)  # Validates it's valid JSON

        assert isinstance(data, dict)
        assert "entries" in data

    def test_generated_at_removed(self, tmp_path: str) -> None:
        """generatedAt was removed to prevent merge conflicts."""
        data = _run_generator(tmp_path)
        assert "generatedAt" not in data

    def test_slack_reactions_value_entry_is_nullable(self, tmp_path: str) -> None:
        """``slack.reactions.*`` values accept null as a suppression sentinel
        and the baseline must advertise that to downstream UI/baseline consumers.
        """
        data = _run_generator(tmp_path)
        entries_by_path = {e["path"]: e for e in data["entries"]}
        entry = entries_by_path.get("slack.reactions.*")
        assert entry is not None, "slack.reactions.* entry missing from baseline"
        assert entry.get("nullable") is True, (
            "slack.reactions.* must be marked nullable (Optional[str] values); " f"got {entry!r}"
        )
        # And the base type is still 'string' — we didn't break the scalar type contract.
        assert entry["type"] == "string"

    def test_otlp_endpoint_is_sensitive(self, tmp_path: str) -> None:
        """Credential-bearing collector URLs are masked by schema consumers."""
        data = _run_generator(tmp_path)
        entries_by_path = {e["path"]: e for e in data["entries"]}
        entry = entries_by_path.get("telemetry.otlp_endpoint")
        assert entry is not None, "telemetry.otlp_endpoint entry missing"
        assert entry["sensitive"] is True
