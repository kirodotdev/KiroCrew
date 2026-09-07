---
title: Tool-Derived Diff Cards — structured diffs as the primary file-change display
status: in-progress
author: zezhexu
created: 2026-08-21
last-audited: 2026-08-21
audited-at: 8c61bc1f0
doc-pr:
implementation-prs: ["#5012"]
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Tool-Derived Diff Cards — structured diffs as the primary file-change display

- Status: in-progress — the dashboard promotion and the runtime-selected
  prompt rule ship with [#5012](https://github.com/kirodotdev/KiroCrew/pull/5012);
  the messaging `OutputEvent` extension (§3.3) is unstarted.
- Author: zezhexu
- Created: 2026-08-21
- Related: `docs/system-specs/modules/memory-skills-hooks.md` (session context
  assembly), `docs/system-specs/modules/acp-client.md` (tool-call event
  parsing), `docs/system-specs/modules/messaging.md` (channel renderers)

## 1. Problem statement

Every file change an agent makes is displayed to the dashboard user twice,
through two channels of very different reliability:

1. **A structured channel that already exists.** kiro-cli's edit tools emit a
   `type: "diff"` content block (`{path, oldText, newText}`) on the ACP
   `tool_call` event. `acp/_dispatch.py` converts it to a unified diff string
   (`make_unified_diff`, `_dispatch.py:304`) and attaches it to the tool-call
   event as `tool_input`. The frontend renders it — but only inside the
   tool-call row's expanded details panel, which is collapsed by default
   (`ToolCallLine.tsx:174`), and turns with more than two working steps fold
   the row further into a "Worked through N steps" group (`TurnBlock.tsx`).
2. **A prompt-mandated prose channel.** The agent prompt (`config/prompt.md:5`,
   `config/prompt-orchestrator.md:5`) and the `_CRITICAL_RULES` block
   (`context.py:963`) order the model to hand-write a ```` ```diff ```` code
   block after ANY file change, "No exceptions". The frontend then re-parses
   that prose: the block assembler classifies it (`useBlockAssembler.ts:103`),
   `DiffBlock.tsx` regex-extracts the file path from `+++`/`---` headers
   (`extractFilePath`, line 72), and when the model omits headers a heuristic
   scrapes "Created /path"-style lines out of the preceding prose
   (`MarkdownRenderer.tsx` `extractPathHintFromText`).

The prose channel is the *prominent* one, and it is the weaker one:

- **It costs tokens on every edit.** The model re-states every change it just
  made through a tool, doubling the output for edit-heavy turns.
- **It constrains output flexibility.** The "No exceptions" mandate is why the
  rule is injected twice (prompt + critical rules) — models drift out of
  format compliance in long sessions, and the renderer's path extraction
  degrades to heuristics when they do.
- **It is display-only sugar.** No backend consumer parses model-authored diff
  fences (verified: `voice_reply.py:159` and `preview_text.py:5` only *strip*
  them; history compression, artifacts, and session summaries never extract
  them). The Activity panel's Files tab reads `GET /api/file-diff` (live
  `git diff HEAD`), and file-change chips read the `file_changes` snapshot
  meta — both independent of message text.

DeepSeek Harness (studied at `packages/core/tools/src/presentation.ts`,
`packages/fs/tool-fs/src/diff.ts`, `packages/client/ui-tool/.../diff-card-model.ts`
in the DSH repo) demonstrates the inverted architecture: tools declare a tagged
render intent (`card: 'diff'`), the backend computes hunks from before/after
text, the structured payload persists with the session log, and the model
never writes a diff in prose. Kiro Crew already receives the equivalent
structured data; it only fails to give it first-class display.

## 2. Constraint discovered during investigation: messaging surfaces

Slack, Discord, and Telegram renderers never see `tool_input`. The unified
transport copies only `{tool_call_id, title, tool_kind, tool_purpose}` onto
`OutputEvent` (`messaging/driver.py:373-386`); the Slack native path reads the
same four fields (`slack/handler.py:3235-3327`); Discord/Telegram show only a
transient "🔧 tool…" footer on the live bubble. **On those surfaces the
model-authored diff block is the ONLY way a user sees what changed.**

Extending `OutputEvent` with `tool_input` and teaching three channel renderers
to render diffs is a real cross-channel project (rate limits, message-length
budgets, mobile formatting) and is explicitly OUT of scope here. Instead, the
prompt rule becomes **runtime-conditional** rather than deleted.

## 3. Design

### 3.1 Frontend: promote tool-derived diffs to first-class cards (dashboard)

A tool-call row whose resolved input is a unified diff (gated on
`tool_kind === 'edit'` AND `isDiffText(input)`, so a `bash` command containing
diff-shaped text never promotes) renders a first-class diff presentation:

- New pure helper `website/src/pages/chat/toolDiff.ts` classifies the row:
  diffs at or under a line cap render the full `DiffBlock` card; over-cap
  diffs (whole-file creates, large refactors) degrade to a summary chip —
  filename, −N/+M counts, click expands the details panel — never to nothing,
  because the relaxed prompt means no model-authored fallback exists.
- `FileChangeChips` follows the same mount discipline for the independent
  assistant-message snapshot channel. Closed rows render a lightweight header
  with the filename, counts, diffstat, artifact badge, and file-open action but
  do not mount Pierre. Opening a row mounts Pierre until its collapse animation
  completes.
- The presentation renders INSIDE `ToolCallLine`, below the pill (the same
  anchoring the MCP-app iframe uses). One render site covers every transcript
  surface — `ChatPage`, split-pane `ChatPane`, and app-sdk `ChatMessageList`
  via `transcriptRenderers` — instead of per-surface promotion branches.
- `TurnBlock.tsx`: an `isDiffCardItem` predicate keeps the row out of BOTH
  folds (the default "N tool calls" group and collapseAll) — a file change is
  a result, not a working step, the same class as the prose diff the final
  summary used to carry. Density relief is per-card: every open card has a
  chip handle that folds it to one line and unfolds it again; over-cap diffs
  never render as cards at all.
- Companion behaviors shipped with the promotion: rejected / auto-denied
  edits never promote (the change was not applied — a first-class card would
  read as if it shipped); an edit row whose transport attached no
  recognizable diff keeps its fold-proof trace via `FileChangeChips` (the
  `file_changes` snapshot channel); `derive_edit_diff` converts bare-JSON
  `strReplace` / `create` / line-numbered `insert` args into a unified diff
  server-side (in all three the rendered lines ARE the change, exactly; an
  insert without a line number derives nothing rather than guessing the hunk
  position); the transport payload
  cap in `make_unified_diff` is RAISED from 6 000 chars to 64 KiB (sized so
  the full-card range is never cut) with line-boundary truncation and a
  machine-detectable `\ diff truncated` annotation, surfaced on the chip as
  a localized note with `≥`-prefixed lower-bound counts; and a session that
  starts on the dashboard but continues from a channel receives a per-turn
  re-assertion of the hard mandate on the same trusted path that refreshes
  `[RUNTIME]` (asymmetric: the dashboard direction injects nothing).
- `DiffBlock` is reused unchanged: it already parses the backend's
  `--- <path>` / `+++ <path>` headers (plain format, same path both sides),
  shows the filename, and offers Open-in-side-panel via a typed path — no
  regex fallback, no `pathHint` scraping.
- The row's diff comes from the live toolLog entry when one backs the row,
  else from persisted `meta.input`; both carry the same
  `_redact_tool_field(event.tool_input)` value, so live and historical
  renderings are identical.

### 3.2 Prompt: unconditional mandate → runtime-SELECTED rule

The bundled prompts (`config/prompt.md`, `config/prompt-orchestrator.md`)
describe the conditional contract in prose. The authoritative per-session rule
lives in the critical-rules block and is selected SERVER-SIDE
(`_critical_rules_for` in `context.py`): the trusted runtime resolution that
already produces the `[RUNTIME]` line picks between two fixed module
constants —

- **Dashboard** (`_CRITICAL_RULES`): tool edits render as diff cards; emit a
  ```diff block only for changes made outside file-editing tools (shell
  `sed`, scripted bulk edits, `git apply`).
- **Every other surface** (`_CRITICAL_RULES_CHANNEL` — messaging channels,
  cron, subagent, CLI): the hard mandate, unchanged from the original rule,
  because no tool cards render there and message text is the only display.

Only the tool-vs-shell distinction stays with the model: the runtime cannot
observe HOW a file was changed. Both variants stay fixed module constants
(never templated), so the marker-neutralization fast path in `build_message`
checks two known prefixes instead of one.

The bundled prompts default to the UNCONDITIONAL mandate and treat the
injected critical rules as the relaxation, not the other way round: a session
that receives no critical-rules block at all (minimal-context cron runs skip
session-context injection entirely) falls back to the hard mandate rather
than to silence, and its output goes to surfaces where message text is the
only display.

The ultra-verbosity carve-out ("diff blocks for file changes" in the
verbosity block) is unchanged: diff blocks remain a required format *when the
selected rule requires them*, and `test_verbosity_config.py` keeps its
exact-string pin.

### 3.3 Explicitly out of scope

- Extending `messaging/renderer.py`'s `OutputEvent` with `tool_input` and
  rendering diffs on Slack/Discord/Telegram (future work; would allow
  retiring the conditional prompt rule entirely).
- Structured `FileDiff[]` hunk metadata on tool results (DSH's exact shape).
  The unified-diff string in `tool_input` is sufficient for display; a typed
  hunk payload is a wire-format change with no additional consumer today.
- Deduplicating a model that still emits a diff block alongside the tool card
  during the transition. Double display is cosmetic and self-corrects as the
  prompt propagates to new sessions.

## 4. What does NOT change

- `make_unified_diff` and the ACP diff-content-block parsing
  (`_dispatch.py:669-692`) — already correct.
- The `/api/file-diff` Activity Files tab — an independent channel, untouched.
- `DiffBlock`, `diffUtils`, `useBlockAssembler` — the model can still emit
  diff blocks (fallback cases) and they render exactly as today.
- Backend event shapes and history persistence.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Older installs with a user prompt override (`~/.kiro/crew/prompt.md`) keep the old mandate | Harmless: double display, not data loss. The critical-rules block is code and updates everywhere immediately. |
| A model misjudges the tool-vs-shell clause on the dashboard | Runtime selection is server-side; only the tool-vs-shell distinction rides on model judgment, and its failure mode is a duplicate diff, not a missing one. Channel surfaces keep the unconditional mandate. |
| `tool_kind` mislabeled by a provider → missed promotion | Fail-safe: the row still renders as today's collapsible ToolCallLine; nothing is lost. |
| A giant diff dominates the transcript | Over-cap diffs render the summary chip (filename, −N/+M, expands details) instead of the full card. |
| The transport bounds `tool_input`: this PR raises `make_unified_diff`'s cap from 6 000 chars to 64 KiB | The cap is sized so the full-card range is never cut; a longer diff is truncated at a line boundary and carries a `\ diff truncated` annotation (the unified-diff escape convention — renderers skip it), so a summary chip's counts can understate only past 64 KiB and the cut is machine-detectable, never silent. |

## 6. Test plan

- Frontend: unit tests for the presentation helper (edit-kind + diff-text
  gating, bash false-positive rejection, over-cap summary degradation);
  TurnBlock predicate tests (diff row out of the default tool fold, folded
  under collapseAll, execute-kind never promotes); render tests for the card,
  the summary chip, and the historical-meta path. `FileChangeChips` mount-count
  tests prove closed rows request no Pierre surface, direct disclosure mounts
  one, and collapse unmounts it after the closing animation.
- Backend: `test_tool_meta_persists_kind` (meta carries the ACP kind);
  `test_diff_rule_is_runtime_selected` (dashboard gets the don't-repeat rule,
  channel/cron/CLI keep the mandate, explicit `runtime_source` wins over the
  key-derived guess); verbosity carve-out test unchanged.
- Manual: screenshot evidence of a dashboard edit turn (diff card, no prose
  diff, visible under the collapsed turn) in light and dark themes.
