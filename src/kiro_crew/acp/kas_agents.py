"""Project a Crew agent spec onto KAS's ``ClientCustomAgent`` shape.

kiro-cli reads agent definitions from ``~/.kiro/agents/*.json`` and selects one
with a ``--agent`` flag. KAS has no such flag: it advertises only its own
built-in modes and takes client agents over the wire, as
``_meta.kiro.customAgents`` on ``session/new``. Each injected agent is
registered and then surfaces as a switchable mode, which is what lets the
ordinary ``session/set_mode`` activation work afterwards.

Two properties of KAS's schema drive the mapping and are easy to get wrong:

* ``prompt`` must be resolved content. A ``file://`` URI is the client's job to
  read, so a spec that points at a prompt file has to be inlined here.
* ``tools`` absent means NO tool access, not "all tools" — KAS resolves it as
  ``agent.tools ?? []``. The list is therefore always emitted explicitly, and an
  ambiguous spec fails closed rather than guessing ``*``.

``mcpServers`` IS projected, minus the names that arrive as session-level broker
stubs. ``@server`` entries in ``tools`` do resolve wherever the server was
declared, so carrying the servers twice would risk a double registration — but
that only arises for a STUBBED server, and stubs are opt-in per server
(``mcp_gateway.stub_servers``, empty by default). With nothing stubbed the
session-level param is an empty array, so omitting the block leaves a KAS session
holding ``tools: ["@kirocrew-core", ...]`` and no definition of what
``kirocrew-core`` is — refs naming nothing, and every Crew tool silently absent.
kiro-cli does not have this problem: it reads the spec off disk itself via
``--agent``.

Filtering by the stub set keeps the no-double-registration guarantee (a stubbed
server is still declared exactly once, by the injection that outranks this block)
while never leaving the session with nothing. Two fields are dropped on the way
through, and a muted or registry-governed entry is not declared at all — see
:func:`_project_mcp_servers`. The runtime then carries the ACTIVE
agent's projected managed entries in the session-level array itself
(:func:`hoist_servers_for_session`), the declaration site the captured 2.18.0 release
honours over a same-named global or workspace server and every probed release
reports as the session's own.

``model`` is deliberately NOT projected: the model is set through its own
protocol verb, so it has exactly one owner rather than being pinned in two places
that can disagree.

``welcomeMessage`` IS projected, through the same
:func:`kiro_crew.agent_discovery.spec_welcome_message` reading the dashboard
renders. One reader, so the hint a KAS session registers and the hint Crew shows
cannot disagree -- and the cap and whitespace rules that reading already applies
to this user-writable field are not re-argued for a second path onto the wire.

``permissions`` IS projected, and is the one field that changes behaviour rather
than just describing it. KAS's policy is keyed by its own capability vocabulary
instead of by tool name, so it is not a rename of Crew's ``allowedTools`` — see
:mod:`kiro_crew.acp.kas_permissions` for the mapping and for why an entry it
cannot classify is left to prompt. Omitting the field is not the neutral choice
it looks like: with no policy, KAS resolves every request to ``ask``, so a spec
that auto-approves a dozen tools on kiro-cli would prompt for all of them here.
The derived rules come from ``allowedTools`` and nothing else, and a
``permissions`` block the spec's author wrote is then intersected with the same
ceiling and appended -- see :func:`kiro_crew.acp.kas_permissions.merge_user_permissions`
for the four rules. Dropping the author's block entirely would leave a pure-KAS
agent -- ``permissions`` authored, no ``allowedTools`` -- reaching the backend with
the field absent, which is the all-``ask`` case above.

The ``mcpServers`` projection lives in :mod:`kiro_crew.acp.kas_agents_mcp_servers`,
the session-level hoist in :mod:`kiro_crew.acp.kas_agents_session_hoist` and the
registered-entry reads in :mod:`kiro_crew.acp.kas_agents_registered`. This module
imports every name they define and stays their import path and patch surface, as a
:mod:`kiro_crew._facade_mirror` facade.
"""

from __future__ import annotations

import importlib as _importlib
import json
import logging
import secrets
import sys as _sys
from collections.abc import Callable, Mapping  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew import mcp_cleanup  # noqa: F401
from kiro_crew import _facade_mirror
from kiro_crew.acp import kas_engine_features as engine  # noqa: F401
from kiro_crew.acp import kas_env_launch, session_label  # noqa: F401
from kiro_crew.acp.kas_agents_mcp_servers import (  # noqa: F401 - re-exported
    MANAGED_MCP_SERVER_NAMES,
    NOT_RUN_INSECURE_URL,
    NOT_RUN_REGISTRY,
    NOT_RUN_REGISTRY_IF_GOVERNED,
    NOT_RUN_SCOPE_MUTED,
    WithheldOut,
    _project_mcp_servers,
)
from kiro_crew.acp.kas_agents_registered import (  # noqa: F401 - re-exported
    _registered_entry,
    _switched_entry,
    active_registered_entry,
    projected_auto_approved,
    projected_crew_answered,
    projected_crew_answered_commands,
    projected_shell_allowlist,
    projected_shell_readonly,
    switched_auto_approved,
    switched_crew_answered,
    switched_crew_answered_commands,
    switched_shell_allowlist,
    switched_shell_readonly,
)
from kiro_crew.acp.kas_agents_session_hoist import (  # noqa: F401 - re-exported
    hoist_servers_for_session,
)
from kiro_crew.acp.kas_app_identity import (  # noqa: F401
    TOKENED_ENV_KEYS_KEPT as _TOKENED_ENV_KEYS_KEPT,
)
from kiro_crew.acp.kas_app_identity import (  # noqa: F401
    gateway_bound_port,
    tokened_app_element,
    without_noop_restrictions,
)
from kiro_crew.acp.kas_auto_approve import auto_approve_refs
from kiro_crew.acp.kas_env_literals import (  # noqa: F401 - re-exported
    _loggable_key_names,
    _relayable_literal,
)
from kiro_crew.acp.kas_env_withhold import (  # noqa: F401 - re-exported
    _ENV_REFERENCE,
    _MANAGED_ENV_KEYS_KEPT,
    _is_relayed_reference,
    _withhold_credential_fields,
)
from kiro_crew.acp.kas_grant_only_settings import (
    without_grant_only_settings,
    without_read_grant_keys,
    without_shell_grant_keys,
    without_write_grant_keys,
)
from kiro_crew.acp.kas_legacy_negotiation import _pin_legacy_negotiation  # noqa: F401
from kiro_crew.acp.kas_managed_entries import (  # noqa: F401 - re-exported for the KAS harness
    resolve_managed_entries,
)
from kiro_crew.acp.kas_permissions import (
    CREW_ANSWERED_BUILTINS_KEY,
    CREW_ANSWERED_COMMANDS_KEY,
    CREW_ANSWERED_HOOK_HELD_KEY,
    CREW_ANSWERED_PATHS_KEY,
    CREW_ANSWERED_READ_PATHS_KEY,
    CREW_ANSWERED_WRITES_KEY,
    CREW_SHELL_ALLOWLIST_KEY,
    CREW_SHELL_READONLY_KEY,
    CREW_SPEC_DENIES_KEY,
    CREW_WRITE_ALLOWLIST_KEY,
    allowed_tools_to_permissions,
    canonical_tools_settings,
    crew_answered_builtins,
    crew_answered_commands,
    crew_answered_paths,
    crew_answered_read_paths,
    hook_held_grants,
    merge_user_permissions,
    projection_accepts,
    projection_dropped_rules,
    shell_denies_carried,
    spec_shell_allowlist,
    spec_write_allowlist,
    tools_settings_to_permissions,
    trusted_agents_withheld,
    withhold_answered_under_authored_rules,
    withhold_hook_gated_auto_approval,
)
from kiro_crew.acp.kas_projection_reads import (  # noqa: F401
    _gated_off_managed_servers,
    _registry_governed,
    _relayed_env_references,
)
from kiro_crew.acp.kas_prompt import (
    _KAS_FALLBACK_PROMPT,
    _KAS_FALLBACK_PROMPT_AGENTS,
    _PROMPT_FILE_SCHEME,
    prompt_file_path,
    spec_prompt,
)
from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path as _is_unsafe_prompt_path
from kiro_crew.acp.kas_settings_mcp import include_mcp_json
from kiro_crew.acp.kas_skill_scope import _skill_scope_summary
from kiro_crew.acp.kas_spec_carry import projected_resources, user_tool_aliases, with_use_aws_denies
from kiro_crew.acp.kas_spec_denies import commands_lifted_by_denies as _commands_lifted_by_denies
from kiro_crew.acp.kas_spec_denies import (
    denied_command_items,
    spec_denies_record,
    uncarried_denied_commands,
    web_fetch_carried,
    web_fetch_gate,
    web_fetch_regranted,
    with_blocked_host_asks,
)
from kiro_crew.acp.kas_spec_grants import SHELL_SETTINGS_KEYS, shell_auto_allows_readonly
from kiro_crew.acp.kas_tool_limits import (  # noqa: F401
    _KAS_CARRIED_ELSEWHERE,
    _KAS_DISCARDED_ENTRY_KEYS,
)
from kiro_crew.acp.kas_tool_limits import disabled_tool_exclusions as _disabled_tool_exclusions
from kiro_crew.acp.kas_unread_commands import name_unreadable_entries, trusted_over_limit_items
from kiro_crew.agent_discovery import (
    AgentsDirMemo,
    AmbiguousAgentSpecError,
    plain_markdown_document,
    read_agent_spec_strict,
    spec_by_declared_name,
    spec_welcome_message,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS  # noqa: F401
from kiro_crew.agent_sdk.spec_hooks import spec_script_hooks, user_hook_files
from kiro_crew.agent_sdk.tools_settings_reach import GATE_READ_MARK, tools_settings_unapplied
from kiro_crew.agent_spec_format import agent_spec_candidates, is_markdown_spec
from kiro_crew.agent_spec_pins import pinned_memo_key
from kiro_crew.file_uri import file_uri_path_text
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, get_global_hook_store, persisted_hook_store
from kiro_crew.mcp_cleanup import (  # noqa: F401
    KIROCREW_BIN_MCP_SERVERS,
    MCP_REGISTRY_TYPE,
    mcp_entry_is_registry_governed,
    mcp_server_muted_in_any_store,
)
from kiro_crew.mcp_oauth_projection import (  # noqa: F401
    oauth_lost_keys,
    registry_withheld,
    servers_with_discarded_oauth,
)
from kiro_crew.platform.governance import (
    may_grant_builtin_at_gate_now,
    may_skip_gate_for_session,
    may_skip_gate_now,
)
from kiro_crew.platform.tool_names import (
    canonical_kiro_cli_tool_name,
    expand_kiro_cli_allowed_tools,
    expand_kiro_cli_tool_exclusions,
    expand_kiro_cli_tool_names,
)
from kiro_crew.platform.tools_settings_aliases import as_authored
from kiro_crew.sel import sel
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)

