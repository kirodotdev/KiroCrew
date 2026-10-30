"""Append-only crew log core -- one test per rule the format promises.

Grouped the way the module is reasoned about: identity and lifecycle, the seq
contract, the two namespace rules, the caps, the read shapes (fold, page,
thread, ref) and finally damage tolerance. Every ``CrewLogError`` code has a test
that pins it, because the code strings are API surface a caller branches on, not
log text.

Damage tolerance, the bounded tail read and the interrupted-tail closers are in
``test_crew_log_core_damage.py``; containment, the sandbox disposition and
segments in ``test_crew_log_core_segments.py``; groups and append rollback in
``test_crew_log_core_groups.py``. The four share the fixtures and helpers in
``crew_log_core_helpers.py``.
"""

from __future__ import annotations

import inspect
import json
import threading

import crew_log_core_helpers
import pytest
from crew_log_core_helpers import CREW, SESSION, _code, _crew, _log_bytes, _raises, _session
from crew_log_type_helpers import minimal_data

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, Ref
from kiro_crew.session_ledger import _store_name

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_isolated_home = crew_log_core_helpers._isolated_home


# --- layout and lifecycle -------------------------------------------------


def test_each_kind_lands_in_its_own_root_one_directory_deep():
    crew, session = _crew(), _session()
    assert crew.path == lg.crew_log_root("crew") / _store_name(CREW) / "log.jsonl"
    assert session.path == lg.crew_log_root("session") / _store_name(SESSION) / "log.jsonl"
    assert crew.path.parent.parent == lg.crew_log_root("crew")


def test_a_colon_bearing_channel_session_id_is_storable():
    # The id is sanctioned by the schema and legal on POSIX, illegal as a
    # Windows directory name. The fold is what keeps it storable everywhere.
    sid = "slack:1712793600.123"
    led = _session(sid, task="watch the thread")
    assert ":" not in led.path.parent.name
    reopened = CrewLog.open(lg.KIND_SESSION, sid)
    assert reopened.header.id == sid
    assert reopened.header.task == "watch the thread"


def test_ids_differing_only_in_case_never_share_a_log():
    # Identity is the digest over the exact id, so a case-insensitive filesystem
    # cannot fold two crews into one file.
    lower, upper = _crew("qa"), _crew("QA")
    assert lower.path != upper.path
    lower.append("item/opened", {"which": "lower"}, src="gateway")
    assert upper.last_seq == 0


def test_the_raw_id_is_recoverable_from_the_header_not_the_directory_name():
    led = _crew("qa-team")
    assert led.path.parent.name != "qa-team"
    assert CrewLog.open(lg.KIND_CREW, "qa-team").header.id == "qa-team"


def test_a_session_log_is_invisible_to_a_flat_transcript_glob():
    # The session root is shared with the existing flat ``<key>.jsonl``
    # transcripts, whose readers glob one level deep. A crew log nested a
    # directory down must not appear to them, or the two stores shadow.
    session = _session()
    assert session.path.is_file()
    assert list(lg.crew_log_root("session").glob("*.jsonl")) == []


def test_exists_is_false_before_create_and_true_after():
    assert CrewLog.exists(lg.KIND_CREW, CREW) is False
    _crew()
    assert CrewLog.exists(lg.KIND_CREW, CREW) is True


def test_create_refuses_an_existing_log():
    _crew()
    with _raises(lg.CODE_ALREADY_EXISTS) as exc:
        _crew()
    assert _code(exc) == lg.CODE_ALREADY_EXISTS


def test_open_refuses_a_missing_log():
    with _raises(lg.CODE_NO_LEDGER) as exc:
        CrewLog.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_NO_LEDGER


def test_an_unknown_kind_is_refused_rather_than_rooted_somewhere():
    with _raises(lg.CODE_BAD_KIND) as exc:
        CrewLog.create("swarm", CREW, name="x")
    assert _code(exc) == lg.CODE_BAD_KIND


@pytest.mark.parametrize("hostile", ["", "../escape", "a/b", "a\\b", "with\0nul", ".."])
def test_a_path_hostile_id_is_refused_loudly_not_folded(hostile):
    with _raises(lg.CODE_INVALID_ID) as exc:
        _crew(hostile)
    assert _code(exc) == lg.CODE_INVALID_ID


# --- headers --------------------------------------------------------------


def test_crew_header_carries_only_the_unit_and_its_creation_time():
    # A crew's display name and template belong to the members store, which can
    # change them. An append-only line cannot, so it must not claim to own them.
    created = _crew().header
    reopened = CrewLog.open(lg.KIND_CREW, CREW).header
    assert reopened == created
    assert reopened.to_dict() == {
        "type": "crew",
        "version": 1,
        "id": CREW,
        "createdAt": created.created_at,
    }


@pytest.mark.parametrize("retired", ["name", "template"])
def test_a_field_the_crew_header_no_longer_carries_is_refused(retired):
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _crew(**{retired: "QA Crew"})
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD


def test_session_header_round_trips_including_its_crew_thread_anchor():
    created = _session(
        task="port the gate",
        pack="review",
        slot="chat-7",
        thread={"crew": CREW, "seq": 120},
        cwd="/w/repo",
        remote={"host": "pod-3"},
    ).header
    reopened = CrewLog.open(lg.KIND_SESSION, SESSION, repair=True).header
    assert reopened == created
    assert reopened.to_dict() == {
        "type": "session",
        "version": 1,
        "id": SESSION,
        "owner": CREW,
        "task": "port the gate",
        "pack": "review",
        "agent": "kirocrew",
        "slot": "chat-7",
        "thread": {"crew": CREW, "seq": 120},
        "cwd": "/w/repo",
        "remote": {"host": "pod-3"},
        "createdAt": created.created_at,
    }


