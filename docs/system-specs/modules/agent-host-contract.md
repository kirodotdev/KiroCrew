# Agent host contract

What an agent backend must supply to Kiro Crew *besides* speaking the Agent Client
Protocol. ACP defines the wire; this document defines the **host** — the
filesystem layout, agent-definition format, session store, credential store,
sandbox posture, MCP delivery channel, billing surface, and permission engine
that sit around the wire and differ per backend.

Four backends are described, and **all four are selectable on a plain public
build**. The baseline registry `BASELINE_SELECTABLE_BACKENDS` (`acp_backends.py`)
contains every id in `ACP_BACKENDS_KNOWN`; there is no frozen
`ACP_BACKENDS_SELECTABLE` constant, because the set is a registry an
edition may extend and a deployment policy may narrow. Claude Code is not a
dormant seam reachable only from an internal companion package: `acp/client.py`
owns the entire CC spawn path (`_is_claude`, `_resolve_claude_acp_bin`,
`_resolve_claude_code_executable`), `providers/acp.py` constructs it, and the
adapter it spawns is a public npm package
(`CLAUDE_ACP_NPM_PKG = "@agentclientprotocol/claude-agent-acp"`). Nothing in the
spawn path is edition-private; the selector switch was the only missing piece.

What actually varies for CC is **machine-local**: it needs two binaries the
operator installs — the `claude-agent-acp` adapter and the `claude` CLI handed to
it as `CLAUDE_CODE_EXECUTABLE`. That is a third question, kept apart from the
other two on purpose: capability (`acp_backends.py`, can this build drive the
harness), permission (the `agent_backend` governance scope, may this deployment
select it), and installation (`agent_sdk/backend_install.py`, is it on this
machine). Only the third can change without a config write or a new build, and it
is the one that reports `installed` / `missing` / `unknown` per component with the
command that installs the adapter. A backend this deployment may not select is
**hidden** from the dashboard rather than greyed out, so no "not enabled in this
build" state is rendered for any agent.

See [claude-code-provider.md](claude-code-provider.md) for why there is no
standalone `ClaudeCodeProvider` class — the ACP provider is the only admissible
`AgentConfig.provider`, and a backend is chosen through `agent.acp_backend` rather
than by adding a provider class — and
[acp-client.md](acp-client.md) for the seam's protocol-level
details.

Two of the four are genuinely *foreign* hosts — **CC and Codex** — and they
teach different lessons. KAS is not one of them: it is Kiro's own agent service
reached through `kiro-cli acp`, so it shares Kiro's identity store, runtime and
model vocabulary, and it is the cheap case rather than the instructive one. CC is
the complete foreign column — every bucket answered, several of them only because
a companion supplies the answer — so it is the column that tells a future provider
author what they are actually signing up for.

Codex is the newest, and it is in this document for a second reason: it is the
first backend added *after* the checklist at the end of this file existed, and it
shipped with every bucket here blank. Nothing failed, because nothing enforced
it. Its column was written afterwards, from the code, by a reader who had to
re-derive what the author knew — which is the cost this document exists to avoid.
`test_agent_host_contract_parity.py` now fails when a backend in
`ACP_BACKENDS_KNOWN` has no column, so the next one cannot arrive silently. Read
the two foreign columns as a pair: CC for what a complete answer looks like,
Codex for what a partial one looks like and which rows stay honest by saying
"unmeasured".

Where a CC row is marked **(companion)**, the behaviour is supplied by an
internal companion package rather than by this repository, and is described here
by what it does. The companion is not public and its internals are deliberately
not cited: what a provider author needs from this table is the *requirement*, not
where one implementation happens to satisfy it.

Those rows are not what makes CC *reachable* — the spawn path is public and a
public build starts a CC session without any of them. They are what makes it
*complete*. A build that overrides none of them runs a working harness with pieces
missing, and the largest of those is stated plainly in §5: a CC session with zero
MCP tools.

## Column meaning

| Column | Backend |
|---|---|
| **kiro-cli** | `ACP_BACKEND_KIRO = ""` — the default. Kiro's CLI over ACP. |
| **KAS** | `ACP_BACKEND_KAS = "kas"` — Kiro's agent service, run through `kiro-cli acp --agent-engine v3 --auth-method cli`. |
| **CC** | `ACP_BACKEND_CLAUDE = "claude"` — `claude-agent-acp`. Selectable on a public build; usable on a given machine once the operator has installed both the adapter and the `claude` CLI. |
| **Codex** | `ACP_BACKEND_CODEX = "codex"` — `codex-acp`, a Node stdio adapter that boots the Codex app server and translates ACP onto its operations. Selectable on a public build; one component to install, and its tool permissions are routed through `session/set_config_option` rather than any file (§7). |

