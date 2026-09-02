/**
 * transcriptRenderers — the dashboard's row set for the shared chat transcript.
 *
 * The single-chat surface (ChatPage) draws its rows from a local role chain.
 * Every OTHER dashboard surface draws through app-sdk/ChatMessageList, whose
 * default registry is deliberately store-free and therefore renders a WEAKER
 * transcript: a static pill instead of the live tool line, and nothing at all
 * for a thinking trace, a sent file, an auto-nudge turn, a workflow launch, a
 * sub-agent launch, a recovery inject or a workflow completion. This module
 * carries ChatPage's row set as registry entries so a second surface reads the
 * SAME transcript rather than a reduced one.
 *
 * It lives under pages/chat rather than in app-sdk on purpose: the registry's
 * own module must stay importable by consumers that have no Redux store at all,
 * so anything store-connected is supplied BY the host as an entry — which is
 * exactly what this is.
 *
 * The returned array is merged AHEAD of the SDK defaults (see mergeRenderers),
 * so an entry reusing a default's `id` REPLACES it, a new `id` ADDS a row type,
 * and a narrow entry must precede the broader one it refines.
 */
import ThinkingBlock from './ThinkingBlock'
import ToolCallLine from './ToolCallLine'
import NudgeCard, { nudgeMatchesLoop } from './NudgeCard'
import RecoveryCard, { resolveInjectCard } from './RecoveryCard'
import { ErrorCard } from './ErrorCard'
import WorkflowRunCard, { extractWorkflowRunId, isWorkflowRunTool } from './WorkflowRunCard'
import SubagentRunCard, { extractSpawnRunLaunch, isSpawnRunTool } from './SubagentRunCard'
import WorkflowCompletionCard, { isWorkflowCompletionMessage } from './WorkflowCompletionCard'
import SubagentCompletionCard from './SubagentCompletionCard'
import { isSubagentCompletionMessage, type ParsedSubagentCompletion } from './subagentCompletion'
import { REASONING_ROLES, hasReasoningContent } from './groupDisplayItems'
import { FileCard } from '../../components/FileCard'
import type { MessageRenderer, MessageRenderContext } from '../../app-sdk/messageRenderers'
import type { ChatMessage } from '../../types'

export interface TranscriptRendererOptions {
  /** Slot these rows belong to. The tool line keys its per-slot log off it. */
  slot: string
  /** Open a file in the host's side panel. Unwired from a pane until #3300:
   *  the dock is `activeSlot`-keyed while pane focus deliberately is not. */
  onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void
  /** Open a directory in the host's side panel. Same #3300 blocker. */
  onFolderOpen?: (path: string) => void
  /** "Show in side panel" on a sub-agent completion card. Same #3300 blocker. */
  onOpenSubagentPanel?: (parsed: ParsedSubagentCompletion) => void
  /** Expanded-state map for tool rows, held ABOVE the row: a virtualised or
   *  remounted transcript unmounts the row and would otherwise forget it. */
  toolDisclosure?: Record<string, boolean>
  onToolDisclosureChange?: (key: string, expanded: boolean) => void
  /** Whether an MCP app may be revealed in the panel, and how. Unwired from a
   *  pane for the same reason as `onFileOpen` — the dock is `activeSlot`-keyed
   *  while pane focus is not, tracked in #3300. */
  appInPanel?: boolean
  onOpenApp?: (toolCallId: string) => void
  /** Id of the auto-nudge loop this surface can open, plus the opener. The
   *  match rule stays here so a host never re-implements it. Unwired from a
   *  pane because a pane's composer cannot reach the auto-nudge popover yet;
   *  wire it when that popover becomes reachable per pane. */
  activeNudgeLoopId?: string | null
  onOpenNudgeLoop?: () => void
  /** Turn-recovery state for the error row's Continue button. Omitted → the
   *  row renders without one, which is correct for a surface that cannot
   *  continue a turn. A pane cannot supply these until `selectContinuable` /
   *  `selectTurnInterrupted` become slot-aware — both read the active slot
   *  today, so a pane cannot ask whether ITS turn was interrupted. */
  continuable?: boolean
  interrupted?: boolean
  continuing?: boolean
  onContinue?: () => void
}

/** Index of the last `error` row, so only that one offers Continue. Derived
 *  from the transcript the list already handed us rather than asked of the
 *  host, which would let the two drift apart. */
function lastErrorIndex(messages: ChatMessage[]): number {
  for (let j = messages.length - 1; j >= 0; j--) if (messages[j].role === 'error') return j
  return -1
}

