"""Local machine: the desktop app's own local controller (G1/G2).

A desktop window can point at a Remote Crew while no gateway runs on this
computer. Then nothing local would own consent or run connectors, so the
desktop main process supervises ``kirocrew connected-computer desktop-host``
and talks to it over the child's own stdin/stdout: one JSON object per line,
each request ``{"id", "op", ...}`` answered by ``{"id", "ok", ...}``.

What makes this the same authority as the local gateway, not a second one:

* the grant store, the OS keyring adapter and the Off generation are the same
  objects the gateway uses (:class:`.local_store.LocalGrantStore`);
* a share runs :func:`.share.share_computer`, the gateway's redeem path, with
  the Off generation its ``describe`` read before the native dialogs opened;
* connectors run under :class:`.controller.LocalController`, holding the
  machine's :class:`.controller_lock.ControllerLock` -- when a local gateway
  holds it, this host answers ``elsewhere`` and exits, so connectors never run
  twice.

What a caller on the pipe can and cannot do (B13):

* the pipe exists only between the desktop main process and its own child; the
  host opens no socket, port or file anyone else could reach;
* ``hello`` names the Remote Crew connections the APP has configured (its own
  store, never a page); only those ids can be shared with, and each is checked
  for a shape that cannot smuggle SSH options or shell text;
* a ``share`` carries only an instance id, a folder and tool names: the person
  chose them in native dialogs, and the root and tools are checked again here by
  this computer's own policy;
* nothing is served before ``hello``, a field or op not listed is refused, and
  no credential, key or peer secret is ever written back.

Lifetime (G2): the host runs while the app runs. stdin EOF -- the app quit or
crashed -- stops every connector and releases the lock; grants are kept. With
the switch off at start, or turned off while running, every active grant is
revoked (the gateway's config-watch behaviour) and the host exits
:data:`EXIT_DISABLED`; nothing comes back when it is turned on again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Final, Optional

from kiro_crew.connected_computers.flag import DISABLED_REASON, local_enabled, local_switch

logger = logging.getLogger(__name__)

EXIT_OK: Final[int] = 0
#: The first line was not ``hello``, or the pipe broke the line protocol.
EXIT_PROTOCOL: Final[int] = 2
#: The local switch is off: nothing runs here until the app starts again.
EXIT_DISABLED: Final[int] = 3
#: A local gateway on this computer runs the connectors.
EXIT_ELSEWHERE: Final[int] = 4
#: Longest request line; a share is a few hundred bytes.
MAX_LINE_BYTES: Final[int] = 64 * 1024
#: Remote Crew connections one hello may name.
MAX_INSTANCES: Final[int] = 16
#: Requests handled at once; the app bounds its own side too.
MAX_IN_FLIGHT: Final[int] = 8
#: How long a quitting app waits for an in-flight share before it is cancelled.
DRAIN_SECS: Final[float] = 30.0

_INSTANCE_ID = re.compile(r"^desktop-([0-9]{1,5})$")
# An SSH destination as the desktop app's remote-crew form accepts it: a host
# alias or user@host, never an option (leading '-') or shell text.
_SSH_HOST = re.compile(r"^[A-Za-z0-9_.@:\[\]][A-Za-z0-9_.@:\[\]-]{0,252}$")
_REMOTE_BIN = re.compile(r"^[A-Za-z0-9_./~+-]{0,512}$")
_INSTANCE_KEYS: Final[frozenset[str]] = frozenset(
    {"id", "name", "ssh_host", "remote_bin", "remote_port", "local_port"}
)
#: Fields each op may carry besides ``id`` and ``op``.
_FIELDS: Final[dict[str, frozenset[str]]] = {
    "hello": frozenset({"instances"}),
    "instances": frozenset({"items"}),
    "describe": frozenset({"instance_id"}),
    "share": frozenset({"instance_id", "consent", "root", "mcp"}),
    "status": frozenset(),
    "revoke": frozenset({"grant_id"}),
    "off": frozenset(),
}

Writer = Callable[[bytes], Awaitable[None]]


class _Invalid(ValueError):
    pass


def _port(value: Any, *, allow_zero: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Invalid("a port must be a number")
    if not (0 if allow_zero else 1) <= value <= 65535:
        raise _Invalid("a port is out of range")
    return value


def parse_instances(items: Any) -> dict[str, SimpleNamespace]:
    """The app's Remote Crew connections, or raise for any malformed entry."""
    if not isinstance(items, list) or len(items) > MAX_INSTANCES:
        raise _Invalid("instances must be a short list")
    out: dict[str, SimpleNamespace] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != _INSTANCE_KEYS:
            raise _Invalid("an instance has missing or extra fields")
        ident, name = item["id"], item["name"]
        ssh_host, remote_bin = item["ssh_host"], item["remote_bin"]
        match = _INSTANCE_ID.match(ident) if isinstance(ident, str) else None
        if match is None or ident in out:
            raise _Invalid("an instance id is malformed or repeated")
        local_port = _port(item["local_port"], allow_zero=False)
        if int(match.group(1)) != local_port:
            raise _Invalid("an instance id must name its own local port")
        remote_port = _port(item["remote_port"], allow_zero=True)
        if not isinstance(name, str) or len(name) > 200 or "\x00" in name:
            raise _Invalid("an instance name is malformed")
        if not isinstance(ssh_host, str) or not _SSH_HOST.match(ssh_host):
            raise _Invalid("an SSH host is malformed")
        if not isinstance(remote_bin, str) or not _REMOTE_BIN.match(remote_bin):
            raise _Invalid("a remote command path is malformed")
        out[ident] = SimpleNamespace(
            id=ident,
            name=name or ssh_host,
            ssh_host=ssh_host,
            remote_bin=remote_bin,
            remote_port=remote_port,
            local_port=local_port,
        )
    return out


