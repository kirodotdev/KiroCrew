"""Update check/apply, log level, ring buffer, and SSE stream handlers."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of the update, log and status-stream routes: every name is
# reached through it as ``kiro_crew.dashboard.handlers.updates.<name>``. A read is served by
# the owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` --
# reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this
# module, and it keeps no code of its own. New work goes to the owner of its responsibility:
#   ``updates_verdict`` -- the cached verdict, version display and the auto-update effect
#   ``updates_check`` -- running the update check and the status fields that report it
#   ``updates_apply`` -- applying an update and restarting the gateway
#   ``updates_streams`` -- the log stream, the log level and the status SSE
#   ``updates_step_up`` -- arming and approving an in-app update
#   ``updates_channel`` -- the auto-update policy, release channel, revalidation, changelog
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.handlers.updates_verdict",
    "kiro_crew.dashboard.handlers.updates_check",
    "kiro_crew.dashboard.handlers.updates_apply",
    "kiro_crew.dashboard.handlers.updates_streams",
    "kiro_crew.dashboard.handlers.updates_step_up",
    "kiro_crew.dashboard.handlers.updates_channel",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.updates_apply import _build_frontend  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import _policy_apply_tasks  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import _PolicyApplyStopped  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import _resolve_restart_target  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import _restart_gateway  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import _run_tracked_policy_apply  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import _snapshot_memory_or_refuse  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import _venv_pip_install  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import api_gateway_restart  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import api_update_apply  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import api_update_cancel  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import api_update_simulate  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_apply import policy_applies_in_flight  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import _changelog_cache  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import _changelog_path  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import _read_changelog  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import api_changelog  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import api_releases  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import api_update_auto  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import api_update_channel  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import api_update_revalidate  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_channel import logger  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _CLI_MANIFEST_SCHEMA  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _FEED_MAX_BYTES  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _FEED_TIMEOUT_SECS  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _MIN_VERSION_RE  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _PUB_DATE_RE  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _UPDATE_CHECK_INTERVAL  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _VERSION_RE  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _can_arm_this_install  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _cancel_update_check  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _check_generation  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _check_git_checkout  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _check_release_feed  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _check_task  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _check_task_generation  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _check_via_provider  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _do_update_check  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _fetch_feed_bytes  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _invalidate_update_check  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _last_update_check  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _release_update_check_task  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import _run_update_check  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import api_update_check  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_check import status_update_fields  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_step_up import _arm_packaged_app  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_step_up import _audit_update_event  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_step_up import _loopback_peer  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_step_up import api_update_approve  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_step_up import api_update_arm  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_step_up import api_update_arm_status  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_step_up import api_update_disarm  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _LOG_LEVELS  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _LOG_RING_SIZE  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _SSE_INTERVAL_SECS  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _log_ring  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _log_ring_handler  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _QueueLogHandler  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _RingLogHandler  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _safe_ws_send  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import _without_pin_ranks  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import api_log_level  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import api_log_level_get  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import api_logs  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import api_stream  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import apply_log_level  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import install_log_ring_handler  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_streams import (  # noqa: F401
        _log_ring_handler_installed,
        apply_log_level_from_config,
    )
    from kiro_crew.dashboard.handlers.updates_verdict import _AUTO_EFFECT_TTL_SECS  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _FIRST_INT_RE  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _PEP440_RE  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _PRE_RANKS  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _auto_effect  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _auto_effect_task  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _capability_fields  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _channel_move_pending  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _display_local_version  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _display_version  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _effective_min_version  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _feed_requires_update  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _is_newer  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _set_update_info  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _shape_effect  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _update_info  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import _version_key  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import get_update_info  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import record_auto_update_effect  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import remediation_command  # noqa: F401
    from kiro_crew.dashboard.handlers.updates_verdict import (  # noqa: F401
        _downgrade_target_below_min_version,
        _effective_update_required,
        _refresh_auto_update_effect,
        _refresh_auto_update_effect_once,
        _start_auto_effect_refresh,
        _status_auto_update_effect,
        prime_status_auto_update_effect,
    )
if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import __version__ as _local_version  # noqa: F401
    from kiro_crew import dep_sync  # noqa: F401
    from kiro_crew import platform_compat  # noqa: F401
    from kiro_crew import shutdown_event  # noqa: F401
    from kiro_crew import update_ownership  # noqa: F401
    from kiro_crew.changelog import Release  # noqa: F401
    from kiro_crew.changelog import base_version  # noqa: F401
    from kiro_crew.changelog import build_release_list  # noqa: F401
    from kiro_crew.changelog import release_of_build  # noqa: F401
    from kiro_crew.config.live import ConfigChange  # noqa: F401
    from kiro_crew.config.loader import ConfigReadError  # noqa: F401
    from kiro_crew.config.loader import KiroCrewConfig  # noqa: F401
    from kiro_crew.config.loader import coerce_dict_section  # noqa: F401
    from kiro_crew.config.loader import config_path  # noqa: F401
    from kiro_crew.config.loader import overlay_pins  # noqa: F401
    from kiro_crew.config.loader import update_config_locked  # noqa: F401
    from kiro_crew.dashboard.chat_utils import run_config_write  # noqa: F401
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request  # noqa: F401
    from kiro_crew.dashboard.handlers._update_feed import fetch_feed_bytes  # noqa: F401
    from kiro_crew.dashboard.state import DashboardState  # noqa: F401
    from kiro_crew.dashboard.state import chat_message_frame  # noqa: F401
    from kiro_crew.dashboard.status_counts import cached_status_snapshot  # noqa: F401
    from kiro_crew.dashboard.urls import is_loopback  # noqa: F401
    from kiro_crew.executors import subprocess_executor  # noqa: F401
    from kiro_crew.gateway_restart import resolve_restart_launcher  # noqa: F401
    from kiro_crew.git_divergence import UNREADABLE_TIMEOUT  # noqa: F401
    from kiro_crew.git_divergence import DivergenceUnreadable  # noqa: F401
    from kiro_crew.git_divergence import count_divergence  # noqa: F401
    from kiro_crew.platform import feed_trust  # noqa: F401
    from kiro_crew.platform.update_capability import AUTO_EFFECT_UNKNOWN  # noqa: F401
    from kiro_crew.platform.update_capability import CHECK_DEFERRED  # noqa: F401
    from kiro_crew.platform.update_capability import CHECK_FAILED  # noqa: F401
    from kiro_crew.platform.update_capability import CHECK_SUCCEEDED  # noqa: F401
    from kiro_crew.platform.update_capability import CHECK_UNCHECKED  # noqa: F401
    from kiro_crew.platform.update_capability import ERR_FEED_MALFORMED  # noqa: F401
    from kiro_crew.platform.update_capability import ERR_FEED_UNREACHABLE  # noqa: F401
    from kiro_crew.platform.update_capability import ERR_GIT_FETCH_FAILED  # noqa: F401
    from kiro_crew.platform.update_capability import ERR_GIT_READ_FAILED  # noqa: F401
    from kiro_crew.platform.update_capability import ERR_UNKNOWN  # noqa: F401
    from kiro_crew.platform.update_capability import ERR_VERSION_UNPARSEABLE  # noqa: F401
    from kiro_crew.platform.update_capability import EXTERNALLY_MANAGED_MESSAGES  # noqa: F401
    from kiro_crew.platform.update_capability import MANAGED_BY_COMMAND  # noqa: F401
    from kiro_crew.platform.update_capability import MANAGED_BY_GIT  # noqa: F401
    from kiro_crew.platform.update_capability import MODE_NONE  # noqa: F401
    from kiro_crew.platform.update_capability import MODE_NOTIFY  # noqa: F401
    from kiro_crew.platform.update_capability import AutoUpdateEffect  # noqa: F401
    from kiro_crew.platform.update_capability import UpdateCapability  # noqa: F401
    from kiro_crew.platform.update_capability import auto_update_effect  # noqa: F401
    from kiro_crew.platform.update_capability import bundled_by_desktop_app  # noqa: F401
    from kiro_crew.platform.update_capability import derive_capability  # noqa: F401
    from kiro_crew.platform.update_governance import min_version  # noqa: F401
    from kiro_crew.platform.update_governance import resolve_remote_url  # noqa: F401
    from kiro_crew.platform.update_governance import update_blocked_reason  # noqa: F401
    from kiro_crew.platform.update_governance import update_required  # noqa: F401
    from kiro_crew.platform.update_layout import cdn_bases as _cdn_bases  # noqa: F401
    from kiro_crew.platform.update_layout import detect_install_layout  # noqa: F401
    from kiro_crew.platform.update_layout import set_release_channel  # noqa: F401
    from kiro_crew.platform.update_layout import wheel_update_command  # noqa: F401
    from kiro_crew.platform.update_layout import release_channel as _release_channel  # noqa: F401
    from kiro_crew.platform.update_provider import CommandProvider  # noqa: F401
    from kiro_crew.platform.update_provider import resolve_provider  # noqa: F401
    from kiro_crew.platform_compat import exit_after_failed_restart_exec  # noqa: F401
    from kiro_crew.platform_compat import reexec_launcher  # noqa: F401
    from kiro_crew.platform_compat import reexec_python_module  # noqa: F401
    from kiro_crew.pre_exec_stops import run_pre_exec_stops  # noqa: F401
    from kiro_crew.safety_override import flush_breadcrumb_writes  # noqa: F401
    from kiro_crew.security import redact_credentials  # noqa: F401
    from kiro_crew.security import redact_exfiltration_urls  # noqa: F401
