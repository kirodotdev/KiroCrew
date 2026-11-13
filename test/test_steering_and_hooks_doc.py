"""``steering-and-hooks.md``'s closed vocabularies are pinned to the code.

``scripts/docs_lint.py`` gates a doc's links, its paths and its symbols. None of
those notice when a doc's *list* goes stale: rename a hook event, add a sixth
one, or move the timeout ceiling, and every backtick in this page still resolves
while the table it sits in is wrong. A reader has no way to tell, which is worse
than the page not existing.

So this module asserts the doc against the constants that implement it. Every
check parses the DOC and compares what it reads there to the live value — never a
constant against itself, which would pass on an empty page.

The scope is deliberately the closed vocabularies and the numeric bounds: the
sets where "the code grew a member and the doc did not" is the whole failure, and
where a behaviour-preserving rewrite of the prose leaves the assertion green.
Prose claims about what a hook *does* are the reviewer's job, not this file's.
"""

import re
from pathlib import Path

import pytest

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS,
)
from kiro_crew.dashboard.handlers.steering import (
    STEERING_FILE_MAX_BYTES,
    STEERING_INCLUSION_DEFAULT,
    STEERING_INCLUSION_MODES,
    STEERING_MAX_FILES,
    STEERING_SOURCES,
)
from kiro_crew.hooks import (
    _SKILLS_ONLY_EVENTS,
    HOOK_EVENT_POST_TOOL_USE,
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENTS,
    HOOK_MATCHER_MODES,
    HOOK_TIMEOUT_MAX,
    HOOK_TIMEOUT_MIN,
    ScriptHook,
)
from kiro_crew.platform.bootstrap import KAS_CLI_FLOOR_KIRO_NOTICE
from kiro_crew.validation import REGISTER_HOOK_SCHEMA

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "src" / "kiro_crew" / "docs" / "steering-and-hooks.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _backticked(text: str) -> list[str]:
    """Every backtick-quoted token in *text*, in order of appearance."""
    return re.findall(r"`([^`\n]+)`", text)


def _section(text: str, heading: str) -> str:
    """The body of the section introduced by *heading*, up to the next heading
    of the same or a higher level.

    Scoped reads rather than whole-file greps: several of these vocabularies
    appear in more than one section (an event name is in the event table and in
    the skills-only paragraph), and a whole-file match would let the wrong
    mention satisfy the assertion.
    """
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading) :]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _unwrapped(text: str) -> str:
    """*text* with every run of whitespace, line breaks included, made one space,
    so a phrase matches however the paragraph happens to wrap."""
    return " ".join(text.split())


class TestHookEvents:
    """The five lifecycle events, as the page's own table lists them."""

    def test_event_table_lists_exactly_the_shipped_events_in_order(self, doc_text: str) -> None:
        table = _section(doc_text, "### The six events")
        rows = [ln for ln in table.splitlines() if ln.startswith("|")]
        # Row 0 is the header, row 1 the alignment rule; the rest are events.
        listed = [_backticked(r)[0] for r in rows[2:] if _backticked(r)]
        assert listed == list(HOOK_EVENTS), (
            "the event table has drifted from hooks.HOOK_EVENTS — "
            f"doc lists {listed}, code ships {list(HOOK_EVENTS)}"
        )

    def test_the_heading_counts_the_events(self, doc_text: str) -> None:
        """A sixth event would leave 'The five events' reading as a fact."""
        words = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
        assert f"### The {words[len(HOOK_EVENTS)]} events" in doc_text

    def test_pretooluse_is_named_as_the_only_deciding_event(self, doc_text: str) -> None:
        """The fail-closed paragraph is scoped to the events that can deny.

        ``ScriptHook``'s exit-code contract gives exit 2 to ``PreToolUse`` alone,
        and makes every other non-zero exit there a block. If another event ever
        gains that power the doc's "the only event" sentence is wrong.
        """
        body = _section(doc_text, "### The six events")
        assert "`PreToolUse` is the only event whose exit code can decide" in body
        # The carve-out belongs beside the contract: PreToolUse also fires as a
        # notification once kiro-cli has already approved the call, where no exit
        # code stops it (``hooks.fire_tool_call_hooks``). A doc stating only the
        # gate half reads as a guarantee the tool can never run.
        assert "does not always get a vote" in body