#: Outstanding native consents kept at once; the oldest is dropped past this.
_MAX_CONSENTS: Final[int] = 16


def _fail(rid: Any, code: str, error: str) -> dict[str, Any]:
    return {"id": rid, "ok": False, "code": code, "error": error}


class DesktopHost:
    """Serve one desktop app's native sharing requests for this computer."""

    poll_secs: float = 2.0

    def __init__(
        self,
        store: Any,
        *,
        enabled: Callable[[], bool] = local_enabled,
        lock: Any,
        plan_factory: Optional[Callable[[], Any]] = None,
        registry_lookup: Callable[[str], Any],
    ) -> None:
        from kiro_crew.connected_computers.controller import LocalController

        self.store = store
        self._enabled = enabled
        self._registry_lookup = registry_lookup
        self.instances: dict[str, SimpleNamespace] = {}
        #: consent token -> (instance id, expiry, the Off generation read when its
        #: dialog began). Each ``describe`` mints its own token and ``share`` spends
        #: exactly that one, so an Off made while the native dialogs were open
        #: refuses the grant, and a later dialog never lends an earlier one its
        #: generation.
        self._consents: dict[str, tuple[str, float, int]] = {}
        self._hello = False
        kwargs: dict[str, Any] = {}
        if plan_factory is not None:
            kwargs["plan_factory"] = plan_factory
        self.controller = LocalController(
            store,
            instance_lookup=self._lookup,
            lock=lock,
            enabled=enabled,
            **kwargs,
        )

    async def _switch_state(self) -> Optional[bool]:
        """The switch for decisions that revoke: True, False, or None when unreadable.

        Read off the loop. Only an authoritative False is the user's Off; an
        unreadable configuration (None) revokes nothing and is read again later.
        """
        if self._enabled is local_enabled:
            return await asyncio.to_thread(local_switch)
        return bool(await asyncio.to_thread(self._enabled))

    async def _switch_on(self) -> bool:
        """The switch, read off the loop: without a live snapshot it reads the config
        files, and a stalled read must not hold a watchdog, a channel or a stop."""
        return bool(await asyncio.to_thread(self._enabled))

    def _lookup(self, instance_id: str) -> Any:
        # A grant made through a local gateway names a registry entry; one made
        # here names an app connection. Each resolves only through its owner.
        if instance_id in self.instances:
            return self.instances[instance_id]
        return self._registry_lookup(instance_id)

    # -- one request ----------------------------------------------------------------
    async def handle(self, msg: Any) -> dict[str, Any]:
        rid = msg.get("id") if isinstance(msg, dict) else None
        if not isinstance(rid, int) or isinstance(rid, bool):
            rid = None
        if not isinstance(msg, dict) or rid is None or not isinstance(msg.get("op"), str):
            return _fail(rid, "invalid_request", "a request needs a numeric id and an op")
        op = msg["op"]
        if op not in _FIELDS:
            return _fail(rid, "unknown_op", "no such operation")
        if op != "hello" and not self._hello:
            return _fail(rid, "hello_first", "the app has not said hello")
        if set(msg) - {"id", "op"} - _FIELDS[op]:
            return _fail(rid, "invalid_request", "unexpected fields")
        try:
            return await getattr(self, f"_op_{op}")(rid, msg)
        except _Invalid as exc:
            return _fail(rid, "invalid_request", str(exc))

    async def _op_hello(self, rid: int, msg: dict[str, Any]) -> dict[str, Any]:
        if self._hello:
            return _fail(rid, "invalid_request", "hello was already said")
        self.instances = parse_instances(msg.get("instances"))
        self._hello = True
        try:
            protection = await asyncio.to_thread(self.store.protection_ready)
        except Exception:  # noqa: BLE001 -- shown as "no protection", shares refused
            protection = ""
        return {"id": rid, "ok": True, "state": await self._settle(), "protection": protection}

    async def _settle(self) -> str:
        """Bring the controller in line with the switch and the lock; its state."""
        # Unknown (None) is not Off: revoke nothing. The controller reads the
        # switch itself before each start, so it runs nothing until it is readable.
        if await self._switch_state() is False:
            await self.controller.off()
            await self.controller.stop_all()
            return "disabled"
        await self.controller.reconcile()
        if self.controller.elsewhere:
            return "elsewhere"
        self.controller.start()
        return "running"

    async def _op_instances(self, rid: int, msg: dict[str, Any]) -> dict[str, Any]:
        self.instances = parse_instances(msg.get("items"))
        return {"id": rid, "ok": True}

    def _named(self, msg: dict[str, Any]) -> Optional[SimpleNamespace]:
        ident = msg.get("instance_id")
        return self.instances.get(ident) if isinstance(ident, str) else None

    async def _op_describe(self, rid: int, msg: dict[str, Any]) -> dict[str, Any]:
        from kiro_crew.connected_computers import local_mcp
        from kiro_crew.connected_computers import share as share_mod
        from kiro_crew.connected_computers.local_routes import CONSENT_TTL_SECS

        instance = self._named(msg)
        if instance is None:
            return _fail(
                rid, "unknown_instance", "the app has no Remote Crew connection by that id"
            )
        try:
            generation = await share_mod.read_off_generation(self.store)
        except share_mod.ShareRefused as exc:
            return _fail(rid, exc.code, exc.message)
        now = time.monotonic()
        for key in [k for k, (_i, exp, _g) in self._consents.items() if exp <= now]:
            del self._consents[key]
        while len(self._consents) >= _MAX_CONSENTS:
            self._consents.pop(next(iter(self._consents)))
        consent = secrets.token_urlsafe(24)
        self._consents[consent] = (instance.id, now + CONSENT_TTL_SECS, generation)
        servers = await asyncio.to_thread(local_mcp.configured_servers)
        return {
            "id": rid,
            "ok": True,
            "consent": consent,
            "instance": {"id": instance.id, "name": instance.name, "ssh_host": instance.ssh_host},
            "mcp": local_mcp.exposable(servers),
        }

    async def _op_share(self, rid: int, msg: dict[str, Any]) -> dict[str, Any]:
        from kiro_crew.connected_computers import share as share_mod

        instance = self._named(msg)
        if instance is None:
            return _fail(
                rid, "unknown_instance", "the app has no Remote Crew connection by that id"
            )
        if not await self._switch_on():
            return _fail(rid, "connected_computers_disabled", DISABLED_REASON)
        token = msg.get("consent")
        opened = self._consents.pop(token, None) if isinstance(token, str) else None
        if opened is None or opened[0] != instance.id or opened[1] <= time.monotonic():
            return _fail(rid, "no_consent", "describe this Remote Crew first, then share")
        try:
            grant = await share_mod.share_computer(
                self.controller,
                instance_id=instance.id,
                instance_lookup=self._lookup,
                root=msg.get("root"),
                mcp=msg.get("mcp", []),
                enabled=self._enabled,
                off_generation=opened[2],
            )
        except share_mod.ShareRefused as exc:
            return _fail(rid, exc.code, exc.message)
        return {"id": rid, "ok": True, "grant": grant.public()}

    async def _op_status(self, rid: int, msg: dict[str, Any]) -> dict[str, Any]:
        rows = await asyncio.to_thread(self.controller.status)
        return {"id": rid, "ok": True, "grants": rows, "enabled": await self._switch_on()}

    async def _op_revoke(self, rid: int, msg: dict[str, Any]) -> dict[str, Any]:
        grant_id = msg.get("grant_id")
        grant = await self.controller.revoke(grant_id) if isinstance(grant_id, str) else None
        if grant is None:
            return _fail(rid, "unknown_grant", "no such grant")
        return {"id": rid, "ok": True, "revoked": grant.grant_id}

    async def _op_off(self, rid: int, msg: dict[str, Any]) -> dict[str, Any]:
        # Off only narrows: it works with the switch off and the keyring locked.
        ended = await self.controller.off()
        return {"id": rid, "ok": True, "revoked": [g.grant_id for g in ended]}

    # -- the pipe -------------------------------------------------------------------
    async def serve(self, reader: asyncio.StreamReader, write: Writer) -> int:
        """Answer the app until stdin ends; the process exit code."""
        write_lock = asyncio.Lock()

        async def send(obj: dict[str, Any]) -> None:
            data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
            async with write_lock:
                await write(data)

        try:
            first = await self._read(reader)
            if first is None:
                return EXIT_OK
            if not isinstance(first, dict) or first.get("op") != "hello":
                rid = first.get("id") if isinstance(first, dict) else None
                await send(_fail(rid, "hello_first", "the first request must be hello"))
                return EXIT_PROTOCOL
            reply = await self.handle(first)
            await send(reply)
            if not reply.get("ok"):
                return EXIT_PROTOCOL
            if reply["state"] == "elsewhere":
                return EXIT_ELSEWHERE
            code = EXIT_DISABLED if reply["state"] == "disabled" else await self._loop(reader, send)
            if code == EXIT_DISABLED:
                await self._tell_remotes()
            return code
        except _LineTooLong:
            return EXIT_PROTOCOL
        finally:
            await self.controller.shutdown()

    async def _tell_remotes(self) -> None:
        """After the stop, one bounded pass over queued remote revokes (R8 order).

        What is not confirmed stays queued in the store and is retried by the
        next controller on this computer; the local refusal already holds.
        """
        try:
            await asyncio.wait_for(self.controller.drain_pending(), DRAIN_SECS)
        except Exception:  # noqa: BLE001 -- queued, retried later
            logger.info("connected computers: remote revokes left queued", exc_info=True)

    async def _loop(
        self, reader: asyncio.StreamReader, send: Callable[..., Awaitable[None]]
    ) -> int:
        gate = asyncio.Semaphore(MAX_IN_FLIGHT)
        tasks: set[asyncio.Task[None]] = set()
        watch = asyncio.ensure_future(self._watch_switch())

        async def run(msg: dict[str, Any]) -> None:
            try:
                await send(await self.handle(msg))
            finally:
                gate.release()

        try:
            while True:
                read = asyncio.ensure_future(self._read(reader))
                done, _ = await asyncio.wait({read, watch}, return_when=asyncio.FIRST_COMPLETED)
                if watch in done:
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                    await self._drain(tasks)
                    return EXIT_DISABLED
                msg = read.result()
                if msg is None:
                    await self._drain(tasks)
                    return EXIT_OK
                if isinstance(msg, _Unreadable):
                    await send(_fail(None, "invalid_request", "a request must be one JSON object"))
                    continue
                if gate.locked():
                    await send(_fail(msg.get("id"), "busy", "too many requests at once"))
                    continue
                await gate.acquire()
                task = asyncio.ensure_future(run(msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)

    async def _watch_switch(self) -> None:
        """Return once the switch is off, after every active grant was revoked."""
        while (await self._switch_state()) is not False:
            await asyncio.sleep(self.poll_secs)
        await self.controller.off()

    async def _drain(self, tasks: "set[asyncio.Task[None]]") -> None:
        if not tasks:
            return
        _, pending = await asyncio.wait(set(tasks), timeout=DRAIN_SECS)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _read(self, reader: asyncio.StreamReader) -> Any:
        """The next request; None at EOF; :class:`_Unreadable` for a bad line."""
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # The stream's own limit: the rest of that line cannot be found again.
            raise _LineTooLong() from exc
        if not line:
            return None
        if len(line) > MAX_LINE_BYTES:
            return _Unreadable()
        try:
            msg = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return _Unreadable()
        return msg if isinstance(msg, dict) else _Unreadable()


class _Unreadable:
    pass


class _LineTooLong(Exception):
    pass


async def _stdio() -> "tuple[asyncio.StreamReader, Writer]":
    """The app's pipe; stray prints from anything else go to stderr, not to it."""
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES * 2)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    # A buffered writer writes every byte (a raw one may write part of a reply).
    out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

    def _write_all(data: bytes) -> None:
        out.write(data)
        out.flush()

    async def write(data: bytes) -> None:
        await asyncio.to_thread(_write_all, data)

    return reader, write


def run_desktop_host() -> int:
    """``kirocrew connected-computer desktop-host``: serve the app on stdio."""
    from kiro_crew import platform_compat

    if platform_compat.IS_WINDOWS:
        print("connected computers are not available on Windows", file=sys.stderr)
        return EXIT_DISABLED

    async def main() -> int:
        from kiro_crew.connected_computers.controller_lock import ControllerLock
        from kiro_crew.connected_computers.local_store import LocalGrantStore
        from kiro_crew.connected_computers.vault_doc import open_vault
        from kiro_crew.instances.registry import InstancesRegistry

        reader, write = await _stdio()
        host = DesktopHost(
            LocalGrantStore(open_vault()),
            lock=ControllerLock.for_this_machine(),
            registry_lookup=lambda iid: InstancesRegistry().get(iid),
        )
        return await host.serve(reader, write)

    return asyncio.run(main())
