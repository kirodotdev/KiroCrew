"""The gateway's persistent identity: one id per data home, or none at all.

The id exists so a gateway can tell ITSELF from another gateway at the far end of
a forward -- the chain cycle guard's whole question. Two answers for one data home
is therefore the failure that matters more than any of the others here, which is
what most of these cases are about.
"""

from __future__ import annotations

import os
import pathlib
import stat
import threading

import pytest

from kiro_crew import gateway_identity


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Each case starts with an empty process cache.

    The cache is keyed by path and lives for the life of the process, so without
    this a later case reads an earlier case's answer for its own tmp_path only by
    accident of ordering.
    """
    gateway_identity._CACHED_IDS.clear()
    yield
    gateway_identity._CACHED_IDS.clear()


def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_identity, "config_dir", lambda: tmp_path)
    return tmp_path / gateway_identity.GATEWAY_ID_FILE


class TestOneIdPerDataHome:
    def test_it_mints_once_and_reads_the_same_id_back(self, tmp_path, monkeypatch):
        path = _home(tmp_path, monkeypatch)
        first = gateway_identity.gateway_id()
        assert gateway_identity._ID_RE.match(first)
        assert path.read_text(encoding="utf-8").strip() == first
        # A second reader in another process has no cache; it must adopt the
        # persisted id rather than mint its own.
        gateway_identity._CACHED_IDS.clear()
        assert gateway_identity.gateway_id() == first

    def test_a_second_repair_adopts_the_first_instead_of_minting_again(self, tmp_path, monkeypatch):
        """The regression. Two gateways repairing one corrupt file must converge on
        one id: each removing the other's replacement leaves two ids for a single
        data home, and the cycle guard would then be comparing two answers for one
        gateway."""
        path = _home(tmp_path, monkeypatch)
        path.write_text("not-a-gateway-id", encoding="utf-8")

        repaired = gateway_identity.gateway_id()
        assert gateway_identity._ID_RE.match(repaired)

        # The second process: same home, no cache, and the file is now VALID, so
        # the repair path must not run at all.
        gateway_identity._CACHED_IDS.clear()
        assert gateway_identity.gateway_id() == repaired
        assert path.read_text(encoding="utf-8").strip() == repaired

    def test_the_cached_value_is_the_one_on_disk(self, tmp_path, monkeypatch):
        """Caching what this process WROTE rather than what the file holds is how
        two processes end up disagreeing, so the cache is filled from a read."""
        path = _home(tmp_path, monkeypatch)
        minted = gateway_identity.gateway_id()
        assert (
            gateway_identity._CACHED_IDS[str(path)]
            == path.read_text(encoding="utf-8").strip()
            == minted
        )

    @pytest.mark.parametrize(
        "corrupt",
        ["", "   ", "nope", "0123456789abcdef", "Z" * 32, "0" * 33],
    )
    def test_every_malformed_shape_is_replaced(self, tmp_path, monkeypatch, corrupt):
        path = _home(tmp_path, monkeypatch)
        path.write_text(corrupt, encoding="utf-8")
        got = gateway_identity.gateway_id()
        assert gateway_identity._ID_RE.match(got), f"{corrupt!r} was published as an id"
        assert path.read_text(encoding="utf-8").strip() == got


class TestTheReadOnlyMode:
    def test_create_false_reports_absence_without_writing(self, tmp_path, monkeypatch):
        path = _home(tmp_path, monkeypatch)
        assert gateway_identity.gateway_id(create=False) == ""
        assert not path.exists(), "a read-only call minted a file"

    def test_create_false_still_reads_a_valid_id(self, tmp_path, monkeypatch):
        path = _home(tmp_path, monkeypatch)
        minted = gateway_identity.gateway_id()
        gateway_identity._CACHED_IDS.clear()
        assert gateway_identity.gateway_id(create=False) == minted
        assert path.exists()

    def test_create_false_does_not_repair_a_corrupt_file(self, tmp_path, monkeypatch):
        path = _home(tmp_path, monkeypatch)
        path.write_text("corrupt", encoding="utf-8")
        assert gateway_identity.gateway_id(create=False) == ""
        assert path.read_text(encoding="utf-8") == "corrupt", "a read-only call rewrote the file"


class TestItNeverRaises:
    def test_an_unwritable_home_yields_the_process_local_id(self, tmp_path, monkeypatch):
        """Refusing would turn an unwritable directory into a gateway that cannot
        connect a crew at all, and a process-local id still tells two LIVE
        gateways apart, which is the comparison the cycle guard makes."""

        def boom(*_a, **_kw):
            raise OSError("read-only file system")

        _home(tmp_path, monkeypatch)
        monkeypatch.setattr(gateway_identity.platform_compat, "open_lock_file", boom)
        assert gateway_identity.gateway_id() == gateway_identity._IN_MEMORY_ID

    def test_a_directory_where_the_id_belongs_yields_the_fallback(self, tmp_path, monkeypatch):
        path = _home(tmp_path, monkeypatch)
        path.mkdir()
        assert gateway_identity.gateway_id() == gateway_identity._IN_MEMORY_ID


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows ACLs are asserted elsewhere")
class TestItIsNotWorldReadable:
    def test_the_id_file_is_owner_only(self, tmp_path, monkeypatch):
        """It is an identity fingerprint, so another local account must not be able
        to read it out of the data home."""
        path = _home(tmp_path, monkeypatch)
        gateway_identity.gateway_id()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert not mode & stat.S_IRGRP, f"group-readable: {mode:o}"
        assert not mode & stat.S_IROTH, f"world-readable: {mode:o}"


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason=(
        "these cases plant a POSIX symlink and a POSIX FIFO; the Windows counterpart of the "
        "redirect is a reparse point, refused inside platform_compat.open_file_no_reparse and "
        "covered by the structural pin below, which is deliberately NOT skipped here"
    ),
)
class TestThePathIsJudgedAsADescriptorNotAsAName:
    """Checking the path and then opening it resolves the name TWICE.

    The data home is owner-writable, so between the two answers the object at that
    path can change, and it is the second answer that gets read. Each case here is
    a different consequence of that, and they fail for different reasons -- a test
    covering only the link leaves the one that costs the gateway its capacity
    unproven.
    """

    def test_a_symlink_at_the_path_is_not_followed(self, tmp_path, monkeypatch):
        """Stated as "the target's contents do not come back", not "the answer is
        empty": an empty answer has other causes, while returning the target's id
        could only mean the link was followed."""
        path = _home(tmp_path, monkeypatch)
        elsewhere = tmp_path / "elsewhere.txt"
        planted = "0123456789abcdef" * 2
        elsewhere.write_text(planted, encoding="utf-8")
        assert gateway_identity._ID_RE.match(planted), "the plant must look like a real id"
        path.symlink_to(elsewhere)

        assert gateway_identity._read_id(path) != planted, "followed a symlink out of the data home"
        assert gateway_identity._read_id(path) == "", "a link is not a usable id file"
        # And through the public entry point, which is what /api/health reaches.
        assert gateway_identity.gateway_id(create=False) != planted

    def test_a_fifo_at_the_path_does_not_hold_the_worker(self, tmp_path, monkeypatch):
        """The half that costs capacity rather than correctness. Opening a FIFO with
        no writer blocks until one appears, and this read runs in
        ``asyncio.to_thread`` on the SHARED default executor -- so a held worker is
        the gateway losing capacity until restart. Run in a thread with a join
        deadline so a regression FAILS here instead of hanging the whole suite.
        """
        path = _home(tmp_path, monkeypatch)
        os.mkfifo(path)
        assert stat.S_ISFIFO(path.lstat().st_mode), "fixture did not create a FIFO"

        out: list[str] = []
        worker = threading.Thread(target=lambda: out.append(gateway_identity._read_id(path)))
        worker.daemon = True  # never let a regression keep the interpreter alive
        worker.start()
        worker.join(timeout=10)

        assert not worker.is_alive(), "the open blocked on a FIFO with no writer"
        assert out == [""], f"a FIFO is not a usable id file: {out}"

    def test_a_swap_at_the_moment_of_the_check_is_not_read_through(self, tmp_path, monkeypatch):
        """THE finding, and the only case here that a path-then-open read fails.

        A statically planted link or FIFO is refused by an ``lstat`` too, so those
        cases say nothing about which shape the read has -- what separates them is
        whether anything can change BETWEEN the check and the open. So this test is
        the attacker: it swaps the real file for a link the instant the path is
        stat-ed, which is exactly the window a two-resolution read leaves open. The
        new shape never calls ``lstat`` at all, so the swap never fires and the real
        id comes back; the old one would stat a regular file and then open the link.
        """
        path = _home(tmp_path, monkeypatch)
        real = gateway_identity.gateway_id()
        assert gateway_identity._ID_RE.match(real)
        gateway_identity._CACHED_IDS.clear()

        elsewhere = tmp_path / "attacker.txt"
        planted = "0123456789abcdef" * 2
        elsewhere.write_text(planted, encoding="utf-8")
        assert planted != real, "the plant has to be distinguishable from the real id"

        original_lstat = pathlib.Path.lstat
        swapped: list[str] = []

        def lstat_then_swap(self, *args, **kwargs):
            st = original_lstat(self, *args, **kwargs)
            if str(self) == str(path) and not swapped:
                # Whoever stat-ed this path is about to resolve the name a second
                # time. Give the second resolution something else to find.
                swapped.append("yes")
                path.unlink()
                path.symlink_to(elsewhere)
            return st

        monkeypatch.setattr(pathlib.Path, "lstat", lstat_then_swap)

        got = gateway_identity._read_id(path)
        assert got != planted, (
            "read the attacker's file: the path was resolved twice and the second "
            "resolution found a symlink"
        )
        assert got == real, f"expected the id on the descriptor it opened, got {got!r}"

    def test_a_fifo_with_a_writer_cannot_supply_the_identity(self, tmp_path, monkeypatch):
        """The FIFO shape that costs correctness rather than capacity, and the one
        the descriptor's own ``S_ISREG`` is the only guard against.

        With no writer the read fails on its own and refuses by accident. With a
        writer feeding well-formed bytes it succeeds -- so without judging what the
        descriptor actually names, an attacker who can plant a FIFO in the data home
        chooses this gateway's identity, and it is the identity two gateways compare
        to refuse a cycle.
        """
        path = _home(tmp_path, monkeypatch)
        os.mkfifo(path)
        planted = "0123456789abcdef" * 2
        assert gateway_identity._ID_RE.match(planted), "the plant must look like a real id"

        stop = threading.Event()

        def feed():
            # Opening for write blocks until a reader arrives, so this thread is
            # the writer the refused open must not be waiting for.
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(planted)
            except OSError:
                pass
            finally:
                stop.set()

        writer = threading.Thread(target=feed)
        writer.daemon = True
        writer.start()

        out: list[str] = []
        reader = threading.Thread(target=lambda: out.append(gateway_identity._read_id(path)))
        reader.daemon = True
        reader.start()
        reader.join(timeout=10)

        assert not reader.is_alive(), "the read blocked on a FIFO"
        assert out and out[0] != planted, f"took its identity from a pipe: {out}"
        assert out == [""], f"a FIFO is not a usable id file: {out}"
        stop.wait(timeout=2)

    def test_a_refused_path_stays_cheap_when_it_is_read_again(self, tmp_path, monkeypatch):
        """A rejected file is deliberately NOT cached, so ``/api/health`` re-enters
        this read on every poll. That repetition is only safe because each attempt
        refuses on its own descriptor: nothing is followed and nothing waits. Ten
        attempts against a FIFO, each inside the deadline, is what says so.
        """
        path = _home(tmp_path, monkeypatch)
        os.mkfifo(path)

        results: list[str] = []

        def read_ten():
            for _ in range(10):
                results.append(gateway_identity._read_id(path))

        worker = threading.Thread(target=read_ten)
        worker.daemon = True
        worker.start()
        worker.join(timeout=10)

        assert not worker.is_alive(), "a repeated read blocked, so the re-read IS the window"
        assert results == [""] * 10, f"expected ten bounded refusals, got {results}"
        assert str(path) not in gateway_identity._CACHED_IDS, "cached a refusal as an identity"


class TestTheOpenGoesThroughTheSharedNoReparsePrimitive:
    """Deliberately NOT skipped anywhere: it is the only case that runs on Windows.

    The POSIX cases above plant a symlink and a FIFO, so they cannot run there --
    which is exactly why the guarantee for that platform has to be stated over the
    code. ``getattr(os, "O_NOFOLLOW", 0)`` is **0** on Windows, so a hand-rolled
    ``os.open`` with that flag refuses a link on POSIX and silently follows a
    reparse point on Windows while reading as if it refused both.
    ``platform_compat.open_file_no_reparse`` is the form that refuses at the final
    name on either platform, and this module's read must go through it.
    """

    def test_it_calls_the_primitive_and_never_the_raw_flag(self):
        """A regression to the raw flag is invisible on a POSIX-only test run."""
        import ast
        import inspect

        src = inspect.getsource(gateway_identity._read_id)
        fn = ast.parse(src).body[0]
        names = [
            ast.unparse(n.func)
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]

        assert "platform_compat.open_file_no_reparse" in names, (
            "the read does not borrow the shared no-reparse opener, so whatever it does "
            f"instead is unproven on Windows: {names}"
        )
        assert "os.open" not in names, (
            "opened the path directly: on Windows that follows a reparse point at the "
            "final name, which is the no-op catalogued in issue 9731"
        )
        # The flag spelling itself, not just the call: a caller could pass it onward.
        assert "O_NOFOLLOW" not in src, "names O_NOFOLLOW directly instead of delegating"
        # A second resolution of the same name is the original defect, in any spelling.
        for forbidden in ("path.lstat", "path.open", "path.stat", "path.read_bytes"):
            assert forbidden not in names, f"{forbidden} resolves the path a second time"

    def test_the_regular_file_check_is_ours_and_precedes_the_read(self):
        """The primitive does NOT refuse a FIFO -- ``nonblocking`` only stops the open
        from waiting for one, as its own docstring says -- so this check is this
        module's own and is what rejects a pipe whose writer wins the race to supply
        bytes. It has to run before a byte is read, or those bytes are already in hand.
        """
        import ast
        import inspect

        fn = ast.parse(inspect.getsource(gateway_identity._read_id)).body[0]
        guard = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "S_ISREG"
        ]
        judged = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.fstat"
        ]
        read = [
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.read"
        ]
        assert judged, "nothing judges the descriptor the primitive returned"
        assert guard, "the descriptor is never checked for being a regular file"
        assert read, "nothing reads the descriptor"
        assert min(guard) < min(read), "the regular-file check runs AFTER the read"

    def test_the_primitive_still_declines_to_wait(self):
        """``nonblocking=True`` is not decoration: without it the open of a FIFO waits
        for a writer, and this read runs on the shared default executor."""
        import ast
        import inspect

        fn = ast.parse(inspect.getsource(gateway_identity._read_id)).body[0]
        borrows = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and ast.unparse(n.func) == "platform_compat.open_file_no_reparse"
        ]
        assert borrows, "no borrow of the primitive to inspect"
        for call in borrows:
            kw = {k.arg: k.value for k in call.keywords}
            assert "nonblocking" in kw, "borrowed the primitive without nonblocking"
            assert (
                getattr(kw["nonblocking"], "value", None) is True
            ), "nonblocking is not literally True, so a FIFO open can wait for a writer"
