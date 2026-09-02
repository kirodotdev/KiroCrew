"""Tests for queue-mutating route handlers in the Design Tweak backend.

Covers lines ~2780-3220 of server.py: /submit follow-up paths, seal-on-send
(/send), /delete-comment, /clear, /delivered, /delete, and /thread — the
handlers that mutate the on-disk queue state.
"""
from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.apps.builtins.design_tweak.backend import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_queue(tmp_path, monkeypatch):
    data = (tmp_path / 'data').resolve()
    queue = data / 'queue'
    handled = data / 'handled'
    queue.mkdir(parents=True)
    handled.mkdir(parents=True)
    monkeypatch.setattr(server, 'DATA_DIR', data)
    monkeypatch.setattr(server, 'QUEUE_DIR', queue)
    monkeypatch.setattr(server, 'HANDLED_DIR', handled)
    monkeypatch.setattr(server, '_ROOT', '')
    monkeypatch.setattr(server, '_TARGET', '')
    monkeypatch.setitem(server._CFG, 'projects', [])
    return queue


# ---------------------------------------------------------------------------
# Handler harness — drives routes without a live socket.
# ---------------------------------------------------------------------------


class _Response:
    """Captures what _json would send over the wire."""

    def __init__(self):
        self.code: int = 0
        self.payload: dict = {}


def _make_handler(method: str, path: str, body: dict | None = None) -> tuple:
    """Build a Handler instance that can invoke route methods directly."""
    raw = json.dumps(body or {}).encode("utf-8") if body is not None else b""
    h = server.Handler.__new__(server.Handler)
    h.path = path
    h.headers = Message()
    h.headers["Content-Length"] = str(len(raw))
    h.rfile = io.BytesIO(raw)
    h.wfile = io.BytesIO()
    h.requestline = f"{method} {path} HTTP/1.1"
    h.close_connection = False
    h._cached_body = raw
    resp = _Response()

    def fake_json(code, payload):
        resp.code = code
        resp.payload = payload

    h._json = fake_json
    return h, resp


def _seed_request(queue_dir: Path, rid: str, req: dict) -> Path:
    """Write a request file to the queue directory."""
    fp = server._request_file(queue_dir, rid)
    server._write_request(fp, req)
    return fp


def _make_draft(rid: str, project_id: str = "proj1",
                comments: list | None = None) -> dict:
    """Create a minimal valid draft request dict."""
    return {
        "type": "visual_edit_batch",
        "id": rid,
        "number": 1,
        "state": "draft",
        "projectId": project_id,
        "projectRoot": "/tmp/fake",
        "createdAt": "2026-01-01T00:00:00Z",
        "sentAt": "",
        "thread": [],
        "comments": comments if comments is not None else [],
    }


def _make_comment(cid: str, index: int = 1, status: str = "new") -> dict:
    """Create a minimal comment sub-item."""
    return {
        "cid": cid,
        "index": index,
        "status": status,
        "comment": f"Fix the {cid} element",
        "createdAt": "2026-01-01T00:00:01Z",
        "selection": {"elements": [{"tag": "div", "id": "x"}]},
        "previewUrl": "http://localhost:3000",
        "projectId": "proj1",
        "sourceFile": "",
        "followUpTo": "",
        "thread": [{"role": "user", "text": f"Fix {cid}", "ts": "2026-01-01T00:00:01Z"}],
    }


# ===========================================================================
# /delete-comment
# ===========================================================================


