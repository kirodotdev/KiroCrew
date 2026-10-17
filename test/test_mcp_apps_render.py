"""Tests for dashboard-side MCP Apps marker interception (mcp_apps_render).

The restore-time recovery of a flag whose claim outlived the slot save, and the
persist-before-claim order, are in ``test_mcp_apps_render_claim_recovery.py``;
the spool fixture and builders both share are in ``mcp_apps_render_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
from pathlib import Path
from unittest import mock

import pytest
from mcp_apps_render_helpers import spool_fixture  # noqa: F401
from mcp_apps_render_helpers import _FakeState, _hex, _write_spool

from kiro_crew import mcp_apps_render

#: A credential-shaped value for the redaction tests. JWT-shaped rather than the
#: more familiar cloud access-key shape because an access-key literal is matched
#: by GitHub's own push protection, which refuses the push outright -- and writing
#: one in fragments to slip past that check would be hiding a credential pattern
#: from a security control for a value that is not a credential at all. What the
#: redaction tests need is only that the detector matches it, which they assert.
FAKE_CREDENTIAL = "eyJhbGciOiJIUzI1NiJ9." + "a" * 30 + "." + "s" * 20

# ── marker regex ─────────────────────────────────────────────────────────────


def test_find_marker_valid_id():
    sid = _hex()
    assert mcp_apps_render.find_marker(f"done [kirocrew-mcp-app:{sid}] ok") == sid


def test_find_marker_none_when_absent():
    assert mcp_apps_render.find_marker("plain tool output") is None
    assert mcp_apps_render.find_marker("") is None
    assert mcp_apps_render.find_marker(None) is None


def test_find_marker_rejects_wrong_length():
    short = "a" * 31
    long = "a" * 33
    assert mcp_apps_render.find_marker(f"[kirocrew-mcp-app:{short}]") is None
    # 33 hex chars: the regex matches the first 32 only if followed by ']', so a
    # 33-char body does NOT form a valid closed marker → no match.
    assert mcp_apps_render.find_marker(f"[kirocrew-mcp-app:{long}]") is None


def test_find_marker_rejects_uppercase():
    upper = "A" * 32
    assert mcp_apps_render.find_marker(f"[kirocrew-mcp-app:{upper}]") is None
    mixed = "abcdef0123456789ABCDEF0123456789"
    assert mcp_apps_render.find_marker(f"[kirocrew-mcp-app:{mixed}]") is None


def test_find_marker_rejects_non_hex():
    bad = "g" * 32
    assert mcp_apps_render.find_marker(f"[kirocrew-mcp-app:{bad}]") is None


def test_strip_marker_removes_all():
    sid1, sid2 = _hex(), _hex()
    text = f"a[kirocrew-mcp-app:{sid1}]b[kirocrew-mcp-app:{sid2}]c"
    assert mcp_apps_render.strip_marker(text) == "abc"


def test_strip_marker_noop_without_marker():
    assert mcp_apps_render.strip_marker("hello") == "hello"
    assert mcp_apps_render.strip_marker("") == ""
    assert mcp_apps_render.strip_marker(None) == ""


# ── load_spool ───────────────────────────────────────────────────────────────


def test_load_spool_valid(spool):
    sid = _hex()
    payload = {
        "schema": 1,
        "server": "excalidraw",
        "tool": "create_view",
        "session_key": "dashboard:1",
        "html": "<h1>hi</h1>",
        "csp": "default-src 'self'",
        "permissions": ["app"],
        "structured_content": {"k": "v"},
        "created_at": "2026-07-23T00:00:00Z",
    }
    _write_spool(spool, sid, payload)
    assert mcp_apps_render.load_spool(sid) == payload


def test_load_spool_missing(spool):
    assert mcp_apps_render.load_spool(_hex()) is None


def test_load_spool_corrupt_json(spool):
    sid = _hex()
    (spool / f"{sid}.json").write_text("{not valid json", encoding="utf-8")
    assert mcp_apps_render.load_spool(sid) is None


def test_load_spool_non_object_json(spool):
    sid = _hex()
    (spool / f"{sid}.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert mcp_apps_render.load_spool(sid) is None


def test_load_spool_rejects_bad_id(spool):
    # Traversal / non-id inputs fail the id regex → None, and no path is built
    # from the input. Confirm no traversal file is ever read even if one exists.
    assert mcp_apps_render.load_spool("../../etc/passwd") is None
    assert mcp_apps_render.load_spool("../secrets") is None
    assert mcp_apps_render.load_spool("A" * 32) is None
    assert mcp_apps_render.load_spool("a" * 31) is None
    assert mcp_apps_render.load_spool("") is None
    assert mcp_apps_render.load_spool(None) is None  # type: ignore[arg-type]


def test_load_spool_traversal_cannot_reach_outside_file(spool, tmp_path):
    # Plant a sensitive file a traversal would target; prove it's unreachable
    # because the id regex rejects any path-bearing string.
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"secret": True}), encoding="utf-8")
    for attempt in (
        "../secret",
        "..%2f..%2fsecret",
        "/" + "a" * 31,
        "a" * 32 + "/../../secret",
    ):
        assert mcp_apps_render.load_spool(attempt) is None


def test_load_spool_oversized_ignored(spool, monkeypatch):
    sid = _hex()
    _write_spool(spool, sid, {"html": "x"})
    monkeypatch.setattr(mcp_apps_render, "_MAX_SPOOL_BYTES", 2)
    assert mcp_apps_render.load_spool(sid) is None


# ── handle_tool_result (the hook) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_tool_result_no_marker_passthrough(spool):
    st = _FakeState()
    out, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc1", text="just output"
    )
    assert out == "just output"
    assert st.calls == []
    # No marker means no app, and the caller must not persist a flag that would
    # put an app notice on an ordinary tool row.
    assert claimed is False


@pytest.mark.asyncio
async def test_handle_tool_result_broadcasts_and_strips(spool):
    sid = _hex()
    _write_spool(
        spool,
        sid,
        {
            "server": "excalidraw",
            "tool": "create_view",
            "html": "<h1>hi</h1>",
            "csp": "default-src 'self'",
            "permissions": ["app"],
            "structured_content": {"nodes": 3},
        },
    )
    st = _FakeState()
    text = f"result [kirocrew-mcp-app:{sid}] tail"
    out, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:7", tool_call_id="tc42", text=text
    )
    # Marker stripped from transcript text.
    assert sid not in out
    assert out == "result  tail"
    # This call took the record's one render, so the caller persists the flag.
    assert claimed is True
    # Exactly one mcp_app_render broadcast with the contract payload.
    assert len(st.calls) == 1
    msg_type, data = st.calls[0]
    assert msg_type == "mcp_app_render"
    assert data == {
        "session_key": "dashboard:7",
        "tool_call_id": "tc42",
        "server": "excalidraw",
        "tool": "create_view",
        "html": "<h1>hi</h1>",
        "csp": "default-src 'self'",
        "permissions": ["app"],
        "spool_id": sid,
        "callback_secret": "",
        "structured_content": {"nodes": 3},
        "tool_input": None,
        "result_content": None,
    }


@pytest.mark.asyncio
async def test_handle_tool_result_marker_but_missing_spool_still_strips(spool):
    sid = _hex()  # no file written
    st = _FakeState()
    text = f"x [kirocrew-mcp-app:{sid}] y"
    out, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc", text=text
    )
    # No spool → no broadcast, but marker still stripped so the user never sees it.
    assert sid not in out
    assert out == "x  y"
    assert st.calls == []
    # A marker whose record is gone produced no app at all, so there is nothing
    # for a row to point at. This is why a caller cannot read marker presence.
    assert claimed is False


@pytest.mark.asyncio
async def test_handle_tool_result_broadcast_exception_degrades_gracefully(spool):
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})

    class _BoomState:
        def broadcast_ws(self, *_a, **_k):
            raise RuntimeError("ws down")

    text = f"a [kirocrew-mcp-app:{sid}] b"
    # Must not raise; still returns stripped text.
    out, claimed = await mcp_apps_render.handle_tool_result(
        _BoomState(), slot_key="dashboard:1", tool_call_id="tc", text=text
    )
    assert sid not in out
    # The send raised, but the claim was already spent, so this app can never be
    # shown for this call. A reader still needs to be told it exists, which is
    # why the flag follows the CLAIM and not the dispatch.
    assert claimed is True


@pytest.mark.asyncio
async def test_handle_tool_result_offloads_spool_read(spool, monkeypatch):
    """The multi-MB spool read runs in a worker thread (asyncio.to_thread),
    never on the event loop thread that runs every co-scheduled chat task."""
    import threading

    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real = mcp_apps_render.load_spool

    def probe(spool_id):
        seen["thread"] = threading.get_ident()
        return real(spool_id)

    monkeypatch.setattr(mcp_apps_render, "load_spool", probe)
    st = _FakeState()
    out, claimed = await mcp_apps_render.handle_tool_result(
        st,
        slot_key="dashboard:1",
        tool_call_id="tc",
        text=f"pre [kirocrew-mcp-app:{sid}] post",
    )
    assert sid not in out
    assert claimed is True
    assert "thread" in seen and seen["thread"] != loop_thread
    assert len(st.calls) == 1


def test_load_spool_rejects_wrong_or_missing_schema(spool):
    """Fail-closed version gate: a stale reader must reject records it does
    not understand instead of silently mis-reading them."""
    sid_v2, sid_none = _hex(), _hex()
    _write_spool(spool, sid_v2, {"schema": 2, "html": "x"})
    payload = {"html": "x"}
    payload["schema"] = None  # explicit non-1 (helper would default it)
    _write_spool(spool, sid_none, payload)
    assert mcp_apps_render.load_spool(sid_v2) is None
    assert mcp_apps_render.load_spool(sid_none) is None


@pytest.mark.asyncio
async def test_handle_tool_result_replayed_marker_is_inert(spool):
    """Single-consume: a record renders at most once — a marker echoed into a
    later turn (LLM/transcript replay) must not re-render the app."""
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    st = _FakeState()
    text = f"a [kirocrew-mcp-app:{sid}] b"
    out1, claimed1 = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc1", text=text
    )
    out2, claimed2 = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc2", text=text
    )
    assert len(st.calls) == 1  # exactly one render
    assert sid not in out1 and sid not in out2  # marker always stripped
    # Only the call that took the claim reports it. The inert replay must not
    # flag its own row: tc2 produced no app of its own.
    assert (claimed1, claimed2) == (True, False)
    # The record itself survives the render claim — the app-call capability
    # path stays valid for the rendered app's lifetime.
    assert mcp_apps_render.load_spool(sid) is not None


@pytest.mark.asyncio
async def test_a_cancelled_redaction_leaves_the_record_claimable(spool):
    """A turn cancelled inside the redaction offload must not spend the claim.

    The claim is irreversible and makes every later replay inert, and
    ``CancelledError`` is a ``BaseException`` that the seam's ``except
    Exception`` does not catch, so a cancellation there returns nothing to the
    caller: no ``app_on_row``, so no row records the app. The property that
    keeps it recoverable is ORDER -- the claim is taken after the redaction, so a
    cancellation in this window has nothing to give back. The very next call
    renders for real. (A cancellation during the claim itself is the next test.)
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    text = f"a [kirocrew-mcp-app:{sid}] b"

    started = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def hang_on_redaction(fn, *args, **kwargs):
        # The redaction call is the lambda; the filesystem step passes a named
        # function, so this suspends exactly the window under test.
        if getattr(fn, "__name__", "") == "<lambda>":
            started.set()
            await asyncio.Event().wait()  # never completes; the test cancels it
        return await real_to_thread(fn, *args, **kwargs)

    st = _FakeState()
    with mock.patch.object(asyncio, "to_thread", hang_on_redaction):
        task = asyncio.ensure_future(
            mcp_apps_render.handle_tool_result(
                st, slot_key="dashboard:1", tool_call_id="tc-cancelled", text=text
            )
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Nothing was delivered and, crucially, the claim was not spent. Asserted
    # after the task has finished unwinding, so a sidecar created by a stray
    # worker thread would still be visible here.
    assert st.calls == []
    assert not (spool / f"{sid}.rendered").exists()

    # So the app is not lost: the next call still renders it and reports the
    # claim, which is the whole point of claiming last.
    out, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc-retry", text=text
    )
    assert claimed is True
    assert sid not in out
    assert len(st.calls) == 1


