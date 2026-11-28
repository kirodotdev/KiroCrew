# A Bot's owner can let its workers hand parts of a task to further workers of the same Bot, with a switch that is off by default

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-22
Supersedes: 2026-11-09-only-a-bots-own-thread-hands-work-to-its-worker.md (in part: "Only a bot's own thread ... hands work to its worker", only for a Bot whose `worker_conducts` switch is on, where the workers its thread opens hand work to its worker too)
Supersedes: 2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md (in part: a conductor "mounts only Crew's own MCP servers and loads only the skills it names itself", only for a conducting worker of a Bot whose `worker_conducts` switch is on, which carries the Bot's additions)

## Decision

A Bot has one switch, `agents.<name>.worker_conducts`, off by default, that its
owner sets and the Bot cannot change. While it is on, each worker the Bot's own
thread opens can run as a conductor for its item: it records sub-items in a work
ledger of its own, opens further workers of the same Bot for them, patrols those
workers and verifies their results, then reports the item back to the Bot's
thread as a worker does today. Those further workers are ordinary workers of the
Bot, with its identity, memory store, permission rules, deny rules, harness pin,
Profile approval posture and `Workers at once` limit; the nesting is one level
deep (the Bot's thread, its conducting workers, their plain workers); and while
the switch is on, `Workers at once` always applies, 4 when the owner has set
none. A Bot that leaves the switch off, the default, is not changed in any way:
for it, both superseded entries stand.

## Why

- The maintainer signed the design in
  `docs/request-for-change/rfc-bot-workers-conduct.md`: "signed, the design is
  accepted".
- The RFC's case: a task of several independent pieces runs one after another
  in one worker, and only the person can split it, by hand, in the Bot's chat.
  With the switch on the pieces run at once, as far as `Workers at once` leaves
  seats, and each piece is checked against its own acceptance before the item
  is reported done.
- The RFC's §8 asks a maintainer to supersede these two entries for the opt-in
  case and keeps the rest of each. For the 2026-11-09 entry, "no chat is bound
  as the Bot's own chat", and every chat outside the thread (the agent picker, a
  chat folder's default agent, an agent channel) "is exactly as decided". For
  the 2026-11-04 entry, "a conducting worker is a conductor that carries the
  user's servers and skills (the Bot's additions), as the team lead already does
  in conductor mode", and "the Bot's own conductor (its thread) gains nothing".
- The 2026-11-04 entry says a change that adds capability to a conductor needs
  "an explicit owner decision"; the RFC answers that "the owner decision the
  entry asks for is exactly what Phase 0 requests", and Phase 0 asks a
  maintainer to accept the RFC.
- The sign-off also asked the RFC to state that it supersedes the no-dispatch
  rule of `docs/request-for-change/rfc-conductor-split-default.md` (#40737) when
  the per-Bot switch is on, and that the rule still holds when the switch is
  off. The RFC's §8 says so.
- This entry changes no other part of either entry.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/40538#issuecomment-7085631323 --
  the maintainer's decision, "signed, the design is accepted", on the pull
  request that adds the RFC.
- https://github.com/kirodotdev/KiroCrew/pull/40538#issuecomment-7077402863 --
  the maintainer's earlier review input on the same pull request, which ties
  the sign-off to these two entries: "the design itself still needs a
  maintainer to sign off, since §8 asks to supersede two recorded decisions
  (2026-11-04, 2026-11-09)".
- https://github.com/kirodotdev/KiroCrew/pull/40538 -- the RFC's pull request,
  merged as 322beeccc1.
- https://github.com/kirodotdev/KiroCrew/pull/40985 -- records the RFC as
  `accepted` on main, merged as 03224a1e17.
- `docs/request-for-change/rfc-bot-workers-conduct.md` -- §1 and §5.1 (the
  switch), §6 Phase 0 (the exit criterion that asks for this entry) and §8 (the
  entries it supersedes, and what each keeps).
- https://github.com/kirodotdev/KiroCrew/issues/40535 -- the feature request
  the RFC answers.