class TestDeleteComment:
    """POST /delete-comment removes exactly the targeted comment from a draft."""

    def test_delete_by_cid_removes_only_target(self, isolated_queue):
        """If the wrong comment is deleted, the user loses work they intended to
        keep, and there is no recovery path — the queue file is the only copy."""
        c1 = _make_comment("aaa", index=1)
        c2 = _make_comment("bbb", index=2)
        c3 = _make_comment("ccc", index=3)
        req = _make_draft("req1", comments=[c1, c2, c3])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delete-comment?id=req1&cid=bbb")
        h._h_delete_comment({"id": ["req1"], "cid": ["bbb"]})

        assert resp.code == 200
        assert resp.payload["ok"] is True
        # Verify on-disk state
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        cids = [c["cid"] for c in saved["comments"]]
        assert "bbb" not in cids
        assert "aaa" in cids and "ccc" in cids

    def test_delete_reindexes_remaining_comments(self, isolated_queue):
        """If indices are not rewritten after deletion, the panel's sub-numbering
        (3.1, 3.2, …) shows gaps, confusing the user about which item is which."""
        c1 = _make_comment("aaa", index=1)
        c2 = _make_comment("bbb", index=2)
        c3 = _make_comment("ccc", index=3)
        req = _make_draft("req1", comments=[c1, c2, c3])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delete-comment?id=req1&cid=aaa")
        h._h_delete_comment({"id": ["req1"], "cid": ["aaa"]})

        assert resp.code == 200
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        indices = [c["index"] for c in saved["comments"]]
        assert indices == [1, 2], "indices must be contiguous after deletion"

    def test_delete_last_comment_removes_entire_request(self, isolated_queue):
        """An emptied draft is noise — removing the file prevents a stale empty
        request from appearing in the panel's queue listing forever."""
        c1 = _make_comment("only", index=1)
        req = _make_draft("req1", comments=[c1])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delete-comment?id=req1&cid=only")
        h._h_delete_comment({"id": ["req1"], "cid": ["only"]})

        assert resp.code == 200
        assert resp.payload.get("removedRequest") is True
        fp = server._request_file(isolated_queue, "req1")
        assert not fp.exists(), "empty draft file must be deleted"

    def test_delete_from_sent_request_refused(self, isolated_queue):
        """Once a request is sent, the agent already has the batch — removing a
        comment would desync the agent's view from the queue file."""
        c1 = _make_comment("aaa", index=1, status="sent")
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T01:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delete-comment?id=req1&cid=aaa")
        h._h_delete_comment({"id": ["req1"], "cid": ["aaa"]})

        assert resp.code == 409
        assert "already sent" in resp.payload["error"]

    def test_delete_nonexistent_cid_returns_404(self, isolated_queue):
        """A stale UI reference to a cid that was already deleted must not
        silently succeed — a 404 tells the panel to refresh its state."""
        c1 = _make_comment("aaa", index=1)
        req = _make_draft("req1", comments=[c1])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delete-comment?id=req1&cid=zzz")
        h._h_delete_comment({"id": ["req1"], "cid": ["zzz"]})

        assert resp.code == 404
        assert "comment not found" in resp.payload["error"]

    def test_delete_invalid_id_returns_400(self, isolated_queue):
        """A malformed request id must produce a structured error, not a traceback
        from _request_file's path-escape check."""
        h, resp = _make_handler("POST", "/delete-comment?id=../etc&cid=x")
        h._h_delete_comment({"id": ["../etc"], "cid": ["x"]})

        assert resp.code == 400
        assert "valid id" in resp.payload["error"]

    def test_delete_missing_request_returns_404(self, isolated_queue):
        """A request id that never existed must 404 cleanly, not raise on
        is_file() for a path that was never written."""
        h, resp = _make_handler("POST", "/delete-comment?id=nope&cid=x")
        h._h_delete_comment({"id": ["nope"], "cid": ["x"]})

        assert resp.code == 404
        assert "not found" in resp.payload["error"]


# ===========================================================================
# /send (seal-on-send)
# ===========================================================================


class TestSend:
    """POST /send seals a draft so no further comments can be appended."""

    def test_seal_marks_all_new_comments_as_sent(self, isolated_queue):
        """If a 'new' comment survives sealing, the agent will never be told
        about it — the panel hides sent requests from the compose flow."""
        c1 = _make_comment("c1", index=1, status="new")
        c2 = _make_comment("c2", index=2, status="new")
        req = _make_draft("req1", comments=[c1, c2])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/send?id=req1")
        h._h_send({"id": ["req1"]})

        assert resp.code == 200
        assert resp.payload["ok"] is True
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        assert saved["state"] == "sent"
        assert saved["sentAt"] != ""
        for c in saved["comments"]:
            assert c["status"] == "sent", (
                f"comment {c['cid']} not sealed — agent will never see it"
            )

    def test_seal_empty_request_refused(self, isolated_queue):
        """Sending an empty batch would dispatch a prompt with no content,
        wasting agent tokens and confusing the user."""
        req = _make_draft("req1", comments=[])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/send?id=req1")
        h._h_send({"id": ["req1"]})

        assert resp.code == 400
        assert "no comments" in resp.payload["error"]

    def test_seal_already_sent_is_idempotent(self, isolated_queue):
        """A duplicate seal (e.g. from a retry after a network timeout) must not
        error — it just confirms the request is already sealed."""
        c1 = _make_comment("c1", index=1, status="sent")
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T01:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/send?id=req1")
        h._h_send({"id": ["req1"]})

        assert resp.code == 200
        assert resp.payload.get("already") is True

    def test_seal_invalid_id_returns_400(self, isolated_queue):
        """A malformed id must fail with a clear error, not a traceback."""
        h, resp = _make_handler("POST", "/send?id=!!bad!!")
        h._h_send({"id": ["!!bad!!"]})

        assert resp.code == 400
        assert "valid id" in resp.payload["error"]

    def test_seal_missing_request_returns_404(self, isolated_queue):
        """A seal for a non-existent request must 404, not 500."""
        h, resp = _make_handler("POST", "/send?id=ghost")
        h._h_send({"id": ["ghost"]})

        assert resp.code == 404
        assert "not found" in resp.payload["error"]


# ===========================================================================
# /delivered
# ===========================================================================


