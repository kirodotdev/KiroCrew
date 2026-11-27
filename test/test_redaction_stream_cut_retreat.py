"""A cut retreat ends for a hit decided from text the stream already committed (its
anchor below the window base): the cookie pair hit's anchor on its pair name makes that
state reachable. Each case runs in one module-scoped spawned worker child, so a retreat
that never ends fails by name at the deadline instead of hanging the run."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from kiro_crew.security.redaction_structured import StreamRedactor


def _hit_anchored_below(base: int) -> object:
    from kiro_crew.security.redaction_structured.model import SECRET, Hit

    return Hit(base + 5, base + 100, "http.session_cookie", SECRET, "cookie", "known_key", anchor=0)


def _anchored_below_scenario(case: str, ctx: str, buf: str) -> object:
    # Runs in the child: the patch the scenario needs is applied there, from plain
    # data, so the child can be spawned on every platform.
    from kiro_crew.security.redaction_structured import compat, stream
    from kiro_crew.security.redaction_structured.engine import ScanResult

    hit = _hit_anchored_below(len(ctx))
    if case == "compat":
        compat._read = lambda text, final=True: compat._Read(  # noqa: SLF001
            ctx + text, [hit], [hit], None, len(ctx), None
        )
        read = compat.stream_read(ctx, buf, boundary=False)
        return (read.cut, bool(read.undecided))
    stream.scan = lambda w, final, origin, **_: ScanResult([hit], None)
    stream.resolve = lambda hits: hits
    s = StreamRedactor()
    s._ctx = ctx  # noqa: SLF001
    return s.feed(buf)


def _serve(requests: object, replies: object, log: str) -> None:
    # The module's one worker child: it runs each scenario request in turn and puts an
    # ``("ok", value)`` or ``("error", text)`` record back. The attributes a scenario
    # patches are put back after each request, so one request's patch never reaches the
    # next. Its stderr goes to *log*, so a parent that finds it dead can quote the tail.
    import os
    import sys
    import traceback

    from kiro_crew.security.redaction_structured import compat, stream

    err = open(log, "a", buffering=1)  # noqa: SIM115 - held for the child's lifetime
    os.dup2(err.fileno(), 2)
    sys.stderr = err
    patched = [(compat, "_read"), (stream, "scan"), (stream, "resolve")]
    while True:
        request = requests.get()  # type: ignore[attr-defined]
        if request is None:
            return
        target, args = request
        saved = [getattr(mod, name) for mod, name in patched]
        try:
            replies.put(("ok", target(*args)))  # type: ignore[attr-defined]
        except BaseException as exc:  # noqa: BLE001 - reported to the test, not swallowed
            tail = "".join(traceback.format_exception(exc)[-6:])
            replies.put(("error", f"{type(exc).__name__}: {exc}\n{tail}"))  # type: ignore[attr-defined]
        finally:
            for (mod, name), value in zip(patched, saved):
                setattr(mod, name, value)


class _Worker:
    def __init__(self, log: str) -> None:
        import multiprocessing

        ctx = multiprocessing.get_context("spawn")
        self.log = log
        self.requests, self.replies = ctx.Queue(), ctx.Queue()
        self.child = ctx.Process(
            target=_serve, args=(self.requests, self.replies, log), daemon=True
        )
        self.child.start()

    def _state(self) -> str:
        code = self.child.exitcode
        return "alive" if code is None else f"exit code {code}"

    def _stderr_tail(self) -> str:
        try:
            with open(self.log, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-10:])
        except OSError as exc:
            return f"(no stderr log: {exc})"

    def run(self, target: Callable[..., object], *args: object) -> object:
        import queue
        import time

        from kiro_crew.testing.wait import default_timeout

        # A module-level target and plain-data arguments, so the spawned child takes
        # them on every platform. The module fixture owns the one child: a dead child
        # fails the test at once, and no replacement starts mid-module.
        name = f"{target.__name__}{args!r}"
        if not self.child.is_alive():
            pytest.fail(
                f"scenario worker is dead before {name} ({self._state()});"
                f" last stderr:\n{self._stderr_tail()}"
            )
        deadline = default_timeout()
        started = time.monotonic()
        self.requests.put((target, args))
        try:
            kind, value = self.replies.get(timeout=default_timeout())
        except queue.Empty:
            elapsed = time.monotonic() - started
            state = self._state()
            self.child.kill()
            self.child.join(deadline)
            pytest.fail(
                f"{name} did not return in {elapsed:.1f}s (deadline {deadline:.1f}s,"
                f" child {state}); last stderr:\n{self._stderr_tail()}"
            )
        if kind == "error":
            pytest.fail(f"{name} raised in the scenario worker: {value}")
        return value

    def close(self) -> None:
        from kiro_crew.testing.wait import default_timeout

        if self.child.is_alive():
            self.requests.put(None)
            self.child.join(default_timeout())
        if self.child.is_alive():
            self.child.kill()
            self.child.join(default_timeout())
        assert not self.child.is_alive(), "the scenario worker did not stop"


@pytest.fixture(scope="module")
def scenario_worker(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Worker]:
    # One interpreter child for the module, not one per test.
    worker = _Worker(str(tmp_path_factory.mktemp("scenario_worker") / "stderr.log"))
    try:
        yield worker
    finally:
        worker.close()


_SCENARIO_GROUP = pytest.mark.xdist_group(name="redaction_stream_scenario_worker")


@_SCENARIO_GROUP
def test_the_cut_retreat_ends_for_a_hit_anchored_in_emitted_text(
    scenario_worker: _Worker,
) -> None:
    # A hit decided from text already emitted (anchor below the window's base)
    # cannot pull the cut back past the base: the retreat stops and holds.
    ctx, buf = "Cookie: theme=" + "a" * 30, "x" * 5 + "V" * 95 + "y" * 20
    assert scenario_worker.run(_anchored_below_scenario, "compat", ctx, buf) == (0, True)


@_SCENARIO_GROUP
def test_the_structured_stream_cut_retreat_ends_for_a_hit_anchored_in_committed_text(
    scenario_worker: _Worker,
) -> None:
    ctx, buf = "Cookie: theme=" + "a" * 30, "x" * 5 + "V" * 95 + "y" * 20
    assert scenario_worker.run(_anchored_below_scenario, "stream", ctx, buf) == ""
