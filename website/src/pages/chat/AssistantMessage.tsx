import { useState, useMemo, useEffect, memo, useRef } from 'react'
import { motion } from 'framer-motion'
import { Copy, Check, Volume2, Code, ClipboardList, CheckCircle, RefreshCw, ChevronLeft, ChevronRight, GitFork, Loader2, Link2, Compass } from 'lucide-react'
import { copyToClipboard } from '../../utils/clipboard'
import { copySessionLink } from '../../utils/shareUrl'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import SelectionToolbar, { useSelectionActions } from '../../components/SelectionToolbar'
import { useSearchHighlight, useCurrentOcc } from '../../hooks/SearchHighlightContext'
import { applySearchHighlights } from '../../utils/domHighlight'
import { scrollCurrentMatchIntoView } from '../../utils/searchScroll'
import FileChangeChips, { type FileChangeEntry } from '../../components/FileChangeChips'
import type { FileChipStyle } from './ChatSettings'
import { loadChatConfig } from './ChatSettings'
import { useSmoothStream } from '../../hooks/useSmoothStream'
import type { PlanStepInput } from '../../api/client'

// Match an [OPTION:] / [OPTIONS:] marker anywhere on a single line. We deliberately
// do NOT anchor to end-of-string. The model frequently appends a closing line after
// the marker — a follow-up question, a note, or an auto-inserted comment — and an
// end-anchored regex then fails to match, leaving the raw "[OPTION: …]" text visible
// with no buttons (the regression this fixes). The body is single-line ([^\]\n]) so
// it can't swallow following paragraphs. Global so we can take the LAST marker, which
// is the actionable gate. Capture group 1 = the optional trailing "S" (multi syntax).
const OPTION_MARKER_RE = /\[OPTION(S)?:\s*([^\]\n]+?)\s*\]/gi
const PLAN_HEADER_RE = /📋\s*Plan for:/i
const STAGE_RE = /^Stage\s+\d+\s*:/m

export function parseOptions(content: string): { text: string; options: string[]; multi: boolean; isPlan: boolean } {
  let last: RegExpMatchArray | null = null
  for (const m of content.matchAll(OPTION_MARKER_RE)) last = m
  if (!last || last.index === undefined) return { text: content, options: [], multi: true, isPlan: false }
  const multi = !!last[1] // [OPTIONS:] is the multi-select syntax; [OPTION:] is single
  const sep = last[2].includes('|') ? '|' : ','
  const options = last[2].split(sep).map(o => o.trim()).filter(Boolean)
  const isPlan = PLAN_HEADER_RE.test(content) && STAGE_RE.test(content)
  // Strip ALL markers from the displayed text (not just the last) so a stray earlier
  // marker can't leak as raw "[OPTION: …]" syntax to the user; options still come from
  // the LAST marker (computed above). OPTION_MARKER_RE is global, so replace removes
  // every occurrence while preserving the prose around them.
  const text = content.replace(OPTION_MARKER_RE, '').trim()
  return { text, options, multi, isPlan }
}

// kiro-cli emits a steering acknowledgment inline in the model's output when it
// consumes a mid-turn steer: `[STEERING steer-<id>: <what it did in response>]`.
// Showing that raw marker is ugly; instead we pull it out and render it as a
// distinct "Steered" chip (mirrors KiRoom's stripSteeringTag display-parity).
// The id part is `steer-<hex>` (no ']' or ':'); the summary is non-greedy up to
// the first ']' (matching KiRoom's behavior — a literal ']' inside a summary ends
// it early, which producers avoid).
const STEER_ACK_RE = /\[STEERING\s+steer-[^\]:]+:\s*([\s\S]*?)\]/g

export function extractSteeringAcks(content: string): { cleaned: string; acks: string[] } {
  const acks: string[] = []
  const cleaned = content.replace(STEER_ACK_RE, (_m, summary) => {
    const s = String(summary).trim()
    if (s) acks.push(s)
    return ''
  })
  // Collapse the blank line the removed marker leaves behind.
  return { cleaned: cleaned.replace(/\n{3,}/g, '\n\n').trimEnd(), acks }
}

