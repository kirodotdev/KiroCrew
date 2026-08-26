# Auto-Improvement — upstream port plan

Source: the upstream auto-improvement app (23,586 src lines + 14,000 test lines)
Target: native Kiro Crew builtin app `auto-improvement`

## What the app does

A measurement-first self-improvement loop. It builds and *proves* a metric (the
"ruler") against a target repo, then runs keep-or-revert cycles: discover a
candidate → propose N fixes in parallel git worktrees → deterministically gate
them (edit allowlist + build/test) → measure A/B (perf) or RED/GREEN (bug) →
keep only if the win clears the noise band → draft a reviewable change.

Thesis from the source docs: *"a measurement system that happens to write code,
not a code-writing system that happens to measure."*

## The four requirements → design decisions

| Req | Decision |
|---|---|
| 1. GitHub as native code host | `gh pr create --draft` replaces `cr --new-review`. The source's own `spine/profile.py` names this exact substitution as the intended external-host path, so the seam already exists. PR status/checks come from Kiro Crew's existing `source_providers.fetch_pull_request{,_checks}` — no new API client. |
| 2. Remove all host-specific internals | Delete/replace: the internal review service + its cookie auth, the internal review CLI prompt block, internal SSH remote construction, the internal build-tool gates, the internal build config and setup shim, internal skill/toolchain discovery, and hardcoded internal model ids and hosts. Verified by the repo's `scripts/scrub-lint.sh`. |
| 3. Integrate with chats + more chats | Three tiers (see below) — upstream had one fire-and-forget launcher. |
| 4. Focus on PRs not CRs | Whole vocabulary renamed CR→PR: `pr_recipe`, `pr_watchers`, `pr_checks`, `pr_queue/`, ledger `pr` field. Watchers track PR mergeability and CI, not the upstream review service's analyzers. |

## Port classification

**Ports ~unchanged (the crown jewel).** `spine/` — 6,157 lines, audited
target-agnostic: no build-tool, auth, or host references at all. Only real
coupling is 3 imports in `agent_runner.py` (the host config class and the host's
ACP event-constant module, both repointed at `kiro_crew`). Kiro Crew's ACP event constants
match the source's names exactly, and `create_provider_factory` exists
(`config/loader.py:4612`), so `SessionAgentRunner` ports directly.

**Rewritten for GitHub/PRs (new code).**
- `backend/pr_checks.py` ← replaces the internal review-service client (303 lines of
  `curl` + cookie auth + a proprietary analyzer vocabulary)
- `profiles/github_repo/pr_recipe.py` ← replaces both `cr_recipe.py`
- `backend/pr_watchers.py` ← from the upstream watcher (1,506 lines), stripping the
  internal review-CLI reference block and the internal reviewer's 5-pass prompt
- `backend/clone_setup.py` ← GitHub-only allowlist, no internal SSH URL construction

**Replaced by one generic profile (deliberate scope call).** The source ships two
profiles totalling 7,251 lines — one optimizing the upstream host's backend (gated
on an internal build tool and eagerly importing the host's test harness) and one
optimizing its frontend. Both are *target-specific harnesses
for the upstream host's own code*; neither is shippable capability here. They are
replaced by a single `profiles/github_repo/` that works against any GitHub
Python/Node repo: pytest/npm-test build gate, ruff/eslint defect discovery,
wall-clock ruler. This is the app's reference profile and the seam stays open for
more.

**Deleted outright.** The internal build config, `setup.py`'s build-tool argv
shim, the internal `setup.cfg` keys, the `bin/auto-improvement` launcher,
`scripts/{install,enable,disable,update}.sh`, `backend/{proxy_auth,middleware}.py`,
`backend/deps.py`, `_vendor/`, `ui/` vite lib build. Reasons: Kiro Crew builtins
run **in-process** (`register_routes(app)`), so no separate process, no port, no
HMAC, no launcher, no lifecycle shell hooks; the UI lives in the main website
bundle; `_vendor/` existed to avoid importing the upstream core, but we can import
`kiro_crew` directly.

