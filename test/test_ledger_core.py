"""Append-only ledger core -- one test per rule the format promises.

Grouped the way the module is reasoned about: identity and lifecycle, the seq
contract, the two namespace rules, the caps, the read shapes (fold, page,
thread, ref) and finally damage tolerance. Every ``LedgerError`` code has a test
that pins it, because the code strings are API surface a caller branches on, not
log text.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import stat
import threading

import pytest

from kiro_crew import ledger as lg
from kiro_crew import sandbox
from kiro_crew.config import paths
from kiro_crew.config.paths import data_home, ensure_data_home
from kiro_crew.ledger import Ledger, LedgerError, Ref, store
from kiro_crew.security.paths import is_sensitive_path
from kiro_crew.session_ledger import _store_name

CREW = "qa"
SESSION = "s-7f3a"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, never the live one."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


def _crew(unit_id: str = CREW, **fields) -> Ledger:
    return Ledger.create(lg.KIND_CREW, unit_id, **fields)


def _session(unit_id: str = SESSION, **fields) -> Ledger:
    fields.setdefault("owner", CREW)
    fields.setdefault("agent", "kirocrew")
    return Ledger.create(lg.KIND_SESSION, unit_id, **fields)


def _raises(code: str):
    return pytest.raises(LedgerError)


def _code(excinfo) -> str:
    return excinfo.value.code


def _ledger_bytes(kind: str = lg.KIND_SESSION, unit_id: str = SESSION) -> bytes:
    """The file exactly as it sits on disk, for asserting nothing was rewritten."""
    return lg.ledger_path(kind, unit_id).read_bytes()


# --- layout and lifecycle -------------------------------------------------


def test_each_kind_lands_in_its_own_root_one_directory_deep():
    crew, session = _crew(), _session()
    assert crew.path == lg.ledger_root("crew") / _store_name(CREW) / "ledger.jsonl"
    assert session.path == lg.ledger_root("session") / _store_name(SESSION) / "ledger.jsonl"
    assert crew.path.parent.parent == lg.ledger_root("crew")


def test_a_colon_bearing_channel_session_id_is_storable():
    # The id is sanctioned by the schema and legal on POSIX, illegal as a
    # Windows directory name. The fold is what keeps it storable everywhere.
    sid = "slack:1712793600.123"
    led = _session(sid, task="watch the thread")
    assert ":" not in led.path.parent.name
    reopened = Ledger.open(lg.KIND_SESSION, sid)
    assert reopened.header.id == sid
    assert reopened.header.task == "watch the thread"


def test_ids_differing_only_in_case_never_share_a_ledger():
    # Identity is the digest over the exact id, so a case-insensitive filesystem
    # cannot fold two crews into one file.
    lower, upper = _crew("qa"), _crew("QA")
    assert lower.path != upper.path
    lower.append("item/opened", {"which": "lower"}, src="gateway")
    assert upper.last_seq == 0


def test_the_raw_id_is_recoverable_from_the_header_not_the_directory_name():
    led = _crew("qa-team")
    assert led.path.parent.name != "qa-team"
    assert Ledger.open(lg.KIND_CREW, "qa-team").header.id == "qa-team"


def test_a_session_ledger_is_invisible_to_a_flat_transcript_glob():
    # The session root is shared with the existing flat ``<key>.jsonl``
    # transcripts, whose readers glob one level deep. A ledger nested a
    # directory down must not appear to them, or the two stores shadow.
    session = _session()
    assert session.path.is_file()
    assert list(lg.ledger_root("session").glob("*.jsonl")) == []


def test_exists_is_false_before_create_and_true_after():
    assert Ledger.exists(lg.KIND_CREW, CREW) is False
    _crew()
    assert Ledger.exists(lg.KIND_CREW, CREW) is True


def test_create_refuses_an_existing_ledger():
    _crew()
    with _raises(lg.CODE_ALREADY_EXISTS) as exc:
        _crew()
    assert _code(exc) == lg.CODE_ALREADY_EXISTS


def test_open_refuses_a_missing_ledger():
    with _raises(lg.CODE_NO_LEDGER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_NO_LEDGER


def test_an_unknown_kind_is_refused_rather_than_rooted_somewhere():
    with _raises(lg.CODE_BAD_KIND) as exc:
        Ledger.create("swarm", CREW, name="x")
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
    reopened = Ledger.open(lg.KIND_CREW, CREW).header
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
    reopened = Ledger.open(lg.KIND_SESSION, SESSION, repair=True).header
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
    first = lg.ledger_path(lg.KIND_SESSION, SESSION).read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first)["task"] is None


def test_a_missing_required_header_field_is_refused():
    with _raises(lg.CODE_BAD_HEADER_FIELD) as exc:
        Ledger.create(lg.KIND_SESSION, SESSION, owner=CREW)
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


def test_opening_a_ledger_whose_header_names_another_unit_is_refused():
    _crew()
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["id"] = "other"
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_opening_a_ledger_whose_header_is_not_json_is_refused():
    _crew()
    lg.ledger_path(lg.KIND_CREW, CREW).write_text("not json\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_opening_an_empty_file_reads_as_a_missing_ledger():
    _crew()
    lg.ledger_path(lg.KIND_CREW, CREW).write_bytes(b"")
    with _raises(lg.CODE_NO_LEDGER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_NO_LEDGER


def test_appending_to_a_ledger_whose_file_was_emptied_reads_as_missing():
    crew = _crew()
    lg.ledger_path(lg.KIND_CREW, CREW).write_bytes(b"")
    with _raises(lg.CODE_NO_LEDGER) as exc:
        crew.append("item/opened", {}, src="gateway")
    assert _code(exc) == lg.CODE_NO_LEDGER


def test_an_interrupted_create_does_not_wedge_the_unit():
    # A short write or ENOSPC mid-header must leave nothing behind. If a
    # fragment could survive, open would truncate it to an empty file and refuse
    # while create refused the file it had just produced -- a unit stuck for
    # good. The header is published by rename, so the only two states are
    # complete and absent; an empty file, however it arose, counts as absent.
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    assert Ledger.exists(lg.KIND_CREW, CREW) is False
    crew = _crew()
    assert crew.append("item/opened", {}, src="gateway").seq == 1
    assert Ledger.open(lg.KIND_CREW, CREW).header.id == CREW


def test_the_header_is_published_whole_never_appended_to_an_existing_file():
    _crew()
    path = lg.ledger_path(lg.KIND_CREW, CREW)
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


def test_seq_stays_contiguous_across_a_reopen():
    crew = _crew()
    for index in range(3):
        crew.append("item/opened", {"i": index}, src="gateway")
    reopened = Ledger.open(lg.KIND_CREW, CREW)
    assert reopened.last_seq == 3
    assert reopened.append("item/opened", {"i": 3}, src="gateway").seq == 4
    assert [entry.seq for entry in reopened.iter_from()] == [1, 2, 3, 4]


def test_two_concurrent_writers_never_claim_the_same_seq():
    # The point of reading seq back from the file INSIDE the lock rather than
    # trusting the in-process cache.
    _crew()
    writers = [Ledger.open(lg.KIND_CREW, CREW) for _ in range(4)]
    barrier = threading.Barrier(len(writers))

    def run(handle: Ledger) -> None:
        barrier.wait()
        for index in range(5):
            handle.append("activity/tick", {"i": index}, src="gateway")

    threads = [threading.Thread(target=run, args=(handle,)) for handle in writers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seqs = [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()]
    assert seqs == list(range(1, 21))


def test_the_entry_envelope_is_exactly_the_documented_shape():
    crew = _crew()
    anchor = crew.append("item/opened", {"item": "pr-4127"}, src="gateway")
    entry = crew.append(
        "crew:qa/report",
        {"item": "pr-4127", "status": "done"},
        src="crew:qa",
        thread=anchor.seq,
        ref=Ref("session", SESSION, 40, 96),
    )
    assert entry.to_dict() == {
        "type": "crew:qa/report",
        "seq": 2,
        "time": entry.time,
        "src": "crew:qa",
        "thread": 1,
        "ref": {"unit": "session", "id": SESSION, "from": 40, "to": 96},
        "data": {"item": "pr-4127", "status": "done"},
    }
    stored = lg.ledger_path(lg.KIND_CREW, CREW).read_text(encoding="utf-8").splitlines()[2]
    assert json.loads(stored) == entry.to_dict()


def test_the_optional_keys_are_absent_not_null_when_unused():
    crew = _crew()
    entry = crew.append("item/opened", {}, src="gateway")
    stored = json.loads(
        lg.ledger_path(lg.KIND_CREW, CREW).read_text(encoding="utf-8").splitlines()[1]
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
        "remote",
        "message",
        "request",
        "context",
        "skill",
        "background",
        "subagent",
        "summary",
        "plan",
    }


#: The session log's complete vocabulary, frozen by this commit. Types and fields
#: are additive-only from here: a type may gain an emitter later, never a
#: different shape. Spelled out in full rather than derived from the ownership
#: registry, so a domain that quietly loses an action is caught -- the registry is
#: prefix-based and would not notice.
FROZEN_SESSION_VOCABULARY: tuple[str, ...] = (
    "session/opened",
    "session/seeded",
    "session/closed",
    "turn/started",
    "turn/refused",
    "turn/completed",
    "message/received",
    "message/sent",
    "message/chunk",
    "message/queued",
    "message/steered",
    "request/configured",
    "context/composed",
    "step/started",
    "step/completed",
    "tool/called",
    "tool/completed",
    "tool/searched",
    "tool/loaded",
    "skill/searched",
    "skill/loaded",
    "approval/requested",
    "approval/decided",
    "model/selected",
    "compaction/applied",
    "summary/written",
    "plan/updated",
    "background/completed",
    "subagent/spawned",
    "subagent/steered",
    "subagent/completed",
    "subagent/failed",
    "remote/placed",
    "remote/lost",
)


def test_the_session_kind_accepts_every_type_in_the_frozen_vocabulary(tmp_path):
    """The format owns the whole vocabulary, emitters or not.

    An emitter added later must not need a format change to land -- that is what
    freezing the schema in one commit buys. So every type is writable now, even
    the ones nothing writes yet.
    """
    led = _session("vocab-sess")
    for entry_type in FROZEN_SESSION_VOCABULARY:
        led.append(entry_type, {"turn": 1}, src="gateway")
    written = [e.type for e in led.iter_from(1)]
    assert written == list(FROZEN_SESSION_VOCABULARY)


def test_a_type_outside_the_vocabulary_is_still_refused(tmp_path):
    # Owning a whole vocabulary is not owning everything: a domain the session
    # kind does not have must still fail closed rather than be written.
    led = _session("vocab-refuse")
    for foreign in ("member/joined", "patrol/ran", "item/phase", "nonsense/happened"):
        with pytest.raises(lg.LedgerError) as excinfo:
            led.append(foreign, {}, src="gateway")
        assert excinfo.value.code == lg.CODE_EVENT_TYPE_NOT_OWNED


def test_a_reader_with_the_frozen_vocabulary_reconstructs_every_type(tmp_path):
    """A fold declaring the whole vocabulary reads a whole log without refusing.

    The point of the declared-vocabulary read: a reader that knows the format
    reconstructs it, and one that meets something outside the format is told
    rather than left to guess.
    """
    led = _session("vocab-read")
    for n, entry_type in enumerate(FROZEN_SESSION_VOCABULARY, start=1):
        led.append(entry_type, {"turn": 1, "marker": n}, src="gateway")

    reader = lg.Ledger.open(lg.KIND_SESSION, "vocab-read")
    seen = list(reader.iter_from(1, known=set(FROZEN_SESSION_VOCABULARY)))
    assert [e.type for e in seen] == list(FROZEN_SESSION_VOCABULARY)
    assert [e.data["marker"] for e in seen] == list(range(1, len(FROZEN_SESSION_VOCABULARY) + 1))
    assert [e.seq for e in seen] == list(range(1, len(FROZEN_SESSION_VOCABULARY) + 1))


def test_message_is_owned_by_both_kinds_because_both_have_messages():
    # Ownership answers "does this KIND have such events", not "is the name
    # taken". A crew forwards messages and a session records its own bodies, so
    # the domain belongs to both registries rather than being renamed in one.
    assert "message" in lg.TYPE_OWNERSHIP[lg.KIND_CREW]
    assert "message" in lg.TYPE_OWNERSHIP[lg.KIND_SESSION]
    assert _crew().append("message/forwarded", {}, src="gateway").seq == 1
    assert _session().append("message/received", {}, src="gateway").seq == 1


@pytest.mark.parametrize("bad", ["noslash", "/leading", "trailing/", "a/b/c", "-bad/x", "x/-bad"])
def test_a_type_that_is_not_domain_slash_action_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_TYPE) as exc:
        crew.append(bad, {}, src="gateway")
    assert _code(exc) == lg.CODE_BAD_TYPE


@pytest.mark.parametrize("src", ["gateway", "acp", "dashboard", "patrol", "session:slack:1712.5"])
def test_the_fixed_and_session_emitters_are_accepted(src):
    assert _crew().append("activity/tick", {}, src=src).src == src


@pytest.mark.parametrize("bad", ["", "Gate way", "session:", "crew:", "app:bad/name", "unknown"])
def test_an_unrecognized_src_is_refused(bad):
    crew = _crew()
    with _raises(lg.CODE_BAD_SRC) as exc:
        crew.append("activity/tick", {}, src=bad)
    assert _code(exc) == lg.CODE_BAD_SRC


# --- rule 2: guest namespaces ---------------------------------------------


def test_a_guest_writes_under_its_own_prefix_on_a_crew_ledger():
    crew = _crew()
    assert crew.append("crew:qa/report", {}, src="crew:qa").type == "crew:qa/report"
    assert crew.append("app:radar/scan", {}, src="app:radar").type == "app:radar/scan"


def test_a_session_ledger_refuses_a_guest_type_outright():
    session = _session()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        session.append("crew:qa/report", {}, src="crew:qa")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


def test_a_guest_may_not_write_another_guests_namespace():
    crew = _crew()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append("crew:other/report", {}, src="crew:qa")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


def test_a_guest_may_not_write_an_owned_domain_either():
    # Its own name is its whole permission; the registry is not also open to it.
    crew = _crew()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append("member/joined", {}, src="crew:qa")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION
    assert exc.value.field == "src"


def test_a_non_guest_emitter_may_not_borrow_a_guest_namespace():
    crew = _crew()
    with _raises(lg.CODE_NAMESPACE_VIOLATION) as exc:
        crew.append("crew:qa/report", {}, src="gateway")
    assert _code(exc) == lg.CODE_NAMESPACE_VIOLATION


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
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"type":"activity/tick","seq":'  # seq 2, terminated, unparseable
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    reopened = Ledger.open(lg.KIND_CREW, CREW)

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
    assert lg.ledger_path(lg.KIND_CREW, CREW).stat().st_size > 72 * 1024

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
    before = lg.ledger_path(lg.KIND_CREW, CREW).read_bytes()
    with _raises(lg.CODE_ENTRY_TOO_LARGE) as exc:
        crew.append("item/opened", {"blob": "x" * (lg.MAX_ENTRY_BYTES + 1)}, src="gateway")
    assert _code(exc) == lg.CODE_ENTRY_TOO_LARGE
    # Caps refuse; a refused append leaves the file byte-identical and does not
    # burn the seq it would have used.
    assert lg.ledger_path(lg.KIND_CREW, CREW).read_bytes() == before
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


def _raw_line(ledger: Ledger, payload: dict) -> None:
    """Append a line the library would never write, to test the READ side."""
    with ledger.path.open("a", encoding="utf-8") as handle:
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
    assert "known" not in inspect.signature(Ledger.page).parameters


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
    later = head.parent / "ledger.3.jsonl"
    lines = head.read_text(encoding="utf-8").splitlines(keepends=True)
    later.write_text(lines[0] + "".join(lines[3:]), encoding="utf-8")
    head.unlink()
    reopened = Ledger.open(lg.KIND_CREW, CREW)
    assert lg.segment_first_seqs(lg.KIND_CREW, CREW) == [3]
    found = reopened.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=4))
    assert found.status == lg.STATUS_PRUNED
    assert not found.ok


def test_a_gap_inside_a_surviving_segment_is_corrupt_not_pruned():
    # The distinction that matters: these lines SHOULD be there. Reporting
    # retention would tell a reader to stop looking for a file that is damaged.
    crew = _crew()
    for index in range(4):
        crew.append("activity/tick", {"i": index}, src="gateway")
    lines = crew.path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[2] = "{ this line is not json\n"  # seq 2, mid-file damage
    crew.path.write_text("".join(lines), encoding="utf-8")
    reopened = Ledger.open(lg.KIND_CREW, CREW)
    found = reopened.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=4))
    assert found.status == lg.STATUS_CORRUPT
    assert found.status != lg.STATUS_PRUNED
    assert found.status != lg.STATUS_GONE
    assert not found.ok
    # The readable lines still come back: a damaged span is reported, not hidden.
    assert [entry.seq for entry in found.entries] == [1, 3, 4]


def test_citing_past_the_newest_entry_is_neither_pruned_nor_corrupt():
    # A pointer at lines nobody has written yet is not damage.
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    found = crew.resolve(Ref(unit=lg.KIND_CREW, id=CREW, from_seq=1, to_seq=50))
    assert found.status == lg.STATUS_OK
    assert [entry.seq for entry in found.entries] == [1]


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


def test_page_of_an_empty_ledger_is_empty_with_no_cursor():
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


def test_resolve_returns_the_cited_segment_of_another_ledger():
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


def test_resolve_follows_a_ref_into_the_citing_ledger_itself():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    resolution = crew.resolve(Ref(lg.KIND_CREW, CREW, 1))
    assert resolution.ok and [entry.seq for entry in resolution.entries] == [1]


def test_resolve_is_gone_when_the_cited_ledger_does_not_exist():
    resolution = _crew().resolve(Ref(lg.KIND_SESSION, "s-missing", 1, 3))
    assert resolution.status == lg.STATUS_GONE
    assert resolution.entries == () and not resolution.ok


def test_resolve_has_exactly_two_outcomes_and_takes_no_access_callback():
    # This layer claims no authorization, so it has none to deny. A check that
    # defaulted to allow would make the shortest call shape the insecure one, and
    # a check with no permission model behind it only looks like a boundary, so
    # `forbidden` arrives with the first caller that HAS one.
    assert not hasattr(lg, "STATUS_FORBIDDEN")
    assert "may_read" not in inspect.signature(Ledger.resolve).parameters
    assert {lg.STATUS_OK, lg.STATUS_GONE} == {"ok", "gone"}


# --- damage tolerance -----------------------------------------------------


def test_a_torn_last_line_is_truncated_on_open():
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    intact = path.read_bytes()
    path.write_bytes(intact + b'{"type":"item/opened","seq":2,"tim')

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert path.read_bytes() == intact  # the crash artifact is gone, history is not
    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {}, src="gateway").seq == 2


def test_a_complete_last_line_missing_only_its_newline_is_kept():
    # Only the separator was lost, so the record is real; the next append
    # re-supplies the newline instead of rewriting the line.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {"next": True}, src="gateway").seq == 2
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1, 2]


def test_a_malformed_interior_line_is_skipped_on_read_and_left_on_disk():
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = '{"type":"activity/tick","seq":'  # terminated, so NOT torn
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert [entry.seq for entry in reopened.iter_from()] == [1, 3]
    assert reopened.last_seq == 3
    assert '{"type":"activity/tick","seq":' in path.read_text(encoding="utf-8")


def test_a_blank_interior_line_is_skipped():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_an_interior_line_whose_envelope_is_wrong_is_skipped():
    crew = _crew()
    crew.append("activity/tick", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_text(
        path.read_text(encoding="utf-8")
        + json.dumps({"type": "activity/tick", "seq": 2, "time": 1, "src": "gateway"})
        + "\n"
        + json.dumps({"type": "activity/tick", "seq": 3, "time": 1, "src": "g", "data": 4})
        + "\n",
        encoding="utf-8",
    )
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_an_interior_line_whose_bytes_are_not_utf8_is_skipped_not_altered():
    # The dangerous shape: invalid bytes INSIDE a JSON string, where a
    # replacement decode would still yield valid JSON. Handing a consumer a
    # value with U+FFFD substituted into it is worse than skipping the line --
    # the record calls itself the authority, so an altered value is a lie a
    # reader cannot detect.
    crew = _crew()
    crew.append("activity/tick", {"note": "intact"}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    damaged = (
        b'{"type":"activity/tick","seq":2,"time":1,"src":"gateway",'
        b'"data":{"note":"caf\xe9 latte"}}\n'
    )
    with open(path, "ab") as handle:
        handle.write(damaged)
    crew_reopened = Ledger.open(lg.KIND_CREW, CREW)

    entries = list(crew_reopened.iter_from())

    assert [entry.seq for entry in entries] == [1]
    assert all("\ufffd" not in str(entry.data) for entry in entries)
    assert damaged.strip() in path.read_bytes()  # skipped on read, kept on disk


def test_a_header_whose_bytes_are_not_utf8_refuses_the_open():
    _crew()
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_bytes(b'{"type":"crew","version":1,"id":"caf\xe9","createdAt":1}\n')
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_torn_tail_of_invalid_bytes_is_truncated_not_decoded():
    crew = _crew()
    crew.append("activity/tick", {"note": "intact"}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    intact = path.read_bytes()
    path.write_bytes(intact + b'{"type":"activity/tick","seq":2,"d\xff\xfe')

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert path.read_bytes() == intact
    assert reopened.last_seq == 1


def test_seq_recovers_from_the_newest_valid_line_when_the_tail_is_damaged():
    crew = _crew()
    for index in range(3):
        crew.append("activity/tick", {"i": index}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = "{damaged"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # The newest PARSEABLE seq is 2, so the next entry is 3 -- the damaged
    # line's number is not reused, because a reader cannot know it was 3.
    assert Ledger.open(lg.KIND_CREW, CREW).append("activity/tick", {}, src="gateway").seq == 3


def test_a_torn_tail_is_repaired_by_an_append_too_not_only_by_a_reopen():
    # A long-lived writer holds its handle open across a crash somewhere else,
    # so the repair cannot live only in ``open``.
    crew = _crew()
    crew.append("item/opened", {"kept": True}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_bytes(path.read_bytes() + b'{"type":"item/opened","seq":2,"tim')

    assert crew.append("item/opened", {"next": True}, src="gateway").seq == 2
    assert [entry.seq for entry in crew.iter_from()] == [1, 2]


def test_an_interior_line_carrying_a_malformed_ref_is_skipped():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    path.write_text(
        path.read_text(encoding="utf-8")
        + json.dumps(
            {
                "type": "item/opened",
                "seq": 2,
                "time": 1,
                "src": "gateway",
                "ref": {"unit": "swarm", "id": "x", "from": 1},
                "data": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert [entry.seq for entry in Ledger.open(lg.KIND_CREW, CREW).iter_from()] == [1]


def test_a_stored_session_thread_anchor_that_is_damaged_reads_as_absent():
    _session()
    path = lg.ledger_path(lg.KIND_SESSION, SESSION)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    header["thread"] = {"crew": CREW}
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    assert Ledger.open(lg.KIND_SESSION, SESSION).header.thread is None


@pytest.mark.parametrize("key,bad", [("createdAt", "yesterday"), ("version", "1")])
def test_a_header_field_of_the_wrong_stored_type_refuses_the_open(key, bad):
    _crew()
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    header[key] = bad
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_CREW, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_session_header_missing_its_owner_refuses_the_open():
    _session()
    path = lg.ledger_path(lg.KIND_SESSION, SESSION)
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    del header["owner"]
    path.write_text(json.dumps(header) + "\n", encoding="utf-8")
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_SESSION, SESSION)
    assert _code(exc) == lg.CODE_BAD_HEADER


def test_a_ledger_of_one_kind_cannot_be_opened_as_the_other():
    # The two roots are separate, so this needs the file moved -- which is
    # exactly what a mistaken restore or a hand-edit does.
    _crew()
    target = lg.ledger_dir(lg.KIND_SESSION, CREW)
    target.mkdir(parents=True, exist_ok=True)
    (target / lg.LEDGER_FILE).write_bytes(lg.ledger_path(lg.KIND_CREW, CREW).read_bytes())
    with _raises(lg.CODE_BAD_HEADER) as exc:
        Ledger.open(lg.KIND_SESSION, CREW)
    assert _code(exc) == lg.CODE_BAD_HEADER


# --- large files: the bounded tail read -----------------------------------


def test_seq_comes_off_a_bounded_window_on_a_file_past_that_window():
    # The window can begin mid-line, so its first fragment is not a record. If
    # that fragment were trusted the seq would come from a partial line.
    crew = _crew()
    for _ in range(3):  # three ~40 KiB lines clear the ~72 KiB window
        crew.append("item/opened", {"blob": "x" * 40_000}, src="gateway")
    assert crew.append("item/opened", {}, src="gateway").seq == 4
    assert Ledger.open(lg.KIND_CREW, CREW).last_seq == 4


def test_seq_falls_back_to_a_full_scan_when_the_window_holds_nothing_valid():
    crew = _crew()
    crew.append("item/opened", {"i": 0}, src="gateway")
    path = lg.ledger_path(lg.KIND_CREW, CREW)
    # One terminated garbage line wider than the tail window, so the window
    # sees only garbage and the real seq is behind it.
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("{" + "z" * 80_000 + "\n")

    reopened = Ledger.open(lg.KIND_CREW, CREW)

    assert reopened.last_seq == 1
    assert reopened.append("item/opened", {"i": 1}, src="gateway").seq == 2
    assert [entry.seq for entry in reopened.iter_from()] == [1, 2]


# --- interrupted-tail closers ---------------------------------------------


def _interrupted_session() -> Ledger:
    """A session ledger whose newest turn was cut off mid-tool-call."""
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user"}, src="gateway")
    led.append("tool/called", {"turn": 1, "call_id": "tc-1", "name": "fs_read"}, src="acp")
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    led.append("turn/started", {"turn": 2, "actor": "user"}, src="gateway")
    led.append("tool/called", {"turn": 2, "call_id": "tc-2", "name": "execute_bash"}, src="acp")
    led.append("tool/called", {"turn": 2, "call_id": "tc-3", "name": "fs_write"}, src="acp")
    return led


def test_a_plain_open_never_touches_an_interrupted_tail():
    # The bug this split exists to prevent. A live writer reconnects to its own
    # ledger for reasons that say nothing about its health -- a handle dropped from
    # a bounded cache is enough -- so an open that repaired would close a turn that
    # is still running and then let it keep writing past its own completion.
    _interrupted_session()
    before = _ledger_bytes()
    reopened = Ledger.open(lg.KIND_SESSION, SESSION)
    assert _ledger_bytes() == before
    assert [e.type for e in reopened.iter_from(1)][-1] == "tool/called"


def test_repair_is_reachable_from_a_handle_as_well_as_from_open():
    # The resume path may already hold a handle, so the same rule needs a method
    # form; a caller must not have to re-open just to ask for the repair.
    led = _interrupted_session()
    assert led.repair_interrupted_turn() == 3
    assert list(led.iter_from(1))[-1].data["stop_reason"] == "interrupted"
    # The handle's cached seq follows the closers, so its next append does not
    # collide with them.
    assert led.append("turn/started", {"turn": 3}, src="gateway").seq == led.last_seq


def test_an_interrupted_turn_is_closed_when_the_ledger_is_opened():
    # A crash leaves the newest turn open. Closing it in the record answers
    # "did this turn finish or did its writer die" once, instead of leaving every
    # reader to carry the same special case.
    _interrupted_session()
    reopened = Ledger.open(lg.KIND_SESSION, SESSION, repair=True)
    body = list(reopened.iter_from(1))
    assert [e.type for e in body[-3:]] == [
        "tool/completed",
        "tool/completed",
        "turn/completed",
    ]
    assert body[-1].data == {"turn": 2, "stop_reason": "interrupted"}


def test_a_closer_names_every_unmatched_call_in_first_seen_order():
    _interrupted_session()
    body = list(Ledger.open(lg.KIND_SESSION, SESSION, repair=True).iter_from(1))
    closers = [e for e in body if e.type == "tool/completed"]
    assert [e.data["call_id"] for e in closers] == ["tc-2", "tc-3"]
    assert all(e.data["status"] == "unknown" for e in closers)
    # The trusted identity the call frame carried rides along, so the closer
    # joins to its call without a reader having to fold backwards for it.
    assert closers[0].data["name"] == "execute_bash"


def test_closers_reuse_the_last_real_entrys_time_and_continue_seq():
    # A closer describes what happened when the writer STOPPED. Stamping it with
    # the clock at open time would put a gap of arbitrary length inside a turn
    # and make any duration computed off these entries a measure of downtime.
    led = _interrupted_session()
    last_real = list(led.iter_from(1))[-1]
    body = list(Ledger.open(lg.KIND_SESSION, SESSION, repair=True).iter_from(1))
    closers = body[-3:]
    assert {e.time for e in closers} == {last_real.time}
    assert [e.seq for e in closers] == [last_real.seq + 1, last_real.seq + 2, last_real.seq + 3]


def test_closing_is_deterministic_and_a_balanced_tail_is_left_alone():
    _interrupted_session()
    closed = Ledger.open(lg.KIND_SESSION, SESSION, repair=True)
    after_first = _ledger_bytes()
    # The tail is balanced now, so a second open must add nothing at all --
    # otherwise every open would grow the file.
    again = Ledger.open(lg.KIND_SESSION, SESSION, repair=True)
    assert _ledger_bytes() == after_first
    assert closed.last_seq == again.last_seq
    assert list(again.iter_from(1))[-1].data["stop_reason"] == "interrupted"


def test_only_a_session_ledger_is_closed_this_way():
    # The closers encode the SESSION turn lifecycle, so the repair is gated on
    # kind rather than on what the tail happens to look like: a crew ledger's
    # types are its own, and its reader must not find turn entries it never wrote.
    # Asserted on the gate directly, because the ownership rule refuses to let a
    # crew ledger hold a `turn/started` in the first place.
    _interrupted_session()
    path = lg.ledger_path(lg.KIND_SESSION, SESSION)
    before = _ledger_bytes()
    assert store._close_interrupted_tail(lg.KIND_CREW, SESSION, path) == 0
    assert _ledger_bytes() == before
    assert store._close_interrupted_tail(lg.KIND_SESSION, SESSION, path) == 3


def test_a_completed_turns_unmatched_call_is_left_open():
    # Scoped to the OPEN turn on purpose: an unmatched call inside a turn that
    # did complete is a different anomaly, and inventing a result for it would be
    # this reader editing history it was not asked about.
    led = _session()
    led.append("turn/started", {"turn": 1, "actor": "user"}, src="gateway")
    led.append("tool/called", {"turn": 1, "call_id": "tc-1", "name": "fs_read"}, src="acp")
    led.append("turn/completed", {"turn": 1, "stop_reason": "end_turn"}, src="acp")
    before = _ledger_bytes()
    Ledger.open(lg.KIND_SESSION, SESSION, repair=True)
    assert _ledger_bytes() == before


# --- containment ----------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_an_id_that_symlinks_out_of_its_root_is_refused():
    # The shape gate cannot see this one: the id has no separator, the ESCAPE is
    # in the filesystem. Containment is re-checked on the resolved path.
    _crew()
    root = lg.ledger_root(lg.KIND_CREW)
    outside = root.parent / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    (root / _store_name("escape")).symlink_to(outside, target_is_directory=True)
    with _raises(lg.CODE_INVALID_ID) as exc:
        _crew("escape")
    assert _code(exc) == lg.CODE_INVALID_ID


def test_the_root_is_established_owner_only_before_any_ledger_exists(monkeypatch, tmp_path):
    # The two other protections are stated per PATH and both are weaker while the
    # name is absent: the Linux bind-mask skips a leaf that does not exist, and an
    # absent directory has no mode to inherit. Establishing the root with the home
    # makes the guarantee a property of the home rather than of the first write.
    home = tmp_path / "fresh-home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    paths._config_dir_memo = None
    ensure_data_home()
    root = home / "ledgers"
    assert root.is_dir(), "no ledger has been written, yet the root exists"
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_an_existing_root_with_a_loose_mode_is_tightened(monkeypatch, tmp_path):
    # mkdir's mode is a no-op on a directory that already exists, so a home
    # restored from a backup keeps whatever mode it arrived with unless startup
    # asserts the mode as well as the creation.
    if os.name != "posix":
        pytest.skip("POSIX mode semantics")
    home = tmp_path / "restored-home"
    (home / "ledgers").mkdir(parents=True)
    # Deliberately group- and world-readable: that is the state a restored backup
    # or an older build leaves behind, and the point is that startup re-asserts
    # owner-only over it rather than accepting what it found.
    (home / "ledgers").chmod(
        stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
    )
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    paths._config_dir_memo = None
    ensure_data_home()
    assert stat.S_IMODE((home / "ledgers").stat().st_mode) == 0o700


# --- sandbox disposition --------------------------------------------------


def test_every_ledger_is_refused_to_the_agents_own_file_tools():
    # The ledger is the AUTHORITY a conductor reads instead of re-deriving, so an
    # agent able to write here could forge an entry attributed to the gateway or
    # rewrite the history it is reporting into. The write-side rules bind callers
    # who go through the library; this floor is what stops a file tool going
    # around it. BOTH kinds, and the subpath is asked of the library rather than
    # spelled out, so renaming a root cannot leave this asserting a path nothing
    # writes.
    for kind in (lg.KIND_CREW, lg.KIND_SESSION):
        rel = lg.ledger_path(kind, "anything").relative_to(data_home()).as_posix()
        for home in (".kiro/crew", ".kirocrew"):
            target = f"~/{home}/{rel}"
            assert is_sensitive_path(target), target


def test_every_ledger_is_masked_from_every_sandboxed_process():
    # One entry at the shared root covers every kind: the file-tool floor above
    # answers the agent's own tools, and this answers a spawned subprocess that
    # calls open() directly -- which no tool gate sees.
    assert "ledgers" in sandbox._CREW_HIDDEN_LEAVES


def test_a_session_ledger_lives_under_the_masked_leaf_not_the_transcript_root():
    # Why the session half needs its own root. Under `sessions/` a ledger has
    # NEITHER fence: that root is the flat transcript store and carries no mask
    # entry, so a sandboxed subprocess can forge a session's history whatever the
    # tool gate says. Asserted on the real created path rather than a root's
    # name -- a name check passes just as happily one directory above the masked
    # leaf.
    path = _session().path
    assert (data_home() / "ledgers") in path.parents
    assert (data_home() / "sessions") not in path.parents


# --- segments: the retention shape ---------------------------------------- #


def _split_into_segments(kind: str, unit_id: str, at_seq: int) -> None:
    """Move every entry from *at_seq* onward into ``ledger.<at_seq>.jsonl``.

    Stands in for a writer that rotates, which nothing does yet: the reader side
    is what this commit freezes, so the fixture produces the on-disk shape a
    future rotation would leave.
    """
    head = lg.ledger_path(kind, unit_id)
    lines = head.read_bytes().splitlines(keepends=True)
    header, entries = lines[0], lines[1:]
    kept, moved = [], []
    for raw in entries:
        (moved if json.loads(raw)["seq"] >= at_seq else kept).append(raw)
    head.write_bytes(header + b"".join(kept))
    (head.parent / f"ledger.{at_seq}.jsonl").write_bytes(header + b"".join(moved))


def test_segments_are_found_and_ordered_by_their_first_seq(tmp_path):
    led = _session("seg-order")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-order", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-order", 5)

    names = [p.name for p in lg.segment_paths(lg.KIND_SESSION, "seg-order")]
    assert names == ["ledger.jsonl", "ledger.5.jsonl", "ledger.9.jsonl"], (
        "segments must order by first seq, not by name -- ledger.9 sorts before "
        "ledger.5 as a string"
    )


def test_a_read_spans_segments_in_order(tmp_path):
    led = _session("seg-read")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-read", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-read", 5)

    reader = lg.Ledger.open(lg.KIND_SESSION, "seg-read")
    seen = list(reader.iter_from(1))
    assert [e.seq for e in seen] == list(range(1, 13))
    assert [e.data["turn"] for e in seen] == list(range(1, 13))


def test_dropping_the_oldest_segments_is_retention_not_damage(tmp_path):
    """The whole point of segments: retention deletes files, never rewrites one.

    A reader of a pruned log sees fewer entries and no error, because the lines
    that remain are byte-identical to the ones that were written.
    """
    led = _session("seg-prune")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n}, src="gateway")
    _split_into_segments(lg.KIND_SESSION, "seg-prune", 9)
    lg.ledger_path(lg.KIND_SESSION, "seg-prune").unlink()

    reader = lg.Ledger.open(lg.KIND_SESSION, "seg-prune")
    seen = list(reader.iter_from(1))
    assert [e.seq for e in seen] == [9, 10, 11, 12]


def test_a_gap_between_segments_is_refused_rather_than_read_across(tmp_path):
    # A hole in the MIDDLE is damage or a partial copy. Yielding across it would
    # hand a fold consecutive entries that are not consecutive facts.
    led = _session("seg-gap")
    for n in range(1, 13):
        led.append("turn/started", {"turn": n}, src="gateway")
    # Newest boundary first: each split re-reads the head, so splitting low then
    # high would leave the second segment empty.
    _split_into_segments(lg.KIND_SESSION, "seg-gap", 9)
    _split_into_segments(lg.KIND_SESSION, "seg-gap", 5)
    (lg.ledger_dir(lg.KIND_SESSION, "seg-gap") / "ledger.5.jsonl").unlink()

    reader = lg.Ledger.open(lg.KIND_SESSION, "seg-gap")
    with pytest.raises(LedgerError) as excinfo:
        list(reader.iter_from(1))
    assert excinfo.value.code == lg.CODE_SEGMENT_GAP


def test_a_neighbour_file_sharing_the_prefix_is_ignored_not_refused(tmp_path):
    # An editor backup or a stray copy must not make a readable ledger unreadable.
    led = _session("seg-neighbour")
    for n in range(1, 5):
        led.append("turn/started", {"turn": n}, src="gateway")
    directory = lg.ledger_dir(lg.KIND_SESSION, "seg-neighbour")
    (directory / "ledger.backup.jsonl").write_text("not a segment\n")

    assert [p.name for p in lg.segment_paths(lg.KIND_SESSION, "seg-neighbour")] == ["ledger.jsonl"]
    reader = lg.Ledger.open(lg.KIND_SESSION, "seg-neighbour")
    assert [e.seq for e in reader.iter_from(1)] == [1, 2, 3, 4]


def test_a_chmod_refusing_filesystem_warns_once_not_once_per_append(monkeypatch, caplog):
    # Every append goes through the private-mkdir helper, so a filesystem that
    # refuses chmod would log a full traceback per entry. That buries the very
    # entries the warning is about, and one true fact about the host does not
    # become truer by being repeated.
    ledger = _session("flood-check")
    monkeypatch.setattr(store, "_restrict_failed", set())
    # Leave the directory loose so the restriction is actually attempted; when it
    # is already owner-only the helper skips the chmod entirely.
    if os.name == "posix":
        ledger.path.parent.chmod(0o755)

    def _refuse(directory):
        raise OSError("chmod not supported on this filesystem")

    monkeypatch.setattr(store, "restrict_dir_to_owner", _refuse)
    with caplog.at_level("WARNING", logger=store.__name__):
        for turn in range(1, 6):
            ledger.append("turn/started", {"turn": turn}, src="gateway")

    warnings = [r for r in caplog.records if "owner-only" in r.getMessage()]
    assert len(warnings) == 1, f"{len(warnings)} warnings for 5 appends, expected 1"
    # The appends themselves still succeed: the restriction is best-effort.
    body = _ledger_bytes("session", "flood-check").decode("utf-8").splitlines()
    assert len([line for line in body if '"turn/started"' in line]) == 5


def test_an_already_restricted_directory_is_not_chmodded_again():
    # The mode is checked before it is set, so the steady state costs one stat
    # instead of a syscall that changes nothing on every entry.
    ledger = _session("skip-check")
    ledger.append("turn/started", {"turn": 1}, src="gateway")
    calls: list[object] = []
    real = store.restrict_dir_to_owner

    def _count(directory):
        calls.append(directory)
        real(directory)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "restrict_dir_to_owner", _count)
        ledger.append("turn/completed", {"turn": 1}, src="gateway")

    if os.name == "posix":
        assert calls == [], f"re-chmodded an already owner-only directory: {calls}"


def test_the_ledger_root_is_restricted_even_when_the_home_cannot_be(tmp_path, monkeypatch):
    # The two tightenings are independent claims. Skipping the ledger root when the
    # home's own chmod fails inverts the priority: that is the case where the root's
    # mode is the ONLY boundary left, because an unreadable parent is not there to
    # do the work -- and every lazily created ledger directory below it would keep
    # the process umask.
    from kiro_crew import platform_compat

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(paths, "config_dir", lambda: home)
    monkeypatch.setattr(paths, "private_runtime_log_dir", lambda: None)

    real = platform_compat.restrict_dir_to_owner
    refused: list[object] = []

    def _refuse_the_home(directory):
        if directory == home:
            refused.append(directory)
            raise OSError("read-only filesystem")
        real(directory)

    monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", _refuse_the_home)

    got = paths.ensure_data_home()

    assert got == home
    assert refused == [home], "the test did not exercise the failing branch"
    root = home / "ledgers"
    assert root.is_dir(), "the ledger root was skipped because the home's chmod failed"
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_a_lazily_created_ledger_directory_is_not_world_readable():
    # The eager root is best-effort, so the per-unit directories the store creates
    # on first write assert the same thing for themselves rather than trusting a
    # parent that may not have been tightened.
    ledger = _session("hardening-check")
    ledger.append("turn/started", {"turn": 1}, src="gateway")

    if os.name == "posix":
        mode = stat.S_IMODE(ledger.path.parent.stat().st_mode)
        assert mode == 0o700, f"the ledger directory is {oct(mode)}, not owner-only"


def test_a_ledger_that_exists_but_will_not_open_is_damage_not_absence():
    # `gone` means "there is no such ledger" and tells a reader to stop looking. A
    # ledger whose header is corrupt is right there and broken, so answering
    # `gone` for it converts a recoverable alarm into silence.
    src = _session()
    victim = _session("other-session")
    entry = victim.append("turn/started", {"turn": 1}, src="gateway")
    ref = lg.Ref(unit="session", id="other-session", from_seq=entry.seq)
    lines = victim.path.read_text(encoding="utf-8").splitlines()
    lines[0] = '{"broken":'
    victim.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    got = src.resolve(ref)
    assert got.status == lg.STATUS_CORRUPT, f"a damaged header reported {got.status!r}"


def test_a_citation_past_a_stale_cached_tail_is_not_called_ok():
    # A second writer advances the file, so an older handle's cached `last_seq`
    # lags. Clamping the expected count to it computes zero expected lines, which
    # answers `ok` for a citation that cannot actually be read.
    first = _session()
    first.append("turn/started", {"turn": 1}, src="gateway")
    stale_tail = first.last_seq

    second = Ledger.open("session", SESSION)
    newer = second.append("turn/completed", {"turn": 1}, src="gateway")
    assert newer.seq > stale_tail

    ref = lg.Ref(unit="session", id=SESSION, from_seq=newer.seq)
    got = first.resolve(ref)
    # The line EXISTS, so this must resolve it rather than shrug at it.
    assert got.status == lg.STATUS_OK, f"a readable line reported {got.status!r}"
    assert [e.seq for e in got.entries] == [newer.seq]


# --- a group whose members are meaningless apart --------------------------


def test_a_group_is_written_contiguously_with_its_citing_entry_last():
    """`append_many` allocates one contiguous seq run and writes it once.

    A body too large for one line becomes chunk entries plus the entry that CITES
    their seqs. Appended one at a time, a hard kill between them leaves the chunks on
    disk with nothing pointing at them: the body is stored and unreachable, and the
    message it belongs to has no record at all. One write leaves either the whole
    group or a torn tail, which the next append truncates.
    """
    session = _session()
    planned = session.plan_group_seqs(3)
    written = session.append_many(
        [
            {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
            {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
            {
                "type": "message/sent",
                "data": {"turn": 1, "chunks": planned[:2], "chars": 4},
            },
        ],
        src="acp",
    )

    assert [e.seq for e in written] == planned, "the plan did not match what was assigned"
    assert [e.seq for e in written] == [written[0].seq + n for n in range(3)], "seqs have a gap"
    assert written[-1].type == "message/sent", "the citing entry is not last"
    assert written[-1].data["chunks"] == [written[0].seq, written[1].seq]
    # The chunks are skippable by a reader that does not know them; the citing entry
    # is not, because a fold that skipped it would lose the message.
    assert written[0].ignorable and written[1].ignorable
    assert not written[-1].ignorable
    assert session.last_seq == written[-1].seq


def test_a_group_is_refused_whole_and_leaves_the_file_identical():
    """Every check happens before a byte is written, as for a single append."""
    session = _session()
    session.append("turn/started", {"turn": 1}, src="acp")
    before = lg.ledger_path(lg.KIND_SESSION, SESSION).read_bytes()

    with _raises("bad_src"):
        session.append_many(
            [
                {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}},
                {"type": "item/opened", "data": {"turn": 1}},  # not a session type
            ],
            src="acp",
        )

    assert lg.ledger_path(lg.KIND_SESSION, SESSION).read_bytes() == before


def test_repair_drops_a_chunk_group_whose_citing_entry_never_landed(caplog):
    """The torn-tail case the batch cannot rule out, and its bounded residual.

    One write still tears, and the citing entry is the last line -- so what a hard
    kill can leave is chunks nothing points at. Dropping them costs the one message
    that was mid-write, the same residual as any single entry lost the same way, and
    it leaves the file free of lines whose only purpose was to be cited by an entry
    that does not exist.
    """
    session = _session()
    session.append("turn/started", {"turn": 1}, src="acp")
    # Exactly the shape a torn group leaves: chunks, and then nothing.
    orphans = session.append_many(
        [
            {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
            {"type": "message/chunk", "data": {"turn": 1, "delta": "bb"}, "ignorable": True},
        ],
        src="acp",
    )
    path = lg.ledger_path(lg.KIND_SESSION, SESSION)
    assert "message/chunk" in path.read_text(encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.ledger.store"):
        Ledger.open(lg.KIND_SESSION, SESSION, repair=True)

    body = path.read_text(encoding="utf-8")
    assert "message/chunk" not in body, "the unreachable chunks were left in the file"
    named = [
        r
        for r in caplog.records
        if "unreachable chunk group" in r.getMessage()
        and f"{orphans[0].seq}-{orphans[-1].seq}" in r.getMessage()
    ]
    assert len(named) == 1, "the drop was silent or did not name the seq range"
    # Still append-only, and with no hole: the repair's own closers reuse the seqs the
    # dropped chunks had held, so a fold sees a contiguous run rather than a gap.
    reopened = Ledger.open(lg.KIND_SESSION, SESSION)
    seqs = [e.seq for e in reopened.iter_from(1)]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq is not contiguous: {seqs}"
    assert seqs[0] == 1 and orphans[0].seq in seqs, "the truncated range was not reused"


def test_repair_keeps_a_chunk_group_that_was_completed():
    """A group followed by any entry landed whole -- it is not an orphan."""
    session = _session()
    session.append("turn/started", {"turn": 1}, src="acp")
    planned = session.plan_group_seqs(2)
    session.append_many(
        [
            {"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True},
            {"type": "message/sent", "data": {"turn": 1, "chunks": planned[:1], "chars": 2}},
        ],
        src="acp",
    )

    Ledger.open(lg.KIND_SESSION, SESSION, repair=True)
    body = lg.ledger_path(lg.KIND_SESSION, SESSION).read_text(encoding="utf-8")
    assert "message/chunk" in body, "a cited chunk was dropped"
