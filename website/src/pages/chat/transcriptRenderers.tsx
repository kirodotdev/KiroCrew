/**
 * transcriptRenderers — the dashboard's row set for the shared chat transcript.
 *
 * This is the ONE dashboard row set (chat-core P5-b): the single-chat surface
 * (ChatPage) spreads this factory into its host list and adds only its
 * page-only entries (the conversational bubble with fork/pin/footer chrome,
 * the undrawn/permission rows); ChatPane calls it with fewer options. Rows the
 * SDK default registry already draws from the same component and the same
 * inputs -- the stop-event card, the notice card, the MCP OAuth banner -- are
 * registered NOWHERE else: not here (a second copy is what the "leaves the
 * stop row to the SDK default" test pins shut) and, since P5-c, not on the
 * page either. Behaviour a surface cannot supply is an
 * OPTION with the pane's default -- the tool row's disclosure key, its
 * "animating" rule, the hot-transcript hint, the completion cards' session
 * hand-offs -- so the two surfaces differ only in what they wire, never in
 * how a row is drawn.
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
import type React from 'react'
import ThinkingBlock from './ThinkingBlock'
import ToolCallLine from './ToolCallLine'
import NudgeCard, { nudgeMatchesLoop } from './NudgeCard'
import RecoveryCard, { injectOpensTurn, resolveInjectCard } from './RecoveryCard'
import { SystemNoticeRow, isSystemNoticeRow } from './CompactionCard'
import { ErrorCard, isAuthRequired, isModelUnentitled, isUsageLimit } from './ErrorCard'
import { FEATURE_REQUEST_FORM_URL, isFeatureRequestRow } from '../../prompts/featureRequest'
import NoticeCard from './NoticeCard'
import { resolveTransientNotice } from './transientNotice'
import WorkflowRunCard, { extractWorkflowRunId, isWorkflowRunTool } from './WorkflowRunCard'
import SubagentRunCard, { extractSpawnRunLaunch, isSpawnRunTool } from './SubagentRunCard'
import WorkflowCompletionCard, { isWorkflowCompletionMessage } from './WorkflowCompletionCard'
import SubagentCompletionCard from './SubagentCompletionCard'
import { isSubagentCompletionMessage, type ParsedSubagentCompletion } from './subagentCompletion'
import { REASONING_ROLES, TURN_OPENER_ROLES, hasReasoningContent } from './groupDisplayItems'
import { FileCard } from '../../components/FileCard'
import UserMessage from './UserMessage'
import CrewmateMessage, { type CrewmateIdentity } from './CrewmateMessage'
import { crewmateBubbleClass, crewmateRunPosition } from '../../components/chat/crewmateBubbles'
import { formatTs, renderAssistantBubble, replyInThreadFor, threadFooterFor, type MessageRenderer, type MessageRenderContext } from '../../app-sdk/messageRenderers'
import { renderUserContent } from './ChatPageMessageContent'
import { fmtMessageTimeFull } from './messageTime'
import type { ChatMessage } from '../../types'

/** Disclosure-map identity for a tool row (#8204). messageRowKey is
 *  `${role}-${clientTs ?? ts}` and tool rows are never clientTs-stamped, so a
 *  burst of tool rows appended in one server tick all share `tool-<tick>` —
 *  expanding one expanded them all, because the row key doubled as the
 *  toolDisclosure map key. Fold `meta.tool_call_id` in when present (ACP-issued,
 *  globally unique) so each row owns its disclosure entry.
 *
 *  Deliberately NOT folded into messageRowKey: the React-key role is not at
 *  stake (each renderMessage element is the sole child of a separately keyed
 *  wrapper), and the key-stability suite pins messageRowKey(tool) === 'tool-<ts>'.
 *  Scoped to role 'tool' and identity when the id is absent, so every other
 *  role's in-session disclosure state keeps its existing key shape.
 *  Shared by every dashboard surface through this row set (chat-core P5-b);
 *  exported for tests. */
