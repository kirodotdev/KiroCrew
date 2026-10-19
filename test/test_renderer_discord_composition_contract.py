"""``DiscordRenderer`` is one class composed from owner modules; these tests pin how.

``kiro_crew.discord.renderer`` keeps the renderer's state and the constructs repository
guards read in that file. An owner function whose first parameter is ``self`` is bound as
the class attribute of the same name, and every other moved name is served from its owner
through ``kiro_crew._facade_mirror``. Pinned here: the owner set and its layer order, that
each moved member is its owner's own function with its base kind and signature, the rules
a function bound from outside the class body needs, the logger name, and that
``DiscordApprovalDecider`` moved whole. The layout is described in
``docs/system-specs/modules/messaging.md`` (Discord's renderer and client composition).
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import source_corpus

import kiro_crew
from kiro_crew.discord import renderer as FACADE_MODULE
from kiro_crew.subprocess_utf8 import UTF8_TEXT

FACADE = "kiro_crew.discord.renderer"
STEM = "renderer"
FACADE_PATH = Path(FACADE_MODULE.__file__).resolve()
DISCORD_DIR = FACADE_PATH.parent
SRC = Path(kiro_crew.__file__).resolve().parent
CLS = FACADE_MODULE.DiscordRenderer
OWNER_PREFIX = "kiro_crew.discord.renderer_"
#: An owner the scan must find importing a lower owner, so it cannot pass vacuously.
FAMILY_IMPORTER = "discord/renderer_live.py"

# Walks src/kiro_crew for owner imports; keep it on one worker.
pytestmark = pytest.mark.xdist_group(name="tree_scan_renderer_discord_composition_contract")

#: Owner modules, lowest layer first: an owner imports only owners listed before it.
OWNERS = (
    "renderer_text",
    "renderer_components",
    "renderer_approval",
    "renderer_status",
    "renderer_live",
)

#: ``DiscordRenderer`` members an owner defines, by owner, each bound as the class attribute.
MOVED_MEMBERS = {
    "renderer_status": (
        "on_turn_start",
        "_ensure_ladder",
        "_set_phase",
        "_note_progress",
        "_typing_loop",
        "_stop_typing",
        "on_thinking",
        "on_tool_call",
        "_context_pct",
        "_with_turn_footer",
    ),
    "renderer_live": (
        "on_text_chunk",
        "_render_tables_for_delivery",
        "_convert_tables",
        "_take_canonical_options",
        "_materialize_chip",
        "_rotate_at_markers",
        "_open_new_message",
        "_segment_text",
        "note_steer",
        "_chip_for_seal",
    ),
}

#: Every member the class had at the base, with its kind and its signature as
#: ``inspect`` renders it. A moved function annotates ``self`` for the type checker;
#: that one annotation is the only difference the comparison removes.
BASE_MEMBERS = {
    "channel_type": ("attribute", "'discord'"),
    "__init__": (
        "function",
        "(self, client: \"'DiscordClient'\", channel_id: 'str', capabilities: 'TransportCapabilities', *, session_key: 'str' = '', uploads_allowed: 'bool' = False, upload_root: 'str' = '', react_message_id: 'str' = '', reactions_enabled: 'bool' = True, show_thinking: 'bool' = False, now: 'Callable[[], float] | None' = None) -> 'None'",
    ),
    "on_turn_start": ("coroutine", "(self) -> 'None'"),
    "_ensure_ladder": ("function", "(self) -> 'PhaseReactionLadder | None'"),
    "_set_phase": ("function", "(self, phase: 'str') -> 'None'"),
    "_note_progress": ("function", "(self) -> 'None'"),
    "_typing_loop": ("coroutine", "(self) -> 'None'"),
    "_stop_typing": ("function", "(self) -> 'None'"),
    "on_text_chunk": ("coroutine", "(self, text: 'str') -> 'None'"),
    "_render_tables_for_delivery": ("function", "(self, raw: 'str', *, final: 'bool') -> 'str'"),
    "_convert_tables": ("coroutine", "(self, *, final: 'bool') -> 'None'"),
    "_take_canonical_options": ("function", "(self) -> 'list[str]'"),
    "_materialize_chip": ("function", "(self) -> 'None'"),
    "on_steer_consumed": ("coroutine", "(self, summary: 'str' = '') -> 'None'"),
    "_rotate_at_markers": ("coroutine", "(self) -> 'None'"),
    "_rotate_on_length": ("coroutine", "(self) -> 'None'"),
    "_seal_rotated_part": ("coroutine", "(self) -> 'None'"),
    "_open_new_message": ("function", "(self) -> 'None'"),
    "_segment_text": ("function", "(self) -> 'str'"),
    "_stream_live": ("coroutine", "(self, *, force: 'bool' = False) -> 'None'"),
    "authorize_upload_root": (
        "function",
        "(self, root: 'str', scratch_root: 'str' = '') -> 'None'",
    ),
    "_uploads_enabled": ("function", "(self) -> 'bool'"),
    "_extract_uploads": ("coroutine", "(self, text: 'str') -> 'tuple[str, list[OutboundFile]]'"),
    "_append_rejections": (
        "function",
        "(self, body: 'str', rejections: 'list[Rejection]') -> 'str'",
    ),
    "_tally_redactions": ("function", "(self, text: 'str') -> 'None'"),
    "_maybe_send_redaction_notice": ("coroutine", "(self) -> 'None'"),
    "_seam_predecessor": ("function", "(self) -> 'str'"),
    "_seam_against": ("staticmethod", "(predecessor: 'str', text: 'str') -> 'str'"),
    "_seam_safe": ("function", "(self, text: 'str') -> 'str'"),
    "_record_sent": ("function", "(self, text: 'str') -> 'None'"),
    "_land_sealed": (
        "coroutine",
        "(self, text: 'str', files: 'list[OutboundFile]', components: 'list[dict] | None') -> 'bool'",
    ),
    "delivery_failed": ("property", "(self) -> 'bool'"),
    "_seal_current": (
        "coroutine",
        "(self, *, components: 'list[dict] | None' = None, extract_uploads: 'bool' = True) -> 'bool'",
    ),
    "on_thinking": ("coroutine", "(self, text: 'str') -> 'None'"),
    "_flush_thinking": ("coroutine", "(self, *, from_on_done: 'bool' = False) -> 'None'"),
    "on_tool_call": (
        "coroutine",
        "(self, tool_call_id: 'str', title: 'str', tool_kind: 'str' = '', tool_purpose: 'str' = '') -> 'None'",
    ),
    "on_prompt_choice": (
        "coroutine",
        "(self, options: 'list[dict[str, Any]]', request_id: 'str | int', tool_title: 'str' = '', tool_purpose: 'str' = '', tool_input: 'str' = '') -> 'None'",
    ),
    "on_compaction": ("coroutine", "(self, context_usage_pct: 'float') -> 'None'"),
    "note_steer": ("function", "(self, text: 'str') -> 'None'"),
    "on_done": ("coroutine", "(self, stop_reason: 'str' = '') -> 'None'"),
    "_context_pct": ("function", "(self) -> 'float | None'"),
    "_with_turn_footer": ("function", "(self, text: 'str') -> 'str'"),
    "bind_context_source": ("function", "(self, provider: \"'LLMProvider | None'\") -> 'None'"),
    "_limit": ("function", "(self) -> 'int'"),
    "_chip_for_seal": ("function", "(self, i: 'int') -> 'str | None'"),
    "close": ("coroutine", "(self) -> 'None'"),
}

#: ``DiscordApprovalDecider``'s members at the base, kind and signature.
DECIDER_BASE_MEMBERS = {
    "_REGISTRY": ("attribute", "dict"),
    "_NONCES": ("attribute", "dict"),
    "_DETACHED": ("attribute", "set"),
    "__init__": ("function", "(self, *, session_key: 'str') -> 'None'"),
    "key": ("staticmethod", "(session_key: 'str', request_id: 'str | int') -> 'str'"),
    "register_nonce": ("classmethod", "(cls, key: 'str', *, detached: 'bool' = False) -> 'str'"),
    "retire": ("classmethod", "(cls, key: 'str') -> 'None'"),
    "refuse_undelivered": ("classmethod", "(cls, key: 'str') -> 'None'"),
    "discard_session": ("classmethod", "(cls, session_key: 'str') -> 'None'"),
    "__call__": ("coroutine", "(self, event: 'Any') -> 'bool'"),
    "resolve_global": (
        "classmethod",
        "(cls, key: 'str', approved: 'bool', *, nonce: 'str' = '') -> 'bool'",
    ),
}

#: Classes that moved whole, by the owner that defines them.
MOVED_CLASSES = {
    "DiscordApprovalDecider": "renderer_approval",
    "_MessageReactionSink": "renderer_status",
}

#: Constructs guards read in ``renderer.py`` itself. ``__init__`` calls zero-argument
#: ``super()``, and it and ``bind_context_source`` annotate with ``LLMProvider``, whose
#: import the agent-sdk boundary baseline counts in this file only. The credential-
#: boundary suite's rotation-and-tally part reads the seam grade, both sinks and the
#: reasoning note here by name (its universal pins read the facade with its owners).
#: The "Discord transformed image text" posture sink names this file: the display-form
#: redaction pass and the re-scan of the text an upload extraction rewrote.
FACADE_PINNED = frozenset(
    {
        "DiscordRenderer",
        "__init__",
        "bind_context_source",
        "_seam_safe",
        "_stream_live",
        "_seal_current",
        "_flush_thinking",
        "_redact_all",
        "_redact_transformed",
        "_extract_uploads",
    }
)


def _owner(stem: str) -> ModuleType:
    return importlib.import_module(f"kiro_crew.discord.{stem}")


def _source(stem: str) -> str:
    return (DISCORD_DIR / f"{stem}.py").read_text(encoding="utf-8")


def _runtime_imports(source: str) -> set[str]:
    """Modules *source* imports when it loads; ``if TYPE_CHECKING:`` blocks excluded."""
    loaded: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            loaded |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            loaded.add(node.module)
            loaded |= {f"{node.module}.{alias.name}" for alias in node.names}
    return loaded


def _describe(member: Any, cls: type) -> tuple[str, str]:
    """A class member's kind and ``inspect`` signature, less the owner's ``self`` annotation."""
    if isinstance(member, property):
        kind, fn = "property", member.fget
    elif isinstance(member, staticmethod):
        kind, fn = "staticmethod", member.__func__
    elif isinstance(member, classmethod):
        kind, fn = "classmethod", member.__func__
    elif inspect.isfunction(member):
        kind = "coroutine" if inspect.iscoroutinefunction(member) else "function"
        fn = member
    elif isinstance(member, (dict, list, set)):
        # Mutable class state (the decider's registries): its contents are runtime state.
        return "attribute", type(member).__name__
    else:
        return "attribute", repr(member)
    signature = str(inspect.signature(fn)).replace(f"self: '{cls.__name__}'", "self", 1)
    return kind, signature


def _members(cls: type) -> set[str]:
    kept = ("__init__", "__call__")
    return {n for n in vars(cls) if (not n.startswith("__") or n in kept) and n != "_abc_impl"}


def _class_body_reads(source: str) -> list[str]:
    """What only a class body provides: zero-argument ``super()``, ``__class__``, mangling."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
        ):
            hits.append(f"{node.lineno}: super()")
        elif isinstance(node, ast.Name) and node.id == "__class__":
            hits.append(f"{node.lineno}: __class__")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and not node.attr.endswith("__")
        ):
            hits.append(f"{node.lineno}: {node.attr}")
    return sorted(hits)


