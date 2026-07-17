# De-Amazoning Report — KiroClaw (public OSS fork)

> **Historical record — names as of 2026-06-02.** This report documents the
> de-Amazon pass, when the project was named **KiroClaw** (package `kiro_claw`).
> The project was later renamed to **KiroCrew** (package `kiro_crew`) in
> 2026-07; the KiroClaw / `kiro_claw` names below are preserved as accurate
> history of that earlier state and are intentionally NOT rewritten.

_Date: 2026-06-02. KiroClaw is the public, GitHub-distributable,
pip+npm-installable OSS fork that strips all Amazon-internal couplings. The
de-Amazoned fork now lives in this `KiroClaw` package._

> **NOTE (updated post-2026-06-02):** a later **KiroACP-only** refactor
> superseded the `claude_code`-default decisions recorded below — `agent.provider`
> is now fixed to `acp` and kiro-cli is REQUIRED; the standalone Claude Code and
> Bedrock providers were removed. See current `CLAUDE.md` / `AGENTS.md`. The
> provider-related lines below are annotated inline where stale.

## Final Health

| Check | Result |
|---|---|
| `import kiro_claw` | **IMPORT_OK** |
| Backend test suite (`pytest test/ -n auto`) | **7780 passed, 39 skipped, 0 failed, 0 errors** |
| Frontend typecheck (`tsc -b`) | **0 errors** |
| Frontend build (`npm run build`) | **succeeds** → `website/dist` → copied to `src/kiro_claw/static/dist` |
| Repo-wide Amazon-infra string scan | **0 remaining** (live URLs / accounts / aliases / org-IDs / build) |
| Default agent provider | `acp` (kiro-cli over ACP) — KiroACP-only (see note above) |
| Original KiroClaw package | untouched (git clean) |

## What Was Removed (deleted)

- **Amazon apps / connectors:** `apps/builtins/mimir/` (Taskei/SIM/Jira/Asana), `apps/builtins/code_reviewer/`,
  `secretary.py` + `dashboard/handlers_secretary.py`, `taskkeeper.py` + `dashboard/handlers_taskkeeper.py` +
  `tools/taskkeeper_helper.py`, `knowledge/connectors/quip.py` — plus all their routes, MCP tools
  (`taskkeeper_complete`), config wiring, and frontend pages/components/slices.
- **Build machinery:** Brazil `Config`, `AUTOSDE.yaml`, `CODE_APPROVERS.yaml`, `setup.py`
  `ToolboxBundlerCommand`/`PublishStable*`/BrazilPython-compat/claude-acp-vendoring, the
  `npm-pretty-much` hook + `@amzn/` package names. `setup.py`/`setup.cfg` are now plain setuptools.
- **Identity (frontend):** live Cognito pool + RUM app id in `rum.ts` (security leak) → no-op telemetry;
  `MwinitTerminal`, `AimPackageManager`, `KiroUsageTab`, secretary/taskkeeper/mimir/code-reviewer pages.
- **Secrets/identifiers:** AWS account `149122183214`, Slack org IDs `E015GUGD2V6`/`E01C2B11VN2`,
  phonetool/personal aliases, internal URLs (git/code/w/docs.hub/taskei.amazon, a2z, aws.dev, ai-registry).

## What Was Stubbed (symbols kept, behavior no-op / "not available in OSS")

- `midway.py`, `browser/auth.py` + `browser/setup.py` + `browser/cli.py` (Midway/Kerberos/MCS/federate),
  `dashboard/handlers/mwinit.py` (`api_mwinit_ws`), `tunnel/manager.py` (AEA tunnels), `aim_agents.py`
  (the `aim` CLI). Every public symbol other modules import is preserved so the import graph stays intact.

## What Changed (behavior)

- **Default agent backend**: the initial de-Amazoning flipped `acp` → `claude_code`, but a later
  **KiroACP-only refactor reversed this** — `agent.provider` is now fixed to `acp` and kiro-cli is
  REQUIRED. The standalone `ClaudeCodeProvider`/`BedrockProvider`, `cc_agent`, and `mirror` modules were
  deleted and the public `claude-agent-acp` registration removed; only the dormant `ACP_BACKEND_CLAUDE`
  seam in `acp/client.py` is intentionally kept (no-op) so an internal companion can re-register Claude
  Code. Generic ACP JSON-RPC protocol layer kept intact.
- **Embeddings** pull from the PUBLIC Ollama registry (`ollama pull qwen3-embedding:0.6b`, documented
  `nomic-embed-text` fallback) instead of the internal Gitfarm GGUF package. SigV4 off by default.
- **Slack enterprise gate** is DEFAULT-OPEN (no hardcoded org-ID frozenset; opt-in via
  `slack.allowed_enterprise_ids`).
