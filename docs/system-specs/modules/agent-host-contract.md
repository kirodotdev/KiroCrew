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

This bucket is a **declared seam**, and the declaration is the whole of it. One
frozen `AgentAuthDeclaration` per harness in `agent_sdk/host_auth.py` is the only
place a harness's auth is written down, and every host layer that has to agree
about a credential reads a projection of it rather than keeping its own copy: the
read-gate floor and its override anchoring (`security/paths.py`), the sandbox
credential mask and the one leaf it spares (`agent_sdk/tool_gate.py`), the
the logout-recycle answer `backends_retired_by_host_logout()`, the `AcpAuthRequired` text
raised at every sign-in failure (`acp/session_provider.py`, `providers/acp.py`,
`acp/client.py`), and the `auth` object on `GET /api/acp-backends`
(`dashboard/handlers/acp_backend_status.py`).

The split is a security property rather than a tidiness one. A harness declares
what it **stores**; this host still decides what is **fenced**. No field names a
path to leave open in general — `adapter_own_leaves` may only name a leaf the same
declaration already put ON the floor, and `AgentAuthDeclaration.__post_init__`
refuses a declaration that tries otherwise, raising at import rather than reaching
a floor that fences less than its author believed.

The rows below are the declaration's own fields, so a harness answers this bucket
by writing one literal. Leaves are home-relative and authored with POSIX
separators; the floor re-joins them for the running platform.

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| `entitlement_source` | `host_identity_store` — `kiro-cli login` writes it; org SSO is `--use-device-flow --license pro` (`kiro_prerequisite.py`) | `host_identity_store` — the same store. Spawned as `kiro-cli acp --agent-engine v3 --auth-method cli`, and that flag is the demonstration: the relay resolves every access token from kiro-cli's own store | `own_credential_file` — brings its own sign-in, and Crew implements no login command for it | `own_credential_file` — brings its own sign-in, and Crew implements neither a login command nor an auth probe |
| `credential_leaves` | none of its own. The locations are the HOST's, declared by `identity_stores.IDENTITY_STORE_ROOTS` and spliced onto the floor from there; a harness re-declaring them would hand a driver a say over the host's own store. The store is projected, never copied: identity tables plus `migrations` rows plus selected `state` rows (`kiro_prerequisite.py`) | none of its own — same store, nothing further to fence | `~/.claude/.credentials.json`. Its credential-refresh **command** is named inside its own `settings.json` and copied **verbatim** into the isolated seed at `0o600`; dropping it breaks auth outright, so the seed cannot simply be emptied (companion) | `~/.codex/auth.json`. Crew never reads or copies it and only ever checks that it exists |
| `home_override_env_vars` | none | none | `CLAUDE_CONFIG_DIR`, `CLAUDE_HOME` | `CODEX_HOME` |
| `adapter_own_leaves` | none | none | none — outside `tool_gate.ENFORCED_ROUTINGS`, so no credential mask is applied and there is nothing to carve an exception out of. Declaring one anyway would assert something about a control that never runs | `~/.codex/auth.json` — the adapter authenticates ITSELF, so a mask that took its own token away would fail every session at start with an opaque authentication error. The asymmetry is safe: the read gate still refuses that leaf to the AGENT's file tools, so the two controls cover different readers rather than cancelling each other |
| `host_logout_retires_children` | `True` — a logout there invalidates a running child | `True` — excluding it would let a KAS session keep serving turns on the previous account's credentials | `False` — a live CC child must survive `kiro-cli logout` | `False` — a `kiro-cli logout` says nothing about whether a running codex child is still authenticated, and retiring its child on that signal would end a live turn for no reason |
| `sign_in_remedy` | run `kiro-cli login`, then start a new chat | the same string verbatim — it is kiro-cli's store the relay reads | run `claude` and complete its sign-in, then start a new chat | two branches: complete Codex's own sign-in, or name a model provider in `~/.codex/config.toml` (`CODEX_HOME` moves that folder). Neither is checked here — the adapter reads them |
| `AgentInteractiveLogin` | not implemented — sign-in happens in the operator's own terminal | not implemented — same | not implemented — the harness brings its own | not implemented — the harness brings its own |
| Entitlement discovery, model side | account API | account API | runtime, from the advertised model set at session init; the registry is filtered down to it (`dashboard/handlers/agents.py`) | runtime, from the model set advertised at `session/new` / `session/load`, but in-memory for that session only — no account API, no registry filter, no cross-session persist (excluded from `ACP_BACKENDS_ADVERTISED_MODEL_SELECTION`) (`acp/client.py`) |
| Readiness probe | `--version` then `whoami`, inside the OS sandbox (`kiro_prerequisite.py`) | same | binary resolution only, but for **both** components and through the spawn's own resolvers, so the answer cannot disagree with what a spawn does (`agent_sdk/backend_install.py`, `agent_sdk/drivers/acp.py`) | one component, adapter resolution only and through the spawn's own resolver: `codex-acp`, reported `installed` / `missing` with the install command, plus `restart_required` when this process cached a negative (`agent_sdk/backend_install.py`). No auth check |