export function toolDisclosureKey(m: ChatMessage, key: string): string {
  if (m.role !== 'tool') return key
  const tcid = m.meta?.tool_call_id
  return typeof tcid === 'string' && tcid ? `${key}-${tcid}` : key
}

export interface TranscriptRendererOptions {
  /** Slot these rows belong to. The tool line keys its per-slot log off it;
   *  omitted (the single-chat surface) it reads the active slot's. */
  slot?: string
  /** Whether a tool row animates as "running". Default: the transcript's
   *  running flag. ChatPage narrows it to the trailing group -- rows after the
   *  last assistant text while the slot is in `tool_running`. */
  toolRunning?: (m: ChatMessage, ctx: MessageRenderContext) => boolean
  /** The transcript is scrolling / streaming hard; tool rows defer their
   *  heavier work. */
  transcriptHot?: boolean
  /** What to draw for a `file` row whose content is not JSON. Default: nothing
   *  (a pane); the single-chat surface has always fallen through to its
   *  conversational bubble for one and passes that. */
  renderUnparsedFile?: (m: ChatMessage, ctx: MessageRenderContext) => React.ReactNode
  /** Session hand-offs on the completion cards: open a session chip, and the
   *  title map + active key the chip resolver needs. */
  onSessionOpen?: (key: string) => void
  sessions?: ReadonlyMap<string, string>
  activeSession?: string
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
  /** Fix affordances for a model-entitlement error row (`model_unentitled`
   *  kind): open this surface's model picker, and deep-link to the Default
   *  Model setting. Omitted → the row renders as plain prose, which is correct
   *  for a surface with no picker of its own (a pane). Offered on EVERY such
   *  row, not only the newest: an entitlement error is settled state the user
   *  still has to act on, whereas Continue resumes a turn and so is unique. */
  onPickModel?: () => void
  onOpenDefaultModel?: () => void
  /** Draw confirmed steers as ordinary user messages (no "Steered into the
   *  running turn" badge). A `steer-only` composer host sets it: every busy
   *  send on that surface is a steer, so the badge would label each one with
   *  the very mechanics the surface hides. Off (default) the SDK's `user`
   *  entry is used unchanged. */
  hideSteerBadge?: boolean
  /** Fix affordance for an `auth_required` row: deep-link to the Kiro sign-in
   *  card in Settings. Omitted on a surface with no settings route. */
  onOpenSignIn?: () => void
  /** Draw the assistant rows as a CREWMATE speaking: avatar + name + time on
   *  the first message of a run, one bordered bubble per message, grouped
   *  corners (components/chat/crewmateBubbles). Set by the Members page for a
   *  member-mode slot; absent everywhere else, so an ordinary chat keeps the
   *  SDK's assistant row byte-for-byte. The host also filters the transcript
   *  with `filterCrewmateChat` — this option only changes how what remains is
   *  drawn. */
  crewmate?: CrewmateIdentity
  /** The UNFILTERED transcript behind a crewmate's chat. The rows the pane
   *  draws are `ctx.messages`; the rows the pane dropped (the `inject` row a
   *  policy block writes among them) are only here. Read for the steer-chip
   *  decision, never for layout. Meaningless without `crewmate`. */
  crewmateTranscript?: ChatMessage[]
}

