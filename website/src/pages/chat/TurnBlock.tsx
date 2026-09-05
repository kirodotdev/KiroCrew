import { memo, useState, useRef, useEffect, useMemo, useCallback, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight } from 'lucide-react'
import type { DisplayItem, TurnItem } from './types'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import { useSearchHighlight } from '../../hooks/SearchHighlightContext'
import { isWorkflowRunTool } from './WorkflowRunCard'
import { isSpawnRunTool } from './SubagentRunCard'
import { isWorkflowCompletionMessage } from './WorkflowCompletionCard'
import { isSubagentCompletionMessage } from './subagentCompletion'
import { isReasoningBurst } from './groupDisplayItems'
import { isDiffToolMessage } from './toolDiff'
import { stripOptionMarkers } from '../../app-sdk/protocol/optionMarker'
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
const isTool = (it: TurnItem, appToolCallIds: ReadonlySet<string>) =>
  it.kind === 'single' && it.msg.role === 'tool' && !isWorkflowRunItem(it) &&
  !isSpawnRunItem(it) && !isMcpAppItem(it, appToolCallIds) && !isDiffCardItem(it)
const isHiddenTool = (it: TurnItem) => it.kind === 'single' && it.msg.role === 'tool' && !it.msg.content.startsWith('🔧')
const isConclusion = (it: TurnItem) => it.kind === 'single' && (it.msg.role === 'assistant' || it.msg.role === 'streaming' || it.msg.role === 'file')
/**
 * "Always visible" items — must render inline regardless of TurnBlock collapse state.
 * mcp_oauth: user must always see the Authorize button to act on it.
 * error: errors should never be hidden behind a "Worked through N steps" toggle.
 */
const isAlwaysVisible = (it: TurnItem) => it.kind === 'single' && (it.msg.role === 'mcp_oauth' || it.msg.role === 'error')

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
 * Probed via `stripOptionMarkers`, which removes BOTH marker kinds. Keying on the
 * content marker alone left an ACTION-only hand-back unrecognised, so the row it
 * ends was buried in the collapse pane — the exact burial this predicate exists to
 * prevent. That helper also keeps the g-flag `lastIndex` hazard inside the protocol
 * module, so there is no `.test()`/`.exec()` footgun to remember here.
 */
