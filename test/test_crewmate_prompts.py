"""The rules ``crewmate_prompts.json`` and the conductor's crew section are held to.

``docs/system-specs/modules/crew-mode.md`` states two rules about the text a crewmate
is told, and this file is where they are checked.

The prompt file rule: every piece in ``src/kiro_crew/crewmate_prompts.json`` is read
by an assembler, every key an assembler names exists in the file, and no sentence of
any piece is left behind as a literal in those modules. The loader itself refuses a
piece with no ``_about`` note; it cannot see the other direction (a key an assembler
asks for that the file lost), which only fails when that line runs.

The tool rule: every crewmate-only tool is named in the assembled conductor prompt,
except a short exempt list. A tool the prompt never names is one the crewmate has no
reason to know it can call.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from kiro_crew import crewmate_prompts

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "kiro_crew"

#: The modules that assemble crewmate prompts from the file, as the spec lists them.
ASSEMBLERS: tuple[str, ...] = (
    "context_assembly/member.py",
    "mate_worker.py",
    "dashboard/goal_reminder.py",
    "dashboard/goal_setup.py",
    "dashboard/mate_thread_wakes.py",
    "dashboard/mate_welcome.py",
    "mate_upgrade_notice.py",
    "mate_capability_catalog.py",
    "dashboard_agentic.py",
)

#: The call names an assembler reads a piece through: the two loader functions and
#: the local aliases ``member.py`` (``_prompt``, ``_fill``) and ``goal_setup.py``
#: (``T``) bind to them.
_READ_CALLS = frozenset({"text", "fill", "_prompt", "_fill", "T"})

#: A sentence shorter than this is too generic to call a copy ("Reply in chat.").
_MIN_SENTENCE = 40


def _tree(rel: str) -> ast.AST:
    return ast.parse((SRC / rel).read_text(encoding="utf-8"), filename=rel)


def _string_constants(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _named_keys(tree: ast.AST) -> set[str]:
    """String constants in the first argument of every read call.

    The whole argument is walked, so a key picked by a conditional
    (``T("a" if x else "b")``) yields both.
    """
    named: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in _READ_CALLS and node.args:
            named.update(_string_constants(node.args[0]))
    return named


def _keys_in_variables(tree: ast.AST, keys: frozenset[str]) -> set[str]:
    """Keys an assembler holds in a string constant and passes on by name.

    ``key = "a" if raised else "b"; text(key)`` reads both pieces without either one
    being a call argument. Any constant that IS a key counts as read here; the
    named-key check above is what catches a constant that looks like a key and is not.
    """
    return set(_string_constants(tree)) & keys


def _sentences(piece: str) -> list[str]:
    """The piece's sentences long enough to be distinctive, placeholders removed."""
    out = []
    for part in re.split(r"(?<=[.!?])\s+|\n+", piece):
        part = re.sub(r"\{[^{}]*\}", "", part).strip()
        if len(part) >= _MIN_SENTENCE:
            out.append(part)
    return out


def test_every_assembler_exists() -> None:
    """A renamed module would turn every check below into a vacuous pass."""
    missing = [rel for rel in ASSEMBLERS if not (SRC / rel).is_file()]
    assert missing == [], f"assembler modules not found: {missing}"


def test_every_key_an_assembler_names_exists() -> None:
    keys = crewmate_prompts.keys()
    unknown = sorted(
        f"{rel}: {key!r}" for rel in ASSEMBLERS for key in _named_keys(_tree(rel)) - keys
    )
    assert unknown == [], f"assemblers read pieces the file does not hold: {unknown}"


def test_every_piece_is_read_by_an_assembler() -> None:
    keys = crewmate_prompts.keys()
    read: set[str] = set()
    for rel in ASSEMBLERS:
        tree = _tree(rel)
        read |= _named_keys(tree) | _keys_in_variables(tree, keys)
    unread = sorted(keys - read)
    assert unread == [], f"pieces no assembler reads: {unread}"


@pytest.mark.parametrize("rel", ASSEMBLERS)
def test_no_sentence_of_a_piece_is_left_as_a_literal(rel: str) -> None:
    literals = _string_constants(_tree(rel))
    copies = sorted(
        f"{key}: {sentence[:60]!r}"
        for key in crewmate_prompts.keys()
        for sentence in _sentences(crewmate_prompts.text(key))
        if any(sentence in literal for literal in literals)
    )
    assert copies == [], f"{rel} keeps prompt text of its own: {copies}"


# -- every crewmate-only tool is named in the conductor prompt --------------- #

#: The server modules whose tools only a crewmate is given.
_CREWMATE_SERVERS: tuple[str, ...] = (
    "mcp_mate.py",
    "mcp_panel.py",
    "mcp_crew_log.py",
    "mcp_work.py",
)
#: The guide tools a crewmate calls directly; the rest are driven by the guide skill.
_GUIDE_TOOLS: tuple[str, ...] = ("search_docs", "find_ui")
#: The session tools a crewmate dispatches its worker through.
_SESSION_TOOLS: tuple[str, ...] = (
    "session_create",
    "session_send",
    "session_read_message",
    "session_stop",
)

#: Crewmate tools the conductor prompt deliberately does not name, with why.
_EXEMPT: dict[str, str] = {
    "panel_templates": "lists panel_publish templates, called only to pick a non-default one",
    "work_brief": "a worker's tool (mcp_work.WORKER_TOOLS), never the conductor's",
    "work_report": "a worker's tool (mcp_work.WORKER_TOOLS), never the conductor's",
    "work_ledger_rebuild": "repairs a damaged or missing ledger, not part of the ordinary loop",
    "offer_dashboard_page": (
        "named by its per-turn note (dashboard/page_offer.py), which is the only time "
        "the crewmate may call it"
    ),
}

