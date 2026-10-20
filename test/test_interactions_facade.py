"""``kiro_crew.slack.interactions`` keeps the patch seams and the surface the one module had.

``kiro_crew.slack.interactions`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over
the owners in its ``_OWNERS``, lowest layer first: ``interactions_context``,
``interactions_modals``, ``interactions_settings``, ``interactions_options``,
``interactions_sessions`` and ``interactions_approvals``. It keeps ``init``, ``dispatch``
and ``_handle_resume_choice`` (with the lock table and closed-tab notice only the choice
reads) itself, binds the owner names that code reads, and every name an owner defines still
resolves as ``kiro_crew.slack.interactions.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it; the owner list and its layering; that ``dispatch`` and the
resume choice stay here; that ``init`` writes the orchestrator into the facade and every
owner that reads ``_orch``; that the view callbacks register at
import, in the one module's order, as the functions the facade serves; that every owner
imports on its own; that every public name the module held before the split still
type-checks through it; that the owners log as ``kiro_crew.slack.interactions``; and the
import inventory that stands in for the F401 check ``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import facade_mirror_scan
import pytest
import source_corpus
import test_campaign_facades_base_public_names as base_names

from kiro_crew import _facade_mirror
from kiro_crew.slack import interactions
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_interactions_facade")

FACADE = "kiro_crew.slack.interactions"
OWNERS = (
    "kiro_crew.slack.interactions_context",
    "kiro_crew.slack.interactions_modals",
    "kiro_crew.slack.interactions_settings",
    "kiro_crew.slack.interactions_options",
    "kiro_crew.slack.interactions_sessions",
    "kiro_crew.slack.interactions_approvals",
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: the orchestrator slot every owner reads, a name the owners only import, a helper an
#: owner defines and the facade's ``dispatch`` reads, a module-level registry the facade
#: keeps, and a function the facade's own ``init`` reads. The scan below must find all of them, so a scanner that stops
#: matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "_orch",
        "sel",
        "is_allowed_user",
        "is_owner",
        "handle_message",
        "update_config_locked",
        "channel_inbound_permitted",
        "redact_credentials",
        "_import_thread_to_slot",
        "_handle_options_submit",
        "_forget_options_control",
        "_post_review_auth_error",
        "_resume_locks",
        "_get_forward_callback",
        "init",
    }
)

#: A test file worth parsing spells the facade itself, never only an ``interactions_*`` owner.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.slack\.interactions(?![\w])"
    r"|from\s+kiro_crew\.slack\s+import\s+"
    r"(?:\([^)]*?\binteractions\b(?!_)|[^\n]*?\binteractions\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    ACTION_ALLOWLIST_APPROVE ACTION_ALLOWLIST_DENY ACTION_TRACK_APPROVE
    ACTION_TRACK_DENY ACTIVATION_REVIEW APPROVAL_INTERACTIVE Any Awaitable Callable
    ConfigReadError CronStoreBusy CronStoreUnreadable LINK_DASHBOARD_ACTION
    LoopBoundLock OPTIONS_ACTION_PREFIX OPTIONS_CHECKBOXES_ACTION OPTIONS_SUBMIT_ACTION
    PostedOptions SESSION_LINK_ACTION STOP_DECLINED_COMPACTING_TEXT SlackApprovalDecider
    StartPriority TOOL_APPROVE_ACTION_PREFIX TOOL_DENY_ACTION_PREFIX
    TOOL_TRUST_ACTION_PREFIX TYPE_CHECKING add_trusted_session aiohttp asyncio
    build_options_selected_blocks channel_inbound_permitted claim_options_answer
    compaction_in_flight config_path consume_stop_declined count_redaction_tags
    decline_stop decode_options_token entries_queued_by escape_mrkdwn expire_options
    forget_slack_options_for_thread handle_interaction handle_message hashlib
    is_allowed_user is_owner json log_probe_failure logging mark_options_terminal
    options_control_is_stale options_edit_lock persist_allowed_user
    persist_tracking_channel re redact_and_truncate redact_credentials
    redact_exfiltration_urls redaction_notice release_options_answer
    replace_options_blocks run_config_write sel set_allowed_users set_tracking_channels
    settle_options_answer slack_options_owner_keys_snapshot slack_options_slot
    split_approval_token time unicodedata update_config_locked
    warn_unreadable_tracked_channels
    """.split())