def test_the_header_is_line_one_and_optional_fields_are_present_as_null():
    _session()
    first = lg.crew_log_path(lg.KIND_SESSION, SESSION).read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first)["task"] is None


def test_a_missing_required_header_field_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        CrewLog.create(lg.KIND_SESSION, SESSION, owner=CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD
    assert exc.value.field == "agent"


def test_an_unknown_header_field_is_refused_rather_than_dropped():
    # Silently dropping it would tell the caller the header stored what they
    # asked for while the value vanished.
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _crew(nickname="qa")
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD
    assert exc.value.field == "nickname"


def test_a_header_field_of_the_wrong_type_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _session(agent=7)
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD


def test_a_session_thread_anchor_of_the_wrong_shape_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _session(thread={"crew": CREW})
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD


def test_a_session_remote_that_is_not_an_object_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        _session(remote="pod-3")
    assert _code(exc) == lg.CODE_BAD_HEADER_FIELD


def test_opening_a_log_whose_header_names_another_unit_is_refused():
    _crew()
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["id"] = "other"
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        CrewLog.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_opening_a_log_whose_header_is_not_json_is_refused():
    _crew()
    lg.crew_log_path(lg.KIND_CREW, CREW).write_text("not json\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        CrewLog.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_opening_an_empty_file_reads_as_a_missing_log():
    _crew()
    lg.crew_log_path(lg.KIND_CREW, CREW).write_bytes(b"")
    with _raises(lg.CODE_NO_LEDGER) as exc:
        CrewLog.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_NO_LEDGER


def test_appending_to_a_log_whose_file_was_emptied_reads_as_missing():
    crew = _crew()
    lg.crew_log_path(lg.KIND_CREW, CREW).write_bytes(b"")
    with _raises(lg.CODE_NO_LEDGER) as exc:
        crew.append("item/opened", {}, src="gateway")
    assert _code(exc) == lg.CODE_NO_LEDGER


def test_an_interrupted_create_does_not_wedge_the_unit():
    # A short write or ENOSPC mid-header must leave nothing behind. If a
    # fragment could survive, open would truncate it to an empty file and refuse
    # while create refused the file it had just produced -- a unit stuck for
    # good. The header is published by rename, so the only two states are
    # complete and absent; an empty file, however it arose, counts as absent.
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    assert CrewLog.exists(lg.KIND_CREW, CREW) is False
    crew = _crew()
    assert crew.append("item/opened", {}, src="gateway").seq == 1
    assert CrewLog.open(lg.KIND_CREW, CREW).header.id == CREW


def test_the_header_is_published_whole_never_appended_to_an_existing_file():
    _crew()
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    assert path.read_bytes().count(b"\n") == 1
    # A file with real content is refused, so a second header can never land.
    with _raises(lg.CODE_ALREADY_EXISTS) as exc:
        _crew()
    assert _code(exc) == lg.CODE_ALREADY_EXISTS
    assert path.read_bytes().count(b"\n") == 1


# --- seq ------------------------------------------------------------------


def test_seq_starts_at_one_after_the_header_and_time_is_epoch_ms():
    crew = _crew()
    first = crew.append("item/opened", {"item": "pr-1"}, src="gateway")
    assert (first.seq, crew.last_seq) == (1, 1)
    assert first.time > 1_600_000_000_000


def test_append_if_writes_nothing_once_the_tail_is_past_the_callers_seq():
    crew = _crew()
    crew.append("item/opened", {"item": "pr-1"}, src="gateway")
    before = _log_bytes(lg.KIND_CREW, CREW)

    declined = crew.append_if("item/opened", {"item": "pr-2"}, src="gateway", max_tail_seq=0)

    assert declined is None
    assert _log_bytes(lg.KIND_CREW, CREW) == before, "a declined append rewrote the file"
    assert crew.last_seq == 1


def test_append_if_writes_and_keeps_the_seq_contract_when_the_tail_still_matches():
    # CONTROL. A bound that never matched would satisfy the test above while
    # silently dropping every closer in the system.
    crew = _crew()
    crew.append("item/opened", {"item": "pr-1"}, src="gateway")

    written = crew.append_if("item/opened", {"item": "pr-2"}, src="gateway", max_tail_seq=1)

    assert written is not None
    assert (written.seq, crew.last_seq) == (2, 2)


def test_append_if_compares_against_the_tail_read_back_under_the_lock():
    """The file decides, not the handle's cached idea of it.

    A second handle -- standing in for another process -- commits an entry this
    handle never saw. The bound must be judged against THAT tail, or a caller
    deciding from a stale cache appends after an entry it never accounted for.
    """
    crew = _crew()
    crew.append("item/opened", {"item": "pr-1"}, src="gateway")
    CrewLog.open(lg.KIND_CREW, CREW).append("item/opened", {"item": "foreign"}, src="gateway")

    # The caller's decision reached seq 1; the file is already at 2.
    declined = crew.append_if("item/opened", {"item": "pr-2"}, src="gateway", max_tail_seq=1)
    assert declined is None, "the foreign entry was not seen, so a stale decision was written"

    # Accounting for it lets the same append through, at the seq after it.
    written = crew.append_if("item/opened", {"item": "pr-2"}, src="gateway", max_tail_seq=2)
    assert written is not None and written.seq == 3


def test_seq_stays_contiguous_across_a_reopen():
    crew = _crew()
    for index in range(3):
        crew.append("item/opened", {"i": index}, src="gateway")
    reopened = CrewLog.open(lg.KIND_CREW, CREW)
    assert reopened.last_seq == 3
    assert reopened.append("item/opened", {"i": 3}, src="gateway").seq == 4
    assert [entry.seq for entry in reopened.iter_from()] == [1, 2, 3, 4]


def test_two_concurrent_writers_never_claim_the_same_seq():
    # The point of reading seq back from the file INSIDE the lock rather than
    # trusting the in-process cache.
    _crew()
    writers = [CrewLog.open(lg.KIND_CREW, CREW) for _ in range(4)]
    barrier = threading.Barrier(len(writers))

    def run(handle: CrewLog) -> None:
        barrier.wait()
        for index in range(5):
            handle.append("activity/tick", {"i": index}, src="gateway")

    threads = [threading.Thread(target=run, args=(handle,)) for handle in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seqs = [entry.seq for entry in CrewLog.open(lg.KIND_CREW, CREW).iter_from()]
    assert seqs == list(range(1, 21))


def test_the_entry_envelope_is_exactly_the_documented_shape():
    crew = _crew()
    anchor = crew.append(
        "crew/dispatch",
        {"item": "pr-4127", "target": {"kind": "crew", "name": "qa"}},
        src="gateway",
    )
    entry = crew.append(
        "crew/report",
        {"item": "pr-4127", "status": "done"},
        src="crew:qa",
        thread=anchor.seq,
        ref=Ref("session", SESSION, 40, 96),
    )
    assert entry.to_dict() == {
        "type": "crew/report",
        "seq": 2,
        "time": entry.time,
        "src": "crew:qa",
        "thread": 1,
        "ref": {"unit": "session", "id": SESSION, "from": 40, "to": 96},
        "data": {"item": "pr-4127", "status": "done"},
    }
    stored = lg.crew_log_path(lg.KIND_CREW, CREW).read_text(encoding="utf-8").splitlines()[2]
    assert json.loads(stored) == entry.to_dict()


def test_the_optional_keys_are_absent_not_null_when_unused():
    crew = _crew()
    entry = crew.append("item/opened", {}, src="gateway")
    stored = json.loads(
        lg.crew_log_path(lg.KIND_CREW, CREW).read_text(encoding="utf-8").splitlines()[1]
    )
    assert "thread" not in stored and "ref" not in stored
    assert entry.thread is None and entry.ref is None


# --- rule 1: ownership ----------------------------------------------------


@pytest.mark.parametrize(
    "kind,owned",
    [
        (lg.KIND_CREW, "member/joined"),
        (lg.KIND_CREW, "patrol/swept"),
        (lg.KIND_SESSION, "turn/start"),
        (lg.KIND_SESSION, "compaction/ran"),
    ],
)
def test_a_kind_accepts_the_domains_it_owns(kind, owned):
    unit = _crew() if kind == lg.KIND_CREW else _session()
    assert unit.append(owned, {}, src="gateway").type == owned


@pytest.mark.parametrize(
    "kind,foreign",
    [(lg.KIND_CREW, "turn/start"), (lg.KIND_SESSION, "member/joined")],
)
def test_a_kind_refuses_a_domain_the_other_kind_owns(kind, foreign):
    unit = _crew() if kind == lg.KIND_CREW else _session()
    with _raises(lg.CODE_EVENT_TYPE_NOT_OWNED) as exc:
        unit.append(foreign, {}, src="gateway")
    assert _code(exc) == lg.CODE_EVENT_TYPE_NOT_OWNED


def test_the_ownership_registry_is_the_documented_partition():
    assert lg.TYPE_OWNERSHIP[lg.KIND_CREW] == {
        "member",
        "activity",
        "slot",
        "patrol",
        "message",
        "crew",
        "item",
        "memory",
    }
    assert lg.TYPE_OWNERSHIP[lg.KIND_SESSION] == {
        "session",
        "turn",
        "step",
        "tool",
        "approval",
        "model",
        "compaction",
        "message",
        "request",
        "context",
        "background",
        "subagent",
        "plan",
        "write",
        "ledger",
        "object",
        "radar",
        "work",
        "panel",
        "dashboard",
        "card",
        "guide",
    }


#: The session log's complete vocabulary. Spelled out in full rather than derived
#: from the ownership registry, so a domain that quietly loses an action is caught --
#: the registry is prefix-based and would not notice. ``KIROCREW_CREW_LOG`` defaults
#: on, so installs hold these types and a change to them needs the additive rule or
#: a migration; this tuple is what makes such a change deliberate rather than silent.
SESSION_VOCABULARY: tuple[str, ...] = (
    "session/opened",
    "session/closed",
    "turn/started",
    "turn/refused",
    "turn/completed",
    "message/received",
    "message/sent",
    "message/chunk",
    "message/queued",
    "request/configured",
    "context/composed",
    "step/started",
    "step/completed",
    "tool/called",
    "tool/completed",
    "approval/requested",
    "approval/decided",
    "model/selected",
    "compaction/applied",
    "plan/updated",
    "background/completed",
    "subagent/spawned",
    "subagent/steered",
    "subagent/completed",
    "subagent/failed",
    "write/dropped",
    "ledger/recorded",
    "object/observed",
    "radar/recorded",
    "work/recorded",
    "panel/published",
    "card/proposed",
    "card/finished",
    "guide/offered",
    "guide/started",
    "guide/finished",
)


def test_the_session_kind_accepts_every_type_in_the_vocabulary(tmp_path):
    """The format owns the whole vocabulary, emitters or not.

    A type is in the vocabulary because a site can honestly produce it, not because
    it is written today: `approval/*`, `background/completed` and `subagent/*` wait
    on one slot-key-to-session-id resolver. Each is writable now, so landing that
    resolver is an emitter change and not a format change.
    """
    led = _session("vocab-sess")
    for entry_type in SESSION_VOCABULARY:
        led.append(entry_type, minimal_data(lg.KIND_SESSION, entry_type), src="gateway")
    written = [e.type for e in led.iter_from(1)]
    assert written == list(SESSION_VOCABULARY)


def test_a_type_the_vocabulary_dropped_is_refused_with_its_domain(tmp_path):
    """A removed type whose whole domain went with it fails closed.

    `skill/*`, `summary/*` and `remote/*` have no site that could honestly produce
    them, so their domains left `TYPE_OWNERSHIP` rather than sitting in it unwritten.
    Re-adding one is a deliberate registry change, which is what this pins: a type
    cannot creep back in on a prefix that was never removed.
    """
    led = _session("vocab-dropped")
    for dropped in ("skill/loaded", "skill/searched", "summary/written", "remote/placed"):
        with _raises(lg.CODE_EVENT_TYPE_NOT_OWNED):
            led.append(dropped, {}, src="gateway")


def test_a_type_outside_the_vocabulary_is_still_refused(tmp_path):
    # Owning a whole vocabulary is not owning everything: a domain the session
    # kind does not have must still fail closed rather than be written.
    led = _session("vocab-refuse")
    for foreign in ("member/joined", "patrol/ran", "item/phase", "nonsense/happened"):
        with _raises(lg.CODE_EVENT_TYPE_NOT_OWNED):
            led.append(foreign, {}, src="gateway")


def test_a_reader_with_the_vocabulary_reconstructs_every_type(tmp_path):
    """A fold declaring the whole vocabulary reads a whole log without refusing.

    The point of the declared-vocabulary read: a reader that knows the format
    reconstructs it, and one that meets something outside the format is told
    rather than left to guess.
    """
    led = _session("vocab-read")
    for entry_type in SESSION_VOCABULARY:
        led.append(entry_type, minimal_data(lg.KIND_SESSION, entry_type), src="gateway")

    reader = lg.CrewLog.open(lg.KIND_SESSION, "vocab-read")
    seen = list(reader.iter_from(1, known=set(SESSION_VOCABULARY)))
    assert [e.type for e in seen] == list(SESSION_VOCABULARY)
    # Order is asserted on `seq`, which the writer assigns under the lock, so it
    # carries the same claim a per-entry marker would and needs no field the
    # entry's own type does not declare.
    assert [e.seq for e in seen] == list(range(1, len(SESSION_VOCABULARY) + 1))


def test_message_is_owned_by_both_kinds_because_both_have_messages():
    # Ownership answers "does this KIND have such events", not "is the name
    # taken". A crew forwards messages and a session records its own bodies, so
    # the domain belongs to both registries rather than being renamed in one.
    assert "message" in lg.TYPE_OWNERSHIP[lg.KIND_CREW]
    assert "message" in lg.TYPE_OWNERSHIP[lg.KIND_SESSION]
    assert _crew().append("message/forwarded", {}, src="gateway").seq == 1
    assert (
        _session()
        .append(
            "message/received", {"turn": 0, "role": "user", "source": "dashboard"}, src="gateway"
        )
        .seq
        == 1
    )


@pytest.mark.parametrize(
    "bad",
    [
        "noslash",
        "/leading",
        "trailing/",
        "a/b/c",
        "-bad/x",
        "x/-bad",
        # The name pattern ends at ``\Z``: a trailing newline is not part of a
        # plain name, and an accepted one would be persisted into every reader of
        # the entry.
        "activity/tick\n",
        "activity\n/tick",
    ],
)
def test_a_type_that_is_not_domain_slash_action_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_TYPE) as exc:
        crew.append(bad, {}, src="gateway")
    assert _code(exc) == lg.CODE_BAD_TYPE


@pytest.mark.parametrize("src", ["gateway", "dashboard", "patrol", "crew:qa", "app:radar"])
def test_a_crew_log_accepts_its_own_emitters(src):
    entry_type = "app:radar/scan" if src == "app:radar" else "activity/tick"
    assert _crew().append(entry_type, {}, src=src).src == src


@pytest.mark.parametrize(
    "bad",
    ["", "Gate way", "session:s-1", "crew:", "app:bad/name", "unknown", "crew:qa\n", "app:radar\n"],
)
def test_an_unrecognized_src_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_SRC) as exc:
        crew.append("activity/tick", {}, src=bad)
    assert _code(exc) == lg.CODE_BAD_SRC


# --- rule 2: authorization on src -----------------------------------------
#
# The full per-kind matrix lives in test_crew_log_kinds.py; these pin the codes.


def test_a_crew_guest_writes_the_crew_kinds_own_domains():
    # Its name in src is the signature, so the type carries the fact alone.
    crew = _crew()
    assert crew.append("crew/finding", {}, src="crew:qa").src == "crew:qa"
    assert crew.append("item/phase", {}, src="crew:qa").src == "crew:qa"


def test_an_app_guest_writes_only_its_own_type_namespace():
    crew = _crew()
    assert crew.append("app:radar/scan", {}, src="app:radar").type == "app:radar/scan"
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append("member/joined", {}, src="app:radar")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION
    assert exc.value.field == "src"


def test_a_session_log_refuses_the_app_type_namespace_outright():
    session = _session()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        session.append("app:radar/scan", {}, src="gateway")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


@pytest.mark.parametrize(
    ("entry_type", "src"),
    [
        pytest.param(
            "app:other/scan", "app:radar", id="test_a_guest_may_not_write_another_guests_namespace"
        ),
        pytest.param(
            "app:radar/scan",
            "gateway",
            id="test_a_non_guest_emitter_may_not_borrow_a_guest_namespace",
        ),
    ],
)
def test_an_app_namespace_is_written_only_by_its_own_guest(entry_type, src):
    crew = _crew()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append(entry_type, {}, src=src)
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


def test_a_type_carrying_a_writers_identity_is_not_a_type():
    # A crew's facts are built-in domains; the writer is named by src. So the
    # only guest type namespace is app:, and crew:<name>/<action> is malformed.
    crew = _crew()
    with _raises(lg.CODE_BAD_TYPE) as exc:
        crew.append("crew:qa/report", {}, src="crew:qa")
    assert _code(exc) == lg.CODE_BAD_TYPE


# --- rule 3: thread, ref, size --------------------------------------------


def test_thread_groups_entries_under_an_earlier_anchor():
    crew = _crew()
    anchor = crew.append("item/opened", {}, src="gateway")
    child = crew.append("item/updated", {}, src="gateway", thread=anchor.seq)
    assert child.thread == anchor.seq


@pytest.mark.parametrize("bad", [0, -1, True, 99])
def test_a_thread_that_names_no_earlier_entry_is_refused(bad):
    crew = _crew()
    crew.append("item/opened", {}, src="gateway")
    with _raises(lg.CODE_BAD_THREAD) as exc:
        crew.append("item/updated", {}, src="gateway", thread=bad)
    assert _code(exc) == lg.CODE_BAD_THREAD


def test_a_thread_may_not_name_the_entry_being_written():
    crew = _crew()
    with _raises(lg.CODE_BAD_THREAD) as exc:
        crew.append("item/opened", {}, src="gateway", thread=1)
    assert _code(exc) == lg.CODE_BAD_THREAD


def test_a_thread_naming_a_seq_no_reader_can_parse_is_refused():
    # In range but unreadable: a group hanging off this anchor would never show
    # the anchor, so the pointer is to nothing.
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"type":"activity/tick","seq":'  # seq 2, terminated, unparseable
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reopened = CrewLog.open(lg.KIND_CREW, CREW)

    with _raises(lg.CODE_BAD_THREAD) as exc:
        reopened.append("activity/tick", {}, src="gateway", thread=2)
    assert _code(exc) == lg.CODE_BAD_THREAD
    # The intact anchors on either side still work.
    assert reopened.append("activity/tick", {}, src="gateway", thread=1).thread == 1
    assert reopened.append("activity/tick", {}, src="gateway", thread=3).thread == 3


def test_an_anchor_older_than_the_tail_window_is_still_accepted():
    # The in-window check cannot see it, so this is the bounded fallback.
    crew = _crew()
    anchor = crew.append("item/opened", {"n": "anchor"}, src="gateway")
    for _ in range(3):
        crew.append("item/updated", {"blob": "x" * 40_000}, src="gateway")
    assert lg.crew_log_path(lg.KIND_CREW, CREW).stat().st_size > 72 * 1024

    child = crew.append("item/updated", {}, src="gateway", thread=anchor.seq)

    assert child.thread == anchor.seq
    assert crew.thread_page(anchor.seq).entries[-1].seq == anchor.seq


@pytest.mark.parametrize(
    "raw",
    [
        {"unit": "swarm", "id": "x", "from": 1},
        {"unit": "crew", "id": "", "from": 1},
        {"unit": "crew", "id": "a/b", "from": 1},
        {"unit": "crew", "id": "x", "from": 0},
        {"unit": "crew", "id": "x", "from": "1"},
        {"unit": "crew", "id": "x", "from": 5, "to": 4},
        {"unit": "crew", "id": "x", "from": 1, "to": 1 + lg.MAX_REF_SPAN},
        "not-an-object",
    ],
)
def test_a_malformed_ref_is_refused(raw):
    crew = _crew()
    with _raises(lg.CODE_BAD_REF) as exc:
        crew.append("item/opened", {}, src="gateway", ref=raw)
    assert _code(exc) == lg.CODE_BAD_REF


def test_a_ref_without_to_cites_one_line():
    assert Ref("crew", CREW, 7).last_seq == 7
    assert Ref("crew", CREW, 7, 9).last_seq == 9


def test_an_entry_over_the_size_ceiling_is_refused_whole():
    crew = _crew()
    crew.append("item/opened", {"note": "kept"}, src="gateway")
    before = lg.crew_log_path(lg.KIND_CREW, CREW).read_bytes()
    with _raises(lg.CODE_ENTRY_TOO_LARGE) as exc:
        crew.append("item/opened", {"blob": "x" * (lg.MAX_ENTRY_BYTES + 1)}, src="gateway")
    assert _code(exc) == lg.CODE_ENTRY_TOO_LARGE
    # Caps refuse; a refused append leaves the file byte-identical and does not
    # burn the seq it would have used.
    assert lg.crew_log_path(lg.KIND_CREW, CREW).read_bytes() == before
    assert crew.append("item/opened", {}, src="gateway").seq == 2


def test_an_entry_just_under_the_ceiling_is_accepted():
    crew = _crew()
    room = lg.MAX_ENTRY_BYTES - 200
    assert crew.append("item/opened", {"blob": "x" * room}, src="gateway").seq == 1


@pytest.mark.parametrize("bad", [None, [], "text", 7])
def test_data_that_is_not_a_json_object_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_DATA) as exc:
        crew.append("item/opened", bad, src="gateway")
    assert _code(exc) == lg.CODE_BAD_DATA