Installed and signed-in are separate questions with separate remedies, which is
why the last row answers only the first. `agent_sdk/backend_install.py` probes no
credential on purpose: reading another harness's token is what the floor exists to
forbid, so a probe that did it would be the one reader the floor cannot fence.

**A provider must declare:** one `AgentAuthDeclaration` — its entitlement source
(`host_identity_store` or `own_credential_file`), the
credential leaves it stores, the `$HOME`-override variables that relocate them,
the one leaf its own child must still read, its operator-facing sign-in remedy,
and whether a host logout may retire its live children. It does not declare a
file to re-expose inside a directory the mask hides: a re-exposure is an edit to the
mask, and the mask is host-owned, so that table stays in `agent_sdk/tool_gate.py`
(`ADAPTER_EXPOSED_CREDENTIAL_LEAVES`). It also declares,
structurally, whether it offers an in-product sign-in: `AgentInteractiveLogin` is
a `runtime_checkable` protocol tested with `isinstance`, never a boolean, so a
harness that has no flow is a shape the type system can see rather than one that
answers a flag and then no-ops. And it declares its readiness probe, which is
about files rather than credentials.

Silence is not an answer here, and unlike most of this document that is enforced:
`test_agent_sdk_host_auth.py` fails when a member of `ACP_BACKENDS_KNOWN` has no
declaration, and `missing_declarations` names the gap rather than asserting a
length. The failure mode it closes is a harness reaching
`BASELINE_SELECTABLE_BACKENDS` without touching the credential floor, and serving
sessions with a live agent-readable token that nothing fences.

The logout-recycle answer is `backends_retired_by_host_logout()`, and its
meaning is *authorization*, not ownership: it records that a `kiro-cli logout` may
retire this backend's live child. It is the projection of
`host_logout_retires_children` over the declaration table, defined in
`agent_sdk/host_auth.py` and re-exported by `acp/types.py` and `acp_backends.py`.
It is a FUNCTION and not an `ACP_BACKENDS_*` set: that naming is harness vocabulary,
whose home is `agent_sdk/backends.py` and which the harness-parity gate enforces, and
a derived answer is not vocabulary. Nor could the set sit beside the other capability
sets in `agent_sdk/backends.py`,
because that module supplies the backend ids the declaration table is keyed by, so
importing the table from there would close a cycle. Membership is positive and
derived from a declared fact rather than from "not claude", the exclusion is
load-bearing, and the inconsistent pair is refused outright: a declaration that
claims `host_logout_retires_children` while resolving its entitlement anywhere
other than the host identity store raises, because a logout says nothing about a
store the harness never reads.

## 4. Sandbox

