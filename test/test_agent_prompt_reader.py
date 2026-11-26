"""The shared custom-agent prompt reader reads a spec the way KAS does.

``_load_agent_prompt`` feeds the ``[AGENT SYSTEM PROMPT]`` block on every
backend that does not load the spec's prompt itself. It takes kiro-cli's
``systemPrompt`` spelling, expands ``$VAR`` in a ``file://`` path, names a file
it cannot use, and refuses the paths KAS's prompt reader refuses.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_PI,
)
from kiro_crew.context_assembly.agent_spec import _load_agent_prompt


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    (home / ".kiro" / "agents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _spec(home: Path, body: dict) -> Path:
    path = home / ".kiro" / "agents" / "helper.json"
    path.write_text(json.dumps({"name": "helper", **body}), encoding="utf-8")
    return path


def test_system_prompt_spelling_is_read(home):
    spec = _spec(home, {"systemPrompt": "You are a helper."})
    assert _load_agent_prompt("helper", spec_path=spec) == "You are a helper."


def test_prompt_wins_over_system_prompt(home):
    spec = _spec(home, {"prompt": "From prompt.", "systemPrompt": "From alias."})
    assert _load_agent_prompt("helper", spec_path=spec) == "From prompt."


def test_file_prompt_expands_env_vars(home, monkeypatch):
    (home / "prompts").mkdir()
    (home / "prompts" / "helper.md").write_text("Persona from file.", encoding="utf-8")
    monkeypatch.setenv("HELPER_PROMPTS", str(home / "prompts"))
    for ref in ("file://$HOME/prompts/helper.md", "file://${HELPER_PROMPTS}/helper.md"):
        spec = _spec(home, {"prompt": ref})
        assert _load_agent_prompt("helper", spec_path=spec) == "Persona from file."


def test_system_prompt_file_is_read(home):
    (home / "p.md").write_text("Alias file persona.", encoding="utf-8")
    spec = _spec(home, {"systemPrompt": "file://$HOME/p.md"})
    assert _load_agent_prompt("helper", spec_path=spec) == "Alias file persona."


@pytest.mark.parametrize("content", [None, "", "   \n"])
def test_missing_or_empty_file_is_named(home, caplog, content):
    target = home / "gone.md"
    if content is not None:
        target.write_text(content, encoding="utf-8")
    spec = _spec(home, {"prompt": "file://$HOME/gone.md"})
    with caplog.at_level(logging.WARNING, logger="kiro_crew.context_assembly.agent_spec"):
        assert _load_agent_prompt("helper", spec_path=spec) == ""
    assert any("gone.md" in r.getMessage() and r.levelno == logging.WARNING for r in caplog.records)


def test_undecodable_file_is_named(home, caplog):
    (home / "bad.md").write_bytes(b"\xff\xfe\x00bad")
    spec = _spec(home, {"prompt": "file://$HOME/bad.md"})
    with caplog.at_level(logging.WARNING, logger="kiro_crew.context_assembly.agent_spec"):
        assert _load_agent_prompt("helper", spec_path=spec) == ""
    assert any("bad.md" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "ref",
    [
        "file://~/.config/gh/hosts.yml",
        "file://../../.config/gh/hosts.yml",
    ],
)
def test_path_kas_refuses_is_not_inlined(home, ref):
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text("SECRET-VALUE", encoding="utf-8")
    spec = _spec(home, {"prompt": ref})
    assert "SECRET-VALUE" not in _load_agent_prompt("helper", spec_path=spec)


@pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="needs /proc")
def test_pseudo_filesystem_is_not_inlined(home):
    spec = _spec(home, {"prompt": "file:///proc/self/status"})
    assert _load_agent_prompt("helper", spec_path=spec) == ""


@pytest.fixture
def checkout(home, tmp_path) -> Path:
    project = tmp_path / "checkout"
    (project / ".kiro" / "agents").mkdir(parents=True)
    (home / "notes.md").write_text("USER-NOTES", encoding="utf-8")
    return project


def _checkout_spec(project: Path, prompt: str) -> Path:
    path = project / ".kiro" / "agents" / "helper.json"
    path.write_text(json.dumps({"name": "helper", "prompt": prompt}), encoding="utf-8")
    return path


@pytest.mark.parametrize("ref", ["file://$HOME/notes.md", "file://~/notes.md", "ABSOLUTE"])
def test_checkout_spec_path_outside_its_project_is_refused(home, checkout, caplog, ref):
    if ref == "ABSOLUTE":
        ref = f"file://{home / 'notes.md'}"
    spec = _checkout_spec(checkout, ref)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.context_assembly.agent_spec"):
        assert _load_agent_prompt("helper", str(checkout), spec_path=spec) == ""
    assert any("outside" in r.getMessage() for r in caplog.records)


def test_checkout_spec_link_out_of_its_project_is_refused(home, checkout):
    from conftest import make_dir_link

    make_dir_link(checkout / "linked", home)
    spec = _checkout_spec(checkout, f"file://{checkout / 'linked' / 'notes.md'}")
    assert _load_agent_prompt("helper", str(checkout), spec_path=spec) == ""


def test_checkout_spec_absolute_path_inside_its_project_is_read(home, checkout, monkeypatch):
    (checkout / "persona").mkdir()
    (checkout / "persona" / "p.md").write_text("Checkout persona.", encoding="utf-8")
    monkeypatch.setenv("CHECKOUT", str(checkout))
    for ref in (f"file://{checkout / 'persona' / 'p.md'}", "file://$CHECKOUT/persona/p.md"):
        spec = _checkout_spec(checkout, ref)
        assert _load_agent_prompt("helper", str(checkout), spec_path=spec) == "Checkout persona."


def test_user_spec_absolute_path_keeps_its_rules(home, tmp_path):
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("User persona.", encoding="utf-8")
    spec = _spec(home, {"prompt": f"file://{elsewhere}"})
    assert _load_agent_prompt("helper", str(tmp_path / "checkout"), spec_path=spec) == (
        "User persona."
    )


def test_an_unread_file_is_reported_to_the_caller(home, caplog):
    """A collecting read logs nothing, so a new chat names the file in one WARNING, the block's."""
    missing = _spec(home, {"prompt": f"file://{home / 'gone.md'}"})
    unread: list[Path] = []
    with caplog.at_level(logging.WARNING, logger="kiro_crew.context"):
        assert _load_agent_prompt("helper", spec_path=missing, unread_out=unread) == ""
    assert unread == [home / "gone.md"]
    assert not [r for r in caplog.records if "gone.md" in r.getMessage()]
    (home / "here.md").write_text("Persona.", encoding="utf-8")
    readable = _spec(home, {"prompt": "file://$HOME/here.md"})
    unread = []
    assert _load_agent_prompt("helper", spec_path=readable, unread_out=unread) == "Persona."
    assert unread == []


