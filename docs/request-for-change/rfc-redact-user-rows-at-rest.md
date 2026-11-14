---
title: Redact credentials in user-typed transcript rows at rest
status: draft
author: rnoack1
created: 2026-11-04
last-audited: 2026-11-04
audited-at: 55d54e3307
doc-pr: null
implementation-prs: [10965]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: redact credentials in user-typed transcript rows at rest

Scrub credentials out of every transcript row when it is written, including the rows a
person typed. Today only model and system rows are scrubbed. This reverses a rule the code
pins, so it needs a recorded decision before it ships.

## Problem

A transcript row is an egress. `ConversationLog` persists it under `sessions/`, and every
dashboard client that opens the conversation is served it. Search, memory consolidation and
session summaries read it too.

At `55d54e3307`, `_redact_at_write_boundary` in `src/kiro_crew/history_append.py` returns
early when `role == "user"`. Its docstring says "text the user typed is stored verbatim".
So a credential a person pastes into a channel (Slack, Discord, Telegram, Teams, Webex,
Feishu, iMessage, WeCom, Weixin, WhatsApp) is kept as typed, for as long as the transcript
exists. In a shared channel more than one human reads that transcript. The person who
pasted the key may not be the only one who sees it again.

Three copies of the same text also skip the boundary and are kept raw: the session title a
channel transport derives from the first message, and the live dashboard projection that
Discord, Telegram and the Slack mirror write into a linked slot.

## What this amends

`rfc-redaction-explain-and-reveal` (`partial`) rests on the same pin. Its section 2 ends
the one-time coach with "Keep secrets at their source. Your messages are saved exactly as
typed.", and its "What does not change" section gives the reason: messages written by
the user are saved exactly as typed and remain with the conversation. That RFC is not
edited. This one amends both statements for channel-typed user rows: once it is accepted,
those rows are saved with credentials removed, so the coach's closing line must no longer
promise verbatim storage for them. Dashboard-typed rows are still saved as typed (see
"Costs accepted" below), so a revised line has to cover both cases.
Neither sentence ships as UI text on main today; only that RFC and its prototype page
carry it.

## Decision proposed

1. `_redact_at_write_boundary` scrubs every role through `redact_log_via_context`, the
   host's composed credential policy. The `role == "user"` early return is removed.
2. The copies that bypass the boundary are scrubbed where they are made: each channel's
   session title before its 40-character cut, and the live slot projection in Discord,
   Telegram and the Slack mirror.
3. On a host whose policy fails to compose, the row is still written, with
   `<withheld: redaction unavailable>` as its body, instead of the append raising.
4. A window replay through `append_if_absent` treats a raw or a redacted copy on disk as the
   same row, so a row is never written twice because the two forms differ.

## Costs accepted

- **The rewrite cannot be undone.** The original span is not kept. A false positive
  rewrites a person's own words permanently, and a later pattern fix cannot restore them.
- **A cold resume loses intent.** A person who gives the agent a key on purpose ("deploy
  with key X") will find `[REDACTED: credential]` when the transcript is replayed to a model later.
- **Two rules in one linked transcript, for now.** The dashboard slot save builds its rows
  in `chat_persistence` and does not pass through this boundary, so a row typed into the
  dashboard is still stored verbatim. A dashboard tab linked to a channel thread holds
  user rows under both rules until that save is routed through the boundary.
- **Old rows stay as they are.** The scrub runs at write. Transcripts written before the
  change are not migrated.

## Alternatives considered

- **Keep the exemption and scrub in each persister.** An earlier revision of #10965 did
  this: eleven caller scrubs, their sink-registry rows and an AST ratchet. Every new
  persister had to be registered, and a missed one leaked silently.
- **Scrub at display time only.** The bytes at rest stay raw, so search, memory
  consolidation and summaries still read the credential, and any reader that skips the
  display pass shows it.
- **Encrypt or reveal-on-demand.** Keeping the original behind a reveal control, as
  `rfc-redaction-explain-and-reveal` does for model output, would remove the
  irreversibility cost. It needs a protected store for the originals, which nothing on
  base has. It is a reasonable follow-up, not a precondition.

## Not a goal

- Telling the reader a row was rewritten. A rewrite mark and its dashboard cue are a
  separate change.
- New credential patterns. Rows go through the policy the host already composes.
- The dashboard's own typed rows. Routing the slot save through the boundary is its own
  change, and it ends the two-rule state above.

## Open questions

1. Should the dashboard slot save route through the boundary in the same release, so one
   transcript never holds two rules?
2. Should a withheld row emit one security event the first time it happens, so an operator
   sees a miscomposed host instead of only placeholder text?
3. Is a reveal-on-demand store for user originals worth its cost, given that it removes the
   only cost here that cannot be reversed?
