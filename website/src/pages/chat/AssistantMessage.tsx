import { useState, useMemo, useEffect, memo, useRef } from 'react'
import { motion } from 'framer-motion'
import { Copy, Check, Volume2, Code, ClipboardList, CheckCircle, RefreshCw, ChevronLeft, ChevronRight, GitFork, Loader2, Link2, Compass, Clock } from 'lucide-react'
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
import { OPTION_MARKER_RE, stripPartialOptionMarker } from '../../utils/optionsMarker'
import { i18nT } from '../../i18n/t'
import { fmtCurrency, fmtDuration, fmtNumber, fmtUnit } from '../../i18n/format'
const PLAN_HEADER_RE = /📋\s*Plan for:/i
const STAGE_RE = /^Stage\s+\d+\s*:/m

/** Per-turn stats attached by the backend to the last assistant message of a
 *  completed turn (chat_runner._attach_turn_stats). Parity with the end-of-turn
 *  line kiro-cli prints natively: elapsed wall clock + credits (kiro) or
 *  API cost (claude_code). Zero fields are omitted by the backend. */
export interface TurnStats { elapsed_ms: number; credits?: number; cost_usd?: number }

/** "8.4s" under 10s, "42s" under a minute, "2m 34s" beyond. */
export function fmtTurnElapsed(ms: number): string {
  const s = ms / 1000
  if (s < 10) return fmtUnit(s, 'second', { maximumFractionDigits: 1, minimumFractionDigits: 1 })
  if (s < 60) return fmtUnit(Math.round(s), 'second', { maximumFractionDigits: 0 })
  // Round to whole seconds FIRST, then split into minutes + remainder so a value
  // like 119.6s renders "2m 0s", never the invalid "1m 60s" (flooring minutes
  // before rounding seconds can push the remainder to 60).
  const total = Math.round(s)
  return fmtDuration([[Math.floor(total / 60), 'minute'], [total % 60, 'second']])
}

/** Trim credit noise: 2 decimals under 10, 1 decimal beyond ("0.25", "12.5"). */
export function fmtCredits(c: number): string {
  // Precision rule unchanged; only the decimal separator becomes locale-aware
  // (de/fr/ru want `0,25`). Both bounds are pinned so trailing zeros survive.
  const digits = c >= 10 ? 1 : 2
  return fmtNumber(c, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

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
        <span className="font-semibold">{i18nT('pages.chat.assistantMessage.steered')}</span>
      </span>
      {summary ? <span className="text-text ml-[19px] mt-0.5">{summary}</span> : null}
    </motion.div>
  )
}

