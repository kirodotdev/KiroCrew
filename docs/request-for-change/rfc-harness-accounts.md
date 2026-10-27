---
title: Named accounts per harness -- sign in, default, per-session pick and live switch
status: draft
author: Premshay
created: 2026-10-27
last-audited: 2026-10-27
audited-at: a62012b058
doc-pr: null
implementation-prs: []
tracking-issues: [4184]
supersedes: []
superseded-by: []
---

# RFC: Named Accounts per Harness

Status: draft. Nothing is built. Acceptance requested from a maintainer. The
request, [#4184](https://github.com/kirodotdev/KiroCrew/issues/4184), is labelled
`tier:T3` and `parked`, so a maintainer must also un-park it
([GOVERNANCE.md](../../GOVERNANCE.md)). Every code reference below was read at
`a62012b058`. Paths are under `src/kiro_crew/` unless they start with `website/`.

**Scope.** #4184 asks for Kiro subscriptions. This RFC covers every harness whose
credential home can be moved: Kiro first, through KAS, where Kiro Crew owns the
token, then Claude Code and Codex. Automatic failover on a usage limit is left to a
follow-up RFC.

**Related drafts.** [rfc-chat-backend-selection](rfc-chat-backend-selection.md)
adds the new-chat picker and refuses a pick that cannot start; this RFC follows that
rule without waiting for it.
[rfc-host-credential-vending](rfc-host-credential-vending.md) changes the same mask
code; whichever lands second rebases.
[rfc-central-governance-ceiling](rfc-central-governance-ceiling.md) is where a
deployment would restrict accounts.

## Summary

An owner can register more than one account per harness: two Claude Code
subscriptions, a personal and a work Codex login, or a Builder ID and an Identity
Center Kiro identity.

Each account has its own credential home or vault, owned by Crew and masked from
agents. Accounts of one harness share its session store where the harness allows
it. Each harness has a default account; a session, crewmate or seat can pick
another. An owner can switch a live session's account through the existing slot
reload, keeping the conversation. A pick that cannot start is refused, never
replaced by another identity. With no accounts configured, nothing changes.

## Motivation

### Current state

- **One credential per harness, from the ambient home.** `AgentAuthDeclaration`
  (`agent_sdk/host_auth.py`) lists the variables that move each harness's
  credential home (`home_override_env_vars`), such as `CLAUDE_CONFIG_DIR`,
  `CODEX_HOME` and `PI_CODING_AGENT_DIR`. Crew never sets them per session:
  `ClaudeHarness.apply_spawn_env` sets only `CLAUDE_CODE_EXECUTABLE`.
- **The operator cannot set them either.** `agent.env` (`config/sections_agent.py`)
  refuses "the variables that move a harness's sign-in directory". The refusal is
  right, because the setting is global and plaintext. So a second account means a
  gateway restart with another environment, or a credential file swap.
- **The KAS vault holds one identity per kind.** `TokenStore` (`auth/store.py`) is
  one vault at `<data_home>/kas`, one entry per kind (`_PRIORITY`). The host
  callback `answer_get_access_token` (`acp/kas_host_auth.py`) always reads
  `default_token_store()`.
- **kiro-cli's store cannot move per process everywhere.** `IDENTITY_STORE_ROOTS`
  (`identity_stores.py`) roots it at `XDG_DATA_HOME` on Linux, which moves every
  tool's data too. On macOS no variable moves it.
- **The backend is chosen per session; the account is not.**
  `select_provider_backend` (`members_parts/member_sessions.py`) picks the backend.
  `_acp` in `create_provider_factory` (`config/loader_config.py`) already takes
  named per-session overrides (`backend_override`, `extra_env`, `permission_mode`).
- **Relaunch in place exists.** `reload_slot_session`
  (`dashboard/chat_api/session_relaunch.py`) tears the process down under the slot
  and session locks, arms a resume spawn, and answers 409 while a turn or subagent
  is live.

### Problems

1. Two subscriptions on one harness cannot run side by side.
2. Changing account by hand changes it for every session at once.
3. When a plan hits its usage limit (`PROVIDER_ERROR_USAGE_LIMIT`,
   `acp/transport_errors.py`), the only way on is a global swap.
4. A config directory per account, started by hand, sits outside Crew's
   credential floor.

## Goals

1. Sign in to each account from the dashboard or CLI, for every harness whose home
   can be moved. Kiro goes through KAS.
2. One default account per harness.
3. An account pick per session, crewmates and seats included, with nothing
   duplicated.
4. Switch a live session's account in place and keep the conversation.
5. No credential exposure beyond the active account's home. Agents cannot switch.
6. No silent change of identity.
7. Nothing changes when no accounts are configured.

## Non-goals

- **Named accounts for kiro-cli-owned sign-in.** Its store cannot move per process
  on macOS. Kiro accounts go through KAS or a per-account API key (§4).
- **Automatic failover on a usage limit.** It needs cooldowns, replay, per-harness
  limit wording and an answer on provider terms. It goes to a follow-up RFC. The
  owner switch (§5) is the manual form.
- Pooling or balancing usage across accounts.
- Sharing a credential between users or hosts.
- Accounts on remote boxes (`cloud/login_vault.py`), left for a later RFC.

## Design

### 1. Data model

Accounts live in config. Credential homes do not, so editing config cannot point a
harness at an arbitrary directory.

```jsonc
"agent": {
  "accounts": {
    "kas":    { "default": "builder", "labels": { "builder": "Builder ID", "corp": "Corp IdC" } },
    "claude": { "default": "work", "labels": { "work": "Work", "personal": "Personal" } },
    "codex":  { "default": "system" }
  }
}
```

- **`system` is reserved.** It is the ambient home, today's behaviour, and always
  exists. Other ids match `[a-z0-9-]{1,32}`.
- **Homes.** `<data_home>/accounts/<harness>/<id>/`, mode 0700. A KAS account is a
  vault at `<data_home>/kas/accounts/<id>/`. KAS `system` stays at
  `<data_home>/kas`, so nothing migrates.
- **Slot.** `_ChatSlot` (`dashboard/state_parts/chat_slot.py`) gains `account`
  (`""` follows the default), persisted beside `model` and `agent` in
  `dashboard/slot_persistence/metadata_codec.py`.
- **Session map.** `SessionMap.set` (`session_map.py`) gains `account=` (missing
  reads as `system`), so a restarted session resumes on the account that owns its
  native id.
- **Crew record.** `KiroCrewAgentConfig` (`config/sections_session.py`) gains one
  pin per harness, because a crewmate can run on `member_acp_backend` or a
  `spawn_run(backend=…)` override.
- **Declaration.** `AgentAuthDeclaration` gains `account_home_env` (the one
  variable Crew sets), `account_private_entries` (globs owned by one account;
  everything else is shared) and `account_login_argv` (taken from a live capture,
  never guessed). A harness that leaves them empty offers no accounts.

### 2. Resolution

`select_account(...)` sits beside `select_provider_backend` and is called once in
`_acp`. First match wins:

1. The slot's explicit pick.
2. The crewmate's pin for this harness, for the keys `select_provider_backend`
   already routes.
3. `agent.accounts.<harness>.default`.
4. `system`.

**No silent fallback.** A pick naming a missing or signed-out account is refused
when set. If a named account later signs out, the next turn is refused with a chat
card offering sign-in or a switch. `system` keeps today's signed-out behaviour. The
fallback could belong to another organisation, so Crew refuses rather than
substitutes.

`_acp` gains a named `account_override` kwarg, never absorbed by `**_kwargs`,
passed through `build_acp_provider` (`agent_sdk/drivers/acp.py`).

**Spawn.** For a named account, Crew sets `env[account_home_env]` after
`agent_env_overlay()` and `apply_spawn_env`, and before the env scrub, so
`agent.env` cannot override it. For Codex this precedes `seed_private_state`
(`acp/codex_sqlite.py`), which reads `CODEX_HOME` from the child env.

**Shared runtimes.** One `AcpRuntime` (`acp/runtime_class.py`) can carry several
sessions, and a shared subagent takes its parent's runtime
(`subagent_manager/run_start.py`). The invariant: **a session never attaches to a
runtime spawned under a different account.** The runtime records its account; the
shared subagent runtime, the warm pool and the background runtime refuse a
mismatch. A dedicated subagent or a Codex `spawn_continue` child resolves from its
own session key.

### 3. Shared session stores

An account home is a **symlink farm**, shared by default. Entries matching
`account_private_entries` are real files. Every other top-level entry of the
ambient home is a symlink to it, created with the account and topped up at spawn.
Sharing by default keeps entries a resume needs, such as Claude's `todos/` or
Codex's `history.jsonl`, without listing them.

| Harness | Account env | Private per account | Note |
|---|---|---|---|
| Claude Code | `CLAUDE_CONFIG_DIR` | `.credentials.json`, `.claude.json` | Resume reads the shared `projects/`. |
| Codex | `CODEX_HOME` | `auth.json`, `state_*.sqlite*`, `logs_*.sqlite*` | SQLite files are never linked ([#15007](https://github.com/kirodotdev/KiroCrew/issues/15007) is that lock-out). A thread resumes from its rollout, so sharing `sessions/` is enough. |
| pi | `PI_CODING_AGENT_DIR` | `auth.json` | Phase 0 confirms the session directory. |
| KAS | none | `kas/accounts/<id>` vault | §4. |

DeepSeek's entitlement is a vault key (`ENTITLEMENT_HOST_VAULT`), so an account is
another secret name. OpenCode (`XDG_DATA_HOME` moves its tools too) and goose (OS
keyring) are deferred.

**Divergence.** Claude Code and Codex may rewrite `settings.json` or `config.toml`
by temp file and rename, replacing the symlink with a real file. Crew never
overwrites it: it marks the entry `diverged`, and the Accounts card (§7) offers
"keep this account's copy" or "restore the link". Only top-level files can
diverge; a rename inside a shared directory lands in the target.

**Claude's `.claude.json`.** It holds `oauthAccount`, so it is private. It also
holds the `mcpServers` that Crew's `ccGlobal` scope edits at `~/.claude.json`
(`_CC_MCP_JSON` in `agent.py`; `dashboard/handlers/mcp_config_files.py`). So
`~/.claude.json` stays the source of truth for Crew-managed keys. At spawn, under a
file lock, Crew copies a declared key list (`account_synced_keys`, starting with
`mcpServers`) one way into the account's file, leaving `oauthAccount` alone.
Whether Claude reads project trust from this file is not verified; Phase 0 settles
it.

**When resume fails.** A provider may refuse a conversation resumed under another
account, for example over thinking blocks bound to the original.
`_is_thinking_binding_mismatch` (`acp/transport_errors.py`) recovers only when one
field carries both the invalid-signature and the bound-elsewhere clauses, so a
cross-account rejection with only the first is not recovered today. So a switch
assumes resume may fail. Then `SessionMap.clear_sid` drops the native id,
`build_session_replay` (`context.py`) rebuilds the turn from Crew's log, and the
event is SEL-audited like `detect_provider_switch` (`session.py`) and announced in
chat. If Phase 0c finds wording the matcher misses, Phase 5 adds a matcher scoped
to right after a switch.

**Concurrent writers.** Sharing `sessions/` adds no writer that one `CODEX_HOME`
lacks today. Whether two Crew runtimes can write one Codex thread at once through
`spawn_continue` is not verified.

### 4. Auth flows

Every flow is owner-only.

- **KAS.** `TokenStore(data_home, account=id)` roots one vault per account, with
  encryption, locking and refresh unchanged. The `/api/kas-login/*` routes and
  `kirocrew kas login|logout` take an account, defaulting to `system`. The host
  callback is bound **per runtime**, so two KAS sessions on different accounts get
  their own tokens. **A named account with an empty vault is refused.** Today a
  sign-out's replacement processes "re-probe the vault and, finding nothing, spawn
  kiro-cli-owned" (`_retire_runtimes_after_sign_out`,
  `dashboard/handlers/kas_login.py`). For a named account that is another
  identity, so it gets a chat card instead. `system` is unchanged.
- **Claude Code, Codex, pi.** Crew runs `account_login_argv` with the account
  variable set, in a Crew-owned PTY shown in a dashboard dialog, never in an agent
  session or a terminal an agent can attach to. Device-code login is preferred
  where offered ([kas-auth.md](../system-specs/modules/kas-auth.md), Transport
  selection). "Signed in" means the credential leaf exists; Crew never reads it,
  as with `ENTITLEMENT_OWN_CREDENTIAL_FILE`. A keychain-held credential reads as
  `unknown`, never `false`.
- **kiro (kiro-cli owns sign-in).** No named accounts. An account can be an API
  key from Crew's vault, injected as `KIRO_API_KEY` for that spawn.
- **Sign-out** removes that account's credential and retires only its processes.
  Today `_retire_runtimes_after_sign_out` retires every KAS and Kiro process.

### 5. Live switch

`POST /api/chat/slots/{slot}/account {account}` joins `/model` and `/agent` in
`dashboard/routes/chat.py` as a fifth commit-before-reset switch handler, under
the same lock order:

1. Validate the account exists and is signed in, else 409 `account_unavailable`.
2. Commit `slot.account`.
3. Call `reload_slot_session` with the reload route's callbacks.
4. The new process resolves its account (§2) and resumes from the shared store, or
   falls to the replay in §3.

The switch inherits reload's 409s. On a session with history, the dialog says the
conversation so far, file contents included, goes to the other account's provider,
and the owner confirms first.

### 6. Warm pool, background runtime, identity sweeps

- **Pool.** A pooled client has no session key (`_apply_session_identity_env`,
  `acp/client_spawn_env.py`), so it runs on the default. `_claim_from_pool`
  (`session_pool.py`) is keyed by `(agent, account)`; a mismatch cold-starts. A new
  default drains the pool as `_claimed_under_previous_identity` does.
- **Background runtime.** It uses the default account, and a new default
  displaces it as a backend switch does (`_draining_bg_runtimes`,
  `session_background.py`).
- **Identity sweeps.** One fingerprint covers the gateway today:
  `_combine_identity_fingerprints` (`kiro_prerequisite_identity.py`) feeds
  `_retire_sessions_on_identity_change`. Signing in to account B would retire A's
  sessions. Phase 2 keys the fingerprint per account, and
  `retire_kiro_identity_sessions` retires only runtimes whose account changed.

### 7. UI

- **Settings → Agent Harness → Accounts.** One card per harness, next to
  `KiroSignInCard.tsx`: each account's state (signed in, signed out, unknown); add,
  sign in, sign out, set default; diverged entries (§3).
- **Header.** The readout capsule (`website/src/shell/topbar/shellHeader.tsx`)
  shows the chat's account when its harness has several, and opens the switcher.
  Usage readings are keyed by account.
- **Chat.** An Account submenu in `ChatHeaderMenu.tsx`, disabled during a turn,
  and an account selector in the new-chat picker.
- **Crewmate profile.** One account row per harness it can run on.
- **API and CLI.** `/api/accounts` routes (state only, never tokens) and
  `kirocrew accounts list|add|login|logout|default|remove`.

## Migration plan

Each phase is one PR and can be abandoned alone.

**Phase 0: probe, no code.** On claude, codex, pi and kas: (a) the variable moves
the credential; (b) the harness accepts the farm, resumes from shared `projects/`
or `sessions/`, and which entries it rewrites by rename; (c) whether a transcript
with thinking blocks resumes under another account, and the rejection wording if
not; (d) on macOS, where Claude keeps its credential under `CLAUDE_CONFIG_DIR` and
where `.claude.json` lands; (e) each private-entry list; (f) whether each sandbox
backend can re-expose one read-write directory inside a masked tree.
*Exit:* verdicts recorded here with harness versions.

**Phase 1: model, resolution, spawn, fences, CLI.** No harness offers accounts yet
except a test declaration.
*Exit:* with `agent.accounts` absent, spawn env and mask are byte-identical to
base (golden test); the read gate refuses every path under `accounts/`; a session
on B never attaches to a runtime spawned on A; a pick of a missing or signed-out
account is refused.

**Phase 2: KAS named accounts.**
*Exit:* two KAS sessions on different accounts each get their own token from
`_kiro/auth/getAccessToken`; signing in to B does not retire A's sessions; signing
out of A retires only A's processes; an empty named account is refused with a chat
card and never spawns kiro-cli-owned.

**Phase 3: Claude Code and Codex homes.** Blocked on Phase 0 for the harness.
*Exit:* a child on a second Claude account has `CLAUDE_CONFIG_DIR` set to its
home; under every masking tier, a child on A cannot read B's credential file; a
named spawn under a tier that drops masks is refused; a diverged `settings.json` is
reported, not overwritten; Crew-managed `mcpServers` reach an account session.

**Phase 4: dashboard accounts.**
*Exit:* a non-owner gets 403 and a SEL row on every account route; signing in to B
leaves A unchanged; the login PTY reaches no agent-readable surface.

**Phase 5: live switch.**
*Exit:* after a switch the next turn runs on the new account with the prior
transcript in context, by session id or an announced replay; the route answers 409
for a turn in flight, attached subagents and `account_unavailable`.

**Phase 6: more harnesses.** A declaration plus a Phase 0 row each.

## Backward compatibility

With no `agent.accounts`, every session resolves to `system`. No variable is set,
no farm is created, the pool key is unchanged, session-map entries without
`account` read as `system`, KAS `system` keeps `<data_home>/kas` and its
kiro-cli-owned fallback, and `kirocrew kas` acts on `system` unless given
`--account`.

## Security considerations

- **Masked homes.** Add `accounts` beside `kas` in `_CREW_SECRET_LEAVES`
  (`security/paths_lists.py`, file tools), `_CREW_HIDDEN_LEAVES`
  (`sandbox_leaves.py`, OS sandbox) and `_CREW_PRECREATE_HIDDEN_DIR_LEAVES`
  (`sandbox_precreate.py`, so an early child is masked too).
- **The active account's home.** The harness must read, and for OAuth refresh
  write, its own home. Nothing does this today. `extra_expose_files`
  (`acp/harness/base.py`) re-exposes single files read-only, which breaks refresh.
  `adapter_hidden_credential_dirs` (`agent_sdk/tool_gate.py`) returns nothing
  outside `ENFORCED_ROUTINGS`, which excludes Claude. So a new `SpawnPlan` field,
  `account_expose_dir`, re-exposes exactly one directory read-write inside the
  masked tree, for every harness that offers accounts.
  - **Masking tier:** a child reads its own account's home and no other. Phase 0f
    confirms each backend can do this.
  - **Tier that drops masks** (`off`, or an opted-in unsandboxed host): any child
    could read every account home under the owner's uid, so exposure grows from
    one credential to N. **A named-account spawn there is refused** in v1.
  - **Residual:** a shell the model runs can read the active credential, the
    trade already accepted for `adapter_own_leaves`. For Claude, whose tools are
    not gated, the sandbox is the only control.
- **Floor.** Leaves are fenced only under roots in the gateway's environment
  (`_ADAPTER_ROOT_ENVS`, `security/paths_targets.py`). Account homes become roots.
- **Data boundary.** A switch sends a conversation built under one account to
  another's provider, for example work code to a personal plan. Controls: the
  confirmation (§5), no silent fallback (§2, §4), and owner-only pins and
  defaults. Forbidding cross-organisation moves needs an enforced ceiling; this RFC
  asks `rfc-central-governance-ceiling` to carry an `accounts` key.
- **Provider terms.** Several consumer subscriptions of one provider, with work
  moved between them, may conflict with its terms. Not verified. This RFC adds no
  automatic switching; the failover RFC must settle terms first.
- **No credential in config or env.** Config holds ids and labels; the env holds a
  masked path.
- **Owner-only.** Every account route uses `is_owner_dashboard_request`
  (`dashboard/handlers/source_providers.py`). No MCP verb, and change cards cannot
  target account keys. An agent's `session_reload` cannot change the account.
- **Login PTY.** Its scrollback can hold device codes and emails. Phase 4 asserts
  it reaches no agent-readable surface.
- **Audit.** Every create, sign-in, sign-out, default change, switch and refusal
  writes a SEL row (`log_api_access` for routes, `log_tool_invocation` with
  `tool_kind="lifecycle"` for switches). Rows carry ids, never a label or token.
- **Pod remap.** Unchanged: `ACP_BACKENDS_POD_HOME_REMAP`
  (`agent_sdk/backends_launch.py`) names only `kiro`, which has no named accounts.

## Alternatives considered

1. **Swap credential files in the ambient home.** Global, so no parallel accounts,
   and live sessions must recover from it
   ([#3393](https://github.com/kirodotdev/KiroCrew/issues/3393)).
2. **One seat or crewmate per account.** Multiplies config by the account count,
   and copies drift.
3. **Allow the home variables in `agent.env`.** Global, plaintext, no per-session
   choice, outside the floor.
4. **One gateway per account.** Doubles every process; conversations cannot move.
5. **Copy the conversation on switch.** Two stores diverge.
6. **List shared entries, not private ones.** Misses what a later release adds.
7. **Remap `HOME` per account.** Also moves SSH, git and tool config.

## Open questions

1. Should an agent ever *request* a switch through an owner-answered card?
   Proposed: no in v1.
2. On macOS, does `CLAUDE_CONFIG_DIR` give Claude Code its own keychain entry
   (Phase 0d)? If not, Claude accounts start on Linux and Windows.
3. Should an owner be able to opt in to named accounts under a tier that drops
   masks?
4. Should an existing directory be adoptable as an account home?
5. Should `system` be hideable once named accounts exist?