/** Whether `row` OPENS a turn, for the two feature-request scans below. Read
 *  from the transcript's own row-kind vocabulary, not a role list of this
 *  module's: the opener ROLES (`TURN_OPENER_ROLES`: a typed row, an auto-nudge
 *  cycle, a drained sub-agent completion) minus a STEER, plus the inject KINDS
 *  the gateway stamps as a prompt of their own (`injectOpensTurn`: a cron
 *  notification, a fan-out synthesis -- a `recovery` or `user_replay` continues
 *  the request above it, and an unstamped inject dispatches nothing). A steer is
 *  persisted as a `user` row with `meta.steer` (chat_delivery.py) and appended
 *  optimistically in the same shape (ChatPage `steer()`), but it was injected
 *  INTO a running turn, so it cannot begin one. Same answer as the store's
 *  `isTurnBoundaryUser`, `selectSlotPendingApproval`'s walk and the turn-head
 *  walk in `app-sdk/turnPolicyBlock.ts`. Every steer row is exempt, the
 *  optimistic bubble included: a bubble the server turned into a NEW turn is
 *  reconciled by the echo that carries its `sendId` (the store deletes its
 *  `steer` flag), and until then a misread here only moves a link between two
 *  rows -- it never splices content, which is the one reason
 *  `isTurnBoundaryUser` keeps its optimistic exception. */
function opensTurn(row: ChatMessage): boolean {
  if (row.role === 'user') return !row.meta?.steer
  return TURN_OPENER_ROLES.has(row.role) || injectOpensTurn(row)
}

/** True when the error row at `index` is the seeded feature-request turn's own
 *  refusal: the nearest TURN OPENER above it (`opensTurn`) is a user row
 *  carrying the flow's stamp (`meta.featureRequest: true`,
 *  `prompts/featureRequest.isFeatureRequestRow`). Stopping at every opener --
 *  not only a typed row -- is what keeps a later turn's limit its own: a nudge
 *  that fires after the request was filed, or a cron notification drained after
 *  it, starts a new turn, so a limit hit below either must get Resume back, not
 *  the form. Walking PAST a steer is the mirror: the user steering the seeded
 *  turn does not unmark it, so the form stays and Resume (a retry that replays
 *  the rejection) stays withheld -- and so does walking past the runtime's own
 *  retry of the request (a `recovery` or `user_replay` inject). The
 *  stamp rides the send's `meta`, which the gateway persists verbatim on the
 *  user row and echoes back, so the rule reads the same before the echo (the
 *  optimistic bubble), after it, on a reloaded transcript and in a second tab
 *  -- nothing is asked of the host. A window that no longer holds the marked
 *  row (paged out) answers false: the form is offered on evidence, never on
 *  the slot alone. */
export function isFeatureRequestRefusal(messages: readonly ChatMessage[], index: number): boolean {
  for (let i = index - 1; i >= 0; i--) {
    const row = messages[i]
    if (!opensTurn(row)) continue
    return row.role === 'user' && isFeatureRequestRow(row.meta)
  }
  return false
}

/** True when the composer's Resume would replay the feature-request refusal:
 *  the newest error row is a `usage_limit` row that `isFeatureRequestRefusal`
 *  claims, and neither an assistant row nor a turn opener (`opensTurn`) follows
 *  it (a later turn is the composer's business, not this row's). ChatPage reads
 *  it to suppress the composer's Resume and its "press Resume" hint beside a
 *  card that has just withheld Resume for the same reason -- the two must not
 *  argue. */
export function featureRequestRefusalIsNewest(messages: readonly ChatMessage[]): boolean {
  const idx = lastErrorIndex(messages)
  if (idx < 0) return false
  for (let j = idx + 1; j < messages.length; j++) {
    const row = messages[j]
    if (row.role === 'assistant' || opensTurn(row)) return false
  }
  return isUsageLimit(messages[idx]) && isFeatureRequestRefusal(messages, idx)
}

/** Index of the last `error` row, so only that one offers Continue. Derived
 *  from the transcript the list already handed us rather than asked of the
 *  host, which would let the two drift apart. */
function lastErrorIndex(messages: readonly ChatMessage[]): number {
  for (let j = messages.length - 1; j >= 0; j--) if (messages[j].role === 'error') return j
  return -1
}