#: The names the facade's own glue imports: the owner loop and the mirror.
_GLUE_IMPORTS = frozenset({"_facade_mirror", "_importlib", "_sys"})

#: The logger every owner names, so a record reads as the one module's did.
_LOGGER_NAME = FACADE

#: The view callbacks the one module registered at import, in its order, with the handler
#: each named.
_BASE_VIEW_REGISTRATIONS = (
    ("mc_config_panel", "_handle_config_submission"),
    ("mc_voice_config", "_handle_voice_config_submission"),
    ("mc_review_edit_submit", "_handle_review_edit_submit"),
    ("mc_review_revise_submit", "_handle_review_revise_submit"),
)

#: Generous for an import, and well under the suite's ``--timeout=120``, so a hung child
#: fails here, named, instead of being killed with its worker.
_CHILD_TIMEOUT_SECONDS = 60


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(interactions)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [interactions, *(sys.modules[name] for name in _index().owners)]


def _source(module: ModuleType) -> str:
    assert module.__file__ is not None
    return Path(module.__file__).read_text(encoding="utf-8")


def _tree(module: ModuleType) -> ast.Module:
    return ast.parse(_source(module))


def patched_names(source: str) -> set[str]:
    """Every name *source* patches through the facade, read by the guards' own reader."""
    return facade_mirror_scan.patched_names(source, {FACADE: FACADE}).get(FACADE, set())


def _run_child(code: str, workdir: Path) -> str:
    """Run *code* in a fresh interpreter: a module imported cold, not this process's."""
    test_dir = Path(__file__).resolve().parent
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(test_dir.parent / "src"), env.get("PYTHONPATH", "")])
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            check=False,
            env=env,
            cwd=str(workdir),
            timeout=_CHILD_TIMEOUT_SECONDS,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"the child did not finish within {_CHILD_TIMEOUT_SECONDS}s: {exc.stderr!r}")
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


