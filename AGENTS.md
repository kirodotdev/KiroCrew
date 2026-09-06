# Rules for AI Assistants

**This file is a ROUTER, not a manual.** It carries only the rules whose violation
causes damage before a pointer could be read. Everything else is a link you MUST
open before touching that subsystem: see
[Read before you touch](#read-before-you-touch). The frontend has its own router,
[`website/AGENTS.md`](website/AGENTS.md).

## What this is

Kiro Crew is an open-source personal AI agent: chat from the web dashboard, the
CLI, or a messaging channel like Slack and Discord; run multi-step tasks
unattended; schedule cron jobs; keep memory across sessions. It drives an LLM
through the KiroACP provider (the ACP adapter running `kiro-cli` over ACP
JSON-RPC) plus MCP tools.

- **Backend:** Python package `kiro_crew` in `src/kiro_crew/`. **Frontend:** React
  + TS + Vite SPA in `website/`, built into `src/kiro_crew/static/dist/` and served
  by the backend.
- **Data home:** `~/.kiro/crew`, overridden with `KIROCREW_HOME`.
- **Distribution:** public GitHub, plain setuptools, public PyPI / public npm.

Full map: [overview](docs/architecture/overview.md). This repo is the de-Amazoned
public fork of an internal package; what must never come back is
[oss-fork-boundaries](docs/system-specs/oss-fork-boundaries.md), gated by
`scripts/scrub-lint.sh` and the `no-new-builtin-apps` rule in `AUTOSDE.yaml`.

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
| picking or defaulting a model anywhere | [model-selection](docs/system-specs/common/model-selection.md) + [model-fallback](docs/system-specs/modules/model-fallback.md) |
| adding or adapting an agent harness (BYO, KAS, claude) | [harness-parity](docs/system-specs/modules/harness-parity.md) (invariants) + [harness-parity-gate](docs/ci/harness-parity-gate.md) (CI) |
| the publicly selectable Claude backend | [claude-code-provider](docs/system-specs/modules/claude-code-provider.md) |
| sessions, slots, session keys, PIDs | [session](docs/system-specs/modules/session.md) + [history](docs/system-specs/modules/history.md) |
| session summaries, the chat summary panel, intent extraction | [session-summary](docs/system-specs/modules/session-summary.md) |
| memory, embeddings, vectors, lessons, skills, hooks | [memory-skills-hooks](docs/system-specs/modules/memory-skills-hooks.md) |
| MCP servers or tools (adding, changing, statelessness) | [mcp](docs/architecture/mcp.md) |
| apps, App Kit, manifests, app agents | [app-kit-platform](docs/system-specs/modules/app-kit-platform.md) + [app-kit/](docs/app-kit/README.md) |
| artifacts, companion chat | [artifacts](docs/system-specs/modules/artifacts.md) |
| `stt/`, `transcribe.py`, `voice_reply.py`, the mic, dictation, TTS | [stt-streaming](docs/system-specs/modules/stt-streaming.md) + [voice-streaming](docs/system-specs/modules/voice-streaming.md) |
| cron, learn, dashboard handlers | [learn-cron-dashboard](docs/system-specs/modules/learn-cron-dashboard.md) |
| Slack, Discord, any channel, messaging, approvals | [messaging](docs/system-specs/modules/messaging.md) + [slack-gateway](docs/system-specs/modules/slack-gateway.md) |
| subagents, spawn, orphan recovery | [subagent](docs/system-specs/modules/subagent.md) |
| crews, `select_crew`, crew bindings, Crew Mode slots | [crew-mode](docs/system-specs/modules/crew-mode.md) |
| the pipeline conductor agent or its skill | [pipeline-conductor](docs/system-specs/modules/pipeline-conductor.md) |
| task runner | [task](docs/system-specs/modules/task.md) + [taskrunner](docs/system-specs/modules/taskrunner.md) |
| `workflows/` (the dynamic-workflow engine) | [workflows](docs/system-specs/modules/workflows.md) |
| themes | [themes](docs/system-specs/modules/themes.md) + [theming-contract](website/docs/theming-contract.md) |
| anything under `website/` | [`website/AGENTS.md`](website/AGENTS.md) |
| user-facing strings, dates, numbers, sort order | [i18n-catalog](website/docs/i18n-catalog.md) (authoring) + [i18n-gates](docs/ci/i18n-gates.md) (CI) |
| tests: flakes, speed, fixtures, sharding, side effects, conftest isolation | [testing-conventions](docs/system-specs/common/testing-conventions.md) + the [writing-tests](src/kiro_crew/builtin_skills/kirocrew-dev/writing-tests/SKILL.md) skill |
| browser E2E | [e2e-gate](docs/ci/e2e-gate.md) |
| proving a worktree change against an isolated running gateway | [worktree-verification-recipes](docs/guides/worktree-verification-recipes.md) |
| CI, PR flow, review gates, commit messages | [ci-and-reviews](docs/ci/ci-and-reviews.md) + [CONTRIBUTING.md](CONTRIBUTING.md) |
| constants, comments, lint, code style, the brand name | [code-style](docs/system-specs/common/code-style.md) |
| connections, connectors, an external account link | [connections](docs/system-specs/modules/connections.md) |
| a POSIX call: locks, signals, PIDs, chmod, RSS | [platform-compat](docs/system-specs/common/platform-compat.md) + [windows-install](docs/guides/windows-install.md) |
| injected `[Cron notification]` / `[Subagent completion event]` | [injected-messages](docs/system-specs/common/injected-messages.md) |
| build, install, dev mode | [CONTRIBUTING.md](CONTRIBUTING.md) + [install](docs/guides/install.md) |
| cutting a release | [release](docs/build/release.md) |
| `CHANGELOG.md` | [changelog](docs/build/changelog.md) |
| errors, retries, user-facing failure text | [error-handling](docs/system-specs/common/error-handling.md) |
| what this public fork must never re-introduce | [oss-fork-boundaries](docs/system-specs/oss-fork-boundaries.md) |
| any doc: moving, renaming, indexing it | [docs/README.md](docs/README.md) |

The whole doc tree is indexed from [`docs/README.md`](docs/README.md). User-facing
docs that ship in the package live in `src/kiro_crew/docs/` and are indexed by
[its README](src/kiro_crew/docs/README.md).

## Security invariants (do NOT weaken)

Detail and rationale: [security](docs/system-specs/modules/security.md),
[governance](docs/system-specs/modules/governance.md),
[computer-use](docs/system-specs/modules/computer-use.md).

- **Keystone.** `security_policy.json`, `profiles/`, `admission_policy.json` and
  `computer_use.json` under the data home stay in `security._SENSITIVE_HOME_DIRS`,
  covering read AND write AND extract verbs. This single mechanism is what makes
  the ceiling un-disableable, and a matcher weakened by accident is invisible.
- **Governance is `POLICY ∩ PROFILE`, tightest-wins**, enforced at Kiro Crew's OWN
  PreToolUse gate even when the kiro agent config granted the call. The evaluator
  is scope-name-agnostic, so adding a scope is a `SCOPE_CATALOG` data change, never
  an evaluator edit.
- **`CONTRACT_VERSION` stays pinned at 1 pre-launch.**
- **Never restate the denied-rule count in prose.**
  `test/test_denied_commands_security.py` pins it, and a restated count goes stale
  silently.
- **A cron script body is never a shell-gate subject.** `is_sensitive_bash_command`
  and `is_denied` read a SHELL COMMAND LINE; `mcp_cron._vet_script_contents` scans a
  Python source body with whole-body, source-aware detectors only (credential path,
  secret env name, exfil URL) and the sandbox is the runtime control. Handing the
  body to the shell gate was tried (#4243 → #8811) and every shell-grammar pass
  produced a permanent false denial on ordinary scripts (#7912, #8563, #8643,
  #8812), each patched with another AST layer that still could not stop
  `open(a + b)`. Do not add a `subject_is_*` flag, a `_traversal_subjects`
  re-pointing, or an `is_sensitive_source_body` back. A new script detector is a
  whole-body match in `_vet_script_contents`, or a sandbox mask. Pinned by
  `test_the_shell_gate_has_no_source_body_entry_point` and
  `test_script_body_is_never_a_shell_gate_subject`.
- **A regex spelling-chase is a review smell, not a fix.** When a security review
  finds "X also reaches the fence via spelling Y", ask first whether the SUBJECT is
  wrong (a document handed to a command-line matcher) or whether the sandbox
  already covers it. Add a table entry only when the subject is genuinely a shell
  command line and the OS sandbox does not hold the path (#7441 went four rounds
  of `command`/`exec -a`/`nice`/`env -i`/`timeout`/`busybox` before restructuring).
- **Computer use is deliberately NOT governed**: it is one operator opt-in on the
  keystone `computer_use.json`. Never add `computer_use.*` scopes, capability rows,
  approval ordinals or pointer permits. Its refusals run **in band** on
  `tools._dispatch`, never at the fail-OPEN `hooks` gate, because a pre-authorized
  tool can skip that gate. `click_method: "auto"` must NEVER resolve onto
  `"global"`: that is the only thing between an ordinary click and the operator's
  real cursor.

## Never hardcode a model id

`claude-*`, `opus*`, `sonnet*`, `haiku*`, `gpt-*` or `fable*` as a default or
fallback fails at runtime for anyone not entitled to it, silently until the first
prompt. The default is `"auto"`; resolve a substitute choice through
`acp.client.resolve_usable_model`; pin a cheaper model only via
`agent.role_models.<role>`. `code-review.yml` fails on a newly added hardcoded
literal. Rules and the one exception:
[model-selection](docs/system-specs/common/model-selection.md).

## Harness parity

Never express "this is the Kiro harness" as the ABSENCE of another one. A negative
test fails toward the permissive answer, so nothing goes red until an operator who
never opted into that harness pays for it — and every id in
`acp_backends.BASELINE_SELECTABLE_BACKENDS` is selectable on a plain public build,
so `not is_claude_backend` is already wrong on the non-Claude ones. Identity is
positive: `is_kiro_backend`, `== ACP_BACKEND_KIRO`, or membership in a named
`ACP_BACKENDS_*` set.

An added harness ADAPTS to the seams the Kiro path already runs through; it never
moves, widens or generalizes them, and it is selected at `agent.acp_backend` —
`agent.provider` stays `enum=["acp"]`. Invariant ids (cite them bare, `H7`),
capability sets and the CI half:
[harness-parity](docs/system-specs/modules/harness-parity.md). Run the added-line
gate locally with
`HARNESS_BASE_REF=origin/main python3 scripts/check_harness_parity.py`.

## Specs and docs

- MUST read the owning spec under `docs/system-specs/` before changing the code it
  covers, and MUST update it in the SAME commit.
- MUST NOT create additional markdown files unless explicitly instructed.
- Everything else about adding, moving, indexing and linting a doc — including
  `scripts/docs-lint.sh` — is [docs/README.md](docs/README.md). Treat
  `docs/task-specs/` as an archive, never as current context.

## Git

- Do NOT proactively `git commit`. Commit only when asked.
- Do NOT `git push` unless the user explicitly says to push. Being asked to commit
  is NOT permission to push.
- `main` is the default branch; changes land through a GitHub PR. The full flow:
  [CONTRIBUTING.md](CONTRIBUTING.md).

```
<type>: <summary — max 72 chars, imperative, lowercase, no period>

<body — what and why, not how; wrapped at 72>
```

Types the PR-title gate in `code-review.yml` accepts: `feat`, `fix`, `docs`,
`style`, `refactor`, `perf`, `test`, `chore`, `ci`, `build`, `revert`. **One
logical change per commit**, and at most two commits per PR.

## CHANGELOG.md

- **Your feature PR does not touch `CHANGELOG.md`.** The release PR writes the
  section covering everything that shipped.
- **Never delete or edit a shipped section.** A release PR prepends one section and
  leaves every earlier one byte-identical. This has already cost 322 lines of
  released history once, which no test caught.
- **A stable release must never ship a version carrying a prerelease suffix.**

When, how, the heading shape and the format budget:
[changelog](docs/build/changelog.md). Cutting a release, and the one escape hatch
from the suffix rule: [release](docs/build/release.md).

## The gate before you commit

```bash
python3 scripts/check_black_formatting.py && python3 scripts/check_subprocess_encoding.py && isort src/kiro_crew test
flake8 src/kiro_crew test && mypy src/kiro_crew
python -m pytest
```

- **On macOS, run `mypy --platform linux src/kiro_crew`.** Without it a local run
  reports errors you did not cause and MISSES the Linux-only errors CI fails on, so
  a clean local run is a false green.
- **Never run bare `black src/kiro_crew test`.** It reformats every baselined file
  and buries your diff. Format only what you touched:
  `black --target-version py310 <the files you changed>`.
- Frontend: `cd website && npm run build && npm run test`.
- A multi-test `--override-ini` MUST keep `-n auto --dist loadgroup
  --max-worker-restart=2`; a bare override silently drops `--dist loadgroup` and
  scatters `@pytest.mark.xdist_group` tests into flaky races.

Gates, flake classes, the conftest isolation floor and the traps that are invisible
when reading a test: [code-style](docs/system-specs/common/code-style.md) +
[testing-conventions](docs/system-specs/common/testing-conventions.md).

## Cross-platform

Route POSIX calls through `platform_compat`: `fcntl`, `termios`, `resource` and
`pty` do not exist on Windows, and **`os.kill(pid, 0)` TERMINATES the target
there** — it is not a liveness probe. The full helper-per-call table:
[platform-compat](docs/system-specs/common/platform-compat.md). Verify process,
signal, file-lock and metrics changes on macOS + Linux.

## LLM-facing capabilities

A new LLM-facing CLI command MUST also ship as an MCP tool, MCP tools MUST be
stateless (no module global holds per-caller data), and a skill any shipped
feature, tool or doc references MUST live in `src/kiro_crew/builtin_skills/` — the
only tree bundled into the package. Why, plus the session-key gate:
[mcp](docs/architecture/mcp.md) +
[memory-skills-hooks](docs/system-specs/modules/memory-skills-hooks.md).

## Injected messages are not the user

`[Cron notification from "job"]`, `[Subagent completion event]` and
`[auto-nudge cycle N]` arrive from automation. Process them; do NOT answer them as
if a human typed them — the user may not be present. Envelopes:
[injected-messages](docs/system-specs/common/injected-messages.md).

## Harness safety

`kirocrew gateway --approval yolo` auto-approves ALL tools and refuses to start
unless `KIROCREW_HOME` is explicitly set to a non-default path. Never point it at
`~/.kiro/crew`. All harness flags: [cli](docs/system-specs/modules/cli.md).
