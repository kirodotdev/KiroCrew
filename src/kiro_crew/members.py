"""Per-crew-member space: ``$KIROCREW_HOME/members/<slug>/``.

A crew member is the same agent running with different context, so its space
holds what belongs to that member alone rather than to the user as a whole. The
first occupant is ``activity.jsonl`` — pointers to the sessions the member took
part in, which is the signal trigger generation reads.

The directory name is a **slug**: stable, immutable, and path-safe. A member's
display name is editable independently, so a rename never has to move files.
This mirrors the artifact store's ``artifacts/<slug>/`` layout, and reuses its
:func:`~kiro_crew.artifacts.slugify` so both surfaces normalize names the same
way.

Activity entries are pointers by design: they carry the session key, not a copy
of what happened. Details are read back from the session itself, so the log
cannot drift from the transcript — and it survives session pruning, which is why
frequency counts taken from it stay stable.
"""

from __future__ import annotations

import importlib as _importlib
import logging
import sys as _sys
from typing import TYPE_CHECKING

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import context_omissions, member_records, platform_compat  # noqa: F401
from kiro_crew.artifacts import slugify  # noqa: F401
from kiro_crew.atomic_write import atomic_write, fsync_dir, read_bytes_with_retry  # noqa: F401
from kiro_crew.config.paths import data_home  # noqa: F401
from kiro_crew.external_text import external_text_requires_redaction  # noqa: F401
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV  # noqa: F401
from kiro_crew.pinned_fs import (  # noqa: F401
    PinnedPathRefusal,
    open_in_pinned_parent,
    supports_pinned_walk,
)
from kiro_crew.slugs import slug_hash_fallback  # noqa: F401
from kiro_crew.validation import (  # noqa: F401
    _AGENT_NAME_RE,
    MAX_SHORT_STRING,
    normalize_unicode,
    sanitize_string,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.members_parts.dm_bindings import (  # noqa: F401
        attributable_session_key,
        dm_binding_path,
        member_identity_for_session,
        read_dm_binding,
        read_dm_binding_for_slot,
        slug_from_dm_slot_key,
        write_dm_binding,
    )
    from kiro_crew.members_parts.member_activity import (  # noqa: F401
        _TRACEABLE_MEMORY_MODES,
        _read_activity_checked,
        read_activity,
        record_activity,
    )
    from kiro_crew.members_parts.member_sessions import (  # noqa: F401
        _CREWMATE_WORKER_MEMBERS,
        _CREWMATE_WORKER_PROMPT_IN_CONTEXT,
        _CREWMATE_WORKER_SLOTS,
        MEMBER_DISPATCH_SERVER,
        MEMBER_PANEL_SERVER,
        MemberLifecycle,
        MemberTurnContext,
        _member_session_element,
        _slot_part,
        crew_panel_enabled,
        crewmate_worker_member_id,
        crewmate_worker_prompt_in_context,
        forget_crewmate_worker_session,
        is_crewmate_worker_session,
        is_member_session_key,
        member_dispatch_session_server,
        member_lifecycle,
        member_panel_session_server,
        member_slot_key,
        member_thread_session_alias,
        member_turn_context,
        note_crewmate_worker_prompt_channel,
        note_crewmate_worker_session,
        select_provider_backend,
    )
    from kiro_crew.members_parts.member_space import (  # noqa: F401
        _CREW_ID_BASE_MAX_CHARS,
        _CREW_ID_SUFFIX_ATTEMPTS,
        _SLUG_RE,
        ACTIVITY_FILE_NAME,
        BRIEFING_FILE_NAME,
        DM_BINDINGS_DIR_NAME,
        DM_FILE_NAME,
        DM_SLOT_KEY_PREFIX,
        DM_SLOT_MODE,
        MEMBER_BRIEFING_MAX_CHARS,
        MEMBER_NAME_MAX_CHARS,
        MEMBER_RULES_MAX_CHARS,
        MEMBERS_DIR_NAME,
        MEMORY_STORE_SLOT_SUFFIX,
        RULES_DIR_NAME,
        WELCOME_KIND_GOAL,
        WELCOME_KIND_MATE,
        WELCOME_OWED_FILENAME,
        MemberNameError,
        MemberNameTaken,
        MemberSlugError,
        NewCrewKey,
        _stable_member_slug,
        canonical_member_key,
        check_crew_label,
        count_person_created_crewmates,
        crew_id_for_display_name,
        crew_showing,
        is_configured_dispatchable_member,
        is_crew_id,
        is_dispatchable_member_name,
        is_person_created_crewmate,
        is_readable_member_name,
        is_valid_member_name,
        key_new_crew,
        mark_welcome_owed,
        member_dir,
        member_pin_matches,
        member_slug,
        members_root,
        owed_welcome_kind,
        slug_for_name,
        validate_member_name,
        validate_slug,
        welcome_owed,
    )
    from kiro_crew.members_parts.prompt_layers import (  # noqa: F401
        _RULES_WRITE_LOCK,
        MEMBER_BRIEFING_TRUNCATION_MARKER,
        MemberRulesChanged,
        MemberRulesUnreadable,
        _briefing_pinned_target,
        _write_member_rules,
        cap_member_briefing,
        member_briefing_path,
        member_briefing_supported,
        member_rules_path,
        read_member_briefing,
        read_member_briefing_bounded,
        read_member_rules,
        write_member_rules,
    )
    from kiro_crew.members_parts.self_edit import (  # noqa: F401
        DEFAULT_GHOST_TRAITS,
        MEMBER_DESCRIPTION_MAX_CHARS,
        SELF_EDITABLE_FIELDS,
        SelfRenameError,
        SelfUpdateError,
        _copy_value,
        _self_target,
        _strip_controls,
        _write_self_fields,
        clean_description,
        clean_display_name,
        clean_self_avatar,
        rename_member_display,
        undo_member_self,
        update_member_self,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    import json  # noqa: F401
    import os  # noqa: F401
    import re  # noqa: F401
    import stat  # noqa: F401
    import threading  # noqa: F401
    from collections.abc import Container, Mapping  # noqa: F401
    from dataclasses import dataclass  # noqa: F401
    from datetime import datetime, timezone  # noqa: F401
    from enum import Enum  # noqa: F401
    from pathlib import Path  # noqa: F401
    from typing import Any  # noqa: F401
# This module is the facade of the member-space owners in ``kiro_crew.members_parts``,
# and every name is reached through it as ``kiro_crew.members.<name>``: a read is served by
# the owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` --
# reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import
# this module, and it keeps no code of its own: new work goes to the owner whose
# responsibility it is, listed in :mod:`kiro_crew.members_parts`.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.members_parts.member_space",
    "kiro_crew.members_parts.member_sessions",
    "kiro_crew.members_parts.dm_bindings",
    "kiro_crew.members_parts.self_edit",
    "kiro_crew.members_parts.prompt_layers",
    "kiro_crew.members_parts.member_activity",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name the module and its owners bind.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
