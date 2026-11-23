# Adjudication (BLOCKED) and overrides

Moved verbatim from the pipeline-conductor `SKILL.md` core; the core says when to read each part.

## Adjudication (BLOCKED) and overrides

**This is the EXCEPTION PATH.** You enter it when a worker reports `BLOCKED`,
never by surveying boards to find something to rule on. A BLOCKED report is a
request for a RULING, and it must arrive with the evidence and the 2-4 options
already assembled — if it does not, send it back for them rather than assembling
them yourself. Then rule from what the report contains plus ONE verification
against the current head. Going and re-deriving the situation is the behaviour
the two-column mode exists to stop, and it is not made legitimate by the word
"adjudication".

Rule by:

- Finding real, remedy wrong (the classic: "revert") → look for the narrower
  forward fix the finding's own wording points at.
- Real but out of scope → route: fix now / own PR / backlog with
  cross-reference. "Not applicable to THIS PR" is the honest disposition for a
  zero-delta-vs-base finding — never "false positive".
- Deterministic red inherited from {default_branch} → prove base-owned three
  ways (base's own run red; gate postdates base; file absent from the diff) →
  ONE minimal unblocking PR for the whole fleet.
- **Override** only when ALL hold: every lane settled · sole red · head SHA
  pinned in the override text · rationale public on the PR · branch
  push-frozen afterwards except review responses. Record every ruling with its
  rejected options. What reaches the human is the four classes in "What you
  decide, and the four things you escalate" -- nothing else does; a design or
  product call inside one item is yours, and an item that IS a design ask was
  refused at intake under `policy.refuse_design_asks`.
- **One sample, then the class.** A mass anomaly — the same red on every open
  PR, an identical failure across workers — is diagnosed from ONE sample and
  fixed as a class. Reading all N of them is the expensive way to learn the
  same thing N times, and it delays the one fix that clears them all.
- **Subtraction, when rounds keep reopening in one place.** Consecutive review
  rounds landing in one function span indicate ONE unwritten contract, not N
  separate mistakes. The convergent ruling there is usually to DELETE the
  mechanism or split the entangled site out, not to apply an Nth patch. Rule for
  the subtraction while the round count is still small; a patch that survives
  review by narrowing itself each round is a mechanism the design does not want.
- **Never re-dispatch a CI run to unstick one queued job.** Ask what a re-run
  REPLACES, not what it retries: a re-run discards the whole run's passing
  check-run set, so unsticking one job throws away every green in that run and
  buys a full round trip.
- **Park with the dependency, and release the claim when you park.** A parked
  item records what it waits on, so it is releasable by a later cycle instead of
  quietly aging; holding a claim on work nobody is doing blocks the operator who
  could.
- **Security-sensitive findings never go to a public channel.** A credential
  path, an injection vector, a bypass: those go to the human directly, never
  into a PR comment, an issue body, or a digest.
- **A worker HOLDING for your ruling builds the artifact both branches need**,
  which is almost always the RED reproduction: the failing test that pins the
  current wrong answer. It is required if you authorize the fix, it is the
  evidence if you uphold the BLOCKED, and it commits to neither option. Say so
  when you acknowledge the escalation — otherwise the worker waits, and a worker
  waiting is the same wasted session as a session parked.
