"""``kirocrew memory import-text``: facts from a plain text file, saved on confirmation."""

from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

import pytest
from member_memory_helpers import forget_declared_stores, write_member_home

from kiro_crew import cli_commands as cc
from kiro_crew import cli_commands_memory, cli_memory_provider, memory_import_text, memory_stores
from kiro_crew.config.loader import config_dir
from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE, create_declared_store, resolve_store_path
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


def _rows(name: str = DEFAULT_MEMORY_STORE) -> dict[str, dict]:
    """Every live semantic row in the store *name*, read directly, by key."""
    store = VectorMemoryStore(db_path=resolve_store_path(name))
    store.init()
    try:
        return {str(row["key"]): row for row in store.get_all_semantic()}
    finally:
        store.close()


def test_parse_reads_one_fact_per_line_and_drops_the_rest() -> None:
    facts, skipped = memory_import_text.parse_facts(_NOTES)
    assert facts == ["Prefers short answers", "Works in Central time", "Drinks tea, not coffee"]
    assert skipped == 0


def test_a_rule_underline_or_table_divider_is_not_a_fact() -> None:
    text = "# Notes\n\n- likes strong tea\n***\n___\n* * *\n- - -\n=====\n|------|------|\n"
    assert memory_import_text.parse_facts(text) == (["likes strong tea"], 0)


def test_a_line_with_a_letter_or_digit_in_any_script_is_still_a_fact() -> None:
    text = "- \u559c\u6b22\u559d\u8336\n- 1984\n- caf\u00e9 au lait\n- a | b\n"
    facts, skipped = memory_import_text.parse_facts(text)
    assert facts == ["\u559c\u6b22\u559d\u8336", "1984", "caf\u00e9 au lait", "a | b"]
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


@pytest.mark.parametrize("head", ["", "\ufeff", "\ufeff- Prefers short answers\x1b]0;Texas\x07\n"])
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
    try:
        store.init()
        store.set_semantic("user.style", "prefers SHORT answers", 1.0, "user_explicit")
        store.set_semantic("user.timezone", "works in central TIME", 1.0, "user_explicit")
        assert store.delete_semantic("user.timezone", "user")
        cc._memory_cmd(_args(_write(tmp_path), yes=True))
        assert store.delete_semantic(memory_import_text.fact_key("Drinks tea, not coffee"), "user")
    finally:
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


@pytest.fixture
def team(monkeypatch: pytest.MonkeyPatch) -> str:
    """A declared V1 store with its directory and no database; ``lost`` has no directory."""
    stores = {name: {"owner_member": "", "memory_version": 1} for name in ("team", "lost")}
    config = {"memory_stores": {DEFAULT_MEMORY_STORE: {}, **stores}}
    (config_dir() / "config.json").write_text(json.dumps(config), encoding="utf-8")
    forget_declared_stores(monkeypatch)
    create_declared_store("team")
    return "team"


def test_without_store_the_default_store_is_used(tmp_path: Path, team: str) -> None:
    cc._memory_cmd(_args(_write(tmp_path), yes=True, store=None))
    assert len(_rows()) == 3
    assert not resolve_store_path(team).exists()


def test_a_named_store_receives_the_facts_and_the_default_store_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], team: str
) -> None:
    path = _write(tmp_path)
    cc._memory_cmd(_args(path, dry_run=True, store=team))
    assert "3 new fact(s)" in capsys.readouterr().out
    assert not resolve_store_path(team).exists()
    for new, known in ((3, 0), (0, 3)):
        cc._memory_cmd(_args(path, yes=True, store=team))
        out = capsys.readouterr().out
        assert f"{new} new fact(s)" in out and f"{known} already known" in out
    assert len(_rows(team)) == 3
    assert not resolve_store_path(DEFAULT_MEMORY_STORE).exists()


