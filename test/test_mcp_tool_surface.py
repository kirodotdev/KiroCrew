"""Tool-set validation for a REPLACED MCP backend (issue #6294).

Two layers, both here so the contract reads in one place:

* :mod:`kiro_crew.mcp_gateway.tool_surface` — the pure projection and the change
  description every adoption point compares through. What counts as a change,
  what deliberately does not, and which listings are unmeasurable.
* :class:`~kiro_crew.mcp_gateway.backend.Backend` — recording the tool set a
  session was told about, and asking a replacement what IT publishes.

The gateway-side decision built on these (refuse the respawn, audit it) lives
with the rest of the respawn cases in ``test_mcp_gatewayd_coverage.py``.

In-memory pipes only: no subprocess, no network, no wall-clock waits.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import (
    TOOL_SURFACE_STUB_PREFIX,
    Backend,
    _PendingRequest,
)
from kiro_crew.mcp_gateway.pool import PoolKey
from kiro_crew.mcp_gateway.tool_surface import (
    _MAX_NAME_LEN,
    _MAX_TOOLS,
    _is_control,
    describe_surface_change,
    project_tool_surface,
)


@pytest.fixture(autouse=True)
def _no_real_metrics_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never append to the operator's real call-metrics file (import-time env)."""
    monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)


def _pool_key(server: str = "surface-mcp") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name="kirocrew",
        command_args_hash="cah",
        effective_env_hash="eeh",
        work_dir="/nonexistent-work-dir",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="aah",
        approval_mode="reads",
        trust_all_tools=False,
        config_snapshot_hash="csh",
    )


def _make_backend() -> Backend:
    """A real :class:`Backend` over a mock process + mock stdin writer."""
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    return Backend(
        pool_key=_pool_key(),
        process=cast(Any, proc),
        stdin=cast(Any, stdin),
        stdout=cast(Any, MagicMock()),
        created_at=now,
        last_used_at=now,
    )


def _listing(*tools: dict[str, Any]) -> dict[str, Any]:
    return {"tools": list(tools)}


def _tool(name: str, schema: Optional[dict[str, Any]] = None, **extra: Any) -> dict:
    entry: dict[str, Any] = {"name": name}
    if schema is not None:
        entry["inputSchema"] = schema
    entry.update(extra)
    return entry


_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}}


# --- the projection ---------------------------------------------------------


