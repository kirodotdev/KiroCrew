"""Startup warm of the SecurityEventLog singleton (#8608).

``SecurityEventLog._init_locked`` runs on whatever thread first calls
``sel()``; after init, a non-critical ``log_api_access`` only enqueues to the
writer thread. Before #8608 that first touch was dodged per call site — 18+
``asyncio.to_thread`` wrappers across the dashboard handlers — while 250+
other ``log_api_access`` sites remained candidate first-touch stalls. The
cause-level fix warms the singleton once, off the loop, in BOTH async server
start paths, before the middleware chain is built and before the socket
binds.

These tests pin the three halves of that contract:

* the warm helper genuinely initializes the singleton OFF the event loop
  (real ``SecurityEventLog``, fresh directory, recorded ``_init_locked``
  thread) — this is the migrated #8523 first-touch property, now asserted at
  the startup warm instead of at a per-site hop;
* a failed warm never raises out of the helper (an SEL init error must not
  keep the gateway from becoming ready);
* both ``start_dashboard`` and ``start_api_server`` await the warm before
  publishing readiness (source guard, same convention as
  ``test_token_auth.test_start_paths_warm_auth_singletons_off_loop``).
"""

from __future__ import annotations

import asyncio
import threading

from kiro_crew import sel as sel_mod


def test_warm_sel_singleton_initializes_off_the_event_loop(tmp_path, monkeypatch):
    """The warm is SEL's first touch and ``_init_locked`` runs off-loop.

    Uses a real ``SecurityEventLog`` against a fresh directory so the
    first-touch work (HMAC key create, chain-head read) genuinely runs, and
    records the thread ``_init_locked`` executes on.

    Mutation guard: replacing ``await asyncio.to_thread(sel)`` with a bare
    ``sel()`` in the helper turns the recorded ident into the loop's and
    fails this.
    """
    sel_dir = tmp_path / "sel-home"
    init_record: dict = {}
    real_init = sel_mod.SecurityEventLog._init_locked

    def _recording_init(inst, base_dir, sync):
        init_record["thread_ident"] = threading.get_ident()
        return real_init(inst, base_dir, sync)

    monkeypatch.setattr(sel_mod.SecurityEventLog, "_init_locked", _recording_init)
    # sync=True keeps appends inline on their caller's thread (no background
    # writer to leak); the FIRST-TOUCH construction below is still real.
    monkeypatch.setattr(
        sel_mod, "sel", lambda: sel_mod.SecurityEventLog(base_dir=sel_dir, sync=True)
    )
    # Displace-then-RESTORE the process singleton (the ``sel_private_root``
    # fixture is unusable here: it pre-builds the instance, which would consume
    # the very first touch this test needs). The reset is immediately followed
    # by the try so a failure can never lose ``prior_instance``.
    prior_instance = sel_mod.SecurityEventLog._instance
    sel_mod.SecurityEventLog._instance = None
    sel_mod.SecurityEventLog._initialized = False
    try:

        async def _drive() -> int:
            await sel_mod.warm_sel_singleton()
            return threading.get_ident()

        loop_ident = asyncio.run(_drive())

        # The warm happened HERE (singleton was reset above) …
        assert init_record.get("thread_ident") is not None, "the warm never initialized SEL"
        # … did its filesystem initialization for real …
        assert (sel_dir / "trust" / "sel_hmac.key").is_file()
        # … and never on the event loop.
        assert init_record["thread_ident"] != loop_ident
    finally:
        # Restore the PRIOR singleton (not ``None``): leaving ``None`` would
        # make the next default ``sel()`` mint a SECOND instance — and a
        # second writer thread — on the worker's session-shared directory.
        sel_mod.SecurityEventLog._instance = prior_instance
        sel_mod.SecurityEventLog._initialized = False


