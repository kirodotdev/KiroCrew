"""``GET /api/instances/{id}/chat-slots`` — a peer's live sessions.

The merged-sessions sidebar renders a connected peer's OPEN sessions as ordinary
rows in this machine's Sessions list. A local relay archive (an old
``executor == "remote"`` chat) hides no peer row behind it: the
archive is read-only, and the peer's own row is where that chat goes on. These
tests pin that, and the untrusted-input discipline every peer read in this
module shares: bound before decoding, refuse rather than reshape.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import aiohttp
import pytest

from kiro_crew.dashboard import handlers_instances as hi
from kiro_crew.dashboard.slot_projection import resolved_row_identity
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, _ChatSlot
from kiro_crew.instances.constants import PEER_SLOTS_REPLY_MAX_BYTES
from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError


class _Req:
    """Request stub mirroring aiohttp's mapping surface.

    ``user`` is "local-app" because the handler's owner gate is POSITIVE
    (``is_owner_dashboard_request``): with no configured owner_id only the local
    dashboard subjects pass, so a bare truthy user would fail the second gate.
    """

    def __init__(self, state, instance_id, identity):
        self.app = {"state": state}
        self.match_info = {"id": instance_id}
        self.headers: dict[str, str] = {}
        self.query: dict[str, str] = {}
        self._attrs = identity

    def get(self, key, default=""):
        return self._attrs.get(key, default)

    def __contains__(self, key):
        return key in self._attrs

    def __getitem__(self, key):
        return self._attrs[key]


def _request(state, instance_id="nobita", app="", user="local-app"):
    return _Req(state, instance_id, {"user": user, "app": app})


def _enable_instances(monkeypatch):
    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )


class _Content:
    """The ``resp.content`` half of an aiohttp response, with its REAL semantics.

    The distinction this models is the one an earlier revision of this file got
    wrong. ``StreamReader.read(n)`` does NOT return ``n`` bytes: it returns as
    soon as ANY buffered data exists, so on a body that arrives in several wire
    chunks it hands back a PREFIX — a JSON document cut mid-structure. A stub
    whose ``read(n)`` returned ``body[:n]`` was therefore stubbing away the exact
    failure the handler had to survive, and every case here passed over it.

    So ``read(n)`` here never returns more than one wire chunk however large
    ``n`` is, and ``iter_chunked`` is the accumulate-to-EOF path the handler
    actually uses. A body above ``_WIRE_CHUNK`` now DISTINGUISHES the two reads
    instead of behaving identically under both.
    """

    #: One buffered chunk. Matches the handler's own ``iter_chunked`` block size,
    #: which is what aiohttp would hand back per loop iteration.
    _WIRE_CHUNK = 65536

    def __init__(
        self, body: bytes, *, interrupt: Exception | None = None, interrupt_after: int = -1
    ) -> None:
        self._body = body
        self._interrupt = interrupt
        #: Chunks handed over before *interrupt* is raised; -1 means all of them,
        #: which is aiohttp's own shape for a body that ends short of its declared
        #: length — the bytes that did arrive are yielded first and the error comes
        #: at what should have been EOF.
        self._interrupt_after = interrupt_after

    async def read(self, n: int) -> bytes:
        return self._body[: min(n, self._WIRE_CHUNK)]

    async def iter_chunked(self, size: int):
        handed = 0
        for start in range(0, len(self._body), size):
            if self._interrupt is not None and handed == self._interrupt_after:
                raise self._interrupt
            yield self._body[start : start + size]
            handed += 1
        if self._interrupt is not None:
            raise self._interrupt


class _Upstream:
    def __init__(self, status: int, content: _Content) -> None:
        self.status = status
        self.content = content


def _manager(
    *,
    status: int = 200,
    body: bytes = b"[]",
    raises: Exception | None = None,
    interrupt: Exception | None = None,
    interrupt_after: int = -1,
):
    """A manager whose ``proxy_request`` is an async CM, as the real one is.

    Records each call so a test can assert the handler asked for the peer's own
    slots list and nothing else — the path is a literal in the handler, and a
    handler that forwarded a caller-supplied one would be a different (and much
    wider) route.

    ``raises`` is the CARRIER failing BEFORE it yields a response, which is the
    only failure ``proxy_request`` converts into a ``ProxyRequestError``.
    ``interrupt`` is the BODY failing after it has, which the carrier does not
    wrap at all — two different exception paths reaching the same handler.
    """
    calls: list[tuple[str, str, str]] = []

    @contextlib.asynccontextmanager
    async def _proxy_request(instance_id, method, path, **_kw):
        calls.append((instance_id, method, path))
        if raises is not None:
            raise raises
        yield _Upstream(
            status, _Content(body, interrupt=interrupt, interrupt_after=interrupt_after)
        )

    return SimpleNamespace(proxy_request=_proxy_request, calls=calls)


def _state(mgr, slots: dict[str, _ChatSlot] | None = None):
    return SimpleNamespace(instances_manager=mgr, _slots=slots or {})


def _remote_slot(key: str = "chat-1", *, instance_id: str = "nobita", remote: str = "peer-chat-9"):
    """A LOCAL relay archive: an old slot whose turns a peer once ran."""
    slot = _ChatSlot(key)
    slot.executor = "remote"
    slot.instance_id = instance_id
    slot.remote_slot = remote
    return slot


def _rows(*keys: str) -> bytes:
    return json.dumps([{"key": k, "title": k.upper()} for k in keys]).encode()


async def _body(resp):
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
class TestRelayArchivesHideNoPeerRow:
    """The peer's own row for a relay archive's chat stays on the list."""

    async def test_the_peer_row_behind_a_relay_archive_is_listed(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _manager(body=_rows("peer-chat-9", "peer-chat-3"))
        state = _state(mgr, {"chat-1": _remote_slot()})

        resp = await hi.api_instances_chat_slots(_request(state))

        assert resp.status == 200
        assert [row["key"] for row in await _body(resp)] == ["peer-chat-9", "peer-chat-3"]

    async def test_a_row_with_no_usable_key_is_kept_not_dropped(self, monkeypatch):
        """Shaping is not validation.

        The sidebar hook already rejects a row whose ``key`` is not a string, so it
        is shaped here with no ``key``, because ``8`` is not a peer string and the
        allowlist emits only what it can vouch for.

        ``"nonsense"`` is not a dict, so it was never a row at all and is the one
        thing the shaping pass does drop.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(body=json.dumps([{"key": 8}, "nonsense", {"key": "peer-chat-9"}]).encode())
        state = _state(mgr, {"chat-1": _remote_slot()})

        data = await _body(await hi.api_instances_chat_slots(_request(state)))

        assert data == [
            {"running": False, "pending_approval": False},
            {
                "key": "peer-chat-9",
                "running": False,
                "pending_approval": False,
                "row_identity": "nobita:peer-chat-9",
            },
        ]

    async def test_the_peer_path_is_a_literal_and_the_method_is_a_read(self, monkeypatch):
        """Nothing caller-supplied reaches the peer, and nothing mutates."""
        _enable_instances(monkeypatch)
        mgr = _manager(body=b"[]")

        await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert mgr.calls == [("nobita", "GET", "api/chat/slots")]