class TestHookFields:
    """The field table's numeric bounds and closed vocabularies."""

    def test_timeout_bounds_and_default_match_the_code(self, doc_text: str) -> None:
        row = next(
            ln
            for ln in _section(doc_text, "### What a hook runs").splitlines()
            if ln.startswith("| `timeout`")
        )
        numbers = [int(n) for n in re.findall(r"\d+", row)]
        assert numbers == [HOOK_TIMEOUT_MIN, HOOK_TIMEOUT_MAX, ScriptHook().timeout], (
            f"timeout row says {numbers}; code ships "
            f"{[HOOK_TIMEOUT_MIN, HOOK_TIMEOUT_MAX, ScriptHook().timeout]}"
        )

    def test_matcher_modes_match_the_authoring_vocabulary(self, doc_text: str) -> None:
        """The modes ``hooks.HOOK_MATCHER_MODES`` accepts, which the form offers."""
        modes = set(HOOK_MATCHER_MODES)
        row = next(
            ln
            for ln in _section(doc_text, "### What a hook runs").splitlines()
            if ln.startswith("| `matcher_mode`")
        )
        assert set(_backticked(row)[1:]) == modes, (
            f"matcher_mode row names {set(_backticked(row)[1:])}; "
            f"hooks.HOOK_MATCHER_MODES accepts {modes}. "
            "Changing the modes means updating the matcher_mode row "
            "in src/kiro_crew/docs/steering-and-hooks.md."
        )

    def test_the_matcher_split_names_both_tool_events(self, doc_text: str) -> None:
        """``matcher_mode`` is read for the message events only.

        ``ScriptHookStore.fire`` routes a tool event's matcher through
        ``_tool_matches`` (a fixed glob vocabulary, no mode) and every other
        event's through ``_context_matches`` (mode-driven). The doc states that
        split, so the two tool events it names must be the two the dispatch
        branches on -- a third tool event would silently make the claim wrong.
        """
        body = _section(doc_text, "### What a hook runs")
        para = body[body.index("**A tool matcher and a message matcher") :]
        # The paragraph is the last thing in its section, and its sentences wrap,
        # so it is read whole rather than sliced at a sentence boundary.
        tool_events = {HOOK_EVENT_PRE_TOOL_USE, HOOK_EVENT_POST_TOOL_USE}
        named = {t for t in _backticked(para) if t in HOOK_EVENTS}
        assert named == set(HOOK_EVENTS), (
            "the matcher-split paragraph must account for every event; " f"it names {named}"
        )
        # The claim itself, not just the vocabulary: a tool matcher ignores the
        # mode, and the message events are where the mode decides.
        assert "`matcher_mode` is not consulted" in para
        for event in sorted(set(HOOK_EVENTS) - tool_events):
            assert f"`{event}`" in para

    def test_skills_only_events_match_the_write_boundary(self, doc_text: str) -> None:
        body = _section(doc_text, "### A hook that loads skills instead of running a command")
        named = {t for t in _backticked(body) if t in HOOK_EVENTS}
        assert named == set(_SKILLS_ONLY_EVENTS), (
            f"the skills-hook section names {named}; "
            f"validate_hook_fields allows {set(_SKILLS_ONLY_EVENTS)}"
        )


class TestSteering:
    """The two roots, the inclusion vocabulary, and the listing caps."""

    def test_both_sources_are_documented_with_their_api_names(self, doc_text: str) -> None:
        table = _section(doc_text, "### Where they live")
        named = {t for t in _backticked(table) if t in STEERING_SOURCES}
        assert named == set(STEERING_SOURCES)

    def test_inclusion_modes_are_the_full_closed_set(self, doc_text: str) -> None:
        body = _section(doc_text, "### What a document declares")
        bullet = next(ln for ln in body.splitlines() if ln.lstrip().startswith("- `inclusion`"))
        assert set(_backticked(bullet)[1:]) == set(STEERING_INCLUSION_MODES)

    def test_the_default_mode_is_the_one_the_code_reports(self, doc_text: str) -> None:
        body = _section(doc_text, "### What a document declares")
        assert f"reads as `{STEERING_INCLUSION_DEFAULT}`" in body

    def test_listing_caps_match_the_handler(self, doc_text: str) -> None:
        body = _section(doc_text, "### Where they live")
        sentence = next(ln for ln in body.splitlines() if "at most" in ln)
        assert f"at most {STEERING_MAX_FILES} files" in sentence
        assert f"{STEERING_FILE_MAX_BYTES // 1024} KiB per document" in sentence


class TestRegisterHook:
    """The agent-side tool, and the two arguments it requires."""

    def test_required_arguments_match_the_tool_schema(self, doc_text: str) -> None:
        body = _section(doc_text, "## `register_hook` is a different thing")
        required = {f.name for f in REGISTER_HOOK_SCHEMA.fields if f.required}
        sentence = next(ln for ln in body.splitlines() if "both required" in ln)
        assert set(_backticked(sentence)) == required, (
            f"the doc requires {set(_backticked(sentence))}; "
            f"{REGISTER_HOOK_SCHEMA.tool_name} requires {required}"
        )


