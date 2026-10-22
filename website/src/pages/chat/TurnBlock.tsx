import { memo, useState, useRef, useEffect, useMemo, useCallback, type CSSProperties, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { RowActivityContext, type RowActivityReporter } from './rowActivity'
import type { DisplayItem, TurnItem } from './types'
import { uniqueRowKeys } from '../../chat-core/transcript/rowKeys'
import { useStableMessageKey } from '../../chat-core/transcript/useStableMessageKey'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { useSearchHighlight } from '../../hooks/SearchHighlightContext'
import { isWorkflowRunTool } from './WorkflowRunCard'
import { isSpawnRunTool } from './SubagentRunCard'
import { isWorkflowCompletionMessage } from './WorkflowCompletionCard'
import { isSubagentCompletionMessage } from './subagentCompletion'
import { isReasoningBurst } from './groupDisplayItems'
import { isSystemNoticeRow } from './CompactionCard'
import { parseRecoveryMessage } from './RecoveryCard'
import { isDiffToolMessage } from './toolDiff'
import { isAskAnsweredToolMessage } from '../../utils/askQuestionAnswers'
import { findOptionMarkers, stripOptionMarkers } from '../../app-sdk/protocol/optionMarker'
import { hasKeepVisibleMarker } from '../../app-sdk/protocol/keepVisibleMarker'
import { i18nT } from '../../i18n/t'

// A workflow_run launch renders as its own always-visible inline card
// (WorkflowRunCard), so it must never be folded into the collapsible tool-call
// group — treat it as a non-tool, always-visible item.
const isWorkflowRunItem = (it: TurnItem) =>
  it.kind === 'single' && it.msg.role === 'tool' && isWorkflowRunTool(it.msg)
// Same for a spawn_run launch (SubagentRunCard): folding it into "Worked
// through N steps" would leave a spawned wave with no visible record in
// scrollback.
const isSpawnRunItem = (it: TurnItem) =>
  it.kind === 'single' && it.msg.role === 'tool' && isSpawnRunTool(it.msg)
// A workflow completion event renders as its own compact card and must stay
// visible even when a turn's reasoning is collapsed (collapseAll mode).
const isWorkflowCompletionItem = (it: TurnItem) =>
  it.kind === 'single' && isWorkflowCompletionMessage(it.msg)
// Same for a sub-agent completion event. The delivery-timeout variant arrives
// under the `assistant` role, which lands mid-turn — collapsing it would hide
// the only notice that a result never made it into the session.
const isSubagentCompletionItem = (it: TurnItem) =>
  it.kind === 'single' && isSubagentCompletionMessage(it.msg)
// An MCP App (SEP-1865) render is anchored to its tool-call row (ToolCallLine
// mounts the sandboxed iframe below the row). Folding that row into a
// collapsed pane hides the interactive app — and re-expanding REMOUNTS the
// iframe, reloading the app and losing in-canvas state. Treat app-bearing
// tool calls like workflow_run / spawn_run cards: first-class, always visible.
// ``appToolCallIds`` is the set of tool_call_ids with a live render payload
// in chat.mcpApps for this slot (computed once per turn render).
const isMcpAppItem = (it: TurnItem, appToolCallIds: ReadonlySet<string>) =>
  it.kind === 'single' && it.msg.role === 'tool' &&
  typeof it.msg.meta?.tool_call_id === 'string' &&
  appToolCallIds.has(it.msg.meta.tool_call_id)
// An edit-tool row promoting an inline diff presentation (ToolCallLine
// renders a DiffBlock card or summary chip below the pill). It stays out of
// BOTH folds — a file change is a result, not a working step: the same class
// as the prose ```diff the final summary used to carry, which neither fold
// ever hid. Density relief is per-card (ToolCallLine's fold chip) plus the
// size caps in presentToolDiff, so an edit-heavy turn is N foldable cards,
// not an immovable wall (see rfc-tool-derived-diff-cards.md).
const isDiffCardItem = (it: TurnItem) =>
  it.kind === 'single' && isDiffToolMessage(it.msg)
// An answered ask_question row carries the only record of the user's answers (its chip), so like a diff card it stays out of both folds.
const isAskAnsweredItem = (it: TurnItem) =>
  it.kind === 'single' && isAskAnsweredToolMessage(it.msg)
const isTool = (it: TurnItem, appToolCallIds: ReadonlySet<string>) =>
  it.kind === 'single' && it.msg.role === 'tool' && !isWorkflowRunItem(it) &&
  !isSpawnRunItem(it) && !isMcpAppItem(it, appToolCallIds) && !isDiffCardItem(it) &&
  !isAskAnsweredItem(it)
const isHiddenTool = (it: TurnItem) => it.kind === 'single' && it.msg.role === 'tool' && !it.msg.content.startsWith('🔧')
const isConclusion = (it: TurnItem) => it.kind === 'single' && (it.msg.role === 'assistant' || it.msg.role === 'streaming' || it.msg.role === 'file')
/**
 * "Always visible" items — must render inline regardless of TurnBlock collapse state.
 * mcp_oauth: user must always see the Authorize button to act on it.
 * error: errors should never be hidden behind a "Worked through N steps" toggle.
 * mcp_app inject: an embedded app's ui/message delivery is the USER'S OWN action
 * (an Acknowledge click, a form submit) landing in the transcript — folding it
 * into the steps pane hides the record of what they did behind a toggle.
 * card: a change card or guide offer — only the user can confirm it, so
 * it is never folded away with the tool steps that proposed it.
 */
const isAlwaysVisible = (it: TurnItem) => it.kind === 'single' && (
  it.msg.role === 'mcp_oauth' || it.msg.role === 'error' || it.msg.role === 'card' ||
  (it.msg.role === 'inject' && it.msg.meta?.injectKind === 'mcp_app')
)

/**
 * Assistant text containing render-significant payloads must stay visible
 * even when reasoning is collapsed. Currently detects:
 *   - <mcwidget>…</mcwidget> bodies
 *   - markdown image embeds: ![alt](path)
 * Without this, a widget or image emitted between tool calls gets folded
 * into the "Worked through N steps" pane and the user can't see it.
 */
const HAS_RENDERABLE_RE = /<mcwidget(?:\s|>)|!\[[^\]]*\]\([^)]+\)/
const isRenderable = (it: TurnItem) =>
  it.kind === 'single' && isConclusion(it) && (it.msg.role === 'file' || HAS_RENDERABLE_RE.test(it.msg.content))