class TestProjectToolSurface:
    def test_tool_order_is_not_part_of_the_comparison(self) -> None:
        """A server free to enumerate its tools differently each spawn has not
        thereby changed what a call is built against."""
        a = project_tool_surface(_listing(_tool("read", _SCHEMA), _tool("write")))
        b = project_tool_surface(_listing(_tool("write"), _tool("read", _SCHEMA)))

        assert a == b
        assert describe_surface_change(a, b) == ""

    def test_key_order_inside_a_schema_is_normalised(self) -> None:
        """Two spellings of the same object are the same schema; a client's
        call is identical either way, so this must not read as a change."""
        a = project_tool_surface(_listing(_tool("t", {"type": "object", "x": 1})))
        b = project_tool_surface(_listing(_tool("t", {"x": 1, "type": "object"})))

        assert describe_surface_change(a, b) == ""

    def test_description_alone_is_not_a_change(self) -> None:
        """Rewording cannot invalidate arguments already built, and refusing on
        it would cost recoveries for servers that only reword."""
        a = project_tool_surface(_listing(_tool("t", _SCHEMA, description="old")))
        b = project_tool_surface(_listing(_tool("t", _SCHEMA, description="new")))

        assert describe_surface_change(a, b) == ""

    def test_a_renamed_tool_reads_as_gone(self) -> None:
        """The old name is what a held call names; the new one is a tool the
        frozen client does not know about."""
        a = project_tool_surface(_listing(_tool("read_file", _SCHEMA)))
        b = project_tool_surface(_listing(_tool("readFile", _SCHEMA)))

        assert describe_surface_change(a, b) == "gone=read_file"

    def test_an_added_tool_alone_is_agreement(self) -> None:
        """A name the client does not know cannot invalidate a call it already
        built, and the frozen tool set could not call it either way — so the
        commonest upgrade shape must not cost the transparent recovery."""
        a = project_tool_surface(_listing(_tool("read", _SCHEMA)))
        b = project_tool_surface(_listing(_tool("read", _SCHEMA), _tool("write")))

        assert describe_surface_change(a, b) == ""

    def test_a_newly_required_field_reads_as_a_schema_change(self) -> None:
        """The exact silent break the issue names: same name, same metadata, a
        call built against the old schema is now invalid."""
        a = project_tool_surface(_listing(_tool("t", {"type": "object"})))
        b = project_tool_surface(_listing(_tool("t", {"type": "object", "required": ["path"]})))

        assert describe_surface_change(a, b) == "schema-changed=t"

    def test_a_changed_argument_type_reads_as_a_schema_change(self) -> None:
        a = project_tool_surface(_listing(_tool("t", {"properties": {"n": {"type": "string"}}})))
        b = project_tool_surface(_listing(_tool("t", {"properties": {"n": {"type": "number"}}})))

        assert describe_surface_change(a, b) == "schema-changed=t"

    @pytest.mark.parametrize(
        "result",
        [
            pytest.param("not-an-object", id="result_not_an_object"),
            pytest.param({"tools": "nope"}, id="tools_not_an_array"),
            pytest.param({"tools": ["bare-string"]}, id="entry_not_an_object"),
            pytest.param({"tools": [{"name": 123}]}, id="name_not_a_string"),
            pytest.param({"tools": [{"noname": 1}]}, id="entry_has_no_name"),
            pytest.param({"tools": [{"name": "t"}, {"name": "t"}]}, id="duplicate_name"),
        ],
    )
    def test_an_unreadable_listing_is_unmeasurable_not_empty(self, result: Any) -> None:
        """``None`` keeps "we could not read what it named" distinguishable from
        "it named nothing" — collapsing them would let a malformed listing read
        as agreement with anything."""
        assert project_tool_surface(result) is None

    def test_an_app_only_tool_is_not_part_of_the_surface(self) -> None:
        surface = project_tool_surface(
            _listing(
                _tool("read", _SCHEMA),
                _tool("app_tool", _SCHEMA, _meta={"ui": {"visibility": ["app"]}}),
            )
        )

        assert set(surface or {}) == {"read"}

    def test_a_tool_withdrawn_from_the_model_reads_as_gone(self) -> None:
        """Visibility is enforced at LIST time only -- nothing re-checks it when a
        model-originated tools/call arrives -- so a frozen client would keep
        calling a tool the replacement no longer offers it."""
        a = project_tool_surface(_listing(_tool("t", _SCHEMA)))
        b = project_tool_surface(
            _listing(_tool("t", _SCHEMA, _meta={"ui": {"visibility": ["app"]}}))
        )

        assert describe_surface_change(a, b) == "gone=t"

    def test_a_tool_newly_offered_to_the_model_is_agreement(self) -> None:
        """The other direction is an addition: the client never held it and its
        tool set is frozen, so no call it built names the tool."""
        a = project_tool_surface(
            _listing(_tool("t", _SCHEMA, _meta={"ui": {"visibility": ["app"]}}))
        )
        b = project_tool_surface(_listing(_tool("t", _SCHEMA)))

        assert describe_surface_change(a, b) == ""

    def test_an_unreadable_visibility_declaration_withholds_the_tool(self) -> None:
        """Matching the listing filter, which withholds a tool whose declared
        visibility this host cannot parse -- so the client never held it."""
        surface = project_tool_surface(
            _listing(_tool("t", _SCHEMA, _meta={"ui": {"visibility": 42}}))
        )

        assert surface == {}

    def test_a_schema_is_retained_as_a_fixed_width_digest(self) -> None:
        """A surface is held per stub for that stub's lifetime and one frame may
        be as large as the transport allows, so the retained VALUE must not scale
        with the schema. The comparison only ever asks for equality."""
        small = project_tool_surface(_listing(_tool("t", {"a": 1})))
        huge = project_tool_surface(
            _listing(_tool("t", {"a": "x" * 100_000, "b": list(range(5_000))}))
        )

        assert small is not None and huge is not None
        assert len(small["t"]) == len(huge["t"]) == 64
        assert small["t"] != huge["t"]

    def test_an_invisible_tool_cannot_disable_validation(self) -> None:
        """Every unmeasurable rule is scoped to the model-visible set. Judged
        before the visibility filter, a tool the model cannot even see could
        switch the guard off for every tool it CAN see -- the anchor clears, and
        the next respawn adopts unvalidated."""
        hidden = {"ui": {"visibility": ["app"]}}
        poison = [
            # An absurd name on an invisible tool.
            _tool("n" * (_MAX_NAME_LEN + 1), _SCHEMA, _meta=hidden),
            # An invisible tool that alone blows the count budget.
            *(_tool(f"pad{i}", _SCHEMA, _meta=hidden) for i in range(_MAX_TOOLS + 1)),
            # An invisible duplicate of a visible name.
            _tool("read", {"other": True}, _meta=hidden),
            # An invisible tool whose schema would not serialise.
            _tool("bad_schema", {"x": {1, 2}}, _meta=hidden),
        ]

        surface = project_tool_surface(_listing(_tool("read", _SCHEMA), *poison))

        assert surface == project_tool_surface(_listing(_tool("read", _SCHEMA)))

    def test_the_count_budget_counts_only_visible_tools(self) -> None:
        """A server may publish thousands of app-only tools and a handful of
        model-visible ones; the budget bounds what is RETAINED."""
        hidden = {"ui": {"visibility": ["app"]}}
        listing = _listing(
            _tool("read", _SCHEMA),
            *(_tool(f"pad{i}", _SCHEMA, _meta=hidden) for i in range(_MAX_TOOLS * 2)),
        )

        assert set(project_tool_surface(listing) or {}) == {"read"}

    def test_a_listing_over_the_tool_budget_is_unmeasurable(self) -> None:
        """Refused, not truncated: dropping tools to fit would make a dropped
        tool's schema change read as agreement."""
        over = {"tools": [{"name": f"t{i}"} for i in range(_MAX_TOOLS + 1)]}

        assert project_tool_surface(over) is None
        assert project_tool_surface({"tools": over["tools"][:_MAX_TOOLS]}) is not None

    def test_an_over_long_tool_name_is_unmeasurable(self) -> None:
        """The name is a retained KEY, so its length is a retention budget and
        not only a display concern."""
        assert project_tool_surface(_listing(_tool("n" * (_MAX_NAME_LEN + 1)))) is None
        assert project_tool_surface(_listing(_tool("n" * _MAX_NAME_LEN))) is not None

    def test_a_server_with_no_tools_is_measurable_and_empty(self) -> None:
        surface = project_tool_surface(_listing())

        assert surface == {}
        # An addition is agreement, so an empty anchor agrees with any listing...
        assert describe_surface_change(surface, {"t": "null"}) == ""
        # ...but an empty surface is still a CLAIM, unlike None: a replacement
        # that cannot be read contradicts "the server named no tools", while
        # against no anchor at all there is nothing to contradict.
        assert describe_surface_change(surface, None) != ""
        assert describe_surface_change(None, None) == ""