class TestDelivered:
    """POST /delivered acknowledges that a sealed request reached the agent."""

    def test_delivered_sets_timestamp(self, isolated_queue):
        """Without a deliveredAt timestamp, the panel cannot distinguish 'sealed
        but the prompt was never dispatched' from 'agent received it'."""
        c1 = _make_comment("c1", index=1, status="sent")
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T01:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delivered?id=req1")
        h._h_delivered({"id": ["req1"]})

        assert resp.code == 200
        assert resp.payload["ok"] is True
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        assert saved["deliveredAt"] != ""

    def test_delivered_idempotent_keeps_first_timestamp(self, isolated_queue):
        """Re-acknowledging must not update the timestamp — otherwise a duplicate
        call makes the request look newer than it is, breaking age-based UX."""
        c1 = _make_comment("c1", index=1, status="sent")
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T01:00:00Z"
        req["deliveredAt"] = "2026-01-01T01:01:00Z"
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delivered?id=req1")
        h._h_delivered({"id": ["req1"]})

        assert resp.code == 200
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        assert saved["deliveredAt"] == "2026-01-01T01:01:00Z"

    def test_delivered_on_draft_refused(self, isolated_queue):
        """Acknowledging a draft would mark work delivered that no agent has seen,
        suppressing the retry bar for the state it is meant to cover."""
        c1 = _make_comment("c1", index=1, status="new")
        req = _make_draft("req1", comments=[c1])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delivered?id=req1")
        h._h_delivered({"id": ["req1"]})

        assert resp.code == 409
        assert resp.payload["code"] == "not_sealed"

    def test_delivered_invalid_id_returns_400(self, isolated_queue):
        """A malformed id must produce a structured error, not an unhandled
        exception from path manipulation."""
        h, resp = _make_handler("POST", "/delivered?id=")
        h._h_delivered({"id": [""]})

        assert resp.code == 400
        assert resp.payload["code"] == "id_required"

    def test_delivered_missing_request_returns_404(self, isolated_queue):
        """A delivered call for a non-existent request must 404 cleanly."""
        h, resp = _make_handler("POST", "/delivered?id=vanished")
        h._h_delivered({"id": ["vanished"]})

        assert resp.code == 404
        assert resp.payload["code"] == "not_found"


# ===========================================================================
# /clear (archive to handled/)
# ===========================================================================


class TestClear:
    """POST /clear archives a request from queue/ to handled/."""

    def test_clear_moves_to_handled(self, isolated_queue, tmp_path):
        """If clear fails to move, the request stays in the active queue forever,
        polluting the panel's pending list with completed work."""
        handled = tmp_path / 'data' / 'handled'
        c1 = _make_comment("c1", index=1, status="done")
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/clear?id=req1")
        h._h_clear({"id": ["req1"]})

        assert resp.code == 200
        assert resp.payload["ok"] is True
        # No longer in queue
        assert not server._request_file(isolated_queue, "req1").exists()
        # Present in handled
        assert server._request_file(handled, "req1").exists()

    def test_clear_missing_returns_404(self, isolated_queue):
        """Clearing a non-existent request must 404, not raise."""
        h, resp = _make_handler("POST", "/clear?id=nope")
        h._h_clear({"id": ["nope"]})

        assert resp.code == 404
        assert "not found" in resp.payload["error"]

    def test_clear_invalid_id_returns_400(self, isolated_queue):
        """A path-escape id must produce a structured 400."""
        h, resp = _make_handler("POST", "/clear?id=../../etc")
        h._h_clear({"id": ["../../etc"]})

        assert resp.code == 400
        assert "valid id" in resp.payload["error"]


# ===========================================================================
# /delete (permanent removal)
# ===========================================================================


class TestDelete:
    """POST /delete permanently removes a request from queue/ or handled/."""

    def test_delete_from_queue(self, isolated_queue):
        """Permanent deletion must leave no trace — a leaked request file means
        the history endpoint shows stale completed work indefinitely."""
        req = _make_draft("req1", comments=[_make_comment("c1")])
        _seed_request(isolated_queue, "req1", req)

        h, resp = _make_handler("POST", "/delete?id=req1")
        h._h_delete({"id": ["req1"]})

        assert resp.code == 200
        assert resp.payload["deleted"] is True
        assert not server._request_file(isolated_queue, "req1").exists()

    def test_delete_from_handled(self, isolated_queue, tmp_path):
        """Deletion must also work on archived (handled/) requests."""
        handled = tmp_path / 'data' / 'handled'
        req = _make_draft("req1", comments=[_make_comment("c1")])
        fp = server._request_file(handled, "req1")
        server._write_request(fp, req)

        h, resp = _make_handler("POST", "/delete?id=req1")
        h._h_delete({"id": ["req1"]})

        assert resp.code == 200
        assert resp.payload["deleted"] is True
        assert not fp.exists()

    def test_delete_missing_returns_404(self, isolated_queue):
        """Deleting a non-existent request must 404."""
        h, resp = _make_handler("POST", "/delete?id=ghost")
        h._h_delete({"id": ["ghost"]})

        assert resp.code == 404

    def test_delete_invalid_id_returns_400(self, isolated_queue):
        """A malformed id must be rejected at the boundary."""
        h, resp = _make_handler("POST", "/delete?id=../../../x")
        h._h_delete({"id": ["../../../x"]})

        assert resp.code == 400


