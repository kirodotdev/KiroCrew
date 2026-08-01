# Rules for AI Assistants

This is the **single source of truth** for working in this repository (the
frontend has its own `website/AGENTS.md`). Read it before non-trivial changes.

## What this is

KiroCrew is an open-source personal AI agent that runs on your own machine —
chat from Slack, a web dashboard, or the CLI; run multi-step tasks unattended;
schedule cron jobs; persist memory across sessions. It drives an LLM through the
KiroACP provider — the ACP adapter running the `kiro-cli` backend over the ACP
JSON-RPC protocol — plus MCP tools.

- **Backend:** Python package `kiro_crew` in `src/kiro_crew/`.
- **Frontend:** React + TS + Vite SPA in `website/`; built `dist/` is staged into
  `src/kiro_crew/static/dist/` and served by the backend.
- **Distribution:** public GitHub → `pip install` (backend) + `npm`/Vite
  (frontend). Plain setuptools — **no Brazil, no internal build tooling.**
- **Data home:** `~/.kiro/crew` (nested under kiro-cli's `~/.kiro/`); the legacy
  `~/.kirocrew` is auto-migrated. Override with `KIROCREW_HOME`.

## This is a public OSS fork — do not re-introduce internal couplings

This repo is the de-Amazoned public fork of an internal package. When adding or
changing code, **never reintroduce** any of the following:

- Build/infra: Brazil (`Config`, `AUTOSDE.yaml`, `CODE_APPROVERS.yaml`),
  `npm-pretty-much`, toolbox bundler, AIM hooks, CodeArtifact registries. Use
  setuptools + public PyPI / public npm only.
- Services/auth: enterprise SSO, MCS, Kerberos, federated login, device-posture
  tunnels, Cognito/RUM ids, builder-mcp, `arcc`, Quip, internal ticketing. The
  internal marker names have been scrubbed from code, comments, and docs — do not
  reintroduce them. `scripts/scrub-lint.sh` gates the scanned source roots
  (`src/`, `website/src/`, `scripts/`, `config/`, `packaging/`, top-level); `docs/`
  is allowlisted (it legitimately describes platform-seam / SSO-marker concepts)
  so keep docs clean by convention.
- These subsystems are **stubbed** (`sso_status.py`, `browser/auth.py`,
  `dashboard/handlers/sso_login.py`, `tunnel/manager.py`, `aim_agents.py`): their
  public symbols are preserved as no-ops so the import graph stays intact — keep
  them stubbed.
- KiroCrew is **KiroACP-only**: the sole provider is the ACP adapter driving
  `kiro-cli` (`agent.provider` is fixed to `acp`; kiro-cli REQUIRED). The
  standalone `ClaudeCodeProvider`/`BedrockProvider`/`cc_agent`/`mirror` modules,
  the `claude_code`/`bedrock` factory branches, the `cc_*`/`bedrock_*` config
  fields, and the `[aws]` extra are gone. The dormant `ACP_BACKEND_CLAUDE` /
  `_is_claude` seam in `acp/client.py` is intentionally kept so an internal
  companion can re-register Claude Code — do NOT delete it, and do NOT re-add the
  public registration glue.
- OSS-flipped defaults (keep): embeddings are **always-on and in-process**
  (vendored llama-cpp-python under `_vendor/`; Qwen3 GGUF over sha256-pinned HTTPS
  from the KiroCrew CDN, override via `KIROCREW_EMBED_MODEL_URL` /
  `memory.embed_model_url`; the `EmbeddingBackend` seam keeps other runtimes
  possible); voice TTS defaults to **Piper** (local); Slack enterprise gate is
  default-open (opt-in allowlist via `slack.allowed_enterprise_ids`); `boto3` /
  `amazon-transcribe` are optional lazy imports for STT (`pip install
  kirocrew[voice]`).

**Keep** the generic security controls (not internal-specific): AKIA/ASIA
credential redaction, destructive-command deny patterns, `~/.aws` / `~/.ssh`
sensitive-path blocking, SEL audit log. The deny patterns are first-class
`DeniedCommandRule` records (`BUILTIN_DENIED_RULES`, **137 rules**) enforced only
at the `hooks.py` PreToolUse gate; default-ON but user-configurable from Settings
→ Security, with the governance `commands` scope as the un-opt-out-able enterprise
force-pin. See `docs/system-specs/modules/security.md`.

**Fork-initiated UX divergences (do not let an upstream sync re-introduce):** the
**Channels** app is hidden from the App Store, the **Board** app is removed, and
the Voice panel adds a local **Piper** TTS provider. These are launch product
choices tracked out-of-tree with the upstream sync tooling.

The artifact **Iterate** divergence is retired: `SHOW_ARTIFACT_ITERATE` gated the
old navigate-away flow "pending an artifact redesign", and the embedded companion
chat panel IS that redesign — so the flag is deleted and the affordance ships. Do
NOT re-add the gate. See `docs/system-specs/modules/artifacts.md` § Companion Chat.

## Platform layer: CPP seam + Governance (read before touching `platform/`)

`src/kiro_crew/platform/` is the **Composed Platform Providers (CPP)** edition
seam **and** the two-level **Governance model** — load-bearing generic core
infrastructure that survives the upstream sync. Full map:
`docs/system-specs/modules/platform-context.md` + `.../governance.md` and the
"Platform layer" section below.

- **CPP:** the core defines extension-point Protocols (`interfaces.py`) and ships a
  `Default*` adapter for each; `PlatformContext` (`context.py`) is the frozen
  bundle, read via `current_context()`. The core **never** imports a companion or
  branches on edition. `CONTRACT_VERSION` is **pinned at 1 pre-launch**.
- **Governance:** `governance.py` + `governance_profiles.py`. `effective = POLICY ∩
  PROFILE`, tightest-wins; the PreToolUse gate denies a tool/MCP call even if the
  kiro agent granted it. The evaluator is scope-name-agnostic — adding a scope is a
  `SCOPE_CATALOG` data change, never an evaluator edit.
- **Keystone (do NOT weaken):** `security_policy.json`, `profiles/`, and
  `admission_policy.json` under the data home are in `security._SENSITIVE_HOME_DIRS`
  so the agent cannot read/write its own ceiling — the single mechanism that makes
  the ceiling un-disableable. When editing `security.py`'s sensitive-path or
  bash-command matchers, keep these covered (incl. write/extract verbs).
- **Computer use is deliberately NOT governed.** It is ONE operator opt-in on the
  keystone `computer_use.json` (fenced by `security._SENSITIVE_HOME_DIRS`, so the
  agent can neither read nor flip it). There are **no** `computer_use.*` scopes, no
  `capabilities.computer_use*` rows, no approval ordinal and no pointer permit — an
  earlier revision had all eight and they were removed by product decision. Do NOT
  reintroduce them without that decision being reversed; `gate.py` is audit-only and
  always permits.
  What still refuses, all enforced **in band** on the `tools._dispatch` path (never
  at the `hooks` PreToolUse gate, which is fail-OPEN and can be skipped by a
  pre-authorized tool): the keystone enable, **KiroCrew's own window**
  (`policy.check_app` — driving our Settings UI would route around the keystone),
  secure/password fields, the sensitive-text scan, and credential redaction. Keep
  these on the dispatch path. Secure-field redaction is an **always-on floor with no
  policy key** — never make it governable.
  **Element-targeted, non-pointer input is the DEFAULT.** `click_method: "global"` is
  the only path that warps the operator's REAL pointer; it needs no separate opt-in,
  but the model MUST NAME it — `auto` must NEVER resolve onto it (load-bearing
  invariant with its own test; it is now the only thing between an ordinary click and
  the user's cursor). Every such gesture is SEL-audited under its own `tool_kind`.
  `sky_click` is deliberately not ported (private SkyLight API).
  Flipping the enable RESTARTS chat sessions (`_reset_all_sessions`), because
  kiro-cli caches `tools/list` per session and ACP has no `tools/list_changed`.
  Keep prose in sync: `config/prompt.md` and `builtin_skills/computer-use/SKILL.md`
  ship to users and must not describe refusals that no longer exist. See
  `docs/system-specs/modules/computer-use.md` + `.../governance.md`.
- Keep the `ACP_BACKEND_CLAUDE` seam and `platform/` extension points intact; don't
  add public registration glue, and keep the stubs stubbed.

## Build / install

```bash
# Frontend first (so the dashboard is bundled), then backend:
cd website && npm install && npm run build      # → website/dist
cp -R website/dist ../src/kiro_crew/static/dist  # stage into the package
cd .. && pip install -e ".[voice]"               # editable; [voice] = STT extras
# Or: make build   (frontend build + dist staging + venv install)
```

`kirocrew` and `kirocrew-browse` are installed onto `PATH`. Self-update is
`git pull` + rebuild + `pip install -e .` + execv restart.

## Platform support

macOS, Linux (x86_64 and ARM/Graviton), **and Windows** (native). Route every
POSIX-only process/signal/metrics/file-lock call through
`kiro_crew.platform_compat` — never raw `os.getuid`/`os.killpg`/`os.getpgid`/
`signal.SIG*`/`fcntl`/`os.kill(pid, 0)` (the last *terminates* the target on
Windows). See `docs/windows-install.md` and the "Platform Support" shim table
below. Verify process/signal/file-lock/metrics changes on macOS + Linux.

## Specification Management

When working on code changes:
- MUST read the relevant module specification from `docs/system-specs/modules/` before making changes
- SHOULD read relevant shared pattern specifications from `docs/system-specs/common/`
- MUST update the appropriate system specification when making changes that impact APIs, schemas, or documented behavior
- MUST include specification updates in the same commit as code changes
- MUST NOT create additional markdown files unless explicitly instructed

When creating task specifications:
- MUST store in `docs/task-specs/YYYY/MM/${task-id}/`
- MUST ignore `docs/task-specs/` for current system context (archived only)

## Development Workflow

1. Read the relevant spec in `docs/system-specs/modules/` first
2. Update specifications when making changes that impact documented behavior
3. Format, lint, type-check, and test before committing (see Build Commands below)
4. Commit with well-formed message (see Git Conventions below)

### Dev Mode

Set `KIROCREW_HOME=.kirocrew-dev` to use an isolated data directory in the repo root (gitignored). This keeps dev data (contacts, lessons, config) separate from your real `~/.kiro/crew`. Data files are visible in the IDE for easy inspection.

Set `KIROCREW_PORT=6777` so dev and production gateways can run side by side. Pass the same env var to `./dev-frontend.sh` so the Vite proxy points at the dev backend.

To authenticate the Vite dev server (port 3000) against the dev gateway (port 6777):

1. Generate a token for the dev instance: `kirocrew token --port 6777`
2. Take the URL, replace `:6777` with `:3000` → `http://localhost:3000?token=xxx`
3. The Vite dev server proxies the token to the backend, sets the auth cookie, and redirects to `/`

### Build Commands

The backend is a standard Python package (pip/setuptools). Run the full quality cycle before committing:

```bash
# 1. Auto-format
black src/kiro_crew test
isort src/kiro_crew test

# 2. Lint + type-check
flake8 src/kiro_crew test
mypy src/kiro_crew

# 3. Test
python -m pytest
```

If all four pass cleanly, you're done. Fix any reported errors (flake8, mypy, pytest) and re-run.

For the frontend (in `website/`):

```bash
npm run build    # production bundle into src/kiro_crew/static/dist/
npm run test     # vitest unit/integration tests
```

### Common Lint/Type Pitfalls

- Flake8 enforces **no unused imports** (F401) — remove any import not directly used in the file
- Flake8 enforces **pep8-naming** (N806) — variables inside functions must be lowercase (use `mock_client` not `MockClient`)
- Flake8 enforces **W504** — line break before binary operator, not after
- mypy requires **type annotations** on untyped collections (e.g. `output: list[str] = []`)
- Use `# type: ignore[arg-type]` sparingly for third-party API mismatches
- `asyncio: mode=strict` — every async test MUST have `@pytest.mark.asyncio`

### Selective Test Execution (testmon)

For faster iteration during development, use `pytest-testmon` to run only tests affected by changed files instead of the full suite. This uses dependency tracking to skip unaffected tests.

```bash
# Fast iteration — only tests affected by your changes.
# Coverage is NOT in addopts, so you no longer need an override to avoid it;
# override only when you actually want to change the xdist flags.
# Multi-test runs MUST keep the xdist flags (-n auto --dist loadgroup) and the
# restart cap: loadgroup keeps @pytest.mark.xdist_group-serialized tests on one
# worker, a bare addopts override silently drops it (producing flaky races), and
# --max-worker-restart=2 turns worker loss into a fast loud failure instead of a
# long silent thrash.
python -m pytest --testmon --override-ini="addopts=-v --ignore=build/private -n auto --dist loadgroup --max-worker-restart=2 --durations=5 --color=yes" -q 2>&1 | tail -25

# Only previously failed tests:
python -m pytest --lf --override-ini="addopts=-v --ignore=build/private -n auto --dist loadgroup --max-worker-restart=2 --durations=5 --color=yes" -q

# Specific test file only — use -n0; per-worker startup dominates a small
# selection (one test measured 36.9s under -n 2 vs ~1.4s under -n0):
python -m pytest test/test_dashboard_chat.py -n0 -q

# Specific test by keyword:
python -m pytest -k "flush_segment" -n0 -q
```

**When to use which:**

| Scenario | Command |
|----------|---------|
| Iterating on a single task | `python -m pytest --testmon ...` |
| Debugging a specific failure | `python -m pytest --lf ...` or `-k "test_name"` |
| Checkpoint / pre-commit | `black && isort && flake8 && mypy && python -m pytest` |

**Note:** The `--override-ini` flag is needed because `setup.cfg` hardcodes `--cov` flags in `addopts` which conflict with selective runs. `--cov` enables coverage measurement and adds significant overhead — skip it during iteration.

## Frontend Integration Tests

Frontend code lives in the `website/` directory.
See `website/README.md` for integration test (MSW) and E2E test (Playwright) instructions.
All integration tests MUST pass before committing frontend changes.

### Browser E2E gate — `python setup.py test_e2e`

`python setup.py test_e2e` (defined in `setup.py::E2eTestCommand`, folds `test/test_playwright_e2e.py`) is the offline browser-E2E gate. It boots a real gateway wired to the packaged fake ACP backend (`kiro_crew.testing.fake_acp_backend`, `KIROCREW_KIRO_BIN`) and shells the in-tree `website/playwright` suite against it — no model, no network. It uses Playwright's own bundled Chromium and runs serially with a long per-test timeout, so it is **not** part of the default `pytest` unit run (too slow for the per-commit gate).

- The test skips gracefully when the `website` dir or its Playwright CLI can't be resolved (a python-only checkout). Set `KIROCREW_E2E_REQUIRE=1` on a CI job to turn an environment-resolution miss into a hard failure so drift is caught at PR time rather than going green on zero specs.

## Git Conventions

- **This repo lives on GitHub; `main` is the default branch.** Changes land through the standard GitHub Pull Request flow — branch off `main`, push, open a PR, let CI + review pass, then merge. See [CONTRIBUTING.md](CONTRIBUTING.md) → "Pull Request Workflow" for the full steps. This is a public OSS project — there is no Brazil/GitFarm or `cr` review path.
- Do NOT proactively `git commit` or `git push` — only when explicitly requested by the user
- Do NOT run `git push` unless the user explicitly says to push. Committing is OK when asked, but pushing requires separate explicit approval.

Commit messages follow this format:

```
<type>: <summary> (max 72 chars)

<body — explain what and why, not how>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`

Examples:
```
feat: add ACP client for kiro-cli JSON-RPC communication
fix: handle EOF in ACP message reader
docs: add system specs for acp-client module
test: add unit tests for config loader
refactor: extract permission handling from prompt reader
```

Rules:
- Summary line: imperative mood, lowercase, no period
- Body: wrap at 72 chars, explain motivation
- One logical change per commit

### Release Changelog

When writing a release entry in `CHANGELOG.md`:

1. **Header**: `## [X.Y.Z] — YYYY-MM-DD`
2. **Sections** in order: Features, Fixes, Refactors, Docs
3. **Features** — bold title + plain-language description with use cases. Explain *why* it matters, not implementation details
4. **Fixes / Refactors / Docs** — one-line bullets, no bold, no implementation details
5. **Contributors** — `Full Name (github-handle)` format
6. Keep it scannable — a reader should understand the release in 30 seconds

Example:
```markdown
## [1.0.2] — 2026-03-20

### Features

- **Inline markdown viewer** — Click file paths in chat to open a side panel with edit/preview/save. Useful for reviewing configs or editing code without leaving the chat.

### Fixes

- `kirocrew learn` CLI now reads from vector store instead of only JSONL
- IME composition: CJK Enter no longer triggers message send

### Contributors

Jane Doe (janedoe), John Smith (jsmith)
```

## Code Style

| Rule | Requirement |
|------|-------------|
| Line length | 100 chars (black configured) |
| Python version | ≥ 3.9 (`from __future__ import annotations` for type hints) |
| Imports | Use `import logging` + `logger = logging.getLogger(__name__)` |
| Error handling | Custom exceptions in `acp/client.py`; return error strings at tool boundaries |
| Async | Use `asyncio` throughout; `async def` for all I/O operations |
| Dataclasses | Use `@dataclass` for data containers |
| Constants | No hardcoded strings/values in business logic. Protocol strings in `acp/types.py`, timeouts in `acp/client.py`, UX strings in `slack/handler.py`, credential keys in `config/loader.py`, hook results in `hooks.py`, memory paths in `memory.py`, lesson limits in `learn.py`, cron limits in `cron.py`, MCP protocol version in `mcp_cron.py`, MCP protocol version in `mcp_core.py`, dashboard port in `dashboard/origin.py`, built-in skills in `skills/*/SKILL.md`, provider event kinds in `providers/base.py`, session limits in `history.py`, context budgets in `context.py` (preferences 1k, projects 2k, history 6k, lessons 1k, conversation 8k, cross-tab 2k, per-message 800, total 50k), task states in `task.py`, heartbeat intervals in `heartbeat.py`, subagent limits in `subagent.py`, agent config in `agents/defaults.json`, shutdown signal in `__init__.py`, slot message cap (5000) in `dashboard/state.py`, JSONL rotation (2MB) in `history.py`, usage cache (600s) in `dashboard/handlers/usage.py`, webhook hook limits (max 6 concurrent, 50KB message, 600s default / 3600s max timeout) in `dashboard/handlers/hooks.py`, embed cache (128) in `embeddings.py` |
| Naming | Module-level constants: `UPPER_SNAKE_CASE`. Private constants: `_UPPER_SNAKE_CASE` |
| Icons | **Never use emojis in the UI.** Use `lucide-react` components with `className="lucide-inline"`. See `website/AGENTS.md` for full icon conventions. |

## Test Patterns

- Test files: `test/test_<module>.py`
- Use `pytest` with `pytest-asyncio` for async tests
- Use `tmp_path` fixture for filesystem tests
- Use `monkeypatch` for config overrides
- Mock external processes (kiro-cli) — never spawn real processes in tests
- Group related tests in classes: `class TestFeatureName:`

## Architecture Principles

- LLMProvider ABC is the interface for all LLM backends (`providers/base.py`)
- ACP provider wraps kiro-cli (JSON-RPC 2.0 over stdio) — full tool execution, session management, and auto-compaction. Backend selection:
  - `"acp"` (required): spawns `kiro-cli acp --agent <name>`
- Config-driven provider selection: `"provider": "acp"` (kiro-cli) is fixed/required
- Tool permissions auto-approved in phase 1; interactive approval in phase 3
- Config loaded from `~/.kiro/crew/config.json` with dataclass defaults
- CLI uses `argparse` (stdlib only, no external deps)
- Minimal dependencies — prefer stdlib over third-party packages

### New Modules (since v1.1.0)

| Module | Purpose |
|--------|---------|
| `autonudge.py` | Reactive same-session self-nudge service |
| `snapshot.py` | Portable snapshot and restore for KiroCrew state |
| `vector_memory.py` | Vector-based semantic memory with FAISS |
| `watchdog.py` | Session cleanup watchdog (CleanupHook dispatcher; RSS-threshold recycle) |
| `voice_reply.py` | Voice reply synthesis |
| `atomic_write.py` | Atomic file write utilities |
| `constants.py` | Shared constants |
| `llm_helpers.py` | LLM helper utilities |
| `git_coord.py` | Git coordination utilities |
| `apps/` | App Kit platform (manifest, manager, registry, routes, scaffold, backend, bridges, permissions, dependencies, dependency_ledger, version) |
| `eval/` | Multi-session eval harness (runner, judge, scenario) |
| `channel.py` | Persistent agent channels for multi-agent collaboration |
| `conductor_skill.py` | Agent delegation conductor |
| `context_management.py` | Conductor context isolation for multi-agent sessions |
| `sync_bridge.py` | Sync-to-async bridge for MCP tools |
| `agent_metadata.py` | Agent metadata extraction |
| `session_workspace.py` | Session workspace management |
| `mcp_shared.py` | Shared MCP utilities |
| `transcribe.py` | Voice memo STT (whisper or optional cloud transcription) |
| `slack/files.py` | Slack file/image/voice attachment handling |
| `slack/events.py` | Slack event dispatch (Home Tab, file handling, display names) |
| `slack/blocks.py` | Block Kit message builder |
| `slack/client.py` | Slack client abstraction (`SlackClientOps` ABC + `RealSlackClient`) |
| `slack/interactions.py` | Slack interactive component handling |
| `config/schema.py` | JSON Schema generation from config dataclasses |
| `aidlc/` | Project management models (Activity, Comment) for dashboard |
| `task_executor.py` | Task execution engine (extracted from taskrunner) |
| `task_models.py` | Task data models (extracted from taskrunner) |
| `task_planner.py` | Task planning engine (extracted from taskrunner) |
| `task_reporter.py` | Task reporting (extracted from taskrunner) |
| `dashboard/handlers/` | API handlers split into focused modules (core, sessions, messaging, files, cron, memory, agents, mcp, hooks, prompts, autonudge, taskrunner, updates, usage) |
| `dashboard/stt_stream.py` | Streaming speech-to-text WebSocket |
| `dashboard/_types.py` | Dashboard type definitions |
| `validation.py` | Input validation for cron, config, user actions |
| `embeddings.py` | In-process embedding runtime (vendored llama-cpp-python) behind the `EmbeddingBackend` ABC (swap seam via `register_embedding_backend`) + background model download; LRU embed cache (128 entries, keyed by text + model_id) |
| `_vendor/` | Vendored llama-cpp-python 0.3.34 (MIT) + per-platform native libs — never edit by hand; see `_vendor/README.md` for the upgrade procedure. Excluded from all linters |
| `session_map.py` | Session key → CWD/provider persistence (split from session.py) |
| `session_pid.py` | PID tracking for ACP processes (split from session.py) |
| `dashboard/handlers/optimizer.py` | Native prompt optimizer (Cmd+Shift+Enter) |
| `subagent_persistence.py` | Folder-per-agent persistence for orphan recovery |
| `doc_parser.py` | Stdlib-only .docx/.pdf/.pptx text extraction |
| `seed.py` | Gateway seed fixtures for reproducible testing |
| `cli_chat.py` | CLI chat commands (split from cli.py) |
| `cli_commands.py` | CLI utility commands (split from cli.py) |
| `cli_config.py` | CLI config commands (split from cli.py) |
| `cli_doctor.py` | CLI doctor diagnostics (split from cli.py) |
| `cli_server.py` | CLI gateway/server commands (split from cli.py) |
| `cli_setup.py` | CLI setup wizard (split from cli.py) |
| `dashboard/chat_runner.py` | Chat execution logic (split from `dashboard/chat.py`) |
| `computer_use/` | Native desktop GUI automation (macOS today; Windows/Linux refuse). `ComputerUseBackend` ABC + swap registry, the keystone primary enable, the audit-only `gate.py`, the in-band refusals in `policy.py`, and the ctypes driver — all in-gateway native work confined to `macos_ffi.py`. The **one documented exception**: `overlay_proc.py` (the Cursor Motion AppKit child) has its own ctypes surface because AppKit needs a main-thread run loop and the gateway's main thread is the asyncio loop, so it MUST be out of process. See `docs/system-specs/modules/computer-use.md`. |
| `platform/` | **Composed Platform Providers (CPP) seam + Governance model** — see the dedicated section below. |

### Platform layer: Composed Platform Providers (CPP) + Governance

`src/kiro_crew/platform/` is the **edition seam** and the **two-level security
governance model**. Touch it carefully — it is load-bearing for both the
public/standalone edition and the (out-of-repo) Amazon companion, and it is the
trust root for the security ceiling. Full spec:
`docs/system-specs/modules/platform-context.md` and
`docs/system-specs/modules/governance.md`.

**CPP seam** (one core, two editions; the core NEVER imports a companion):
- `interfaces.py` — extension-point Protocols. `context.py` — the frozen
  `PlatformContext` (one chosen adapter per interface) + `current_context()`
  (fail-closed lazy default) + `CONTRACT_VERSION` (**pinned at 1 pre-launch** — a
  companion built against a different version refuses to compose, but the
  companion rebuilds in lockstep so it always sees `1 == 1`). `defaults.py` — the `Default*`
  adapters that reproduce today's open-source behavior. `bootstrap.py` —
  `boot_platform`/`bootstrap_context` compose the context, assert the floors,
  install it. `discovery.py` / `profile.py` — companion discovery + edition
  resolution (`standalone` | `enterprise`). `security_authority.py` — the `@final`
  ADD-only deny floor (`PolicyAuthority` + `assert_security_floor`).
  `admission.py` — signed-plugin admission (the trust-root precedent the
  governance loader mirrors).
- **Reading the context:** module-level code reads `current_context()`; never
  hard-code an Amazon class or branch on the edition in the core.

**Governance model** (`governance.py` + `governance_profiles.py`): two levels,
`effective = POLICY ∩ PROFILE`, tightest-wins. Level 1 POLICY is the
enterprise security ceiling (`GovernanceCeiling`, loaded at boot from the
trust-root path — `KIROCREW_SECURITY_POLICY` env, else
`~/.kiro/crew/security_policy.json`; **never** merged from `config.json`); once
present the app + agent cannot weaken it. Level 2 PROFILE
(`~/.kiro/crew/profiles/<name>.json`) is a per-surface/app/task narrow-only
ceiling KiroCrew enforces at its OWN PreToolUse gate — it denies a tool/MCP call
even if the kiro agent config granted it. Every control is one of four
archetypes (ScopedRuleset / OrdinalControl / CapabilityGate / ScopedMap), each
with one composition algebra; the evaluator is **scope-name-agnostic** (dispatch
by archetype) so adding a scope/capability/transport is a `SCOPE_CATALOG` data
change, never an evaluator edit.

**Keystone:** the policy/profile/admission files live in
`security._SENSITIVE_HOME_DIRS` so the agent cannot read OR write its own ceiling
(`is_sensitive_path` is the shared gate; bash write/extract verbs are covered).
This single mechanism is what makes the ceiling un-disableable — do not weaken it.

**Denied commands** are first-class `DeniedCommandRule` records
(`BUILTIN_DENIED_RULES`, 137 rules) enforced **only** at the `hooks.py` PreToolUse
gate — not injected into `~/.kiro/agents/*.json` (the `agent._enforce_denied_commands`
path + `autoAllowReadonly` are retired; read-only auto-approve moved into
`hooks.py` after the deny/governance checks). They are default-ON but
**user-configurable from Settings → Security** (`config.json`
`hooks.denied_commands` = `disable_all`/`disabled_ids`/`user_added`); the
governance `commands` scope is the un-opt-out-able enterprise force-pin
(tightest-wins). Keep the generic security controls intact. See
`docs/system-specs/modules/security.md` + `governance.md`.

**If you change `platform/`:** read the two spec docs first and update them in
the same commit; leave `CONTRACT_VERSION` at **1** pre-launch (the companion
rebuilds in lockstep — no field/interface change bumps it until the first public
release); keep the evaluator scope-name-agnostic; keep the governance trust-root
files on the sensitive-path floor.

### Frontend Architecture

React + TypeScript SPA in the `website/` directory. Built assets are bundled into `src/kiro_crew/static/dist/`.

**All frontend conventions (icons, components, layout, styling, data fetching) are documented in `website/AGENTS.md`.** Refer to that file when making frontend changes.

### Platform Support

KiroCrew runs on macOS, Linux (x86_64 and ARM/Graviton), and **Windows** (native).
macOS/Linux install via the `bin/kirocrew` launcher; **Windows runs natively from a Python
source install** — CPython 3.12 + a venv + `pip install -e .`, launched as `python -m
kiro_crew gateway`. See `docs/windows-install.md`.

**All code changes MUST be verified for macOS + Linux + Windows compatibility:**
- **Backend**: macOS, Linux, Windows — route POSIX-only calls through `platform_compat`
- **Frontend**: Chrome, Firefox, Safari, Edge — use standard Web APIs only, guard browser-specific APIs (e.g. `typeof Notification !== 'undefined'`)

**`platform_compat.py` is the cross-platform shim — use it instead of raw POSIX calls.**
POSIX-only modules (`fcntl`, `termios`, `resource`, `pty`) do not exist on Windows, and
some `os` calls behave DIFFERENTLY there — most dangerously **`os.kill(pid, 0)` TERMINATES
the process on Windows** (it is not a liveness probe). Always go through the shim:

| Need | Use (`platform_compat`) | NOT |
|------|--------------------------|-----|
| File lock | `file_lock(fd, exclusive=)` / `acquire_lock`+`release_lock` / `try_acquire_lock` | `fcntl.flock` |
| Liveness probe | `pid_exists(pid)` / `pid_liveness(pid)` | `os.kill(pid, 0)` (kills on Windows!) |
| Kill a process | `kill_pid(pid, sig)` | `os.kill(pid, sig)` |
| Kill a tree | `kill_process_tree(pid, sig)` | `os.killpg(os.getpgid(pid), sig)` |
| Parent PID | `get_ppid(pid)` | `/proc` read / libproc |
| Match process cmdline | `process_matches(pid, needles)` | `/proc/<pid>/cmdline` / `ps` |
| Signals | `platform_compat.SIGKILL` / `SIGTERM` | `signal.SIGKILL` (undefined on Windows) |
| Spawn isolation | `start_new_session=IS_POSIX` + `creationflags=CREATE_NEW_PROCESS_GROUP` | bare `start_new_session=True` |
| File mode | `chmod_safe(path, mode)` / `fchmod_safe(fd, mode)` | `os.chmod` / `os.fchmod` (no `os.fchmod` on Windows) |
| Owner-only secret (fail-loud) | `restrict_to_owner(path)` | `os.chmod(path, 0o600)` under `if IS_POSIX` (silent no-op on Windows) |
| Process RSS / CPU | `proc_rss_bytes()` / `proc_cpu_seconds()` | `resource.getrusage` |
| FD soft limit | `raise_nofile_soft_limit(n)` | `resource.setrlimit` |
| Port -> PID | `find_listening_pids(port)` / `listening_pid_tool_available()` | `lsof` directly |
| strftime no-pad | `strftime(dt, "%-I")` | bare `dt.strftime("%-I")` (`ValueError` on Windows) |

Other Windows specifics:
- **tzdata**: Windows ships no system IANA tz database, so `zoneinfo.ZoneInfo(...)` raises —
  `tzdata` is declared in `setup.cfg` under a `platform_system == "Windows"` marker.
- **UTF-8 console**: `platform_compat.ensure_utf8_console()` runs first in `cli.main()` /
  `__main__` so non-ASCII output can't crash a cp1252 stdout; the gateway log handler is
  opened `encoding="utf-8"`.
- **Signal handling** (`slack/gateway.py`): `loop.add_signal_handler()` raises
  `NotImplementedError` on the Windows ProactorEventLoop — fall back to `signal.signal(SIGINT)`.
- **Web terminal / interactive SSO-login PTY** (`dashboard/handlers/terminal.py`): rely on
  `pty`/`fork`/`termios` — POSIX-only; they degrade to a clear "not supported on Windows"
  response rather than crashing.
- **System metrics** (`handlers_system.py`): macOS `sysctl`/`vm_stat`; Linux `/proc/*`;
  Windows via `platform_compat` ctypes helpers.
- **Frontend build** (`setup.py`): uses `/bin/bash build-frontend.sh`.
- **Launcher scripts**: `bin/kirocrew` (POSIX sh); Windows uses the pip console script.

### Skills & MCP Tools for the LLM

KiroCrew exposes capabilities to the LLM via two mechanisms:

1. **MCP tools** (native): kiro-cli calls them directly with structured JSON params — **preferred for all LLM-facing operations**
   - `kirocrew-cron` MCP server: `cron_list`, `cron_add`, `cron_update`, `cron_remove`, `cron_remove_all`, `cron_pause`, `cron_resume`, `cron_trigger`
   - `kirocrew-core` MCP server: `spawn_run`, `spawn_list`, `spawn_status`, `learn_add`, `learn_list`, `learn_remove`, `task_run`, `wait`, `register_hook`, `send_message`, `send_notification`, `local_knowledge_search`
   - `kirocrew-computer` MCP server (10 tools): `computer_list_apps`, `computer_get_state`, `computer_click`, `computer_drag`, `computer_type_text`, `computer_press_key`, `computer_set_value`, `computer_scroll`, `computer_perform_action`, `computer_end_turn` — native desktop GUI automation. Default-OFF behind a keystone primary enable (`~/.kiro/crew/computer_use.json`, NOT `config.json`); the stdio process is a thin shim and the authoritative fail-closed gate runs in the gateway. `computer_click` takes **either** `element_index` **or** `x`+`y` (never both) plus optional `click_count` (1-3), `mouse_button` (`left`/`right`/`middle`) and `click_method` (`auto`/`accessibility`/`app_post`/`global`); `computer_drag` is coordinate-only. Both keyboard tools (`computer_type_text`, `computer_press_key`) REQUIRE `element_index`: an unnamed target has no role/subrole, so the always-on secure-field (password) refusal could not inspect it. See `docs/system-specs/modules/computer-use.md`.
   - `playwright` MCP server (`@playwright/mcp`): `browser_navigate`, `browser_click`, `browser_snapshot`, `browser_take_screenshot`, `browser_fill_form`, `browser_type`, `browser_press_key`, `browser_evaluate`, `browser_hover`, `browser_drag`, `browser_select_option`, `browser_tabs`, `browser_close`, `browser_wait_for`, `browser_resize`
   - `slack-mcp` (mcpServers): Slack integration
   - Configured in `agents/defaults.json` → `mcpServers` → installed to `kirocrew.json`
   - `kirocrew-cron`, `kirocrew-core` and `kirocrew-computer` are managed MCP servers in `agent.py:_MANAGED_MCP_SERVERS` — auto-registered, refreshed preserving user customizations. `kirocrew-computer` is deliberately added to `tools` but **NOT** `allowedTools`, and its managed spec carries **no `autoApprove` key** (an autoApproved MCP tool never reaches `hooks.on_tool_call`)
   - MCP discovery (`mcp_discovery.py`): on-demand only — users trigger from dashboard "Discover & Sync" button

2. **Skills** (`skills/*/SKILL.md`): on-demand knowledge files for specialized workflows
   - Supports nested directories (e.g. `skills/utils/tiny-url/SKILL.md`)
   - Descriptions loaded at session start; full content loaded when the LLM reads the file
   - Skills with `always: true` in frontmatter have full content injected every session
   - Triggers support `!` prefix for negative matching (e.g. `triggers: search, code, !test` excludes when "test" appears)
   - Skills with auxiliary files (scripts, assets) include `dir` path so the LLM can `cd` and run them

#### IMPORTANT: MCP-First Rule

**When adding a new LLM-facing CLI command, MUST also add it as an MCP tool.**

kiro-cli reliably calls MCP tools but may refuse to run CLI commands via bash.
MCP tools are defined in:
- `mcp_cron.py` — cron scheduling tools
- `mcp_core.py` — spawn, learn, task tools
- External MCP servers — configured in `agents/defaults.json` → `mcpServers`

The CLI commands (`kirocrew spawn/learn/cron/run`) remain for human use but the LLM
should always use the MCP tool equivalents.

| CLI Command | MCP Tool | MCP Server |
|-------------|----------|------------|
| `kirocrew cron add` | `cron_add` | kirocrew-cron |
| `kirocrew cron list` | `cron_list` | kirocrew-cron |
| `kirocrew cron remove` | `cron_remove` | kirocrew-cron |
| `kirocrew cron remove-all` | `cron_remove_all` | kirocrew-cron |
| `kirocrew cron pause` | `cron_pause` | kirocrew-cron |
| `kirocrew cron resume` | `cron_resume` | kirocrew-cron |
| `kirocrew cron trigger` | `cron_trigger` | kirocrew-cron |
| `kirocrew cron update` | `cron_update` | kirocrew-cron |
| `kirocrew spawn run` | `spawn_run` | kirocrew-core |
| `kirocrew spawn list` | `spawn_list` | kirocrew-core |
| — | `spawn_status` | kirocrew-core |
| `kirocrew learn add` | `learn_add` | kirocrew-core |
| `kirocrew learn list` | `learn_list` | kirocrew-core |
| `kirocrew learn remove` | `learn_remove` | kirocrew-core |
| `kirocrew run TASK.md` | `task_run` | kirocrew-core |
| — | `wait` | kirocrew-core |
| — | `register_hook` | kirocrew-core |
| — | `send_message` | kirocrew-core |
| — | `send_notification` | kirocrew-core |
| — | `local_knowledge_search` | kirocrew-core |
| — | `file_send` | kirocrew-core |
| — | `autonudge_stop` | kirocrew-core |
| — | `ask_question` | kirocrew-core |
| — | `artifact_folder_list` | kirocrew-core |
| — | `artifact_folder_create` | kirocrew-core |
| — | `artifact_folder_rename` | kirocrew-core |
| — | `artifact_folder_move` | kirocrew-core |
| — | `artifact_folder_delete` | kirocrew-core |
| — | `artifact_move` | kirocrew-core |
| — | `artifact_get_comments` | kirocrew-core |
| — | `artifact_post_comment` | kirocrew-core |
| — | `artifact_mark_review` | kirocrew-core |
| — | `artifact_delete_comment` | kirocrew-core |
| `kirocrew computer apps` | `computer_list_apps` | kirocrew-computer |
| — | `computer_get_state` | kirocrew-computer |
| — | `computer_click` | kirocrew-computer |
| — | `computer_drag` | kirocrew-computer |
| — | `computer_type_text` | kirocrew-computer |
| — | `computer_press_key` | kirocrew-computer |
| — | `computer_set_value` | kirocrew-computer |
| — | `computer_scroll` | kirocrew-computer |
| — | `computer_perform_action` | kirocrew-computer |
| — | `computer_end_turn` | kirocrew-computer |
| `kirocrew computer call <tool>` / `call --calls '[…]'` | — (deliberately none) | — |
| — | `browser_navigate` | playwright |
| — | `browser_click` | playwright |
| — | `browser_snapshot` | playwright |
| — | `browser_take_screenshot` | playwright |
| — | `browser_fill_form` | playwright |
| — | `browser_type` | playwright |
| — | `browser_evaluate` | playwright |
| — | `browser_close` | playwright |

- **Handler keywords**: only for instant user-typed commands with no LLM round-trip (e.g. `cron list`, `spawn list`)
- **Do NOT** add regex to match NL variants — the LLM handles NL interpretation
- **`kirocrew computer call` is the one deliberate "no MCP twin" row.** It is not a capability — it is a human debug/repro harness that runs the ten existing `computer_*` tools through the same gated chokepoint (optionally a JSON array of them in ONE process, so `element_index` values stay resolvable). The MCP-first rule exists so the model gets a structured tool instead of shelling out, and the model already has all ten. A tool that runs other tools would let a model launder one per-call gate decision into many — so do NOT add `computer_call`.

#### Project-Level Configuration

Agent config and skills live in top-level project directories for easy editing without code changes:

```
agents/                  ← agent config (edit without rebuilding)
├── defaults.json        ← base agent config (tools, model, permissions)
├── prompt.md            ← system prompt
└── README.md

skills/                  ← on-demand skill definitions (edit without rebuilding)
├── utils/tiny-url/SKILL.md       ← nested directories supported
└── README.md
```

- `KIROCREW_PROJECT_DIR` env var points to the project root
- Auto-detected from CWD at CLI startup (walks up looking for `agents/` + `skills/`)
- Saved to `~/.kiro/crew/project_dir` during `kirocrew setup` so it works from any directory
- Falls back to bundled copies in the Python package if project dir not found

#### Skill Bundling: `builtin_skills/` vs. top-level `skills/` (MUST read before adding a skill)

There are **two** places a skill can live, and they ship very differently:

- **`src/kiro_crew/builtin_skills/`** — **bundled** into the Python package. On
  gateway start, `_ensure_builtin_skills()` copies these into the user's
  `~/.kiro/crew/skills/`, so **every** `pip` / DMG install receives them
  automatically. This is the ONLY path that reaches end users.
- **top-level `skills/`** — a **repo-checkout-only** convenience dir loaded via
  `KIROCREW_PROJECT_DIR` when running from a source tree. It is **NOT** packaged,
  so `pip`/DMG users never get these skills.

**Rule — decide by whether the skill is load-bearing for a shipped feature:**

- If a skill is **fundamentally required** for a feature to work as designed
  (the feature, MCP tool, or docs reference it, or it breaks/degrades without the
  skill), it **MUST** live under `src/kiro_crew/builtin_skills/` so it is bundled
  and reaches all users. Do NOT ship a required skill only in top-level `skills/`
  — that leaves installed users without it (this has bitten us: `prepare-pr`,
  `artifacts`, and `widgets` were code-referenced but lived only in `skills/`, so
  DMG users silently ran without them until they were promoted to
  `builtin_skills/`).
- Only use top-level `skills/` for **reference-only or optional/suggestion**
  skills — nice-to-have knowledge that no shipped feature depends on.

When in doubt (a feature or docs mention the skill by name), treat it as required
and bundle it. Nested layout is preserved on copy (e.g.
`builtin_skills/kirocrew-dev/prepare-pr/`).

#### Skill Loading

1. **Always-on skills**: skills with `always: true` in YAML frontmatter have full content injected into every new session context
2. **On-demand skills**: skill summaries (name + description + dir path) are in session context. Matched via word-overlap scoring with negative trigger support — skills are loaded when message words overlap with trigger phrases and no `!`-prefixed negative trigger matches.
3. **Nested directories**: skills can be organized in subdirectories (e.g. `skills/utils/tiny-url/`). The skill name is the relative path.

#### Example Flows

- User says "report system status every 5 minutes" → LLM calls `cron_add` MCP tool directly → CronService writes to crons.json → gateway picks it up
- User says "poll a status endpoint every 5 min, don't carry context between runs" → LLM calls `cron_add` with `persistent_session: false` → each run opens a fresh session, no `last_result` prefix, session context cannot accumulate.
- User says "remember to always use X" → LLM calls `learn_add` MCP tool directly → saved to lessons.jsonl
- User says "run 4Sum in 6 languages in parallel" → LLM calls `spawn_run` MCP tool 6 times → gateway spawns subagents

## Injected Messages

Messages from automated sources may appear in your conversation. These are **not typed by the user** — treat them as automated input and respond accordingly.

### Cron notifications
```
[Cron notification from "job name"]
<content from the cron agent>
[End of cron notification]
```
A cron job sent this via `send_message(session="origin")`. This appears as an `inject` role message (⏰ icon) in the dashboard chat and triggers an agent turn. The session should process it automatically — e.g., if a cron reports a build failure, fix it. The user may not be present.

### Subagent completions
```
[Subagent completion event]
Agent <id> completed ✅
<result>
```
A background subagent finished its task. This is injected directly into the LLM context (not visible in the dashboard chat). Synthesize the result into your response — your reply is what the user sees.

### Subagent delivery failures
```
[Subagent completion event]
Agent <id> ❌ delivery timed out
Task: <task preview>
The agent finished but result delivery timed out.
Result saved at: <path>
Use the read tool to retrieve it if needed.
```
The subagent completed but injection timed out. Result is on disk — use `read` tool if needed.

## File Attachments

Users attach files via `@filename` syntax in chat input. The file picker searches the active project directory.

- `@relative/path` tokens resolved to full paths
- Image files rendered inline as markdown `![image](path)`
- Non-image files sent as `[attached_file N] /full/path`
- The `[PROJECT]` context entry tells you which directory is active

## Widget Protocol

Widgets rendered via `<mcwidget title="Title">HTML</mcwidget>` now support bidirectional communication:

- Widgets can emit `data-action` events back to the agent session
- Use `window.parent.postMessage({type: 'kirocrew:action', action: 'name', payload: {...}}, '*')` from widget JS
- The agent receives these as `[Widget action event]` messages in the conversation
- Tailwind CSS and theme variables (`var(--bg)`, `var(--text)`, etc.) are available in all widgets

## Service Management

KiroCrew can run as a system service:

```bash
kirocrew service install    # systemd (Linux) or launchd (macOS)
kirocrew service status
kirocrew service uninstall
```

## Steering Files

Workspace `.kiro/steering` files are automatically loaded into kiro-cli sessions. Place project-specific rules in `.kiro/steering/*.md` and they apply without manual configuration.

## App SDK Hooks

Apps can register gateway-level hooks via the App SDK:

- Gateway hooks fire for lifecycle events (session start/end, tool call, message)
- `ChatEmbed` component lets apps embed a full chat interface within their UI

## Testing Conventions

- Frontend: jscpd duplication check — copy-paste code fails the build
- Frontend: vitest coverage emitted as cobertura XML
- Backend: pytest-timeout enforced, xdist loadgroup mode for parallel execution (`--dist loadgroup` so `xdist_group`-marked tests serialize on one worker)
- Backend: security-critical modules require 80%+ coverage

## Gateway Test Harness

For integration tests and eval harnesses, use composable CLI flags:

```bash
kirocrew gateway --test-mode                    # bundle: ephemeral port + json-ready + reads approval
kirocrew gateway --port auto --json-ready       # OS-assigned port, prints KIROCREW_READY:{port,token,pid,home}
kirocrew gateway --approval reads               # auto-approve read-only tools
kirocrew gateway --approval yolo                # auto-approve ALL tools (requires KIROCREW_HOME != ~/.kiro/crew)
```

Safety: `--approval yolo` refuses to start unless `KIROCREW_HOME` is explicitly set to a non-default path.
