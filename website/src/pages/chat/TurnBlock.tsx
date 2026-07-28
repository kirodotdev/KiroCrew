import { useState, useRef, useEffect, useMemo, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight } from 'lucide-react'
import type { DisplayItem, TurnItem } from './types'
import { useSearchHighlight } from '../../hooks/SearchHighlightContext'
import { isWorkflowRunTool } from './WorkflowRunCard'
import { isSpawnRunTool } from './SubagentRunCard'
import { isWorkflowCompletionMessage } from './WorkflowCompletionCard'
import { OPTION_MARKER_RE } from '../../utils/optionsMarker'

// A workflow_run launch renders as its own always-visible inline card
// (WorkflowRunCard), so it must never be folded into the collapsible tool-call
// group — treat it as a non-tool, always-visible item.
const isWorkflowRunItem = (it: TurnItem) =>
  it.kind === 'single' && it.msg.role === 'tool' && isWorkflowRunTool(it.msg)
// Same for a spawn_run launch (SubagentRunCard): folding it into "Worked
// through N steps" is precisely what left a spawned wave with no visible
// record in scrollback.
const isSpawnRunItem = (it: TurnItem) =>
  it.kind === 'single' && it.msg.role === 'tool' && isSpawnRunTool(it.msg)
// A workflow completion event renders as its own compact card and must stay
// visible even when a turn's reasoning is collapsed (collapseAll mode).
const isWorkflowCompletionItem = (it: TurnItem) =>
  it.kind === 'single' && isWorkflowCompletionMessage(it.msg)
const isTool = (it: TurnItem) =>
  it.kind === 'single' && it.msg.role === 'tool' && !isWorkflowRunItem(it) && !isSpawnRunItem(it)
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

/** Either a renderable assistant message (widget/image) or a role that must
 *  surface inline (mcp_oauth, error), or a workflow_run launch card. All bypass
 *  the collapse pane. */
const isVisibleInline = (it: TurnItem) => isRenderable(it) || isAlwaysVisible(it) || isWorkflowRunItem(it) || isSpawnRunItem(it) || isWorkflowCompletionItem(it)