# ── the owners ───────────────────────────────────────────────────────────────────


def test_the_owners_are_every_sibling_module_in_layer_order() -> None:
    on_disk = {path.stem for path in DISCORD_DIR.glob(f"{STEM}_*.py")}
    assert on_disk == set(OWNERS)
    assert FACADE_MODULE._OWNERS == tuple(f"kiro_crew.discord.{stem}" for stem in OWNERS)


def test_an_owner_imports_only_owners_below_it_and_never_its_facade() -> None:
    for index, stem in enumerate(OWNERS):
        loaded = _runtime_imports(_source(stem))
        assert FACADE not in loaded, stem
        siblings = {
            name.rsplit(".", 1)[-1]
            for name in loaded
            if name.startswith(f"kiro_crew.discord.{STEM}_") and name.count(".") == 2
        }
        assert siblings <= set(OWNERS[:index]), stem


def test_every_owner_logs_under_the_facades_name() -> None:
    assert FACADE_MODULE.logger is logging.getLogger(FACADE)
    for stem in OWNERS:
        logger = vars(_owner(stem)).get("logger")
        if logger is not None:
            assert logger is logging.getLogger(FACADE), stem


def test_the_facade_installs_its_mirror_when_run_as_a_script(tmp_path: Path) -> None:
    """Under ``python -m`` the facade is ``__main__``: one logger by literal name keeps
    the family to one object per name, which the mirror's install requires."""
    result = subprocess.run(
        [sys.executable, "-m", FACADE],
        capture_output=True,
        cwd=tmp_path,
        env=dict(os.environ),
        timeout=120,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr


# ── the class surface ────────────────────────────────────────────────────────────


def test_the_class_keeps_every_base_member() -> None:
    assert sorted(set(BASE_MEMBERS) - _members(CLS)) == []


def test_every_base_member_keeps_its_kind_and_signature() -> None:
    members = vars(CLS)
    for name, described in BASE_MEMBERS.items():
        assert _describe(members[name], CLS) == described, name


def test_a_moved_member_is_its_owners_function() -> None:
    members = vars(CLS)
    for stem, names in MOVED_MEMBERS.items():
        owner = _owner(stem)
        for name in names:
            assert members[name] is getattr(owner, name), f"{stem}.{name}"
            assert members[name].__module__ == owner.__name__, f"{stem}.{name}"
            assert members[name].__qualname__ == name, f"{stem}.{name}"


def test_every_owner_function_of_self_is_bound_on_the_class() -> None:
    bound = {name for names in MOVED_MEMBERS.values() for name in names}
    for stem in OWNERS:
        owner = _owner(stem)
        for name, value in vars(owner).items():
            if not inspect.isfunction(value) or value.__module__ != owner.__name__:
                continue
            if list(inspect.signature(value).parameters)[:1] == ["self"]:
                assert name in bound and vars(CLS)[name] is value, f"{stem}.{name}"


def test_no_bound_function_needs_the_class_body() -> None:
    """A function bound into the class from an owner was compiled outside it: it has no
    ``__class__`` cell, so a zero-argument ``super()`` or a ``__class__`` read raises, and
    a ``self.__name`` is never mangled. So no owner spells one; a method that needs one
    stays in the class body."""
    assert {stem: hits for stem in OWNERS if (hits := _class_body_reads(_source(stem)))} == {}


def test_the_class_body_scan_can_fail() -> None:
    planted = "def f(self):\n    super().g()\n    return __class__, self.__secret, self.__dict__\n"
    assert _class_body_reads(planted) == ["2: super()", "3: __class__", "3: __secret"]


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def test_each_guarded_construct_stays_in_the_facade() -> None:
    """The constructs ``FACADE_PINNED`` names are defined in the facade's own file."""
    tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"))
    cls_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == CLS.__name__
    )
    members = {
        node.name
        for node in cls_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert sorted(FACADE_PINNED - (_top_level_names(tree) | members)) == []
    for stem in OWNERS:
        owned = _top_level_names(ast.parse(_source(stem)))
        assert sorted(FACADE_PINNED & owned) == [], stem


def _imported_modules(node: ast.Import | ast.ImportFrom, module: str) -> list[str]:
    """Dotted names an import statement in *module* reaches, relative spellings resolved."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.split(".")[: -node.level]
        base = ".".join([*package, *([node.module] if node.module else [])])
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _owner_importers(sources: Iterable[tuple[Path, str]], root: Path) -> set[str]:
    """The files among *sources* (relative to *root*) whose code imports a family owner."""
    found = set()
    for path, text in sources:
        rel = path.relative_to(root)
        module = ".".join([root.name, *rel.with_suffix("").parts])
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                name.startswith(OWNER_PREFIX) for name in _imported_modules(node, module)
            ):
                found.add(rel.as_posix())
    return found


def test_only_the_family_imports_an_owner() -> None:
    """Callers reach the owners' names through the facade, never by importing an owner."""
    family = {f"discord/{stem}.py" for stem in (STEM, *OWNERS)}
    sources = source_corpus.iter_candidate_sources(require_any=(f"{STEM}_",))
    importers = _owner_importers(
        ((path, text) for path, text in sources if path.is_relative_to(SRC)), SRC
    )
    assert FAMILY_IMPORTER in importers  # owners import the owners below them
    assert importers - family == set()


def test_the_owner_import_scan_finds_an_outside_importer(tmp_path: Path) -> None:
    root = tmp_path / "kiro_crew"
    owner = OWNERS[0]
    sources = {
        root / "pkg" / "caller.py": f"from kiro_crew.discord.{owner} import x\n",
        root / "other.py": f"from kiro_crew.discord import {owner} as low\n",
        root / "discord" / "relative.py": f"from .{owner} import x\n",
        root / "discord" / "deep" / "up.py": f"from .. import {owner}\n",
        root / "facade_user.py": f"from kiro_crew.discord.{STEM} import x\n",
    }
    assert _owner_importers(sources.items(), root) == {
        "pkg/caller.py",
        "other.py",
        "discord/relative.py",
        "discord/deep/up.py",
    }


def test_each_moved_class_is_served_whole_from_its_owner() -> None:
    """A class that moved keeps one object: the facade serves the owner's, whose
    ``__module__`` names the owner. Nothing keys behaviour on that module name."""
    for name, stem in MOVED_CLASSES.items():
        owner = _owner(stem)
        assert getattr(FACADE_MODULE, name) is getattr(owner, name), name
        assert getattr(owner, name).__module__ == owner.__name__, name


def test_the_decider_keeps_every_base_member_with_its_kind_and_signature() -> None:
    decider = FACADE_MODULE.DiscordApprovalDecider
    assert sorted(set(DECIDER_BASE_MEMBERS) - _members(decider)) == []
    for name, described in DECIDER_BASE_MEMBERS.items():
        assert _describe(vars(decider)[name], decider) == described, name
