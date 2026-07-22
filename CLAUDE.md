# CLAUDE.md — KiroCrew (backend)

Guidance for Claude Code working in this repository. This file is the
high-signal quick reference; **`AGENTS.md` holds the exhaustive conventions**
(MCP-first rule, injected-message protocols, skill loading, widget protocol,
full module map). It is imported below so it is always in context — read it
before non-trivial changes. The frontend has its own `website/CLAUDE.md`.

@AGENTS.md

## What this is

KiroCrew is an open-source personal AI agent that runs on your own machine —
chat from Slack, a web dashboard, or the CLI; run multi-step tasks unattended;
schedule cron jobs; persist memory across sessions. It drives an LLM through
the KiroACP provider — the ACP adapter running the `kiro-cli` backend over the
ACP JSON-RPC protocol — plus MCP tools.

- **Backend:** Python package `kiro_crew` in `src/kiro_crew/` (~216 modules).
- **Frontend:** React + TS + Vite SPA in `website/`; built `dist/` is staged
  into `src/kiro_crew/static/dist/` and served by the backend.
- **Distribution:** public GitHub → `pip install` (backend) + `npm`/Vite
  (frontend). Plain setuptools — **no Brazil, no internal build tooling.**

## This is a public OSS fork — do not re-introduce Amazon-internal couplings

This repo is the de-Amazoned public fork of an internal package. When adding or
changing code, **never reintroduce** any of the following:

- Build/infra: Brazil (`Config`, `AUTOSDE.yaml`, `CODE_APPROVERS.yaml`),
  `npm-pretty-much`, toolbox bundler, AIM hooks, CodeArtifact registries.
  Use setuptools + public PyPI / public npm only.
- Services/auth: enterprise SSO, MCS, Kerberos, federated login, device-posture
  tunnels, Cognito/RUM ids, builder-mcp, `arcc`, Quip, Taskei/SIM/mimir. The
  internal marker names (Midway, mwinit, brazil, taskei, meshclaw, AIM, etc.)
  have been scrubbed from code, comments, and docs — do not reintroduce them.
  The `scripts/scrub-lint.sh` CI gate blocks regressions in the scanned source
  roots (`src/`, `website/src/`, `scripts/`, `config/`, `packaging/`, top-level).
  `docs/` is genericized but broadly allowlisted (it legitimately describes the
  platform-seam / SSO-marker concepts), so it is NOT gate-enforced — keep docs
  clean by convention.
- These subsystems are **stubbed** (`sso_status.py`, `browser/auth.py`,
  `dashboard/handlers/sso_login.py`, `tunnel/manager.py`, `aim_agents.py`): their
  public symbols are preserved as no-ops so the import graph stays intact — keep
  them stubbed, don't wire them back to internal services.
- KiroCrew is **KiroACP-only**: the sole provider is the ACP adapter driving
  the **`kiro-cli`** backend (`agent.provider` is fixed to `acp` and kiro-cli is
  REQUIRED). The standalone `ClaudeCodeProvider`, `BedrockProvider`, `cc_agent`,
  and `mirror` modules were deleted; the `claude_code`/`bedrock` factory
  branches, the `cc_*`/`bedrock_*` config fields, and the `[aws]` extra are
  gone. The dormant `ACP_BACKEND_CLAUDE` / `_is_claude` protocol seam in
  `acp/client.py` is intentionally kept so an internal companion can
  re-register Claude Code — do NOT delete it, but do NOT re-add the public
  registration glue either. (See the "Package Split" design.)
- Other OSS-flipped defaults (keep these): embeddings are **always-on and
  in-process** (vendored llama-cpp-python under `_vendor/`; the Qwen3 GGUF
  downloads over sha256-pinned HTTPS from the KiroCrew CDN — override via
  `KIROCREW_EMBED_MODEL_URL` / `memory.embed_model_url`; no Ollama server, no
  git/git-lfs — the `EmbeddingBackend` seam keeps other runtimes possible);
  voice TTS defaults to **Piper** (local), not Polly; Slack enterprise gate is
  default-open (opt-in allowlist via `slack.allowed_enterprise_ids`);
  `boto3` / `amazon-transcribe` are **optional** lazy imports for STT only
  (`pip install kirocrew[voice]`).

**Keep** the generic security controls (these are not Amazon-specific): AKIA/ASIA
credential redaction, destructive-command deny patterns, `~/.aws` / `~/.ssh`
sensitive-path blocking, SEL audit log.

**Fork-initiated UX divergences (do not let an upstream sync re-introduce):** the
artifact **Iterate** button is hidden (`SHOW_ARTIFACT_ITERATE` in
`ArtifactDetailPage.tsx`), the **Channels** app is hidden from the App Store
(`"hidden": True` on its `_BUILTIN_APPS` entry + the `AppsPage` Browse filter),
the **Board** app is removed, and the Voice panel adds a local **Piper** TTS
provider upstream lacks. These are launch product choices, recorded with their
exact mechanisms tracked with the upstream sync tooling (kept out-of-tree).

> Stale references: `website/Config` (Brazil) and `website/AUTOSDE.yaml` are
> leftover internal files not used by the public build — don't treat them as
> the build system, and don't add new ones.

## Platform layer: CPP seam + Governance (read before touching `platform/`)

`src/kiro_crew/platform/` is the **Composed Platform Providers (CPP)** edition
seam **and** the two-level security **Governance model**. It is load-bearing and
generic core infrastructure — it survives the upstream sync. See
`docs/system-specs/modules/platform-context.md` + `.../governance.md`, and
`AGENTS.md` → "Platform layer" for the full map.

