"""Every crewmate prompt piece lives in ``kiro_crew/crewmate_prompts.json``.

Pins where a crewmate's prompt text lives:

* the modules that assemble a crewmate prompt hold no prose of their own: no
  sentence of any piece survives as a string literal in them;
* every piece in the file is read by some assembler, and every key an assembler
  names is in the file, with an ``_about`` note;
* a fill refuses a missing or an unknown placeholder;
* the file ships in the wheel.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from kiro_crew import crewmate_prompts

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"

#: The modules that assemble crewmate prompts from the file.
ASSEMBLERS = (
    "context_assembly/member.py",
    "mate_worker.py",
    "mate_upgrade_notice.py",
    "mate_capability_catalog.py",
    "crewmate_worktree.py",
    "dashboard/handlers/agent_panel.py",
    "dashboard/handlers/dashboard_page_checks.py",
    "dashboard/goal_reminder.py",
    "dashboard/goal_setup.py",
    "dashboard/mate_welcome.py",
    "dashboard/mate_thread_wakes.py",
)

#: How a key is named in an assembler: ``text("k")``, ``fill("k", ...)``, the
#: member owner's call-time wrappers ``_prompt("k")`` / ``_fill("k", ...)``, and
#: goal_setup's local alias ``T("k")`` for ``crewmate_prompts.text``.
_CALLS = ("text", "fill", "_prompt", "_fill", "T")


def _key_literals(tree: ast.AST) -> set[str]:
    """Every piece key named in *tree*: the first string argument of a loader call,
    or a string in either arm of a conditional handed to one."""
    found: set[str] = set()

    def strings(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.IfExp):
            return strings(node.body) + strings(node.orelse)
        return []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _CALLS:
            found.update(strings(node.args[0]))
    return found


def _named_keys() -> set[str]:
    keys: set[str] = set()
    for rel in ASSEMBLERS:
        keys |= _key_literals(ast.parse((SRC / rel).read_text(encoding="utf-8")))
    return {k for k in keys if "." in k or k in crewmate_prompts.keys()}


def _literals(rel: str) -> list[str]:
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            out.append("".join(v.value for v in node.values if isinstance(v, ast.Constant)))
    return out


def _sentences(text: str) -> list[str]:
    """The prose sentences of a piece long enough to be unmistakably its own."""
    bare = re.sub(r"\{[^{}]*\}", "\x00", text)
    parts = re.split(r"(?<=[.:;])\s+|\n|\x00", bare)
    return [p.strip() for p in parts if len(p.strip()) >= 40]


def test_every_piece_is_read_and_every_named_piece_exists() -> None:
    named = _named_keys()
    held = set(crewmate_prompts.keys())
    assert named - held == set(), "an assembler names a piece the file does not hold"
    assert held - named == set(), "the file holds a piece no assembler reads"


def test_every_piece_has_an_about_note_naming_its_assembler() -> None:
    raw = json.loads(crewmate_prompts.PROMPTS_PATH.read_text(encoding="utf-8"))
    about = raw[crewmate_prompts.ABOUT_KEY]
    assert set(about) == set(crewmate_prompts.keys())
    for key, note in about.items():
        assert ".py" in note, key


@pytest.mark.parametrize("rel", ASSEMBLERS)
def test_no_assembler_keeps_prose_of_its_own(rel: str) -> None:
    literals = _literals(rel)
    left = [
        (key, sentence)
        for key in sorted(crewmate_prompts.keys())
        for sentence in _sentences(crewmate_prompts.text(key))
        if any(sentence in literal for literal in literals)
    ]
    assert left == [], f"{rel} still spells prompt prose: {left[:3]}"


def test_a_fill_refuses_a_missing_or_an_unknown_placeholder() -> None:
    with pytest.raises(crewmate_prompts.PromptError, match="missing placeholders \\['every'"):
        crewmate_prompts.fill(
            "heartbeat", preamble="", block="", gateway_lines="", handoff="", patrol=""
        )
    with pytest.raises(crewmate_prompts.PromptError, match="unknown \\['extra'\\]"):
        crewmate_prompts.fill("worker_report.ended_many", turns=2, extra=1)
    with pytest.raises(crewmate_prompts.PromptError, match="no crewmate prompt"):
        crewmate_prompts.text("no.such.piece")
    assert crewmate_prompts.fill("worker_report.ended_many", turns=2) == (
        "ended 2 turns since you last heard"
    )


def test_the_file_ships_in_the_wheel() -> None:
    cfg = (ROOT / "setup.cfg").read_text(encoding="utf-8")
    block = cfg.split("[options.package_data]", 1)[1].split("\n[", 1)[0]
    assert re.search(r"^\s+crewmate_prompts\.json$", block, re.M)
    assert "include src/kiro_crew/crewmate_prompts.json" in (ROOT / "MANIFEST.in").read_text()


def test_the_offline_preview_assembles_a_fixture_exactly_as_its_golden() -> None:
    """``scripts/crewmate_prompt_preview.py`` over the round-4 all-met fixture.

    Run as its own process: the script stands in for the gateway's config and card
    store in a throwaway data home, which no test process should inherit.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "crewmate_prompt_preview.py"),
            "--fixture",
            "all-met",
            "--diff",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    golden = (ROOT / "test/fixtures/crewmate_prompts/all-met.golden.txt").read_text()
    assert golden.count("=== ") == 15
    assert crewmate_prompts.text("milestones.all_met") in golden


