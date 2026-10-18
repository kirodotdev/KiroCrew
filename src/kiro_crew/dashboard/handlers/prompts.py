"""Prompts (Agent SOPs) and Skills API handlers."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the prompt and skill routes: every name is reached through
# it as ``kiro_crew.dashboard.handlers.prompts.<name>``. A read is served by the owner that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New
# work goes to the owner of its responsibility:
#   ``prompts_caller_gates`` -- who may read or change a prompt or skill, and the audit
#   ``prompts_paths`` -- where prompts live on disk and how a prompt directory is pinned
#   ``prompts_reading`` -- listing and describing prompts
#   ``prompts_authoring`` -- reading, creating, updating and deleting one prompt
#   ``prompts_skill_catalog`` -- the skills catalog, project trust, a skill's file tree
#   ``prompts_skill_edits`` -- a skill's detail, edits, pin and inject toggles, creation
#   ``prompts_skill_pending`` -- staged skill candidates: list, read, approve, dismiss
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.prompts_caller_gates",
    "kiro_crew.dashboard.handlers.prompts_paths",
    "kiro_crew.dashboard.handlers.prompts_reading",
    "kiro_crew.dashboard.handlers.prompts_authoring",
    "kiro_crew.dashboard.handlers.prompts_skill_catalog",
    "kiro_crew.dashboard.handlers.prompts_skill_edits",
    "kiro_crew.dashboard.handlers.prompts_skill_pending",
)  # the loop below imports them in this order
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.prompts_authoring import _PROMPT_SCOPES  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import _PROMPT_WRITE_LOCK  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import MAX_PROMPT_BYTES  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import MAX_PROMPT_NAME_BYTES  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import _api_prompt_write  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import _api_user_prompt_detail  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import _utf8_or_none  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import api_prompt_detail  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import api_prompts_create  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_authoring import (  # noqa: F401
        _invalidate_prompt_cache,
    )
    from kiro_crew.dashboard.handlers.prompts_caller_gates import _audit_unread  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_caller_gates import _named_slot  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_caller_gates import _prompt_audit  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_caller_gates import _refuse  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_caller_gates import _sel  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_caller_gates import (  # noqa: F401
        _CODE_APP_TOKEN_FORBIDDEN,
        _CODE_DASHBOARD_OWNER_REQUIRED,
        _DASHBOARD_SURFACE_KEY,
        _deny_foreign_app_skill_slot,
        _deny_non_owner_prompt_write,
        _deny_non_owner_skill_operation,
        _prompt_local_project,
    )
    from kiro_crew.dashboard.handlers.prompts_paths import _DIR_FD_SUPPORTED  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _IS_WINDOWS  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _UNNAMED_CREATE_SUPPORTED  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _WINERROR_BUFFER_OVERFLOW  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _WINERRORS_TOO_LONG  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _is_path_too_long_error  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _linked_prompt_root  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _local_prompt_scan_root  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _pin_prompt_dir  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _plain_stem_ok  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _prompt_read_root  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _prompt_read_within_root  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _resolve_prompt_dir  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import _user_prompt_dir  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_paths import (  # noqa: F401
        _WINERROR_FILENAME_EXCED_RANGE,
        _local_prompt_dir_in_project,
    )
    from kiro_crew.dashboard.handlers.prompts_reading import _description_from_text  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_reading import _extract_sop_description  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_reading import _find_prompt  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_reading import _gated_sop_description  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_reading import _list_aim_prompts  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_reading import _local_prompt_entry  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_reading import _redact_prompt  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_reading import api_prompts  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import _catalog_handoff  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import _catalog_lock  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import _catalog_waiters  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import _trust_snapshot  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import api_skill_file  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import api_skill_tree  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import api_skills  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import api_skills_trust  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_catalog import (  # noqa: F401
        _assemble_skills_catalog,
        _assemble_skills_catalog_uncached,
        _catalog_assembly_locks,
        _grant_reviewed_project,
        api_skills_trust_grant,
        api_skills_trust_revoke,
    )
    from kiro_crew.dashboard.handlers.prompts_skill_edits import _SKILL_WRITE_LOCK  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_edits import _match_package_row  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_edits import api_skill_detail  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_edits import api_skill_pin  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_edits import api_skills_create  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_edits import logger  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_edits import (  # noqa: F401
        READONLY_SKILL_KEY_PREFIXES,
        _under_skill_write_lock,
        api_skill_inject_on_trigger,
    )
    from kiro_crew.dashboard.handlers.prompts_skill_pending import api_skills_pending  # noqa: F401
    from kiro_crew.dashboard.handlers.prompts_skill_pending import (  # noqa: F401
        _CODE_LIVE_SKILL_EXISTS,
        _CODE_PENDING_APPROVAL_REFUSED,
        _CODE_PENDING_SKILL_NOT_FOUND,
        _CODE_PENDING_SKILL_UNREADABLE,
        _CODE_SCRIPT_VALIDATION_FAILED,
        api_skill_pending_approve,
        api_skill_pending_detail,
        api_skill_pending_dismiss,
        api_skills_pending_dismiss_all,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import pinned_fs  # noqa: F401
    from kiro_crew.agent_discovery import SkillScopeResolutionError  # noqa: F401
    from kiro_crew.agent_discovery import agent_skill_globs  # noqa: F401
    from kiro_crew.agent_discovery import served_provider_label  # noqa: F401
    from kiro_crew.agent_discovery import session_skill_globs  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _capability_manager  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _get_skills  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _read_session_key  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import _resolve_skill_root  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import active_project_dir  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import collect_skills_blocking  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import list_skill_tree  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import read_bounded_json  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import read_skill_file  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import requesting_slot_project  # noqa: F401
    from kiro_crew.dashboard.handlers.source_providers import (  # noqa: F401
        is_owner_dashboard_request,
    )
    from kiro_crew.dashboard.slot_ownership import slot_not_found  # noqa: F401
    from kiro_crew.dashboard.state import DashboardState  # noqa: F401
    from kiro_crew.executors import discovery_executor  # noqa: F401
    from kiro_crew.frontmatter import SKILL_LOADER  # noqa: F401
    from kiro_crew.frontmatter import parse_frontmatter  # noqa: F401
    from kiro_crew.hooks import FileTooLargeError  # noqa: F401
    from kiro_crew.hooks import safe_read_file_bytes_nolink  # noqa: F401
    from kiro_crew.hooks import validate_file_path  # noqa: F401
    from kiro_crew.hooks import verified_replace_file_nolink  # noqa: F401
    from kiro_crew.loop_lock import LoopBoundLock  # noqa: F401
    from kiro_crew.platform_compat import is_link_or_junction  # noqa: F401
    from kiro_crew.security import redact_credentials  # noqa: F401
    from kiro_crew.security import redact_exfiltration_urls  # noqa: F401
    from kiro_crew.skill_trust import (  # noqa: F401
        ReviewedProjectChanged as _ReviewedProjectChanged,
    )
    from kiro_crew.skill_trust import TrustStoreFull  # noqa: F401
    from kiro_crew.skill_trust import TrustStoreUnreadable  # noqa: F401
    from kiro_crew.skill_trust import canonical_key  # noqa: F401
    from kiro_crew.skill_trust import grant_project_trust  # noqa: F401
    from kiro_crew.skill_trust import is_key_trusted  # noqa: F401
    from kiro_crew.skill_trust import list_trusted_projects  # noqa: F401
    from kiro_crew.skill_trust import revoke_project_trust  # noqa: F401
    from kiro_crew.skills import PROJECT_SKILL_BODY_CAP  # noqa: F401
    from kiro_crew.skills import SKILL_READ_CAPACITY  # noqa: F401
    from kiro_crew.skills import PendingApprovalRefused  # noqa: F401
    from kiro_crew.skills import SkillReadRefusal  # noqa: F401
    from kiro_crew.validation import MAX_SKILL_KEY_CHARS  # noqa: F401