export function createTranscriptRenderers(
  o: TranscriptRendererOptions,
): readonly MessageRenderer[] {
  const toolLine = (m: ChatMessage, ctx: MessageRenderContext) =>
    ctx.row(
      <ToolCallLine
        message={m}
        running={ctx.running}
        slot={o.slot}
        onFileOpen={o.onFileOpen}
        disclosure={o.toolDisclosure?.[ctx.key]}
        disclosureKey={ctx.key}
        onDisclosureChange={o.onToolDisclosureChange}
        appInPanel={o.appInPanel}
        onOpenApp={o.onOpenApp}
      />,
      true,
    )

  return [
    // ── Shape-matched rows, ahead of anything keyed only by role ──
    {
      // Replaces the default: same card, but wired to open a folder and the
      // side panel the way the single-chat surface does.
      id: 'subagent_completion',
      roles: ['*'],
      match: isSubagentCompletionMessage,
      render: (m, ctx) => ctx.row(
        <SubagentCompletionCard
          key={ctx.key}
          message={m}
          onFileOpen={o.onFileOpen}
          onFolderOpen={o.onFolderOpen}
          disclosureKey={ctx.key}
          onOpenPanel={o.onOpenSubagentPanel}
        />,
        true,
      ),
    },

    // ── Tool rows: the two launch cards refine the generic line, so they
    //    must be resolved before it. Both reuse the shared predicate the
    //    grouping logic uses, so a launch card and TurnBlock can never
    //    disagree about whether a row is a launch. ──
    {
      id: 'workflow_run_tool',
      roles: ['tool'],
      match: m => !!m.content?.startsWith('🔧') && isWorkflowRunTool(m),
      render: (m, ctx) => {
        const runId = extractWorkflowRunId(m)
        // The match already proved this, but a null here must draw the generic
        // line rather than crash the row.
        if (!runId) return toolLine(m, ctx)
        return ctx.row(<WorkflowRunCard key={ctx.key} runId={runId} message={m} slot={o.slot} />, true)
      },
    },
    {
      id: 'subagent_run_tool',
      roles: ['tool'],
      match: m => !!m.content?.startsWith('🔧') && isSpawnRunTool(m),
      render: (m, ctx) => {
        const launch = extractSpawnRunLaunch(m)
        if (!launch) return toolLine(m, ctx)
        return ctx.row(<SubagentRunCard key={ctx.key} launch={launch} slot={o.slot} />, true)
      },
    },
    {
      // Replaces the default pill with the live, store-connected tool line:
      // purpose label, expandable detail, elapsed time, file affordance, MCP
      // app reveal. The 🔧 guard is the default's and must be kept — the
      // hidden 🚫 deny sibling shares this role and is never drawn.
      id: 'tool',
      roles: ['tool'],
      match: m => !!m.content?.startsWith('🔧'),
      render: toolLine,
    },

    // ── Rows the default registry leaves undrawn ──
    {
      // The default registry draws nothing for a thinking trace. It carries
      // real content, so it gets its own block.
      //
      // LIMITATION: `thinking` is in GROUPED_ROLES, so this row renders INSIDE
      // the collapsible group rather than standalone the way the single-chat
      // surface renders it. Opting a grouped role out of the group is not an
      // extension point yet — tracked in #2940.
      id: 'thinking_block',
      // Both the role key and the content guard derive from the shared
      // reasoning predicate (see groupDisplayItems.ts) so this surface can
      // never drift from ChatPage's renderer or the gate→fold pipeline.
      roles: [...REASONING_ROLES],
      render: (m, ctx) => (hasReasoningContent(m) ? ctx.row(<ThinkingBlock content={m.content} disclosureKey={ctx.key} />) : null),
    },
    {
      // Replaces the default's null with the player / download card.
      id: 'file',
      roles: ['file'],
      render: (m, ctx) => {
        let file
        try {
          file = JSON.parse(m.content)
        } catch {
          return null
        }
        return ctx.row(<FileCard file={file} />)
      },
    },
    {
      // No default entry: an auto-nudge turn would draw nothing at all.
      id: 'nudge',
      roles: ['nudge'],
      render: (m, ctx) =>
        ctx.row(
          <NudgeCard
            message={m}
            disclosureKey={ctx.key}
            onOpenLoop={
              o.onOpenNudgeLoop && nudgeMatchesLoop(m, o.activeNudgeLoopId) ? o.onOpenNudgeLoop : undefined
            }
          />,
        ),
    },
    {
      // Refines `inject`: a gateway-authored injection is a one-line card, not
      // the cron-notification bubble the default draws.
      //
      // Matched via the SHARED resolver rather than the recovery parser alone, so
      // this registry (ChatPane, SideChat, ChatEmbed) makes the same decision
      // ChatPage does. Gating only on a recognised recovery marker left every
      // other injected shape — the sub-agent synthesis prompt included — falling
      // through to the SDK default, which paints raw machine prose as a bubble.
      id: 'recovery_inject',
      roles: ['inject'],
      match: m => resolveInjectCard(m) !== null,
      render: (m, ctx) => {
        const parsed = resolveInjectCard(m)
        if (!parsed) return null
        return ctx.row(<RecoveryCard parsed={parsed} disclosureKey={ctx.key} />)
      },
    },
    {
      // Refines `assistant`: an injected workflow completion is a compact
      // status card, not a full markdown reply.
      id: 'workflow_completion',
      roles: ['assistant'],
      match: isWorkflowCompletionMessage,
      render: (m, ctx) => ctx.row(
        <WorkflowCompletionCard
          key={ctx.key}
          message={m}
          onFileOpen={o.onFileOpen}
          onFolderOpen={o.onFolderOpen}
          disclosureKey={ctx.key}
        />,
        true,
      ),
    },
    {
      // Replaces the default's bare div: same text, plus the Continue
      // affordance on the LAST error when a turn was interrupted.
      id: 'error',
      roles: ['error'],
      render: (m, ctx) =>
        ctx.row(
          <ErrorCard
            content={m.content}
            onContinue={
              o.onContinue && o.continuable && o.interrupted && ctx.index === lastErrorIndex(ctx.messages)
                ? o.onContinue
                : undefined
            }
            continuing={o.continuing}
          />,
        ),
    },
  ]
}
