"""Every site that changes a slot's history key joins the slot's alias group.

A slot's history key comes from ``linked_session_key`` and, for an unlinked slot,
``channel_origin``. Aliases share one ``TranscriptBinding`` only because
``bind_transcript`` runs after every change to either field. A new assignment site
that forgets the call silently gives that slot a private copy of the shared
project again, so this scan finds every assignment in ``src/kiro_crew`` and
requires its enclosing function to call ``bind_transcript``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
_FIELDS = {"linked_session_key", "channel_origin"}

#: (path under src/kiro_crew, function) pairs that assign a field without joining,
#: each with the reason the join happens elsewhere.
_EXEMPT = {
    # Field defaults for a new slot, which starts with its own private binding.
    ("dashboard/state_parts/slot_session.py", "init_session_fields"),
    ("dashboard/state_parts/slot_turns.py", "init_turns_fields"),
    # A row reader inside ``metadata_codec.apply``, which joins once after every row.
    ("dashboard/slot_persistence/metadata_codec.py", "_read_linked_session_key"),
}


def _assigned_fields(node: ast.AST) -> set[str]:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    return {t.attr for t in targets if isinstance(t, ast.Attribute) and t.attr in _FIELDS}


def _calls_bind(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = (
                f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            )
            if name == "bind_transcript":
                return True
    return False


def _unjoined_sites(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            hits = [n for n in ast.walk(fn) if _assigned_fields(n)]
            if not hits or (rel, fn.name) in _EXEMPT or _calls_bind(fn):
                continue
            found += [f"{rel}:{n.lineno} {fn.name}" for n in hits]
    return found


def test_every_history_key_change_joins_the_alias_group():
    assert _unjoined_sites(_SRC) == []


def test_the_exemptions_still_exist():
    """A stale exemption would hide a new site that reuses its name."""
    for rel, name in _EXEMPT:
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        names = {
            n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert name in names, f"{rel}:{name} is exempt but no longer exists"


def test_the_scan_can_fail(tmp_path):
    (tmp_path / "m.py").write_text(
        "def join(slot):\n    slot.linked_session_key = 'x'\n\n"
        "def ok(state, slot):\n    slot.channel_origin = True\n    bind_transcript(state, slot)\n",
        encoding="utf-8",
    )
    assert _unjoined_sites(tmp_path) == ["m.py:2 join"]


def test_a_rebound_channel_tab_joins_its_live_alias(tmp_path, monkeypatch):
    """The channel rebind links an existing slot; it must share the peer's directory."""
    from test_cleared_project_not_resurrected import _make_state

    from kiro_crew.dashboard import channel_slots

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    key = "slack:1.1"
    live = state.get_or_create_slot("w", linked_session_key=key)
    live.project = str(tmp_path / "p")
    unbound = state.get_or_create_slot("slack_1.1", channel_origin=True)
    unbound._channel_runtime_origin = True
    monkeypatch.setattr(channel_slots, "_sync_dashboard_slots", lambda _state: None)

    assert channel_slots._rebind_unbound_channel_slot(state, unbound, key)

    assert unbound._binding is live._binding
    assert unbound.project == str(tmp_path / "p")
