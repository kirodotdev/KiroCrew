"""Unit tests for scripts/generate_config_baseline.py.

Verifies the baseline generator produces valid JSON with the expected
structure and entry count, and that the committed snapshot matches it.

The last section goes past shape: a help string is what an operator acts on, so
where one asserts something the code decides, it is measured against the code
rather than reviewed. ``TestSpawnQueueWaitHelpMatchesTheDaemon`` is the first
such key.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

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
        # ``requiresRestart`` only for a field marked ``restart=True``. Both are
        # valid extra keys but never required.
        optional_keys = {"nullable", "requiresRestart"}

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

    def test_output_uses_lf_newlines(self, tmp_path: str) -> None:
        """Generated bytes are platform-neutral and match the committed LF snapshot."""
        generated = _generate_bytes(tmp_path)
        assert generated.endswith(b"\n")
        assert b"\r\n" not in generated

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
# Help text vs. the code it describes
# ---------------------------------------------------------------------------

_SPAWN_QUEUE_WAIT_PATH = "mcp_gateway.spawn_queue_wait_secs"

#: The owning spec -- the third copy of the claim, after the schema help and the
#: committed snapshot. All three are one sentence, so all three face the same
#: measurement.
_SPAWN_QUEUE_WAIT_DOC = os.path.join(_REPO_ROOT, "docs", "system-specs", "modules", "config.md")


def _schema_help(path: str) -> str:
    """The help an operator reads in the dashboard and the CLI."""
    for entry in SCHEMA_REGISTRY:
        if entry.path == path:
            return entry.help
    raise AssertionError(f"{path} missing from SCHEMA_REGISTRY")


def _baseline_help(path: str) -> str:
    """The same string as the committed snapshot ships it."""
    with open(_COMMITTED_BASELINE, encoding="utf-8") as handle:
        for entry in json.load(handle)["entries"]:
            if entry["path"] == path:
                return entry["help"]
    raise AssertionError(f"{path} missing from config-baseline.json")


def _doc_paragraph_about(doc_path: str, needle: str) -> str:
    """The one blank-line-delimited paragraph of *doc_path* that names *needle*.

    Scoping to a paragraph is what keeps a prose assertion from passing on a
    word borrowed from elsewhere in a 1700-line spec; more than one match is a
    failure rather than a join, because then the claim has two homes and this
    test would only be watching one of them.
    """
    paragraphs = [
        p for p in Path(doc_path).read_text(encoding="utf-8").split("\n\n") if needle in p
    ]
    assert len(paragraphs) == 1, (
        f"{doc_path} mentions {needle!r} in {len(paragraphs)} paragraphs; "
        "the claim about this key has to live in exactly one of them"
    )
    return paragraphs[0]


def _armed_queue_wait(asked: float, ceiling: float) -> float:
    """The wait the daemon really arms for a stub that asked for *asked*.

    Read through the daemon's own negotiation rather than re-implemented, so a
    change to the arithmetic reaches this test instead of being mirrored by it.
    Imported inside the call because ``gatewayd`` is a heavyweight module and
    every other test here needs none of it.
    """
    from kiro_crew.mcp_gateway import admission as adm
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway import host_budget as hb

    admission = adm.Admission(
        gate=adm.SpawnGate(1),
        budget=hb.HostBudget(hb.HostBudgetLimits(max_procs=0)),
        initialize_timeout_secs=1.0,
        spawn_queue_wait_secs=ceiling,
    )
    armed = gw._negotiated_wait_budget({"wait_budget_secs": asked}, admission)
    assert armed is not None, "a finite positive budget is a queue-aware stub"
    return armed


class TestSpawnQueueWaitHelpMatchesTheDaemon:
    """The operator-facing sentence about this key has to survive measurement.

    ``test_mcp_gateway_spawn_gate.py`` pins the arithmetic and
    ``test_committed_snapshot_matches_generator`` holds the schema and the
    snapshot byte-identical, and a help string can still assert the opposite of
    both and go green through every one of them: neither reads the prose. The
    key is a CEILING that the stub's own budget caps first, and the daemon
    subtracts ``_QUEUE_REFUSAL_MARGIN_SECS`` from what survives, so the refusal
    reaches a listening stub -- an operator told instead that raising the key
    buys a longer wait raises it, gets nothing, and concludes the queue is
    broken.
    """

    def test_the_key_is_a_ceiling_the_stub_budget_caps_first(self) -> None:
        """Measured, not read: raising the key past the stub's ask moves nothing."""
        from kiro_crew.config.sections import McpGatewayConfig
        from kiro_crew.mcp_gateway import stub as stub_mod

        asked = float(stub_mod._SPAWN_QUEUE_WAIT_BUDGET_SECS)
        shipped = float(McpGatewayConfig().spawn_queue_wait_secs)
        at_default = _armed_queue_wait(asked, shipped)

        assert at_default < asked, (
            f"the daemon arms {at_default}s against a stub that asked for "
            f"{asked}s: at or above the ask the stub gives up first and runs "
            "the fallback exec a capacity refusal exists to withhold"
        )
        for ceiling in (shipped * 2, shipped * 10, 1e9):
            assert _armed_queue_wait(asked, ceiling) == at_default, (
                f"raising the key to {ceiling}s moved the armed wait off "
                f"{at_default}s, so it is not a ceiling and the help text's "
                "'buys a queued stub no extra wait' is false"
            )
        # ...and lowering it still binds, which is the half that stays useful.
        assert _armed_queue_wait(asked, shipped / 2.0) < at_default

    def test_every_site_states_the_ceiling_and_never_its_inverse(self) -> None:
        """One sentence, three copies: schema help, snapshot, owning spec.

        Each site is read down to the text that talks about THIS key -- the two
        help copies are that text already, and the spec is scoped to the
        paragraph naming the key, so a check cannot be satisfied by a word the
        rest of the file happens to carry (`ceiling >= floor`, two sentences
        along, is exactly that trap).
        """
        sites = {
            "src/kiro_crew/config/sections.py": _schema_help(_SPAWN_QUEUE_WAIT_PATH),
            "config-baseline.json": _baseline_help(_SPAWN_QUEUE_WAIT_PATH),
            _SPAWN_QUEUE_WAIT_DOC: _doc_paragraph_about(
                _SPAWN_QUEUE_WAIT_DOC, "spawn_queue_wait_secs"
            ),
        }
        # The falsified direction, in the spellings that shipped it. A site
        # asserting the stub loses the race contradicts
        # ``test_the_key_is_a_ceiling_the_stub_budget_caps_first`` above.
        inverse = (
            "gives up before the queue",
            "needs that constant raised too",
            "stub gives up first",
        )
        for name, text in sites.items():
            flat = " ".join(text.split()).lower()
            for phrase in inverse:
                assert phrase not in flat, (
                    f"{name} still tells an operator {phrase!r}; the daemon's "
                    "wait is capped by what the stub asked for and then cut by "
                    "a margin, so the daemon is the side that gives up first"
                )
            assert "ceiling" in flat, (
                f"{name} does not say the key is a ceiling, which is the only "
                "thing that makes 'raising it buys no extra wait' follow"
            )
            assert (
                "no extra wait" in flat
            ), f"{name} does not tell an operator what raising the key yields"

    def test_the_number_the_prose_names_is_the_shipped_default(self) -> None:
        """Both help copies name a figure; it has to be the one that ships."""
        from kiro_crew.config.sections import McpGatewayConfig
        from kiro_crew.mcp_gateway import stub as stub_mod

        shipped = McpGatewayConfig().spawn_queue_wait_secs
        assert float(stub_mod._RECONNECT_TOTAL_BUDGET_SECS) == float(shipped)
        for name, text in (
            ("src/kiro_crew/config/sections.py", _schema_help(_SPAWN_QUEUE_WAIT_PATH)),
            ("config-baseline.json", _baseline_help(_SPAWN_QUEUE_WAIT_PATH)),
        ):
            assert f"above {shipped}" in text, (
                f"{name} names a threshold other than the shipped default "
                f"{shipped}; a stale figure sends an operator to the wrong knob"
            )


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
    can fall arbitrarily far behind and nothing goes red -- by whole blocks of
    entries, or by a single stale default.
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

    @pytest.mark.parametrize("autocrlf", ["true", "input", "false"])
    def test_checkout_preserves_generated_bytes(self, tmp_path: Path, autocrlf: str) -> None:
        """Git checkout must preserve the generator's LF bytes on every platform."""
        generated_bytes = _generate_bytes(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config-baseline.json").write_bytes(generated_bytes)
        (repo / ".gitattributes").write_bytes((Path(_REPO_ROOT) / ".gitattributes").read_bytes())
        (repo / "newline-control.txt").write_bytes(b"control\n")
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        # Exercise real Git conversion without inheriting host/repository overrides.
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        git = [
            "git",
            "-c",
            f"core.autocrlf={autocrlf}",
            "-c",
            f"core.attributesFile={os.devnull}",
        ]
        for args in (
            ["init", "--quiet"],
            ["add", "--", ".gitattributes", "config-baseline.json", "newline-control.txt"],
            [
                "checkout-index",
                f"--prefix={checkout.as_posix()}/",
                "--",
                "config-baseline.json",
                "newline-control.txt",
            ],
        ):
            subprocess.run(
                git + args, cwd=repo, env=env, capture_output=True, check=True, timeout=30
            )

        # The control proves autocrlf=true actually performs a CRLF checkout.
        expected_control = b"control\r\n" if autocrlf == "true" else b"control\n"
        assert (checkout / "newline-control.txt").read_bytes() == expected_control
        assert (checkout / "config-baseline.json").read_bytes() == generated_bytes

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
