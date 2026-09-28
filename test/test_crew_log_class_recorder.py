"""Every surface that changes a session's CLASS must record it.

The crew log records a session's class -- its memory mode, its owning app, and whether
its conversation is published to a channel -- because a reader deciding whether one
session may read another's log asks about the whole life of that log, and for a session
that has closed the log is the only thing left to ask. Sampling the class at each turn's
start cannot see a channel link that commits and is removed inside ONE turn, and content
authored through that link is in the log with nothing saying it was published.

So the record is taken where the fact BECOMES true. This file is what keeps that true as
the code grows: the site lists below are DERIVED from the source by walking it, never
written out by hand, so a link path added later fails here instead of silently reopening
the hole. An exemption has to name its reason, and an exemption for a site that does not
exist fails too -- a stale list is how a derived check quietly becomes a hand-written
one.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"

#: The recorder every link-commit site must reach.
RECORDER = "note_crew_log_class"

#: The session map's own announcement, which the mirror and Slack bind paths must make.
ANNOUNCER = "_note_bind"

#: Sites that assign a link WITHOUT recording, and why each is sound. Keyed by
#: ``<path relative to src/kiro_crew>:<function>``. A site missing from both this map and
#: the recorder's callers fails; an entry here naming a site that does not assign a
#: link fails as well.
LINK_EXEMPT: dict[str, str] = {
    "dashboard/chat_persistence.py:_apply_recent_session": (
        "a restore replays a link that was already recorded when it was first set, and "
        "it runs before the restored slot takes a turn, so the opening entry of its next "
        "session states the class it comes back with"
    ),
    "dashboard/chat_persistence.py:_rehydrate_slot_from_history": (
        "same as the restore above: replay, before any turn"
    ),
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _calls(node: ast.AST) -> set[str]:
    """Every attribute or bare name CALLED anywhere inside *node*."""
    names: set[str] = set()
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        target = inner.func
        if isinstance(target, ast.Attribute):
            names.add(target.attr)
        elif isinstance(target, ast.Name):
            names.add(target.id)
    return names


def _assigns_attribute(node: ast.AST, attribute: str) -> bool:
    """Whether *node* SETS *attribute* to something other than the empty string.

    A clearing assignment is skipped, and on a property of the site rather than as an
    exemption someone must maintain: the class fold holds each member at the most
    restrictive value the log ever recorded, so unlinking cannot make a log more
    restrictive and a record of it would change no reader's answer. The field's own
    declaration in the slot's constructor is the same shape and drops out here too.
    """
    for inner in ast.walk(node):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(inner, ast.Assign):
            targets = list(inner.targets)
            value = inner.value
        elif isinstance(inner, (ast.AugAssign, ast.AnnAssign)):
            targets = [inner.target]
            value = inner.value
        if isinstance(value, ast.Constant) and value.value == "":
            continue
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr == attribute:
                return True
    return False


def _records_beside_the_assignment(func: ast.AST) -> bool:
    """Whether every link assignment in *func* has the recorder as a BLOCK SIBLING.

    Not "somewhere in the function", and the difference is what the check is worth. A
    name that merely appears in the function is satisfied by a call the code never
    reaches -- wrap it in ``if False:`` and an occurrence check stays green while the
    record is dead -- and it is equally satisfied by a call on a DIFFERENT branch than
    the assignment, which records on one path and not the other.

    Requiring the two to be siblings in one statement list is the structural form of
    "the record is taken beside the change": moving the call into any nested block moves
    it out of that list, and the check fails.
    """
    for block in _statement_lists(func):
        assigns = any(_assigns_attribute_here(stmt, "linked_session_key") for stmt in block)
        if not assigns:
            continue
        if not any(RECORDER in _calls_here(stmt) for stmt in block):
            return False
    return True


def _statement_lists(node: ast.AST) -> list[list[ast.stmt]]:
    """Every statement LIST inside *node* -- a body, an orelse, a finalbody."""
    blocks: list[list[ast.stmt]] = []
    for inner in ast.walk(node):
        for field in ("body", "orelse", "finalbody"):
            value = getattr(inner, field, None)
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                blocks.append(value)
    return blocks


def _assigns_attribute_here(stmt: ast.stmt, attribute: str) -> bool:
    """Whether *stmt* ITSELF is a non-clearing assignment to *attribute*."""
    targets: list[ast.expr] = []
    value: ast.expr | None = None
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
        value = stmt.value
    elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
        targets = [stmt.target]
        value = stmt.value
    else:
        return False
    if isinstance(value, ast.Constant) and value.value == "":
        return False
    return any(isinstance(t, ast.Attribute) and t.attr == attribute for t in targets)


def _calls_here(stmt: ast.stmt) -> set[str]:
    """Names called by *stmt* itself, not by anything nested in a deeper block.

    An ``Expr`` wrapping a call is the shape a recorder call takes, so that is what is
    read. A call buried inside an ``if`` in the same list is that ``if``'s business, and
    counting it here would let a guarded call satisfy an unguarded assignment.
    """
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return set()
    target = stmt.value.func
    if isinstance(target, ast.Attribute):
        return {target.attr}
    if isinstance(target, ast.Name):
        return {target.id}
    return set()


def _link_assigning_functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """``<relative path>:<function>`` for every function that SETS a session's link."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "linked_session_key" not in text:
            continue
        rel = path.relative_to(SRC).as_posix()
        for func in _functions(_tree(path)):
            if _assigns_attribute(func, "linked_session_key"):
                found[f"{rel}:{func.name}"] = func
    return found