#: How long the preview child may run before the test gives up on it.
_PREVIEW_TIMEOUT_SECONDS = 120


def _server_tools(source: str) -> set[str]:
    """Tool names a server module declares, read from its source."""
    text = (SRC / source).read_text(encoding="utf-8")
    names = set(re.findall(r'^\s+"name":\s*"([a-z_]+)",$', text, re.MULTILINE))
    listed = re.search(r"^TOOLS: tuple\[str, \.\.\.\] = \(([^)]*)\)", text, re.MULTILINE)
    if listed:
        names |= set(re.findall(r'"([a-z_]+)"', listed.group(1)))
    return names


def _crewmate_tools() -> set[str]:
    tools = set(_GUIDE_TOOLS) | set(_SESSION_TOOLS)
    for source in _CREWMATE_SERVERS:
        tools |= _server_tools(source)
    return tools - set(_EXEMPT)


@lru_cache(maxsize=1)
def _conductor_prompt() -> str:
    """The conductor's member section and operating mode, assembled for a crewmate.

    Through ``scripts/crewmate_prompt_preview.py`` in a child process: the preview
    patches module globals to fake a configured crewmate in a throwaway data home,
    and a child keeps those patches out of this test process.
    """
    # ``-X utf8`` makes the child write UTF-8 whatever the platform's code page, so
    # the decode below matches it on Windows too.
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts" / "crewmate_prompt_preview.py"),
            "--fixture",
            "all-met",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_PREVIEW_TIMEOUT_SECONDS,
        check=True,
    )
    sections = dict(
        re.findall(r"^=== (\w+) ===\n(.*?)(?=^=== \w+ ===$|\Z)", proc.stdout, re.M | re.S)
    )
    return sections["conductor"] + sections["operating_mode"]


def test_the_server_scan_finds_the_known_crewmate_tools() -> None:
    """A scan that read nothing would make the naming check pass vacuously."""
    tools = _crewmate_tools()
    for name in (
        "set_goal",
        "dashboard_save",
        "crew_log_read",
        "work_ledger_record",
        "search_docs",
    ):
        assert name in tools, name
    assert len(tools) >= 30, sorted(tools)


def test_every_exempt_tool_is_still_a_crewmate_tool() -> None:
    """An exemption for a tool no server declares hides nothing and should go."""
    declared = set(_GUIDE_TOOLS) | set(_SESSION_TOOLS)
    for source in _CREWMATE_SERVERS:
        declared |= _server_tools(source)
    stale = sorted(set(_EXEMPT) - declared)
    assert stale == [], f"exempt tools no server declares: {stale}"


def test_every_crewmate_only_tool_is_named_in_the_assembled_conductor_prompt() -> None:
    prompt = _conductor_prompt()
    missing = sorted(t for t in _crewmate_tools() if not re.search(rf"\b{t}\b", prompt))
    assert missing == [], f"the crewmate conductor prompt never names: {missing}"


# Bug-bash H4: a crewmate said "Yes, I'll install Rust", hit a safety rule, then
# offered the chip "Ask for a card to let you install it", which no card kind can
# honour. These pin the guidance that stops each step.

#: Internal words a reply written in the user's voice must not carry.
_CHIP_JARGON = ("card", "propose_change", "worker", "helper")


def _all_pieces() -> dict[str, str]:
    return {key: crewmate_prompts.text(key) for key in crewmate_prompts.keys()}


def test_the_conductor_prompt_hedges_a_gated_action_instead_of_promising_it() -> None:
    prompt = _conductor_prompt()
    assert "Promise only what you can do." in prompt
    assert "say you will try" in prompt
    assert "Never offer to ask for a permission or a card to get past a safety rule." in prompt


def test_a_blocked_command_is_handed_over_as_the_run_yourself_card() -> None:
    """The fence the chat draws as RunYourselfCard (website/src/components)."""
    prompt = _conductor_prompt()
    assert "```run-yourself" in prompt
    assert '```run-yourself summary="<what it installs or does, and from where>"' in prompt
    assert "facts only, no persuasion" in prompt
    card = (ROOT / "website" / "src" / "components" / "RunYourselfCard.tsx").read_text(
        encoding="utf-8"
    )
    assert "export const RUN_YOURSELF_LANG = 'run-yourself'" in card


def test_no_piece_makes_a_flat_promise_of_a_gated_action() -> None:
    flat = re.compile(r"\bI(?:'|’)?ll (?:install|run the install|set (?:it|that) up)\b", re.I)
    offenders = sorted(key for key, text in _all_pieces().items() if flat.search(text))
    assert offenders == []


def test_the_chip_guidance_offers_only_what_the_mate_can_do() -> None:
    prompt = _conductor_prompt()
    assert "offer only what you can then do" in prompt
    assert "never ask you for a card" in prompt
    for word in _CHIP_JARGON:
        assert word in prompt.split("no internal word:", 1)[1], word


def test_no_piece_carries_an_example_chip_with_jargon() -> None:
    """An example [OPTIONS:] line in a prompt is copied verbatim by the model."""
    offenders = []
    for key, text in _all_pieces().items():
        for labels in re.findall(r"\[OPTIONS?:([^\]]*)\]", text):
            for label in labels.split("|"):
                if any(re.search(rf"\b{w}s?\b", label, re.I) for w in _CHIP_JARGON):
                    offenders.append(f"{key}: {label.strip()}")
    assert offenders == []


def test_the_worker_hands_a_blocked_command_back_for_the_user() -> None:
    text = crewmate_prompts.text("worker.principles")
    assert "When a safety rule blocks a command" in text
    assert "do not look for a way around it" in text
    assert "exact command in a code block" in text
    assert "what it installs or does, and from where" in text
    assert "no persuasion" in text