@pytest.mark.asyncio
async def test_the_claim_is_offloaded_to_a_worker_thread(spool):
    """Both filesystem steps are offloaded; neither runs on the event loop.

    ``os.close()`` on a file descriptor is named by the ``blocking: true``
    ``no-blocking-call-on-event-loop`` rule in ``AUTOSDE.yaml``, so the claim
    cannot run inline however cheap its syscalls are on a local path: one stalled
    spool filesystem freezes the user's turn and the liveness heartbeat together
    until the watchdog kills the process. This run reaches the claim (it renders)
    so the assertion is not vacuous.

    Offloading alone would lose an app to a cancellation, because the worker
    thread finishes whatever happens to the awaiting coroutine. The test below
    pins the property that pays for that.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    real_to_thread = asyncio.to_thread
    offloaded: list[str] = []

    async def record(fn, *args, **kwargs):
        offloaded.append(getattr(fn, "__name__", "") or repr(fn))
        return await real_to_thread(fn, *args, **kwargs)

    st = _FakeState()
    with mock.patch.object(asyncio, "to_thread", record):
        out, claimed = await mcp_apps_render.handle_tool_result(
            st, slot_key="dashboard:1", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
        )

    # The claim WAS reached: this rendered for real.
    assert claimed is True
    assert len(st.calls) == 1
    assert sid not in out
    # The record read parses up to MAX_SPOOL_BYTES.
    assert "_load_bound" in offloaded
    # The claim's own os.open/os.close are named by the rule.
    assert "_take_claim" in offloaded, f"the claim ran on the loop: {offloaded}"


@pytest.mark.asyncio
async def test_a_cancellation_during_the_claim_gives_the_claim_back(spool):
    """A claim its caller can never record must be given back, not kept.

    This is the window the offload reopens and the one an inline call did not
    have: the worker thread runs to completion whatever happens to the awaiting
    coroutine, so a cancellation delivered while it runs creates the sidecar
    while the caller raises and records nothing. A kept claim makes every later
    replay inert, so the app would be gone with no row saying it existed.

    Shielding the call keeps the outcome knowable through the cancellation, so
    the seam releases the claim it cannot use. Unlike the inline version's
    property, this one IS schedulable: the sidecar provably exists at the moment
    the test cancels, so the release is measured rather than inferred.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    text = f"a [kirocrew-mcp-app:{sid}] b"

    taken = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def hold_after_claiming(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "_take_claim":
            result = await real_to_thread(fn, *args, **kwargs)
            # The claim is genuinely spent before the cancellation lands, which
            # is what makes the release the thing under test.
            assert (spool / f"{sid}.rendered").exists()
            taken.set()
            await asyncio.sleep(0.05)
            return result
        return await real_to_thread(fn, *args, **kwargs)

    st = _FakeState()
    with mock.patch.object(asyncio, "to_thread", hold_after_claiming):
        task = asyncio.ensure_future(
            mcp_apps_render.handle_tool_result(
                st, slot_key="dashboard:1", tool_call_id="tc-cancelled", text=text
            )
        )
        await asyncio.wait_for(taken.wait(), timeout=5)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Nothing was delivered, and the claim was handed back: the release runs
        # inside the cancellation path, so the task finishing means it is done.
        assert st.calls == []
        assert not (spool / f"{sid}.rendered").exists()

    # So the app is not lost: the next call renders it and reports the claim.
    out, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc-retry", text=text
    )
    assert claimed is True
    assert sid not in out
    assert len(st.calls) == 1