/**
 * A mid-turn hand-back: an assistant message carrying an [OPTIONS:] follow-up
 * marker. The agent emits that marker ONLY when it believes it is ending the
 * turn, so a message bearing it is by construction a user-facing hand-back — a
 * direct signal of *intent*, not a proxy for importance. (Gating on [OPTIONS:]
 * rather than a length / size heuristic is deliberate: the collapse setting is
 * literally "hide intermediate reasoning", so gating on size would override a
 * preference the user set on purpose.) A single turn can contain SEVERAL
 * hand-backs when the
 * agent resumes in the same turn — after a denied tool call, an auto-nudge /
 * monitor cycle, a queued message, or an injected subagent / workflow
 * completion — but findConclusionIdx keeps only the LAST one, so every earlier
 * hand-back would otherwise be buried in the collapse pane. Surfacing each one
 * inline fixes that.
 *
 * Asks the marker module rather than probing a regex: a candidate whose terminator
 * belongs to an unmatched opener is NOT a marker, and only that module can tell.
 */
function hasOptionsMarker(text: string): boolean {
  return findOptionMarkers(text).length > 0
}
const isHandBack = (it: TurnItem) =>
  it.kind === 'single' && isConclusion(it) && hasOptionsMarker(it.msg.content)

/**
 * A message the agent explicitly marked to survive the collapse: a substantive
 * mid-turn deliverable (a report or synthesis followed by more tool calls or a
 * short sign-off) carrying the invisible `<!-- keep-visible -->` marker (#7948).
 * Without it, findConclusionIdx keeps only the LAST substantive message and a
 * deliverable emitted before a terminal tool call folds into the collapse pane.
 * Same design rule as isHandBack above: gate on an explicit intent marker, not
 * on size — the collapse setting is a user preference, so only a direct signal
 * of agent intent may exempt a message from it. The marker is an HTML comment,
 * so the rendered message shows nothing extra (rehypeRaw emits a comment node,
 * which the react renderer skips).
 */
const isKeepVisible = (it: TurnItem) =>
  it.kind === 'single' && isConclusion(it) && hasKeepVisibleMarker(it.msg.content)

