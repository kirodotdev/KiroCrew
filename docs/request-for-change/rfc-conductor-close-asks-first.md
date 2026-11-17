---
title: Closing a conductor with open workers asks first
status: draft
author: tim-finnigan, with kirocrew
created: 2026-11-08
last-audited: 2026-11-08
audited-at: 0a53e02b65
doc-pr: null
implementation-prs: [32788]
tracking-issues: [31629]
supersedes: []
superseded-by: []
---

# RFC: Closing a conductor with open workers asks first

- Status: `draft`. Acceptance is requested from a maintainer; the status flips to `accepted` when one records it on this line with the date. Nothing is on main. This document lands first because the change adds a confirmation to a close that has none by default, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#31629](https://github.com/kirodotdev/KiroCrew/issues/31629)
- Implementation: [#32788](https://github.com/kirodotdev/KiroCrew/pull/32788)

This amends [rfc-conductor-close-closes-workers.md](rfc-conductor-close-closes-workers.md), which chose option A and left "A confirm dialog or a 'keep workers' choice on close (option B)" under "Not decided here". That document is accepted and is not edited here; this one decides the confirm dialog half of option B and records its own acceptance separately.

## Problem

Since [#30877](https://github.com/kirodotdev/KiroCrew/pull/30877), closing a conductor (an Ultra Mode root or a Bot thread) also closes every open worker it still manages. The close asks nothing unless the person turned on Confirm Before Closing Session, which defaults off (`confirmCloseSession: false` in `website/src/pages/chat/ChatSettings.tsx`). So one click on the conductor's close ends work in N other sessions, some of them mid-turn, without saying so.

## Current behavior

Verified at `0a53e02b65`:

1. `DELETE /api/chat/slots/{slot}` closes the conductor and then its managed workers through `_close_managed` in `dashboard/chat_api/conductor_close.py`.
2. The browser asks first only when `confirmCloseSession` is on or the closed session itself has live work (`closeConfirmPrompt` in `website/src/lib/closeConfirmPrompt.ts`, reading `closeBusyReasons`). A conductor that is idle while its workers run closes with no prompt.

## Decision

A person's close of a conductor asks first when it would take workers with it, whatever `confirmCloseSession` says.

| Workers the close would take | What the close does |
|---|---|
| N > 0 | asks "Close this session and its N workers?", with Close all and Cancel |
| count could not be read | asks the same question without a number |
| 0 | closes with no prompt, as today |

- N is the server's count of what the close would take, read from a read-only preview of the same rule the close runs, so the number cannot disagree with what closes.
- Every close path that cascades asks: the sidebar ✕, a session menu's Close, ⌘W / Ctrl+W and its ⌥⇧W alias. The folder "Close sessions and delete" panel shows the count on the conductor's row, and its own Close button is the confirmation.
- Close all runs the same close as before. Cancel closes nothing.

The reason is that every other close touches one session, so a setting that is off by default was enough. A conductor's close touches N sessions the person did not click on, and the off-by-default setting no longer matches what one click costs. A conductor with no open workers costs one session, so it keeps today's behavior.

## Not decided here

- A "keep workers" choice on close, the other half of option B. Confirming closes the workers; there is no way to close the conductor alone.
- Any change to what the close takes, to agent, app or idle-sweep closes, or to `agent.session_manager_chain`.
