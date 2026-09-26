"""The lent hop port is owned by the OS, not merely skipped by our allocator.

Every case here binds from a SEPARATE PROCESS on purpose. A test that asserts the
allocator skips the port re-pins the defect being fixed: the allocator exclusion was
already there and already passed that assertion, while any other process on the host
-- including a second gateway with its own registry, which cannot see the lease at all
-- could still take the port and be handed the chained crew's bearer credential.
"""

from __future__ import annotations

import errno
import socket
import subprocess
import sys
import time

import pytest

from kiro_crew.instances.hop_port_guard import HopPortGuard
from kiro_crew.subprocess_utf8 import UTF8_TEXT

# Binds the way the real competitors do: OpenSSH's forward listener and this repo's
# own availability probe both set SO_REUSEADDR, so a hold that only refuses a plain
# bind would not refuse either of them.
_BINDER = """
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", port))
except OSError as e:
    print("REFUSED", e.errno)
else:
    print("BOUND")
finally:
    s.close()
"""


def _bind_from_another_process(port: int) -> str:
    out = subprocess.run(
        [sys.executable, "-c", _BINDER, str(port)],
        capture_output=True,
        timeout=60,
        # Pinned rather than inherited: text mode without this decodes with the
        # Windows ANSI code page, and nothing about reading "BOUND" wants that.
        **UTF8_TEXT,
    )
    assert out.returncode == 0, f"binder crashed: {out.stderr}"
    return out.stdout.strip()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _squat(port: int = 0) -> socket.socket:
    """A LISTENING socket on *port*, standing in for one of our own leaked forwarders.

    Listening, not merely bound, because that is what a real forwarder does and it is
    the shape the hold has to lose to.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    return s


def _hold_a_port(guard: HopPortGuard, until: float) -> int:
    """A held port, retrying past a collision rather than failing on one.

    Asking the kernel for an ephemeral port and then closing it before use is a race
    against every other process on the host, this suite included: the number is only a
    hint by the time the hold tries to bind it. One unlucky draw would otherwise read as
    'the hold does not work', which is the one thing these cases must report truthfully,
    so a collision is retried and only a run of them fails.
    """
    for _ in range(40):
        port = _free_port()
        if guard.hold(port, until):
            return port
    raise AssertionError("could not take any port after 40 attempts")


@pytest.fixture
def guard():
    g = HopPortGuard()
    try:
        yield g
    finally:
        g.close_all()


class TestAnotherProcessCannotTakeAHeldPort:
    def test_a_second_process_is_refused_while_the_lease_stands(self, guard):
        """The whole finding in one assertion, and it is about another PROCESS.

        Stated as a BEFORE and an AFTER on the same port, so 'refused' cannot be passed
        by a port that was never bindable in the first place.
        """
        probe = _free_port()
        assert _bind_from_another_process(probe) == "BOUND", "port was not free to start"

        port = _hold_a_port(guard, time.time() + 300)
        assert guard.held_ports() == {port}

        result = _bind_from_another_process(port)
        assert result.startswith(
            "REFUSED"
        ), f"another process bound a lent hop port whose lease still stands: {result}"
        # The two platforms refuse with different CONDITIONS, not merely different
        # numbers for one condition. POSIX reports EADDRINUSE because a listening socket
        # owns the address. Windows reports EACCES -- measured, 13 -- because
        # SO_EXCLUSIVEADDRUSE makes a conflicting bind a PERMISSION denial rather than an
        # address-in-use; WSAEADDRINUSE is accepted there too, since a Winsock build may
        # surface the in-use code instead and both are REFUSALS.
        #
        # This is not a set that swallows anything: the property under test is that the
        # bind was refused, and "BOUND" -- the outcome that would mean the port was not
        # held -- is rejected by the assertion above and is not in this set. Do NOT
        # "correct" EACCES to an in-use code here; on Windows it is the refusal.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            accepted = {errno.EACCES, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}
        else:
            accepted = {errno.EADDRINUSE}
        assert int(result.split()[1]) in accepted, (
            f"refused, but not the way this platform refuses an owned port: got "
            f"{result.split()[1]}, expected one of {sorted(accepted)}"
        )

    def test_the_port_is_free_again_once_the_lease_lapses(self, guard):
        """A hold that outlived its lease would squat a port that is legitimately
        free, so the release is as load-bearing as the hold."""
        # Already lapsed: the reaper's own sweep must drop it without anyone asking.
        port = _hold_a_port(guard, time.time() - 1)

        deadline = time.time() + 20
        while time.time() < deadline and guard.held_ports():
            time.sleep(0.2)

        assert guard.held_ports() == set(), "a lapsed lease still holds its port"
        assert (
            _bind_from_another_process(port) == "BOUND"
        ), "the port stayed unbindable after its lease lapsed"

    def test_release_frees_it_for_a_legitimate_reconnect(self, guard):
        """The crew comes back and needs a port to bind: our own hold must not be what
        refuses it."""
        port = _hold_a_port(guard, time.time() + 300)
        assert _bind_from_another_process(port).startswith("REFUSED")

        guard.release(port)

        assert _bind_from_another_process(port) == "BOUND", "release did not free the port"

    def test_a_repeated_failure_does_not_re_log_at_error(self, guard, caplog):
        """The retry must not bury every other error in the gateway.

        The retry runs on each reaper pass, so a port another binder holds would emit
        ~2 ERROR records a second for the whole lease -- of the order of 10^5 identical
        lines on a 20h TTL. That does not make the exposure more visible; it evicts
        every other error from a size-capped sink. ERROR once per port, again only when
        the reason changes, DEBUG thereafter, with `owed_ports()` as the durable signal.
        """
        import logging

        squatter = _squat()
        port = squatter.getsockname()[1]
        try:
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.instances.hop_port_guard"):
                assert guard.hold(port, time.time() + 300) is False
                first = [r for r in caplog.records if r.levelno == logging.ERROR]
                assert len(first) == 1, f"expected one ERROR for the first failure: {first}"

                caplog.clear()
                for _ in range(5):
                    assert guard.hold(port, time.time() + 300) is False
                again = [r for r in caplog.records if r.levelno == logging.ERROR]
                assert again == [], f"re-logged at ERROR for the same reason: {again}"
                assert [
                    r for r in caplog.records if r.levelno == logging.DEBUG
                ], "the repeats are not logged at all, so a live exposure goes silent"

            # The durable signal is the owed set, not the log volume.
            assert guard.owed_ports() == {port}
        finally:
            squatter.close()

    def test_a_hold_that_failed_is_retried_until_it_is_taken(self, guard):
        """The path behind the one usually named: a hold that failed ONCE.

        A lease whose bind failed and is never retried is a live lease with no socket --
        the same exposure, reached without any orphan or reclaim. The usual cause is one
        of our own leaked forwarders still occupying the port at startup, so the retry
        is what closes it the moment that process goes, rather than waiting for an
        unrelated crew's teardown to call `sync` again.
        """
        squatter = _squat()
        port = squatter.getsockname()[1]

        assert guard.hold(port, time.time() + 300) is False, "bound an occupied port"
        assert guard.owed_ports() == {port}, "a failed hold was forgotten"
        assert guard.held_ports() == set()

        squatter.close()  # the orphan goes

        deadline = time.time() + 20
        while time.time() < deadline and port not in guard.held_ports():
            time.sleep(0.2)

        assert port in guard.held_ports(), "the owed port was never retaken"
        assert guard.owed_ports() == set(), "still owed after it was taken"
        assert _bind_from_another_process(port).startswith("REFUSED")

    def test_an_owed_port_is_forgotten_once_its_lease_lapses(self, guard):
        """Owing is bounded by the credential, not retried forever."""
        squatter = _squat()
        port = squatter.getsockname()[1]
        try:
            assert guard.hold(port, time.time() + 1.5) is False
            assert guard.owed_ports() == {port}

            deadline = time.time() + 20
            while time.time() < deadline and guard.owed_ports():
                time.sleep(0.2)

            assert guard.owed_ports() == set(), "kept owing a port whose lease lapsed"
            assert guard.held_ports() == set()
        finally:
            squatter.close()

    def test_the_platform_option_is_the_exclusive_one_not_the_permissive_one(self):
        """Structural, because the option's MEANING inverts between platforms.

        On POSIX ``SO_REUSEADDR`` on a listening socket refuses a second binder; on
        Windows it INVITES one to steal an active listener, and only
        ``SO_EXCLUSIVEADDRUSE`` makes the bind exclusive. An earlier revision of this
        module set ``SO_REUSEADDR`` unconditionally and therefore held nothing at all on
        Windows while its docstring claimed it did -- caught by CI, not by any case here,
        because these cases cannot run on that platform. Pinned over the code so the
        branch cannot be flattened back into one unconditional option.
        """
        import ast
        import inspect
        import textwrap

        from kiro_crew.instances import hop_port_guard

        src = textwrap.dedent(inspect.getsource(hop_port_guard.HopPortGuard.hold))
        tree = ast.parse(src)

        def options(nodes) -> set[str]:
            found = set()
            for stmt in nodes:
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Attribute) and n.attr.startswith("SO_"):
                        found.add(n.attr)
            return found

        # Find the branch that chooses the option, and check the CONDITION, not just
        # that both names appear somewhere. Asserting only presence passed a mutation
        # that flattened the test to a constant: both options were still in the source,
        # one of them simply unreachable, and the hold held nothing on Windows again.
        branches = [
            n for n in ast.walk(tree) if isinstance(n, ast.If) and "SO_REUSEADDR" in options(n.body)
        ]
        assert branches, "nothing branches on the platform to choose the option"
        branch = branches[0]
        cond = ast.unparse(branch.test)
        assert "IS_POSIX" in cond, (
            f"the option is not chosen by PLATFORM but by {cond!r}; on Windows "
            "SO_REUSEADDR lets another local process steal an ACTIVE listener, so a "
            "branch that does not test the platform holds nothing there"
        )
        assert "SO_EXCLUSIVEADDRUSE" in options(branch.orelse), (
            "no Windows alternative: the exclusive option must be what the non-POSIX "
            f"path sets, found {options(branch.orelse)}"
        )
        assert "SO_EXCLUSIVEADDRUSE" not in options(
            branch.body
        ), "both options on one socket: they are alternatives, not a pair"

    @pytest.mark.skipif(
        hasattr(socket, "SO_EXCLUSIVEADDRUSE"),
        reason=(
            "POSIX-specific: this records why the hold must LISTEN there. On Windows the "
            "operative distinction is not bind-vs-listen but SO_EXCLUSIVEADDRUSE vs "
            "SO_REUSEADDR, pinned structurally above"
        ),
    )
    def test_binding_alone_would_not_have_held_it(self):
        """Why the hold must LISTEN, pinned rather than asserted in a comment.

        A socket that is bound but not listening does NOT refuse a second
        ``SO_REUSEADDR`` bind, so the obvious cheap shape -- occupy the port without
        listening, and let a stale client's connect be refused before it ever
        transmits -- does not actually hold the port. This is why the credential can
        reach a completed handshake at all, and therefore why the reaper below matters.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        try:
            assert _bind_from_another_process(port) == "BOUND", (
                "bind-without-listen refused a second binder, so the listen in "
                "HopPortGuard.hold could be dropped -- re-derive the shape if so"
            )
            s.listen(8)
            assert _bind_from_another_process(port).startswith(
                "REFUSED"
            ), "listening did not hold the port"
        finally:
            s.close()