# --------------------------------------------------------------------------- #
# The patch scan
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from kiro_crew.slack import interactions\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(interactions, 'A', 1)\n",
            {"A"},
        ),
        (
            "import kiro_crew.slack.interactions as ix\n"
            "from unittest import mock\n"
            "def t():\n    with mock.patch.object(ix, 'B'):\n        pass\n",
            {"B"},
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.slack.interactions.C'):\n        pass\n",
            {"C"},
        ),
        (
            "def t(monkeypatch):\n"
            "    monkeypatch.setattr('kiro_crew.slack.interactions.D', 1)\n",
            {"D"},
        ),
        # An owner is not the facade, even bound under the facade's name.
        (
            "from kiro_crew.slack import interactions_options as interactions\n"
            "def t(monkeypatch):\n    monkeypatch.setattr(interactions, 'X', 1)\n",
            set(),
        ),
        (
            "from unittest.mock import patch\n"
            "def t():\n    with patch('kiro_crew.slack.interactions_sessions.Y'):\n        pass\n",
            set(),
        ),
    ],
    ids=[
        "monkeypatch-from-import",
        "patch-object-import-as",
        "mock-patch-string",
        "dotted-setattr",
        "owner-named-like-the-facade",
        "owner-string",
    ],
)
def test_the_patch_scan_reads_every_spelling_and_nothing_else(
    source: str, expected: set[str]
) -> None:
    assert patched_names(source) == expected


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It reads the whole test tree, so a leaf-only diff must still run it."""
    root = source_corpus.repo_root()
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


def test_the_patch_scan_finds_every_known_seam() -> None:
    """Non-vacuity: a scan that found nothing would pass the reach tests over nothing."""
    assert sorted(_KNOWN_SEAMS - _corpus_patched_names()) == []


# --------------------------------------------------------------------------- #
# Reach: a patch through the facade lands on every module holding the name
# --------------------------------------------------------------------------- #


def _reach_gaps(names: set[str], index: _facade_mirror.MirrorIndex) -> list[str]:
    """``owner.name`` for each patched name an owner binds that the index does not route."""
    return [
        f"{module.__name__}.{name}"
        for name in sorted(names)
        for module in _family()[1:]
        if name in vars(module) and module.__name__ not in index.holders.get(name, ())
    ]


def test_every_scanned_name_is_routed_to_every_owner_holding_it() -> None:
    assert _reach_gaps(set(_corpus_patched_names()), _index()) == []


def test_the_reach_scan_can_fail() -> None:
    """The routing check reads the index: blind one name and the gap shows."""
    index = _index()
    name, holders = next((n, h) for n, h in sorted(index.holders.items()) if h)
    blind = dataclasses.replace(index, holders={**index.holders, name: ()})
    assert _reach_gaps({name}, blind) == [f"{owner}.{name}" for owner in holders]


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(interactions, name) or isinstance(getattr(interactions, name), ModuleType):
            continue  # a shared module (asyncio, time) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(interactions, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(interactions, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


# --------------------------------------------------------------------------- #
# The orchestrator slot: init writes it through the facade
# --------------------------------------------------------------------------- #


def _orch_bindings() -> dict[str, object]:
    """Every family member that binds ``_orch`` -> what it binds."""
    return {m.__name__: vars(m)["_orch"] for m in _family() if "_orch" in vars(m)}


def test_the_facade_and_every_owner_hold_the_orchestrator_slot() -> None:
    """Every owner reads ``_orch``, and so do the facade's ``dispatch`` and resume choice, so
    each of them binds it: one object, indexed, which a write through the facade sets in all
    of them."""
    assert sorted(_orch_bindings()) == sorted([FACADE, *OWNERS])
    assert _index().holders["_orch"] == OWNERS
    assert "_orch" not in _index().exports
    assert _index().divergent() == ()


def test_the_resume_choice_and_its_caller_stay_in_the_facade() -> None:
    """``_handle_resume_choice`` stats the session file inside its ``async def``, a call the
    sync-io gate's fs baseline records against this file, and ``dispatch`` is its caller:
    both are defined here, with the lock table and the closed-tab notice only the choice
    reads, and no owner holds any of them."""
    for name in ("dispatch", "_handle_resume_choice", "_resume_locks", "_RESUME_TAB_CLOSED_MSG"):
        assert name in vars(interactions), name
        assert name in _index().facade_only, name
        assert all(name not in vars(sys.modules[owner]) for owner in OWNERS), name
    assert interactions.dispatch.__module__ == FACADE
    assert interactions._handle_resume_choice.__module__ == FACADE


def test_init_writes_the_orchestrator_into_every_owner_that_reads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``init`` writes ``_orch`` through the facade, so the mirror sets it in the facade and
    every owner; a second ``init`` rebinds them all, as the one module's ``global`` did, and a
    patch of ``interactions._orch`` still reaches each of them."""
    monkeypatch.setattr(interactions, "_orch", None)  # the undo puts every holder back
    monkeypatch.setattr(interactions, "_get_forward_callback", lambda: "")
    first, second, patched = object(), object(), object()
    family = [FACADE, *OWNERS]
    interactions.init(first)  # type: ignore[arg-type]
    assert _orch_bindings() == dict.fromkeys(family, first)
    assert interactions._orch is first
    interactions.init(second)  # type: ignore[arg-type]
    assert _orch_bindings() == dict.fromkeys(family, second)
    assert interactions._orch is second
    monkeypatch.setattr(interactions, "_orch", patched)
    assert _orch_bindings() == dict.fromkeys(family, patched)
    assert _index().divergent() == ()