function hasOptionsMarker(text: string): boolean {
  return stripOptionMarkers(text) !== text
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
 * A crew-mode answer: a forwarded topic result, a meta render, or a question
 * back to the user. Crew Mode breaks this component's central assumption —
 * that the LAST assistant message of a turn is the conclusion and the earlier
 * ones are reasoning. There, each forward is the FINAL answer for a different
 * topic, so collapsing all but the last hides answers the user asked for.
 * Keyed on the persisted marker class rather than the live-only `kind`, so it
 * still holds after a reload.
 */
const isCrewReply = (it: TurnItem) =>
  it.kind === 'single' && isConclusion(it) &&
  // `meta.crew_reply` is the durable signal: the periodic slot flush keeps `meta`
  // for every role but keeps `cls` only for role === 'system', so a class-only
  // marker was dropped on the main persistence path. The class check stays as a
  // fallback for rows written before the marker moved, and for the live frame.
  (it.msg.meta?.crew_reply === true || /(^|\s)crew-reply(\s|$)/.test(it.msg.cls || ''))

/** A renderable assistant message (widget/image), a mid-turn hand-back
 *  ([OPTIONS:] marker), a keep-visible-marked deliverable (#7948), a crew-mode
 *  answer, a role that must surface inline (mcp_oauth, error), a workflow_run /
 *  spawn_run / workflow-completion / sub-agent-completion card, or an MCP
 *  App-bearing tool call (interactive iframe anchored to the row). All
 *  bypass the collapse pane. */
const isVisibleInline = (it: TurnItem, appToolCallIds: ReadonlySet<string>) =>
  isRenderable(it) || isHandBack(it) || isKeepVisible(it) || isAlwaysVisible(it) || isCrewReply(it) ||
  isWorkflowRunItem(it) || isSpawnRunItem(it) ||
  isSubagentCompletionItem(it) ||
  isWorkflowCompletionItem(it) || isMcpAppItem(it, appToolCallIds) ||
  isDiffCardItem(it)

/** One ordered run of the collapse split: a contiguous run of items that hide
 *  behind the toggle, or a single item that must render in place. */
type Seg =
  | { type: 'collapsed'; items: { it: TurnItem; idx: number }[] }
  | { type: 'visible'; it: TurnItem; idx: number }

/**
 * Split items into ordered segments: contiguous "collapsed" runs interleaved
 * with items that must render in place (widgets/images, hand-backs, crew
 * replies, mcp_oauth/error rows, workflow_run / spawn_run / completion cards,
 * MCP-App tool rows, diff cards — see isVisibleInline).
 *
 * ONE definition, shared by the collapseAll split and the interim-fan-out fold,
 * so "what may never be hidden behind a toggle" cannot drift between them.
 * `idx` is the item's index in the caller's list, offset by `offset` when the
 * caller passes a slice.
 */
function splitSegments(items: TurnItem[], appToolCallIds: ReadonlySet<string>, offset = 0): Seg[] {
  const segs: Seg[] = []
  for (let i = 0; i < items.length; i++) {
    const it = items[i]
    if (isVisibleInline(it, appToolCallIds)) {
      segs.push({ type: 'visible', it, idx: offset + i })
    } else {
      const last = segs[segs.length - 1]
      if (last?.type === 'collapsed') last.items.push({ it, idx: offset + i })
      else segs.push({ type: 'collapsed', items: [{ it, idx: offset + i }] })
    }
  }
  return segs
}

/** Steps a collapse toggle can honestly claim to be hiding. */
const countCollapsedSteps = (segs: Seg[]): number =>
  segs.flatMap(s => s.type === 'collapsed' ? s.items : []).filter(({ it }) => !isHiddenTool(it)).length

/** Stable empty set so the mcpApps selector returns a referentially-equal
 *  value when the slot has no app renders (avoids useless re-renders). */
const EMPTY_ID_SET: ReadonlySet<string> = new Set()

/** Strip BOTH marker kinds plus markdown formatting and return plain text length.
 *  Both kinds, because this feeds the >= 50-char conclusion test: counting an action
 *  marker's own label as substance is how a row whose only content is a marker reads
 *  as a substantive conclusion. */
function substantiveLength(text: string): number {
  return stripOptionMarkers(text).replace(/[#*_`>\-|]/g, '').trim().length
}

/**
 * Find the index of a turn's conclusion item: the last `isConclusion` item that
 * is substantive (>= 50 chars), falling back to the last `isConclusion` item of
 * any length, else -1. Shared by the auto-expand decision and the render split
 * so the "what's the always-visible conclusion vs collapsed reasoning" answer
 * can't drift between them (a mismatch wrongly expands reasoning above a visible
 * match and pushes it down).
 */
function findConclusionIdx(items: TurnItem[]): number {
  let conclusionIdx = -1
  let fallbackIdx = -1
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i]
    if (isConclusion(it)) {
      if (fallbackIdx === -1) fallbackIdx = i
      if (it.kind === 'single' && substantiveLength(it.msg.content) >= 50) { conclusionIdx = i; break }
    }
  }
  return conclusionIdx === -1 ? fallbackIdx : conclusionIdx
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

/** Collapsible agent turn. collapseAll=false (default): only tool calls collapse. collapseAll=true: all working steps collapse, only final assistant text visible.
 *
 *  ``appToolCallIds``: tool_call_ids in THIS pane's slot that have a live MCP
 *  App render payload. Those rows carry an interactive iframe and must never
 *  fold into the collapse (see isMcpAppItem). Passed in as a prop rather than
 *  read from Redux so this component stays store-free — app-sdk/ChatMessageList
 *  renders it for ChatEmbed with no Provider mounted, and a pane must scope the
 *  set to its OWN session key, not the globally-active slot.
 */
function TurnBlock({ turn, renderItem, collapseAll = false, appToolCallIds = EMPTY_ID_SET, disclosure, disclosureKey, onDisclosureChange }: { turn: Extract<DisplayItem, {kind:'turn'}>; renderItem: (item: TurnItem, i: number) => ReactNode; collapseAll?: boolean; appToolCallIds?: ReadonlySet<string>; disclosure?: boolean; disclosureKey?: string; onDisclosureChange?: (key: string, expanded: boolean) => void }) {
  // memo() bails out of the provider-level language repaint, so this component
  // subscribes to language generation itself: its i18nT() strings must
  // re-translate even when no prop moves.
  useLanguageGeneration()
  const [localExpanded, setLocalExpanded] = useState(!turn.complete)
  // Disclosure is HOST-OWNED when `disclosure` is supplied, and that is what
  // makes an explicit choice durable: the transcript is virtualised, so this
  // row is unmounted whenever it leaves the mounted window and any state held
  // here dies with it. `undefined` means the user has not chosen yet, so the
  // local default below applies.
  const expanded = disclosure ?? localExpanded
  // An explicit click PINS the disclosure state, and the auto-collapse below
  // honours that pin. `turn.complete` is not a stable property of the turn: it
  // is derived from the slot's running flag, which ChatPage re-reconciles from
  // every slots broadcast, so a broadcast that catches the slot momentarily
  // idle between tool calls flips it true mid-turn. The pin is what keeps an
  // incidental transport-level event from overriding a deliberate user
  // gesture. CollapsibleToolGroup pins its own auto-collapse the same way.
  const userToggled = useRef(false)
  const toggle = useCallback(() => {
    userToggled.current = true
    const next = !expanded
    if (onDisclosureChange && disclosureKey !== undefined) onDisclosureChange(disclosureKey, next)
    else setLocalExpanded(next)
  }, [expanded, onDisclosureChange, disclosureKey])
  const wasComplete = useRef(turn.complete)
  useEffect(() => {
    if (turn.complete && !wasComplete.current && !userToggled.current) setLocalExpanded(false)
    wasComplete.current = turn.complete
  }, [turn.complete])

  // Fold the turn's reasoning bursts into ONE `thinking` row (see
  // mergeTurnThinking). Every render path below reads THIS list, not
  // turn.items, so a running turn shows one live reasoning line and a settled
  // turn shows one collapsed "Thought process" instead of a per-burst wall.
  const items = useMemo(() => mergeTurnThinking(turn.items), [turn.items])

  // Auto-expand only when the active search match lives inside a COLLAPSED
  // segment of this turn — collapsed reasoning is mounted but height-0, so the
  // match's <mark> would be invisible. Crucially we must NOT expand when the
  // match is in the always-visible conclusion / inline items: expanding the
  // reasoning above would shove the (already-visible) match down out of view.
  const { term, currentMessageIdx } = useSearchHighlight()
  const matchInCollapsedSegment = useMemo(() => {
    if (!term || currentMessageIdx < 0) return false
    // Default mode only collapses tool calls, which are never search matches —
    // but an interim fan-out turn folds its prose in BOTH modes, so it has to be
    // checked before that bail-out or a match inside it stays height-0.
    if (!collapseAll && !turn.interim) return false
    const msgIdxs = (it: TurnItem): number[] =>
      it.kind === 'single'
        ? [it.idx]
        : it.kind === 'group'
          ? Array.from({ length: it.msgs.length }, (_, k) => it.startIdx + k)
          : []
    // Mirror the render's conclusion-finding so we know which items are the
    // (always-visible) conclusion vs the collapsible pre-conclusion reasoning.
    // An interim turn has no conclusion carve-out: every non-visible-inline
    // item of it is collapsed.
    const conclusionIdx = turn.interim ? -1 : findConclusionIdx(items)
    const beforeItems = turn.interim ? items : (conclusionIdx > 0 ? items.slice(0, conclusionIdx) : [])
    // Only the non-visible-inline pre-conclusion items are actually collapsed.
    return beforeItems.some(it => !isVisibleInline(it, appToolCallIds) && msgIdxs(it).includes(currentMessageIdx))
  }, [items, term, currentMessageIdx, collapseAll, appToolCallIds, turn.interim])
  // Revealing a search match must win over the current disclosure state, and it
  // has to travel the SAME channel the host owns, or a controlled row would
  // stay collapsed and hide the <mark>. Held in a ref so an inline parent
  // callback cannot re-fire this effect on every render.
  const onDisclosureChangeRef = useRef(onDisclosureChange)
  onDisclosureChangeRef.current = onDisclosureChange
  const disclosureKeyRef = useRef(disclosureKey)
  disclosureKeyRef.current = disclosureKey
  useEffect(() => {
    if (!matchInCollapsedSegment) return
    const notify = onDisclosureChangeRef.current
    if (notify && disclosureKeyRef.current !== undefined) notify(disclosureKeyRef.current, true)
    else setLocalExpanded(true)
  }, [matchInCollapsedSegment])

  // Interim fan-out region: everything the agent emitted between the user's
  // prompt and the synthesis turn that restates it (see `interim` in types.ts).
  // Folded in BOTH modes and with no conclusion carve-out — the region's last
  // assistant message is a per-completion summary, which is exactly the row the
  // conclusion rule would have kept visible. `isVisibleInline` still holds, so
  // the spawn_run card, the completion cards and any error stay in place: the
  // reader keeps the record that a wave ran, without the prose.
  if (turn.interim) {
    const segs = splitSegments(items, appToolCallIds)
    const stepCount = countCollapsedSteps(segs)
    if (!turn.complete || stepCount === 0) {
      return <>{items.map((it, i) => renderItem(it, i))}</>
    }
    return (
      <>
        <CollapseToggle expanded={expanded} onToggle={toggle}
          label={expanded ? i18nT('pages.chat.thinkingBlock.hide_reasoning') : i18nT('pages.chat.turnBlock.worked_through_step', { count: stepCount })} />
        {segs.map((seg, si) => seg.type === 'visible' ? (
          <div key={`v-${si}`}>{renderItem(seg.it, seg.idx)}</div>
        ) : (
          <CollapsibleSection key={`c-${si}`} expanded={expanded}>
            {seg.items.map(({ it, idx }) => renderItem(it, idx))}
          </CollapsibleSection>
        ))}
      </>
    )
  }

  // collapseAll mode: collapse everything except the last assistant message (original behavior)
  if (collapseAll) {
    // Find last substantive assistant message as conclusion (skip weak ones like bare OPTIONS)
    const conclusionIdx = findConclusionIdx(items)
    const conclusion = conclusionIdx >= 0 ? items[conclusionIdx] : null
    const after = conclusionIdx >= 0 ? items.slice(conclusionIdx + 1) : items
    const beforeItems = conclusionIdx > 0 ? items.slice(0, conclusionIdx) : []

    // Split pre-conclusion items into ordered segments (see splitSegments):
    // visible items render in place; collapsed runs hide behind the reasoning
    // toggle.
    const segs = splitSegments(beforeItems, appToolCallIds)
    const stepCount = countCollapsedSteps(segs)

    if (!turn.complete || stepCount === 0) {
      return <>{items.map((it, i) => renderItem(it, i))}</>
    }

    return (
      <>
        <CollapseToggle expanded={expanded} onToggle={toggle}
          label={expanded ? i18nT('pages.chat.thinkingBlock.hide_reasoning') : i18nT('pages.chat.turnBlock.worked_through_step', { count: stepCount })} />
        {segs.map((seg, si) => seg.type === 'visible' ? (
          <div key={`v-${si}`}>{renderItem(seg.it, seg.idx)}</div>
        ) : (
          <CollapsibleSection key={`c-${si}`} expanded={expanded}>
            {seg.items.map(({ it, idx }) => renderItem(it, idx))}
          </CollapsibleSection>
        ))}
        {conclusion && renderItem(conclusion, conclusionIdx)}
        {after.map((it, i) => renderItem(it, conclusionIdx + 1 + i))}
      </>
    )
  }

  // Default: only collapse tool calls
  const toolCount = items.filter(it => isTool(it, appToolCallIds)).length
  if (!turn.complete || toolCount === 0) {
    return <>{items.map((it, i) => renderItem(it, i))}</>
  }

  type Segment = { type: 'tools'; items: { it: TurnItem; idx: number }[] } | { type: 'visible'; it: TurnItem; idx: number }
  const segments: Segment[] = []
  for (let i = 0; i < items.length; i++) {
    const it = items[i]
    if (isTool(it, appToolCallIds)) {
      const last = segments[segments.length - 1]
      if (last?.type === 'tools') last.items.push({ it, idx: i })
      else segments.push({ type: 'tools', items: [{ it, idx: i }] })
    } else {
      segments.push({ type: 'visible', it, idx: i })
    }
  }

  return (
    <>
      <CollapseToggle expanded={expanded} onToggle={toggle}
        label={expanded ? i18nT('pages.chat.turnBlock.hide_tool_calls') : i18nT('pages.chat.collapsibleToolGroup.tool_call', { count: toolCount })} />
      {segments.map((seg, si) => seg.type === 'visible' ? (
        <div key={si}>{renderItem(seg.it, seg.idx)}</div>
      ) : (
        <AnimatePresence key={si} initial={false}>
          {expanded && (
            <CollapsibleSection expanded={true}>
              {seg.items.map(({ it, idx }) => renderItem(it, idx))}
            </CollapsibleSection>
          )}
        </AnimatePresence>
      ))}
    </>
  )
}

function CollapseToggle({ expanded, onToggle, label }: { expanded: boolean; onToggle: () => void; label: string }) {
  return (
    <div className="px-4 py-0 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <button className="flex items-center gap-2 text-[12px] leading-5 text-muted/60 hover:text-muted cursor-pointer bg-transparent border-none py-1 transition-colors" onClick={onToggle}>
        <ChevronRight size={12} className={`transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`} />
        {label}
      </button>
    </div>
  )
}

function CollapsibleSection({ expanded, children }: { expanded: boolean; children: ReactNode }) {
  return (
    <motion.div
      // Collapse marker for the bubble-vanish probe (useBubbleVanishProbe):
      // rows inside stay MOUNTED while the height animates to 0, so without
      // this attribute a collapse is indistinguishable from a windowing bug.
      // Present exactly while this section hides its mounted children, i.e.
      // when the interim / collapseAll folds pass expanded={false}. The
      // default-mode tool fold is different: it hard-codes expanded={true}
      // and collapses by UNMOUNTING under AnimatePresence, so it never sets
      // this marker — and moves no probe counter either, because tool items
      // carry no [data-display-index] of their own.
      data-collapsed={expanded ? undefined : 'true'}
      initial={{ height: 0, opacity: 0 }}
      animate={expanded ? { height: 'auto', opacity: 1 } : { height: 0, opacity: 0 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ height: { duration: 0.3, ease: [0.4, 0, 0.2, 1] }, opacity: { duration: 0.2 } }}
      style={{ overflow: 'hidden' }}
    >
      <div className="mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        <div className="shadow-[inset_2px_0_0_0_var(--border)] forced-colors:border-l-2 opacity-60">{children}</div>
      </div>
    </motion.div>
  )
}

// Memoized so settled turns bail out entirely when the grouping's structural
// sharing (createTurnGrouper) hands back identical `turn` references across
// streaming flushes. The bail-out only holds when the host also passes stable
// renderItem/onDisclosureChange props — ChatPage hoists both for exactly this.
export default memo(TurnBlock)