- **Default agent config** drops `@builder-mcp` + `arcc-governance` + the AIM publish-metrics hook +
  brazil deny lines; keeps `kiroclaw-core` + `kiroclaw-cron` (KiroClaw's own servers).
- **Self-update** is `git pull` + `pip install -e .` + `npm build` (no toolbox/brazil).
- **App registry** install rewritten from Brazil/`ssh://git.amazon.com` to generic `git clone`.
- `amazon-transcribe` + `boto3` + Polly are OPTIONAL (lazy imports; `[options.extras_require]`); local
  whisper STT is the default. (The Bedrock provider was later REMOVED in the KiroACP-only refactor — the
  `[aws]` extra is gone; `boto3` now only serves optional STT.)

## What Stayed (intentional, generic)

- Generic security: AKIA/ASIA AWS-credential redaction, destructive-command deny patterns,
  `~/.aws`/`~/.ssh` sensitive-path blocking, SEL audit log.
- Core product: Slack gateway, web dashboard, CLI, sessions, cron, heartbeat, subagents, task runner,
  memory/learning, skills, artifacts, knowledge (local_folder/obsidian), channels, side conversations.
- ACP client + the single ACP provider driving the `kiro-cli` backend (KiroACP-only). The
  claude-agent-acp and Bedrock providers were removed in the later KiroACP-only refactor; the dormant
  `ACP_BACKEND_CLAUDE` protocol seam in `acp/client.py` is kept for an internal companion.
- Internal module/field names (`kiro_claw`, `kiro_agent`, etc.) — renaming the import namespace was out
  of scope and would break the 7780-test suite. Only user-facing strings were genericized.

## Tests

- Deleted orphaned tests for fully-removed subsystems (mimir, code_reviewer, secretary, taskkeeper,
  quip, toolbox-bundler, enterprise-org-ID, midway). Adapted tests for changed behavior (provider
  default, embeddings public pull, default-open enterprise, app-registry git install, builder-mcp-free
  agent config, optional voice/transcribe, stubbed tunnel/mwinit/browser/aim). Net suite: **7780 green**.

## Follow-ups for public launch (human / out of scope here)

- Replace `https://github.com/YOUR_ORG/kiroclaw` placeholders with the real repo URL.
- Rotate any previously-committed secrets at the source (the live Cognito/RUM ids were removed here but
  must be torn down in AWS by the owner).
- Add a `LICENSE` file (Apache-2.0) and run the formal OSS release / legal clearance before publishing.
- `claude-agent-acp` is installed via `npm i -g @agentclientprotocol/claude-agent-acp` at setup; verify
  the pinned public version in the install scripts.

---

## Code-Review Findings — All Fixed (2026-06-02)

A recall-mode review of the de-Amazoning diff surfaced 15 findings + 6 sweep gaps; all are fixed and verified (backend 7789 passed / frontend 2159 passed / both typecheck clean, 0 Amazon-infra refs).

**Correctness / security:**
1. `slack/gateway.py::_auto_apply_update` — the third (gateway auto-update) path was still fully Amazon-coupled (toolbox/brazil/aim); rewritten to git pull + build in-tree `website/` + pip install + execv restart.
2. `slack/enterprise.py` — `validate_enterprise` now FAILS CLOSED when an allowlist is configured but `auth.test` can't verify the workspace (was fail-open); still default-open only when no allowlist is set.
3. `apps/routes.py` blob proxy — replaced `git archive --remote` (GitHub-incompatible) with sandboxed `git clone --depth 1 --single-branch`; widened repo validation to accept vetted https/scp/ssh git URLs (was bare-name-only).
4/5/6. Build/update unification — `frontend.py` now builds the in-tree `website/` and stages `website/dist → src/kiro_claw/static/dist`; `cli_server._update` and `dashboard/handlers/updates.py` both use the shared helper and stage the served dir.
7/8/9. Phantom UI — removed `taskkeeper`/`secretary` from backend `_BUILTIN_APPS`; removed the frontend TaskKeeper Settings tab + panel + tk* client methods; removed the leftover Amazon Midway card from OverviewPage.
10. `dashboard/handlers/usage.py` — provider fallback `acp` → `claude_code` (matches new default).
11/12. `voice_reply.DEFAULT_PROVIDER` → piper (local); removed `use_aws` from default agent tools/allowedTools.
13/14/15 + cleanup. Removed dead code (embeddings `_create_from_gguf` + ollama-pull fallback to nomic-embed-text; tunnel/browser dead members; ARCC empty-constant call; quip connector refs); added launchd/systemd OLD-label migration; deleted orphaned `code-reviewer` FE app.

**Test-env fix:** added a deterministic in-memory `localStorage`/`sessionStorage` polyfill to `website/integration/setup.ts` (Node 25's native `--localstorage-file` storage was shadowing jsdom's spec-complete Storage, breaking ~700 FE tests on `localStorage.clear`). Methods live on `Storage.prototype` so quota-error spies still work.