def test_init_registers_the_forward_shortcut_submission_it_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a forward callback configured, ``init`` registers the modal submission the
    facade serves under that callback id, as the one module did."""
    registry = interactions.VIEW_REGISTRY
    monkeypatch.setattr(interactions, "_orch", None)
    monkeypatch.setattr(interactions, "_get_forward_callback", lambda: "fwd_probe_cb")
    monkeypatch.delitem(registry, "fwd_probe_cb", raising=False)
    interactions.init(object())  # type: ignore[arg-type]
    try:
        assert registry["fwd_probe_cb"] is interactions._handle_shortcut_submission
    finally:
        registry.pop("fwd_probe_cb", None)


# --------------------------------------------------------------------------- #
# Registration at import, and each owner on its own
# --------------------------------------------------------------------------- #

_REGISTRY_PROBE = """
import json, sys
import kiro_crew.slack.interactions as facade
context = sys.modules["kiro_crew.slack.interactions_context"]
print(json.dumps({
    "order": list(facade.VIEW_REGISTRY),
    "handlers": {key: fn.__name__ for key, fn in facade.VIEW_REGISTRY.items()},
    "served": {key: fn is getattr(facade, fn.__name__) for key, fn in facade.VIEW_REGISTRY.items()},
    "one_registry": facade.VIEW_REGISTRY is context.VIEW_REGISTRY,
}))
"""


def test_the_view_callbacks_register_at_import_in_the_one_modules_order(tmp_path: Path) -> None:
    """A fresh import registers the one module's four view callbacks beside their handlers,
    in its order, each as the very function the facade serves, into the one registry. A
    callback added later may sit anywhere; these keep their order among themselves."""
    report = json.loads(_run_child(_REGISTRY_PROBE, tmp_path))
    base = [key for key, _ in _BASE_VIEW_REGISTRATIONS]
    assert [key for key in report["order"] if key in base] == base
    assert dict(_BASE_VIEW_REGISTRATIONS).items() <= report["handlers"].items()
    assert all(report["served"][key] for key in base)
    assert report["one_registry"] is True


_COLD_IMPORT_PROBE = """
import importlib, json, sys
importlib.import_module({owner!r})
preloaded = "kiro_crew.slack.interactions" in sys.modules
import kiro_crew.slack.interactions as facade
index = vars(facade)["__facade_mirror__"]
print(json.dumps({{
    "preloaded": preloaded, "divergent": list(index.divergent()), "owners": list(index.owners)
}}))
"""


@pytest.mark.parametrize("owner", OWNERS)
def test_each_owner_imports_on_its_own_before_the_facade(owner: str, tmp_path: Path) -> None:
    """A tree scan imports modules by name, in any order: an owner imported first must
    load whole, and the facade it pulls in must still index one object per name."""
    report = json.loads(_run_child(_COLD_IMPORT_PROBE.format(owner=owner), tmp_path))
    assert report == {"preloaded": False, "divergent": [], "owners": list(OWNERS)}


# --------------------------------------------------------------------------- #
# The owners and their layering
# --------------------------------------------------------------------------- #


def test_the_mirror_indexes_exactly_the_owners_with_no_exclusion_and_no_forward() -> None:
    index = _index()
    assert index.owners == OWNERS == interactions._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == interactions._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_interactions_sibling() -> None:
    assert interactions.__file__ is not None
    siblings = sorted(
        f"kiro_crew.slack.{path.stem}"
        for path in Path(interactions.__file__).parent.glob("interactions_*.py")
    )
    assert siblings == sorted(OWNERS)


def _project_imports(tree: ast.Module) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _layering_gaps(trees: dict[str, ast.Module]) -> dict[str, list[str]]:
    """Owner -> the family modules it imports that it may not: the facade, or an owner
    listed at or after it."""
    gaps: dict[str, list[str]] = {}
    for position, owner in enumerate(OWNERS):
        allowed = set(OWNERS[:position])
        reached = _project_imports(trees[owner]) & {FACADE, *OWNERS}
        bad = sorted(reached - allowed)
        if bad:
            gaps[owner] = bad
    return gaps


def test_each_owner_imports_only_owners_listed_before_it_and_never_the_facade() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    assert _layering_gaps(trees) == {}


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse(
        "from kiro_crew.slack.interactions_approvals import _handle_tool_approval\n"
    )
    trees[OWNERS[1]] = ast.parse("from kiro_crew.slack import interactions\n")
    assert _layering_gaps(trees) == {
        OWNERS[0]: [OWNERS[5]],
        OWNERS[1]: [FACADE],
    }


# --------------------------------------------------------------------------- #
# Static visibility
# --------------------------------------------------------------------------- #


def _statically_bound(tree: ast.Module) -> set[str]:
    """Names a type checker sees on the module: every module-level binding, the
    ``TYPE_CHECKING`` imports included."""
    names = set(_facade_mirror._defined_names(tree))
    for stmt in _facade_mirror._module_level_statements(tree):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {alias.asname or alias.name.split(".")[0] for alias in stmt.names}
    return names


def _home_imports() -> dict[str, tuple[str, str]]:
    """Names an owner imports from a project module outside the family."""
    family = set(OWNERS)
    found: dict[str, tuple[str, str]] = {}
    for owner in OWNERS:
        for node in _tree(sys.modules[owner]).body:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                if not node.module.startswith("kiro_crew") or node.module in family:
                    continue
                for alias in node.names:
                    if f"{node.module}.{alias.name}" not in family:
                        found.setdefault(alias.asname or alias.name, (node.module, alias.name))
    return found


def _home_manifest() -> dict[str, tuple[str, str]]:
    """The manifest's imports from outside the family: the ``TYPE_CHECKING`` block after
    the mirror install, not the ones the header kept from the one module."""
    blocks = [
        node
        for node in _tree(interactions).body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
    ]
    out: dict[str, tuple[str, str]] = {}
    for stmt in blocks[-1].body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module not in set(OWNERS):
            for alias in stmt.names:
                out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_name_served_from_outside_the_family_is_visible_to_type_checkers() -> None:
    """A name an owner imports from a project module and the facade serves without
    binding is named from its home under ``TYPE_CHECKING`` (none today: the facade
    keeps binding every project import it had)."""
    assert _home_imports(), "the owners import nothing from the project -- the scan went blind"
    served = set(_index().exports)
    assert _home_manifest() == {n: h for n, h in _home_imports().items() if n in served}


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(interactions))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """A caller may import any name the facade serves (``VIEW_REGISTRY``, ``ack_button``,
    the handlers it does not bind itself) from here, so mypy must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(interactions))) == []


