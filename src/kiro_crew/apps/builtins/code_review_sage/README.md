# Code Review Sage

A self-evolving deep code reviewer packaged as a built-in KiroCrew app. Reviews
**GitHub pull requests**, learns per-repository from shipped fixes + review
comments + design discussions, and produces a prioritized **Focus Report** so
you know which changes actually deserve scrutiny. Findings are read **in the
app**, next to the pull request they came from — nothing is written to the pull
request unless you turn on `review.auto_post`, which publishes them as a PENDING
(draft) review for you to submit.

## Ask the reviewer

A report states conclusions; "why did you decide that?" is answerable only by the
session that decided it. After a deep review the reviewer's session is kept alive
for a bounded while (idle TTL plus an absolute cap, see `sage_lib/chat_session.py`)
so the Focus Report can offer a chat panel against that same session. Once it is
reclaimed the transcript stays readable and the composer disappears, because an
input that cannot send is worse than none.

Asking requires the safety override (YOLO) to be active, with enough remaining
runway to cover the whole turn. Without it the panel explains why instead of
offering a composer: the reviewer's session carries pre-approved tools, and a
turn that is not authorized to use tools at all must not start.

### Known limitation: pre-approved tools are gated after they run

Authorization is enforced in two places, and for one class of tool the second is
POST-HOC. An agent spec's `allowedTools` pre-approves tools, which then execute
with **no permission request** — there is nothing to reject, and by the time
`EVENT_TOOL_CALL` arrives the call has already been made. So the per-tool checks
applied there (operator denied-commands, the `~/.aws` / `~/.ssh` sensitive-path
blocks, the enterprise profile ceiling) cannot prevent the FIRST pre-approved
call. They still abort the turn, which stops every subsequent tool and withholds
the answer, and they remain a genuine pre-execution gate for any tool that does
raise a permission event.

This matters here specifically because the session's context is
attacker-influenced: it was built by reviewing a pull request whose diff and
description come from an outsider, and this feature then lets a human keep
prompting that same session. An instruction planted in a diff could steer a
follow-up answer into a pre-approved read of a credential file.

Scope: the reviewer agent pre-approves one MCP server; the fallback `kirocrew`
agent pre-approves roughly thirty entries. The session's spec cannot be narrowed
after the fact — it is the review's own session, which is the point of keeping
it — so moving the check cannot close this. Closing it requires either a session
with no pre-approved tools, or routing every tool call through the same
`HookManager` the dashboard and Slack paths use, with the turn-level override
kept as an additional restriction on top.

Until then this is an accepted limitation, bounded by three conditions holding at
once: the reviewed pull request is authored by an untrusted party, an operator has
turned the safety override on, and someone asks a follow-up question. With the
override off the chat surface does not exist.

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
