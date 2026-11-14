---
title: Reference a saved artifact from the composer "+" menu
status: draft
author: rinbn, with kirocrew
created: 2026-11-12
last-audited: 2026-11-13
audited-at: ba3a1caec3
doc-pr: null
implementation-prs: []
tracking-issues: [18390]
supersedes: []
superseded-by: []
---

# RFC: Reference a Saved Artifact from the Composer "+" Menu

- Status: `draft`. Acceptance is requested from a maintainer; the status flips to `accepted` when one records it on this line with the date. Nothing is on main. This document lands first because the change adds one item to the fixed order in which a composer submit builds a message, and that order is a contract other code depends on.
- Tracking issue: [#18390](https://github.com/kirodotdev/KiroCrew/issues/18390), item 2 only ("Artifact" in the session "+" menu). Item 1 ("Organize suggestions" on the Artifacts page) is not covered.
- Related: [#36115](https://github.com/kirodotdev/KiroCrew/issues/36115) is the opposite direction (the agent shows a saved artifact in its reply). [#26511](https://github.com/kirodotdev/KiroCrew/issues/26511) proposes access levels for shared artifacts. Neither is decided here.

Every claim about current behavior was verified at `7a7f070afc` (main, 2026-11-12) and re-read at `ba3a1caec3` on 2026-11-13, where none of the cited code had changed. Code is cited by symbol name.

## 1. Summary

A person in a chat session opens the composer "+" menu, chooses "Reference an artifact", and picks a saved artifact from a list. The composer shows the pick as a chip above the text box. On send, the message ends with a markdown link to the artifact, so the transcript shows the artifact's name as a link and the agent receives the slug it needs to load the content.

This is the same mechanism a session dragged into the composer already uses. The proposal adds a second kind of thing that mechanism can reference, and a menu entry to reach it.

## 2. Motivation

### 2.1 Current state

1. **Artifact to session exists; session to artifact does not.** The artifact detail page offers More → Send to a session (`ArtifactSendToSessionSubmenu` in `website/src/components/ArtifactSendToSession.tsx`). It lists up to 10 live sessions (`MAX_LISTED_SESSIONS`) and appends one sentence to the target session's stored draft (`handOff` in `useArtifactSendToSession`). Nothing in the composer lists artifacts.
2. **The sentence is written for the agent, and the person sees it.** The text is `Reference artifact "<name>" (slug \`<slug>\`; load it with artifact_get).` (`artifactReferencePrompt` in `website/src/components/artifactReference.prompt.ts`). It sits in the text box before the send and in the user's message afterwards.
3. **A widget's slug is not readable.** A widget the agent emits registers as an artifact with a generated slug such as `801b9d2ce619433c`. The sentence in point 2 shows that string to the person.
4. **The composer already has a pointer mechanism.** A session dragged from the sidebar is staged as a chip above the text box (`website/src/components/SessionRefStrip.tsx`), kept per session in the draft store (`sessionRefDrafts` in `website/src/pages/chat/page/composerDrafts.ts`), capped at 8 (`MAX_SESSION_REFS` in `website/src/utils/sessionRefs.ts`), and sent as a markdown link appended to the message (`formatSessionRefLink` and `appendSessionRefLinks`, called from `buildOutgoingTurn` in `website/src/chat-core/composer/outgoingTurn.ts`). The link carries no transcript; the agent follows it on demand.
5. **The "+" menu already opens composer pickers.** Its third group has three rows, Command, Reference a file, and Use a skill (`AttachMenu` in `website/src/components/chat-input/attach.tsx`). Each closes the "+" menu and opens a list anchored above the composer (`openTrigger` in `usePlusMenu`), at most 320 px tall (`MENU_MAX_HEIGHT` in `website/src/lib/pickerMenu.ts`).
6. **The transcript already understands artifact links.** The message renderer extracts the slug from an `/artifacts/<slug>` link, with or without an origin (`artifactSlugFromHref` in `website/src/components/markdown/linkTargets.ts`), and opens it in the app.

### 2.2 Problems

1. A person who is already in a session must leave it, find the artifact, and send it back to reach a saved artifact. Issue #18390 reports this.
2. The only existing path puts an agent instruction and a generated slug in front of the person.

## 3. Goals

1. From a chat session, a person references a saved artifact without leaving the session.
2. The person sees the artifact's name before and after the send, and never the slug or an agent instruction.
3. The agent receives a pointer, not the content, and can load the content from it.
4. A message with no artifact reference is byte-identical to today's.

## 4. Non-goals

1. A reference card that shows type, access level, and version, or pins a historical version (the 2026-10-31 comment on #18390). Access levels depend on #26511.
2. Copying artifact content into the message.
3. A typed trigger character for artifacts in the text box.
4. Any change to the artifact store, its API, or the agent's artifact tools.
5. "Organize suggestions" (item 1 of #18390).

## 5. Design

### 5.1 What the person sees

| Step | Behavior |
|---|---|
| Open "+" | The third group gains one row, "Reference an artifact", directly below "Reference a file". |
| Choose the row | The "+" menu closes. A list opens above the composer, in the position and size the file and skill pickers use. |
| Read the list | Pinned artifacts first, then the rest by most recent change. Each row shows the name and the time of the last change. The slug is not shown. |
| Filter | A text field in the list filters by name and by slug. Arrow keys move the selection, Enter picks, Escape closes. |
| Pick | The list closes. A chip with the artifact's name appears above the text box, in the strip that holds staged session references. The text box is unchanged. |
| Remove | The chip's remove button unstages it. |
| Send | The message ends with a link whose label is the artifact's name. The chip strip clears. |
| Read the transcript | The user's message shows the name as a link. Clicking it opens the artifact. |

The list reads `GET /api/artifacts`, which already takes a text query and a pinned filter (the HTTP API table in `docs/system-specs/modules/artifacts.md`, `artifacts` in `website/src/api/client/artifacts.ts`). Each artifact in the response carries `pinned` and `updated_at` (the `Artifact` type in `website/src/types/index.artifacts.ts`), which are the two sort keys.

### 5.2 What is sent

The link is `[<name>](<dashboard origin>/artifacts/<slug>)`, one per staged artifact, each on its own line.

1. **Label.** The name passes through the same reduction a session title does (`sanitizeRefTitle`), which removes the characters that let a label close early or carry a second link. An empty result falls back to the slug.
2. **Position.** Artifact links follow the session reference links, joined the same way. This is step 3 of the order documented at the top of `outgoingTurn.ts`. Appending leaves earlier paste-token ranges untouched, which is the reason session links append.
3. **Both channels.** The link is plain text, so it rides a mid-turn steer as session links do.
4. **Staging rules.** Staged artifacts follow the staged-session rules: a duplicate pick is ignored, the cap is 8 per message, the set is saved with the session's draft, a session switch restores it, and a failed send puts it back.

### 5.3 The agent can load an artifact from the link

This was tested on 2026-11-12. Two new agent sessions with no memory, lessons, or project context each received one message: a question, a blank line, and a link to an existing artifact. One link used a `http://localhost` origin and the other an `https` origin. Neither message named a tool.

1. Both agents answered with details that appear only in the artifact's content.
2. The artifact's activity log gained one `referenced` event (`ALLOWED_EVENT_TYPES` in `src/kiro_crew/artifact_store/records.py`) attributed to each of the two sessions. The test did not capture which tool call each agent made.

The link therefore needs no accompanying instruction for the default agent. Section 10 lists what the test did not cover.

### 5.4 Where the entry is not offered

1. **Split mode and a composer with no active session.** Session references are refused there today (`canStageSessionRef` in `website/src/pages/chat/page/composerStaging.ts`), and artifact references follow.
2. **Touch layouts where "+" is a bare file input.** The menu does not mount there (`directFilePicker` in `AttachMenu`), so its rows, including Sketch, are absent. This RFC accepts the same gap.
3. **App embeds.** The embed composer has no artifact list to read from. A picker whose read the host does not carry is not mounted, which is the existing rule for composer pickers (`useComposerPickers` in `website/src/chat-core/composer/pickers.ts`).

## 6. Migration plan

### Phase 1: reference an artifact from the dashboard composer

Sections 5.1, 5.2, and 5.4.

Exit criteria:

1. Picking an artifact stages one chip and leaves the text box unchanged.
2. Sending produces a message whose last line is the link in section 5.2, on both the send and the steer channel.
3. A message sent with nothing staged is byte-identical to one built at `7a7f070afc`.
4. A name containing `]`, `\`, `<`, or `>` yields exactly one link, pointing at the artifact.
5. A staged artifact survives a session switch and a failed send.
6. A ninth pick on one message is ignored.

### Phase 2: Send to a session stages the same chip

Send to a session stages an artifact reference in the target session and stops appending the sentence. Both directions then show the person the same thing.

Exit criteria:

1. After Send to a session, the target composer shows a chip and its text is unchanged.
2. The sentence in section 2.1 point 2 no longer reaches a draft.

Phase 2 changes what an existing action does, so it is blocked on a maintainer's answer to open question 1. Phase 1 ships and stands without it.

## 7. Backward compatibility

1. Phase 1 adds a menu row and a kind of staged reference. No stored format changes. A stored draft without staged artifacts reads as before.
2. Messages already in transcripts are unaffected. A message sent with this feature is ordinary markdown and renders in a build that predates it.
3. Reverting Phase 1 removes the row. Sent messages keep their links, which the renderer already handles.

## 8. Security considerations

1. **Link label.** An artifact name is settable by an agent. Section 5.2 point 1 reuses the reduction that already guards session titles, and exit criterion 4 tests it.
2. **Link target.** The slug is the only variable part of the URL. It is percent-encoded when the link is built.
3. **No content crosses the composer.** The message carries a pointer. The agent reads the content through the artifact store, so the checks on that read apply unchanged, and the message cannot spend the session's context on a large artifact.
4. **No new read.** The list shows what the Artifacts page already shows the same person.

## 9. Alternatives considered

1. **Insert the existing sentence into the text box.** It needs no change to the send path. It is rejected because it shows the person an agent instruction and a generated slug (section 2.1, points 2 and 3).
2. **A typed trigger character.** It is faster for keyboard users. It changes the meaning of a character in every draft, which is a larger contract than this RFC needs, and it can be added later on top of the same staging.
3. **A chip in the transcript backed by a record on the message.** Collapsed pastes work this way. A link gives the same result in the transcript with no record, because the renderer already handles artifact links.
4. **The reference card from the #18390 comment.** It depends on access levels that do not exist (#26511). A link can become a card later without changing what is sent.
5. **A submenu of recent artifacts with no filter**, mirroring Send to a session. Widgets register as artifacts automatically, so the list is long, and a person cannot reach an older artifact without a filter.

## 10. Open questions

1. **Does Phase 2 belong here?** It replaces the visible result of an existing action. A maintainer decides whether that is in scope or a separate RFC.
2. **A filter field inside a composer picker is new.** The existing pickers filter on text typed after a trigger character. If maintainers prefer the trigger character, alternative 2 replaces the field and section 4 point 3 moves into scope.
3. **Agents other than the default.** The test in section 5.3 used the default dashboard agent. Agents on other backends, and agents without the artifact tools, were not tested. If one cannot resolve the link, the fix is a line in that agent's instructions; what is sent stays the same.
4. **Private sessions.** A session in an incognito or temporary memory mode cannot be the source of a session reference (`PRIVATE_MEMORY_MODES`). Whether such a session may reference an artifact is not decided here.
5. **One cap or two.** Section 5.2 point 4 gives artifacts their own cap of 8. A shared cap across sessions and artifacts is the alternative.
