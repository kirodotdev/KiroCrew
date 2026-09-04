"""Tests for the consecutive-probe-failure counter and its dashboard surface.

The defect this addresses: a probe verdict was display-only and forgotten between
rounds, so nothing anywhere could say "this server has failed every probe since
Tuesday". These cover the counter's arithmetic, the statuses that deliberately
carry NO verdict, the fail-open behaviour of an unreadable store, the wire
annotation, and the reset endpoint.

Scope note: this does NOT unmount a failing server. That half has no safe lever in
the generated agent config (see the spec's deferred section) and is tracked
separately, so nothing here asserts anything about ``tools`` or ``mcpServers``.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_quarantine


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store at tmp_path and pin the threshold to 3."""
    path = tmp_path / "mcp-quarantine.json"
    monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", path)
    monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 3)
    return path


def _fail(name: str, status: str = "error", error: str = "boom"):
    return [(name, status, error)]


def _failing() -> set[str]:
    """Names currently past the threshold, read back through the PUBLIC surface.

    ``record_verdicts`` returns nothing and there is no ``failing_names`` helper:
    nothing in the product acts on a crossing, it only reports one, so the store's
    only consumer is the snapshot the API serves. Asserting through that is what
    the dashboard actually sees.
    """
    return {name for name, state in mcp_quarantine.snapshot().items() if state["failing"]}


# ---------------------------------------------------------------------------
# the counter
# ---------------------------------------------------------------------------


class TestCounter:
    def test_failures_accumulate_and_cross_the_threshold_once(self, store):
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == set()
        assert mcp_quarantine.state_for("airbnb")["fails"] == 2

        # Third consecutive failure is the one that crosses.
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}

        # crossed_at is stamped once and never moved by a later failure, so
        # "failing since" stays the time it actually started failing.
        crossed = mcp_quarantine.state_for("airbnb")["since"]
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}
        assert mcp_quarantine.state_for("airbnb")["since"] == crossed
        assert mcp_quarantine.state_for("airbnb")["fails"] == 4

    def test_a_timeout_counts_the_same_as_an_error(self, store):
        mcp_quarantine.record_verdicts(_fail("bazi", status="timeout", error="timeout after 15s"))
        mcp_quarantine.record_verdicts(_fail("bazi", status="error"))
        mcp_quarantine.record_verdicts(_fail("bazi", status="timeout"))
        assert _failing() == {"bazi"}

    def test_one_success_clears_the_counter_outright(self, store):
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts([("airbnb", "ok", "")])
        # Not decremented to 1 -- gone. The claim is "consistently unreachable",
        # and one good handshake disproves it, so the next two failures must not
        # be enough to cross.
        assert mcp_quarantine.state_for("airbnb") is None
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == set()

    def test_a_success_clears_a_server_already_over_the_threshold(self, store):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}
        mcp_quarantine.record_verdicts([("airbnb", "ok", "")])
        assert _failing() == set()
        assert mcp_quarantine.state_for("airbnb") is None

    def test_servers_are_counted_independently(self, store):
        mcp_quarantine.record_verdicts([("a", "error", ""), ("b", "ok", "")])
        mcp_quarantine.record_verdicts([("a", "error", ""), ("b", "error", "")])
        mcp_quarantine.record_verdicts([("a", "error", ""), ("b", "error", "")])
        assert _failing() == {"a"}
        assert mcp_quarantine.state_for("b")["fails"] == 2

    def test_the_stored_error_is_bounded(self, store):
        mcp_quarantine.record_verdicts(_fail("noisy", error="x" * 5000))
        assert len(mcp_quarantine.state_for("noisy")["lastError"]) == 400


# ---------------------------------------------------------------------------
# statuses that carry no verdict
# ---------------------------------------------------------------------------


class TestNonVerdictStatuses:
    @pytest.mark.parametrize("status", ["needs_auth", "unknown", "outdated", "disabled", ""])
    def test_status_is_neither_a_failure_nor_a_success(self, store, status):
        """These are "no result", not "a bad result".

        ``needs_auth`` is the load-bearing one: a server asking for OAuth sign-in
        is working correctly and saying so. Counting it would label every
        connection the user has not signed into yet as failing.
        """
        mcp_quarantine.record_verdicts(_fail("srv"))
        before = mcp_quarantine.state_for("srv")
        mcp_quarantine.record_verdicts([("srv", status, "")])
        assert mcp_quarantine.state_for("srv") == before

    def test_a_nameless_row_is_skipped(self, store):
        mcp_quarantine.record_verdicts([("", "error", "")])
        assert mcp_quarantine.snapshot() == {}