export function createTranscriptRenderers(
  o: TranscriptRendererOptions,
): readonly MessageRenderer[] {
  const toolLine = (m: ChatMessage, ctx: MessageRenderContext) => {
    const dKey = toolDisclosureKey(m, ctx.key)
    return ctx.row(
      <ToolCallLine
        message={m}
        running={o.toolRunning ? o.toolRunning(m, ctx) : ctx.running}
        slot={o.slot}
        onFileOpen={o.onFileOpen}
        disclosure={o.toolDisclosure?.[dKey]}
        disclosureKey={dKey}
        onDisclosureChange={o.onToolDisclosureChange}
        appInPanel={o.appInPanel}
        onOpenApp={o.onOpenApp}
        transcriptHot={o.transcriptHot}
      />,
      true,
    )
  }
  // Narrowed once here so the crewmate entry below can close over a definite
  // identity instead of re-asserting `o.crewmate` inside its render.
  const crewmate = o.crewmate
  const crewmateTranscript = o.crewmateTranscript

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
          onSessionOpen={o.onSessionOpen}
          sessions={o.sessions}
          activeSession={o.activeSession}
          messageTs={m.ts}
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
        return ctx.row(<SubagentRunCard key={ctx.key} launch={launch} slot={o.slot ?? ''} />, true)
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
    {
      // The ✅ / 🚫 completion sibling of a tool call carries state, not a row:
      // completion is drawn by the tool line's own icon, and the 🚫 sibling is
      // read for the auto-denied flag. Claimed explicitly so it draws nothing on
      // EVERY surface -- a pane's unclaimed-role fallback already drew nothing,
      // but the single-chat surface's is the bubble, and "✅ done" as a bubble is
      // the row this closes.
      id: 'tool_completion',
      roles: ['tool'],
      render: () => null,
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
          return o.renderUnparsedFile ? o.renderUnparsedFile(m, ctx) : null
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
      // Refines `assistant`: a gateway system notice (kind=compaction or
      // kind=session_reload, the SYSTEM_NOTICE_KINDS set the last-real-message
      // scans already skip) is a status card, not a reply. The gateway writes
      // them as assistant rows (chat_utils._append_compaction_notice,
      // chat_handlers' reload confirmation); the compaction row's content is the
      // backend's whole context summary, so the bubble fallback painted
      // kilobytes of machine digest as if the model had said it — on this page
      // AND in every ChatPane (Crew DM) that shares this factory. Must precede
      // any assistant-keyed bubble.
      id: 'system_notice',
      roles: ['assistant'],
      match: isSystemNoticeRow,
      render: (m, ctx) => ctx.row(<SystemNoticeRow key={ctx.key} message={m} disclosureKey={ctx.key} />),
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
          onSessionOpen={o.onSessionOpen}
          sessions={o.sessions}
          activeSession={o.activeSession}
          messageTs={m.ts}
          disclosureKey={ctx.key}
        />,
        true,
      ),
    },
    // Replaces the SDK's `assistant` entry (same id) ONLY for a crewmate's
    // chat: the same AssistantMessage (markdown, option chips, hover actions),
    // placed as a bubble in a run under the crewmate's avatar and name. The
    // run position is derived from the list the pane already filtered, so the
    // neighbours it reads are the rows drawn next to it. The two assistant-role
    // refinements above (system notice, workflow completion) still precede it;
    // the pane's filter has already dropped both for a crewmate anyway.
    ...(crewmate
      ? [{
          id: 'assistant',
          roles: ['assistant', 'streaming'],
          render: (m: ChatMessage, ctx: MessageRenderContext) => {
            // Run position reads turn boundaries off the UNFILTERED transcript
            // (a patrol wake between two replies is filtered from `ctx.messages`).
            const pos = crewmateRunPosition(ctx.messages, ctx.index, crewmateTranscript)
            // The run ends here (single / end): the row after it is a boundary
            // the user sees or the turn ended, so this bubble is the one that
            // carries the hover actions. The policy-block read goes to
            // the unfiltered transcript — see `crewmateTranscript`; the row is
            // located by identity, since the filter keeps the same objects.
            const full = crewmateTranscript
            const fullIndex = full ? full.indexOf(m) : -1
            const bubble = renderAssistantBubble(m, ctx, crewmateBubbleClass(pos), {
              forceFooter: pos === 'single' || pos === 'end',
              policyBlockTranscript: full && fullIndex >= 0 ? { messages: full, index: fullIndex } : undefined,
            })
            if (bubble === null) return null
            return ctx.row(
              <CrewmateMessage crewmate={crewmate} pos={pos} ts={m.ts}>{bubble}</CrewmateMessage>,
              true,
            )
          },
        } satisfies MessageRenderer]
      : []),
    {
      // Replaces the default's bare div: same text, plus the Continue
      // affordance on the LAST error when a turn was interrupted.
      id: 'error',
      roles: ['error'],
      render: (m, ctx) => {
        // A transient-5xx notice the gateway is already retrying against is
        // routine status, not a failure: localized copy on a soft NoticeCard.
        // Only its terminal shape ("please try again") stays a red ErrorCard,
        // with the same localized text.
        const transient = resolveTransientNotice(m, ctx.messages, ctx.index)
        if (transient?.card === 'notice') {
          return ctx.row(<NoticeCard content={transient.text} tone={transient.tone} />)
        }
        const unentitled = isModelUnentitled(m)
        const authRequired = isAuthRequired(m)
        // The form is offered on the seeded turn's own refusal and nowhere else:
        // a #4198 refused-send row in the same slot carries no kind (the send
        // never went out, so a retry CAN help); a usage limit under a user row
        // the pill did not stamp is some other turn's; and a limit hit after
        // the user typed on in the same slot belongs to THAT turn, so it keeps
        // today's card, Continue included. The route is a constant of the flow,
        // never read from the row: the stamp only selects it.
        const featureRequestFormUrl =
          isUsageLimit(m) && isFeatureRequestRefusal(ctx.messages, ctx.index) ? FEATURE_REQUEST_FORM_URL : undefined
        return ctx.row(
          <ErrorCard
            content={transient ? transient.text : m.content}
            meta={m.meta}
            // A rejection the backend says no retry can fix never offers Continue,
            // even when this row is the newest and the turn was interrupted:
            // resuming would replay the identical rejection (or the same
            // signed-out wall, or the same spent allowance).
            onContinue={
              !unentitled && !authRequired && !featureRequestFormUrl && o.onContinue && o.continuable && o.interrupted && ctx.index === lastErrorIndex(ctx.messages)
                ? o.onContinue
                : undefined
            }
            continuing={o.continuing}
            onPickModel={unentitled ? o.onPickModel : undefined}
            onOpenDefaultModel={unentitled ? o.onOpenDefaultModel : undefined}
            onOpenSignIn={authRequired ? o.onOpenSignIn : undefined}
            unentitledElsewhere={unentitled}
            featureRequestFormUrl={featureRequestFormUrl}
          />,
        )
      },
    },
    // Replaces the SDK's `user` entry (same id) ONLY when the host asks for it:
    // identical content path (renderUserContent — paste chips, inline images
    // and file cards included), one prop different. Absent the flag no entry is emitted, so every other
    // surface keeps the SDK row byte-for-byte.
    ...(o.hideSteerBadge
      ? [{
          id: 'user',
          roles: ['user'],
          render: (m: ChatMessage, ctx: MessageRenderContext) => ctx.wrapper(
            <>
              <UserMessage
                content={m.content}
                meta={m.meta}
                timestamp={formatTs(m.ts)}
                timestampTitle={fmtMessageTimeFull(m.ts)}
                renderContent={(c, mt) => renderUserContent({ content: c, meta: mt, onFileOpen: ctx.onFileOpen })}
                hideSteerBadge
                onReplyInThread={replyInThreadFor(m, ctx)}
              />
              {threadFooterFor(m, ctx, 'end')}
            </>,
            true,
          ),
        } satisfies MessageRenderer]
      : []),
  ]
}
