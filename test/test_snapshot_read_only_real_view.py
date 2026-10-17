"""``kirocrew snapshot`` inside the product's own sandbox, where ``memory_stores/`` is sealed.

The standard sandbox seals the stores tree: on Linux the namespace launcher binds it over
itself and remounts that bind read-only, and on macOS the Seatbelt profile denies every
write under it. Each test here runs the reader as a child under that real sandbox,
``namespace_argv`` or ``sandbox_exec_argv`` with the standard tier, while the test process
plays the gateway and writes through its own view. The child first proves its view of
the stores refuses a write, then runs ``snapshot_main``.

What runs where: macOS runs every test, since Seatbelt needs no privilege. Linux needs
unprivileged user and mount namespaces, which the namespace-sandbox CI job provides and
the shard runners do not, so there the sandboxed tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from test_snapshot import _setup_fake_kirocrew, unpinnable_argv

from kiro_crew import member_memory_backup, platform_compat, sandbox, sandbox_plan
from kiro_crew.memory_stores import MEMORY_DB_FILE, MEMORY_STORES_DIR_NAME
from kiro_crew.sandbox import userns_available

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="the sandbox backends are POSIX-only"
)

real_view = pytest.mark.skipif(
    not (sys.platform == "darwin" or userns_available()),
    reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
    "namespaces)",
)

STORE = "acme"
ROWS = 3
# A writer's row batch: enough rows that every batch spans several pages.
BATCH = 40
REFUSED_WRITE = {"linux": "EROFS", "darwin": "EPERM"}


@pytest.fixture(autouse=True)
def _no_gateway(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    # The sandbox plans its seals from the resolved data home, so the home is resolved
    # here too, and every path the tests compare is spelled the way the plan spells it.
    home = (tmp_path / "home").resolve()
    home.mkdir()
    _setup_fake_kirocrew(home)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


def _store_db(home: Path, *, journal_mode: str = "wal") -> Path:
    """A named store with ROWS committed rows and no connection holding it open."""
    db = home / MEMORY_STORES_DIR_NAME / STORE / MEMORY_DB_FILE
    db.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == journal_mode
        conn.execute("CREATE TABLE semantic_memory (key TEXT PRIMARY KEY, pad TEXT)")
        conn.executemany(
            "INSERT INTO semantic_memory VALUES (?, ?)", [(f"k{i}", "p" * 400) for i in range(ROWS)]
        )
        conn.commit()
    with member_memory_backup.hold_stores_for_read(home / MEMORY_STORES_DIR_NAME):
        pass
    assert sorted(p.name for p in db.parent.iterdir()) == [MEMORY_DB_FILE]
    return db


def _tree(root: Path) -> list[str]:
    """Every name under *root*, so a file the reader created shows up as a difference."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


# The reader. It refuses to run against any home but the test's own, proves that its
# view of the stores refuses a write, and then runs the snapshot command. With "pause",
# the store database's copy stops after its first page until a line arrives on stdin.
_READER = r"""
import errno, json, os, sys

home, stores, out, pause = sys.argv[1:5]
extra = sys.argv[5:]
if os.environ.get("KIROCREW_HOME") != home:
    sys.exit(97)
probe = os.path.join(stores, ".write-probe")
try:
    os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
except OSError as e:
    refused = errno.errorcode.get(e.errno, str(e.errno))
else:
    os.unlink(probe)
    refused = None
rdonly = bool(os.statvfs(stores).f_flag & os.ST_RDONLY)
print("VIEW " + json.dumps({"refused": refused, "rdonly": rdonly}), flush=True)
if refused is None:
    sys.exit(98)

import sqlite3
from kiro_crew import snapshot as snap

if pause == "pause":
    store_db = os.path.join(stores, "acme", "memory.db")

    class _Connection:
        def __init__(self, conn, database):
            self._conn = conn
            self._pause = store_db in database

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def backup(self, target, *args, **kwargs):
            target = getattr(target, "_conn", target)
            if not self._pause:
                return self._conn.backup(target, *args, **kwargs)
            pending = [True]

            def step(_status, remaining, _total):
                if remaining and pending:
                    pending.pop()
                    print("PAUSED", flush=True)
                    sys.stdin.readline()

            kwargs.update(pages=1, progress=step)
            return self._conn.backup(target, *args, **kwargs)

    class _Driver:
        def __getattr__(self, name):
            return getattr(sqlite3, name)

        def connect(self, database, *args, **kwargs):
            return _Connection(sqlite3.connect(database, *args, **kwargs), str(database))

    snap.sqlite3 = _Driver()

sys.exit(snap.snapshot_main([out, "--components", "memory", *extra]))
"""