## 1. Agent definition and layout

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Where a managed agent lives | `~/.kiro/agents/kirocrew.json` (+ `-lite`, `-conductor`, `-knowledge`, `-research`, `-heartbeat` variants, `agent_files.py`) | Nowhere on disk — agents ride `_meta.kiro.customAgents` on `session/new` (`acp/kas_agents.py`) | `~/.claude/agents/<name>.md`, Markdown with YAML frontmatter (companion: a JSON→Markdown agent translator writes it) | Nowhere — spawned bare, with no `--agent` and no spec on disk (`acp/client.py`) |
| Format | JSON, validated with `deny_unknown_fields`; an unknown key silently falls back to the default agent, which is why bookkeeping lives in an `agent_state` sidecar (`agent.py`) | JSON projected onto the wire; `prompt` must be inlined, and `tools` absent means *no* tools (`kas_agents.py`) | YAML frontmatter (`name/description/model/permissionMode/tools/mcpServers/disallowedTools/hooks`) plus the system prompt as the body | None — no definition is projected in any shape: the spec is not read and no wire equivalent is sent (`acp/client.py`). `providers/mirrors/registry.py` records that as codex's declared state rather than leaving the omission unexplained |
| Selection mechanism | `--agent <name>`; `$PWD/.kiro/agents` shadows `~/.kiro/agents` (`agent_discovery.py`) | `session/set_mode` against a wire-supplied agent | No `--agent` and **no `set_mode` at all**; the agent-activation privilege check is skipped (`acp/client.py`) | No `--agent`, and **no `set_mode`** — activation is gated `if self._is_kiro` (`acp/client.py`). What IS applied before the first prompt is `session/set_config_option("mode", "read-only")`, and a session that never advertised the option is refused (`acp_backends.py`, `acp/client.py`) |
| Hook event names | Crew's own | Hooks cannot ride an over-the-wire agent (`kas_agents.py`) | Renamed: `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `SessionStart` / `Stop` (companion) | Crew's own, unrenamed — nothing is projected into the harness, so the gate is reached only through `session/request_permission` → `HookManager.on_tool_call` |
| Model pin | `model` in the spec; `"auto"` resolvable | Not projected | Separate `cc_model` sidecar field; cannot resolve `"auto"`, so background agents get a concrete pin (`agent.py`) | over the wire, not in a spec: `session/set_config_option("model", …)` (`acp_backends.py`). `"auto"` is **not** resolved — it is skipped and the adapter's own default stands (`acp/client.py`). Effort takes the same channel; advertised-spelling folding is excluded (`acp_backends.py`) |
| Protocol version | date-stamped `2025-08-22` | date-stamped | numeric `1` (`acp/client.py`) | numeric `1`, kept as its own literal rather than folded into CC's (`acp/client.py`) |

**A provider must declare:** its definition target (or that there is none), its
validation strictness, whether bookkeeping may live in-spec, its
shadowing/selection rules, its hook-event vocabulary, whether it can resolve an
`"auto"` model, and its protocol-version shape.

## 2. Session persistence

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Replay store | `<kiro home>/sessions/cli/<sid>.json` + `.jsonl`, read to resume (`session_storage.py`) | same store (it *is* kiro-cli) | Its own: `<cc root>/projects/<encoded cwd>/<sid>.jsonl` + `<sid>/`, plus `<cc root>/file-history/<sid>/` (companion: a dedicated transcript-cleanup module exists for exactly this) | None on Crew's side — the adapter keeps its own records and resolves them from the `sessionId` (`acp/client.py`); the session map skips both the transcript pre-check and the prune for any non-kiro label (`session_map.py`) |
| Store root | `KIRO_HOME` | `KIRO_HOME` | `CLAUDE_CONFIG_DIR` → `<config dir>/cc-config` → `~/.claude` (companion) | not Crew's to compute — `CODEX_HOME` is read only to re-anchor the credential leaf, never to locate a transcript (`security.py`) |
| Directory naming | session id | session id | `realpath(cwd)` with every non-alphanumeric replaced by `-`. **`realpath`, not `abspath`** — on cloud desktops `/home/<user>` is a symlink to `/local/home/<user>`, and an abspath encoding silently misses the transcript directory (companion) | unknown to this repository — nothing here names or reads a codex session directory |
| `session/load` | carries the transcript path, guarded by a file-exists pre-check (`acp/client.py`) | same | carries **no** path; the pre-check is skipped entirely (`acp/client.py`) | carries **no** path and no `_meta`; the file-exists pre-check is skipped, because gating on the kiro transcript would make `file_ok` always False and silently start every resume fresh (`acp/client.py`) |
| Compaction | asynchronous, reported by `_kiro.dev/compaction/status`; the session id changes | same | `/compact` runs **synchronously inside `session/prompt`**; no status notification ever arrives, and the session id survives (`dashboard/chat_runner.py`) | not supported — excluded from `ACP_BACKENDS_COMPACT`, so a manual `/compact` is refused up front instead of stranding the status waiter (`acp_backends.py`, `acp/session_provider.py`) |
| Sessions per process | many (demultiplexed over `AcpRuntime`) | many | **one** (`acp/types.py`) | **one** — not in `ACP_BACKENDS_ACP_RUNTIME`, so it takes the `AcpClient` path, spawned per session (`acp_backends.py`); no shared subagent session either |
| History replay | full history every turn, so one oversized image block wedges a transcript permanently and Crew repairs kiro-cli's own file (`session_image_repair.py`) | same | not applicable | not applicable — no Crew-side transcript to replay or repair |

**A provider must declare:** its replay-store path and naming (or that it has
none), its store root and how that root is recomputable from config alone,
whether `session/load` needs a local transcript, its compaction model
(synchronous or reported), how many sessions share a process, and whether it
replays full history.

