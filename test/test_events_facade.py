"""``kiro_crew.slack.events`` keeps the patch seams and the surface the one module had.

``kiro_crew.slack.events`` is a mirror facade (:mod:`kiro_crew._facade_mirror`) over the owners
in its ``_OWNERS``, lowest layer first: ``events_block_text``, ``events_queue``,
``events_slash``, ``events_home_tab``, ``events_voice`` and ``events_inbound``. It keeps
``init_socket_mode`` and ``_route_message`` itself, and every name an owner defines still
resolves as ``kiro_crew.slack.events.<name>``.

``test/test_facade_mirror_guards.py`` holds every mirrored facade to the shared rules. These
tests pin what is particular to this one: every name the test trees patch through it reaches
every module holding it; the owner list and its layering; that each global rebound at run
time lives in the one module that rebinds it, with every reader beside it; that the slash
commands register at import, in the one module's order, as the functions the facade
serves; that every owner imports on its own; that every public name the module held before
the split still type-checks through it; that the owners log as ``kiro_crew.slack.events``;
and the import inventory that stands in for the F401 check ``setup.cfg`` switches off
for it.
"""

from __future__ import annotations

import ast
import functools
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
from kiro_crew.slack import events
from kiro_crew.subprocess_utf8 import UTF8_TEXT

pytestmark = pytest.mark.xdist_group(name="tree_scan_events_facade")

#: Checks ``test_facade_shared_checks.py`` runs against this module's helpers; each is
#: the same for every facade, so it is written once there and parametrized per module.
SHARED_FACADE_CHECKS = (
    "known_seams",
    "spelling_cases",
    "corpus_gate",
    "reach_routing",
    "reach_scan_can_fail",
    "owner_layering",
    "home_manifest",
    "logger_scan_can_fail",
)

FACADE = "kiro_crew.slack.events"
OWNERS = (
    "kiro_crew.slack.events_block_text",
    "kiro_crew.slack.events_queue",
    "kiro_crew.slack.events_slash",
    "kiro_crew.slack.events_home_tab",
    "kiro_crew.slack.events_voice",
    "kiro_crew.slack.events_inbound",
)

#: Names the test trees are known to patch through the facade, one or more per kind of
#: seam: names the owners only import, functions an owner defines and another owner reads, a
#: class the gateway boot builds, a global one owner rebinds, a module-level ledger, and the
#: name the interactions facade is imported as. The scan below must find all of them, so a
#: scanner that stops matching goes red instead of passing over nothing.
_KNOWN_SEAMS = frozenset(
    {
        "is_allowed_user",
        "is_owner",
        "sel",
        "handle_message",
        "handle_message_transport",
        "is_yolo_mode",
        "format_schedule",
        "transcribe_audio",
        "_route_message",
        "_dispatch_queued",
        "_transcribe_files",
        "_get_skills_loader",
        "_handle_slash",
        "SeenCache",
        "init_socket_mode",
        "_home_tab_collect_sem",
        "_trusted_bot_turns",
        "dispatch_interactive",
    }
)

#: A test file worth parsing spells the facade itself, never only an ``events_*`` owner.
_SPELLS_THE_FACADE = re.compile(
    r"kiro_crew\.slack\.events(?![\w])"
    r"|from\s+kiro_crew\.slack\s+import\s+"
    r"(?:\([^)]*?\bevents\b(?!_)|[^\n]*?\bevents\b(?!_))"
)

