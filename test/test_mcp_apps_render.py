"""Tests for dashboard-side MCP Apps marker interception (mcp_apps_render)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import mcp_apps_render

# ── marker regex ─────────────────────────────────────────────────────────────


def _hex() -> str:
    return uuid.uuid4().hex  # 32 lowercase hex chars


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


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    d = tmp_path / "mcp-apps"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", str(d))
    return d


def _write_spool(spool_dir: Path, sid: str, payload: dict) -> None:
    # Readers enforce the schema version — default it so tests exercise the
    # fields they care about; schema-rejection tests set it explicitly.
    payload.setdefault("schema", mcp_apps_render.SPOOL_SCHEMA_VERSION)
    (spool_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


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


class _FakeState:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: dict) -> None:
        self.calls.append((msg_type, data))


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
    caller: no ``app_claimed``, so no row records the app. The property that
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
            "tool_input": {"key": "AKIAIOSFODNN7EXAMPLE"},
            "structured_content": {"note": "leaked AKIAIOSFODNN7EXAMPLE here"},
        },
    )
    st = _FakeState()
    await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
    )
    _, data = st.calls[0]
    blob = json.dumps({"a": data["tool_input"], "b": data["structured_content"]})
    assert "AKIAIOSFODNN7EXAMPLE" not in blob


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
