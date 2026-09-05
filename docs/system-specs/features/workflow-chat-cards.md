# Workflow Chat Cards

## Behavior

Workflow launches and completions render as durable inline cards in chat. `useChatPageTranscriptController.tsx` renders them for the primary chat surface, and `transcriptRenderers.tsx::createTranscriptRenderers` registers the same cards for SDK transcript hosts. `TurnBlock.tsx` keeps those items outside collapsed tool and reasoning groups so a launch or completion remains available in either collapse mode.

### Launch card

`website/src/pages/chat/WorkflowRunCard.tsx` renders a launch card only for a `tool` message whose persisted `meta.output` yields a run ID through `extractWorkflowRunId`; `isWorkflowRunTool` enforces the role check. The output contract originates in `src/kiro_crew/mcp_tools/workflows.py::workflow_run`, which returns a successful-launch message for definition, intent, and source launches.

The card reads the live entry from `chat.workflowRuns`. `website/src/hooks/useWebSocket.ts` folds workflow event frames into that slice and reconciles it with the workflow-runs API; `WorkflowRunCard` also uses `useRunSnapshot` when its live entry is not running. This makes the card useful both while a run is active and after an event frame was missed or the live entry has gone away.

The card sanitizes display text, switches to its own slot before opening the Workflows panel when rendered from a background pane, and dispatches `openActivityToTab('workflows')`. A finished run exposes the save-workflow flow; `WorkflowRunCard` requires a snapshot source before the library-promotion action is enabled.

### Completion card

`src/kiro_crew/dashboard/workflow_inject.py::_summarize` creates the completion header, and `inject_workflow_result` appends it as an `assistant` message to the originating slot, broadcasts it to the live chat, and persists the same message for replay. The broadcast includes a workflow-result kind, while the durable message is an assistant row; the renderer therefore detects the persisted content rather than relying on an event kind.

`website/src/pages/chat/WorkflowCompletionCard.tsx::parseWorkflowCompletion` parses the backend header and separates the display body. It removes the trailing agent-facing workflow-tool hint only from the rendered body; it does not mutate the message. The card sanitizes the workflow title, renders the body with `MarkdownRenderer`, starts with the completion body collapsed, and opens the Workflows panel through `openActivityToTab('workflows')`.

### Rendering invariants

**Parse-gated completion detection prevents data loss.** `isWorkflowCompletionMessage` accepts only an assistant message that `parseWorkflowCompletion` successfully parses. `useChatPageTranscriptController.tsx` and `transcriptRenderers.tsx` use that predicate before selecting `WorkflowCompletionCard`; an unparseable header therefore falls through to ordinary markdown instead of selecting a card that returns no content. `WorkflowCompletionCard.test.tsx` pins this fallback.

**Launch detection has the same fallback.** `WorkflowRunCard.tsx::extractWorkflowRunId` returns no ID when persisted tool output does not match the launch contract. `useChatPageTranscriptController.tsx` then renders the generic `ToolCallLine`, and `transcriptRenderers.tsx` does the same inside its launch renderer. This preserves the normal tool row while output is absent, malformed, or from another tool.

**Cards remain visible independently of turn folding.** `TurnBlock.tsx::isWorkflowRunItem` removes launch cards from the collapsed tool set, and `isWorkflowCompletionItem` includes completion cards in `isVisibleInline`. Without those classifications, the only chat anchor for a workflow lifecycle event can be hidden behind a turn disclosure.

## Tests

- `website/src/test/WorkflowRunCard.test.tsx` covers launch detection, live-state and intent fallback rendering, panel opening, slot retargeting, and saving a finished workflow.
- `website/src/test/WorkflowCompletionCard.test.tsx` covers parsing, parse-gated fallback, rendering disclosure, panel opening, and completion-body containment.
- `website/src/test/transcriptRenderersRenderCov80.test.tsx` verifies that the shared transcript registry selects both cards and wraps their rows.
- `test/test_workflows_inject.py::test_summary_header_format_is_pinned_for_frontend` pins the header emitted by `_summarize` for the completion parser.