@pytest.mark.asyncio
async def test_a_spent_claim_on_the_same_row_recovers_the_flag(spool):
    """The durable claim alone is enough to put the row's flag back.

    ``_take_claim`` writes the sidecar durably while ``meta["mcp_app"]`` reaches
    disk only when the slot is next persisted. A gateway death between the two
    would otherwise leave the claim spent with no flag: the app unrenderable
    forever and nothing on the row saying it existed. Re-processing the SAME row
    reports the flag from the sidecar, so the row recovers with no ordering
    between the two writes and no restart-time scan for orphaned claims.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    text = f"a [kirocrew-mcp-app:{sid}] b"

    st = _FakeState()
    _, first = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc-same", text=text, row_ts=["1000.5"]
    )
    assert first is True
    assert len(st.calls) == 1

    # The crash: the sidecar is on disk, the flag never was. A fresh state
    # stands in for the restarted gateway, which has no memory of the send. The
    # row's `ts` is what identifies it across that restart: it is persisted with
    # the transcript, where the process's own memory of the send is not.
    st2 = _FakeState()
    out, again = await mcp_apps_render.handle_tool_result(
        st2, slot_key="dashboard:1", tool_call_id="tc-same", text=text, row_ts=["1000.5"]
    )
    assert again is True, "the owning row must recover its flag from the claim"
    assert st2.calls == [], "recovery records the flag; it does not re-render"
    assert sid not in out


@pytest.mark.asyncio
async def test_the_claim_records_the_row_that_owns_it(spool):
    """Attribution is durable, so it survives the process that wrote it."""
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    await mcp_apps_render.handle_tool_result(
        _FakeState(),
        slot_key="dashboard:7",
        tool_call_id="tc-owner",
        text=f"x [kirocrew-mcp-app:{sid}] y",
        row_ts=["12.5", "", "9.25"],
        row_ids=["m-2", "", "m-1"],
    )
    owner = json.loads((spool / f"{sid}.rendered").read_text(encoding="utf-8"))
    # The row MIDS are the attribution. The timestamps ride along for transcript
    # order and for a reader written before the ids; the call id is diagnostics
    # only. Blanks are dropped and the rest sorted on BOTH, so the record is
    # canonical however the caller ordered them.
    assert owner == {
        "slot_key": "dashboard:7",
        "rows": ["12.5", "9.25"],
        "row_ids": ["m-1", "m-2"],
    }
    # No claim temp is left behind: its suffix is swept, but a leak would still
    # accumulate one file per render.
    assert list(spool.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_a_reused_call_id_on_a_later_row_stays_inert(spool):
    """A reissued ``tool_call_id`` must not make a replay look like recovery.

    ``tool_call_id`` is the BACKEND's id and is session-local in general, so a
    session reset that preserves the transcript can hand a genuinely new call an
    id an older row already used. If attribution matched on it, a marker echoed
    into that new call's text would read as "spent by this row" and the new row
    would be told an app of its own is not viewable -- a notice pointing at
    nothing. Row timestamps cannot collide this way: they are strictly
    increasing within a slot, so the later row's ``ts`` is always distinct.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    text = f"a [kirocrew-mcp-app:{sid}] b"

    st = _FakeState()
    _, owner_flag = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:3", tool_call_id="call_1", text=text, row_ts=["100.0"]
    )
    assert owner_flag is True
    assert len(st.calls) == 1

    # The reset: same slot, the SAME id reissued, a later row, and the marker
    # echoed into this call's result text.
    st2 = _FakeState()
    out, replay_flag = await mcp_apps_render.handle_tool_result(
        st2, slot_key="dashboard:3", tool_call_id="call_1", text=text, row_ts=["500.0"]
    )
    assert replay_flag is False, "a reused id must not flag a row that never had the app"
    assert st2.calls == [], "the replay must not render either"
    assert sid not in out