def _sandboxed(argv: list[str]) -> tuple[list[str], str | None]:
    """*argv* wrapped in the standard sandbox this platform's backend builds."""
    if sys.platform == "darwin":
        return sandbox.sandbox_exec_argv(argv, "standard")
    return sandbox.namespace_argv(argv, "standard"), None


class Reader:
    """The snapshot command, run as a child inside the standard sandbox.

    Spawn one through the ``readers`` fixture, which closes every reader at teardown, so
    a test that fails while a reader is paused still reaps the child and its profile.
    """

    def __init__(self, home: Path, out: Path, *, pause: bool) -> None:
        stores = home / MEMORY_STORES_DIR_NAME
        argv = [sys.executable, "-c", _READER, str(home), str(stores), str(out)]
        argv += ["pause" if pause else "run", *unpinnable_argv()]
        wrapped, self._cleanup = _sandboxed(argv)
        self.lines: list[str] = []
        try:
            self.child = subprocess.Popen(
                wrapped,
                cwd=home,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                env=dict(os.environ),
            )
        except BaseException:
            self._remove_profile()
            raise

    def until(self, marker: str) -> None:
        """Read the child's output up to the line *marker*; fail if it exits first."""
        for line in self.child.stdout:
            self.lines.append(line.rstrip("\n"))
            if self.lines[-1] == marker:
                return
        pytest.fail(f"the reader exited before {marker!r}:\n" + self.output)

    def finish(self) -> int:
        """Release a paused copy, wait for the child, and return its exit code."""
        try:
            out, _ = self.child.communicate("\n", timeout=120)
            self.lines += out.splitlines()
        finally:
            self.close()
        return self.child.returncode

    def close(self) -> None:
        """Stop the child if it still runs, close its pipes, and remove its profile."""
        if self.child.poll() is None:
            self.child.kill()
            self.child.wait(timeout=60)
        for stream in (self.child.stdin, self.child.stdout):
            if stream is not None and not stream.closed:
                stream.close()
        self._remove_profile()

    def _remove_profile(self) -> None:
        if self._cleanup and os.path.lexists(self._cleanup):
            os.unlink(self._cleanup)

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def view(self) -> dict:
        (line,) = [ln for ln in self.lines if ln.startswith("VIEW ")]
        return json.loads(line[len("VIEW ") :])


@pytest.fixture
def readers(home) -> Iterator[Callable[..., Reader]]:
    spawned: list[Reader] = []

    def spawn(out: Path, *, pause: bool = False) -> Reader:
        reader = Reader(home, out, pause=pause)
        spawned.append(reader)
        return reader

    yield spawn
    for reader in spawned:
        reader.close()


def _run_reader(readers: Callable[..., Reader], out: Path) -> Reader:
    reader = readers(out)
    reader.finish()
    return reader


def _assert_sealed(reader: Reader) -> None:
    """The child's own view refused the write the sandbox exists to refuse."""
    view = reader.view()
    assert view["refused"] == REFUSED_WRITE[sys.platform], reader.output
    if sys.platform == "linux":
        # The seal is a read-only mount, which the reader confirms before it tries a write.
        assert view["rdonly"], reader.output


# The gateway's writer: WAL, no automatic checkpoint, driven a command at a time. Its
# busy timeout is zero, so a statement the reader would block fails at once and names
# SQLite's refusal instead of waiting it out.
_WRITER = r"""
import sqlite3, sys

db, batch = sys.argv[1], int(sys.argv[2])
conn = sqlite3.connect(db, isolation_level=None, timeout=0)
conn.execute("PRAGMA wal_autocheckpoint=0")

def rows(prefix):
    return [(f"{prefix}{i}", "p" * 400) for i in range(batch)]

print("open", flush=True)
for line in sys.stdin:
    cmd, _, prefix = line.strip().partition(" ")
    try:
        if cmd == "commit":
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("INSERT INTO semantic_memory VALUES (?, ?)", rows(prefix))
            conn.execute("COMMIT")
        elif cmd == "hold":
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("INSERT INTO semantic_memory VALUES (?, ?)", rows(prefix))
        elif cmd == "rollback":
            conn.execute("ROLLBACK")
        elif cmd == "close":
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
            conn.close()
            print("closed", busy, flush=True)
            break
    except sqlite3.Error as e:
        print("error", e, flush=True)
        continue
    print("done", flush=True)
"""


