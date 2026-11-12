"""The restore-time recovery of a render flag whose claim outlived the slot save.

``load_claimed_row_groups`` and ``apply_claimed_rows``: a restore puts back the
``mcp_app`` flag a durable claim names, gives each claimed occurrence its own lead,
refuses an oversized sidecar rather than reading it, and bounds both one claim's
row identities and the number of claims it keeps; the rows a claim names are
persisted before the claim is taken. Marker parsing, ``load_spool``,
``handle_tool_result`` and the claim itself are in ``test_mcp_apps_render.py``; the
shared fixture and builders are in ``mcp_apps_render_helpers.py``.
"""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest
from mcp_apps_render_helpers import spool_fixture  # noqa: F401
from mcp_apps_render_helpers import _FakeState, _hex, _write_spool

from kiro_crew import mcp_apps_render


def _reconcile(slot_key: str, rows: list[dict]) -> int:
    """Drive both halves of the restore-time pass, as a restore driver does.

    ``load_claimed_row_groups`` reads the spool and ``apply_claimed_rows`` flags the
    rows; they are separate so a loop-affine caller can put the read on a worker
    thread. Taking the key here is the point: the read is what a bare-vs-canonical
    key gets wrong.
    """
    claims = mcp_apps_render.load_claimed_row_groups(slot_key)
    return mcp_apps_render.apply_claimed_rows(claims, rows)


