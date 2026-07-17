# KiroClaw — Global De-Amazoning Execution Map

> **Historical record — names as of commit `cbe157b` (pre-rename).** This plan
> documents the de-Amazon pass, when the product was named **KiroClaw** (module
> `kiro_claw`) and the fixed goal was to keep that name. The project was later
> renamed to **KiroCrew** (module `kiro_crew`) in 2026-07; the KiroClaw /
> `kiro_claw` names and paths below are preserved as accurate history of that
> earlier state and are intentionally NOT rewritten.

**Role:** Integration lead. The per-subsystem PLANS array arrived **empty**, so this map was
derived directly from the codebase at `/Volumes/workplace/KiroClaw/src/KiroClaw`
(commit `cbe157b`). It is the single authoritative edit plan for the de-Amazoning fork.

**Goal (fixed):** Fork KiroClaw into a PUBLIC, GitHub-distributable, pip+npm-installable app with
ZERO Amazon-internal couplings, fully functional standalone. Product name `KiroClaw` and Python
module `kiro_claw` stay. Only Amazon infra is stripped.

**Architecture facts that shape the plan (verified in code):**
- **Backend abstraction already exists.** `providers/acp.py::AcpProvider` is backend-agnostic and
  drives BOTH kiro-cli and `claude-agent-acp` over the same ACP JSON-RPC layer (`acp/client.py`).
  The generic protocol STAYS. Only the *default* and binary-resolution change.
- **Builtin apps are filesystem-discovered**, not hardcoded — `apps/discovery.py` scans
  `apps/builtins/*/app.json`. Deleting `mimir/` and `code_reviewer/` directories de-registers them
  automatically. The ONLY hardcoded coupling is `apps/builtins/__init__.py::BUILTIN_NAMES = ["mimir"]`.
- **Knowledge connectors use try/except registration** — `knowledge/connectors/__init__.py` already
  guards `QuipConnector` with `except ImportError: QuipConnector = None`. Deleting `quip.py` is safe
  if the symbol name is preserved as `None`.
- **Dashboard auth is already generic HMAC** — `dashboard/token_auth.py` (`generate_token`,
  `validate_token`, `token_auth_middleware`, `_sign`). There is **no** `require_midway_auth` symbol
  anywhere in Python. Midway in the dashboard is surfaced only as **status/TTL display endpoints**
  (`api_midway_ttl`, `api_mwinit_ws`) — strip behavior, keep symbol names.
- **`config/loader.py` is already largely OSS-shaped**: `provider` enum includes `claude_code`,
  `embedding_provider` enum is `none|ollama`, default embedding model `qwen3-embedding:0.6b`. Main
  change: flip `AgentConfig.provider` default `"acp"` → `"claude_code"`, and the `_acp`/`_claude_code`
  factories already coexist.

---

## 1. FILE OWNERSHIP TABLE (de-duplicated; one owner per file)

Legend — **Action**: STRIP (remove Amazon strings/behavior, keep importable), STUB (neutralize to
no-op/return "not available in OSS"), REWRITE (substantive rework), DELETE (rm), CONFIG (data/build),
KEEP-VERIFY (audit only, likely no change). Files touched by multiple concerns are **merged** into a
single combined instruction so the editor handles all concerns at once.

### 1A. Agent runtime / providers / ACP