#: Every name the facade's import statements bound before its owners moved out. They stay
#: bound here because callers and tests read and patch them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    ACTIVATION_MENTION ACTIVATION_OBSERVE ACTIVATION_OFF ACTIVATION_REVIEW APPROVAL_AUTO
    APPROVAL_INTERACTIVE Any AsyncIterator AsyncWebClient Callable Coroutine
    InterceptDecision Iterable Iterator KiroCrewConfig LINK_WINDOW_SECS Literal
    MAX_SESSION_TTL_SECS OrderedDict STOP_DECLINED_COMPACTING_TEXT SkillsLoader
    SocketModeRequest SocketModeResponse Stats TYPE_CHECKING
    VOICE_MEMO_DURATION_UNVERIFIED VOICE_MEMO_FAILED VOICE_MEMO_TOO_LONG
    VOICE_MEMO_UNAVAILABLE WSSocketModeClient _HOME_TAB_SESSIONS_PER_KIND
    _SESSION_KIND_DASHBOARD _SESSION_KIND_TASKRUNNER __version__ _build_sessions_blocks
    _collect_recent_sessions_off_loop _compaction_in_flight _message_surface_limit
    admit_inbound_callback agent_spec_stems aiohttp asyncio audio_exceeds_secs
    batch_duration_cap_secs build_stopping_blocks canonical_key
    channel_inbound_permitted channels_modal command_hint_block consume_stop_declined
    contextlib current_context dashboard_link_block decline_stop describe_grant_lifetime
    dispatch_interactive entries_queued_by flat_dm_session_key format_schedule
    get_update_info hand_queue_to_successor handle_message handle_message_transport
    inspect is_allowed_user is_markdown_spec is_owner is_voice_memo is_yolo_mode
    iter_agent_spec_files json kiro_agents_dir legacy_key list_servers load_stt_config
    logging note_user_stop os overload owner_token parse_agent_spec_text parse_duration
    person_priority process_slack_files prompt_track_channel re redact_credentials
    redact_exfiltration_urls register_drain run_config_write safe_context_call
    safe_read_file safety_override sel send_dashboard_link sessions_include_ended
    set_allowed_users set_dashboard_state set_open_channels set_orch_cfg set_owner_id
    set_tracking_channels set_yolo_mode should_record_observe_history slack_cfg
    slack_manifest stt_available subprocess_executor tag_entry tempfile transcribe_audio
    trusted_bot_admission unlink_queued_temp_paths validated_self_user_id
    voice_config_modal voice_memo_notes weakref yolo_policy_permits
    """.split())

#: The names the facade's own glue imports: the owner loop and the mirror.
_GLUE_IMPORTS = frozenset({"_facade_mirror", "_importlib", "_sys"})

#: The logger every owner names, so a record reads as the one module's did.
_LOGGER_NAME = FACADE

#: The ``/kirocrew`` sub-commands the one module registered at import, in its order, with
#: the handler each named.
_BASE_SLASH_REGISTRATIONS = (
    ("dashboard", "_handle_dashboard"),
    ("agent", "_handle_agent"),
    ("voice", "_handle_voice"),
    ("yolo", "_handle_yolo"),
    ("config", "_handle_config"),
    ("users", "_handle_allowlist_cmd"),
    ("channels", "_handle_channel_cmd"),
    ("sessions", "_handle_sessions"),
    ("status", "_handle_status"),
    ("restart", "_handle_restart"),
)

#: Globals the family rebinds at run time with ``global name`` today, each with the one owner
#: that rebinds it: the scan below must find at least these.
_REBOUND_HOMES = {
    "_skills_loader": "kiro_crew.slack.events_home_tab",
    "_home_tab_collect_sem": "kiro_crew.slack.events_home_tab",
}

#: Generous for an import, and well under the suite's ``--timeout=120``, so a hung child
#: fails here, named, instead of being killed with its worker.
_CHILD_TIMEOUT_SECONDS = 60


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(events)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _family() -> list[ModuleType]:
    return [events, *(sys.modules[name] for name in _index().owners)]


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


#: Each spelling the patch scan must read, and each it must skip, keyed by case id.
_SPELLING_CASES = {
    "monkeypatch-from-import": (
        "from kiro_crew.slack import events as ev\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(ev, 'A', 1)\n",
        {"A"},
    ),
    "patch-object-import-as": (
        "import kiro_crew.slack.events as events\n"
        "from unittest import mock\n"
        "def t():\n    with mock.patch.object(events, 'B'):\n        pass\n",
        {"B"},
    ),
    "mock-patch-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.slack.events.C'):\n        pass\n",
        {"C"},
    ),
    "dotted-setattr": (
        "def t(monkeypatch):\n    monkeypatch.setattr('kiro_crew.slack.events.D', 1)\n",
        {"D"},
    ),
    # An owner is not the facade, even bound under the facade's name.
    "owner-named-like-the-facade": (
        "from kiro_crew.slack import events_inbound as events\n"
        "def t(monkeypatch):\n    monkeypatch.setattr(events, 'X', 1)\n",
        set(),
    ),
    "owner-string": (
        "from unittest.mock import patch\n"
        "def t():\n    with patch('kiro_crew.slack.events_queue.Y'):\n        pass\n",
        set(),
    ),
}


def _test_tree_files() -> list[Path]:
    root = source_corpus.repo_root()
    test_root = root / "test"
    out = []
    for path in source_corpus.repo_files_named(".py"):
        rel = path.relative_to(root).as_posix()
        if rel == "conftest.py" or path.is_relative_to(test_root) or "/tests/" in rel:
            out.append(path)
    return out


@functools.lru_cache(maxsize=1)
def _corpus_patched_names() -> frozenset[str]:
    """Every name the test trees patch through the facade, scanned once per worker."""
    found: set[str] = set()
    for path in _test_tree_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if _SPELLS_THE_FACADE.search(text):
            found |= patched_names(text)
    return frozenset(found)


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


def test_a_facade_patch_of_every_scanned_name_reaches_every_module_holding_it() -> None:
    family = _family()
    missed: dict[str, list[str]] = {}
    for name in sorted(_corpus_patched_names()):
        if not hasattr(events, name) or isinstance(getattr(events, name), ModuleType):
            continue  # a shared module (aiohttp, asyncio, os) is one object in every holder
        sentinel = object()
        holders = [module for module in family if name in vars(module)]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(events, name, sentinel)
            stale = [m.__name__ for m in holders if vars(m).get(name) is not sentinel]
            if getattr(events, name) is not sentinel:
                stale.append(FACADE)
        if stale:
            missed[name] = stale
    assert missed == {}
    assert _index().divergent() == ()


# --------------------------------------------------------------------------- #
# Globals rebound at run time stay with their writer
# --------------------------------------------------------------------------- #


def _rebound_globals(tree: ast.Module) -> set[str]:
    return {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}


def _global_reads(module: ModuleType) -> set[str]:
    return facade_mirror_scan.global_reads(_source(module), module.__name__)


def test_each_rebound_global_lives_with_its_writer_and_every_reader() -> None:
    """A global rebound with ``global name`` exists in one namespace, the writer's: a
    reader in another module would keep the value bound at import. So the writer holds
    it alone, no other member binds it, and every member that reads it is the writer."""
    family = _family()
    homes = {n: m.__name__ for m in family for n in _rebound_globals(_tree(m))}
    assert _REBOUND_HOMES.items() <= homes.items(), "the scan lost a known rebound global"
    for name, home in homes.items():
        holders = [m.__name__ for m in family if name in vars(m)]
        readers = [m.__name__ for m in family if name in _global_reads(m)]
        assert holders == [home], name
        assert readers == [home], name


@pytest.mark.parametrize("name", sorted(_REBOUND_HOMES))
def test_a_facade_write_of_a_rebound_global_lands_with_its_writer(name: str) -> None:
    """``test_slack_home_tab`` resets the collector gate through the facade; the reset
    must reach the owner whose ``global`` statement reads and rebinds it."""
    home = sys.modules[_REBOUND_HOMES[name]]
    sentinel = object()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(events, name, sentinel)
        assert vars(home)[name] is sentinel
        assert getattr(events, name) is sentinel
        assert name not in vars(events)
    assert vars(home)[name] is not sentinel
    assert name not in vars(events)
    assert _index().divergent() == ()


# --------------------------------------------------------------------------- #
# Registration at import, and each owner on its own
# --------------------------------------------------------------------------- #

_REGISTRY_PROBE = """
import json, sys
import kiro_crew.slack.events as facade
slash = sys.modules["kiro_crew.slack.events_slash"]
print(json.dumps({
    "order": list(facade.SLASH_REGISTRY),
    "handlers": {name: fn.__name__ for name, (fn, _) in facade.SLASH_REGISTRY.items()},
    "served": {
        name: fn is getattr(facade, fn.__name__) for name, (fn, _) in facade.SLASH_REGISTRY.items()
    },
    "one_registry": facade.SLASH_REGISTRY is slash.SLASH_REGISTRY,
}))
"""


def test_the_slash_commands_register_at_import_in_the_one_modules_order(tmp_path: Path) -> None:
    """A fresh import registers the one module's ten sub-commands beside their handlers, in
    its order, each as the very function the facade serves, into the one registry. A
    sub-command added later may sit anywhere; these keep their order among themselves."""
    report = json.loads(_run_child(_REGISTRY_PROBE, tmp_path))
    base = [name for name, _ in _BASE_SLASH_REGISTRATIONS]
    assert [name for name in report["order"] if name in base] == base
    assert dict(_BASE_SLASH_REGISTRATIONS).items() <= report["handlers"].items()
    assert all(report["served"][key] for key in base)
    assert report["one_registry"] is True


_COLD_IMPORT_PROBE = """
import importlib, json, sys
importlib.import_module({owner!r})
preloaded = "kiro_crew.slack.events" in sys.modules
import kiro_crew.slack.events as facade
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
    assert index.owners == OWNERS == events._OWNERS
    assert index.excluded == ()
    assert index.forward_reads == "sys_modules_first"
    assert dict(index.forwards) == {}
    assert dict(index.served) == dict(index.exports) == events._EXPORTS
    assert index.divergent() == ()


