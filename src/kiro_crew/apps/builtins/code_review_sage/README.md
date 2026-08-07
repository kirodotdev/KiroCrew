# Code Review Sage

A self-evolving deep code reviewer packaged as a built-in KiroCrew app. Reviews
**GitHub pull requests**, learns per-repository from shipped fixes + review
comments + design discussions, and produces a prioritized **Focus Report** so
you know which changes actually deserve scrutiny. Findings are read **in the
app**, next to the pull request they came from — nothing is written to the pull
request unless you turn on `review.auto_post`, which publishes them as a PENDING
(draft) review for you to submit.

## Architecture (V1)

- **Deterministic shell** (`sage_lib/`): data store + self-heal layout, GitHub
  source adapter, blast-radius signals, result records, scorer/export.
  Token-free, unit-tested.
- **LLM judgment** (`skills/` + sub-agents): the per-change design gate +
  dimension review and the final report synthesis, each in a clean session.
- **Backend** (`backend/routes.py`): registers the `/api/apps/code-review-sage`
  routes on the dashboard app.
- **UI**: the `/code-review-sage` dashboard page is a React component at
  `website/src/apps/code-review-sage/CodeReviewSagePage.tsx` (registered in
  `website/src/apps/builtinRegistry.ts`).

## Layout

```
code_review_sage/
├── app.json                 # manifest (GitHub-only; depends on the `gh` CLI)
├── __init__.py              # exposes register_routes
├── backend/
│   ├── __init__.py
│   └── routes.py            # /api/apps/code-review-sage routes
├── sage_lib/
│   ├── store.py             # data layout self-heal + config
│   ├── review_driver.py     # code-enforced 1-isolated-spawn-per-change loop
│   ├── review_pool.py       # reviewer worker pool (config-driven model/effort)
│   ├── adapters.py          # GitHub PR source adapter
│   └── ...
├── skills/
│   ├── sage-review/         # review ruleset
│   └── learn-from-sage/     # miss-analysis learning
└── tests/                   # unit tests
```

App-local runtime data lives under `~/.kiro/crew/apps/code-review-sage/data/`
(honors `KIROCREW_HOME`; created on first use, never committed).

## Enable

Code Review Sage is a built-in app (listed in `kiro_crew.apps.builtins`). Enable
it from the dashboard Apps page, or:

```bash
kirocrew app enable code-review-sage
```

Reviewing GitHub PRs requires an authenticated `gh` CLI on the gateway host:

```bash
gh auth login --hostname github.com
```
