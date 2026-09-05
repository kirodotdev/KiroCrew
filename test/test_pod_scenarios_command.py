"""Tests for ``kirocrew pod scenarios`` discovery."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import kiro_crew
from kiro_crew import seed as seed_mod
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod.config import PodConfig

_SRC = str(Path(kiro_crew.__file__).resolve().parents[1])
_REPO = Path(_SRC).parent

# Fixtures every checkout ships. Assertions against the real registry name these
# rather than pinning the whole list: the registry is a directory scan, so adding
# a fixture is a supported operation and must not redden a test about how this
# command FORMATS its output. Formatting itself is pinned on a stubbed registry
# below, where the input is fixed by the test instead of by the packaged tree.
_BASELINE_FIXTURES = frozenset({"empty", "minimal", "rich"})


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the real parser outside the checkout with isolated host state."""
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "os-home"),
            "USERPROFILE": str(tmp_path / "os-home"),
            "KIROCREW_HOME": str(tmp_path / "crew-home"),
            "KIROCREW_PROJECT_DIR": str(_REPO),
            "PYTHONPATH": _SRC,
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "kiro_crew", "pod", "scenarios", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=30,
        check=False,
    )


def _human_table(stdout: str) -> tuple[str, dict[str, str], str]:
    """Split human output into its header, its name -> row map, and its hint line.

    The column width is derived from the longest NAME in the registry, so the
    row offsets and the padding both move when a fixture is added. Parsing by
    name keeps the assertions about content rather than about position.
    """
    lines = stdout.splitlines()
    header, body = lines[0], lines[1:]
    # The hint is preceded by a blank separator line.
    blank = body.index("")
    rows = {line.split()[0]: line for line in body[:blank]}
    return header, rows, body[-1]


def test_human_output_lists_sorted_names_and_first_sentences(tmp_path: Path) -> None:
    result = _run_cli(tmp_path)

    assert result.returncode == 0, result.stderr
    header, rows, hint = _human_table(result.stdout)

    assert header.startswith("SCENARIO")
    assert header.endswith("DESCRIPTION")
    assert _BASELINE_FIXTURES <= set(rows)
    assert list(rows) == sorted(rows)
    # The column carries the last complete sentence that fits, not the whole
    # scalar — which for every shipped fixture is its first sentence.
    assert rows["empty"].split(maxsplit=1)[1].strip() == (
        "A KIROCREW_HOME with nothing in it but this manifest."
    )
    # The hint names whichever fixture sorts first, so it moves with the registry.
    assert hint == ("seed one with: kirocrew pod up <worktree> --seed " + next(iter(rows)))


def test_json_output_contains_complete_literal_descriptions(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "--json")

    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    names = [row["name"] for row in rows]

    assert _BASELINE_FIXTURES <= set(names)
    assert names == sorted(names)
    assert all(set(row) == {"name", "description"} for row in rows)
    assert all(row["description"] for row in rows)

    described = {row["name"]: row["description"] for row in rows}
    # JSON keeps the full literal scalar, including paragraph and list structure.
    assert described["empty"].startswith(
        "A KIROCREW_HOME with nothing in it but this manifest. The gateway writes its"
    )
    assert "\n" in described["empty"]
    assert "\n\nUse ``rich``" in described["minimal"]
    assert "\nShipped sessions:\n  - dashboard_starter" in described["rich"]
    assert " ".join(described["rich"].split()).endswith(
        "SQLite binaries would require a reproducible builder script to be diffable."
    )


def test_output_absorbs_an_added_fixture_without_moving_known_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fixture added to the registry must not disturb the existing rows.

    Pins the property the real-registry tests above rely on, on an input this
    test controls: the added name sorts ahead of every baseline fixture AND is
    longer than the ``SCENARIO`` header, so it moves both the row order and the
    column padding at once.
    """
    registry = ["apps-installed", *sorted(_BASELINE_FIXTURES)]
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: registry)
    monkeypatch.setattr(seed_mod, "fixture_summary", lambda name: f"about {name}.")

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))
    header, rows, hint = _human_table(capsys.readouterr().out)

    assert list(rows) == registry
    assert header == "SCENARIO        DESCRIPTION"
    assert rows["empty"] == "empty           about empty."
    # The hint follows the registry instead of naming a hard-coded fixture.
    assert hint == "seed one with: kirocrew pod up <worktree> --seed apps-installed"


def test_human_output_keeps_a_second_sentence_that_fits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row with room for both sentences shows both.

    Shortening is delegated to ``truncate_summary``, so nothing discards text
    the column had room for.
    """
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: ["alpha"])
    monkeypatch.setattr(
        seed_mod,
        "fixture_summary",
        lambda name: "First sentence. Second sentence with implementation detail.",
    )

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))

    assert capsys.readouterr().out.splitlines()[1] == (
        "alpha     First sentence. Second sentence with implementation detail."
    )


