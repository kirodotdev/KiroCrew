"""Build gate + tests: the Slack persist path stays off the event loop (#1699),
and a slot orders its rows against foreign on-disk rows (#1689).

## Why a gate and not just tests

``save_conversation_turn`` makes TWO ``ConversationLog.append`` calls, and append
is not cheap: ~11.8 ms on a 1.6 MB / 400-row transcript, dominated by the flock
acquire and the write path, and it scales with transcript size because rotation
reads and rewrites the whole file. Called from an ``async def`` without
offloading, that is ~24 ms per turn of event-loop time no other coroutine can
use.

Worse than the latency: ``_locked`` makes only ONE non-blocking acquire when it
detects a running loop and raises ``HistoryLockTimeout`` if it fails, so an
on-loop caller **drops the durable write** exactly when another writer is
active — and most of these call sites swallow that in a best-effort ``except``.
Off the loop there is no running loop in the worker thread, so ``_locked`` takes
the patient poll-to-deadline path instead.

``history.py`` already warns (with ``stack_info``) when append runs on the loop,
but a warning is only as good as someone reading logs. This gate is the
deterministic form: a NEW Slack call site cannot persist a turn on the loop
without failing the build.

The offloaded form passes the gate for free. ``asyncio.to_thread(save_conversation_turn, …)``
passes the function as a bare ``Name`` — it is not a *call* at that point — so
only the on-loop shape is ever flagged. A ``# loop-ok: <reason>`` trailing
comment suppresses a line that is genuinely safe; every suppression must state
its reason so the exception is auditable and greppable.
"""

from __future__ import annotations

import ast
import asyncio
import io
import pathlib
import tokenize
from datetime import datetime
from unittest.mock import patch

import pytest

from kiro_crew.history import latest_transcript_ts, monotonic_transcript_ts, transcript_sort_key

_BANNED_FUNC = "save_conversation_turn"
_SUPPRESS = "loop-ok"

# A nested def/lambda is a different execution frame (a sync helper, a thread
# target, an offloaded callable); a nested async def is walked on its own.
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


# ── STRUCTURAL tier ───────────────────────────────────────────────────────────


def _src_root() -> pathlib.Path:
    """Locate the kiro_crew source tree (import-first, repo-path fallback)."""
    try:
        import kiro_crew  # noqa: PLC0415

        return pathlib.Path(kiro_crew.__file__).resolve().parent
    except Exception:
        return pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"


def _bound_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """How a module might name the persist helper.

    Returns ``(direct, modules)``:
      * ``direct`` -- locals bound by ``from ... import save_conversation_turn``,
        ``as``-renames included, so a bare ``sct(...)`` still resolves.
      * ``modules`` -- locals bound to the module that DEFINES it, so the
        attribute form ``llm_helpers.save_conversation_turn(...)`` resolves too.

    Both forms are needed or the gate is one import away from being evaded, which
    would make it worse than no gate: it would report green while the defect it
    exists to prevent walked straight past it.
    """
    direct: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("llm_helpers"):
                    modules.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == _BANNED_FUNC:
                    direct.add(alias.asname or alias.name)
                elif alias.name == "llm_helpers":
                    modules.add(alias.asname or alias.name)
    return direct, modules


def _suppressed_lines(source: str) -> set[int]:
    """Lines carrying a genuine ``# loop-ok`` COMMENT, not a substring.

    Tokenizing rather than substring-scanning means a ``loop-ok`` inside a string
    literal does NOT suppress, while a real trailing comment does.
    """
    out: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT and _SUPPRESS in tok.string:
                out.add(tok.start[0])
    except (tokenize.TokenError, IndentationError):
        pass
    return out


