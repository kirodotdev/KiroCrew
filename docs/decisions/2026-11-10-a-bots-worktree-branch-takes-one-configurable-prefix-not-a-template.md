# A bot's worktree branch takes one configurable prefix, not a template

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

`agent.member_branch_prefix` (default `crew/`) replaces `crew/` in front of
`<bot>/<repo>` on the branch the gateway cuts for a bot's new worktree; the
branch name is not a template with `{bot}`, `{repo}` or `{user}` placeholders,
and a worktree that already exists keeps its branch.

## Why

- A prefix set in each person's own config covers the three cases the request
  named: personal branches under `<user>/`, push rules or CI filters keyed on a
  branch pattern, and two people's same-named bots on one remote.
- Keeping the bot and the repository as the last two parts means two bots can
  never share a branch, with no extra check.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/33980#issuecomment-6757510321 --
  the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/33980 -- the request.