class TestARestoreRecoversAFlagItsClaimOutlived:
    """The claim is durable at once; the row's flag waits for the slot save.

    A gateway death between the writes leaves a spent claim on an unflagged row. The
    in-turn recovery runs only on the live path and the marker was stripped, so the
    restore reads the claim's ``{slot_key, rows}`` body back.
    """

    def _claim(self, spool, sid, slot_key, rows, call="tc"):
        (spool / f"{sid}.rendered").write_text(
            json.dumps({"slot_key": slot_key, "tool_call_id": call, "rows": rows}),
            encoding="utf-8",
        )

    def test_the_key_the_claim_was_written_under_is_the_key_that_finds_it(self, spool):
        """A claim is keyed by the CANONICAL producing session, not the bare slot.

        ``_take_claim`` receives ``binding_key``; other chat events route on the bare
        slot key. A restore asking with the bare key would silently match nothing.
        """
        sid = _hex()
        ts = "2026-01-01T00:00:00.000001Z"
        canonical = "dashboard:chat-1-1"
        self._claim(spool, sid, canonical, [ts])

        bare_rows = [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}]
        assert _reconcile("chat-1-1", bare_rows) == 0, "a bare key must not match"
        assert "mcp_app" not in bare_rows[0]["meta"]

        canonical_rows = [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}]
        assert _reconcile(canonical, canonical_rows) == 1
        assert canonical_rows[0]["meta"]["mcp_app"] is True

    def test_the_spool_read_and_the_flagging_are_separable(self, spool):
        """The read half is callable alone, which is what lets it leave the loop.

        A loop-affine restore driver hands ``load_claimed_row_groups`` to a worker
        thread and then applies on the loop. If the read were fused into the
        apply, that split would be impossible and the driver would stall the
        gateway on a spool scan, so the two halves staying separate IS the fix.
        """
        sid = _hex()
        ts = "2026-01-01T00:00:00.000001Z"
        self._claim(spool, sid, "dashboard:1", [ts])

        claims = mcp_apps_render.load_claimed_row_groups("dashboard:1")
        assert claims == [{ts}], "the read half alone must answer which rows are claimed"

        rows = [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}]
        assert mcp_apps_render.apply_claimed_rows(claims, rows) == 1
        assert rows[0]["meta"]["mcp_app"] is True

    def test_the_apply_half_touches_no_file(self, monkeypatch):
        """The half that runs on the loop must do no filesystem work at all.

        Pinned by making a spool read raise: the apply half never reaches one, so
        it is unaffected. This is what makes it safe on the event loop.
        """
        ts = "2026-01-01T00:00:00.000001Z"

        def _boom(*_a, **_k):
            raise AssertionError("the apply half must not touch the spool")

        monkeypatch.setattr(mcp_apps_render, "_spool_dir", _boom)
        # flake-ok: pre-existing os.scandir rebound process-wide through mcp_apps_render's os alias, scoped to this test by the patch, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(mcp_apps_render.os, "scandir", _boom)

        rows = [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}]
        assert mcp_apps_render.apply_claimed_rows([{ts}], rows) == 1
        assert rows[0]["meta"]["mcp_app"] is True

    def test_a_missing_spool_logs_no_traceback(self, tmp_path, monkeypatch, caplog):
        """An absent spool is the normal state before any app renders: it is
        logged as a plain debug line, not a stack trace on every rehydrate."""
        monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", str(tmp_path / "never-created"))

        with caplog.at_level("DEBUG", logger=mcp_apps_render.logger.name):
            assert mcp_apps_render.load_claimed_row_groups("dashboard:1") == []

        records = [r for r in caplog.records if "claim reconcile skipped" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is None

    def test_an_unreadable_spool_keeps_its_traceback(self, spool, monkeypatch, caplog):
        """A spool that exists but cannot be read is abnormal: the debug line
        keeps its traceback so the cause is diagnosable."""

        def _denied(_path):
            raise PermissionError(13, "Permission denied")

        # flake-ok: pre-existing os.scandir rebound process-wide through mcp_apps_render's os alias, scoped to this test by the patch, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(mcp_apps_render.os, "scandir", _denied)

        with caplog.at_level("DEBUG", logger=mcp_apps_render.logger.name):
            assert mcp_apps_render.load_claimed_row_groups("dashboard:1") == []

        records = [r for r in caplog.records if "claim reconcile skipped" in r.getMessage()]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert isinstance(records[0].exc_info[1], PermissionError)

    def test_a_spent_claim_puts_the_flag_back_on_its_row(self, spool):
        sid = _hex()
        ts = "2026-01-01T00:00:00.000001Z"
        self._claim(spool, sid, "dashboard:1", [ts])
        rows = [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}]

        n = _reconcile("dashboard:1", rows)

        assert n == 1
        assert rows[0]["meta"]["mcp_app"] is True
        assert rows[0]["meta"]["mcp_app_lead"] is True, "a recovered row must be drawable"

    def test_a_claim_owned_by_another_slot_is_not_applied(self, spool):
        """The claim names its slot, so one session cannot take another's flag."""
        sid = _hex()
        ts = "2026-01-01T00:00:00.000002Z"
        self._claim(spool, sid, "dashboard:OTHER", [ts])
        rows = [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}]

        n = _reconcile("dashboard:1", rows)

        assert n == 0
        assert "mcp_app" not in rows[0]["meta"]

    def test_a_row_not_named_by_any_claim_is_left_alone(self, spool):
        sid = _hex()
        self._claim(spool, sid, "dashboard:1", ["2026-01-01T00:00:00.000003Z"])
        rows = [
            {"role": "tool", "ts": "2026-01-01T00:00:09.000009Z", "meta": {"tool_call_id": "tc"}}
        ]

        n = _reconcile("dashboard:1", rows)

        assert n == 0
        assert "mcp_app" not in rows[0]["meta"]

    def test_only_the_first_recovered_row_of_a_call_takes_the_lead(self, spool):
        """One notice per call after recovery, exactly as the live path writes it."""
        sid = _hex()
        first = "2026-01-01T00:00:00.000004Z"
        second = "2026-01-01T00:00:00.000005Z"
        self._claim(spool, sid, "dashboard:1", [first, second])
        rows = [
            {"role": "tool", "ts": first, "meta": {"tool_call_id": "tc"}},
            {"role": "tool", "ts": second, "meta": {"tool_call_id": "tc"}},
        ]

        n = _reconcile("dashboard:1", rows)

        assert n == 2, "both rows of the occurrence carry the flag"
        assert rows[0]["meta"].get("mcp_app_lead") is True
        assert "mcp_app_lead" not in rows[1]["meta"], "the sibling must stay bare"

    def test_an_already_flagged_row_is_not_rewritten(self, spool):
        """A row whose flag survived keeps the lead marker the turn gave it.

        Re-deciding the lead here would move the notice on a row that was already
        correct, and this pass runs on every restore.
        """
        sid = _hex()
        first = "2026-01-01T00:00:00.000006Z"
        second = "2026-01-01T00:00:00.000007Z"
        self._claim(spool, sid, "dashboard:1", [first, second])
        rows = [
            {
                "role": "tool",
                "ts": first,
                "meta": {"tool_call_id": "tc", "mcp_app": True, "mcp_app_lead": True},
            },
            {"role": "tool", "ts": second, "meta": {"tool_call_id": "tc", "mcp_app": True}},
        ]

        n = _reconcile("dashboard:1", rows)

        assert n == 0, "nothing needed recovering"
        assert rows[0]["meta"]["mcp_app_lead"] is True
        assert "mcp_app_lead" not in rows[1]["meta"]

    def test_a_legacy_empty_claim_recovers_nothing(self, spool):
        """It names no slot and no row, so it cannot say which row to flag.

        This is the artifact the shipped code leaves on every render, so treating
        it as a recovery source would flag rows at random across the transcript.
        """
        sid = _hex()
        (spool / f"{sid}.rendered").write_bytes(b"")
        rows = [
            {"role": "tool", "ts": "2026-01-01T00:00:00.000008Z", "meta": {"tool_call_id": "tc"}}
        ]

        n = _reconcile("dashboard:1", rows)

        assert n == 0
        assert "mcp_app" not in rows[0]["meta"]

    def test_running_it_twice_changes_nothing_further(self, spool):
        """Restore runs on every boot, so the pass has to be idempotent."""
        sid = _hex()
        ts = "2026-01-01T00:00:00.000010Z"
        self._claim(spool, sid, "dashboard:1", [ts])
        rows = [{"role": "tool", "ts": ts, "meta": {"tool_call_id": "tc"}}]

        first = _reconcile("dashboard:1", rows)
        snapshot = dict(rows[0]["meta"])
        second = _reconcile("dashboard:1", rows)

        assert (first, second) == (1, 0)
        assert rows[0]["meta"] == snapshot
        assert (spool / f"{sid}.rendered").exists(), "the pass must not consume the claim"