## 3. Identity and auth

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Sign-in | `kiro-cli login`; SSO `--use-device-flow --license pro` (`kiro_prerequisite.py`) | same | brings its own: a credential-refresh **command** named inside its own `settings.json`, plus a provider-routing env var on the child (companion) | brings its own, and Crew implements none: no login command and no auth probe. The `AcpAuthRequired` login prompt is an `AcpRuntime` path this backend never takes (`acp/session_provider.py`) |
| Credential store | projected, never copied: identity tables plus `migrations` rows plus selected `state` rows (`kiro_prerequisite.py`) | same store | its own; that refresh command is copied **verbatim** into the isolated seed at `0o600`. Dropping it breaks auth outright, so the seed cannot simply be emptied (companion) | its own `~/.codex/auth.json`, on the read-gate floor with `CODEX_HOME` re-anchored so an override cannot move it out from under the gate (`security.py`). Crew never reads or copies it, and it is the ONE leaf excluded from the child's OS credential mask so the adapter can still authenticate (`acp_tool_gate.py`) |
| Recyclable on a host logout | yes | yes (`ACP_BACKENDS_KIRO_IDENTITY_STORE`, `acp_backends.py`) | **no** — a live CC child must survive `kiro-cli logout` | **no** — excluded from `ACP_BACKENDS_KIRO_IDENTITY_STORE`: a `kiro-cli logout` says nothing about whether a running codex child is still authenticated (`acp_backends.py`) |
| Entitlement discovery | account API | account API | runtime, from the advertised model set at session init; the registry is filtered down to it (`dashboard/handlers/agents.py`) | runtime, from the model set advertised at `session/new` / `session/load`, but in-memory for that session only — no account API, no registry filter, no cross-session persist (excluded from `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`) (`acp/client.py`) |
| Readiness probe | `--version` then `whoami`, inside the OS sandbox (`kiro_prerequisite.py`) | same | binary resolution only, but for **both** components and through the spawn's own resolvers, so the answer cannot disagree with what a spawn does (`agent_sdk/backend_install.py`, `agent_sdk/drivers/acp.py`) | one component, adapter resolution only and through the spawn's own resolver: `codex-acp`, reported `installed` / `missing` with the install command, plus `restart_required` when this process cached a negative (`agent_sdk/backend_install.py`). No auth check |

**A provider must declare:** its login and org-SSO commands, its credential
locations, whether a host logout may retire its live children, how entitlement is
discovered, and its readiness probe.

The membership set is named `ACP_BACKENDS_KIRO_IDENTITY_STORE`, but its meaning is
*authorization*, not ownership: it records that a `kiro-cli logout` may retire
this backend's live child. A provider that brings its own auth is excluded, and
the exclusion is load-bearing.

## 4. Sandbox

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Self-sandboxing | yes, ≥ 2.13, via `~/.kiro/settings/amazon-internal.json` key `"sandbox"` (`sandbox.py`) | KAS-owned Seatbelt/Bubblewrap, not passed by kiro-cli | **no** — "a Node or Python harness does not qualify" (`acp/types.py`) | **no** — a Node adapter; the sandbox modes it can apply are in-process policy, not an OS sandbox Crew's could nest inside (`acp_backends.py`) |
| Interaction with Crew's sandbox | mutually exclusive on macOS: internal ON → Crew seatbelt OFF, because seatbelt cannot nest (`sandbox.py`) | Crew seatbelt remains the sole isolation layer | Crew keeps its own wrap | Crew keeps its own wrap, and here it is load-bearing rather than incidental: the wrap carries the credential mask that compensates for the ungated passive read, so a session that would spawn UNWRAPPED is **refused** before the spawn (`acp_tool_gate.py`, `acp/client.py`). The test is whether the mask will be applied, keyed on the EFFECTIVE tier, so a governance floor raising `off` still qualifies (`sandbox.py`) |
| Delegation predicate | `argv[0]` basename is literally `kiro-cli` (`sandbox.py`) | same binary | never delegates | never delegates — not in `ACP_BACKENDS_INTERNAL_SANDBOX`, so `is_kiro_cli` is False and neither the macOS seatbelt skip nor the Windows Kiro-only delegation is granted |
| Extra hidden paths | core tiers | core tiers | adds `.midway`/`.ada`/`.aws`/`sso`/`.krb5`, ADD-only on both tiers (companion) | core tiers **plus the whole read-gate floor, derived rather than enumerated**: `sandbox_credential_targets` re-projects every `_SENSITIVE_HOME_DIRS` leaf with the gate's own override anchoring, minus `.codex/auth.json` — the one leaf the adapter must read to authenticate (`acp_tool_gate.py`). Ungated `.ssh` arrives with it, so git-over-SSH inside such a session stops working; accepted rather than worked around, and there is no local opt-out |

Every detection failure resolves toward Crew's own sandbox (`sandbox.py`).

**A provider must declare:** whether it self-sandboxes and how that is detected,
its nesting compatibility, its delegation predicate, and any additional paths its
credentials occupy. An unknown provider defaults to *not* self-sandboxing.