@pytest.mark.asyncio
async def test_a_claim_taken_with_no_row_cannot_be_attributed_later(spool):
    """No row at claim time means no later "spent" can be attributed to it.

    The caller's flag loop only runs for a call that HAS a transcript row, so a
    claim taken without one has no flag a recovery could restore. Reporting
    ``"spent"`` on the strength of the slot alone would instead hand the flag to
    whichever later row happened to ask, which is the false notice this
    attribution exists to prevent -- so the unprovable case stays silent.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    text = f"a [kirocrew-mcp-app:{sid}] b"

    st = _FakeState()
    _, first = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:4", tool_call_id="", text=text
    )
    assert first is True, "the render itself still happens; only attribution needs a row"
    assert len(st.calls) == 1
    owner = json.loads((spool / f"{sid}.rendered").read_text(encoding="utf-8"))
    assert owner["rows"] == []

    st2 = _FakeState()
    _, again = await mcp_apps_render.handle_tool_result(
        st2, slot_key="dashboard:4", tool_call_id="", text=text, row_ts=["900.0"]
    )
    assert again is False
    assert st2.calls == []


@pytest.mark.asyncio
async def test_a_stale_claim_temp_does_not_block_the_claim(spool):
    """A residue at a reproducible temp name must not fail the claim forever.

    The temp is private scratch, so nothing already sitting in the spool dir may
    decide whether the claim is taken. A name this code composed from the pid
    could: a hard kill between the open and the unlink leaves
    ``<id>.claim.<pid>.tmp`` behind, and a respawn handed the same pid then
    collides on ``O_CREAT|O_EXCL`` and answers ``"error"`` for every later
    claim on that record until the 24h sweep -- the app neither rendered nor
    recorded. This plants that residue under BOTH the current pid and a
    neighbour, so a composed name is blocked whichever one the process gets.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    planted = [
        spool / f"{sid}.claim.{os.getpid()}.tmp",
        spool / f"{sid}.claim.{os.getpid() + 1}.tmp",
    ]
    for stale in planted:
        stale.write_bytes(b"residue from a killed process")

    st = _FakeState()
    out, flagged = await mcp_apps_render.handle_tool_result(
        st,
        slot_key="dashboard:9",
        tool_call_id="tc-stale",
        text=f"a [kirocrew-mcp-app:{sid}] b",
        row_ts=["77.0"],
    )

    assert flagged is True
    assert len(st.calls) == 1
    assert sid not in out
    sentinel = spool / f"{sid}.rendered"
    assert sentinel.exists()
    owner = json.loads(sentinel.read_text(encoding="utf-8"))
    # This caller passed no ids, so none are recorded, and such a claim is
    # attributed by ts exactly as a claim written before the ids is.
    assert owner == {
        "slot_key": "dashboard:9",
        "rows": ["77.0"],
        "row_ids": [],
    }
    # The planted residue is left for the sweep, and the claim leaked no temp of
    # its own: exactly the two files planted remain.
    assert sorted(p.name for p in spool.glob("*.tmp")) == sorted(p.name for p in planted)


