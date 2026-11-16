"""What the Settings -> Advanced page may show and change, derived from the config schema.

The Advanced page lists every scalar or string-list config key, so a person can
change a setting that has no control of its own without editing ``config.json``.
This module answers, per schema path:

* whether the page shows the key at all (``descriptor`` returns ``None`` for a
  secret, a retired key, a whole section, a map or a list of non-strings);
* whether the page may change it (``mode`` ``"edit"``) or only shows it
  (``"readonly"``), and with which control;
* the PATCH rule for a key that has no curated rule in ``_EDITABLE_CONFIG``
  (``generic_spec``), so ``PATCH /api/config/kirocrew`` keeps validating every
  value it stores.

Booleans, numbers and enums are editable unless :data:`READONLY_PATHS` names
them, a channel tag marks them, or this build locks them. Free text and string
lists are editable when :data:`TEXT_EDIT_PATHS` names them, each with a check
that fits its type (an absolute path, an http(s) URL, a one-line name). A key
that changes who may reach the agent, what it may run, what it may skip asking or
what is sent elsewhere is editable only when :data:`RISK_NOTE_PATHS` names it,
and its row then carries a one-line note on what the change risks; the others in
:data:`READONLY_REASONS` stay read-only.

A generic rule is never added to ``_EDITABLE_CONFIG``, so a change card (which
reads only that table) cannot propose one of these keys.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from kiro_crew.dashboard.handlers.config_all_settings_checks import RISK_CHECKS as _RISK_CHECKS
from kiro_crew.dashboard.handlers.config_all_settings_checks import (
    TEXT_CHECKS as _EXTRA_TEXT_CHECKS,
)
from kiro_crew.dashboard.handlers.config_all_settings_checks import stored_value as _stored_value
from kiro_crew.dashboard.handlers.config_all_settings_checks import (
    text_max_len,
)

#: Why a read-only row cannot be changed here, as the code the page turns into
#: plain words (``settings.advanced.why_<code>``).
WHY_SAFETY = "safety"  # what the agent may run, or skip asking about
WHY_SIGN_IN = "sign_in"  # how sign-ins and credentials are kept
WHY_REACH = "reach"  # who or what can reach this machine, the agent or a session
WHY_SHARE = "share"  # sends activity to another service
WHY_OWNED = "owned"  # another step changes it together with other settings
WHY_MARKER = "marker"  # Kiro Crew sets it itself as a step is finished
WHY_ALIAS = "alias"  # read from another setting, so a write here changes nothing
WHY_TEXT = "text"  # free text such as a path, address or command
WHY_LOCKED = "locked"  # an organization's policy sets it
WHY_COMPOUND = "compound"  # several values that must change together

#: Keys the page shows but never changes, each with the reason it shows. A key
#: with a control of its own elsewhere in Settings is a link row instead
#: (:data:`_OWNING_PAGES`), so it needs no entry here.
READONLY_REASONS: dict[str, str] = {
    # Which memory answers: only ``PUT /api/memory/provider`` switches, so its checks
    # (mode gaps, consent, the Bot-store pin), the ``switched_at`` stamp that keeps
    # open chats on their memory, and the audit record always apply.
    "memory.provider.mode": WHY_OWNED,
    "memory.provider.id": WHY_OWNED,
    "memory.provider.switched_at": WHY_OWNED,
    "memory.provider.stop_learning_ack": WHY_OWNED,
    # What the agent may run.
    "agent.conductor_own_child_verbs": WHY_SAFETY,
    "agent.sandbox_allow_no_isolation": WHY_SAFETY,
    "agent.sandbox_allow_unsandboxed_exec": WHY_SAFETY,
    "agent.jail": WHY_SAFETY,
    "agent.dangerously_skip_permissions": WHY_SAFETY,
    "agent.interactive_command_policy": WHY_SAFETY,
    # On, a deny-ruled shell command can be allowed from a card, and session
    # overrides granted earlier apply again (dashboard/deny_override.py:258-273).
    "agent.deny_override": WHY_SAFETY,
    # Off, a KAS web_fetch.blocked entry it cannot apply refuses no fetch
    # (acp/kas_unread_commands.py:237-249, acp/kas_permissions.py:1904-1905).
    "agent.kas_fail_closed_web_fetch_blocked": WHY_SAFETY,
    # The loader's _clamp_security_bounds raises a non-zero value below
    # SUBAGENT_TIMEOUT_MIN and logs it as possible tampering; the table range alone
    # (0..SUBAGENT_TIMEOUT_MAX) would let this page save one.
    "agent.subagent_timeout_secs": WHY_SAFETY,
    "mcp.honour_auto_approve": WHY_SAFETY,
    "mcp.protect_kiro_permission_rules": WHY_SAFETY,
    "hooks.batch_write_guard": WHY_SAFETY,
    "skills.project_skills_enabled": WHY_SAFETY,
    "skills.generate_scripts": WHY_SAFETY,
    "skills.approval_required": WHY_SAFETY,
    "dashboard.terminal.shell": WHY_SAFETY,
    # Sign-ins, credentials and governance.
    # The browser spec keeps the live view's password cover on, with no control.
    "browser.live.mask_passwords": WHY_SIGN_IN,
    "agent.kas_persist_mcp_sign_in": WHY_SIGN_IN,
    "agent.kas_refuse_plain_http_mcp_headers": WHY_SIGN_IN,
    "agent.mcp_registry_mode": WHY_SIGN_IN,
    "dashboard.sso_login_flags": WHY_SIGN_IN,
    "dashboard.qr_session_until_restart": WHY_SIGN_IN,
    "dashboard.qr_session_persist_across_restart": WHY_SIGN_IN,
    "wakatime.oauth_client_id": WHY_SIGN_IN,
    # Off, a vault read leaves no row in the security event log
    # (secrets/read_audit.py note_reads), so only the operator turns it.
    "dashboard.audit_secret_reads": WHY_SIGN_IN,
    # The decision judge's provider timeout: its owner control lives behind the
    # consent route, not in config.json.
    "decisions.provider.timeout_ms": WHY_OWNED,
    # The memory setup writes it together with the vector width
    # (memory_embeddings.py api_memory_enable_embeddings); a provider alone does not
    # match the stored vectors.
    "memory.embedding_provider": WHY_OWNED,
    # The loader's _clamp_security_bounds lowers it to chat_turn_timeout_secs less
    # APPROVAL_TURN_MARGIN_SECS and logs that as possible tampering, so its limit
    # moves with another key.
    "agent.tool_approval_timeout_secs": WHY_COMPOUND,
    # What one session or Bot may reach, and who can reach this machine.
    "agent.session_sharing": WHY_REACH,
    "agent.session_control": WHY_REACH,
    "agent.session_manager_chain": WHY_REACH,
    "agent.member_dispatch": WHY_REACH,
    "agent.crew_panel": WHY_REACH,
    # Which direct messages share one session: one person's DM could read another's.
    "messaging.dm_scope": WHY_REACH,
    "instances.crew_reads_enabled": WHY_REACH,
    "mcp_gateway.enabled": WHY_REACH,
    "mcp_gateway.forward_declared_env": WHY_REACH,
    "tunnel.enabled": WHY_REACH,
    "tunnel.name_mode": WHY_REACH,
    "tunnel.relay_enabled": WHY_REACH,
    "tunnel.relay_viewer_enabled": WHY_REACH,
    "dashboard.tailscale.bind_refresh_chains": WHY_REACH,
    "dashboard.tailscale.pin_scope": WHY_REACH,
    "dashboard.shared_crew.containment_enabled": WHY_REACH,
    "dashboard.shared_crew.hard_limits": WHY_REACH,
    "dashboard.shared_crew.ssm_port": WHY_REACH,
    # Outbound activity reports.
    "wakatime.enabled": WHY_SHARE,
    "wakatime.send_heartbeats": WHY_SHARE,
    # Where those reports and the WakaTime API key go.
    "wakatime.api_base_url": WHY_SHARE,
    # Lets memory and knowledge text leave this machine for a remote Ollama host.
    "memory.ollama_allow_remote": WHY_SHARE,
    # On, each turn's spans go to the collector the config or the gateway's
    # environment names (tracing/otlp.py:206-237); this route refuses the same
    # for telemetry.enabled (core_config_patch.py:664-690).
    "telemetry.traces_enabled": WHY_SHARE,
    # On, a webhook caller that sends wait gets the agent's answer back
    # (dashboard/handlers/hooks.py _answer_fields): agent output leaves to an
    # outside system.
    "hooks.return_answer": WHY_SHARE,
    # Markers the product sets; flipping one by hand skips a step a person must see.
    "dashboard.privacy_acked": WHY_MARKER,
    "dashboard.privacy_ack_version": WHY_MARKER,
    "dashboard.kiro_telemetry_ack_version": WHY_MARKER,
    "dashboard.onboarded": WHY_MARKER,
    "dashboard.import_onboarded": WHY_MARKER,
    "dashboard.crewmates_onboarded": WHY_MARKER,
    "dashboard.model_picker_configured": WHY_MARKER,
    "memory.migrated": WHY_MARKER,
    "mcp_gateway.apps_enabled": WHY_MARKER,
    # Top-level fields the loader reads, and to_dict saves, under slack.*: a
    # write to the top-level path would change nothing.
    "observe_max_messages": WHY_ALIAS,
    "observe_ttl_hours": WHY_ALIAS,
    "slack_dm_activation": WHY_ALIAS,
    # The old name of mcp_gateway.stub_servers, read only while that key is absent.
    "mcp_gateway.poolable_servers": WHY_ALIAS,
    # Free text that decides what the agent may run, read or write.
    # Directories the OS sandbox keeps read-only, the holes in them, and where a
    # subagent may be started.
    "agent.sandbox_readonly_paths": WHY_SAFETY,
    "agent.sandbox_readonly_exceptions": WHY_SAFETY,
    "agent.subagent_cwd_allowed_roots": WHY_SAFETY,
    # Where MCP server programs are looked up, and the agent specs each session loads.
    "mcp.extra_path_dirs": WHY_SAFETY,
    "mcp_gateway.overlay_dir": WHY_SAFETY,
    # The Strands project's code runs outside the tool gate; the fingerprint is the
    # person's confirmation of the version that may run.
    "agent.strands_project": WHY_SAFETY,
    "agent.strands_project_sha256": WHY_SAFETY,
    # Roots an artifact may be moved into, beyond the home directory.
    "publish.relocate_roots": WHY_SAFETY,
    # Free text that decides who or what can be reached.
    # The logo route serves this file to anyone who can open the sign-in page.
    "dashboard.avatar": WHY_REACH,
    "dashboard.tailscale.allowed_logins": WHY_REACH,
    "knowledge.doc_ingest_hosts": WHY_REACH,
    "dashboard.github_hosts": WHY_REACH,
    "dashboard.gitlab_hosts": WHY_REACH,
    "dashboard.jira_hosts": WHY_REACH,
    "dashboard.bitbucket_server_hosts": WHY_REACH,
    "dashboard.gerrit_hosts": WHY_REACH,
    "mcp_gateway.socket_path": WHY_REACH,
    "tunnel.relay_lease_url": WHY_REACH,
    "tunnel.relay_viewer_cps_url": WHY_REACH,
    "tunnel.relay_viewer_relay_domain": WHY_REACH,
    "tunnel.relay_handle_email_domains": WHY_REACH,
    # Free text that decides how a sign-in or a shared credential is used.
    "tunnel.relay_lease_token_type": WHY_SIGN_IN,
    "tunnel.relay_viewer_oidc_scope_prefix": WHY_SIGN_IN,
    "mcp_gateway.pool_identity_env": WHY_SIGN_IN,
    # Free text that names where activity is sent.
    "telemetry.beacon_endpoint": WHY_SHARE,
    "publish.allowed_destinations": WHY_SHARE,
    # PUT /api/config/default-agent writes it: a template name goes to
    # agent.default_agent, and a name that is no Bot alias is refused, since the
    # loader would replace it with another Bot on the next load.
    "default_agent": WHY_OWNED,
    # POST /api/mcp-gateway/servers/stub writes it after freezing the resolved
    # set, so a server the deprecated alias still stubs is not dropped.
    "mcp_gateway.stub_servers": WHY_OWNED,
    # Written together with apps_trusted on Settings -> Security -> Apps.
    "agent.apps_trusted_local": WHY_OWNED,
    # PUT /api/config/theme writes it, and the browser's theme state reads it.
    "dashboard.theme_color": WHY_OWNED,
    # The memory setup writes it with the model's stamp
    # (memory_embeddings.py api_memory_enable_embeddings).
    "memory.embed_model_id": WHY_OWNED,
    # Kiro Crew records an app here once it installed it by default; removing a
    # name (or Reset) installs that app again on the next start
    # (apps/default_installs.py reconcile_default_installs).
    "agent.apps_default_installs_applied": WHY_MARKER,
    # Managed by the memory setup as a model is applied.
    "memory.embed_rebuild_generation": WHY_MARKER,
    "memory.embed_model_legacy_ids": WHY_MARKER,
}

#: Keys in :data:`READONLY_REASONS` whose reason is safety, sign-in, reach or
#: share, and which the page may change anyway (never the declared skip-all
#: approvals grant, ``agent.dangerously_skip_permissions``: the repository's
#: backend-security-controls rule gives it no dashboard toggle): the owner chose to let a person
#: change them here with no extra step. Each row shows one plain line under its
#: control saying what the change risks (``settings.advanced.risk.*`` in the
#: website catalog, matched to these paths by ``allSettingsRisk.ts``). A key joins
#: only with that line written; ``test_config_all_settings_risk.py`` checks both
#: sides. The write is the same owner-only PATCH as every Settings write.
RISK_NOTE_PATHS: frozenset[str] = frozenset(
    {
        # What the agent may run, read or write.
        "agent.conductor_own_child_verbs",
        "agent.deny_override",
        "agent.interactive_command_policy",
        "agent.jail",
        "agent.kas_fail_closed_web_fetch_blocked",
        "agent.sandbox_allow_no_isolation",
        "agent.sandbox_allow_unsandboxed_exec",
        "agent.sandbox_readonly_exceptions",
        "agent.sandbox_readonly_paths",
        "agent.strands_project",
        "agent.strands_project_sha256",
        "agent.subagent_cwd_allowed_roots",
        "agent.subagent_timeout_secs",
        "hooks.batch_write_guard",
        "mcp.extra_path_dirs",
        "mcp_gateway.overlay_dir",
        "publish.relocate_roots",
        "skills.generate_scripts",
        "skills.project_skills_enabled",
        # How sign-ins and credentials are kept.
        "agent.kas_persist_mcp_sign_in",
        "agent.kas_refuse_plain_http_mcp_headers",
        "agent.mcp_registry_mode",
        "browser.live.mask_passwords",
        "dashboard.audit_secret_reads",
        "dashboard.qr_session_persist_across_restart",
        "dashboard.qr_session_until_restart",
        "dashboard.sso_login_flags",
        "mcp_gateway.pool_identity_env",
        "tunnel.relay_lease_token_type",
        "tunnel.relay_viewer_oidc_scope_prefix",
        "wakatime.oauth_client_id",
        # Who or what can reach this machine, the agent or a session.
        "agent.crew_panel",
        "agent.member_dispatch",
        "agent.session_control",
        "agent.session_manager_chain",
        "agent.session_sharing",
        "dashboard.avatar",
        "dashboard.bitbucket_server_hosts",
        "dashboard.gerrit_hosts",
        "dashboard.github_hosts",
        "dashboard.gitlab_hosts",
        "dashboard.jira_hosts",
        "dashboard.shared_crew.containment_enabled",
        "dashboard.shared_crew.hard_limits",
        "dashboard.shared_crew.ssm_port",
        "dashboard.tailscale.allowed_logins",
        "dashboard.tailscale.bind_refresh_chains",
        "dashboard.tailscale.pin_scope",
        "instances.crew_reads_enabled",
        "knowledge.doc_ingest_hosts",
        "mcp_gateway.enabled",
        "mcp_gateway.forward_declared_env",
        "mcp_gateway.socket_path",
        "messaging.dm_scope",
        "tunnel.enabled",
        "tunnel.name_mode",
        "tunnel.relay_enabled",
        "tunnel.relay_handle_email_domains",
        "tunnel.relay_lease_url",
        "tunnel.relay_viewer_cps_url",
        "tunnel.relay_viewer_enabled",
        "tunnel.relay_viewer_relay_domain",
        # What is sent to another service.
        "hooks.return_answer",
        "memory.ollama_allow_remote",
        "publish.allowed_destinations",
        "telemetry.beacon_endpoint",
        "telemetry.traces_enabled",
        "wakatime.api_base_url",
        "wakatime.enabled",
        "wakatime.send_heartbeats",
    }
)

#: Every key the page never changes by name: the reasons table's keys, less the
#: ones :data:`RISK_NOTE_PATHS` makes editable.
READONLY_PATHS: frozenset[str] = frozenset(READONLY_REASONS) - RISK_NOTE_PATHS

#: Keys whose control lives on a Settings page that the settings registry does
#: not tie to the key (no ``configKey`` on the control): the row links to that
#: page instead of drawing a second control. ``highlight`` names the registry id
#: of the exact control where one exists. Each row was checked against the
#: page's own writer.
_OWNING_PAGES: dict[str, dict[str, str]] = {
    # Settings -> Agent Harness: the harness, the warm pool and subagent budgets.
    "agent.acp_backend": {"tab": "agent"},
    "session.pool_size": {"tab": "agent"},
    "session.pool_agent": {"tab": "agent"},
    "session.pool_ttl_secs": {"tab": "agent"},
    "agent.subagent_max_turns": {"tab": "agent"},
    "agent.max_subagents": {"tab": "agent"},
    "agent.subagent_auto_max": {"tab": "agent"},
    # The Strands setup card, shown when Strands is the harness.
    "agent.strands_provider": {"tab": "agent"},
    "agent.strands_model": {"tab": "agent"},
    # Settings -> Developer -> Configuration.
    "agent.approval_mode": {"tab": "developer", "sub": "config"},
    "agent.sandbox": {"tab": "developer", "sub": "config"},
    "agent.default_agent": {"tab": "developer", "sub": "config"},
    "agent.tool_search": {"tab": "developer", "sub": "config"},
    "session.timeout_secs": {"tab": "developer", "sub": "config"},
    # Settings -> Chat.
    "agent.model": {"tab": "chat"},
    "agent.reasoning_effort": {"tab": "chat"},
    "agent.completion_keep": {"tab": "chat"},
    "agent.completion_keep_chars": {"tab": "chat"},
    "agent.soft_stop_budget_secs": {"tab": "chat"},
    "session_summary.enabled": {"tab": "chat"},
    "dashboard.user_role": {"tab": "chat"},
    "dashboard.user_role_other": {"tab": "chat"},
    "dashboard.user_technical_level": {"tab": "chat"},
    "dashboard.folder_sort": {"tab": "chat"},
    # Settings -> Voice.
    "stt.enabled": {"tab": "voice", "highlight": "voice.enabled"},
    "stt.dictation_panel": {"tab": "voice", "highlight": "voice.dictation-panel"},
    "stt.transcribe_region": {"tab": "voice", "highlight": "voice.aws-region-transcribe"},
    "stt.transcribe_profile": {"tab": "voice", "highlight": "voice.aws-profile-transcribe"},
    # Settings -> Display, Computer Use, Remote Crew, Browser.
    "dashboard.recent_tint_count": {"tab": "display"},
    "computer_use.max_tree_nodes": {
        "tab": "computer-use",
        "highlight": "computer-use.max-tree-nodes",
    },
    "computer_use.attach_screenshot": {
        "tab": "computer-use",
        "highlight": "computer-use.attach-screenshots",
    },
    "computer_use.screenshot_max_px": {
        "tab": "computer-use",
        "highlight": "computer-use.screenshot-width",
    },
    "computer_use.cursor_motion": {
        "tab": "computer-use",
        "highlight": "computer-use.show-cursor-motion",
    },
    "instances.warm_set_cap": {"tab": "instances"},
    "instances.default_destination": {"tab": "instances"},
    "browser.credentials.enabled": {"tab": "browser"},
    "browser.credentials.domains": {"tab": "browser"},
    "dashboard.browser_local_origins": {"tab": "browser"},
    # Settings -> Security.
    "agent.apps_allow_third_party": {
        "tab": "security",
        "sub": "apps",
        "highlight": "security.trust-every-third-party-app",
    },
    "agent.apps_trusted": {"tab": "security", "sub": "apps"},
    "dashboard.tailscale.enabled": {"tab": "security", "sub": "tailnet"},
    "dashboard.tailscale.trust_identity": {
        "tab": "security",
        "sub": "tailnet",
        "highlight": "security.trust-this-machine-s-tailnet-name",
    },
    # Settings -> People -> Advanced.
    "dashboard.shared_crew.admission_enabled": {"tab": "people"},
    "dashboard.shared_crew.tailnet_direct": {"tab": "people"},
    "dashboard.shared_crew.ssm": {"tab": "people"},
    "dashboard.shared_crew.principal_concurrency": {"tab": "people"},
    "dashboard.shared_crew.crew_concurrency": {"tab": "people"},
    "dashboard.shared_crew.typing_idle_seconds": {"tab": "people"},
    # Settings -> Developer -> Memory: the embedding-model card changes the model
    # and its width together and re-embeds; a width alone breaks the loaded model.
    "memory.embedding_dim": {"tab": "developer", "sub": "memory"},
    "memory.embed_model_path": {"tab": "developer", "sub": "memory"},
    # Settings -> Overview -> Memory: PUT /api/memory/settings keeps at least 7 days
    # and 0.5 hours. A generic 0 days would prune every past day's history.
    "memory.history_max_days": {"tab": "overview"},
    "memory.history_idle_hours": {"tab": "overview"},
    # Settings -> Overview -> Memory: the repeated-failures card's toggle.
    "memory.repeated_failures_notice": {"tab": "overview"},
    # Settings -> Developer -> Logs: the level picker (POST /api/logs/level).
    "agent.log_level": {"tab": "developer", "sub": "logs"},
    # Settings -> Developer -> Feature Previews.
    "agent.kas_bundle": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.run-kas-on-kiro-crew-s-own-engine",
    },
    "dashboard.crewmate_threads": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.reply-threads-on-bot-chat-messages",
    },
    "decisions.bucket": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.decisions",
    },
    "decisions.nudge_wake.provider": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.decisions",
    },
    "decisions.nudge_wake.llm_model": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.decisions",
    },
    "decisions.history_budget_chars": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.decisions",
    },
    "decisions.provider.endpoint": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.decisions",
    },
    # PUT /api/decisions/provider writes it with the endpoint, under its lock and
    # consent carry; a model saved alone would split that pair.
    "decisions.provider.model": {
        "tab": "developer",
        "sub": "feature-previews",
        "highlight": "developer.decisions",
    },
}

#: Channels with a page of their own under Settings -> Channels, by schema tag.
_CHANNEL_PAGES = frozenset(
    {
        "slack",
        "telegram",
        "discord",
        "wecom",
        "weixin",
        "whatsapp",
        "feishu",
        "webex",
        "teams",
        "imessage",
    }
)


@functools.cache
def _registry_setting_ids() -> dict[str, str]:
    """Config path -> registry id, for every Settings control that names its key.

    Read from the bundled ``settings-registry.generated.json`` (the same file the
    agent's ``find_setting`` reads), so the page and the agent agree on which keys
    already have a control. A missing or unreadable file leaves the map empty:
    the page then lists those keys by the other rules.
    """
    import json
    from pathlib import Path

    import kiro_crew

    path = Path(kiro_crew.__file__).parent / "docs" / "settings-registry.generated.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("settings", [])
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for row in rows:
        key = row.get("configKey") if isinstance(row, dict) else None
        if isinstance(key, str) and isinstance(row.get("id"), str):
            out.setdefault(key, row["id"])
    return out


def owning_page(entry: Any) -> dict[str, str] | None:
    """The Settings page that holds *entry*'s own control, or ``None``."""
    page = _OWNING_PAGES.get(entry.path)
    if page is not None:
        return page
    if entry.path.startswith(_READONLY_PREFIXES):
        return {"tab": "developer", "sub": "feature-previews"}
    # A channel's page writes its whole section, tagged field or not.
    section = entry.path.split(".", 1)[0]
    return (
        {"tab": "channels", "sub": section}
        if section in _CHANNEL_PAGES and "." in entry.path
        else None
    )


#: Free-text and string-list keys the page may change. Each runs its reader's own
#: check, or the one ``config_all_settings_checks.TEXT_CHECKS`` names for it: a
#: path must be absolute after ``~`` is expanded, a URL must carry its reader's
#: scheme, a name must be one line. A key whose text decides what the agent may
#: run or who can reach it is in :data:`READONLY_REASONS` instead.
TEXT_EDIT_PATHS: frozenset[str] = frozenset(
    {
        "mcp_gateway.keep_alive_servers",
        "knowledge.auto_ingest_artifact_kinds",
        "timezone",
        # What the Company SSO sign-in form starts with (auth/company_sign_in.py).
        "agent.kas_sso_start_url",
        "agent.kas_sso_region",
        "memory.extraction_instructions",
        *_EXTRA_TEXT_CHECKS,
    }
)

#: Bounds for a generic rule. A key the loader bounds for security takes the
#: loader's own range (:func:`_security_bounds`). shortcut: any other key gets
#: these wide bounds, and its loader may clamp it to a narrower range at read time,
#: so a save outside that range reads back clamped; give such a key its loader
#: range here once one turns up in a report.
_INT_LIMIT = 10**12
#: Integer keys whose loader takes a negative value as a mode, though the
#: default is not negative: each floor matches the loader (`sections_session`
#: `_archive_retention_days`, `sections_limits` `xdist_auto_cap` `lo=-1`).
_SENTINEL_FLOORS = {
    "session.archive_retention_days": -1,
    "resource_limits.xdist_auto_cap": -1,
}
_STR_MAX_LEN = 256
_LIST_MAX_ITEMS = 64
_LIST_MAX_ITEM_LEN = 256

_SCALAR_TYPES = ("boolean", "integer", "number", "string")


@functools.cache
def _entries() -> dict[str, Any]:
    from kiro_crew.config.schema import SCHEMA_REGISTRY

    return {e.path: e for e in SCHEMA_REGISTRY}


@functools.cache
def _security_bounds() -> dict[str, tuple[int, int]]:
    """Dotted path -> (min, max) for each integer the loader clamps as a security bound."""
    from kiro_crew.config.loader_builders import _SECURITY_BOUNDED_FIELDS

    return {f"{section}.{key}": (lo, hi) for section, key, lo, hi in _SECURITY_BOUNDED_FIELDS}


def _is_string_list(entry: Any) -> bool:
    item = _entries().get(entry.path + ".*")
    return entry.type == "array" and item is not None and item.type == "string"


def _shown(entry: Any) -> bool:
    if "*" in entry.path or entry.sensitive or entry.deprecated:
        return False
    if entry.type in _SCALAR_TYPES and not entry.has_children:
        return True
    return _is_string_list(entry)


def _control_from_schema(entry: Any) -> dict[str, Any] | None:
    """The control and PATCH rule the schema alone supports, or ``None``."""
    if entry.other_types:
        return None
    if entry.enum_values:
        return {"type": "enum", "values": list(entry.enum_values)}
    if entry.type == "boolean":
        return {"type": "bool"}
    default = entry.default_value
    if entry.type == "integer":
        bounded = _security_bounds().get(entry.path)
        if bounded is not None:
            # The loader clamps these on every read and records a clamp as possible
            # tampering, so this route takes only the loader's own range.
            return {"type": "int", "min": bounded[0], "max": bounded[1]}
        low = default if isinstance(default, int) and default < 0 else 0
        low = min(low, _SENTINEL_FLOORS.get(entry.path, 0))
        return {"type": "int", "min": low, "max": _INT_LIMIT}
    if entry.type == "number":
        low_f = float(default) if isinstance(default, (int, float)) and default < 0 else 0.0
        return {"type": "float", "min": low_f, "max": float(_INT_LIMIT)}
    if entry.path not in TEXT_EDIT_PATHS and entry.path not in RISK_NOTE_PATHS:
        return None
    if entry.type == "string":
        return {"type": "str", "max_len": text_max_len().get(entry.path, _STR_MAX_LEN)}
    if _is_string_list(entry):
        return {
            "type": "str_list",
            "max_items": _LIST_MAX_ITEMS,
            "max_item_len": _LIST_MAX_ITEM_LEN,
        }
    return None


@functools.cache
def _dashboard_setting_ids() -> dict[str, str]:
    """Config path -> Settings registry id, for the controls that save through
    ``PUT /api/dashboard/config`` rather than this route."""
    from kiro_crew.change_card_catalog import DASHBOARD_LIST_SETTINGS, DASHBOARD_SETTINGS

    out = {f"dashboard.{key}": sid for sid, (key, _values) in DASHBOARD_SETTINGS.items()}
    out.update({f"dashboard.{spec[0]}": sid for sid, spec in DASHBOARD_LIST_SETTINGS.items()})
    out.update(_THEME_ROUTE_SETTING_IDS)
    return out


#: Keys ``PUT /api/config/theme`` writes, with the Display control that saves each.
#: The browser holds these in its theme state, which only that route's save
#: refreshes, so a write here would show a choice the page is not drawing.
_THEME_ROUTE_SETTING_IDS = {
    "dashboard.theme_mode": "display.mode",
    "dashboard.translucent_panels": "display.translucent-panels",
    "dashboard.language": "display.language",
}


#: Prefixes of keys another control owns and mirrors elsewhere (a preview flag is
#: also cached in the browser), so a write here would leave that copy stale.
_READONLY_PREFIXES = ("dashboard.feature_previews.",)


def _own_control(entry: Any) -> dict[str, Any] | None:
    """The link record for a key with its own Settings control, or ``None``."""
    setting_id = _dashboard_setting_ids().get(entry.path) or _registry_setting_ids().get(entry.path)
    if setting_id is not None:
        return {"mode": "control", "settingId": setting_id}
    page = owning_page(entry)
    if page is not None:
        return {"mode": "control", "page": dict(page)}
    return None


def _blocked(entry: Any) -> bool:
    """A key the page never changes, whatever rule could write it."""
    # Through the facade: only the core_* family imports its owners.
    from kiro_crew.dashboard.handlers.core import _MOVED_CONFIG_FIELDS
    from kiro_crew.edition_locks import config_path_locked

    # A channel's settings carry its tag; its own page runs the sign-in and
    # allow-list checks that decide who may message the agent.
    # A key with its own control elsewhere is saved there, never by a generic rule:
    # the same check `descriptor` uses to draw the link row.
    return (
        entry.path in READONLY_PATHS
        or _own_control(entry) is not None
        or entry.path.startswith(_READONLY_PREFIXES)
        or (bool(entry.tags) and entry.path not in RISK_NOTE_PATHS)
        or entry.path in _MOVED_CONFIG_FIELDS
        or config_path_locked(entry.path)
    )


def _readonly_reason(entry: Any) -> str:
    """Why the page only shows *entry*: a ``WHY_*`` code."""
    from kiro_crew.edition_locks import config_path_locked

    if config_path_locked(entry.path):
        return WHY_LOCKED
    if entry.path in READONLY_REASONS:
        return READONLY_REASONS[entry.path]
    if "wakatime" in entry.tags:
        return WHY_SHARE
    return WHY_TEXT if entry.type in ("string", "array") else WHY_COMPOUND


def generic_spec(path: str) -> dict[str, Any] | None:
    """The schema-derived PATCH rule for *path*, or ``None`` when it has none.

    Only for a key with no curated rule in ``_EDITABLE_CONFIG``: the caller asks
    that table first. The rule carries ``"generic": True`` so the handler applies
    the strict type check and allows ``unset``.
    """
    entry = _entries().get(path)
    if entry is None or not _shown(entry) or _blocked(entry):
        return None
    control = _control_from_schema(entry)
    if control is None:
        return None
    return {**control, "generic": True}


#: Curated rule types the page can draw a control for.
_CURATED_CONTROLS = ("bool", "enum", "int", "float", "str", "str_list")


def descriptor(entry: Any) -> dict[str, Any] | None:
    """The ``allSettings`` record for *entry* in ``GET /api/config/schema``.

    ``None`` when the page does not list the key. Otherwise ``mode`` is
    ``"edit"``, ``"readonly"``, or ``"control"`` with the ``settingId`` of the
    Settings control that owns it; an editable key also names its ``control`` and,
    where the rule has them, its ``values`` / ``min`` / ``max``. ``resettable`` is
    true when ``unset`` may remove the stored value.
    """
    if not _shown(entry):
        return None
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    curated = _EDITABLE_CONFIG.get(entry.path)
    own = _own_control(entry)
    if own is not None:
        return own
    if _blocked(entry):
        return {"mode": "readonly", "reason": _readonly_reason(entry)}
    if curated is not None:
        if curated["type"] not in _CURATED_CONTROLS or "values_fn" in curated:
            return {"mode": "readonly", "reason": WHY_COMPOUND}
        spec: dict[str, Any] = curated
        resettable = False
    else:
        generic = generic_spec(entry.path)
        if generic is None:
            return {"mode": "readonly", "reason": _readonly_reason(entry)}
        spec, resettable = generic, True
    out: dict[str, Any] = {"mode": "edit", "control": spec["type"], "resettable": resettable}
    for key in ("values", "min", "max", "max_len", "max_items", "max_item_len"):
        if key in spec:
            out[key] = spec[key]
    if entry.path in RISK_NOTE_PATHS:
        # The page draws the key's own risk line under the control.
        out["risk"] = _readonly_reason(entry)
    return out


def _timezone_error(value: str) -> str | None:
    """Refuse a zone the cron scheduler cannot load; ``""`` clears it to the host's."""
    from kiro_crew.cron_service.schedule import is_valid_timezone

    if value == "" or is_valid_timezone(value):
        return None
    return "must be an IANA timezone name such as America/Los_Angeles, or local"


def _sso_start_url_error(value: str) -> str | None:
    """Refuse a portal URL sign-in would refuse; ``""`` clears it to the edition's."""
    from kiro_crew.auth.service import InvalidStartUrlError, _canonical_start_url

    try:
        _canonical_start_url(value)
    except InvalidStartUrlError as exc:
        return str(exc)
    return None


def _sso_region_error(value: str) -> str | None:
    """Refuse a region outside the AWS region-name grammar; ``""`` clears it."""
    from kiro_crew.auth.service import _REGION_RE

    if value == "" or _REGION_RE.match(value):
        return None
    return "must be an AWS region name such as us-east-1"


#: Free-text keys whose value must also mean something to its reader.
_TEXT_CHECKS: dict[str, Callable[[Any], str | None]] = {
    "timezone": _timezone_error,
    "agent.kas_sso_start_url": _sso_start_url_error,
    "agent.kas_sso_region": _sso_region_error,
    **_EXTRA_TEXT_CHECKS,
    **_RISK_CHECKS,
}


def generic_value_error(path: str, value: object) -> str | None:
    """Why *value* cannot be stored at *path* under its generic rule, or ``None``.

    The type check is ``kiro_crew config set``'s own (``cli_config._declared_type_error``):
    the curated branches of the PATCH route coerce (``int("3")``, ``float(True)``)
    for their older callers, and a generic key gets exactly the type its schema
    declares. A free-text or string-list key named in ``_TEXT_CHECKS`` is checked
    by its reader's own validator too.
    """
    from kiro_crew.cli_config import _declared_type_error

    entry = _entries().get(path)
    if entry is None:
        return f"unknown config key {path}"
    reason = _declared_type_error(entry, value)
    if reason:
        return reason
    if isinstance(value, list) and any(not isinstance(item, str) for item in value):
        # The schema check reads only the list itself; a check below reads items as text.
        return "each item must be text"
    check = _TEXT_CHECKS.get(path)
    return check(value) if check and value is not None else None


def generic_stored_value(path: str, value: object) -> object:
    """The value to save at *path* once :func:`generic_value_error` passed it.

    A path is saved with its ``~`` expanded, because not every reader expands it.
    """
    check = _TEXT_CHECKS.get(path)
    return _stored_value(check, value) if check else value


def attach_descriptor(serialized: dict[str, Any], entry: Any) -> dict[str, Any]:
    """Add *entry*'s ``allSettings`` record to its schema-route dict, in place; return it."""
    record = descriptor(entry)
    if record is not None:
        serialized["allSettings"] = record
    return serialized
