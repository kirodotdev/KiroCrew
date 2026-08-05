# Rules for AI Assistants

**This file is a ROUTER, not a manual.** It carries only the rules whose violation
causes damage before a pointer could be read. Everything else is a link you MUST
open before touching that subsystem: see
[Read before you touch](#read-before-you-touch). The frontend has its own router,
[`website/AGENTS.md`](website/AGENTS.md).

## What this is

Kiro Crew is an open-source personal AI agent: chat from Slack, a web dashboard, or
the CLI; run multi-step tasks unattended; schedule cron jobs; keep memory across
sessions. It drives an LLM through the KiroACP provider (the ACP adapter running
`kiro-cli` over ACP JSON-RPC) plus MCP tools.

- **Backend:** Python package `kiro_crew` in `src/kiro_crew/`.
- **Frontend:** React + TS + Vite SPA in `website/`; the built `dist/` is staged
  into `src/kiro_crew/static/dist/` and served by the backend.
- **Data home:** `~/.kiro/crew` (the legacy `~/.kirocrew` auto-migrates).
  Override with `KIROCREW_HOME`.
- **Distribution:** public GitHub, plain setuptools, public PyPI / public npm.

Full map: [`docs/architecture/overview.md`](docs/architecture/overview.md).

## Read before you touch

Load the doc for the row you are working in **before** you change code. Update it
in the **same commit** when you change what it documents.

| If you are touching… | Read first |
|---|---|
| `platform/`, editions, CPP seam, governance | [platform-context](docs/system-specs/modules/platform-context.md) + [governance](docs/system-specs/modules/governance.md) |
| `security.py`, `hooks.py`, denied commands, sensitive paths | [security](docs/system-specs/modules/security.md) + [sel](docs/system-specs/modules/sel.md) |
| the security model as a whole, threat boundaries | [security-deep-dive](docs/architecture/security-deep-dive.md) |
| `computer_use/` | [computer-use](docs/system-specs/modules/computer-use.md) |
| `acp/`, kiro-cli transport, providers | [acp-client](docs/system-specs/modules/acp-client.md) + [providers](docs/system-specs/modules/providers.md) |
| sessions, slots, session keys, PIDs | [session](docs/system-specs/modules/session.md) + [history](docs/system-specs/modules/history.md) |
| memory, embeddings, vectors, lessons, skills, hooks | [memory-skills-hooks](docs/system-specs/modules/memory-skills-hooks.md) |
| MCP servers or tools (adding, changing, statelessness) | [mcp](docs/architecture/mcp.md) |
| apps, App Kit, manifests, app agents | [app-kit-platform](docs/system-specs/modules/app-kit-platform.md) + [app-kit/](docs/app-kit/README.md) |
| artifacts, companion chat | [artifacts](docs/system-specs/modules/artifacts.md) |
| cron, learn, dashboard handlers | [learn-cron-dashboard](docs/system-specs/modules/learn-cron-dashboard.md) |
| Slack, Discord, any channel, messaging, approvals | [messaging](docs/system-specs/modules/messaging.md) + [slack-gateway](docs/system-specs/modules/slack-gateway.md) |
| subagents, spawn, orphan recovery | [subagent](docs/system-specs/modules/subagent.md) |
| task runner | [task](docs/system-specs/modules/task.md) + [taskrunner](docs/system-specs/modules/taskrunner.md) |
| `workflows/` (the dynamic-workflow engine) | [workflows](docs/system-specs/modules/workflows.md) + [workflow-gates](docs/system-specs/modules/workflow-gates.md) |
| themes | [themes](docs/system-specs/modules/themes.md) + [theming-contract](website/docs/theming-contract.md) |
| anything under `website/` | [`website/AGENTS.md`](website/AGENTS.md) |
| user-facing strings, dates, numbers, sort order | [i18n-catalog](website/docs/i18n-catalog.md) (authoring) + [i18n-gates](docs/ci/i18n-gates.md) (CI) |
| tests: flakes, speed, fixtures, sharding | [testing-conventions](docs/system-specs/common/testing-conventions.md) |
| browser E2E | [e2e-gate](docs/ci/e2e-gate.md) |
| CI, PR flow, review gates | [ci-and-reviews](docs/ci/ci-and-reviews.md) + [CONTRIBUTING.md](CONTRIBUTING.md) |
| constants, magic numbers, where a limit lives | [code-style](docs/system-specs/common/code-style.md) |
| injected `[Cron notification]` / `[Subagent completion event]` | [injected-messages](docs/system-specs/common/injected-messages.md) |
| build, install, dev mode | [CONTRIBUTING.md](CONTRIBUTING.md) + [install](docs/guides/install.md) |
| Windows / cross-platform process, signal, lock, metrics | [windows-install](docs/guides/windows-install.md) + the shim table below |
| a release, or `CHANGELOG.md` | [release](docs/build/release.md) |
| errors, retries, user-facing failure text | [error-handling](docs/system-specs/common/error-handling.md) |

The whole doc tree is indexed from [`docs/README.md`](docs/README.md). User-facing
docs that ship in the package live in `src/kiro_crew/docs/` and are indexed by
[its README](src/kiro_crew/docs/README.md).

## Never re-introduce (this is a public OSS fork)

This repo is the de-Amazoned public fork of an internal package. Never re-add:

- **Build/infra:** Brazil (`Config`, root `AUTOSDE.yaml` is NOT this),
  `CODE_APPROVERS.yaml`, `npm-pretty-much`, toolbox bundler, AIM hooks,
  CodeArtifact registries. setuptools + public PyPI / public npm only.
- **Services/auth:** enterprise SSO, MCS, Kerberos, federated login,
  device-posture tunnels, Cognito/RUM ids, builder-mcp, `arcc`, Quip, internal
  ticketing. The internal marker names are scrubbed from code, comments, and docs.
- **Keep these stubbed** (public symbols preserved as no-ops so the import graph
  holds): `sso_status.py`, `browser/auth.py`, `dashboard/handlers/sso_login.py`,
  `tunnel/manager.py`, `aim_agents.py`.
- **Other providers.** Kiro Crew is KiroACP-only: `agent.provider` is fixed to
  `acp` and kiro-cli is REQUIRED. Keep the dormant `ACP_BACKEND_CLAUDE` /
  `_is_claude` seam in `acp/client.py` so an internal companion can re-register
  Claude Code; do NOT re-add the public registration glue.
- **OSS-flipped defaults:** always-on in-process embeddings, Piper TTS by default,
  a default-open Slack enterprise gate, lazy STT extras.
- **Fork UX divergences:** the Channels app is hidden from the App Store and the
  Board app is removed. An upstream sync must not restore them.

`scripts/scrub-lint.sh` gates `src/`, `website/src/`, `scripts/`, `config/`,
`packaging/`, and the top level; keep `docs/` clean by convention. Rationale for
what was removed: [post-launch-removals](docs/system-specs/post-launch-removals.md).

**Keep** the generic security controls: AKIA/ASIA credential redaction,
destructive-command deny rules, `~/.aws` / `~/.ssh` path blocking, the SEL audit log.

## Security invariants (do NOT weaken)

- **Keystone.** `security_policy.json`, `profiles/`, `admission_policy.json`, and
  `computer_use.json` under the data home are in `security._SENSITIVE_HOME_DIRS`,
  so the agent can neither read nor write its own ceiling. When editing
  `security.py`'s sensitive-path or bash-command matchers, keep these covered,
  including write and extract verbs. This single mechanism is what makes the
  ceiling un-disableable.
- **Governance.** `effective = POLICY ∩ PROFILE`, tightest-wins, enforced at
  Kiro Crew's OWN PreToolUse gate: it denies a tool or MCP call even when the kiro
  agent config granted it. The evaluator is scope-name-agnostic, so adding a scope
  is a `SCOPE_CATALOG` data change, never an evaluator edit.
- **`CONTRACT_VERSION` stays pinned at 1 pre-launch.**
- **Denied commands** are `DeniedCommandRule` records (`BUILTIN_DENIED_RULES`, 139 rules)
  enforced only at the `hooks.py` PreToolUse gate. Never restate the rule count in
  prose: `test/test_denied_commands_security.py` pins it, and a restated count goes
  stale silently.
- **Computer use is deliberately NOT governed.** It is one operator opt-in on the
  keystone `computer_use.json`. Do not add `computer_use.*` scopes, capability
  rows, approval ordinals, or pointer permits. Its refusals run **in band** on the
  `tools._dispatch` path, never at the fail-OPEN `hooks` gate, because a
  pre-authorized tool can skip that gate. Keep them there. Secure-field redaction
  is an always-on floor with no policy key. `click_method: "auto"` must NEVER
  resolve onto `"global"`: that is the only thing between an ordinary click and the
  operator's real cursor.

## Specification management

- MUST read the relevant spec under `docs/system-specs/modules/` before changing
  the code it covers.
- MUST update the spec in the SAME commit when an API, schema, or documented
  behavior changes.
- MUST add the doc to its directory `README.md` when creating one, and MUST update
  every index that points at a doc you move, rename, or delete. `scripts/docs-lint.sh`
  enforces this; run it before you commit a docs change.
- MUST NOT create additional markdown files unless explicitly instructed.
- Task specs go in `docs/task-specs/YYYY/MM/${task-id}/`. Treat `docs/task-specs/`
  as an archive, never as current context.

## Git

- Do NOT proactively `git commit`. Commit only when asked.
- Do NOT `git push` unless the user explicitly says to push. Being asked to commit
  is NOT permission to push.
- `main` is the default branch; changes land through a GitHub PR. Full flow:
  [CONTRIBUTING.md](CONTRIBUTING.md).

```
<type>: <summary — max 72 chars, imperative, lowercase, no period>

<body — what and why, not how; wrapped at 72>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `ci`, `build`, `revert`.
One logical change per commit.

## The gate before you commit

```bash
black src/kiro_crew test && isort src/kiro_crew test
flake8 src/kiro_crew test && mypy src/kiro_crew
python -m pytest
```

Frontend: `cd website && npm run build && npm run test`. Faster loops (testmon,
`--lf`, single-file runs) are in
[testing-conventions](docs/system-specs/common/testing-conventions.md). A
multi-test `--override-ini` MUST keep `-n auto --dist loadgroup
--max-worker-restart=2`, because a bare override silently drops `--dist loadgroup`
and scatters `@pytest.mark.xdist_group` tests into flaky races.

Gates you will trip:

| Gate | Rule |
|---|---|
| flake8 F401 | no unused imports |
| flake8 N806 | function-local variables are lowercase (`mock_client`, not `MockClient`) |
| flake8 W504 | line break BEFORE a binary operator |
| mypy | annotate empty collections (`output: list[str] = []`) |
| pytest | `asyncio: mode=strict`, so every async test needs `@pytest.mark.asyncio` |

Never fix a flake with a rerun, a longer `sleep`, or a weakened assertion. Read
[testing-conventions](docs/system-specs/common/testing-conventions.md) § Determinism
for the five flake classes and the one correct fix for each. In particular, a timing
test that asserts algorithmic **complexity** must bound the doubling RATIO, not an
absolute duration: CI enables coverage on 3.12 only, and that multiplier made one shard
fail on 3.12 and pass on 3.10 at the same commit.

## Code style

| Rule | Requirement |
|---|---|
| Line length | 100 chars (black configured) |
| Python version | ≥ 3.10 (`from __future__ import annotations` for type hints) |
| Imports | `import logging` + `logger = logging.getLogger(__name__)` |
| Async | `asyncio` throughout; `async def` for all I/O |
| Dataclasses | `@dataclass` for data containers |
| Constants | No hardcoded strings or values in business logic; every limit has an owning module. Index: [code-style](docs/system-specs/common/code-style.md) |
| Comments | Explain **behavior and rationale (the why)**: invariants, edge cases, units, non-obvious constraints. NOT a task log: no PR/CR numbers, review-round markers, incident dates, milestone tags, or commit SHAs. No "previously/used to/we now" narration, state current behavior in present tense. Don't restate what the code plainly does. `_vendor/` and pragmas are exempt. |
| Icons | **Never use emojis in the UI.** Use `lucide-react` with `className="lucide-inline"`. |
| Product name | The product is **Kiro Crew**: two words, a space, capital `K`. Identifiers keep the spelling their own system gave them (the `kirodotdev/KiroCrew` repo slug, `KiroCrew.dmg` artifacts, the `KiroCrew Nightly` OS identifier, the `kirocrew` CLI, `KIROCREW_*` env vars, `kiro_crew` imports). CI-gates the lines a change adds; run `BRAND_BASE_REF=origin/main python3 scripts/check_brand_name.py` before pushing. |
| User-facing strings | The dashboard is translated into 10 languages. **Never hardcode a user-facing English string, and never format a date, number, or sort order without naming a locale.** Both are CI-gated. Backend-owned strings have no catalog path yet, so a new non-2xx JSON body MUST carry a machine-readable `code` field. |

## Cross-platform: route POSIX calls through `platform_compat`

Kiro Crew runs on macOS, Linux (x86_64 and ARM), and Windows (native). `fcntl`,
`termios`, `resource`, and `pty` do not exist on Windows, and
**`os.kill(pid, 0)` TERMINATES the target there**: it is not a liveness probe.

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
| Owner-only secret (fail-loud) | `restrict_to_owner(path)` | `os.chmod(path, 0o600)` under `if IS_POSIX` (silent no-op leaves secrets world-readable) |
| Directory link | `symlink_or_junction(target, link)` | `os.symlink` (`WinError 1314` without elevation) |
| Detect/remove a dir link | `is_link_or_junction(path)` / `unlink_link_or_junction(path)` | `path.is_symlink()` (misses a Windows junction) |
| Process RSS / CPU | `proc_rss_bytes()` / `proc_cpu_seconds()` | `resource.getrusage` |
| FD soft limit | `raise_nofile_soft_limit(n)` | `resource.setrlimit` |
| Port to PID | `find_listening_pids(port)` / `listening_pid_tool_available()` | `lsof` directly |
| strftime no-pad | `strftime(dt, "%-I")` | bare `dt.strftime("%-I")` (`ValueError` on Windows) |

Verify process, signal, file-lock, and metrics changes on macOS + Linux. Frontend:
Chrome, Firefox, Safari, Edge, using standard Web APIs and guarding the rest.
Windows specifics: [windows-install](docs/guides/windows-install.md).

## LLM-facing capabilities

- **MCP-first.** A new LLM-facing CLI command MUST also ship as an MCP tool
  (`mcp_cron.py` / `mcp_core.py`): kiro-cli calls MCP tools reliably and may refuse
  to run a CLI command via bash. There is exactly one deliberate exception,
  `kirocrew computer call`, a human debug harness rather than a capability; do not
  add another without reading [mcp](docs/architecture/mcp.md). Do NOT add regex to
  match natural-language variants, the LLM interprets NL.
- **MCP tools MUST be stateless.** One server process serves many sessions and
  sub-agents, so no module global may hold per-caller data. Resolve identity per
  call, and use `_resolve_session_key_strict()` for anything that mutates or
  targets a specific session (the lenient resolver walks process ancestors and a
  sub-agent would resolve to its parent slot). Durable state lives behind a gateway
  endpoint keyed by session. Why, plus the `ask_question` reference
  implementation: [mcp](docs/architecture/mcp.md).
- **A skill that any shipped feature, tool, or doc references MUST live in
  `src/kiro_crew/builtin_skills/`.** That is the only path bundled into the
  package and copied into a user's `~/.kiro/crew/skills/`. Top-level `skills/` is
  repo-checkout-only and reaches no installed user.

## Injected messages are not the user

`[Cron notification from "job"]`, `[Subagent completion event]`, and
`[auto-nudge cycle N]` arrive from automation, not from a human. Process them; do
not answer them as if a user typed them. The user may not be present. Envelope
formats: [injected-messages](docs/system-specs/common/injected-messages.md).

## Harness safety

`kirocrew gateway --approval yolo` auto-approves ALL tools and refuses to start
unless `KIROCREW_HOME` is explicitly set to a non-default path. Never point it at
`~/.kiro/crew`. All harness flags: [cli](docs/system-specs/modules/cli.md).
