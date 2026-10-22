"""The import route's body: its format, its streamed read and the host bounds that admit it.

Pins gzip told from plain JSON by its magic (across chunk boundaries, one member only), the
body streamed to disk off the event loop with no size ceiling, a file bundle's
transcript-only mark read from its sender's flag, an exported file installing over the real
handlers, and the bounds an arrival is admitted under: concurrent expansions and uploads, a
stalled sender's cutoff, the parse-memory reservation from the document measure, cgroup
limits and disk headroom. ``test_session_transfer_intake.py`` pins how the intake's
exceptions are named; the sender's upload lives in ``test_session_transfer_outbound.py``,
the slot an arrival lands as in ``test_session_transfer_landing.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from types import SimpleNamespace

import pytest
from aiohttp import web
from session_transfer_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _ChunkedContent,
    _export_gz,
    _isolate_import_tmp,
    _make_request,
    _run_import,
    _stub_state,
    _valid,
)

from kiro_crew.testing.wait import async_wait_until

# ── install from a FILE: the body format ─────────────────────────────────


@pytest.mark.asyncio
async def test_import_accepts_the_exact_bytes_the_export_endpoint_writes(monkeypatch):
    """The acceptance bar: an exported file installs with no step in between.

    Compressed with the EXPORT MODULE'S OWN serialiser, not a local
    ``gzip.compress`` that happens to look similar — the defect this closes was
    precisely that the two halves of one product disagreed about a format, so a
    test that re-implements the sending half could pass while the endpoint still
    refuses the real file. ``GET .../export`` answers ``application/gzip``; the
    importer read ``request.json()`` and rejected those bytes as malformed JSON.
    """
    from kiro_crew.dashboard import session_transfer as st

    resp = await _run_import(st, monkeypatch, None, gz=_export_gz(_valid()))

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["messages"] == 1


@pytest.mark.asyncio
async def test_import_still_accepts_a_plain_json_body(monkeypatch):
    """The tunnel's server-to-server caller posts uncompressed JSON and must not
    break: the sender is an independently-updated install, so a receiver that
    started demanding gzip would refuse every peer that has not shipped this."""
    from kiro_crew.dashboard import session_transfer as st

    resp = await _run_import(st, monkeypatch, _valid())

    assert resp.status == 200, resp.body


@pytest.mark.asyncio
async def test_import_sniffs_the_magic_and_not_the_content_type(monkeypatch):
    """A browser uploading a ``.gz`` off disk sends whatever its platform guesses
    for the type — often ``application/octet-stream``, sometimes nothing. The
    stub request carries NO content type at all, so a handler that branched on
    the header could not reach the gzip path this asserts."""
    from kiro_crew.dashboard import session_transfer as st

    request = _make_request(_stub_state(st, monkeypatch), None, gz=_export_gz(_valid()))
    assert not hasattr(request, "content_type")

    resp = await st.api_chat_slot_import(request)

    assert resp.status == 200, resp.body


@pytest.mark.asyncio
async def test_import_refuses_a_corrupt_gzip_with_its_own_code(monkeypatch):
    """A truncated upload gets a code of its own, not the bad-JSON one.

    The sender needs to know its FILE did not survive the trip; told
    ``transfer_invalid_json`` it would go looking for a syntax error in a
    document it never wrote by hand.
    """
    from kiro_crew.dashboard import session_transfer as st

    truncated = _export_gz(_valid())[: len(_export_gz(_valid())) // 2]
    resp = await _run_import(st, monkeypatch, None, gz=truncated)

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_invalid_gzip"


@pytest.mark.asyncio
async def test_import_still_refuses_plain_garbage_as_bad_json(monkeypatch):
    """Bytes that are neither gzip nor JSON keep the code they always had."""
    from kiro_crew.dashboard import session_transfer as st

    state = _stub_state(st, monkeypatch)
    resp = await st.api_chat_slot_import(_make_request(state, None, raw="{not json"))

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "transfer_invalid_json"


@pytest.mark.asyncio
async def test_import_has_no_size_ceiling(monkeypatch):
    """A bundle far past 20 MB of content / 40 MB of Layer B / a ~68 MiB
    decompressed body imports successfully — the owner's decision that a transfer
    is never blocked by size.
    """
    from kiro_crew.dashboard import session_transfer as st

    huge = _valid(
        messages=[{"role": "user", "content": "x" * 4_000_000, "ts": ""} for _ in range(8)]
    )
    gz = _export_gz(huge)
    resp = await _run_import(st, monkeypatch, None, gz=gz)

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["ok"] is True


def test_import_never_reads_the_whole_body_into_memory():
    """Source guard: the arrival path streams and must not buffer the whole body.

    ``request.read()`` / ``.post()`` / ``.json()`` each materialise the entire body
    in memory AND are the calls aiohttp enforces ``client_max_size`` in — using any
    of them would both cap the size and defeat the streaming. Assert the handler and
    its body reader never call them (comments stripped, so a comment mentioning one
    does not mask a real call).
    """
    import ast
    from pathlib import Path as _Path

    import kiro_crew.dashboard.session_transfer as st

    tree = ast.parse(
        "\n".join(
            _Path(st.__file__).with_name(f"session_transfer{part}.py").read_text(encoding="utf-8")
            for part in ("", "_intake", "_install")
        )
    )
    guarded = {"_read_bundle_body", "_stream_request_to_file", "_install_arrived_bundle"}
    targets = {
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in guarded
    }
    # Every guarded handler must be found, or the loop below inspects nothing and
    # the guard passes whatever the arrival path does.
    assert {fn.name for fn in targets} == guarded, sorted(guarded - {fn.name for fn in targets})
    banned = {"read", "post", "json"}
    offenders = []
    for fn in targets:
        for node in ast.walk(fn):
            # request.<banned>(...) — an attribute call on a name/attr chain whose
            # attribute is one of the buffering reads.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in banned
                and isinstance(node.func.value, (ast.Name, ast.Attribute))
            ):
                base = node.func.value
                base_name = base.id if isinstance(base, ast.Name) else getattr(base, "attr", "")
                if base_name == "request":
                    offenders.append(f"{fn.name}: request.{node.func.attr}()")
    assert not offenders, offenders


# ── install from a FILE: the round trip ──────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_expansions_are_bounded_and_the_excess_is_refused(monkeypatch):
    """The per-body cap bounds ONE request; the sum is what exhausts a host.

    Without a concurrency bound, N authenticated requests each hold up to the
    ceiling at the same time. Asserted on the OBSERVED simultaneity, not on the
    presence of a semaphore: the gunzip is replaced with one that records how
    many are inside it at once.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_expansion_lock", None)
    monkeypatch.setattr(st, "_expansion_slots", None)
    monkeypatch.setattr(st, "_expansion_waiting", 0)

    inside = {"now": 0, "peak": 0}
    release = asyncio.Event()

    async def _slow_to_thread(fn, *args):
        if fn is st._gunzip_file:
            inside["now"] += 1
            inside["peak"] = max(inside["peak"], inside["now"])
            # flake-ok: pre-existing park of the held gunzip until the test releases it, moved verbatim; tracked for a de-flake pass
            await release.wait()
            inside["now"] -= 1
            return fn(*args)
        return fn(*args)

    monkeypatch.setattr(st.asyncio, "to_thread", _slow_to_thread)
    gz = _export_gz(_valid())

    # More than the queue allows, all in flight together.
    running = [
        asyncio.create_task(_run_import(st, monkeypatch, None, gz=gz))
        for _ in range(st._MAX_CONCURRENT_EXPANSIONS + st._MAX_QUEUED_EXPANSIONS + 2)
    ]
    # Wait for the gate rather than a fixed sleep: every request streams its body
    # to disk before it reaches admission, which takes longer than any fixed tick
    # on a slow runner. The gunzip is held shut, so an admitted or queued import
    # cannot finish yet; any task done before the release is one the gate refused.
    await async_wait_until(
        lambda: any(t.done() for t in running),
        describe=lambda: "no import ever reached the gate",
    )
    refused = [t for t in running if t.done()]
    release.set()
    results = await asyncio.gather(*running)

    assert inside["peak"] <= st._MAX_CONCURRENT_EXPANSIONS, inside
    assert refused, "the excess must be refused immediately, not parked"
    busy = [r for r in results if r.status == 429]
    assert busy, [r.status for r in results]
    assert json.loads(busy[0].body)["code"] == "transfer_expansion_busy"


