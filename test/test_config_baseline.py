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
_COMMITTED_BASELINE = os.path.join(_REPO_ROOT, "config-baseline.json")


def _generate_bytes(tmp_path: str) -> bytes:
    """Run the baseline generator and return the raw bytes it wrote."""
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

    with open(out_path, "rb") as f:
        return f.read()


def _run_generator(tmp_path: str) -> dict:
    """Run the baseline generator and return the parsed JSON output."""
    return json.loads(_generate_bytes(tmp_path).decode("utf-8"))


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


# ---------------------------------------------------------------------------
# Committed-snapshot parity
# ---------------------------------------------------------------------------


def _drift_report(committed: dict, generated: dict) -> str:
    """Summarize how the committed snapshot differs from generator output.

    The raw diff of this file runs to four figures of lines, so report entry
    paths rather than content: a caller only needs to know the snapshot is
    behind, and the fix is always the same one command.
    """
    committed_entries = {e["path"]: e for e in committed.get("entries", [])}
    generated_entries = {e["path"]: e for e in generated.get("entries", [])}

    missing = sorted(set(generated_entries) - set(committed_entries))
    extra = sorted(set(committed_entries) - set(generated_entries))
    changed = sorted(
        path
        for path, entry in generated_entries.items()
        if path in committed_entries and committed_entries[path] != entry
    )

    def _sample(paths: list[str]) -> str:
        head = ", ".join(paths[:10])
        return f"{head}, ... (+{len(paths) - 10} more)" if len(paths) > 10 else head

    lines = [
        f"committed {len(committed_entries)} entries, generator produces "
        f"{len(generated_entries)}",
    ]
    if missing:
        lines.append(f"{len(missing)} missing from the snapshot: {_sample(missing)}")
    if extra:
        lines.append(f"{len(extra)} no longer in the schema: {_sample(extra)}")
    if changed:
        lines.append(f"{len(changed)} with drifted content: {_sample(changed)}")
    if not (missing or extra or changed):
        # Entry-for-entry identical, so the mismatch is serialization only
        # (key order, indentation, trailing newline).
        lines.append("entries are equivalent; the files differ in serialization only")
    return "\n  ".join(lines)


class TestCommittedBaselineParity:
    """The committed snapshot must match what the generator produces.

    Without this the snapshot is unchecked: every other test in this module
    runs the generator into a temp directory and compares it against the
    in-memory ``SCHEMA_REGISTRY``, so ``config-baseline.json`` at the repo root
    can fall arbitrarily far behind and nothing goes red. It did -- by 72
    entries (#3664), and by a stale default before that (#2862).
    """

    def test_committed_snapshot_matches_generator(self, tmp_path: str) -> None:
        """``config-baseline.json`` is byte-identical to generator output."""
        with open(_COMMITTED_BASELINE, "rb") as f:
            committed_bytes = f.read()
        generated_bytes = _generate_bytes(tmp_path)

        if committed_bytes == generated_bytes:
            return

        report = _drift_report(
            json.loads(committed_bytes.decode("utf-8")),
            json.loads(generated_bytes.decode("utf-8")),
        )
        pytest.fail(
            "config-baseline.json is out of date with the config schema.\n"
            f"  {report}\n"
            "Regenerate and commit it in the same change that touched the schema:\n"
            "  python scripts/generate_config_baseline.py"
        )

    def test_drift_report_names_each_kind_of_difference(self) -> None:
        """The failure message distinguishes added, removed and changed entries."""
        committed = {
            "entries": [{"path": "kept"}, {"path": "gone"}, {"path": "moved", "type": "a"}]
        }
        generated = {"entries": [{"path": "kept"}, {"path": "new"}, {"path": "moved", "type": "b"}]}

        report = _drift_report(committed, generated)

        assert "committed 3 entries, generator produces 3" in report
        assert "1 missing from the snapshot: new" in report
        assert "1 no longer in the schema: gone" in report
        assert "1 with drifted content: moved" in report

    def test_drift_report_reports_serialization_only_mismatch(self) -> None:
        """Byte parity is stricter than entry parity, and says so when that is why."""
        entries = {"entries": [{"path": "kept", "type": "string"}]}

        report = _drift_report(entries, {"entries": [dict(entries["entries"][0])]})

        assert "serialization only" in report