class TestEachClaimGetsItsOwnLead:
    """Two app occurrences can share a ``tool_call_id``, so the lead cannot key on it.

    A reset that keeps the transcript can hand a new call an id an older row used. A
    lead keyed on the id leaves the newer occurrence without one, so the claim is
    the key.
    """

    def _claim(self, spool, sid, slot_key, rows, call="tc"):
        (spool / f"{sid}.rendered").write_text(
            json.dumps({"slot_key": slot_key, "tool_call_id": call, "rows": rows}),
            encoding="utf-8",
        )

    def _row(self, ts, *, flagged=False, lead=False, call="tc"):
        meta = {"tool_call_id": call}
        if flagged:
            meta["mcp_app"] = True
        if lead:
            meta["mcp_app_lead"] = True
        return {"role": "tool", "ts": ts, "meta": meta}

    def test_a_reused_call_id_on_an_older_flagged_row_does_not_steal_the_lead(self, spool):
        key = "dashboard:chat-1-1"
        old, new = "2026-01-01T00:00:00.000001Z", "2026-01-01T00:00:00.000002Z"
        self._claim(spool, _hex(), key, [old])
        self._claim(spool, _hex(), key, [new])
        # The older occurrence's flag reached disk; the newer one's did not, and both
        # rows carry the SAME call id because a reset reissued it.
        rows = [self._row(old, flagged=True, lead=True), self._row(new)]

        assert _reconcile(key, rows) == 1

        assert rows[1]["meta"]["mcp_app"] is True
        assert rows[1]["meta"].get("mcp_app_lead") is True, (
            "the newer occurrence got no lead, so its absence notice never renders "
            "and the two lost apps read as one"
        )
        assert rows[0]["meta"] == {
            "tool_call_id": "tc",
            "mcp_app": True,
            "mcp_app_lead": True,
        }, "a row whose flag survived must be left byte-identical"

    def test_two_claims_sharing_an_id_each_draw_their_own_notice(self, spool):
        """Neither is older: both flags were lost, and both must lead."""
        key = "dashboard:chat-1-1"
        a, b = "2026-01-01T00:00:00.000001Z", "2026-01-01T00:00:00.000002Z"
        self._claim(spool, _hex(), key, [a])
        self._claim(spool, _hex(), key, [b])
        rows = [self._row(a), self._row(b)]

        assert _reconcile(key, rows) == 2

        assert [r["meta"].get("mcp_app_lead") for r in rows] == [True, True]

    def test_one_claim_naming_two_rows_still_gets_exactly_one_lead(self, spool):
        """The grouping must not become one lead PER ROW, which is the other way to fail."""
        key = "dashboard:chat-1-1"
        a, b = "2026-01-01T00:00:00.000001Z", "2026-01-01T00:00:00.000002Z"
        self._claim(spool, _hex(), key, [a, b])
        rows = [self._row(a), self._row(b)]

        assert _reconcile(key, rows) == 2

        leads = [r["meta"].get("mcp_app_lead") for r in rows]
        assert leads == [True, None], "one occurrence draws once, on its earliest row"

    def test_a_claim_whose_rows_are_half_flagged_gives_no_second_lead(self, spool):
        """A sidecar naming a mix is disk content this pass does not author.

        It is the one input that reaches the already-flagged branch's own
        ``lead_taken`` write, and without that write the recovered sibling would
        draw a second notice for an occurrence that already has one on disk.
        """
        key = "dashboard:chat-1-1"
        a, b = "2026-01-01T00:00:00.000001Z", "2026-01-01T00:00:00.000002Z"
        self._claim(spool, _hex(), key, [a, b])
        rows = [self._row(a, flagged=True, lead=True), self._row(b)]

        assert _reconcile(key, rows) == 1

        assert rows[1]["meta"]["mcp_app"] is True
        assert (
            "mcp_app_lead" not in rows[1]["meta"]
        ), "this occurrence's lead is already on disk, so a second one draws twice"