## 5. MCP server injection

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Delivery channel | reads the agent file; Crew rewrites copies into `<config dir>/mcp-gateway/agents/` and injects stubs per session over `session/new`, which outranks the same-named agent-spec entry (`mcp_gateway/rewriter.py`) | not projected at all (`kas_agents.py`) | reads **no file**; servers must be passed as the `mcpServers` parameter on **both** `session/new` and `session/load` (`acp/client.py`) | reads **no file**; servers would ride the `mcpServers` parameter on both `session/new` and `session/load`, spliced by its own `_is_codex` arm — deliberately not through `ACP_BACKENDS_SESSION_MCP_ARRAY`, whose only member is CC (`acp/client.py`). No hot reload either (`acp_backends.py`) |
| Shape | agent-file JSON | — | different: `env` and `headers` are **required arrays** of `{"name","value"}` and the transport `type` must be explicit (a url with no `type` is routed to the stdio branch and rejected for having no command); omitting either array fails the whole `session/new` with `-32602 expected array, received undefined`, so both are always emitted — empty when there is nothing to carry (`acp/session_mcp.py`) | unverified — no projection exists, so no shape has been exercised. The one established fact is that codex-acp answers an unsupported transport with `-32602` for the WHOLE `session/new` rather than skipping that server, so a wrong shape costs the session (`acp/client.py`) |
| Public-core default | real | — | real: `_session_mcp_servers()` translates the materialized kiro agent spec into the array on every spawn (`acp/session_mcp.py`, `session_mcp_servers`). The spec stays the single source of truth — there is no second, CC-shaped registry — so installing or toggling a server takes effect on the **next session** with no gateway restart. The spec's `tools` references are honoured, so an entry kiro-cli would declare but not mount (every `opt_in` grant, and any hand-narrowed spec) cannot come alive just because the session ran on CC; `type: "registry"` catalog pointers are withheld (CC cannot resolve a registry, and their command/url are placeholders kiro-cli itself ignores); Crew's own `kirocrew-core` / `kirocrew-cron` are re-derived from the managed source (`agent.managed_mcp_spec_entry`) so a stale hand-edited command cannot cost a session its control plane. A missing or malformed spec degrades to the control plane alone, never to a failed spawn | **nothing is projected.** `_codex_session_mcp_servers` returns `[]`, so no entry reaches a codex session from the agent spec. The only entries it gets are the shared MCP gateway's broker stubs, appended for every backend alike and empty when that gateway is off (`acp/client.py`, `mcp_gateway/session_servers.py`). So whether Crew's own control plane arrives is decided by the gateway rather than by anything codex-shaped: with the gateway off a codex session has no MCP tools at all, and with it on the session carries exactly the stubs the overlay wrapped and nothing this harness declared. It stays empty because the projection is unwritten and the adapter's accepted shape unverified, **not** because nobody can reach it: codex is in `BASELINE_SELECTABLE_BACKENDS`, so a public build reaches exactly this. This is the §5 failure the checklist below names, arriving a second time on a second harness |
| Env expansion | Crew reimplements kiro-cli's expander byte-for-byte: unresolved `${VAR}` stays literal, `env:` prefix dropped (`mcp_gateway/rewriter.py`) | — | adapter-side | not applicable — nothing projects a spec, so there is no `${VAR}` to expand on Crew's side |
| Loader strictness | an `mcpServers` entry without a command makes kiro-cli reject the whole agent file, surfacing as "Mode not found" at session time while `agent list`/`validate` still pass (`apps/bridges.py`) | — | frontmatter-scoped | whole-session: `-32602` on an unadvertised transport fails `session/new` outright, not just the offending entry (`acp/client.py`) |
| Auto-approve bypass | `allowedTools` is the one path that never reaches `hooks.on_tool_call` (`apps/bridges.py`) | KAS `rules` array | see §7 | **none reaches the harness** — no `--agent`, no spec, no session MCP array, and not in `ACP_BACKENDS_SEED_LOCAL_SETTINGS`; `allowedTools` is consumed Crew-side by the gate only. Routing is `SESSION_CONFIG`, so `read-only` is applied through `session/set_config_option` before the first prompt and the session is refused otherwise. The residual that does NOT close: ACP v1 cannot require a prompt for a passive READ, so the sensitive-path block never sees this harness's reads, and §4's OS-boundary mask is the compensating control (`acp_backends.py`) |
| Tool-name grammar | `@server/tool` split on `/`, so slash-bearing keys are slugged or expose zero tools (`mcp_utils.py`) | — | `mcp__server`; `fs_read`→`Read`, `execute_bash`→`Bash`; `use_aws` has no equivalent and is dropped (companion) | unexercised — no MCP server is ever injected, so no grammar is reached |

**A provider must declare:** its injection channel and precedence rule, its
server shape, its env-expansion semantics, its loader strictness, which field (if
any) bypasses the permission callback, and its tool-reference grammar.

## 6. Usage, billing, credits

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Unit | Kiro credits (`bills_kiro_credits`, membership-only and False for unknown ids) | Kiro credits | US dollars per token | unknown to this repository. `TurnUsage` carries both `credits` and `cost_usd` and each harness fills what it bills in (`acp/types.py`), and the generic `parse_usage_cost` picks up a `cost: {amount, currency}` on `usage_update` from any adapter that sends one, USD only (`acp/_dispatch.py`). Whether codex-acp sends it is not established here |
| Quota API | RTS endpoints, target prefix `com.amazon.aws.codewhisperer.runtime.AmazonCodeWhispererService` (`dashboard/handlers/kiro_usage_api.py`) | same | none; cost is computed from token counts | none in core — no codex path in `dashboard/handlers/kiro_usage_api.py` |
| Token discovery | `data.sqlite3` across four per-OS locations × two product names, four key spellings (`kiro_usage_api.py`) | same | not applicable | not applicable — `.codex/auth.json` is the adapter's own store, classified on the read-gate floor and never read by Crew, which only checks existence to name a sign-in command (`security.py`) |
| Consumer surface | a boolean flag read by the session readout and `dashboard/state.py` | same | `GET /api/usage/cost` returns `mode="cost"` vs `mode="kiro"`, plus a companion-side budget cap (companion) | none, and this is the one row in this bucket that is a gap rather than a seam: the credit pill's background fetch is not harness-aware — it gates on `kiro-cli` being resolvable, not on the session's backend (`dashboard/handlers/sessions.py`) — so a codex session on a host with kiro-cli installed still shows Kiro credits |