const AssistantMessage = memo(function AssistantMessage({ content, isStreaming, onFileOpen, onFolderOpen, onArtifactOpen, planTaskId, onApplyPlan, slotRunning, onSpeak, timestamp, timestampTitle, showFooter = true, onRegenerate, variants, variantIdx, onSwitchVariant, isRegenerating, onFork, onPlanFromHere, forkIndex, onQuote, onAsk, messageTs, slotKey, slotTitle, mode, fileChanges, onOpenDiff, fileChipStyle, artifactPaths, turnStats, linkPreviews }: { content: string; isStreaming: boolean; onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void; onFolderOpen?: (path: string) => void; onArtifactOpen?: (slug: string) => void; planTaskId?: string; onApplyPlan?: (steps: PlanStepInput[]) => Promise<boolean>; slotRunning?: boolean; onSpeak?: (content: string) => void; timestamp?: string; timestampTitle?: string; showFooter?: boolean; onRegenerate?: () => void; variants?: { content: string; ts?: string }[]; variantIdx?: number; onSwitchVariant?: (index: number) => void; isRegenerating?: boolean; onFork?: (index: number) => void | Promise<void>; onPlanFromHere?: (index: number) => void | Promise<void>; forkIndex?: number; onQuote?: (text: string, rect: DOMRect) => void; onAsk?: (text: string, rect: DOMRect) => void; messageTs?: string; slotKey?: string; slotTitle?: string; mode?: string; fileChanges?: FileChangeEntry[]; onOpenDiff?: (path: string, modified: string, original: string) => void; fileChipStyle?: FileChipStyle; artifactPaths?: Set<string>; turnStats?: TurnStats; linkPreviews?: boolean }) {
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
  const { text: parsedText } = parseOptions(effectiveContent)
  // While the marker line is still arriving it has no closing `]`, so
  // OPTION_MARKER_RE can't match it yet and the raw `[OPTIONS: …` would type
  // itself out as prose before flipping to pills at turn end. Suppress the
  // growing tail — streaming only, so a finished message still renders an
  // unterminated marker (prose about the syntax, or a truncated turn) as written.
  const text = isStreaming ? stripPartialOptionMarker(parsedText) : parsedText
  // Pull kiro-cli's [STEERING …] acknowledgments out of the prose; render them as
  // chips instead of raw markers. Feed the cleaned text (marker removed) to the
  // stream so the raw tag never renders.
  const { cleaned: steerCleaned, acks: steerAcks } = useMemo(() => extractSteeringAcks(text), [text])
  const [smooth] = useState(() => loadChatConfig().streamMode !== 'immediate')
  // 1x = ~0.4s constant lag behind the live edge (see useSmoothStream's
  // LAG_SECS). The constant-latency controller bounds the lag for ANY model
  // speed; a higher multiplier only shrinks the smoothing window — 4x cuts it
  // to ~0.1s, smaller than typical inter-burst gaps, so the reveal starves
  // between bursts and reads as chunky.
  const speed = 1
  const smoothedText = useSmoothStream(steerCleaned, isStreaming, smooth, speed)
  // The smooth buffer keeps draining for ~LAG_SECS AFTER isStreaming flips false
  // (see useSmoothStream's continuation condition), so for a beat the rendered
  // text is still truncated — possibly mid-URL. MarkdownRenderer's own `live`
  // gate only covers isStreaming, so suppress unfurl for the drain window too:
  // a half-revealed `https://exa` must not be fetched just because the turn
  // ended. Chips/cards appear once the reveal catches up.
  const draining = smoothedText.length < steerCleaned.length

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
  const selectionActions = useSelectionActions(onQuote, onAsk)

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

  // Four whole-sentence keys, one per combination of the two optional clauses,
  // rather than a base sentence with ` and used …` / ` (… API cost)` appended.
  // A translator handed those two fragments cannot place them: the credit clause
  // and the cost parenthetical bind to different parts of the sentence in other
  // languages, and several put the duration last. Interpolated values are
  // already locale-formatted by the `format.ts` seam.
  const turnStatsTitle = (() => {
    if (!turnStats) return undefined
    const elapsed = fmtTurnElapsed(turnStats.elapsed_ms)
    const hasCredits = (turnStats.credits ?? 0) > 0
    const hasCost = (turnStats.cost_usd ?? 0) > 0
    const credits = hasCredits ? fmtCredits(turnStats.credits!) : ''
    const cost = hasCost
      ? fmtCurrency(turnStats.cost_usd!, 'USD', { maximumFractionDigits: 4, minimumFractionDigits: 4 })
      : ''
    if (hasCredits && hasCost) return i18nT('pages.chat.assistantMessage.turn_took_credits_cost', { elapsed, credits, cost })
    if (hasCredits) return i18nT('pages.chat.assistantMessage.turn_took_credits', { elapsed, credits })
    if (hasCost) return i18nT('pages.chat.assistantMessage.turn_took_cost', { elapsed, cost })
    return i18nT('pages.chat.assistantMessage.turn_took', { elapsed })
  })()

  return <div data-role="assistant" className="group/msg">
    {/* 'message-bubble' is a stable theming hook — see website/docs/theming-contract.md */}
    <div ref={contentRef} className="message-bubble msg-content group/bubble relative text-sm leading-relaxed text-text overflow-hidden" style={{ overflowWrap: 'anywhere', wordBreak: 'break-word' }}>
      <MessageErrorBoundary rawContent={smoothedText}>
        <MarkdownRenderer content={smoothedText} streaming={isStreaming} onFileOpen={onFileOpen} onFolderOpen={onFolderOpen} onArtifactOpen={onArtifactOpen} rawMode={rawMode} messageTs={messageTs} slotKey={slotKey} glow={isStreaming} smooth={smooth} linkPreviews={linkPreviews && !draining} />
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
      <FileChangeChips fileChanges={fileChanges} onOpenDiff={onOpenDiff} style={fileChipStyle} artifactPaths={artifactPaths} disclosureKey={messageTs ? `fcc-${messageTs}` : undefined} />
    )}
    {!isStreaming && showFooter && turnStats && turnStats.elapsed_ms > 0 && (
      /* No `font-mono`: "1.98 credits · 59s" is a labelled measurement, not
         code, and Tailwind's `font-mono` pins `var(--mono)` — a token the Font
         Family setting never writes, so this line ignored the user's choice.
         `tabular-nums` stays: fixed-width digits are what the mono was earning
         here, and it works in a proportional face too. */
      <div className="flex items-center gap-1 mt-1 text-[11px] text-muted/60 tabular-nums" data-testid="turn-stats" title={turnStatsTitle}>
        {/* Cost leads, elapsed trails: credits are the scarce resource users
            actually budget, so they read first. The clock icon travels WITH the
            elapsed value (never leads the line) so it never appears to label
            the credit figure. */}
        {(() => {
          const credits = turnStats.credits ?? 0
          const cost = turnStats.cost_usd ?? 0
          const billed = credits > 0
            ? `${fmtCredits(credits)} credits`
            : cost > 0 ? `$${cost.toFixed(cost < 0.01 ? 4 : 2)}` : ''
          return <>
            {billed && <span>{billed} ·</span>}
            <Clock size={11} aria-hidden="true" />
            <span>{fmtTurnElapsed(turnStats.elapsed_ms)}</span>
          </>
        })()}
      </div>
    )}
    {!isStreaming && showFooter && (
      <div className="flex items-center gap-1 mt-0.5 opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300 [@media(hover:none)]:opacity-100">
        {/* No `font-mono`: a formatted date is prose, and Tailwind's `font-mono`
            pins `var(--mono)` — a token the Font Family setting never writes, so
            it overrode the user's choice and put JetBrains Mono (no CJK
            coverage) under a date that a zh/ja dashboard renders WITH CJK
            characters. `tabular-nums` keeps digits fixed-width, which is the
            alignment the mono was actually there for — and it holds the action
            row below at the same x across messages. */}
        {timestamp && <span className="text-muted text-[12px] tabular-nums mr-1.5" title={timestampTitle}>{timestamp}</span>}
        <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title={i18nT('pages.chat.assistantMessage.copy')} aria-label={copied ? i18nT('pages.chat.assistantMessage.copied') : i18nT('pages.chat.assistantMessage.copy')} onClick={() => { copyToClipboard(steerCleaned).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500) }).catch(() => {}) }}>{copied ? <Check size={14} className="text-ok" /> : <Copy size={14} />}</button>
        {messageTs && slotKey && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title={i18nT('pages.chat.assistantMessage.copy_link_to_message')} aria-label={i18nT('pages.chat.assistantMessage.copy_link_to_message')} onClick={() => { copySessionLink(slotKey, slotTitle, messageTs, mode).then(() => { setLinkCopied(true); setTimeout(() => setLinkCopied(false), 1500) }).catch(() => {}) }}>{linkCopied ? <Check size={14} className="text-ok" /> : <Link2 size={14} />}</button>}
        {onFork && forkIndex !== undefined && <button className="text-muted hover:text-text p-0.5 rounded transition-colors disabled:opacity-50" disabled={busyAction !== null} title={i18nT('pages.chat.assistantMessage.fork_conversation_from_here')} aria-label={i18nT('pages.chat.assistantMessage.fork_conversation_from_here')} onClick={async () => { setBusyAction('fork'); try { await onFork(forkIndex) } finally { setBusyAction(null) } }}>{busyAction === 'fork' ? <Loader2 size={14} className="animate-spin" /> : <GitFork size={14} />}</button>}
        {onPlanFromHere && forkIndex !== undefined && <button className="text-muted hover:text-text p-0.5 rounded transition-colors disabled:opacity-50" disabled={busyAction !== null} title={i18nT('pages.chat.assistantMessage.plan_from_here')} aria-label={i18nT('pages.chat.assistantMessage.plan_from_here')} onClick={async () => { setBusyAction('plan'); try { await onPlanFromHere(forkIndex) } finally { setBusyAction(null) } }}>{busyAction === 'plan' ? <Loader2 size={14} className="animate-spin" /> : <ClipboardList size={14} />}</button>}
        {text.length >= 50 && onSpeak && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title={i18nT('pages.chat.assistantMessage.speak')} aria-label={i18nT('pages.chat.assistantMessage.speak_message')} onClick={() => onSpeak(content)}><Volume2 size={14} /></button>}
        {text.length > 20 && <button className={`p-0.5 rounded transition-colors flex items-center gap-0.5 text-[11px] ${rawMode ? 'text-text' : 'text-muted hover:text-text'}`} title={rawMode ? i18nT('pages.chat.assistantMessage.rendered_view') : i18nT('pages.chat.assistantMessage.raw_markdown')} aria-label={rawMode ? i18nT('pages.chat.assistantMessage.switch_to_rendered_view') : i18nT('pages.chat.assistantMessage.switch_to_raw_markdown_view')} onClick={() => setRawMode(!rawMode)}><Code size={14} />{rawMode ? i18nT('pages.chat.assistantMessage.rendered') : i18nT('pages.chat.assistantMessage.raw')}</button>}
        {onRegenerate && !slotRunning && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title={i18nT('pages.chat.assistantMessage.regenerate')} aria-label={i18nT('pages.chat.assistantMessage.regenerate_response')} onClick={onRegenerate}><RefreshCw size={14} /></button>}
        {hasVariants && (() => {
          const curIdx = activeIdx
          const switchFn = onSwitchVariant || ((i: number) => setLocalIdx(i))
          return (
            <div className="flex items-center gap-0.5 ml-1 text-[11px] text-muted">
              <button className="hover:text-text p-0.5 rounded transition-colors disabled:opacity-30 disabled:cursor-default cursor-pointer" title={i18nT('pages.chat.assistantMessage.previous_version')} aria-label={i18nT('pages.chat.assistantMessage.previous_version')} disabled={curIdx <= 0 || !!slotRunning} onClick={() => switchFn(curIdx - 1)}><ChevronLeft size={14} /></button>
              {/* No `font-mono`, same as the timestamp two elements to the left:
                  "2/3" is a pagination counter, not code, and it sits in the
                  SAME hover row — leaving it on `var(--mono)` would have made
                  half of one row follow the Font Family setting and half ignore
                  it. `tabular-nums` also stops the chevrons shifting when the
                  index crosses into two digits. */}
              <span className="tabular-nums">{curIdx + 1}/{variants!.length}</span>
              <button className="hover:text-text p-0.5 rounded transition-colors disabled:opacity-30 disabled:cursor-default cursor-pointer" title={i18nT('pages.chat.assistantMessage.next_version')} aria-label={i18nT('pages.chat.assistantMessage.next_version')} disabled={curIdx >= variants!.length - 1 || !!slotRunning} onClick={() => switchFn(curIdx + 1)}><ChevronRight size={14} /></button>
            </div>
          )
        })()}
      </div>
    )}
    {planSteps && onApplyPlan && !applied && !isRegenerating && (
      <button className="mt-1 px-3 py-1.5 rounded-md text-[13px] font-medium border border-accent text-accent bg-transparent cursor-pointer hover:bg-accent hover:text-accent-fg transition-all" onClick={async () => { const ok = await onApplyPlan(planSteps); if (ok) setApplied(true) }}>
        <ClipboardList className="lucide-inline" /> {i18nT('pages.chat.assistantMessage.use_as_plan_count', { count: planSteps.length })}
      </button>
    )}
    {applied && <div className="mt-1 text-[13px] text-ok"><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.assistantMessage.applied_to_tasks')}</div>}
  </div>
})

export default AssistantMessage