@pytest.mark.asyncio
async def test_the_expansion_permit_outlives_the_decompression(monkeypatch):
    """A permit that ends at the gunzip bounds the CPU, not the memory.

    A decompressed bundle stays resident -- first as bytes, then as the parsed
    document -- through redaction and persistence, so what has to be bounded is
    how many are resident AT ONCE, not how many are inflating at once. Measured
    inside the post-decompression pass rather than at the gunzip: with the permit
    released when the body has been read, every admitted caller frees its slot
    immediately and they all pile into redaction together, which is precisely the
    sum the bound exists to prevent.
    """
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_expansion_lock", None)
    monkeypatch.setattr(st, "_expansion_slots", None)
    monkeypatch.setattr(st, "_expansion_waiting", 0)

    resident = {"now": 0, "peak": 0}
    release = asyncio.Event()

    async def _park_in_redaction(fn, *args):
        if fn is st._build_redacted_rows:
            resident["now"] += 1
            resident["peak"] = max(resident["peak"], resident["now"])
            # flake-ok: pre-existing park of the redaction pass until the test releases it, moved verbatim; tracked for a de-flake pass
            await release.wait()
            resident["now"] -= 1
        return fn(*args)

    monkeypatch.setattr(st.asyncio, "to_thread", _park_in_redaction)
    gz = _export_gz(_valid())

    running = [
        asyncio.create_task(_run_import(st, monkeypatch, None, gz=gz))
        for _ in range(st._MAX_CONCURRENT_EXPANSIONS + st._MAX_QUEUED_EXPANSIONS + 2)
    ]
    # Wait for the first caller to park, then give the rest time to pile in
    # behind it: a fixed sleep measures the harness's speed, not the permit.
    await async_wait_until(
        lambda: resident["peak"], describe=lambda: f"resident peak so far: {resident['peak']}"
    )
    # flake-ok: pre-existing 100 ms pile-in window behind the parked caller, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 100 ms pile-in window behind the parked caller, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0.1)
    peak_while_parked = resident["peak"]
    release.set()
    await asyncio.gather(*running)

    assert peak_while_parked, "no caller reached redaction; the harness missed the pass"
    assert peak_while_parked <= st._MAX_CONCURRENT_EXPANSIONS, resident


@pytest.mark.asyncio
async def test_the_transcript_only_mark_reads_layer_b_skipped_not_absence(monkeypatch):
    """A file bundle MAY carry Layer B and usually will not — so the mark is
    driven by the sender's explicit signal, never inferred from absence.

    Three cases, because inferring from absence gets two of them wrong:

    * no Layer B and no signal — a session that never HAD a kiro-cli context.
      Nothing was lost, so nothing is marked. Inference would mark it.
    * no Layer B, ``layer_b_skipped`` set — context existed and was withheld
      (the export's default-off posture, or a mid-turn source). Marked.
    * Layer B present — full fidelity, not marked.
    """
    from kiro_crew.dashboard import session_transfer as st

    never_had = await _run_import(st, monkeypatch, None, gz=_export_gz(_valid()), return_slot=True)
    assert "transcript only" not in never_had.title

    withheld = await _run_import(
        st,
        monkeypatch,
        None,
        gz=_export_gz(_valid(layer_b_skipped=True)),
        return_slot=True,
    )
    assert "transcript only" in withheld.title


# ── the round trip, over the real handlers ───────────────────────────────


