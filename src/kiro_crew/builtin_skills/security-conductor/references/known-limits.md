# Known limits

Moved verbatim from the security-conductor `SKILL.md` core. State them to the operator; don't hide them.

- Every script call is `execute_bash`, which is mounted but never auto-approved:
  `allowedTools` cannot match arguments, so trusting the bundled scripts would
  mean trusting arbitrary shell. Unattended operation needs the operator to arm
  this session in trust mode — without it the patrol stalls on its first scope
  check, not on its first intervention. Pre-approving shell alone is not enough:
  `session_send`, `session_stop` and `session_close` are not auto-approved
  either, so a seed or stop stalls. `session_create`'s `message` is refused
  for this agent (`message_needs_send_approval`): create, then `session_send`.
- The verifier's independence is procedural, not enforced. It comes from a fresh
  session and a brief that withholds the auditor's reasoning; a shared model can
  still share a blind spot.
- An auditor's own report is the only evidence that it stayed inside the rules of
  engagement. The scope script gates what it ASKS about, not what it does, which
  is why the forbidden list is written as absolutes and why a self-reported
  circumvention is a stop condition rather than a note.
- A lesson only changes behaviour on the NEXT round, and only after a human
  approves it. Nothing here learns inside a round.
- **A policy block with no finding is recorded as both a golden path and a
  lesson.** A `policy_block` is deliberately an event rather than a finding, so it
  has no id for `--source-finding` to resolve; `propose-lesson` therefore takes
  `--source-policy-block` instead, and a lesson names exactly one of the two. Both
  halves of the retrospective's ruling survive a block that filed nothing — the
  golden path that stops a future fix re-breaking the operation, and the guidance
  that stops the next auditor walking into the same refusal. A policy-block
  reference is not a foreign key and cannot be, since the events are the round's
  own record rather than a table in the ledger, so this source is attributable but
  not referentially checked.
- One set of rules of engagement = one target. A second target is a second set,
  reviewed on its own.