@pytest.mark.asyncio
async def test_an_unreadable_spent_claim_flags_nothing(spool):
    """Unattributable fails towards silence on this row, never a false notice.

    A flag says "an app from this step is not viewable here". Putting that on a
    row whose claim cannot be attributed would point at nothing, so the
    unreadable case is treated as the replayed-marker case.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    (spool / f"{sid}.rendered").write_text("not json", encoding="utf-8")
    out, flagged = await mcp_apps_render.handle_tool_result(
        _FakeState(),
        slot_key="dashboard:1",
        tool_call_id="tc-unreadable",
        text=f"x [kirocrew-mcp-app:{sid}] y",
    )
    assert flagged is False
    assert sid not in out


@pytest.mark.asyncio
async def test_a_failed_close_still_reports_the_claim_it_took(spool):
    """The directory entry is the claim, so ``close`` must not overturn it.

    ``os.open`` with ``O_CREAT|O_EXCL`` is the only syscall here that can fail to
    create the sidecar. Reporting the ``close`` outcome instead left the sidecar
    on disk while the seam answered "not claimed", which makes every later replay
    inert AND writes no flag on the row -- the app is gone and nothing says it
    existed. A deferred write error surfacing at ``close()`` is a real case on the
    network or FUSE spool storage the module plans for.

    Releasing the sidecar is not the alternative: the caller strips the marker
    from the transcript on both branches, so a released record has no replay left
    to render it.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    text = f"a [kirocrew-mcp-app:{sid}] b"

    real_close = os.close
    closed: list[int] = []

    def close_fails(fd):
        closed.append(fd)
        real_close(fd)  # release it for real, then report the failure the FS gave
        raise OSError(5, "input/output error on close")

    st = _FakeState()
    with mock.patch.object(os, "close", close_fails):
        out, claimed = await mcp_apps_render.handle_tool_result(
            st, slot_key="dashboard:1", tool_call_id="tc-close", text=text
        )

    # The claim it took is reported, so the row records the app...
    assert claimed is True
    assert closed, "the test did not exercise the close path"
    # ...the frame still went out, so the app is shown live...
    assert len(st.calls) == 1
    assert sid not in out
    # ...and the sidecar stands, so a replay is still inert.
    assert (spool / f"{sid}.rendered").exists()

    out2, claimed2 = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc-close-replay", text=text
    )
    assert claimed2 is False
    assert sid not in out2
    assert len(st.calls) == 1


@pytest.mark.asyncio
async def test_a_second_cancellation_still_gives_the_claim_back(spool):
    """Cancelling twice must not strand the claim, which a shield alone allows.

    ``asyncio.shield`` protects the inner future, NOT the awaiting coroutine, so
    a second ``.cancel()`` raises out of the release path's own ``await`` and
    skips the unlink -- ``CancelledError`` is a ``BaseException`` that the
    handler's ``except Exception`` does not catch. The sidecar would stay taken
    with nothing recorded, which makes every later replay inert: the app is gone
    and no row says it existed. A turn deadline and a slot deletion firing on one
    task is ordinary operation, not a contrived pair.

    The claim is held on an event the TEST owns, so the second cancellation
    provably lands while the claim is still in flight rather than after the
    unlink has already been dispatched. That is what makes the drain the thing
    under test instead of the scheduler.
    """
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})
    text = f"a [kirocrew-mcp-app:{sid}] b"

    taken = asyncio.Event()
    finish_claim = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def hold_the_claim(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "_take_claim":
            result = await real_to_thread(fn, *args, **kwargs)
            assert (spool / f"{sid}.rendered").exists()
            taken.set()
            # The claim resolves only when the test says so, AFTER both
            # cancellations have landed.
            await finish_claim.wait()
            return result
        return await real_to_thread(fn, *args, **kwargs)

    st = _FakeState()
    with mock.patch.object(asyncio, "to_thread", hold_the_claim):
        task = asyncio.ensure_future(
            mcp_apps_render.handle_tool_result(
                st, slot_key="dashboard:1", tool_call_id="tc-twice", text=text
            )
        )
        await asyncio.wait_for(taken.wait(), timeout=5)

        task.cancel()  # raises out of the claim's shielded await
        for _ in range(3):
            await asyncio.sleep(0)  # let the handler reach the release path
        task.cancel()  # must be absorbed, not allowed to skip the unlink

        finish_claim.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert st.calls == []
        assert not (spool / f"{sid}.rendered").exists()

    # And the app is still renderable, which is the whole point of the release.
    out, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc-after-twice", text=text
    )
    assert claimed is True
    assert sid not in out
    assert len(st.calls) == 1