# ===========================================================================
# /thread
# ===========================================================================


class TestThread:
    """POST /thread appends progress notes to a comment or request thread."""

    def test_thread_appends_to_comment(self, isolated_queue):
        """If a thread note targets the wrong comment (or disappears), the
        in-preview bubble shows stale progress for the wrong item."""
        c1 = _make_comment("c1", index=1)
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T00:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        body = {"role": "agent", "text": "Working on it"}
        h, resp = _make_handler("POST", "/thread?id=req1&cid=c1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["req1"], "cid": ["c1"]})

        assert resp.code == 200
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        thread = saved["comments"][0]["thread"]
        assert any(e["text"] == "Working on it" for e in thread)

    def test_thread_appends_to_request_level(self, isolated_queue):
        """A request-level note (no cid) must land on the request's own thread,
        not accidentally on the first comment."""
        c1 = _make_comment("c1", index=1)
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T00:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        body = {"role": "system", "text": "Batch started"}
        h, resp = _make_handler("POST", "/thread?id=req1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["req1"], "cid": [""]})

        assert resp.code == 200
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        assert any(e["text"] == "Batch started" for e in saved["thread"])
        # Verify comment thread was NOT touched
        assert len(saved["comments"][0]["thread"]) == 1

    def test_thread_status_done_fans_out_to_all_comments(self, isolated_queue):
        """A request-level 'done' must mark every sub-comment as done — otherwise
        the panel shows individual items as still in-progress when the agent
        reported completion for the whole batch."""
        c1 = _make_comment("c1", index=1, status="sent")
        c2 = _make_comment("c2", index=2, status="sent")
        req = _make_draft("req1", comments=[c1, c2])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T00:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        body = {"role": "agent", "text": "All done", "status": "done"}
        h, resp = _make_handler("POST", "/thread?id=req1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["req1"], "cid": [""]})

        assert resp.code == 200
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        for c in saved["comments"]:
            assert c["status"] == "done"

    def test_thread_on_draft_promotes_to_sent(self, isolated_queue):
        """Agent activity on a draft means it was dispatched without a formal seal
        — the state must be normalised to 'sent' so it never silently collects
        new comments that the agent will never see."""
        c1 = _make_comment("c1", index=1, status="new")
        req = _make_draft("req1", comments=[c1])
        _seed_request(isolated_queue, "req1", req)

        body = {"role": "agent", "text": "Starting work"}
        h, resp = _make_handler("POST", "/thread?id=req1&cid=c1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["req1"], "cid": ["c1"]})

        assert resp.code == 200
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        assert saved["state"] == "sent"
        assert saved["sentAt"] != ""

    def test_thread_nonexistent_comment_returns_404(self, isolated_queue):
        """Appending to a cid that doesn't exist must 404, not silently drop
        the note — the agent would think its update was persisted."""
        c1 = _make_comment("c1", index=1)
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T00:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        body = {"role": "agent", "text": "Note"}
        h, resp = _make_handler("POST", "/thread?id=req1&cid=nonexistent", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["req1"], "cid": ["nonexistent"]})

        assert resp.code == 404
        assert "not in request" in resp.payload["error"]

    def test_thread_missing_request_returns_404(self, isolated_queue):
        """A thread append for a request that doesn't exist must 404."""
        body = {"role": "agent", "text": "Note"}
        h, resp = _make_handler("POST", "/thread?id=ghost&cid=c1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["ghost"], "cid": ["c1"]})

        assert resp.code == 404

    def test_thread_invalid_id_returns_400(self, isolated_queue):
        """A malformed request id must produce a structured 400."""
        body = {"role": "agent", "text": "Note"}
        h, resp = _make_handler("POST", "/thread?id=!!&cid=c1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["!!"], "cid": ["c1"]})

        assert resp.code == 400

    def test_thread_empty_text_and_no_status_returns_400(self, isolated_queue):
        """At least text or status must be provided — a no-op append would waste
        a disk write and confuse the thread history."""
        c1 = _make_comment("c1", index=1)
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        _seed_request(isolated_queue, "req1", req)

        body = {"role": "agent", "text": "", "status": ""}
        h, resp = _make_handler("POST", "/thread?id=req1&cid=c1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["req1"], "cid": ["c1"]})

        assert resp.code == 400
        assert "text or status" in resp.payload["error"]

    def test_thread_comment_status_update_without_text(self, isolated_queue):
        """A status-only update (no text) must still succeed — the agent reports
        completion status without a prose note sometimes."""
        c1 = _make_comment("c1", index=1, status="sent")
        req = _make_draft("req1", comments=[c1])
        req["state"] = "sent"
        req["sentAt"] = "2026-01-01T00:00:00Z"
        _seed_request(isolated_queue, "req1", req)

        body = {"role": "agent", "text": "", "status": "done"}
        h, resp = _make_handler("POST", "/thread?id=req1&cid=c1", body)
        with patch.object(server, 'redact_exfiltration_urls',
                          side_effect=lambda t: (t, False)):
            with patch.object(server, 'redact_credentials',
                              side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["req1"], "cid": ["c1"]})

        assert resp.code == 200
        fp = server._request_file(isolated_queue, "req1")
        saved = json.loads(fp.read_text())
        assert saved["comments"][0]["status"] == "done"


# ===========================================================================
# /submit — follow-up and draft bookkeeping paths
# ===========================================================================


class TestSubmitFollowUp:
    """POST /submit creates or appends to the project's open draft."""

    def test_submit_creates_draft_when_none_exists(self, isolated_queue, monkeypatch):
        """Without draft creation, a newly captured comment has nowhere to land
        and the user's work is silently lost."""
        monkeypatch.setitem(server._CFG, 'projects', [
            {"id": "proj1", "path": "/tmp/fake", "name": "fake"}
        ])
        monkeypatch.setitem(server._CFG, 'activeId', 'proj1')

        body = {
            "type": "visual_edit_request",
            "selection": {"elements": [{"tag": "div", "id": "main"}]},
            "comment": "Make this wider",
            "projectId": "proj1",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()

        with patch.object(server, '_resolve_project',
                          return_value=("proj1", "/tmp/fake", "")):
            with patch.object(server, '_sanitize_selection_sources'):
                h._h_submit()

        assert resp.code == 200
        assert resp.payload["ok"] is True
        assert resp.payload["state"] == "draft"
        assert resp.payload["commentCount"] == 1
        # Verify file was created
        rid = resp.payload["id"]
        fp = server._request_file(isolated_queue, rid)
        assert fp.exists()

    def test_submit_appends_to_existing_draft(self, isolated_queue, monkeypatch):
        """Multiple comments before sending must land in the same draft — opening
        a new draft for each would dispatch them as separate requests."""
        monkeypatch.setitem(server._CFG, 'projects', [
            {"id": "proj1", "path": "/tmp/fake", "name": "fake"}
        ])
        # Seed an existing draft
        c1 = _make_comment("existing", index=1)
        req = _make_draft("draft1", project_id="proj1", comments=[c1])
        _seed_request(isolated_queue, "draft1", req)

        body = {
            "type": "visual_edit_request",
            "selection": {"elements": [{"tag": "span", "id": "x"}]},
            "comment": "Second comment",
            "projectId": "proj1",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()

        with patch.object(server, '_resolve_project',
                          return_value=("proj1", "/tmp/fake", "")):
            with patch.object(server, '_sanitize_selection_sources'):
                h._h_submit()

        assert resp.code == 200
        assert resp.payload["commentCount"] == 2
        assert resp.payload["id"] == "draft1"

    def test_submit_follow_up_link(self, isolated_queue, monkeypatch):
        """A followUpTo reference links the new comment to an earlier one so the
        agent sees it as a continuation, not an unrelated request."""
        monkeypatch.setitem(server._CFG, 'projects', [
            {"id": "proj1", "path": "/tmp/fake", "name": "fake"}
        ])

        body = {
            "type": "visual_edit_request",
            "selection": {"elements": [{"tag": "div", "id": "hero"}]},
            "comment": "Also fix the padding",
            "followUpTo": "prev-comment-id",
            "projectId": "proj1",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()

        with patch.object(server, '_resolve_project',
                          return_value=("proj1", "/tmp/fake", "")):
            with patch.object(server, '_sanitize_selection_sources'):
                h._h_submit()

        assert resp.code == 200
        rid = resp.payload["id"]
        fp = server._request_file(isolated_queue, rid)
        saved = json.loads(fp.read_text())
        last_comment = saved["comments"][-1]
        assert last_comment["followUpTo"] == "prev-comment-id"

    def test_submit_invalid_follow_up_sanitized(self, isolated_queue, monkeypatch):
        """A followUpTo that doesn't match _ID_RE must be sanitized to empty —
        it must not persist path-escape characters into the queue file."""
        monkeypatch.setitem(server._CFG, 'projects', [
            {"id": "proj1", "path": "/tmp/fake", "name": "fake"}
        ])

        body = {
            "type": "visual_edit_request",
            "selection": {"elements": [{"tag": "div", "id": "x"}]},
            "comment": "test",
            "followUpTo": "../../etc/passwd",
            "projectId": "proj1",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()

        with patch.object(server, '_resolve_project',
                          return_value=("proj1", "/tmp/fake", "")):
            with patch.object(server, '_sanitize_selection_sources'):
                h._h_submit()

        assert resp.code == 200
        rid = resp.payload["id"]
        fp = server._request_file(isolated_queue, rid)
        saved = json.loads(fp.read_text())
        assert saved["comments"][-1]["followUpTo"] == ""

    def test_submit_wrong_type_returns_400(self, isolated_queue):
        """Only visual_edit_request payloads are valid — anything else must be
        rejected before touching the queue."""
        body = {
            "type": "something_else",
            "selection": {"elements": [{"tag": "div"}]},
            "comment": "test",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()
        h._h_submit()

        assert resp.code == 400
        assert "type must be" in resp.payload["error"]

    def test_submit_missing_elements_returns_400(self, isolated_queue):
        """A submit without selection.elements would crash _el_name when the queue
        endpoint later tries to summarize the request."""
        body = {
            "type": "visual_edit_request",
            "selection": {},
            "comment": "test",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()
        h._h_submit()

        assert resp.code == 400
        assert resp.payload["code"] == "selection_required"

    def test_submit_non_dict_elements_returns_400(self, isolated_queue):
        """String elements would raise AttributeError in _el_name — reject at
        the boundary instead of persisting a broken payload."""
        body = {
            "type": "visual_edit_request",
            "selection": {"elements": ["not-a-dict"]},
            "comment": "test",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()
        h._h_submit()

        assert resp.code == 400
        assert resp.payload["code"] == "selection_malformed"

    def test_submit_draft_comment_limit(self, isolated_queue, monkeypatch):
        """Exceeding MAX_DRAFT_COMMENTS must return 429, not grow the file
        unboundedly — the limit protects against a hostile previewed page."""
        monkeypatch.setitem(server._CFG, 'projects', [
            {"id": "proj1", "path": "/tmp/fake", "name": "fake"}
        ])
        comments = [_make_comment(f"c{i}", index=i) for i in range(
            1, server.MAX_DRAFT_COMMENTS + 1
        )]
        req = _make_draft("req1", project_id="proj1", comments=comments)
        _seed_request(isolated_queue, "req1", req)

        body = {
            "type": "visual_edit_request",
            "selection": {"elements": [{"tag": "div", "id": "x"}]},
            "comment": "one too many",
            "projectId": "proj1",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()

        with patch.object(server, '_resolve_project',
                          return_value=("proj1", "/tmp/fake", "")):
            with patch.object(server, '_sanitize_selection_sources'):
                h._h_submit()

        assert resp.code == 429
        assert resp.payload["code"] == "draft_comment_limit"

    def test_submit_cid_always_server_generated(self, isolated_queue, monkeypatch):
        """The cid must be server-minted, never taken from the payload — a
        caller-supplied cid could be a duplicate or non-string, making the
        comment unreachable by /delete-comment or /thread."""
        monkeypatch.setitem(server._CFG, 'projects', [
            {"id": "proj1", "path": "/tmp/fake", "name": "fake"}
        ])

        body = {
            "type": "visual_edit_request",
            "selection": {"elements": [{"tag": "div", "id": "y"}]},
            "comment": "test",
            "cid": "attacker-chosen-id",
            "projectId": "proj1",
        }
        h, resp = _make_handler("POST", "/submit", body)
        h._cached_body = json.dumps(body).encode()

        with patch.object(server, '_resolve_project',
                          return_value=("proj1", "/tmp/fake", "")):
            with patch.object(server, '_sanitize_selection_sources'):
                h._h_submit()

        assert resp.code == 200
        cid = resp.payload["cid"]
        assert cid != "attacker-chosen-id"
        # Verify the stored comment uses the server-generated cid
        rid = resp.payload["id"]
        fp = server._request_file(isolated_queue, rid)
        saved = json.loads(fp.read_text())
        assert saved["comments"][-1]["cid"] == cid


class TestPersistedRequestLifecycle:
    """Pin the complete persisted state machine, not only its individual cuts."""

    def test_submit_through_history_preserves_order_and_opens_the_next_draft(
        self, isolated_queue, tmp_path, monkeypatch
    ):
        project_root = (tmp_path / "project").resolve()
        project_root.mkdir()
        monkeypatch.setattr(
            server,
            "_CFG",
            {
                "projects": [
                    {"id": "project-one", "path": str(project_root), "name": "project"}
                ],
                "activeId": "project-one",
            },
        )

        ids = iter(("req-one", "comment-one", "req-two", "comment-two"))
        expected_times = [
            "2026-08-01T00:00:00+00:00",  # first request created
            "2026-08-01T00:00:02+00:00",  # first request sealed
            "2026-08-01T00:00:03+00:00",  # second request created
            "2026-08-01T00:00:05+00:00",  # first request delivered
            "2026-08-01T00:00:06+00:00",  # done note appended
        ]
        times = iter(expected_times)
        time_calls = []

        def _fixed_now():
            value = next(times)
            time_calls.append(value)
            return value

        monkeypatch.setattr(server, "_new_id", lambda: next(ids))
        monkeypatch.setattr(server, "_now_iso", _fixed_now)

        def _request(method: str, path: str, body: dict | None = None) -> _Response:
            handler, response = _make_handler(method, path, body)
            handler._authorized = lambda *_args: True
            getattr(server.Handler, f"do_{method}")(handler)
            return response

        first_comment = "Make the save button primary"
        first_comment_created = "2026-08-01T00:00:01+00:00"
        submit = _request(
            "POST",
            "/api/submit/",
            {
                "type": "visual_edit_request",
                "selection": {
                    "mode": "single",
                    "elements": [{"tag": "button", "id": "save", "locator": "#save"}],
                },
                "comment": first_comment,
                "createdAt": first_comment_created,
                "projectId": "project-one",
            },
        )
        first_queue_file = server._request_file(isolated_queue, "req-one")
        assert submit.code == 200
        assert submit.payload == {
            "ok": True,
            "id": "req-one",
            "number": 1,
            "state": "draft",
            "cid": "comment-one",
            "index": 1,
            "label": "1.1",
            "commentCount": 1,
            "savedTo": str(first_queue_file),
        }
        assert first_queue_file.is_file()

        sealed = _request("POST", "/api/send/?id=req-one")
        assert sealed.code == 200
        assert sealed.payload["ok"] is True
        assert {
            key: sealed.payload["request"][key]
            for key in ("id", "number", "state", "status", "sentAt", "deliveredAt")
        } == {
            "id": "req-one",
            "number": 1,
            "state": "sent",
            "status": "sent",
            "sentAt": "2026-08-01T00:00:02+00:00",
            "deliveredAt": "",
        }

        # The sealed request deliberately remains in queue while the next
        # capture arrives. That is what proves seal-on-send, rather than archive,
        # is the boundary that opens a new draft.
        second_comment_created = "2026-08-01T00:00:04+00:00"
        second = _request(
            "POST",
            "/api/submit/",
            {
                "type": "visual_edit_request",
                "selection": {"elements": [{"tag": "nav", "id": "primary"}]},
                "comment": "Tighten the navigation spacing",
                "createdAt": second_comment_created,
                "projectId": "project-one",
            },
        )
        second_queue_file = server._request_file(isolated_queue, "req-two")
        assert second.code == 200
        assert second.payload == {
            "ok": True,
            "id": "req-two",
            "number": 2,
            "state": "draft",
            "cid": "comment-two",
            "index": 1,
            "label": "2.1",
            "commentCount": 1,
            "savedTo": str(second_queue_file),
        }
        assert first_queue_file.is_file()
        assert second_queue_file.is_file()

        delivered = _request("POST", "/api/delivered/?id=req-one")
        assert delivered.code == 200
        assert delivered.payload["ok"] is True
        assert delivered.payload["request"]["deliveredAt"] == "2026-08-01T00:00:05+00:00"

        done = _request(
            "POST",
            "/api/thread/?id=req-one&cid=comment-one",
            {"role": "agent", "text": "Applied the change", "status": "done"},
        )
        assert done.code == 200
        assert {
            key: done.payload[key] for key in ("ok", "id", "cid", "status")
        } == {
            "ok": True,
            "id": "req-one",
            "cid": "comment-one",
            "status": "done",
        }
        assert done.payload["request"]["doneCount"] == 1
        assert done.payload["request"]["comments"][0]["status"] == "done"

        archived = _request("POST", "/api/clear/?id=req-one")
        handled_file = server._request_file(server.HANDLED_DIR, "req-one")
        assert archived.code == 200
        assert archived.payload == {"ok": True, "id": "req-one"}
        assert not first_queue_file.exists()
        assert handled_file.is_file()
        assert second_queue_file.is_file(), "archiving one request must not move the next draft"

        history = _request("GET", "/api/history/")
        assert history.code == 200
        assert history.payload == {
            "history": [
                {
                    "id": "req-one",
                    "number": 1,
                    "state": "sent",
                    "status": "done",
                    "createdAt": "2026-08-01T00:00:00+00:00",
                    "sentAt": "2026-08-01T00:00:02+00:00",
                    "deliveredAt": "2026-08-01T00:00:05+00:00",
                    "projectId": "project-one",
                    "projectRoot": str(project_root),
                    "thread": [],
                    "doneCount": 1,
                    "comments": [
                        {
                            "cid": "comment-one",
                            "index": 1,
                            "status": "done",
                            "comment": first_comment,
                            "createdAt": first_comment_created,
                            "element": "button#save",
                            "locator": "#save",
                            "parentLocator": "",
                            "point": {},
                            "count": 1,
                            "mode": "single",
                            "previewUrl": "",
                            "projectId": "project-one",
                            "sourceFile": "",
                            "source": {},
                            "followUpTo": "",
                            "thread": [
                                {
                                    "role": "user",
                                    "text": first_comment,
                                    "ts": first_comment_created,
                                },
                                {
                                    "role": "agent",
                                    "text": "Applied the change",
                                    "ts": "2026-08-01T00:00:06+00:00",
                                },
                            ],
                        }
                    ],
                }
            ]
        }
        assert time_calls == expected_times


class TestThreadEntryCap:
    """A full thread must refuse further appends rather than grow without bound.

    `/thread` is the agent's progress channel, so it is written far more often
    than a human comments, and every append rewrites the WHOLE record. Unbounded,
    a stuck agent looping on progress posts turns one request into quadratic
    rewrite work and unbounded disk. The refusal has to be a structured 429 the
    caller can read, not a truncation that silently drops what it was told.
    """

    def _full_thread(self, n: int) -> list:
        return [{"role": "agent", "text": f"step {i}", "ts": "2026-01-01T00:00:00Z"}
                for i in range(n)]

    def test_request_level_append_is_refused_when_full(self, isolated_queue):
        c1 = _make_comment("c1", index=1)
        req = _make_draft("reqcap", comments=[c1])
        req["state"] = "sent"
        req["thread"] = self._full_thread(server.MAX_THREAD_ENTRIES)
        _seed_request(isolated_queue, "reqcap", req)

        h, resp = _make_handler("POST", "/thread?id=reqcap",
                                {"role": "agent", "text": "one more"})
        with patch.object(server, "redact_exfiltration_urls", side_effect=lambda t: (t, False)):
            with patch.object(server, "redact_credentials", side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["reqcap"], "cid": [""]})

        assert resp.code == 429
        assert resp.payload["code"] == "thread_entry_limit"
        saved = json.loads(server._request_file(isolated_queue, "reqcap").read_text())
        assert len(saved["thread"]) == server.MAX_THREAD_ENTRIES, "the record grew anyway"

    def test_comment_level_append_is_refused_when_full(self, isolated_queue):
        c1 = _make_comment("c1", index=1)
        c1["thread"] = self._full_thread(server.MAX_THREAD_ENTRIES)
        req = _make_draft("reqcap2", comments=[c1])
        req["state"] = "sent"
        _seed_request(isolated_queue, "reqcap2", req)

        h, resp = _make_handler("POST", "/thread?id=reqcap2&cid=c1",
                                {"role": "agent", "text": "one more"})
        with patch.object(server, "redact_exfiltration_urls", side_effect=lambda t: (t, False)):
            with patch.object(server, "redact_credentials", side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["reqcap2"], "cid": ["c1"]})

        assert resp.code == 429
        assert resp.payload["code"] == "thread_entry_limit"
        saved = json.loads(server._request_file(isolated_queue, "reqcap2").read_text())
        assert len(saved["comments"][0]["thread"]) == server.MAX_THREAD_ENTRIES

    def test_one_below_the_cap_still_appends(self, isolated_queue):
        """The cap must not be off by one and lock out the last usable slot."""
        c1 = _make_comment("c1", index=1)
        req = _make_draft("reqcap3", comments=[c1])
        req["state"] = "sent"
        req["thread"] = self._full_thread(server.MAX_THREAD_ENTRIES - 1)
        _seed_request(isolated_queue, "reqcap3", req)

        h, resp = _make_handler("POST", "/thread?id=reqcap3",
                                {"role": "agent", "text": "last one"})
        with patch.object(server, "redact_exfiltration_urls", side_effect=lambda t: (t, False)):
            with patch.object(server, "redact_credentials", side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["reqcap3"], "cid": [""]})

        assert resp.code == 200
        saved = json.loads(server._request_file(isolated_queue, "reqcap3").read_text())
        assert len(saved["thread"]) == server.MAX_THREAD_ENTRIES
        assert saved["thread"][-1]["text"] == "last one"

    def test_a_status_only_post_is_not_blocked_by_a_full_thread(self, isolated_queue):
        """The cap bounds STORAGE, so it must not strand a comment's status.

        A `done` with no text appends nothing, so refusing it would leave the
        agent unable to resolve a comment whose thread happens to be full.
        """
        c1 = _make_comment("c1", index=1, status="sent")
        c1["thread"] = self._full_thread(server.MAX_THREAD_ENTRIES)
        req = _make_draft("reqcap4", comments=[c1])
        req["state"] = "sent"
        _seed_request(isolated_queue, "reqcap4", req)

        h, resp = _make_handler("POST", "/thread?id=reqcap4&cid=c1",
                                {"role": "agent", "text": "", "status": "done"})
        with patch.object(server, "redact_exfiltration_urls", side_effect=lambda t: (t, False)):
            with patch.object(server, "redact_credentials", side_effect=lambda t: (t, False)):
                h._h_thread({"id": ["reqcap4"], "cid": ["c1"]})

        saved = json.loads(server._request_file(isolated_queue, "reqcap4").read_text())
        assert resp.code == 200, "a text-free status update appends nothing and must be allowed"
        assert saved["comments"][0]["status"] == "done"
