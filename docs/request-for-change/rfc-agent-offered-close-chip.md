---
title: The dashboard agent may offer a one-click close-session chip when its work is done
status: draft
author: rnoack1, with kirocrew
created: 2026-11-14
last-audited: 2026-11-14
audited-at: f9ebf3a1e2
doc-pr: null
implementation-prs: [6823]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: The dashboard agent may offer a one-click close-session chip when its work is done

Status: draft. Acceptance is requested from a maintainer. It flips to `accepted` when one records it on this line with the date. Nothing is on main. Code references were read on main at `f9ebf3a1e2`. This document changes no code. It lands before [#6823](https://github.com/kirodotdev/KiroCrew/pull/6823) because that PR changes what the default dashboard agent says at the end of its work, and the First Principles lane reads that decision from the base branch.

## Summary

When a dashboard session's work is clearly finished, the default agent may end its reply with the control tag `<!-- option-action:close=<label> -->`. The dashboard shows it as a "Close session — <label>" chip after the option chips. Taking the chip closes the session through the same close as the sidebar ✕. No model turn is spent.

## Motivation

### Current state on main

- The tag already has a reserved meaning and a backend strip. The control-tag comment in `src/kiro_crew/constants.py` lists `<!-- option-action:close=<label> -->` as the "dashboard close-session chip", and `_is_string_tag_line` in the same file strips it from every channel's copy of a reply.
- No agent emits the tag and the dashboard does not render it. A user who is done must either type a reply, which spends an LLM turn to say nothing, or find the sidebar ✕.
- The sidebar close (`useSessionActions().close`, `website/src/hooks/useSessionActions.ts`) asks first when the session is busy, or when the user's "Confirm Before Closing Session" setting is on (`confirmCloseSession`, default off in `website/src/pages/chat/ChatSettings.tsx`). It records the close so the reopen shortcut brings the session back.
- A close keeps the session's drafts (the `closeConfirmPrompt` doc comment in `website/src/lib/closeConfirmPrompt.ts`), and a closed session can be reopened.

### The decision this records

The change is to a default: the dashboard's critical rules (`_DIFF_RULE_DASHBOARD` in `src/kiro_crew/context_assembly/output_contract.py`) gain one rule, so every dashboard session's agent may offer the close. That is a product-shape change, which is why it needs this record.

## Goals

- A user can close a finished session in one place, with no model turn.
- The close behaves exactly like the sidebar close: same asking rule, same reopen record, drafts kept.
- A stray click does not close a session that would otherwise close without asking.

## Non-goals

- Closing anything automatically. The chip closes nothing by itself.
- Any action other than `close`. An unknown action offers nothing.
- Other channels (Slack, Discord, Webex, the CLI). The backend strip already hides the tag there.
- The side chat and the app-SDK embed. Only the main chat page renders the chip.
- Changing how any other close confirms.

## Design

### The rule

The dashboard variant of the critical rules gains this text. The channel variant does not.

> When the session's work is clearly finished and nothing is left open (no question, choice, approval or follow-up you expect), you may offer the user a one-click close: end the reply with `<!-- option-action:close=<label> -->` on its own line, after any `[OPTIONS:]` line, where `<label>` is a few words naming what was done. The dashboard shows it as a close-session chip the user can take or ignore; it closes nothing by itself and is hidden from every other channel. Never offer it while work is still running or something is still owed.

### The chip

- It renders after the content chips: on its own row in the multiline layout, beside the strip in the scroll layout.
- It reads "Close session — <label>", with a danger border and an ✕ glyph, so it does not look like a content option.
- It can never send its label as chat text. It has one click handler and no send segment.
- It is offered only from the newest reply. Once the scan crosses a newer user turn, even a failed one, no close is offered, because that request is still owed.

### The close

- The chip calls `closeSessionAsking`, the sidebar close's own body, exported from `useSessionActions.ts`.
- Where that close would ask anyway (busy session, or the setting on), one click opens its dialog.
- Where it would close at once, the first click arms the chip ("Click again to close — <label>") for 3 seconds, and only a second click closes. The armed state resets on a newer offer or another session.

## Migration plan

One phase, [#6823](https://github.com/kirodotdev/KiroCrew/pull/6823).

Exit criteria:
- A reply ending in the tag shows the chip; a reply without it renders, copies and searches as before.
- The chip closes through `closeSessionAsking`, and the reopen shortcut brings the session back.
- No chip appears across a newer user turn.
- The dashboard rule is present only in the dashboard variant of the critical rules.

## Backward compatibility

Every new prop is optional, and no locale key is renamed or removed. A reply without the tag behaves exactly as today. Other channels already strip the tag.

## Security considerations

The label is model-written text. It is shown, never interpreted. The tag reader is the same line reader the keep-visible marker uses (`isStringTagLine`), mirrored by the backend's `_is_string_tag_line`, so the chip and the backend strip agree on every line.

## Alternatives considered

- **No producer, renderer only.** Ships a chip nobody can see. The First Principles lane blocked #6823 on exactly this.
- **An opt-in setting for the rule.** Keeps the default unchanged, but almost no user would find it, so it is close to no producer at all.
- **Close with no confirmation.** One stray click would close a session. The two-click arm costs a second click only where no dialog would ask.
- **A guard that refuses to close over an unsent draft.** Removed, because a close keeps drafts.

## Open questions

1. Should the offer be limited further, for example to sessions that already produced a PR, CR or other deliverable? The rule leaves that judgement to the agent today.