def test_data_that_cannot_be_serialized_is_refused():
    crew = _crew()
    with _raises(lg.CODE_BAD_DATA) as exc:
        crew.append("item/opened", {"when": object()}, src="gateway")
    assert _code(exc) == lg.CODE_BAD_DATA


# --- reads: fold, get, page -----------------------------------------------


def test_iter_from_is_oldest_first_and_starts_where_asked():
    crew = _crew()
    for index in range(5):
        crew.append("activity/tick", {"i": index}, src="gateway")
    assert [entry.seq for entry in crew.iter_from()] == [1, 2, 3, 4, 5]
    assert [entry.seq for entry in crew.iter_from(4)] == [4, 5]


# --- the ignorable marker -------------------------------------------------


def _raw_line(log: CrewLog, payload: dict) -> None:
    """Append a line the library would never write, to test the READ side."""
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def test_an_unmarked_entry_carries_no_ignorable_key_at_all():
    # The marker is additive: every line this format already produced stays
    # byte-identical, so a reader older than the marker sees no new key.
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    raw = json.loads(crew.path.read_text(encoding="utf-8").splitlines()[1])
    assert "ignorable" not in raw
    assert crew.get(1).ignorable is False


def test_a_marked_entry_says_so_on_the_line():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway", ignorable=True)
    raw = json.loads(crew.path.read_text(encoding="utf-8").splitlines()[1])
    assert raw["ignorable"] is True
    assert crew.get(1).ignorable is True