class TestDescribeSurfaceChange:
    def test_no_anchor_is_agreement(self) -> None:
        """Nothing was ever served, so there is no claim a replacement can
        break — and refusing an adoption on a comparison that never had an
        anchor would fail a recovery on no evidence."""
        assert describe_surface_change(None, {"t": "null"}) == ""
        assert describe_surface_change(None, None) == ""

    def test_an_unreadable_replacement_is_reported_when_a_claim_exists(self) -> None:
        """Asymmetric with the case above on purpose: the same server answered
        projectably before and does not now, which IS the change."""
        assert "could not be read" in describe_surface_change({"t": "null"}, None)

    def test_the_named_tools_are_bounded(self) -> None:
        """This string is logged and SEL-audited; a 400-tool server must not put
        its whole listing in a log line."""
        old = {f"t{i}": "null" for i in range(9)}

        change = describe_surface_change(old, {})

        assert change.startswith("gone=t0,t1,t2,t3,t4 (+4 more)")

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("a\nWARNING forged", id="newline"),
            pytest.param("a\rWARNING forged", id="carriage_return"),
            pytest.param("a\x1b[31mred", id="escape"),
            pytest.param("a\x85next-line", id="c1_next_line"),
            pytest.param("a\u2028line-separator", id="u2028"),
            pytest.param("a\u2029paragraph-separator", id="u2029"),
        ],
    )
    def test_a_control_bearing_tool_name_cannot_break_the_line(self, raw: str) -> None:
        """The name is the SERVER's string and this description is written to a
        log line and an append-only audit field: a name carrying a newline would
        end the record early and open a second one the server controls."""
        change = describe_surface_change({raw: "null"}, {})

        assert change.startswith("gone=")
        assert not any(_is_control(ch) for ch in change)
        assert "\\x" in change or "\\u" in change
        # And what a consumer sees really is one line.
        assert len(change.splitlines()) == 1

    def test_the_escape_set_is_whatever_splitlines_breaks_on(self) -> None:
        """Defined by consequence, not by Unicode category: the set must cover
        whatever a consumer reading the line would split on, and Python's own
        splitlines is the reference. A future gap fails here rather than in a
        forged audit record -- which is how U+2028 was missed by a predicate
        built from the C0/C1 ranges alone."""
        missed = [
            hex(cp)
            for cp in range(0x11000)
            if len(f"a{chr(cp)}a".splitlines()) > 1 and not _is_control(chr(cp))
        ]

        assert missed == []

    def test_a_pathological_name_length_is_capped(self) -> None:
        """Capping the name COUNT alone would still let one name put kilobytes
        into a log line."""
        change = describe_surface_change({"t" * 500: "null"}, {})

        assert change.endswith("...")
        assert len(change) < 120