#: Cap KAS enforces on ``_meta.kiro.customAgents`` (``z.array(...).max(50)``).
KAS_MAX_CUSTOM_AGENTS = 50

#: Spec keys with no slot in KAS's ``ClientCustomAgent`` wire schema.
#:
#: "No slot on the wire" is NOT "no such capability in KAS" — conflating the two
#: is what kept ``hooks`` written off as unsupported. KAS runs pre/post-tool-use
#: hooks natively and loads them from an agent profile ON DISK (it even accepts
#: Crew's object form), so what is lost here is a delivery path, not a feature:
#: an agent injected over the wire cannot carry them. Crew's turn loop fires the
#: spec's ``hooks`` for such a session instead (:mod:`kiro_crew.agent_sdk.spec_hooks`), so of
#: these keys only :data:`SPEC_KEYS_WITHOUT_CARRIER` is actually lost.
#:
#: ``allowedTools`` is deliberately NOT in this set. It has no slot either, but
#: :mod:`kiro_crew.acp.kas_permissions` translates it into ``permissions``, so
#: the capability survives under another name.
UNSUPPORTED_SPEC_KEYS = frozenset(
    {
        "hooks",
        "slashCommand",
        "toolsSettings",
    }
)


#: The keys in :data:`UNSUPPORTED_SPEC_KEYS` that nothing carries to a KAS session,
#: so an agent that sets one runs without it. The user is told once per session.
#: ``toolsSettings`` is not here: it is translated into ``permissions``
#: (:func:`kiro_crew.acp.kas_permissions.tools_settings_to_permissions`), and only
#: the entries that translation cannot carry are reported.
SPEC_KEYS_WITHOUT_CARRIER = UNSUPPORTED_SPEC_KEYS - {"hooks", "toolsSettings"}


def spec_keys_without_carrier(
    spec: dict[str, Any],
    *,
    translates_tools_settings: bool = False,
    discards_mcp_oauth: bool = False,
    withholds_oauth_secret: bool = False,
    session_key: str = "",
    governance_app: str | None = None,
) -> list[str]:
    """What *spec* sets that the session runs without, sorted.

    Whole keys from :data:`SPEC_KEYS_WITHOUT_CARRIER`, a user's ``toolAliases``
    and each dropped knowledge base (:mod:`kiro_crew.acp.kas_spec_carry`). ``toolsSettings`` is named
    as :func:`~kiro_crew.agent_sdk.tools_settings_reach.tools_settings_unapplied`
    names it (whole, or each entry beside a ``subagent.availableAgents`` the spawn
    gate applies), unless *translates_tools_settings* says this session receives its
    ``permissions`` translation (KAS only); then each ``toolsSettings.<tool>.<key>``
    the translation cannot carry is named instead, each ``allowedCommands`` or
    ``allowedPaths`` entry Kiro Crew cannot read on its own (``kas_unread_commands.name_unreadable_entries``),
    ``permissions`` when the merge refuses it, and ``permissions.rules[<i>]`` for each
    ``allow`` it leaves out alone. A ``trustedAgents`` allow is judged for
    *session_key* and *governance_app* as projected.

    *discards_mcp_oauth* says this session's backend drops a remote MCP server's
    ``oauth`` block (KAS only); each declared server that sets one is then named
    as ``mcpServers.<name>.oauth``. *withholds_oauth_secret* says the session is
    on KAS, whose projection sends nothing from a block that names a
    ``clientSecret`` or keeps no key it can send
    (``mcp_oauth_projection.oauth_withheld``); each declared server with such a
    block is then named the same way, whatever the engine keeps.
    """
    lost = [k for k in SPEC_KEYS_WITHOUT_CARRIER if spec.get(k)]
    lost.extend(projected_resources(spec.get("resources"))[1])
    if user_tool_aliases(spec.get("toolAliases")):
        lost.append("toolAliases")
    if discards_mcp_oauth or withholds_oauth_secret:
        servers = servers_with_discarded_oauth(
            spec, _registry_governed, withheld_only=not discards_mcp_oauth
        )
        lost.extend(oauth_lost_keys(servers))
    raw = spec.get("toolsSettings")
    if not translates_tools_settings:
        lost.extend(tools_settings_unapplied(raw))
        return sorted(lost)
    # kiro-cli reads one spelling per alias group; each name is the one the spec wrote.
    tools_settings, dropped = canonical_tools_settings(raw)
    lost.extend(f"toolsSettings.{key}" for key in dropped)
    translated, not_carried = tools_settings_to_permissions(tools_settings)
    authored = spec.get("permissions")
    # A `web_fetch.trusted` KAS cannot carry, Crew's gate may (`web_fetch_carried`).
    gate_carried = web_fetch_carried(tools_settings, authored)
    named = name_unreadable_entries(
        tools_settings, [path for path in not_carried if path not in gate_carried]
    )
    if "web_fetch.trusted" in gate_carried:
        named += trusted_over_limit_items(tools_settings)
    # A `deniedCommands` KAS did not carry is enforced at Crew's gate
    # (`uncarried_denied_commands`): only the entries neither enforces are lost.
    deny_items = denied_command_items(tools_settings, translated)
    deny_keys = {f"{key}.deniedCommands" for key in SHELL_SETTINGS_KEYS}
    # JSON preserves structured facts across the existing list[str] notice API.
    # Only the final notice renderer formats the raw rule for a person.
    deny_notices = [
        json.dumps(
            {"deniedCommand": {**item, "key": f"toolsSettings.{as_authored(raw, item['key'])}"}}
        )
        for item in deny_items
        if item["key"] in named
    ]
    lost.extend(
        f"toolsSettings.{as_authored(raw, path)}" for path in named if path not in deny_keys
    )
    if authored is not None and not projection_accepts(authored):
        lost.append("permissions")
    lost.extend(f"permissions.rules[{i}]" for i in projection_dropped_rules(authored))
    gate = may_skip_gate_for_session(session_key, governance_app)
    lost.extend(
        f"toolsSettings.{as_authored(raw, path)}"
        for path in trusted_agents_withheld(tools_settings, gate)
    )
    if (
        isinstance(tools_settings, dict)
        and crew_answered_commands(tools_settings)
        and _allowed_commands_withheld(spec, None, tools_settings)
        and not _commands_lifted_by_denies(spec)
    ):
        # Every entry is usable, but the session answers none of them: name each
        # shell key that carries one, so the operator learns why they prompt.
        lost.extend(
            f"toolsSettings.{as_authored(raw, key)}.allowedCommands"
            for key in ("execute_bash", "shell")
            if isinstance(tools_settings.get(key), dict)
            and tools_settings[key].get("allowedCommands")
        )
    for tool, keys, answered in (
        ("fs_write", ("fs_write", "write"), crew_answered_paths),
        ("fs_read", ("fs_read", "read"), crew_answered_read_paths),
    ):
        if (
            isinstance(tools_settings, dict)
            and answered(tools_settings)
            and _allowed_commands_withheld(spec, None, tools_settings, tool)
        ):
            lost.extend(
                f"toolsSettings.{as_authored(raw, key)}.allowedPaths"
                for key in keys
                if isinstance(tools_settings.get(key), dict)
                and tools_settings[key].get("allowedPaths")
            )
    # An allowlist the gate's own readers enforce is marked, so the notice never
    # says it has no effect.
    gate_read = set()
    if isinstance(tools_settings, dict):
        if spec_shell_allowlist(tools_settings, spec.get("allowedTools"), translated) is not None:
            gate_read |= {
                f"toolsSettings.{as_authored(raw, key)}.allowedCommands"
                for key in ("execute_bash", "shell")
            }
        if spec_write_allowlist(tools_settings) is not None:
            gate_read |= {
                f"toolsSettings.{as_authored(raw, key)}.allowedPaths"
                for key in ("fs_write", "write")
            }
    return sorted(
        {f"{key}{GATE_READ_MARK}" if key in gate_read else key for key in lost} | set(deny_notices)
    )