def test_warm_sel_singleton_swallows_init_failure(monkeypatch, caplog):
    """A failed warm logs and returns — it must never block gateway readiness.

    Both start paths await this helper before ``state.ready = True``; an
    exception escaping it would turn an SEL init error (e.g. an unwritable
    trust dir) into a gateway that never comes up. The per-action semantics
    are unchanged by a failed warm: the first later touch retries init on its
    caller's thread, and every ``critical=True`` audit still fails closed at
    its own site.

    Mutation guard: removing the helper's ``except Exception`` fails this.
    """

    def _boom():
        raise RuntimeError("trust root too short to sign the chain")

    monkeypatch.setattr(sel_mod, "sel", _boom)

    with caplog.at_level("WARNING", logger=sel_mod.logger.name):
        asyncio.run(sel_mod.warm_sel_singleton())  # must not raise

    assert any("SEL startup warm failed" in r.message for r in caplog.records)


def test_start_paths_warm_sel_singleton_before_ready() -> None:
    """Source guard: both async server startup paths must await
    ``warm_sel_singleton()`` BEFORE publishing readiness, so no handler's
    first ``log_api_access`` can be the singleton's on-loop first touch.

    Position matters, not just presence: a warm awaited after
    ``state.ready = True`` (or after the middleware chain is serving) would
    race the very first request. The helper itself must offload via
    ``asyncio.to_thread`` — a bare ``sel()`` inside an ``async def`` would
    run the blocking init on the loop.
    """
    import inspect

    from kiro_crew.dashboard import server as _srv

    helper_src = inspect.getsource(sel_mod.warm_sel_singleton)
    assert (
        "asyncio.to_thread(sel)" in helper_src
    ), "warm_sel_singleton no longer offloads the first touch off the loop"
    assert "except Exception" in helper_src, (
        "warm_sel_singleton is no longer best-effort; a failed warm would "
        "keep the gateway from becoming ready"
    )

    for fn in (_srv.start_dashboard, _srv.start_api_server):
        src = inspect.getsource(fn)
        warm_at = src.find("await warm_sel_singleton()")
        assert warm_at != -1, f"{fn.__name__} must await warm_sel_singleton()"
        ready_at = src.find("state.ready = True")
        assert ready_at != -1, f"{fn.__name__} lost its readiness publication?"
        assert warm_at < ready_at, (
            f"{fn.__name__} warms SEL only after readiness is published; the "
            "first request can then beat the warm and init on the loop"
        )
        chain_at = src.find("app.middlewares[:]")
        assert chain_at == -1 or warm_at < chain_at, (
            f"{fn.__name__} builds the middleware chain before the SEL warm; "
            "a deny-audit middleware's first refusal could then be the "
            "singleton's on-loop first touch"
        )


def test_writer_unavailable_fallback_never_writes_on_the_event_loop(tmp_path, monkeypatch):
    """When the writer can't start, the non-critical fallback is loop-aware.

    ``log()``'s enqueue path falls back to a synchronous ``_flush_batch`` when
    ``_ensure_writer()`` raises (thread/resource exhaustion). With the per-site
    ``to_thread`` wrappers gone (#8608), that fallback is now reachable ON the
    event loop — where its redaction + chain lock + open/write would freeze
    every task the loop serves. So on the loop the event is dropped with a
    warning; off the loop the synchronous write still lands the entry. Either
    way the pending credit taken before the failed enqueue is returned, so
    ``flush()`` cannot wait on a count nothing will decrement.

    Mutation guards: removing the ``_on_event_loop()`` drop branch fails the
    on-loop half; making the drop unconditional fails the off-loop half.
    """
    sel_dir = tmp_path / "sel-home"
    prior_instance = sel_mod.SecurityEventLog._instance
    sel_mod.SecurityEventLog._instance = None
    sel_mod.SecurityEventLog._initialized = False
    try:
        inst = sel_mod.SecurityEventLog(base_dir=sel_dir, sync=False)
        flushed: list[list] = []
        monkeypatch.setattr(inst, "_flush_batch", lambda batch, **kw: flushed.append(batch))

        def _no_writer(self=None):
            raise RuntimeError("cannot start sel-writer")

        monkeypatch.setattr(inst, "_ensure_writer", _no_writer)

        # ON the loop: dropped, not written, and no pending credit leaks.
        async def _on_loop():
            inst.log_api_access(caller="t", operation="op", outcome="ok")

        asyncio.run(_on_loop())
        assert flushed == [], "the fallback wrote synchronously on the event loop"
        assert inst._pending == 0, "a dropped event leaked its pending credit"

        # OFF the loop (plain thread, no running loop): written synchronously.
        inst.log_api_access(caller="t", operation="op", outcome="ok")
        assert len(flushed) == 1, "the off-loop fallback no longer lands the entry"
        assert inst._pending == 0
    finally:
        sel_mod.SecurityEventLog._instance = prior_instance
        sel_mod.SecurityEventLog._initialized = False


