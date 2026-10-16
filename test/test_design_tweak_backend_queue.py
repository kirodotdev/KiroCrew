"""Design Tweak backend tests: the request queue, selections and the agent thread.

Queue record size ceilings, selections a previewed page supplies, the single-winner
send, server-minted comment ids, concurrent writers, delivery acknowledgement, and
redaction of thread and comment text on the way in and out.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from design_tweak_backend_helpers import isolated_queue_fixture  # noqa: F401
from design_tweak_backend_helpers import _JsonHandler

from conftest import requires_symlinks
from kiro_crew.apps.builtins.design_tweak.backend import server
from kiro_crew.testing.wait import wait_until


class TestThreadGrowthIsBounded:
    """`/thread` is the agent's progress channel, so it is the high-frequency writer.

    Every append rewrites the WHOLE queue record, so an unbounded thread is
    quadratic rewrite work on top of unbounded disk. A stuck agent looping on
    progress posts is the realistic path, not an attacker. The functional
    enforcement lives in `test_design_tweak_queue_routes.py`, which owns the
    route harness; this pins that BOTH append paths are bounded, since a cap on
    one of them leaves the other free to grow.
    """

    def test_the_cap_is_enforced_on_both_append_paths(self):
        import inspect

        src = inspect.getsource(server.Handler._h_thread)
        assert (
            src.count("MAX_THREAD_ENTRIES") >= 2
        ), "both the request-level and comment-level appends must be bounded"
        assert src.count("thread_entry_limit") >= 2

    def test_the_cap_is_a_positive_int(self):
        assert isinstance(server.MAX_THREAD_ENTRIES, int)
        assert server.MAX_THREAD_ENTRIES > 0


class TestQueueReadIsSizeBounded:
    """A queue record on disk is untrusted input, not just our own output.

    The bundled skill hands the agent this exact directory, so anything with the
    user's filesystem access can write here — and `/queue` reads EVERY pending
    file, so one oversized record would take the whole route down rather than
    just itself.
    """

    def test_an_oversized_record_reads_as_absent(self, isolated_queue):
        fp = server._request_file(server.QUEUE_DIR, "1700000000000-big000")
        # Valid JSON, just far too large — the point is that SIZE alone refuses it.
        fp.write_text('{"id": "x", "pad": "' + "a" * (server.MAX_BODY_BYTES + 64) + '"}')
        assert server._read_request(fp) is None

    def test_a_normal_record_still_reads(self, isolated_queue):
        rid = "1700000000000-ok0000"
        fp = server._request_file(server.QUEUE_DIR, rid)
        server._write_request(fp, {"id": rid, "comments": []})
        got = server._read_request(fp)
        assert got is not None and got["id"] == rid


class TestWhatIsWrittenStaysReadable:
    """The write ceiling and the read ceiling must be the SAME number.

    A record the writer accepts but the reader refuses is a draft the user cannot
    see: `_read_request` reports it absent, so `/queue` stops listing
    it and the queued work is effectively gone. Refusing the append that would
    have crossed the line is strictly better — the user keeps the draft and is
    told it is full. Each individual payload is under `MAX_BODY_BYTES`, so the
    accumulation is the only way to get there and a per-payload cap cannot see it.
    """

    def test_an_oversized_write_is_refused(self, isolated_queue):
        fp = server._request_file(server.QUEUE_DIR, "1700000000000-toobig")
        req = {"id": "x", "pad": "a" * (server.MAX_RECORD_BYTES + 64)}
        with pytest.raises(server._RecordTooLarge):
            server._write_request(fp, req)

    def test_a_refused_write_leaves_the_previous_record_intact(self, isolated_queue):
        """The whole point: a full draft must not cost the user the draft."""
        rid = "1700000000000-keepit"
        fp = server._request_file(server.QUEUE_DIR, rid)
        server._write_request(fp, {"id": rid, "comments": [{"cid": "c1"}]})
        with pytest.raises(server._RecordTooLarge):
            server._write_request(fp, {"id": rid, "pad": "a" * (server.MAX_RECORD_BYTES + 64)})
        still = server._read_request(fp)
        assert still is not None, "the refused write destroyed the record it could not replace"
        assert still["comments"] == [{"cid": "c1"}]

    def test_anything_the_writer_accepts_the_reader_returns(self, isolated_queue):
        """Round-trip AT the ceiling, so an off-by-one cannot hide between them.

        The writer refuses `len(data) > max_bytes` and the reader refuses
        `st_size > MAX_RECORD_BYTES`, so a record serialising to exactly the
        ceiling is the single input both must accept. Either bound flipping to
        `>=` rejects it, and no other test in this file writes a record of
        exactly that size.
        """
        fp = server._request_file(server.QUEUE_DIR, _AT_LIMIT_RID)
        at_ceiling = _record_serialising_to(_AT_LIMIT_RID, server.MAX_RECORD_BYTES)
        server._write_request(fp, at_ceiling)
        assert (
            fp.stat().st_size == server.MAX_RECORD_BYTES
        ), "the record no longer lands on the ceiling, so the boundary is untested"
        assert (
            server._read_request(fp) is not None
        ), "the largest record the writer accepted is unreadable"

    def test_one_byte_over_the_ceiling_is_refused(self, isolated_queue):
        """Pins the ceiling exactly.

        `test_an_oversized_write_is_refused` uses a grossly oversized record, which
        an off-by-one bound still refuses; only the +1 case can tell the two apart.
        """
        fp = server._request_file(server.QUEUE_DIR, _AT_LIMIT_RID)
        over = _record_serialising_to(_AT_LIMIT_RID, server.MAX_RECORD_BYTES + 1)
        with pytest.raises(server._RecordTooLarge):
            server._write_request(fp, over)

    def test_the_boundary_round_trip_stays_inside_an_io_budget(self, isolated_queue, monkeypatch):
        """Bound the I/O, because a quadratic probe is invisible to a pass/fail assert.

        Establishing the ceiling by growing a record and calling the real writer at
        each step costs sum(k) bytes, not O(k): measured at 2081 writer calls and
        2.03 GiB serialised and written to reach a 2 MiB answer. On windows-latest
        that overran the 180s per-test timeout, and replacing the killed worker
        pushed the whole shard past its 40-minute job cap, so every other test in
        that shard went unreported. The ceiling is arithmetic on the serialised
        bytes, so candidates must be measured in memory.
        """
        offered: list[int] = []
        real_write_json = server._atomic_write_json

        def counting(path: Path, payload: dict, **kw: Any) -> None:
            offered.append(len(json.dumps(payload, indent=2).encode("utf-8")))
            return real_write_json(path, payload, **kw)

        monkeypatch.setattr(server, "_atomic_write_json", counting)

        fp = server._request_file(server.QUEUE_DIR, _AT_LIMIT_RID)
        server._write_request(fp, _record_serialising_to(_AT_LIMIT_RID, server.MAX_RECORD_BYTES))
        assert server._read_request(fp) is not None

        assert len(offered) <= 2, (
            f"{len(offered)} writer calls to establish one ceiling; a real write per "
            "candidate size is quadratic and hangs the Windows shard"
        )
        assert sum(offered) <= 2 * server.MAX_RECORD_BYTES, (
            f"the boundary round-trip pushed {sum(offered)} bytes through the writer "
            f"for a {server.MAX_RECORD_BYTES}-byte answer"
        )


class TestPreviewSuppliedSourcePathsAreContained:
    """A previewed page must not aim the agent's edit outside the project.

    `previewUrl` and each element's `source` block are produced INSIDE the
    previewed page, so both are attacker-controlled when the previewed project
    is hostile. Neither value is opened by this backend — they are handed to the
    agent as "the exact source file to edit", so the barrier protects the edit
    TARGET, and an unchecked `..` would point a legitimate agent at a file
    outside the registered root.
    """

    def test_traversal_in_preview_url_yields_no_source_file(self, tmp_path, monkeypatch):
        proj = tmp_path / "site"
        proj.mkdir()
        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        pid, root, source_file = server._resolve_project(
            {
                "projectId": "p1",
                "previewUrl": "/apps/design-tweak/api/proxy/p1/../../../../etc/passwd",
            }
        )
        assert pid == "p1"
        assert root == str(proj)
        # Escaped -> cleared, NOT joined into a path outside the project.
        assert source_file == ""
        assert "etc/passwd" not in source_file

    def test_ordinary_served_file_still_resolves(self, tmp_path, monkeypatch):
        """The barrier must not break the normal case it guards."""
        proj = tmp_path / "site"
        (proj / "sub").mkdir(parents=True)
        (proj / "sub" / "index.html").write_text("<h1>x</h1>")
        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        _pid, _root, source_file = server._resolve_project(
            {"projectId": "p1", "previewUrl": "/apps/design-tweak/api/proxy/p1/sub/index.html"}
        )
        assert source_file == str(Path(os.path.realpath(proj)) / "sub" / "index.html")

    @requires_symlinks
    def test_symlink_escape_in_preview_url_is_rejected(self, tmp_path, monkeypatch):
        """A symlink inside the project pointing out of it must not resolve.

        `_contained` realpaths BOTH sides, so the link is followed before the
        containment test — a served path is judged by where it lands, not how it
        is spelled.
        """
        proj = tmp_path / "site"
        proj.mkdir()
        secret = tmp_path / "outside"
        secret.mkdir()
        (secret / "creds.env").write_text("TOKEN=1")
        (proj / "escape").symlink_to(secret)

        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        _pid, _root, source_file = server._resolve_project(
            {
                "projectId": "p1",
                "previewUrl": "/apps/design-tweak/api/proxy/p1/escape/creds.env",
            }
        )
        assert source_file == ""

    def test_absolute_path_in_preview_url_is_rejected(self, tmp_path, monkeypatch):
        """An absolute `served_rel` wins `os.path.join`; containment must catch it."""
        proj = tmp_path / "site"
        proj.mkdir()
        cfg = {
            "projects": [{"id": "p1", "path": str(proj), "name": "site"}],
            "activeId": "p1",
            "counter": 0,
        }
        monkeypatch.setattr(server, "_CFG", cfg)
        monkeypatch.setattr(server, "_ROOT", str(proj))

        _pid, _root, source_file = server._resolve_project(
            {"projectId": "p1", "previewUrl": "/apps/design-tweak/api/proxy/p1//etc/passwd"}
        )
        assert source_file == ""

    def test_element_source_escape_is_cleared_and_downgraded(self, tmp_path):
        """A hostile `data-kiro-source` cannot name a file outside the root."""
        proj = tmp_path / "site"
        proj.mkdir()
        sel: dict[str, Any] = {
            "mode": "single",
            "elements": [
                {
                    "locator": "div.a",
                    "source": {"file": "../../../../etc/hosts", "line": 1, "confidence": "high"},
                },
                {
                    "locator": "div.b",
                    "source": {"file": "/etc/passwd", "line": 2, "confidence": "high"},
                },
            ],
        }
        server._sanitize_selection_sources(sel, str(proj))

        for el in sel["elements"]:
            assert el["source"]["file"] == ""
            # Cleared with a stale "high" would be an incoherent hint.
            assert el["source"]["confidence"] == "low"
            assert el["locator"]  # the usable anchor is untouched

    def test_element_source_inside_root_survives(self, tmp_path):
        """React Fiber reports ABSOLUTE paths under the project — keep them."""
        proj = tmp_path / "site"
        (proj / "src").mkdir(parents=True)
        real = Path(os.path.realpath(proj)) / "src" / "App.tsx"
        sel: dict[str, Any] = {
            "elements": [
                {"source": {"file": str(real), "line": 7, "confidence": "medium"}},
                {"source": {"file": "src/App.tsx", "line": 7, "confidence": "high"}},
            ]
        }
        server._sanitize_selection_sources(sel, str(proj))

        assert sel["elements"][0]["source"]["file"] == str(real)
        assert sel["elements"][0]["source"]["confidence"] == "medium"
        assert sel["elements"][1]["source"]["file"] == str(real)
        assert sel["elements"][1]["source"]["confidence"] == "high"

    def test_no_known_root_fails_closed(self):
        """With nothing to contain against, no hint is trusted."""
        sel: dict[str, Any] = {
            "elements": [{"source": {"file": "src/App.tsx", "confidence": "high"}}]
        }
        server._sanitize_selection_sources(sel, "")
        assert sel["elements"][0]["source"]["file"] == ""
        assert sel["elements"][0]["source"]["confidence"] == "low"

    def test_malformed_selection_shapes_are_tolerated(self):
        """Never raise on a shape the preview page controls."""
        for bad in (
            None,
            [],
            "x",
            {"elements": None},
            {"elements": ["x", None, {}]},
            {"elements": [{"source": "notadict"}, {"source": {}}]},
        ):
            server._sanitize_selection_sources(bad, "/tmp")

    def test_submit_routes_the_selection_through_the_barrier(self):
        """The sanitizer is actually WIRED into the submit path, not just present."""
        import inspect

        src = inspect.getsource(server.Handler._h_submit)
        assert "_sanitize_selection_sources" in src


class TestDraftCommentCap:
    """A previewed page drives captures, so the draft must be bounded.

    Nothing reaches the agent without a separate Send, so the exposure is
    unbounded growth of the user's own queue file rather than an agent action —
    a cap bounds it without requiring a parent-side gesture, which would break
    the in-preview interaction the product exists for.
    """

    def test_cap_is_enforced_and_reports_a_code(self):
        import inspect

        src = inspect.getsource(server.Handler._h_submit)
        assert "MAX_DRAFT_COMMENTS" in src, "the draft cap is not enforced on submit"
        assert "draft_comment_limit" in src, "the refusal carries no machine-readable code"
        assert server.MAX_DRAFT_COMMENTS > 0


class TestMalformedSelectionCannotPoisonTheQueue:
    """A bad selection must be refused at the boundary, not persisted.

    `_el_name` does `el.get("tag")`, so a STRING element raises AttributeError
    when `/queue` summarises the request. Because the selection was already
    written to disk by then, that endpoint keeps returning 500 on every later
    poll until someone deletes the queue file by hand — a one-request,
    self-inflicted, persistent outage from a payload the preview page controls.
    """

    def _submit(self, payload):
        """Run the real `_h_submit` against a hand-rolled handler.

        Every caller takes `isolated_queue`, including the ones that expect a
        refusal. A payload the guard lets through reaches the draft transaction
        for real, and `QUEUE_DIR`/`HANDLED_DIR` are frozen at import off
        `$KIROCREW_HOME` (server.py), so the accepted case writes
        `queue/<id>.json` — and, via the unscoped counter, `config.json` — into
        whatever data home the session resolved: outside `tmp_path`, outside the
        run's temp root, so none of conftest's residue guards can see it, and
        permanent when the operator exported `KIROCREW_HOME` themselves. The
        refused cases write nothing today; they take the fixture so a regression
        of the guard fails as a test rather than as a file in someone's queue.
        """

        sent: list[tuple[int, dict]] = []

        class _H(server.Handler):
            def __init__(self):
                self.sent = sent

            def _read_body(self):
                return payload

            def _json(self, code, body):
                sent.append((code, body))

        server.Handler._h_submit(_H())
        return sent[0] if sent else (None, None)

    def test_a_string_element_is_refused(self, isolated_queue):
        code, body = self._submit(
            {"type": "visual_edit_request", "selection": {"elements": ["x"]}, "comment": "c"}
        )
        assert code == 400
        assert body.get("code") == "selection_malformed"

    def test_a_mixed_list_is_refused(self, isolated_queue):
        """One bad element poisons the whole request, so all-or-nothing."""
        code, body = self._submit(
            {
                "type": "visual_edit_request",
                "selection": {"elements": [{"tag": "div"}, "x"]},
                "comment": "c",
            }
        )
        assert code == 400
        assert body.get("code") == "selection_malformed"

    def test_non_list_and_empty_elements_are_refused(self, isolated_queue):
        for bad in ({"elements": "div"}, {"elements": {}}, {"elements": []}, {}):
            code, body = self._submit(
                {"type": "visual_edit_request", "selection": bad, "comment": "c"}
            )
            assert code == 400, bad
            assert body.get("code") in ("selection_required", "selection_malformed"), bad

    def test_summarize_tolerates_an_already_poisoned_file(self):
        """The READ path must not 500 on a file written before the guard existed.

        A 500 here is unrecoverable without deleting the queue file by hand, so
        `_summarize_comment` drops non-dict elements instead of crashing.
        """
        summary = server._summarize_comment(
            {
                "cid": "abc",
                "comment": "make it blue",
                "selection": {"elements": ["x", None, 7, {"tag": "div", "id": "hero"}]},
            }
        )
        # The one usable element is found rather than the request being lost.
        assert summary["element"] == "div#hero"
        assert summary["count"] == 1

    def test_summarize_survives_an_all_bad_selection(self):
        summary = server._summarize_comment(
            {"cid": "abc", "comment": "c", "selection": {"elements": ["x", "y"]}}
        )
        assert summary["element"] == ""
        assert summary["count"] == 0

    # ── field TYPES, not just the element type ──────────────────────────────
    #
    # A dict passes the check above and still poisons the queue: `_el_name` did
    # `name = el.get("tag", "")` then `name += f"#{el['id']}"`, so a non-string
    # `tag` raised `TypeError: unsupported operand type(s) for +=: 'int' and
    # 'str'`. Same persistent-500 outage, one layer deeper.

    def test_a_non_string_tag_is_refused(self, isolated_queue):
        code, body = self._submit(
            {
                "type": "visual_edit_request",
                "selection": {"elements": [{"tag": 42, "id": "x"}]},
                "comment": "c",
            }
        )
        assert code == 400
        assert body.get("code") == "selection_malformed"

    def test_a_non_string_id_or_classes_is_refused(self, isolated_queue):
        bad_elements = [
            {"tag": "div", "id": 7},
            {"tag": "div", "classes": "card"},
            {"tag": "div", "classes": ["card", 3]},
            {"tag": "div", "classes": {"a": 1}},
        ]
        for el in bad_elements:
            code, body = self._submit(
                {
                    "type": "visual_edit_request",
                    "selection": {"elements": [el]},
                    "comment": "c",
                }
            )
            assert code == 400, el
            assert body.get("code") == "selection_malformed", el

    def test_well_formed_selections_are_still_accepted(self, isolated_queue):
        """The guard must not reject the shapes the preview legitimately sends.

        `id` and `classes` are both optional, and an empty `classes` list is
        normal for an element selected by tag alone -- so absence and emptiness
        must pass, or the guard would break ordinary use.
        """
        for el in (
            {"tag": "div"},
            {"tag": "div", "id": "hero"},
            {"tag": "div", "classes": []},
            {"tag": "div", "classes": ["card", "grid"]},
            {"tag": "div", "id": None, "classes": None},
        ):
            code, body = self._submit(
                {
                    "type": "visual_edit_request",
                    "selection": {"elements": [el]},
                    "comment": "c",
                }
            )
            # Anything other than a malformed-selection refusal means it got past
            # this guard; later stages may still answer for their own reasons.
            assert body is None or body.get("code") != "selection_malformed", el

    def test_summarize_tolerates_non_string_fields_already_on_disk(self):
        """The boundary guard cannot heal a file an OLDER build already wrote.

        This is the same read/write pairing the record-size ceiling needed: the
        write side refuses the shape, and the read side stays total so one
        legacy record cannot 500 `/queue` forever.
        """
        summary = server._summarize_comment(
            {
                "cid": "abc",
                "comment": "make it blue",
                "selection": {"elements": [{"tag": 42, "id": "x"}]},
            }
        )
        # Coerced into a label rather than raising.
        assert summary["element"] == "42#x"
        assert summary["count"] == 1

    def test_summarize_tolerates_non_string_classes_already_on_disk(self):
        summary = server._summarize_comment(
            {
                "cid": "abc",
                "comment": "c",
                "selection": {"elements": [{"tag": "div", "classes": ["card", 3, None]}]},
            }
        )
        # The unusable entries are dropped, not coerced into `.3` / `.None`.
        assert summary["element"] == "div.card"

    def test_summarize_tolerates_classes_that_is_not_a_list(self):
        summary = server._summarize_comment(
            {
                "cid": "abc",
                "comment": "c",
                "selection": {"elements": [{"tag": "div", "classes": "card"}]},
            }
        )
        assert summary["element"] == "div"


class TestSendIsASingleWinnerCut:
    """The seal is atomic with exactly one winner, and says so in the response.

    The frontend dispatches the agent prompt only when it performed the seal.
    That is only decidable because `/send` reports `already: True` to a loser —
    `ok` is True on BOTH paths, so a client checking only `ok` would dispatch
    twice and the agent would apply every edit in the batch a second time. This
    test locks the contract the frontend guard reads.
    """

    def _send(self, rid="r1"):
        sent: list[tuple[int, dict]] = []

        class _H(server.Handler):
            def __init__(self):
                self.sent = sent

            def _json(self, code, body):
                sent.append((code, body))

        server.Handler._h_send(_H(), {"id": [rid]})
        return sent[-1]

    def test_second_send_reports_already_and_does_not_reseal(self, isolated_queue):
        req = {
            "type": "visual_edit_batch",
            "id": "r1",
            "number": 7,
            "state": "draft",
            "projectId": "p1",
            "comments": [{"cid": "c1", "index": 1, "status": "new", "comment": "make it blue"}],
        }
        (isolated_queue / "r1.json").write_text(json.dumps(req), encoding="utf-8")

        code1, body1 = self._send()
        assert code1 == 200 and body1.get("ok") is True
        assert not body1.get("already"), "the FIRST caller performed the seal"
        first_sent_at = json.loads((isolated_queue / "r1.json").read_text())["sentAt"]

        code2, body2 = self._send()
        assert code2 == 200
        assert body2.get("ok") is True, "ok is True on both paths — hence `already`"
        assert body2.get("already") is True, "the loser must be told it did not seal"
        # The cut happened once: the timestamp is not overwritten by the loser.
        assert json.loads((isolated_queue / "r1.json").read_text())["sentAt"] == first_sent_at


# A canonical AWS example key — `redact_credentials` matches AKIA + 16 chars.
_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_EXFIL_HOST = "evil.example.com"


def _stored(fp) -> dict:
    """Read a queue file back, failing loudly if it became unreadable."""
    req = server._read_request(fp)
    assert req is not None
    return req


@pytest.fixture()
def queued_request(isolated_queue):
    """One real request file in a PRIVATE queue dir.

    `_write_request` re-asserts containment under `DATA_DIR`, so the file cannot
    live in a bare `tmp_path` — but `isolated_queue` redirects `DATA_DIR` itself,
    which satisfies the barrier without touching the shared real queue. Sharing
    it was a cross-worker race: `-n auto --dist loadgroup` distributes these
    tests independently, so two of them ran concurrently against ONE fixed
    filename and the first teardown deleted the file out from under the other.
    """
    rid = "test-thread-redaction"
    fp = server._request_file(server.QUEUE_DIR, rid)
    server._write_request(
        fp,
        {
            "id": rid,
            "number": 1,
            "state": "sent",
            "createdAt": server._now_iso(),
            "comments": [{"cid": "c1", "index": 1, "status": "sent", "comment": "tweak this"}],
        },
    )
    yield rid, fp


class TestThreadTextRedaction:
    """`/thread` text is AGENT output — it must be redacted BEFORE it is stored.

    The note is persisted into the queue JSON and rendered back in the panel, so
    the repo's mandatory output redaction applies. It runs at ingest, not on the
    way out: a leaked credential must not reach disk at all.
    """

    def _post(self, rid, payload, cid=""):
        import json as _json

        h = _JsonHandler(body=_json.dumps(payload).encode("utf-8"))
        qs = {"id": [rid]}
        if cid:
            qs["cid"] = [cid]
        h._h_thread(qs)
        return h

    def test_credential_does_not_survive_into_the_queue_json(self, queued_request):
        rid, fp = queued_request
        h = self._post(rid, {"role": "agent", "text": f"used key {_FAKE_KEY} to deploy"})
        assert h.json_sent[0][0] == 200

        # The assertion that matters: the raw bytes on disk.
        raw = fp.read_text("utf-8")
        assert _FAKE_KEY not in raw
        assert "[REDACTED: credential]" in raw

        stored = _stored(fp)
        note = stored["thread"][-1]
        assert _FAKE_KEY not in note["text"]
        assert note["role"] == "agent"

    def test_credential_in_a_comment_level_note_is_redacted_too(self, queued_request):
        rid, fp = queued_request
        self._post(rid, {"role": "agent", "text": f"token: {_FAKE_KEY}"}, cid="c1")
        assert _FAKE_KEY not in fp.read_text("utf-8")
        stored = _stored(fp)
        note = stored["comments"][0]["thread"][-1]
        assert "[REDACTED: credential]" in note["text"]

    def test_exfiltration_url_is_redacted_host_and_all(self, queued_request):
        """Also pins the ORDER: URLs are redacted before credentials.

        A credential-first pass would rewrite only the `AKIA…` inside the query
        and leave a live, fetchable `https://evil.example.com/collect?key=…`
        standing. So the proof is that the URL was replaced WHOLE by the URL tag
        (which names the host by design), not merely stripped of its token.
        """
        rid, fp = queued_request
        url = f"https://{_EXFIL_HOST}/collect?key={_FAKE_KEY}"
        self._post(rid, {"role": "agent", "text": f"posting to {url}"})
        raw = fp.read_text("utf-8")
        assert _FAKE_KEY not in raw
        assert f"https://{_EXFIL_HOST}" not in raw
        assert f"[REDACTED: suspicious URL to {_EXFIL_HOST}]" in raw
        assert "[REDACTED: credential]" not in raw, "credential pass ran first"

    def test_clean_text_is_stored_verbatim(self, queued_request):
        """Redaction must not mangle ordinary progress notes."""
        rid, fp = queued_request
        msg = "Renamed the button label in src/app/Header.tsx and ran tsc."
        self._post(rid, {"role": "agent", "text": msg})
        assert _stored(fp)["thread"][-1]["text"] == msg

    def test_status_only_note_still_works(self, queued_request):
        """Empty text + a status is a valid call; redaction must not break it."""
        rid, fp = queued_request
        h = self._post(rid, {"role": "agent", "status": "done"}, cid="c1")
        assert h.json_sent[0][0] == 200
        assert _stored(fp)["comments"][0]["status"] == "done"


class TestCommentTextIsRedactedOnTheWayOut:
    """The comment TEXT gets the same output floor as the thread text.

    `/submit` redacts on ingest, but ingest is not the only writer: the delivery
    model hands the agent the queue JSON directly, so a comment rewritten in the
    FILE would otherwise render verbatim in the panel. `_summarize_comment`
    redacted its sibling `thread` field and returned `comment` raw, which is the
    gap these tests close.
    """

    def test_a_credential_in_the_comment_is_redacted(self):
        out = server._summarize_comment({"cid": "c1", "comment": f"use {_FAKE_KEY} here"})
        assert _FAKE_KEY not in out["comment"]

    def test_a_plain_external_url_is_left_intact(self):
        """Deliberate, not a gap: only a URL CARRYING a secret is rewritten.

        `redact_exfiltration_urls` does not blanket-redact external links --
        doing so would mangle the ordinary "see https://react.dev/..." note. The
        suspicious-URL rule fires on a URL that carries something worth
        exfiltrating, which the next test covers.
        """
        msg = f"post it to https://{_EXFIL_HOST}/collect"
        assert server._summarize_comment({"cid": "c1", "comment": msg})["comment"] == msg

    def test_a_credential_inside_a_url_leaves_no_host_standing(self):
        """Pins the ORDER, which is why the two calls live in one helper.

        `redact_exfiltration_urls` keys off the host, so a credential-first pass
        would rewrite only the token inside the query and leave a live, fetchable
        URL standing. The proof is that the URL was replaced WHOLE by the URL tag,
        not merely stripped of its token.
        """
        out = server._summarize_comment(
            {"cid": "c1", "comment": f"https://{_EXFIL_HOST}/x?key={_FAKE_KEY}"}
        )
        assert _FAKE_KEY not in out["comment"]
        assert f"https://{_EXFIL_HOST}" not in out["comment"]
        assert f"[REDACTED: suspicious URL to {_EXFIL_HOST}]" in out["comment"]
        assert "[REDACTED: credential]" not in out["comment"], "credential pass ran first"

    def test_ordinary_comment_text_is_untouched(self):
        """The floor must not mangle the text users actually write."""
        out = server._summarize_comment({"cid": "c1", "comment": "make the header bigger"})
        assert out["comment"] == "make the header bigger"

    def test_a_missing_or_non_string_comment_does_not_raise(self):
        """Same read-path totality rule as `_el_name`: never 500 the queue."""
        assert server._summarize_comment({"cid": "c1"})["comment"] == ""
        assert server._summarize_comment({"cid": "c1", "comment": 42})["comment"] == "42"
        assert server._summarize_comment({"cid": "c1", "comment": None})["comment"] == ""


class _GetHandler(server.Handler):
    """Minimal ``do_GET`` harness: bypass proxy auth, capture the JSON reply."""

    def __init__(self, path):
        self.path = path
        self.sent: list[tuple[int, Any]] = []
        self.headers = {}

    def _authorized(self, *_a, **_k):
        return True  # auth is not what this test is about

    def _json(self, code, payload) -> None:
        self.sent.append((code, payload))


class TestLatestRouteRedaction:
    """``GET /latest`` hands the agent the raw request dict, unlike ``/queue``
    and ``/history`` which go through ``_summarize()``. It must apply the SAME
    ``_redact_thread`` floor those two already apply, or agent-written
    credential text sitting in the queue JSON — persisted before the ingest
    pass existed, or written by some path other than ``/thread`` — is served
    verbatim through this authenticated GET.
    """

    def test_request_level_thread_credential_is_redacted(self, isolated_queue):
        rid = "test-latest-redaction"
        fp = server._request_file(server.QUEUE_DIR, rid)
        server._write_request(
            fp,
            {
                "id": rid,
                "number": 1,
                "state": "sent",
                "createdAt": server._now_iso(),
                # Written directly, bypassing /thread's ingest-time redaction —
                # exactly the gap _redact_thread's own docstring calls out.
                "thread": [{"role": "agent", "text": f"used key {_FAKE_KEY} to deploy"}],
                "comments": [],
            },
        )
        h = _GetHandler("/latest")
        server.Handler.do_GET(h)
        code, payload = h.sent[0]
        assert code == 200
        assert _FAKE_KEY not in json.dumps(payload)
        assert "[REDACTED: credential]" in payload["thread"][0]["text"]

    def test_comment_level_thread_credential_is_redacted(self, isolated_queue):
        rid = "test-latest-redaction-comment"
        fp = server._request_file(server.QUEUE_DIR, rid)
        server._write_request(
            fp,
            {
                "id": rid,
                "number": 1,
                "state": "sent",
                "createdAt": server._now_iso(),
                "thread": [],
                "comments": [
                    {
                        "cid": "c1",
                        "index": 1,
                        "status": "sent",
                        "comment": "tweak this",
                        "thread": [{"role": "agent", "text": f"token: {_FAKE_KEY}"}],
                    }
                ],
            },
        )
        h = _GetHandler("/latest")
        server.Handler.do_GET(h)
        code, payload = h.sent[0]
        assert code == 200
        assert _FAKE_KEY not in json.dumps(payload)
        assert "[REDACTED: credential]" in payload["comments"][0]["thread"][0]["text"]

    def test_other_fields_pass_through_unchanged(self, isolated_queue):
        """The agent needs the FULL payload — only thread text is redacted."""
        rid = "test-latest-passthrough"
        fp = server._request_file(server.QUEUE_DIR, rid)
        server._write_request(
            fp,
            {
                "id": rid,
                "number": 1,
                "state": "sent",
                "createdAt": server._now_iso(),
                "projectId": "abc123",
                "projectRoot": "/tmp/project",
                "thread": [],
                "comments": [
                    {
                        "cid": "c1",
                        "index": 1,
                        "status": "sent",
                        "comment": "tweak this",
                        "sourceFile": "src/App.tsx",
                        "thread": [],
                    }
                ],
            },
        )
        h = _GetHandler("/latest")
        server.Handler.do_GET(h)
        _code, payload = h.sent[0]
        assert payload["projectId"] == "abc123"
        assert payload["projectRoot"] == "/tmp/project"
        assert payload["comments"][0]["sourceFile"] == "src/App.tsx"

    def test_no_pending_requests_returns_empty_dict(self, isolated_queue):
        h = _GetHandler("/latest")
        server.Handler.do_GET(h)
        assert h.sent[0] == (200, {})


def _widen_the_race(monkeypatch, contenders: int = 2):
    """Hold the first write until every other contender has reached the queue.

    A contender has reached it once it has asked for the queue lock or read the
    queue (a request file or the pending listing). A version holding the lock
    across read-through-write parks every other contender on that lock, so the
    write goes ahead and they read what it wrote. A version that takes no lock,
    or takes it only around the write, lets them read the queue before the write
    lands, so a lost update is forced on every run rather than left to thread
    scheduling.
    """
    reached: set[int] = set()
    guard = threading.Lock()
    first_write = threading.Event()
    real_lock = server._QUEUE_LOCK
    real_write = server._write_request
    real_read = server._read_request
    real_pending = server._pending_files

    def _arrived() -> None:
        with guard:
            reached.add(threading.get_ident())

    class _ObservedLock:
        def __enter__(self):
            _arrived()
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    def observed_read(fp):
        _arrived()
        return real_read(fp)

    def observed_pending():
        _arrived()
        return real_pending()

    def held_write(fp, req):
        with guard:
            first = not first_write.is_set()
            first_write.set()
        if first:
            me = threading.get_ident()
            wait_until(
                lambda: len(reached - {me}) >= contenders - 1,
                describe=lambda: f"{len(reached - {me})} of {contenders - 1} others reached",
            )
        real_write(fp, req)

    monkeypatch.setattr(server, "_QUEUE_LOCK", _ObservedLock())
    monkeypatch.setattr(server, "_read_request", observed_read)
    monkeypatch.setattr(server, "_pending_files", observed_pending)
    monkeypatch.setattr(server, "_write_request", held_write)


_AT_LIMIT_RID = "1700000000000-atlimit"


def _record_serialising_to(rid: str, size: int) -> dict:
    """A queue record whose `json.dumps(..., indent=2)` encoding is exactly `size` bytes.

    Solved, not searched. The writer's ceiling is a property of the serialised
    bytes, so a candidate can be measured in memory; growing a record and calling
    the real writer at each step spends O(n^2) bytes of fsynced disk I/O to learn
    the same number. The pad is `a`, which JSON never escapes, so length is linear
    in the pad and the correction below lands in one step.
    """
    req: dict = {"id": rid, "notes": [""]}
    for _ in range(3):
        have = len(json.dumps(req, indent=2).encode("utf-8"))
        if have == size:
            return req
        if len(req["notes"][0]) + size - have < 0:
            raise AssertionError(f"{size} bytes is below the record skeleton")
        req["notes"][0] = "a" * (len(req["notes"][0]) + size - have)
    raise AssertionError(f"could not serialise to exactly {size} bytes")


def _submit(project_id: str, text: str, extra: dict | None = None) -> _JsonHandler:
    payload = {
        "type": "visual_edit_request",
        "comment": text,
        "projectId": project_id,
        "selection": {"mode": "single", "elements": [{"tag": "div", "id": text}]},
    }
    if extra:
        payload.update(extra)
    h = _JsonHandler(body=json.dumps(payload).encode("utf-8"))
    h._h_submit()
    return h


class TestCommentIdIsServerMinted:
    """The comment id is the lookup key, so the caller must not get to choose it.

    `/delete-comment` and `/thread` both read `cid` from the QUERY STRING, so the
    value they compare against is always a `str`. A caller-supplied id defeats
    that two ways: a JSON number persists as an int and no lookup ever matches it
    again (an undeletable, unanswerable comment), and a duplicate of an existing
    id makes one delete take both comments.
    """

    def test_numeric_cid_from_the_payload_is_not_persisted(self, isolated_queue):
        _submit("p-cid-num", "one", extra={"cid": 123})
        fp = next(iter(server.QUEUE_DIR.glob("*.json")))
        cid = _stored(fp)["comments"][0]["cid"]
        assert isinstance(cid, str), "an int cid breaks every later string lookup"
        assert cid not in ("123", 123)

    def test_caller_cannot_force_a_duplicate_cid(self, isolated_queue):
        _submit("p-cid-dup", "one", extra={"cid": "fixed"})
        _submit("p-cid-dup", "two", extra={"cid": "fixed"})
        cids = [
            c["cid"]
            for fp in sorted(server.QUEUE_DIR.glob("*.json"))
            for c in _stored(fp)["comments"]
        ]
        assert len(cids) == 2
        assert len(set(cids)) == 2, "one delete would remove both of these"

    def test_minted_cid_matches_the_id_format_the_lookups_expect(self, isolated_queue):
        _submit("p-cid-fmt", "one")
        fp = next(iter(server.QUEUE_DIR.glob("*.json")))
        cid = _stored(fp)["comments"][0]["cid"]
        assert server._ID_RE.match(cid)


def _run_concurrently(fns) -> None:
    threads = [threading.Thread(target=fn) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a submit thread hung — deadlock?"


class TestConcurrentQueueWrites:
    """`ThreadingHTTPServer` means two dashboard tabs are two handler threads.

    Appending a comment is a read-modify-write of one JSON file. Held only around
    the write (or not at all), both threads read the same draft, append to their
    own copy, and the second write silently discards the other comment — the
    queue file IS the record, so that is data loss, not a stale read.
    """

    def test_concurrent_appends_to_one_draft_keep_both_comments(self, isolated_queue, monkeypatch):
        rid = "1700000000000-aaaaaa"
        server._write_request(
            server._request_file(server.QUEUE_DIR, rid),
            {
                "type": "visual_edit_batch",
                "id": rid,
                "number": 1,
                "state": "draft",
                "projectId": "p-concurrent",
                "projectRoot": "",
                "createdAt": server._now_iso(),
                "sentAt": "",
                "thread": [],
                "comments": [
                    {"cid": "seed", "index": 1, "status": "new", "comment": "seed", "thread": []}
                ],
            },
        )
        _widen_the_race(monkeypatch)

        _run_concurrently(
            [
                lambda: _submit("p-concurrent", "from-tab-a"),
                lambda: _submit("p-concurrent", "from-tab-b"),
            ]
        )

        files = sorted(server.QUEUE_DIR.glob("*.json"))
        assert len(files) == 1, "the project's single open draft was forked"
        req = _stored(files[0])
        got = [c["comment"] for c in req["comments"]]
        assert sorted(got) == ["from-tab-a", "from-tab-b", "seed"], got
        # Sub-numbering stays contiguous — two threads must not both claim index 2.
        assert [c["index"] for c in req["comments"]] == [1, 2, 3]

    def test_concurrent_first_comments_open_exactly_one_draft(self, isolated_queue, monkeypatch):
        """The find-or-create half of the same transaction.

        With the read and the write in separate critical sections, every thread
        sees "no open draft" and creates its own — the project ends up with N
        drafts all numbered 1, which is the same lost-update bug wearing a
        different hat.
        """
        labels = [f"c{n}" for n in range(4)]
        _widen_the_race(monkeypatch, contenders=len(labels))
        _run_concurrently([lambda t=t: _submit("p-fresh", t) for t in labels])

        files = sorted(server.QUEUE_DIR.glob("*.json"))
        assert len(files) == 1, f"opened {len(files)} drafts for one project"
        req = _stored(files[0])
        assert sorted(c["comment"] for c in req["comments"]) == labels
        assert [c["index"] for c in req["comments"]] == [1, 2, 3, 4]

    def test_send_and_submit_do_not_interleave(self, isolated_queue, monkeypatch):
        """Seal-on-send races an in-flight submit.

        Whichever order they land in, the outcome must be self-consistent: either
        the comment made it into the batch before it sealed (all three sent), or
        it opened a fresh draft. What must NOT happen is a comment vanishing, or a
        sealed request carrying a `new` comment the agent will never be told about.
        """
        rid = "1700000000000-bbbbbb"
        server._write_request(
            server._request_file(server.QUEUE_DIR, rid),
            {
                "type": "visual_edit_batch",
                "id": rid,
                "number": 1,
                "state": "draft",
                "projectId": "p-race",
                "projectRoot": "",
                "createdAt": server._now_iso(),
                "sentAt": "",
                "thread": [],
                "comments": [
                    {"cid": "seed", "index": 1, "status": "new", "comment": "seed", "thread": []}
                ],
            },
        )
        _widen_the_race(monkeypatch)

        def send():
            _JsonHandler()._h_send({"id": [rid]})

        _run_concurrently([lambda: _submit("p-race", "late"), send])

        seen = []
        for fp in sorted(server.QUEUE_DIR.glob("*.json")):
            seen.extend(c["comment"] for c in _stored(fp)["comments"])
        assert sorted(seen) == ["late", "seed"], "a comment was lost to the race"
        sealed = _stored(server._request_file(server.QUEUE_DIR, rid))
        if sealed["state"] == "sent":
            assert all(
                c["status"] != "new" for c in sealed["comments"]
            ), "sealed a request with an unsent comment"


class TestDeliveryAcknowledgement:
    """`/send` seals; the panel dispatches afterwards. Two steps need an ack.

    A tab closed between the seal and the prompt reaching the agent would leave
    the request sealed and undeliverable: the send bar only renders for a draft,
    so the batch was stranded with no retry. `deliveredAt` records that the
    dispatch actually happened, which is what lets the panel offer a resend for
    the sealed-but-unacknowledged case.
    """

    def _seal(self, rid):
        h = _JsonHandler()
        h._h_send({"id": [rid]})
        return h.json_sent[0]

    def _ack(self, rid):
        h = _JsonHandler()
        h._h_delivered({"id": [rid]})
        return h.json_sent[0]

    def test_sealed_request_starts_unacknowledged(self, queued_request):
        """The stranded state must be observable, or the panel cannot offer a retry."""
        rid, fp = queued_request
        code, body = self._seal(rid)
        assert code == 200 and body["ok"]
        assert body["request"]["deliveredAt"] == ""
        assert not _stored(fp).get("deliveredAt")

    def test_ack_stamps_delivered_at(self, queued_request):
        rid, fp = queued_request
        self._seal(rid)
        code, body = self._ack(rid)
        assert code == 200 and body["ok"]
        stamped = _stored(fp)["deliveredAt"]
        assert stamped
        assert body["request"]["deliveredAt"] == stamped

    def test_ack_is_idempotent_and_keeps_the_first_timestamp(self, queued_request):
        """A duplicate ack must not make a delivered request look newer."""
        rid, fp = queued_request
        self._seal(rid)
        self._ack(rid)
        first = _stored(fp)["deliveredAt"]
        code, _ = self._ack(rid)
        assert code == 200
        assert _stored(fp)["deliveredAt"] == first

    def test_draft_cannot_be_acknowledged(self, queued_request):
        """Acking a draft would clear the retry bar for the state it exists for."""
        rid, fp = queued_request
        # The fixture writes a sealed request; make it a genuine draft. Both
        # halves are required — `_request_status` treats COMMENT statuses as
        # authoritative, so resetting `state` alone still reads as "sent".
        req = _stored(fp)
        req["state"] = "draft"
        for c in req["comments"]:
            c["status"] = "new"
        server._write_request(fp, req)
        assert server._is_draft(_stored(fp)), "test setup did not produce a draft"

        code, body = self._ack(rid)
        assert code == 409
        assert body["code"] == "not_sealed"
        assert not _stored(fp).get("deliveredAt")

    def test_ack_rejects_a_bad_id_without_touching_disk(self, isolated_queue):
        h = _JsonHandler()
        h._h_delivered({"id": ["../etc/passwd"]})
        code, body = h.json_sent[0]
        assert code == 400 and body["code"] == "id_required"
        assert not list(isolated_queue.glob("*.json"))

    def test_ack_on_a_missing_request_is_404(self, isolated_queue):
        code, body = self._ack("no-such-request")
        assert code == 404 and body["code"] == "not_found"

    def test_delivered_route_is_registered(self):
        """The handler is only reachable if do_POST routes to it."""
        import inspect

        src = inspect.getsource(server.Handler.do_POST)
        assert '"/delivered"' in src
        assert "_h_delivered" in src


class TestThreadRedactionOnRead:
    """Redaction is a floor on BOTH edges, not just at ingest.

    `_h_thread` redacts before writing, but ingest is not the only writer: the
    delivery model hands the agent the queue JSON directly, so an entry written
    into the file (or persisted before the ingest pass existed) would otherwise be
    rendered verbatim in the panel.
    """

    def _req_with_thread(self, entries, comment_thread=None):
        return {
            "id": "r1",
            "number": 1,
            "state": "sent",
            "createdAt": server._now_iso(),
            "thread": entries,
            "comments": [
                {
                    "cid": "c1",
                    "index": 1,
                    "status": "sent",
                    "comment": "tweak",
                    "thread": comment_thread if comment_thread is not None else [],
                }
            ],
        }

    def test_request_level_thread_is_redacted_on_serialize(self):
        req = self._req_with_thread([{"role": "agent", "text": f"key {_FAKE_KEY} used"}])
        out = server._summarize(req)
        assert _FAKE_KEY not in json.dumps(out)
        assert "[REDACTED: credential]" in out["thread"][0]["text"]

    def test_comment_level_thread_is_redacted_on_serialize(self):
        req = self._req_with_thread([], comment_thread=[{"role": "agent", "text": _FAKE_KEY}])
        out = server._summarize(req)
        assert _FAKE_KEY not in json.dumps(out)
        assert "[REDACTED: credential]" in out["comments"][0]["thread"][0]["text"]

    def test_url_pass_runs_before_the_credential_pass(self):
        """Same ordering the ingest path pins: a whole exfil URL, host and all.

        Credentials-first would rewrite the token inside the URL and leave the
        host standing, which is the thing that makes the URL dangerous.
        """
        url = f"https://{_EXFIL_HOST}/collect?key={_FAKE_KEY}"
        out = server._summarize(self._req_with_thread([{"role": "agent", "text": f"to {url}"}]))
        text = out["thread"][0]["text"]
        assert _FAKE_KEY not in text
        assert _EXFIL_HOST not in text.replace(f"[REDACTED: suspicious URL to {_EXFIL_HOST}]", "")
        assert f"[REDACTED: suspicious URL to {_EXFIL_HOST}]" in text

    def test_clean_thread_text_is_untouched(self):
        out = server._summarize(
            self._req_with_thread([{"role": "agent", "text": "renamed the CTA"}])
        )
        assert out["thread"][0]["text"] == "renamed the CTA"

    def test_non_text_fields_survive(self):
        """Only `text` is rewritten — role/ts must reach the panel intact."""
        entry = {"role": "agent", "text": "ok", "ts": "2026-01-01T00:00:00Z", "cid": "c1"}
        got = server._summarize(self._req_with_thread([entry]))["thread"][0]
        assert got["role"] == "agent" and got["ts"] == entry["ts"] and got["cid"] == "c1"

    def test_malformed_thread_does_not_break_the_read_path(self):
        """A hand-edited file must not 500 `/queue` for every later poll."""
        req = self._req_with_thread(["not-a-dict", {"role": "agent", "text": "fine"}])
        out = server._summarize(req)
        assert [e["text"] for e in out["thread"]] == ["fine"]

        req2 = self._req_with_thread("not-a-list")
        assert server._summarize(req2)["thread"] == []


class TestPreviewUrlProjectResolution:
    """`sourceFile` must survive the removal of the gateway-proxied route.

    Static previews are served from this backend's own loopback origin, so a URL
    parser that only knows the old `/api/proxy/` shape leaves every static
    capture's `sourceFile` empty — the value the visual-edit prompt hands the
    agent as the file to edit.
    """

    def test_static_preview_url_yields_project_and_file(self, monkeypatch):
        monkeypatch.setitem(server._CFG, "projects", [{"id": "p1", "path": "/tmp/x"}])
        monkeypatch.setattr(server, "_STATIC_SRV", {"p1": {"url": "http://127.0.0.1:9911/"}})
        proj, rel = server._proj_for_preview("http://127.0.0.1:9911/p1/src/index.html")
        assert proj is not None and proj["id"] == "p1"
        assert rel == "src/index.html"

    def test_dev_server_route_is_not_read_as_a_file(self, monkeypatch):
        # Same path SHAPE as a static preview; only the origin differs. A route is
        # not a file on disk, so guessing one would misdirect the agent.
        monkeypatch.setitem(server._CFG, "projects", [{"id": "pricing", "path": "/tmp/x"}])
        monkeypatch.setattr(server, "_STATIC_SRV", {"pricing": {"url": "http://127.0.0.1:9911/"}})
        proj, rel = server._proj_for_preview("http://localhost:5173/pricing/plans")
        assert proj is None
        assert rel == ""

    def test_legacy_proxy_url_still_parses(self, monkeypatch):
        # Comments captured before the proxied route was deleted still carry it.
        monkeypatch.setitem(server._CFG, "projects", [{"id": "p1", "path": "/tmp/x"}])
        monkeypatch.setattr(server, "_STATIC_SRV", {"p1": {"url": "http://127.0.0.1:9911/"}})
        proj, rel = server._proj_for_preview("/apps/design-tweak/api/proxy/p1/a/b.html")
        assert proj is not None and proj["id"] == "p1"
        assert rel == "a/b.html"

    def test_two_projects_own_recorded_origins_do_not_cross_resolve(self, monkeypatch):
        """A URL on p1's origin must resolve to p1, never to p2, even though
        both projects are registered and both have a live entry in
        `_STATIC_SRV` at the same time."""
        monkeypatch.setitem(
            server._CFG,
            "projects",
            [{"id": "p1", "path": "/tmp/x"}, {"id": "p2", "path": "/tmp/y"}],
        )
        monkeypatch.setattr(
            server,
            "_STATIC_SRV",
            {
                "p1": {"url": "http://127.0.0.1:9911/"},
                "p2": {"url": "http://127.0.0.1:9922/"},
            },
        )
        proj, rel = server._proj_for_preview("http://127.0.0.1:9922/p2/index.html")
        assert proj is not None and proj["id"] == "p2"
        assert rel == "index.html"