def test_a_reader_that_declares_nothing_still_sees_every_type():
    # The default is unchanged behaviour, which is what keeps every caller that
    # predates the marker working: no declaration, no gate.
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    crew.append("patrol/finished", {}, src="patrol")
    assert [entry.type for entry in crew.iter_from()] == ["activity/tick", "patrol/finished"]


def test_a_declared_reader_skips_an_unknown_type_the_writer_marked_ignorable():
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    crew.append("patrol/sampled", {}, src="patrol", ignorable=True)
    crew.append("activity/tick", {}, src="gateway")
    seen = [entry.seq for entry in crew.iter_from(known={"activity/tick"})]
    assert seen == [1, 3]


def test_a_declared_reader_refuses_an_unknown_type_that_is_not_marked():
    # The whole point of the marker: a REQUIRED entry a reader cannot interpret
    # may change the meaning of everything after it, so the fold stops instead
    # of returning a confident wrong answer.
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    crew.append("crew/topic-opened", {}, src="gateway")
    with _raises(lg.CODE_UNKNOWN_ENTRY_TYPE) as exc:
        list(crew.iter_from(known={"activity/tick"}))
    assert _code(exc) == lg.CODE_UNKNOWN_ENTRY_TYPE
    assert "crew/topic-opened" in exc.value.message