This is the best-sealed bucket: consumers read a flag, not the endpoints.

**A provider must declare:** its billing unit, its quota API (or none), how its
token is discovered, and its unattributable-overhead model.

## 7. Security and permission parity

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Where denial is enforced | Crew's own PreToolUse gate; built-in deny rules are **not** injected into the agent spec (`security.py`) | same | CC has a **native** permission engine that runs *upstream of and invisible to* the host `canUseTool` gate | Crew's own PreToolUse gate — and alone among the four, the routing is **verified before it is trusted**: codex is `Routing.SESSION_CONFIG`, the one mechanism in `ENFORCED_ROUTINGS`, so `_apply_session_permission_routing` refuses the session when `mode=read-only` was not advertised or the write failed (`acp_backends.py`, `acp_tool_gate.py`, `acp/client.py`) |
| Deny-pattern engine | authored for kiro-cli's linear-time RE2-style engine; two patterns are catastrophic under Python `re`, so a behaviourally identical linear matcher is substituted (`security.py`) | same | regex, not globs — Crew translates globs with metacharacter escaping (companion) | same as kiro-cli — the calls reach `HookManager.on_tool_call`, so the built-ins run on Crew's substituted linear-time matcher (`acp_tool_gate.py`) |
| Auto-approve vocabulary | `allowedTools` | `{"rules":[{"capability":"mcp","match":["srv/*"],"effect":"allow"}]}`; unmatched → `ask`; `match` omitted → `['**']` (`acp/kas_permissions.py`) | frontmatter `tools`; `deniedCommands` → `disallowedTools: ["Bash(<cmd>)"]` | **none reaches the harness** — see §5; `allowedTools` is Crew-side only (`acp_backends.py`) |
| Permission-request options | `allow_once` / `allow_always`, with a `cancelled` fallback for deny | kiro vocabulary | `allow` / `allow_always` **and a real `reject`** (`acp/session_handle.py`) | parsed from the ACP spec `kind` vocabulary (`allow_once` / `allow_always` / `reject_once` / `reject_always`, plus legacy aliases) with no codex-specific mapping (`acp/_dispatch.py`). Which ids codex-acp actually emits is unobserved |
| Auto mode | protocol flag | protocol flag | a per-session **file**: `<work dir>/.claude/settings.local.json`, `permissions.defaultMode`, written by `_write_claude_local_settings` for the *next* spawn. The work dir is frequently a project the user also uses with CC by hand, so the writer **snapshots** any pre-existing file (bytes plus mode) and merges Crew's keys over it; on reset the snapshot is restored byte-for-byte, or the file is removed when Crew created it. Either way no `bypassPermissions` Crew asked for survives a crash, and a file Crew never seeded is left alone (`acp/types.py`, `acp/client.py`) | **none, and deliberately unreachable.** `mode` is written once at session init to `read-only` and nothing rewrites it; the only later `set_config_option` calls are `model` (`acp/client.py`). Approval breadth is Crew's gate, not a harness mode |
| Inherited-config hazard | — | omitting `permissions` resolves everything to `ask` | an inherited `defaultMode: dontAsk`, or any inherited `allow`/`ask` wildcard, is auto-approved by CC's engine **without calling `canUseTool`**, silently bypassing Crew's gate — so `defaultMode`/`allow`/`ask` are stripped from the seed (companion) | the adapter reads the operator's own `~/.codex/config.toml` (`CODEX_HOME`) and Crew neither strips it nor reads it back. What a session **cannot** inherit is a looser permission mode, because `read-only` is asserted per session rather than seeded to a file (`acp_backends.py`) |
| Rules Crew cannot override | — | shell/filesystem families are refused rather than translated | the user's own deny rules run first; a detector fnmatches them against benign canaries (`ls`, `git status`, `pwd`) and **surfaces** what it cannot beat (companion) | **passive reads.** ACP v1 has no way to require a prompt for a read, so `read-only` still permits them and the sensitive-path block never sees them. Compensated at the OS boundary by §4's derived mask, and `enforce_sandbox_floor` refuses the session outright when that mask would not be applied. The cost is stated rather than hidden: `.ssh` is masked, so git-over-SSH stops working, and there is no local opt-out (`acp_tool_gate.py`) |
| Tool-call titles | prefixed `Reading `/`Running: ` | prefixed | no prefix, so sensitive-path gates must run on every target (`hooks.py`) | unobserved whether codex-acp prefixes them; the gate does not depend on it, running every check on every target (`hooks.py`) |

**The parity rule this bucket establishes:** when a foreign host lacks an
enforcement *mode* rather than a rule, parity cannot be reached by translation.
kiro-cli's 42 "suspicious bash" patterns are audit-only; CC has no audit-only
mode, so they are deliberately **not** translated and the gap is recorded as a
known security gap rather than silently downgraded (companion). The
honest contract is a declared capability plus a documented gap. Note what CC's
selectability does to the rest of this bucket: every row marked (companion) —
the glob-to-regex translation, the stripping of an inherited `defaultMode`, the
unbeatable-rule detector — is protection a public build does **not** have, and the
inherited-config hazard row above is the one to read first, because it is where
CC's own engine can auto-approve without ever calling Crew's gate.

**A provider must declare:** where denial is enforced relative to the host gate,
its regex-engine class, its auto-approve vocabulary and default-when-absent
semantics, its permission-option vocabulary, how auto mode is expressed and
whether it is spawn-scoped, which of its own rules the host cannot override, and
whether tool titles carry a verb prefix.