/**
 * READ-ONLY COMPAT for transcripts written by the retired Crew Mode: a
 * forwarded topic result, a meta render, or a question back to the user. That
 * mode broke this component's central assumption — that the LAST assistant
 * message of a turn is the conclusion and the earlier ones are reasoning —
 * because each forward was the FINAL answer for a different topic, so
 * collapsing all but the last hides answers the user asked for. Nothing writes
 * these rows any more (the dispatcher is gone), but the sessions it wrote are
 * still the user's record, so the persisted marker keeps rendering them open.
 * Keyed on the persisted `meta.crew_reply` (the durable signal — the periodic
 * slot flush keeps `meta` for every role but `cls` only for role === 'system'),
 * with the older `crew-reply` class as a fallback for rows written before the
 * marker moved.
 */
const isCrewReply = (it: TurnItem) =>
  it.kind === 'single' && isConclusion(it) &&
  (it.msg.meta?.crew_reply === true || /(^|\s)crew-reply(\s|$)/.test(it.msg.cls || ''))

/** A renderable assistant message (widget/image), a mid-turn hand-back
 *  ([OPTIONS:] marker), a keep-visible-marked deliverable (#7948), a legacy
 *  crew-mode answer, a role that must surface inline (mcp_oauth, error), a
 *  workflow_run / spawn_run / workflow-completion / sub-agent-completion card,
 *  an MCP App-bearing tool call (interactive iframe anchored to the row), a
 *  diff card, an answered ask_question row, or a row the caller pins
 *  (`pinnedRows`, see findAnsweredRefusalRows).
 *  All bypass the collapse pane. */
const NO_PINNED_ROWS: ReadonlySet<TurnItem> = new Set()
const isVisibleInline = (it: TurnItem, appToolCallIds: ReadonlySet<string>, pinnedRows = NO_PINNED_ROWS) =>
  pinnedRows.has(it) ||
  isRenderable(it) || isHandBack(it) || isKeepVisible(it) || isAlwaysVisible(it) || isCrewReply(it) ||
  isWorkflowRunItem(it) || isSpawnRunItem(it) ||
  isSubagentCompletionItem(it) ||
  isWorkflowCompletionItem(it) || isMcpAppItem(it, appToolCallIds) ||
  isDiffCardItem(it) || isAskAnsweredItem(it)

/** How many tool-call rows a RUNNING turn keeps open. */
const LIVE_ACTIVITY_WINDOW = 5

/** A hidden ✅/🚫 completion row renders nothing in every host, so it is
 *  never folded and never counted: one call is one step. */
const isCountedRow = (it: TurnItem) => !isHiddenTool(it)

type FoldMode = 'interim' | 'collapseAll' | 'default'

/**
 * The rows a turn MAY fold, before live rows are taken out (see the fold rule
 * in TurnBlock). Ascending indices into `items`.
 *
 * - Both phases: every tool row older than the newest LIVE_ACTIVITY_WINDOW.
 *   The window is counted over all tool rows FIRST, so a live row among the
 *   newest five never shifts an older one into view.
 * - Settled, on top: the mode's own collapse. Default folds every tool row;
 *   collapseAll folds what precedes the conclusion, or every working step
 *   when the turn has none (collapseAllFoldRange); an interim fan-out folds
 *   every row. `isVisibleInline` rows never fold.
 *
 * The settled set contains the running set, so completion only ever folds
 * more rows. It never opens a row the window had folded.
 */
function foldCandidates(items: TurnItem[], appToolCallIds: ReadonlySet<string>, mode: FoldMode, complete: boolean, answeredRefusalRows: ReadonlySet<TurnItem>): number[] {
  const tools = items.flatMap((it, i) => isTool(it, appToolCallIds) && isCountedRow(it) ? [i] : [])
  const out = new Set(tools.slice(0, Math.max(0, tools.length - LIVE_ACTIVITY_WINDOW)))
  if (complete && mode === 'default') tools.forEach(i => out.add(i))
  if (complete && mode !== 'default') {
    const { end, wholeTurn } = mode === 'interim'
      ? { end: items.length, wholeTurn: false }
      : collapseAllFoldRange(items)
    const pinned = mode === 'interim' ? NO_PINNED_ROWS : answeredRefusalRows
    for (let i = 0; i < end; i++) {
      const it = items[i]
      if (wholeTurn && !isWorkingStepRow(it)) continue
      if (isCountedRow(it) && !isVisibleInline(it, appToolCallIds, pinned)) out.add(i)
    }
  }
  return Array.from(out).sort((a, b) => a - b)
}

const NO_ACTIVE_KEYS: ReadonlySet<string> = new Set()

/** The tool call rendered an MCP App (persisted on the message), so its row may host one again. */
const NEVER_LIVE = (): boolean => false
const hadMcpApp = (it: TurnItem) => it.kind === 'single' && it.msg.meta?.mcp_app === true