- **CPP:** the core defines extension-point Protocols (`interfaces.py`) and ships
  a `Default*` adapter for each; `PlatformContext` (`context.py`) is the frozen
  bundle, read via `current_context()`. The core **never** imports a companion or
  branches on edition. `CONTRACT_VERSION` is **pinned at 1 pre-launch** (the
  companion rebuilds in lockstep, so the mismatch guard always sees `1 == 1`;
  pre-release field/interface additions land under v1 with no bump). Start
  incrementing only after the first public release.
- **Governance:** `governance.py` (archetypes + evaluator + policy loader) +
  `governance_profiles.py` (profile store + resolution). `effective = POLICY ∩
  PROFILE`, tightest-wins; the PreToolUse gate denies a tool/MCP call even if the
  kiro agent granted it. The evaluator is scope-name-agnostic — adding a scope is
  a `SCOPE_CATALOG` data change, never an evaluator edit.
- **Keystone (do NOT weaken):** `~/.kirocrew/security_policy.json`, `profiles/`,
  and `admission_policy.json` are in `security._SENSITIVE_HOME_DIRS` so the agent
  cannot read/write its own ceiling. This is the single mechanism that makes the
  ceiling un-disableable. When editing `security.py`'s sensitive-path or
  bash-command matchers, keep these covered (incl. write/extract verbs).
- This layer is the seam an internal companion composes against — keep the
  `ACP_BACKEND_CLAUDE` seam and `platform/` extension points intact; don't add
  public registration glue, and keep the stubs stubbed.

## Build / install

```bash
# Frontend first (so the dashboard is bundled), then backend:
cd website && npm install && npm run build      # → website/dist
cp -R website/dist ../src/kiro_crew/static/dist  # stage into the package
cd .. && pip install -e ".[voice]"               # editable; [voice] = STT extras

# Or use the Makefile (does frontend build + dist staging + venv install):
make build
```

`kirocrew` and `kirocrew-browse` are installed onto `PATH`. Self-update is
`git pull` + rebuild + `pip install -e .` + execv restart (no toolbox/brazil).

## Test / lint / type-check

Run the full quality cycle before committing:

```bash
black src/kiro_crew test && isort src/kiro_crew test
flake8 src/kiro_crew test
mypy src/kiro_crew
python -m pytest                 # full suite: -n auto worksteal, --cov (from setup.cfg)
```

**Gotcha — `setup.cfg` hardcodes `--cov` in `addopts`.** Coverage adds heavy
overhead and conflicts with selective runs. For fast iteration, override it:

```bash
# Only tests affected by your changes:
python -m pytest --testmon --override-ini="addopts=-v --ignore=build/private --durations=5 --color=yes" -q
# Single file / keyword:
python -m pytest test/test_dashboard_chat.py --override-ini="addopts=" -p no:cacheprovider -q
python -m pytest -k "flush_segment" --override-ini="addopts=" -p no:cacheprovider -q
```

- Async tests **must** carry `@pytest.mark.asyncio` (`asyncio_mode=strict`).
- Mock external processes (`kiro-cli`) — never spawn real ones in tests.
- `TestCleanupLoopResilience` in `test_session.py` is timing-flaky under
  parallel load but passes in isolation.

## Code style (essentials — see `AGENTS.md` for the full table)

- Line length 100 (black). Python ≥ 3.9; `from __future__ import annotations`.
- `import logging` + `logger = logging.getLogger(__name__)`.
- `asyncio` for all I/O; `@dataclass` for data containers.
- **No hardcoded strings/values in business logic** — constants live in
  designated modules (`AGENTS.md` lists each one).
- flake8 enforces no unused imports (F401), pep8-naming (N806), W504.
- **Never use emojis in the UI** — frontend uses `lucide-react`. See
  `website/CLAUDE.md`.

## MCP-first rule

When adding an LLM-facing CLI command, **also add it as an MCP tool**
(`mcp_cron.py` / `mcp_core.py`). The LLM reliably calls MCP tools but may refuse
bash CLI commands. `kirocrew-cron` + `kirocrew-core` are the managed servers
(`agent.py:_MANAGED_MCP_SERVERS`). Full CLI↔MCP mapping is in `AGENTS.md`.

## Platform support

macOS, Linux (x86_64 and ARM/Graviton), **and Windows** (native).
Route every POSIX-only process/signal/metrics/file-lock call through
`kiro_crew.platform_compat` — never raw `os.getuid`/`os.killpg`/`os.getpgid`/
`signal.SIG*`/`fcntl`/`os.kill(pid, 0)` (the last *terminates* the target on
Windows). The shim keeps macOS + Linux behavior byte-for-byte identical while
adding Windows fallbacks (`taskkill`/`netstat`/`msvcrt.locking`/WMI). See
`docs/WINDOWS_INSTALL.md` for the Windows install path and AGENTS.md "Platform
Support" for the full shim table. Verify process management, signal handling,
file locking, and system-metrics changes on macOS + Linux (Windows-only
branches don't execute on the Linux CI host — that's expected).

## Git conventions

- **GitHub is the home of this repo; `main` is the default branch.** Land work
  via a Pull Request against `main` (branch → PR → CI checks → review → merge) —
  the standard GitHub flow. Full steps in
  [CONTRIBUTING.md](CONTRIBUTING.md) → "Pull Request Workflow". There is no
  Brazil/GitFarm/`cr` path for this repo.
- Do **not** `git commit` or `git push` unless the user explicitly asks.
  Pushing requires separate explicit approval even after a commit.
- Commit format: `<type>: <summary>` (≤72 chars, imperative, lowercase, no
  period), types `feat|fix|refactor|docs|test|chore`, body wrapped at 72.

## Specs

When changing documented behavior, read the relevant spec in
`docs/system-specs/modules/` first and update it **in the same commit**. Do not
create new top-level markdown files unless explicitly asked.