def _scope_calls(node: ast.AST):
    """Yield Call nodes reachable from *node* without crossing a nested scope."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _NESTED_SCOPES):
            continue
        if isinstance(child, ast.Call):
            yield child
        yield from _scope_calls(child)


def find_violations(source: str, path: str = "<source>") -> list[tuple[str, int]]:
    """Return ``(path, lineno)`` for on-loop ``save_conversation_turn`` calls."""
    tree = ast.parse(source)
    direct, modules = _bound_names(tree)
    if not (direct or modules):
        return []
    suppressed = _suppressed_lines(source)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in _scope_calls(node):
            func = call.func
            hit = (isinstance(func, ast.Name) and func.id in direct) or (
                isinstance(func, ast.Attribute)
                and func.attr == _BANNED_FUNC
                and isinstance(func.value, ast.Name)
                and func.value.id in modules
            )
            if not hit:
                continue
            span = range(call.lineno, (call.end_lineno or call.lineno) + 1)
            if any(ln in suppressed for ln in span):
                continue
            out.append((path, call.lineno))
    return out


def collect_repo_violations() -> list[tuple[str, int]]:
    """Scan every ``kiro_crew/**/*.py`` for an on-loop turn persist."""
    root = _src_root()
    base = root.parent
    out: list[tuple[str, int]] = []
    for py in sorted(root.rglob("*.py")):
        try:
            src = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        try:
            rel = str(py.relative_to(base))
        except ValueError:  # pragma: no cover - defensive
            rel = str(py)
        try:
            out.extend(find_violations(src, rel))
        except SyntaxError:  # pragma: no cover - defensive
            continue
    return out


def test_no_turn_is_persisted_on_the_event_loop() -> None:
    """save_conversation_turn must never be CALLED inside an async def body."""
    violations = collect_repo_violations()
    if violations:
        detail = "\n".join(f"  {path}:{lineno}" for path, lineno in violations)
        raise AssertionError(
            "save_conversation_turn called directly inside an `async def` body.\n\n"
            "It makes two ConversationLog.append calls (~24ms of loop time per "
            "turn on a large transcript), and on the loop `_locked` makes a "
            "single non-blocking acquire and raises HistoryLockTimeout under "
            "contention — so the durable write is DROPPED exactly when another "
            "writer is active. Offload it:\n"
            "    await asyncio.to_thread(save_conversation_turn, log, key, ...)\n"
            "If an on-loop call is genuinely correct, add a trailing "
            "'# loop-ok: <reason>' comment.\n\n"
            f"{detail}"
        )


# ── Meta-tests: prove the detector fires and stays quiet ─────────────────────


def test_detector_flags_an_on_loop_call() -> None:
    src = (
        "from kiro_crew.llm_helpers import save_conversation_turn\n"
        "async def f(log, k):\n"
        "    save_conversation_turn(log, k, 'a', 'b')\n"
    )
    assert [v[1] for v in find_violations(src)] == [3]


def test_detector_flags_an_aliased_on_loop_call() -> None:
    """A one-import rename must not defeat the gate."""
    src = (
        "from kiro_crew.llm_helpers import save_conversation_turn as sct\n"
        "async def f(log, k):\n"
        "    sct(log, k, 'a', 'b')\n"
    )
    assert [v[1] for v in find_violations(src)] == [3]


def test_detector_flags_the_attribute_form() -> None:
    """Importing the MODULE must not be a way around the gate."""
    src = (
        "from kiro_crew import llm_helpers\n"
        "async def f(log, k):\n"
        "    llm_helpers.save_conversation_turn(log, k, 'a', 'b')\n"
    )
    assert [v[1] for v in find_violations(src)] == [3]


def test_detector_flags_an_aliased_module_attribute_call() -> None:
    src = (
        "import kiro_crew.llm_helpers as lh\n"
        "async def f(log, k):\n"
        "    lh.save_conversation_turn(log, k, 'a', 'b')\n"
    )
    assert [v[1] for v in find_violations(src)] == [3]


def test_detector_ignores_a_same_named_method_on_another_object() -> None:
    """An unrelated object that happens to share the name must not trip it."""
    src = (
        "from kiro_crew import llm_helpers\n"
        "async def f(store, log, k):\n"
        "    store.save_conversation_turn(log, k, 'a', 'b')\n"
    )
    assert find_violations(src) == []


def test_detector_accepts_the_offloaded_form() -> None:
    """to_thread receives the function as a bare Name, so it is not a call."""
    src = (
        "import asyncio\n"
        "from kiro_crew.llm_helpers import save_conversation_turn\n"
        "async def f(log, k):\n"
        "    await asyncio.to_thread(save_conversation_turn, log, k, 'a', 'b')\n"
    )
    assert find_violations(src) == []


def test_detector_ignores_a_sync_caller() -> None:
    """A sync function is not on the loop; only async bodies are in scope."""
    src = (
        "from kiro_crew.llm_helpers import save_conversation_turn\n"
        "def f(log, k):\n"
        "    save_conversation_turn(log, k, 'a', 'b')\n"
    )
    assert find_violations(src) == []


def test_detector_ignores_a_nested_sync_helper_inside_an_async_body() -> None:
    """A nested def is a separate frame — typically the thread target itself."""
    src = (
        "from kiro_crew.llm_helpers import save_conversation_turn\n"
        "async def f(log, k):\n"
        "    def _do():\n"
        "        save_conversation_turn(log, k, 'a', 'b')\n"
        "    return _do\n"
    )
    assert find_violations(src) == []


def test_loop_ok_comment_suppresses() -> None:
    src = (
        "from kiro_crew.llm_helpers import save_conversation_turn\n"
        "async def f(log, k):\n"
        "    save_conversation_turn(log, k, 'a', 'b')  # loop-ok: empty test log\n"
    )
    assert find_violations(src) == []


def test_loop_ok_inside_a_string_does_not_suppress() -> None:
    src = (
        "from kiro_crew.llm_helpers import save_conversation_turn\n"
        "async def f(log, k):\n"
        '    save_conversation_turn(log, k, "loop-ok", "b")\n'
    )
    assert [v[1] for v in find_violations(src)] == [3]


def test_every_slack_persist_site_goes_through_the_choke_point() -> None:
    """The converse of the gate: the Slack modules DO still persist turns.

    Without this, deleting every call site would make the gate pass vacuously.
    Asserted against the choke point rather than a raw ``to_thread`` count,
    because collapsing the eleven identical offload hunks into
    ``save_conversation_turn_off_loop`` is the point -- a new async caller now
    inherits the offload instead of restating it.
    """
    root = _src_root()
    sites = 0
    for name in ("handler.py", "gateway.py", "transport_dispatch.py"):
        tree = ast.parse((root / "slack" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "save_conversation_turn_off_loop"
            ):
                sites += 1
    assert sites >= 11, f"expected the known Slack persist sites, found {sites}"


def test_the_choke_point_offloads_and_is_awaitable() -> None:
    """The choke point must actually offload, and callers must be able to await it.

    A synchronous helper would satisfy the AST gate (no direct call in an async
    body) while still running the write on the loop -- the gate would then be
    theatre. And callers go on to read the transcript back, so the write has to be
    awaited rather than fired at the executor.
    """
    import inspect

    from kiro_crew.llm_helpers import save_conversation_turn_off_loop

    assert inspect.iscoroutinefunction(save_conversation_turn_off_loop)
    src = inspect.getsource(save_conversation_turn_off_loop)
    assert "to_thread" in src, "the choke point does not offload the write"
    assert "await" in src, "the choke point does not await the write"


@pytest.mark.asyncio
async def test_two_concurrent_turns_do_not_interleave(tmp_path) -> None:
    """A turn is a PAIR, and offloading made the pair breakable.

    ``append`` locks per ROW. On the event loop that was harmless: a synchronous
    ``save_conversation_turn`` never yields between its two appends, so the pair
    was effectively atomic. Dispatching it to worker threads makes two concurrent
    turns for the same session genuinely interleavable into
    ``user_A, user_B, assistant_A, assistant_B`` -- turns that no longer pair up,
    which no timestamp ordering can repair because every row's ``ts`` is
    individually correct.

    The barrier makes this DETERMINISTIC in both directions rather than a race:
    each writer pauses after its user row and waits for the other to reach the
    same point. Holding the turn lock, the second writer cannot get that far, so
    the wait times out and the pairs stay intact. WITHOUT the lock both writers
    arrive, the barrier trips, and the rows interleave every time.
    """
    import threading

    from kiro_crew.history import ConversationLog
    from kiro_crew.llm_helpers import save_conversation_turn_off_loop

    log = ConversationLog(tmp_path / "history")
    key = "s1"
    both_wrote_user = threading.Barrier(2, timeout=1.0)
    real_append = ConversationLog.append

    def _append_then_pause(self, k, role, content, **kw):
        real_append(self, k, role, content, **kw)
        if role == "user":
            try:
                both_wrote_user.wait()
            except threading.BrokenBarrierError:
                pass  # the turn lock kept the other writer out -- the pass case

    with patch.object(ConversationLog, "append", _append_then_pause):
        await asyncio.gather(
            save_conversation_turn_off_loop(log, key, "u1", "a1"),
            save_conversation_turn_off_loop(log, key, "u2", "a2"),
        )

    rows = [(r["role"], r["content"]) for r in log.read_messages(key)]
    assert len(rows) == 4, f"expected four rows, got {rows}"
    for user_row, assistant_row in (rows[0:2], rows[2:4]):
        assert user_row[0] == "user" and assistant_row[0] == "assistant", (
            f"turns interleaved: {rows}"
        )
        assert user_row[1][1:] == assistant_row[1][1:], (
            f"an assistant row was paired with the wrong user row: {rows}"
        )


# ── latest_transcript_ts: the shared floor combiner ──────────────────────────


class TestLatestTranscriptTs:
    def test_it_picks_the_later_candidate(self) -> None:
        early = "2026-08-06T04:00:00.000000+00:00"
        late = "2026-08-06T04:00:00.000001+00:00"
        assert latest_transcript_ts(early, late) == late
        assert latest_transcript_ts(late, early) == late

    def test_it_ignores_missing_candidates(self) -> None:
        ts = "2026-08-06T04:00:00.000000+00:00"
        assert latest_transcript_ts(None, ts, "") == ts
        assert latest_transcript_ts(None, None) is None
        assert latest_transcript_ts() is None

    def test_it_compares_the_two_stored_formats_in_one_domain(self) -> None:
        """Rows carry both aware and naive isoformat, so string order is wrong.

        A naive local timestamp can sort AFTER an aware UTC one as raw strings
        while being earlier in real time. Comparing through transcript_sort_key
        is what makes the floor correct rather than lexicographic.
        """
        aware = "2026-08-06T04:00:00.000000+00:00"
        naive_later = datetime.now().replace(microsecond=0).isoformat()
        picked = latest_transcript_ts(aware, naive_later)
        assert transcript_sort_key(picked) == max(
            transcript_sort_key(aware), transcript_sort_key(naive_later)
        )


# ── #1689: a slot orders against a foreign on-disk row ───────────────────────
#
# The behavioural coverage for that lives in test_transcript_row_ordering.py,
# which owns transcript ordering and already carries the colliding-clock
# simulator these cases need (class TestTheDashboardWriterAndAForeignRow).


def test_the_stamper_still_advances_on_a_stalled_clock() -> None:
    """Guard the primitive the floor feeds, so a floor bug cannot hide here."""
    instant = "2026-08-06T04:00:00.000000+00:00"
    stamped = monotonic_transcript_ts(instant, datetime.fromisoformat(instant))
    assert transcript_sort_key(stamped) > transcript_sort_key(instant)


@pytest.mark.parametrize("bad", ["", "not-a-timestamp", "2026-13-45T99:99:99"])
def test_a_corrupt_ts_is_skipped_not_ranked(bad: str) -> None:
    """An unparseable candidate must never win, or the floor switches OFF.

    transcript_sort_key buckets what it cannot parse AFTER every real instant, so
    ranking candidates by it would let one corrupt row beat a valid timestamp.
    monotonic_transcript_ts then ignores the unparseable floor it was handed and
    emits the bare clock reading -- which on a coarse-tick host ties the row it
    was supposed to sort after. One bad line on disk would disable the guarantee
    for the whole session.

    The earlier version of this test asserted ``in (good, bad)``, which passed
    for exactly the broken behaviour it was meant to catch.
    """
    good = "2026-08-06T04:00:00.000000+00:00"
    assert latest_transcript_ts(bad, good) == good
    assert latest_transcript_ts(good, bad) == good


@pytest.mark.parametrize("bad", ["not-a-timestamp", "2026-13-45T99:99:99"])
def test_all_candidates_corrupt_yields_no_floor(bad: str) -> None:
    """No usable floor means None -- never a bogus value the stamper will drop."""
    assert latest_transcript_ts(bad, bad) is None