@pytest.mark.asyncio
async def test_handle_tool_result_refuses_cross_session_marker(spool):
    """Slot binding: a record bound to session A must not render (nor arm its
    callback capability) when its marker lands in session B."""
    sid = _hex()
    _write_spool(
        spool,
        sid,
        {
            "server": "s",
            "tool": "t",
            "html": "h",
            "session_key": "dashboard:A",
        },
    )
    st = _FakeState()
    out, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:B", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
    )
    assert st.calls == []
    assert sid not in out
    # Refused before the claim, so no flag: session B's row has no app.
    assert claimed is False


@pytest.mark.asyncio
async def test_handle_tool_result_renders_in_bound_session(spool):
    sid = _hex()
    _write_spool(
        spool,
        sid,
        {
            "server": "s",
            "tool": "t",
            "html": "h",
            "session_key": "dashboard:A",
        },
    )
    st = _FakeState()
    _text, claimed = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:A", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
    )
    assert len(st.calls) == 1
    assert claimed is True


@pytest.mark.asyncio
async def test_wrong_slot_replay_does_not_burn_the_render_claim(spool):
    """Regression: the session-binding check runs BEFORE the single-consume
    claim. A marker echoed into the WRONG session first must not consume the
    record's one render — the legitimate slot still renders afterwards."""
    sid = _hex()
    _write_spool(
        spool,
        sid,
        {
            "server": "s",
            "tool": "t",
            "html": "h",
            "session_key": "dashboard:A",
        },
    )
    st = _FakeState()
    text = f"[kirocrew-mcp-app:{sid}]"
    # Wrong slot arrives first: refused, and the claim is NOT taken.
    await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:B", tool_call_id="tc1", text=text
    )
    assert st.calls == []
    assert not (spool / f"{sid}.rendered").exists()
    # The legitimate slot still gets its render.
    await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:A", tool_call_id="tc2", text=text
    )
    assert len(st.calls) == 1


def test_default_spool_dir_uses_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("KIROCREW_MCP_APPS_SPOOL", raising=False)
    monkeypatch.setattr(mcp_apps_render, "config_dir", lambda: tmp_path)
    assert mcp_apps_render._spool_dir() == tmp_path / "mcp-apps"


def test_env_override_spool_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", str(tmp_path / "custom"))
    assert mcp_apps_render._spool_dir() == tmp_path / "custom"


def test_module_has_no_import_side_effect_on_env(monkeypatch):
    # _spool_dir() reads the env at call time, not import time.
    monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", "/tmp/a")
    assert mcp_apps_render._spool_dir() == Path("/tmp/a")
    monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", "/tmp/b")
    assert mcp_apps_render._spool_dir() == Path("/tmp/b")


def test_os_import_available():
    # Guard: module uses os.environ; ensure it's importable in the module ns.
    assert hasattr(mcp_apps_render, "os") and mcp_apps_render.os is os


@pytest.mark.asyncio
async def test_handle_tool_result_redacts_credentials_in_leaves(spool):
    """Credential/exfil-URL leaves in app-bound tool data are redacted before
    they cross into the server-authored iframe."""
    sid = _hex()
    _write_spool(
        spool,
        sid,
        {
            "server": "s",
            "tool": "t",
            "html": "h",
            "tool_input": {"key": FAKE_CREDENTIAL},
            "structured_content": {"note": f"leaked {FAKE_CREDENTIAL} here"},
        },
    )
    st = _FakeState()
    await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
    )
    _, data = st.calls[0]
    blob = json.dumps({"a": data["tool_input"], "b": data["structured_content"]})
    assert FAKE_CREDENTIAL not in blob


@pytest.mark.asyncio
async def test_binding_uses_producing_session_key_not_slot(spool):
    """The binding check compares the canonical producing key, not the bare
    slot key — a real render is not silently refused, and a genuine mismatch
    still is."""
    sid = _hex()
    _write_spool(
        spool, sid, {"server": "s", "tool": "t", "html": "h", "session_key": "dashboard:9"}
    )
    st = _FakeState()
    await mcp_apps_render.handle_tool_result(
        st,
        slot_key="9",
        tool_call_id="tc",
        text=f"[kirocrew-mcp-app:{sid}]",
        producing_session_key="dashboard:9",
    )
    assert len(st.calls) == 1

    sid2 = _hex()
    _write_spool(
        spool, sid2, {"server": "s", "tool": "t", "html": "h", "session_key": "dashboard:9"}
    )
    st2 = _FakeState()
    await mcp_apps_render.handle_tool_result(
        st2,
        slot_key="9",
        tool_call_id="tc",
        text=f"[kirocrew-mcp-app:{sid2}]",
        producing_session_key="dashboard:OTHER",
    )
    assert len(st2.calls) == 0