# --- what the backend records -----------------------------------------------


class TestServedToolSurfaceRecording:
    @pytest.mark.asyncio
    async def test_a_model_facing_listing_is_recorded_under_its_own_stub(self) -> None:
        backend = _make_backend()
        assert backend.served_tool_surface("stub-1") is None

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=7, method="tools/list"),
            {"id": 7, "result": _listing(_tool("read", _SCHEMA))},
        )

        assert backend.served_tool_surface("stub-1") == project_tool_surface(
            _listing(_tool("read", _SCHEMA))
        )
        # A co-pooled session that was told nothing still has no anchor.
        assert backend.served_tool_surface("stub-2") is None

    @pytest.mark.asyncio
    async def test_one_stubs_listing_cannot_overwrite_anothers_anchor(self) -> None:
        """A pool key carries no caller identity, so one backend serves several
        sessions. A single scalar would let whichever tenant listed most recently
        stand in for what a particular session was told."""
        backend = _make_backend()

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-a", original_id=1, method="tools/list"),
            {"id": 1, "result": _listing(_tool("for_a", _SCHEMA))},
        )
        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-b", original_id=2, method="tools/list"),
            {"id": 2, "result": _listing(_tool("for_b", _SCHEMA))},
        )

        assert set(backend.served_tool_surface("stub-a") or {}) == {"for_a"}
        assert set(backend.served_tool_surface("stub-b") or {}) == {"for_b"}

    @pytest.mark.asyncio
    async def test_detaching_a_stub_drops_its_anchor(self) -> None:
        """Bounded by the attached-stub set, which is also why the respawn path
        reads the anchor before it detaches."""
        backend = _make_backend()
        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=7, method="tools/list"),
            {"id": 7, "result": _listing(_tool("read", _SCHEMA))},
        )

        await backend.detach_stub("stub-1")

        assert backend.served_tool_surface("stub-1") is None

    @pytest.mark.asyncio
    async def test_the_anchor_is_the_model_visible_set(self) -> None:
        """What the client ends up HOLDING is what a replacement can contradict.
        An app-only tool was never in its listing, so recording it would make a
        change to it read as a refusal for a call no client could have built."""
        backend = _make_backend()
        app_only = _tool("app_tool", _SCHEMA, _meta={"ui": {"visibility": ["app"]}})
        result = _listing(_tool("read", _SCHEMA), app_only)

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=7, method="tools/list"),
            {"id": 7, "result": result},
        )

        # The delivered listing lost the app-only tool...
        assert [t["name"] for t in result["tools"]] == ["read"]
        # ...and so did the anchor, which is projected with the SAME predicate,
        # so recording before or after the strip cannot change it.
        assert set(backend.served_tool_surface("stub-1") or {}) == {"read"}

    @pytest.mark.asyncio
    async def test_an_internal_probe_listing_records_nothing(self) -> None:
        """The probe asks on the GATEWAY's behalf. Letting its answer overwrite
        the record would make the replacement its own anchor and the comparison
        would always agree."""
        backend = _make_backend()
        stub = f"{TOOL_SURFACE_STUB_PREFIX}abc"

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid=stub, original_id=7, method="tools/list"),
            {"id": 7, "result": _listing(_tool("read", _SCHEMA))},
        )

        assert backend.served_tool_surface(stub) is None

    @pytest.mark.asyncio
    async def test_an_unreadable_listing_leaves_no_anchor(self) -> None:
        backend = _make_backend()

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=7, method="tools/list"),
            {"id": 7, "result": {"tools": [{"name": 123}]}},
        )

        assert backend.served_tool_surface("stub-1") is None

    @pytest.mark.asyncio
    async def test_a_first_page_leaves_no_anchor(self) -> None:
        """``tools/list`` is paginated. A page recorded as the whole tool set
        would be compared against the probe's FIRST page and refuse a
        replacement that never changed."""
        backend = _make_backend()

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=7, method="tools/list"),
            {
                "id": 7,
                "result": {"tools": [_tool("read", _SCHEMA)], "nextCursor": "p2"},
            },
        )

        assert backend.served_tool_surface("stub-1") is None

    @pytest.mark.asyncio
    async def test_a_final_page_leaves_no_anchor_either(self) -> None:
        """The last page carries no ``nextCursor``, so it is indistinguishable
        from a complete listing on the response alone — the REQUEST's cursor is
        what says it is a continuation."""
        backend = _make_backend()

        await backend._maybe_intercept_ui_result(
            _PendingRequest(
                stub_uuid="stub-1",
                original_id=8,
                method="tools/list",
                list_paginated=True,
            ),
            {"id": 8, "result": _listing(_tool("read", _SCHEMA))},
        )

        assert backend.served_tool_surface("stub-1") is None

    @pytest.mark.asyncio
    async def test_a_page_supersedes_an_earlier_complete_listing(self) -> None:
        """A server that starts paginating has stopped telling this session its
        whole tool set, so an earlier complete claim no longer describes it."""
        backend = _make_backend()
        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=1, method="tools/list"),
            {"id": 1, "result": _listing(_tool("read", _SCHEMA))},
        )
        assert backend.served_tool_surface("stub-1") is not None

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=2, method="tools/list"),
            {"id": 2, "result": {"tools": [_tool("read", _SCHEMA)], "nextCursor": "p2"}},
        )

        assert backend.served_tool_surface("stub-1") is None

    @pytest.mark.asyncio
    async def test_an_unreadable_listing_supersedes_a_readable_one(self) -> None:
        """The client just received a listing this host cannot read, so an
        earlier readable claim no longer describes what the session holds."""
        backend = _make_backend()
        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=1, method="tools/list"),
            {"id": 1, "result": _listing(_tool("read", _SCHEMA))},
        )

        await backend._maybe_intercept_ui_result(
            _PendingRequest(stub_uuid="stub-1", original_id=2, method="tools/list"),
            {"id": 2, "result": {"tools": [{"name": 123}]}},
        )

        assert backend.served_tool_surface("stub-1") is None


