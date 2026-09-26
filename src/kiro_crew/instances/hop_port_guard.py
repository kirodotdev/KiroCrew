"""OS-level ownership of a lent hop port, for as long as its lease stands.

A hop lease withholds a port from THIS gateway's allocator. That is not ownership:
once the forward that served the lent crew is torn down, the port is free at the OS
level, and any other local process -- including a second gateway with its own
registry, which cannot see this lease at all -- may bind it. The holder of the
chained credential goes on dialling that port, so whatever bound it receives a live
bearer token. The hub-side identity check only runs on a subsequent mint, and the
proactive re-mint is scheduled at ~80% of the token's lifetime, so on the default 20h
TTL the exposure would last hours rather than being caught promptly.

This closes it by making the parent keep the port itself while the lease stands.

**Why the socket must LISTEN, and why the option differs by platform.** Binding alone
does not hold a port against the binders that matter here: this repo's own availability
probe sets ``SO_REUSEADDR`` (:func:`kiro_crew.instances.port_allocator._port_free`) and
so does OpenSSH's forward listener, and on POSIX a second ``SO_REUSEADDR`` bind against
a socket that is bound but NOT listening SUCCEEDS. Only a listening socket refuses it
with ``EADDRINUSE``.

On Windows the option itself inverts: ``SO_REUSEADDR`` there lets another process steal
an ACTIVE listener, so setting it would give away the ownership this class exists to
hold, and merely omitting it is not enough because the other process can set it on its
own socket. ``SO_EXCLUSIVEADDRUSE`` is the opt-in that makes the bind exclusive. This is
the same branch, for the same reason, as :mod:`kiro_crew.browser_cli.view` and the
dashboard's own listener -- an earlier revision of this module set ``SO_REUSEADDR``
unconditionally and held nothing at all on Windows while claiming otherwise.

**Why it accepts, and why it never reads.** Because the socket has to listen, the TCP
handshake completes, so a stale client WILL transmit its request -- "the token is
never sent" is not reachable once listening is mandatory. What is reachable is that
nothing ever reads it. Two shapes were measured:

* Listen and never accept: the connection completes and then sits in the accept
  queue, request and credential buffered in the kernel, until the client times out.
  The pane HANGS, and every queued connection is holding a credential that any later
  accept would hand over.
* Accept immediately, never read, close with ``SO_LINGER`` 0: the credential's
  residence is bounded by the instant between accept and close, no queue of
  credential-bearing connections accumulates, and ``SO_LINGER`` 0 makes the close an
  RST rather than a FIN, so the failure is unambiguous rather than an empty body a
  client might mistake for a valid reply.

The second is chosen: it is both the safer shape and the kinder one. **A stale pane
dialling a held port sees its connection RESET at once** (``ECONNRESET``), not a hang
and not an empty success.

A deliberately blunt refusal: answering something courteous like ``410 Gone`` would
mean draining the request first, and the request carries the credential. The whole
point is that no code here touches it, so the connection is refused unread.

**The window this does not close.** The forward must release the port before this can
bind it, so there is an interval between those two operations during which the port is
briefly free. It is the gap between two adjacent statements rather than the hours the
lease would otherwise stand unenforced, and closing it entirely would need the kernel
to transfer the binding, which it offers no way to do.
"""

from __future__ import annotations

import errno
import logging
import selectors
import socket
import struct
import threading
import time

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

#: ``SO_LINGER`` with a zero timeout: close() emits RST instead of FIN, so a stale
#: client gets an unambiguous failure rather than a clean empty reply.
_LINGER_RESET = struct.pack("ii", 1, 0)

_LOOPBACK = "127.0.0.1"


