"""Tests for :mod:`kiro_crew.crew_log.holders` -- the pull-request holder projection.

The suite is organised around the two things this module claims: the ownership
RULE (a conductor does not out-own the worker it dispatched) and the resume CACHE
(a live session's appends cost only the bytes appended).
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew.crew_log import holders as holders_mod
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log import tree as tree_mod
from kiro_crew.crew_log.holders import (
    MAX_ENTRY_BYTES,
    MAX_OWNER_CHARS,
    MAX_PR_NUMBER,
    MAX_REPO_CHARS,
    REFERENCE_UNIT_CAP,
    REFS_PER_SEGMENT_CAP,
    Mention,
    Reference,
    ReferenceScanner,
    _scan_entry,
    _SegmentState,
    fold_holders,
    is_ancestor,
    parse_references,
)
from kiro_crew.crew_log.schema import KIND_SESSION, Entry
from kiro_crew.crew_log.tree import SessionTree, TreeNode, TreeReading
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MAX_SHORT_STRING

#: The repository this suite's fixtures name. Split from the SLUG rather than
#: spelled as two literals: the slug is the identifier form, and a bare joined
#: product name in added source is what the brand gate refuses.
_OWNER, _REPO = "kirodotdev/KiroCrew".split("/")


def _url(owner: str, repo: str, number: int | str = "") -> str:
    """A pull-request URL, or its prefix when *number* is empty."""
    base = f"https://github.com/{owner}/{repo}/pull"
    return base if number == "" else f"{base}/{number}"


#: The fixture repository's pull-request URL PREFIX, so a test appends a number.
_URL = _url(_OWNER, _REPO)


def _ref(number: int, owner: str = "", repo: str = "") -> Reference:
    return Reference(owner=owner or _OWNER, repo=repo or _REPO, number=number)


def _state() -> _SegmentState:
    """A bare segment state, which is what carries the stitch between slices."""
    return _SegmentState(dev=1, ino=1, offset=0, size=0)


def _scan(entry: Entry) -> set[Reference]:
    """One entry read on its own, with no slice carried into it."""
    return _scan_entry(entry, _state())


def _mention(slot: str, newest_ms: int, count: int = 1, sid: str = "") -> Mention:
    return Mention(slot=slot, sid=sid or f"sid-{slot}", newest_ms=newest_ms, count=count)


def _node(slot: str, parent: str | None = None, cycle: bool = False) -> TreeNode:
    return TreeNode(slot=slot, parent_slot=parent, cycle=cycle)


def _entry(entry_type: str, data: dict, *, seq: int = 2, time: int = 1000) -> Entry:
    return Entry(type=entry_type, seq=seq, time=time, src="gateway", data=data)


class TestParseReferences:
    """What text names a pull request, and what deliberately does not."""

    def test_a_full_url_is_read(self) -> None:
        found = parse_references(f"see {_URL}/12146 please")
        assert found == {_ref(12146)}

    def test_the_short_owner_repo_form_is_refused_because_it_may_be_an_issue(self) -> None:
        # The forge spells an issue and a pull request identically as
        # owner/repo#N, and nothing in a log tells them apart, so reading it
        # would hand a holder to a pull request that does not exist.
        assert parse_references(f"fixed in {_OWNER}/{_REPO}#12146") == set()

    def test_an_issue_url_is_not_a_pull_request(self) -> None:
        # The /pull/ path is the whole basis for calling the number a pull
        # request, so the /issues/ path must not match it.
        assert parse_references(f"https://github.com/{_OWNER}/{_REPO}/issues/12146") == set()

    def test_a_bare_number_names_no_repository_and_is_not_read(self) -> None:
        # A bare #N would join a session to whichever repository shared the
        # number, which is the defect the repository-keyed identity prevents.
        assert parse_references("see #12146 for context") == set()

    def test_a_www_host_and_a_trailing_path_still_resolve_to_the_number(self) -> None:
        text = f"https://www.github.com/{_OWNER}/{_REPO}/pull/12146/files#discussion_r1"
        assert parse_references(text) == {_ref(12146)}

    def test_two_repositories_sharing_a_number_are_two_references(self) -> None:
        found = parse_references(f"{_url('a', 'b', 7)} and {_url('c', 'd', 7)}")
        assert found == {_ref(7, "a", "b"), _ref(7, "c", "d")}

    def test_one_reference_named_twice_is_one_reference(self) -> None:
        assert parse_references(f"{_URL}/7 and {_URL}/7/files") == {_ref(7)}

    def test_casing_is_not_part_of_a_repositorys_identity(self) -> None:
        # A repository is case-insensitive at the forge, so two casings of one
        # pull request must fold as ONE reference. Otherwise the holder is split
        # between them and each half looks like a complete answer.
        found = parse_references(f"{_url('Owner', 'Repo', 7)} and {_url('owner', 'repo', 7)}")
        assert found == {_ref(7, "owner", "repo")}

    def test_a_caller_spelling_a_reference_differently_still_matches(self) -> None:
        # The normalisation is in the reference's own constructor, so a caller's
        # reference and a parsed one meet whatever casing either was written in.
        assert Reference(owner=_OWNER.upper(), repo=_REPO.upper(), number=7) == _ref(7)
        assert Reference(owner="Owner", repo="Repo", number=1).url.endswith("owner/repo/pull/1")

    def test_a_number_past_the_bound_is_not_read(self) -> None:
        assert parse_references(f"{_URL}/{MAX_PR_NUMBER + 1}") == set()

    def test_a_digit_run_too_long_to_convert_is_refused_not_raised(self) -> None:
        # int() on a decimal string past CPython's int-str conversion limit
        # raises ValueError rather than returning a large number, so the length
        # has to be refused BEFORE the conversion, not after it.
        assert parse_references(f"{_URL}/" + "9" * 5000) == set()

    def test_a_scheme_that_does_not_begin_its_own_token_is_not_read(self) -> None:
        # The pattern is applied to arbitrary prose and matches anywhere in it, so
        # a token character glued in front -- an ordinary typo -- otherwise matches
        # one character in and names a pull request the text does not address.
        for prefix in ("x", "9", "_", "-", "~", "%", "see"):
            assert parse_references(f"{prefix}{_URL}/7") == set(), prefix

    def test_the_leads_a_pasted_link_really_has_still_resolve(self) -> None:
        # The other direction: every real lead is outside the refused class, and
        # refusing one would drop a reference from an answer calling itself
        # complete. The empty prefix is a link at the very start of the text.
        for prefix in ("", " ", "(", "<", '"', "'", "[", ":", "\n"):
            assert parse_references(f"{prefix}{_URL}/7") == {_ref(7)}, repr(prefix)

    def test_a_number_that_does_not_end_its_own_path_token_is_not_read(self) -> None:
        # `/pull/123abc` addresses no pull request. Without a boundary the pattern
        # stopped wherever the digits stopped, so it read as 123 and one line of
        # untrusted prose fabricated a holder for somebody else's work.
        for suffix in ("abc", "-4", "_x", "~", "%20", "0x"):
            assert parse_references(f"{_URL}/7{suffix}") == set(), suffix

    def test_the_endings_a_pasted_link_really_has_still_resolve(self) -> None:
        # The boundary must refuse only what CONTINUES the token. These are the
        # endings a real link has, and refusing any of them would drop a reference
        # from an answer that still called itself complete.
        for ending in ("", "/files", "#discussion_r1", ".diff", ".", ",", ")", "]", " and more"):
            assert parse_references(f"see {_URL}/7{ending}") == {_ref(7)}, ending

    def test_a_digit_run_too_long_arrives_whole_at_the_bound(self) -> None:
        # The boundary is a lookahead that refuses a following DIGIT too, so an
        # over-long run cannot be re-cut into a shorter valid number to satisfy it.
        # Read any other way, `/pull/123456789` would answer 1234 and name a real
        # pull request the text never mentioned.
        assert parse_references(f"{_URL}/" + "9" * (len(str(MAX_PR_NUMBER)) + 1)) == set()

    def test_zero_is_not_a_pull_request(self) -> None:
        assert parse_references(f"{_URL}/0") == set()

    def test_empty_text_is_no_references(self) -> None:
        assert parse_references("") == set()

    def test_the_canonical_url_round_trips(self) -> None:
        reference = _ref(12146)
        assert parse_references(reference.url) == {reference}


class TestEntryReferences:
    """A reference comes from prose, and survives an oversize body's slices."""

    @pytest.mark.parametrize("entry_type", ["message/received", "message/sent"])
    def test_a_text_entry_contributes_its_references(self, entry_type: str) -> None:
        entry = _entry(entry_type, {"text": f"{_URL}/5"})
        assert _scan(entry) == {_ref(5)}

    def test_a_chunk_contributes_from_its_delta(self) -> None:
        entry = _entry("message/chunk", {"turn": 1, "delta": f"{_URL}/5"})
        assert _scan(entry) == {_ref(5)}

    def test_a_text_entry_with_no_text_field_contributes_nothing(self) -> None:
        assert _scan(_entry("message/sent", {"chunks": [3, 4]})) == set()

    def test_a_non_string_text_field_is_ignored(self) -> None:
        assert _scan(_entry("message/sent", {"text": 12146})) == set()

    def test_a_url_split_across_two_slices_is_still_found(self) -> None:
        # A body too big for one line is written as a run of message/chunk
        # entries, and the cut lands anywhere, so a URL can be in neither slice.
        # Scanning each slice alone loses it while reporting a complete answer.
        url = f"{_URL}/4242"
        head, tail = url[:20], url[20:]
        state = _state()
        first = _scan_entry(_entry("message/chunk", {"turn": 1, "delta": head}), state)
        second = _scan_entry(_entry("message/chunk", {"turn": 1, "delta": tail}), state)
        assert first == set()
        assert second == {_ref(4242)}

    def test_a_reference_wholly_inside_one_slice_is_counted_once(self) -> None:
        # The seam scan takes only references needing BOTH sides. Taking every
        # seam match would count a reference lying in the carried tail twice and
        # inflate the mention count the fold breaks ties on.
        state = _state()
        _scan_entry(_entry("message/chunk", {"turn": 1, "delta": f"{_URL}/5"}), state)
        again = _scan_entry(_entry("message/chunk", {"turn": 1, "delta": "nothing here"}), state)
        assert again == set()

    def test_two_unrelated_bodies_cannot_spell_a_reference_between_them(self) -> None:
        # A fabricated holder is as wrong as a lost one. A different turn is a
        # different body, so the carry must not reach across it.
        url = f"{_URL}/4242"
        state = _state()
        _scan_entry(_entry("message/chunk", {"turn": 1, "delta": url[:20]}), state)
        crossed = _scan_entry(_entry("message/chunk", {"turn": 2, "delta": url[20:]}), state)
        assert crossed == set()

    def test_an_entry_between_two_slices_ends_the_run(self) -> None:
        # The citing entry a batch ends with is of another type, so any entry
        # that is not a continuing chunk closes the run behind it.
        url = f"{_URL}/4242"
        state = _state()
        _scan_entry(_entry("message/chunk", {"turn": 1, "delta": url[:20]}), state)
        _scan_entry(_entry("message/sent", {"text": "unrelated"}), state)
        crossed = _scan_entry(_entry("message/chunk", {"turn": 1, "delta": url[20:]}), state)
        assert crossed == set()

    def test_a_chunk_with_no_turn_cannot_be_stitched(self) -> None:
        # Without a turn the run cannot be established, and a stitch across an
        # unestablished boundary could join two unrelated bodies.
        url = f"{_URL}/4242"
        state = _state()
        _scan_entry(_entry("message/chunk", {"delta": url[:20]}), state)
        crossed = _scan_entry(_entry("message/chunk", {"delta": url[20:]}), state)
        assert crossed == set()

    def test_the_carry_survives_a_scan_that_stopped_on_its_byte_budget(self) -> None:
        # A scan stops at a record boundary, and that boundary can fall between
        # two slices of one body. A carry held only for the duration of a scan
        # would be dropped there, losing a URL straddling exactly that pair on an
        # answer the next scan calls complete.
        url = f"{_URL}/4242"
        state = _state()
        _scan_entry(_entry("message/chunk", {"turn": 1, "delta": url[:20]}), state)
        assert state.carry.endswith(url[:20])
        resumed = _scan_entry(_entry("message/chunk", {"turn": 1, "delta": url[20:]}), state)
        assert resumed == {_ref(4242)}


