"""Agent discovery — scans ~/.kiro/agents/ for installed agents.

Provides ``list_agents()`` which returns metadata about all installed
agents, including KiroCrew's own agent and any agents shipped by
locally-installed skill packages (agent config files on disk). It only
reads on-disk agent config files and has no external-tool dependency.

Each agent is identified by its ``modeId`` — the value passed to
``session/set_mode`` in the ACP protocol to switch the backend's behavior.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import agent_state, hooks  # noqa: F401
from kiro_crew.agent_files import (  # noqa: F401
    AGENT_FILENAME,
    GUEST_AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
)
from kiro_crew.agent_spec_format import (  # noqa: F401
    is_agent_spec_name,
    is_app_agent_launch_name,
    is_markdown_spec,
    is_native_skill_alias_name,
    iter_agent_spec_files,
    markdown_head_is_fenceless,
    parse_agent_spec_bytes,
    shadowed_markdown_specs,
    spec_stem,
)
from kiro_crew.config.paths import (  # noqa: F401
    kiro_agents_dir,
    project_agents_dir,
    project_kiro_dir,
)
from kiro_crew.dashboard.side_readonly_spec import is_readonly_spec_description  # noqa: F401
from kiro_crew.executors import discovery_executor  # noqa: F401
from kiro_crew.hooks import FileTooLargeError, is_unc_shape, unc_probe_allowed  # noqa: F401
from kiro_crew.pinned_fs import open_fenced_for_read  # noqa: F401
from kiro_crew.platform import redact_log_via_context  # noqa: F401
from kiro_crew.security import is_sensitive_canonical_path, is_sensitive_path  # noqa: F401
from kiro_crew.sel import sel as _sel  # noqa: F401
from kiro_crew.user_json import strict_json_equal  # noqa: F401

logger = logging.getLogger(__name__)


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.agent_discovery_parts.agent_roster import (  # noqa: F401
        _global_agent_info,
        _mcp_server_names,
        _project_agent_info,
        _with_edition_agents,
        agent_model_map,
        agent_welcome_message,
        list_agents,
    )
    from kiro_crew.agent_discovery_parts.dir_revision import (  # noqa: F401
        _AGENTS_DIR_MEMO_MAX_KEYS,
        _AGENTS_DIR_RACY_MAX_DIGEST_BYTES,
        _AGENTS_DIR_RACY_MAX_DIGESTS,
        _AGENTS_DIR_RACY_WINDOW_NS,
        _DIGEST_CHUNK_BYTES,
        _NO_DIGEST,
        AGENTS_DIR_MEMO_ENABLED,
        AgentsDirMemo,
        AgentsDirRevision,
        T,
        _revision_pin,
        _SpecStatRevision,
        _young_entry_digest,
        agents_dir_revision,
    )
    from kiro_crew.agent_discovery_parts.project_agents import (  # noqa: F401
        _declared_project_agent_name,
        _project_agent_fallback_name,
        _project_scope_denied,
        _project_signature,
        _scan_project_agent_files,
        cached_project_agent_names,
        clear_project_agent_cache,
        project_agent_files,
        project_agent_name,
        project_agent_names,
        warm_project_agent_names,
    )
    from kiro_crew.agent_discovery_parts.skill_scope import (  # noqa: F401
        SkillScopeResolutionError,
        agent_skill_globs,
        can_inherit_skills,
        inherited_skill_globs,
        served_provider_label,
        session_skill_globs,
        skill_search_refusal,
    )
    from kiro_crew.agent_discovery_parts.spec_fields import (  # noqa: F401
        _DEFER_MODEL,
        _SKILL_SEARCH_TOOL,
        AGENT_SPEC_SUFFIX,
        SCOPE_GLOBAL,
        SCOPE_PROJECT,
        SKILL_URI_PREFIX,
        WELCOME_MESSAGE_MAX_CHARS,
        AgentInfo,
        _builder_mcp_skills,
        _extract_skills,
        _skills_from_resources,
        expand_skill_uri,
        is_internal_agent_spec,
        skill_resource_uris,
        skill_uri_for_path,
        spec_model,
        spec_str,
        spec_welcome_message,
    )
    from kiro_crew.agent_discovery_parts.spec_reads import (  # noqa: F401
        _APPLE_DOUBLE_MAGICS,
        _FENCE_PROBE_BYTES,
        _KIRO_AGENTS_DIR,
        _WINDOWS,
        AmbiguousAgentSpecError,
        SensitiveAgentSpecPathError,
        _audit_denied,
        _fence_refuses,
        _kiro_agents_dir,
        _probe_spec_head,
        _read_agent_spec,
        _read_head,
        _read_spec_bytes,
        _SpecReadRefused,
        _unc_refused,
        _warn_on_systematic_scan_failure,
        agent_spec_stems,
        apple_double_sidecar,
        plain_markdown_document,
        read_agent_spec_strict,
        spec_by_declared_name,
    )
    from kiro_crew.agent_discovery_parts.spec_snapshot import (  # noqa: F401
        _LIST_AGENTS_CACHE,
        _LIST_AGENTS_KEY,
        _PARSED_SPECS_CACHE,
        _PARSED_SPECS_GEN,
        _PARSED_SPECS_LOCK,
        _PARSED_SPECS_REFRESHING,
        _PROJECT_NAMES_CACHE,
        _dir_signature,
        _iter_spec_entries,
        _ListAgentsSig,
        cached_agent_specs,
        clear_list_agents_cache,
        parsed_agent_specs,
        spec_cache_generation,
        warm_agent_specs,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import asyncio  # noqa: F401
    import errno  # noqa: F401
    import fnmatch  # noqa: F401
    import functools  # noqa: F401
    import hashlib  # noqa: F401
    import os  # noqa: F401
    import stat  # noqa: F401
    import threading  # noqa: F401
    import time  # noqa: F401
    from dataclasses import asdict, dataclass, field  # noqa: F401
    from pathlib import Path  # noqa: F401
    from typing import Any, Callable, Generic, Iterator, Sequence, TypeVar  # noqa: F401
# This module is the facade of the agent-discovery owners in
# ``kiro_crew.agent_discovery_parts``, and every name is reached through it as
# ``kiro_crew.agent_discovery.<name>``: a read is served by the owner that binds the name,
# and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding it
# (:mod:`kiro_crew._facade_mirror`). Owners never import this module, and it keeps no code
# of its own: new work goes to the owner whose responsibility it is, listed in
# :mod:`kiro_crew.agent_discovery_parts`.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.agent_discovery_parts.spec_reads",
    "kiro_crew.agent_discovery_parts.spec_fields",
    "kiro_crew.agent_discovery_parts.spec_snapshot",
    "kiro_crew.agent_discovery_parts.dir_revision",
    "kiro_crew.agent_discovery_parts.project_agents",
    "kiro_crew.agent_discovery_parts.agent_roster",
    "kiro_crew.agent_discovery_parts.skill_scope",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name the module and its owners bind.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