# ---------------------------------------------------------------------------
# the off switch
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_threshold_zero_never_counts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", tmp_path / "q.json")
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 0)
        for _ in range(10):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == set()
        assert mcp_quarantine.snapshot() == {}

    def test_threshold_zero_clears_records_written_earlier(self, store, monkeypatch):
        """Turning it off has to release what it already flagged.

        Otherwise the switch is half a switch: new failures stop counting but the
        servers already labelled stay labelled, with no surface explaining why.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 0)
        assert _failing() == set()

    def test_raising_the_threshold_clears_a_server_below_it(self, store, monkeypatch):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 5)
        assert _failing() == set()


# ---------------------------------------------------------------------------
# the store itself
# ---------------------------------------------------------------------------


class TestStore:
    def test_absent_store_reads_as_empty(self, store):
        assert _failing() == set()
        assert mcp_quarantine.snapshot() == {}
        assert mcp_quarantine.state_for("anything") is None

    @pytest.mark.parametrize("body", ["{ not json", "[]", '{"servers": 5}', '"text"'])
    def test_a_corrupt_store_fails_open(self, store, body):
        """A store we cannot parse must label NOTHING.

        The records only ever ADD a diagnostic to a row, so failing closed would
        let one bad byte on disk mislabel the user's whole MCP fleet.
        """
        store.write_text(body, encoding="utf-8")
        assert _failing() == set()
        assert mcp_quarantine.snapshot() == {}

    def test_invalid_utf8_reads_as_empty_rather_than_raising(self, store):
        """``read_text`` decodes strictly, and ``UnicodeDecodeError`` is neither an
        ``OSError`` nor a ``JSONDecodeError`` -- it is a ``ValueError`` raised
        before json sees the bytes. It escaped both other arms and surfaced as a
        500 from whichever handler happened to read the store.
        """
        store.write_bytes(b'{"servers": {"a\xff\xfe": {}}}')
        assert _failing() == set()
        assert mcp_quarantine.snapshot() == {}

    def test_an_oversized_integer_reads_as_corrupt_rather_than_raising(self, store):
        """A literal integer longer than ``sys.get_int_max_str_digits()`` (4300 by
        default on 3.11+) makes the json scanner's own ``int()`` raise a PLAIN
        ``ValueError`` -- not a ``JSONDecodeError``, so a tuple naming only that
        and ``UnicodeDecodeError`` missed it and the store surfaced a 500. The
        store is not fenced under ``_CREW_SECRET_LEAVES``, so its contents are
        attacker-influenced and this arm cannot enumerate failure modes.
        """
        store.write_text(
            '{"version": 1, "servers": {"a": {"fails": ' + "9" * 4400 + "}}}",
            encoding="utf-8",
        )
        assert _failing() == set()
        assert mcp_quarantine.snapshot() == {}
        assert mcp_quarantine.state_for("a") is None
        # And it is CORRUPT, not merely unreadable, so the counter can recover.
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}

    def test_an_absurd_stored_counter_is_clamped_not_propagated(self, store):
        """A ``fails`` of 4300 nines PARSES fine, so the corrupt arm never sees it,
        and then ``+ 1`` makes it 4301 digits -- at which point ``json.dumps``
        raises ``ValueError`` on the way back out and the probe request 500s. The
        store is not fenced under ``_CREW_SECRET_LEAVES``, so an agent's file tools
        can write exactly that.
        """
        store.write_text(
            json.dumps({"version": 1, "servers": {"a": {"fails": int("9" * 4300)}}}),
            encoding="utf-8",
        )
        # Read back bounded, so nothing downstream can fail to serialize it.
        assert mcp_quarantine.state_for("a")["fails"] == mcp_quarantine._FAILS_MAX

        # And a probe round over it completes and persists.
        mcp_quarantine.record_verdicts(_fail("a"))
        assert mcp_quarantine.state_for("a")["fails"] == mcp_quarantine._FAILS_MAX
        assert json.loads(store.read_text(encoding="utf-8"))["servers"]["a"]["fails"] == (
            mcp_quarantine._FAILS_MAX
        )

    @pytest.mark.parametrize("bogus", [-5, True, "3", None, 2.5, [1]])
    def test_a_non_counter_value_reads_as_zero(self, store, bogus):
        """``True`` is the sharp one: ``isinstance(True, int)`` is True in Python,
        so a bare int check would treat it as a count of 1.
        """
        store.write_text(
            json.dumps({"version": 1, "servers": {"a": {"fails": bogus}}}),
            encoding="utf-8",
        )
        assert mcp_quarantine.state_for("a")["fails"] == 0
        assert _failing() == set()

    def test_a_deeply_nested_document_reads_as_corrupt(self, store):
        """``RecursionError`` is a ``RuntimeError``, not a ``ValueError``, so it
        escaped the arm that caught every earlier parse failure.
        """
        store.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
        assert _failing() == set()
        assert mcp_quarantine.snapshot() == {}
        # Corrupt, not unreadable, so the counter still recovers.
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}

    def test_any_parse_failure_is_corrupt_not_an_exception(self, store, monkeypatch):
        """The CLASS ratchet, not another instance of it.

        Four review rounds found four different exception types escaping
        successively wider tuples in this arm. The store is unfenced, so the set
        of ways a parse can fail is open-ended and grows with the interpreter --
        the invariant is that NOTHING from the parse escapes as an exception, so
        this asserts it with an error type that is not in any tuple anyone would
        have thought to write.
        """

        class Exotic(Exception):
            pass

        store.write_text('{"version": 1, "servers": {}}', encoding="utf-8")
        monkeypatch.setattr(
            mcp_quarantine.json, "loads", lambda *_a, **_k: (_ for _ in ()).throw(Exotic("odd"))
        )
        # No raise, and classified corrupt rather than unreadable.
        assert mcp_quarantine._read() == ({}, "corrupt")
        assert mcp_quarantine.snapshot() == {}

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX-only special files")
    def test_a_symlinked_store_is_refused_not_followed(self, store):
        """The store path is agent-writable, so a link can be pre-planted at it.

        Pointed at ``/dev/zero`` a read-to-EOF never ends: every ``GET /api/mcp``
        becomes an unbounded allocation and takes the gateway down. The link is
        refused outright rather than followed and then judged by what it points at.
        """
        store.symlink_to("/dev/zero")
        # Returns, promptly, with nothing -- and does not allocate.
        assert mcp_quarantine.snapshot() == {}
        assert _failing() == set()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX-only special files")
    def test_a_fifo_store_does_not_hang_the_request(self, store):
        """Same shape, different ending: ``open`` on a FIFO with no writer blocks
        forever, so the request hangs rather than crashing. ``O_NONBLOCK`` is what
        keeps the refusal reachable, and ``fstat`` on the descriptor is what makes
        it a refusal.
        """
        os.mkfifo(store)
        assert mcp_quarantine.snapshot() == {}

    def test_an_oversized_store_is_not_read_whole(self, store, monkeypatch):
        """A regular file can also be arbitrarily large. The cap is enforced on the
        BYTES READ, not on ``st_size``, which a file growing under us understates.
        """
        monkeypatch.setattr(mcp_quarantine, "_STORE_MAX_BYTES", 2048)
        store.write_bytes(b'{"version": 1, "servers": {}}' + b" " * 4096)
        assert mcp_quarantine._read() == ({}, "corrupt")

        # Under the cap it still reads normally, including across chunk reads.
        monkeypatch.setattr(mcp_quarantine, "_STORE_MAX_BYTES", 8 * 1024 * 1024)
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("a"))
        assert _failing() == {"a"}

    def test_a_read_failure_is_never_classified_corrupt(self, store, monkeypatch):
        """The other half of the split: broadening the parse arm must not let an
        environmental read failure be read as "not our format" and overwritten.

        Patches the CURRENT read primitive. What is pinned is the classification,
        not the call -- if the I/O is reworked again, repoint the patch rather than
        weakening the assertion.
        """

        def boom(*_a, **_k):
            raise OSError("sharing violation")

        monkeypatch.setattr(mcp_quarantine.os, "open", boom)
        assert mcp_quarantine._read() == ({}, "unreadable")

    @pytest.mark.parametrize("stack", [0, 300])
    def test_a_nested_extra_key_cannot_reach_the_encoder(self, store, stack):
        """A record may hold a value that ``json.loads`` accepts and
        ``json.dumps`` cannot re-emit: ~900 nested arrays raise ``RecursionError``
        on the way out, which is a ``RuntimeError`` and escaped the save guard.

        What is asserted is deliberately NOT the resulting count. Whether ``loads``
        succeeds and only ``dumps`` fails is a band that moves with the interpreter
        and with the frames left at the call: verified directly, this document at a
        300-frame stack loads fine on 3.12 and raises inside ``loads`` on 3.11.
        Both are legitimate -- one leaves the prior record readable, the other
        classifies the store corrupt and starts the count over -- so pinning
        ``fails == 2`` pinned the interpreter rather than the behaviour, and broke
        on the 3.10 shard.

        The invariants that hold on BOTH paths are the real contract: nothing
        raises, the store is still parseable afterwards, and the nested key is not
        in it. Parametrized over stack depth because a shallow stack has enough
        headroom to pass WITHOUT the fix, so the deep case is the load-bearing one.
        """
        store.write_text(
            '{"version": 1, "servers": {"a": {"fails": 1, "junk": ' + "[" * 900 + "]" * 900 + "}}}",
            encoding="utf-8",
        )

        def deepen(n, fn):
            return fn() if n == 0 else deepen(n - 1, fn)

        # No raise, from either depth.
        deepen(stack, lambda: mcp_quarantine.record_verdicts(_fail("a")))

        # The write landed, is parseable, and carries none of the nested payload.
        on_disk = json.loads(store.read_text(encoding="utf-8"))["servers"]["a"]
        assert "junk" not in on_disk
        assert on_disk["fails"] >= 1
        assert mcp_quarantine.state_for("a")["fails"] >= 1

    def test_a_record_is_rebuilt_from_known_fields_only(self, store):
        """The CLASS ratchet for record content.

        Three rounds found three different unknown-key or out-of-range payloads
        reaching ``json.dumps``. Rebuilding from an allowlist is what ends that, so
        this asserts the SHAPE: no key from the file survives, and every value is a
        scalar this module produced.
        """
        store.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": {
                        "a": {
                            "fails": 2,
                            "last_status": "error",
                            "last_error": "boom",
                            "crossed_at": 123.5,
                            "last_failed_at": 456.5,
                            "attacker": {"nested": ["anything", 1, None]},
                            "tools": ["@a/x"],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rec = mcp_quarantine._read()[0]["a"]
        assert set(rec) == {
            "fails",
            "last_status",
            "last_error",
            "last_failed_at",
            "crossed_at",
        }
        assert all(isinstance(v, (int, float, str)) for v in rec.values())
        # The fields we own are preserved, not merely defaulted away.
        assert rec["fails"] == 2 and rec["last_status"] == "error"
        assert rec["crossed_at"] == 123.5 and rec["last_failed_at"] == 456.5

    @pytest.mark.parametrize("bogus", [True, "x", None, [1], -1, float("inf"), float("nan")])
    def test_a_bogus_timestamp_reads_as_zero(self, store, bogus):
        """``True`` and the non-finite floats are the sharp ones: ``True`` is an
        ``int`` (so it would become the timestamp 1.0 and read as crossed), and
        ``json.dumps`` emits NaN / Infinity as bare words that are valid Python
        but not valid JSON, so the file would stop round-tripping.
        """
        store.write_text(
            json.dumps({"version": 1, "servers": {"a": {"fails": 5, "crossed_at": bogus}}}),
            encoding="utf-8",
        )
        assert mcp_quarantine._read()[0]["a"]["crossed_at"] == 0.0
        # crossed_at 0 means "has not crossed", so no badge despite fails >= 3.
        assert _failing() == set()

    def test_a_malformed_record_is_dropped_not_trusted(self, store):
        store.write_text(
            json.dumps({"version": 1, "servers": {"a": "not-a-dict", "b": {"fails": 9}}}),
            encoding="utf-8",
        )
        # ``b`` has no crossed_at, so it is a counter that never crossed.
        assert _failing() == set()
        assert "a" not in mcp_quarantine.snapshot()

    def test_a_corrupt_store_may_be_overwritten(self, store):
        """The way BACK from corrupt.

        Bytes that are not our format can never become readable, and there is
        nothing recoverable to protect, so a write must be allowed to replace
        them. Refusing here would wedge the counter permanently with no path out
        but a hand-deleted file.
        """
        store.write_text("{ not json", encoding="utf-8")
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}

    def test_a_transient_read_failure_does_not_erase_saved_counters(self, store, monkeypatch):
        """The data-loss ratchet.

        A read that fails for an environmental reason -- a Windows sharing
        violation against the antivirus scanner, EIO, a permission flip -- says
        nothing about what is on disk. Folding it into "no records" and then
        saving replaces real history with only what this round happened to see, so
        a probe round during one unlucky moment silently resets every counter.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == {"airbnb"}
        on_disk = store.read_text(encoding="utf-8")

        real_open = mcp_quarantine.os.open
        broken = {"on": True}

        def flaky(p, *a, **k):
            if broken["on"] and str(p) == str(store):
                raise OSError("sharing violation")
            return real_open(p, *a, **k)

        monkeypatch.setattr(mcp_quarantine.os, "open", flaky)
        # A whole probe round lands while the store is unreadable.
        mcp_quarantine.record_verdicts([("airbnb", "error", ""), ("other", "error", "")])
        # Flag rather than ``monkeypatch.undo()``: undo would also revert the
        # fixture's _STORE_PATH and threshold patches, pointing the assertions
        # below at the real store and passing for the wrong reason.
        broken["on"] = False

        # Untouched: not rewritten with only this round's view.
        assert store.read_text(encoding="utf-8") == on_disk
        assert _failing() == {"airbnb"}
        assert mcp_quarantine.state_for("other") is None

    def test_clear_refuses_rather_than_reporting_a_reset_it_could_not_read(
        self, store, monkeypatch
    ):
        """The reset endpoint tells the user the count is clear, so an unreadable
        store has to surface as a failure -- not as a successful no-op that leaves
        the count on disk.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))

        def boom(*_a, **_k):
            raise OSError("sharing violation")

        monkeypatch.setattr(mcp_quarantine.os, "open", boom)
        with pytest.raises(OSError):
            mcp_quarantine.clear("airbnb")

    def test_an_unwritable_store_does_not_raise(self, store, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == set()

    def test_an_unpersisted_crossing_is_not_visible(self, store, monkeypatch):
        """A crossing the store did not accept must not show on the row."""
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts(_fail("airbnb"))

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == set()

    def test_clear_propagates_a_write_failure(self, store, monkeypatch):
        """Unlike the counter, a reset must NOT degrade quietly -- its caller tells
        the user the count is clear.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        with pytest.raises(OSError):
            mcp_quarantine.clear("airbnb")

    def test_every_mutation_holds_the_write_lock_across_load_and_save(self, store, monkeypatch):
        """The read-modify-write must be one transaction.

        Without it a probe round and a reset race on the same file: both read the
        same records and whichever saves last discards the other's decision.
        Asserted at the write, which is the far end of the window.
        """
        held: list[bool] = []
        real = mcp_quarantine.atomic_write

        def watching(*a, **k):
            held.append(mcp_quarantine._WRITE_LOCK.locked())
            return real(*a, **k)

        monkeypatch.setattr(mcp_quarantine, "atomic_write", watching)
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.clear("airbnb")
        assert held == [True, True], "a mutation reached its write without the lock"

    def test_snapshot_reads_the_store_once_regardless_of_size(self, store, monkeypatch):
        """Pins the fix for a quadratic read on the event loop.

        ``snapshot`` used to call ``state_for`` per name, and each of those re-read
        the store AND the config -- so annotating an N-server probe response cost N
        file reads, in a handler that runs on every dashboard poll.
        """
        mcp_quarantine.record_verdicts([(f"srv{i}", "error", "") for i in range(25)])
        reads = {"n": 0}
        real = mcp_quarantine._load

        def counting():
            reads["n"] += 1
            return real()

        monkeypatch.setattr(mcp_quarantine, "_load", counting)
        snap = mcp_quarantine.snapshot()
        assert len(snap) == 25
        assert reads["n"] == 1, f"snapshot read the store {reads['n']} times for 25 servers"

    def test_the_store_is_valid_json_with_a_version(self, store):
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        payload = json.loads(store.read_text(encoding="utf-8"))
        assert payload["version"] == mcp_quarantine.STORE_VERSION
        assert payload["servers"]["airbnb"]["fails"] == 1

    def test_store_path_follows_the_data_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", None)
        monkeypatch.setattr(mcp_quarantine, "data_home", lambda: tmp_path / "crew")
        assert mcp_quarantine.store_path() == tmp_path / "crew" / "mcp-quarantine.json"


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_resets_the_counter_as_well_as_the_flag(self, store):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        removed = mcp_quarantine.clear("airbnb")
        # The removed record is handed back so a caller whose accompanying work
        # fails can tell what it undid.
        assert removed is not None and removed["fails"] == 3
        assert _failing() == set()
        # The counter too: resetting to one-short-of-the-threshold would make the
        # button look broken -- press it, one failure, label back.
        assert mcp_quarantine.state_for("airbnb") is None
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert _failing() == set()

    def test_clear_is_idempotent_and_reports_nothing_to_do(self, store):
        assert mcp_quarantine.clear("never-seen") is None


