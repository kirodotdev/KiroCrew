---
title: The follow-up card's Start sends its prompt
status: draft
author: Pearcekieser, with kirocrew-contributor
created: 2026-10-30
last-audited: 2026-10-30
audited-at: 95f78562f1
doc-pr: 26380
implementation-prs: [25926]
tracking-issues: [25925]
supersedes: []
superseded-by: []
---

# RFC: The follow-up card's Start sends its prompt

- Status: draft. Nothing in this document is built on main. It lands before its implementation because it changes a consent rule, and the First Principles lane reads that decision off the base branch.
- Tracking issue: [#25925](https://github.com/kirodotdev/KiroCrew/issues/25925)
- Implementation: [#25926](https://github.com/kirodotdev/KiroCrew/pull/25926), held until this document is accepted.

## Problem

`suggest_followup` puts a card above the composer with up to three agent-written suggestions. Each one has "Start in new worktree" (or "Start in new chat" when there is no repository to branch), "Add to this session" and "Skip". Today Start creates and scopes a new session and pre-fills its composer with the item's prompt. The person then has to press Send in that session. They have already chosen to run the follow-up by pressing Start, so the second press adds a step without adding a decision. Someone who switches away before pressing Send leaves an empty session behind with the prompt in its draft.

## The rule today

Verified at `95f78562f1`. `FollowUpCard.tsx` states the rule as a deliberate product constraint: "Both non-skip actions PRE-FILL a composer rather than sending ... so a click can never start an unattended turn." The `suggest_followup` tool description in `src/kiro_crew/mcp_tools/control_parts/schemas.py` and `src/kiro_crew/docs/followup-suggestions.md` promise the same thing.

Text from outside a keystroke reaches a session in one of two ways (`website/src/pages/chat/page/launchIntake.ts`). The main surfaces are:

| Surface | What a click does today |
|---|---|
| A navigation that names its own text: `?autoSend=1` from the Projects page, Dev Fleet, the Prompts tab and the command bar | Sends |
| A signed chat link (`?token=`) | Sends the signed prompt |
| An app's chat launch (`window.__mc_chat_launch`), unless it passes `autoSend: false` | Sends |
| An `[OPTIONS:]` chip in a transcript: double-click or Send now (`FollowUpBar.tsx`), or a single click when `dashboard.quick_send` is on (`tryQuickSend` in `ComposerDockView.tsx`) | Sends the option text |
| The follow-up card: Start in new worktree, Start in new chat | Pre-fills the new session |
| The follow-up card: Add to this session | Pre-fills this session |
| A Dashboard card's `kirocrew-dashboard:send` ([rfc-dashboard-card-send.md](rfc-dashboard-card-send.md), draft, implementation #22379) | Proposed: sends the card's text into the card's own thread, only after a recent real interaction while the card frame holds focus, and the turn gets no auto-approval |
| A widget action (`WidgetFrame.tsx`) | Pre-fills only, as a security rule: widget script can post the message itself, so it cannot be told from a click |
| The error card's "Ask the agent", the redaction card, the Projects "Chat" button, an artifact's "Send to session" | Pre-fills |

So one click already sends in several places, and with Quick Send on, a single click already sends agent-written text: an `[OPTIONS:]` chip. The difference is what the person can see. A chip shows exactly the text it sends. A follow-up item shows a title and a description, while the text Start sends is a separate prompt of up to 8000 characters that runs as a new session's task. That is why Decision 3 shows the start of the prompt on the card and the rest behind Show prompt.

Both documents follow one rule: a click sends text with the authority of typed text only when the person pressed a native control and the exact text was on the page at the click, either shown or one toggle away. A Start press passes that test, so its turn takes the same `/api/chat` path and approval mode as a typed message. Risks records the gap the toggle leaves. A Dashboard card send does not pass, because card script picks the text and fires the send. So that document adds the interaction check, refuses the send while an approval or question is open, and gives the turn no auto-approval and no delegation (its sections 3.6 and 3.7). Widget actions avoid the question by pre-filling only.

## Decision

1. **Start sends.** "Start in new worktree" and "Start in new chat" create and scope the new session as today. They then send the item's prompt to that session by its key, as its first message, before the switch.
2. **Only Start changes.** Every other surface in the table keeps its current behaviour. "Add to this session" keeps pre-filling. Widget actions keep pre-filling, because their trust problem is the script, not the click. No other card gains one-click sending through this document.
3. **The person can read what will run before the click.** Each item shows the first three lines of its exact prompt as plain text, under the title and description, with no toggle needed. A "Show prompt" toggle expands the rest when the prompt is longer. The title and description are labels the agent chose; the prompt is what runs, so its opening lines are on the card at every Start and the rest is one toggle away. The card footer says that Start sends the prompt and Add only pre-fills it.
4. **Editing and cancelling.**
   - To change the prompt before it runs, the person presses "Add to this session". That pre-fills the composer, where they edit it and send it themselves. This route already exists and is unchanged. Its cost is that the work then runs in the current session and directory, not in a new worktree or chat. A person who wants both can start a new chat and paste the edited prompt there. This document accepts that cost; see Alternatives for a Start-then-edit button.
   - To not run it, they press Skip.
   - After Start, the new session's turn has the normal Stop control.
   - No separate edit or confirm step is added to Start. That would bring back the second press this change removes.
5. **One run per click.** A Start sends at most once. The item leaves the card once its send is accepted, so its Start button cannot run the prompt a second time. A second click on the same item while its start is in flight does nothing, including on a card that was remounted by a session switch or a route change.
6. **The contract text changes with it.** The tool description, `followup-suggestions.md`, the owning spec paragraph in `docs/system-specs/modules/learn-cron-dashboard.md` and `docs/architecture/followup-suggestions-trust-model.md` say that Start sends and Add pre-fills.

## When the send does not go through

The card shows every failure on the item it came from, unless that card is gone, in which case it shows a page notice. It never puts the prompt in another session's composer.

| Outcome | What happens to the new session | What the person sees |
|---|---|---|
| Refused before reaching the server, or refused by it, and the new session is untouched (no messages, not running) | Closed, so a retry on the worktree route reuses the worktree instead of getting a 409 "in use" refusal | Not sent, on the item, with the item kept so Start can retry |
| Refused, but the new session is no longer untouched (another window opened it and started a turn) | Kept, because closing it would end that turn | Not sent, with a pointer to that session in the sidebar. No retry is offered, because on the worktree route the kept session holds the worktree and a retry gets a 409 |
| Refused, and closing the untouched session fails | Left as it is | Same as the row above: point to the session, no retry offer |
| No answer (a transport error or a late response): the turn may already be running | Kept | The item leaves the card, the first message carries the pending-delivery mark, and a page notice says to open that session and check it. No retry is offered, because it could run the prompt twice |
| Accepted, but the switch to the new session fails, or the person opened another session meanwhile | Running | The item leaves the card. A page notice names the new session in the sidebar, and the person stays where they are |

The second and third rows are what #25926 does not yet do. Today the delete after a refused send ignores its own failure, and the card still offers a retry that gets a 409. Those two rows are a condition of this document.

## Security

Start now sends agent-written text as a user turn in a new session with the agent's tools. The protections are:

- the card is posted only on a turn a person sent, to that person's own session (`apply_session_directive`);
- every field is redacted server-side before it is posted (`_redact_followup_item`);
- the full prompt can be read on the card before the click;
- the edit route through Add stays available.

The new session is scoped to its worktree before it can receive a turn, so the prompt never runs in the default directory. The prompt reaches the server through the same `/api/chat` path and approval mode as a typed message, so tool approvals apply to it as they do to anything the person sends.

## Alternatives considered

- **Keep the pre-fill and focus the new composer.** It still needs the second press, so it does not solve the problem.
- **Send only after a confirm dialog that shows the prompt.** It is a second press with a larger surface, and "Add to this session" already gives a person who wants to review and edit a way to do that.
- **Expand the whole prompt by default.** Rejected: a prompt can be 8000 characters, and three of them make the card taller than the transcript it sits on.
- **Collapse the prompt entirely behind Show prompt.** This is what #25926 first built. Rejected in review: most people would press Start without opening the toggle, so most starts would run text nobody read. Three lines show the task's opening, which is where an agent states what the prompt does.
- **A Start-then-edit button that opens the new worktree session with the prompt pre-filled.** That is today's Start. Keeping it beside a sending Start puts two near-identical buttons on every item, and the card already has three.
- **Run the start in the background without switching.** This is a separate change (card-3). It builds on this one and does not change the consent question.

## Risks

- A person who presses Start without opening "Show prompt" has read only the first three lines of a longer prompt. An agent could put the real instruction after line three. The footer and the label say Start sends, the toggle shows there is more, and the agent cannot make Start run anything other than the prompt the card shows.
- The card is sent to every open dashboard window, and Start clears the item only in the window where it was pressed. So the same item can be started from a second window. On the worktree route that second start is refused with a 409, because the first session is scoped to the worktree. On the new-chat route it opens a second session that runs the same prompt in the same project directory. Clearing the item everywhere needs a new server broadcast and is out of scope.
- The refused-send cleanup decides whether the session is untouched from this window's copy of the session list. A turn that another window starts inside the same request may not have reached that copy yet. Closing that gap needs a server-side "delete only if idle", which is out of scope.

## Rollout

1. This document. Exit: on main with `status: accepted`.
2. [#25926](https://github.com/kirodotdev/KiroCrew/pull/25926), including the second and third rows of the failure table with a test each, and the three-line prompt preview from Decision 3. Exit: every row of the Decision and the failure table is covered by a test, and the contract text in point 6 matches.
3. Card-3 (start without switching), after #25926 merges.

## Open questions

None. Whether to show the prompt by default was settled in review: the first three lines are shown, the rest behind Show prompt.