def test_every_link_assignment_records_the_class_it_changed():
    """MUTATION-SENSITIVE: the derived half. A new link path fails HERE.

    Counted by walking the source rather than by listing the paths, because the defect
    a hand-written list cannot see is an ASSIGNMENT SOMEWHERE ELSE -- and that is the
    shape this hole had. A site either calls the recorder in the same function or names
    its reason in :data:`LINK_EXEMPT`.
    """
    sites = _link_assigning_functions()
    assert sites, "the walk found no link assignment at all, so it is measuring nothing"
    unrecorded = sorted(
        key
        for key, func in sites.items()
        if not _records_beside_the_assignment(func) and key not in LINK_EXEMPT
    )
    assert not unrecorded, (
        "these set a session's channel link without recording the class it changed IN "
        "THE SAME BLOCK, so a link that commits and is removed inside one turn leaves "
        "content in the log with nothing saying it was published: "
        f"{unrecorded}. Call state.{RECORDER}(slot) beside the assignment -- beside, so "
        "a guard cannot disable it and a sibling branch cannot stand in for it -- or add "
        "it to LINK_EXEMPT with the reason it is sound."
    )


def test_no_exemption_names_a_site_that_is_gone():
    """A stale exemption is how a derived check becomes a hand-written one.

    Without this the list only ever grows: a site that stops assigning a link keeps its
    entry, and the next reader cannot tell a live exemption from a fossil.
    """
    sites = _link_assigning_functions()
    stale = sorted(key for key in LINK_EXEMPT if key not in sites)
    assert not stale, f"LINK_EXEMPT names sites that no longer assign a link: {stale}"


def test_every_committed_binding_is_announced_to_the_recorder():
    """MUTATION-SENSITIVE: the mirror half, pinned at the STORE rather than its callers.

    A mirror or Slack binding is committed inside the session map, and the map cannot
    know a session's memory mode or owning app -- so it announces the commit and the
    dashboard records. Pinning the announcement here rather than at the 20-odd call
    sites is what makes a third bind path safe by construction: any function in this
    module that persists a binding must announce it.

    Derived by asking which functions WRITE a binding key and persist it, so a new one
    is caught without being named.
    """
    path = SRC / "session_map.py"
    binding_keys = ("mirror", "slack_thread_ts")
    missing: list[str] = []
    for func in _functions(_tree(path)):
        writes_binding = False
        for inner in ast.walk(func):
            if not isinstance(inner, ast.Assign):
                continue
            for target in inner.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in binding_keys
                ):
                    writes_binding = True
        calls = _calls(func)
        if writes_binding and "_save" in calls and ANNOUNCER not in calls:
            missing.append(func.name)
    assert not missing, (
        "these persist a channel binding without announcing it, so the class record is "
        f"never taken for bindings they make: {sorted(missing)}. Call "
        f"self.{ANNOUNCER}(key) after the save."
    )


def test_the_manager_exposes_both_listener_sinks():
    """MUTATION-SENSITIVE: the wiring CALL has to resolve, not merely exist.

    ``wire_session_bind_listener`` reaches the session map through the session manager,
    and the manager wraps each module-level sink as its own method. Adding the sink and
    the wiring without the wrapper leaves a gateway that raises on every start -- which
    is not visible from the dashboard side at all: the wiring method is present, the
    import succeeds, and only an actual boot fails. Asking the manager for both names is
    the cheap form of that boot.
    """
    from kiro_crew.session import SessionManager

    for name in ("set_unbind_listener", "set_bind_listener"):
        assert callable(getattr(SessionManager, name, None)), (
            f"SessionManager has no {name}; the gateway wires it at start, so its "
            "absence is an AttributeError on every boot rather than a missing record"
        )