def test_the_refusal_names_the_seq_it_stopped_at():
    # So a caller can report where reconstruction died, or resume past it once it
    # learns the type, without re-reading to find the boundary itself.
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    crew.append("crew/topic-opened", {}, src="gateway")
    with _raises(lg.CODE_UNKNOWN_ENTRY_TYPE) as exc:
        list(crew.iter_from(known={"activity/tick"}))
    assert "2" in exc.value.message
    assert exc.value.field == "type"


def test_the_known_entries_before_an_unknown_one_still_reach_the_reader():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    crew.append("activity/tick", {"i": 1}, src="gateway")
    crew.append("crew/topic-opened", {}, src="gateway")
    seen = []
    with _raises(lg.CODE_UNKNOWN_ENTRY_TYPE):
        for entry in crew.iter_from(known={"activity/tick"}):
            seen.append(entry.seq)
    assert seen == [1, 2]


@pytest.mark.parametrize("forged", ["true", 1, "yes", [], {}])
def test_only_a_literal_true_relaxes_the_guard(forged):
    # The marker turns a refusal into a skip, so a truthy COERCION would let a
    # damaged line -- or one planted in this agent-writable tree -- switch the
    # guard off with a string or a number. Anything else reads as absent.
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    _raw_line(
        crew,
        {
            "type": "crew/topic-opened",
            "seq": 2,
            "time": 1789000000000,
            "src": "gateway",
            "ignorable": forged,
            "data": {},
        },
    )
    assert crew.get(2).ignorable is False
    with _raises(lg.CODE_UNKNOWN_ENTRY_TYPE) as exc:
        list(crew.iter_from(known={"activity/tick"}))
    assert _code(exc) == lg.CODE_UNKNOWN_ENTRY_TYPE


