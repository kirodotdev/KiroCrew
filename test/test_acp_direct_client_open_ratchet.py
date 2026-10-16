"""Ratchet: the direct ACP client opens a session only through its checked send.

``AcpClient._send_request`` refuses a ``session/new|load|resume`` on a launch the
main agent spec does not currently bind. These tests pin that every open the direct
client sends reaches that check, and plant the frames a call-name scan alone
would miss to prove the scan sees them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_crew.acp.types import METHOD_SESSION_LOAD, METHOD_SESSION_NEW


def _direct_client_trees() -> list[tuple[str, ast.Module]]:
    import sys

    import kiro_crew.acp.client as client_mod

    modules = [client_mod, *(sys.modules[owner] for owner in client_mod._CLIENT_OWNERS)]
    return [
        (Path(m.__file__).name, ast.parse(Path(m.__file__).read_text(encoding="utf-8")))
        for m in modules
    ]


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return ""


_OPEN_NAMES = {"METHOD_SESSION_NEW", "METHOD_SESSION_LOAD", "METHOD_SESSION_RESUME"}
_OPEN_LITERALS = {"session/new", "session/load", "session/resume"}
#: The methods the checked send admits: the opens plus the mode switch. A
#: notification sender may name none of them.
_GUARDED_NAMES = _OPEN_NAMES | {"METHOD_SET_MODE"}
_GUARDED_LITERALS = _OPEN_LITERALS | {"session/set_mode"}
#: The request writer and the only scopes that may name it: the checked send
#: and the class body binding it onto ``AcpClient``.
_WRITER_SCOPES = {
    "_write_request_bounded": {
        "client_transport.py::_send_request",
        "client.py::AcpClient",
    },
}
_NOTIFICATION_WRITER = "write_notification_best_effort"


def _folded_str(node: ast.AST) -> str | None:
    """The string an expression of string literals spells, or None."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _folded_str(node.left), _folded_str(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_folded_str(v) for v in node.values]
        return None if None in parts else "".join(p for p in parts if p is not None)
    return None


