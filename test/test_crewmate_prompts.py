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


# -- ledger tracking and the gated patrol, only where the ledger is mounted ---- #

#: What the patrol text must name, and what a render without the ledger must not.
_PATROL_MARKS = ('watch="work-ledger"', "300..900", "patrol_budget.py renew")
#: The ledger verbs the tracking sentence names only where they are mounted.
_LEDGER_TOOLS = ("work_ledger_record", "work_ledger_read", "work_ledger_rebuild")


def test_the_patrol_piece_names_the_gated_ledger_watch_band_and_renewal() -> None:
    from kiro_crew.context_assembly import member

    text = member.ledger_rules_block("conductor.patrol")
    for mark in _PATROL_MARKS:
        assert mark in text, mark
    assert "Never pass gate:false on a patrol." in text
    # Bind before seed: create, then session_create, then bind, then the seed.
    order = [
        text.index("action=create"),
        text.index("2. `session_create`"),
        text.index("action=bind"),
        text.index("4. `session_send`"),
    ]
    assert order == sorted(order)
    # A quick single task keeps the report flow.
    assert "[Worker report] stays your patrol" in text


def test_the_tracking_sentence_without_the_ledger_names_no_ledger_tool() -> None:
    plain = crewmate_prompts.text("conductor.tracking")
    assert not any(tool in plain for tool in _LEDGER_TOOLS)
    mounted = crewmate_prompts.text("conductor.tracking_ledger")
    assert "work_ledger_record" in mounted and "work_ledger_read" in mounted


def _render(monkeypatch: pytest.MonkeyPatch, *, ledger: bool) -> str:
    """``build_member_section`` plus ``operating_mode_block``, as the session start
    joins them, with the ledger answer forced either way."""
    from types import SimpleNamespace

    from kiro_crew import context as ctx
    from kiro_crew import member_mate_tools
    from kiro_crew.context_assembly import member

    binding = SimpleNamespace(
        description="", triggers="", kiro_agent="", workspace="", member_id=""
    )
    cfg = ctx.KiroCrewConfig()
    cfg.agents["Atlas"] = binding
    monkeypatch.setattr(ctx.KiroCrewConfig, "load", staticmethod(lambda *a, **k: cfg))
    monkeypatch.setattr(member_mate_tools, "binding_has_ledger_tools", lambda _b: ledger)
    section = member.build_member_section("Atlas", include_briefing=False)
    return section + member.operating_mode_block("Atlas", ledger=ledger)


def test_a_render_with_the_ledger_teaches_tracking_and_the_gated_patrol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.context_assembly import member

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    text = _render(monkeypatch, ledger=True)
    for mark in _PATROL_MARKS:
        assert mark in text, mark
    assert crewmate_prompts.text("conductor.tracking_ledger") in text
    assert member.ledger_rules_block("conductor.patrol") in text
    assert _READ_THE_SKILL in text


#: The words that send the crewmate to the skill's operating procedure.
_READ_THE_SKILL = "before planning work with more than one item"


def test_the_skill_line_points_at_goal_conductor_for_the_whole_procedure() -> None:
    from kiro_crew.context_assembly import member

    text = member.ledger_rules_block("conductor.patrol")
    head = text[: text.index("Tracked work:")]
    assert "`goal-conductor` skill" in head and _READ_THE_SKILL in head
    assert "follow it for dispatch, patrol, verdicts and worker debate" in head


def test_a_render_without_the_ledger_never_sends_the_crewmate_to_the_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    text = _render(monkeypatch, ledger=False)
    assert _READ_THE_SKILL not in text
    assert "goal-conductor" not in text


def test_a_render_without_the_ledger_is_the_text_every_crewmate_had(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kiro_crew.context_assembly import member

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    text = _render(monkeypatch, ledger=False)
    for mark in _PATROL_MARKS:
        assert mark not in text, mark
    assert "monitor_start" not in text
    assert crewmate_prompts.text("conductor.tracking") in text
    assert crewmate_prompts.text("conductor.tracking_ledger") not in text
    assert member.operating_mode_block("Atlas") == crewmate_prompts.fill(
        "conductor.operating_mode", agent_label="Atlas"
    )


# -- which spec counts as having the ledger ---------------------------------- #


def _conductor_spec(**over: object) -> dict:
    spec: dict = {
        "mcpServers": {"kirocrew-work": {"command": "kirocrew", "args": ["mcp-work"]}},
        "tools": ["fs_read", "@kirocrew-work"],
        "allowedTools": [
            "@kirocrew-work/work_ledger_read",
            "@kirocrew-work/work_ledger_record",
        ],
    }
    spec.update(over)
    return spec


def test_a_spec_that_mounts_and_grants_the_ledger_has_it() -> None:
    from kiro_crew.member_mate_tools import spec_has_ledger_tools

    assert spec_has_ledger_tools(_conductor_spec())
    assert spec_has_ledger_tools(_conductor_spec(tools="*"))
    assert spec_has_ledger_tools(_conductor_spec(allowedTools=["@kirocrew-work"]))
    assert spec_has_ledger_tools(_conductor_spec(tools=["@kirocrew-work/work_ledger_record"]))


@pytest.mark.parametrize(
    "over",
    [
        pytest.param({"mcpServers": {}}, id="not-mounted"),
        pytest.param({"tools": ["fs_read"]}, id="server-not-in-tools"),
        pytest.param({"tools": ["@kirocrew-work/work_ledger_read"]}, id="only-read-selected"),
        pytest.param(
            {"mcpServers": {"kirocrew-work": {"command": "k", "disabled": True}}},
            id="muted",
        ),
        pytest.param(
            {"excludedTools": ["@kirocrew-work/work_ledger_record"]},
            id="record-excluded",
        ),
        # What ``auto_approve._apply_allowed_tools_ceiling`` leaves when the ceiling
        # withholds the verb: mounted, but not granted.
        pytest.param(
            {"allowedTools": ["@kirocrew-work/work_ledger_read"]},
            id="ceiling-withholds-record",
        ),
        pytest.param({"allowedTools": []}, id="nothing-granted"),
    ],
)
def test_a_spec_without_the_mounted_granted_ledger_has_none(over: dict) -> None:
    from kiro_crew.member_mate_tools import spec_has_ledger_tools

    assert not spec_has_ledger_tools(_conductor_spec(**over))


def test_a_binding_whose_chat_spec_cannot_be_read_has_no_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew import member_mate_tools

    def boom(_b: object) -> dict:
        raise OSError("unreadable")

    monkeypatch.setattr(member_mate_tools, "_chat_spec", boom)
    assert member_mate_tools.binding_has_ledger_tools(object()) is False
    assert member_mate_tools.binding_has_ledger_tools(None) is False


def test_the_ledger_preview_fixture_renders_the_patrol_cleanly() -> None:
    """The operating mode only: the conductor section varies by platform (briefing
    reads), so the golden beside the fixture is a reading aid, not compared here."""
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "scripts" / "crewmate_prompt_preview.py"),
            "--fixture",
            "ledger",
            "--piece",
            "operating_mode",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_PREVIEW_TIMEOUT_SECONDS,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for mark in (*_PATROL_MARKS, _READ_THE_SKILL):
        assert mark in proc.stdout, mark
