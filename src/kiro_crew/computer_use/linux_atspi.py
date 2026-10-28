"""AT-SPI 2 half of the Linux computer-use driver, spoken directly over D-Bus.

No GObject introspection and no ``libatspi``: the gateway runs in its own Python
environment where ``gi`` is not importable, and AT-SPI is a D-Bus protocol anyway.
The transport is ``jeepney`` (pure Python), and every walk is PIPELINED: all
requests for one breadth-first level go out before the first reply is read, so a
1000-node tree costs a handful of socket round trips rather than five thousand.

Two facts shape the client:

* **The accessibility bus is separate from the session bus.** Its address comes from
  ``org.a11y.Bus.GetAddress`` on the session bus, and applications only export their
  tree when ``org.a11y.Status.IsEnabled`` is true. :func:`connect` sets that flag, the
  Linux analogue of macOS's Electron ``AXManualAccessibility`` opt-in. Toolkits read
  it at startup, so an app started before it was set may need a restart; the driver
  reports that case instead of rendering an empty tree as a working app.
* **A reply may never arrive.** A hung application does not answer its accessibility
  calls, and the registry does not answer for it. Every batch has a deadline, and a
  missing reply becomes a per-call error, never an unbounded wait in a worker thread.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from kiro_crew.computer_use import linux_x11

# ``jeepney`` is a Linux-only dependency (setup.cfg gates it on sys_platform), and
# every computer_use module must import on any OS, so it is imported where it is
# used, never at module scope.

logger = logging.getLogger(__name__)

ACCESSIBLE = "org.a11y.atspi.Accessible"
COMPONENT = "org.a11y.atspi.Component"
ACTION = "org.a11y.atspi.Action"
TEXT = "org.a11y.atspi.Text"
EDITABLE_TEXT = "org.a11y.atspi.EditableText"
VALUE = "org.a11y.atspi.Value"
SELECTION = "org.a11y.atspi.Selection"
PROPS = "org.freedesktop.DBus.Properties"

REGISTRY = "org.a11y.atspi.Registry"
ROOT_PATH = "/org/a11y/atspi/accessible/root"
NULL_PATH = "/org/a11y/atspi/null"

COORD_SCREEN = 0
COORD_WINDOW = 1
#: What a newly built client asks extents in. The driver sets it once per session
#: kind, so a client rebuilt after ``reset_client`` keeps the same space.
default_coord_type = COORD_SCREEN

# AtspiStateType bit numbers (at-spi2-core ``atspi-constants.h``).
STATE_ACTIVE = 1
STATE_CHECKED = 4
STATE_DEFUNCT = 6
STATE_EDITABLE = 7
STATE_ENABLED = 8
STATE_EXPANDABLE = 9
STATE_EXPANDED = 10
STATE_FOCUSABLE = 11
STATE_FOCUSED = 12
STATE_HORIZONTAL = 14
STATE_ICONIFIED = 15
STATE_PRESSED = 20
STATE_SELECTED = 23
STATE_SENSITIVE = 24
STATE_SHOWING = 25
STATE_VERTICAL = 29
STATE_VISIBLE = 30
STATE_MANAGES_DESCENDANTS = 31
STATE_READ_ONLY = 43

#: ``ATSPI_ROLE_PASSWORD_TEXT``. The secure check keys on the role NUMBER as well as
#: the name, so a failed ``GetRoleName`` lookup can never turn a password entry into
#: an ordinary text field.
ROLE_PASSWORD_TEXT = 40

#: Batch deadline. Generous, because a large Chromium tree answers slowly, but finite.
CALL_TIMEOUT_SECS = 4.0

#: Children read from a MANAGES_DESCENDANTS container (a sheet, a big tree view).
MANAGED_CHILD_LIMIT = 64

#: Messages in flight at once. A single-threaded application answers its queue in
#: order, so an unbounded flood only moves every later reply past the deadline.
CHUNK = 256


class AtspiUnavailable(RuntimeError):
    """The accessibility bus could not be reached."""


class CallError(Exception):
    """One D-Bus call in a batch failed (error reply or no reply by the deadline)."""

    def __init__(self, name: str, detail: Any = "") -> None:
        super().__init__(f"{name}: {detail}")
        self.name = name


@dataclass(frozen=True)
class Ref:
    """One accessible object: the bus name of its application plus its object path."""

    bus: str
    path: str

    @property
    def is_null(self) -> bool:
        return not self.bus or self.path == NULL_PATH


@dataclass
class Node:
    """The data one walk reads for one accessible."""

    ref: Ref
    depth: int
    role_id: int = 0
    role: str = ""
    name: str = ""
    description: str = ""
    states: frozenset[int] = frozenset()
    interfaces: frozenset[str] = frozenset()
    children: list[Ref] = field(default_factory=list)
    child_count: int = 0
    extents: "tuple[int, int, int, int] | None" = None
    text: str = ""
    value: "float | None" = None
    actions: tuple[str, ...] = ()
    failed: bool = False
    #: True when only a bounded prefix of the children was read.
    children_capped: bool = False
    #: True when a child-list call failed, so the subtree read is incomplete.
    children_failed: bool = False
    #: True when the role name could not be read. The driver treats such a node as
    #: secure: it cannot prove the node is not a password entry.
    role_unknown: bool = False


def _call(ref: Ref, iface: str, method: str, sig: str = "", body: tuple = ()) -> Any:
    from jeepney import DBusAddress, new_method_call

    addr = DBusAddress(ref.path, bus_name=ref.bus, interface=iface)
    return new_method_call(addr, method, sig, body)


def _states(raw: "Iterable[int]") -> frozenset[int]:
    out = set()
    for word_index, word in enumerate(raw):
        for bit in range(32):
            if word & (1 << bit):
                out.add(word_index * 32 + bit)
    return frozenset(out)


def _variant(value: Any) -> Any:
    # jeepney unpacks a 'v' as (signature, value).
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


class AtspiClient:
    """One connection to the accessibility bus, serialized by a lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn: Any = None
        self._session: Any = None
        self._role_names: dict[int, str] = {}
        self._pid_cache: dict[str, int] = {}
        #: Coordinate space ``GetExtents`` answers in. Screen space on X11; window
        #: space on Wayland, where a client has no idea where its window sits.
        self.coord_type = default_coord_type

    # ── connection ──

    def _session_address(self) -> str:
        return (
            os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or f"unix:path=/run/user/{linux_x11._uid()}/bus"
        )

    def _ensure(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            from jeepney import DBusAddress, MessageType, new_method_call
            from jeepney.io.blocking import open_dbus_connection

            session = open_dbus_connection(bus=self._session_address())
            bus_addr = DBusAddress(
                "/org/a11y/bus", bus_name="org.a11y.Bus", interface="org.a11y.Bus"
            )
            reply = session.send_and_get_reply(
                new_method_call(bus_addr, "GetAddress"), timeout=CALL_TIMEOUT_SECS
            )
            if reply.header.message_type == MessageType.error:
                raise AtspiUnavailable(f"org.a11y.Bus.GetAddress failed: {reply.body}")
            a11y_address = reply.body[0]
            # Turn the toolkit bridges on. Idempotent, and the documented way an
            # assistive technology announces itself; GTK/Qt/Chromium read it.
            status = DBusAddress("/org/a11y/bus", bus_name="org.a11y.Bus", interface=PROPS)
            session.send_and_get_reply(
                new_method_call(
                    status, "Set", "ssv", ("org.a11y.Status", "IsEnabled", ("b", True))
                ),
                timeout=CALL_TIMEOUT_SECS,
            )
            conn = open_dbus_connection(bus=a11y_address)
        except AtspiUnavailable:
            raise
        except Exception as exc:  # socket errors, auth failures, a missing bus
            raise AtspiUnavailable(f"the AT-SPI accessibility bus is unreachable ({exc})") from exc
        self._session = session
        self._conn = conn
        return conn

    def available(self) -> "tuple[bool, str]":
        try:
            with self._lock:
                self._ensure()
            return True, ""
        except AtspiUnavailable as exc:
            return False, str(exc)

    def close(self) -> None:
        with self._lock:
            for conn in (self._conn, self._session):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._conn = None
            self._session = None
            self._pid_cache.clear()

    # ── batched calls ──

    def batch(self, messages: "list[Any]", *, timeout: float = CALL_TIMEOUT_SECS) -> list[Any]:
        """Send every message, then collect replies. Each result is a body or CallError."""
        if not messages:
            return []
        if len(messages) > CHUNK:
            out: list[Any] = []
            for start in range(0, len(messages), CHUNK):
                out.extend(self.batch(messages[start : start + CHUNK], timeout=timeout))
            return out
        with self._lock:
            try:
                return self._batch_locked(messages, timeout)
            except (OSError, ConnectionError, EOFError) as exc:
                # A dropped bus connection (the session restarted). Reconnect once.
                logger.debug("AT-SPI connection dropped, reconnecting: %s", exc)
                self.close()
                return self._batch_locked(messages, timeout)

    def _batch_locked(self, messages: "list[Any]", timeout: float) -> list[Any]:
        from jeepney import HeaderFields, MessageType

        conn = self._ensure()
        serials: dict[int, int] = {}
        for position, message in enumerate(messages):
            serial = next(conn.outgoing_serial)
            conn.send(message, serial=serial)
            serials[serial] = position
        results: list[Any] = [CallError("NoReply", "no reply before the deadline")] * len(messages)
        deadline = time.monotonic() + timeout
        pending = len(serials)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = conn.receive(timeout=remaining)
            except TimeoutError:
                break
            reply_to = msg.header.fields.get(HeaderFields.reply_serial)
            slot = serials.pop(reply_to, None) if isinstance(reply_to, int) else None
            if slot is None:
                continue  # a signal, or a reply to an abandoned earlier batch
            pending -= 1
            if msg.header.message_type == MessageType.error:
                name = msg.header.fields.get(HeaderFields.error_name, "Error")
                results[slot] = CallError(str(name), msg.body)
            else:
                results[slot] = msg.body
        return results

    def call(self, ref: Ref, iface: str, method: str, sig: str = "", body: tuple = ()) -> Any:
        result = self.batch([_call(ref, iface, method, sig, body)])[0]
        if isinstance(result, CallError):
            raise result
        return result

    def get_prop(self, ref: Ref, iface: str, prop: str) -> Any:
        return _variant(self.call(ref, PROPS, "Get", "ss", (iface, prop))[0])

    def set_prop(self, ref: Ref, iface: str, prop: str, sig: str, value: Any) -> None:
        self.call(ref, PROPS, "Set", "ssv", (iface, prop, (sig, value)))

    # ── applications ──

    def applications(self) -> "list[tuple[Ref, int, str]]":
        """``(app_root_ref, pid, name)`` for every application on the registry."""
        root = Ref(REGISTRY, ROOT_PATH)
        body = self.call(root, ACCESSIBLE, "GetChildren")
        refs = [Ref(str(b), str(p)) for b, p in body[0]]
        msgs: list[Any] = []
        for ref in refs:
            msgs.append(_call(ref, PROPS, "Get", "ss", (ACCESSIBLE, "Name")))
        names = self.batch(msgs)
        out = []
        for ref, name in zip(refs, names):
            pid = self.pid_of(ref.bus)
            label = "" if isinstance(name, CallError) else str(_variant(name[0]))
            out.append((ref, pid, label))
        return out

    def pid_of(self, bus_name: str) -> int:
        cached = self._pid_cache.get(bus_name)
        if cached is not None:
            return cached
        dbus = Ref("org.freedesktop.DBus", "/org/freedesktop/DBus")
        try:
            pid = int(
                self.call(
                    dbus, "org.freedesktop.DBus", "GetConnectionUnixProcessID", "s", (bus_name,)
                )[0]
            )
        except CallError:
            pid = 0
        self._pid_cache[bus_name] = pid
        return pid

    # ── reading nodes ──

    def _role_name(self, node: Node) -> str:
        if node.role_id == ROLE_PASSWORD_TEXT:
            return "password text"
        name = self._role_names.get(node.role_id)
        if name is None:
            try:
                name = str(self.call(node.ref, ACCESSIBLE, "GetRoleName")[0])
            except CallError:
                # Never cached: a timeout from a slow application must not pin this
                # role id to a non-secure name for the life of the client.
                node.role_unknown = True
                return "unknown"
            self._role_names[node.role_id] = name
        return name

    def read_basic(self, nodes: "list[Node]", *, child_limit: int = 400) -> None:
        """Fill role, states, name, interfaces and child refs, in two pipelined batches.

        Children are fetched in a SECOND batch, after ``ChildCount`` is known, and that
        ordering is load-bearing. ``GetChildren`` on a spreadsheet asks the application
        to materialise every cell: LibreOffice Calc reports a sheet with billions of
        children, and one such call pinned it at 90% CPU and left it answering nothing
        for minutes. So a node with more than *child_limit* children, or one that
        declares ``MANAGES_DESCENDANTS`` (tables and trees whose children are created
        on demand), is enumerated with ``GetChildAtIndex`` up to a small bound instead,
        and ``children_capped`` records that the list is partial.
        """
        msgs: list[Any] = []
        for node in nodes:
            msgs.extend(
                (
                    _call(node.ref, ACCESSIBLE, "GetRole"),
                    _call(node.ref, ACCESSIBLE, "GetState"),
                    _call(node.ref, ACCESSIBLE, "GetInterfaces"),
                    _call(node.ref, PROPS, "GetAll", "s", (ACCESSIBLE,)),
                )
            )
        results = self.batch(msgs)
        for i, node in enumerate(nodes):
            role, state, ifaces, props = results[i * 4 : i * 4 + 4]
            if isinstance(role, CallError) or isinstance(state, CallError):
                node.failed = True
                continue
            node.role_id = int(role[0])
            node.states = _states(state[0])
            if not isinstance(ifaces, CallError):
                node.interfaces = frozenset(str(x) for x in ifaces[0])
            if not isinstance(props, CallError):
                mapping = props[0]
                node.name = str(_variant(mapping.get("Name", ("s", ""))) or "")
                node.description = str(_variant(mapping.get("Description", ("s", ""))) or "")
                node.child_count = int(_variant(mapping.get("ChildCount", ("i", 0))) or 0)

        plan: list[tuple[Node, str]] = []
        kid_msgs: list[Any] = []
        for node in nodes:
            if node.failed or node.child_count <= 0:
                continue
            managed = STATE_MANAGES_DESCENDANTS in node.states
            if node.child_count <= child_limit and not managed:
                plan.append((node, "all"))
                kid_msgs.append(_call(node.ref, ACCESSIBLE, "GetChildren"))
                continue
            bound = min(node.child_count, MANAGED_CHILD_LIMIT if managed else child_limit)
            node.children_capped = node.child_count > bound
            for index in range(bound):
                plan.append((node, "one"))
                kid_msgs.append(_call(node.ref, ACCESSIBLE, "GetChildAtIndex", "i", (index,)))
        for (node, kind), result in zip(plan, self.batch(kid_msgs)):
            if isinstance(result, CallError):
                node.children_failed = True
                continue
            if kind == "all":
                node.children = [Ref(str(b), str(p)) for b, p in result[0]]
            else:
                bus_name, path = result[0]
                ref = Ref(str(bus_name), str(path))
                if not ref.is_null:
                    node.children.append(ref)
        for node in nodes:
            if not node.failed:
                node.role = self._role_name(node)

    def read_details(self, nodes: "list[Node]", *, text_limit: int, skip_text: "set[int]") -> None:
        """Fill extents, text, value and actions for *nodes*, one pipelined batch.

        ``skip_text`` lists positions whose text must NOT be read (secure nodes): the
        call is never made, so credential bytes never cross into this process.
        """
        plan: list[tuple[int, str]] = []
        msgs: list[Any] = []
        for position, node in enumerate(nodes):
            if node.failed:
                continue
            if COMPONENT in node.interfaces:
                plan.append((position, "extents"))
                msgs.append(_call(node.ref, COMPONENT, "GetExtents", "u", (self.coord_type,)))
            if TEXT in node.interfaces and position not in skip_text and text_limit > 0:
                # The length first: LibreOffice answers GetText with an EMPTY string
                # when the end offset lies past the text, where GTK and Chromium clamp.
                # Asking for exactly min(length, limit+1) works on all of them, and
                # never pulls a whole document the way an end of -1 would.
                plan.append((position, "text_len"))
                msgs.append(_call(node.ref, PROPS, "Get", "ss", (TEXT, "CharacterCount")))
            if VALUE in node.interfaces and position not in skip_text:
                plan.append((position, "value"))
                msgs.append(_call(node.ref, PROPS, "Get", "ss", (VALUE, "CurrentValue")))
            if ACTION in node.interfaces:
                plan.append((position, "actions"))
                msgs.append(_call(node.ref, ACTION, "GetActions"))
        results = self.batch(msgs)
        text_reads: list[tuple[int, int]] = []
        for (position, kind), result in zip(plan, results):
            if isinstance(result, CallError):
                continue
            node = nodes[position]
            if kind == "extents":
                x, y, w, h = result[0]
                node.extents = (int(x), int(y), int(w), int(h))
            elif kind == "text_len":
                try:
                    length = int(_variant(result[0]))
                except (TypeError, ValueError):
                    length = 0
                if length > 0:
                    text_reads.append((position, min(length, text_limit + 1)))
            elif kind == "value":
                try:
                    node.value = float(_variant(result[0]))
                except (TypeError, ValueError):
                    node.value = None
            elif kind == "actions":
                # Keep EVERY entry, in order: the position is the DoAction index, so
                # dropping an unnamed entry would shift every later action onto its
                # neighbour.
                node.actions = tuple(str(entry[0]) if entry else "" for entry in result[0])
        if text_reads:
            texts = self.batch(
                [_call(nodes[p].ref, TEXT, "GetText", "ii", (0, end)) for p, end in text_reads]
            )
            for (position, _end), result in zip(text_reads, texts):
                if not isinstance(result, CallError):
                    nodes[position].text = str(result[0])
        # Chromium answers GetActions with LOCALIZED names, which are empty, while
        # GetName(i) carries the real ones ("press", "showContextMenu").
        unnamed = [(n, i) for n in nodes for i, name in enumerate(n.actions) if not name]
        if unnamed:
            names = self.batch([_call(n.ref, ACTION, "GetName", "i", (i,)) for n, i in unnamed])
            fixed: dict[int, list[str]] = {}
            for (node, index), result in zip(unnamed, names):
                current = fixed.setdefault(id(node), list(node.actions))
                if not isinstance(result, CallError) and result[0]:
                    current[index] = str(result[0])
                else:
                    current[index] = f"action{index}"
            for node in nodes:
                if id(node) in fixed:
                    node.actions = tuple(fixed[id(node)])

    def live(self, ref: Ref) -> Node:
        """A fresh read of one node (role + states + name), for pre-write checks."""
        node = Node(ref=ref, depth=0)
        self.read_basic([node])
        return node


_client: "AtspiClient | None" = None
_client_lock = threading.Lock()


def client() -> AtspiClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AtspiClient()
        return _client


def reset_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None