class HopPortGuard:
    """Holds lent hop ports so nothing else on the host can bind them.

    One reaper thread serves every held port through a selector, rather than a thread
    per port: the number of simultaneously held ports is bounded by the number of live
    leases, and a thread each would make that a thread count.
    """

    def __init__(self, host: str = _LOOPBACK) -> None:
        self._host = host
        self._lock = threading.Lock()
        self._held: dict[int, tuple[socket.socket, float]] = {}
        #: Ports we OWE a hold: leased, not held, because the bind failed. Something
        #: else had the port (an orphaned forwarder of ours, most often) or descriptors
        #: ran out. Retried by the reaper until taken or lapsed, because a hold that
        #: failed once and is never retried is a live lease with no socket -- the exact
        #: exposure, reached by a different route than the one that is usually named.
        #: Deadlines are carried here too, so a retry needs no registry read.
        self._pending: dict[int, float] = {}
        #: Last bind-failure code per owed port, so a retry that keeps failing for
        #: the SAME reason logs once at ERROR and then at DEBUG.
        self._last_fail_code: dict[int, int] = {}
        self._selector = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._selector.register(self._wake_r, selectors.EVENT_READ)
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    # -- state -------------------------------------------------------------

    def held_ports(self) -> set[int]:
        with self._lock:
            return set(self._held)

    def owed_ports(self) -> set[int]:
        """Leased ports we could not take. Non-empty means an exposure is open."""
        with self._lock:
            return set(self._pending)

    # -- arming ------------------------------------------------------------

    def hold(self, port: int, until: float) -> bool:
        """Take OS ownership of *port* until the epoch *until*.

        True when held, False when it could not be. The deadline is carried on the hold
        itself so the reaper can drop it without reading the registry from a thread and
        without a second timer: a hold that outlived its lease would squat a port that
        is legitimately free again, which is its own small defect.

        False is a REFUSAL the caller must act on rather than a line to log and move
        past: an unheld lease is exactly the exposure this class removes. The usual
        causes are a descriptor limit and something else having already bound the port
        -- and that second case means the exposure has already happened, so it is
        reported at ERROR.
        """
        with self._lock:
            existing = self._held.get(port)
            if existing is not None:
                # Never shorten a hold: same rule as the lease it mirrors.
                sock, have = existing
                self._held[port] = (sock, max(have, float(until)))
                return True
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if platform_compat.IS_POSIX:
                    # Mirrors what the forward listener and the availability probe do,
                    # so this hold refuses exactly the binders they would otherwise
                    # win: a second SO_REUSEADDR bind against a LISTENING socket is
                    # refused with EADDRINUSE.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    # Windows inverts the meaning: SO_REUSEADDR there lets another
                    # process STEAL an active listener, so setting it would hand away
                    # the very ownership this class exists to hold -- and merely not
                    # setting it is not enough either, because the other process can
                    # set it on ITS socket. SO_EXCLUSIVEADDRUSE is the opt-in that
                    # makes our bind exclusive. Same branch, and for the same reason,
                    # as `browser_cli.view` and the dashboard's own listener.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((self._host, port))
                sock.listen(8)
                sock.setblocking(False)
            except OSError as e:
                sock.close()
                self._pending[port] = float(until)
                self._ensure_reaper()
                code = e.errno or 0
                # ERROR once per port, and again only when the reason CHANGES; every
                # other retry is DEBUG. The retry runs on each reaper pass, so a port
                # some other binder holds would otherwise emit ~2 ERROR records a second
                # for the whole lease -- of the order of 10^5 identical lines on the
                # default 20h TTL, which does not make the exposure more visible, it
                # buries every other gateway error and evicts them from a size-capped
                # sink. `owed_ports()` is the durable signal; the log is the alert.
                first = self._last_fail_code.get(port) != code
                self._last_fail_code[port] = code
                (logger.error if first else logger.debug)(
                    "could not take ownership of lent hop port %d (%s): a chained "
                    "credential naming it is still valid and the port is not ours",
                    port,
                    errno.errorcode.get(code, str(e.errno)),
                )
                return False
            self._held[port] = (sock, float(until))
            self._pending.pop(port, None)
            self._last_fail_code.pop(port, None)
            self._selector.register(sock, selectors.EVENT_READ)
            self._ensure_reaper()
        self._wake()
        return True

    def release(self, port: int) -> None:
        """Give *port* back. Call this BEFORE a real forward binds it.

        Closing the listener frees the port immediately, which is what lets a crew
        reconnect onto the same port once its lease has lapsed.
        """
        with self._lock:
            self._pending.pop(port, None)
            self._last_fail_code.pop(port, None)
            entry = self._held.pop(port, None)
            if entry is None:
                return
            sock, _until = entry
            try:
                self._selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            sock.close()

    def sync(self, leases: dict[int, float], in_use: set[int]) -> set[int]:
        """Make the held set exactly the leased ports that no forward is serving.

        Returns the ports it could NOT hold, so a caller that must fail closed can.

        Idempotent, which is what lets one call serve every trigger: a teardown that
        frees a lent port, a connect that is about to bind one, and startup -- where it
        is load-bearing, because the lease is persisted and a socket is not, so without
        it a restart leaves every live lease withheld from this allocator and owned by
        nothing at all.
        """
        busy = {int(p) for p in in_use}
        want = {int(p): float(u) for p, u in leases.items() if int(p) not in busy}
        for port in (self.held_ports() | self.owed_ports()) - set(want):
            self.release(port)
        failed: set[int] = set()
        for port, until in want.items():
            if not self.hold(port, until):
                failed.add(port)
        return failed

    def close_all(self) -> None:
        self._stop.set()
        self._wake()
        reaper = self._reaper
        if reaper is not None:
            reaper.join(timeout=5)
        for port in self.held_ports():
            self.release(port)

    # -- the reaper --------------------------------------------------------

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\x01")
        except OSError:
            pass

    def _ensure_reaper(self) -> None:
        """Caller holds the lock."""
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._stop.clear()
        self._reaper = threading.Thread(target=self._reap, name="hop-port-guard", daemon=True)
        self._reaper.start()

    def _reap(self) -> None:
        """Accept and immediately reset, never reading a byte.

        Reading is what would put the credential in this process; the connection is
        refused unread instead. The same pass drops holds whose lease has lapsed, so a
        hold cannot outlive the credential it protects and squat a port that is free
        again.
        """
        while not self._stop.is_set():
            try:
                self._drop_lapsed()
                self._retry_owed()
                events = self._selector.select(timeout=0.5)
            except OSError:
                continue
            except Exception:
                # This thread is the only thing that answers a held port. If it dies
                # the holds stay bound but stop accepting, so every stale pane hangs
                # instead of being reset, and no owed port is ever retaken -- a silent
                # downgrade of both halves. Log and keep the loop alive.
                logger.exception("hop port guard reaper pass failed; continuing")
                self._stop.wait(0.5)
                continue
            for key, _ in events:
                if key.fileobj is self._wake_r:
                    try:
                        self._wake_r.recv(4096)
                    except OSError:
                        pass
                    continue
                listener = key.fileobj
                try:
                    conn, _peer = listener.accept()  # type: ignore[union-attr]
                except OSError:
                    continue
                try:
                    conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_RESET)
                finally:
                    # No read, ever. The close emits RST and the kernel discards
                    # whatever the client sent, credential included.
                    conn.close()

    def _retry_owed(self) -> None:
        """Take the leased ports whose bind failed earlier, and forget the lapsed ones.

        The usual cause is one of our own orphaned forwarders still occupying the port
        at startup: the moment it goes the bind succeeds, and the exposure is closed
        within a pass rather than waiting for an unrelated teardown to call ``sync``.
        """
        now = time.time()
        with self._lock:
            owed = dict(self._pending)
        for port, until in owed.items():
            if until <= now:
                with self._lock:
                    self._pending.pop(port, None)
                    self._last_fail_code.pop(port, None)
                continue
            if self.hold(port, until):
                logger.info("took ownership of lent hop port %d on retry", port)

    def _drop_lapsed(self) -> None:
        now = time.time()
        with self._lock:
            lapsed = [p for p, (_s, until) in self._held.items() if until <= now]
        for port in lapsed:
            logger.info("lent hop port %d released: its lease has lapsed", port)
            self.release(port)