@pytest.mark.asyncio
class TestPeerTextIsRedactedAndAllowlisted:
    """A peer slot title is model-authored text that lands in this machine's list.

    ``slot_projection.py`` redacts a LOCAL slot's ``display_title`` before the
    browser sees it. These rows are merged into that same list, so forwarding them
    as the peer sent them would have left exactly one kind of row in it whose text
    never met a redactor — and the peer is untrusted by this module's own design
    (see the handler's docstring on its reply being "a claim rather than a
    guarantee").

    Assertions are on the PROPERTY (the secret is gone, the row still renders)
    rather than on the redactor's placeholder text, which belongs to
    ``peer_redaction`` and is free to change without this route regressing.
    """

    async def test_a_credential_shaped_title_never_reaches_the_browser(self, monkeypatch):
        """The finding this pass exists for.

        A peer answering with an access-key id in a session title would have had
        it rendered verbatim in the sidebar, and the local slot beside it would
        have been redacted — the same string treated two ways in one list.
        """
        _enable_instances(monkeypatch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        mgr = _manager(body=json.dumps([{"key": "p1", "title": f"deploy {secret}"}]).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert len(data) == 1
        assert secret not in data[0]["title"]

    async def test_redaction_runs_before_the_clamp(self, monkeypatch):
        """``_cap_str`` scrubs and THEN truncates, and the order is load-bearing.

        The credential is positioned to STRADDLE the 512-char clamp, which is the
        only place the two orders give different answers. Burying it entirely past
        the limit would prove nothing: truncation alone deletes it, so the test
        would pass with redaction removed (it did, on the first draft of this
        file). Straddling means clamp-first leaves a PREFIX of the key behind —
        too short for the credential regex to match on a later pass, and still a
        disclosure — while redact-first removes the whole thing before any
        truncation happens.
        """
        _enable_instances(monkeypatch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        title = "x" * (512 - len(secret) // 2) + secret
        mgr = _manager(body=json.dumps([{"key": "p1", "title": title}]).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert secret[: len(secret) // 2] not in data[0]["title"]
        assert len(data[0]["title"]) <= 512

    async def test_only_the_fields_the_sidebar_reads_are_forwarded(self, monkeypatch):
        """An allowlist, not a passthrough.

        The peer's own projection carries a message PREVIEW, the pending tool's
        input, option labels, source links, todo and MCP payloads. None of it is
        rendered by a peer row, so forwarding it hands the browser peer-authored
        text that no local code path reads — and lets a peer on another build put
        content into a row through a key this gateway has never heard of.
        """
        _enable_instances(monkeypatch)
        row = {
            "key": "p1",
            "title": "Refactor",
            "agent": "claude",
            "running": True,
            "pending_approval": False,
            "last_turn_ts": "2026-01-01T00:00:00Z",
            "last_ts": "2026-01-01T00:00:01Z",
            "created": "2025-12-31T00:00:00Z",
            # Everything below is real `slot_projection` output the sidebar never
            # reads off a PEER row.
            "last_message": "here is the token I found",
            "pending_approval_info": {"tool_input": "cat ~/.aws/credentials"},
            "options": ["yes", "no"],
            "workspace": "/Users/someone/secret-project",
            "source_links": [{"url": "https://internal.example"}],
            "messages": 42,
            "unknown_future_field": "surprise",
            # A citation is forwarded, but only its two keys: a peer cannot ride
            # extra content in on the parent object either.
            "parent": {"slot": "p0", "key": "p0", "title": "smuggled", "note": "x"},
        }
        mgr = _manager(body=json.dumps([row]).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert set(data[0]) == {
            "key",
            "title",
            "agent",
            "running",
            "pending_approval",
            "last_turn_ts",
            "last_ts",
            "created",
            "parent",
            # Not forwarded FROM the peer — stamped BY this hub after the
            # allowlist runs. A peer cannot influence it: the value is composed
            # from the instance id this route was called with plus the row's own
            # allowlisted `key`, so a peer sending its own `row_identity` has it
            # overwritten rather than honoured.
            "row_identity",
        }
        assert data[0]["parent"] == {"slot": "p0", "key": "p0"}

    async def test_every_field_the_sidebar_reads_survives(self, monkeypatch):
        """The allowlist must not cost the feature its rows.

        Pinned as a whole-dict equality so dropping a field from
        ``_PEER_SLOT_STR_FIELDS`` fails here rather than silently emptying a
        column of the sessions list.
        """
        _enable_instances(monkeypatch)
        row = {
            "key": "p1",
            "title": "Refactor the sidebar",
            "agent": "claude",
            "running": True,
            "pending_approval": True,
            "last_turn_ts": "2026-01-01T00:00:00Z",
            "last_ts": "2026-01-01T00:00:01Z",
            "created": "2025-12-31T00:00:00Z",
            "parent": {"slot": "p0", "key": "p0"},
            "lineage_pending": True,
        }
        mgr = _manager(body=json.dumps([row]).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        # Still a whole-dict equality, so dropping an allowlisted field fails here.
        # `row_identity` is added by this route rather than forwarded, so it is
        # named explicitly instead of loosening the comparison to a subset check.
        assert data == [{**row, "row_identity": "nobita:p1"}]

    async def test_a_peer_rows_creator_citation_reaches_the_lane(self, monkeypatch):
        """The conductor lane nests a peer row on ``parent.key`` -- the same field a
        local row carries. With the citation stripped at this hop, every session a
        peer's conductor opened rendered at the top level of this dashboard as a
        stray, while the peer's own sidebar nested them.

        The key is forwarded as the peer spelled it: the lane resolves a citation
        within the row's own origin, so rewriting it to ``<instance>:<key>`` here
        would make it resolve nothing.
        """
        _enable_instances(monkeypatch)
        rows = [
            {"key": "lead", "title": "Lead"},
            {"key": "w1", "title": "Worker", "parent": {"slot": "lead", "key": "lead"}},
        ]
        mgr = _manager(body=json.dumps(rows).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        by_key = {r["key"]: r for r in data}
        assert by_key["w1"]["parent"] == {"slot": "lead", "key": "lead"}
        # No citation stays ABSENT, as on a local row with ``parent: None`` -- not
        # an empty object the lane would have to special-case.
        assert "parent" not in by_key["lead"]
        assert "lineage_pending" not in by_key["lead"]

    async def test_a_citation_with_neither_half_a_string_is_no_citation(self, monkeypatch):
        """Both halves are dereferenced by the lane on every frame, so a peer
        answering ``"parent": "lead"`` or ``{"key": {}}`` must not reach it. Such a
        row is forwarded WITHOUT a parent, not dropped: the session is real, only
        its citation is unusable."""
        _enable_instances(monkeypatch)
        rows = [
            {"key": "a", "parent": "lead"},
            {"key": "b", "parent": {"key": {"nested": 1}}},
            {"key": "c", "parent": {"slot": 7, "key": None}},
            {"key": "d", "parent": None},
            {"key": "e", "parent": ["lead"]},
            {"key": "f", "parent": {}},
        ]
        mgr = _manager(body=json.dumps(rows).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert [r["key"] for r in data] == ["a", "b", "c", "d", "e", "f"]
        assert all("parent" not in r for r in data)

    async def test_the_orphan_citation_keeps_slot_when_key_is_gone(self, monkeypatch):
        """``lineage_parents`` sends ``{"slot": <creator>, "key": null}`` for a
        row whose creator has closed. That half feeds the lane's "opened by"
        glyph and its move baseline, so it must cross with ``key`` omitted --
        neither dropped as malformed nor padded with an empty ``key``."""
        _enable_instances(monkeypatch)
        rows = [{"key": "w", "parent": {"slot": "gone", "key": None}}]
        mgr = _manager(body=json.dumps(rows).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert data[0]["parent"] == {"slot": "gone"}

    def test_a_peer_spelled_hub_key_is_not_forwarded(self):
        """Only the two allowlisted halves of a citation cross."""
        assert hi._clean_peer_parent({"slot": "a", "key": "a", "hub_key": "chat-1"}) == {
            "slot": "a",
            "key": "a",
        }

    async def test_lineage_pending_is_forwarded_only_when_literally_true(self, monkeypatch):
        """The lane tests PRESENCE to skip a provisional frame when it records what
        the user has opened. ``"yes"`` or ``1`` must not read as a settled flag, and
        ``False`` must not appear at all -- the local payload omits it too."""
        _enable_instances(monkeypatch)
        rows = [
            {"key": "a", "lineage_pending": True},
            {"key": "b", "lineage_pending": "yes"},
            {"key": "c", "lineage_pending": 1},
            {"key": "d", "lineage_pending": False},
        ]
        mgr = _manager(body=json.dumps(rows).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        by_key = {r["key"]: r for r in data}
        assert by_key["a"]["lineage_pending"] is True
        for k in ("b", "c", "d"):
            assert "lineage_pending" not in by_key[k]

    async def test_a_citation_key_is_redacted_and_clamped_like_a_key(self, monkeypatch):
        """``parent.key`` goes through the same sink as ``key``: a credential-shaped
        citation never reaches the browser, and an oversized one is clamped."""
        _enable_instances(monkeypatch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        rows = [
            {"key": "a", "parent": {"key": f"lead {secret}"}},
            {"key": "b", "parent": {"key": "x" * (hi._PEER_FIELD_MAX_CHARS * 3)}},
        ]
        mgr = _manager(body=json.dumps(rows).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        by_key = {r["key"]: r for r in data}
        assert secret not in json.dumps(data)
        assert by_key["a"]["parent"]["key"].startswith("lead ")
        assert len(by_key["b"]["parent"]["key"]) == hi._PEER_FIELD_MAX_CHARS

    async def test_an_absent_title_stays_absent_rather_than_becoming_blank(self, monkeypatch):
        """``""`` and "no title" are different rows.

        The sidebar falls back to a placeholder for a row with no title; sending
        an empty string instead would render a blank label and look like a bug in
        the peer.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(body=json.dumps([{"key": "p1"}]).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert "title" not in data[0]

    async def test_a_non_string_title_is_dropped_not_forwarded(self, monkeypatch):
        """A peer's declared JSON types are a claim, not a guarantee.

        An object reaching a row is rendered as a React child, which throws and
        takes the whole sidebar down. The hook guards this too; the point of
        doing it here is that the wire never carries the shape at all.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(body=json.dumps([{"key": "p1", "title": {"nested": "x"}}]).encode())

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert "title" not in data[0]
        assert data[0]["key"] == "p1"

    async def test_a_truthy_non_boolean_raises_no_badge_it_never_claimed(self, monkeypatch):
        """``running`` and ``pending_approval`` are coerced, not truth-tested.

        A peer answering ``"running": "no"`` would otherwise spin a row's
        activity indicator, and a string in ``pending_approval`` would raise an
        approval badge for a decision nobody is waiting on.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(
            body=json.dumps([{"key": "p1", "running": "no", "pending_approval": 1}]).encode()
        )

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert data[0]["running"] is False
        assert data[0]["pending_approval"] is False


@pytest.mark.asyncio
class TestUntrustedPeerReply:
    """A peer's reply is input, so it is bounded and then refused, not reshaped."""

    async def test_a_non_2xx_from_the_peer_is_a_502(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _manager(status=403, body=b"nope")

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_refused"

    async def test_a_body_over_the_cap_is_refused_rather_than_truncated(self, monkeypatch):
        """Truncating would hand ``json.loads`` a half object and read as malformed.

        The cap is enforced while accumulating, so the read STOPS the moment it is
        crossed rather than buffering however much more the peer meant to send.
        """
        _enable_instances(monkeypatch)
        oversized = b"[" + b'{"key":"x"},' * 4 + b"]"
        monkeypatch.setattr(hi, "PEER_SLOTS_REPLY_MAX_BYTES", 8)
        mgr = _manager(body=oversized)

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_too_large"

    async def test_a_reply_exactly_at_the_cap_is_accepted(self, monkeypatch):
        """The cap is a ceiling, not an off-by-one exclusion."""
        _enable_instances(monkeypatch)
        body = b'[{"key":"x"}]'
        monkeypatch.setattr(hi, "PEER_SLOTS_REPLY_MAX_BYTES", len(body))
        mgr = _manager(body=body)

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 200
        assert [row["key"] for row in await _body(resp)] == ["x"]

    async def test_a_multi_chunk_reply_is_read_whole_not_to_the_first_chunk(self, monkeypatch):
        """The failing case here is the HEALTHY peer, not the hostile one.

        ``StreamReader.read(n)`` returns as soon as any buffered data exists, so a
        single call yields a prefix of a body that arrived in several wire chunks.
        A peer with enough open sessions to cross one chunk would therefore have
        had its perfectly valid JSON cut mid-document and been reported
        ``peer_slots_malformed`` — a busy crew's sessions silently missing from
        the sidebar, with a diagnosis pointing at the peer.

        Deliberately far UNDER the cap: this is about reading to EOF, not about
        the bound. Both properties are asserted, because a handler that fixed the
        truncation by dropping the cap would pass on row count alone.
        """
        _enable_instances(monkeypatch)
        rows = [{"key": f"peer-{i}", "title": "t" * 200} for i in range(500)]
        body = json.dumps(rows).encode()
        assert len(body) > _Content._WIRE_CHUNK, "body must span >1 chunk or this proves nothing"
        assert len(body) < PEER_SLOTS_REPLY_MAX_BYTES, "and must sit under the cap"
        mgr = _manager(body=body)

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 200
        payload = await _body(resp)
        assert [row["key"] for row in payload] == [f"peer-{i}" for i in range(500)]

    async def test_the_production_cap_is_generous_enough_for_an_honest_peer(self):
        """A real gateway's list must never trip it — see the constant's comment."""
        assert PEER_SLOTS_REPLY_MAX_BYTES >= 1024 * 1024

    async def test_undecodable_json_is_a_502_not_a_500(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _manager(body=b"<html>gateway error</html>")

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_malformed"

    async def test_a_json_object_where_a_list_belongs_is_a_502(self, monkeypatch):
        """``api_chat_slots`` answers a BARE list; anything else is not that peer.

        Left unchecked, a dict would iterate as its KEYS and the sidebar would
        render a row per field name.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(body=b'{"slots": [{"key": "x"}]}')

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_malformed"

    async def test_deeply_nested_json_is_a_502_not_a_crash(self, monkeypatch):
        """A peer is untrusted input, and `RecursionError` is not a `ValueError`.

        `json.loads` recurses once per nesting level, so a document that is only a
        few KB -- far under the byte cap -- can exhaust the interpreter's stack. Left
        uncaught it escapes as an HTTP 500 from a route whose every other unreadable
        reply is a 502, which turns a peer's malformed answer into this gateway
        looking broken.
        """
        _enable_instances(monkeypatch)
        # A few KB of pure nesting: cheap to send, deep enough to exhaust the stack.
        deep = b"[" * 100_000 + b"]" * 100_000
        mgr = _manager(body=deep)

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_malformed"

    async def test_a_relay_archive_never_shares_the_peer_rows_identity(self, monkeypatch):
        """The archive and the peer row both render now, so their sidebar keys
        must differ: the archive projects its own local key."""
        _enable_instances(monkeypatch)
        mgr = _manager(body=b'[{"key": "peer-chat-9", "title": "T"}]')

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))
        rows = await _body(resp)

        assert resp.status == 200
        archive = _remote_slot("chat-local-1")
        assert resolved_row_identity(archive) == "chat-local-1"
        assert rows[0]["row_identity"] != resolved_row_identity(archive)

    async def test_an_empty_list_is_a_200_with_no_rows(self, monkeypatch):
        """A peer with nothing open is not an error, and must not read as one."""
        _enable_instances(monkeypatch)

        resp = await hi.api_instances_chat_slots(_request(_state(_manager(body=b"[]"))))

        assert resp.status == 200
        assert await _body(resp) == []


@pytest.mark.asyncio
class TestAnInterruptedReplyIsRefusedNotDecoded:
    """A body that stops partway is a peer-side fault, and must not read as a bug.

    ``proxy_request`` wraps only the phase BEFORE it yields a response — the
    connect, the credential, the 401 retry. Once it has yielded, anything raised
    while the body is being read travels straight out of the handler, so an
    interrupted stream answered 500: an unhandled exception, no ``code`` for the
    sidebar's banner to name, and a fault on the other machine presented as a
    fault in this one.
    """

    async def test_a_body_that_ends_short_is_a_502_not_a_500(self, monkeypatch):
        """aiohttp's own shape: the bytes that arrived, then the error at EOF.

        ``ClientPayloadError`` is what a chunked reply cut short of its declared
        length raises, and it arrives AFTER the partial body has been handed over
        — so the handler is holding bytes and must still refuse them.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(body=_rows("peer-1", "peer-2"), interrupt=aiohttp.ClientPayloadError())

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_interrupted"

    async def test_a_tunnel_dropping_before_any_chunk_is_a_502_not_a_500(self, monkeypatch):
        """The other end of the range: nothing arrived at all.

        ``interrupt_after=0`` raises before the first chunk, which is the forward
        dying between the response headers and the body — the handler has an
        EMPTY buffer, which ``json.loads`` would report as ``peer_slots_malformed``
        and blame on the peer's JSON rather than on the connection.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(
            body=_rows("peer-1"),
            interrupt=aiohttp.ClientConnectionError("forward closed"),
            interrupt_after=0,
        )

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_interrupted"

    async def test_the_read_idle_timeout_expiring_is_a_502_not_a_500(self, monkeypatch):
        """``TimeoutError`` is not in aiohttp's exception tree.

        The carrier sets ``sock_read`` on its timeout, so a peer that stops
        sending mid-body surfaces as a bare ``TimeoutError``. Pinned separately
        because a handler that caught only ``aiohttp.ClientError`` would pass both
        cases above and still answer 500 for the commonest interruption of all —
        a tunnel that goes quiet rather than closing.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(body=_rows("peer-1"), interrupt=TimeoutError())

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "peer_slots_interrupted"

    async def test_the_partial_body_is_never_echoed_back(self, monkeypatch):
        """What arrived is untrusted peer text that never reached a redactor.

        The re-shaping pass is downstream of this refusal, so a handler that put
        the partial body into its error message — the reflex when diagnosing a
        truncated read — would forward peer bytes to the browser with none of the
        allowlisting or redaction the success path applies.
        """
        _enable_instances(monkeypatch)
        secret = "AKIAIOSFODNN7EXAMPLE"
        body = json.dumps([{"key": "p1", "title": f"deploy {secret}"}]).encode()
        mgr = _manager(body=body, interrupt=aiohttp.ClientPayloadError())

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert secret not in resp.body.decode()
        assert "deploy" not in resp.body.decode()

    async def test_an_interruption_after_the_cap_is_already_crossed_stays_oversized(
        self, monkeypatch
    ):
        """The bound wins over the interruption, because it is already decided.

        A hostile peer streaming an unbounded reply and then dropping the
        connection must not be able to relabel its own refusal: the read BREAKS
        at the cap, so the interruption never happens and the answer stays
        ``peer_slots_too_large``. Without the break the peer would choose which
        of the two codes the operator sees.
        """
        _enable_instances(monkeypatch)
        monkeypatch.setattr(hi, "PEER_SLOTS_REPLY_MAX_BYTES", 4)
        mgr = _manager(
            body=b'[{"key":"x"},{"key":"y"}]',
            interrupt=aiohttp.ClientPayloadError(),
        )

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert (await _body(resp))["code"] == "peer_slots_too_large"


@pytest.mark.asyncio
class TestTheRowCountIsBoundedNotOnlyTheByteCount:
    """A byte cap is not a row cap, and the expensive half is per-ROW.

    ``{"key":"x"},`` is twelve bytes, so a peer fits hundreds of thousands of
    minimal rows under the 4 MiB reply cap — and every one of them would then run
    the relay's redaction chain once per string field in ``_clean_peer_slot``. The
    byte cap bounds the hub's memory; this bounds its CPU and the size of the
    reply the browser has to render.
    """

    async def test_a_reply_past_the_row_cap_is_truncated_not_refused(self, monkeypatch):
        """Truncated, unlike the byte cap, which refuses.

        Bytes past that cap are only ever garbage, but a peer on a build with a
        higher slot ceiling is honest — refusing would cost it every row rather
        than the tail.
        """
        _enable_instances(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances.MAX_LIVE_SLOTS", 3)
        mgr = _manager(body=_rows("a", "b", "c", "d", "e"))

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 200
        assert [row["key"] for row in await _body(resp)] == ["a", "b", "c"]

    async def test_the_cap_bounds_the_redaction_work_not_just_the_reply(self, monkeypatch):
        """The load-bearing case: WHERE the slice happens, not just that it does.

        A cap applied to ``shaped`` — after the re-shaping pass — returns exactly
        the same rows and so passes the case above, while still running the
        redactor over every row the peer sent. That is the cost this bound exists
        to stop, so it is asserted directly on the call count.
        """
        _enable_instances(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances.MAX_LIVE_SLOTS", 3)
        seen: list[object] = []
        real = hi._clean_peer_slot

        def _spy(row):
            seen.append(row)
            return real(row)

        monkeypatch.setattr(hi, "_clean_peer_slot", _spy)
        mgr = _manager(body=_rows(*[f"peer-{i}" for i in range(50)]))

        await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert len(seen) == 3

    async def test_a_reply_exactly_at_the_row_cap_is_untouched(self, monkeypatch):
        """A ceiling, not an off-by-one exclusion — same rule as the byte cap."""
        _enable_instances(monkeypatch)
        monkeypatch.setattr("kiro_crew.dashboard.handlers_instances.MAX_LIVE_SLOTS", 3)
        mgr = _manager(body=_rows("a", "b", "c"))

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert [row["key"] for row in data] == ["a", "b", "c"]

    async def test_the_production_cap_is_the_peers_own_slot_ceiling(self, monkeypatch):
        """Derived, not chosen — which is why it is not ``_CAP_MAX_ROWS``.

        ``MAX_LIVE_SLOTS`` is the ceiling every allocating path on a Kiro Crew
        gateway enforces, so a peer running this software cannot honestly answer
        with more rows than that and the bound needs no judgement of its own. The
        capability reader's ``_CAP_MAX_ROWS`` is a judgement about how long a
        picker may usefully be; it happens to hold the same number today and has
        no reason to move when the slot ceiling does.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(body=_rows(*[f"peer-{i}" for i in range(MAX_LIVE_SLOTS)]))

        data = await _body(await hi.api_instances_chat_slots(_request(_state(mgr))))

        assert len(data) == MAX_LIVE_SLOTS


@pytest.mark.asyncio
class TestCarrierFailures:
    """``ProxyRequestError`` carries its own status; this route honours it."""

    async def test_a_disconnected_crew_surfaces_as_503_with_its_code(self, monkeypatch):
        """503 so the caller can distinguish "not connected yet" from "broken".

        The sidebar drops an unreachable crew's rows and names it in one line
        rather than emptying the list, so the distinction reaches the user.
        """
        _enable_instances(monkeypatch)
        mgr = _manager(
            raises=ProxyRequestError(
                "proxy_not_connected", "crew is not connected", http_status=503
            )
        )

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 503
        assert (await _body(resp))["code"] == "proxy_not_connected"

    async def test_any_other_carrier_failure_is_a_502(self, monkeypatch):
        _enable_instances(monkeypatch)
        mgr = _manager(raises=ProxyRequestError("proxy_request_failed", "tunnel died"))

        resp = await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert resp.status == 502
        assert (await _body(resp))["code"] == "proxy_request_failed"

    async def test_no_instances_manager_is_a_503(self, monkeypatch):
        """A gateway built without the feature answers "unavailable", not 500."""
        _enable_instances(monkeypatch)
        state = SimpleNamespace(instances_manager=None, _slots={})

        resp = await hi.api_instances_chat_slots(_request(state))

        assert resp.status == 503
        assert (await _body(resp))["code"] == "instances_unavailable"


@pytest.mark.asyncio
class TestAuthorization:
    """Same bar as the proxy and the capability read — this spends the owner's
    peer credential and discloses every open session title on that machine."""

    async def test_an_unauthenticated_caller_is_refused(self, monkeypatch):
        _enable_instances(monkeypatch)

        resp = await hi.api_instances_chat_slots(_request(_state(_manager()), user=""))

        assert resp.status == 401

    async def test_a_slack_origin_cannot_list_a_peers_sessions(self, monkeypatch):
        _enable_instances(monkeypatch)
        request = _request(_state(_manager()))
        request.headers["X-Session-Key"] = "slack:C123"

        resp = await hi.api_instances_chat_slots(request)

        assert resp.status == 403

    async def test_the_feature_being_disabled_refuses_the_read(self, monkeypatch):
        monkeypatch.setattr(
            hi.KiroCrewConfig,
            "load",
            staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=False))),
        )

        resp = await hi.api_instances_chat_slots(_request(_state(_manager())))

        assert resp.status == 403

    async def test_a_non_owner_dashboard_subject_is_refused(self, monkeypatch):
        """A Slack-invited user holding a `!dashboard` link is an authenticated
        subject with an empty app, so `_guard` alone would let them through."""
        _enable_instances(monkeypatch)
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers_instances.is_owner_dashboard_request",
            lambda _r: False,
        )

        resp = await hi.api_instances_chat_slots(_request(_state(_manager())))

        assert resp.status in (401, 403)

    async def test_a_refused_caller_never_reaches_the_peer(self, monkeypatch):
        """The gate is BEFORE the tunnel call, not a filter on its result.

        Otherwise a non-owner's request would still spend the owner's credential
        on the peer and appear in its logs.
        """
        _enable_instances(monkeypatch)
        mgr = _manager()
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers_instances.is_owner_dashboard_request",
            lambda _r: False,
        )

        await hi.api_instances_chat_slots(_request(_state(mgr)))

        assert mgr.calls == []


class TestRouteRegistration:
    """The literal must be registered before the catch-all that would swallow it."""

    def test_chat_slots_resolves_to_its_own_handler_not_the_proxy(self):
        from aiohttp import web

        from kiro_crew.dashboard.routes import connections

        app = web.Application()
        connections.register(app)

        paths = [
            (r.resource.canonical, r.handler)
            for r in app.router.routes()
            if r.resource is not None and "instances" in r.resource.canonical
        ]
        chat_slots = [i for i, (p, _) in enumerate(paths) if p.endswith("/chat-slots")]
        proxy = [
            i
            for i, (_, h) in enumerate(paths)
            if h is connections.handlers_instances.api_instances_proxy
        ]

        assert chat_slots, "the chat-slots route is not registered at all"
        assert proxy, "the proxy catch-all is not registered"
        assert chat_slots[0] < proxy[0]
        assert paths[chat_slots[0]][1] is connections.handlers_instances.api_instances_chat_slots

    def test_the_route_admits_no_mutating_verb(self):
        """Narrower than the ``("api", "chat")`` proxy row it replaces for this
        read: that row admits the peer's mutating verbs too, and a session on
        another machine has no local mutation to offer.

        A subset assertion rather than ``== {"GET"}`` because ``add_get`` binds
        HEAD alongside GET by default (``allow_head=True``) — which is still a
        read, and pinning the exact pair would fail on an aiohttp that stops
        doing it while the property under test held."""
        from aiohttp import web

        from kiro_crew.dashboard.routes import connections

        app = web.Application()
        connections.register(app)

        methods = {
            r.method
            for r in app.router.routes()
            if r.resource is not None and r.resource.canonical.endswith("/chat-slots")
        }
        assert "GET" in methods
        assert methods <= {"GET", "HEAD"}