def test_paging_has_no_such_gate_because_showing_a_line_is_not_a_wrong_answer():
    # `page` renders history for a human. An unfamiliar line displayed is a
    # missing detail, not a corrupted fold, so paging keeps returning everything.
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    crew.append("crew/topic-opened", {}, src="gateway")
    assert [entry.seq for entry in crew.page().entries] == [2, 1]
    assert "known" not in inspect.signature(CrewLog.page).parameters


def test_resolving_a_ref_does_not_apply_a_caller_vocabulary():
    # `resolve` cites a segment for display or drill-down, so it reads like
    # `page`, not like a fold: it must not refuse a segment holding a type the
    # citing reader happens not to know.
    crew = _crew()
    crew.append("crew/topic-opened", {}, src="gateway")
    found = crew.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1))
    assert found.ok
    assert [entry.type for entry in found.entries] == ["crew/topic-opened"]


# --- retention is not damage ----------------------------------------------


def test_a_span_below_the_oldest_surviving_segment_is_pruned():
    # Retention deletes whole segments off the FRONT. A citation reaching below
    # what survives is a normal answer: the entries are gone on purpose.
    crew = _crew()
    for index in range(4):
        crew.append("activity/tick", {"i": index}, src="gateway")
    head = crew.path
    later = head.parent / "log.3.jsonl"
    lines = head.read_text(encoding="utf-8").splitlines(keepends=True)
    later.write_text(lines[0] + "".join(lines[3:]), encoding="utf-8")
    head.unlink()
    reopened = CrewLog.open(lg.KIND_CREW, CREW)
    assert lg.segment_first_seqs(lg.KIND_CREW, CREW) == [3]
    found = reopened.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=4))
    assert found.status == lg.STATUS_PRUNED
    assert not found.ok