/** Strip OPTIONS/markdown formatting and return plain text content length */
function substantiveLength(text: string): number {
  return text.replace(OPTION_MARKER_RE, '').replace(/[#*_`>\-|]/g, '').trim().length
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

/** Collapsible agent turn. collapseAll=false (default): only tool calls collapse. collapseAll=true: all working steps collapse, only final assistant text visible. */
export default function TurnBlock({ turn, renderItem, collapseAll = false }: { turn: Extract<DisplayItem, {kind:'turn'}>; renderItem: (item: TurnItem, i: number) => ReactNode; collapseAll?: boolean }) {
  const [expanded, setExpanded] = useState(!turn.complete)
  const wasComplete = useRef(turn.complete)
  useEffect(() => {
    if (turn.complete && !wasComplete.current) setExpanded(false)
    wasComplete.current = turn.complete
  }, [turn.complete])

  // Auto-expand only when the active search match lives inside a COLLAPSED
  // segment of this turn — collapsed reasoning is mounted but height-0, so the
  // match's <mark> would be invisible. Crucially we must NOT expand when the
  // match is in the always-visible conclusion / inline items: expanding the
  // reasoning above would shove the (already-visible) match down out of view.
  const { term, currentMessageIdx } = useSearchHighlight()
  const matchInCollapsedSegment = useMemo(() => {
    if (!term || currentMessageIdx < 0) return false
    // Default mode only collapses tool calls, which are never search matches.
    if (!collapseAll) return false
    const msgIdxs = (it: TurnItem): number[] =>
      it.kind === 'single'
        ? [it.idx]
        : it.kind === 'group'
          ? Array.from({ length: it.msgs.length }, (_, k) => it.startIdx + k)
          : []
    // Mirror the render's conclusion-finding so we know which items are the
    // (always-visible) conclusion vs the collapsible pre-conclusion reasoning.
    const conclusionIdx = findConclusionIdx(turn.items)
    const beforeItems = conclusionIdx > 0 ? turn.items.slice(0, conclusionIdx) : []
    // Only the non-visible-inline pre-conclusion items are actually collapsed.
    return beforeItems.some(it => !isVisibleInline(it) && msgIdxs(it).includes(currentMessageIdx))
  }, [turn.items, term, currentMessageIdx, collapseAll])
  useEffect(() => {
    if (matchInCollapsedSegment) setExpanded(true)
  }, [matchInCollapsedSegment])

  // collapseAll mode: collapse everything except the last assistant message (original behavior)
  if (collapseAll) {
    // Find last substantive assistant message as conclusion (skip weak ones like bare OPTIONS)
    const conclusionIdx = findConclusionIdx(turn.items)
    const conclusion = conclusionIdx >= 0 ? turn.items[conclusionIdx] : null
    const after = conclusionIdx >= 0 ? turn.items.slice(conclusionIdx + 1) : turn.items
    const beforeItems = conclusionIdx > 0 ? turn.items.slice(0, conclusionIdx) : []

    // Split pre-conclusion items into ordered segments: contiguous "collapsed"
    // runs (tool calls + non-renderable assistant text) interleaved with
    // "visible" items (assistant text containing widgets/images, plus
    // mcp_oauth/error rows). Visible items render in place; collapsed runs
    // hide behind the reasoning toggle.
    type Seg = { type: 'collapsed'; items: { it: TurnItem; idx: number }[] } | { type: 'visible'; it: TurnItem; idx: number }
    const segs: Seg[] = []
    for (let i = 0; i < beforeItems.length; i++) {
      const it = beforeItems[i]
      if (isVisibleInline(it)) {
        segs.push({ type: 'visible', it, idx: i })
      } else {
        const last = segs[segs.length - 1]
        if (last?.type === 'collapsed') last.items.push({ it, idx: i })
        else segs.push({ type: 'collapsed', items: [{ it, idx: i }] })
      }
    }
    const stepCount = segs
      .flatMap(s => s.type === 'collapsed' ? s.items : [])
      .filter(({ it }) => !isHiddenTool(it))
      .length

    if (!turn.complete || stepCount === 0) {
      return <>{turn.items.map((it, i) => renderItem(it, i))}</>
    }

    return (
      <>
        <CollapseToggle expanded={expanded} onToggle={() => setExpanded(e => !e)}
          label={expanded ? 'Hide reasoning' : `Worked through ${stepCount} step${stepCount !== 1 ? 's' : ''}`} />
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
  const toolCount = turn.items.filter(isTool).length
  if (!turn.complete || toolCount === 0) {
    return <>{turn.items.map((it, i) => renderItem(it, i))}</>
  }

  type Segment = { type: 'tools'; items: { it: TurnItem; idx: number }[] } | { type: 'visible'; it: TurnItem; idx: number }
  const segments: Segment[] = []
  for (let i = 0; i < turn.items.length; i++) {
    const it = turn.items[i]
    if (isTool(it)) {
      const last = segments[segments.length - 1]
      if (last?.type === 'tools') last.items.push({ it, idx: i })
      else segments.push({ type: 'tools', items: [{ it, idx: i }] })
    } else {
      segments.push({ type: 'visible', it, idx: i })
    }
  }

  return (
    <>
      <CollapseToggle expanded={expanded} onToggle={() => setExpanded(e => !e)}
        label={expanded ? 'Hide tool calls' : `${toolCount} tool call${toolCount !== 1 ? 's' : ''}`} />
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
    <div className="px-5 py-0 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <button className="flex items-center gap-1.5 text-[12px] text-muted/60 hover:text-muted cursor-pointer bg-transparent border-none py-1 transition-colors" onClick={onToggle}>
        <ChevronRight size={12} className={`transition-transform duration-150 ${expanded ? 'rotate-90' : ''}`} />
        {label}
      </button>
    </div>
  )
}

function CollapsibleSection({ expanded, children }: { expanded: boolean; children: ReactNode }) {
  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={expanded ? { height: 'auto', opacity: 1 } : { height: 0, opacity: 0 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ height: { duration: 0.3, ease: [0.4, 0, 0.2, 1] }, opacity: { duration: 0.2 } }}
      style={{ overflow: 'hidden' }}
    >
      <div className="px-5 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        <div className="border-l-2 border-l-border opacity-60">{children}</div>
      </div>
    </motion.div>
  )
}
