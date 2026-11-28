---
title: Optimize Before Sending — a send-as-typed chord, and text the optimizer must leave alone (suggested prompts, paste blocks, fenced code, block quotes)
status: draft
author: tmack8001
created: 2026-11-25
last-audited: 2026-11-25
audited-at: a2d7d28328
doc-pr: null
implementation-prs: []
tracking-issues: [39145]
supersedes: []
superseded-by: []
---

# RFC: Optimize Before Sending — a send-as-typed chord, and text the optimizer must leave alone (suggested prompts, paste blocks, fenced code, block quotes)

Status: draft. Acceptance requested from a maintainer of the dashboard chat composer. This answers the request on [#39145](https://github.com/kirodotdev/KiroCrew/issues/39145#issuecomment-6985505981) to propose the design through the request-for-change process. Code references were read on main at `a2d7d28328`. This document changes no code.

## Summary

With Settings > Chat > Composer > Optimize Before Sending on, the first Send of every non-empty draft runs the optimizer. Today the person has no one-keystroke way to say "send this one as typed", and the composer does not use what it already knows about a draft to skip an optimize that cannot help.

This RFC proposes three changes and a fourth that is built last:

1. **Send as typed.** While Optimize Before Sending is on, the existing chord Cmd/Ctrl+Shift+Enter sends the draft without optimizing it. While the setting is off, the chord keeps its present meaning (run Optimize prompt and keep the rewrite for review).
2. **Skip by default when the composer knows.** A draft counts as already reviewed, so Send sends it as typed, when the composer filled it from the agent's suggested follow-up, or when nothing is left to optimize: the draft holds only paste blocks, protected spans (fenced code, block quotes) and whitespace.
3. **Leave fenced code and block quotes as written.** The optimizer must not change a fenced code block or a block quote in a draft, whether the optimize was started by Send, the sparkle button or the chord. The server already enforces this for paste placeholders and for file and folder mentions; this extends the same rule to two more kinds of text people already use to mean "this is literal".
4. **Optimize and send, a separate opt-in setting, built last.** #39145 also asks for the rewrite to be sent with no review. That is a different setting from Optimize Before Sending, off by default, and it does not use the chord: the chord keeps the two meanings above. It needs a rule for what happens when the optimizer fails (proposed in open question 1), so it is its own phase after 1 to 3, and it can move to its own RFC if the maintainers prefer.

#39145 bundles two requests: skip the optimize when the composer can tell (this RFC's change 2, which also covers its point 1), and optimize and send without review (change 4, built last). Changes 1 and 3 are new.

## Motivation

### Current state on main

- `useOptimizeBeforeSend` (`website/src/components/chat-input/optimizer.ts`) wraps Enter, the Send button and the Lexical send. It intercepts a Send when the setting is on, the host offers the optimizer, the draft is non-empty, the draft does not start with `/`, and the slot's draft is not already in `reviewedRef`. It then starts an optimize, adds the slot to `reviewedRef`, and sends nothing. The next Send of that draft goes through. `reviewedRef` clears when the draft in that slot is emptied. Nothing else can put a slot in `reviewedRef`.
- There is no send-as-typed action. The only route today is a three-step workaround that works because of an accident of the same code: press Enter (the optimize starts and the slot is marked reviewed), wait, press Escape (`useComposerOptimizeEscape` in `optimizeEscape.ts` cancels the request and the draft is handed back unchanged), wait, press Enter again (the slot is already reviewed, so the draft is sent). It costs two waits and two extra keystrokes, and a request to the model is still started and aborted.
- Cmd/Ctrl+Shift+Enter is bound to Optimize prompt in two places: the textarea's keydown in `chat-input/keyboard.ts` (guarded by `promptOptimizer`, and a no-op rather than a send on a host that opted out) and the Lexical editor's `onOptimizeChord` in `LexicalComposerInput.tsx`. `shortcutRegistry.ts` lists it as `optimize-prompt`, dispatch `code`. The sparkle button and the plus-menu row call the same `optimizePrompt`.
- Large pastes are already tracked. `usePasteTokens` collapses a paste into a paste block when it has at least `PASTE_THRESHOLD_LINES` (3) lines or `PASTE_THRESHOLD_CHARS` (200) characters (`utils/pasteTokens.ts`), unless Settings > Chat > Composer > Show full pastes (`showFullPastes`) is on, and Cmd/Ctrl+Shift+V forces one raw paste.

### What the optimizer does to a paste block, a fence and a quote today

This matters for the design, so it is stated first. The optimizer does not rewrite the text behind a paste block. The draft holds a placeholder token (`[ Paste #N · M lines ]`); the content is sent to the model as context only (`pastes` in `usePromptOptimizer`, `_PASTE_CONTENT_BUDGET` in `handlers/optimizer.py`); and the server rejects a rewrite whose placeholders are not preserved exactly (`_paste_token_counts`, `_restore_paste_ids`). So in the default configuration a long pasted log or code sample is not reworded.

What the optimizer can still do to a draft that contains a paste is:

- reword the prose around the placeholder, which may be one short line such as "why does this fail?";
- cost a model round trip, which is the whole of the cost when the draft is only a placeholder;
- reword text that is not in a block: a paste below both thresholds, any paste while `showFullPastes` is on, a raw paste, or code typed or dictated into the composer, including code the person put in a fence on purpose.

Fenced code and block quotes have no such protection. `OPTIMIZER_SYSTEM` in `handlers/optimizer.py` tells the model to keep paste placeholders, file mentions and folder mentions verbatim, and to return a clear prompt unchanged; it does not mention fenced code or block quotes, and nothing after the model call checks them (the checks are `_strip_outer_wrapper_tag`, the paste-token multiset, `redact_credentials` and the `changed` comparison). A person who fences a log line or a code sample to say "do not touch this" gets no guarantee that the optimizer agrees. This was read from the prompt and the response checks; I have not run the optimizer on a fenced or quoted draft to see what it does, and Phase 0 does that.

The cases the person reported (pasting logs and code and wanting them sent exactly) therefore split into "already protected, only the latency is wasted" and "not protected, because it never became a block". This RFC does not claim the second group is large. Phase 0 below measures it before any design depends on it.

## Goals

- One keystroke sends the current draft exactly as typed, while Optimize Before Sending stays on.
- The composer skips an optimize it can prove adds nothing, using facts it already holds, never by reading the text.
- Fenced code and block quotes come back from the optimizer exactly as the draft had them.
- A person can tell, from the setting's description and the chord's label, which sends are optimized.

## Non-goals

- Making optimize and send the default, or tying it to Optimize Before Sending. It is a separate opt-in setting (Phase 4).
- Detecting that text "looks like a log or code" by reading it.
- Any change to the paste-block preservation rules, or to what the optimizer does to the rest of the draft.
- Styling fenced blocks in the composer (a separate issue).
- Any change to the default value of Optimize Before Sending (it stays off).
- Any change to Send, Enter, steer or queue behaviour while Optimize Before Sending is off.
- The Bot composer's optimizer (tracked in #26851).

## Design

### 1. The chord follows the setting

| Optimize Before Sending | Enter / Send button | Cmd/Ctrl+Shift+Enter |
|---|---|---|
| off (default) | sends as typed | Optimize prompt, rewrite kept for review (today) |
| on | optimizes first, then sends on the next press (today) | **sends as typed** (new) |

The sparkle button and the plus-menu row keep calling Optimize prompt in both rows, so the manual optimize stays reachable. With the setting on, Enter already optimizes, so the chord's old meaning is the least needed one.

The design principle is that the chord is always the opposite of what Enter does. With the setting off, Enter sends as typed and the chord optimizes for review, as today; with the setting on, Enter optimizes for review and the chord sends as typed. The chord never sends an optimized rewrite by itself.

Rules for the new row:

1. The chord calls the host's send directly, bypassing `useOptimizeBeforeSend`, and marks nothing as reviewed. It uses the same guards as Send: a disconnected composer, an optimize in flight, a typing lock, terminal mode and a running turn behave as Enter does (a running turn steers or queues the typed text).
2. A host with `promptOptimizer` off keeps today's rule: the chord does nothing, and never sends (the existing comment in `keyboard.ts` explains why the `ctrl-enter` send binding excludes Shift).
3. The chord works in both editors: the textarea keydown and Lexical's `onOptimizeChord` path, which today receives the chord from the same prop. The chord's handler becomes the single place that reads the setting.
4. `shortcutRegistry.ts` keeps the id `optimize-prompt` so a person's rebound chord is not lost, and a second label for the "on" meaning appears in the shortcut reference. The sparkle button's tooltip names the chord only while the chord optimizes; while the setting is on, the Send button's tooltip and the shortcut reference carry the chord's meaning.
5. A visible equivalent for touch and mouse users is a "Send as typed" row in the plus menu, shown while the setting is on and the draft is non-empty, because a chord alone is not reachable on a phone (open question 3).

### 2. Skip by default when the composer knows

A draft is marked reviewed, as if an optimize had already run, so the next Send sends it as typed, in these cases:

| Case | Fact the composer uses | Today |
|---|---|---|
| Filled from the agent's suggested follow-up (chip, or Tab on the ghost text) | the fill path in `chat-input/followUpGhost.ts` and `onFollowUpSelect` already know the source | optimized (#39145 point 1) |
| Nothing left to optimize: only paste blocks, protected spans (fenced code, block quotes) and whitespace | the placeholders and the protected spans of section 3 found in the draft | optimized, for no gain: the blocks and spans are preserved, so no text is left that could change |
| A draft the person sent once and recalled with the up arrow | the prompt-history recall path | optimized again |

There is no length threshold. Any other text outside a placeholder or a protected span means the draft is optimized as today, so a draft with one line of prose beside a paste is still optimized. A fence around a paste, which a person may add to format it, counts as having nothing left. Editing a skipped draft follows the existing rule: it stays reviewed until the composer is emptied.

This change reads only state the composer already holds. It adds no endpoint and no setting.

### 3. Fenced code and block quotes are never rewritten

Which text. A fenced code block is a run opened by a line made of whitespace of any depth, at least three backticks or tildes, and an optional info string (a language tag), and closed by a later line made of whitespace and at least as many of the same character, with nothing after them. A backtick fence's info string may not contain a backtick, so three backticks around code on a single line are an inline span and not a fence. An unclosed fence runs to the end of the draft, as in CommonMark. The scan errs toward protecting, because the two mistakes are not equal: a span protected by mistake is only masked and comes back unchanged, while a span missed is rewritten without a sign. So the indent is not limited, a fence inside a list item or a quote is protected, and a line indented four or more spaces that opens a fence is protected as a fence even though CommonMark would call it indented code. What this version leaves out on purpose is indented (four-space) code with no fence. A block quote is a run of one or more consecutive lines that each begin with `>` (up to three spaces of indent, then `>`, then usually one space and the text). A line that is only `>` or `> ` is part of the run, which is how a multi-paragraph quote is written, and a line without `>` ends it. The composer's own Quote action produces exactly this shape (`quoteIntoDraft` in `chat-core/composer/quoteDraft.ts` prefixes every line, blank ones included, with `> `, and separates stacked quotes with a blank line), so a one-line quote must count. Both are read from the draft text, which is the one place this RFC allows reading text, because the person has marked the span themselves with syntax whose meaning is fixed. A span inside a paste block is out of scope: the optimizer never sees that text.

How it is enforced. Mask first, with discard as the fallback, reusing the machinery paste blocks already have:

1. **Mask.** Before the request, the client replaces each fenced block and block quote in the prompt with a synthetic placeholder in the paste-token format and sends the span's text in `pastes`, so the model reads the content as context and has nothing to edit. After the response the client puts each span back. The synthetic numbers must not collide with the draft's real paste numbers (`nextSeq` in `utils/pasteTokens.ts`).
2. **Discard as the fallback.** The server's existing check (`_paste_token_counts`) already rejects a rewrite that drops, duplicates or alters a placeholder, and returns the original with `changed: false`. Masked spans therefore get that protection with no new server scheme: if the model damages a placeholder, the rewrite is discarded.
3. **Prompt.** The optimizer prompt says to keep fenced code blocks and block quotes character for character, which lowers the rate of discarded rewrites.

The mask path has not been prototyped. Phase 3 starts with a prototype; if it does not hold up (for example the content budget `_PASTE_CONTENT_BUDGET` cuts the context a prompt needs), the fallback is discard-only, with the server extracting the same spans from the draft and the rewrite and requiring them to match.

It applies to every optimize, so the sparkle button, the chord with the setting off, and Optimize Before Sending behave the same. This changes the optimizer's contract (a draft with a fence now has a stricter acceptance rule), which is why it is in an RFC and not a single PR.

Cost. With masking, edits to the prose around a span are kept. With discard alone, a rewrite that touches a span is thrown away and looks the same as "already clear", so a person may see no change on a draft with a long quote around a rambling sentence. That is why masking is tried first.

A separate issue, [#42836](https://github.com/kirodotdev/KiroCrew/issues/42836), covers styling fenced blocks and block quotes in the composer under Style Markdown While Typing (the follow-up #14734 left out of scope). It is not part of this RFC and does not depend on it.

### 4. Say it in the setting

The Optimize Before Sending description states: a draft is optimized once; a slash command, a suggested follow-up, a paste-only draft and a send-as-typed send are not optimized; fenced code and block quotes are never rewritten; and what the chord does while the setting is on. The Optimize prompt tooltip says the same about fences and quotes. No new setting is added.

## Migration plan

- **Phase 0 — measure.** With a throwaway local counter (not committed) and a day of real use with Optimize Before Sending on, count how often a Send was intercepted for each case in section 2, and how often a draft containing text outside a block was one the person then cancelled with Escape. Also run the optimizer on a handful of drafts that contain a fenced block or a block quote and record whether it ever changes them. Exit criteria: the counts and that result are written in this RFC. If cancelled-by-Escape sends are rare, section 2 shrinks to the suggested-follow-up case only.
- **Phase 1 — send as typed.** Section 1 and the description change. Exit criteria: with the setting on, the chord sends the draft unchanged and starts no optimize request in the textarea and in Lexical; with the setting off the chord optimizes as before; a host with `promptOptimizer` off sends nothing on the chord. Independently shippable.
- **Phase 2 — skip by default.** Section 2, one case per PR. Exit criteria for each case: a Send of that draft starts no optimize request, and an ordinary typed draft still does. Blocked on Phase 0 for the paste-only and recall cases.
- **Phase 3 — fences and quotes.** The prompt line, the server check and tests. Exit criteria: for a draft containing a fenced block and a block quote, a rewrite that changes either is discarded and returns the original with `changed: false`; a rewrite that leaves both intact is accepted; an unclosed fence is treated as running to the end; a fence-shaped line inside a paste block is not scanned. Break the check and confirm its dedicated test turns red. Starts with a prototype of masking and falls back to discard if the prototype does not hold up. Exit criteria for the mask path also include: a masked span comes back byte-identical, and the prose around it is still rewritten. Phase 0 should also record whether the unprotected optimizer ever alters a fence or quote.
- **Phase 4 — optimize and send (separate opt-in setting).** A new setting next to Optimize Before Sending, off by default (working name: Send after optimizing), which changes what the optimize that Enter starts does next: with it on, the rewrite is sent without a second press. The chord is unchanged: setting on, it sends as typed, so it is also the way to skip the optimize for one send. Exit criteria: with the new setting off, nothing changes; with it on, one Enter ends in one send; an optimizer failure or a timeout sends nothing and leaves the draft in the composer with the error shown, as Optimize Before Sending does today; a rewrite the server discards sends the original; Escape cancels and sends nothing. Blocked on a maintainer's agreement to open question 1, and on the choice to keep it in this RFC or move it to its own.
- Each phase can be abandoned alone. Phase 1 does not depend on Phase 2, 3 or 4.

## Backward compatibility

- Optimize Before Sending is off by default, so most installs see no change.
- For a person with it on, the chord changes meaning from "optimize and keep for review" to "send as typed". This is a changed shortcut, not a removed capability: the sparkle button and plus-menu row keep Optimize prompt. The change is announced in the setting's description and in the release notes. A rebound chord keeps its binding.
- Phase 2 changes which sends are intercepted, only in the direction of fewer optimizes. Nothing that was sent before is blocked.
- Phase 4 adds a setting that is off by default, so it changes nothing for anyone who does not turn it on.
- Phase 3 makes the optimizer return fewer changes, never more: a rewrite that touched a fence or a quote used to be accepted and is now discarded. No stored data or config key changes.

## Security considerations

No change to what is sent to the optimizer or the gateway. Send as typed sends text the person already had in the composer, through the same send path. Skipping an optimize sends less to the optimizer, not more.

## Alternatives considered

- **A new, separate chord for send as typed.** No meaning changes with a setting, so it is easier to explain. Costs a second chord from a crowded set (Cmd/Ctrl+Enter is a send in some modes and the busy-state alternate in others) and leaves Cmd/Ctrl+Shift+Enter doing the least useful thing while the setting is on.
- **A "send as typed" entry only in the send button's menu.** Reachable on touch, but not a faster path than the Escape workaround for a keyboard user.
- **Detect pasted logs and code by their text.** Misfires on prose that quotes a command and on code that looks like prose. Rejected; the composer already knows what was pasted.
- **Only tell the model to keep fences and quotes, and check nothing.** Cheapest, but it is the weakness the paste and mention guards were added to remove: the model is not the enforcement point.
- **Skip the optimize for any draft that contains a fence or quote.** Simple, but it gives up optimizing the prose around them. Section 3 masks the spans instead.
- **Turn Optimize Before Sending off.** Loses optimization for the prompts the person does want optimized.

## Open questions

The answers marked "proposed" come from tmack8001 (2026-11-25) and still need a maintainer's agreement.

1. **Optimize and send, as a separate setting** (#39145 point 2; #23665's "send automatically" mode). Proposed: a new opt-in setting, off by default, apart from Optimize Before Sending, and not on the chord. Proposed failure rule: stop, keep the draft, show the error; this is what Optimize Before Sending already does, and it never sends an unreviewed prompt. A rewrite the server discards counts as success with no change and sends the original. Still open: whether it belongs in this RFC or in its own, and its name and placement in Settings > Chat > Composer.
2. **Short note around paste blocks.** Proposed: no threshold. Skip only when nothing but placeholders, protected spans and whitespace remain (section 2). Revisit with Phase 0 counts.
3. **A visible send-as-typed control** for touch and mouse. Proposed: a plus-menu row (section 1, rule 5). Open concern: the plus menu holds no send actions today, so the row may look out of place or busy; its placement should be settled with a mockup. The send button's menu stays the alternative.
4. **Show full pastes.** Settled as proposed: with it on, no paste becomes a block, so the paste-only case does not apply, and the composer does not track "was pasted". Fences and the chord cover those drafts.
5. **Tooltips.** Settled as proposed: the sparkle tooltip stops naming the chord while it means send as typed, and the Send button's tooltip and the shortcut reference name it.
6. **Mask or discard** for fences and quotes. Proposed: mask first, discard as the fallback (section 3).
7. **Block quote definition.** Proposed in section 3: any run of lines that start with `>`, one line is enough. Still open: CommonMark lets a line without `>` continue a quote's paragraph, and the proposal does not, so that line is outside the protected span. A line-start `>` that is really a shell prompt is protected needlessly, which costs a discarded rewrite and never a changed one.
8. **Fence shapes.** Proposed, and deliberately over-protecting (section 3): three or more backticks or tildes at any indent, closed by the same character at equal or greater length, an unclosed fence runs to the end, an inline triple-backtick span is not a fence, and indented code with no fence is left out for now. Still open: whether a later version should also protect indented code blocks.