class TestAnOversizedSidecarIsRefusedRatherThanRead:
    """The caps bound what a read KEEPS; this bounds what it READS.

    A sidecar is untrusted disk content; capping after a whole read still loads it
    all. The limit is derived from the two caps, so no claim this code writes is
    refused.
    """

    def test_the_limit_is_above_the_largest_claim_this_code_writes(self):
        widest = ["m" * mcp_apps_render._MAX_ROW_ID_LEN] * (mcp_apps_render._MAX_CLAIM_ROWS * 2)
        payload = mcp_apps_render._claim_payload("dashboard:s", "tc", widest, widest)

        assert (
            len(payload) <= mcp_apps_render._MAX_CLAIM_BYTES
        ), "a claim this code writes at full width must not trip the read limit"

    def test_the_restore_ignores_an_oversized_sidecar(self, spool):
        key = "dashboard:huge"
        path = spool / f"{_hex()}.rendered"
        # The padding is TRAILING WHITESPACE, which `json.loads` accepts, so the body
        # still parses after the read truncates it. That is what makes this probe
        # honest: if the refusal were merely a parse error on a truncated body, the
        # size check could be deleted and this would still pass. It cannot, so only the
        # size check can account for the empty result.
        body = json.dumps({"slot_key": key, "tool_call_id": "tc", "row_ids": ["m-1"]})
        path.write_text(
            body + " " * (mcp_apps_render._MAX_CLAIM_BYTES + 1024 - len(body)),
            encoding="utf-8",
        )
        assert path.stat().st_size > mcp_apps_render._MAX_CLAIM_BYTES
        truncated = path.read_bytes()[: mcp_apps_render._MAX_CLAIM_BYTES + 1]
        assert json.loads(truncated.decode())["row_ids"] == ["m-1"], (
            "the probe is only meaningful while the TRUNCATED body still parses and "
            "matches: otherwise a parse error, not the size check, explains the refusal"
        )

        assert (
            mcp_apps_render.load_claimed_row_groups(key) == []
        ), "an oversized sidecar must be refused on its SIZE, not parsed"

    def test_an_oversized_sentinel_is_inert_on_the_live_path(self, tmp_path):
        sentinel = tmp_path / "abc.rendered"
        # Trailing whitespace again, so the truncated body parses and OVERLAPS the
        # asking row. Without the size check this claim would attribute rather than go
        # inert, which is what makes the assertion below about the size and nothing
        # else.
        body = json.dumps({"slot_key": "dashboard:s", "row_ids": ["m-1"]})
        sentinel.write_text(
            body + " " * (mcp_apps_render._MAX_CLAIM_BYTES + 1024 - len(body)),
            encoding="utf-8",
        )

        out = mcp_apps_render._attribute_spent_claim(
            sentinel, "abc", "dashboard:s", "tc", ["2026-01-01T00:00:01.000001Z"], ["m-1"]
        )

        assert out == "inert", (
            "an unreadable claim must not be handed to the asking row: that re-renders "
            "an app on whichever row replayed the marker"
        )

    def test_a_slot_key_too_long_to_read_back_is_omitted_not_raised(self):
        """An unencodable key costs the attribution only, never the render.

        A truncated key is another slot's real key, so it is omitted. Raising would hit
        the blanket handler, which strips the marker and records nothing, so the app
        would be neither rendered nor recoverable.
        """
        payload = mcp_apps_render._claim_payload(
            "d:" + "x" * mcp_apps_render._MAX_SLOT_KEY_LEN, "tc", ["t"], ["m"]
        )

        body = json.loads(payload)
        assert "slot_key" not in body
        assert body["row_ids"] == ["m"]
        assert len(payload) <= mcp_apps_render._MAX_CLAIM_BYTES

    def test_a_claim_with_no_slot_key_matches_no_slot_rather_than_every_slot(self, spool):
        """Omitting the key must fail towards silence, which is the direction that matters.

        A body with no key read as matching ANY slot would attribute one slot's rows to
        another, which is worse than the loss it replaces. The read compares the stored
        key against the caller's, and a missing field is never equal to a real one.
        """
        long_key = "d:" + "x" * mcp_apps_render._MAX_SLOT_KEY_LEN
        (spool / f"{_hex()}.rendered").write_bytes(
            mcp_apps_render._claim_payload(long_key, "tc", ["t"], ["m-1"])
        )

        assert mcp_apps_render.load_claimed_row_groups(long_key) == []
        assert mcp_apps_render.load_claimed_row_groups("dashboard:someone-else") == []

    def test_an_unencodable_key_is_not_truncated_onto_the_slot_that_owns_that_prefix(self, spool):
        """This is why the key is omitted rather than trimmed to fit like every other field.

        A truncated key is not a harmless near-miss: it is a REAL key, owned by whichever
        slot's own key happens to be that prefix. Trimming would therefore hand this
        claim's rows to that slot and flag a row there that never had an app -- an
        attribution to the wrong slot, where omitting loses the attribution and stops.
        """
        victim = "dashboard:" + "x" * (mcp_apps_render._MAX_SLOT_KEY_LEN - len("dashboard:"))
        assert len(victim) == mcp_apps_render._MAX_SLOT_KEY_LEN
        overlong = victim + "-suffix-that-pushes-it-over"

        (spool / f"{_hex()}.rendered").write_bytes(
            mcp_apps_render._claim_payload(overlong, "tc", ["t"], ["m-1"])
        )

        assert mcp_apps_render.load_claimed_row_groups(victim) == []

    def test_a_claim_at_full_width_still_fits_the_byte_limit(self):
        """The limit accounts for EVERY field, which is the property that kept failing.

        A key at its own limit plus both identity lists at theirs is the largest body
        this code can write, and it has to stay under the read limit -- otherwise the
        reader refuses a claim this code wrote and the notice is lost in exactly the
        crash window the claim exists for.
        """
        widest_ids = ["m" * mcp_apps_render._MAX_ROW_ID_LEN] * (mcp_apps_render._MAX_CLAIM_ROWS * 2)
        payload = mcp_apps_render._claim_payload(
            "d" * mcp_apps_render._MAX_SLOT_KEY_LEN, "tc", widest_ids, widest_ids
        )

        assert len(payload) <= mcp_apps_render._MAX_CLAIM_BYTES

    def test_a_sidecar_at_ordinary_size_still_reads(self, spool):
        key = "dashboard:normal"
        (spool / f"{_hex()}.rendered").write_text(
            json.dumps({"slot_key": key, "tool_call_id": "tc", "row_ids": ["m-1"]}),
            encoding="utf-8",
        )

        assert mcp_apps_render.load_claimed_row_groups(key) == [{"m-1"}]


