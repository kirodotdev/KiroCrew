# A webhook call that reuses a sessionKey starts a fresh conversation

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-24

## Decision

A `POST /api/hooks/agent` turn runs in a one-shot session: a later call on the same
`sessionKey` starts a fresh conversation that carries nothing of the earlier one. The one
exception is a turn that ended while a sub-agent it started still owed it a report: that
report is delivered into its conversation as before, and the next call on the key continues
that conversation; that call's own end forgets it unless sub-agent work is still owed then.

## Why

- The inbound webhooks guide promises it: webhook sessions are ephemeral, two calls with the
  same `sessionKey` are two unrelated turns, and continuity across calls is explicit, through
  `register_hook`. Its handoff pattern is built on that.
- The code kept the resume id after the turn's reset, so a reused `sessionKey` loaded the first
  call's conversation, against the guide.
- A sub-agent that a hook turn started and left running still reports into its `sessionKey`;
  this decision does not stop that work.

## Evidence

- Maintainer decision: https://github.com/kirodotdev/KiroCrew/issues/41891#issuecomment-7138712570
- Issue: https://github.com/kirodotdev/KiroCrew/issues/41891