def test_damage_in_the_surviving_part_of_a_pruned_citation_is_corrupt():
    crew = _crew()
    for index in range(4):
        crew.append("activity/tick", {"i": index}, src="gateway")
    head = crew.path
    later = head.parent / "log.3.jsonl"
    lines = head.read_text(encoding="utf-8").splitlines(keepends=True)
    later.write_text(lines[0] + "{ damaged surviving entry\n" + lines[4], encoding="utf-8")
    head.unlink()

    reopened = CrewLog.open(lg.KIND_CREW, CREW)
    found = reopened.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=4))

    assert (
        found.status == lg.STATUS_CORRUPT
    ), "a damaged surviving cited line was hidden as normal retention"
    assert [entry.seq for entry in found.entries] == [4]


def test_a_duplicated_seq_cannot_stand_in_for_a_missing_cited_line():
    """Coverage of the cited seqs is the test, not how many lines were read.

    Seq is unique under the append lock, so two lines claiming one seq is itself
    damage. A tally of lines lets that duplicate fill the place of a line that is
    gone, and `ok` then tells a caller the whole citation is still readable while
    handing it a hole -- the one answer a caller resolving a citation must be able
    to trust.
    """
    crew = _crew()
    for index in range(4):
        crew.append("activity/tick", {"i": index}, src="gateway")
    lines = crew.path.read_text(encoding="utf-8").splitlines(keepends=True)
    # Seq 3 is gone and seq 2 appears twice, so the line COUNT still reaches four.
    crew.path.write_text(lines[0] + lines[1] + lines[2] + lines[2] + lines[4], encoding="utf-8")

    reopened = CrewLog.open(lg.KIND_CREW, CREW)
    found = reopened.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=4))

    assert found.status == lg.STATUS_CORRUPT, (
        "a duplicated seq padded the tally, so a citation missing a line was "
        "reported as fully readable"
    )


def test_a_duplicated_seq_is_damage_even_when_nothing_is_missing():
    """Two lines claiming one seq cannot both be the writer's.

    Seq is unique under the append lock, so a duplicate is damage on its own --
    not only when it stands in for a line that is gone. Answering `ok` for such a
    file tells a caller it is intact, and the caller then folds a history in which
    one position has two accounts of itself.
    """
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    lines = crew.path.read_text(encoding="utf-8").splitlines(keepends=True)
    crew.path.write_text(lines[0] + lines[1] + lines[2] + lines[2] + lines[3], encoding="utf-8")

    reopened = CrewLog.open(lg.KIND_CREW, CREW)
    found = reopened.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=3))

    assert found.status == lg.STATUS_CORRUPT, (
        "a file holding one seq twice was reported intact, so a reader folds two "
        "accounts of one position"
    )


def test_a_gap_inside_a_surviving_segment_is_corrupt_not_pruned():
    # The distinction that matters: these lines SHOULD be there. Reporting
    # retention would tell a reader to stop looking for a file that is damaged.
    crew = _crew()
    for index in range(4):
        crew.append("activity/tick", {"i": index}, src="gateway")
    lines = crew.path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[2] = "{ this line is not json\n"  # seq 2, mid-file damage
    crew.path.write_text("".join(lines), encoding="utf-8")
    reopened = CrewLog.open(lg.KIND_CREW, CREW)
    found = reopened.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=4))
    assert found.status == lg.STATUS_CORRUPT
    assert found.status != lg.STATUS_PRUNED
    assert found.status != lg.STATUS_GONE
    assert not found.ok
    # The readable lines still come back: a damaged span is reported, not hidden.
    assert [entry.seq for entry in found.entries] == [1, 3, 4]


def test_a_citation_naming_lines_that_are_not_there_is_not_ok():
    """`ok` means every cited seq was read back. Nothing weaker.

    A `Ref` always names a definite span -- absent `to_seq` means the single line at
    `from_seq`, so there is no open-ended spelling -- which makes a span reaching
    past the newest entry a claim about lines that are not in the file. Answering
    `ok` with fewer entries than the citation names hands a caller a hole it cannot
    see, and that caller asked precisely whether the citation could still be read.

    Retention is the one shortening that is normal, and it keeps its own verdict in
    `pruned`; damage and a bad citation both read `corrupt`, because what a caller
    acts on is resolvable or not.
    """
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    found = crew.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=50))
    assert found.status == lg.STATUS_CORRUPT
    # The entries that DO resolve still come back, so a caller can see how far the
    # citation got rather than only that it failed.
    assert [entry.seq for entry in found.entries] == [1]


def test_a_citation_of_lines_that_are_all_present_is_ok():
    """The other side of the same rule, so it cannot be satisfied by refusing."""
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    found = crew.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=3))
    assert found.status == lg.STATUS_OK
    assert [entry.seq for entry in found.entries] == [1, 2, 3]
    # A file that GREW past the citation still resolves ok: the walk stops at the
    # cited end, so later entries cannot inflate or spoil the count.
    crew.append("activity/tick", {"i": 3}, src="gateway")
    again = crew.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=3))
    assert again.status == lg.STATUS_OK
    assert [entry.seq for entry in again.entries] == [1, 2, 3]


def test_a_cleanly_truncated_tail_is_reported_rather_than_answered_ok():
    """The case that has no torn bytes to give it away.

    Whole lines removed and nothing left half-written: the read raises nothing, and
    a reopened handle's `last_seq` simply drops. Clamping the expected count to that
    tail made it agree with what survived, so the verdict came back `ok` with the
    cited lines gone -- the one answer a caller cannot defend itself against.
    """
    crew = _crew()
    for index in range(4):
        crew.append("activity/tick", {"i": index}, src="gateway")
    citation = Ref(unit=lg.KIND_CREW, id=CREW, from_seq=2, to_seq=4)
    assert crew.resolve(citation).status == lg.STATUS_OK

    # A clean end-truncation: drop the last whole line, terminator included.
    path = lg.crew_log_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(lines[:-1]), encoding="utf-8")

    reopened = CrewLog.open(lg.KIND_CREW, CREW)
    verdict = reopened.resolve(citation)
    assert verdict.status == lg.STATUS_CORRUPT, "a lost cited line was reported as ok"
    assert [entry.seq for entry in verdict.entries] == [2, 3]


