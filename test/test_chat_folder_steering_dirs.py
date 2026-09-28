"""Folder ``steering_dirs``: validation, accumulative inheritance, PATCH clears.

A chat folder may carry extra steering directories loaded for every chat in its
subtree. They validate per directory like ``project_dir`` (absolute, existing,
not sensitive) plus a list cap and no duplicates, resolve ACCUMULATIVELY up the
``parent_id`` chain (unlike the nearest-wins project resolver), and a PATCH with
an empty list clears them.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import folder_steering, pinned_fs
from kiro_crew.dashboard.chat_folders import (
    MAX_FOLDER_STEERING_DIR_LEN,
    MAX_FOLDER_STEERING_DIRS,
    _resolve_folder_steering_dirs,
    _validate_steering_dirs,
    api_chat_folder_create,
    api_chat_folder_update,
    slot_steering_principal,
)
from kiro_crew.dashboard.chat_runner import _folder_steering_turn
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.folder_steering import collect_folder_steering

#: The validator refuses a non-empty ``steering_dirs`` where a directory cannot
#: be opened relative to a descriptor (native Windows), so every test that
#: expects a directory to be ADMITTED or resolved carries this marker; the
#: lexical refusals (UNC, relative, empty, cap) run everywhere.
_needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_tree_walk(),
    reason="steering_dirs are refused where a directory cannot be opened relative to a descriptor",
)

# ── which turns resolve the folder's steering ──


class _Exec:
    def __init__(self, member_id: str | None) -> None:
        self.member_id = member_id


def _slot(mode: str = "chat", agent: str = "") -> Any:
    slot = MagicMock()
    slot.mode = mode
    slot.agent = agent
    return slot


@pytest.mark.parametrize(
    "context_is_new,provider_has_history,needs_reinjection,expected",
    [
        (True, False, False, True),  # fresh provider session
        (True, True, False, False),  # resumed: original injection already held
        (False, False, True, True),  # reinjection after compaction
        (False, False, False, False),  # warm template turn
    ],
)
def test_template_chat_resolves_only_on_session_start_turns(
    context_is_new, provider_has_history, needs_reinjection, expected
):
    assert (
        _folder_steering_turn(
            _slot(),
            _Exec(None),
            context_is_new=context_is_new,
            provider_has_history=provider_has_history,
            needs_reinjection=needs_reinjection,
        )
        is expected
    )


def test_warm_v2_member_turn_still_resolves_folder_steering():
    """The member essentials envelope is rebuilt every turn and declares itself
    the complete replacement for prior snapshots, so a warm member turn that
    resolved nothing would withdraw the folder's guides from the model."""
    assert (
        _folder_steering_turn(
            _slot("member", "eng-crew"),
            _Exec("m-123"),
            context_is_new=False,
            provider_has_history=True,
            needs_reinjection=False,
        )
        is True
    )


@pytest.mark.parametrize(
    "slot,execution",
    [
        (_slot("member", "eng-crew"), _Exec(None)),  # named member, no V2 identity
        (_slot("member", ""), _Exec("m-123")),  # member mode without a member pick
        (_slot("chat", "eng-crew"), _Exec("m-123")),  # template chat
        (_slot("member", "eng-crew"), None),  # no execution context at all
    ],
)
def test_warm_turn_without_a_v2_member_envelope_does_not_resolve(slot, execution):
    assert (
        _folder_steering_turn(
            slot,
            execution,
            context_is_new=False,
            provider_has_history=True,
            needs_reinjection=False,
        )
        is False
    )


# ── validation ──


def test_validate_rejects_relative_path():
    resolved, err = _validate_steering_dirs(["not/absolute"])
    assert resolved == []
    assert err and "absolute" in err.lower()


@_needs_pinned_walk
def test_validate_rejects_missing_directory(tmp_path):
    missing = str(tmp_path / "nope")
    resolved, err = _validate_steering_dirs([missing])
    assert resolved == []
    assert err and "existing directory" in err.lower()


