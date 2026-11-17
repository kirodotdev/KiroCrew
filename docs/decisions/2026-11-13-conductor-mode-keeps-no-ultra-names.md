# Conductor mode keeps no Ultra names, and Title Case labels write it in lower case

Decided by: @bolichen97 (maintainer)
Date: 2026-11-13
Supersedes: 2026-11-13-the-crew-mode-feature-is-named-conductor-mode.md (in part: "Title Case only inside a label whose own convention is Title Case", and the values that "keep their spelling")

## Decision

No Ultra name is kept for compatibility: the refusal code is `conductor_mode_only`,
the config key is `telemetry.traces_conductor_mode`, and the trace values are
`conductor_mode` (the `kirocrew.crew.root_kind` value, the round trace kind, and the
span names `conductor_mode round N` and `conductor_mode outcome ...`), with no
fallback for the old spellings. A label in Title Case writes the name in lower case
too ("Start New Chats in conductor mode"). Everything else in the entry it
supersedes applies unchanged.

## Why

- Ultra Mode only ever lived behind the default-off feature preview, so nothing
  outside the preview depended on the old spellings.
- One casing everywhere, the way the dashboard writes "bots".

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37086 -- the issue that states the
  ruling.