def _is_open(
    node: ast.AST,
    aliases: set[str],
    names: set[str] = _OPEN_NAMES,
    literals: set[str] = _OPEN_LITERALS,
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names or node.id in aliases
    if isinstance(node, ast.IfExp):
        return _is_open(node.body, aliases, names, literals) or _is_open(
            node.orelse, aliases, names, literals
        )
    return _folded_str(node) in literals


def _is_guarded(node: ast.AST, aliases: set[str]) -> bool:
    return _is_open(node, aliases, _GUARDED_NAMES, _GUARDED_LITERALS)


def _names(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else None


def _scoped_nodes(tree: ast.Module):
    """Yield ``(scope, node)``: the innermost enclosing def or class of each node."""

    def walk(node: ast.AST, scope: str):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = child.name
            yield inner, child
            yield from walk(child, inner)

    yield from walk(tree, "<module>")


def _direct_client_open_sends(trees: list[tuple[str, ast.Module]]) -> dict[str, set[str]]:
    """Every direct-client send of a session-open verb, sorted by how it is sent."""
    found: dict[str, set[str]] = {
        "raw": set(),
        "checked": set(),
        "builders": set(),
        "writers": set(),
        "stray_writer_refs": set(),
        "open_notifications": set(),
    }
    for filename, tree in trees:
        functions = {
            fn.name
            for fn in ast.walk(tree)
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for scope, node in _scoped_nodes(tree):
            named = _names(node)
            if named in _WRITER_SCOPES and f"{filename}::{scope}" not in _WRITER_SCOPES[named]:
                # Any reference, so a writer bound to another name is still seen.
                found["stray_writer_refs"].add(f"{filename}:{node.lineno} {named}")
            if named == _NOTIFICATION_WRITER and scope not in functions:
                # Outside a function it could be rebound and called from one
                # the per-function verb check below would not see.
                found["stray_writer_refs"].add(f"{filename}:{node.lineno} {named}")
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assigns = [
                node
                for node in ast.walk(fn)
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name)
            ]
            aliases = {
                t.id
                for node in assigns
                if _is_open(node.value, set())
                for t in node.targets
                if isinstance(t, ast.Name)
            }
            guarded_aliases = {
                t.id
                for node in assigns
                if _is_guarded(node.value, set())
                for t in node.targets
                if isinstance(t, ast.Name)
            }
            # Any reference, so a writer bound to a local name still counts.
            notifies = any(_names(n) == _NOTIFICATION_WRITER for n in ast.walk(fn))
            for node in ast.walk(fn):
                # A notification sender may name no open or mode-switch verb
                # anywhere, however the frame carrying it is built. A notification
                # on the open session (``session/cancel``) is allowed anywhere.
                if notifies and _is_guarded(node, guarded_aliases):
                    found["open_notifications"].add(f"{filename}:{node.lineno}")
                if not isinstance(node, ast.Call):
                    continue
                name = _call_name(node)
                site = f"{filename}:{node.lineno}"
                if name == "JsonRpcRequest":
                    found["builders"].add(f"{filename}::{fn.name}")
                if name == "_write_request_bounded":
                    found["writers"].add(f"{filename}::{fn.name}")
                if node.args and _is_open(node.args[0], aliases):
                    found["checked" if name == "_send_request" else "raw"].add(site)
    return found


_ALIASED_REQUEST_WRITER = """
async def _planted_alias_open(self):
    w = self._write_request_bounded
    await w(b'{"jsonrpc":"2.0","id":1,"method":"session/new"}\\n', 1, "x")
"""
_HAND_BUILT_NOTIFICATION = """
async def {fn}(self):
    from kiro_crew.acp.transport_framing import write_notification_best_effort
    frame = {{"jsonrpc": "2.0", "id": 1, "method": "session/" + "new", "params": {{}}}}
    await write_notification_best_effort(
        self._process.stdin, None, json.dumps(frame).encode(), bound_secs=1
    )
"""
_SET_MODE_NOTIFICATION = """
async def _planted_mode_switch(self):
    from kiro_crew.acp.transport_framing import write_notification_best_effort
    method = METHOD_SET_MODE
    frame = {"jsonrpc": "2.0", "method": method, "params": {"modeId": "x"}}
    await write_notification_best_effort(self._process.stdin, None, frame, bound_secs=1)
"""
_MODULE_LEVEL_ALIAS = """
_send_frame = write_notification_best_effort
"""
_CANCEL_NOTIFICATION = """
async def _planted_cancel(self):
    frame = {"jsonrpc": "2.0", "method": METHOD_CANCEL, "params": {"sessionId": "s"}}
    await write_notification_best_effort(self._process.stdin, None, frame, bound_secs=1)
"""


def _planted(filename: str, source: str) -> list[tuple[str, ast.Module]]:
    """The real owner trees with ``source`` appended to ``filename``'s; none on disk."""
    trees = []
    for name, tree in _direct_client_trees():
        if name == filename:
            planted = ast.parse(source)
            tree = ast.Module(body=[*tree.body, *planted.body], type_ignores=[])
        trees.append((name, tree))
    return trees


@pytest.mark.parametrize(
    ("filename", "source", "caught_as"),
    [
        ("client_lifecycle.py", _ALIASED_REQUEST_WRITER, "stray_writer_refs"),
        (
            "client_lifecycle.py",
            _HAND_BUILT_NOTIFICATION.format(fn="_planted_dict_open"),
            "open_notifications",
        ),
        (
            "client_turn_control.py",
            _HAND_BUILT_NOTIFICATION.format(fn="cancel_session"),
            "open_notifications",
        ),
        ("client_turn_control.py", _SET_MODE_NOTIFICATION, "open_notifications"),
        ("client_turn_control.py", _MODULE_LEVEL_ALIAS, "stray_writer_refs"),
    ],
    ids=[
        "aliased-request-writer",
        "hand-built-notification",
        "open-verb-in-cancel-sender",
        "set-mode-notification",
        "module-level-notification-alias",
    ],
)
def test_the_direct_client_ratchet_catches_a_planted_open_frame(filename, source, caught_as):
    """Self-test: open frames the name-based scan alone could not see are flagged.

    Each source is planted into a copy of the real owner module's tree; nothing
    is written to disk.
    """
    found = _direct_client_open_sends(_planted(filename, source))
    assert found["raw"] == set(), "the call-name check sees it on its own"
    assert found[caught_as], f"the planted frame in {filename} is not caught"


@pytest.mark.parametrize("filename", ["client_lifecycle.py", "client_turn_control.py"])
def test_a_notification_on_the_open_session_is_not_flagged(filename):
    """A ``session/cancel`` write from any direct-client function passes.

    It names no open or mode-switch verb, so it can only act on the session
    already open; the ratchet holds the verb, not the call site.
    """
    found = _direct_client_open_sends(_planted(filename, _CANCEL_NOTIFICATION))
    assert found["stray_writer_refs"] == set(), sorted(found["stray_writer_refs"])
    assert found["open_notifications"] == set(), sorted(found["open_notifications"])


def test_every_direct_client_session_open_goes_through_the_checked_send():
    """Ratchet: the direct client sends no ``session/new|load|resume`` raw.

    The direct client is ``AcpClient`` and the owner modules it binds members
    from (``client._CLIENT_OWNERS``), so a member moved to a new owner stays in
    the scan. Each call passing an open verb (a constant, a literal, or a local
    bound to either) must be ``_send_request``; that function is the only place a
    request frame is built and the only caller of the bounded request write, and
    its launch-bound check runs before both. A frame writer may be named only
    where it is used today, so one bound to another name is still seen, and a
    function writing a notification names no open or mode-switch verb at all.
    """
    found = _direct_client_open_sends(_direct_client_trees())
    assert found["raw"] == set(), f"raw direct-client session-open sends at {sorted(found['raw'])}"
    # Two session/new sends, the load-or-resume restore and the entitlement probe.
    assert len(found["checked"]) == 4, sorted(found["checked"])
    assert found["builders"] == {"client_transport.py::_send_request"}
    assert found["writers"] == {"client_transport.py::_send_request"}
    assert found["stray_writer_refs"] == set(), sorted(found["stray_writer_refs"])
    assert found["open_notifications"] == set(), sorted(found["open_notifications"])

    from kiro_crew.acp import client_transport

    assert client_transport._SESSION_OPEN_METHODS == {
        METHOD_SESSION_NEW,
        METHOD_SESSION_LOAD,
        "session/resume",
    }
    send = next(
        fn
        for _name, tree in _direct_client_trees()
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "_send_request"
    )
    # In source order (ast.walk is breadth-first).
    order = [
        name
        for _line, name in sorted(
            (node.lineno, node.id if isinstance(node, ast.Name) else _call_name(node))
            for node in ast.walk(send)
            if (isinstance(node, ast.Name) and node.id == "require_main_spec_launch_bound")
            or (
                isinstance(node, ast.Call)
                and _call_name(node) in {"JsonRpcRequest", "_write_request_bounded"}
            )
        )
    ]
    assert order.index("require_main_spec_launch_bound") < order.index("JsonRpcRequest")
    assert order.index("require_main_spec_launch_bound") < order.index("_write_request_bounded")


def test_the_launch_agent_set_mode_is_held_like_an_open():
    """The direct client checks its ``set_mode`` with the same launch bound as its opens.

    kiro-cli resolves that frame's bare name on an unbound launch exactly as it
    resolves an open's, and the shared runtime refuses it during an episode, so
    the two clients agree. Every request the direct client writes goes through
    ``_send_request`` (the writer-scope ratchet above), which admits these.
    """
    from kiro_crew.acp import client_transport
    from kiro_crew.acp.types import METHOD_SESSION_RESUME, METHOD_SET_MODE

    assert client_transport._ADMITTED_METHODS == {
        METHOD_SESSION_NEW,
        METHOD_SESSION_LOAD,
        METHOD_SESSION_RESUME,
        METHOD_SET_MODE,
    }