| | kiro-cli | KAS | CC | Codex |
|---|---|---|---|---|
| Self-sandboxing | yes, ≥ 2.13, via `~/.kiro/settings/amazon-internal.json` key `"sandbox"` (`sandbox.py`) | KAS-owned Seatbelt/Bubblewrap, not passed by kiro-cli | **no** — "a Node or Python harness does not qualify" (`acp/types.py`) | **no** — a Node adapter; the sandbox modes it can apply are in-process policy, not an OS sandbox Crew's could nest inside (`acp_backends.py`) |
| Interaction with Crew's sandbox | mutually exclusive on macOS: internal ON → Crew seatbelt OFF, because seatbelt cannot nest (`sandbox.py`) | Crew seatbelt remains the sole isolation layer | Crew keeps its own wrap | Crew keeps its own wrap, and here it is load-bearing rather than incidental: the wrap carries the credential mask that compensates for the ungated passive read, so a session that would spawn UNWRAPPED is **refused** before the spawn (`acp_tool_gate.py`, `acp/client.py`). The test is whether the mask will be applied, keyed on the EFFECTIVE tier, so a governance floor raising `off` still qualifies (`sandbox.py`) |
| Delegation predicate | `argv[0]` basename is literally `kiro-cli` (`sandbox.py`) | same binary | never delegates | never delegates — not in `ACP_BACKENDS_INTERNAL_SANDBOX`, so `is_kiro_cli` is False and neither the macOS seatbelt skip nor the Windows Kiro-only delegation is granted |
| Extra hidden paths | core tiers | core tiers | adds `.midway`/`.ada`/`.aws`/`sso`/`.krb5`, ADD-only on both tiers (companion) | core tiers **plus the whole read-gate floor, derived rather than enumerated**: `sandbox_credential_targets` re-projects every `_SENSITIVE_HOME_DIRS` leaf with the gate's own override anchoring, minus `.codex/auth.json` — the one leaf the adapter must read to authenticate. `~/.aws` stays hidden on both platforms and only `.aws/config` is re-exposed read-only (`adapter_expose_files`, the cc tier's `_CC_EXPOSE_FILES` posture) — a restored 0444 copy on Linux, a `require-not (literal)` carve-out of the subpath deny on macOS — so `credentials` and the SSO cache remain masked (`acp_tool_gate.py`). Accepted residual, shared with cc's `_CC_EXPOSE_FILES`: static `aws_access_key_id`/`aws_secret_access_key` lines an operator writes INTO `config` are readable through the carve-out on both platforms (Seatbelt exposes the real inode, so the Linux copy is kept byte-identical rather than redacted on one platform only); the launcher reads `config` with the same plain `open()` the cc tier has always used, so a symlink, hardlink or bind alias the operator plants there is followed as it is for Claude Code today — the child cannot plant one (`~/.aws` is bind-masked in its session and the agent's file tools are fenced from it), so this is the operator exposing their own store, the same pre-existing residual, not a boundary the child can cross. Ungated `.ssh` arrives with it, so git-over-SSH inside such a session stops working; accepted rather than worked around, and there is no local opt-out |

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
| How a tool result arrives | `content[].content.text` blocks, or `rawOutput.items[].Text` / `.Json.stdout` | `content[].content.text` blocks, or a **flat `rawOutput` object with no `items`** (measured: `{output, exitCode, message}`, `{response, imageBase64Urls, message}`, `{kind, retracted}`) — and an MCP result can arrive **already-serialised**, **copied into several fields**, one field **replaced by an offload reference** above a size threshold, and every string **capped at 30k chars** (head 500 + tail 500) | `content[]` text blocks | unmeasured — no codex tool-result shape is recorded. Codex adds no builder of its own and rides the shared `_build_tool_result_event` path, which handles `content[].content.text`, `rawOutput.items[]`, and any other non-empty `rawOutput` object by serialising it (`acp/_dispatch.py`) |
| Directive marker readable in the result | yes | **no**, and it does not need to be | yes | unmeasured, and it does not need to be |
| How the tool call's `rawInput` arrives | complete on `tool_call` | complete on `tool_call`, uncapped (`tool-call-emitter.ts` passes it around its wire cap (kiro-agent `tool-call-emitter.ts`)); carries a `_meta` block the MCP server never sees | empty on `tool_call`, complete on `tool_call_update` | unmeasured — rides the shared `_build_tool_call_event` / `_build_tool_refinement_event`, which record `raw_tool_params` from whichever frame carries it |
| Wire title of an MCP tool call | `_meta.kiro` identity, title not needed | `Running: @<serverName>/<toolName>` (measured; the `Running: ` prefix is the backend's, and `directive_tool_from_call` strips one) | raw Claude tool name `mcp__<server>__<tool>` | unmeasured — a codex MCP call whose title matches neither spelling above resolves to no directive tool and records no digest; declaring the spelling is part of onboarding the backend |

`rawOutput` is unstructured passthrough, so `items[]` is one producer's wrapper
rather than a contract. `_build_tool_result_event` therefore serialises any other
non-empty `rawOutput` object instead of reading it as "no output": treating an
unfamiliar shape as absent discarded the whole `EVENT_TOOL_RESULT`, which is the
event that writes both `meta["output"]` and `meta["done"]` for the pill — losing
the Output tab outright, and leaving `done` to `chat_runner`'s post-tool text
sweep, which only fires when assistant text follows the tool group.

**A session directive is never selected from the result body.** One consumer
still reads a control marker out of the tool-result TEXT: the MCP App render
marker (`mcp_apps_render.find_marker`), and it is re-attached under the result cut
for that reason. The session-directive marker is display-only. On kiro-cli the
directive is applied from the marker under the verified `_meta.kiro` identity,
exactly as before. On a backend that emits no `_meta.kiro`, the tool has already
reported its CALL to the gateway out of band — its own name and the raw `tools/call`
arguments, never the payload. The gateway re-runs that tool on those arguments
(`mcp_core.derive_directive`, with `_emit_directive` in capture mode) to derive the
record's payload itself and computes the claim key
`session_directive.call_input_digest(tool, raw_args)` itself, so a caller who can
reach the route controls only what the named session's own call would produce and
cannot pair a chosen payload with the key of some other call. The derivation runs
AS the request's kernel-verified `X-Session-Key`, installed as the call's
`CallerContext` (the identity gatewayd injects for a pooled backend), so the tools
that refuse without a strict identity — `monitor_watch`, `monitor_stop`,
`monitor_update` — and the tools that refuse a non-nudgeable session — a `cron:`
key — behave exactly as they would in the stub. The consumer computes the same
digest from the `tool_call` frame (a `tool_call_update` carrying `rawInput` or the real title refreshes it)
and claims the record by that key during the same turn. The name half comes
from `session_directive.directive_tool_from_call`: the trusted `_meta.kiro`
identity where a backend emits one, else the wire title: `@kirocrew-core/<tool>`
as kiro-agent's MCP wrapper stamps it, behind the backend's own `Running: `
prefix (KAS; a recorded frame read `Running: @kirocrew-core/ask_question`), or Claude's raw tool name
`mcp__kirocrew-core__<tool>` as claude-agent-acp passes it through (CC, whose
`toolInfoFromToolUse` has no MCP case). Both the shared `_dispatch` builders and
the legacy `AcpClient` builders that serve CC set `AcpEvent.wire_title` from the
backend's own field; the display `title`, which `select_tool_title` fills from a
shell call's model-authored `rawInput.description`, is never read for this. A call that resolves to
no directive tool records no digest. The name is in the key because every
no-argument tool hashes `{}` alike: an args-only key let a planted
`reset_conversation({})` be claimed by the victim's `resource_status({})` frame. Neither side reads the result, so a backend may
re-serialise, duplicate, offload or cap the result body and the directive still
lands. The claim is attempted whenever the frame carries a marker OR a record is
parked for the session and the frame's call has a digest, so a result whose
marker was capped off entirely still arms. Display is separate: `strip_marker`
cuts a tail-anchored marker to the end of the text as before, but a marker embedded
inside a serialised envelope is replaced in place so the envelope stays valid JSON
in the transcript instead of being truncated mid-string. Two edges are pinned by
`test_session_directive_input_digest.py`: an explicit empty `rawInput`
(`reset_conversation({})`) is an argument set and produces a digest, so the
parser reads the first PRESENT input key rather than the first truthy one, while
the permission event's trusted-params cache stays truthy-gated; and when a native
sub-agent call in the same turn shares the parent frame's digest (two no-argument
tools both hash `{}`), the parent frame refuses to claim rather than guess whose
record it is, leaving the child's own frame to retire it on the isolation path.