class TestOneClaimCarriesABoundedNumberOfRowIdentities:
    """Capping the claims is not a bound while one claim's set can grow freely.

    Row identities come from all tool rows sharing one ``tool_call_id``, so repeating
    frames grow one claim. Both write and load sides are bounded, since the sidecar
    is untrusted disk content.
    """

    def test_the_write_side_caps_what_one_claim_records(self):
        many = [f"m-{i:05d}" for i in range(mcp_apps_render._MAX_CLAIM_ROWS * 3)]

        payload = json.loads(
            mcp_apps_render._claim_payload("dashboard:s", "tc", many, many).decode()
        )

        assert len(payload["row_ids"]) == mcp_apps_render._MAX_CLAIM_ROWS
        assert len(payload["rows"]) == mcp_apps_render._MAX_CLAIM_ROWS

    def test_the_load_side_caps_a_claim_it_did_not_write(self, spool):
        many = [f"m-{i:05d}" for i in range(mcp_apps_render._MAX_CLAIM_ROWS * 3)]
        key = "dashboard:oversized"
        (spool / f"{_hex()}.rendered").write_text(
            json.dumps({"slot_key": key, "tool_call_id": "tc", "row_ids": many}),
            encoding="utf-8",
        )

        groups = mcp_apps_render.load_claimed_row_groups(key)

        assert len(groups) == 1
        assert len(groups[0]) == mcp_apps_render._MAX_CLAIM_ROWS, (
            "a claim written by something other than this code must still cost a "
            "bounded amount of memory to read"
        )

    def test_an_overlong_identity_is_dropped_not_retained(self, spool):
        key = "dashboard:overlong"
        good = "m-good"
        overlong = "m-" + "x" * mcp_apps_render._MAX_ROW_ID_LEN
        (spool / f"{_hex()}.rendered").write_text(
            json.dumps({"slot_key": key, "tool_call_id": "tc", "row_ids": [good, overlong]}),
            encoding="utf-8",
        )

        groups = mcp_apps_render.load_claimed_row_groups(key)

        assert groups == [{good}], "an identity longer than a mid or a ts is malformed"

    def test_a_claim_of_ordinary_size_is_untouched(self, spool):
        key = "dashboard:ordinary"
        ids = ["m-1", "m-2"]
        (spool / f"{_hex()}.rendered").write_text(
            json.dumps({"slot_key": key, "tool_call_id": "tc", "row_ids": ids}),
            encoding="utf-8",
        )

        groups = mcp_apps_render.load_claimed_row_groups(key)

        assert groups == [set(ids)], "the bound must not touch a real occurrence"