class TestKiroCliV2PromptHook:
    """Where a hook's ``prompt`` is the user's message, and where it is the model's.

    In a dashboard chat and on a channel turn that runs the Hooks page's hooks it
    is the user's message, never the prompt Kiro Crew builds for the model, though
    a dashboard turn can put Kiro Crew text before the typed text (queued
    background context, a sender line, a cancelled turn), and the page promises no
    size for that text. It is the prompt the backend receives on the other
    channel turns, where Kiro Crew fires an agent spec's hooks alone, and on
    kiro-cli's v2 engine, which runs an agent spec's ``userPromptSubmit`` itself.
    That prompt carries the whole session-start context only on a session's first
    message, and part of it again on the first message after a compaction. The
    page says so, and tells the reader how to spot that engine by quoting the
    gateway's log line, so both halves are pinned to the code they depend on.
    """

    _CLAIM = "On kiro-cli's v2 engine a `userPromptSubmit` hook reads the whole prompt."

    def _paragraph(self, doc_text: str) -> str:
        body = _section(doc_text, "### kiro-cli hooks that survive an update")
        assert self._CLAIM in body, (
            "the kiro-cli hooks section must say that a spec userPromptSubmit hook "
            "on kiro-cli's v2 engine reads the whole prompt, not the typed message"
        )
        start = body.index(self._CLAIM)
        end = body.find("\n\n", start)
        return _unwrapped(body[start : end if end != -1 else len(body)])

    def test_the_kiro_cli_section_states_what_the_hook_reads(self, doc_text: str) -> None:
        para = self._paragraph(doc_text)
        assert (
            "A session's first message carries the full session-start context: the agent "
            "prompt, the critical rules, memory and session context." in para
        )
        assert (
            "The first message after a compaction carries part of it again (the agent "
            "prompt, the memory index, skills and lessons), and later messages carry Kiro "
            "Crew's per-turn context instead." in para
        )
        assert "the first after a compaction, carries the full" not in para, (
            "a compaction re-sends only part of the session-start context, never the "
            "critical rules (context_assembly/turn.post_compaction_parts)"
        )
        assert "A room or guest conversation gets less at both points" in para
        assert "In every case that hook's stdin `prompt` is not the message you typed" in para
        assert (
            "A Hooks-tab `UserPromptSubmit` hook never gets the agent prompt, rules or "
            "memory, wherever it runs. Nor does the agent spec's hook on KAS" in para
        )
        assert "never Kiro Crew's context" not in para, (
            "a dashboard turn can put Kiro Crew text before the typed text "
            "(take_turn_context, sender_line, the cancelled-turn preamble), so a hook "
            "Kiro Crew fires is not free of every Kiro Crew word"
        )
        assert "`kiro-cli update`" in para
        assert "in a dashboard chat and on a channel turn that runs the Hooks page's hooks" in para
        assert "On the other channel turns it reads the prompt the backend receives" in para
        assert "(#what-a-hook-runs)" in para

    def test_the_quoted_log_line_is_the_one_the_gateway_logs(self, doc_text: str) -> None:
        """The reader greps the gateway log for this phrase; it must exist there."""
        quoted = re.search(r'"([^"]+)"', self._paragraph(doc_text))
        assert quoted, "the paragraph quotes no gateway log line"
        assert quoted.group(1) in _unwrapped(KAS_CLI_FLOOR_KIRO_NOTICE), (
            f"the doc quotes {quoted.group(1)!r}, which "
            "platform.bootstrap.KAS_CLI_FLOOR_KIRO_NOTICE does not contain"
        )

    def test_kiro_cli_is_still_the_one_running_the_spec_hook(self, doc_text: str) -> None:
        """The paragraph is true only while Kiro Crew does not fire the spec's
        observer events on kiro-cli; a change that makes it fire them must
        rewrite the paragraph."""
        assert self._CLAIM in doc_text
        assert ACP_BACKEND_KIRO not in ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS

    def test_the_stdin_prompt_sentence_names_the_exception(self, doc_text: str) -> None:
        body = _unwrapped(_section(doc_text, "### What a hook runs"))
        assert (
            "In a dashboard chat, and on a channel turn that runs the Hooks page's hooks "
            "(your own conversation, such as a Slack 1:1 DM), `prompt` is your message, not "
            "the prompt Kiro Crew builds for the model: it has none of the agent prompt, "
            "rules, memory or session context." in body
        )
        assert (
            "A dashboard turn can still put Kiro Crew text before yours, and that text can "
            "be long. It can be background context an app queued for the chat, a "
            "`[from: ...]` line in a shared chat, or the request a Stop cut off and the "
            "reply so far." in body
        )
        assert "short Kiro Crew block" not in body, (
            "a context entry an app queues (POST /api/chat/slots/{slot}/context) is "
            "bounded only by that endpoint's own cap, and take_turn_context drains every "
            "queued entry into turn.message before the UserPromptSubmit fire"
        )
        assert "`prompt` is the message you typed" not in body, (
            "background context, a [from: ...] sender line or a cancelled-turn preamble "
            "can sit before the typed text (dashboard/chat_turn/prompt_assembly.py)"
        )
        assert (
            "On the other channel turns (a Slack channel or group DM, a room or guest "
            "conversation, an autonudge or monitor wake) the Hooks page's hooks do not run, "
            "and an agent spec's own `userPromptSubmit` hook reads the prompt the backend "
            "receives" in body
        )
        assert "And on kiro-cli's older v2 engine kiro-cli runs an agent spec's" in body
        assert "(#kiro-cli-hooks-that-survive-an-update)" in body