## 8. Auxiliary runtimes the host cannot self-discover

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Primary binary | `kiro-cli`, resolved by `_resolve_kiro_bin` | `kiro-cli` | `claude-agent-acp`, resolved via vendored `node_modules` / mise / PATH (`acp/client.py`) | `codex-acp`, resolved by `_resolve_codex_acp_bin` on the same ladder as CC's: `CODEX_ACP_BIN` override, then a packaged or project-local `node_modules` (only with the `@agentclientprotocol/sdk` marker beside it), then mise, then an augmented `PATH`. The `codex` CLI does **not** serve ACP — it reads `acp` as a prompt (`acp/client.py`) |
| Additional runtime | none | none | a **second** native binary (~250 MB) that the adapter's SDK will not find itself; Crew injects `CLAUDE_CODE_EXECUTABLE` into the child env (`acp/client.py`) | **none** — one component, not two: the adapter ships a compatible Codex binary as an npm dependency, and there is deliberately no `CODEX_PATH` constant (`agent_sdk/backend_install.py`, `acp/client.py`) |
| Failure mode when missing | resolution error | resolution error | reported **before** a session by the install probe, which names which of the two halves is absent and, for the adapter, the `npm i -g` that fixes it (`agent_sdk/backend_install.py`); a session started anyway still dies at `session/new` with "Claude native binary not found" after a warning log (`acp/client.py`) | reported **before** a session by `_probe_codex` as `missing`, naming `codex-acp` plus the `npm i -g` that fixes it, with `restart_required` when it resolves now but the process cached a negative; a session started anyway dies at spawn with "`codex-acp` not found … The 'codex' CLI alone does not serve ACP." Credentials are deliberately not probed (`agent_sdk/backend_install.py`, `acp/client.py`) |

The probe's answer is deliberately three-valued, and the third value is not
padding: a resolver that *fails* reports `unknown`, never `missing`, because
telling an operator to install what they may already have is the worse error. It
also discloses one skew it cannot fix — the adapter resolves on disk now, but the
running gateway already cached its absence for the process's lifetime, so the row
reads `installed` with `restart_required` rather than promising something that
then fails.

**A provider must declare:** every runtime it needs beyond its own entry point,
how each is discovered, and how a missing one is reported *before* a session is
attempted rather than during it. CC is the one backend that now answers the third
part, and its answer is the shape a new provider should copy: a probe that asks
through the spawn's own resolvers, one row per component so a half-install is
distinguishable, and a remedy named only where this repository actually
establishes one — the `claude` CLI reports an empty install command rather than an
invented one.

## 9. Tool-result text fidelity (control markers)

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| How a tool result arrives | `content[].content.text` blocks, or `rawOutput.items[].Text` / `.Json.stdout` | `content[].content.text` blocks, or a **flat `rawOutput` object with no `items`** (measured: `{output, exitCode, message}`, `{response, imageBase64Urls, message}`, `{kind, retracted}`) — and an MCP result can arrive as an **already-serialised** JSON envelope under a key this repository does not recognise | `content[]` text blocks | unmeasured — no codex tool-result shape is recorded. Codex adds no builder of its own and rides the shared `_build_tool_result_event` path, which handles `content[].content.text`, `rawOutput.items[]`, and any other non-empty `rawOutput` object by serialising it (`acp/_dispatch.py`) |
| Marker survives untouched | yes | **no** — the envelope reaches the consumer through `json.dumps`, which escapes every quote in it | yes | unmeasured, and not asserted either way |
| Recovery | not needed | `acp/_dispatch._repair_escaped_marker`, run over the joined output before redaction and the head cut | not needed | already unconditional, per builder rather than per backend: `_repair_escaped_marker` runs over the joined output before redaction and the head cut (`acp/_dispatch.py`), so the ratchet below covers a codex builder added later by construction |
| Same text in several fields | no | **yes** — `{response, …, message}` carries the result TWICE, so one directive raises the frame's marker count to 2 | no | unmeasured — no codex tool-result frame has been recorded (see the row above) |

Duplication is why the recovery deduplicates by VALUE rather than counting sentinels. The refusal to guess between two markers is real — applying the wrong directive is worse than applying none — but it must fire on two DIFFERENT payloads, not on one payload delivered twice. It fired on the duplicate, so `_repair_escaped_marker` returned nothing, `peek` could name no selector, and the gateway-parked record went unclaimed: every `monitor_start` from a KAS-backed session was acknowledged to the model and armed no loop. The envelope branch now takes the single DISTINCT marker-bearing string, and additionally requires that string to hold exactly one sentinel, because the whole-text refusal it runs ahead of is what used to cover the two-directives-in-one-field case.

`rawOutput` is unstructured passthrough, so `items[]` is one producer's wrapper
rather than a contract. `_build_tool_result_event` therefore serialises any other
non-empty `rawOutput` object instead of reading it as "no output": treating an
unfamiliar shape as absent discarded the whole `EVENT_TOOL_RESULT`, which is the
event that writes both `meta["output"]` and `meta["done"]` for the pill — losing
the Output tab outright, and leaving `done` to `chat_runner`'s post-tool text
sweep, which only fires when assistant text follows the tool group. That third
path joins its part into the same string the recovery row above runs over, so it
needs no separate marker handling.

