# Workflow Chat Cards

## Problem

A dynamic workflow launched from chat (`workflow_run`) surfaces poorly at both
ends of its lifecycle:

- **Launch:** the `workflow_run` tool call renders as a generic tool pill and is
  folded into the collapsed "N tool calls" group — no persistent, scannable,
  clickable anchor to the run's live state.
- **Completion:** the injected completion event (see
  `dashboard/workflow_inject.py`) dumps the entire result JSON inline as a wall
  of text.

## Goal

Give both lifecycle ends a persistent, clickable inline card in the chat flow
that shows run state and opens the Workflows side panel — **render-only**, with
no backend or persistence changes and no change to the agent-facing message
content.

## Design

Frontend-only. Both cards are special-cased in `ChatPage.tsx` `renderMessage`
and exempted from folding in `TurnBlock.tsx`; neither adds Redux/backend state.

### Components

| Component | Renders for | Live source |
|---|---|---|
| `website/src/pages/chat/WorkflowRunCard.tsx` | a `workflow_run` tool message | `chat.workflowRuns[run_id]` (folded from `workflow_run_event` WS frames) — status / phase / last-log |
| `website/src/pages/chat/WorkflowCompletionCard.tsx` | the injected `[Workflow completion event]` assistant message | header parsed from content; full result folded behind a "Show result" toggle |

Both open the Workflows side panel on click via
`dispatch(openActivityToTab('workflows'))` (the same Redux path `/side` and
approvals use, bridged into the `usePanelTabs` tab model by ChatPage). Header
fields pass through `sanitizeLlmOutput`; the completion body renders through
`MarkdownRenderer` (sanitized rehype pipeline).

### Detection contract (content-based)

Detection keys off message **content**, not the WS `kind` — the injected
completion is persisted as a plain `assistant` message and the `kind` is dropped
on persist, so content-matching is what survives a history reload. This creates
a cross-layer contract with two backend strings:

| Frontend matcher | Backend source | Pinned by |
|---|---|---|
| `WF_RUN_ID_RE` = `` /Started workflow run `(wf_[A-Za-z0-9_]+)`/ `` | `mcp_core.py` `workflow_run` handler (both `source` and `intent` paths) | frontend unit test |
| `WF_COMPLETION_RE` = ``/^\[Workflow completion event\]\nWorkflow `([^`]+)` \((wf_…)\) → \*\*([a-z]+)\*\*/`` | `workflow_inject.py` `_summarize` header | `test/test_workflows_inject.py::test_summary_header_format_is_pinned_for_frontend` |

**Invariant — graceful degradation, never data loss.** Detection must be gated
on a *successful parse*, not a loose prefix. `isWorkflowCompletionMessage`
returns true only when `parseWorkflowCompletion` succeeds; otherwise the message
falls through to normal markdown rendering (the pre-feature behavior). This
prevents a card that renders `null` from swallowing a completion whose header
doesn't parse (e.g. an unusual workflow name). The launch card likewise falls
through to the generic `ToolCallLine` on a regex miss.

Known follow-ups (tracked, not in this feature): (1) persist a structured
marker (`meta.workflow_run_id` / a completion `kind`) so the frontend keys off
data instead of prose, removing both regexes; (2) carry the `run_id` through
panel-open so the panel focuses that specific run (mirroring
`openActivityToTool`'s `focusToolCallId`).

### TurnBlock fold exemption

`TurnBlock.tsx` treats both cards as always-visible inline items (via
`isWorkflowRunItem` / `isWorkflowCompletionItem`, added to `isVisibleInline`),
so they are never folded into the collapsible tool-call / reasoning panes in
either collapse mode, and the launch card does not inflate the "N tool calls"
count.

## Tests

- `website/src/test/WorkflowRunCard.test.tsx` — detection helpers, live status
  render, intent fallback, click-opens-panel.
- `website/src/test/WorkflowCompletionCard.test.tsx` — parse/detection incl. the
  parse-gated fallback (prefix-but-unparseable is not detected) and newline-in-
  name tolerance, compact render, collapsed-by-default, expand toggle,
  click-opens-panel.
- `test/test_workflows_inject.py::test_summary_header_format_is_pinned_for_frontend`
  — pins the completion-header format the frontend depends on.