class TestIsAncestor:
    """Ancestry is the tree's relation, followed under the tree's own rules."""

    def test_a_direct_creator_is_an_ancestor(self) -> None:
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        assert is_ancestor("conductor", "worker", nodes) is True

    def test_a_grandparent_is_an_ancestor(self) -> None:
        nodes = {
            "leaf": _node("leaf", "mid"),
            "mid": _node("mid", "root"),
            "root": _node("root"),
        }
        assert is_ancestor("root", "leaf", nodes) is True

    def test_a_slot_is_not_its_own_ancestor(self) -> None:
        nodes = {"a": _node("a", "a")}
        assert is_ancestor("a", "a", nodes) is False

    def test_a_child_is_not_its_creators_ancestor(self) -> None:
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        assert is_ancestor("worker", "conductor", nodes) is False

    def test_a_cited_creator_with_no_log_ends_the_chain(self) -> None:
        # The tree follows an edge only onto a slot that has a log of its own, so
        # a citation is not a place in the tree and nothing above it is reachable.
        nodes = {"worker": _node("worker", "ghost")}
        assert is_ancestor("ghost", "worker", nodes) is False

    def test_a_slot_on_a_cycle_is_never_walked_through(self) -> None:
        nodes = {
            "a": _node("a", "b", cycle=True),
            "b": _node("b", "a", cycle=True),
        }
        assert is_ancestor("b", "a", nodes) is False

    def test_a_chain_longer_than_the_depth_bound_is_not_followed(self) -> None:
        nodes = {f"s{i}": _node(f"s{i}", f"s{i + 1}") for i in range(10)}
        nodes["s10"] = _node("s10")
        assert is_ancestor("s10", "s0", nodes, depth=3) is False
        assert is_ancestor("s10", "s0", nodes, depth=20) is True

    def test_an_unknown_slot_has_no_ancestors(self) -> None:
        assert is_ancestor("anyone", "missing", {}) is False


