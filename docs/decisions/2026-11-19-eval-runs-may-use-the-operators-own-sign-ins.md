# An eval run may use the operator's own sign-ins when the operator asks for it

Decided by: Joe Guo (maintainer, @iamwhatever)
Date: 2026-11-19

## Decision

`kirocrew eval run --suite tasks --use-operator-credentials` runs every arm as
the operator, with the operator's existing kiro-cli sign-in and the `claude`
already working on the machine, and the operator accepts that the agents and
the hidden check can then reach the operator's credentials, and that the
Claude Code arm then merges the operator's user settings (allow rules, hooks)
with the case's tools, so the arms' same-tools rule of `rfc-builtin-eval.md`
§3.2 does not fully hold; without the flag,
preflight keeps the dedicated eval user and eval-only keys of
`rfc-builtin-eval.md` §4.1 and §4.3.

## Why

- Setting up a dedicated eval OS user, sudo installs, a separate Kiro key and a
  Bedrock key was too much work for a first eval run on a machine where Kiro
  and Claude Code are already signed in.
- The owner asked to reuse those sign-ins and accepted the risk, on the
  condition that it is opt-in, said before the batch starts and recorded in
  every run record.
- Keeping Claude Code off the operator's user settings would also drop a
  sign-in kept there (for example `apiKeyHelper`), which is what the flag
  exists to reuse.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/39317 -- the pull request that
  adds the flag, the risk line and the `credentials` field, and marks
  `rfc-builtin-eval.md` §4.4 items 1, 4 and 6 and the §14.1 eval-user fence as
  not holding under it.
- https://github.com/kirodotdev/KiroCrew/pull/39317#issuecomment-7016988991
  -- Joe Guo's comment on that pull request stating this decision.