class Writer:
    """Another process writing the store through a writable view, as the gateway does."""

    def __init__(self, db: Path) -> None:
        self.child = subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(db), str(BATCH)],
            cwd=db.parent,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert self.child.stdout.readline().strip() == "open"

    def send(self, command: str) -> None:
        """Run *command*; fail unless the writer finished it without waiting on a lock."""
        self.child.stdin.write(command + "\n")
        self.child.stdin.flush()
        reply = self.child.stdout.readline().strip()
        assert reply == "done", f"{command!r} answered {reply!r}"

    def close(self) -> int:
        """Checkpoint, close, and return the checkpoint's busy flag."""
        self.child.stdin.write("close\n")
        self.child.stdin.flush()
        reply = self.child.stdout.readline().split()
        self.child.stdin.close()
        self.child.wait(timeout=60)
        self.child.stdout.close()
        assert self.child.returncode == 0
        assert reply and reply[0] == "closed", reply
        return int(reply[1])


@contextmanager
def _writer(db: Path) -> Iterator[Writer]:
    writer = Writer(db)
    try:
        yield writer
    finally:
        if writer.child.poll() is None:
            writer.child.kill()
            writer.child.wait(timeout=60)
        for stream in (writer.child.stdin, writer.child.stdout):
            if not stream.closed:
                stream.close()


def _archive(out: Path, tmp_path: Path) -> tuple[str, list[str]]:
    """The archived store's integrity verdict and its keys."""
    (tarball,) = out.glob("kirocrew-snapshot-*.tar.gz")
    extract = tmp_path / "extract"
    with tarfile.open(tarball) as tar:
        tar.extractall(extract, filter="data")
    (root,) = extract.iterdir()
    db = root / MEMORY_STORES_DIR_NAME / STORE / MEMORY_DB_FILE
    with closing(sqlite3.connect(db)) as conn:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        keys = [k for (k,) in conn.execute("SELECT key FROM semantic_memory ORDER BY key")]
    return verdict, keys


def _keys(*prefixes: str) -> list[str]:
    found = [f"k{i}" for i in range(ROWS)] if "k" in prefixes else []
    for prefix in prefixes:
        if prefix != "k":
            found += [f"{prefix}{i}" for i in range(BATCH)]
    return sorted(found)


def _live(db: Path) -> tuple[str, list[str]]:
    """The live store's integrity verdict and keys, read through the writable view.

    A read-write connection is the last one to close, so it removes the -wal and -shm
    its open created; a read-only one would leave both behind.
    """
    with closing(sqlite3.connect(db)) as conn:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        keys = [k for (k,) in conn.execute("SELECT key FROM semantic_memory ORDER BY key")]
    return verdict, keys


@pytest.mark.parametrize(
    "backend",
    [
        pytest.param(
            sandbox_plan.BACKEND_NAMESPACE,
            marks=pytest.mark.skipif(
                sys.platform != "linux", reason="the namespace launcher is Linux-only"
            ),
        ),
        sandbox_plan.BACKEND_SEATBELT,
    ],
)
def test_the_standard_sandbox_seals_the_stores_read_only(home, backend):
    plan = sandbox._spawn_plan(backend, "standard")
    assert str(home / MEMORY_STORES_DIR_NAME) in plan.readonly


@real_view
def test_a_snapshot_inside_the_sandbox_carries_a_closed_store(home, tmp_path, readers):
    stores = home / MEMORY_STORES_DIR_NAME
    db = _store_db(home)
    before = _tree(stores)

    reader = _run_reader(readers, tmp_path / "out")

    assert reader.child.returncode == 0, reader.output
    _assert_sealed(reader)
    assert _archive(tmp_path / "out", tmp_path) == ("ok", _keys("k"))
    # No lock file, -wal or -shm: the reader made nothing under the stores.
    assert _tree(stores) == before
    assert _live(db) == ("ok", _keys("k"))
    assert _tree(stores) == before