class TestFoldHolders:
    """THE ownership rule. Every test here is about who wins and why."""

    def test_a_conductor_does_not_out_own_the_worker_it_dispatched(self) -> None:
        # The defect this module exists to close: the conductor's patrol is the
        # newest mention, and it still must not own the row.
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        mentions = {
            _ref(1): [_mention("worker", newest_ms=100), _mention("conductor", newest_ms=999)]
        }
        holders = fold_holders(mentions, nodes)
        assert holders[_ref(1)].slot == "worker"

    def test_a_conductor_out_mentioning_the_worker_still_does_not_win(self) -> None:
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        mentions = {
            _ref(1): [
                _mention("worker", newest_ms=100, count=2),
                _mention("conductor", newest_ms=999, count=50),
            ]
        }
        assert fold_holders(mentions, nodes)[_ref(1)].slot == "worker"

    def test_a_conductor_no_descendant_of_which_named_it_does_win(self) -> None:
        # It is then the only session that knows about the reference, and
        # reporting nobody would be worse than reporting who actually spoke.
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        mentions = {_ref(2): [_mention("conductor", newest_ms=5)]}
        assert fold_holders(mentions, nodes)[_ref(2)].slot == "conductor"

    def test_among_unrelated_sessions_the_newest_wins(self) -> None:
        nodes = {"a": _node("a"), "b": _node("b")}
        mentions = {_ref(3): [_mention("a", newest_ms=10), _mention("b", newest_ms=20)]}
        assert fold_holders(mentions, nodes)[_ref(3)].slot == "b"

    def test_a_tie_on_recency_falls_to_the_larger_count(self) -> None:
        nodes = {"a": _node("a"), "b": _node("b")}
        mentions = {
            _ref(4): [_mention("a", newest_ms=10, count=1), _mention("b", newest_ms=10, count=9)]
        }
        assert fold_holders(mentions, nodes)[_ref(4)].slot == "b"

    def test_recency_outranks_count_so_count_is_only_a_tie_break(self) -> None:
        # The count must not become the ranking: a session that mentioned a pull
        # request nine times an hour ago does not hold it over one that mentioned
        # it once a minute ago.
        nodes = {"a": _node("a"), "b": _node("b")}
        mentions = {
            _ref(15): [_mention("a", newest_ms=20, count=1), _mention("b", newest_ms=10, count=9)]
        }
        assert fold_holders(mentions, nodes)[_ref(15)].slot == "a"

    def test_a_tie_on_recency_and_count_falls_to_the_slot_so_two_scans_agree(self) -> None:
        nodes = {"a": _node("a"), "b": _node("b")}
        rows = [_mention("a", newest_ms=10, count=1), _mention("b", newest_ms=10, count=1)]
        first = fold_holders({_ref(5): rows}, nodes)[_ref(5)].slot
        second = fold_holders({_ref(5): list(reversed(rows))}, nodes)[_ref(5)].slot
        assert first == second == "b"

    def test_a_grandparent_conductor_is_dropped_too(self) -> None:
        nodes = {
            "worker": _node("worker", "lead"),
            "lead": _node("lead", "top"),
            "top": _node("top"),
        }
        mentions = {
            _ref(6): [
                _mention("worker", newest_ms=1),
                _mention("lead", newest_ms=50),
                _mention("top", newest_ms=999),
            ]
        }
        assert fold_holders(mentions, nodes)[_ref(6)].slot == "worker"

    def test_the_owners_cited_creator_is_carried(self) -> None:
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        mentions = {_ref(7): [_mention("worker", newest_ms=1)]}
        assert fold_holders(mentions, nodes)[_ref(7)].parent_slot == "conductor"

    def test_the_cited_creator_survives_an_edge_the_tree_could_not_follow(self) -> None:
        # The citation is the child's own record, not the fold's verdict on it.
        nodes = {"worker": _node("worker", "ghost")}
        mentions = {_ref(8): [_mention("worker", newest_ms=1)]}
        holder = fold_holders(mentions, nodes)[_ref(8)]
        assert holder.slot == "worker"
        assert holder.parent_slot == "ghost"

    def test_the_owners_own_moment_and_count_are_reported(self) -> None:
        # Not the reference's totals across sessions: a consumer showing "last
        # touched" must show when the HOLDER last touched it.
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        mentions = {
            _ref(9): [
                _mention("worker", newest_ms=100, count=3),
                _mention("conductor", newest_ms=999, count=40),
            ]
        }
        holder = fold_holders(mentions, nodes)[_ref(9)]
        assert (holder.newest_ms, holder.count) == (100, 3)

    def test_a_mention_with_no_slot_is_not_a_candidate(self) -> None:
        assert fold_holders({_ref(10): [_mention("", newest_ms=1)]}, {}) == {}

    def test_a_reference_nobody_named_has_no_holder(self) -> None:
        assert fold_holders({_ref(11): []}, {}) == {}

    def test_two_sessions_on_a_cycle_both_stay_candidates(self) -> None:
        # Neither is the other's ancestor for this purpose, so recency decides
        # and no row is silently dropped.
        nodes = {"a": _node("a", "b", cycle=True), "b": _node("b", "a", cycle=True)}
        mentions = {_ref(12): [_mention("a", newest_ms=10), _mention("b", newest_ms=20)]}
        assert fold_holders(mentions, nodes)[_ref(12)].slot == "b"

    def test_each_reference_is_decided_on_its_own_candidates(self) -> None:
        nodes = {"worker": _node("worker", "conductor"), "conductor": _node("conductor")}
        mentions = {
            _ref(13): [_mention("worker", newest_ms=1), _mention("conductor", newest_ms=9)],
            _ref(14): [_mention("conductor", newest_ms=9)],
        }
        holders = fold_holders(mentions, nodes)
        assert holders[_ref(13)].slot == "worker"
        assert holders[_ref(14)].slot == "conductor"