| File | Action | Combined concerns (merged from all units) |
|---|---|---|
| `src/kiro_claw/config/loader.py` | REWRITE | **(provider)** flip `AgentConfig.provider` default `"acp"`→`"claude_code"`; in `load()` change `agent_data.get("provider","acp")`→`"claude_code"`. Keep `acp`,`bedrock`,`claude_code` enum + all 3 factory branches. **(embeddings)** drop `aws_sigv4` from default `embedding_auth` help text but keep field (user opt-in); leave `ollama` default. **(voice/stt)** keep `SttConfig` incl. `transcribe_*` fields (optional). **(branding)** none. Preserve ALL public symbols: `KiroClawConfig`, `AgentConfig`, `MemoryConfig`, `SlackConfig`, `SttConfig`, `SecretaryConfig`, `TaskKeeperConfig`, `resolve_agent_bindings`, `create_provider_factory`, `workspace_root`, `config_dir`, etc. |
| `src/kiro_claw/acp/client.py` | REWRITE | Default backend → `claude-agent-acp`. `_resolve_kiro_bin()`: keep symbol, remove `~/.toolbox/bin/kiro-cli` hardcoded path; resolve via PATH only → returns `None` gracefully on public machines (do NOT hard-require). `_resolve_claude_acp_bin()`: drop `KiroClawWebsite/node_modules` + toolbox-bundle vendored paths; resolve `@agentclientprotocol/claude-agent-acp` via public npm/PATH/node_modules. `KIRO_CLI_BIN` stays. Update error strings that mention "KiroClawWebsite build and the toolbox bundle" / "toolbox install claude". Keep `_spawn` allowed_prefixes but `arcc`/`builder`/`aim` prefixes become harmless (binaries absent). |
| `src/kiro_claw/providers/acp.py` | KEEP-VERIFY | Backend-agnostic. No Amazon strings. No change beyond confirming default path works with claude backend (it does). |
| `src/kiro_claw/providers/__init__.py` | KEEP | Re-exports `LLMEvent`, `LLMProvider`. No change. |
| `src/kiro_claw/providers/base.py` | KEEP-VERIFY | Audit for strings; likely clean. |
| `src/kiro_claw/providers/claude_code.py` | KEEP-VERIFY | `_CC_DEFAULT_MODEL = global.anthropic.claude-opus-4-8[1m]` stays. Keep `_CC_MODEL_ALIASES`, `_CC_DEFAULT_MODEL`. Audit only. |
| `src/kiro_claw/providers/bedrock.py` | STRIP-OPTIONAL | Keep `BedrockProvider` (optional, needs user AWS creds). Make `boto3` import lazy/guarded so module imports without boto3 on a vanilla machine. |
| `src/kiro_claw/providers/cleanup.py` | KEEP-VERIFY | CC/kiro session cleanup paths. Audit for kiro-only strings. |
| `src/kiro_claw/cc_agent.py` | REWRITE | Default CC provider helper. Remove toolbox CC-defaults dir lookups behavior where Amazon-specific; `_kiroclaw_bin()` drop brazil-path. `generate_mcp_json` / `build_acp_mcp_servers`: drop `builder-mcp`/`arcc-governance` from defaults but keep `kiroclaw-core`/`kiroclaw-cron`. Keep ALL public symbols (`cc_isolation_enabled`, `generate_mcp_json`, `install_cc_agent`, `install_cc_global_deny_settings`, `seed_isolated_cc_config`, `cc_config_root`) — imported by `agent.py`, `config/loader.py`. Keep generic AWS deny patterns; remove `Bash(brazil ws snapshot push*)` deny lines (no longer relevant) — optional. |
| `src/kiro_claw/agent.py` | REWRITE | **Highest-coupling file.** Remove `arcc-governance` from `_MANAGED_MCP_SERVERS` and the `_ARCC_BIN` resolution. Remove `builder-mcp` injection (`_inject_builder_mcp_flags`, `_BUILDER_MCP_TOOL_TAGS`, `_inject_skill_paths` builder usage) from default config; keep generic MCP merge from `~/.kiro|~/.kiroclaw/mcp.json`. Remove AIM install (`_install_aim_capabilities`, `_KIROCLAW_AIM_PACKAGE`, `sync_aim_packages`, `_install_knowledge_agent`'s builder-mcp requirement) — neutralize to no-op (keep function names; they're called in `rebuild_agent_config`). Remove toolbox/brazil/apollo binary resolution (`_bin_is_usable` brazil/apollo checks, `_resolve_toolbox_exec_passthrough`, `_toolbox_cc_defaults_dir`, brazil-path in `_resolve_kiroclaw_bin`). Drop `installed_kiro_packages_missing_from_cc` parity warnings (still importable as no-op). Keep `kiroclaw-cron`/`kiroclaw-core` managed servers. Preserve: `rebuild_agent_config`/`install_agent`, `build_agent_config`, `KIRO_AGENTS_DIR`, `AGENT_FILENAME`, `repair_agent_configs`, `install_cc_agent_config`, `get_shipped_tools`, `_project_dir`. |
| `src/kiro_claw/aim_agents.py` | STUB | Entirely AIM/`aim`-CLI coupled. Neutralize all `aim` subprocess calls to graceful no-ops (binary absent → empty results). **Must preserve symbols imported elsewhere:** `installed_kiro_packages_missing_from_cc` (agent.py), `list_agents`/`AimAgent` (dashboard handlers), `list_cc_plugins`, `is_cc_plugin_installed`, `install_cc_plugin`. `list_agents()` still scans `~/.kiro/agents/*.json` (generic, keep). |
| `src/kiro_claw/conductor_skill.py` | KEEP-VERIFY | Uses `aim` ref in grep; audit only. |
| `src/kiro_claw/subagent.py` | STRIP | Audit kiro/toolbox/brazil refs; strip path assumptions. |
| `src/kiro_claw/sandbox.py` | KEEP-VERIFY | `wrap_argv` generic OS sandbox. Audit for brazil/toolbox env passthrough; keep behavior. |

### 1B. Identity / Auth / Browser

| File | Action | Combined concerns |
|---|---|---|
| `src/kiro_claw/midway.py` | STUB | All functions return "not available" / empty. **Preserve symbols** `midway_status`, `midway_status_async`, `get_midway_status_line` (imported by `dashboard/handlers_system.py`, `slack/handler.py`, `slack/events.py`). `get_midway_status_line` → returns `""`; `midway_status*` → `{"available": False}`. |
| `src/kiro_claw/browser/__init__.py` | KEEP | Docstring only; update to drop "Midway/Kerberos/Federate" wording → generic "browser auth (OSS stub)". |
| `src/kiro_claw/browser/auth.py` | STUB | Neutralize Midway/mcscli/Kerberos/federate/AEA. **Preserve every symbol** (imported by `browser/setup.py`, `browser/cli.py`, `dashboard/handlers/messaging.py`): `MIDWAY_COOKIE_PATH`, `cookie_path`, `parse_netscape_cookies`, `has_mcscli`→`False`, `mcs_keys_process_running`→`False`, `refresh_cookie_via_mcs`→`False`, `refresh_aea`→`False`, `has_kerberos_ticket`→`False`, `health`→`{"available": False, "reason": "not available in OSS"}`, `ensure`→same, `federate_auth`→same. |
| `src/kiro_claw/browser/setup.py` | STUB | Imports `MIDWAY_COOKIE_PATH`, `parse_netscape_cookies` from auth (keep). Neutralize Amazon-auth setup; keep public functions importable (used by `cli_setup.py`, `dashboard/server.py`, `handlers/messaging.py`). |
| `src/kiro_claw/browser/cli.py` | STUB | Neutralize `_cmd_auth_*`; keep `main`, `run_browse` (imported by `cli.py`). Print "not available in OSS". |
| `src/kiro_claw/dashboard/token_auth.py` | KEEP | Already generic HMAC. No change. (Default-open / generic token auth retained.) |
| `src/kiro_claw/dashboard/handlers/mwinit.py` | STUB | Keep `api_mwinit_ws` symbol (routed in server.py, re-exported in handlers/__init__). Return "not available in OSS" / close WS gracefully. |
| `src/kiro_claw/dashboard/handlers_system.py` | STRIP | Keep `api_midway_ttl`, `api_status`, `api_system`, `api_compliance_yolo_status`. `api_midway_ttl` returns null/0 TTL. Strip ollama/qwen internal package refs, amazon URLs. |
| `src/kiro_claw/dashboard/origin.py` | STRIP | Strip `*.amazon.com` safe-origin lines; keep `is_loopback`. |

