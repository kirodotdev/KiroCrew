"""Tests for the MCP-only crew-scoped session reads: the wire behaviour.

``/api/crew-sessions/{search,list,read}`` (``handlers_instances_crew_reads``) back
the ``crew=`` scope on the kirocrew-core session read tools. Direct-handler tests
of what crosses the tunnel and what comes back: the STRICT internal-secret gate,
unknown-crew 404, crew resolution, peer reply reshaping and redaction, the
``exclude_incognito`` contract on both sides of the hop, key vetting, and
proxy-error and peer-status mapping. The owner-provenance roster and ancestry
walk are pinned in ``test_instances_crew_reads_auth.py``.

Shared doubles come from ``instances_crew_reads_helpers`` (this family) and
``instances_helpers`` (every per-topic instances test).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from instances_crew_reads_helpers import CrewReadsCase
from instances_helpers import _body, _enable, _FakeReq, _State


class TestCrewScopedSessionReads(CrewReadsCase):
    """MCP-only crew-scoped session reads (api_crew_sessions_search|list|read)."""

    def test_search_requires_internal_secret(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # A request WITHOUT internal_auth (a cookie fall-through) is refused.
        req = _FakeReq(_State(reg, self._mgr_with()), query={"crew": "cd-1", "q": "hello"})
        r = asyncio.run(handlers.api_crew_sessions_search(req))
        assert r.status == 403
        assert _body(r)["code"] == "internal_secret_required"

    def test_search_unknown_crew_404(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        state = _State(reg, self._mgr_with())
        r = asyncio.run(
            handlers.api_crew_sessions_search(self._internal_req(state, crew="nope", q="hello"))
        )
        assert r.status == 404
        assert _body(r)["code"] == "crew_unknown"

    def test_search_reshapes_and_resolves_by_name(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        payload = (
            True,
            {
                "sessions": [
                    {"key": "k1", "title": "Hello", "snippet": "world", "created": "2026-01-01"},
                    {"title": "no key — dropped"},
                ]
            },
        )
        state = _State(reg, self._mgr_with(search=payload))
        # Resolve the crew by NAME, not just id.
        r = asyncio.run(
            handlers.api_crew_sessions_search(self._internal_req(state, crew="CD", q="hello"))
        )
        assert r.status == 200
        rows = _body(r)["sessions"]
        assert [row["key"] for row in rows] == ["k1"]  # keyless row dropped
        assert rows[0]["title"] == "Hello"

    def test_search_maps_remote_failure(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        payload = (False, {"error": "peer prose that must not reach the agent", "code": "x"})
        state = _State(reg, self._mgr_with(search=payload))
        r = asyncio.run(
            handlers.api_crew_sessions_search(self._internal_req(state, crew="cd-1", q="hello"))
        )
        assert r.status == 502
        assert _body(r)["code"] == "crew_peer_refused"
        assert "peer prose" not in r.text

    def test_list_reshapes_rows(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        body = json.dumps(
            {"sessions": [{"key": "s1", "title": "T", "agent": "kiro", "messages": 4}]}
        ).encode()
        state = _State(reg, self._mgr_with(list_body=body))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert r.status == 200
        rows = _body(r)["sessions"]
        assert rows[0]["key"] == "s1" and rows[0]["messages"] == 4

    def test_read_returns_tail_capped_messages(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        # api_session_detail returns a raw ARRAY; three messages, cap to 2.
        body = json.dumps(
            [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ]
        ).encode()
        state = _State(reg, self._mgr_with(read_body=body))
        r = asyncio.run(
            handlers.api_crew_sessions_read(
                self._internal_req(state, crew="cd-1", key="k1", max_messages="2")
            )
        )
        assert r.status == 200
        msgs = _body(r)["messages"]
        assert [m["content"] for m in msgs] == ["two", "three"]  # tail-capped

    def test_read_rejects_traversal_key(self, tmp_path, monkeypatch):
        """A refused key is a denied crew read and is audited as one, with the
        caller's key: the audit middleware skips GETs, so without the handler's own
        row SEL would hold the authorization and not the refusal."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        events: list[tuple] = []
        monkeypatch.setattr(handlers, "_audit", lambda *a, **k: events.append((a, k)))
        mgr = self._mgr_with(read_body=b"[]")
        state = _State(reg, mgr)
        r = asyncio.run(
            handlers.api_crew_sessions_read(self._internal_req(state, crew="cd-1", key="../secret"))
        )
        assert r.status == 400
        assert _body(r)["code"] == "crew_bad_key"
        denied = [
            (a[0], a[1], k.get("error"), k.get("caller")) for a, k in events if a[1] == "denied"
        ]
        assert denied == [("crew_read", "denied", "crew_bad_key", "dashboard:ui")]
        assert getattr(mgr, "last_path", None) is None, "a refused key must not reach the peer"

    def test_read_maps_proxy_error(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        exc = ProxyRequestError("proxy_peer_not_connected", "down", http_status=503)
        state = _State(reg, self._mgr_with(raise_exc=exc))
        r = asyncio.run(
            handlers.api_crew_sessions_read(self._internal_req(state, crew="cd-1", key="k1"))
        )
        assert r.status == 503
        assert _body(r)["code"] == "proxy_peer_not_connected"

    @pytest.mark.parametrize(
        "peer_status, hub_status, code, peer_confirms",
        [
            # A real 503 from the peer's exclude_incognito arm carries no
            # confirmation header, so this case also pins that the status is
            # judged BEFORE the header: busy, never "too old".
            (503, 503, "crew_peer_busy", False),
            (503, 503, "crew_peer_busy", True),
            (404, 404, "crew_session_not_found", False),
            (500, 502, "crew_peer_refused", False),
        ],
    )
    def test_read_keeps_the_peers_own_status(
        self, tmp_path, monkeypatch, peer_status, hub_status, code, peer_confirms
    ):
        """A peer's non-2xx survives the hop with its meaning, never as "malformed".

        The peer's ``exclude_incognito`` arm answers ``503 transcript_busy`` so a
        caller does not mistake a retryable lock for an empty conversation; the hub
        must carry that distinction to the agent rather than collapse it into
        ``crew_malformed_reply``. A ``404`` is a key that names nothing on that peer.
        The peer's body is never echoed: it is untrusted text, and the status is the
        diagnosis.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        mgr = self._mgr_with(
            read_status=peer_status,
            read_body=b'{"error": "peer prose that must not reach the agent", "code": "x"}',
            peer_confirms_exclusion=peer_confirms,
        )
        state = _State(reg, mgr)
        r = asyncio.run(
            handlers.api_crew_sessions_read(self._internal_req(state, crew="cd-1", key="k1"))
        )
        assert r.status == hub_status
        assert _body(r)["code"] == code
        assert "peer prose" not in r.text
        assert mgr.body_reads == 0

    def test_list_404_is_a_refusal_not_a_missing_session(self, tmp_path, monkeypatch):
        """The list path names no session, so a peer ``404`` there cannot mean "no
        such session"; it is reported as the peer's refusal."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg, self._mgr_with(list_status=404, list_body=b"{}"))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert r.status == 502
        assert _body(r)["code"] == "crew_peer_refused"

    def test_list_reports_an_over_cap_reply_as_too_large(self, tmp_path, monkeypatch):
        """A reply past ``PEER_SESSIONS_REPLY_MAX_BYTES`` is a size the hub refuses,
        reported as ``crew_reply_too_large`` so a session that outgrew the bound is
        told so -- not as a peer that answered garbage."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.instances.constants import PEER_SESSIONS_REPLY_MAX_BYTES

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        huge = (
            b'{"sessions": ['
            + b'{"key": "k", "title": "'
            + b"x" * PEER_SESSIONS_REPLY_MAX_BYTES
            + b'"}]}'
        )
        state = _State(reg, self._mgr_with(list_body=huge))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert r.status == 502
        assert _body(r)["code"] == "crew_reply_too_large"

    @pytest.mark.parametrize("leg", ["read", "list", "search"])
    def test_a_large_peer_reply_is_decoded_and_redacted_off_the_event_loop(
        self, tmp_path, monkeypatch, leg
    ):
        """A crew transcript legitimately runs to megabytes, and the gateway runs
        every chat stream and the liveness heartbeat on one loop: decoding the reply
        and running the redaction chain over every row inline would hold that loop
        for the whole pass. Past ``_OFF_LOOP_BYTES`` both run on a thread, as the
        peer proxy's ``_redact_sse_event_async`` does; a small reply stays inline.

        Asserted on the THREAD the redaction ran on, not by spying on
        ``asyncio.to_thread``, so it pins the property rather than the mechanism.
        """
        import threading

        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.dashboard import handlers_instances_crew_reads as crew_reads

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        seen: list[str] = []
        real_cap = handlers._cap_str

        def _spy(value, limit):
            seen.append(threading.current_thread().name)
            return real_cap(value, limit)

        # Through the facade: ``_cap_str`` is bound by two owners, and the mirror
        # patches every holder (and restores every holder) only from there.
        monkeypatch.setattr(handlers, "_cap_str", _spy)
        loop_thread: dict[str, str] = {}

        def _run(body: bytes):
            seen.clear()
            if leg == "read":
                mgr = self._mgr_with(read_body=body)
                call, query = handlers.api_crew_sessions_read, {"crew": "cd-1", "key": "k1"}
            elif leg == "list":
                mgr = self._mgr_with(list_body=body)
                call, query = handlers.api_crew_sessions_list, {"crew": "cd-1", "limit": "50"}
            else:
                mgr = self._mgr_with(search=(True, json.loads(body)))
                call, query = handlers.api_crew_sessions_search, {
                    "crew": "cd-1",
                    "q": "needle",
                    "limit": "50",
                }
            state = _State(reg, mgr)

            async def _go():
                loop_thread["name"] = threading.current_thread().name
                return await call(self._internal_req(state, **query))

            return asyncio.run(_go())

        if leg == "read":
            # A transcript body keeps its own, larger ceiling, so one long message
            # is both over the off-loop threshold and still redacted.
            big = "x" * (crew_reads._OFF_LOOP_BYTES + 1)
            large = json.dumps([{"role": "user", "content": big}]).encode()
            small = json.dumps([{"role": "user", "content": "hi"}]).encode()
        else:
            # A short field over ``_FIELD_MAX_RAW_CHARS`` is replaced without being
            # redacted, so a large LIST reply is many honest rows, each redacted.
            title = "x" * (crew_reads._FIELD_MAX_RAW_CHARS // 2)
            rows = crew_reads._OFF_LOOP_BYTES // len(title) + 2
            large = json.dumps(
                {"sessions": [{"key": f"s{i}", "title": title} for i in range(rows)]}
            ).encode()
            assert len(large) > crew_reads._OFF_LOOP_BYTES
            small = json.dumps({"sessions": [{"key": "s1", "title": "hi"}]}).encode()

        r = _run(large)
        assert r.status == 200
        assert seen and all(t != loop_thread["name"] for t in seen), "redacted ON the loop"
        r = _run(small)
        assert r.status == 200
        assert seen and all(t == loop_thread["name"] for t in seen), "a thread hop for nothing"

    def test_an_over_long_short_field_is_replaced_never_cut(self, tmp_path, monkeypatch):
        """The redaction chain is linear in its input, and a hostile peer can put
        megabytes behind a 2 KiB field, so the work per field is bounded -- but not
        by cutting the raw text. A cut can split a credential so the detector no
        longer sees its shape, and the chain strips hidden Unicode AFTER the cut, so
        zero-width padding can push a secret's delimiter past any fixed cut and then
        vanish, leaving the secret whole inside the clamp. The field is judged whole
        or refused whole: over ``_FIELD_MAX_RAW_CHARS`` it becomes the placeholder
        and the chain never runs; under it the chain sees every character."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.dashboard import handlers_instances_crew_reads as crew_reads
        from kiro_crew.dashboard.handlers_instances_gate import _PEER_FIELD_MAX_CHARS

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        seen: list[str] = []
        real_cap = handlers._cap_str

        def _spy(value, limit):
            seen.append(value)
            return real_cap(value, limit)

        monkeypatch.setattr(handlers, "_cap_str", _spy)  # through the facade, as above
        cap = crew_reads._FIELD_MAX_RAW_CHARS
        assert cap > _PEER_FIELD_MAX_CHARS
        # The attack the old pre-cut admitted: zero-width padding so the URI's
        # "user:secret" prefix ends exactly at the cut and the "@" lands past it.
        secret = "hunter2secretvalue"
        uri = f"postgres://alice:{secret}@db.internal/x"
        pad = "\u200b" * (cap - len(f"postgres://alice:{secret}"))
        padded = pad + uri
        assert len(padded) > cap and padded[cap] == "@"
        # The pin that a cut is NOT a safe alternative: the chain, handed the field
        # cut at the cap, strips the padding and leaves the secret whole.
        assert secret in real_cap(padded[:cap], _PEER_FIELD_MAX_CHARS)
        body = json.dumps(
            {
                "sessions": [
                    {"key": "s1", "title": padded},
                    {"key": "s2", "title": "t" * (cap * 8)},
                    {"key": "s3", "title": uri},
                ]
            }
        ).encode()
        state = _State(reg, self._mgr_with(list_body=body))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert r.status == 200
        rows = {row["key"]: row["title"] for row in _body(r)["sessions"]}
        assert secret not in r.text, "a split credential survived into the reply"
        assert rows["s1"] == crew_reads._FIELD_TOO_LONG
        assert rows["s2"] == crew_reads._FIELD_TOO_LONG
        assert secret not in rows["s3"], "the whole URI is redacted by the chain"
        assert all(len(v) <= cap for v in seen), "the chain was handed a cut field"
        assert not any(len(v) > cap for v in seen), "an over-long field reached the chain"

    @pytest.mark.parametrize("leg", ["read", "list", "search"])
    def test_an_interrupted_peer_body_is_a_coded_audited_failure(self, tmp_path, monkeypatch, leg):
        """A peer that answers 2xx and headers, then drops mid-body, is a peer-side
        fault. The carrier wraps only the phase before the response is yielded, so
        the body read's ``ClientPayloadError`` would otherwise escape every handler
        as an unaudited 500. It is reported as ``proxy_peer_unreachable`` (502),
        audited as a failure, and the partial body is never decoded."""
        import contextlib

        from aiohttp import ClientPayloadError
        from instances_crew_reads_helpers import _FakeResp

        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.history import INCOGNITO_EXCLUDED_HEADER

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")

        class _Cut:
            """A body that yields a valid-looking prefix, then dies."""

            async def iter_chunked(self, _n):
                yield b'{"sessions": [{"key": "k1", "title": "partial"'
                raise ClientPayloadError("Response payload is not completed")

        class FakeMgr:
            @contextlib.asynccontextmanager
            async def proxy_request(self, iid, method, path, *, params=None, **_k):
                resp = _FakeResp(200, b"", {INCOGNITO_EXCLUDED_HEADER: "1"})
                resp.content = _Cut()
                yield resp

        audited: list[tuple] = []
        monkeypatch.setattr(handlers, "_audit", lambda *a, **k: audited.append((a, k)))
        state = _State(reg, FakeMgr())
        call, query = {
            "read": (handlers.api_crew_sessions_read, {"crew": "cd-1", "key": "k1"}),
            "list": (handlers.api_crew_sessions_list, {"crew": "cd-1"}),
            "search": (handlers.api_crew_sessions_search, {"crew": "cd-1", "q": "needle"}),
        }[leg]
        r = asyncio.run(call(self._internal_req(state, **query)))
        assert r.status == 502
        assert _body(r)["code"] == "proxy_peer_unreachable"
        assert "partial" not in r.text, "the partial body must never be decoded or echoed"
        assert [(a[1], k.get("error")) for a, k in audited] == [
            ("failure", "proxy_peer_unreachable")
        ]

    def test_a_name_two_crews_share_is_refused_not_guessed(self, tmp_path, monkeypatch):
        """The registry de-duplicates ids, not names. A ``crew=`` value that names
        two crews must not silently read the first row's corpus: it is refused as
        ``crew_ambiguous`` and the caller is told to use an id. An exact id still
        resolves even when it collides with another crew's name, and a name only
        one crew carries still resolves."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="Dev Box", ssh_host="a-alias", instance_id="cd-a")
        reg.add(name="Dev Box", ssh_host="b-alias", instance_id="cd-b")
        reg.add(name="cd-a", ssh_host="c-alias", instance_id="cd-c")  # name == another's id
        reg.add(name="Solo", ssh_host="d-alias", instance_id="cd-d")
        mgr = self._mgr_with(list_body=b'{"sessions": []}')
        state = _State(reg, mgr)

        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="Dev Box")))
        assert r.status == 409
        assert _body(r)["code"] == "crew_ambiguous"

        seen: list[str] = []
        real_proxy = mgr.proxy_request

        def _spy(iid, *a, **k):
            seen.append(iid)
            return real_proxy(iid, *a, **k)

        mgr.proxy_request = _spy  # type: ignore[method-assign]
        for crew, expect in (("cd-a", "cd-a"), ("Solo", "cd-d")):
            r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew=crew)))
            assert r.status == 200, crew
        assert seen == ["cd-a", "cd-d"]

    def test_a_name_is_matched_through_the_selectors_own_normalization(self, tmp_path, monkeypatch):
        """The ``crew=`` value is NFC-normalized by ``sanitize_string``; a registry
        name is stored as typed. Compared through the same pipeline, a name the
        owner typed with a decomposed accent still resolves, and two names the
        pipeline folds together are ``crew_ambiguous`` rather than the first one."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        decomposed = "Caf\u0065\u0301"  # "Cafe" + COMBINING ACUTE ACCENT (NFD)
        composed = "Caf\u00e9"  # "Café" (NFC)
        reg.add(name=decomposed, ssh_host="a-alias", instance_id="cd-a")
        reg.add(name="Th\u00e9", ssh_host="b-alias", instance_id="cd-b")
        mgr = self._mgr_with(list_body=b'{"sessions": []}')
        state = _State(reg, mgr)
        seen: list[str] = []
        real_proxy = mgr.proxy_request

        def _spy(iid, *a, **k):
            seen.append(iid)
            return real_proxy(iid, *a, **k)

        mgr.proxy_request = _spy  # type: ignore[method-assign]

        # Either spelling of the one name resolves to the one crew.
        for crew in (composed, decomposed):
            r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew=crew)))
            assert r.status == 200, crew
        assert seen == ["cd-a", "cd-a"]

        # Two names that differ only in normalization are one name: ambiguous.
        reg.add(name=composed, ssh_host="c-alias", instance_id="cd-c")
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew=composed)))
        assert (r.status, _body(r)["code"]) == (409, "crew_ambiguous")

    def test_no_tunnel_manager_is_its_own_answer(self, tmp_path, monkeypatch):
        """Without a tunnel manager (instances off, or a headless gateway, which builds
        none) every leg answers ``MANAGER_UNAVAILABLE_CODE`` -- the answer every
        instances route gives in that state -- and never ``crew_unknown`` for a crew
        that IS in the registry."""
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.dashboard.handlers_instances_gate import MANAGER_UNAVAILABLE_CODE

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg, None)
        for call in (
            lambda: handlers.api_crew_sessions_search(
                self._internal_req(state, crew="cd-1", q="hello")
            ),
            lambda: handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")),
            lambda: handlers.api_crew_sessions_read(
                self._internal_req(state, crew="cd-1", key="k1")
            ),
        ):
            r = asyncio.run(call())
            assert r.status == 503
            assert _body(r)["code"] == MANAGER_UNAVAILABLE_CODE

    def test_search_drops_incognito_rows(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        payload = (
            True,
            {
                "sessions": [
                    {"key": "pub", "title": "Public"},
                    {"key": "priv", "title": "Private", "memory_mode": "incognito"},
                    {"key": "tmp", "title": "Temp", "memory_mode": "temporary"},
                ]
            },
        )
        state = _State(reg, self._mgr_with(search=payload))
        r = asyncio.run(
            handlers.api_crew_sessions_search(self._internal_req(state, crew="cd-1", q="hello"))
        )
        assert r.status == 200
        # incognito + temporary rows excluded, mirroring the local tool.
        assert [row["key"] for row in _body(r)["sessions"]] == ["pub"]

    def test_list_drops_incognito_rows(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        body = json.dumps(
            {
                "sessions": [
                    {"key": "pub", "title": "P"},
                    {"key": "priv", "title": "X", "memory_mode": "incognito"},
                ]
            }
        ).encode()
        state = _State(reg, self._mgr_with(list_body=body))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert [row["key"] for row in _body(r)["sessions"]] == ["pub"]

    def test_read_filters_to_recall_roles(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        body = json.dumps(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi"},
                {"role": "tool", "content": "toolout"},
                {"role": "assistant", "content": "hello"},
            ]
        ).encode()
        state = _State(reg, self._mgr_with(read_body=body))
        r = asyncio.run(
            handlers.api_crew_sessions_read(self._internal_req(state, crew="cd-1", key="k1"))
        )
        # system + tool rows dropped, matching get_chat_session's RECALL_ROLES.
        # (The endpoint returns raw roles; the MCP client is what title-cases.)
        assert [m["role"] for m in _body(r)["messages"]] == ["user", "assistant"]

    def test_resolve_prefers_exact_id_over_colliding_name(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        # Entry B's NAME equals entry A's ID; an exact id match must win.
        reg.add(name="alpha", ssh_host="a-alias", instance_id="cd-1")
        reg.add(name="cd-1", ssh_host="b-alias", instance_id="cd-2")
        seen = {}
        mgr = self._mgr_with(search=(True, {"sessions": [{"key": "k", "title": "t"}]}))
        orig = mgr.proxy_request

        def spy(iid, *a, **k):
            seen["iid"] = iid
            return orig(iid, *a, **k)

        mgr.proxy_request = spy
        state = _State(reg, mgr)
        r = asyncio.run(
            handlers.api_crew_sessions_search(self._internal_req(state, crew="cd-1", q="hello"))
        )
        assert r.status == 200
        assert seen["iid"] == "cd-1"  # the entry whose ID is cd-1, not the one NAMED cd-1

    def test_crew_pattern_accepts_display_names(self):
        from kiro_crew.validation import _CREW_RE

        assert _CREW_RE.match("chick")
        assert _CREW_RE.match("My Crew")  # spaces allowed (display name)
        assert _CREW_RE.match("crew-é")  # unicode allowed
        assert not _CREW_RE.match("bad\nname")  # control char rejected
        assert not _CREW_RE.match("")  # empty rejected

    def test_crew_pattern_admits_any_name_the_registry_admits(self):
        """``Instance.validate`` caps no length — it requires only a non-empty name.

        A 64-char bound here refused a crew the operator can see in the roster, so
        the selector is bounded at ``MAX_SHORT_STRING`` instead: the same limit
        ``SELECT_CREW_SCHEMA``'s own ``crew`` field already uses. The bound is a
        TRANSPORT one (the value rides a proxy query leg), not a semantic one.
        """
        from kiro_crew.validation import _CREW_RE, MAX_SHORT_STRING

        assert _CREW_RE.match("x" * 65)  # the length the old {1,64} refused
        assert _CREW_RE.match("x" * MAX_SHORT_STRING)
        assert not _CREW_RE.match("x" * (MAX_SHORT_STRING + 1))  # still bounded

    def test_the_three_read_tools_admit_the_same_crew_length_as_the_pattern(self):
        """The schema ``max_len`` and the pattern must agree.

        Two bounds on one field is how a value passes the regex and is then refused
        by the field cap with a different message — so this pins them together
        rather than trusting they were edited in step.
        """
        from kiro_crew.validation import (
            GET_CHAT_SESSION_SCHEMA,
            LIST_SESSIONS_SCHEMA,
            MAX_SHORT_STRING,
            SEARCH_CHAT_HISTORY_SCHEMA,
        )

        for schema in (SEARCH_CHAT_HISTORY_SCHEMA, LIST_SESSIONS_SCHEMA, GET_CHAT_SESSION_SCHEMA):
            crew = next(f for f in schema.fields if f.name == "crew")
            assert crew.max_len == MAX_SHORT_STRING, schema.tool_name

    def test_handlers_do_not_truncate_a_name_the_schema_admits(self, tmp_path, monkeypatch):
        """A schema bound the HANDLER does not share is not a bound, it is a trap.

        Raising ``max_len`` alone left each handler clamping the query value at 64
        characters, so a 65-character name passed validation, was silently cut
        server-side, and then matched no registry entry — surfacing as
        ``crew_unknown`` for a crew the operator can see in the roster. Driven
        through the handler rather than asserted against the constant: the earlier
        schema-only test passed while this defect was live.
        """
        from kiro_crew.dashboard import handlers_instances as handlers
        from kiro_crew.validation import MAX_SHORT_STRING

        _enable(tmp_path, monkeypatch)
        long_name = "c" * 65
        reg = self._reg(tmp_path)
        reg.add(name=long_name, ssh_host="cd-1-alias", instance_id="cd-1")
        seen = {}
        mgr = self._mgr_with(search=(True, {"sessions": [{"key": "k", "title": "t"}]}))
        orig = mgr.proxy_request

        def spy(iid, *a, **k):
            seen["iid"] = iid
            return orig(iid, *a, **k)

        mgr.proxy_request = spy
        state = _State(reg, mgr)
        r = asyncio.run(
            handlers.api_crew_sessions_search(self._internal_req(state, crew=long_name, q="hello"))
        )
        assert r.status == 200, _body(r)
        assert seen["iid"] == "cd-1"
        # The clamp itself stays, at the bound the schema actually declares.
        assert MAX_SHORT_STRING > 64

    def test_read_requests_incognito_exclusion(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        mgr = self._mgr_with(read_body=json.dumps([{"role": "user", "content": "hi"}]).encode())
        state = _State(reg, mgr)
        r = asyncio.run(
            handlers.api_crew_sessions_read(self._internal_req(state, crew="cd-1", key="k1"))
        )
        assert r.status == 200
        # read enforces incognito peer-side (not just the search/list discovery filter).
        assert mgr.last_params == {"exclude_incognito": "1"}

    def test_read_refuses_a_peer_that_does_not_confirm_the_exclusion(self, tmp_path, monkeypatch):
        """An older peer is refused UNREAD, not trusted.

        A gateway from before ``exclude_incognito`` existed ignores the unknown
        parameter and answers 200 with the unfiltered transcript and no
        ``INCOGNITO_EXCLUDED_HEADER``. The hub must treat that reply as "the peer
        cannot vouch": a named ``crew_peer_too_old`` refusal, and the body never
        read -- so the hub never holds rows the peer's exclusion did not cover.
        The discovery legs are unaffected: they filter on ``memory_mode`` locally.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        mgr = self._mgr_with(
            read_body=b'[{"role": "user", "content": "would leak"}]',
            peer_confirms_exclusion=False,
        )
        state = _State(reg, mgr)
        r = asyncio.run(
            handlers.api_crew_sessions_read(self._internal_req(state, crew="cd-1", key="k1"))
        )
        assert r.status == 502
        assert _body(r)["code"] == "crew_peer_too_old"
        assert "would leak" not in r.text
        assert mgr.body_reads == 0, "the unconfirmed reply must be refused before its body is read"

        # The discovery legs are held to the same gate: their rows carry a mode
        # the hub can filter on, but a preview or snippet read on the peer is
        # content, and only the peer can read it under the transcript's lock.
        for leg, kwargs in (
            ("list", {"list_body": b'{"sessions": [{"key": "k1", "title": "t", "preview": "p"}]}'}),
            ("search", {"search": (True, {"sessions": [{"key": "k1", "snippet": "s"}]})}),
        ):
            mgr = self._mgr_with(peer_confirms_exclusion=False, **kwargs)
            state = _State(reg, mgr)
            if leg == "list":
                r = asyncio.run(
                    handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1"))
                )
            else:
                r = asyncio.run(
                    handlers.api_crew_sessions_search(
                        self._internal_req(state, crew="cd-1", q="hello")
                    )
                )
            assert r.status == 502, leg
            assert _body(r)["code"] == "crew_peer_too_old", leg
            assert "k1" not in r.text, leg

    def test_the_peer_route_confirms_the_exclusion_on_every_guarded_answer(self):
        """The peer half of the capability gate: ``api_session_detail`` sets
        ``INCOGNITO_EXCLUDED_HEADER`` on both answers of the guarded arm -- the rows
        and the ``[]`` refusal -- and NOT on the plain click-through, which never
        asked for the exclusion and must not claim it.
        """
        from kiro_crew.dashboard.handlers import sessions as sessions_handlers
        from kiro_crew.history import INCOGNITO_EXCLUDED_HEADER, TranscriptWithheld

        class _Log:
            def __init__(self, exc=None):
                self._exc = exc

            def derive_messages(self, key):
                if self._exc is not None:
                    raise self._exc
                return [{"role": "user", "content": "hi"}]

            def read_messages(self, key):
                return [{"role": "user", "content": "plain"}]

        class _St:
            owner_id = "owner"

            def __init__(self, log):
                self.conversation_log = log

        guarded = _FakeReq(_St(_Log()), match={"key": "k1"}, query={"exclude_incognito": "1"})
        r = asyncio.run(sessions_handlers.api_session_detail(guarded))
        assert r.status == 200
        assert r.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"

        withheld = _FakeReq(
            _St(_Log(TranscriptWithheld("restricted"))),
            match={"key": "k1"},
            query={"exclude_incognito": "1"},
        )
        r = asyncio.run(sessions_handlers.api_session_detail(withheld))
        assert r.status == 200
        assert json.loads(r.text) == []
        assert r.headers.get(INCOGNITO_EXCLUDED_HEADER) == "1"

        plain = _FakeReq(_St(_Log()), match={"key": "k1"}, query={})
        r = asyncio.run(sessions_handlers.api_session_detail(plain))
        assert r.status == 200
        assert INCOGNITO_EXCLUDED_HEADER not in r.headers

    def test_the_incognito_metadata_read_leaves_the_event_loop(self):
        """``exclude_incognito`` must not validate and read the transcript inline.

        This is the arm the crew read turns on, and it is the ONE path a remote
        agent can reach. ``derive_messages`` stats the file, parses its first line
        and reads the rows under one lock; the adjacent plain ``read_messages`` call
        was already wrapped, so an inline seam read stalled every other request for
        the duration.

        Asserted on the THREAD rather than by spying on ``asyncio.to_thread`` so it
        pins the property (the read is off the loop) rather than the mechanism.
        """
        import threading

        from kiro_crew.dashboard.handlers import sessions as sessions_handlers

        seen: dict[str, str] = {}

        class _Log:
            def derive_messages(self, key):
                seen["derive"] = threading.current_thread().name
                return [{"role": "user", "content": "hi"}]

            def read_messages(self, key):
                seen["messages"] = threading.current_thread().name
                return []

        class _St:
            # The owner's own data: the dashboard-owner guard matches the caller
            # against this, like ``_State`` does, rather than the host's implicit
            # subject set that another test module may have reconfigured.
            owner_id = "owner"
            conversation_log = _Log()

        loop_thread: dict[str, str] = {}

        async def _run(req):
            loop_thread["name"] = threading.current_thread().name
            return await sessions_handlers.api_session_detail(req)

        r = asyncio.run(
            _run(_FakeReq(_St(), match={"key": "k1"}, query={"exclude_incognito": "1"}))
        )
        assert r.status == 200
        assert json.loads(r.text) == [{"role": "user", "content": "hi"}]
        assert seen["derive"] != loop_thread["name"]
        assert "messages" not in seen, "the guarded arm must not fall through to the plain read"

        # The pre-existing wrap on the dashboard's own click-through, asserted
        # alongside so a regression on either is caught.
        r = asyncio.run(_run(_FakeReq(_St(), match={"key": "k1"}, query={})))
        assert r.status == 200
        assert seen["messages"] != loop_thread["name"]

    def test_the_incognito_exclusion_reads_through_the_derivation_seam(self):
        """The ``exclude_incognito`` arm refuses what the seam withholds, fails closed.

        A restricted line raises ``TranscriptWithheld`` from
        ``derive_messages`` and the route answers ``[]`` — the contract the crew
        read relies on. A busy lock is not a refusal: it answers 503 so the remote
        caller does not read a retryable state as an empty conversation. The plain
        ``read_messages`` must never be reached on this arm: it is the ungated
        twin, and reaching it would ship rows the line forbids.
        """
        from kiro_crew.dashboard.handlers import sessions as sessions_handlers
        from kiro_crew.history import TranscriptBusy, TranscriptWithheld

        class _Log:
            def __init__(self, exc):
                self._exc = exc
                self.plain_reads = 0

            def derive_messages(self, key):
                raise self._exc

            def read_messages(self, key):
                self.plain_reads += 1
                return [{"role": "user", "content": "leaked"}]

        class _St:
            owner_id = "owner"

            def __init__(self, log):
                self.conversation_log = log

        withheld = _Log(TranscriptWithheld("restricted"))
        req = _FakeReq(_St(withheld), match={"key": "k1"}, query={"exclude_incognito": "1"})
        r = asyncio.run(sessions_handlers.api_session_detail(req))
        assert r.status == 200
        assert json.loads(r.text) == []
        assert withheld.plain_reads == 0

        busy = _Log(TranscriptBusy("lock"))
        req = _FakeReq(_St(busy), match={"key": "k1"}, query={"exclude_incognito": "1"})
        r = asyncio.run(sessions_handlers.api_session_detail(req))
        assert r.status == 503
        assert json.loads(r.text)["code"] == "transcript_busy"
        assert busy.plain_reads == 0

    def test_a_non_string_memory_mode_from_a_peer_is_dropped_not_a_500(self, tmp_path, monkeypatch):
        """A non-string ``memory_mode`` must not reach the frozenset membership test.

        ``INCOGNITO_MEMORY_MODES`` is a ``frozenset``, so an unhashable value like
        ``[]`` raises ``TypeError`` against it — a 500 rather than a refusal, on a
        payload where only ``key`` is type-checked. The row is dropped (fail closed)
        and the sibling still lists, which is what proves the guard is a filter and
        not a bail-out.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        body = b'{"sessions": [{"key": "hostile", "memory_mode": []}, {"key": "ordinary"}]}'
        state = _State(reg, self._mgr_with(list_body=body))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert r.status == 200
        keys = [row["key"] for row in _body(r)["sessions"]]
        assert "hostile" not in keys
        assert "ordinary" in keys

    def test_a_row_with_no_memory_mode_is_still_listed(self, tmp_path, monkeypatch):
        """An ABSENT mode is the ordinary case, not a restricted one.

        Most rows carry no ``memory_mode`` at all, so a type guard that treated a
        missing mode as restricted would drop nearly every legitimate row — the
        opposite failure from the crash it fixes.
        """
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        state = _State(reg, self._mgr_with(list_body=b'{"sessions": [{"key": "plain"}]}'))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert r.status == 200
        assert [row["key"] for row in _body(r)["sessions"]] == ["plain"]

    def test_a_string_incognito_mode_is_still_excluded(self, tmp_path, monkeypatch):
        """The membership test itself must survive the type guard."""
        from kiro_crew.dashboard import handlers_instances as handlers

        _enable(tmp_path, monkeypatch)
        reg = self._reg(tmp_path)
        reg.add(name="CD", ssh_host="cd-1-alias", instance_id="cd-1")
        body = (
            b'{"sessions": [{"key": "secret", "memory_mode": "incognito"},'
            b' {"key": "Secret", "memory_mode": "Incognito"},'
            b' {"key": "tmp", "memory_mode": "TEMPORARY"}, {"key": "ok"}]}'
        )
        state = _State(reg, self._mgr_with(list_body=body))
        r = asyncio.run(handlers.api_crew_sessions_list(self._internal_req(state, crew="cd-1")))
        assert r.status == 200
        # The canonical predicate normalizes case: a peer is untrusted input, so a
        # header spelled with capitals must refuse like the lowercase one does.
        assert [row["key"] for row in _body(r)["sessions"]] == ["ok"]