class TestTheRestoreKeepsABoundedNumberOfClaims:
    """One restore's retention is capped, and the cap drops the OLDEST claims.

    The spool is swept at 24h, so a busy slot leaves far more sidecars than a restore
    can match. The order is pinned, not just the count: the newest claims are the
    ones a restored row can still match.
    """

    def _claim_at(self, spool, slot_key, stamp, mtime):
        path = spool / f"{_hex()}.rendered"
        path.write_text(
            json.dumps({"slot_key": slot_key, "tool_call_id": "tc", "rows": [stamp]}),
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))
        return path

    KEY = "dashboard:chat-cap-1"

    def _spread(self, spool, count):
        """`count` claims for one slot, oldest first, each with its own mtime."""
        stamps = [f"2026-01-01T00:00:{i:02d}.000001Z" for i in range(count)]
        for index, stamp in enumerate(stamps):
            self._claim_at(spool, self.KEY, stamp, 1_000_000.0 + index)
        return stamps

    def test_retention_stops_at_the_cap(self, spool, monkeypatch):
        monkeypatch.setattr(mcp_apps_render, "_MAX_RESTORE_CLAIMS", 3)
        self._spread(spool, 8)

        groups = mcp_apps_render.load_claimed_row_groups(self.KEY)

        assert len(groups) == 3, "a restore must not retain one group per spooled claim"

    def test_the_cap_drops_the_oldest_claims_not_arbitrary_ones(self, spool, monkeypatch):
        monkeypatch.setattr(mcp_apps_render, "_MAX_RESTORE_CLAIMS", 3)
        stamps = self._spread(spool, 8)

        groups = mcp_apps_render.load_claimed_row_groups(self.KEY)
        kept = set().union(*groups) if groups else set()

        assert kept == set(stamps[-3:]), (
            "the cap must keep the NEWEST claims: those are the ones a restored row "
            "can still match, and an arbitrary scan order would drop them"
        )

    def test_a_slot_under_the_cap_is_unaffected(self, spool, monkeypatch):
        monkeypatch.setattr(mcp_apps_render, "_MAX_RESTORE_CLAIMS", 3)
        stamps = self._spread(spool, 2)

        groups = mcp_apps_render.load_claimed_row_groups(self.KEY)
        kept = set().union(*groups) if groups else set()

        assert kept == set(stamps), "the cap must not touch a slot below it"

    def test_the_newest_win_whatever_order_the_directory_reports(self, spool, monkeypatch):
        """The cap must not inherit the directory's order.

        Some filesystems already list newest-first, so the scan is handed the oldest
        first on purpose: keeping the newest three can then only be the code's order.
        """
        monkeypatch.setattr(mcp_apps_render, "_MAX_RESTORE_CLAIMS", 3)
        stamps = self._spread(spool, 8)

        real_scandir = os.scandir

        def oldest_first(path):
            # A generator, not a list: the code under test closes the directory handle
            # it is given, and a list has no ``close``. This also keeps the stub honest
            # about being streamed rather than materialised.
            ordered = sorted(real_scandir(path), key=lambda entry: entry.stat().st_mtime)
            return (entry for entry in ordered)

        # Confined to the one call: the real ``os.scandir`` is a context manager and
        # the fixture's own teardown uses it that way, so leaving a plain-list stub
        # installed past this line breaks cleanup rather than the code under test.
        # flake-ok: pre-existing os.scandir rebound process-wide through mcp_apps_render's os alias, scoped to this test by the patch, moved verbatim; tracked for a de-flake pass
        with mock.patch.object(mcp_apps_render.os, "scandir", oldest_first):
            groups = mcp_apps_render.load_claimed_row_groups(self.KEY)

        kept = set().union(*groups) if groups else set()

        assert kept == set(stamps[-3:]), (
            "the newest claims must win even when the directory reports the oldest "
            "first, or the cap is keeping whatever the filesystem happened to list"
        )

    def test_the_directory_handle_is_closed(self, spool, monkeypatch):
        """The scan is streamed, so the handle it opens has to be closed here.

        Left to the collector, the handle leaks under repeated restores.
        """
        monkeypatch.setattr(mcp_apps_render, "_MAX_RESTORE_CLAIMS", 3)
        self._spread(spool, 5)

        real_scandir = os.scandir
        closed: list[bool] = []

        class _Tracked:
            def __init__(self, inner):
                self._inner = inner

            def __iter__(self):
                return iter(self._inner)

            def close(self):
                closed.append(True)

        # flake-ok: pre-existing os.scandir rebound process-wide through mcp_apps_render's os alias, scoped to this test by the patch, moved verbatim; tracked for a de-flake pass
        with mock.patch.object(
            mcp_apps_render.os, "scandir", lambda path: _Tracked(list(real_scandir(path)))
        ):
            mcp_apps_render.load_claimed_row_groups(self.KEY)

        assert closed == [True], "the directory handle must be closed by this pass"

    def test_the_shipped_cap_is_a_real_bound(self):
        cap = mcp_apps_render._MAX_RESTORE_CLAIMS
        assert isinstance(cap, int) and cap > 0, "the shipped cap must bound something"