@pytest.mark.asyncio
async def test_render_uses_owner_only_channel_not_generic(spool):
    """#418/#11: the render frame carries the callback_secret, so it MUST go to
    the owner-only WS channel and NEVER the generic broadcast. Reverting the
    channel selection would leak the capability to guest sockets."""

    class _OwnerState:
        def __init__(self):
            self.owner_calls: list[tuple[str, dict]] = []
            self.generic_calls: list[tuple[str, dict]] = []

        def broadcast_ws_owners(self, msg_type: str, data: dict) -> None:
            self.owner_calls.append((msg_type, data))

        def broadcast_ws(self, msg_type: str, data: dict) -> None:
            self.generic_calls.append((msg_type, data))

    sid = _hex()
    _write_spool(
        spool, sid, {"server": "s", "tool": "t", "html": "h", "callback_secret": "cap-xyz"}
    )
    st = _OwnerState()
    await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
    )
    assert len(st.owner_calls) == 1
    assert st.generic_calls == []
    assert st.owner_calls[0][1]["callback_secret"] == "cap-xyz"


def test_load_spool_rejects_and_reaps_expired(spool, monkeypatch):
    """#5: load_spool enforces the capability TTL on read (not only via the
    sweep) — a record past SPOOL_TTL_SECS is refused and reaped along with its
    .rendered sidecar, so a stale callback_secret can't authorize forever."""
    import os as _os
    import time as _time

    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h", "callback_secret": "cap"})
    rec = spool / f"{sid}.json"
    sidecar = spool / f"{sid}.rendered"
    sidecar.write_text("", encoding="utf-8")
    # Backdate mtime well past the TTL.
    old = _time.time() - mcp_apps_render.SPOOL_TTL_SECS - 60
    _os.utime(rec, (old, old))

    assert mcp_apps_render.load_spool(sid) is None
    assert not rec.exists()
    assert not sidecar.exists()

    # A fresh record still loads.
    sid2 = _hex()
    _write_spool(spool, sid2, {"server": "s", "tool": "t", "html": "h"})
    assert mcp_apps_render.load_spool(sid2) is not None


class TestAClaimSurvivesAFilesystemWithoutHardLinks:
    """``os.link`` is not available everywhere, and it was the only publisher.

    exFAT and a number of FUSE and network mounts refuse every ``link``, which
    is a standing property of the data home rather than a transient fault. The
    old code read that as ``"error"``, and the caller strips the marker on that
    branch without recording anything, so EVERY app render on such a host was
    lost in silence with no replay left to recover it.
    """

    def test_the_claim_is_taken_when_link_is_unsupported(self, spool):
        sid = _hex()
        sentinel = spool / f"{sid}.rendered"
        rows = ["2026-01-01T00:00:00.000001Z"]

        def link_unsupported(src, dst, **kw):
            raise OSError(errno.EPERM, "operation not permitted")

        with mock.patch.object(os, "link", link_unsupported):
            out = mcp_apps_render._take_claim(sid, "dashboard:1", "tc", rows)

        assert out == "took"
        assert sentinel.exists()
        owner = json.loads(sentinel.read_text(encoding="utf-8"))
        assert owner["rows"] == rows, "the fallback must publish the identity too"
        assert owner["slot_key"] == "dashboard:1"

    def test_the_fallback_still_reports_a_spent_claim(self, spool):
        """``O_CREAT|O_EXCL``, not ``os.replace``: an existing claim must win.

        A replacing publisher would let a second caller silently take a claim
        another row owns, rendering the app twice and destroying the first row's
        record. The fallback must keep the same refusal the link path had.
        """
        sid = _hex()
        sentinel = spool / f"{sid}.rendered"
        rows = ["2026-01-01T00:00:00.000001Z"]
        sentinel.write_text(
            json.dumps({"slot_key": "dashboard:1", "tool_call_id": "tc", "rows": rows}),
            encoding="utf-8",
        )

        def link_unsupported(src, dst, **kw):
            raise OSError(errno.EOPNOTSUPP, "not supported")

        with mock.patch.object(os, "link", link_unsupported):
            same = mcp_apps_render._take_claim(sid, "dashboard:1", "tc", rows)
            other = mcp_apps_render._take_claim(
                sid, "dashboard:1", "tc2", ["2026-01-01T00:00:09.000009Z"]
            )

        assert same == "spent", "the owning row must still get its flag back"
        assert other == "inert", "a different row must not be handed the app"

    def test_a_host_that_can_create_nothing_still_errors(self, spool):
        """The fallback must not turn a genuinely broken home into a claim."""
        sid = _hex()

        def link_unsupported(src, dst, **kw):
            raise OSError(errno.EPERM, "operation not permitted")

        real_open = os.open

        def open_refuses(path, flags, *a, **kw):
            if str(path).endswith(".rendered"):
                raise OSError(errno.EROFS, "read-only file system")
            return real_open(path, flags, *a, **kw)

        with mock.patch.object(os, "link", link_unsupported):
            with mock.patch.object(os, "open", open_refuses):
                out = mcp_apps_render._take_claim(sid, "dashboard:1", "tc", ["t1"])

        assert out == "error"
        assert not (spool / f"{sid}.rendered").exists()