# --- asking a replacement what it publishes ---------------------------------


class _ProbeBackend:
    """Minimal stand-in for the attach/forward/detach seam the probe rides.

    ``Backend.probe_tool_surface`` is exercised as an unbound call against this
    so the reply can be scripted without a subprocess, while the real method
    body (id matching, error handling, teardown) is the code under test.
    """

    def __init__(self, reply: Optional[dict[str, Any]]) -> None:
        self._reply = reply
        self.pid = 4242
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.attached: list[str] = []
        self.detached: list[str] = []
        self.cancelled: list[str] = []
        self.forwarded: list[dict[str, Any]] = []
        self.nonces: list[str] = []

    async def attach_stub(self, stub_uuid: str) -> "asyncio.Queue[bytes]":
        self.attached.append(stub_uuid)
        return self.inbox

    async def forward_from_stub(
        self,
        stub_uuid: str,
        msg: dict[str, Any],
        *,
        caller: Any = None,
        tenant_nonce: str = "",
    ) -> None:
        self.forwarded.append(msg)
        self.nonces.append(tenant_nonce)
        if self._reply is not None:
            payload = dict(self._reply)
            payload["id"] = msg["id"]
            await self.inbox.put(json.dumps(payload).encode("utf-8"))

    async def cancel_in_flight_for_stub(self, stub_uuid: str) -> list:
        self.cancelled.append(stub_uuid)
        return []

    async def detach_stub(self, stub_uuid: str) -> int:
        self.detached.append(stub_uuid)
        return 0