# --- the rows a claim names must be durable first ---------------------------


@pytest.mark.asyncio
async def test_the_owner_rows_are_persisted_before_the_claim_is_taken(spool, monkeypatch):
    """A durable claim must be strictly newer than the rows it names.

    The sidecar is durable at once, the rows only after a flush. A hard exit between
    them leaves a claim on rows that never reached disk, which neither the
    ``"spent"`` branch nor the restore pass can recover.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    order: list[str] = []
    real_take = mcp_apps_render._take_claim

    def _recording_take(*a, **k):
        order.append("claim")
        return real_take(*a, **k)

    monkeypatch.setattr(mcp_apps_render, "_take_claim", _recording_take)

    async def _persist():
        order.append("persist")

    _, flagged = await mcp_apps_render.handle_tool_result(
        _FakeState(),
        slot_key="dashboard:9",
        tool_call_id="tc-order",
        text=f"a [kirocrew-mcp-app:{sid}] b",
        row_ts=["5.5"],
        row_ids=["m-9"],
        persist_rows=_persist,
    )
    # PREMISE: the claim really was taken, so the ordering asserted below is
    # about a live claim rather than a path that never reached one.
    assert flagged is True
    assert order == [
        "persist",
        "claim",
    ], "the rows a claim names must reach disk before the claim does"


@pytest.mark.asyncio
async def test_a_claim_that_names_no_row_persists_nothing(spool):
    """A claim that attributes nothing needs no row on disk.

    The collapsed-id path passes no rows; the claim is still written to bar a second
    render, and a flush would be a wasted transcript write.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    calls: list[str] = []

    async def _persist():
        calls.append("persist")

    _, flagged = await mcp_apps_render.handle_tool_result(
        _FakeState(),
        slot_key="dashboard:9",
        tool_call_id="tc-norow",
        text=f"a [kirocrew-mcp-app:{sid}] b",
        persist_rows=_persist,
    )
    assert flagged is True, "the render-once claim is still taken"
    assert calls == [], "a claim naming no row must not spend a transcript write"


@pytest.mark.asyncio
async def test_a_persist_that_raises_takes_no_claim(spool):
    """The persist is a PRECONDITION of the claim, not a call beside it.

    When it raises, this seam's own error path returns the stripped text with no
    flag and the claim was never created -- so the record stays claimable and a
    later render can still take it. Taking the claim anyway would leave exactly
    the sidecar-without-rows that ordering the persist first exists to prevent.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})

    async def _persist():
        raise RuntimeError("the transcript write failed")

    out, flagged = await mcp_apps_render.handle_tool_result(
        _FakeState(),
        slot_key="dashboard:9",
        tool_call_id="tc-raise",
        text=f"a [kirocrew-mcp-app:{sid}] b",
        row_ts=["7.5"],
        row_ids=["m-7"],
        persist_rows=_persist,
    )
    assert flagged is False
    assert sid not in out
    assert not (
        spool / f"{sid}.rendered"
    ).exists(), "no claim may be taken when the rows it would name are not on disk"