def test_the_recorder_is_wired_wherever_its_sibling_is():
    """The listener is useless unregistered, and one startup path is easy to miss.

    Both wiring calls sit together at every gateway start today. Pinning the PAIR rather
    than a count is what catches a new startup path that registers the notice and forgets
    the record.
    """
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        unbind = text.count("wire_session_unbind_listener()")
        bind = text.count("wire_session_bind_listener()")
        assert bind >= unbind, (
            f"{path.relative_to(SRC).as_posix()} wires the unbind notice "
            f"{unbind} time(s) and the class record {bind} -- a gateway that starts "
            "without the record takes none for a mid-turn link"
        )


def test_a_binding_before_the_log_exists_survives_being_removed():
    """MUTATION-SENSITIVE: the recorder MARKS a restriction it cannot yet append.

    The sequence a review lane named, and the one case the recorder's swallow-and-carry-on
    argument does not cover. That argument rests on the far end recording its own losses,
    which holds for a write the writer drops -- but a slot with no open log hands the
    writer nothing, so there is no loss to record and the fact simply vanishes.

    It is reachable without anything unusual: an idle session is bound to a channel, a
    turn is routed through that binding, and the link is removed before the session's
    first turn opens a log. Reading the class from the live slot at THAT point states
    never-published about a log whose content includes channel-authored words, and the
    dispatcher reading it would be told the session was private.

    So the mark is what carries the fact across, and it is never cleared -- consistent
    with the fold, which holds each member at the most restrictive value the log ever
    recorded.
    """
    from kiro_crew.dashboard.chat_runner import PENDING_CHANNEL_ATTR, _crew_log_class
    from kiro_crew.dashboard.state import note_crew_log_class

    class _Slot:
        key = "chat-1"
        memory_mode = "persistent"
        _app = ""
        _acp_client = None  # no live session yet, so no log to append to
        linked_session_key = "telegram:-100999"

    slot = _Slot()
    note_crew_log_class(None, slot)
    assert (
        getattr(slot, PENDING_CHANNEL_ATTR, False) is True
    ), "a binding the recorder could not append was dropped instead of marked"

    slot.linked_session_key = ""
    assert _crew_log_class(None, slot) == (
        "persistent",
        "",
        True,
    ), "the removed link left no trace, so the opening entry would say never-published"


def test_a_slot_that_was_never_bound_is_not_marked():
    """The accepting case: the mark is not a blanket restriction on every idle slot."""
    from kiro_crew.dashboard.chat_runner import PENDING_CHANNEL_ATTR, _crew_log_class
    from kiro_crew.dashboard.state import note_crew_log_class

    class _Slot:
        key = "chat-2"
        memory_mode = "persistent"
        _app = ""
        _acp_client = None
        linked_session_key = ""

    slot = _Slot()
    note_crew_log_class(None, slot)
    assert getattr(slot, PENDING_CHANNEL_ATTR, False) is False
    assert _crew_log_class(None, slot) == ("persistent", "", False)


def test_a_live_session_id_does_not_stop_the_mark(monkeypatch):
    """MUTATION-SENSITIVE: the mark is keyed on the restriction, not on lacking a sid.

    A session id is not evidence that a log exists. A restored session publishes its id
    while its log is still absent, so a class append made in that window finds no log and
    returns having written nothing AND recorded no loss -- the one shape the recorder's
    swallow-and-carry-on argument cannot cover, because that argument rests on the far end
    recording its own losses. Conditioning the mark on the id being ABSENT therefore misses
    it: a channel bound and removed inside the window leaves no trace, and the opening
    entry states never-published about a log holding channel-authored words.

    Both halves are asserted. The append is still attempted, so the mark BACKS the record
    rather than replacing it; a fix that only marked would pass a mark-only assertion while
    silently dropping the write for every session whose log does exist.
    """
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.dashboard.chat_runner import PENDING_CHANNEL_ATTR, _crew_log_class
    from kiro_crew.dashboard.state import note_crew_log_class

    seen: list[tuple[str, bool, str]] = []
    monkeypatch.setattr(
        crew_log_emit,
        "on_class_observed",
        lambda sid, *, memory, app, channel, workspace: seen.append((sid, channel, workspace)),
    )

    class _Client:
        # A restored session publishes its id before its first turn opens a log.
        session_id = "sess-restored"

    class _Slot:
        key = "chat-3"
        memory_mode = "persistent"
        _app = ""
        _acp_client = _Client()
        linked_session_key = "telegram:-100777"
        workspace = "alpha"

    slot = _Slot()
    note_crew_log_class(None, slot)

    assert seen == [
        ("sess-restored", True, "alpha")
    ], "the append was not attempted, so the mark replaced the record instead of backing it"
    assert (
        getattr(slot, PENDING_CHANNEL_ATTR, False) is True
    ), "a live session id suppressed the mark, so a log opened later states never-published"

    slot.linked_session_key = ""
    assert _crew_log_class(None, slot) == (
        "persistent",
        "",
        True,
    ), "the binding left no trace once the link was removed inside the window"
