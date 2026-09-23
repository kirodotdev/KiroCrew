"""A real process is killed mid-session, and the production reader repairs its file.

Every other crash test in this suite writes the damaged file itself, so it proves
"given this file state, the repair does X" and takes on faith that a real crash
produces that state. These tests remove the faith: a child process drives the real
writer, parks at a named failpoint, is killed with SIGKILL, and the file it leaves
is then opened with the ordinary reader.

The pattern is deliberately event-driven, not timed. The child announces its
failpoint by writing a TERMINAL marker value; the parent waits for that exact
value, so it cannot kill during setup and read the result as a verdict. Nothing
here sleeps for a fixed duration or races a clock.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog

CHILD = Path(__file__).parent / "fixtures" / "crew_log_crash_child.py"
SESSION = "crash-child-session"
#: Generous, and only an upper bound on a failure: the wait returns as soon as the
#: marker holds its terminal value, so a passing run does not spend it.
MARKER_TIMEOUT = 60.0


def _wait_for_marker(marker: Path, expected: str) -> None:
    """Block until *marker* holds exactly *expected*.

    The exact value, not a prefix and not mere existence: a partially written
    marker would otherwise let the kill land while the child was still setting up,
    which turns a real defect into an intermittent one.
    """
    deadline = time.monotonic() + MARKER_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if marker.read_text(encoding="utf-8") == expected:
                return
        except (FileNotFoundError, UnicodeDecodeError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"child never reached failpoint {expected!r}")


def _kill_at(tmp_path: Path, failpoint: str, marker_value: str) -> Path:
    """Run the child to *failpoint*, SIGKILL it, and return the data home."""
    home = tmp_path / "home"
    marker = tmp_path / "marker"
    # A DECLARED environment, not the runner's. Inheriting `os.environ` wholesale
    # makes this child's behaviour depend on whatever else the shard it landed in
    # exported -- a proxy, a TMPDIR, another test's KIROCREW_* -- so the same test
    # can pass in one shard and fail in another for reasons that have nothing to do
    # with what it asserts. A crash fixture has to be reproducible above all else,
    # so it names what it needs. PATH is carried because the interpreter's own
    # startup can need it; nothing else is.
    env = {
        "KIROCREW_HOME": str(home),
        "KIROCREW_CREW_LOG": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PATH": os.environ.get("PATH", ""),
        # Keep the child's own temp files inside the test's directory, for the same
        # reason as `cwd` below.
        "TMPDIR": str(tmp_path),
    }
    if sys.platform == "win32":  # pragma: no cover - the suite skips Windows
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(CHILD), failpoint, str(marker)],
        env=env,
        # Confined to the temp directory: a failpoint added later that writes a
        # relative path would otherwise land in the checkout, and a test that
        # dirties the working tree is a test that fails the next gate run for
        # reasons unrelated to itself.
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_marker(marker, marker_value)
        # SIGKILL, not terminate: no atexit hook, no drain, no chance to tidy up.
        # That is the whole point -- a graceful stop would exercise the shutdown
        # drain instead of a crash.
        child.send_signal(signal.SIGKILL)
        assert child.wait(timeout=30) != 0, "the child exited on its own"
    finally:
        if child.poll() is None:  # pragma: no cover - only on an assertion above
            child.kill()
            child.wait(timeout=30)
    return home


def _entries(home: Path) -> "list[dict]":
    env_home = os.environ.get("KIROCREW_HOME")
    os.environ["KIROCREW_HOME"] = str(home)
    try:
        path = lg.crew_log_path(lg.KIND_SESSION, SESSION)
        raw = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in raw[1:] if line.strip()]
    finally:
        if env_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = env_home


def _repair(home: Path) -> int:
    """Repair via the METHOD, so the returned count is this call's own work.

    Opened WITHOUT ``repair=True`` on purpose: that flag already runs the repair
    during open, which would leave the explicit call below nothing to do and a
    count of zero -- a green test that asserted nothing.
    """
    env_home = os.environ.get("KIROCREW_HOME")
    os.environ["KIROCREW_HOME"] = str(home)
    try:
        return CrewLog.open(lg.KIND_SESSION, SESSION).repair_interrupted_turn()
    finally:
        if env_home is None:
            os.environ.pop("KIROCREW_HOME", None)
        else:
            os.environ["KIROCREW_HOME"] = env_home


# Windows has no SIGKILL with these semantics and no fork-free equivalent that
# leaves the file in the same state; the property is the same on every platform,
# so it is asserted on the ones where the kill means what it says.
#
# macOS is skipped as a BISECT STEP, not because the property differs there. The
# macOS backend shard that runs this file also runs the apps deps-provisioning
# suite, and `test_concurrent_provisioning_is_serialized_by_the_deps_lock` began
# failing on that shard with `ENOENT: .kirocrew-deps.lock` on the first head that
# contained this file, and on every head since, while passing before it and passing
# on other branches. That failure is a race in the provisioning code -- the lock is
# opened through a pinned dir_fd, and ENOENT through a valid fd means the pinned
# directory was unlinked -- and this suite spawns real processes that are killed, so
# the interference is plausible even though the mechanism is not yet proven. Skipping
# here isolates the two: if that shard goes green, the interference is real and both
# the race and this interaction are followed up separately; if it still fails, this
# file was never the cause and the skip is removed.
#
# The darwin half is SCOPED to kirodotdev/KiroCrew#10704, which owns removing it. A
# real kill is asserted nowhere else, so an expiry-free skip would mean a macOS
# regression in crash repair has no test that would catch it.
pytestmark = pytest.mark.skipif(
    sys.platform in ("win32", "darwin"),
    reason=(
        "win32: SIGKILL semantics differ; "
        "darwin: bisecting shard interference, tracked in kirodotdev/KiroCrew#10704"
    ),
)


def test_a_real_kill_leaves_an_open_turn_that_the_reader_closes(tmp_path):
    """The file a killed writer leaves really does have an open turn.

    This is the assumption every hand-written repair test rests on, asserted
    against a process that was actually killed rather than a file written to look
    like one.
    """
    home = _kill_at(tmp_path, "open-turn", "turn-open")

    before = [e["type"] for e in _entries(home)]
    assert "turn/started" in before, "the child's setup never reached the file"
    assert "turn/completed" not in before, "a killed turn should not be closed"

    assert _repair(home) >= 1, "the repair wrote no closer"
    after = _entries(home)
    closers = [e for e in after if e["type"] == "turn/completed"]
    assert len(closers) == 1
    assert closers[0]["data"]["stop_reason"] == "interrupted"
    # Still append-only and still contiguous across the repair.
    seqs = [e["seq"] for e in after]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), f"seq is not contiguous: {seqs}"


def test_a_real_kill_leaves_an_open_tool_call_that_the_reader_closes(tmp_path):
    """A tool call in flight when the process died has no outcome to report.

    The synthesized closer says `unknown` rather than inventing a result, and the
    turn's own closer follows it -- a turn cannot close while a call inside it is
    open.
    """
    home = _kill_at(tmp_path, "open-tool", "tool-open")

    before = [e["type"] for e in _entries(home)]
    assert "tool/called" in before
    assert "tool/completed" not in before

    assert _repair(home) >= 2, "the repair closed the turn without closing its call"
    after = _entries(home)
    tool_closers = [e for e in after if e["type"] == "tool/completed"]
    assert len(tool_closers) == 1
    assert tool_closers[0]["data"]["status"] == "unknown", "an unseen outcome was guessed"
    types = [e["type"] for e in after]
    assert types.index("tool/completed") < types.index("turn/completed")


def test_a_real_kill_between_chunks_and_their_citing_entry_is_repaired(tmp_path):
    """The torn-group case, produced by a kill rather than by an edited file.

    The chunks are on disk and nothing points at them, which is the one shape the
    batched group write cannot rule out. The repair drops them, so the file is left
    without lines whose only purpose was to be cited by an entry that does not
    exist.
    """
    home = _kill_at(tmp_path, "orphan-chunks", "chunks-orphaned")

    before = [e["type"] for e in _entries(home)]
    assert before.count("message/chunk") == 2, "the child did not leave orphan chunks"
    assert "message/sent" not in before, "the citing entry should be absent"

    _repair(home)
    after = [e["type"] for e in _entries(home)]
    assert "message/chunk" not in after, "unreachable chunks survived the repair"
    assert "turn/completed" in after, "the open turn was left open"