// A compact "Steered" chip rendered in place of the raw [STEERING …] marker.
function SteerAckChip({ summary }: { summary: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, ease: 'easeOut' }}
      className="mt-2 inline-flex flex-col items-start rounded-lg bg-accent-subtle px-2.5 py-1.5 text-[12px] leading-snug max-w-full"
    >
      <span className="inline-flex items-center gap-1.5 text-accent">
        <Compass size={13} className="shrink-0" />
        <span className="font-semibold">Steered</span>
      </span>
      {summary ? <span className="text-text ml-[19px] mt-0.5">{summary}</span> : null}
    </motion.div>
  )
}

const AssistantMessage = memo(function AssistantMessage({ content, isStreaming, onFileOpen, planTaskId, onApplyPlan, slotRunning, onSpeak, timestamp, showFooter = true, onRegenerate, variants, variantIdx, onSwitchVariant, isRegenerating, onFork, onPlanFromHere, forkIndex, onQuote, messageTs, slotKey, slotTitle, mode, fileChanges, onOpenDiff, fileChipStyle }: { content: string; isStreaming: boolean; onFileOpen?: (path: string) => void; planTaskId?: string; onApplyPlan?: (steps: PlanStepInput[]) => Promise<boolean>; slotRunning?: boolean; onSpeak?: () => void; timestamp?: string; showFooter?: boolean; onRegenerate?: () => void; variants?: { content: string; ts?: string }[]; variantIdx?: number; onSwitchVariant?: (index: number) => void; isRegenerating?: boolean; onFork?: (index: number) => void | Promise<void>; onPlanFromHere?: (index: number) => void | Promise<void>; forkIndex?: number; onQuote?: (text: string, rect: DOMRect) => void; messageTs?: string; slotKey?: string; slotTitle?: string; mode?: string; fileChanges?: FileChangeEntry[]; onOpenDiff?: (path: string, modified: string, original: string) => void; fileChipStyle?: FileChipStyle }) {
  const [applied, setApplied] = useState(false)
  const [copied, setCopied] = useState(false)
  const [linkCopied, setLinkCopied] = useState(false)
  const [busyAction, setBusyAction] = useState<'fork' | 'plan' | null>(null)
  const [rawMode, setRawMode] = useState(false)
  const [localIdx, setLocalIdx] = useState<number | null>(null)
  useEffect(() => { setLocalIdx(null) }, [content, variants?.length])

  const hasVariants = variants && variants.length > 1
  const activeIdx = onSwitchVariant ? (typeof variantIdx === 'number' ? variantIdx : (variants?.length ?? 1) - 1) : (localIdx ?? (typeof variantIdx === 'number' ? variantIdx : (variants?.length ?? 1) - 1))
  const effectiveContent = hasVariants && localIdx !== null && !onSwitchVariant ? (variants[localIdx]?.content ?? content) : content
  // Reset the "Applied to Tasks" flag only when the message content changes.
  // `applied` is intentionally omitted: including it would re-run this effect
  // the instant `applied` flips to true and immediately clear it, making the
  // Applied state impossible to reach. setApplied is stable.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { if (applied) setApplied(false) }, [effectiveContent])
  const { text } = parseOptions(effectiveContent)
  // Pull kiro-cli's [STEERING …] acknowledgments out of the prose; render them as
  // chips instead of raw markers. Feed the cleaned text (marker removed) to the
  // stream so the raw tag never renders.
  const { cleaned: steerCleaned, acks: steerAcks } = useMemo(() => extractSteeringAcks(text), [text])
  const [smooth] = useState(() => loadChatConfig().streamMode !== 'immediate')
  const speed = 4 // force high speed smooth streaming to avoid lagging behind raw model output
  const smoothedText = useSmoothStream(steerCleaned, isStreaming, smooth, speed)

  const planSteps = useMemo<PlanStepInput[] | null>(() => {
    if (isStreaming || !planTaskId || !effectiveContent) return null
    const jsonMatch = effectiveContent.match(/```json\s*\n([\s\S]*?)\n```/)
    if (!jsonMatch) return null
    try {
      const parsed: unknown = JSON.parse(jsonMatch[1])
      if (!Array.isArray(parsed) || !parsed.length) return null
      const valid = parsed.every((s: unknown) => {
        const step = s as { title?: unknown; depends_on?: unknown }
        return typeof step?.title === 'string' && step.title.trim() &&
          (!step.depends_on || (Array.isArray(step.depends_on) && step.depends_on.every((d: unknown) => typeof d === 'number')))
      })
      return valid ? (parsed as PlanStepInput[]) : null
    } catch {}
    return null
  }, [effectiveContent, isStreaming, planTaskId])

  const contentRef = useRef<HTMLDivElement>(null)
  const selectionActions = useSelectionActions(onQuote)

  const { term, caseSensitive } = useSearchHighlight()
  const currentOcc = useCurrentOcc()

  useEffect(() => {
    const el = contentRef.current
    if (!el) return

    const run = () => applySearchHighlights(el, term, caseSensitive, currentOcc)
    run()
    // After highlighting, center the active occurrence so a jump lands on the
    // exact searched text. Converges across frames so a far (just-mounted,
    // unmeasured) row still lands correctly on the first click — see
    // scrollCurrentMatchIntoView. Capture its cancel so the loop is aborted
    // when this effect re-runs (next occurrence) or the message unmounts —
    // otherwise rapid navigation piles up concurrent loops + window listeners.
    const cancelScroll = currentOcc >= 0 ? scrollCurrentMatchIntoView(el) : undefined

    // Code blocks use dangerouslySetInnerHTML — hljs runs in a child
    // useEffect and sets innerHTML asynchronously after this effect.
    // A MutationObserver catches those deferred DOM updates and re-runs
    // the TreeWalker so code block content gets highlighted too.
    //
    // The observer also fires when our own applySearchHighlights mutates
    // the DOM (inserting <mark> elements). To prevent an infinite loop:
    // 1. Disconnect the observer before running the TreeWalker
    // 2. Re-observe after the TreeWalker finishes
    // 3. Batch rapid mutations via requestAnimationFrame + a scheduled flag
    //
    // Performance: the observer fires on any subtree mutation (React
    // re-renders, hljs updates, our own marks). Each firing runs one
    // TreeWalker pass which is sub-millisecond even for long messages,
    // so the extra runs are negligible.
    if (!term) return () => cancelScroll?.()
    let disposed = false
    let scheduled = false
    const observer = new MutationObserver(() => {
      if (scheduled) return
      scheduled = true
      requestAnimationFrame(() => {
        scheduled = false
        if (disposed) return
        observer.disconnect()
        run()
        observer.observe(el, { childList: true, subtree: true, characterData: true })
      })
    })
    observer.observe(el, { childList: true, subtree: true, characterData: true })
    return () => { disposed = true; observer.disconnect(); cancelScroll?.() }
  }, [term, caseSensitive, currentOcc, effectiveContent, rawMode])

  return <div data-role="assistant" className="group/msg">
    <div ref={contentRef} className="msg-content group/bubble relative text-sm leading-relaxed text-text overflow-hidden" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
      <MessageErrorBoundary rawContent={smoothedText}>
        <MarkdownRenderer content={smoothedText} streaming={isStreaming} onFileOpen={onFileOpen} rawMode={rawMode} messageTs={messageTs} glow={isStreaming} smooth={smooth} />
      </MessageErrorBoundary>
      {/* Render the steer ack the moment kiro-cli emits the [STEERING …] marker
          — including mid-stream — so the user sees the agent acknowledge the
          steer live, not only after the whole turn finishes. */}
      {steerAcks.length > 0 && (
        <div className="flex flex-col items-start gap-1 mb-2">
          {steerAcks.map((a, i) => <SteerAckChip key={i} summary={a} />)}
        </div>
      )}
      {!isStreaming && selectionActions.length > 0 && <SelectionToolbar containerRef={contentRef} actions={selectionActions} />}
    </div>
    {fileChanges && fileChanges.length > 0 && !isStreaming && (
      <FileChangeChips fileChanges={fileChanges} onOpenDiff={onOpenDiff} style={fileChipStyle} />
    )}
    {!isStreaming && showFooter && (
      <div className="flex items-center gap-1 mt-0.5 opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300">
        {timestamp && <span className="text-muted text-[12px] font-mono mr-1.5">{timestamp}</span>}
        <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Copy" aria-label={copied ? 'Copied!' : 'Copy'} onClick={() => { copyToClipboard(steerCleaned).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) }).catch(() => {}) }}>{copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}</button>
        {messageTs && slotKey && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Copy link to message" aria-label="Copy link to message" onClick={() => { copySessionLink(slotKey, slotTitle, messageTs, mode).then(() => { setLinkCopied(true); setTimeout(() => setLinkCopied(false), 1500) }).catch(() => {}) }}>{linkCopied ? <Check size={14} className="text-ok" /> : <Link2 size={14} />}</button>}
        {onFork && forkIndex !== undefined && <button className="text-muted hover:text-text p-0.5 rounded transition-colors disabled:opacity-50" disabled={busyAction !== null} title="Fork conversation from here" aria-label="Fork conversation from here" onClick={async () => { setBusyAction('fork'); try { await onFork(forkIndex) } finally { setBusyAction(null) } }}>{busyAction === 'fork' ? <Loader2 size={14} className="animate-spin" /> : <GitFork size={14} />}</button>}
        {onPlanFromHere && forkIndex !== undefined && <button className="text-muted hover:text-text p-0.5 rounded transition-colors disabled:opacity-50" disabled={busyAction !== null} title="Plan from here" aria-label="Plan from here" onClick={async () => { setBusyAction('plan'); try { await onPlanFromHere(forkIndex) } finally { setBusyAction(null) } }}>{busyAction === 'plan' ? <Loader2 size={14} className="animate-spin" /> : <ClipboardList size={14} />}</button>}
        {text.length >= 50 && onSpeak && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Speak" aria-label="Speak message" onClick={onSpeak}><Volume2 size={14} /></button>}
        {text.length > 20 && <button className={`p-0.5 rounded transition-colors flex items-center gap-0.5 text-[11px] ${rawMode ? 'text-text' : 'text-muted hover:text-text'}`} title={rawMode ? 'Rendered view' : 'Raw markdown'} aria-label={rawMode ? 'Switch to rendered view' : 'Switch to raw markdown view'} onClick={() => setRawMode(!rawMode)}><Code size={14} />{rawMode ? 'rendered' : 'raw'}</button>}
        {onRegenerate && !slotRunning && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title="Regenerate" aria-label="Regenerate response" onClick={onRegenerate}><RefreshCw size={14} /></button>}
        {hasVariants && (() => {
          const curIdx = activeIdx
          const switchFn = onSwitchVariant || ((i: number) => setLocalIdx(i))
          return (
            <div className="flex items-center gap-0.5 ml-1 text-[11px] text-muted">
              <button className="hover:text-text p-0.5 rounded transition-colors disabled:opacity-30 disabled:cursor-default cursor-pointer" title="Previous version" aria-label="Previous version" disabled={curIdx <= 0 || !!slotRunning} onClick={() => switchFn(curIdx - 1)}><ChevronLeft size={14} /></button>
              <span className="font-mono">{curIdx + 1}/{variants!.length}</span>
              <button className="hover:text-text p-0.5 rounded transition-colors disabled:opacity-30 disabled:cursor-default cursor-pointer" title="Next version" aria-label="Next version" disabled={curIdx >= variants!.length - 1 || !!slotRunning} onClick={() => switchFn(curIdx + 1)}><ChevronRight size={14} /></button>
            </div>
          )
        })()}
      </div>
    )}
    {planSteps && onApplyPlan && !applied && !isRegenerating && (
      <button className="mt-1 px-3 py-1.5 rounded-md text-[13px] font-medium border border-accent text-accent bg-transparent cursor-pointer hover:bg-accent hover:text-accent-fg transition-all" onClick={async () => { const ok = await onApplyPlan(planSteps); if (ok) setApplied(true) }}>
        <ClipboardList className="lucide-inline" /> Use as Plan ({planSteps.length} steps)
      </button>
    )}
    {applied && <div className="mt-1 text-[13px] text-ok"><CheckCircle className="lucide-inline" /> Applied to Tasks</div>}
  </div>
})

export default AssistantMessage
