"""The per-type ``data`` registry: what it declares, and that the append path enforces it.

Three claims, and they are separate on purpose. The registry must accept every
entry a real WRITER produces (the emitter and the crash-repair closers), it must
refuse each way a payload can be wrong, and it must leave an undeclared type
alone -- which is what keeps the crew log and every guest namespace writable
while the session families are checked.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew.crew_log import (
    CODE_BAD_DATA_FIELD,
    SESSION_ENTRY_TYPES,
    CrewLog,
    CrewLogError,
    crew_log_path,
    declaration_for,
    emit,
)
from kiro_crew.crew_log import entry_types as reg
from kiro_crew.crew_log import (
    render_markdown,
)
from kiro_crew.crew_log import store as store_mod
from kiro_crew.crew_log import (
    validate_data,
)
from kiro_crew.crew_log.store import (
    STOP_REASON_INTERRUPTED,
    TOOL_STATUS_UNKNOWN,
    _closer_entries,
    _OpenTail,
)

SESSION = "acp-sess-types"

#: One canonical ``data`` per declared type, spelled the way its writer spells it:
#: every required field, and the optional fields whose presence a reader depends on.
#: The set is asserted to COVER the registry, so a type declared without an example
#: here fails rather than going untested.
CANONICAL: dict[str, dict] = {
    "session/opened": {
        "agent": "kirocrew",
        "slot": "dashboard:3",
        "model": "",
        "cwd": "/home/u/proj",
        "owner": "default",
        "resumed": False,
        "parent": {"slot": "chat-7", "sid": "acp-sess-creator"},
        "class": {"memory": "persistent", "app": "secretary", "channel": True},
    },
    "session/class": {"memory": "persistent", "app": "secretary", "channel": True},
    "session/closed": {"reason": "reset"},
    "turn/started": {"turn": 3, "actor": "user", "depth": 0, "message_seq": 11, "attempt": 2},
    "turn/refused": {"turn": 4, "actor": "cron", "reason": "gateway_closing", "depth": 1},
    "turn/completed": {
        "turn": 3,
        "depth": 0,
        "stop_reason": "end_turn",
        "duration_ms": 1300,
        "model": "claude",
        "provider": "anthropic",
        "credits": 0.0021,
        "tokens": {"input": 812, "output": 143, "cache_read": 0, "cache_write": 0},
    },
    "write/dropped": {"dropped_count": 3, "dropped_bytes": 2048},
    "message/received": {
        "turn": 3,
        "role": "user",
        "text": "fix the build",
        "source": "dashboard",
        "attachments": ["att-1"],
        "attachments_omitted": 2,
    },
    "message/sent": {"turn": 3, "step": 2, "text": "Done.", "interrupted": True},
    "message/chunk": {"turn": 3, "step": 2, "delta": "one slice"},
    "message/queued": {"source": "slack", "bytes": 214, "queued_seq": "q-8"},
    "request/configured": {
        "turn": 3,
        "model": "claude",
        "provider": "anthropic",
        "context_window": 200000,
        "system": "9f2b",
        "system_bytes": 42,
    },
    "context/composed": {
        "turn": 3,
        "step": 1,
        "sources": [{"kind": "system", "chars": 4000, "tokens": 1000}],
        "chars": 4000,
        "tokens": 1000,
        "tokens_estimated": True,
    },
    "step/started": {"turn": 3, "step": 1},
    "step/completed": {"turn": 3, "step": 1, "ms": 900},
    "tool/called": {
        "turn": 3,
        "call_id": "c-01",
        "name": "read",
        "server": "",
        "kind": "fs",
        "call_index": 1,
        "step": 1,
        "args_hash": "9f2b",
        "args_bytes": 42,
    },
    "tool/completed": {
        "turn": 3,
        "call_id": "c-01",
        "name": "read",
        "server": "",
        "status": "completed",
        "call_index": 1,
        "step": 1,
        "elapsed_ms": 90,
        "is_error": False,
        "result_hash": "1a3c",
        "result_bytes": 512,
    },
    "approval/requested": {"turn": 3, "approval_id": "a-1", "tool": "shell", "reason": "rm build/"},
    "approval/decided": {
        "turn": 3,
        "approval_id": "a-1",
        "decision": "approved",
        "by": "host",
        "cause": "no_budget",
    },
    "model/selected": {"model": "claude-fallback", "source": "fallback", "turn": 3},
    "compaction/applied": {"pct_before": 82.0, "pct_after": 41.0, "freed_pct": 41.0},
    "plan/updated": {
        "turn": 3,
        "items": [
            {"id": "1", "text": "read the code", "state": "done"},
            {"id": "2", "text": "write the fix", "state": "open"},
        ],
        "total": 9,
    },
    "subagent/spawned": {
        "agent_id": "sub-9",
        "turn": 3,
        "agent": "kirocrew-worker",
        "model": "claude",
        "scope": {"memory": True, "lessons": True, "project": False},
    },
    "subagent/steered": {"agent_id": "sub-9", "mode": "follow_up"},
    "subagent/completed": {"agent_id": "sub-9", "ms": 41200},
    "subagent/failed": {
        "agent_id": "sub-9",
        "reason": "TimeoutError",
        "outcome": "stopped",
        "ms": 1800000,
    },
    "background/completed": {
        "kind": "title",
        "model": "claude-lite",
        "provider": "anthropic",
        "credits": 0.0004,
        # Only the dimensions this provider billed: the writer drops every zero, so
        # a half-filled mapping is the ORDINARY shape here, unlike turn/completed's.
        "tokens": {"input": 611, "output": 12},
        "ms": 940,
    },
    "ledger/recorded": {
        "slot": "dashboard:3",
        "goal": "land the projection change",
        "phase": "implementation",
        "next": "regenerate the reference tables",
        "tried": {"approach": "stored document", "rejected_because": "cannot survive compaction"},
        "artifacts": {"worktree": "/w/proj", "branch": "feat/x", "pr": "123"},
        "event": "folded the ledger over the crew log",
        "event_kind": "phase",
    },
    "object/observed": {
        "producer": "probe",
        "kind": "github_pull_request",
        "target": "https://github.com/acme/widgets/pull/7",
        "fingerprint": "9f2b" * 16,
        "facts": {
            "kind": "github_pull_request",
            "target": "https://github.com/acme/widgets/pull/7",
            "state": "open",
            "draft": False,
            "head_revision": "abc123",
            "mergeability": "mergeable",
            "review_decision": "approved",
            "blocking_review": "none",
            "unresolved_review_threads": 0,
            "review_threads_complete": True,
            "checks": {"failed": [], "passed": ["ci"], "pending": [], "unknown": []},
            "checks_complete": True,
        },
        "facts_omitted": [],
        "observed_at": 1789000002.5,
    },
    "radar/recorded": {
        "crew_id": "c_0a1b2c3d",
        "owner": "kirodotdev",
        "repo": "KiroCrew",  # brand-ok: the repository name
        "number": 2251,
        "phase": "implementing",
        "next": "add the Windows branch to _safe_chmod",
        "tried": {"approach": "hasattr guard", "rejected_because": "loses the ACL"},
        "branch": "fix/safe-chmod-2251",
        "pr_number": 2271,
        "ci_state": {"state": "running", "round": 3},
        "event": "entered implementing: the test already fails",
        "event_kind": "implement",
    },
    "work/recorded": {
        "slot": "dashboard:3",
        "actor": "worker",
        "by": "dashboard:9",
        "action": "report",
        "item_id": "it_0badc0de",
        "status": "progress",
        "summary": "scoped tests green, opening the PR next",
        "artifacts": {"branch": "feat/x", "pr": "123"},
        "pr": 123,
        "event": "progress: scoped tests green",
        "event_kind": "report",
    },
}


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()


# --- what is declared ------------------------------------------------------


def test_every_type_written_today_is_declared_and_nothing_else_is():
    # The registry declares the types that HAVE a writer. A type nothing writes
    # would declare a shape no site produces, and the first emitter to land would
    # have to satisfy a contract written without it.
    #
    # 23 before this count moved: the six the emitter already wrote and the registry
    # had never declared -- four subagent/*, background/completed and plan/updated --
    # joined it. The first five stopped a fold outright; plan/updated was skipped
    # instead, which the class fold reads as damage.
    #
    # The two past those six are the crew-facing boards' own records: the Issue Radar
    # crew's ``radar/recorded`` and the work ledger's ``work/recorded``. A board's
    # writes are entries in this log rather than a second record beside it.
    assert len(SESSION_ENTRY_TYPES) == 31
    # Nine types the vocabulary owns that nothing writes. Declaring one would state
    # a shape no writer produces, and the first emitter to land would have to
    # satisfy a contract written without it. They pass through undeclared instead.
    no_writer = {
        "session/seeded",
        "message/steered",
        "tool/searched",
        "tool/loaded",
        "skill/searched",
        "skill/loaded",
        "summary/written",
        "remote/placed",
        "remote/lost",
    }
    assert no_writer.isdisjoint(SESSION_ENTRY_TYPES)
    # Held to the writers rather than to this list: a type earns a declaration by
    # having a producing site, so wiring an emitter is what makes one legitimate.
    #
    # This direction alone is NOT the whole property. It iterates the DECLARED set,
    # so it can only ever report a subset of it -- a type the writer appends and the
    # registry never declared is invisible to it by construction, which is exactly
    # the gap that left six types undeclared. It also decides "has a writer" by a
    # text search rather than by parsing the call. Both directions are measured from
    # the writers' syntax trees in
    # ``test_the_declared_vocabulary_is_exactly_what_the_writers_append``; this one
    # stays as the cheaper statement of the same half.
    assert set(SESSION_ENTRY_TYPES) == set(_types_with_a_producing_site())


def test_the_canonical_examples_cover_exactly_the_declared_types():
    assert set(CANONICAL) == set(SESSION_ENTRY_TYPES)


def test_the_actor_enum_is_the_emitters_own_set():
    # Enforced BECAUSE the emitter clamps to this set: declaring a different one
    # would refuse a value the emitter is willing to write.
    assert set(reg.ACTOR_VALUES) == set(emit.ACTORS)


def test_only_a_vocabulary_the_writer_clamps_is_enforced():
    # A closed enum refuses; an open one is reference material. Enforcing a
    # vocabulary that arrives from a provider, the gateway's teardown reasons or a
    # subagent runtime would turn "the upstream set grew" into a lost entry.
    #
    # The two actor sets, the ledger's event_kind, the observation's producer, a plan
    # row's state and the radar ledger's three vocabularies are the only ones a producing
    # site clamps: a plan row's state is computed as done-or-open from one boolean, so a
    # third value has no path to the entry, and the crew store refuses an unknown phase
    # or event kind before anything is appended, and coerces an unknown skip scope to
    # ``other``, so no value outside these sets ever reaches an entry. A type with no
    # producing site cannot qualify, however small its spec vocabulary looks: there
    # is no code enforcing the set, so the first resolver to report a value outside
    # it would have the entry refused rather than recorded.
    closed = {
        (spec.type, field.name)
        for spec in SESSION_ENTRY_TYPES.values()
        for field in _walk(spec.fields)
        if field.enum_closed
    }
    assert closed == {
        ("turn/started", "actor"),
        ("turn/refused", "actor"),
        ("ledger/recorded", "event_kind"),
        ("object/observed", "producer"),
        ("plan/updated", "state"),
        ("radar/recorded", "phase"),
        ("radar/recorded", "scope"),
        ("radar/recorded", "event_kind"),
        # The work ledger clamps every one of these before it builds the entry:
        # the vocabularies are declared beside the type and the writer imports
        # them, so the closed enum and the writer's refusal are one set.
        ("work/recorded", "actor"),
        ("work/recorded", "action"),
        ("work/recorded", "state"),
        ("work/recorded", "verdict"),
        ("work/recorded", "status"),
        ("work/recorded", "event_kind"),
    }
    emitted = set(_types_with_a_producing_site())
    assert {spec_type for spec_type, _ in closed} <= emitted


#: The append primitives in :mod:`kiro_crew.crew_log.emit`, each mapped to the
#: index of the positional argument that names the entry type.
#:
#: Keyed on the CALLEE rather than on any type-shaped literal, because emit.py also
#: hands an entry type to helpers that append nothing -- ``_entry_line_fits`` is
#: given ``"plan/updated"`` to measure a line -- and counting those would report a
#: type as emitted at a site that never appends.
_EMIT_PRIMITIVES: dict[str, int] = {
    "_write": 1,
    "append": 0,
    "_append_body_entry": 1,
}


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _literal_str(node) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _is_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _types_the_writers_append() -> dict[str, bool]:
    """Every entry type the WRITERS append under a literal name -> is EVERY append
    of it ignorable.

    Parsed off the writers' own syntax trees, so a newly wired site is covered the
    day it lands rather than the day someone remembers to extend a list here. Both
    writers are read: the emitter, and ``store``'s crash-repair closer, which names
    its types with a ``type=`` keyword instead. Missing a call shape would
    under-report the undeclared side -- the direction that breaks folds -- which is
    why the declared-but-never-appended column below is checked as a control rather
    than assumed empty.

    Scope, stated because it bounds what a caller may conclude: a type named by a
    LITERAL. One emitter helper takes its type as a parameter, and both of its call
    sites pass a literal, so they are seen; a future site that computes a type would
    not be.
    """
    skippable: dict[str, bool] = {}

    def _record(entry_type: str, ignorable: bool) -> None:
        # A type appended from two sites is skippable only if EVERY site says so:
        # one non-ignorable append is all it takes to stop a folding reader.
        skippable[entry_type] = skippable.get(entry_type, True) and ignorable

    for module in (emit, store_mod):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                keywords = {item.arg: item.value for item in node.keywords if item.arg}
                index = _EMIT_PRIMITIVES.get(_callee_name(node), -1)
                named = ""
                if 0 <= index < len(node.args):
                    named = _literal_str(node.args[index])
                elif "type" in keywords:
                    # The closer builds its entries directly: Entry(type="...", ...).
                    named = _literal_str(keywords["type"])
                if named:
                    _record(named, _is_true(keywords.get("ignorable")))
            elif isinstance(node, ast.Dict):
                # A batch member, spelled as a literal: {"type": ..., "ignorable": True}.
                members = {
                    key.value: value
                    for key, value in zip(node.keys, node.values)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                }
                named = _literal_str(members.get("type"))
                if named:
                    _record(named, _is_true(members.get("ignorable")))
    return skippable


def _walk(fields):
    for field in fields:
        yield field
        yield from _walk(field.fields)


def _types_with_a_producing_site():
    # Read the writers rather than restating their list: a type is emitted when a
    # producing module names it. This holds the closed-enum rule to code that
    # exists, so wiring a resolver cannot quietly leave an unenforceable set behind.
    sources = [
        Path(emit.__file__).read_text(encoding="utf-8"),
        Path(store_mod.__file__).read_text(encoding="utf-8"),
    ]
    for spec_type in SESSION_ENTRY_TYPES:
        if any(f'"{spec_type}"' in text for text in sources):
            yield spec_type


def test_the_declared_vocabulary_is_exactly_what_the_writers_append():
    """The registry and the writers must name the SAME set, in both directions.

    Neither direction is optional, and they fail differently:

    - a type APPENDED and never declared is what breaks readers. A folding reader
      passes ``known=`` to ``iter_from``, which refuses an entry it does not know
      unless the writer marked it skippable -- so a non-ignorable one ends every
      later fold of that log, permanently. A skippable one is not safe either: it is
      SKIPPED, and a skip is a seq discontinuity, which the class fold reads as
      damage and ``recorded_class`` then refuses on.
    - a type DECLARED and never appended states a shape no site produces, so the
      first emitter to land has to satisfy a contract written without it.

    This is the ratchet the whole class needed: nothing else derives the reader's
    vocabulary from the writers, so adding an append site is one edit and declaring
    it is a second, unlinked one. Six types had drifted apart that way.
    """
    appended = _types_the_writers_append()
    declared = set(SESSION_ENTRY_TYPES)

    # Control 1 -- non-empty sides. An extractor that silently stopped matching
    # reports a clean diff, and an empty declared side would read as "everything is
    # undeclared": a finding about the instrument, not about the code.
    assert declared, "the declared side is empty: the registry could not be read"
    assert appended, "the appended side is empty: the writers could not be parsed"

    # Control 2 -- negative control. Four declarations read by hand, which must come
    # back as appended AND as declared. This is what an over-matching extractor fails.
    known_good = {"context/composed", "approval/decided", "tool/called", "session/opened"}
    assert known_good <= set(appended)
    assert known_good <= declared

    # Control 3 -- the skippable flag is read, in BOTH values. Without this the flag
    # could be ignored entirely and every assertion below would still hold.
    assert appended["plan/updated"] is True, "an ignorable append read as non-ignorable"
    assert appended["turn/completed"] is False, "a non-ignorable append read as skippable"

    undeclared = sorted(set(appended) - declared)
    assert not undeclared, (
        "the writers append these types and the registry does not declare them, so a "
        f"log holding one cannot be folded, or has a damaged class record: {undeclared}"
    )
    # The writer-side completeness control, and it is what licenses reading the
    # column above as complete: an extractor that misses a call shape under-reports
    # BOTH columns, and this is the one that goes non-empty when it does.
    never_appended = sorted(declared - set(appended))
    assert not never_appended, (
        "the registry declares these types and no writer appends them, so each states "
        f"a shape no site produces: {never_appended}"
    )


def test_the_sampled_types_are_the_ignorable_ones():
    # Both sample a stream, which is the only thing that earns the marker: an
    # oversize body's slices, and a plan the agent overwrites at will.
    assert {spec.type for spec in SESSION_ENTRY_TYPES.values() if spec.ignorable} == {
        "message/chunk",
        "plan/updated",
    }


# --- the canonical examples validate --------------------------------------


@pytest.mark.parametrize("entry_type", sorted(CANONICAL))
def test_a_canonical_example_validates(entry_type):
    validate_data("session", entry_type, CANONICAL[entry_type])


@pytest.mark.parametrize("entry_type", sorted(CANONICAL))
def test_a_canonical_example_reduced_to_its_required_fields_validates(entry_type):
    # The optional fields are genuinely optional: a writer that omits every one of
    # them is still writing a legal entry.
    spec = SESSION_ENTRY_TYPES[entry_type]
    required = set(spec.required_names)
    validate_data(
        "session",
        entry_type,
        {name: value for name, value in CANONICAL[entry_type].items() if name in required},
    )


@pytest.mark.parametrize("entry_type", sorted(CANONICAL))
def test_dropping_any_required_field_is_refused_and_the_error_names_it(entry_type):
    spec = SESSION_ENTRY_TYPES[entry_type]
    for name in spec.required_names:
        payload = {k: v for k, v in CANONICAL[entry_type].items() if k != name}
        with pytest.raises(CrewLogError) as caught:
            validate_data("session", entry_type, payload)
        assert caught.value.code == CODE_BAD_DATA_FIELD
        assert caught.value.field == f"data.{name}"


@pytest.mark.parametrize("entry_type", sorted(CANONICAL))
def test_an_undeclared_field_is_refused_rather_than_written(entry_type):
    # The same posture as an unknown HEADER field, for the same reason: a caller
    # that misspells a field would otherwise be told the entry landed as asked
    # while the value it meant to record silently vanished.
    with pytest.raises(CrewLogError) as caught:
        validate_data("session", entry_type, {**CANONICAL[entry_type], "typoed": 1})
    assert caught.value.code == CODE_BAD_DATA_FIELD
    assert caught.value.field == "data.typoed"


#: One wrong-typed value per JSON type, chosen so the substitution cannot be read
#: as the declared type by accident.
_WRONG: dict[str, object] = {
    reg.JSON_STRING: 7,
    reg.JSON_INT: "7",
    reg.JSON_FLOAT: "7.0",
    reg.JSON_BOOL: "yes",
    reg.JSON_OBJECT: [],
    reg.JSON_ARRAY: {},
}


@pytest.mark.parametrize("entry_type", sorted(CANONICAL))
def test_every_declared_field_refuses_a_wrong_json_type(entry_type):
    spec = SESSION_ENTRY_TYPES[entry_type]
    example = CANONICAL[entry_type]
    for field in spec.fields:
        if field.name not in example:
            continue
        with pytest.raises(CrewLogError) as caught:
            validate_data("session", entry_type, {**example, field.name: _WRONG[field.json_type]})
        assert caught.value.code == CODE_BAD_DATA_FIELD
        assert caught.value.field == f"data.{field.name}"


@pytest.mark.parametrize(
    ("entry_type", "payload", "path"),
    [
        # A closed enum refuses an unknown value.
        ("turn/started", {"turn": 1, "actor": "wizard", "depth": 0}, "data.actor"),
        # A JSON `true` is a Python int, and every numeric field here counts or
        # measures something, so a flag must not land where a count belongs.
        ("step/completed", {"turn": 1, "step": True, "ms": 0}, "data.step"),
        (
            "compaction/applied",
            {"pct_before": True, "pct_after": 1.0, "freed_pct": 0.0},
            "data.pct_before",
        ),
        # An object member is checked like a top-level field, at its own path.
        (
            "turn/completed",
            {"turn": 1, "stop_reason": "end_turn", "tokens": {"input": "812"}},
            "data.tokens.input",
        ),
        # Every dimension the producing site fills is required INSIDE the object, so
        # a half-filled mapping is a mistake rather than a provider that billed less.
        (
            "turn/completed",
            {"turn": 1, "stop_reason": "end_turn", "tokens": {"input": 812}},
            "data.tokens.output",
        ),
        (
            "turn/completed",
            {"turn": 1, "stop_reason": "end_turn", "tokens": {"nope": 1}},
            "data.tokens.nope",
        ),
        # The creator object names its slot or it names nothing: a `parent` with
        # only a sid would be an edge to a unit the tree cannot place.
        (
            "session/opened",
            {
                "agent": "a",
                "slot": "chat-9",
                "model": "",
                "cwd": "",
                "owner": "default",
                "resumed": False,
                "parent": {"sid": "acp-sess-creator"},
            },
            "data.parent.slot",
        ),
        (
            "session/opened",
            {
                "agent": "a",
                "slot": "chat-9",
                "model": "",
                "cwd": "",
                "owner": "default",
                "resumed": False,
                "parent": {"slot": "chat-7", "creator": "chat-7"},
            },
            "data.parent.creator",
        ),
        # An array's declared element type, and its members.
        (
            "message/received",
            {"turn": 1, "role": "u", "source": "s", "chunks": ["4"]},
            "data.chunks[0]",
        ),
        (
            "context/composed",
            {
                "turn": 1,
                "sources": [{"kind": "system", "chars": 1}],
                "chars": 1,
                "tokens": 0,
                "tokens_estimated": True,
            },
            "data.sources[0].tokens",
        ),
    ],
)
def test_a_bad_value_is_refused_at_the_path_that_holds_it(entry_type, payload, path):
    with pytest.raises(CrewLogError) as caught:
        validate_data("session", entry_type, payload)
    assert caught.value.code == CODE_BAD_DATA_FIELD
    assert caught.value.field == path


def test_an_open_enum_accepts_a_value_it_does_not_list():
    # `stop_reason` is the provider's own terminal reason, so the registry lists
    # what it knows and enforces nothing: an unlisted reason is a record to keep.
    validate_data("session", "turn/completed", {"turn": 1, "stop_reason": "max_tokens"})
    validate_data("session", "session/closed", {"reason": "evicted"})
    validate_data(
        "session", "tool/completed", CANONICAL["tool/completed"] | {"status": "cancelled"}
    )


def test_a_token_mapping_is_required_whole_but_only_once_it_is_there():
    # The parent field stays optional: a nested requirement is checked once its
    # object is present, which is what lets the crash-repair closer write a
    # turn/completed with no tokens at all.
    validate_data("session", "turn/completed", {"turn": 1, "stop_reason": "interrupted"})


# --- what is not declared passes through ---------------------------------


@pytest.mark.parametrize(
    ("kind", "entry_type"),
    [
        # A crew log's own families: no crew emitter exists, so nothing is
        # declared and a crew write must not be blocked by this registry.
        ("crew", "member/joined"),
        ("crew", "activity/tick"),
        ("crew", "item/opened"),
        # A guest's own name is its permission; its payload is its own business.
        ("crew", "crew:qa/report"),
        ("crew", "app:radar/scanned"),
        # An action a session owns but nothing declares yet.
        ("session", "turn/noted"),
    ],
)
def test_an_undeclared_type_is_not_validated(kind, entry_type):
    assert declaration_for(kind, entry_type) is None
    validate_data(kind, entry_type, {"whatever": ["shape"], "missing": None})


def test_a_session_type_is_not_declared_for_a_crew_log():
    # The registry is per KIND. A crew log cannot hold `turn/started` at all --
    # ownership refuses it first -- so declaring it there would be a shape for a
    # write that never happens.
    assert declaration_for("crew", "turn/started") is None
    validate_data("crew", "turn/started", {})


def test_a_non_mapping_payload_is_left_to_require_data():
    # `bad_data` already owns "data is not an object". Two codes for one fact
    # would make a caller branch twice on the same refusal.
    validate_data("session", "turn/started", ["not", "a", "dict"])


# --- the append path enforces it -----------------------------------------


def _entries(session_id: str = SESSION) -> list[dict]:
    path = crew_log_path("session", session_id)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _fresh() -> CrewLog:
    return CrewLog.create("session", SESSION, owner="default", agent="kirocrew")


def test_append_refuses_a_bad_payload_and_leaves_the_file_identical():
    led = _fresh()
    before = crew_log_path("session", SESSION).read_bytes()
    with pytest.raises(CrewLogError) as caught:
        led.append("turn/started", {"turn": 1, "actor": "user"}, src="gateway")
    assert caught.value.code == CODE_BAD_DATA_FIELD
    assert crew_log_path("session", SESSION).read_bytes() == before
    # The handle is still usable: a refusal happens before any byte is written.
    led.append("turn/started", CANONICAL["turn/started"], src="gateway")
    assert [entry["type"] for entry in _entries()[1:]] == ["turn/started"]


def test_a_group_is_refused_whole_when_one_member_is_bad():
    led = _fresh()
    before = crew_log_path("session", SESSION).read_bytes()
    with pytest.raises(CrewLogError) as caught:
        led.append_many(
            [
                {"type": "message/chunk", "data": {"turn": 1, "delta": "a"}, "ignorable": True},
                {"type": "message/chunk", "data": {"turn": 1}, "ignorable": True},
            ],
            src="acp",
        )
    assert caught.value.field == "data.delta"
    assert crew_log_path("session", SESSION).read_bytes() == before


def test_a_group_refuses_a_citing_entry_the_registry_rejects():
    led = _fresh()
    before = crew_log_path("session", SESSION).read_bytes()
    with pytest.raises(CrewLogError) as caught:
        led.append_many(
            [{"type": "message/chunk", "data": {"turn": 1, "delta": "a"}, "ignorable": True}],
            src="acp",
            cite=lambda seqs: {"type": "message/sent", "data": {"turn": 1, "typo": seqs}},
        )
    assert caught.value.field == "data.typo"
    assert crew_log_path("session", SESSION).read_bytes() == before


def test_a_crew_append_is_untouched_by_the_registry():
    crew = CrewLog.create("crew", "qa")
    joined = crew.append("member/joined", {}, src="gateway")
    crew.append(
        "crew/report",
        {"anything": 1},
        src="crew:qa",
        ref={"unit": "crew", "id": "qa", "from": joined.seq},
    )
    assert crew.last_seq == 2


# --- the real writers satisfy the registry -------------------------------


def test_every_entry_a_real_turn_produces_validates():
    # The emitter is the source the declarations were read off, so this is the
    # assertion that keeps them honest: it drives the production emitters and
    # checks what actually landed, rather than a hand-written payload.
    emit.on_session_opened(
        SESSION,
        agent="kirocrew",
        slot="chat-7",
        model="claude-opus-5",
        cwd="/home/dev/project",
        owner="default",
    )
    emit.on_message_received(SESSION, 1, role="user", text="fix it", source="dashboard")
    emit.on_request_configured(SESSION, 1, model="claude", provider="kiro", context_window=200000)
    emit.on_context_composed(SESSION, 1, blocks={"system": 4000, "memory": 200}, total_chars=4200)
    emit.on_turn_started(SESSION, 1, "user")
    step = emit.on_step_started(SESSION, 1)
    emit.on_tool_called(SESSION, 1, name="fs_read", call_id="tc-1", kind="fs", args="{}")
    emit.on_tool_completed(SESSION, 1, status="completed", call_id="tc-1", result="ok")
    emit.on_step_completed(SESSION, 1, step, ms=90)
    emit.on_message_sent(SESSION, 1, step=step, text="done")
    emit.on_approval_requested(SESSION, 1, approval_id="a-1", tool="shell")
    emit.on_approval_decided(SESSION, 1, approval_id="a-1", decision="approved")
    emit.on_model_selected(SESSION, "claude-fallback", "fallback", turn=1)
    emit.on_compaction_applied(SESSION, pct_before=82.0, pct_after=41.0)
    emit.on_message_queued(SESSION, source="slack", size_bytes=214, queued_seq="q-8")
    # The six types declared for writers that already had one. Driven here rather
    # than trusted to a hand-written payload, because declaring a type turns
    # validation ON for it and a refusal is a permanently DROPPED write, not a
    # raise -- so the dropped_writes() assertion below is what proves the specs are
    # not narrower than what production actually emits.
    emit.on_plan_updated(SESSION, 1, items=[{"id": "1", "text": "read", "completed": True}])
    # Every other shape this writer produces: a cleared plan, a non-dict task it
    # skips, and a list long enough to clip, which is the only way `total` appears.
    emit.on_plan_updated(SESSION, 1, items=[])
    emit.on_plan_updated(SESSION, 1, items=["not a task", {"id": "2", "text": "write"}])
    emit.on_plan_updated(
        SESSION,
        1,
        items=[{"id": str(n), "text": f"task {n}"} for n in range(emit._MAX_PLAN_ITEMS + 2)],
    )
    emit.on_background_completed(
        SESSION,
        kind="title",
        model="claude-lite",
        provider="anthropic",
        credits=0.0004,
        input_tokens=611,
        duration_ms=940,
    )
    emit.on_subagent_spawned(
        SESSION,
        1,
        agent_id="sub-1",
        agent="kirocrew-worker",
        model="claude",
        scope={"memory": True, "lessons": False, "project": True},
    )
    emit.on_subagent_steered(SESSION, agent_id="sub-1", mode="follow_up")
    emit.on_subagent_completed(SESSION, agent_id="sub-1", duration_ms=41200)
    emit.on_subagent_failed(
        SESSION, agent_id="sub-2", reason="TimeoutError", outcome="stopped", duration_ms=1800000
    )
    # The turn-less shapes too: a spawn with no asking turn, and closers with
    # nothing measured, which is what the crash-repair closer resembles.
    emit.on_subagent_spawned(SESSION, 0, agent_id="sub-3")
    emit.on_subagent_completed(SESSION, agent_id="sub-3")
    emit.on_subagent_failed(SESSION, agent_id="sub-4")
    emit.on_background_completed(SESSION, kind="memory_consolidation")
    emit.on_turn_completed(
        SESSION,
        1,
        input_tokens=812,
        output_tokens=143,
        credits=0.42,
        duration_ms=1300,
        stop_reason="end_turn",
        model="claude",
        provider="kiro",
    )
    emit.on_session_closed(SESSION, "reset")
    assert emit.flush(timeout=5.0)
    assert emit.dropped_writes() == 0
    body = _entries()[1:]
    assert body, "the emitter wrote nothing"
    # A presence control. Without it an emitter that silently returned early would
    # leave this test green while proving nothing about its declaration, which is the
    # shape that let six types stay undeclared behind passing tests.
    produced = {entry["type"] for entry in body}
    assert {
        "plan/updated",
        "background/completed",
        "subagent/spawned",
        "subagent/steered",
        "subagent/completed",
        "subagent/failed",
    } <= produced
    # The clipping shape reached the log too, so `total` was exercised rather than
    # just declared.
    assert any(
        entry["type"] == "plan/updated" and "total" in entry["data"] for entry in body
    ), "no clipped plan entry, so the total field was never produced"
    for entry in body:
        assert declaration_for("session", entry["type"]) is not None, entry["type"]
        validate_data("session", entry["type"], entry["data"])


def test_an_oversize_body_and_its_chunk_group_validate():
    emit.on_session_opened(SESSION, agent="kirocrew", owner="default")
    emit.on_message_sent(SESSION, 1, step=1, text="x" * 200_000)
    assert emit.flush(timeout=5.0)
    body = _entries()[1:]
    kinds = [entry["type"] for entry in body]
    assert "message/chunk" in kinds and "message/sent" in kinds
    for entry in body:
        validate_data("session", entry["type"], entry["data"])


def test_the_loss_marker_the_writer_builds_validates():
    loss = emit._PendingLoss(dropped_count=3, dropped_bytes=2048)
    validate_data("session", "write/dropped", loss.data())


def test_the_failed_turn_closer_validates_without_credits_or_tokens():
    emit.on_session_opened(SESSION, agent="kirocrew", owner="default")
    emit.on_turn_started(SESSION, 1, "user")
    emit.on_turn_failed(SESSION, 1, error="AcpError", duration_ms=12, model="c", provider="kiro")
    assert emit.flush(timeout=5.0)
    closer = [e for e in _entries()[1:] if e["type"] == "turn/completed"][-1]
    assert "credits" not in closer["data"] and "tokens" not in closer["data"]
    validate_data("session", "turn/completed", closer["data"])


def test_the_crash_repair_closers_validate():
    # WHY four fields of `turn/completed` are optional. The repair knows the turn
    # and that the writer is gone; it does not know the model, the provider, the
    # depth or a duration, and a required field it cannot supply would refuse the
    # one write that closes an interrupted turn.
    tail = _OpenTail(
        turn=7,
        calls=({"call_id": "c-1", "name": "read", "server": ""},),
        last_time=1789000000000,
    )
    closers = _closer_entries(tail, 42)
    assert [entry.type for entry in closers] == ["tool/completed", "turn/completed"]
    assert closers[0].data["status"] == TOOL_STATUS_UNKNOWN
    assert closers[1].data["stop_reason"] == STOP_REASON_INTERRUPTED
    for entry in closers:
        validate_data("session", entry.type, entry.data)


def test_a_repaired_log_reopens_with_its_closers_on_disk():
    # End to end, through the store: the closers are appended by the repair path
    # and the file that comes back holds entries the registry accepts.
    #
    # There is no release/close on a handle -- ownership is bound to the process
    # and returns when it exits -- so the reopen below adopts the lease this
    # process already holds rather than being refused as a second writer.
    led = _fresh()
    led.append("turn/started", CANONICAL["turn/started"], src="gateway")
    led.append("tool/called", CANONICAL["tool/called"], src="acp")
    reopened = CrewLog.open("session", SESSION, repair=True)
    assert reopened.path == crew_log_path("session", SESSION)
    body = _entries()[1:]
    assert [entry["type"] for entry in body][-2:] == ["tool/completed", "turn/completed"]
    for entry in body:
        validate_data("session", entry["type"], entry["data"])


# --- the reference tables -------------------------------------------------


def test_the_markdown_dump_covers_every_type_and_field():
    rendered = render_markdown()
    for spec in SESSION_ENTRY_TYPES.values():
        assert f"## `{spec.type}`" in rendered
        for field in spec.fields:
            assert f"`{field.name}`" in rendered
    # A nested member is addressed by its path, so a reader can tell a member from
    # a top-level field of the same name.
    assert "`tokens.input`" in rendered
    assert "`sources[].tokens`" in rendered
    # An open vocabulary says so; a closed one does not have to.
    assert "`max_tokens`" not in rendered
    assert "(open)" in rendered


def test_the_markdown_dump_marks_the_sampled_types():
    # Derived from the registry rather than stated: the marker must appear once per
    # ignorable declaration, so adding one cannot leave the renderer silent about it
    # and the number here cannot go stale.
    sampled = [spec for spec in SESSION_ENTRY_TYPES.values() if spec.ignorable]
    assert sampled, "no ignorable declaration, so this check would pass vacuously"
    rendered = render_markdown()
    assert rendered.count("Always written with `ignorable: true`.") == len(sampled)
    for spec in sampled:
        assert f"## `{spec.type}`" in rendered


def test_the_markdown_dump_is_empty_for_a_kind_with_no_declarations():
    assert render_markdown("crew").strip() == "# Declared `crew` crew log entry types"


def test_the_cli_prints_the_tables_and_refuses_anything_else(capsys):
    assert reg.main(["--markdown"]) == 0
    assert "## `turn/started`" in capsys.readouterr().out
    assert reg.main([]) == 2
    assert "--markdown" in capsys.readouterr().err


def test_the_module_runs_as_a_command():
    # `python -m kiro_crew.crew_log.entry_types --markdown` is the documented form,
    # so the module has to be importable AS a script and not only as a library.
    root = Path(__file__).resolve().parents[1]
    # The venv this suite runs under may have ``kiro_crew`` installed from a
    # different checkout, so point the child at THIS tree's ``src`` -- the test is
    # about the module having a working entry point, not about it being installed.
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root / "src")
    result = subprocess.run(
        [sys.executable, "-m", "kiro_crew.crew_log.entry_types", "--markdown"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "# Declared `session` crew log entry types" in result.stdout


# --- the declarations are well formed ------------------------------------


def test_a_declaration_that_names_an_unknown_json_type_is_a_build_error():
    with pytest.raises(ValueError):
        reg.Field("x", "stringy")


def test_an_array_declaration_must_say_what_it_holds():
    with pytest.raises(ValueError):
        reg.Field("x", reg.JSON_ARRAY)


def test_members_may_only_be_declared_on_something_holding_objects():
    with pytest.raises(ValueError):
        reg.Field("x", reg.JSON_STRING, fields=(reg.Field("y", reg.JSON_INT),))


def test_a_closed_enum_with_no_values_is_a_build_error():
    with pytest.raises(ValueError):
        reg.Field("x", reg.JSON_STRING, enum_closed=True)
