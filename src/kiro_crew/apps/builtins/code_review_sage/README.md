# Code Review Sage

A self-evolving code reviewer packaged as a built-in Kiro Crew app. Reviews
**local Git working trees first**, learns per-repository from shipped fixes + review
comments + design discussions, and produces a prioritized **Focus Report** so
you know which changes actually deserve scrutiny. Findings are read **in the
app**, next to the pull request they came from — nothing is written to the pull
request unless you turn on `review.auto_post`, which publishes them as a PENDING
(draft) review for you to submit.

The Local view is the review → feedback → fix loop for uncommitted changes. It
fingerprints the reviewed working tree, validates every inline finding against an
added line, persists dispositions and human guidance, and refuses a fix when the
working tree has moved since the review. The fix agent receives only the selected
findings and never commits or pushes on the user's behalf. Applying a fix is a
governed Task Runner lifecycle — a retained candidate worktree whose apply,
commit, push, and discard steps are user-confirmed state transitions — specified
in `docs/system-specs/modules/taskrunner.md` (Review-Fix Lifecycle).

## Ask the reviewer

A report states conclusions; "why did you decide that?" is answerable only by the
session that decided it. So after a deep review that session's transcript is kept
(`sage_lib/followup.py`) and the Focus Report offers to open a **follow-up
session**: an ordinary chat session whose kiro-cli session is `session/load`ed
from the review's own transcript, filed in a `Sage Review` folder and titled
`followup-pr#<n>-<pull request title>`.

Nothing is held resident between the review and the question. Asking is the rare
case, so a follow-up pays a cold load from disk rather than pinning the shared
reviewer subprocess on the chance that someone asks. From the first turn on it is
a normal session: it survives restarts, appears in the sidebar, and its tool use
runs through the dashboard's own approval pipeline — which sees real permission
requests and can reject *before* execution, rather than the app gating tool calls
after the fact.

A resume that would not restore the review is refused rather than attempted. The
dashboard's fallback for a failed resume is to replay Kiro Crew's own conversation
log, and a follow-up session has none, so a session opened anyway would answer
confidently with no idea what was reviewed. The panel therefore says why it is
offering nothing: the review kept no session, its transcript is gone, or the run
is still going (its findings can still be replaced by a second coverage pass).

Follow-up offers are retired after two weeks with no activity, measured from the
transcript's own mtime so a conversation still in use keeps its offer. Retiring an
offer removes only Sage's descriptor: the one session id available here comes from
a file the reviewer itself can write, so it proves the form of a session id and
never which session Sage recorded, and deleting on that authority would let a
prompt-injected review name any session on the machine. Reclaiming a transcript is
the platform's own user-controlled session cleanup.

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
│   ├── routes.py            # /api/apps/code-review-sage routes
│   └── fix_tasks.py         # review-fix task endpoints + Task Runner-backed actions
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

### GitHub Enterprise Server

GitHub Enterprise hosts are opt-in. Add each instance to `github_hosts` in
`~/.kiro/crew/apps/code-review-sage/data/config.json` (the list replaces the
default, so keep `github.com` if you still review there) and authenticate `gh`
for it:

```json
"github_hosts": ["github.com", "ghe.example.com"]
```

```bash
gh auth login --hostname ghe.example.com
```

Hosts are matched exactly against the parsed URL hostname — never as a
substring — so lookalike hosts are refused.
