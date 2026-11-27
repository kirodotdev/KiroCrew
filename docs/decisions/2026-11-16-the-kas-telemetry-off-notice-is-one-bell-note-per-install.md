# The notice that KAS cannot confirm the kiro-cli telemetry opt-out is one bell note per install, not a row in every chat

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-16

## Decision

When Kiro Crew asks the KAS engine to turn its telemetry off and cannot confirm that it did,
it says so once per install, as one note in the dashboard's notification bell the first time
a session starts on such an engine, and no longer as a row at the start of every chat; the
notice that a chat's own folder opt-out was not applied stays a row once in that chat, and a
chat that showed it still gets the opt-out row when its answer changes back.

## Why

- The request was to stop the notice repeating in every chat while keeping the person told once.
- The sentence is the same every time, because the engine never confirms the setting, so it
  belongs to the install rather than to each chat. The not-applied notice keeps showing once in
  the chat it concerns.
- The bell already carries one-time notes like it, without a new settings line or a "Don't show
  again" control.
- It is once per install rather than once per change of the setting: a later opt-out would
  repeat a sentence already read, and re-arming it would mean re-checking the setting every time
  a KAS engine starts.
- An engine that reports the telemetry value it applied would let the note go when the value
  matches; that is a change to the engine, a separate change in Kiro Crew, and it would not undo
  this one.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/35967#issuecomment-6916506031 -- the maintainer's ruling.
- https://github.com/kirodotdev/KiroCrew/issues/35967 -- the request.