### 1C. MCP / Tools / Telemetry

| File | Action | Combined concerns |
|---|---|---|
| `src/kiro_claw/mcp_core.py` | STRIP | Remove builder-mcp/arcc/aim references from default tool wiring. Keep kiroclaw-core server (KiroClaw's own). Keep generic redaction. |
| `src/kiro_claw/mcp_discovery.py` | STRIP | Drop builder-mcp/aim discovery defaults; keep generic MCP discovery from user mcp.json. |
| `src/kiro_claw/mcp_shared.py`, `mcp_cleanup.py`, `mcp_playwright_proxy.py` | KEEP-VERIFY | Audit kiro path refs; keep generic. |
| `src/kiro_claw/mcp_cron.py` | KEEP | KiroClaw's own cron MCP server. Keep. |
| `src/kiro_claw/dashboard/handlers/mcp.py` | STRIP | Remove builder-mcp/arcc/aim from default/managed lists in management UI. Keep generic editable MCP config. Preserve all `api_mcp_*` symbols. |
| `src/kiro_claw/dashboard/handlers/agents.py` | STRIP | Remove AIM agent install/list/uninstall behavior → graceful no-ops (binary absent). Preserve ALL `api_aim_*`, `api_cc_aim_*`, `_run_aim`, `_friendly_aim_error` symbols (re-exported in handlers/__init__). |
| `website/src/rum.ts` | REWRITE | **Security leak.** Remove hardcoded `APPLICATION_ID`, `IDENTITY_POOL_ID` (live Cognito pool), RUM endpoint. Make `initRum`, `recordEvent`, `recordSessionStart`, `getRum` no-ops returning safely. Drop `aws-rum-web` import. **Preserve all 4 exported function names** (imported by App.tsx, main.tsx, useRumPageView.ts, RegistryManager.tsx, AppDetailPage.tsx, AppsPage.tsx). |
| `website/src/hooks/useRumPageView.ts` | KEEP-VERIFY | Calls no-op `recordEvent` after rum.ts rewrite. No change needed. |

### 1D. Amazon apps / connectors (leaf deletions + neutralization)

| File | Action | Combined concerns |
|---|---|---|
| `src/kiro_claw/apps/builtins/__init__.py` | REWRITE | `BUILTIN_NAMES = []` (was `["mimir"]`). This + dir deletion fully de-registers mimir. Symbol `BUILTIN_NAMES` preserved (imported by `cli.py`, `dashboard/server.py`, `apps/routes.py`). |
| `src/kiro_claw/apps/builtins/mimir/**` | DELETE | Whole tree (Taskei/SIM/jira/asana/builder_mcp/taskei_graphql). De-registered via discovery + BUILTIN_NAMES. |
| `src/kiro_claw/apps/builtins/code_reviewer/**` | DELETE | Whole tree (kiro fix engine). De-registered via discovery (not in BUILTIN_NAMES, filesystem-discovered only). |
| `src/kiro_claw/knowledge/connectors/quip.py` | DELETE | Quip knowledge connector. |
| `src/kiro_claw/knowledge/connectors/__init__.py` | KEEP | Already `try/except ImportError: QuipConnector = None`. After delete, `QuipConnector` stays `None`. Preserve `__all__ = ['BaseConnector', 'QuipConnector']`. No edit required (import will fail → None). |
| `src/kiro_claw/secretary.py` | DELETE | AIM slack-mcp secretary engine. **Callers must be neutralized first** (see §2). |
| `src/kiro_claw/taskkeeper.py` | DELETE | Outlook-mcp task keeper. |
| `src/kiro_claw/tools/taskkeeper_helper.py` | DELETE | Bundled taskkeeper helper. |
| `src/kiro_claw/dashboard/handlers_secretary.py` | DELETE | Secretary HTTP handlers. **Routes must be removed from server.py first.** |
| `src/kiro_claw/dashboard/handlers_taskkeeper.py` | DELETE | TaskKeeper HTTP handlers. |
| `src/kiro_claw/apps/registry.py` | REWRITE | App-store install path is Brazil/`git.amazon.com`-only. Replace `_brazil_build_app`/`_run_brazil_build`/`brazil ws use` with public `git clone` + `git pull` + (npm/pip) build. Change `ssh://git.amazon.com/pkg/` → user-configurable public git host (e.g. GitHub). Strip `BRAZIL_*` from `_SAFE_ENV_KEYS`. Preserve public API symbols (`list_registry`, `install_from_registry`, `get_registry_app`, `known_registry_repos`, etc.). |
| `src/kiro_claw/apps/discovery.py` | KEEP | Filesystem discovery; generic. No change. |
| `src/kiro_claw/apps/backend.py` | KEEP-VERIFY | Module-style builtin loader mentions `code_reviewer` only in comments. Update comment; logic generic. |
| `src/kiro_claw/apps/routes.py` | STRIP | Imports `BUILTIN_NAMES` (now empty). Audit amazon URLs. |
| `src/kiro_claw/knowledge/ingestion.py`, `readers.py`, `agent_fetch.py`, `llm_pool.py` | STRIP | `llm_pool.py` references builder-mcp/ReadInternalWebsites for URL fetch — make generic/optional. Keep `local_folder` + `obsidian`-style readers. Audit quip refs (none beyond connector). |

### 1E. Slack

| File | Action | Combined concerns |
|---|---|---|
| `src/kiro_claw/slack/gateway.py` | REWRITE | **Multi-concern.** Remove `secretary` import + `_init_secretary` + `secretary_svc` (deleted subsystem) → guard/no-op so gateway boots without it. Update Amazon registry URL string (`https://ai-registry.amazon.dev/mcp-registry/...`) to generic. Remove `taskkeeper` wiring if present. Keep ContextBuilder/scheduler/heartbeat/subagents/taskrunner. Preserve `SlackGateway` public surface. |
| `src/kiro_claw/slack/enterprise.py` | REWRITE | **Default-open.** Remove hardcoded org IDs `E015GUGD2V6`/`E01C2B11VN2` (`_AMAZON_ENTERPRISE_IDS`). `validate_enterprise()` → default-allow (return True) unless user configured `slack.allowed_enterprise_ids`; `check_message_origin()` → allow when no allowlist configured. **Preserve symbols** `validate_enterprise`, `check_message_origin` (imported by `slack/events.py`). Keep SEL audit calls. |
| `src/kiro_claw/slack/events.py` | STRIP | Imports `get_midway_status_line` (now `""`) + `check_message_origin` (now default-open). Audit amazon URL strings. Keep behavior. |
| `src/kiro_claw/slack/handler.py` | STRIP | Imports `get_midway_status_line`. Strip amazon refs; keep. |
| `src/kiro_claw/slack/interactions.py` | STRIP | Audit amazon.com URLs in block links. |
| `src/kiro_claw/slack/files.py` | KEEP-VERIFY | boto3/transcribe? audit; make optional if used. |
| `src/kiro_claw/slack/allowlist.py`, `blocks.py`, `channel_resolver.py`, `client.py`, `format.py`, `sessions_view.py` | KEEP-VERIFY | Audit only. |

### 1F. Dashboard server + handlers

| File | Action | Combined concerns |
|---|---|---|
| `src/kiro_claw/dashboard/server.py` | REWRITE | **Multi-concern.** Remove ALL `/api/secretary/*` routes (15) and `/api/taskkeeper/*` routes (21) + their `handlers_secretary`/`handlers_taskkeeper` imports (deleted). Keep `/api/midway-ttl`→`api_midway_ttl` (stub), `/api/mwinit`→`api_mwinit_ws` (stub). Imports `BUILTIN_NAMES` (now `[]`). Keep `token_auth_middleware`. Strip amazon URLs. Preserve `build_app`/server bootstrap symbols. |
| `src/kiro_claw/dashboard/handlers/__init__.py` | STRIP | **Re-export hub.** Keep re-exporting `api_midway_ttl`, `api_mwinit_ws` (stubbed sources). Remove imports of any deleted-handler symbols. Everything else re-exported stays. |
| `src/kiro_claw/dashboard/handlers/core.py` | STRIP | `_build_stt_install_script`, `_stt_prereq_commands` reference whisper (keep) + transcribe (optional). Strip amazon URLs / `api_branding` watermark text. Preserve `api_branding`, `api_kiroclaw_config`, etc. |
| `src/kiro_claw/dashboard/handlers/knowledge.py`, `memory.py` | STRIP | ollama/qwen embedding refs — keep ollama default, drop internal package mentions. |
| `src/kiro_claw/dashboard/handlers/updates.py` | REWRITE | Update mechanism: replace toolbox/brazil self-update with `git pull` + pip/npm rebuild. Preserve `_do_update_check`, `api_update_*` symbols. |
| `src/kiro_claw/dashboard/handlers/usage.py` | KEEP-VERIFY | `api_kiro_usage` name kept; audit kiro refs. |
| `src/kiro_claw/dashboard/handlers/files.py`, `prompts.py`, `sessions.py`, `_shared.py`, `messaging.py` | STRIP | `_shared.py`/`prompts.py`/`sessions.py` have AIM-skill scanning (`_list_aim_skills`, `_resolve_aim_skill_path`, `_list_aim_prompts`) → keep symbols, make AIM-dir scan a no-op when `~/.aim` absent (already guarded). `messaging.py` imports browser auth (stubbed). Audit amazon URLs. |
| `src/kiro_claw/dashboard/chat*.py` (chat_runner, chat_voice, chat.py, chat_title, chat_rewind, chat_utils, chat_handlers) | STRIP | `chat_voice.py`/`chat.py` reference polly/transcribe → optional (whisper default). kiro refs in chat_runner/title/rewind/utils → keep generic. Audit. |
| `src/kiro_claw/dashboard/state.py` | STRIP | `_secretary_state`/`_secretary_inbox` lazy fields — set to permanent `None` / remove (secretary deleted). Audit kiro refs. |

### 1G. Embeddings / Voice / Build / Misc

| File | Action | Combined concerns |
|---|---|---|
| `src/kiro_claw/embeddings.py` | REWRITE | **Embeddings.** Pull model from PUBLIC Ollama registry via `ollama pull qwen3-embedding:0.6b` instead of internal `KiroClawModelQwen3Embedding` GGUF package (remove `_MODEL_PACKAGE`, `_GGUF_FILENAME` internal-fetch path). Make `botocore`/SigV4 import already-guarded path OPTIONAL and OFF by default (`embedding_auth=none`). Keep `OllamaEmbedder`, `OllamaManager`, `_sigv4_sign` (guarded). |
| `src/kiro_claw/knowledge/embedder.py` | STRIP | ollama/qwen refs — public pull. Audit. |
| `src/kiro_claw/vector_memory.py` | KEEP-VERIFY | ollama default. Audit. |
| `src/kiro_claw/transcribe.py` | STRIP-OPTIONAL | `amazon-transcribe` + `boto3` module-level imports → lazy/guarded so module imports without them. Keep public symbols. |
| `src/kiro_claw/voice_reply.py` | STRIP-OPTIONAL | Polly optional (needs AWS creds). Lazy boto3. Keep symbols. |
| `src/kiro_claw/dashboard/stt_stream.py` | STRIP-OPTIONAL | transcribe streaming optional; whisper default path stays. Audit amazon URLs. |
| `src/kiro_claw/cli_doctor.py` | STRIP | Remove kiro/toolbox/brazil/ollama-internal/transcribe/polly hard checks; report optional features gracefully. |
| `src/kiro_claw/cli.py` | STRIP | Imports `BUILTIN_NAMES`, `run_browse` (stubbed), browser cli. Strip `mcscli`/amazon refs. Keep entrypoints. |
| `src/kiro_claw/cli_setup.py`, `cli_server.py`, `cli_config.py`, `cli_commands.py` | STRIP | kiro/toolbox/brazil self-update + AIM setup refs → git/pip/npm. Keep CLI symbols. |
| `src/kiro_claw/constants.py` | STRIP | Remove `ARCC_REGISTRY` (s3 buildertoolbox), `ARCC_TOOLBOX_PACKAGE`, `policy.a2z.com` URL. Keep generic constants. |
| `src/kiro_claw/security.py` | KEEP | **KEEP** generic deny patterns + AKIA/ASIA AWS credential redaction (generic). Remove only Amazon-specific safe-domains (`*.amazon.com`/`a2z.com`/`aws.dev`), brazil deny lines, `.ada`/`.midway` sandbox-exposure entries. Preserve `is_sensitive_path`, `redact`, `redact_credentials`, `redact_exfiltration_urls`. |
| `src/kiro_claw/sel.py` | KEEP-VERIFY | Security event log; generic. Audit. |
| `src/kiro_claw/effort.py`, `context.py`, `frontend.py`, `seed.py`, `skills.py`, `history.py`, `mirror.py`, `suggestions.py`, `cron_script.py` | STRIP | Each flagged for kiro/aim/amazon strings in grep. Audit + strip user-facing internal URLs / kiro hard paths. `mirror.py` uses boto3? → lazy. |
| `src/kiro_claw/tunnel/manager.py`, `setup.py` | STRIP/STUB | AEA Amazon Tunnels → stub (`TunnelConfig.enabled=False` default already). Keep `TunnelManager` symbols, return "not available in OSS". |
| `src/kiro_claw/service/linux.py`, `common.py` | STRIP | brazil/amazon path refs in service install. Keep. |
| `src/kiro_claw/eval/judge.py` | STRIP | kiro refs; audit. |

### 1H. Build / packaging / install (CONFIG)

| File | Action | Combined concerns |
|---|---|---|
| `setup.py` | REWRITE | Remove `KiroClawWebsite` brazil-path/sibling/brazil-bootstrap frontend resolution → expect pre-built `static/dist/` in-tree (npm build output). Remove `_BrazilPythonCompatDistribution`, `bdist_toolbox` / `_TOOLBOX_BUCKET_TEMPLATE`, brazil verbosity-arg filtering. Plain setuptools. |
| `setup.cfg` | REWRITE | Move `amazon-transcribe` + `boto3` OUT of `install_requires` into `[options.extras_require]` (e.g. `voice`/`aws`). Remove `amzn-midwayclientsuitelibrarypython` note. Replace `[brazilpython_*]` / `test_command = brazilpython_pytest` / brazil coverage paths with plain `pytest`. Keep `aiohttp`, `slack-sdk`, `numpy`, `PyYAML`, etc. |
| `pyproject.toml` | STRIP | Remove "declared in setup.cfg for Brazil compat" note; standard build-system. |
| `Config` (Brazil) | DELETE | Brazil build config — not used by pip/npm. |
| `install.sh`, `setup.sh`, `minimal_install.sh` | REWRITE | Remove toolbox/midway/brazil/kiro-cli/AIM bootstrap. New flow: clone + `pip install -e .[extras]` + `npm ci && npm run build` + `ollama pull`. Remove `midway-auth.amazon.com`/`*.aws.dev` toolbox bootstrap URLs. |
| `dev-backend.sh`, `dev-seed.sh`, `ensure-node.sh`, `clean.sh`, `Makefile` | STRIP | Audit brazil/toolbox; convert to pip/npm. |
| `website/package.json` | STRIP | Remove `aws-rum-web` dependency (rum.ts no longer imports it). Ensure public-only deps. |
| `AGENTS.md`, `README.md`, `CONTRIBUTING.md`, `SLACK_SETUP.md`, `docs/**` | STRIP | User-facing docs: strip internal URLs (`git.amazon.com`, `code.amazon.com`, `*.amazon.com`, `taskei.amazon.dev`, `w.amazon.com`, `docs.hub.amazon.dev`), account `149122183214`, aliases `phonetool`/`bolichen`/`zejiangg`, brazil/toolbox/midway/AIM setup instructions. |
| `AUTOSDE.yaml`, `CODE_APPROVERS.yaml` | DELETE/STRIP | Amazon CR tooling config — remove or genericize. |
| `.kiro/**` | KEEP-VERIFY | Project steering files — audit for internal URLs only. |

---

## 2. LEAF DELETIONS — ordered, with import sites to neutralize FIRST

Each subsystem deletion must neutralize its **external import sites BEFORE** the `rm`, so the import
graph never breaks. Order matters: neutralize callers → delete leaf.

### D1. `mimir` (Taskei/SIM) — `apps/builtins/mimir/`
- **External import sites (neutralize first):**
  - `apps/builtins/__init__.py` → set `BUILTIN_NAMES = []` (remove `"mimir"`).
  - No Python `import kiro_claw.apps.builtins.mimir` outside the tree (discovery is filesystem-based;
    `apps/backend.py` references it only in comments). `cli.py`/`server.py`/`routes.py` import only
    `BUILTIN_NAMES`.
- **Then:** `rm -rf src/kiro_claw/apps/builtins/mimir/`

### D2. `code_reviewer` (kiro fix engine) — `apps/builtins/code_reviewer/`
- **External import sites:** none hardcoded (not in `BUILTIN_NAMES`; filesystem-discovered only;
  `apps/backend.py` comment-only). Module-style loader resolves dynamically.
- **Then:** `rm -rf src/kiro_claw/apps/builtins/code_reviewer/`

### D3. `secretary` (AIM slack-mcp) — `secretary.py` + `dashboard/handlers_secretary.py`
- **External import sites (neutralize first):**
  - `slack/gateway.py` (lines ~106, 1634, 1640, 1644): remove `from kiro_claw.secretary import SecretaryItem, SecretaryService, _find_slack_mcp`, the `_init_secretary` method, `self.secretary_svc`, and the `handlers_secretary._redact_item` broadcast.
  - `dashboard/handlers_taskkeeper.py:23` `from kiro_claw.secretary import SlackMcpClient` → removed with D4.
  - `dashboard/state.py:696-697` `_secretary_state`/`_secretary_inbox` → remove or pin `None`.
  - `dashboard/server.py` → remove the 15 `/api/secretary/*` routes + `handlers_secretary` import.
- **Then:** `rm src/kiro_claw/secretary.py src/kiro_claw/dashboard/handlers_secretary.py`

### D4. `taskkeeper` (outlook-mcp) — `taskkeeper.py` + `dashboard/handlers_taskkeeper.py` + `tools/taskkeeper_helper.py`
- **External import sites (neutralize first):**
  - `dashboard/server.py` → remove the 21 `/api/taskkeeper/*` routes + `handlers_taskkeeper` import.
  - `dashboard/handlers_taskkeeper.py` imports `kiro_claw.taskkeeper` + `kiro_claw.secretary.SlackMcpClient` (both deleted together).
  - `taskkeeper.py:655` bundles `tools/taskkeeper_helper.py` (internal reference only).
  - MCP tool `taskkeeper_complete` (in `mcp_core.py`) → remove tool registration.
- **Then:** `rm src/kiro_claw/taskkeeper.py src/kiro_claw/dashboard/handlers_taskkeeper.py src/kiro_claw/tools/taskkeeper_helper.py`

### D5. `quip` knowledge connector — `knowledge/connectors/quip.py`
- **External import sites:** `knowledge/connectors/__init__.py` ALREADY guards with
  `try: from ...quip import QuipConnector / except ImportError: QuipConnector = None`. No edit needed;
  symbol `QuipConnector` stays defined as `None`. Verify no other `import ...quip` exists (none found).
- **Then:** `rm src/kiro_claw/knowledge/connectors/quip.py`

### D6. Build-system leaves
- `rm Config` (Brazil). Remove `AUTOSDE.yaml`, `CODE_APPROVERS.yaml` (after confirming no test/build
  reads them).

> **Note:** `aim_agents.py`, `midway.py`, `browser/*`, `tunnel/*` are **NOT deleted** — they are
> STUBBED because core modules import their symbols. Deleting them would break the import graph.

---

## 3. GLOBAL SYMBOL-PRESERVATION LIST

These module-level public names are imported across files and MUST keep their names (change behavior
only — no-op / external / safe default). Grouped by owning module:

- **`midway.py`** → `midway_status`, `midway_status_async`, `get_midway_status_line`
- **`browser/auth.py`** → `MIDWAY_COOKIE_PATH`, `cookie_path`, `parse_netscape_cookies`, `has_mcscli`,
  `mcs_keys_process_running`, `refresh_cookie_via_mcs`, `refresh_aea`, `has_kerberos_ticket`,
  `health`, `ensure`, `federate_auth`
- **`browser/cli.py`** → `main`, `run_browse`; **`browser/setup.py`** → all public setup fns
- **`aim_agents.py`** → `AimAgent`, `list_agents`, `list_cc_plugins`, `is_cc_plugin_installed`,
  `install_cc_plugin`, `installed_kiro_packages_missing_from_cc`
- **`agent.py`** → `rebuild_agent_config`, `install_agent`, `build_agent_config`, `KIRO_AGENTS_DIR`,
  `AGENT_FILENAME`, `repair_agent_configs`, `install_cc_agent_config`, `get_shipped_tools`,
  `_project_dir`, `sync_aim_packages` (no-op), `_install_aim_capabilities` (no-op)
- **`cc_agent.py`** → `cc_isolation_enabled`, `cc_config_root`, `generate_mcp_json`,
  `install_cc_agent`, `install_cc_global_deny_settings`, `seed_isolated_cc_config`,
  `kiroclaw_stdio_servers`, `build_acp_mcp_servers`, `acp_servers_from_cc_map`
- **`config/loader.py`** → `KiroClawConfig`, `AgentConfig`, `MemoryConfig`, `SlackConfig`,
  `SttConfig`, `SecretaryConfig`, `TaskKeeperConfig`, `TunnelConfig`, `resolve_agent_bindings`,
  `validate_kiro_agent_references`, `config_dir`, `config_path`, `workspace_root`,
  `resolve_agent_config_path`, `create_provider_factory` (kept on the dataclass).
  *(Note: `SecretaryConfig`/`TaskKeeperConfig` dataclasses + `cfg.secretary`/`cfg.taskkeeper` fields
  are referenced by `to_dict`/`load`/tests — KEEP the config dataclasses even though the runtime
  services are deleted, OR remove all references atomically. Recommend KEEP dataclasses as inert
  config to minimize blast radius.)*
- **`apps/builtins/__init__.py`** → `BUILTIN_NAMES` (value → `[]`)
- **`knowledge/connectors/__init__.py`** → `BaseConnector`, `QuipConnector` (→ `None`), `__all__`
- **`slack/enterprise.py`** → `validate_enterprise`, `check_message_origin` (default-open behavior)
- **`dashboard/token_auth.py`** → `generate_token`, `validate_token`, `validate_token_with_app`,
  `token_auth_middleware`, `generate_app_secret`, `validate_app_secret`, `revoke_all_sessions`, etc.
- **`dashboard/handlers/mwinit.py`** → `api_mwinit_ws`
- **`dashboard/handlers_system.py`** → `api_midway_ttl`, `api_status`, `api_system`,
  `api_compliance_yolo_status`
- **`dashboard/handlers/__init__.py`** → continues re-exporting `api_midway_ttl`, `api_mwinit_ws`
  and all current handler symbols (minus any from deleted handler modules).
- **`acp/client.py`** → `AcpClient`, `AcpError`, `KIRO_CLI_BIN`, `_resolve_kiro_bin`,
  `_resolve_claude_acp_bin`
- **`embeddings.py`** → `OllamaEmbedder`, `OllamaManager`, `_sigv4_sign` (guarded)
- **`tunnel/manager.py`** → `TunnelManager` (+ public methods, return "not available in OSS")
- **`website/src/rum.ts`** → `initRum`, `recordEvent`, `recordSessionStart`, `getRum` (no-ops)

---

## 4. PHASE PLAN (parallel vs sequential)

The cross-file safety contract requires disjoint file sets per parallel wave. Sequencing is dictated
by deletion-before-caller-edit and re-export hubs.

**PHASE 0 — Symbol-preserving STUBS (fully parallel; disjoint files).** No deletions yet, so every
caller keeps importing successfully. Run all simultaneously:
- `midway.py`, `browser/auth.py`, `browser/setup.py`, `browser/cli.py`, `browser/__init__.py`,
  `aim_agents.py`, `tunnel/manager.py`, `tunnel/setup.py`, `dashboard/handlers/mwinit.py`,
  `website/src/rum.ts`, `slack/enterprise.py`, `embeddings.py`, `transcribe.py`, `voice_reply.py`,
  `providers/bedrock.py`, `constants.py`, `security.py`.

**PHASE 1 — Caller neutralization for deletions (SEQUENTIAL within group; these touch shared hubs).**
Must precede Phase 2 deletes. Edit in this order due to tight coupling:
1. `apps/builtins/__init__.py` (`BUILTIN_NAMES=[]`).
2. `dashboard/server.py` (remove secretary+taskkeeper routes/imports). **Single owner — high fan-in.**
3. `dashboard/handlers/__init__.py` (drop deleted-module re-exports; keep stub re-exports). **Hub.**
4. `slack/gateway.py` (remove secretary init/imports).
5. `dashboard/state.py` (drop secretary fields).
   *(These 5 are sequential because server.py ↔ handlers/__init__ ↔ gateway share the secretary/
   taskkeeper surface; editing in parallel risks conflicting partial states.)*

**PHASE 2 — Leaf deletions (parallel; disjoint trees, after Phase 1).**
- `rm -rf apps/builtins/mimir/`, `apps/builtins/code_reviewer/`
- `rm secretary.py handlers_secretary.py taskkeeper.py handlers_taskkeeper.py tools/taskkeeper_helper.py`
- `rm knowledge/connectors/quip.py`
- `rm Config AUTOSDE.yaml CODE_APPROVERS.yaml`

**PHASE 3 — Core runtime rewrites (mostly parallel; a few sequential on circular imports).**
- *Sequential subgroup (circular: agent ↔ config.loader ↔ cc_agent):* `cc_agent.py` → `agent.py` →
  `config/loader.py`. Edit cc_agent first (provides symbols agent imports), then agent, then loader
  (provider default flip + factory). Then `acp/client.py` (backend default + bin resolution).
- *Parallel subgroup (disjoint):* `mcp_core.py`, `mcp_discovery.py`, `apps/registry.py`,
  `dashboard/handlers/mcp.py`, `dashboard/handlers/agents.py`, `dashboard/handlers/updates.py`,
  `knowledge/llm_pool.py`, `knowledge/embedder.py`, `knowledge/ingestion.py`.

**PHASE 4 — Peripheral STRIP (fully parallel; disjoint).**
- `slack/events.py`, `slack/handler.py`, `slack/interactions.py`, `slack/files.py`,
  `dashboard/origin.py`, `dashboard/handlers_system.py`, `dashboard/handlers/core.py`,
  `dashboard/handlers/knowledge.py`, `dashboard/handlers/memory.py`, `dashboard/handlers/files.py`,
  `dashboard/chat*.py`, `cli*.py`, `cli_doctor.py`, `effort.py`, `context.py`, `frontend.py`,
  `seed.py`, `skills.py`, `history.py`, `mirror.py`, `suggestions.py`, `service/*`,
  `dashboard/stt_stream.py`, `eval/judge.py`.

**PHASE 5 — Build/packaging + docs (parallel; disjoint, run last).**
- `setup.py`, `setup.cfg`, `pyproject.toml`, `install.sh`, `setup.sh`, `minimal_install.sh`,
  `dev-*.sh`, `Makefile`, `website/package.json`, `README.md`, `AGENTS.md`, `CONTRIBUTING.md`,
  `SLACK_SETUP.md`, `docs/**`.

**PHASE 6 — Test triage** (see §5).

**Gate after each phase:** `python -c "import kiro_claw"` + `python -m pytest --collect-only` must
succeed (import graph intact) before advancing.

---

## 5. TEST-SUITE IMPACT

Test root `test/` has 339 files. Two buckets:

### DELETE (tests for removed subsystems)
- **mimir:** `test_mimir_adapters.py`, `test_mimir_builder_mcp.py`, `test_mimir_config.py`,
  `test_mimir_core.py`, `test_mimir_handlers.py`, `test_mimir_jira.py`, `test_mimir_mcp_server.py`,
  `test_mimir_setup.py`, `test_mimir_tag_filter.py`, `test_mimir_taskei.py`
- **code_reviewer:** `test_code_reviewer_app.py`
- **secretary:** `test_secretary.py`, `test_secretary_emojis.py`, `test_secretary_react.py`
- **taskkeeper:** `test_taskkeeper.py`, `test_taskkeeper_helper.py`, `test_handlers_taskkeeper.py`,
  `test_mcp_taskkeeper_complete.py`
- **midway/mwinit/browser auth (Amazon-auth):** `test_midway.py`, `test_mwinit_ws.py`,
  `test_browser_auth.py`, `test_browser_setup.py`, `test_skill_browser.py` (verify scope)
- **AIM:** `test_aim_cc_parity.py`, `test_aim_mcp_registry.py`, `test_run_aim_path.py`
- **enterprise gate (Amazon org IDs):** `test_enterprise.py` → DELETE or REWRITE to default-open
- **transcribe:** `test_transcribe.py` → DELETE or move under optional `aws` extra marker

### ADAPT (behavior changed, keep coverage)
- `test_embeddings.py`, `test_enable_embeddings_faiss.py` — drop internal-package GGUF path, assert
  public `ollama pull`; SigV4 path becomes opt-in.
- `test_chat_voice.py`, `test_voice_reply.py`, `test_set_orch_cfg_voice.py` — whisper default; Polly/
  transcribe become optional (skip when boto3 absent).
- `test_tunnel_manager.py` — assert "not available in OSS" stub behavior.
- Any `test_config*` / `test_agent*` — update for `provider` default `claude_code`, removed managed
  servers (no `arcc-governance`/`builder-mcp`), `SecretaryConfig`/`TaskKeeperConfig` kept-as-inert.
- Any test importing `BUILTIN_NAMES` expecting `["mimir"]` → `[]`.
- RUM/telemetry: no Python tests found; verify website test dir (`website/src/test`) for rum mocks.

### KEEP
- The bulk of the ~8777-test suite (core chat, session, cron, memory, ACP protocol, skills, sandbox,
  security redaction) is untouched — the brand/module names stay, satisfying the contract's "do not
  break 8777 tests" constraint.

---

## 6. OPEN DECISIONS / RISKS

1. **Empty PLANS array.** This map was derived solely from source inspection. If the orchestrator
   intended specific per-subsystem plans, reconcile — but the contract's GLOBAL DECISIONS are fully
   self-consistent with what the code requires, so this map is executable as-is.
2. **`SecretaryConfig`/`TaskKeeperConfig` dataclasses vs deleted services.** `config/loader.py`
   `load()`/`to_dict()` and the schema reference these. RECOMMENDATION: **keep the config dataclasses
   inert** (fields default-disabled) to minimize blast radius across loader + schema + tests; delete
   only the *runtime services*, *handlers*, and *routes*. Confirm acceptable.
3. **App-store install path (`apps/registry.py`).** Currently 100% Brazil + `ssh://git.amazon.com`.
   Public replacement (git clone + npm/pip build) is a substantive rewrite and the in-tree
   `app-registry.json` likely lists internal repos. DECISION NEEDED: ship an empty/curated public
   registry initially, or fully port the build pipeline now?
4. **`config/loader.py` field names** still say `kiro_agent`, `pool_size` mentions "kiro-cli
   processes". Per BRANDING decision (don't rename import namespace), KEEP field names; only update
   user-facing help strings. Confirm `kiro_agent`/`kiro-cli` field names are acceptable to retain.
5. **claude-agent-acp binary availability.** Default backend now requires `@agentclientprotocol/
   claude-agent-acp` from public npm. The install scripts must `npm i -g` or vendor it; otherwise
   first run yields "backend not found". Kiro-cli stays as graceful-None optional. Verify the public
   npm package name/version pin.
6. **Ollama embedding model.** `ollama pull qwen3-embedding:0.6b` must exist in the PUBLIC Ollama
   registry at the pinned dim (1024). Verify availability; fallback `nomic-embed-text` (768) noted in
   config help — keep as documented alternative.
7. **`docs/kiro-cli/` doc tree + `.kiro/` steering** reference internal systems. Scope the doc strip
   to user-facing internal URLs; internal steering files may be dropped wholesale.
8. **`amazon-transcribe`/`boto3` moving to extras** changes `transcribe.py`/`voice_reply.py` from
   module-level to lazy imports — ensure no top-level import remains that would crash a vanilla
   `import kiro_claw`.
9. **Security deny-list scope.** Keep generic AWS AKIA/ASIA redaction (explicitly required). Removing
   `.midway`/`.ada` sensitive-path entries must not weaken generic `is_sensitive_path` for `~/.ssh`
   etc. — surgical removal of Amazon-only lines only.
10. **RUM removal in website** drops `aws-rum-web` from `package.json`; confirm no other module
    imports it transitively, and that `useRumPageView` + 6 call sites tolerate no-op exports.