This replaces two generations of result-body repair (#8182 for the escaped
envelope, #8841 for the duplicated field) that lived in the ACP parser shared by
every backend, each one a branch a backend could invalidate with its next
release. It cost several gateway restarts to find on KAS precisely because every
layer looked healthy: the frame still looked like it carried a directive, the
tool answered "requested", and no loop armed.

**Why the input digest is not weaker than the marker selector it replaces.** Both
are model-controlled content bound to the session by arriving on that session's
own event stream in the call the tool served. A caller who can park a record for
another session still needs THAT session's model to make a call with identical
arguments in the same turn — the bar the marker set. The applied payload is
always the record's; the digest only picks which record.

**The refusal posture covers every encode on the dispatch path.** The permission
event's cache-miss input fallback, the initial `tool_call` input, the tool-result
branches and the refinement input all serialise backend-shaped payloads through a
refusal-guarded encode (`_dumps_degraded`): an encoder refusal degrades that one
frame to readable text — `str(payload)` for a `(TypeError, ValueError)` refusal,
the `UNSERIALISABLE_SIBLING_VALUE` placeholder for a `RecursionError` — instead of
propagating and aborting the turn.

**A directive tool's handler must be pure up to its directive.** Gateway-side
derivation re-runs the handler in the gateway process on every directive, so any
side effect other than the directive it publishes — a notification, a file
write, a counter, an audit row — happens twice: once in the stub for the model's
call and once in the gateway for the derivation. The derivation path already
bypasses the shared SEL invocation logging (`_call_tool_body` in capture mode
runs validation and the handler directly rather than through
`call_tool_with_logging`, and `test_gateway_derivation_writes_no_audit_row`
pins that the replay adds no row to the stub's one). Adding a tool to
`DIRECTIVE_TOOLS` therefore means: the handler validates, encodes and returns —
`_emit_directive` is its only write. A handler that needs another effect must
perform it from the CONSUMER when the directive is applied, not from the tool.

**A provider must declare:** on which frame (`tool_call` or `tool_call_update`)
its complete `rawInput` arrives, and whether it is capped. A backend whose
`rawInput` never arrives cannot carry the out-of-band control plane; the consumer
logs `session-directive NO CALL INPUT` for it. `test_session_directive_input_digest.py`
drives the real consumer with every KAS result shape observed so far and requires
each to arm with the marker unreadable.

That test enforces a *behaviour*; the column gate added with the Codex column
enforces only that a column exists — see the checklist below for what that does
and does not buy. A new backend also declares the wire-title spelling of its MCP
tool calls (the last row of the table), since `directive_tool_from_call` resolves
only the spellings it knows and an unknown one records no digest.

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
| 3 Identity and auth | real — one `AgentAuthDeclaration` per harness (`agent_sdk/host_auth.py`); the floor, the sandbox mask, the own-leaf exclusion, the logout-recycle set, the auth-required message and the panel caveat are all projections of it, and a known backend with no declaration fails a test |
| 1 Agent definition | partial — `acp/kas_agents.py` is a genuine projection; the *writer* (`agent.py`) has none |
| 4 Sandbox delegation | weak — one decision function, a hardcoded predicate |
| 2 Session persistence | **none** — the path is spelled literally in at least four modules |
| 5 MCP injection | **none** — an overridable method returning `[]` is the whole extension point, and now that CC is selectable that neutral default is what a public build actually runs |
| 7 Regex-engine parity | **none** — the deny catalog is authored against one engine |
| 8 Auxiliary runtimes | partial — `agent_sdk/backend_install.py` is a real preflight that names each absent component before a session, but the *requirement* is still declared nowhere a type checker can see: the probe knows CC needs two binaries because it was written to, not because CC declared it |
| 9 Tool-result marker fidelity | real — one recovery function, and a bucket whose requirement a test enforces rather than a comment asserting it |

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

The frame-replay corpus is the second requirement of that kind. A provider must
add `test/fixtures/acp_frames/<id>/` holding at least one recorded frame
sequence, and `test/test_acp_frame_replay.py` must pass on it. The gate fails
rather than skips when an id in `ACP_BACKENDS_KNOWN` has no directory, and fails
again when a directory does not reach an initialize response, a `session/new`
response, an `agent_message_chunk` turn, a `tool_call`, a `tool_call_update`, a
`session/request_permission` frame, and a response carrying a `stopReason`.

What that buys is narrower than the checklist above and worth stating exactly.
The corpus does not judge whether a declaration is true. It pins the event
stream each backend's frames translate into, so a refactor of the dispatch layer
that changes the SHAPE of a turn — a dropped refinement event, a tool input that
stops being redacted, two events reordered — fails with a diff of the events
instead of passing green. That is the property the Agent SDK boundary work needs
and did not have. `test/fixtures/acp_frames/README.md` documents how to record a
sequence, the review a recording must pass before it is committed, and which of
the committed fixtures are captures rather than synthesized shapes.

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