@pytest.mark.asyncio
async def test_an_exported_file_installs_with_no_step_in_between(tmp_path):
    """The two halves of the feature, wired to each other over HTTP.

    Every other test here stubs one side. This one stubs NEITHER: a real
    ``DashboardState``, the real ``api_chat_slot_export``, the real
    ``api_chat_slot_import``. It is the test whose absence let the defect ship —
    each half was covered, the PAIR was not, and the pair is where they disagreed
    about a format.

    Also covers, in one pass, the two exit criteria that are about repetition
    rather than about a single install: the same file installed twice yields TWO
    separate sessions, and a plain-JSON body keeps working alongside gzip so the
    tunnel is unregressed.
    """
    import gzip

    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.session_export import api_chat_slot_export
    from kiro_crew.dashboard.session_transfer import api_chat_slot_import

    state = _make_state(tmp_path)
    # Make the SOURCE session auto-approving, so the exported file genuinely
    # RECORDS `approval_policy: "auto"`. Exit criterion: an installed session
    # lands interactive regardless of what its source record says — asserted
    # below, and vacuous unless the file actually carries the dangerous value.
    state.sessions.has_session.return_value = True
    state.sessions.get_approval_policy.return_value = "auto"
    app = web.Application(client_max_size=60 * 1024 * 1024)
    app["state"] = state
    app.router.add_post("/api/chat/slots/import", api_chat_slot_import)
    app.router.add_get("/api/chat/slots/{slot}/export", api_chat_slot_export)

    async with TestClient(TestServer(app)) as client:
        body = json.dumps(_valid(origin="seedbox", title="round trip")).encode()

        # Seed one session the way the tunnel does: an uncompressed JSON body.
        seeded = await client.post(
            "/api/chat/slots/import", data=body, headers={"Content-Type": "application/json"}
        )
        assert seeded.status == 200, await seeded.text()
        seeded_key = (await seeded.json())["key"]

        # Export it. These are the bytes a user is handed.
        exported = await client.get(f"/api/chat/slots/{seeded_key}/export")
        assert exported.status == 200, await exported.text()
        assert exported.headers["Content-Type"] == "application/gzip"
        gz = await exported.read()
        assert gz[:2] == b"\x1f\x8b"
        # The file really does record auto-approval, so the interactive-on-arrival
        # assertion at the end is measuring something. Without this the mock could
        # stop reporting a policy and that assertion would still pass.
        assert json.loads(gzip.decompress(gz))["source"]["approval_policy"] == "auto"

        # Install them UNCHANGED. No gunzip, no re-encode, no manual step. This
        # is the assertion the whole change exists for.
        first = await client.post(
            "/api/chat/slots/import",
            data=gz,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert first.status == 200, await first.text()
        first_key = (await first.json())["key"]

        # The same file again: another session, not a refusal and not a merge.
        second = await client.post(
            "/api/chat/slots/import",
            data=gz,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert second.status == 200, await second.text()
        second_key = (await second.json())["key"]
        assert second_key != first_key

        # And the tunnel's plain body still lands, next to them.
        tunnel_again = await client.post(
            "/api/chat/slots/import", data=body, headers={"Content-Type": "application/json"}
        )
        assert tunnel_again.status == 200, await tunnel_again.text()

    # The same file installed twice is TWO sessions, not one replaced: install
    # only ever adds, so a second install of one file cannot reach the first.
    assert first_key != second_key, (first_key, second_key)
    assert first_key in state._slots and second_key in state._slots

    # An installed session lands INTERACTIVE, whatever the source recorded: the
    # materialiser applies a fixed field set that does not include
    # approval_policy, so there is no path by which a file can arrive
    # pre-approved to run tools.
    for key in (first_key, second_key):
        assert getattr(state._slots[key], "approval_policy", "") == ""


@pytest.mark.asyncio
async def test_a_body_past_the_server_client_max_size_still_imports(tmp_path):
    """A body larger than the Application's ``client_max_size`` imports successfully.

    The import path streams ``request.content`` to disk instead of calling
    ``request.read()``, so aiohttp's ``client_max_size`` — which it enforces only in
    the buffering reads — does not gate it, exactly as the streaming multipart upload
    path bypasses the same limit. A valid bundle several times the (deliberately
    tiny) server limit installs successfully rather than being told it is too large,
    over BOTH transports.
    """
    import gzip

    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.session_transfer import api_chat_slot_import

    app = web.Application(client_max_size=1024)
    app["state"] = _make_state(tmp_path)
    app.router.add_post("/api/chat/slots/import", api_chat_slot_import)

    # A valid bundle whose serialised size is far past the 1024-byte server limit.
    # High-entropy content so the gzip form is also over the limit (a run of one
    # character would compress to well under 1 KiB and prove nothing about gzip).
    big = _valid(
        origin="seedbox",
        # nosemgrep: kirocrew.test-unseeded-random -- pre-existing high-entropy filler: no assertion reads its bytes, only its size against the limit; moved verbatim
        messages=[{"role": "user", "content": os.urandom(40_000).hex(), "ts": ""}],
    )
    plain = json.dumps(big).encode()
    assert len(plain) > 1024

    async with TestClient(TestServer(app)) as client:
        # Plain JSON (the tunnel's shape), past the limit.
        r_plain = await client.post(
            "/api/chat/slots/import", data=plain, headers={"Content-Type": "application/json"}
        )
        assert r_plain.status == 200, await r_plain.text()

        # Gzip (the exported-file shape); the compressed form is also > 1024.
        gz = gzip.compress(plain)
        assert len(gz) > 1024
        r_gz = await client.post(
            "/api/chat/slots/import",
            data=gz,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r_gz.status == 200, await r_gz.text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [[b"\x1f", b"\x8b\x08\x00rest"], [b"\x1f", b"\x8b", b"\x08\x00rest"]],
    ids=["one-byte-first-chunk", "one-byte-chunks"],
)
async def test_gzip_is_sniffed_across_a_one_byte_first_chunk(tmp_path, chunks):
    """The gzip magic is two bytes; a network chunk can be one. Deciding from a
    one-byte prefix would read every such gzip body as plain JSON."""
    from kiro_crew.dashboard import session_transfer as st

    dst = tmp_path / "body"
    is_gzip, total = await st._stream_request_to_file(
        SimpleNamespace(content=_ChunkedContent(chunks)), dst
    )

    assert is_gzip is True
    assert dst.read_bytes() == b"".join(chunks)
    assert total == len(b"".join(chunks))


@pytest.mark.asyncio
async def test_a_body_shorter_than_the_magic_is_kept_whole(tmp_path):
    from kiro_crew.dashboard import session_transfer as st

    dst = tmp_path / "body"
    is_gzip, total = await st._stream_request_to_file(
        SimpleNamespace(content=_ChunkedContent([b"{"])), dst
    )

    assert is_gzip is False
    assert dst.read_bytes() == b"{" and total == 1


@pytest.mark.asyncio
async def test_the_temp_file_is_opened_and_closed_off_the_event_loop(tmp_path, monkeypatch):
    """A close flushes, so it is as able to stall the loop as a write is."""
    import threading

    from kiro_crew.dashboard import session_transfer as st

    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    real_open = open

    class _Tracked:
        def __init__(self, f):
            self._f = f

        def write(self, b):
            seen["write"] = threading.get_ident()
            return self._f.write(b)

        def close(self):
            seen["close"] = threading.get_ident()
            return self._f.close()

    def _open(path, mode="r", *a, **k):
        seen["open"] = threading.get_ident()
        return _Tracked(real_open(path, mode, *a, **k))

    monkeypatch.setattr(st, "open", _open, raising=False)
    await st._stream_request_to_file(
        SimpleNamespace(content=_ChunkedContent([b'{"a":', b"1}"])), tmp_path / "body"
    )

    assert set(seen) == {"open", "write", "close"}
    assert all(t != loop_thread for t in seen.values()), seen


def _fake_memory(st, monkeypatch, *, total_mib, available, cgroup=(None, None)):
    """Point the host readings at fixed values; *available* is an iterator of MiB.
    *cgroup* is the (limit, headroom) in bytes the gateway's own cgroup reads;
    pinned so the runner's real cgroup never decides a test."""
    monkeypatch.setattr(st, "_cgroup_memory_bounds", lambda: cgroup)
    monkeypatch.setattr(st.platform_compat, "host_total_mib", lambda: total_mib)
    monkeypatch.setattr(st.platform_compat, "host_available_mib", lambda: next(available))
    monkeypatch.setattr(st, "_MEMORY_POLL_SECS", 0.001)
    monkeypatch.setattr(st, "_MEMORY_HEADROOM_BYTES", 0)
    monkeypatch.setattr(st, "_memory_reserved", 0)


_MIB = 1024 * 1024


@pytest.mark.asyncio
async def test_a_large_import_waits_for_memory_instead_of_being_refused(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    readings = iter([10, 10, 10_000])  # short, short, then freed
    _fake_memory(st, monkeypatch, total_mib=100_000, available=readings)

    async with st._memory_admission(100 * _MIB):  # needs 300 MiB
        assert st._memory_reserved == 300 * _MIB
    assert st._memory_reserved == 0
    assert next(readings, None) is None, "it looked again until memory was free"


@pytest.mark.asyncio
async def test_a_wait_past_the_budget_is_refused_as_retryable(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    _fake_memory(st, monkeypatch, total_mib=100_000, available=iter(lambda: 1, None))
    monkeypatch.setattr(st, "SESSION_IMPORT_MEMORY_WAIT_SECS", 0.01)

    with pytest.raises(st._MemoryWaitTimedOut):
        async with st._memory_admission(100 * _MIB):
            pass
    assert st._memory_reserved == 0


@pytest.mark.asyncio
async def test_a_document_larger_than_the_host_is_refused_without_waiting(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    # One reading only: a second, which a wait would take, fails the test.
    _fake_memory(st, monkeypatch, total_mib=100, available=iter([100]))

    with pytest.raises(st._NeverFits):
        async with st._memory_admission(100 * _MIB):  # needs 300 MiB of 100
            pass


@pytest.mark.asyncio
async def test_a_second_import_waits_for_the_first_ones_reservation(monkeypatch):
    """Both see the same free memory; the reservation is what stops them both
    parsing at once."""
    from kiro_crew.dashboard import session_transfer as st

    _fake_memory(st, monkeypatch, total_mib=100_000, available=iter(lambda: 400, None))
    monkeypatch.setattr(st, "SESSION_IMPORT_MEMORY_WAIT_SECS", 5.0)
    order: list[str] = []
    first_in = asyncio.Event()
    release_first = asyncio.Event()

    async def first():
        async with st._memory_admission(100 * _MIB):  # 300 of 400
            order.append("first-in")
            first_in.set()
            # flake-ok: pre-existing park of the first admission until the test releases it, moved verbatim; tracked for a de-flake pass
            await release_first.wait()
        order.append("first-out")

    async def second():
        # flake-ok: pre-existing wait for the first admission's entry signal, moved verbatim; tracked for a de-flake pass
        await first_in.wait()
        async with st._memory_admission(100 * _MIB):
            order.append("second-in")

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    # flake-ok: pre-existing wait for the first admission's entry signal, moved verbatim; tracked for a de-flake pass
    await first_in.wait()
    # flake-ok: pre-existing 50 ms window in which the second admission must stay out, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window in which the second admission must stay out, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0.05)
    assert order == ["first-in"], "the second must not be admitted beside the first"
    release_first.set()
    await asyncio.gather(t1, t2)
    assert order == ["first-in", "first-out", "second-in"]


@pytest.mark.asyncio
async def test_an_unreadable_host_is_not_gated(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    _fake_memory(st, monkeypatch, total_mib=0, available=iter(lambda: 0, None))

    async with st._memory_admission(10_000 * _MIB):
        pass


@pytest.mark.asyncio
async def test_the_import_route_answers_a_session_the_host_cannot_hold(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    _fake_memory(st, monkeypatch, total_mib=0, available=iter(lambda: 1, None))
    monkeypatch.setattr(st.platform_compat, "host_total_mib", lambda: 1)
    monkeypatch.setattr(st, "_PARSE_MEMORY_FACTOR", 1_000_000)
    resp = await _run_import(st, monkeypatch, _valid())

    assert resp.status == 413
    assert json.loads(resp.body)["code"] == "transfer_bundle_too_large"


def _low_disk(st, monkeypatch, free: int = 0):
    """Report *free* bytes on every volume, and check on every chunk."""
    monkeypatch.setattr(st.shutil, "disk_usage", lambda _p: SimpleNamespace(free=free))
    monkeypatch.setattr(st, "_DISK_CHECK_EVERY_CHUNKS", 1)


def test_decompression_stops_before_the_volume_fills(tmp_path, monkeypatch):
    """With no size ceiling, a small gzip can expand to anything; the write stops
    once the volume is down to its headroom."""
    import gzip as _gzip

    from kiro_crew.dashboard import session_transfer as st

    src = tmp_path / "body.gz"
    src.write_bytes(_gzip.compress(b"x" * (4 * st._CHUNK_BYTES)))
    _low_disk(st, monkeypatch)

    with pytest.raises(st._DiskFull):
        st._gunzip_file(src, tmp_path / "out.json")


@pytest.mark.asyncio
async def test_a_plain_body_stops_before_the_volume_fills(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    _low_disk(st, monkeypatch)
    chunks = [b"{" + b" " * 1000] * 4

    with pytest.raises(st._DiskFull):
        await st._stream_request_to_file(
            SimpleNamespace(content=_ChunkedContent(chunks)), tmp_path / "body"
        )


@pytest.mark.asyncio
async def test_the_import_route_answers_507_when_the_volume_is_full(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    _low_disk(st, monkeypatch)
    resp = await _run_import(st, monkeypatch, _valid())

    assert resp.status == 507
    assert json.loads(resp.body)["code"] == "transfer_disk_full"


@pytest.mark.asyncio
async def test_an_unreadable_volume_is_not_gated(tmp_path, monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    def _raise(_p):
        raise OSError("no statvfs")

    monkeypatch.setattr(st.shutil, "disk_usage", _raise)
    monkeypatch.setattr(st, "_DISK_CHECK_EVERY_CHUNKS", 1)

    is_gzip, total = await st._stream_request_to_file(
        SimpleNamespace(content=_ChunkedContent([b"{}", b"  "])), tmp_path / "body"
    )
    assert (is_gzip, total) == (False, 4)


@pytest.mark.asyncio
async def test_a_session_too_big_for_the_host_names_the_budget(monkeypatch):
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st.platform_compat, "host_total_mib", lambda: 2048)
    monkeypatch.setattr(st, "_MEMORY_HEADROOM_BYTES", 1024 * _MIB)
    monkeypatch.setattr(st, "_PARSE_MEMORY_FACTOR", 1_000_000_000)
    resp = await _run_import(st, monkeypatch, _valid())

    assert resp.status == 413
    assert "can spare 1.0 GiB" in json.loads(resp.body)["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", [1, 2], ids=["raw-body-temp", "decompressed-temp"])
async def test_a_temp_file_the_volume_cannot_create_is_a_coded_507(monkeypatch, fail_on):
    import gzip as _gzip

    from kiro_crew.dashboard import session_transfer as st

    real = st._new_import_temp
    calls = {"n": 0}

    def _new_temp(suffix):
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise OSError(28, "No space left on device")
        return real(suffix)

    monkeypatch.setattr(st, "_new_import_temp", _new_temp)
    body = _gzip.compress(json.dumps(_valid()).encode())
    resp = await _run_import(st, monkeypatch, None, gz=body)

    assert resp.status == 507
    assert json.loads(resp.body)["code"] == "transfer_disk_full"


@pytest.mark.parametrize(
    "aligned", [True, False], ids=["member-ends-on-a-read", "member-ends-mid-read"]
)
def test_a_second_gzip_member_is_refused_wherever_the_first_ends(tmp_path, monkeypatch, aligned):
    """A concatenated gzip is refused rather than decoded to its first member,
    including when the first member ends exactly on a read boundary, where
    ``unused_data`` is empty and the rest of the file has simply not been read."""
    import gzip as _gzip
    import zlib as _zlib

    from kiro_crew.dashboard import session_transfer as st

    first = _gzip.compress(b'{"a":1}')
    monkeypatch.setattr(st, "_CHUNK_BYTES", len(first) if aligned else len(first) + 3)
    src = tmp_path / "two-members.gz"
    src.write_bytes(first + _gzip.compress(b'{"b":2}'))

    with pytest.raises(_zlib.error):
        st._gunzip_file(src, tmp_path / "out.json")


def _marks_outside_strings(doc: str) -> int:
    """Reference count of structural marks outside JSON strings, one character
    at a time."""
    count, in_string, escaped = 0, False, False
    for ch in doc:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[,:":
            count += 1
    return count


def test_the_document_measure_counts_messages_across_chunk_boundaries(tmp_path, monkeypatch):
    """Every message is counted exactly once, including one whose key straddles
    a read boundary, and the count never has to hold the document."""
    from kiro_crew.dashboard import session_transfer as st

    doc = json.dumps(_valid(messages=[{"role": "user", "content": "x", "ts": ""}] * 50))
    p = tmp_path / "doc.json"
    p.write_bytes(doc.encode())
    marks = _marks_outside_strings(doc)
    for chunk in (1, 2, 5, 6, 7, 64, 1 << 20):
        monkeypatch.setattr(st, "_CHUNK_BYTES", chunk)
        assert st._measure_document(p) == (len(doc.encode()), 50, marks, 3), chunk


def test_the_value_count_skips_marks_inside_strings(tmp_path, monkeypatch):
    """Code-dense text and an embedded log carry ``{ [ , :`` in their content;
    those build no values and must not reserve for them, at any chunk size."""
    from kiro_crew.dashboard import session_transfer as st

    content = 'def f(a, b): return {"k": [a, b]}  \\ ' + '{"ev":"x","n":[1,2]}\n' * 20
    doc = json.dumps({"messages": [{"role": "user", "content": content}] * 5, "tail": "\\"})
    p = tmp_path / "doc.json"
    p.write_bytes(doc.encode())
    expected = _marks_outside_strings(doc)
    assert expected < sum(doc.count(c) for c in "{[,:") // 10
    for chunk in (1, 2, 3, 5, 17, 64, 1 << 20):
        monkeypatch.setattr(st, "_CHUNK_BYTES", chunk)
        assert st._measure_document(p)[2] == expected, chunk


def test_the_measure_splits_escape_runs_at_any_chunk_boundary(tmp_path, monkeypatch):
    """Odd and even backslash runs straddling a chunk boundary keep their quotes
    paired, so the value count matches the unchunked count."""
    from kiro_crew.dashboard import session_transfer as st

    doc = json.dumps(
        {"messages": [{"content": "a" + "\\" * n + '"' + "{,:"} for n in range(9)], "k": [1, 2]}
    )
    p = tmp_path / "doc.json"
    p.write_bytes(doc.encode())
    expected = _marks_outside_strings(doc)
    for chunk in (1, 2, 3, 4, 7, 1 << 20):
        monkeypatch.setattr(st, "_CHUNK_BYTES", chunk)
        assert st._measure_document(p)[2] == expected, chunk


def test_a_long_backslash_run_is_measured_in_bounded_memory(tmp_path, monkeypatch):
    """A body that is one long run of backslashes never parses as JSON, but the
    measure runs before the memory admission, so it must not accumulate it."""
    import tracemalloc

    from kiro_crew.dashboard import session_transfer as st

    chunk = 4096
    monkeypatch.setattr(st, "_CHUNK_BYTES", chunk)
    p = tmp_path / "doc.json"
    p.write_bytes(b"\\" * (chunk * 256))
    tracemalloc.start()
    try:
        st._measure_document(p)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # A few chunk-sized buffers, far below the 1 MiB run.
    assert peak < chunk * 32, peak


@pytest.mark.parametrize(
    "text, ensure_ascii, factor",
    [
        ("plain ascii", True, 3),
        ("中文", False, 6),
        ("中文", True, 5),
        ("\U0001f600", False, 12),
        ("\U0001f600", True, 9),
    ],
)
def test_the_parse_factor_follows_the_widest_character(
    tmp_path, monkeypatch, text, ensure_ascii, factor
):
    """One wide character makes CPython store the whole decoded text, and the
    string holding it, at that width; the reservation has to follow."""
    from kiro_crew.dashboard import session_transfer as st

    doc = json.dumps(
        {"messages": [{"role": "user", "content": "x" * 100 + text}]}, ensure_ascii=ensure_ascii
    )
    p = tmp_path / "doc.json"
    p.write_bytes(doc.encode())
    for chunk in (1, 3, 1 << 20):
        monkeypatch.setattr(st, "_CHUNK_BYTES", chunk)
        assert st._measure_document(p)[3] == factor, chunk


@pytest.mark.asyncio
async def test_many_short_messages_are_reserved_for_by_count_not_only_by_size(monkeypatch):
    """Their objects outweigh their text, so the reservation grows with the count."""
    from kiro_crew.dashboard import session_transfer as st

    _fake_memory(st, monkeypatch, total_mib=100_000, available=iter(lambda: 100_000, None))

    async with st._memory_admission(1 * _MIB, 1000):
        assert st._memory_reserved == 3 * _MIB + 1000 * st._PER_MESSAGE_BYTES


@pytest.mark.asyncio
async def test_the_import_route_counts_messages_into_the_reservation(monkeypatch):
    """A document small by size but heavy by count is refused on a host that
    the size-only estimate would have admitted."""
    from kiro_crew.dashboard import session_transfer as st

    body = _valid(messages=[{"role": "user", "content": "x", "ts": ""}] * 2000)
    size = len(json.dumps(body).encode())
    # A host whose whole budget is 1.5x the size-only estimate.
    budget_mib = max(1, (size * st._PARSE_MEMORY_FACTOR * 3 // 2) // _MIB)
    _fake_memory(st, monkeypatch, total_mib=budget_mib, available=iter(lambda: budget_mib, None))
    monkeypatch.setattr(st, "_PER_MESSAGE_BYTES", 1 * _MIB)
    resp = await _run_import(st, monkeypatch, body)

    assert resp.status == 413


@pytest.mark.asyncio
async def test_a_body_smaller_than_the_check_interval_is_still_checked(tmp_path, monkeypatch):
    """The headroom holds for every body, not only those past the first interval."""
    import gzip as _gzip

    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st.shutil, "disk_usage", lambda _p: SimpleNamespace(free=0))
    assert st._DISK_CHECK_EVERY_CHUNKS > 1

    with pytest.raises(st._DiskFull):
        await st._stream_request_to_file(
            SimpleNamespace(content=_ChunkedContent([b"{}  "])), tmp_path / "body"
        )
    src = tmp_path / "small.gz"
    src.write_bytes(_gzip.compress(b'{"a":1}'))
    with pytest.raises(st._DiskFull):
        st._gunzip_file(src, tmp_path / "out.json")


@pytest.mark.asyncio
async def test_a_container_heavy_document_is_reserved_for_by_its_values(monkeypatch):
    """An array of empty objects carries no messages and costs about 24 times its
    text; the value count is what reserves for it."""
    from kiro_crew.dashboard import session_transfer as st

    _fake_memory(st, monkeypatch, total_mib=100_000, available=iter(lambda: 100_000, None))

    async with st._memory_admission(1 * _MIB, 0, 1000):
        assert st._memory_reserved == 3 * _MIB + 1000 * st._PER_VALUE_BYTES


@pytest.mark.asyncio
async def test_the_import_route_reserves_for_a_messageless_container_flood(monkeypatch):
    """A document with no ``role`` key at all but many empty objects is refused on
    a host that a text-and-message estimate would have admitted."""
    from kiro_crew.dashboard import session_transfer as st

    body = _valid(messages=[{"role": "user", "content": "x", "ts": ""}])
    body["filler"] = [{}] * 20000
    size = len(json.dumps(body).encode())
    budget_mib = max(1, (size * st._PARSE_MEMORY_FACTOR * 3 // 2) // _MIB)
    _fake_memory(st, monkeypatch, total_mib=budget_mib, available=iter(lambda: budget_mib, None))
    monkeypatch.setattr(st, "_PER_VALUE_BYTES", 1 * _MIB)
    resp = await _run_import(st, monkeypatch, body)

    assert resp.status == 413


@pytest.mark.asyncio
@pytest.mark.parametrize("emoji, status", [("", 200), ("\U0001f600", 413)])
async def test_one_emoji_raises_the_imports_reservation(monkeypatch, emoji, status):
    """A single character past the BMP widens the whole parse, so a host that
    fits the ASCII estimate with room to spare must refuse the same document
    carrying one emoji rather than admit it and run out of memory."""
    from kiro_crew.dashboard import session_transfer as st

    body = _valid(messages=[{"role": "user", "content": "x" * (2 * _MIB) + emoji, "ts": ""}])
    size = len(json.dumps(body).encode())
    budget_mib = (size * st._PARSE_MEMORY_FACTOR * 3 // 2) // _MIB
    _fake_memory(st, monkeypatch, total_mib=budget_mib, available=iter(lambda: budget_mib, None))
    resp = await _run_import(st, monkeypatch, body)

    assert resp.status == status, resp.body


class _StalledContent:
    """``request.content`` stand-in that sends *sent* chunks and then goes quiet."""

    def __init__(self, sent: int = 1) -> None:
        self._sent = sent

    async def iter_chunked(self, _n: int):
        for _ in range(self._sent):
            yield b'{"bundle_version": 2'
        # flake-ok: pre-existing never-ending sender, the condition under test; the reader's own deadline or the test's cancel ends the read, moved verbatim; tracked for a de-flake pass
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [0, 1, 3])
async def test_a_sender_that_goes_quiet_is_cut_off(tmp_path, monkeypatch, sent):
    """No total deadline, but a no-progress one: a stalled upload cannot hold
    its connection and temp file forever, whether it went quiet before its first
    chunk or after some."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "DEFAULT_SESSION_TRANSFER_TIMEOUT_SECS", 0.05)
    task = asyncio.create_task(
        st._stream_request_to_file(SimpleNamespace(content=_StalledContent(sent)), tmp_path / "b")
    )
    done, _ = await asyncio.wait({task}, timeout=5)
    if not done:
        task.cancel()
    assert done, "the stalled read was never cut off"
    assert isinstance(task.exception(), TimeoutError)


@pytest.mark.asyncio
async def test_an_upload_in_flight_holds_no_arrival_permit(tmp_path, monkeypatch):
    """Slow senders must not fill the permits every other import waits on; the
    permit is taken only once the body is on disk."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_expansion_lock", None)
    monkeypatch.setattr(st, "_expansion_slots", None)
    monkeypatch.setattr(st, "_expansion_waiting", 0)
    request = SimpleNamespace(content=_StalledContent())
    async with contextlib.AsyncExitStack() as keep:
        task = asyncio.create_task(st._read_bundle_body(request, keep))
        # flake-ok: pre-existing 50 ms window for the stalled upload to reach its read, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window for the stalled upload to reach its read, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        try:
            assert not task.done()
            assert st._expansion_waiting == 0
            slots = st._expansion_slots
            assert slots is None or slots._value == st._MAX_CONCURRENT_EXPANSIONS
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_uploads_past_the_cap_are_refused_before_a_file_is_opened(monkeypatch):
    """Each in-flight upload holds a staging descriptor before the arrival permit,
    so the number streaming at once is capped and the slot is released after."""
    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_MAX_CONCURRENT_UPLOADS", 2)
    monkeypatch.setattr(st, "_uploads_in_flight", 0)
    opened: list[str] = []
    real_new = st._new_import_temp

    def _counting_new(suffix):
        opened.append(suffix)
        return real_new(suffix)

    monkeypatch.setattr(st, "_new_import_temp", _counting_new)
    async with contextlib.AsyncExitStack() as keep:
        stalled = [
            asyncio.create_task(
                st._read_bundle_body(SimpleNamespace(content=_StalledContent()), keep)
            )
            for _ in range(2)
        ]
        # flake-ok: pre-existing 50 ms window for both stalled uploads to reach their read, moved verbatim; tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window for both stalled uploads to reach their read, moved verbatim; tracked for a de-flake pass
        await asyncio.sleep(0.05)
        try:
            assert st._uploads_in_flight == 2
            body, resp = await st._read_bundle_body(
                SimpleNamespace(content=_StalledContent()), keep
            )
            assert body is None and resp.status == 429
            assert json.loads(resp.body)["code"] == "transfer_uploads_busy"
            assert opened == [".body", ".body"]
        finally:
            for t in stalled:
                t.cancel()
            for t in stalled:
                with contextlib.suppress(asyncio.CancelledError):
                    await t
    assert st._uploads_in_flight == 0


@pytest.mark.asyncio
async def test_a_cgroup_limit_below_the_host_refuses_what_the_pod_cannot_hold(monkeypatch):
    """A gateway under a 4 GiB unit limit on a far larger host: the unit's limit,
    not the host's RAM, decides whether the document can ever fit."""
    from kiro_crew.dashboard import session_transfer as st

    _fake_memory(
        st,
        monkeypatch,
        total_mib=100_000,
        available=iter(lambda: 100_000, None),
        cgroup=(4096 * _MIB, 4096 * _MIB),
    )

    with pytest.raises(st._NeverFits):
        async with st._memory_admission(2048 * _MIB):  # needs 6 GiB
            pass


@pytest.mark.asyncio
async def test_a_full_cgroup_waits_rather_than_failing_open(monkeypatch):
    """No headroom left under the cgroup reads as zero available, which waits;
    it is not the unreadable-host reading that lets everything through."""
    from kiro_crew.dashboard import session_transfer as st

    headroom = iter([0, 0, 4096 * _MIB])
    _fake_memory(st, monkeypatch, total_mib=100_000, available=iter(lambda: 100_000, None))
    monkeypatch.setattr(st, "_cgroup_memory_bounds", lambda: (8192 * _MIB, next(headroom)))

    async with st._memory_admission(1 * _MIB):
        assert next(headroom, None) is None, "admitted before the cgroup had room"


def test_the_cgroup_limit_is_the_tightest_on_the_process_ancestry(tmp_path, monkeypatch):
    """The walk reads every level from the process's cgroup up to the mount and
    keeps the smallest finite limit; ``max`` at a level does not constrain."""
    from kiro_crew import subagent
    from kiro_crew.dashboard import session_transfer as st

    mount = tmp_path / "cg"
    leaf = mount / "system.slice" / "gateway.service"
    leaf.mkdir(parents=True)
    (mount / "memory.max").write_bytes(b"max\n")
    (mount / "system.slice" / "memory.max").write_bytes(b"%d\n" % (8 << 30))
    (leaf / "memory.max").write_bytes(b"%d\n" % (4 << 30))
    monkeypatch.setattr(st.platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(subagent, "_cgroup_memory_roots", lambda: [(leaf, mount, True)])
    monkeypatch.setattr(subagent, "_container_cgroup_available_gb", lambda: 1.5)

    assert st._cgroup_memory_bounds() == (4 << 30, int(1.5 * 1024**3))


def _adversarial_documents():
    """Shapes that stress each term of the parse-memory reservation."""
    n = 60_000
    big = 1 << 21
    return {
        "empty-objects": "[" + ",".join(["{}"] * n) + "]",
        "empty-arrays": "[" + ",".join(["[]"] * n) + "]",
        "small-numbers": "[" + ",".join(["1"] * n) + "]",
        "short-messages": json.dumps(
            {"messages": [{"role": "user", "content": "x"} for _ in range(n // 4)]}
        ),
        "raw-cjk": json.dumps(
            {"messages": [{"role": "user", "content": "中" * big}]}, ensure_ascii=False
        ),
        "raw-emoji": json.dumps(
            {"messages": [{"role": "user", "content": "x" * big + "\U0001f600"}]},
            ensure_ascii=False,
        ),
        "escaped-emoji": json.dumps(
            {"messages": [{"role": "user", "content": "x" * big + "\U0001f600"}]}
        ),
        "escape-heavy": json.dumps(
            {"messages": [{"role": "user", "content": '\\"\n\t' * (big // 4)}]}
        ),
        "code-in-strings": json.dumps(
            {"messages": [{"role": "user", "content": '{"a": [1, 2], "b": {}}, ' * (big // 24)}]}
        ),
    }


@pytest.mark.parametrize("shape", sorted(_adversarial_documents()))
def test_the_reservation_covers_the_measured_parse_peak(tmp_path, shape):
    """The admission reserves from :func:`_measure_document`; the parse it then
    admits must peak under that reservation on every shape, so drift in CPython's
    object sizes or in the estimate fails here rather than in a gateway."""
    import tracemalloc

    from kiro_crew.dashboard import session_transfer as st

    p = tmp_path / "doc.json"
    p.write_text(_adversarial_documents()[shape], encoding="utf-8")
    size, messages, values, factor = st._measure_document(p)
    reserved = size * factor + values * st._PER_VALUE_BYTES + messages * st._PER_MESSAGE_BYTES
    tracemalloc.start()
    try:
        doc = st._load_json_file(p)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    del doc
    assert peak <= reserved, (shape, peak, reserved, peak / size)


@pytest.mark.asyncio
async def test_concurrent_uploads_share_one_disk_headroom(tmp_path, monkeypatch):
    """Arrivals stream outside every permit, so each write counts against the free
    space the others are already cleared to use; the headroom holds however many
    arrive at once."""
    from kiro_crew.dashboard import session_transfer as st

    chunk = st._CHUNK_BYTES
    # Room for exactly two in-flight chunks above the headroom.
    free = st._DISK_HEADROOM_BYTES + 2 * chunk
    monkeypatch.setattr(st.shutil, "disk_usage", lambda _p: SimpleNamespace(free=free))
    monkeypatch.setattr(st, "_disk_inflight", 0)
    target = tmp_path / "body"

    await st._reserve_disk(target, chunk)
    await st._reserve_disk(target, chunk)
    with pytest.raises(st._DiskFull):
        await st._reserve_disk(target, chunk)
    st._release_disk(chunk)
    await st._reserve_disk(target, chunk)
    assert st._disk_inflight == 2 * chunk


@pytest.mark.asyncio
async def test_a_reserved_write_releases_when_the_write_finishes(tmp_path, monkeypatch):
    """The release rides the write's own completion, so abandoning the await
    cannot drop the reservation while the worker is still writing."""
    import threading

    from kiro_crew.dashboard import session_transfer as st

    monkeypatch.setattr(st, "_disk_inflight", 0)
    monkeypatch.setattr(st.shutil, "disk_usage", lambda _p: SimpleNamespace(free=1 << 62))
    gate = threading.Event()

    class _SlowFile:
        def write(self, data):
            gate.wait(5)
            return len(data)

    write = await st._write_reserved(_SlowFile(), b"x" * 10, tmp_path / "body")
    waiter = asyncio.ensure_future(asyncio.shield(write))
    await asyncio.sleep(0)
    waiter.cancel()
    # flake-ok: pre-existing 50 ms window in which the cancelled waiter must not release the reservation, moved verbatim; tracked for a de-flake pass
    # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms window in which the cancelled waiter must not release the reservation, moved verbatim; tracked for a de-flake pass
    await asyncio.sleep(0.05)
    assert st._disk_inflight == 10, "released while the write was still running"
    gate.set()
    # flake-ok: pre-existing await of the reserved write, which ends once the gate is set (the fake's gate.wait is bounded at 5 s), moved verbatim; tracked for a de-flake pass
    await write
    await asyncio.sleep(0)
    assert st._disk_inflight == 0
