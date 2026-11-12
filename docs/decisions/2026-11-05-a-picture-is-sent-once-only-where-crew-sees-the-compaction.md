# A picture a session already holds is sent once only on harnesses whose compaction Crew sees

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-05

## Decision

A session remembers the pictures its conversation already holds, so text that
names the same picture on every turn sends it once, then its path with a note.
That record is kept only on the harnesses whose compaction, started by Crew or by
the harness itself, sends a frame Crew reads: kiro, kas, codex and claude
(`ACP_BACKENDS_SENT_IMAGE_RECORD`). Every other harness keeps no record and sends
a named picture on every turn. A harness joins the set only once it is shown to
report its own compaction.

## Why

- The backend replays every picture a session was sent, so a picture named on
  every turn added one more full-size copy per turn until every request was
  refused (#15264).
- On opencode, goose and pi a compaction the harness starts on its own sends
  nothing Crew reads, so a record there would tell the model it holds a picture
  the summary dropped (#20731). Without a record those harnesses behave as before,
  and no harness is worse off.
- The other option, keeping the record everywhere and documenting the gap, could
  leave the model without a picture it was told it already had.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/20474#issuecomment-6672738208 --
  the maintainer choice, option (a).
- https://github.com/kirodotdev/KiroCrew/pull/20474#issuecomment-6428392098 --
  the two options as the author set them out.
- https://github.com/kirodotdev/KiroCrew/issues/20731 -- the silent-compaction gap.
