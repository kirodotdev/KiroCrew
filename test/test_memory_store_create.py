"""``kirocrew memory create-store``: the explicit step that makes a declared store usable.

A named V1 store declared in ``memory_stores`` has no directory until something
creates it, and use never creates it: ``require_memory_store`` refuses a missing
directory before ``ensure_memory_store_dir`` is reached, so a store whose directory
was deleted stays a visible loss instead of coming back empty. That rule stays. What
these tests pin is the one explicit verb that creates the directory, what it refuses,
and that ``kirocrew doctor`` names it for a missing store.
"""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from kiro_crew import cli_commands as cc
from kiro_crew import memory_stores as ms
from kiro_crew.config import loader as loader_mod
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.memory_stores import (
    DEFAULT_MEMORY_STORE,
    UnknownMemoryStore,
    memory_stores_root,
    require_memory_store,
)

pytestmark = pytest.mark.xdist_group("memory_store_create")


def _write_config(stores: dict, agents: dict | None = None) -> None:
    payload = {
        "memory_stores": {DEFAULT_MEMORY_STORE: {}, **stores},
        "default_memory_store": DEFAULT_MEMORY_STORE,
    }
    if agents:
        payload["agents"] = agents
    (config_dir() / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    loader_mod._invalidate_config_cache()


def _run(*argv: str) -> None:
    cc._memory_cmd(argparse.Namespace(mem_action="create-store", name=argv[0]))


def test_declared_store_refuses_until_created_then_resolves(capsys) -> None:
    _write_config({"team": {"owner_member": "", "memory_version": 1}})
    target = memory_stores_root() / "team"
    with pytest.raises(UnknownMemoryStore, match="missing"):
        require_memory_store("team")

    _run("team")

    assert target.is_dir()
    assert require_memory_store("team") == "team"
    assert "Created memory store 'team'" in capsys.readouterr().out


def test_create_is_idempotent_and_keeps_existing_content(capsys) -> None:
    _write_config({"team": {"owner_member": "", "memory_version": 1}})
    path, created = ms.create_declared_store("team")
    assert created
    (path / "preferences.md").write_text("- keep me\n", encoding="utf-8")

    path_again, created_again = ms.create_declared_store("team")
    assert path_again == path and not created_again
    assert (path / "preferences.md").read_text(encoding="utf-8") == "- keep me\n"

    _run("team")
    assert "already exists" in capsys.readouterr().out


def test_use_still_does_not_recreate_a_deleted_store() -> None:
    """The explicit step is the ONLY creator: use keeps refusing a missing directory."""
    _write_config({"team": {"owner_member": "", "memory_version": 1}})
    path, _ = ms.create_declared_store("team")
    path.rmdir()
    with pytest.raises(UnknownMemoryStore, match="missing"):
        require_memory_store("team")
    assert not path.exists()


@pytest.mark.parametrize(
    ("name", "stores", "match"),
    [
        ("ghost", {}, "not declared"),
        (DEFAULT_MEMORY_STORE, {}, "default store"),
        ("../x", {}, ""),
    ],
)
def test_create_refuses_what_it_must_not_make(name, stores, match) -> None:
    _write_config(stores)
    with pytest.raises(UnknownMemoryStore, match=match):
        ms.create_declared_store(name)
    assert not (memory_stores_root() / "ghost").exists()


def test_create_refuses_a_member_store() -> None:
    _write_config(
        {"m1": {"owner_member": "", "memory_version": 2, "owner_member_id": "mid-1"}},
        agents={"alice": {"memory_store": "m1", "member_id": "mid-1"}},
    )
    with pytest.raises(UnknownMemoryStore, match="member"):
        ms.create_declared_store("m1")
    assert not (memory_stores_root() / "m1").exists()


def test_cli_refusal_exits_one_with_one_line(capsys) -> None:
    _write_config({})
    with pytest.raises(SystemExit) as exit_info:
        _run("ghost")
    assert exit_info.value.code == 1
    assert "not declared" in capsys.readouterr().err


def test_parser_offers_the_verb(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["kirocrew", "memory", "create-store", "--help"])
    from kiro_crew.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    assert "declared" in capsys.readouterr().out


def test_doctor_names_the_create_verb_for_a_missing_store(capsys) -> None:
    from kiro_crew.doctor_checks.agents import _doctor_member_memory_bindings

    _write_config(
        {"team": {"owner_member": "", "memory_version": 1}},
        agents={"team": {"memory_store": "team"}},
    )
    issues: list[str] = []
    _doctor_member_memory_bindings(KiroCrewConfig.load(), issues)
    out = capsys.readouterr().out
    assert "unavailable" in out
    assert "kirocrew memory create-store team" in out
    assert issues


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_create_leaves_an_existing_directory_mode_alone(capsys) -> None:
    _write_config({"team": {"owner_member": "", "memory_version": 1}})
    path, _ = ms.create_declared_store("team")
    path.chmod(0o750)
    _run("team")
    assert stat.S_IMODE(path.stat().st_mode) == 0o750
    assert "nothing changed" in capsys.readouterr().out


def test_create_refuses_a_file_in_the_store_path(capsys) -> None:
    _write_config({"team": {"owner_member": "", "memory_version": 1}})
    memory_stores_root().mkdir(parents=True, exist_ok=True)
    blocker = memory_stores_root() / "team"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        _run("team")
    assert exit_info.value.code == 1
    assert "not a directory" in capsys.readouterr().err
    assert blocker.read_text(encoding="utf-8") == "x"


def test_doctor_names_no_create_verb_when_the_binding_has_another_defect(capsys) -> None:
    """A V1 store bound by a member that carries a member identity: creating fixes nothing."""
    from kiro_crew.doctor_checks.agents import _doctor_member_memory_bindings

    _write_config(
        {"team": {"owner_member": "", "memory_version": 1}},
        agents={"team": {"memory_store": "team", "member_id": "mid-9"}},
    )
    _doctor_member_memory_bindings(KiroCrewConfig.load(), [])
    out = capsys.readouterr().out
    assert "unavailable" in out
    assert "create-store" not in out
