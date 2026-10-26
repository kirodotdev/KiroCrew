# ACP concurrency and session ownership contract (0.2.2)

This file documents the **implemented** synchronization boundaries, not a
promise that arbitrary nested locks or blocking operations are safe.

## Session lifecycle

* `AcpServer.sessions` is the authoritative mapping of *active* Session objects.
  Registry operations and the `Session.replacing`/`Session.retired` flags are
  serialized by `_sessions_lock`.
* A `session/load` / `session/resume` reserves its session ID in
  `_restoring_sessions` before reading persisted data or building runtimes. The
  old session is marked `replacing`; overlapping restores, new prompts, close,
  and delete are rejected with a busy response. On failure this reservation is
  released without cancelling an existing turn.
* `_put_session` acquires the outgoing `turn_lock` **non-blockingly** while
  holding the registry lock. If the turn is active it refuses replacement;
  it never cancels or closes the old runtime. On success it marks the old
  instance `retired` before publishing the replacement and closes old
  resources only while still owning the outgoing `turn_lock`.
* A prompt acquiring a reference just before replacement must check that it
  is still the registered, non-retired, non-replacing object **after** acquiring
  its own `turn_lock`. A stale reference can never start a turn.
* `session/close` and `session/delete` retire the session ID immediately.
  `_retiring_sessions` blocks another load with that ID until the prior turn
  has quiesced, the final persistence/delete action has completed, and MCP/
  native runtimes are closed. After 30 seconds the completion is deferred to
  a daemon reaper: timeout is **never authorization to close an in-use runtime**.

## Lock table and acquisition rules

| Lock | Owner / purpose | Contract |
| --- | --- | --- |
| `_sessions_lock` (RLock) | Registry, lifecycle flags, restore/retirement sets | Do not perform network/filesystem I/O under this lock. `_put_session` has exactly one exceptional **nonblocking** try-acquire of the outgoing `turn_lock`; no waiting is allowed there. |
| `session.turn_lock` | Exclusive logical turn, protects MCP/native against concurrent close | A session writer may acquire `state_lock` beneath it. Runtime closing may briefly acquire `_sessions_lock` to clear a retirement entry; do not introduce a blocking `_sessions_lock → turn_lock` dependency. |
| `session.state_lock` (RLock) | Conversation/steering state | Never wait for turn ownership while holding it. Prefer copying state and performing blocking I/O after release; audit legacy callsites before broad refactors. |
| `_send_lock` | Atomically switches pre-run output to a single writer, serializes nonblocking queue admissions | After writer startup there is no stdout write while holding this lock. `send()` uses `put_nowait` into a bounded queue. **Before `run()` only**, test/early diagnostic sends may write synchronously. |
| `_pending_lock` | Client-request response waiters | Protect map operations only; event waits happen outside the lock. |
| `DurableRunStore._locks_guard` | Registry of per-store locks | Only constructs/retrieves RLocks; durable file I/O uses the distinct per-store lock. |

The `turn_lock -> _sessions_lock` paths are safe with the current registry
implementation because `_sessions_lock -> turn_lock` is nonblocking. Adding a
*blocking* registry-to-turn acquisition creates a deadlock risk and is forbidden.

## ACP outbound contract

* The single outbound writer is started before `run()` accepts an inbound frame.
* The transition from synchronous *pre-run* sends to the writer is serialized
  with `_send_lock`, so no send can be inline after startup and pre-run sends
  cannot be overtaken by queued sends.
* The queue has at most 2048 waiting frames; enqueue never waits under
  `turn_lock` or `state_lock`. Overflow marks the transport failed and raises
  `OutboundTransportError` rather than indefinitely consuming memory.
* A broken stdout is terminal. The writer logs the original exception and
  stops instead of silently draining/dropping the queued frames. Subsequent
  sends fail. This is a fail-stop transport policy, not an at-least-once
  message delivery guarantee: stdio has no application-level ACK.
* A peer that permanently blocks in `stdout.write` can still strand the writer
  thread. The bounded queue prevents unbounded growth; the process must be
  restarted to restore the dead connection.

## Out-of-scope / residual risks

* Existing application-level state handlers may still perform synchronous
  trace/persistence I/O under `state_lock`. This patch does not claim a global
  zero-I/O guarantee or rewrite the durable checkpoint flow.
* Test helpers invoking `handle()` without `run()` intentionally preserve the
  pre-run direct output behavior. Production starts the writer before accepting
  input; pre-run writes must not be used for asynchronous production traffic.
* `session/close` and `session/delete` can acknowledge retirement before a
  stalled active turn has completed; callers retrying load meanwhile receive
  a busy response. The runtime is not force-closed.