class TestNothingReadsTheCredential:
    def test_a_stale_client_is_reset_at_once_and_its_bytes_are_never_read(self, guard):
        """What the user actually experiences, and the reason the refusal is blunt.

        The pane gets a RESET, not a hang and not an empty success. A courteous reply
        would mean draining the request first, and the request is what carries the
        credential.
        """
        port = _hold_a_port(guard, time.time() + 300)

        c = socket.socket()
        c.settimeout(10)
        c.connect(("127.0.0.1", port))
        try:
            # The send is INSIDE the expectation, not before it. The guard accepts and
            # resets as soon as the connection lands, so the reset can arrive while the
            # request is still being written -- in which case `sendall` is what raises
            # and a test expecting it only from `recv` fails with the very error it was
            # asserting. Which of the two sees it is a timing detail; that one of them
            # does is the property.
            with pytest.raises(OSError) as caught:
                # Exactly what a stale pane would send, credential included.
                c.sendall(b"GET /api/x HTTP/1.1\r\nAuthorization: Bearer SECRET_TOK\r\n\r\n")
                while True:
                    if c.recv(4096) == b"":
                        raise AssertionError(
                            "clean end of stream: a client could read that as a valid "
                            "empty reply rather than a failure"
                        )
            assert (
                caught.value.errno == errno.ECONNRESET
            ), f"expected an unambiguous reset, got {caught.value!r}"
        finally:
            c.close()

    def test_a_client_that_sends_nothing_is_also_reset_not_closed_politely(self, guard):
        """The case ``SO_LINGER`` uniquely decides, and the reason it is not decoration.

        Closing a socket that has UNREAD data already sends RST on its own, so the case
        above passes with or without ``SO_LINGER`` -- it cannot discriminate, and a
        mutation removing the option proved it. A client that connects and sends
        NOTHING is the case that separates them: a plain close is a FIN, which the
        client reads as an orderly end of stream and may treat as a connection it may
        quietly reopen. ``SO_LINGER`` 0 makes the refusal a reset either way, so the
        failure does not depend on whether the client happened to speak first.
        """
        port = _hold_a_port(guard, time.time() + 300)

        c = socket.socket()
        c.settimeout(10)
        c.connect(("127.0.0.1", port))
        try:
            with pytest.raises(OSError) as caught:
                for _ in range(100):
                    if c.recv(4096) == b"":
                        raise AssertionError(
                            "orderly FIN: the refusal depends on the client having sent "
                            "something first, so SO_LINGER is not in force"
                        )
            assert (
                caught.value.errno == errno.ECONNRESET
            ), f"expected a reset for a silent client, got {caught.value!r}"
        finally:
            c.close()

    def test_the_guard_never_calls_a_read(self):
        """Structural, because 'nothing read the token' is a property of the CODE.

        A behavioural test cannot distinguish a guard that reads and discards from one
        that never reads, and the difference is the whole point: a read puts the
        credential in this process.
        """
        import ast
        import inspect

        from kiro_crew.instances import hop_port_guard

        tree = ast.parse(inspect.getsource(hop_port_guard))
        reads = [
            ast.unparse(n.func)
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"recv", "read", "recv_into", "recvfrom", "makefile"}
        ]
        # The wake pipe is drained, which is ours and carries no credential.
        assert reads == [
            "self._wake_r.recv"
        ], f"the guard reads from something other than its own wake pipe: {reads}"