@real_view
def test_a_store_a_live_writer_holds_is_snapshotted_from_inside_the_sandbox(
    home, tmp_path, readers
):
    """The gateway holds the store in WAL with a write transaction open across the copy.

    The writer commits a batch while the copy is paused after its first page, and holds a
    write transaction open on both sides of the pause. Its busy timeout is zero, so each
    of those statements finishing at all shows the reader never held it up. The archive
    must be one committed state, with none of the rows a transaction held uncommitted.
    """
    stores = home / MEMORY_STORES_DIR_NAME
    db = _store_db(home)
    initial = _tree(stores)
    with _writer(db) as writer:
        writer.send("commit w")
        writer.send("hold v")
        before = _tree(stores)
        reader = readers(tmp_path / "out", pause=True)
        reader.until("PAUSED")
        writer.send("rollback")
        writer.send("commit u")
        writer.send("hold x")
        rc = reader.finish()
        during = _tree(stores)
        writer.send("rollback")
        busy = writer.close()

    assert rc == 0, reader.output
    _assert_sealed(reader)
    verdict, keys = _archive(tmp_path / "out", tmp_path)
    assert verdict == "ok"
    # The copy restarts when the source changes under it, so either committed state may
    # be the one it took; the rolled-back batches are in neither.
    assert keys in (_keys("k", "w"), _keys("k", "w", "u")), keys
    # Once the reader was gone the writer checkpointed the whole log.
    assert busy == 0
    # The -wal and -shm are the writer's; the reader added nothing beside them, and the
    # writer's close removed both.
    assert during == before
    assert _tree(stores) == initial
    assert _live(db) == ("ok", _keys("k", "w", "u"))


@real_view
def test_a_writer_arriving_during_the_reader_lock_copy_is_not_blocked(home, tmp_path, readers):
    """A closed store is copied immutably under the reader lock; a writer opens it mid-copy.

    The writer is not blocked: with a zero busy timeout its commit lands while the copy is
    paused. Its log now sits beside the database, so the copy is refused instead of being
    kept, and a second run once the writer has closed carries every committed row.
    """
    stores = home / MEMORY_STORES_DIR_NAME
    db = _store_db(home)
    before = _tree(stores)
    reader = readers(tmp_path / "out", pause=True)
    reader.until("PAUSED")
    # The reader is inside its copy, and has created nothing to get there.
    assert _tree(stores) == before
    with _writer(db) as writer:
        writer.send("commit u")
        writer.send("hold v")
        rc = reader.finish()
        writer.send("rollback")
        busy = writer.close()

    assert rc == 1, reader.output
    _assert_sealed(reader)
    assert "changed while it was being copied" in reader.output
    assert not list((tmp_path / "out").glob("kirocrew-snapshot-*"))
    assert busy == 0
    # The writer's close removed its -wal and -shm, and the reader left nothing behind.
    assert _tree(stores) == before
    assert _live(db) == ("ok", _keys("k", "u"))

    again = _run_reader(readers, tmp_path / "out")

    assert again.child.returncode == 0, again.output
    assert _archive(tmp_path / "out", tmp_path) == ("ok", _keys("k", "u"))
    assert _tree(stores) == before


@real_view
def test_writers_still_refuse_inside_the_sandbox(home, tmp_path):
    """Only the read path takes the read-only fallback; a store write still fails loudly."""
    stores = home / MEMORY_STORES_DIR_NAME
    _store_db(home)
    before = _tree(stores)
    program = (
        "import sys\n"
        "from pathlib import Path\n"
        "from kiro_crew import member_memory_backup\n"
        "try:\n"
        "    with member_memory_backup.hold_stores_for_replace(Path(sys.argv[1]), [sys.argv[2]]):\n"
        "        pass\n"
        "except OSError as e:\n"
        "    import errno\n"
        "    print(errno.errorcode.get(e.errno, e.errno), flush=True)\n"
        "    sys.exit(3)\n"
    )
    wrapped, cleanup = _sandboxed([sys.executable, "-c", program, str(stores), STORE])
    try:
        result = subprocess.run(
            wrapped,
            cwd=home,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=dict(os.environ),
            timeout=120,
        )
    finally:
        if cleanup:
            os.unlink(cleanup)

    assert result.returncode == 3, result.stdout + result.stderr
    assert result.stdout.strip() == REFUSED_WRITE[sys.platform]
    assert _tree(stores) == before