def _session_start_notices(
    backend: str, monkeypatch, *, resumed: bool = False, execution_context=None
) -> list[str]:
    """The notice rows a new dashboard session for agent ``helper`` on *backend* shows."""
    import asyncio
    from types import SimpleNamespace

    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard import chat_runner

    rows: list[str] = []
    monkeypatch.setattr(
        chat_runner, "append_and_surface", lambda _st, _sl, role, text, *_a, **_k: rows.append(text)
    )
    turn = SimpleNamespace(
        kiro_agent="helper",
        execution_context=execution_context,
        _turn_project="",
        session_key="chat-1",
        resumed=resumed,
    )
    client = SimpleNamespace(capabilities=capabilities_for(backend), cwd="")
    slot = SimpleNamespace(agent="helper", mode="chat", _session_notice_marks=[])
    asyncio.run(chat_runner._notice_unread_prompt_file(SimpleNamespace(), slot, client, turn))
    return rows


def test_a_new_session_names_the_unread_prompt_file(home, monkeypatch):
    from kiro_crew import agent as agent_mod
    from kiro_crew.agent_sdk.agent_specs import prompt_unread_notice

    spec = {"name": "helper", "prompt": f"file://{home / 'gone.md'}"}
    agents_dir = agent_mod.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "helper.json").write_text(json.dumps(spec), "utf-8")
    assert _session_start_notices(ACP_BACKEND_CLAUDE, monkeypatch) == [
        prompt_unread_notice("helper", [home / "gone.md"])
    ]
    (home / "gone.md").write_text("Persona.", encoding="utf-8")
    assert _session_start_notices(ACP_BACKEND_CLAUDE, monkeypatch) == []


def test_a_resumed_session_gets_no_prompt_file_notice(home, monkeypatch):
    """The notice belongs to a new chat's start: a resumed session gets none."""
    from kiro_crew import agent as agent_mod

    spec = {"name": "helper", "prompt": f"file://{home / 'gone.md'}"}
    agents_dir = agent_mod.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "helper.json").write_text(json.dumps(spec), "utf-8")
    assert _session_start_notices(ACP_BACKEND_CLAUDE, monkeypatch, resumed=True) == []


def test_a_crewmate_picked_in_a_chat_gets_no_prompt_file_notice(home, monkeypatch):
    """Its prompt arrives through its essentials, so the block reads no file to miss."""
    from types import SimpleNamespace

    from kiro_crew import agent as agent_mod
    from kiro_crew import member_essential_context

    spec = {"name": "helper", "prompt": f"file://{home / 'gone.md'}"}
    agents_dir = agent_mod.kiro_agents_dir_path()
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "helper.json").write_text(json.dumps(spec), "utf-8")
    monkeypatch.setattr(
        member_essential_context,
        "member_context_identity",
        lambda member, *, member_is_id=True: (member, "helper") if member == "m-1" else ("", ""),
    )
    execution = SimpleNamespace(
        member_id="m-1",
        selection_kind="member",
        selection_name="mate",
        store=SimpleNamespace(legacy_name=None),
        memory_mode="persistent",
    )
    notices = _session_start_notices(ACP_BACKEND_CLAUDE, monkeypatch, execution_context=execution)
    assert notices == []


def test_the_notice_names_a_refused_location_as_a_cause(home):
    """The reader skips a file it may not read, so the row does not call it missing."""
    from kiro_crew.agent_sdk.agent_specs import prompt_unread_notice

    spec = _spec(home, {"prompt": "file://../../../outside.md"})
    (home.parent / "outside.md").write_text("Persona.", encoding="utf-8")
    unread: list[Path] = []
    assert _load_agent_prompt("helper", spec_path=spec, unread_out=unread) == ""
    assert unread == [Path("../../../outside.md")]
    assert "in a place Kiro Crew does not read prompts from" in prompt_unread_notice(
        "helper", unread
    )


@pytest.mark.parametrize(
    "backend, shown",
    [
        (ACP_BACKEND_CLAUDE, True),
        (ACP_BACKEND_PI, True),
        (ACP_BACKEND_KAS, False),
        (ACP_BACKEND_KIRO, False),
        ("no-such-backend", False),
    ],
)
def test_only_a_backend_given_the_prompt_in_the_block_names_the_file(backend, shown):
    """KAS names the file itself and kiro-cli reads it, so each keeps its one notice."""
    from types import SimpleNamespace

    from kiro_crew.agent_sdk.agent_specs import prompt_only_in_context_block
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    client = SimpleNamespace(capabilities=capabilities_for(backend))
    assert prompt_only_in_context_block(client) is shown