def test_a_missing_unit_is_still_gone_rather_than_corrupt():
    crew = _crew()
    found = crew.resolve(Ref(unit=lg.KIND_SESSION, id="no-such-session", from_seq=1))
    assert found.status == lg.STATUS_GONE


def test_get_returns_the_entry_or_none():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    assert crew.get(1).data == {"i": 0}
    assert crew.get(2) is None
    assert crew.get(0) is None


def test_page_walks_the_whole_history_newest_first_with_no_phantom_page():
    crew = _crew()
    for index in range(7):  # odd count, so the last page is a partial one
        crew.append("activity/tick", {"i": index}, src="gateway")
    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        page = crew.page(before=cursor, limit=3)
        seen.extend(entry.seq for entry in page.entries)
        pages += 1
        if page.next_before is None:
            break
        cursor = page.next_before
    assert seen == [7, 6, 5, 4, 3, 2, 1]
    assert pages == 3


def test_a_page_that_lands_exactly_on_the_oldest_entry_ends_the_walk():
    # 6 entries at limit 3 is the boundary a naive cursor turns into a fourth,
    # empty page.
    crew = _crew()
    for index in range(6):
        crew.append("activity/tick", {"i": index}, src="gateway")
    first = crew.page(limit=3)
    second = crew.page(before=first.next_before, limit=3)
    assert [entry.seq for entry in second.entries] == [3, 2, 1]
    assert second.next_before is None


def test_page_limit_is_clamped_to_the_read_ceiling():
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    assert len(crew.page(limit=10**6).entries) == 1
    assert len(crew.page(limit=0).entries) == 1


def test_page_of_an_empty_log_is_empty_with_no_cursor():
    page = _crew().page()
    assert page.entries == () and page.next_before is None


def test_thread_page_returns_the_group_newest_first_with_the_anchor_last():
    crew = _crew()
    anchor = crew.append("item/opened", {"n": "anchor"}, src="gateway")
    crew.append("activity/tick", {"n": "unrelated"}, src="gateway")
    members = [
        crew.append("item/updated", {"n": index}, src="gateway", thread=anchor.seq)
        for index in range(3)
    ]
    page = crew.thread_page(anchor.seq)
    assert [entry.seq for entry in page.entries] == [
        members[2].seq,
        members[1].seq,
        members[0].seq,
        anchor.seq,
    ]
    assert page.next_before is None


def test_thread_page_pages_and_reaches_the_anchor_on_the_last_page():
    crew = _crew()
    anchor = crew.append("item/opened", {}, src="gateway")
    for index in range(4):
        crew.append("item/updated", {"i": index}, src="gateway", thread=anchor.seq)
    first = crew.thread_page(anchor.seq, limit=2)
    assert [entry.seq for entry in first.entries] == [5, 4]
    second = crew.thread_page(anchor.seq, before=first.next_before, limit=2)
    assert [entry.seq for entry in second.entries] == [3, 2]
    third = crew.thread_page(anchor.seq, before=second.next_before, limit=2)
    assert [entry.seq for entry in third.entries] == [anchor.seq]
    assert third.next_before is None


def test_thread_page_of_an_unknown_anchor_is_empty_rather_than_a_refusal():
    crew = _crew()
    crew.append("activity/tick", {}, src="gateway")
    assert crew.thread_page(99).entries == ()


# --- reads: resolve -------------------------------------------------------


def test_resolve_returns_the_cited_segment_of_another_log():
    session = _session()
    for index in range(5):
        session.append("turn/start", {"i": index}, src="acp")
    crew = _crew()
    resolution = crew.resolve(Ref(lg.KIND_SESSION, SESSION, 2, 4))
    assert resolution.status == lg.STATUS_OK and resolution.ok
    assert [entry.seq for entry in resolution.entries] == [2, 3, 4]


def test_resolve_without_to_returns_the_single_cited_line():
    session = _session()
    session.append("turn/start", {"i": 0}, src="acp")
    session.append("turn/end", {"i": 1}, src="acp")
    resolution = _crew().resolve({"unit": "session", "id": SESSION, "from": 2})
    assert [entry.type for entry in resolution.entries] == ["turn/end"]


def test_resolve_follows_a_ref_into_the_citing_log_itself():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    resolution = crew.resolve(Ref(lg.KIND_CREW, CREW, 1))
    assert resolution.ok and [entry.seq for entry in resolution.entries] == [1]


def test_resolve_is_gone_when_the_cited_log_does_not_exist():
    resolution = _crew().resolve(Ref(lg.KIND_SESSION, "s-missing", 1, 3))
    assert resolution.status == lg.STATUS_GONE
    assert resolution.entries == () and not resolution.ok


def test_resolve_has_exactly_two_outcomes_and_takes_no_access_callback():
    # This layer claims no authorization, so it has none to deny. A check that
    # defaulted to allow would make the shortest call shape the insecure one, and
    # a check with no permission model behind it only looks like a boundary, so
    # `forbidden` arrives with the first caller that HAS one.
    assert not hasattr(lg, "STATUS_FORBIDDEN")
    assert "may_read" not in inspect.signature(CrewLog.resolve).parameters
    assert {lg.STATUS_OK, lg.STATUS_GONE} == {"ok", "gone"}