def test_the_forgotten_and_known_checks_read_the_named_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], team: str
) -> None:
    tea = memory_import_text.fact_key("Drinks tea, not coffee")
    for name, key, text in (
        (DEFAULT_MEMORY_STORE, "user.timezone", "works in central TIME"),
        (team, tea, "Drinks tea, not coffee"),
    ):
        store = VectorMemoryStore(db_path=resolve_store_path(name))
        try:
            store.init()
            store.set_semantic(key, text, 1.0, "user_explicit")
            assert name == DEFAULT_MEMORY_STORE or store.delete_semantic(tea, "user")
        finally:
            store.close()
    cc._memory_cmd(_args(_write(tmp_path), dry_run=True, store=team))
    out = capsys.readouterr().out
    assert "2 new fact(s)" in out and "0 already known, 1 previously forgotten" in out
    assert "Works in Central time" in out


def test_a_member_store_is_refused_like_memory_import(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    write_member_home(config_dir(), "alice")
    forget_declared_stores(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        cc._memory_cmd(_args(_write(tmp_path), yes=True, store="member-alice"))
    assert exc.value.code == 1
    assert "is a private V2 store" in capsys.readouterr().err
    assert not resolve_store_path(DEFAULT_MEMORY_STORE).exists()


@pytest.mark.parametrize(("store", "words"), [("nope", "unavailable"), ("lost", "is missing")])
def test_an_unusable_store_is_refused_and_nothing_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], team: str, store: str, words: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        cc._memory_cmd(_args(_write(tmp_path), yes=True, store=store))
    assert exc.value.code == 1
    assert words in capsys.readouterr().err
    assert not resolve_store_path(DEFAULT_MEMORY_STORE).exists()
    assert not resolve_store_path(team).exists()


def test_a_provider_that_replaces_the_named_store_refuses_the_verb(
    tmp_path: Path, team: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_memory_provider, "provider_replaces", lambda store=None: store == team)
    with pytest.raises(SystemExit) as exc:
        cc._memory_cmd(_args(_write(tmp_path), yes=True, store=team))
    assert exc.value.code == 2
    assert not resolve_store_path(team).exists()


def test_a_named_store_the_store_refuses_every_fact_is_left_without_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], team: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(VectorMemoryStore, "set_semantic_if_absent", lambda *_a, **_k: "rejected")
    with pytest.raises(SystemExit) as exc:
        cc._memory_cmd(_args(_write(tmp_path), yes=True, store=team))
    assert exc.value.code == 1
    assert "still has no memory.db" in capsys.readouterr().out
    assert not resolve_store_path(team).exists()


def test_a_preview_racing_an_import_that_removes_its_empty_database_leaves_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], team: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, errors, at_lock = _write(tmp_path), [], threading.Event()
    real_lock = memory_stores.memory_store_namespace_lock

    def preview() -> None:
        try:
            cc._memory_cmd(_args(path, dry_run=True, store=team))
        except BaseException as exc:  # noqa: BLE001 - kept for the assertion below
            errors.append(exc)

    racer = threading.Thread(target=preview)

    def watched_lock(*args: object, **kwargs: object) -> object:
        if threading.current_thread() is racer:
            at_lock.set()
        return real_lock(*args, **kwargs)

    def refuse(*_args: object, **_kwargs: object) -> str:
        # The --yes import holds the lock and has created the database: the preview
        # starts now, and the import goes on only once the preview waits on that lock.
        if not racer.is_alive():
            racer.start()
            assert at_lock.wait(30)
        return "rejected"

    monkeypatch.setattr(memory_stores, "memory_store_namespace_lock", watched_lock)
    monkeypatch.setattr(VectorMemoryStore, "set_semantic_if_absent", refuse)
    with pytest.raises(SystemExit):
        cc._memory_cmd(_args(path, yes=True, store=team))
    racer.join(30)
    assert not racer.is_alive() and errors == []
    assert "3 new fact(s)" in capsys.readouterr().out
    assert not resolve_store_path(team).exists()
    assert cli_commands_memory._admitted_store_path(team, None, may_create=False) is None