def test_validate_refuses_a_non_empty_list_where_the_pinned_walk_is_unavailable(
    tmp_path, monkeypatch
):
    """Where the collector cannot walk pinned, the validator refuses BEFORE any
    filesystem call.

    A stored value would never be delivered on such a host, and validating it
    would itself be the by-name probe the collector refuses: ``isdir`` /
    ``realpath`` on a name an agent running as this user has swapped for a
    junction at a share is the outbound SMB authentication. The empty list stays
    valid so a folder can still CLEAR its steering there.
    """
    from kiro_crew.dashboard import chat_folders

    real = tmp_path / "standards"
    real.mkdir()
    monkeypatch.setattr(chat_folders.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    touched: list[str] = []
    monkeypatch.setattr(chat_folders, "validate_file_path", lambda p: touched.append(p) or str(p))
    monkeypatch.setattr(chat_folders.os.path, "isdir", lambda p: touched.append(p) or True)
    resolved, err = _validate_steering_dirs([str(real)])
    assert resolved == []
    assert err and "not supported on this platform" in err
    assert touched == [], "nothing may touch the filesystem on a host that cannot walk pinned"
    assert _validate_steering_dirs([]) == ([], None)


@_needs_pinned_walk
def test_validate_proves_existence_with_a_pinned_open_that_refuses_a_link(tmp_path):
    """The existence check opens the leaf ``O_DIRECTORY | O_NOFOLLOW``; a link is refused.

    ``os.path.isdir`` on the canonical name follows whatever sits there NOW,
    so a directory swapped for a link between the screen and the check would
    pass as an ordinary directory. Opening it pinned is the same test the
    collector will apply on every read, so the two cannot disagree.
    """
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    # A canonicalizer that leaves the link spelling in place stands in for the
    # swap-after-screen window: the screen saw a directory, the leaf is a link.
    from kiro_crew.dashboard import chat_folders

    resolved, err = _validate_steering_dirs([str(target)])
    assert err is None and resolved == [str(target.resolve())]
    import unittest.mock as um

    with um.patch.object(chat_folders, "validate_file_path", lambda p: str(link)):
        resolved, err = _validate_steering_dirs([str(link)])
    assert resolved == []
    assert err and "existing directory" in err.lower()


@_needs_pinned_walk
def test_validate_rejects_sensitive_path(tmp_path, monkeypatch):
    real = tmp_path / "standards"
    real.mkdir()
    monkeypatch.setattr("kiro_crew.dashboard.chat_folders.is_sensitive_path", lambda p: True)
    resolved, err = _validate_steering_dirs([str(real)])
    assert resolved == []
    assert err and "sensitive" in err.lower()


@_needs_pinned_walk
@pytest.mark.parametrize(
    ("which", "expect"),
    [
        ("is-workspace", "memory store"),
        # The hardened path screen already fences memory_stores/ as sensitive;
        # either refusal is the right outcome, and the screen wins by running first.
        ("inside-named-store", "sensitive location"),
        ("contains-config-dir", "memory store"),
    ],
)
def test_validate_rejects_a_root_that_crosses_a_memory_silo(tmp_path, monkeypatch, which, expect):
    """A named memory store is a silo; a steering root must not be, contain, or lie
    inside the Global workspace or the named-store tree.

    Folder steering hands an operator-pointed directory's Markdown to every
    chat in the folder -- a V2 member's included -- so a root at (or over) the
    person's ``workspace/`` would carry Global preferences, projects and history
    into that member's prompt with nothing going red.
    """
    from kiro_crew.config.loader import config_dir
    from kiro_crew.memory import WORKSPACE_DIR_NAME
    from kiro_crew.memory_stores import MEMORY_STORES_DIR_NAME

    base = config_dir()
    if which == "is-workspace":
        target = base / WORKSPACE_DIR_NAME
    elif which == "inside-named-store":
        target = base / MEMORY_STORES_DIR_NAME / "reviewer-store" / "notes"
    else:
        target = base
    target.mkdir(parents=True, exist_ok=True)
    resolved, err = _validate_steering_dirs([str(target)])
    assert resolved == []
    assert err and expect in err.lower()
    # A sibling of the silos under the same data home is fine: the fence is the
    # silo, not the data home.
    ok = base / "not-a-store"
    ok.mkdir(exist_ok=True)
    resolved, err = _validate_steering_dirs([str(ok)])
    assert err is None and resolved == [str(ok.resolve())]


def test_silo_fence_is_case_insensitive_like_the_sensitive_path_gate():
    """An alternate-case spelling of a silo is the same directory on macOS/Windows.

    ``Path.resolve()`` does not canonicalize case, so the fence casefolds both
    sides -- the same rule ``is_sensitive_path`` applies. Pinned on the pure
    predicate so it holds on case-sensitive CI too (there, folding can only
    over-refuse an alternate-case sibling, which is the safe side).
    """
    from pathlib import Path

    from kiro_crew.folder_steering import crosses_memory_silo

    silos = (Path("/Users/me/.kiro/crew/workspace"), Path("/Users/me/.kiro/crew/memory_stores"))
    for spelling in (
        "/Users/me/.kiro/crew/Workspace",  # the silo itself, alternate case
        "/USERS/ME/.kiro/crew/workspace/history",  # inside it
        "/users/me/.KIRO/crew",  # contains it
        "/Users/me/.kiro/crew/Memory_Stores/reviewer",  # inside the named-store tree
    ):
        assert crosses_memory_silo(Path(spelling), silos), spelling
    # Not a prefix match on a longer sibling name, in either case.
    for spelling in ("/Users/me/.kiro/crew/workspace-notes", "/Users/me/.kiro/crew/WorkspaceX"):
        assert not crosses_memory_silo(Path(spelling), silos), spelling


@_needs_pinned_walk
def test_validate_rejects_a_configured_absolute_workspace_as_a_silo(tmp_path, monkeypatch):
    """A V1 workspace configured at an ABSOLUTE path outside the data home is a silo too.

    ``workspace_dir_for`` returns such a directory verbatim and the memory
    layer opens ``preferences.md`` / ``history/`` there, so a steering root at
    it (or inside it) would hand that person's memory to every chat in the
    folder. The fence is built from the same resolver, so it follows the
    config rather than assuming ``config_dir()/workspace``.
    """
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.sections import WorkspaceConfig

    research = tmp_path / "elsewhere" / "research-ws"
    (research / "history").mkdir(parents=True)
    cfg = KiroCrewConfig()
    cfg.workspaces["research"] = WorkspaceConfig(dir=str(research))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    for target in (research, research / "history", research.parent):
        resolved, err = _validate_steering_dirs([str(target)])
        assert resolved == [] and err and "memory store" in err.lower(), target
    # An unrelated sibling of the configured workspace is still admitted.
    other = tmp_path / "elsewhere" / "standards"
    other.mkdir()
    resolved, err = _validate_steering_dirs([str(other)])
    assert err is None and resolved == [str(other.resolve())]


@_needs_pinned_walk
def test_validate_bounds_the_retained_canonical_string_not_only_the_submission(
    tmp_path, monkeypatch
):
    """The length bound applies to the field as STORED.

    A short submission can expand past the bound through ``~`` or a link chain;
    ``folders.json`` retains the expansion and every later resolve re-validates
    it, so an unbounded stored value would trip the length check on every read
    and silently drop the chain's steering. Refuse at write time instead.
    """
    from kiro_crew.dashboard import chat_folders as cf

    real = tmp_path / "standards"
    real.mkdir()
    short = str(tmp_path / "s")
    # Force the canonical form past the bound without building a 4 KB tree:
    # the screen is what expands the path, so stand in for its expansion.
    long_canonical = str(real) + "/" + "x" * cf.MAX_FOLDER_STEERING_DIR_LEN
    monkeypatch.setattr(cf, "validate_file_path", lambda _p: long_canonical)
    resolved, err = _validate_steering_dirs([short])
    assert resolved == []
    assert err and "expands to more than" in err
    # The bound is the same constant on both sides of the expansion.
    assert str(cf.MAX_FOLDER_STEERING_DIR_LEN) in err


def test_validate_rejects_over_cap(tmp_path):
    dirs = []
    for i in range(MAX_FOLDER_STEERING_DIRS + 1):
        d = tmp_path / f"d{i}"
        d.mkdir()
        dirs.append(str(d))
    resolved, err = _validate_steering_dirs(dirs)
    assert resolved == []
    assert err and str(MAX_FOLDER_STEERING_DIRS) in err


def test_validate_rejects_an_over_long_entry_before_touching_the_filesystem(monkeypatch):
    """The directory-count cap alone would retain 16 strings of any size."""
    import kiro_crew.dashboard.chat_folders as cf

    touched = MagicMock()
    monkeypatch.setattr(cf, "validate_file_path", touched)
    long_entry = "/" + "a" * MAX_FOLDER_STEERING_DIR_LEN
    resolved, err = _validate_steering_dirs([long_entry])
    assert resolved == []
    assert err and str(MAX_FOLDER_STEERING_DIR_LEN) in err
    touched.assert_not_called()


@pytest.mark.parametrize("unc", [r"\\evil\share\steering", "//evil/share/steering"])
def test_validate_refuses_a_unc_path_before_resolving_it(monkeypatch, unc):
    """``realpath``/``isdir`` on ``\\\\host\\share`` opens an SMB connection on
    Windows -- an outbound credential probe -- so a UNC-shaped entry must be
    refused lexically, before ``validate_file_path`` ever runs on it."""
    import kiro_crew.dashboard.chat_folders as cf

    touched = MagicMock()
    monkeypatch.setattr(cf, "validate_file_path", touched)
    monkeypatch.setattr(cf, "unc_probe_allowed", lambda raw: False)
    resolved, err = _validate_steering_dirs([unc])
    assert resolved == []
    assert err and "UNC" in err
    touched.assert_not_called()


@_needs_pinned_walk
def test_validate_admits_through_the_hardened_screen_not_bare_realpath(tmp_path, monkeypatch):
    """Every entry is canonicalized by ``validate_file_path`` and its verdict wins.

    The lexical UNC gate cannot see a LOCAL Windows junction aimed at a share,
    and a bare ``realpath``/``isdir`` on the raw input follows it -- the
    outbound SMB probe. ``validate_file_path`` screens link targets before any
    resolution, so the validator must both call it on the raw entry and treat
    its refusal as final, never falling back to resolving the name itself.
    """
    import kiro_crew.dashboard.chat_folders as cf

    real = tmp_path / "standards"
    real.mkdir()
    seen: list[str] = []
    real_screen = cf.validate_file_path

    def _spy(raw: str) -> str | None:
        seen.append(raw)
        return real_screen(raw)

    monkeypatch.setattr(cf, "validate_file_path", _spy)
    resolved, err = _validate_steering_dirs([str(real)])
    assert err is None and resolved == [str(real.resolve())]
    assert seen == [str(real)]

    monkeypatch.setattr(cf, "validate_file_path", lambda raw: None)
    audited: list[dict] = []
    monkeypatch.setattr(
        cf, "sel", lambda: MagicMock(log_api_access=lambda **kw: audited.append(kw))
    )
    resolved, err = _validate_steering_dirs([str(real)])
    assert resolved == []
    assert err and "link" in err.lower()
    # The screen fences sensitive paths itself, so its refusal is where a
    # sensitive submission is denied in practice: it must reach the audit log.
    (event,) = audited
    assert event["outcome"] == "denied"
    assert event["operation"] == "chat.folder_steering_dirs"
    assert event["resources"] == str(real)


@_needs_pinned_walk
def test_validate_rejects_duplicates(tmp_path):
    d = tmp_path / "standards"
    d.mkdir()
    # Two spellings of one directory still count as a duplicate (compared by the
    # resolved realpath).
    resolved, err = _validate_steering_dirs([str(d), str(d) + "/."])
    assert resolved == []
    assert err and "repeat" in err.lower()


def test_validate_accepts_empty_list_and_refuses_explicit_none():
    """``[]`` is the one spelling that clears; an explicit ``None`` is malformed.

    Accepting ``null`` as "clear" would let a single ordinary PATCH silently
    drop a folder's configured directories.
    """
    assert _validate_steering_dirs([]) == ([], None)
    resolved, err = _validate_steering_dirs(None)
    assert resolved == [] and err and "list of strings" in err


@_needs_pinned_walk
def test_validate_resolves_and_dedups_ok(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    resolved, err = _validate_steering_dirs([str(a), str(b)])
    assert err is None
    assert resolved == [str(a.resolve()), str(b.resolve())]


# ── accumulative inheritance + cycle guard ──


@_needs_pinned_walk
def test_resolver_accumulates_root_first(tmp_path):
    org = tmp_path / "org"
    repo = tmp_path / "repo"
    org.mkdir()
    repo.mkdir()
    folders: list[dict[str, Any]] = [
        {"id": "root", "parent_id": None, "steering_dirs": [str(org)]},
        {"id": "child", "parent_id": "root", "steering_dirs": [str(repo)]},
    ]
    resolved, err = _resolve_folder_steering_dirs(folders, "child")
    assert err is None
    # Root ancestor's dirs lead, child's follow.
    assert resolved == [str(org.resolve()), str(repo.resolve())]


@_needs_pinned_walk
def test_resolver_dedups_across_levels(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    folders: list[dict[str, Any]] = [
        {"id": "root", "parent_id": None, "steering_dirs": [str(shared)]},
        {"id": "child", "parent_id": "root", "steering_dirs": [str(shared)]},
    ]
    resolved, err = _resolve_folder_steering_dirs(folders, "child")
    assert err is None
    assert resolved == [str(shared.resolve())]


@_needs_pinned_walk
def test_resolver_cycle_guarded(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    # A parent_id cycle must terminate, not spin.
    folders: list[dict[str, Any]] = [
        {"id": "x", "parent_id": "y", "steering_dirs": [str(a)]},
        {"id": "y", "parent_id": "x"},
    ]
    resolved, err = _resolve_folder_steering_dirs(folders, "x")
    assert err is None
    assert resolved == [str(a.resolve())]


@_needs_pinned_walk
@pytest.mark.parametrize(
    ("slot_app", "expect_app_dir"),
    [("", False), ("other-app", False), ("acme", True)],
    ids=["human-slot", "foreign-app-slot", "owning-app-slot"],
)
def test_resolver_gates_app_owned_steering_by_slot_owner(tmp_path, slot_app, expect_app_dir):
    """An app's folder steers only that app's own slots.

    Steering accumulates up the parent chain, and a person can file a chat into
    (or nest a folder under) a folder an app created. Without this gate the app
    would write rules into the person's model context through the chain -- a
    confused deputy the tree-shaping ownership rules never intended. The
    person's own folders keep contributing to everyone beneath them.
    """
    app_dir = tmp_path / "app-rules"
    person_dir = tmp_path / "person-rules"
    app_dir.mkdir()
    person_dir.mkdir()
    folders: list[dict[str, Any]] = [
        {"id": "app-root", "parent_id": None, "owner_app": "acme", "steering_dirs": [str(app_dir)]},
        {"id": "mine", "parent_id": "app-root", "steering_dirs": [str(person_dir)]},
    ]
    resolved, err = _resolve_folder_steering_dirs(folders, "mine", slot_app=slot_app)
    assert err is None
    assert (str(app_dir.resolve()) in resolved) is expect_app_dir
    assert str(person_dir.resolve()) in resolved


def _member_execution(store: str) -> Any:
    """A V2 member execution bound to *store*, as ``_run_chat`` captures it."""
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    return ExecutionContext(
        "reviewer",
        MemoryStoreRef(store_id=store, member_id="reviewer"),
        "member",
        "kirocrew",
        selection_name="reviewer",
    )


@pytest.mark.parametrize(
    ("app", "execution", "expected"),
    [
        ("", None, ""),
        ("acme", None, "acme"),
        ("", "reviewer-store", "member:reviewer-store"),
        ("acme", "reviewer-store", "acme"),
    ],
    ids=["person", "app", "member", "app-claim-first"],
)
def test_slot_steering_principal_mirrors_folder_principal(app, execution, expected):
    """The slot side spells its principal exactly as ``owner_app`` is stamped.

    ``folder_principal`` stamps an app's bare name, or ``member:<store>`` for
    a crew member; the resolver compares ``owner_app`` against the value this
    helper returns. Any drift between the two alphabets silently drops a
    principal's own steering, which is exactly the member-slot defect this pins.
    """
    slot = MagicMock()
    slot._app = app
    ctx = _member_execution(execution) if execution else None
    assert slot_steering_principal(slot, ctx) == expected


@_needs_pinned_walk
def test_member_owned_folder_steers_the_members_own_slot(tmp_path):
    """A crew member's folder (``owner_app="member:<store>"``) reaches its chat.

    Before this pin, the resolver compared ``owner_app`` against ``slot._app``,
    which is empty for a member slot, so every member-owned folder's steering
    was skipped -- silently and on the documented member path. The fence still
    holds against everyone else: the person's slot and another member's slot
    receive nothing from the member's folder.
    """
    member_dir = tmp_path / "member-rules"
    member_dir.mkdir()
    folders: list[dict[str, Any]] = [
        {
            "id": "m-root",
            "parent_id": None,
            "owner_app": "member:reviewer-store",
            "steering_dirs": [str(member_dir)],
        },
    ]
    slot = MagicMock()
    slot._app = ""
    own = slot_steering_principal(slot, _member_execution("reviewer-store"))
    other = slot_steering_principal(slot, _member_execution("other-store"))
    person = slot_steering_principal(slot, None)
    assert _resolve_folder_steering_dirs(folders, "m-root", slot_app=own)[0] == [
        str(member_dir.resolve())
    ]
    assert _resolve_folder_steering_dirs(folders, "m-root", slot_app=other)[0] == []
    assert _resolve_folder_steering_dirs(folders, "m-root", slot_app=person)[0] == []


def test_resolver_empty_when_no_dirs():
    folders = [{"id": "root", "parent_id": None}]
    assert _resolve_folder_steering_dirs(folders, "root") == ([], None)


@_needs_pinned_walk
def test_resolver_refile_reresolves_to_new_folder(tmp_path):
    # Re-filing a slot from one folder to another re-resolves its effective
    # steering dirs from the NEW folder_id — the seam resolves per folder_id, so
    # a slot moved between these folders sees a different result each time.
    orig = tmp_path / "orig"
    dest = tmp_path / "dest"
    orig.mkdir()
    dest.mkdir()
    folders = [
        {"id": "a", "parent_id": None, "steering_dirs": [str(orig)]},
        {"id": "b", "parent_id": None, "steering_dirs": [str(dest)]},
    ]
    from_a, _ = _resolve_folder_steering_dirs(folders, "a")
    from_b, _ = _resolve_folder_steering_dirs(folders, "b")
    assert from_a == [str(orig.resolve())]
    assert from_b == [str(dest.resolve())]


@_needs_pinned_walk
def test_resolver_skips_a_bad_stored_entry_and_keeps_the_rest(tmp_path, caplog):
    """folders.json is not trusted, but one stale entry must not drop the chain.

    A stored RELATIVE path (or a directory that has since vanished) fails
    re-validation and is skipped at read time with a warning naming the
    folder; the well-formed sibling and the ancestor's directory still steer.
    Only a MALFORMED shape fails the whole resolution.
    """
    good = tmp_path / "good"
    good.mkdir()
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    gone = tmp_path / "gone"
    folders = [
        {"id": "root", "parent_id": None, "steering_dirs": [str(ancestor)]},
        {
            "id": "a",
            "parent_id": "root",
            "steering_dirs": ["relative/dir", str(gone), str(good)],
        },
    ]
    with caplog.at_level(logging.WARNING, logger="kiro_crew.dashboard.chat_folders"):
        resolved, err = _resolve_folder_steering_dirs(folders, "a")
    assert err is None
    assert resolved == [str(ancestor.resolve()), str(good.resolve())]
    skipped = [r.getMessage() for r in caplog.records if "skipped at read time" in r.message]
    assert len(skipped) == 2 and all("folder a" in m for m in skipped)
    # A malformed stored SHAPE is a corrupt folders.json, not a stale entry.
    malformed = [{"id": "m", "parent_id": None, "steering_dirs": ["ok", 7]}]
    resolved, err = _resolve_folder_steering_dirs(malformed, "m")
    assert resolved == [] and err is not None
    # ...and a falsy non-list (``{}``, ``""``) is malformed too, not "none
    # declared": only ``None`` and ``[]`` mean the folder declares nothing.
    for shape in ({}, ""):
        corrupt = [{"id": "c", "parent_id": None, "steering_dirs": shape}]
        resolved, err = _resolve_folder_steering_dirs(corrupt, "c")
        assert resolved == [] and err is not None, shape
    for none_declared in (None, []):
        clean = [{"id": "n", "parent_id": None, "steering_dirs": none_declared}]
        assert _resolve_folder_steering_dirs(clean, "n") == ([], None)


# ── handler wiring: create + PATCH clears ──


def _state(folders: list[dict[str, Any]]) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._folders = folders
    slot = _ChatSlot("chat-1-100")
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.conversation_log = None

    async def _mutate(fn: Any, on_committed: Any = None) -> Any:
        changed, value = fn(state._folders)
        if changed and on_committed is not None:
            on_committed()
        return value

    async def _read(fn: Any) -> Any:
        return fn(state._folders)

    state.mutate_folders = _mutate
    state.read_folders = _read
    return state


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler: Any) -> Any:
        request["app"] = ""
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/folders", api_chat_folder_create)
    app.router.add_patch("/api/chat/folders/{id}", api_chat_folder_update)
    return app


@pytest.mark.asyncio
@_needs_pinned_walk
async def test_create_stores_steering_dirs(tmp_path):
    d = tmp_path / "standards"
    d.mkdir()
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Org", "steering_dirs": [str(d)]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 201, await resp.text()
        body = await resp.json()
    assert body["steering_dirs"] == [str(d.resolve())]


@pytest.mark.asyncio
async def test_create_rejects_non_array_steering_dirs():
    state = _state([])
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/folders",
            json={"name": "Org", "steering_dirs": "not-a-list"},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "steering_dirs_invalid"


@pytest.mark.asyncio
async def test_patch_empty_list_clears(tmp_path):
    d = tmp_path / "standards"
    d.mkdir()
    folders = [
        {
            "id": "fldr0001",
            "name": "Org",
            "parent_id": None,
            "order": 0,
            "steering_dirs": [str(d.resolve())],
        }
    ]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"steering_dirs": []},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 200, await resp.text()
    # Empty list drops the key entirely, so "absent means none" stays canonical.
    assert "steering_dirs" not in folders[0]


@pytest.mark.asyncio
async def test_patch_explicit_null_is_refused_and_keeps_the_stored_list(tmp_path):
    """``{"steering_dirs": null}`` is 400, not "clear": the configured list survives."""
    d = tmp_path / "standards"
    d.mkdir()
    stored = [str(d.resolve())]
    folders = [
        {"id": "fldr0001", "name": "Org", "parent_id": None, "order": 0, "steering_dirs": stored}
    ]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"steering_dirs": None},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400, await resp.text()
        body = await resp.json()
    assert body["code"] == "steering_dirs_invalid"
    assert folders[0]["steering_dirs"] == stored


@pytest.mark.asyncio
async def test_patch_rejects_bad_dir(tmp_path):
    folders = [{"id": "fldr0001", "name": "Org", "parent_id": None, "order": 0}]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"steering_dirs": [str(tmp_path / "missing")]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "steering_dirs_invalid"


@_needs_pinned_walk
def test_validate_rejects_path_the_os_cannot_spell():
    """An embedded NUL is rejected, never raised.

    On POSIX ``realpath``/``lstat`` raise ``ValueError`` for the NUL and the
    validator must turn that into an ordinary rejection (a 400, not a 500).
    Windows ``realpath`` tolerates the byte and the path then fails the normal
    existing-directory rule -- also a rejection. The contract is the same on
    both: no exception escapes, nothing resolves.
    """
    resolved, err = _validate_steering_dirs(["/tmp/a\x00b"])
    assert resolved == []
    assert err and err.startswith("Steering directory")


@pytest.mark.asyncio
async def test_patch_with_nul_path_returns_400_not_500():
    folders = [{"id": "fldr0001", "name": "Org", "parent_id": None, "order": 0}]
    state = _state(folders)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.patch(
            "/api/chat/folders/fldr0001",
            json={"steering_dirs": ["/tmp/a\u0000b"]},
            headers={"X-Session-Key": "dashboard:chat-1-100"},
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "steering_dirs_invalid"


# ── refuse rather than fall back where descriptor-relative opens are unavailable ──


def test_walk_refuses_where_descriptor_relative_opens_are_unavailable(
    tmp_path, monkeypatch, caplog
):
    """No by-name fallback: refuse rather than walk names.

    A by-name walk is not a weaker mode of the pinned one -- its check-then-open
    window is exactly what an agent running as this user can win by swapping
    an ancestor for a junction. Where the platform cannot open relative to a
    descriptor the walk yields nothing and says so once at warning, so folder
    steering is simply absent on such a host rather than probed by name. Lives
    here (not with the collector tests) because it must run on Windows too.
    """
    root = tmp_path / "standards"
    root.mkdir()
    (root / "own.md").write_text("Prefer small diffs.", encoding="utf-8")
    monkeypatch.setattr(folder_steering.pinned_fs, "supports_pinned_tree_walk", lambda: False)
    listed: list[object] = []
    real_scandir = os.scandir

    def _spy(arg, *a, **kw):
        listed.append(arg)
        return real_scandir(arg, *a, **kw)

    monkeypatch.setattr(folder_steering.os, "scandir", _spy)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.folder_steering"):
        docs = collect_folder_steering([str(root)], project=None, home=tmp_path / "home")
    assert docs == []
    assert listed == []  # nothing was listed by name
    assert any("relative to a descriptor" in r.getMessage() for r in caplog.records)