async def _probe(fake: _ProbeBackend, **kwargs: Any) -> Optional[dict[str, str]]:
    return await Backend.probe_tool_surface(cast(Any, fake), **kwargs)


class TestProbeToolSurface:
    @pytest.mark.asyncio
    async def test_a_listing_reply_is_projected(self) -> None:
        fake = _ProbeBackend({"result": _listing(_tool("read", _SCHEMA))})

        surface = await _probe(fake)

        assert surface == project_tool_surface(_listing(_tool("read", _SCHEMA)))
        assert fake.forwarded[0]["method"] == "tools/list"

    @pytest.mark.asyncio
    async def test_it_asks_on_an_internal_stub(self) -> None:
        """The prefix is what keeps the model-visibility filter off the
        server's own declaration."""
        fake = _ProbeBackend({"result": _listing()})

        await _probe(fake)

        assert fake.attached[0].startswith(TOOL_SURFACE_STUB_PREFIX)

    @pytest.mark.asyncio
    async def test_it_asks_in_the_connections_tenant_namespace(self) -> None:
        """The recorded listing was forwarded with the connection's tenant nonce.
        Probing without it would ask an unnamed caller's question in the
        per-process namespace instead, and read the difference as drift."""
        fake = _ProbeBackend({"result": _listing()})

        await _probe(fake, tenant_nonce="nonce-abc")

        assert fake.nonces == ["nonce-abc"]

    @pytest.mark.asyncio
    async def test_a_jsonrpc_error_establishes_nothing(self) -> None:
        fake = _ProbeBackend({"error": {"code": -32601, "message": "no tools"}})

        assert await _probe(fake) is None

    @pytest.mark.asyncio
    async def test_a_paginated_reply_establishes_nothing(self) -> None:
        """The probe asks without a cursor by design — following the chain would
        make a recovery path issue an unbounded request sequence — so a first
        page says nothing about the whole tool set."""
        fake = _ProbeBackend({"result": {"tools": [_tool("read", _SCHEMA)], "nextCursor": "p2"}})

        assert await _probe(fake) is None

    @pytest.mark.asyncio
    async def test_a_silent_backend_times_out_as_unmeasurable(self) -> None:
        fake = _ProbeBackend(None)

        assert await _probe(fake, timeout=0.01) is None

    @pytest.mark.asyncio
    async def test_every_outcome_cancels_then_detaches(self) -> None:
        """A probe that left its stub attached would hold this backend's
        refcount above where it started and block recycling forever."""
        for reply in ({"result": _listing()}, {"error": {"code": -1}}, None):
            fake = _ProbeBackend(cast(Any, reply))

            await _probe(fake, timeout=0.01)

            assert fake.cancelled == fake.attached
            assert fake.detached == fake.attached

    @pytest.mark.asyncio
    async def test_a_raising_seam_is_unmeasurable_not_a_crash(self) -> None:
        """Called from the recovery path, so a probe that blows up must not take
        the recovery down with it."""
        fake = _ProbeBackend({"result": _listing()})
        fake.forward_from_stub = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom")
        )

        assert await _probe(fake) is None
        assert fake.detached == fake.attached
