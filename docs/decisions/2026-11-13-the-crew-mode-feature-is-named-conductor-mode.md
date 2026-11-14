# The Crew Mode feature is named conductor mode

Decided by: @bolichen97 (maintainer)
Date: 2026-11-13
Supersedes: 2026-10-29-crew-mode-ships-behind-a-default-off-feature-preview.md (in part: the switch and its surfaces "are named **Ultra Mode** in user-facing text")
Supersedes: 2026-10-30-ultra-mode-is-marked-with-a-bolt-and-its-tab-is-the-board.md (in part: "No user text says "conductor" for Ultra Mode", for the feature's own name only)

## Decision

The dashboard's Crew Mode feature is named **conductor mode** wherever a person
reads it, written the way the dashboard writes "bots": "Conductor mode" at the
start of a label or sentence, "conductor mode" inside one, and Title Case only
inside a label whose own convention is Title Case ("Start New Chats in Conductor
Mode"). Non-English catalogs keep the English name untranslated with a capital C,
as they write "Bot". The code names that carried the old name follow it; code,
config keys and routes that say `crew_mode` keep it. The feature's name is the one
place user text says "conductor"; a message the main chat delivered still reads
"From the main chat". The entries below were written while the feature was named
Ultra Mode. They are left as written and apply unchanged to conductor mode:

- 2026-10-29-crew-mode-ships-behind-a-default-off-feature-preview.md (except the name)
- 2026-10-30-the-ultra-mode-name-wears-the-purple-to-blue-gradient.md (the gradient is on the words "Conductor mode")
- 2026-10-30-ultra-mode-is-marked-with-a-bolt-and-its-tab-is-the-board.md (except the line above)
- 2026-11-02-an-ultra-mode-row-shows-only-what-needs-attention.md
- 2026-11-04-ultra-mode-pauses-its-crew-and-runs-on-the-team-lead.md
- 2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md
- 2026-11-06-every-task-a-bot-dispatches-goes-into-its-work-ledger.md
- 2026-11-06-the-ultra-mode-chip-is-grey-while-off.md

## Why

- A maintainer ruled that the product name is final as conductor mode and asked
  for the code, docs and UI names to change to it, with every place inspected.
- Entries are immutable, so the old ones keep the name they were decided under;
  this entry is what maps them to the new one.
- Values a running install, another machine or a saved query may already hold
  keep their spelling: the `ultra_mode_only` refusal code, the saved config key
  `telemetry.traces_ultra_mode`, and the telemetry values `ultra` (the
  `kirocrew.crew.root_kind` value and the trace kind) and the span names
  `ultra round N` and `ultra outcome ...`.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/36869 -- the issue that states the
  ruling.