def test_sel_is_warm_tracks_the_singleton_state(monkeypatch):
    """The gate ``_audit_denied`` asks before choosing enqueue-vs-hop: false
    with no instance, false with an instance whose init never completed (a
    failed warm), true once initialized."""
    prior_instance = sel_mod.SecurityEventLog._instance
    try:
        sel_mod.SecurityEventLog._instance = None
        assert sel_mod.sel_is_warm() is False

        inst = object.__new__(sel_mod.SecurityEventLog)
        inst._initialized = False
        sel_mod.SecurityEventLog._instance = inst
        assert sel_mod.sel_is_warm() is False, "an allocated-but-uninitialized instance is not warm"

        inst._initialized = True
        assert sel_mod.sel_is_warm() is True
    finally:
        sel_mod.SecurityEventLog._instance = prior_instance


def test_audit_denied_hops_off_the_loop_only_when_the_warm_failed(monkeypatch):
    """The deny path is reached by every refused request. With a warmed
    singleton it must stay a direct enqueue (#8608: no per-call hop). With a
    FAILED warm the next ``sel()`` runs ``_init_locked`` -- blocking file I/O --
    on the caller's thread, and the caller here is the event loop: that case
    must take the hop. Both branches are driven through the real helper with
    ``sel()`` and ``asyncio.to_thread`` replaced by recorders."""
    from types import SimpleNamespace

    from kiro_crew.dashboard import server as server_mod

    calls: list[str] = []

    class _Log:
        def log_api_access(self, **kw):
            calls.append(f"write:{threading.get_ident() == loop_ident}")

    async def _fake_to_thread(fn, *a, **kw):
        calls.append("hop")
        return fn(*a, **kw)

    monkeypatch.setattr(server_mod, "sel", lambda: _Log())
    monkeypatch.setattr(server_mod.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(server_mod, "mark_audit_claimed", lambda request: None)
    request = SimpleNamespace(method="POST", path="/api/x")
    loop_ident = threading.get_ident()

    # Warm succeeded: enqueue inline, no hop.
    monkeypatch.setattr(server_mod, "sel_is_warm", lambda: True)
    asyncio.run(server_mod._audit_denied("caller", request, "denied"))
    assert calls == ["write:True"], calls

    # Warm failed: hop, then write inside the hop.
    calls.clear()
    monkeypatch.setattr(server_mod, "sel_is_warm", lambda: False)
    asyncio.run(server_mod._audit_denied("caller", request, "denied"))
    assert calls == ["hop", "write:True"], calls  # recorder runs inline; the ORDER is the property

    # Best-effort on both paths: a raising write is swallowed, never a 500.
    class _Boom:
        def log_api_access(self, **kw):
            raise RuntimeError("trust root too short")

    monkeypatch.setattr(server_mod, "sel", lambda: _Boom())
    for warm in (True, False):
        monkeypatch.setattr(server_mod, "sel_is_warm", lambda warm=warm: warm)
        asyncio.run(server_mod._audit_denied("caller", request, "denied"))  # must not raise