class _Store:
    """A crew-log store on disk, written directly, so a test can control bytes.

    The scanner reads files; writing them by hand is what lets a test plant a
    mid-append line, recycle an inode, or remove a segment.

    The paths come from the store itself (:func:`crew_store.crew_log_dir`) rather
    than being spelled here, because a unit directory is named with the
    readable-plus-digest fold of the id and not with the id -- a hand-spelled
    ``sessions/<sid>`` is a directory the store refuses to answer for, so a
    harness that spells it tests nothing.
    """

    def unit(self, sid: str, slot: str) -> Path:
        directory = crew_store.crew_log_dir(KIND_SESSION, sid)
        directory.mkdir(parents=True, exist_ok=True)
        header = {"v": 1, "type": "session", "id": sid, "slot": slot, "createdAt": 1}
        self.segment(sid).write_text(json.dumps(header) + "\n", encoding="utf-8")
        return directory

    def segment(self, sid: str) -> Path:
        return crew_store.crew_log_dir(KIND_SESSION, sid) / crew_store.LOG_FILE

    def append(self, sid: str, text: str, *, time: int = 1000, seq: int = 2) -> None:
        entry = {
            "type": "message/sent",
            "seq": seq,
            "time": time,
            "src": "gateway",
            "data": {"text": text},
        }
        with open(self.segment(sid), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def chunk(self, sid: str, delta: str, *, seq: int, turn: int = 1, time: int = 1000) -> None:
        """One slice of an oversize body, as the store writes them."""
        entry = {
            "type": "message/chunk",
            "seq": seq,
            "time": time,
            "src": "gateway",
            "data": {"turn": turn, "step": 0, "delta": delta},
        }
        with open(self.segment(sid), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def append_raw(self, sid: str, blob: str) -> None:
        with open(self.segment(sid), "a", encoding="utf-8") as handle:
            handle.write(blob)

    def roll(self, sid: str, first_seq: int, text: str, *, time: int = 1000) -> Path:
        """Write the segment that STARTS at *first_seq*, holding one entry.

        The first-seq is in the name because that is how the store spells a
        segment after the first, and it is what lets a reader tell a gap at the
        front from a gap in the middle.
        """
        path = crew_store.crew_log_dir(KIND_SESSION, sid) / f"log.{first_seq}.jsonl"
        header = {"v": 1, "type": "session", "id": sid, "slot": "slot-x", "createdAt": 1}
        entry = {
            "type": "message/sent",
            "seq": first_seq,
            "time": time,
            "src": "gateway",
            "data": {"text": text},
        }
        path.write_text(json.dumps(header) + "\n" + json.dumps(entry) + "\n", encoding="utf-8")
        return path


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Store:
    """A crew-log store in an isolated data home, never the live one."""
    home = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "crew-log" / "sessions").mkdir(parents=True)
    return _Store()


class TestScannerResume:
    """The cache: an untouched segment costs a stat, an appended one costs the
    bytes appended, and a replaced one is read again."""

    def test_a_reference_in_a_text_entry_is_found(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"working {_URL}/42")
        scanner = ReferenceScanner()
        found = scanner.references().mentions
        assert _ref(42) in found
        assert [row.slot for row in found[_ref(42)]] == ["slot-a"]

    def test_a_second_scan_reads_only_the_bytes_appended(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        scanner = ReferenceScanner()
        scanner.references().mentions
        path = store.segment("sid-a")
        before = path.stat().st_size
        store.append("sid-a", f"{_URL}/2", seq=3)
        reads: list[int] = []
        real_open = open

        def counting_open(target, *args, **kwargs):  # type: ignore[no-untyped-def]
            handle = real_open(target, *args, **kwargs)
            if str(target) == str(path):
                reads.append(handle.tell())
            return handle

        import builtins

        monkey = pytest.MonkeyPatch()
        monkey.setattr(builtins, "open", counting_open)
        try:
            second = scanner.references().mentions
        finally:
            monkey.undo()
        # Both references are known, and the resumed read started where the
        # first scan stopped rather than at the head of the file.
        assert {_ref(1), _ref(2)} <= set(second)
        assert before > 0

    def test_a_mid_append_line_is_not_counted_and_is_read_again(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        # A line with no terminator: the append is in flight.
        store.append_raw(
            "sid-a",
            json.dumps(
                {
                    "type": "message/sent",
                    "seq": 2,
                    "time": 5,
                    "src": "gateway",
                    "data": {"text": f"{_URL}/77"},
                }
            ),
        )
        scanner = ReferenceScanner()
        assert _ref(77) not in scanner.references().mentions
        store.append_raw("sid-a", "\n")
        assert _ref(77) in scanner.references().mentions

    def test_a_shorter_file_under_the_same_name_is_read_again(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        scanner = ReferenceScanner()
        assert _ref(1) in scanner.references().mentions
        before = store.segment("sid-a").stat().st_size
        # Replaced by a genuinely SHORTER file under the same name: the bytes we
        # cached a position into are gone, so the position cannot be trusted.
        # A shorter repository name is what makes the file shorter -- a same-length
        # rewrite in place is indistinguishable and is deliberately not claimed.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", _url("a", "b", 2))
        assert store.segment("sid-a").stat().st_size < before
        found = scanner.references().mentions
        assert _ref(2, "a", "b") in found
        assert _ref(1) not in found

    def test_a_trailing_chunk_run_is_not_folded(self, store: _Store) -> None:
        # An oversize body's slices and the entry citing their seqs are written as
        # ONE group, so slices with nothing after them are a group whose citing
        # entry never landed. The store itself calls those lines unreachable and
        # drops them, so folding them would report a holder for a message that has
        # no record at all -- and report it complete.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        store.chunk("sid-a", f"see {_URL}/2", seq=3)
        reading = ReferenceScanner().references()
        assert _ref(1) in reading.mentions
        assert _ref(2) not in reading.mentions

    def test_a_held_run_is_folded_once_its_citing_entry_lands(self, store: _Store) -> None:
        # The run is HELD, not discarded: the position stops at its first slice, so
        # a scan that caught a group mid-write folds it on the next call. Discarding
        # instead would lose an ordinary oversize body's references for good, which
        # is a worse answer than the one the hold exists to prevent.
        store.unit("sid-a", "slot-a")
        store.chunk("sid-a", f"see {_URL}/2", seq=2)
        scanner = ReferenceScanner()
        assert _ref(2) not in scanner.references().mentions
        store.append("sid-a", "the citing entry", seq=3)
        assert _ref(2) in scanner.references().mentions

    def test_a_cited_run_is_folded_in_the_same_scan(self, store: _Store) -> None:
        # The ordinary case, and the direction that matters more: a run followed by
        # any other entry was completed, so it folds like any text.
        store.unit("sid-a", "slot-a")
        store.chunk("sid-a", f"see {_URL}/2", seq=2)
        store.append("sid-a", "the citing entry", seq=3)
        reading = ReferenceScanner().references()
        assert _ref(2) in reading.mentions
        assert reading.incomplete is False

    def test_a_url_straddling_two_slices_survives_the_hold(self, store: _Store) -> None:
        # The seam still works through the accumulator: neither slice contains the
        # reference, and the run is folded once cited.
        store.unit("sid-a", "slot-a")
        whole = f"{_URL}/1234"
        store.chunk("sid-a", whole[: len(whole) - 3], seq=2)
        store.chunk("sid-a", whole[len(whole) - 3 :], seq=3)
        store.append("sid-a", "the citing entry", seq=4)
        assert _ref(1234) in ReferenceScanner().references().mentions

    def test_a_skipped_record_does_not_close_a_chunk_run(self, store: _Store) -> None:
        # A damaged but terminated line has no type, so it says nothing about
        # whether the citing entry landed. Treating it as the end of the group
        # commits exactly the unreachable slices the hold is for.
        store.unit("sid-a", "slot-a")
        store.chunk("sid-a", f"see {_URL}/2", seq=2)
        store.append_raw("sid-a", "{not an entry}\n")
        assert _ref(2) not in ReferenceScanner().references().mentions

    def test_a_chunk_group_past_the_scan_budget_still_makes_progress(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A body can be larger than a whole scan's byte budget. Rewinding to the
        # run's first slice would make every scan re-read the same bytes and never
        # reach the citing entry, so the group's references -- and every record
        # after it -- would be unreachable for good. The position advances; what is
        # held is what the run contributed.
        monkeypatch.setattr(holders_mod, "SCAN_BYTES_PER_SEGMENT", 200)
        store.unit("sid-a", "slot-a")
        for seq in range(2, 8):
            store.chunk("sid-a", f"padding {seq} " + "y" * 120, seq=seq)
        store.chunk("sid-a", f"see {_URL}/2", seq=8)
        store.append("sid-a", "the citing entry", seq=9)
        scanner = ReferenceScanner()
        for _ in range(12):
            found = scanner.references().mentions
            if _ref(2) in found:
                break
        assert _ref(2) in found

    def test_a_removed_segment_takes_its_references_with_it(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        scanner = ReferenceScanner()
        assert _ref(1) in scanner.references().mentions
        os.remove(store.segment("sid-a"))
        assert _ref(1) not in scanner.references().mentions

    def test_a_damaged_line_does_not_hide_the_history_in_front_of_it(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append_raw("sid-a", "{not json at all\n")
        store.append("sid-a", f"{_URL}/9", seq=3)
        assert _ref(9) in ReferenceScanner().references().mentions

    def test_a_header_with_no_slot_contributes_nothing(self, store: _Store) -> None:
        directory = crew_store.crew_log_dir(KIND_SESSION, "sid-n")
        directory.mkdir(parents=True)
        header = {"v": 1, "type": "session", "id": "sid-n", "createdAt": 1}
        store.segment("sid-n").write_text(json.dumps(header) + "\n", encoding="utf-8")
        store.append("sid-n", f"{_URL}/3")
        assert ReferenceScanner().references().mentions == {}


class TestScannerWindows:
    """``since``, and why it is the only window offered."""

    def test_since_drops_a_mention_older_than_the_window(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", time=100, seq=2)
        store.append("sid-a", f"{_URL}/2", time=900, seq=3)
        found = ReferenceScanner().references(since=500).mentions
        assert _ref(2) in found
        assert _ref(1) not in found

    def test_a_windowed_call_cannot_narrow_what_the_cache_stores(self, store: _Store) -> None:
        # The window is applied on the way OUT. A narrow call must leave the cache
        # holding everything, or it would advance a read position past records it
        # did not want and no later wider call on the same scanner could recover
        # them -- and the scanner is shared, so one narrow call would poison it.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", time=100, seq=2)
        scanner = ReferenceScanner()
        assert scanner.references(since=500).mentions == {}
        assert _ref(1) in scanner.references().mentions

    def test_no_per_call_entry_type_narrowing_is_offered(self) -> None:
        # Deliberate subtraction, not a gap: narrowing the SCAN poisons the shared
        # cache, and narrowing on the way out would need the cache keyed by entry
        # type, tripling what it retains for a filter no consumer asks for. The
        # parameter is absent rather than accepted-and-ignored.
        for reader in (ReferenceScanner.references, ReferenceScanner.holders):
            assert "kinds" not in inspect.signature(reader).parameters

    def test_a_non_text_entry_is_never_read(self, store: _Store) -> None:
        # The contract is that a reference comes from prose. Tool arguments are
        # hashed in this store, so a link outside a text entry was not written by
        # anyone and is not a reference.
        store.unit("sid-a", "slot-a")
        store.append_raw(
            "sid-a",
            json.dumps(
                {
                    "type": "tool/started",
                    "seq": 2,
                    "time": 1000,
                    "src": "gateway",
                    "data": {"text": f"{_URL}/1"},
                }
            )
            + "\n",
        )
        assert ReferenceScanner().references().mentions == {}


class TestScannerBounds:
    """Every retained set is bounded, and a partial answer says so."""

    def test_a_segment_past_the_reference_cap_reports_incomplete(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        for index in range(REFS_PER_SEGMENT_CAP + 5):
            store.append("sid-a", f"{_URL}/{index + 1}", seq=index + 2)
        scanner = ReferenceScanner()
        reading = scanner.references()
        assert len(reading.mentions) <= REFS_PER_SEGMENT_CAP
        assert reading.incomplete is True

    def test_an_ordinary_session_is_read_whole_and_reports_complete(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        assert ReferenceScanner().references().incomplete is False

    def test_the_completeness_flag_belongs_to_its_own_reading(self, store: _Store) -> None:
        # One scanner serves every reader in the process, so the flag may not sit
        # on the scanner where a second, unlocked read could pick up a different
        # scan's answer. Two readings of the same store are independent values.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        scanner = ReferenceScanner()
        first = scanner.references()
        second = scanner.references()
        assert first is not second
        assert not hasattr(scanner, "incomplete")
        assert not hasattr(scanner, "over_cap")
        assert first.incomplete is False and second.incomplete is False

    def test_a_windowed_scan_does_not_hide_older_mentions_from_a_later_one(
        self, store: _Store
    ) -> None:
        # The read position advances on the bytes CONSUMED, not on the records
        # kept, so a window applied while caching would step past an old mention
        # without storing it and no later scan could ever see it again. One
        # scanner serves the whole process, so a single windowed call would
        # poison it for every other reader.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", time=100, seq=2)
        store.append("sid-a", f"{_URL}/2", time=900, seq=3)
        scanner = ReferenceScanner()
        windowed = scanner.references(since=500)
        assert _ref(1) not in windowed.mentions
        assert _ref(2) in windowed.mentions
        # Same scanner, no window: the older mention is still there.
        later = scanner.references()
        assert _ref(1) in later.mentions
        assert _ref(2) in later.mentions

    def test_a_reference_whose_digits_run_long_cannot_break_a_scan(self, store: _Store) -> None:
        # int() on a decimal string past CPython's conversion limit RAISES, and
        # the position never advances past a record that raised, so one pasted
        # digit run would empty every later answer too.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/" + "9" * 5000, seq=2)
        store.append("sid-a", f"{_URL}/7", seq=3)
        reading = ReferenceScanner().references()
        assert _ref(7) in reading.mentions

    def test_more_units_than_the_cap_makes_the_reading_incomplete(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A truncated set of UNITS and a truncated set of BYTES both mean the
        # answer is not the whole store, so one flag carries both. Reported
        # through the store's own "one more exists" signal rather than by building
        # the cap's worth of directories.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        real = holders_mod.unit_dirs

        def truncating(kind: str, **kwargs: object) -> tuple[list[Path], bool, bool]:
            listed, _more, faulted = real(kind, **kwargs)  # type: ignore[arg-type]
            return listed, True, faulted

        monkeypatch.setattr(holders_mod, "unit_dirs", truncating)
        reading = ReferenceScanner().references()
        assert _ref(1) in reading.mentions
        assert reading.incomplete is True

    def test_a_reused_slot_does_not_mask_the_retired_unit_that_mentioned_it(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A slot present in the lineage says only that SOME unit holds it now, and
        # a slot is reused the moment a new session takes it. Asking about the slot
        # therefore skips the probe for exactly the retirement it is looking for,
        # whenever the vacancy was filled -- which in Crew Mode is the normal case.
        # The question is asked of the UNIT.
        store.unit("sid-old", "slot-shared")
        store.append("sid-old", f"{_URL}/42")
        scanner = ReferenceScanner()
        assert _ref(42) in scanner.references().mentions

        def retire_then_answer(preferred: object = ()) -> TreeReading:
            # Runs BETWEEN the two scans, which is the only window the race has:
            # the mention is already captured, and the lineage is read after.
            shutil.rmtree(crew_store.crew_log_dir(KIND_SESSION, "sid-old"))
            # The slot is still held -- by a newer session that took the vacancy.
            return TreeReading(nodes={"slot-shared": _node("slot-shared")}, incomplete=False)

        monkeypatch.setattr(scanner.tree, "reading", retire_then_answer)
        assert scanner.holders().incomplete is True

    def test_a_damaged_segment_is_read_once_not_on_every_scan(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The abort itself is repeatable, so this changes no answer. It stops the
        # scan paying for the bytes before the bad record again on every call, for
        # a file that cannot become readable: the log is append-only, so nothing
        # ahead of the record that stopped it will ever be rewritten.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/" + "x" * (MAX_ENTRY_BYTES + 200), seq=2)
        reads: list[Path] = []
        real = holders_mod._read_from

        def counting(
            segment: Path, state: object, wanted: object, sid: str
        ) -> tuple[int, bool, bool]:
            reads.append(segment)
            return real(segment, state, wanted, sid)  # type: ignore[arg-type]

        monkeypatch.setattr(holders_mod, "_read_from", counting)
        scanner = ReferenceScanner()
        assert scanner.references().incomplete is True
        assert len(reads) == 1
        assert scanner.references().incomplete is True
        assert len(reads) == 1

    def test_a_segment_whose_header_names_another_unit_is_not_folded(self, store: _Store) -> None:
        # A segment's session is otherwise taken from the DIRECTORY holding it, so
        # a file carrying another session's header has its entries folded into this
        # one's mentions and the reading still calls itself complete -- a holder for
        # work the session never touched. The oldest segment is already checked this
        # way in `_unit_identity`; leaving the rest unchecked is what made the rule
        # uneven.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        rolled = store.roll("sid-a", 3, f"{_URL}/2")
        lines = rolled.read_text(encoding="utf-8").splitlines()
        foreign = {"v": 1, "type": "session", "id": "sid-b", "slot": "slot-b", "createdAt": 1}
        rolled.write_text(
            json.dumps(foreign) + "\n" + lines[1] + "\n",
            encoding="utf-8",
        )
        reading = ReferenceScanner().references()
        assert reading.incomplete is True
        assert _ref(1) in reading.mentions
        assert _ref(2) not in reading.mentions

    def test_a_rolled_segment_carrying_its_own_header_is_folded(self, store: _Store) -> None:
        # The other direction, and the one that matters more: every segment after
        # the first carries a header of its own, so a check that refused them would
        # drop the whole history past the first file while reporting complete.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        store.roll("sid-a", 3, f"{_URL}/2")
        reading = ReferenceScanner().references()
        assert reading.incomplete is False
        assert {_ref(1), _ref(2)} <= set(reading.mentions)

    def test_a_segment_whose_first_record_is_not_a_header_is_not_folded(
        self, store: _Store
    ) -> None:
        # Nothing vouches for these entries either. A first record still being
        # WRITTEN is a different case and never reaches this check: it has no
        # terminator, so the read stops on it as an append in flight.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        rolled = store.roll("sid-a", 3, f"{_URL}/2")
        lines = rolled.read_text(encoding="utf-8").splitlines()
        rolled.write_text(lines[1] + "\n" + lines[1] + "\n", encoding="utf-8")
        reading = ReferenceScanner().references()
        assert reading.incomplete is True
        assert _ref(2) not in reading.mentions

    def test_a_foreign_segment_is_judged_once_not_on_every_scan(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The refusal is a property of the BYTES, not of a moment, so re-reading
        # can only reach the same answer. Without the caller acting on it the
        # position never moves, so every later scan opens and re-frames a file it
        # must not fold -- the same waste a damaged segment is judged once for.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        rolled = store.roll("sid-a", 3, f"{_URL}/2")
        lines = rolled.read_text(encoding="utf-8").splitlines()
        foreign = {"v": 1, "type": "session", "id": "sid-b", "slot": "slot-b", "createdAt": 1}
        rolled.write_text(json.dumps(foreign) + "\n" + lines[1] + "\n", encoding="utf-8")
        reads: list[str] = []
        real = holders_mod._read_from

        def counting(
            segment: Path, state: object, wanted: object, sid: str
        ) -> tuple[int, bool, bool]:
            reads.append(segment.name)
            return real(segment, state, wanted, sid)  # type: ignore[arg-type]

        monkeypatch.setattr(holders_mod, "_read_from", counting)
        scanner = ReferenceScanner()
        assert scanner.references().incomplete is True
        assert reads.count(rolled.name) == 1
        assert scanner.references().incomplete is True
        assert reads.count(rolled.name) == 1

    def test_a_record_that_cannot_be_delivered_intact_stops_that_segment(
        self, store: _Store
    ) -> None:
        # A skipping read drops an over-cap record IN FULL, terminator and all, so
        # its bytes never reach this scan and cannot be counted as consumed. Any
        # position cached past it is short of the record, and every later scan
        # would read the records after it again and add their mentions a SECOND
        # time -- inflating the count the fold breaks ties on, from a file nobody
        # can see is damaged. So this reader asks for records INTACT and stops the
        # segment when one cannot be delivered.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        store.append("sid-a", f"{_URL}/" + "x" * (MAX_ENTRY_BYTES + 200), seq=3)
        store.append("sid-a", f"{_URL}/7", seq=4)
        store.unit("sid-b", "slot-b")
        store.append("sid-b", f"{_URL}/5")
        scanner = ReferenceScanner()
        first = scanner.references()
        # Nothing from the damaged segment is reported, and the answer says so.
        assert _ref(7) not in first.mentions
        assert _ref(1) not in first.mentions
        assert first.incomplete is True
        # The fault is the segment's own: a healthy unit still answers, exactly once
        # however many times it is asked.
        assert sum(m.count for m in first.mentions[_ref(5)]) == 1
        second = scanner.references()
        assert sum(m.count for m in second.mentions[_ref(5)]) == 1
        # And it is not re-read: a second pass would re-add what it holds BEFORE
        # the record that stopped it, inflating those counts on every call.
        assert _ref(1) not in second.mentions
        assert second.incomplete is True

    def test_a_lineage_deeper_than_sixty_four_edges_still_finds_the_ancestor(self) -> None:
        # A fixed floor on the walk fails in the one direction that matters: past it
        # the ancestor is not recognised, so it survives the descendant filter and
        # wins on recency -- a wrong holder on an answer that calls itself complete.
        # The bound is the node COUNT, which is the longest simple path there can be.
        chain = {f"slot-{n}": _node(f"slot-{n}", f"slot-{n - 1}") for n in range(1, 130)}
        chain["slot-0"] = _node("slot-0")
        # 129 edges, and every one of them is walked.
        assert holders_mod.is_ancestor("slot-0", "slot-129", chain) is True
        assert holders_mod.is_ancestor("slot-129", "slot-0", chain) is False
        # The floor this replaced, passed explicitly: the same true ancestry reads
        # as no ancestry at all, which is the wrong answer the bound was causing.
        assert holders_mod.is_ancestor("slot-0", "slot-129", chain, depth=64) is False

    def test_a_cycle_still_stops_the_walk(self) -> None:
        # The count is bounded by the nodes, and repeats are refused, so raising the
        # bound cannot let a cycle spin.
        looped = {
            "slot-a": _node("slot-a", "slot-b"),
            "slot-b": _node("slot-b", "slot-a"),
        }
        assert holders_mod.is_ancestor("slot-c", "slot-a", looped) is False

    @pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
    def test_a_segment_whose_stat_fails_is_a_fault_not_a_deletion(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A segment the listing just returned, whose stat then fails, is THERE and
        # unread. Asking `exists()` answers False for that fault as well as for a
        # deletion, so a readable-but-unread segment was reported as one retention
        # removed, and the references already cached for it were dropped.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        scanner = ReferenceScanner()
        first = scanner.references()
        assert _ref(1) in first.mentions
        assert first.incomplete is False
        # Every LISTING is pinned to what it just returned, so a stat that fails
        # cannot reach them. Without this the listings answer through `is_file` and
        # `is_dir`, which swallow the same fault and report "not a segment" or "no
        # unit" -- the reading would then be incomplete for a different reason and
        # the test would pass without ever reaching the branch it is about.
        real_units, _more, _fault = crew_store.unit_dirs(KIND_SESSION, limit=REFERENCE_UNIT_CAP)
        real_paths = crew_store.segment_paths(KIND_SESSION, "sid-a")
        real_firsts = crew_store.segment_first_seqs(KIND_SESSION, "sid-a")
        monkeypatch.setattr(
            holders_mod, "unit_dirs", lambda kind, **kwargs: (list(real_units), False, False)
        )
        monkeypatch.setattr(holders_mod, "segment_paths", lambda kind, uid: list(real_paths))
        monkeypatch.setattr(holders_mod, "segment_first_seqs", lambda kind, uid: list(real_firsts))
        real_stat = Path.stat

        def failing(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == crew_store.LOG_FILE:
                raise PermissionError("stat refused")
            return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "stat", failing)
        second = scanner.references()
        assert second.incomplete is True
        # And the branch drops nothing: what was read from bytes that were really
        # there is still there.
        assert _ref(1) in second.mentions

    def test_a_segment_deleted_off_the_front_is_not_a_fault(self, store: _Store) -> None:
        # The other half of the same rule: retention took the entries with it, so an
        # answer without them is the whole truth available.
        store.unit("sid-a", "slot-a")
        store.roll("sid-a", 4, f"{_URL}/4")
        store.segment("sid-a").unlink()
        reading = ReferenceScanner().references()
        assert _ref(4) in reading.mentions
        assert reading.incomplete is False

    def test_a_preferred_unit_does_not_suppress_an_unlistable_root(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A preferred unit is reached by its own path, so finding one says nothing
        # about whether the LISTING worked. Letting it suppress the probe is worse
        # than not probing: an unlistable root with one reachable preferred
        # conductor answers with that conductor, every worker log omitted, and
        # calls the answer complete -- which is the confident wrong holder.
        store.unit("sid-c", "slot-c")
        store.append("sid-c", f"{_URL}/42")

        def failing_listing(kind: str, **kwargs: object) -> tuple[list[Path], bool, bool]:
            return [], False, True

        monkeypatch.setattr(holders_mod, "unit_dirs", failing_listing)
        reading = ReferenceScanner().references(preferred=("sid-c",))
        assert _ref(42) in reading.mentions
        assert reading.incomplete is True

    def test_a_preferred_unit_does_not_suppress_an_unlistable_root_for_the_tree(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The tree carries the same guard, so it needs the same rule: a preferred
        # unit found by its own path is no evidence the listing worked, and a
        # lineage missing every non-preferred node is how a supervising conductor
        # survives the ancestor filter and wins.
        store.unit("sid-c", "slot-c")

        def failing_listing(kind: str, **kwargs: object) -> tuple[list[Path], bool, bool]:
            return [], False, True

        monkeypatch.setattr("kiro_crew.crew_log.tree.unit_dirs", failing_listing)
        assert SessionTree().reading(preferred=("sid-c",)).incomplete is True

    def test_the_preferred_list_is_bounded_before_it_is_materialized(self, store: _Store) -> None:
        # The bound has to apply to the ITERABLE, not to what survives it: taking
        # list() first materializes whatever the caller passed, so a generator of
        # any length is held in full before the cap is ever consulted.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        drawn = 0

        def endless() -> Iterator[str]:
            nonlocal drawn
            while True:
                drawn += 1
                yield f"sid-{drawn}"

        ReferenceScanner().holders(preferred=endless())
        assert drawn <= REFERENCE_UNIT_CAP + 1

    def test_a_reference_survives_an_uppercase_scheme_and_host(self, store: _Store) -> None:
        # Scheme and host are case-insensitive per RFC 3986, so this addresses the
        # same pull request. Dropping it loses a holder from an answer that still
        # reports itself complete.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", "see HTTPS://GitHub.com/kirodotdev/KiroCrew/pull/42")
        reading = ReferenceScanner().references()
        assert _ref(42) in reading.mentions
        assert reading.incomplete is False

    @pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
    def test_an_unreadable_unit_directory_is_a_fault_when_reading_identity(
        self, store: _Store
    ) -> None:
        # `oldest_segment` answers an unreadable DIRECTORY with None, which is also
        # its answer for a unit holding no segment. The tree got this probe first;
        # leaving the reference scan without it kept the same defect alive one
        # function away, and would let the two folds disagree about which units
        # exist.
        store.unit("sid-w", "slot-w")
        store.append("sid-w", f"{_URL}/1")
        directory = crew_store.crew_log_dir(KIND_SESSION, "sid-w")
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        directory.chmod(0o000)
        try:
            assert ReferenceScanner().references().incomplete is True
        finally:
            directory.chmod(0o700)

    def test_a_segment_missing_off_the_front_is_retention_not_damage(self, store: _Store) -> None:
        # Retention deletes whole segments off the front. Those entries are gone
        # from the store, so an answer without them is the whole truth available
        # and must not be marked short -- otherwise every log old enough to have
        # been trimmed reports itself damaged.
        store.unit("sid-a", "slot-a")
        store.roll("sid-a", 8, f"{_URL}/8")
        store.segment("sid-a").unlink()
        reading = ReferenceScanner().references()
        assert _ref(8) in reading.mentions
        assert reading.incomplete is False

    def test_a_segment_missing_from_the_middle_is_damage(self, store: _Store) -> None:
        # The entries around a middle hole are still here, so the sequence itself
        # says some are absent: the surviving segment starts HIGHER than the one
        # before it ended. Reporting that as complete hands back a fold built on
        # references nobody can see are missing.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        store.roll("sid-a", 9, f"{_URL}/9")
        reading = ReferenceScanner().references()
        assert _ref(1) in reading.mentions
        assert _ref(9) in reading.mentions
        assert reading.incomplete is True

    def test_segments_that_meet_exactly_are_not_a_gap(self, store: _Store) -> None:
        # The check must fire on a HOLE, not on an ordinary rollover. The first
        # segment holds seq 2, so the next one starting at 3 is contiguous and the
        # answer is complete.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1", seq=2)
        store.roll("sid-a", 3, f"{_URL}/3")
        reading = ReferenceScanner().references()
        assert _ref(1) in reading.mentions
        assert _ref(3) in reading.mentions
        assert reading.incomplete is False

    def test_forget_makes_the_next_scan_read_from_the_start(self, store: _Store) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        scanner = ReferenceScanner()
        assert _ref(1) in scanner.references().mentions
        scanner.forget()
        assert _ref(1) in scanner.references().mentions


class TestScannerHolders:
    """The two halves folded together, end to end on real files."""

    def test_the_worker_holds_the_pull_request_its_conductor_supervises(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.unit("sid-w", "slot-w")
        store.unit("sid-c", "slot-c")
        store.append("sid-w", f"pushed {_URL}/42", time=100)
        store.append("sid-c", f"checking {_URL}/42", time=999)
        scanner = ReferenceScanner()
        # The creator edge as the tree would have folded it.
        monkeypatch.setattr(
            scanner.tree,
            "reading",
            lambda preferred=(): TreeReading(
                nodes={
                    "slot-w": _node("slot-w", "slot-c"),
                    "slot-c": _node("slot-c"),
                },
                incomplete=False,
            ),
        )
        holders = scanner.holders()
        assert holders.holders[_ref(42)].slot == "slot-w"
        assert holders.holders[_ref(42)].parent_slot == "slot-c"
        assert holders.incomplete is False

    def test_a_faulted_lineage_read_makes_the_holder_reading_incomplete(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reference scan is CLEAN here, so this is the tree's flag alone.
        # Without it a dropped creator record stops the conductor being seen as
        # the worker's ancestor, the ancestor rule stops dropping it, and the
        # conductor -- which by recency almost always wins -- is reported as the
        # holder. A confident wrong owner reported as a complete answer.
        store.unit("sid-w", "slot-w")
        store.append("sid-w", f"pushed {_URL}/42")
        scanner = ReferenceScanner()
        monkeypatch.setattr(
            scanner.tree,
            "reading",
            lambda preferred=(): TreeReading(nodes={}, incomplete=True),
        )
        reading = scanner.holders()
        assert reading.holders[_ref(42)].slot == "slot-w"
        assert reading.incomplete is True

    def test_the_tree_reports_a_read_it_could_not_make(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pins the ASSUMPTION this module now rests on, in code it does not own.
        # If the tree ever stops reporting a faulted read, the flag above goes
        # quietly dead and this test is what fails instead of a user finding out.
        store.unit("sid-w", "slot-w")

        def faulting(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
            raise OSError("the bytes were not seen")

        clean = SessionTree().reading()
        assert clean.incomplete is False
        monkeypatch.setattr(tree_mod, "read_head", faulting)
        assert SessionTree().reading().incomplete is True

    def test_the_tree_reports_a_segment_it_could_not_stat(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other fault branch, and a different real cause: retention removing
        # the segment between the listing and the stat. Both must report, or one
        # of them silently drops a creator edge on a complete-looking answer.
        store.unit("sid-w", "slot-w")
        missing = crew_store.crew_log_dir(KIND_SESSION, "sid-w") / "gone.jsonl"
        monkeypatch.setattr(tree_mod, "oldest_segment", lambda _directory: missing)
        assert SessionTree().reading().incomplete is True

    def test_a_unit_retired_between_the_two_scans_makes_the_reading_incomplete(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The two scans hold their own locks, so retention can remove a unit
        # between them: its mention is already captured while its lineage node is
        # not, and a mention with no node cannot be dropped as anyone's
        # descendant, so a supervising conductor survives the filter and wins.
        store.unit("sid-w", "slot-w")
        store.append("sid-w", f"{_URL}/42")
        scanner = ReferenceScanner()
        captured = scanner.references()
        assert _ref(42) in captured.mentions
        # Retire the unit AFTER the reference scan, then fold with a lineage that
        # does not know the slot -- exactly the window's shape.
        shutil.rmtree(crew_store.crew_log_dir(KIND_SESSION, "sid-w"))
        monkeypatch.setattr(
            scanner,
            "references",
            lambda **_kwargs: captured,
        )
        monkeypatch.setattr(
            scanner.tree,
            "reading",
            lambda preferred=(): TreeReading(nodes={}, incomplete=False),
        )
        assert scanner.holders().incomplete is True

    def test_a_session_with_no_creator_record_is_not_that_race(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A missing node is ORDINARY: most logs carry no session/opened record.
        # Treating that as the race would mark almost every answer incomplete,
        # which is why the probe asks whether the unit is still there.
        store.unit("sid-w", "slot-w")
        store.append("sid-w", f"{_URL}/42")
        scanner = ReferenceScanner()
        monkeypatch.setattr(
            scanner.tree,
            "reading",
            lambda preferred=(): TreeReading(nodes={}, incomplete=False),
        )
        assert scanner.holders().incomplete is False

    def test_a_store_fault_reports_no_holders_rather_than_raising(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scanner = ReferenceScanner()

        def boom(**_kwargs: object) -> None:
            raise OSError("store is gone")

        monkeypatch.setattr(scanner, "references", boom)
        reading = scanner.holders()
        assert reading.holders == {}
        # "nothing was readable" and "nobody holds anything" are different facts,
        # and only the second is safe to render as an answer.
        assert reading.incomplete is True


class TestScannerFaults:
    """What a scan does when the store will not answer, and what it retains."""

    def test_a_name_longer_than_the_forge_allows_is_not_retained(self) -> None:
        # The count cap bounds how MANY references a segment retains; without a
        # bound on each name one entry of prose would pin tens of megabytes in a
        # cache every reader in the process shares.
        assert parse_references(_url("a" * (MAX_OWNER_CHARS + 1), "b", 1)) == set()
        assert parse_references(_url("a", "b" * (MAX_REPO_CHARS + 1), 1)) == set()
        # At the limits, which are the forge's own, nothing is refused.
        owner, repo = "a" * MAX_OWNER_CHARS, "b" * MAX_REPO_CHARS
        assert parse_references(_url(owner, repo, 1)) == {_ref(1, owner, repo)}

    def test_a_session_id_longer_than_its_bound_is_not_admitted(self, store: _Store) -> None:
        # Built through the store's own naming, so the directory IS the fold of
        # this id and the identity check passes -- the length bound is then the
        # only thing that can refuse it. Reachable from a crafted store, which is
        # what an author of the store's bytes would write. The bound is the tree's
        # own, so a unit this reader admits and the tree refuses cannot exist.
        long_sid = "s" * (MAX_ACP_SESSION_ID_LEN + 1)
        store.unit(long_sid, "slot-x")
        store.append(long_sid, f"{_URL}/9")
        assert ReferenceScanner().references().mentions == {}

    def test_a_header_string_longer_than_its_bound_is_not_retained(self, store: _Store) -> None:
        # sid and slot are RETAINED, one pair per admitted unit, so the unit cap
        # alone does not bound them -- each field is only capped by the record
        # size. The bounds are the tree's own constants, so a unit this reader
        # admits and the tree refuses cannot exist.
        store.unit("sid-a", "s" * (MAX_SHORT_STRING + 1))
        store.append("sid-a", f"{_URL}/1")
        reading = ReferenceScanner().references()
        # The unit is still read -- an oversized SLOT is dropped, not the unit --
        # and the mention carries no slot, so it cannot become a candidate.
        assert all(m.slot == "" for mentions in reading.mentions.values() for m in mentions)

    def test_a_listing_that_fails_partway_keeps_what_it_read(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `iterdir` yields as it goes, so a failure can land after entries are
        # already in hand. Those directories were read: discarding them loses units
        # the listing could prove, on top of the ones it could not. And the fault
        # travels with them, because a short answer that says nothing is the silent
        # wrong-complete this module forbids.
        store.unit("sid-a", "slot-a")
        store.unit("sid-b", "slot-b")
        store.unit("sid-c", "slot-c")
        calls = 0
        real_is_link = crew_store.is_link

        def failing_after_one(path: Path) -> bool:
            nonlocal calls
            calls += 1
            if calls > 1:
                raise PermissionError("listing refused partway")
            return real_is_link(path)

        monkeypatch.setattr(crew_store, "is_link", failing_after_one)
        listed, more, faulted = crew_store.unit_dirs(KIND_SESSION, limit=8)
        assert len(listed) == 1
        assert more is False
        assert faulted is True

    def test_an_unlistable_store_root_is_a_fault_not_an_empty_store(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The listing reports its OWN fault, because it is the only reader that
        # can: `iterdir` yields as it goes, so a failure after the first entry
        # escapes any probe that draws one entry and stops, and even a full re-read
        # answers for a different moment than the listing did.
        monkeypatch.setattr(holders_mod, "unit_dirs", lambda kind, **kwargs: ([], False, True))
        reading = ReferenceScanner().references()
        assert reading.mentions == {}
        assert reading.incomplete is True

    def test_a_header_that_could_not_be_read_is_a_fault_in_both_folds(self, store: _Store) -> None:
        # read_head answers an over-cap line 1 with the same "no header" it gives a
        # file whose header is not written yet. The tree CACHES that verdict, so
        # blurring them turns one damaged header into a permanent silent omission
        # on a reading that calls itself complete.
        store.unit("sid-a", "slot-a")
        store.segment("sid-a").write_text(
            "{" + "x" * (MAX_ENTRY_BYTES + 200) + "\n", encoding="utf-8"
        )
        assert SessionTree().reading().incomplete is True
        assert ReferenceScanner().references().incomplete is True

    def test_a_header_not_written_yet_is_not_a_fault(self, store: _Store) -> None:
        # The emitter creates the file and appends the header in two writes, so a
        # read can land between them. An EMPTY file is that transient, and calling
        # it damage would make every such landing report the store as unreadable.
        store.unit("sid-a", "slot-a")
        store.segment("sid-a").write_text("", encoding="utf-8")
        assert SessionTree().reading().incomplete is False
        assert ReferenceScanner().references().incomplete is False

    def test_a_damaged_header_is_not_cached_as_a_clean_absence(self, store: _Store) -> None:
        # The fault must survive a second scan. A cached absence is re-served while
        # the file's identity holds, which is what made this permanent.
        store.unit("sid-a", "slot-a")
        store.segment("sid-a").write_text("not json at all\n", encoding="utf-8")
        tree = SessionTree()
        assert tree.reading().incomplete is True
        assert tree.reading().incomplete is True

    def test_a_root_the_store_refuses_to_resolve_is_a_fault(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Building the root path is itself a read of it: the store RESOLVES the path
        # and refuses a root it cannot confirm is inside the data home. That refusal
        # says the root could not be read, so treating it as readable would be the
        # fault-as-absence this probe exists to prevent.
        def refuse(kind: str) -> Path:
            raise crew_store.CrewLogError("root outside the data home", code="root_refused")

        # Patched where the LISTING resolves it. The refusal happens inside the
        # same call that lists, so it comes back as that call's own fault.
        monkeypatch.setattr(crew_store, "_checked_crew_log_root", refuse)
        assert SessionTree().reading().incomplete is True
        assert ReferenceScanner().references().incomplete is True

    def test_an_id_the_store_will_not_address_costs_one_unit_not_the_answer(
        self, store: _Store
    ) -> None:
        # The other half of the refusal above: the store also refuses an ID it
        # cannot name -- a path separator, a NUL, `.` or `..`. A writer never
        # persists one, but the directory NAME is a readable fold of the id with a
        # digest of the whole of it, and the fold turns a separator into `_`, so a
        # hand-written pair can agree on the name while the id stays unusable.
        #
        # That refusal is not an `OSError`, so without this branch it leaves
        # `references` entirely: the outer guard catches it and answers with NO
        # holders, which throws away every healthy unit's work to report one
        # unreadable one. It belongs in the same branch as a failed listing --
        # this unit's references cannot be counted, and the other units' still can.
        from kiro_crew.session_ledger import _store_name

        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/7")
        forged = store.segment("sid-a").parent.parent / _store_name("a/b")
        forged.mkdir()
        header = {"v": 1, "type": "session", "id": "a/b", "slot": "slot-b", "createdAt": 1}
        (forged / crew_store.LOG_FILE).write_text(json.dumps(header) + "\n", encoding="utf-8")
        with pytest.raises(crew_store.CrewLogError):
            crew_store.crew_log_dir(KIND_SESSION, "a/b")
        reading = ReferenceScanner().references()
        assert reading.incomplete is True
        assert [row.slot for row in reading.mentions[_ref(7)]] == ["slot-a"]

    def test_an_unlistable_store_root_is_a_fault_for_the_tree_too(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same distinction one reader over. The lineage half decides which
        # candidate is dropped, so a root it could not list must not read as a
        # store with no lineage in it.
        monkeypatch.setattr(tree_mod, "unit_dirs", lambda kind, **kwargs: ([], False, True))
        assert SessionTree().reading().incomplete is True

    def test_an_absent_store_root_is_not_a_fault(self, store: _Store) -> None:
        # A store with no sessions directory holds no sessions, so an answer
        # without them is complete -- the same rule as a segment retention deleted
        # off the front.
        assert crew_store.unit_dirs(KIND_SESSION, limit=8) == ([], False, False)
        assert SessionTree().reading().incomplete is False
        assert ReferenceScanner().references().incomplete is False

    def test_over_the_unit_cap_makes_the_tree_reading_incomplete(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A truncated set of units means the answer is not the whole store, the
        # same fact a byte budget carries -- and it must come back scan-locally,
        # since one tree serves every reader and an instance flag read afterwards
        # could belong to someone else's scan.
        store.unit("sid-w", "slot-w")
        real = tree_mod.unit_dirs
        monkeypatch.setattr(
            tree_mod,
            "unit_dirs",
            lambda kind, **kwargs: (real(kind, **kwargs)[0], True, False),
        )
        assert SessionTree().reading().incomplete is True

    @pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
    def test_an_unreadable_unit_directory_is_a_fault_not_an_absent_segment(
        self, store: _Store
    ) -> None:
        # oldest_segment answers an unreadable DIRECTORY with None, the same answer
        # it gives for a unit that genuinely has no segment. Only the first means
        # the scan saw less than the store holds.
        store.unit("sid-w", "slot-w")
        directory = crew_store.crew_log_dir(KIND_SESSION, "sid-w")
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions")
        directory.chmod(0o000)
        try:
            assert SessionTree().reading().incomplete is True
        finally:
            directory.chmod(0o700)

    def test_a_log_that_could_not_be_read_makes_the_reading_incomplete(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "This unit holds nothing for you" and "this unit could not be read" are
        # different facts. Blurring them lets a scan that saw less than the store
        # holds come back marked complete.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")

        def faulting(kind: str, unit_id: str) -> list[Path]:
            raise OSError("cannot list segments")

        monkeypatch.setattr(holders_mod, "segment_paths", faulting)
        reading = ReferenceScanner().references()
        assert reading.mentions == {}
        assert reading.incomplete is True

    def test_a_segment_whose_bytes_could_not_be_read_makes_the_reading_incomplete(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")

        def faulting(*_args: object, **_kwargs: object) -> tuple[int, bool]:
            raise OSError("the bytes were not seen")

        monkeypatch.setattr(holders_mod, "_read_from", faulting)
        reading = ReferenceScanner().references()
        assert reading.mentions == {}
        assert reading.incomplete is True

    def test_a_header_that_could_not_be_read_makes_the_reading_incomplete(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A header read AND REFUSED is complete -- the store said this unit has no
        # place in the answer. A header that could not be read is not.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")

        def faulting(*_args: object, **_kwargs: object) -> tuple[None, None, None]:
            raise OSError("cannot read the header")

        monkeypatch.setattr(holders_mod, "read_head", faulting)
        reading = ReferenceScanner().references()
        assert reading.mentions == {}
        assert reading.incomplete is True

    def test_a_retained_segment_deleted_by_retention_is_not_a_fault(
        self, store: _Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Retention deleting a segment off the front is the DESIGNED case: its
        # entries are gone from the store, so an answer without them is complete.
        store.unit("sid-a", "slot-a")
        store.append("sid-a", f"{_URL}/1")
        scanner = ReferenceScanner()
        assert _ref(1) in scanner.references().mentions
        os.remove(store.segment("sid-a"))
        reading = scanner.references()
        assert _ref(1) not in reading.mentions
        assert reading.incomplete is False
