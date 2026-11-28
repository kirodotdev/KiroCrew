# A Bot's Heartbeat wake sends only its final reply to a linked thread

Decided by: Bolin Chen (@bolichen97, maintainer)
Date: 2026-11-16

## Decision

A Bot's Heartbeat wake posts no `_Thinking…_` placeholder or task stream to a linked thread or channel, and a quiet one (`nothing_to_do`) posts nothing there; every other quiet end keeps its outcome line.

## Why

- The Heartbeat RFC (`docs/request-for-change/rfc-crewmate-heartbeat.md`) says a quiet wake "ends with `nothing_to_do` and posts nothing".
- The `nothing_to_do` tool tells the model its note is a record "the user can inspect; it is not a message to them".
- At the 30-minute interval the outcome line and the task stream made two or more posts every wake, all day, in the person's DM.
- The outcome line exists so that a watched patrol or auto-nudge cycle does not look like it never ran; that reason does not apply to a Heartbeat wake, so those turns keep it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37541#issuecomment-6910240093
- https://github.com/kirodotdev/KiroCrew/issues/37541
- https://github.com/kirodotdev/KiroCrew/pull/23911