/** Stable empty set so the mcpApps selector returns a referentially-equal
 *  value when the slot has no app renders (avoids useless re-renders). */
const EMPTY_ID_SET: ReadonlySet<string> = new Set()

/** Strip OPTIONS/markdown formatting and return plain text content length */
function substantiveLength(text: string): number {
  return stripOptionMarkers(text).replace(/[#*_`>\-|]/g, '').trim().length
}

/**
 * Find the index of a turn's conclusion item: the last `isConclusion` item that
 * is substantive (>= 50 chars), falling back to the last `isConclusion` item of
 * any length, else -1. System notice rows (a watchdog recycle, a compaction)
 * never qualify: they trail the answer and must not displace it. Shared by the
 * auto-expand decision and the render split so the "what's the always-visible
 * conclusion vs collapsed reasoning" answer can't drift between them (a
 * mismatch wrongly expands reasoning above a visible match and pushes it down).
 */
function findConclusionIdx(items: TurnItem[]): number {
  let conclusionIdx = -1
  let fallbackIdx = -1
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (isConclusion(it) && !(it.kind === 'single' && isSystemNoticeRow(it.msg))) {
      if (fallbackIdx === -1) fallbackIdx = i
      if (it.kind === 'single' && substantiveLength(it.msg.content) >= 50) { conclusionIdx = i; break }
    }
  }
  return conclusionIdx === -1 ? fallbackIdx : conclusionIdx
}

/**
 * Where the collapseAll fold stops: the conclusion's index, so everything the
 * agent did BEFORE handing the user its conclusion may fold. A turn with no
 * conclusion handed the user nothing, so there is nothing to keep out of the
 * fold and every row is a working step: the end is the whole turn. That is a
 * quiet `nothing_to_do` end, a turn that stopped on an error, and the rows
 * above a mid-turn steer: the steer is its own display row (the pinned-prompt
 * band pins it, so it cannot live inside a turn), which leaves the work before
 * it as a turn of its own with no answer. A conclusion at `0` is the other
 * boundary, nothing precedes it. Mapping "no conclusion" to `0` instead left
 * every such turn flat, each tool row expanded.
 *
 * `wholeTurn` marks that fold, and it folds only WORKING STEPS
 * (isWorkingStepRow). A turn with no answer usually ends on the row that says
 * why: a Stop card, a `notice`, a compaction or stuck-turn notice. Those rows
 * are not the agent's work, they are the turn's outcome, so they stay in view.
 * An allowlist, not a list of exemptions, so a row kind added later stays
 * visible rather than silently folding. A turn that has a conclusion folds
 * exactly as before.
 */
const collapseAllFoldRange = (items: TurnItem[]): { end: number; wholeTurn: boolean } => {
  const conclusionIdx = findConclusionIdx(items)
  return conclusionIdx < 0
    ? { end: items.length, wholeTurn: true }
    : { end: conclusionIdx, wholeTurn: false }
}

/** A row the agent produced while working: a tool call, a reasoning burst, or
 *  its own text (a system notice is the gateway's, not the agent's). */
const isWorkingStepRow = (it: TurnItem): boolean => {
  if (it.kind !== 'single') return it.kind === 'group'
  const { role } = it.msg
  if (role === 'tool') return true
  if (role === 'assistant' || role === 'streaming') return !isSystemNoticeRow(it.msg)
  return isReasoningBurst(it)
}

/**
 * A tool-refusal recovery card whose dispatch ended on a substantive message,
 * plus that message. The gateway queues the recovery after the dispatch ends,
 * so the reply to it becomes the turn's conclusion and would fold a message the
 * user has already read. Text followed by a tool call is working narration and
 * still folds; other recovery kinds resume an interrupted dispatch.
 */
function findAnsweredRefusalRows(items: TurnItem[]): ReadonlySet<TurnItem> {
  const rows = new Set<TurnItem>()
  items.forEach((it, i) => {
    const prev = items[i - 1]
    if (it.kind === 'single' && it.msg.role === 'inject' && parseRecoveryMessage(it.msg.content)?.kind === 'refusal' &&
        prev?.kind === 'single' && isConclusion(prev) && substantiveLength(prev.msg.content) >= 50) {
      rows.add(prev)
      rows.add(it)
    }
  })
  return rows
}

/**
 * Fold a turn's reasoning bursts into ONE `thinking` row, hoisted to the TURN
 * TOP.
 *
 * chatSlice opens a fresh `thinking` message per burst — one above every tool
 * step it explains (#4178). That keeps the live stream anchored correctly, but
 * a long agentic turn (a prepare-pr round, a monitor cycle) settles into a WALL
 * of a dozen-plus collapsed "Thought process" rows once the interleaved tool
 * calls fold away. This merges every content-bearing burst of the turn into a
 * single synthetic row.
 *
 * WHY TOP, not the first burst's slot: reasoning is client-only and never
 * persisted, so the `chat_done` slot refresh rebuilds the turn from server
 * history (which has no reasoning) and `mergePreservedThinking` re-inserts the
 * saved bursts. It anchors each burst on its FOLLOWING tool call (#4578/#4218),
 * which keeps the burst↔tool 1:1 case interleaved — but history holds ONE
 * assistant answer row for ALL bursts (segment flush is gated on pending text),
 * so any burst NOT followed by a distinct tool (trailing reasoning, or several
 * bursts collapsing onto that one answer row) falls back to the answer-text
 * anchor and lands at the TAIL, below the answer and its footer. Anchoring the
 * merged row at the first burst's position would therefore drop it below the
 * answer for exactly those turns. Pinning it to the turn top instead makes the
 * folded row's position independent of where the refresh parked the bursts:
 * live (interleaved) and reloaded (piled at the tail) both render one reasoning
 * row above the turn's output, matching the pre-#4178 single-block placement.
 *
 * The merge is render-only: the per-burst messages in the store are untouched,
 * so nothing downstream of the transcript (persistence, search idx, the live
 * accumulation in sseThinkingChunk) changes. The synthetic row reuses the first
 * burst's message object (and therefore its `clientTs`, so `messageRowKey` is
 * stable across renders) with the concatenated content, and keeps that burst's
 * `idx` so `renderItem`'s `renderMessage(idx, msg)` still keys it.
 *
 * Because the content GROWS as later bursts arrive (and as the open burst
 * streams), ThinkingBlock's content-growth liveness fires on the merged row too
 * — so a running turn shows ONE live "Thinking" line with the streaming tail
 * rather than sprouting a new row per burst, and it settles to a single
 * "Thought process" when the turn goes quiet. Empty placeholder `thinking` rows
 * (no content) are left in place, so a bare "Thinking…" placeholder is
 * unaffected; a single burst already sitting at the top is returned untouched.
 */
function mergeTurnThinking(items: TurnItem[]): TurnItem[] {
  const thinkingPositions: number[] = []
  const bursts: Extract<TurnItem, { kind: 'single' }>[] = []
  for (let i = 0; i < items.length; i++) {
    const it = items[i]
    // Shared with the wrap gate that routes multi-burst batches here — see
    // isReasoningBurst in groupDisplayItems.ts for why there is ONE definition.
    if (isReasoningBurst(it)) { thinkingPositions.push(i); bursts.push(it) }
  }
  if (thinkingPositions.length === 0) return items
  // A single burst already at the top is the settled, correct shape (the live
  // path and a 1:1 reload both produce it) — leave it, so a plain reasoning
  // turn is not needlessly rewritten.
  if (thinkingPositions.length === 1 && thinkingPositions[0] === 0) return items
  const first = bursts[0]
  const merged = bursts.map(b => b.msg.content).join('\n\n')
  const mergedItem: TurnItem = { kind: 'single', msg: { ...first.msg, content: merged }, idx: first.idx }
  const drop = new Set(thinkingPositions)
  // Hoist the merged reasoning to the turn top; every other item keeps its order.
  const out: TurnItem[] = [mergedItem]
  for (let i = 0; i < items.length; i++) {
    if (!drop.has(i)) out.push(items[i])
  }
  return out
}

/* Row keys, by message identity.
 *
 * The keys for a turn's rows come from `uniqueRowKeys` — the transcript's own
 * `row-<identity>` / `grp-<identity>` scheme with its collision tie-break — fed
 * by `useStableMessageKey`, the ONE spelling of the per-message identity rule
 * (`clientTs` → `ts` → a minted id) every virtualized host keys rows with. So a
 * turn's rows are keyed exactly as the transcript keys the turn, and a change to
 * that rule cannot de-sync the two.
 *
 * Why identity and not an index: a `single`'s `idx` and a `group`'s `startIdx`
 * are array positions in the transcript, and a history backfill (older page
 * landing) renumbers every one of them while leaving message identities intact
 * — so an index-keyed wrapper would remount every row below the prepend, an
 * inline MCP App iframe among them. The mapped position is worse still:
 * `mergeTurnThinking` hoists a later reasoning burst above earlier rows on the
 * next flush. Every render path keys visible rows through these, in the
 * streaming and the folded render alike (#11083).
 *
 * The minted-id fallback only ever applies to a message with neither timestamp.
 * `mergeTurnThinking`'s merged row spreads its first burst, so it carries that
 * burst's `ts` and keys on it; a timestamp-less burst would mint a new id per
 * flush (a fresh object each time) and re-key that one collapsed row, never an
 * app row, which is always a tool message with a `ts`. */

/** Collapsible agent turn. collapseAll=false (default): only tool calls collapse. collapseAll=true: all working steps collapse, only final assistant text visible.
 *
 *  ``appToolCallIds``: tool_call_ids in THIS pane's slot that have a live MCP
 *  App render payload. Those rows carry an interactive iframe and must never
 *  fold into the collapse (see isMcpAppItem). Passed in as a prop rather than
 *  read from Redux so this component stays store-free — app-sdk/ChatMessageList
 *  renders it for ChatEmbed with no Provider mounted, and a pane must scope the
 *  set to its OWN session key, not the globally-active slot. A host that passes
 *  no set is still safe: an app-bearing pill reports itself live (rowActivity).
 *
 *  ``mayBeLive``: a host hint that a row may still report itself live, so a
 *  closed settled fold keeps its content mounted (ChatPage: a pill the user
 *  opened, read from its own disclosure map). Without it such a row folds
 *  after a remount, as it always did.
 */
function TurnBlock({ turn, renderItem, collapseAll = false, appToolCallIds = EMPTY_ID_SET, disclosure, disclosureKey, onDisclosureChange, mayBeLive = NEVER_LIVE }: { turn: Extract<DisplayItem, {kind:'turn'}>; renderItem: (item: TurnItem, i: number) => ReactNode; collapseAll?: boolean; appToolCallIds?: ReadonlySet<string>; disclosure?: boolean; disclosureKey?: string; onDisclosureChange?: (key: string, expanded: boolean) => void; mayBeLive?: (it: TurnItem) => boolean }) {
  // memo() bails out of the provider-level language repaint, so this component
  // subscribes to language generation itself: its i18nT() strings must
  // re-translate even when no prop moves.
  useLanguageGeneration()

  // ONE disclosure for the whole life of the turn, running and settled alike.
  // Folded is the default in both phases; `true` opens the fold, whichever
  // rows it holds right now. It is HOST-OWNED when the host passes
  // `disclosure`, because the transcript is virtualised and row-local state
  // dies on every unmount. Deriving the view from this one value at render
  // time is what keeps a choice across completion, a resume and a remount,
  // with no effect that corrects the view a frame late.
  const [localOpen, setLocalOpen] = useState(false)
  const open = disclosure ?? localOpen
  const setOpen = useCallback((next: boolean) => {
    if (onDisclosureChange && disclosureKey !== undefined) onDisclosureChange(disclosureKey, next)
    else setLocalOpen(next)
  }, [onDisclosureChange, disclosureKey])

  // Fold the turn's reasoning bursts into ONE `thinking` row (see
  // mergeTurnThinking). Every render path below reads THIS list, not
  // turn.items, so a running turn shows one live reasoning line and a settled
  // turn shows one collapsed "Thought process" instead of a per-burst wall.
  const items = useMemo(() => mergeTurnThinking(turn.items), [turn.items])
  const answeredRefusalRows = useMemo(() => findAnsweredRefusalRows(items), [items])
  // One React key per row of `items`, by message identity (see the block
  // comment above); `rowKey(i)` indexes THIS list.
  const msgKey = useStableMessageKey()
  const keys = useMemo(() => uniqueRowKeys(items, msgKey), [items, msgKey])
  const rowKey = (i: number): string => keys[i] ?? `pos-${i}`

  // Live rows. A row's content reports itself through RowActivityContext
  // (running, awaiting approval, hosting an MCP App, opened by the user), and
  // a row holding keyboard focus is live too. Counted per key, since a group
  // row can hold several reporters.
  const [activeKeys, setActiveKeys] = useState<ReadonlySet<string>>(NO_ACTIVE_KEYS)
  const activeCounts = useRef(new Map<string, number>())
  const reporters = useRef(new Map<string, RowActivityReporter>())
  const reporterFor = (k: string): RowActivityReporter => {
    let r = reporters.current.get(k)
    if (!r) {
      r = () => {
        const n = (activeCounts.current.get(k) ?? 0) + 1
        activeCounts.current.set(k, n)
        if (n === 1) setActiveKeys(s => new Set(s).add(k))
        return () => {
          const m = (activeCounts.current.get(k) ?? 1) - 1
          if (m > 0) { activeCounts.current.set(k, m); return }
          activeCounts.current.delete(k)
          setActiveKeys(s => { if (!s.has(k)) return s; const next = new Set(s); next.delete(k); return next })
        }
      }
      reporters.current.set(k, r)
    }
    return r
  }
  // The row holding keyboard focus, read from document focus events: rows
  // are siblings with no shared parent element, and a fold under the focused
  // element would drop focus to <body>.
  const [focusedKey, setFocusedKey] = useState<string | null>(null)
  const keysRef = useRef(keys)
  keysRef.current = keys
  useEffect(() => {
    const onFocusIn = (e: FocusEvent) => {
      const k = (e.target as Element | null)?.closest?.('[data-fold-row]')?.getAttribute('data-fold-row') ?? null
      setFocusedKey(k !== null && keysRef.current.includes(k) ? k : null)
    }
    // A null relatedTarget with the document unfocused means focus left the
    // page (keep the row); with the document focused it moved to nothing here.
    const onFocusOut = (e: FocusEvent) => { if ((!e.relatedTarget) && document.hasFocus()) setFocusedKey(null) }
    document.addEventListener('focusin', onFocusIn)
    document.addEventListener('focusout', onFocusOut)
    return () => {
      document.removeEventListener('focusin', onFocusIn)
      document.removeEventListener('focusout', onFocusOut)
    }
  }, [])

  // The fold rule, one for both phases: the candidates (foldCandidates) minus
  // every live row. A row that is running, awaiting approval, hosting an app,
  // opened by the user or focused never folds, whatever the disclosure says.
  const mode: FoldMode = turn.interim ? 'interim' : collapseAll ? 'collapseAll' : 'default'
  const candidates = useMemo(
    () => foldCandidates(items, appToolCallIds, mode, turn.complete, answeredRefusalRows),
    [items, appToolCallIds, mode, turn.complete, answeredRefusalRows],
  )
  const fold = useMemo(() => {
    const rows = new Set<number>()
    let first = -1
    for (const i of candidates) {
      const k = keys[i] ?? `pos-${i}`
      if (activeKeys.has(k)) continue
      // The toggle's place ignores focus: tabbing from a row onto the toggle
      // must not move (and so remount) the toggle that just took focus.
      if (first < 0) first = i
      if (k !== focusedKey) rows.add(i)
    }
    return { rows: rows as ReadonlySet<number>, first: (rows.size > 0 || open) ? first : -1 }
  }, [candidates, keys, activeKeys, focusedKey, open])

  // A search match inside the fold opens it: a folded row is not rendered
  // visibly, so its <mark> would be invisible. A match in a row already on
  // screen leaves the fold alone, or opening it would push the match down.
  const { term, currentMessageIdx } = useSearchHighlight()
  const matchInFold = useMemo(() => {
    if (!term || currentMessageIdx < 0) return false
    for (const i of fold.rows) {
      const it = items[i]
      if (it.kind === 'single' ? it.idx === currentMessageIdx
        : it.kind === 'group' && currentMessageIdx >= it.startIdx && currentMessageIdx < it.startIdx + it.msgs.length) return true
    }
    return false
  }, [items, term, currentMessageIdx, fold.rows])
  // Held in a ref so an inline parent callback cannot re-fire this effect.
  const setOpenRef = useRef(setOpen)
  setOpenRef.current = setOpen
  useEffect(() => { if (matchInFold) setOpenRef.current(true) }, [matchInFold])

  const candidateSet = useMemo(() => new Set(candidates), [candidates])
  const count = fold.rows.size
  // "Earlier" only holds while nothing the fold skipped sits above the toggle.
  // A live row older than the folded ones keeps its place there, so the
  // running label falls back to the neutral tool-call count.
  const liveAboveFold = fold.first > 0 && candidates.some(i => i < fold.first)
  const label = !turn.complete && !liveAboveFold
    ? (open ? i18nT('pages.chat.turnBlock.hide_earlier_steps') : i18nT('pages.chat.turnBlock.earlier_step', { count }))
    : mode === 'default' || !turn.complete
      ? (open ? i18nT('pages.chat.turnBlock.hide_tool_calls') : i18nT('pages.chat.collapsibleToolGroup.tool_call', { count }))
      : (open ? i18nT('pages.chat.thinkingBlock.hide_reasoning') : i18nT('pages.chat.turnBlock.worked_through_step', { count }))

  // ONE render for every mode and both phases. Each row is a plain keyed
  // <div> in one flat list, so a fold, an unfold, the completion flip, a
  // reorder or a history backfill never changes a row's element, parent or
  // key (the row-identity rule: a re-parented row remounts, and a remounted
  // MCP App iframe reloads and loses its state). Folding collapses that same
  // element's one grid track to 0fr (with `inert`), so a row folding out of
  // the running window or a click on the toggle eases height and opacity
  // instead of jumping; reduced motion cuts straight to the end state. Only a
  // fold candidate clips its content, so no other row's overflow changes.
  // The toggle sits right above the first folded row and is
  // keyed by that row, so when it changes place it is a new element, never
  // a moved one: moving a keyed sibling makes React re-insert the rows it
  // passes, and a re-inserted iframe reloads too.
  const out: ReactNode[] = []
  items.forEach((it, i) => {
    const k = rowKey(i)
    if (i === fold.first) {
      out.push(<CollapseToggle key={`fold-${k}`} expanded={open} onToggle={() => setOpen(!open)} label={label} />)
    }
    const inFold = fold.rows.has(i)
    const folded = inFold && !open
    out.push(
      <div
        key={k}
        // Same marker the bubble-vanish probe reads: a folded row stays mounted.
        data-collapsed={folded ? 'true' : undefined}
        aria-hidden={folded || undefined}
        {...(folded ? { inert: '' } : {})}
        data-fold-row={k}
        className={`grid grid-cols-[minmax(0,1fr)] transition-[grid-template-rows,opacity] duration-200 ease-out motion-reduce:transition-none ${
          folded ? 'grid-rows-[0fr] opacity-0'
          // An open fold's rows keep the dimmed rail the folded section always
          // had, drawn on the row itself at the content edge.
          : inFold ? 'grid-rows-[1fr] relative opacity-60 before:absolute before:inset-y-0 before:w-[2px] before:bg-[var(--border)] forced-colors:before:bg-[CanvasText] before:left-[var(--fold-rail-left)]'
          : 'grid-rows-[1fr]'}`}
        // The rail sits at the content column's left edge.
        style={inFold && open ? { '--fold-rail-left': 'max(0px, calc((100% - var(--mc-content-width, 900px)) / 2))' } as CSSProperties : undefined}
      >
        {/* A folded row keeps its content mounted while it could still go
            live: only the content can say so. A closed settled default-mode
            fold drops the content of the rows that cannot (as that fold always
            did, since a settled pill costs store reads on every update): not
            running (a live row is never in the fold, and a settled row cannot
            start running again), no MCP App on the message, and not one the
            host knows may be live (a pill the user opened). */}
        <div className={candidateSet.has(i) ? 'min-h-0 min-w-0 overflow-hidden' : 'min-h-0 min-w-0'}>
          {folded && turn.complete && mode === 'default' && !hadMcpApp(it) && !mayBeLive(it)
            ? null
            : <RowActivityContext.Provider value={reporterFor(k)}>{renderItem(it, i)}</RowActivityContext.Provider>}
        </div>
      </div>,
    )
  })
  return <>{out}</>
}

function CollapseToggle({ expanded, onToggle, label }: { expanded: boolean; onToggle: () => void; label: string }) {
  return (
    <div className="px-4 py-0 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      {/* The visible label IS the accessible name ("Worked through 2 steps" folded,
          "Hide reasoning" open), so no aria-label; aria-expanded carries the state,
          which a rotated chevron alone never announces. */}
      <button aria-expanded={expanded} className="flex items-center gap-2 text-[12px] leading-5 text-muted/60 hover:text-muted cursor-pointer bg-transparent border-none py-1 transition-colors" onClick={onToggle}>
        <ChevronRight size={12} aria-hidden="true" className={`transition-transform duration-150 motion-reduce:transition-none ${expanded ? 'rotate-90' : ''}`} />
        {label}
      </button>
    </div>
  )
}

// Memoized so settled turns bail out entirely when the grouping's structural
// sharing (createTurnGrouper) hands back identical `turn` references across
// streaming flushes. The bail-out only holds when the host also passes stable
// renderItem/onDisclosureChange props — ChatPage hoists both for exactly this.
export default memo(TurnBlock)