def _allowed_commands_withheld(
    spec: dict[str, Any],
    policy: dict[str, Any] | None,
    tools_settings: Any,
    tool: str = "execute_bash",
) -> bool:
    """Whether *spec*'s usable ``shell.allowedCommands`` go unanswered on KAS.

    They do when the governance ceiling withholds the shell grant at the gate, or
    when the spec's own author restricted the shell elsewhere (an ``ask``/``deny``
    in *policy* or the raw ``permissions`` block, or another shell setting): the
    same reading a shell grant in ``allowedTools`` gets. The ``allowedCommands``
    key itself is the grant, so it is not read as a restriction. One reader for the
    projection and the session-start notice, so the two cannot disagree. With
    *tool* ``fs_write`` / ``fs_read`` the same for the write / read ``allowedPaths``.
    """
    if not may_grant_builtin_at_gate_now(tool):
        return True
    strip = {"execute_bash": without_shell_grant_keys, "fs_read": without_read_grant_keys}.get(
        tool, without_write_grant_keys
    )
    return not withhold_answered_under_authored_rules(
        {tool},
        policy,
        authored_permissions=spec.get("permissions"),
        tools_settings=strip(tools_settings),
    )


class KasAgentTranslationError(ValueError):
    """A spec cannot be projected onto KAS's schema at all."""


class KasAgentSpecAbsent(KasAgentTranslationError):
    """No spec declares the agent and no file carries its name: there is nothing to read.

    Distinct from a spec that exists and cannot be read, because the remedy is
    different: something has to put the definition back (typically by enabling the
    app that registers it), and the caller can say so instead of quoting a failed
    file open. A subclass, so every ``except KasAgentTranslationError`` keeps
    refusing the session exactly as before.
    """

    def __init__(self, agent_id: str, agents_dir: Path) -> None:
        super().__init__(f"agent {agent_id!r} has no spec in {agents_dir}")
        self.agent_id = agent_id


class KasPromptFileUnreadable(KasAgentTranslationError):
    """A ``file://`` prompt names an allowed path that is missing, unreadable or empty.

    kiro-cli runs such an agent with no prompt, so the KAS projection does too
    (:func:`build_kas_custom_agents`); every other reader of the prompt still
    refuses, as a subclass. A path that is not an allowed location is not this.
    """

    def __init__(self, message: str, path: Path) -> None:
        super().__init__(message)
        self.path = path