# ---------------------------------------------------------------------------
# POST /api/mcp/quarantine/clear
# ---------------------------------------------------------------------------


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", tmp_path / "q.json")
    monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 3)
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    return SimpleNamespace(mod=mcp_mod)


async def _client(mod) -> TestClient:
    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_post("/api/mcp/quarantine/clear", mod.api_mcp_quarantine_clear)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
class TestResetEndpoint:
    async def test_reset_clears_the_count(self, endpoint):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "name": "airbnb", "released": True}
        finally:
            await client.close()
        assert _failing() == set()

    async def test_resetting_an_uncounted_server_is_a_no_op(self, endpoint):
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "healthy"})
            assert resp.status == 200
            assert (await resp.json())["released"] is False
        finally:
            await client.close()

    async def test_reset_drops_the_stale_annotation_from_the_probe_cache(self, endpoint):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        endpoint.mod._mcp_probe_cache[:] = [
            {"name": "airbnb", "status": "error", "probeFailing": True, "probeFailures": 3}
        ]
        client = await _client(endpoint.mod)
        try:
            await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
        finally:
            await client.close()
        row = endpoint.mod._mcp_probe_cache[0]
        assert "probeFailing" not in row and "probeFailures" not in row

    async def test_a_failed_store_write_reports_500_and_a_code(self, endpoint, monkeypatch):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 500
            assert (await resp.json())["code"] == "quarantine_store_write_failed"
        finally:
            await client.close()
        # Nothing was reset, and nothing pretended otherwise.
        assert _failing() == {"airbnb"}

    async def test_a_missing_name_is_rejected(self, endpoint):
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={})
            assert resp.status == 400
        finally:
            await client.close()

    async def test_a_non_object_body_is_rejected(self, endpoint):
        """A JSON array or bare null parses fine and then reaches ``.get`` on the
        identifier read, which surfaced as a 500 for a malformed request.
        """
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json=[1, 2])
            assert resp.status == 400
            assert (await resp.json())["code"] == "body_not_object"
        finally:
            await client.close()

    async def test_invalid_json_is_rejected(self, endpoint):
        client = await _client(endpoint.mod)
        try:
            resp = await client.post(
                "/api/mcp/quarantine/clear",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# row annotation
# ---------------------------------------------------------------------------


class TestAnnotation:
    def test_every_row_returning_endpoint_annotates(self):
        """The reading has to be on the endpoint the table LOADS from.

        Found by driving a real pod, not by reading code: the first version
        annotated only the two probe endpoints, so a failing server rendered as a
        plain error row until the user happened to press Probe. This pins all four
        call sites, and that they run off the event loop -- the annotation reads
        the store.
        """
        import inspect

        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for fn in (
            mcp_mod.api_mcp_servers,
            mcp_mod.api_mcp_probe,
            mcp_mod.api_mcp_probe_cached,
            mcp_mod._run_mcp_probe,
        ):
            src = inspect.getsource(fn)
            assert "_annotate_quarantine" in src, f"{fn.__name__} returns rows without the reading"
            assert (
                "to_thread(_annotate_quarantine" in src
            ), f"{fn.__name__} annotates on the event loop"

    def test_recording_is_offloaded_whole(self):
        """Both halves of the record step must be inside ONE ``to_thread``."""
        import inspect

        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for fn in (mcp_mod.api_mcp_probe, mcp_mod._run_mcp_probe):
            src = inspect.getsource(fn)
            assert "to_thread(_record_probe_verdicts, result)" in src

    def test_the_reprobe_is_armed_after_the_last_await(self):
        """The annotation I added is an ``await``, and an ``await`` after
        ``create_task`` hands the loop to the new task: a fast probe completes and
        its done-callback drops it from ``_background_tasks`` before the caller can
        see a reprobe was armed. That regressed two upstream tests.

        The fix is an ordering, so this pins the ordering rather than the symptom:
        in every handler that arms a reprobe, the ``_arm_reprobe`` call must come
        AFTER the handler's final ``await``. It must also be adjacent to the flag
        set -- a flag set on a path that then fails to create the task stays True
        for the life of the process and silently disables re-probing.
        """
        import inspect

        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for fn in (mcp_mod.api_mcp_servers, mcp_mod.api_mcp_probe_cached):
            src = inspect.getsource(fn)
            assert "_arm_reprobe(request)" in src, f"{fn.__name__} no longer arms a reprobe"
            arm_at = src.rindex("_arm_reprobe(request)")
            last_await = src.rindex("await ")
            assert last_await < arm_at, (
                f"{fn.__name__} awaits after arming the reprobe; the task can "
                "complete and self-discard before the response is returned"
            )
            # Flag and task together, so neither can happen without the other.
            flag_at = src.rindex("_mcp_probe_in_progress = True")
            assert 0 < arm_at - flag_at < 200, (
                f"{fn.__name__} sets the in-flight flag away from the arming; a "
                "path that sets it without creating the task wedges re-probing"
            )

    def test_healthy_rows_are_left_byte_identical(self, store):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        rows: list[dict] = [{"name": "healthy", "status": "ok"}]
        mcp_mod._annotate_quarantine(rows)
        assert rows == [{"name": "healthy", "status": "ok"}]

    def test_a_failing_row_carries_the_count_before_the_threshold(self, store):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mcp_quarantine.record_verdicts(_fail("airbnb"))
        rows: list[dict] = [{"name": "airbnb", "status": "error"}]
        mcp_mod._annotate_quarantine(rows)
        assert rows[0]["probeFailures"] == 1
        assert rows[0]["probeFailing"] is False

    def test_a_row_over_the_threshold_says_so(self, store):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        rows: list[dict] = [{"name": "airbnb", "status": "error"}]
        mcp_mod._annotate_quarantine(rows)
        assert rows[0]["probeFailing"] is True
        assert rows[0]["probeFailures"] == 3

    def test_every_server_is_counted(self, store):
        """Including Kiro Crew's own. An earlier revision filtered these out so no
        badge could claim an unmount that did not happen; nothing is unmounted
        now, so withholding the count would only hide information.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        rows = [
            {"name": "kirocrew-core", "status": "error", "error": "x"},
            {"name": "airbnb", "status": "error", "error": "x"},
        ]
        assert mcp_mod._quarantine_verdicts(rows) == [
            ("kirocrew-core", "error", "x"),
            ("airbnb", "error", "x"),
        ]

    def test_verdict_extraction_tolerates_missing_keys(self, store):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        assert mcp_mod._quarantine_verdicts([{}]) == []
        assert mcp_mod._quarantine_verdicts([{"name": "a", "status": "ok", "error": None}]) == [
            ("a", "ok", "")
        ]

    def test_a_declared_listing_is_not_a_verdict(self, store):
        """``probeMode: "declared"`` reports the tools a managed package declares
        when the sandbox could not probe it. Discovery sets ``status = "ok"`` for
        it while its own comment says nothing verified the server can start, so
        letting that ``ok`` through would clear a real failure streak without a
        single successful handshake.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        rows = [
            {"name": "declared-srv", "status": "ok", "error": "", "probeMode": "declared"},
            {"name": "real-srv", "status": "ok", "error": "", "probeMode": "handshake"},
        ]
        assert mcp_mod._quarantine_verdicts(rows) == [("real-srv", "ok", "")]

    def test_a_declared_ok_does_not_erase_a_failure_streak(self, store):
        """End to end, through the store rather than just the filter."""
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("srv"))
        assert _failing() == {"srv"}

        # The sandbox goes unavailable and the next round reports a declared
        # listing for the same server.
        declared = [{"name": "srv", "status": "ok", "error": "", "probeMode": "declared"}]
        mcp_quarantine.record_verdicts(mcp_mod._quarantine_verdicts(declared))
        assert _failing() == {"srv"}
        assert mcp_quarantine.state_for("srv")["fails"] == 3

        # A real handshake still clears it.
        proven = [{"name": "srv", "status": "ok", "error": "", "probeMode": "handshake"}]
        mcp_quarantine.record_verdicts(mcp_mod._quarantine_verdicts(proven))
        assert mcp_quarantine.state_for("srv") is None