def test_the_owners_are_every_events_sibling() -> None:
    assert events.__file__ is not None
    siblings = sorted(
        f"kiro_crew.slack.{path.stem}" for path in Path(events.__file__).parent.glob("events_*.py")
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


def test_the_layering_check_can_fail() -> None:
    trees = {name: _tree(sys.modules[name]) for name in OWNERS}
    trees[OWNERS[0]] = ast.parse("from kiro_crew.slack.events_inbound import SeenCache\n")
    trees[OWNERS[1]] = ast.parse("from kiro_crew.slack import events\n")
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
        for node in _tree(events).body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"
    ]
    out: dict[str, tuple[str, str]] = {}
    for stmt in blocks[-1].body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module not in set(OWNERS):
            for alias in stmt.names:
                out[alias.asname or alias.name] = (stmt.module or "", alias.name)
    return out


def test_every_base_public_name_type_checks_through_the_facade() -> None:
    missing = base_names._BASE_PUBLIC_NAMES[FACADE] - _statically_bound(_tree(events))
    assert sorted(missing) == []


def test_every_served_name_type_checks_through_the_facade() -> None:
    """``gateway_runtime.service_boot``, ``handler_runtime.commands`` and the interactions
    owners import moved names from here, so mypy must see each one."""
    assert sorted(set(_index().exports) - _statically_bound(_tree(events))) == []


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
    """The moved routing code logs as the one module did: an owner's ``__name__`` would be a
    logger no filter, level or capture names (``caplog.at_level(logger=...)``)."""
    names = [n for owner in OWNERS for n in _logger_names(_tree(sys.modules[owner]))]
    assert repr(_LOGGER_NAME) in names, "the scan found no owner logger -- it went blind"
    assert "__name__" not in names
    assert all(vars(sys.modules[owner])["logger"] is events.logger for owner in OWNERS)


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
    assert unexpected_imports(_source(events), _owner_names(), _owners_read()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: an import nothing reads, and an owner
    import of a name that owner does not define, are both reported; an import a kept
    function reads is not."""
    planted = (
        _source(events)
        + "\nimport pickle\nimport shelve\n"
        + "from kiro_crew.slack.events_queue import _safe_log\n"
        + "\n\ndef _planted() -> object:\n    return shelve\n"
    )
    assert unexpected_imports(planted, _owner_names(), _owners_read()) == [
        "pickle",
        "kiro_crew.slack.events_queue._safe_log",
    ]