#: The servers whose tools only a crewmate's conductor holds, by source file.
_CREWMATE_SERVERS = ("mcp_mate.py", "mcp_panel.py", "mcp_crew_log.py", "mcp_work.py")
#: Crewmate-side tools the conductor section does not need to name, each with why.
_EXEMPT = {
    "panel_templates": "the retired panel catalog; panel_publish is the verb in use",
    "work_brief": "a dispatched worker's own brief, refused to an unbound session",
    "work_report": "a dispatched worker's report, refused to an unbound session",
    "work_ledger_rebuild": "a repair verb for a damaged ledger, not daily work",
}
#: The guide tools a crewmate uses directly; the rest are driven by the guide skill.
_GUIDE_TOOLS = ("search_docs", "find_ui")
_SESSION_TOOLS = ("session_create", "session_send", "session_read_message", "session_stop")


def _server_tools(source: str) -> set[str]:
    """Tool names a server module lists, read from its source: ``"name": "<tool>"``."""
    text = (SRC / source).read_text(encoding="utf-8")
    names = set(re.findall(r'^\s+"name":\s*"([a-z_]+)",$', text, re.M))
    listed = re.search(r"^TOOLS: tuple\[str, \.\.\.\] = \(([^)]*)\)", text, re.M)
    if listed:
        names |= set(re.findall(r'"([a-z_]+)"', listed.group(1)))
    return names


def _crewmate_tools() -> set[str]:
    tools: set[str] = set(_GUIDE_TOOLS) | set(_SESSION_TOOLS)
    for source in _CREWMATE_SERVERS:
        tools |= _server_tools(source)
    return tools - set(_EXEMPT)


def _conductor_prompt() -> str:
    """The conductor's member section and operating mode, assembled live for a crewmate.

    Through the offline preview in its own process, which configures a real crewmate
    in a throwaway data home and runs ``build_member_section`` over it.
    """
    import subprocess
    import sys

    out = []
    for piece in ("conductor", "operating_mode"):
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "crewmate_prompt_preview.py"),
                "--fixture",
                "all-met",
                "--piece",
                piece,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=True,
        )
        out.append(proc.stdout)
    return "".join(out)


def test_the_server_scan_finds_the_known_crewmate_tools() -> None:
    tools = _crewmate_tools()
    for name in (
        "set_goal",
        "dashboard_save",
        "crew_log_read",
        "work_ledger_record",
        "search_docs",
    ):
        assert name in tools
    assert len(tools) >= 30


def test_every_crewmate_only_tool_is_named_in_the_assembled_conductor_prompt() -> None:
    prompt = _conductor_prompt()
    missing = sorted(t for t in _crewmate_tools() if t not in prompt)
    assert missing == [], f"the crewmate conductor prompt never names: {missing}"


def test_the_crew_log_section_rides_a_crewmates_conductor_prompt() -> None:
    section = crewmate_prompts.text("conductor.crew_log_section")
    assert section.strip() in _conductor_prompt()
    for concept in (
        "Crew log",
        "Dashboard tab",
        "search_docs",
        "find_ui",
        "worktree",
        "Permissions",
    ):
        assert concept in section, concept
    for word in ("must", "never", "exactly", "do not"):
        assert word not in section.lower()


def test_the_worker_is_told_to_record_dashboard_facts_as_crew_log_events() -> None:
    from kiro_crew import mate_worker

    assert "crew-log events" in mate_worker.MATE_WORKER_PROMPT
    assert "worktree" in mate_worker.MATE_WORKER_PROMPT


def test_the_conductor_looks_for_an_unnamed_source_before_calling_it_missing() -> None:
    """Round 4: Atlas twice answered "no source of user complaints" with teamchat on
    the gateway; the ask named no system, so the existing check never applied."""
    text = crewmate_prompts.text("conductor.mate_tools_identity")
    assert "the ask names no system" in text
    sentence = text.split("the ask names no system", 1)[1].split(". ", 1)[0]
    assert "find_capabilities" in sentence and "worker" in sentence
