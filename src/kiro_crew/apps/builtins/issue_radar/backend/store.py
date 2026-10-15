"""Issue Radar — on-disk data layout.

Everything lives under ``~/.kiro/crew/apps/issue-radar/data/`` (via
``kiro_crew.apps.manager.app_data_dir``, the platform-standard app-scoped data
dir). Nothing is stored on a KiroCrew-hosted backend and no GitHub App/PAT is
used — auth is entirely delegated to the user's own ``gh`` CLI session.

Layout::

    <data_dir>/config.json                          # connected repos, no secrets
    <data_dir>/repos/{owner}/{repo}/issues-cache.json   # last-fetched open issues
    <data_dir>/repos/{owner}/{repo}/members-cache.json  # repo members (derived)

``root`` is accepted on every function (mirroring code_review_sage's
``store.py``) so tests can point at a tmp dir instead of the real app data dir.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
from typing import TYPE_CHECKING

from kiro_crew import _facade_mirror

# This module is the facade of Issue Radar's on-disk store, and every name is reached through it
# as ``kiro_crew.apps.builtins.issue_radar.backend.store.<name>``: a read is served by the owner
# that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. New work
# goes to the owner of its responsibility:
#   ``store_repos`` -- data root, ``config.json``, repos, settings, watch state, GitLab groups
#   ``store_labels`` -- the label and member caches
#   ``store_ai`` -- AI caches, recommendations, tagging queue, investigations, the clock
#   ``store_deps`` -- the dependency-edge cache
#   ``store_issues`` -- issue list, detail and reference caches, triage write-through
#   ``store_pulls`` -- pull-request list and detail caches


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.apps.builtins.issue_radar.backend.store_repos",
    "kiro_crew.apps.builtins.issue_radar.backend.store_labels",
    "kiro_crew.apps.builtins.issue_radar.backend.store_ai",
    "kiro_crew.apps.builtins.issue_radar.backend.store_deps",
    "kiro_crew.apps.builtins.issue_radar.backend.store_issues",
    "kiro_crew.apps.builtins.issue_radar.backend.store_pulls",
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
    from kiro_crew.apps.builtins.issue_radar.backend.store_ai import (  # noqa: F401
        _INVESTIGATION_STATUSES,
        TAGGING_CACHE_SCHEMA,
        _cache_generated_at,
        _findings_run_key,
        _merge_findings,
        _normalize_findings,
        _normalize_tagging,
        _now_iso,
        _tagging_cache_lock,
        _write_tagging_cache_unlocked,
        delete_issue_ai_cache,
        drop_tagging_suggestions,
        investigation_path,
        issue_ai_cache_path,
        issue_ai_cache_paths,
        merge_tagging_suggestions,
        now_iso,
        pr_ai_cache_path,
        read_investigation,
        read_issue_ai_cache,
        read_pr_ai_cache,
        read_recommendations_cache,
        read_tagging_cache,
        recommendations_cache_path,
        tagging_cache_path,
        write_investigation,
        write_issue_ai_cache,
        write_pr_ai_cache,
        write_recommendations_cache,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.store_deps import (  # noqa: F401
        _DEP_SOURCES,
        _DEPS_STAMP_FUTURE_SLACK_SEC,
        DEP_SOURCE_INFERRED,
        DEP_SOURCE_NATIVE,
        DEPS_CACHE_SCHEMA,
        DEPS_CACHE_TTL_SEC,
        _coerce_stamp,
        _deps_cache_lock,
        _normalize_deps,
        _read_deps_fetched_at_raw,
        deps_cache_path,
        read_deps_cache,
        write_deps_cache,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.store_issues import (  # noqa: F401
        ISSUES_CACHE_SCHEMA,
        REF_SUMMARY_CACHE_TTL_SEC,
        _list_cache_age_sec,
        _load_list_cache,
        _read_list_snapshot,
        _write_issues_cache_unlocked,
        apply_assignees_change_to_caches,
        apply_label_change_to_caches,
        apply_state_change_to_caches,
        issue_detail_cache_path,
        issue_write_lock,
        issues_cache_lock,
        issues_cache_path,
        read_issue_detail_cache,
        read_issues_cache,
        read_issues_snapshot,
        read_ref_summary_cache,
        ref_summary_cache_path,
        refresh_issues_cache,
        write_issue_detail_cache,
        write_issues_cache,
        write_ref_summary_cache,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.store_labels import (  # noqa: F401
        LABELS_CACHE_TTL_SEC,
        _labels_cache_age_sec,
        _read_labels_fetched_at,
        _write_labels_cache_unlocked,
        add_label_to_cache,
        labels_cache_lock,
        labels_cache_path,
        members_cache_path,
        read_labels_cache,
        read_members_cache,
        refresh_labels_cache,
        write_labels_cache,
        write_members_cache,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.store_pulls import (  # noqa: F401
        PR_DETAIL_CACHE_SCHEMA,
        PR_DETAIL_CACHE_TTL_SEC,
        PULLS_CACHE_SCHEMA,
        _pulls_cache_lock,
        apply_pr_checks_to_list_cache,
        apply_pr_state_change_to_caches,
        drop_pr_detail_cache,
        drop_pulls_cache,
        pr_detail_cache_path,
        pulls_cache_path,
        read_pr_detail_cache,
        read_pulls_cache,
        read_pulls_snapshot,
        write_pr_detail_cache,
        write_pulls_cache,
    )
    from kiro_crew.apps.builtins.issue_radar.backend.store_repos import (  # noqa: F401
        _CASE_INSENSITIVE_NAME_PROVIDERS,
        _PROVIDER_SUBTREE,
        _SETTINGS_LABEL_ROLES,
        APP_NAME,
        CI_WATCH_MAX_RUNS,
        DEFAULT_REPO_SETTINGS,
        GITLAB_GROUPS_KEY,
        SettingsConflict,
        _ci_watch_generations,
        _ci_watch_lock,
        _config_lock,
        _host_slug,
        _name_matches,
        _normalize_settings,
        _same_repo,
        add_connected_repo,
        add_gitlab_group,
        add_setting_label,
        ci_watch_generation,
        ci_watch_state_path,
        config_path,
        data_dir,
        find_connected_repo,
        is_gitlab_group_added,
        is_repo_connected,
        list_connected_repos,
        list_gitlab_groups,
        logger,
        name_compare_key,
        provider_root,
        read_ci_watch_record,
        read_ci_watch_state,
        read_config,
        read_repo_settings,
        read_watch_state,
        remove_connected_repo,
        remove_gitlab_group,
        repo_data_dir,
        reset_ci_watch_state,
        set_repo_permissions,
        watch_state_path,
        write_ci_watch_state,
        write_config,
        write_repo_settings,
        write_watch_state,
    )

if TYPE_CHECKING:  # names it serves from outside the owner family, from their homes
    from kiro_crew import (  # noqa: F401
        platform_compat,
    )
    from kiro_crew.apps.manager import (  # noqa: F401
        app_data_dir,
    )
    from kiro_crew.atomic_write import (  # noqa: F401
        atomic_write,
        read_json_or,
    )