## Chat integration (requirement 3)

Upstream had exactly one path: `useChatLauncher().openChat({message})` — fire and
forget, always a new session, no slot identity. Kiro Crew offers three; we use all
three, each where it fits:

1. **Resumable per-PR sessions** (the main upgrade) — `issue_radar`'s pattern:
   `createSlot` → `renameSlot` → `sendChat` → `switchSlot`, persisting
   `{slot_key, folder_id}` in our own store so re-clicking *resumes* the same
   conversation instead of starting over. One slot per PR, filed in a per-repo
   chat folder. 404 on `switchSlot` ⇒ slot was deleted ⇒ open a fresh one.
2. **Silent background sessions** — `review_pool.py`'s direct `AcpRuntime`
   pattern for the autonomous loop's own agent runs, so cycles never spam the
   chat surface with agent cards or approval prompts.
3. **Fire-and-forget launcher** — `useChatLauncher()` for one-shot "discuss this
   finding" actions where no continuity is wanted.

New chat surfaces beyond the upstream single "discuss this change": discuss a *finding*,
discuss the *ruler/calibration*, discuss a *failed gate*, and a run-level
"explain this run" session.

## Layout

```
src/kiro_crew/apps/builtins/auto_improvement/
├── app.json                    manifest (kebab name, /api/apps/auto-improvement/*)
├── __init__.py                 re-exports register_routes  (REQUIRED by server.py)
├── backend/
│   ├── routes.py               in-process aiohttp routes, is_app_enabled-gated
│   ├── store.py                app_data_dir("auto-improvement")
│   ├── pr_checks.py            PR status/checks via source_providers
│   ├── pr_watchers.py          per-PR watcher threads
│   ├── clone_setup.py          GitHub-only clone + push-disable
│   ├── runner.py               RunSupervisor
│   ├── data_store.py           artifact read/normalize
│   ├── profile_normalize.py    pstats/.cpuprofile → frame tree
│   └── chat_sessions.py        NEW — slot lifecycle for req 3
├── spine/                      ported ~verbatim (the engine)
├── profiles/github_repo/       NEW generic profile
├── skills/                     ai-discover, metric-design
├── agents/                     discovery, pr-author
└── tests/
```

Registration touchpoints (all four required):
`builtins/__init__.py` BUILTIN_NAMES · `website/src/apps/builtinRegistry.ts` ·
`website/src/apps/builtinIcons.tsx` · `website/public/app-assets/auto-improvement/*.svg`

## Constraints from AGENTS.md that bind this work

- No new third-party deps (stdlib + aiohttp only; aiohttp already in core)
- No blocking syscalls on the event loop — `subprocess`/file IO via
  `run_in_executor(subprocess_executor(), ...)`. The source is a threaded
  sync driver, which suits this: it already runs off-loop.
- Frontend: i18n mandatory (no hardcoded English), React Query for server state,
  lucide icons only, no emoji, no text below 10px, `<Clickable>` not `<div onClick>`
- jscpd duplication gate: `threshold: 0`, `minTokens: 180`
- black/isort/flake8/mypy clean, line length 100
- Keep the push-disabled invariant and draft-only policy intact — they are the
  app's core safety controls, and are not host-specific.

## Safety invariants preserved verbatim

1. **Push-disabled clones** — `git remote set-url --push origin DISABLED_NO_PUSH`,
   asserted before preflight; run refuses to start otherwise.
2. **Draft-only** — never `--publish`, never auto-merge unattended.
3. **Protected-branch denylist** — non-overridable; retargeted from
   org-specific names to `main`/`master`. <!-- wokeignore:rule=master -->
4. **Edit allowlist** — mechanically forbids the agent touching the ruler,
   harness, tests, or auth (the reward-hacking guard).
5. **Do-not-pollute gate** — host state hashed before/after; nonzero diff blocks.
6. **Second independent reproduce** before a PR is drafted.
