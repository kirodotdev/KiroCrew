"""``kirocrew memory import-text``: facts from a plain text file, saved on confirmation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import cli_commands_memory, memory_import_text
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, resolve_store_path
from kiro_crew.vector_memory import VectorMemoryStore

_NOTES = """# What I know about you

Preferences:
- Prefers short answers
* Works in Central time
1. Drinks tea, not coffee
2) Prefers short answers

---
"""


def _args(path: Path, **kw: object) -> argparse.Namespace:
    values = {"mem_action": "import-text", "file": str(path), "yes": False, "dry_run": False}
    return argparse.Namespace(**{**values, **kw})


def _write(tmp_path: Path, text: str = _NOTES) -> Path:
    path = tmp_path / "about-me.md"
    path.write_text(text, encoding="utf-8")
    return path


def _rows() -> dict[str, dict]:
    """Every live semantic row in the default store, read directly, by key."""
    store = VectorMemoryStore(db_path=resolve_store_path(DEFAULT_MEMORY_STORE))
    store.init()
    try:
        return {str(row["key"]): row for row in store.get_all_semantic()}
    finally:
        store.close()


def test_parse_reads_one_fact_per_line_and_drops_the_rest() -> None:
    facts, skipped = memory_import_text.parse_facts(_NOTES)
    assert facts == ["Prefers short answers", "Works in Central time", "Drinks tea, not coffee"]
    assert skipped == 0


def test_a_long_file_is_offered_in_runs_after_known_facts_are_left_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    limit, long = memory_import_text.MAX_FACTS, "x" * (memory_import_text.MAX_FACT_CHARS + 1)
    path = _write(tmp_path, "\n".join([long] + [f"fact number {n}" for n in range(limit + 1)]))
    for new, known in ((limit, 0), (1, limit)):
        cc._memory_cmd(_args(path, yes=True))
        out = capsys.readouterr().out
        assert f"{new} new fact(s)" in out and f"{known} already known" in out
        assert f"1 skipped.{' 1 more over the 200 limit; run again.' * (new == limit)}\n" in out
    assert len(_rows()) == limit + 1


def test_a_dry_run_or_no_terminal_lists_facts_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_commands_memory, "_interactive_terminal", lambda: False)
    for dry_run, words in ((True, "1. Prefers short answers"), (False, "re-run with --yes")):
        cc._memory_cmd(_args(_write(tmp_path), dry_run=dry_run))
        out = capsys.readouterr().out
        assert "3 new fact(s)" in out and words in out
    assert not resolve_store_path(DEFAULT_MEMORY_STORE).exists()


@pytest.mark.parametrize("head", ["", "\ufeff", "\ufeff- Prefers short answers\n"])
def test_yes_saves_every_fact_as_imported_and_a_rerun_adds_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], head: str
) -> None:
    path = _write(tmp_path, head + _NOTES)
    cc._memory_cmd(_args(path, yes=True))
    assert "Saved 3 fact(s)" in capsys.readouterr().out
    rows = _rows()
    assert len(rows) == 3
    assert {row["source"] for row in rows.values()} == {memory_import_text.SOURCE}
    key = memory_import_text.fact_key("prefers  SHORT answers")
    assert json.loads(rows[key]["value_json"]) == "Prefers short answers"
    cc._memory_cmd(_args(path, yes=True))
    out = capsys.readouterr().out
    assert "0 new fact(s)" in out and "3 already known" in out
    assert len(_rows()) == 3


def test_a_fact_the_store_holds_or_the_owner_forgot_is_not_offered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = VectorMemoryStore(db_path=resolve_store_path(DEFAULT_MEMORY_STORE))
    store.init()
    store.set_semantic("user.style", "prefers SHORT answers", 1.0, "user_explicit")
    store.set_semantic("user.timezone", "works in central TIME", 1.0, "user_explicit")
    assert store.delete_semantic("user.timezone", "user")
    cc._memory_cmd(_args(_write(tmp_path), yes=True))
    assert store.delete_semantic(memory_import_text.fact_key("Drinks tea, not coffee"), "user")
    store.close()
    capsys.readouterr()
    cc._memory_cmd(_args(_write(tmp_path, _NOTES + "- Uses a standing desk\n"), yes=True))
    out = capsys.readouterr().out
    assert "1 new fact(s)" in out and "1 already known, 2 previously forgotten" in out
    assert "Central time" not in out and "Saved 1 fact(s); 0 already present." in out
    assert len(_rows()) == 2


def test_only_the_numbers_the_owner_confirms_are_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_commands_memory, "_interactive_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2, 2")
    cc._memory_cmd(_args(_write(tmp_path)))
    assert [json.loads(row["value_json"]) for row in _rows().values()] == ["Works in Central time"]


def test_an_answer_outside_the_list_saves_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_commands_memory, "_interactive_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "1 9")
    with pytest.raises(SystemExit) as exc:
        cc._memory_cmd(_args(_write(tmp_path)))
    assert exc.value.code == 1
    assert "nothing saved" in capsys.readouterr().err
    assert not resolve_store_path(DEFAULT_MEMORY_STORE).exists()


def test_a_file_that_cannot_be_used_exits_one_without_a_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    big = _write(tmp_path, "a fact\n" * memory_import_text.MAX_FILE_BYTES)
    empty = tmp_path / "empty.txt"
    empty.write_text("# only a heading\n", encoding="utf-8")
    for path, words in (
        (tmp_path / "gone.md", "could not be read"),
        (big, "larger than"),
        (empty, "no facts"),
    ):
        with pytest.raises(SystemExit) as exc:
            cc._memory_cmd(_args(path, yes=True))
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert words in err and str(tmp_path) not in err
    assert not resolve_store_path(DEFAULT_MEMORY_STORE).exists()


def test_the_store_refusal_of_a_fact_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(VectorMemoryStore, "set_semantic_if_absent", lambda *_a, **_k: "rejected")
    with pytest.raises(SystemExit) as exc:
        cc._memory_cmd(_args(_write(tmp_path), yes=True))
    assert exc.value.code == 1
    assert "refused 3 fact(s)" in capsys.readouterr().err