Two consumers read a control marker out of the tool-result TEXT rather than out
of a structured field: a session directive (`session_directive.peek` — how
`monitor_start` / `monitor_update` / `autonudge_stop` reach the session that owns
the loop) and an MCP App render marker (`mcp_apps_render.find_marker`). Both
sentinels are quote-free, so JSON escaping leaves them perfectly intact while
mangling the payload behind them. The failure that produces is silent and
expensive: the frame still looks like it carries a directive, the consumer can no
longer name the record the MCP stub parked, the tool answers "requested", and no
loop arms. It cost several gateway restarts to find on KAS precisely because
every layer looked healthy.

The recovery is keyed on the sentinel, not on any envelope field name, because
the field differs per backend; and acceptance is the test — a candidate is used
only when `peek` actually reads a selector from it, so a wrong guess degrades to
the original text instead of substituting something worse. A frame carrying two
DIFFERENT markers is refused rather than resolved to the first, since applying
the wrong directive is worse than applying none.

**Nesting depth is chosen by the tool, so the recovery degrades rather than
raises.** The envelope's non-marker fields are copied by an ITERATIVE walk
(`_elide_marker_value`, an explicit heap stack) because a recursive one raises
`RecursionError` — a `RuntimeError`, outside the `(ValueError, TypeError)`
handlers on this path, so it escapes `parse_session_update` and aborts the whole
turn. `json.dumps` of that copy is a second, independent ceiling: it recurses in C
against the process stack, so the depth it refuses at is a platform property (a
branch that encodes on Linux raises on Windows). When it refuses, the copy
degrades PER FIELD — every top-level field that encodes is kept and only the
offending ones become `UNSERIALISABLE_SIBLING_VALUE` — because the two losses are
not symmetric: a dropped directive silently unarms a loop the model was told was
armed, while dropped sibling detail costs transcript content the user can see is
missing. The directive is emitted even when no sibling copy survives at all.

**The refusal posture covers every encode on the dispatch path, not just the
marker branch.** The permission event's cache-miss input fallback, the initial
`tool_call` input, the two non-marker tool-result branches and the refinement
input all serialise backend-shaped payloads through a refusal-guarded encode
(`_dumps_degraded`): an encoder refusal degrades that one frame to readable
text — `str(payload)` for a `(TypeError, ValueError)` refusal, the
`UNSERIALISABLE_SIBLING_VALUE` placeholder for a `RecursionError` — instead of
propagating and aborting the turn. A payload that encodes today keeps its exact
rendering; only the frames that previously killed the turn change.

**A provider must declare:** whether its tool-result text arrives verbatim or
pre-serialised, and — if any builder it adds can emit an `EVENT_TOOL_RESULT` —
that the builder runs the repair. This is the one bucket in this document with a
ratchet instead of a checklist line: `test_session_directive_transport.py` walks
every `AcpEvent(kind=EVENT_TOOL_RESULT)` construction under `acp/` and fails when
one of them does not call `_repair_escaped_marker`, because a provider author is
exactly the person who will not know this constraint exists.

It is no longer the only ratchet in this document, but it remains the only one
that enforces a *behaviour*. The column gate added with the Codex column enforces
only that a column exists — see the checklist below for what that does and does
not buy.

## The KAS backend, Crew side

`agent.acp_backend = "kas"` selects the second, adapted ACP backend; the default
and first-class path stays `kiro-cli`, and `agent.provider` remains `"acp"` —
the harness is never the provider selector (see
[harness-parity.md](harness-parity.md)). Only the Kiro Crew-side integration is
documented here: that backend is not open source, so its wire shapes, storage,
auth and process internals are out of scope, and every place its signals differ
from `kiro-cli`'s is absorbed in one Crew module so a change there is a one-file
edit.

- It runs through the existing `AcpRuntime` (one process, multiplexed sessions)
  via the established backend seam — no runtime subclass, just a spawn-argv
  branch plus adapters. `kiro-cli` keeps its own spawn path, per-harness
  handshake literals and session machinery unchanged (harness-parity H9/H10).
- Backend-specific parsing of the display and telemetry frames whose shape
  differs from `kiro-cli`'s is localized in `acp/kas_wire.py`. Backend-neutral
  logic — the context-meter math — lives on `AcpPromptStats` and is shared by
  both paths, so they cannot drift.
- Capabilities the session layer reads off a provider are declared on the
  `LLMProvider` ABC with safe defaults (harness-parity H14), so the adapted
  backend never forces a `getattr` probe onto the `kiro-cli` path.
- Every "is this the `kiro-cli` harness" test is a positive comparison against a
  named constant or membership in a named `ACP_BACKENDS_*` set, never
  `not is_<other>`, which would hand a branch to a later harness by default. The
  full invariant catalogue and its CI gate are in
  [harness-parity.md](harness-parity.md).

Switching backends is a config write plus a restart, and affects only new
sessions; `restart` reaps the prior runtime process, and `kirocrew doctor` reports
the selected backend's readiness.

```
kirocrew config set agent.acp_backend kas   # then: kirocrew restart
kirocrew config set agent.acp_backend ""     # back to kiro-cli; restart
```

Three KAS parity items are deferred: hooks are not wired for it, `/clear` maps to
a `kiro-cli`-only notification and so is a no-op there (a local reset is the
intended fix), and an alternative transport mode is out of scope pending its own
design and review.

## Seam status today

