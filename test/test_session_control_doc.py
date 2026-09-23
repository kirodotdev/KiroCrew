"""The packaged session-control page is pinned to ``mcp_dashboard``'s real tool surface.

``scripts/docs_lint.py`` gates a doc's links, its reachability from an index, and the
paths it cites. None of that asks the question this page's whole value rests on: does
it still describe the 14 tools the server actually advertises? A tool added to
``_tool_definitions`` and absent from the page is a capability an agent reading the
shipped docs cannot find, and a tool the page names after the server drops it is worse
— the agent calls it and gets ``Error: unknown tool``.

So the tool-name set is asserted in BOTH directions against the live definitions
rather than against a hand-copied list, and the three claims most likely to rot
independently of any name — the argument defaults the page tabulates, the session
tools a channel agent is contained from, and the config switch that gates the whole
surface — are asserted against the code that decides them.

Deliberately NOT pinned here: the prose. A page that may only say what a test can
phrase is a page nobody improves.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC = REPO_ROOT / "src" / "kiro_crew" / "docs" / "session-control.md"
INDEX = REPO_ROOT / "src" / "kiro_crew" / "docs" / "index.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def advertised_tools() -> list[dict]:
    from kiro_crew.mcp_dashboard import _tool_definitions

    return _tool_definitions()


def test_the_page_names_every_advertised_tool(doc_text: str, advertised_tools: list[dict]) -> None:
    """Every tool the server advertises is documented, and nothing else is claimed.

    Both directions from one source. A one-way check would let the page fall behind a
    new tool (nobody finds it) or keep a removed one (the agent calls a name the
    dispatcher answers ``unknown tool`` for), and those are the two ways this page
    fails its reader.
    """
    advertised = {str(t["name"]) for t in advertised_tools}
    documented = {name for name in advertised if f"`{name}`" in doc_text}
    assert advertised == documented, (
        "session-control.md does not match mcp_dashboard's advertised surface; "
        f"undocumented: {sorted(advertised - documented)}"
    )

    # The other direction needs the vocabulary of tool-shaped names the page could be
    # naming, which only the page itself carries — so scan its own backticked spans for
    # anything shaped like one of this server's tools and not advertised. Scoped to the
    # half of the page that names tools: the refusal table below it lists error CODES
    # (``session_control_disabled``), which share the prefix and are not tools.
    tool_half = doc_text.split("## What you cannot reach", 1)[0]
    claimed = {
        match.group(1)
        for match in re.finditer(r"`((?:session|chat_folder|chat_tag)_[a-z_]+)`", tool_half)
    }
    assert claimed, "found no backticked tool names at all — the scan anchor moved"
    assert claimed <= advertised, (
        "session-control.md names tools this server does not advertise: "
        f"{sorted(claimed - advertised)}"
    )


def test_the_page_states_the_tool_count_it_documents(
    doc_text: str, advertised_tools: list[dict]
) -> None:
    """The page opens on a count, and a count is the one claim a reader cannot check."""
    assert f"all {len(advertised_tools)} of its tools" in doc_text
    assert f"the {len(advertised_tools)} `kirocrew-dashboard` MCP tools" in INDEX.read_text(
        encoding="utf-8"
    ), "index.md's row states a different tool count than the server advertises"


def test_the_agent_inheritance_warning_quotes_the_live_schema(
    doc_text: str, advertised_tools: list[dict]
) -> None:
    """The page's headline warning is a QUOTE, so it must still be one.

    ``session_create``'s ``agent`` description is where the runtime tells a caller that
    omitting the argument clones the caller's own agent. The page reproduces it as a
    block quote precisely so the two cannot say different things; asserting the
    sentence is present in the schema keeps that honest when the schema is reworded.
    """
    create = next(t for t in advertised_tools if t["name"] == "session_create")
    schema_text = create["inputSchema"]["properties"]["agent"]["description"]
    assert "inherits the CALLER'S OWN agent" in schema_text
    assert "inherits the CALLER'S OWN agent" in doc_text, (
        "the page must keep quoting session_create's own wording for the " "agent-inheritance trap"
    )
    assert "kirocrew-worker" in schema_text and "kirocrew-worker" in doc_text


def test_the_documented_defaults_match_the_schemas(
    doc_text: str, advertised_tools: list[dict]
) -> None:
    """Tabulated defaults and caps come from the schemas, not from memory."""
    by_name = {str(t["name"]): t for t in advertised_tools}

    read = by_name["session_read_message"]["inputSchema"]["properties"]
    assert read["limit"]["default"] == 20
    assert "default 20, max 100" in read["limit"]["description"]
    assert "default 20, max 100" in doc_text

    send = by_name["session_send"]["inputSchema"]
    assert send["required"] == ["target", "message"]
    assert "Default false." in send["properties"]["steer"]["description"]
    assert "default `false`" in doc_text

    # ``session_create`` requires nothing, which is exactly why the page has to say so:
    # a reader who assumes ``agent`` is required cannot hit the trap above.
    assert by_name["session_create"]["inputSchema"]["required"] == []
    assert "No argument is formally required." in doc_text

    # Read-only tools take no arguments at all, so the page's "none" column is a claim
    # about the schema rather than a formatting choice.
    for argless in ("chat_folder_tree", "chat_tag_list"):
        assert by_name[argless]["inputSchema"].get("properties") == {}


def test_the_page_matches_the_session_control_group_and_the_channel_block(
    doc_text: str,
) -> None:
    """Which tools are session control, and which a channel agent may never call.

    ``SESSION_CONTROL_TOOLS`` is the identity-gated group and ``CHANNEL_AGENT_BLOCKED_TOOLS``
    is the containment list. The page splits the server into those two halves and tells
    a channel agent it has none of them, so both sets are read from their own modules.
    """
    from kiro_crew.channel import CHANNEL_AGENT_BLOCKED_TOOLS
    from kiro_crew.mcp_dashboard import SESSION_CONTROL_TOOLS

    assert len(SESSION_CONTROL_TOOLS) == 7
    for tool in SESSION_CONTROL_TOOLS:
        assert f"`{tool}`" in doc_text
        assert tool in CHANNEL_AGENT_BLOCKED_TOOLS, (
            f"{tool} left the channel containment list; the page tells a channel agent "
            "it is blocked from all five"
        )
    assert "all five\nsession tools" in doc_text or "all five session tools" in doc_text


def test_every_tabulated_refusal_code_is_one_the_source_raises(doc_text: str) -> None:
    """The refusal table is the codes a caller reads back, so each must exist.

    ``session_control.py`` is the only place that mints them, and the table is the
    part of the page an agent consults while handling an error rather than while
    learning the surface -- so a code renamed there and left here sends the caller
    looking for a string it will never see. Asserted as literals rather than by
    importing a set, because the module keeps no enumeration of them.
    """
    source = (
        Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "session_control.py"
    ).read_text(encoding="utf-8")
    section = doc_text.split("## What you cannot reach", 1)[1].split("### Switches", 1)[0]
    # ROWS only. The prose around the table backticks plenty of non-codes
    # (``list_sessions``, a slot key), so a section-wide scan would assert those
    # against a module that never raises them.
    rows = [ln for ln in section.splitlines() if ln.startswith("| `")]
    codes = sorted({tok for row in rows for tok in re.findall(r"`([a-z_]{6,})`", row)})
    # ``agent.session_control`` is a config key, not a refusal code, and reaches this
    # scan because the dotted prefix sits outside the backticks in the same cell.
    codes = [c for c in codes if c != "session_control"]
    assert len(codes) >= 12, f"the refusal table shrank to {codes}"
    for code in codes:
        assert f'code="{code}"' in source or f'"{code}"' in source, (
            f"the page tabulates refusal code {code!r}, which session_control.py "
            "no longer raises"
        )


def test_the_three_target_forms_match_the_resolver(doc_text: str) -> None:
    """``target`` resolves by key, transcript stem and title -- and refuses a tie.

    The page tells a caller to pass the KEY when a title might collide, which is only
    sound while the resolver still checks every form before answering and still
    refuses a cross-form tie instead of preferring the first hit. Both halves are
    asserted, because a resolver that returned on the first key hit would make the
    page's advice pointless rather than wrong.
    """
    source = (
        Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "session_control.py"
    ).read_text(encoding="utf-8")
    assert 'code="ambiguous_target"' in source
    assert "address it by its session key instead" in source
    assert (
        "transcript_stem(slot_history_key(candidate)) == target" in source
    ), "the transcript-stem form the page documents is gone from the resolver"
    for form in ("`chat-7`", "`dashboard_chat-7`"):
        assert form in doc_text, f"the page no longer shows the {form} target form"


def test_the_queue_row_matches_what_each_verb_does_to_the_queue(doc_text: str) -> None:
    """Stop and close treat the queue differently, and in neither case simply.

    Both halves are asserted against the code that decides them, because the row is a
    two-column comparison and a reader acts on the column, not the prose: a caller
    told the queue is kept will re-send a stop to force the issue and lose the queued
    messages it was protecting, and one told the queue dies with the tab will re-type
    work the archive already holds.

    The stop half lives in the force branch of ``stop_slot_turn`` (a first, soft stop
    leaves the queue alone; the escalation clears it). The close half is the durable
    queue: ``queued_prompts`` is written with the archived conversation and handed
    back by ``sanitize_restored_queue`` when the slot is rehydrated.
    """
    handlers = (
        Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "chat_handlers.py"
    ).read_text(encoding="utf-8")
    persistence = (
        Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard" / "chat_persistence.py"
    ).read_text(encoding="utf-8")

    assert (
        "slot._queue.clear()" in handlers
    ), "the escalated stop no longer clears the queue; the row says it does"
    # The soft-stop branch is pinned by SHAPE: exactly one ``_queue.clear()`` exists in
    # this module and it sits in the hard-kill branch, so "the first stop preserves the
    # queue" IS the absence of a second one. A comment string would go stale in both
    # directions -- outliving the behaviour, or reworded while the behaviour holds.
    assert (
        handlers.count("_queue.clear()") == 1
    ), "a second _queue.clear() appeared; only the escalated stop may clear the queue"
    hard_kill = handlers.split("Stop (force): hard-killing session", 1)
    assert len(hard_kill) == 2, "the hard-kill branch marker moved"
    assert (
        "_queue.clear()" in hard_kill[0].rsplit("if force", 1)[-1]
    ), "the single _queue.clear() is no longer inside the force branch"
    assert (
        'sanitize_restored_queue(meta.get("queued_prompts"))' in persistence
    ), "a reopened conversation no longer restores its queued prompts; the row says it does"

    row = next(
        (ln for ln in doc_text.splitlines() if ln.startswith("| Queued messages |")),
        "",
    )
    assert row, "the stop-vs-close table lost its Queued messages row"
    stop_cell, close_cell = row.split("|")[2], row.split("|")[3]
    # Neither cell may read as unconditional -- that is the exact shape that was wrong.
    assert "escalated" in stop_cell, f"stop cell does not name the escalation: {stop_cell!r}"
    assert "reopened" in close_cell, f"close cell does not name the restore: {close_cell!r}"
    assert "Gone with the tab" not in row


def test_the_documented_switch_defaults_match_config(doc_text: str) -> None:
    """``agent.session_control`` and ``agent.member_dispatch`` defaults, from the section."""
    from kiro_crew.config.sections import AgentConfig

    agent_cfg = AgentConfig()
    assert agent_cfg.session_control is True
    assert agent_cfg.member_dispatch is True
    for key in ("agent.session_control", "agent.member_dispatch"):
        assert f"`{key}`" in doc_text
    switch_table = doc_text.split("### Switches and ceilings", 1)[1]
    assert (
        switch_table.count("| `true` |") == 2
    ), "the switches table must state both defaults as true, matching AgentConfig"


def test_the_documented_limits_match_their_constants(doc_text: str) -> None:
    """Rate-limit window, budgets, and the capacity ceilings behind them."""
    from kiro_crew.dashboard.chat_folders import MAX_CHAT_FOLDERS
    from kiro_crew.dashboard.create_rate_limit import (
        MAX_FOLDER_CREATES_PER_WINDOW,
        MAX_SESSION_CREATES_PER_WINDOW,
        MAX_TAG_CREATES_PER_WINDOW,
        WINDOW_SECS,
    )
    from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, MAX_SLOTS_PER_CREATOR
    from kiro_crew.dashboard.stop_retry import WINDOW_SECS as STOP_WINDOW_SECS

    assert f"over a {int(WINDOW_SECS)}-second window" in doc_text
    assert f"**{MAX_SESSION_CREATES_PER_WINDOW}** session" in doc_text
    assert f"**{MAX_FOLDER_CREATES_PER_WINDOW}** folder" in doc_text
    assert f"**{MAX_TAG_CREATES_PER_WINDOW}** tag" in doc_text
    assert (
        f"{MAX_LIVE_SLOTS} live sessions, {MAX_SLOTS_PER_CREATOR} per creator, "
        f"{MAX_CHAT_FOLDERS} folders" in doc_text
    )
    assert (
        f"({int(STOP_WINDOW_SECS)} seconds)" in doc_text
    ), "the stop-retry window the page quotes must be stop_retry.WINDOW_SECS"