# --------------------------------------------------------------------------- #
# The logger and the import inventory
# --------------------------------------------------------------------------- #


def _logger_names(tree: ast.Module) -> list[str]:
    """The argument of every ``logging.getLogger(...)`` call, unparsed."""
    return [
        ast.unparse(node.args[0]) if node.args else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "logging.getLogger"
    ]


def test_the_owners_log_under_the_facade_name() -> None:
    """The moved handlers log as the one module did: an owner's ``__name__`` would be a
    logger no filter, level or capture names (``caplog.at_level(logger=...)``)."""
    names = [n for owner in OWNERS for n in _logger_names(_tree(sys.modules[owner]))]
    assert repr(_LOGGER_NAME) in names, "the scan found no owner logger -- it went blind"
    assert "__name__" not in names
    assert all(vars(sys.modules[owner])["logger"] is interactions.logger for owner in OWNERS)


def test_the_logger_scan_can_fail() -> None:
    tree = ast.parse("import logging\nlogging.getLogger(__name__).warning('x')\n")
    assert _logger_names(tree) == ["__name__"]


def _names_read_by_functions(tree: ast.Module) -> set[str]:
    """Every bare name a function in *tree* reads, nested functions included."""
    return {
        node.id
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for node in ast.walk(fn)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }


def unexpected_imports(
    source: str, owner_names: dict[str, set[str]], owners_read: set[str]
) -> list[str]:
    """Names *source* binds by a runtime import that nothing accounts for.

    An import is accounted for when the module had it before the split, when it is the
    glue's own, or, from an owner module, when that owner defines the name. Any other
    import is accounted for when the family's own code reads it: a function *source* keeps,
    or an owner's (*owners_read*). So an import a later change adds for code that uses it
    passes, and one nothing reads is reported. Imports under ``if TYPE_CHECKING:`` bind
    nothing at run time and are the mirror guard's business, so they are skipped.
    """
    tree = ast.parse(source)
    read = owners_read | _names_read_by_functions(tree)
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            continue
        if isinstance(node, ast.Import):
            bound = [(alias.asname or alias.name.split(".")[0], None) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            bound = [(alias.asname or alias.name, node.module) for alias in node.names]
        else:
            continue
        for name, module in bound:
            if module in owner_names:
                if name not in owner_names[module]:
                    found.append(f"{module}.{name}")
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS | read:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _facade_mirror._defined_names(_tree(sys.modules[owner])) for owner in OWNERS}


def _owners_read() -> set[str]:
    return set().union(*(_names_read_by_functions(_tree(sys.modules[owner])) for owner in OWNERS))


def test_the_facade_imports_nothing_its_family_does_not_account_for() -> None:
    assert unexpected_imports(_source(interactions), _owner_names(), _owners_read()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: an import nothing reads, and an owner
    import of a name that owner does not define, are both reported; an import a kept
    function reads is not."""
    planted = (
        _source(interactions)
        + "\nimport pickle\nimport shelve\n"
        + "from kiro_crew.slack.interactions_context import dispatch\n"
        + "\n\ndef _planted() -> object:\n    return shelve\n"
    )
    assert unexpected_imports(planted, _owner_names(), _owners_read()) == [
        "pickle",
        "kiro_crew.slack.interactions_context.dispatch",
    ]