def test_human_output_cuts_at_a_sentence_end_when_the_next_will_not_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cut lands on a sentence boundary, not mid-word — the shipped case.

    Every packaged fixture is this shape: a short first sentence followed by
    more prose than the column can hold. ``truncate_summary`` alone must produce
    the clean single-sentence row here, which is what makes a separate
    first-sentence pass redundant.
    """
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: ["alpha"])
    monkeypatch.setattr(
        seed_mod,
        "fixture_summary",
        lambda name: "First sentence. " + "padding " * 20 + "trailing.",
    )
    monkeypatch.setattr(pod_cli, "_SCENARIOS_TABLE_WIDTH", 40)

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))

    assert capsys.readouterr().out.splitlines()[1] == "alpha     First sentence."


def test_human_output_truncates_only_at_a_word_boundary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: ["alpha"])
    monkeypatch.setattr(
        seed_mod,
        "fixture_summary",
        lambda name: "alpha bravo charlie delta echo foxtrot.",
    )
    monkeypatch.setattr(pod_cli, "_SCENARIOS_TABLE_WIDTH", 30)

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))

    assert capsys.readouterr().out.splitlines()[1] == "alpha     alpha bravo charlie…"


def test_human_output_reuses_shared_long_token_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: ["alpha"])
    monkeypatch.setattr(
        seed_mod,
        "fixture_summary",
        lambda name: "Supercalifragilisticexpialidocious",
    )
    monkeypatch.setattr(pod_cli, "_SCENARIOS_TABLE_WIDTH", 20)

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))

    assert capsys.readouterr().out.splitlines()[1] == "alpha     Supercali…"


def test_unknown_flag_is_rejected_by_argparse(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "--not-a-scenarios-flag")

    assert result.returncode == 2
    assert "unrecognized arguments: --not-a-scenarios-flag" in result.stderr
    assert result.stdout == ""


def test_handler_sorts_even_if_registry_order_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: ["zeta", "alpha"])
    monkeypatch.setattr(
        seed_mod,
        "fixture_summary",
        lambda name: {"alpha": "first", "zeta": "last"}[name],
        raising=False,
    )

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=True))

    assert json.loads(capsys.readouterr().out) == [
        {"name": "alpha", "description": "first"},
        {"name": "zeta", "description": "last"},
    ]


def test_empty_registry_has_explicit_human_and_json_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(seed_mod, "available_fixtures", lambda: [])

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=False))
    assert capsys.readouterr().out == (
        "no seed scenarios found (the packaged fixtures tree is missing)\n"
    )

    pod_cli._scenarios(PodConfig.load(), argparse.Namespace(json=True))
    assert json.loads(capsys.readouterr().out) == []


def test_description_parser_preserves_a_literal_multiline_scalar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / seed_mod.FIXTURE_MANIFEST).write_text(
        "description: |\n"
        "  First   line\n"
        "    continuation words.\n"
        "\n"
        "  Second\tparagraph.\n"
        "next-key: value\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(seed_mod, "_resolve_fixture", lambda name: fixture)

    assert seed_mod.fixture_summary("custom") == (
        "First   line\n  continuation words.\n\nSecond\tparagraph."
    )


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ("description: plain value\n", "plain value"),
        ('description: "quoted value"\n', '"quoted value"'),
        ("description: >\n  folded   value\n  continues\n", "folded value continues"),
    ],
)
def test_description_parser_keeps_supported_scalar_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: str,
    expected: str,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / seed_mod.FIXTURE_MANIFEST).write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(seed_mod, "_resolve_fixture", lambda name: fixture)

    assert seed_mod.fixture_summary("custom") == expected


@pytest.mark.parametrize(
    "manifest",
    [
        "fixture-name: custom\n",
        "description:\n",
        "description: |\nnext-key: value\n",
    ],
)
def test_missing_empty_or_malformed_description_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: str,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / seed_mod.FIXTURE_MANIFEST).write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(seed_mod, "_resolve_fixture", lambda name: fixture)

    assert seed_mod.fixture_summary("custom") == ""


def test_description_parser_needs_no_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)

    # This used to assert the truncated first physical line. The command is a
    # discovery API, so preserving only that line is data loss.
    summary = seed_mod.fixture_summary("minimal")
    assert "\n\nUse ``rich``" in summary
    assert " ".join(summary.split()).endswith(
        "SQLite binaries would require a reproducible builder script to be diffable."
    )
