"""Contracts the ``config.sections`` facade keeps over its ``sections_*`` owners.

``test_facade_mirror_guards.py`` holds every mirrored facade to the generic contract
(one object per name, the TYPE_CHECKING manifest, no owner importing its facade, patch
reach). This file pins what is particular to this one: its owner list, that it binds
every name its owners define except a global an owner rebinds, and its frozen import
inventory, which stands in for the F401 check ``setup.cfg`` switches off for it.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
from pathlib import Path

import pytest

from kiro_crew import _facade_mirror
from kiro_crew.config import sections

_OWNERS = tuple(
    f"kiro_crew.config.sections_{name}"
    for name in (
        "defaults",
        "agent_env",
        "agent_fields",
        "agent",
        "session",
        "limits",
        "tailnet",
        "dashboard",
        "dashboard_hosts",
        "slack",
        "channels",
        "telegram",
        "stt",
        "decisions",
        "telemetry",
    )
)

#: Every name the facade's import statements bound before its owners moved out. They
#: stay bound here because callers and tests read them as attributes of this module.
_BASE_IMPORTS = frozenset("""
    Any AppSettingsConfig AwsConfig Callable ComputerUseConfig CronHistoryConfig
    DEFAULT_ACP_BACKEND DEFAULT_AUTO_INGEST_ARTIFACT_KINDS DEFAULT_MAX_PARALLEL_STEPS DEFAULT_RUNTIME_CEILING_SECS
    DEGRADED_TAILSCALE EFFORT_LEVELS ExternalRegistryConfig FORWARD_DECLARED_ENV_DEFAULT
    HeartbeatConfig InstancesConfig Iterable KnowledgeConfig MANAGED_STUB_FOLLOWERS
    MAX_RUNTIME_CEILING_SECS Mapping McpConfig McpGatewayConfig MemoryConfig MemoryStoreConfig
    MessagingConfig MonitoringConfig Path PublishConfig SECRET_URI_PREFIX SessionSummaryConfig
    SkillsConfig TaskRunnerConfig TunnelConfig TypeVar WatchdogConfig WorkLedgerConfig _COLOR_HEX_RE
    _CONNECT_TIMEOUT_CEILING _CU_DEFAULT_ATTACH_SCREENSHOT _CU_DEFAULT_MAX_TREE_DEPTH
    _CU_DEFAULT_MAX_TREE_NODES _CU_DEFAULT_SCREENSHOT_JPEG_QUALITY _CU_DEFAULT_SCREENSHOT_MAX_PX
    _CU_DEFAULT_TEXT_LIMIT _DEFAULT_BACKOFF_MAX _DEFAULT_MAX_RECOVERY _DEFAULT_PROBE_FAILS
    _DEFAULT_SPAWN_MIN_MEMORY_GB _DEFAULT_SSH_COMPRESSION _DEFAULT_SUBAGENT_COST_GB
    _DEFAULT_SUBAGENT_MAX_TURNS _DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS _DEFAULT_TUNNEL_BASE_PORT
    _DEFAULT_WARM_SET_CAP _MAX_RECOVERY_CEILING _MINT_TIMEOUT_CEILING _MINT_TIMEOUT_FLOOR
    _OBSERVED_DEGRADED_SECTIONS _RECOVER_BACKOFF_CEILING _STT_CATALOG _STT_CUSTOM_MODEL
    _STT_DEFAULT_IDLE_EVICT_SECS _STT_DEFAULT_MODEL _STT_DEFAULT_PARTIAL_INTERVAL_MS
    _STT_DEFAULT_SILENCE_MS _SUBAGENT_TIMEOUT_MAX _SUBAGENT_TIMEOUT_MIN _SUBAGENT_TIMEOUT_SECS
    _WARM_SET_CAP_AUTO _coerce_embedding_provider _coerce_int _declared_fields _is_invisible
    _meta _os _port_or_unset _re _read_auto_add_documents _resolve_stt_model _resolve_stub_base
    _resolve_stub_overrides _resolve_stub_roster _resolve_stub_servers _safe_bool _safe_color
    _safe_dict _safe_float _safe_int _safe_list _safe_nonnegative_int _safe_pack_id
    _unicodedata _urlsplit _valid_stt_custom_path _valid_stt_custom_sha256 dataclass field
    field_default is_valid_effort logging math model_registry overload
    resolve_memory_store_config resolve_selected_backend
    """.split())

#: The names the facade's own glue imports: the owner loop, the mirror, its class swap.
_GLUE_IMPORTS = frozenset({"TYPE_CHECKING", "_facade_mirror", "_importlib", "_sys"})


def _index() -> _facade_mirror.MirrorIndex:
    index = vars(sections)[_facade_mirror.INDEX_ATTRIBUTE]
    assert isinstance(index, _facade_mirror.MirrorIndex)
    return index


def _tree(module_name: str) -> ast.Module:
    path = Path(importlib.import_module(module_name).__file__)
    return ast.parse(path.read_text(encoding="utf-8"))


def _defined_by(module_name: str) -> set[str]:
    """Names *module_name* defines at its top level (a def, class or assignment)."""
    return _facade_mirror._defined_names(_tree(module_name))


def _rebound_by(module_name: str) -> set[str]:
    return {
        name
        for node in ast.walk(_tree(module_name))
        if isinstance(node, ast.Global)
        for name in node.names
    }


def _is_type_checking(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING"


def unexpected_imports(source: str, owner_names: dict[str, set[str]]) -> list[str]:
    """Names *source* binds by a runtime import beyond the frozen inventory.

    The inventory is the BASE imports, the glue's own, and from each owner module only
    names that owner defines. Imports under ``if TYPE_CHECKING:`` bind nothing at run
    time and are the mirror guard's business, so they are skipped.
    """
    found: list[str] = []
    for node in ast.parse(source).body:
        if _is_type_checking(node):
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
            elif name not in _BASE_IMPORTS | _GLUE_IMPORTS:
                found.append(name)
    return found


def _owner_names() -> dict[str, set[str]]:
    return {owner: _defined_by(owner) for owner in _OWNERS}


def test_owners_are_every_sections_sibling_lowest_layer_first() -> None:
    index = _index()
    assert (index.owners, index.excluded, dict(index.forwards)) == (_OWNERS, (), {})
    assert index.divergent() == ()
    siblings = sorted(
        f"kiro_crew.config.{path.stem}"
        for path in Path(sections.__file__).parent.glob("sections_*.py")
    )
    assert siblings == sorted(_OWNERS)


def test_each_owner_imports_only_owners_listed_before_it() -> None:
    for position, owner in enumerate(_OWNERS):
        imported = {
            node.module
            for node in _tree(owner).body
            if isinstance(node, ast.ImportFrom) and node.module in _OWNERS
        }
        assert imported <= set(_OWNERS[:position]), owner


def test_the_facade_binds_every_owner_name_but_a_rebound_global() -> None:
    """A global an owner rebinds at run time is served from the owner: a copy bound here
    would keep the old value after the owner replaced it. Every other owner-defined
    name is bound here, so the namespace reads as it did before the split."""
    rebound = {name for owner in _OWNERS for name in _rebound_by(owner)}
    assert rebound == {"_LAST_WARNED_TRANSCRIBE_LANGUAGES", "_LAST_WARNED_TRANSCRIBE_VOCABULARY"}
    assert set(_index().exports) == rebound
    defined = {name for owner in _OWNERS for name in _defined_by(owner)}
    assert sorted(defined - rebound - set(vars(sections))) == []
    assert sorted(rebound & set(vars(sections))) == []


def test_every_owner_dataclass_is_bound_on_the_facade() -> None:
    """``test_config_schema`` evaluates field annotations in ``vars(config.sections)``,
    and a name missing there makes it skip that subtree without failing."""
    for owner in _OWNERS:
        module = importlib.import_module(owner)
        for name, value in vars(module).items():
            if dataclasses.is_dataclass(value) and value.__module__ == owner:
                assert vars(sections).get(name) is value, f"{owner}.{name}"


def test_a_rebound_global_patched_here_reaches_its_owner() -> None:
    stt = importlib.import_module("kiro_crew.config.sections_stt")
    before = stt._LAST_WARNED_TRANSCRIBE_VOCABULARY
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sections, "_LAST_WARNED_TRANSCRIBE_VOCABULARY", "'patched'")
        assert stt._LAST_WARNED_TRANSCRIBE_VOCABULARY == "'patched'"
        assert sections._LAST_WARNED_TRANSCRIBE_VOCABULARY == "'patched'"
        assert "_LAST_WARNED_TRANSCRIBE_VOCABULARY" not in vars(sections)
    assert stt._LAST_WARNED_TRANSCRIBE_VOCABULARY is before
    assert "_LAST_WARNED_TRANSCRIBE_VOCABULARY" not in vars(sections)


def test_the_facade_imports_nothing_beyond_its_frozen_inventory() -> None:
    source = Path(sections.__file__).read_text(encoding="utf-8")
    assert unexpected_imports(source, _owner_names()) == []


def test_the_inventory_check_reports_a_planted_import() -> None:
    """The F401 stand-in must be able to fail: a stray import, and an owner import of a
    name that owner does not define, are both reported."""
    source = Path(sections.__file__).read_text(encoding="utf-8")
    planted = source + "\nimport json\nfrom kiro_crew.config.sections_stt import SectionReader\n"
    assert unexpected_imports(planted, _owner_names()) == [
        "json",
        "kiro_crew.config.sections_stt.SectionReader",
    ]