| Bucket | Seam |
|---|---|
| 6 Usage / billing | real — consumers read a boolean flag |
| 7 Permission vocabulary | real — `acp/kas_permissions.py`, shared by the wire projection and the on-disk writer so they cannot drift |
| 1 Agent definition | partial — `acp/kas_agents.py` is a genuine projection; the *writer* (`agent.py`) has none |
| 3 Auth-store reading | weak — the projection is isolated, the paths and table names are inline constants |
| 4 Sandbox delegation | weak — one decision function, a hardcoded predicate |
| 2 Session persistence | **none** — the path is spelled literally in at least four modules |
| 5 MCP injection | **none** — an overridable method returning `[]` is the whole extension point, and now that CC is selectable that neutral default is what a public build actually runs |
| 7 Regex-engine parity | **none** — the deny catalog is authored against one engine |
| 8 Auxiliary runtimes | partial — `agent_sdk/backend_install.py` is a real preflight that names each absent component before a session, but the *requirement* is still declared nowhere a type checker can see: the probe knows CC needs two binaries because it was written to, not because CC declared it |
| 9 Tool-result marker fidelity | real — one recovery function, and the only bucket whose requirement a test enforces rather than a comment asserting it |

## New-provider checklist

A provider author must answer every "must declare" line above. Where the answer
is **"not supported"**, that is a valid declaration and the corresponding Crew
surface degrades rather than assuming. Silence is not an answer: the two failure
modes observed on the CC seam are a missing MCP override yielding a session with
zero tools, and a missing settings seed silently collapsing the context window
from 1M to 200K — both documented as comments rather than enforced by an
interface. Neither is hypothetical any more. CC is selectable on a public build,
so a public build reaches both, and a comment is not a thing an operator can
read.

Codex then showed that "silence is not an answer" was itself only a comment. It
landed selectable, with a column in none of the nine buckets, and the §5 failure
recurred unchanged: `_codex_session_mcp_servers()` returns `[]`, so a public
build serves a harness onto which none of Crew's own control plane is projected.
The same defect, on a second harness, one document section after it was written
down. That is the argument for a gate rather than a stronger sentence.

`test_agent_host_contract_parity.py` is that gate, and its scope is deliberately
narrow. It parses the header row of every bucket table above, resolves each
column label through the Column-meaning table to a backend constant, and fails
when the resolved set is not exactly `ACP_BACKENDS_KNOWN`. So a new id cannot
reach `ACP_BACKENDS_KNOWN` without a column here.

What it does **not** do is judge a cell. A column of "unknown" passes it. That is
the honest limit of a text gate, and the reason the checklist above still matters:
the gate makes the omission visible, a reviewer makes it answered. Where an
answer genuinely is not known yet, write that rather than a guess — several Codex
rows say "unmeasured", and an unmeasured row a reader can see beats a confident
row that is wrong.

Bucket 9 remains the exception worth copying, because it is the one requirement
enforced by *behaviour* rather than by text: a new builder that drops the marker
recovery fails a test instead of shipping a monitor loop that never arms. A
bucket whose requirement can be expressed that way should be.

## What supporting one foreign host costs today

Because none of the buckets above is a typed contract, the public core carries the
CC host seam as **live** conditional surface: reachable on a plain build, and
still driven by holes that nothing declares. CC being selectable does not retire
any of the entries below — it retires only the argument that nobody can reach
them. The `getattr` seams, the neutral-return overrides and the sentinels are on
the path a public build takes when an operator picks Claude Code.

| Kind | Count |
|---|---|
| `getattr`-by-name seams whose target the public core never defines | 2 (`acp/client.py`) |
| Defensive attribute probes across the provider boundary | 4 |
| Methods returning a neutral value purely for a companion to override | 6 |
| `ClaudeCodeProvider is not None and isinstance(...)` guards against a name hard-coded to `None` | 11 sites, 2 sentinels (`session.py`, `subagent.py`) |
| Comment clusters naming the companion or a deleted module as the supplier | 19 |
| Refusal / downgrade mechanisms | 9, including the degrade log in `acp_backends.resolve_selected_backend()` and five capability non-memberships |
| Live `_is_claude` branches inside `acp/` | 19 |
| CC-symbol lines in `src/kiro_crew` | 146 (352 including `test/`) |

The counts are a point-in-time audit and drift with every edit to the surface they
measure; the symbols are the durable reference, and a reader checking a number
should re-derive it rather than trust it. For the same reason this document cites
modules and symbols, never line numbers.

Codex was the first backend added after that census, so it is the one datapoint
on whether the cost above is inherent or historical, and it splits. On mechanism
it is the counter-example: it added no `if` chain but a `Routing` enum plus two
identity-keyed tables read through accessors that fail closed on an unknown id,
and enforcement dispatches on the mechanism (`ENFORCED_ROUTINGS` holds `Routing`
members) rather than on the harness — so a future backend implementing the same
routing opts in with one table entry. On declaration it repeats the pattern
exactly: its capability gaps live in frontend prose behind a plain
`if (value === CODEX)`, nothing puts them on the wire, and a third harness with a
caveat adds a third `if`. Mechanism improved; declaration did not.

The registration seam itself is coherent —
`ProviderRegistry.register_acp_backends` / `create_factory`
(`platform/interfaces.py`), a documented no-op default
(`platform/defaults.py`), one wiring site (`platform/bootstrap.py`),
and an explicit rule that the core never imports the companion. Its *purpose* has
narrowed, though: with the baseline covering every known backend, an edition needs
it only for a harness the core does not ship at all, not to make a shipped one
reachable. Everything below it is still incoherent: the behaviour a companion must
supply is delivered through three different kinds of undeclared hole, none
type-checked and none failing loudly when omitted.
