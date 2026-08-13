"""Tests for dashboard-side MCP Apps marker interception (mcp_apps_render)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

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
    out = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc1", text="just output"
    )
    assert out == "just output"
    assert st.calls == []


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
    out = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:7", tool_call_id="tc42", text=text
    )
    # Marker stripped from transcript text.
    assert sid not in out
    assert out == "result  tail"
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
    out = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc", text=text
    )
    # No spool → no broadcast, but marker still stripped so the user never sees it.
    assert sid not in out
    assert out == "x  y"
    assert st.calls == []


@pytest.mark.asyncio
async def test_handle_tool_result_broadcast_exception_degrades_gracefully(spool):
    sid = _hex()
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h"})

    class _BoomState:
        def broadcast_ws(self, *_a, **_k):
            raise RuntimeError("ws down")

    text = f"a [kirocrew-mcp-app:{sid}] b"
    # Must not raise; still returns stripped text.
    out = await mcp_apps_render.handle_tool_result(
        _BoomState(), slot_key="dashboard:1", tool_call_id="tc", text=text
    )
    assert sid not in out


@pytest.mark.asyncio
async def test_handle_tool_result_offloads_spool_read(spool, monkeypatch):
    """Regression for the no-blocking-call-on-event-loop rule: the multi-MB
    spool read must execute in a worker thread (asyncio.to_thread), never on
    the event loop thread that runs every co-scheduled chat task."""
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
    out = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc",
        text=f"pre [kirocrew-mcp-app:{sid}] post",
    )
    assert sid not in out
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
    out1 = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc1", text=text
    )
    out2 = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:1", tool_call_id="tc2", text=text
    )
    assert len(st.calls) == 1  # exactly one render
    assert sid not in out1 and sid not in out2  # marker always stripped
    # The record itself survives the render claim — the app-call capability
    # path stays valid for the rendered app's lifetime.
    assert mcp_apps_render.load_spool(sid) is not None


@pytest.mark.asyncio
async def test_handle_tool_result_refuses_cross_session_marker(spool):
    """Slot binding: a record bound to session A must not render (nor arm its
    callback capability) when its marker lands in session B."""
    sid = _hex()
    _write_spool(spool, sid, {
        "server": "s", "tool": "t", "html": "h", "session_key": "dashboard:A",
    })
    st = _FakeState()
    out = await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:B", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
    )
    assert st.calls == []
    assert sid not in out


@pytest.mark.asyncio
async def test_handle_tool_result_renders_in_bound_session(spool):
    sid = _hex()
    _write_spool(spool, sid, {
        "server": "s", "tool": "t", "html": "h", "session_key": "dashboard:A",
    })
    st = _FakeState()
    await mcp_apps_render.handle_tool_result(
        st, slot_key="dashboard:A", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]"
    )
    assert len(st.calls) == 1


@pytest.mark.asyncio
async def test_wrong_slot_replay_does_not_burn_the_render_claim(spool):
    """Regression: the session-binding check runs BEFORE the single-consume
    claim. A marker echoed into the WRONG session first must not consume the
    record's one render — the legitimate slot still renders afterwards."""
    sid = _hex()
    _write_spool(spool, sid, {
        "server": "s", "tool": "t", "html": "h", "session_key": "dashboard:A",
    })
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
    _write_spool(spool, sid, {
        "server": "s", "tool": "t", "html": "h",
        "tool_input": {"key": "AKIAIOSFODNN7EXAMPLE"},
        "structured_content": {"note": "leaked AKIAIOSFODNN7EXAMPLE here"},
    })
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
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h",
                              "session_key": "dashboard:9"})
    st = _FakeState()
    await mcp_apps_render.handle_tool_result(
        st, slot_key="9", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid}]",
        producing_session_key="dashboard:9",
    )
    assert len(st.calls) == 1

    sid2 = _hex()
    _write_spool(spool, sid2, {"server": "s", "tool": "t", "html": "h",
                               "session_key": "dashboard:9"})
    st2 = _FakeState()
    await mcp_apps_render.handle_tool_result(
        st2, slot_key="9", tool_call_id="tc", text=f"[kirocrew-mcp-app:{sid2}]",
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
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h",
                              "callback_secret": "cap-xyz"})
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
    _write_spool(spool, sid, {"server": "s", "tool": "t", "html": "h",
                              "callback_secret": "cap"})
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