def resolve_prompt(
    spec: dict[str, Any],
    *,
    agent_id: str,
    agents_dir: Path,
) -> str:
    """Return the spec's prompt as literal text, reading a ``file://`` URI.

    Separated from :func:`to_client_custom_agent` so the projection itself stays
    pure. Because the resolved content is shipped to KAS over the wire, the path
    of a ``file://`` URI is checked before it is read:

    * A RELATIVE path is anchored to *agents_dir* (where the agent config lives,
      the documented base for ``file://./prompts/x.md``), never the gateway cwd.
      It may use ``..`` (``file://../prompts/x.md``), as on kiro-cli's v2 engine:
      an absolute path reaches the same files, so a containment rule on the
      relative form fenced nothing.
    * An ABSOLUTE or ``~`` path is accepted, though kiro-cli's v3 on-disk loader
      refuses it, because the app bridge writes an app agent's prompt that way.
    * Relative or absolute, the RESOLVED path (symlinks followed) must not be a
      credential/governance location or a pseudo-filesystem
      (:func:`kiro_crew.acp.kas_prompt_paths.is_unsafe_prompt_path`).

    A spec from a project checkout is untrusted and stays inside that checkout's
    agents directory; that rule is enforced where the checkout's spec is chosen
    (``acp.harness.kas.resolve_projected_spec``), not here.

    The prompt is ``prompt``, else kiro-cli's ``systemPrompt`` alias
    (:func:`~kiro_crew.acp.kas_prompt.spec_prompt`), and a ``file://`` path
    expands ``$VAR`` and ``~`` (:func:`~kiro_crew.acp.kas_prompt.prompt_file_path`).
    A file that is missing, unreadable or empty raises
    :class:`KasPromptFileUnreadable`.

    A spec with no prompt (missing or blank) resolves to ``""``, which KAS runs
    on its own default prompt alone, as kiro-cli does for the same spec. Crew's
    background agent ``kirocrew-lite`` is the one exception: it gets the small
    inline :data:`_KAS_FALLBACK_PROMPT` brief. That text is a literal, not a file
    read, so it carries none of the ``file://`` path's exfiltration / decode risk.
    Only KAS reaches this — the kiro-cli path keeps its empty-prompt behaviour
    untouched.
    """
    raw = spec_prompt(spec)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # Missing or blank — an intentionally prompt-less agent.
        if agent_id in _KAS_FALLBACK_PROMPT_AGENTS:
            return _KAS_FALLBACK_PROMPT
        return ""
    if not isinstance(raw, str):
        # A non-string prompt is a malformed spec, not a prompt-less one — fail
        # loud rather than silently running with unrelated fallback text.
        raise KasAgentTranslationError(
            f"agent {agent_id!r} prompt must be a string, got {type(raw).__name__}"
        )
    if not raw.startswith(_PROMPT_FILE_SCHEME):
        return raw
    ref = file_uri_path_text(raw)
    try:
        path = prompt_file_path(ref, agents_dir)
    except (OSError, RuntimeError) as exc:  # ``~nosuchuser``, a symlink loop
        bad = f"agent {agent_id!r} prompt path {ref!r} cannot be resolved: {exc}"
        raise KasPromptFileUnreadable(bad, agents_dir / ref) from exc
    if _is_unsafe_prompt_path(path):
        raise KasAgentTranslationError(
            f"agent {agent_id!r} prompt path {path} is not an allowed location; refusing to inline it"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KasPromptFileUnreadable(
            f"agent {agent_id!r} prompt file {path} is unreadable: {exc}. Point the "
            f"spec's prompt at a readable file, delete the {agent_id!r} spec in "
            f"{agents_dir}, or choose another agent",
            path,
        ) from exc
    if not text.strip():
        raise KasPromptFileUnreadable(f"agent {agent_id!r} prompt file {path} is empty", path)
    return text


# The kiro-cli -> KAS tool-name reading lives in ``platform.tool_names`` (a
# stdlib-only leaf) because the PreToolUse gate reads the SAME table in the other
# direction: a spec mounted here in KAS ids must be governed under the kiro-cli
# names an operator's rules are written in. Imported above and applied here.
#
# Why the projection needs it at all: Crew's specs are written in kiro-cli's
# vocabulary (``defaults.json`` mounts ``fs_read``, ``fs_write``, ``grep``,
# ``glob``), and KAS's allowlist matcher (``tools/tool-filter.ts``
# ``matchesPattern``) compares an entry against a tool's exact id or one of its
# tags with no translation -- its only alias is the ``execute_bash`` <->
# ``execute_pwsh`` shell pair. On KAS ``fs_read``, ``grep`` and ``glob`` are not
# tool ids at all (``fs_read`` is only a policy CAPABILITY name there,
# ``policy/capabilities.ts``), so each such entry mounted nothing; ``fs_write``
# is BOTH a registered id (the create/overwrite tool) and the capability name
# ``str_replace`` / ``fs_append`` / ``delete_file`` are filed under, so it
# mounted one tool of its family and none of the rest. A stock Crew spec
# projected verbatim therefore reached the engine holding only the names that
# happen to match on both (``execute_bash``, ``fs_write``, ``code``,
# ``web_fetch``) plus its MCP refs: no way to read a file, list a directory,
# search, or edit in place, and the agent shelled out for every read.
#
# The expansion ADDS the family beside the entry as written, never substitutes
# it: a name that matches on both engines keeps working, a KAS-native name a
# spec already carries is not duplicated, and a reader in either vocabulary
# recognises the list. Applied to ``tools`` and ``excludedTools`` alike, so a
# spec that withholds ``fs_read`` on kiro-cli withholds the same reads here.


def _project_tools(spec: dict[str, Any], agent_id: str) -> str | list[str]:
    """Resolve the tool allowlist, failing closed when the spec is silent.

    ``"*"`` anywhere in the list is KAS's all-tools literal, which is a
    different type from a list, so it cannot simply be passed through. A list
    is spelled out in KAS's own tool ids as well as the spec's
    (:func:`expand_kiro_cli_tool_names`), because the engine translates nothing.
    """
    raw = spec.get("tools")
    if raw == "*":
        return "*"
    if isinstance(raw, list):
        entries = [t for t in raw if isinstance(t, str) and t]
        if "*" in entries:
            return "*"
        return expand_kiro_cli_tool_names(entries)
    # Absent or malformed: KAS would resolve this to zero tools anyway. Emit that
    # explicitly and say so, rather than inferring an allowlist nobody wrote.
    logger.warning(
        "agent %r declares no usable 'tools' list; sending an empty allowlist, so "
        "it will run with no tool access on KAS",
        agent_id,
    )
    return []


#: What a kiro-cli ``excludedTools`` entry of ``@*`` (every MCP server) is on KAS.
#: KAS matches an exclusion against each tool's tags too, and its built-ins carry
#: ``@builtin``, so ``@*`` there withholds every tool, the built-ins the spec's
#: ``tools`` grants included. ``@mcp`` and ``@powers`` are the tags of MCP-served
#: and Powers tools, which is what ``@*`` names on kiro-cli and in Crew's own reads
#: (``default_crew_servers.opted_out``, ``mcp_refs.entry_reaches``).
_KAS_MCP_WILDCARD_EXCLUSIONS: tuple[str, ...] = ("@mcp", "@powers")


def _kas_mcp_wildcard(entries: list[str]) -> list[str]:
    """*entries* with each exact ``@*`` replaced by :data:`_KAS_MCP_WILDCARD_EXCLUSIONS`.

    Only an exclusion list is rewritten, so the built-ins the agent keeps are still
    the ones its ``tools`` list names. Every other entry stays as written.
    """
    if "@*" not in entries:
        return entries
    out: list[str] = []
    for entry in entries:
        out.extend(_KAS_MCP_WILDCARD_EXCLUSIONS if entry == "@*" else (entry,))
    return list(dict.fromkeys(out))


def _authored_allow_gate(session_key: str, governance_app: str | None) -> Callable[[str], bool]:
    """The ceiling predicate for the spec's own allows (``allowedTools``, ``permissions``).

    ``may_skip_gate_now``, except for the sub-agent tool, whose grant meets only the
    profiles that govern this session (``may_skip_gate_for_session``), as a translated
    ``trustedAgents`` allow does. Both fail closed.
    """
    for_session = may_skip_gate_for_session(session_key, governance_app)

    def permits(ref: str) -> bool:
        return for_session(ref) if ref == "use_subagent" else may_skip_gate_now(ref)

    return permits


def _ceiling_permitted(
    allowed_tools: Any, agent_id: str, permits: Callable[[str], bool]
) -> list[str]:
    """``allowedTools`` with every entry the governance ceiling withholds removed.

    Every writer of an ``allowedTools`` list already consults the ceiling when
    it WRITES, so on a freshly rebuilt spec this changes nothing. It is here
    because projection READS a file, and the file can predate the ceiling that now
    governs it: a spec written on an ungoverned host, restored from a backup, or
    edited by hand carries grants nobody ever cleared. Re-asking at projection, like
    ``rebuild_agent_config``'s last sanitizer pass over ``mcpServers``, is deliberate.

    Each entry is asked of *permits* under Crew's name for it (``subagent`` as
    ``use_subagent``). It fails closed: what is dropped here keeps prompting.
    """
    if not isinstance(allowed_tools, list):
        return []
    permitted: list[str] = []
    withheld: list[str] = []
    for raw in allowed_tools:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = canonical_kiro_cli_tool_name(raw.strip())
        if permits(entry):
            permitted.append(entry)
        else:
            withheld.append(entry)
    if withheld:
        names = ", ".join(sorted(withheld))
        logger.info(
            "agent %r: the governance ceiling withholds auto-approval for %s; "
            "projecting no rule for them, so they keep prompting",
            agent_id,
            names,
        )
        _audit_permission_decision(names, "withheld", "governance ceiling", agent_id)
    return permitted


#: Per outcome, the security-event operation it is recorded under. A withhold keeps
#: the name every other writer already uses, so the trail reads as one class of
#: event however the decision was reached; a relay is its own operation, because it is
#: the opposite decision and must never be counted as a withhold.
_PERMISSION_DECISION_OPERATIONS = {
    "withheld": "mcp_auto_approve_withheld",
    "relayed": "kas_authored_permissions_relayed",
}


def _audit_permission_decision(refs: str, outcome: str, reason: str, agent_id: str) -> None:
    """Record one projection-time auto-approve decision in the security event log.

    A withhold is a permission DECISION, and every writer that reaches this state
    records it; ``agent_materialization/auto_approve.py`` owns the shared decision.
    Projection is the only writer whose input is a file it did not write, so a stale grant is likelier to be withheld HERE than
    anywhere else; leaving it at a log line would make the most likely case the one
    with no audit trail.

    Both projection inputs reach it: the ``allowedTools`` derivation and the
    author's own ``permissions`` block, whose withheld ``allow`` is the same
    decision about the same ceiling. One writer so the two cannot drift into
    recording it differently, and it is passed to the merge rather than imported
    there because :mod:`kiro_crew.acp.kas_permissions` is a leaf.

    A RELAY is recorded as well as a withhold. The trail exists so a permission state
    can be reconstructed from it, and "this grant was auto-approved because the author
    asked for it and the ceiling allowed it" is the half a log of refusals alone cannot
    answer.

    Never fails the projection on an audit error: the decision itself has already
    happened, and for a withhold that is the safe direction.
    """
    relayed = outcome == "relayed"
    try:
        sel().log_api_access(
            caller="system",
            operation=_PERMISSION_DECISION_OPERATIONS.get(outcome, "mcp_auto_approve_withheld"),
            outcome="ok",
            source="kas_agent_projection",
            resources=(
                f"{refs} projected with auto-approve ({reason}) " f"for agent {agent_id or '?'}"
                if relayed
                else (
                    f"{refs} projected without auto-approve "
                    f"({reason}) for agent {agent_id or '?'}; "
                    "calls go through the approval gate"
                )
            ),
        )
    except Exception:  # noqa: BLE001 — audit must not break projection
        logger.debug("SEL audit unavailable for KAS projection decision", exc_info=True)


def _audit_skill_scope(rules: tuple[dict[str, Any], ...], agent_id: str) -> None:
    """Record the skill-scope denies a projection adds in the security event log.

    They narrow only, so no ceiling decides them, but they are part of the session's
    permission policy and the trail must be able to reconstruct it. Never fails the
    projection on an audit error.
    """
    try:
        sel().log_api_access(
            caller="system",
            operation="kas_skill_scope_denied",
            outcome="ok",
            source="kas_agent_projection",
            resources=f"{_skill_scope_summary(rules)} for agent {agent_id or '?'}",
        )
    except Exception:  # noqa: BLE001 — audit must not break projection
        logger.debug("SEL audit unavailable for KAS skill scope", exc_info=True)


def to_client_custom_agent(
    agent_id: str,
    spec: dict[str, Any],
    prompt: str,
    *,
    stub_server_names: frozenset[str] = frozenset(),
    member_dispatch: bool = False,
    crew_panel: bool = False,
    session_key: str = "",
    pre_tool_hook_matchers: tuple[str, ...] = (),
    skill_scope_rules: tuple[dict[str, Any], ...] = (),
    withheld_out: WithheldOut | None = None,
    launch_key: str = "",
    engine_features: frozenset[str] = frozenset(),
    forward_ssh_auth_sock: bool = False,
    governance_app: str | None = None,
    scope_muted: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Project one Crew agent spec onto a KAS ``ClientCustomAgent`` descriptor.

    Pure: *prompt* is already-resolved content (see :func:`resolve_prompt`).
    *withheld_out* and *forward_ssh_auth_sock* are handed to :func:`_project_mcp_servers`.

    *stub_server_names* are the servers that will arrive as the session-level
    ``mcpServers`` param and must not also be declared here — see
    :func:`_project_mcp_servers`. The default is empty, which is correct for a
    caller with no shared gateway: nothing is stubbed, so nothing is subtracted.

    *member_dispatch* widens the projection for a crew member's DM session:
    ``@kirocrew-dashboard`` joins ``tools`` (the server itself arrives as a
    session-level entry, but KAS grants only what ``tools`` names), and the
    member's approval-free dashboard verbs join the ``allowedTools`` input
    BEFORE the governance ceiling filter — the conductor grant set plus the
    write verbs the server-side ``created_by`` ownership fence bounds, passed
    through the same ceiling every other grant crosses.

    *crew_panel* widens it the same way for the member's own webview:
    ``@kirocrew-panel`` joins ``tools`` and ``agent._MEMBER_PANEL_GRANTS`` joins
    the same ``allowedTools`` input. Two flags rather than one, because the two
    capabilities are assigned per server and withdrawn by separate operator
    switches: a member may hold session control without a panel, or a panel
    without session control.

    *pre_tool_hook_matchers* are the matchers of every PreToolUse hook this
    session's calls meet on Crew's permission path (the spec's own and the Hooks
    page's). A capability one of them covers is not auto-approved, so its calls
    reach that path, where a surface that fires the hooks has Crew answer an MCP grant
    one held; see
    :func:`kiro_crew.acp.kas_permissions.withhold_hook_gated_auto_approval`.

    *skill_scope_rules* are the ``skill`` deny rules that hold the session to the
    skills its kiro-cli twin loads (:func:`kiro_crew.acp.kas_skill_scope.skill_scope_rules`);
    they join ``permissions`` after every merge. Empty adds nothing.

    A translated ``toolsSettings`` allow, and the spec's own sub-agent allow
    (:func:`_authored_allow_gate`), meet only the profiles the gate could resolve for
    *session_key* and *governance_app* (``governance.may_skip_gate_for_session``).
    """
    if not agent_id:
        raise KasAgentTranslationError("agent id must be non-empty")
    if not isinstance(prompt, str):
        raise KasAgentTranslationError(f"agent {agent_id!r} prompt must be a string")

    dropped = sorted(k for k in UNSUPPORTED_SPEC_KEYS if spec.get(k))
    if dropped:
        # Says WHY the key is dropped: a missing wire field, not a missing KAS
        # feature. Debug, not warning: this fires on every session/new with a
        # constant payload. The user learns of it from the session-start notice
        # the turn loop posts for SPEC_KEYS_WITHOUT_CARRIER, and ``hooks`` still
        # runs, fired by that loop.
        logger.debug(
            "agent %r: spec keys the customAgents wire schema cannot carry, "
            "so an injected agent runs without them: %s",
            agent_id,
            ", ".join(dropped),
        )

    out: dict[str, Any] = {
        "id": agent_id,
        "prompt": prompt,
        "tools": _project_tools(spec, agent_id),
    }
    allowed_tools_input = spec.get("allowedTools")
    if isinstance(allowed_tools_input, list):
        # kiro-cli's own spellings, `@builtin` and `*` under the names every
        # reader below acts on; each name still meets the ceiling one by one.
        allowed_tools_input = expand_kiro_cli_allowed_tools(
            allowed_tools_input, spec.get("mcpServers")
        )
    if member_dispatch:
        # The dashboard server arrives as a session-level entry; naming it in
        # ``tools`` is what grants its tools (KAS resolves ``tools ?? []``).
        # ``"*"`` already covers it.
        tools = out["tools"]
        if isinstance(tools, list) and "@kirocrew-dashboard" not in tools:
            out["tools"] = [*tools, "@kirocrew-dashboard"]
        # circular import: agent imports the config loader, which sits below
        # this module; resolved at call time like the other heavy seams here.
        from kiro_crew.agent import _MEMBER_DASHBOARD_GRANTS

        base_allowed = allowed_tools_input if isinstance(allowed_tools_input, list) else []
        merged = list(base_allowed)
        merged.extend(g for g in _MEMBER_DASHBOARD_GRANTS if g not in merged)
        allowed_tools_input = merged

    if crew_panel:
        # Same two moves as the block above, and for the same reason: the panel
        # server arrives as a session-level entry, and naming it in ``tools`` is
        # what grants its tools. Kept as its own block rather than folded into
        # the one above so a member that holds one capability and not the other
        # is projected with exactly the server it holds.
        tools = out["tools"]
        if isinstance(tools, list) and "@kirocrew-panel" not in tools:
            out["tools"] = [*tools, "@kirocrew-panel"]
        # circular import: same seam as the dashboard grants above.
        from kiro_crew.agent import _MEMBER_PANEL_GRANTS

        base_allowed = allowed_tools_input if isinstance(allowed_tools_input, list) else []
        merged = list(base_allowed)
        merged.extend(g for g in _MEMBER_PANEL_GRANTS if g not in merged)
        allowed_tools_input = merged

    # Read before the autoApprove refs join the list: whether the operator wrote an
    # allowlist is a fact about `allowedTools`, not about a server's autoApprove.
    allowlist_present = isinstance(allowed_tools_input, list)
    # A server's `autoApprove` has no KAS slot; the verbs the kiro-cli route would
    # keep join the derivation input and meet the same ceiling below.
    auto_approved = auto_approve_refs(spec)
    if auto_approved:
        base_allowed = allowed_tools_input if isinstance(allowed_tools_input, list) else []
        allowed_tools_input = [*base_allowed, *(r for r in auto_approved if r not in base_allowed)]

    # Two inputs, one of them governed twice. The derivation is `allowedTools`
    # and nothing else; the spec's own `permissions` block is then folded in by
    # `merge_user_permissions`, which intersects it with the same ceiling instead
    # of adding to it — a user `deny`/`ask` travels as written, a user `allow`
    # only where the ceiling permits that capability and that resource, and the
    # shell and filesystem families never travel as an allow. Dropping the block
    # instead is not the neutral choice it looks like: a spec that authors
    # `permissions` and no `allowedTools` reaches KAS with the field absent, and
    # absent resolves every request to `ask`, so the author's policy becomes a
    # prompt for each of the calls it described.
    authored_gate = _authored_allow_gate(session_key, governance_app)
    permissions = allowed_tools_to_permissions(
        _ceiling_permitted(allowed_tools_input, agent_id, authored_gate), agent_id=agent_id
    )
    # The built-ins the projection keeps at ``ask`` that the spec still granted:
    # Crew answers their permission requests itself, after its own checks. Kept
    # on the entry for the session's record and stripped before the wire
    # (``CREW_ONLY_ENTRY_KEYS``); it is never an allow rule. Not read off
    # ``_ceiling_permitted``: that asks ``may_skip_gate``, which refuses every
    # floor-gated built-in because a static grant would skip the gate. These are
    # answered inside the gate, so they ask the ceiling without that clause.
    crew_answered = frozenset(
        name
        for name in crew_answered_builtins(allowed_tools_input, spec.get("permissions"))
        if may_grant_builtin_at_gate_now(name)
    )
    permissions = merge_user_permissions(
        permissions,
        spec.get("permissions"),
        ceiling_permits=authored_gate,
        audit_decision=lambda refs, outcome, reason: _audit_permission_decision(
            refs, outcome, reason, agent_id
        ),
        # The list's PRESENCE, not its content: an empty list is still the operator
        # saying "auto-approve nothing", and a block must not answer that for them.
        allowlist_present=allowlist_present,
        agent_id=agent_id,
    )
    # `toolsSettings` has no wire slot, but KAS's `permissions` is its replacement. The
    # translated block goes through the same merge as an authored one: its denies travel,
    # its allows meet the ceiling and this session's profiles. Every reader below sees one
    # key per kiro-cli alias group, the one kiro-cli itself keeps.
    tools_settings, _ = canonical_tools_settings(spec.get("toolsSettings"))
    translated, _ = tools_settings_to_permissions(tools_settings)
    untranslated_policy = permissions  # what a checked deny is lifted against
    permissions = merge_user_permissions(
        permissions,
        translated,
        ceiling_permits=may_skip_gate_for_session(session_key, governance_app),
        audit_decision=lambda refs, outcome, reason: _audit_permission_decision(
            refs, outcome, reason, agent_id
        ),
        allowlist_present=allowlist_present,
        agent_id=agent_id,
        source="translated `toolsSettings`",
    )
    authored_policy = permissions  # before the hooks' asks, which restrict nothing
    permissions = with_use_aws_denies(
        permissions, tools_settings, agent_id, _audit_permission_decision, may_skip_gate_now
    )
    # With the batch write guard on, a write-class MCP tool the policy allows gets
    # an exact ``ask`` so its call reaches the guard; the refs are answered by
    # Crew's gate (``batch-guard-grant``), so a write alone still runs unprompted.
    from kiro_crew.acp import batch_guard

    # The guard fails open, as everywhere else: an error here leaves the
    # projection as the spec wrote it instead of failing the session.
    try:
        permissions, guard_answered = batch_guard.withhold_write_auto_approval(
            permissions,
            audit_decision=lambda refs, outcome, reason: _audit_permission_decision(
                refs, outcome, reason, agent_id
            ),
            agent_id=agent_id,
        )
    except Exception:  # noqa: BLE001 - the guard never refuses more
        logger.debug("batch guard projection step failed for %s", agent_id, exc_info=True)
        guard_answered = []
    unhooked = permissions
    permissions = withhold_hook_gated_auto_approval(
        permissions,
        pre_tool_hook_matchers,
        audit_decision=lambda refs, outcome, reason: _audit_permission_decision(
            refs, outcome, reason, agent_id
        ),
        agent_id=agent_id,
    )
    # The MCP grants those asks took from KAS: Crew's gate answers them instead, on
    # a surface that fires the hooks (``kas_hook_coverage.projected_hook_held``).
    hook_held = hook_held_grants(unhooked, permissions)
    if skill_scope_rules:
        # Denies only, so they narrow and need no pass through the ceiling. KAS
        # loads the default skill trees on top of ``resources``; these rules hold
        # the session to the scope its kiro-cli twin loads (``kas_skill_scope``).
        block = dict(permissions) if isinstance(permissions, dict) else {}
        block["rules"] = [*(block.get("rules") or []), *skill_scope_rules]
        permissions = block
        _audit_skill_scope(skill_scope_rules, agent_id)
    # A `web_fetch.blocked` entry KAS cannot carry as a host deny that Crew's gate
    # refuses: an `ask` on its host brings a fetch there to the gate. Read before
    # them: whether those asks alone hold back a fetch the policy ran unprompted.
    web_fetch_lifted = web_fetch_regranted(tools_settings, spec.get("permissions"), permissions)
    permissions = with_blocked_host_asks(permissions, tools_settings)
    if permissions:
        out["permissions"] = permissions
    # Read before the hook asks AND from the raw inputs: an ``ask`` or ``deny`` the
    # spec's author wrote for a capability -- including one a merge refused or a
    # translation could not carry -- keeps its built-ins out of what Crew answers,
    # so the call prompts exactly as the author asked.
    grant_candidates = crew_answered
    crew_answered = withhold_answered_under_authored_rules(
        crew_answered,
        authored_policy,
        authored_permissions=spec.get("permissions"),
        tools_settings=without_grant_only_settings(tools_settings),
    )
    if crew_answered:
        out[CREW_ANSWERED_BUILTINS_KEY] = sorted(crew_answered)
    if guard_answered:
        out[CREW_ANSWERED_WRITES_KEY] = sorted(guard_answered)
    if hook_held:
        out[CREW_ANSWERED_HOOK_HELD_KEY] = hook_held
    # `toolsSettings.shell.allowedCommands` and `permissions` shell globs, answered alike: the gate
    # approves a shell request whose command fullmatches one, after its checks.
    # Asked of the ceiling as the shell tool, which is what the call runs, and
    # withheld under the same authored restrictions as a shell grant. The
    # `allowedCommands` key itself is the grant, so it is not read as one of them.
    answered_commands = crew_answered_commands(tools_settings, spec.get("permissions"))
    if answered_commands and not _allowed_commands_withheld(spec, authored_policy, tools_settings):
        out[CREW_ANSWERED_COMMANDS_KEY] = answered_commands
    # What only a `deniedCommands` / write `deniedPaths` held back, with the denies
    # the gate checks first (`kas_spec_denies`).
    denies = spec_denies_record(
        spec,
        candidates=grant_candidates,
        answered=crew_answered,
        policy=untranslated_policy,
        commands=answered_commands,
        commands_answered=CREW_ANSWERED_COMMANDS_KEY in out,
    )
    # A plain-URL `web_fetch.trusted` grant, held back by any blocked match, and
    # the blocked entries the gate refuses; with the asks above, a fetch they hold
    # back that no entry reaches is granted by name.
    web_fetch = web_fetch_gate(tools_settings, spec.get("permissions"))
    if web_fetch:
        base = denies or {"builtins": [], "commands": [], "deniedCommands": [], "deniedPaths": []}
        denies = {**base, **web_fetch}
        if web_fetch_lifted:
            denies["builtins"] = sorted({*denies["builtins"], "web_fetch"})
    # A `deniedCommands` entry KAS received no rule for: the gate refuses what it
    # names on every approval policy, so an auto-approved call cannot run it. It
    # grants nothing (a record with no lifted grant only refuses).
    uncarried = uncarried_denied_commands(tools_settings, permissions)
    if uncarried and not (denies and denies["deniedCommands"]):
        base = denies or {"builtins": [], "commands": [], "deniedCommands": [], "deniedPaths": []}
        denies = {**base, "deniedCommands": uncarried}
    if denies:
        out[CREW_SPEC_DENIES_KEY] = denies
    # `toolsSettings.shell.denyByDefault`: the only commands the shell may run.
    # A restriction, so recorded whatever the ceiling says; the gate refuses every
    # other shell call of this agent (`spec-shell-allowlist` tier), unless
    # `allowedTools` names the shell and every deny rides *permissions*.
    shell_allowlist = spec_shell_allowlist(
        tools_settings, spec.get("allowedTools"), authored_policy
    )
    if shell_allowlist is not None:
        out[CREW_SHELL_ALLOWLIST_KEY] = shell_allowlist
        # `autoAllowReadonly`: a read-only command passes, unless a deny cannot travel.
        if shell_auto_allows_readonly(tools_settings) and shell_denies_carried(
            tools_settings, authored_policy
        ):
            out[CREW_SHELL_READONLY_KEY] = True
    answered_paths = crew_answered_paths(tools_settings)
    if answered_paths and not _allowed_commands_withheld(
        spec, authored_policy, tools_settings, "fs_write"
    ):
        out[CREW_ANSWERED_PATHS_KEY] = answered_paths
    # The read tool's `allowedPaths`, answered alike (`gate_tiers._spec_read_granted`).
    read_paths = crew_answered_read_paths(tools_settings)
    if read_paths and not _allowed_commands_withheld(
        spec, authored_policy, tools_settings, "fs_read"
    ):
        out[CREW_ANSWERED_READ_PATHS_KEY] = read_paths
    # `toolsSettings.fs_write.fallbackAction: "deny"`: the only paths the write
    # tool may touch, as the spec wrote them. Recorded whatever the ceiling says,
    # like the shell's; the gate refuses every other write of this agent
    # (`spec-write-allowlist` tier).
    write_allowlist = spec_write_allowlist(tools_settings)
    if write_allowlist is not None:
        out[CREW_WRITE_ALLOWLIST_KEY] = write_allowlist

    description = spec.get("description")
    if isinstance(description, str) and description:
        out["description"] = description

    excluded = spec.get("excludedTools")
    if isinstance(excluded, list):
        entries = [t for t in excluded if isinstance(t, str) and t]
        if entries:
            # Same vocabulary gap as ``tools``, in the restricting direction, and
            # with the restricting direction's polarity: a spec that excludes
            # ``fs_read`` for kiro-cli must not leave ``read_file`` mounted here,
            # and one that excludes ``fs_write`` must not leave ``delete_file``.
            out["excludedTools"] = _kas_mcp_wildcard(expand_kiro_cli_tool_exclusions(entries))
    switched_off, unusable = _disabled_tool_exclusions(spec)
    if unusable:
        raise KasAgentTranslationError(
            f"agent {agent_id!r} sets disabledTools on {', '.join(unusable)} to something "
            "other than a list of tool names, so those tools cannot be switched off on KAS. "
            "Make each disabledTools a list of tool names, or remove it"
        )
    if switched_off:
        # A server's ``disabledTools`` has no effect on the wire, so its tools are
        # withheld here instead (see ``kas_tool_limits.disabled_tool_exclusions``).
        out["excludedTools"] = list(dict.fromkeys([*out.get("excludedTools", []), *switched_off]))

    welcome = spec_welcome_message(spec)
    if welcome:
        out["welcomeMessage"] = welcome

    # Both flags widen the agent's VISIBLE tool set; neither auto-approves
    # anything, so they do not cross the governance ceiling that filters
    # ``allowedTools``. A tool they reveal is still resolved by ``permissions``,
    # and this projection derives that from ``allowedTools`` alone -- so a
    # revealed tool with no rule resolves to ``ask``.
    #
    # Forwarded ONLY when the spec states a bool, and no default is synthesized
    # for an absent one: kiro-cli's serde default and KAS's consumer
    # (``tools/tool-filter.ts`` destructures ``includeMcpJson = false``) both read
    # absent as false. kiro-cli also takes the ``useLegacyMcpJson`` spelling, which
    # the wire lacks, so it travels as ``includeMcpJson`` (``include_mcp_json``).
    #
    # ``includeMcpJson`` travels only as ``false``. On KAS ``true`` adds every MCP
    # tool on top of ``tools``, where on kiro-cli it only lets ``tools`` name the
    # ``mcp.json`` servers. The opt-in still decides which of those servers KAS
    # may start: the harness stamps it (``kas_settings_mcp.stamp_settings_mcp``),
    # and the exclusion after the hoist reads it there, so a ref in ``tools``
    # still reaches its server.
    #
    # A non-bool is dropped rather than coerced: ``z.boolean()`` rejects it, and a
    # client agent that fails the wire schema is dropped WHOLE, costing the
    # session its injected agent.
    for flag, value in (
        ("includeMcpJson", include_mcp_json(spec)),
        ("includePowers", spec.get("includePowers")),
    ):
        if isinstance(value, bool) and not (flag == "includeMcpJson" and value):
            out[flag] = value

    # Knowledge-base objects too, checked; the session notice names a dropped one.
    entries, _ = projected_resources(spec.get("resources"))
    if entries:
        out["resources"] = entries

    mcp_servers = _project_mcp_servers(
        spec,
        agent_id,
        stub_server_names,
        session_key,
        withheld_out=withheld_out,
        launch_key=launch_key,
        engine_features=engine_features,
        forward_ssh_auth_sock=forward_ssh_auth_sock,
        scope_muted=scope_muted,
    )
    if mcp_servers:
        out["mcpServers"] = mcp_servers
    if asks := kas_env_launch.connection_asks(out.get("permissions"), mcp_servers, session_key):
        out["permissions"] = asks

    return out


# The declared-name scan's resolved answers, pinned to the agents directory's
# stat-only revision. Its own instance, not the tool-policy read's: the two
# scans carry different SEL ``operation`` labels and must not share answers.
_SPEC_SCAN_MEMO: AgentsDirMemo[dict[str, Any] | None] = AgentsDirMemo()


def _declared_spec(agents_dir: Path, agent_id: str) -> dict[str, Any] | None:
    """The spec that declares *agent_id*, memoised; the one directory scan both
    :func:`load_agent_spec` and :func:`agent_spec_absent` read."""
    # The pin is part of the key: the revision is of the directory, and a pin change
    # in config.json must not be answered from the previous choice. One snapshot
    # keys and resolves, so a concurrent pin write cannot cross the two.
    from kiro_crew import agent_spec_pins

    pins = agent_spec_pins.spec_pins()
    return _SPEC_SCAN_MEMO.get(
        agents_dir,
        pinned_memo_key(agent_id, pins),
        lambda: spec_by_declared_name(
            agents_dir, agent_id, operation="kas_agent_projection", source="unknown", pins=pins
        ),
    )


def agent_spec_absent(agents_dir: Path, agent_id: str) -> bool:
    """Whether *agent_id* has no spec at all in *agents_dir*.

    True when no spec declares the id and neither ``<agent_id>.json`` nor
    ``<agent_id>.md`` exists: a KAS built-in mode (``vibe``) a session switched
    to. A spec that exists but cannot be read is NOT absent; the caller keeps
    its own fail-closed answer for that. Raises when the directory cannot be
    scanned, as :func:`load_agent_spec` does.
    """
    if any(p.is_file() for p in agent_spec_candidates(agents_dir, agent_id)):
        return False
    try:
        declared = _declared_spec(agents_dir, agent_id)
    except AmbiguousAgentSpecError:
        return False
    except OSError as exc:
        raise KasAgentTranslationError(f"agents dir {agents_dir} is unreadable: {exc}") from exc
    return declared is None


def load_agent_spec(agents_dir: Path, agent_id: str) -> dict[str, Any]:
    """Read a materialized agent spec.

    Takes the directory explicitly rather than resolving it here so this module
    stays free of :mod:`kiro_crew.agent`, which imports the config loader and
    would form an import cycle.

    A spec that DECLARES ``name == agent_id`` wins, found through
    :func:`kiro_crew.agent_discovery.spec_by_declared_name`, and
    ``<agent_id>.json`` or ``<agent_id>.md`` (the markdown form, frontmatter
    plus a body that is the prompt -- see :mod:`kiro_crew.agent_spec_format`)
    is read only when no spec declares the id. That is the
    order :func:`kiro_crew.agent.agent_spec_path` and the documented resolution
    convention use, and it is what keeps a misnamed ``<agent_id>.json`` that
    declares some other agent from being projected under this id, with that
    other agent's tools and prompt, while the spec that does declare the id
    sits beside it unread. Two DIFFERING specs declaring *agent_id* are
    refused, as :func:`kiro_crew.agent.agent_spec_path` refuses them: which is
    live is undefined, and picking either would project an agent the operator
    did not name. Two identical ones are one spec, and the scan projects it:
    whichever the backend runs, the projection is the same.

    The scan's parsed spec is what the projection uses: it was read under the hardened
    reader's guards, labelled ``kas_agent_projection`` so a denial is
    attributed to the projection, and reopening the file it came from would
    read it a second time with none of them. The fallback read of
    ``<agent_id>.json`` (or ``.md``) goes through
    :func:`kiro_crew.agent_discovery.read_agent_spec_strict`, the same guards
    with the failure class kept; a spec declaring no name at all, or a name
    other than its stem, reaches the projection only through it.

    A resolved scan answer is served from :data:`_SPEC_SCAN_MEMO`, the
    directory-revision memo, while the directory is unchanged: every KAS
    session start otherwise hardened-reads every spec in the directory to
    find one. That keeps the freshness contract above, because the revision
    is a ``stat`` of every spec entry in the directory, plus a digest of the
    bytes of any entry written inside the racy window, taken before and after
    the read -- an edit changes it, and a directory containing a symlinked
    spec is never memoized (:func:`kiro_crew.agent_discovery.agents_dir_revision`,
    which also refuses a spec entry whose kind cannot be read and a young
    entry it cannot hash). The answer handed
    back is a deep copy (:func:`kiro_crew.user_json.copy_json_document`, which
    walks an explicit stack so a deeply nested spec cannot raise
    ``RecursionError`` out of session start), so it is still a parse the
    caller owns and a caller's mutation cannot reach a later session. The
    fallback read below is one file and is not memoized.

    The scan and the fallback read raise :class:`KasAgentTranslationError` on
    an ``OSError`` for the same reason: every caller of this module handles the
    translation error, not an ``OSError``. On 3.12 ``Path.glob`` propagates one
    from the ``is_dir`` probe it runs on the directory itself (3.13 and 3.14
    run no such probe), and the strict reader raises one for a file it cannot
    resolve or open on every supported version, so an unsearchable agents dir
    reaches this function as an ``OSError`` and the conversion is what makes
    the failure uniform. Neither exception leaves anything in the memo.
    """
    candidates = agent_spec_candidates(agents_dir, agent_id)
    path = candidates[0]
    try:
        declared = _declared_spec(agents_dir, agent_id)
    except AmbiguousAgentSpecError as exc:
        raise KasAgentTranslationError(str(exc)) from exc
    except OSError as exc:
        raise KasAgentTranslationError(f"agent spec {path} is unreadable: {exc}") from exc
    if declared is not None:
        # An iterative copy, not ``copy.deepcopy``: the reader admits a spec
        # nested hundreds of levels deep, and a recursive copy of it would
        # raise ``RecursionError`` out of session start instead of projecting
        # or refusing it.
        return copy_json_document(declared)
    # ``<id>.json`` first: beside an ``<id>.md`` twin the JSON wins, the same
    # rule the directory scan applies (see ``agent_spec_format``).
    present = [p for p in candidates if p.is_file()]
    if present:
        path = present[0]
    try:
        # The hardened reader, not a bare ``read_text``: the agents directory is
        # user-writable, so a symlink here must not be followed to a sensitive
        # target or an oversized file slurped into the projection.
        raw = read_agent_spec_strict(path, operation="kas_agent_projection", source="unknown")
    except (OSError, ValueError) as exc:
        if is_markdown_spec(path) and plain_markdown_document(path):
            # ``<agent_id>.md`` with no opening fence and no JSON twin is a
            # prose document sharing the name, not this agent's spec: it is
            # skipped, which leaves the agent with no spec at all -- the same
            # answer as no candidate file. A FENCED document that fails to
            # parse falls through and raises as a broken spec.
            raise KasAgentTranslationError(
                f"agent {agent_id!r} has no spec: {path} has no frontmatter fence, "
                "so it is a plain markdown document, not an agent spec"
            ) from None
        if isinstance(exc, OSError):
            raise KasAgentTranslationError(f"agent spec {path} is unreadable: {exc}") from exc
        raise KasAgentTranslationError(f"agent spec {path} is not a valid spec: {exc}") from exc
    if not isinstance(raw, dict):
        raise KasAgentTranslationError(f"agent spec {path} is not an object")
    return raw


def pre_tool_hook_matchers(agent_id: str, spec: dict[str, Any]) -> tuple[str, ...]:
    """The matchers of every PreToolUse hook a KAS session's calls meet.

    Three sources, all fired by Crew's permission path on KAS: the spec's own
    ``hooks`` (Crew fires them on KAS, see :mod:`kiro_crew.agent_sdk.spec_hooks`),
    the Hooks page's store, and the user's standalone ``~/.kiro/hooks/*.json``
    files (:func:`kiro_crew.agent_sdk.spec_hooks.user_hook_files`). The projection
    withholds auto-approval for what they cover, because an auto-approved call
    never reaches that path -- a user-file PreToolUse hook that exits 2 would
    otherwise be skipped on exactly the calls KAS auto-approves. Read at session
    start: a Hooks-page or user-file hook added or edited later gates
    auto-approved calls from the next projection on
    (:func:`kiro_crew.agent_sdk.spec_hooks.hook_projection_stale` reads this same
    list, so an edit that changes a file's content marks the session stale).

    Fails toward gating. A source that cannot be read answers ``("*",)``, which
    withholds every auto-approval, rather than an empty tuple that would let a call
    past a hook nobody could list.
    """
    try:
        # A process that registers no store (``kirocrew run``) still has the
        # saved hooks on disk, and they still gate its calls; a saved file that
        # cannot be read raises, which withholds every auto-approval below.
        store = get_global_hook_store() or persisted_hook_store()
        hooks = [*spec_script_hooks(agent_id, spec), *store.list_all(), *user_hook_files()]
    except Exception:  # noqa: BLE001 - fail toward gating, see the docstring
        logger.warning(
            "PreToolUse hooks for %r could not be listed; no call is auto-approved",
            agent_id,
            exc_info=True,
        )
        return ("*",)
    return tuple(
        h.matcher
        for h in hooks
        if h.enabled and h.event == HOOK_EVENT_PRE_TOOL_USE and h.command.strip()
    )


def build_kas_custom_agents(
    agents_dir: Path,
    agent_id: str,
    spec: dict[str, Any],
    *,
    stub_server_names: frozenset[str] = frozenset(),
    member_dispatch: bool = False,
    crew_panel: bool = False,
    session_key: str = "",
    skill_scope_rules: tuple[dict[str, Any], ...] = (),
    withheld_out: WithheldOut | None = None,
    prompt_unread_out: list[Path] | None = None,
    engine_features: frozenset[str] = frozenset(),
    forward_ssh_auth_sock: bool = False,
    governance_app: str | None = None,
    scope_muted: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the ``_meta.kiro.customAgents`` batch that binds *agent_id* on KAS.

    *withheld_out*, *engine_features*, *forward_ssh_auth_sock* and *scope_muted* are handed to
    :func:`_project_mcp_servers`.

    A ``file://`` prompt that is missing, unreadable or empty
    (:class:`KasPromptFileUnreadable`) is projected as ``""``, as kiro-cli runs it
    with no prompt, and its path is appended to *prompt_unread_out* for the
    session-start notice.

    One entry, *agent_id*'s: KAS registers it, it surfaces as a mode, and the
    ordinary ``session/set_mode`` activation selects it; the harness appends the
    agents its sub-agent tool may name (:mod:`kiro_crew.acp.kas_subagent_targets`).
    Without this the session stays on KAS's own default mode.

    A prompt-less spec is projected with ``"prompt": ""``, so it runs on KAS's
    own default prompt as it does on kiro-cli; only ``kirocrew-lite`` gets the
    small :data:`_KAS_FALLBACK_PROMPT` (see :func:`resolve_prompt`).

    *stub_server_names* is forwarded to :func:`_project_mcp_servers`; the caller
    holds the gateway overlay this session will inject from, so it is the only
    layer that can answer which names are stubbed.

    *spec* is REQUIRED and positional, and this function performs no read of its
    own. Its answer becomes the session's whole tool surface, so it has to be
    built from the spec the caller verified under the freshness gate -- reading the
    file here would be a SECOND read, milliseconds later, and a revocation landing
    in between would be projected as though it had been checked. A defaulted
    parameter that fell back to :func:`load_agent_spec` would restore exactly that
    hole for any caller that forgot to pass one, which is why there is no default.
    *agents_dir* stays for :func:`resolve_prompt`, which anchors a ``file://``
    prompt URI and reads a different artifact than the spec.
    """
    try:
        prompt = resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    except KasPromptFileUnreadable as exc:
        logger.warning("%s; the session runs on KAS's default prompt", exc)
        prompt = ""
        if prompt_unread_out is not None:
            prompt_unread_out.append(exc.path)
    return [
        to_client_custom_agent(
            agent_id,
            spec,
            prompt,
            stub_server_names=stub_server_names,
            member_dispatch=member_dispatch,
            crew_panel=crew_panel,
            session_key=session_key,
            pre_tool_hook_matchers=pre_tool_hook_matchers(agent_id, spec),
            skill_scope_rules=skill_scope_rules,
            withheld_out=withheld_out,
            engine_features=engine_features,
            forward_ssh_auth_sock=forward_ssh_auth_sock,
            governance_app=governance_app,
            scope_muted=scope_muted,
            # Any key turns the relay on (its reference is shared), so a session
            # with none yet (a background or pooled runtime) still relays env.
            launch_key=session_key or f"unkeyed-{secrets.token_hex(8)}",
        )
    ]


# The owners hold the ``mcpServers`` projection (``kas_agents_mcp_servers``), the
# session-level hoist (``kas_agents_session_hoist``) and the registered-entry reads
# (``kas_agents_registered``). Every name this module bound before they existed stays
# readable as ``kiro_crew.acp.kas_agents.<name>``:
#
# * Every import of this module stays bound here, so a patch of
#   ``kiro_crew.acp.kas_agents.<name>`` reaches every owner that binds the same object
#   (``_facade_mirror``), as it reached this module's own code.
# * A moved name that callers or this module's own code read is an ordinary import above.
# * Every other moved name is SERVED: absent from this namespace on purpose, read from
#   its owner through the mirror, and a write or delete through this module lands on
#   every owner binding it.
#
# ``test/test_kas_agents_facade.py`` pins the reach.
#: Owner modules, lowest layer first: an owner imports only owners listed before it.
_KAS_AGENTS_OWNERS: tuple[str, ...] = (
    "kiro_crew.acp.kas_agents_registered",
    "kiro_crew.acp.kas_agents_mcp_servers",
    "kiro_crew.acp.kas_agents_session_hoist",
)
for _owner in _KAS_AGENTS_OWNERS:
    _importlib.import_module(_owner)
#: Module-level names this module bound before the split that only owner code reads.
_SERVED: frozenset[str] = frozenset(
    {
        "_CREDENTIAL_BEARING_FIELDS",
        "_HOISTABLE_ENTRY_KEYS",
        "_WRAPPER_MARKERS",
        "_managed_entry",
        "_managed_first",
        "_tokened_managed_element",
    }
)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__],
    _KAS_AGENTS_OWNERS,
    excluded=(),
    surface=frozenset(globals()) | _SERVED,
)
_EXPORTS.update(_MIRROR.served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name the module and its owners bind.
__all__ = sorted(
    n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_") and n != "TYPE_CHECKING"
)

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.acp.kas_agents_mcp_servers import (  # noqa: F401
        _CREDENTIAL_BEARING_FIELDS,
        _WRAPPER_MARKERS,
    )
    from kiro_crew.acp.kas_agents_session_hoist import (  # noqa: F401
        _HOISTABLE_ENTRY_KEYS,
        _managed_entry,
        _managed_first,
        _tokened_managed_element,
    )