class TestAnIdentitylessClaimIsNeverHandedToTheAskingRow:
    """A zero-byte sentinel must stay inert, because LEGACY renders leave one.

    The shipped code took its claim as ``os.open(sentinel, O_CREAT|O_EXCL)``
    followed by a bare ``os.close`` -- nothing written. So on every deployed
    gateway each SUCCESSFULLY rendered app leaves an empty ``<id>.rendered``
    behind for the record's TTL. Emptiness therefore cannot mean "nobody owns
    this", and treating it as an unowned claim would re-render every one of those
    apps on whichever row replayed its marker after an upgrade.
    """

    def test_an_empty_sentinel_is_inert_not_taken(self, spool):
        sid = _hex()
        sentinel = spool / f"{sid}.rendered"
        sentinel.write_bytes(b"")

        out = mcp_apps_render._attribute_spent_claim(
            sentinel, sid, "dashboard:1", "tc", ["2026-01-01T00:00:00.000001Z"]
        )

        assert out == "inert"
        assert sentinel.read_bytes() == b"", "an identityless claim must be left untouched"

    def test_the_legacy_shape_is_reproduced_and_still_inert(self, spool):
        """Built the way the shipped code builds it, not hand-written as empty.

        This is the discriminating half: if the claim were ever taken from such a
        sentinel, a replayed marker would render an app that already rendered.
        """
        sid = _hex()
        sentinel = spool / f"{sid}.rendered"
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        assert sentinel.stat().st_size == 0, "the legacy claim is a zero-byte file"

        out = mcp_apps_render._attribute_spent_claim(
            sentinel, sid, "dashboard:1", "tc", ["2026-01-01T00:00:00.000001Z"]
        )

        assert out == "inert"

    def test_a_non_empty_unparseable_sentinel_is_also_inert(self, spool):
        """Damage is not absence either, and both fail towards silence."""
        sid = _hex()
        sentinel = spool / f"{sid}.rendered"
        sentinel.write_bytes(b"{not json")

        out = mcp_apps_render._attribute_spent_claim(
            sentinel, sid, "dashboard:1", "tc", ["2026-01-01T00:00:00.000001Z"]
        )

        assert out == "inert"
        assert sentinel.read_bytes() == b"{not json", "an unreadable owner must be left alone"

    def test_a_failed_body_write_unlinks_the_sentinel_it_could_not_fill(self, spool):
        """The recoverable half of the window, which IS detectable in-process.

        A write failure is observed by the caller that created the sentinel, so it
        can restore the pre-claim state; only a hard death cannot, and that case
        is accepted rather than guessed at.

        The fault is aimed at the SENTINEL's own descriptor, not at every write.
        Failing all of them kills the temp's write instead, which returns before
        the sentinel is ever created -- so the assertion below would hold with the
        unlink deleted, and the test would measure nothing. Confirmed: the
        all-writes form let that mutation survive.
        """
        sid = _hex()
        sentinel = spool / f"{sid}.rendered"
        real_open = os.open
        real_write = os.write
        sentinel_fds: set[int] = set()

        def open_tracking(path, flags, *a, **kw):
            fd = real_open(path, flags, *a, **kw)
            if str(path) == str(sentinel):
                sentinel_fds.add(fd)
            return fd

        def write_fails_on_the_sentinel(fd, data):
            if fd in sentinel_fds:
                raise OSError(errno.EIO, "input/output error")
            return real_write(fd, data)

        def link_unsupported(src, dst, **kw):
            raise OSError(errno.EPERM, "operation not permitted")

        with mock.patch.object(os, "link", link_unsupported):
            with mock.patch.object(os, "open", open_tracking):
                with mock.patch.object(os, "write", write_fails_on_the_sentinel):
                    out = mcp_apps_render._take_claim(sid, "dashboard:1", "tc", ["t1"])

        assert sentinel_fds, "the sentinel was never created, so this proves nothing"
        assert out == "error"
        assert not sentinel.exists(), "a sentinel that could not be filled must not survive"


class TestTheClaimBodyIsWrittenWhole:
    """``os.write`` may write fewer bytes than asked and raise nothing.

    A truncated body parses as nothing, so the claim would later read as
    "owner unreadable" -- inert -- with the sentinel still in place: the app
    silent and unrecoverable. Pipes and some network filesystems short-write on
    ordinary traffic, not only under failure.
    """

    def test_a_short_writing_filesystem_still_publishes_a_whole_body(self, spool):
        sid = _hex()
        sentinel = spool / f"{sid}.rendered"
        rows = ["2026-01-01T00:00:00.000001Z"]
        real_write = os.write

        def short_write(fd, data):
            # Accept one byte per call, the worst legal behaviour.
            return real_write(fd, bytes(data[:1]))

        with mock.patch.object(os, "write", short_write):
            out = mcp_apps_render._take_claim(sid, "dashboard:1", "tc", rows)

        assert out == "took"
        owner = json.loads(sentinel.read_text(encoding="utf-8"))
        assert owner["rows"] == rows
        assert "tool_call_id" not in owner, (
            "the call id is not carried: nothing reads it back, and an unbounded "
            "field in a body with a byte limit made a long one push a legitimate "
            "claim past the limit, where the reader then refused it"
        )

    def test_write_all_passes_every_byte_through(self):
        """The helper itself, so a regression names the helper rather than a claim."""
        seen: list[bytes] = []

        def collecting(fd, data):
            chunk = bytes(data[:3])
            seen.append(chunk)
            return len(chunk)

        with mock.patch.object(os, "write", collecting):
            mcp_apps_render._write_all(7, b"abcdefgh")

        assert b"".join(seen) == b"abcdefgh"
        assert len(seen) == 3, "a short write must be resumed, not retried whole"
