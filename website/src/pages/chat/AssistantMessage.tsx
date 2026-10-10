import { useState, useMemo, useEffect, memo, useRef, useId, type ReactNode } from 'react'
import { motion } from 'framer-motion'
import { Copy, ClipboardType, Check, Volume2, Code, Eye, ClipboardList, CheckCircle, RefreshCw, ChevronLeft, ChevronRight, GitFork, Loader2, Link2, Compass, Clock, MessageSquare, Pin, PinOff, MoreHorizontal, Share2, X, Quote } from 'lucide-react'
import { lazy, Suspense } from 'react'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from '../../components/ui/dropdown-menu'
import MessageContextMenu, { type MessageMenuItem } from './MessageContextMenu'
import { copyRichToClipboard, copyToClipboard } from '../../utils/clipboard'
import { markdownToCleanHtml } from '../../components/markdown/richTextClipboard'
import { stripKeepVisibleMarker } from '../../app-sdk/protocol/keepVisibleMarker'
import { copySessionLink } from '../../utils/shareUrl'
import { ICON_ACTION_ROW_CLS } from '../../utils/touchActions'
import { isTouchDevice } from '../../utils/isTouchDevice'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import SelectionToolbar, { useSelectionActions } from '../../components/SelectionToolbar'
import { useSearchHighlight, useCurrentOcc } from '../../hooks/SearchHighlightContext'
import { applySearchHighlights, clearSearchHighlights } from '../../utils/domHighlight'
import { scrollCurrentMatchIntoView } from '../../utils/searchScroll'
import FileChangeChips, { type FileChangeEntry } from '../../components/FileChangeChips'
import DecisionStrip from './DecisionStrip'
import { readDecisionRecords, readMemoryRecallInStrip } from './decisionRecord'
import MemoryRecallStrip from './MemoryRecallStrip'
import type { FileChipStyle } from './ChatSettings'
import { loadChatConfig } from './ChatSettings'
import { useSmoothStream } from '../../hooks/useSmoothStream'
import type { PlanStepInput } from '../../api/client'
import { extractSteeringAcks, parseOptions, stripPartialOptionMarker } from '../../app-sdk/protocol'
import { i18nT } from '../../i18n/t'
import { ROUTING_PREFIX_RE } from '../../providers/modelRegistry'
import { fmtCredits, fmtCurrency, fmtDuration, fmtUnit } from '../../i18n/format'
import ErrorNotice from '../../components/ErrorNotice'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'

/** Per-turn stats attached by the backend to the last assistant message of a
 *  completed turn (chat_runner._attach_turn_stats). Parity with the end-of-turn
 *  line kiro-cli prints natively: elapsed wall clock + credits (kiro) or
 *  API cost (claude_code). Zero fields are omitted by the backend. */
export interface TurnStats { elapsed_ms: number; credits?: number; cost_usd?: number; model?: string }

/** Trim a served model id to a compact footer label: drop region/vendor
 *  routing prefixes ("global.anthropic.claude-opus-4-8[1m]" → "claude-opus-4-8[1m]").
 *  The full untrimmed id stays available in the footer tooltip, so this only
 *  affects the inline label. Unknown shapes pass through unchanged. Shares the
 *  one routing-prefix pattern with the registry fold (providers/modelRegistry.ts). */
export function fmtTurnModel(id: string): string {
  return id.replace(ROUTING_PREFIX_RE, '')
}

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

// A compact "Steered" chip rendered in place of the raw [STEERING …] marker.
// The marker's text after the colon is how the turn responded to the steer
// (`app-sdk/protocol/steering.ts`): often a real outcome ("Stopped at phase 4
// as requested"), sometimes the model's own reasoning ("this steer is the
// only request…"). So it is kept but folded behind the chip: shown only when
// the reader opens it, never dropped.
// `entrance` gates the fade-in to the STREAMING moment the chip first appears.
// A settled transcript's chip must render at its final state: framer replays
// `initial` on every MOUNT, and transcript rows legitimately remount (window
// shifts, regroups) — with the entrance unconditional, each remount replayed
// the fade and a parked reader saw the chip "blinking" (caught mid-fade in a
// screen recording at ~50% opacity).
function SteerAckChip({ summary, entrance }: { summary: string; entrance: boolean }) {
  const [open, setOpen] = useState(false)
  const summaryId = useId()
  const label = (
    <>
      <Compass size={13} className="shrink-0" aria-hidden="true" />
      <span className="font-semibold">{i18nT('pages.chat.assistantMessage.steered')}</span>
    </>
  )
  return (
    <motion.div
      initial={entrance ? { opacity: 0, y: 4 } : false}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, ease: 'easeOut' }}
      className="mt-2 inline-flex flex-col items-start rounded-lg bg-accent-subtle px-3 py-2 text-[12px] leading-5 max-w-full"
    >
      {summary
        ? (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={summaryId}
            onClick={() => setOpen(o => !o)}
            className="inline-flex items-center gap-2 text-accent rounded-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            data-testid="steer-ack-toggle"
          >
            {label}
            <ChevronRight size={12} className={`shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} aria-hidden="true" />
          </button>
        )
        : <span className="inline-flex items-center gap-2 text-accent">{label}</span>}
      {summary && open ? <span id={summaryId} className="text-text ml-6 mt-1" data-testid="steer-ack-summary">{summary}</span> : null}
    </motion.div>
  )
}

/** Shared by the fork row button below; identical to its base-branch class. */
const ROW_ACTION_CLS = 'text-muted hover:text-text p-0.5 rounded transition-colors disabled:opacity-50'

/** Loaded on first open: the share dialog pulls in html-to-image, which no
 *  chat render path should pay for before the user actually shares. */
const LazyShareMessageModal = lazy(() => import('./share/ShareMessageModal'))

/** The footer's hover-reveal + touch-target contract, shared by the action row and the
    unavailable fork affordance that sits outside it. */
const ACTIONS_REVEAL_CLS = `flex items-center gap-y-1 mt-1 opacity-0 transition-opacity duration-300 delay-100 group-hover/msg:opacity-100 group-hover/msg:delay-300 group-focus-within/msg:opacity-100 group-focus-within/msg:delay-300 ${ICON_ACTION_ROW_CLS}`

const AssistantMessage = memo(function AssistantMessage({ content, isStreaming, onFileOpen, onFolderOpen, onArtifactOpen, onSessionOpen, sessions, activeSession, planTaskId, onApplyPlan, slotRunning, onSpeak, timestamp, timestampTitle, showFooter = true, revealActions = false, onRegenerate, variants, variantIdx, onSwitchVariant, isRegenerating, onFork, forkIndex, forkMessageId, onLoadEarlier, loadingOlder, earlierRemaining, onQuote, onAsk, messageTs, slotKey, slotTitle, mode, fileChanges, fileChangesOmittedFiles, onOpenDiff, fileChipStyle, artifactPaths, turnStats, decisionsStrip, linkPreviews, pinned, onTogglePin, suppressSteerAck, prevUserText, shareEnabled = false, bubbleClassName, onReplyInThread, blockedLinks, redactions, showRedactionCoach = false, onQuoteMessage }: { content: string; isStreaming: boolean; onFileOpen?: (path: string, opts?: { line?: number; endLine?: number }) => void; onFolderOpen?: (path: string) => void; onArtifactOpen?: (slug: string) => void; onSessionOpen?: (key: string) => void; sessions?: ReadonlyMap<string, string>; activeSession?: string; planTaskId?: string; onApplyPlan?: (steps: PlanStepInput[]) => Promise<boolean>; slotRunning?: boolean; onSpeak?: (content: string) => void; timestamp?: string; timestampTitle?: string; showFooter?: boolean; revealActions?: boolean; onRegenerate?: () => void; variants?: { content: string; ts?: string; blocked_links?: unknown; redactions?: unknown }[]; variantIdx?: number; onSwitchVariant?: (index: number) => void; isRegenerating?: boolean; onFork?: (index: number, messageId?: string) => void | Promise<void>; forkIndex?: number; forkMessageId?: string; onLoadEarlier?: () => void; loadingOlder?: boolean; earlierRemaining?: number; onQuote?: (text: string, rect: DOMRect) => void; onAsk?: (text: string, rect: DOMRect) => void; messageTs?: string; slotKey?: string; slotTitle?: string; mode?: string; fileChanges?: FileChangeEntry[]; /** Raw `meta.file_changes_omitted_files`: files the turn's snapshot limits left out of `fileChanges`. Rendered only beside a non-empty `fileChanges`, which is the only way the gateway sends it. */ fileChangesOmittedFiles?: unknown; onOpenDiff?: (path: string, modified: string, original: string) => void; fileChipStyle?: FileChipStyle; artifactPaths?: Set<string>; turnStats?: TurnStats; /** Raw `decisions_strip` record off the message, validated here. Absent renders nothing. */ decisionsStrip?: unknown; linkPreviews?: boolean; pinned?: boolean; onTogglePin?: () => void; /** Drop the steer chip: this turn's steer was a system policy notice, not the user's. */ suppressSteerAck?: boolean; /** The user question this reply answered — enables the share card's Q&A pairing. */ prevUserText?: string; /** Governance answer from `/api/dashboard/config` (`social_share_enabled`). The host passes it explicitly; an absent prop hides Share, so a forgotten wire fails closed. */ shareEnabled?: boolean; /** Extra classes on the `.message-bubble` element — a host that draws the reply as its own framed bubble (a crewmate's chat, filled gray) passes its surface and corners here; the stable theming hook itself is untouched. */ bubbleClassName?: string; /** Open (or start) the reply thread on this message. Only a crewmate's chat offers it. */ onReplyInThread?: () => void; /** Raw `meta.blocked_links` off this message — the step-3 suspicious-URL records the blocked-link chip renders from. Handed straight to `MarkdownRenderer`, which validates the shape. */ blockedLinks?: unknown; /** Raw `meta.redactions`: one record per credential placeholder, validated by `MarkdownRenderer`. */ redactions?: unknown; /** This is the first reply in the session with a removed credential: show the one-time coach after it. */ showRedactionCoach?: boolean; /** Stage this whole reply as the quote of the next send. Offered: the row reads seat + Copy + More, where the seat is Quote unless Reply in thread, Regenerate or Fork holds it (the raw toggle, Copy link and Pin move into More); it also arms the bubble's right-click / long-press menu (Quote first). Absent: row and bubble unchanged. Receives the text this bubble is SHOWING (the locally browsed variant when there is one), so the quote is what the reader sees. */ onQuoteMessage?: (shownContent: string) => void }) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const [applied, setApplied] = useState(false)
  // Successful Copy / Copy-link presses flash on the icon for 1.5s. Text-copy
  // refusal persists in an ErrorNotice below; link-copy retains its established
  // compact feedback because this change does not touch that action.
  type CopyOutcome = 'idle' | 'ok' | 'failed'
  const [copied, setCopied] = useState<CopyOutcome>('idle')
  const [linkCopied, setLinkCopied] = useState<CopyOutcome>('idle')
  const [richCopied, setRichCopied] = useState<CopyOutcome>('idle')
  // Which format the row icon's current tick is for, so its label names it.
  const [rowCopiedRich, setRowCopiedRich] = useState(false)
  const [copyFailed, setCopyFailed] = useState(false)
  const [overflowOpen, setOverflowOpen] = useState(false)
  const flashCopy = (set: (v: CopyOutcome) => void) => (ok: boolean) => {
    set(ok ? 'ok' : 'failed')
    setTimeout(() => set('idle'), 1500)
  }
  const copyOutcomeIcon = (state: CopyOutcome, idle: ReactNode) =>
    state === 'ok' ? <Check size={14} className="text-ok" />
      : state === 'failed' ? <X size={14} className="text-danger" />
        : idle
  const copyOutcomeLabel = (state: CopyOutcome, idle: string, ok = i18nT('pages.chat.assistantMessage.copied')) =>
    state === 'ok' ? ok
      : state === 'failed' ? i18nT('pages.chat.assistantMessage.copy_failed')
        : idle
  const [shareOpen, setShareOpen] = useState(false)
  const [busyAction, setBusyAction] = useState<'fork' | null>(null)
  // The disabled reason is VISIBLE text, so it needs an id to be referenced by
  // rather than a tooltip only a patient mouse can reach.
  const reasonId = useId()
  // `forkIndex === undefined` covers TWO states: older history remains, OR the cursor
  // still names the chat we left -- and only the first of those can actually page.
  const unavailableReason = !onLoadEarlier
    ? i18nT('pages.chat.assistantMessage.needs_active_chat')
    : typeof earlierRemaining === 'number' && earlierRemaining > 0
      ? i18nT('pages.chat.assistantMessage.needs_earlier_history_count', { count: earlierRemaining })
      : i18nT('pages.chat.assistantMessage.needs_earlier_history')
  const forkLabel = i18nT('pages.chat.assistantMessage.fork_conversation_from_here')
  const runForkAction = async () => {
    if (!onFork || forkIndex === undefined || busyAction !== null) return
    setBusyAction('fork')
    try {
      await (forkMessageId ? onFork(forkIndex, forkMessageId) : onFork(forkIndex))
    } finally {
      setBusyAction(null)
    }
  }
  // Stops on lack of PROGRESS, never a page cap: a cap false-reports distant but
  // reachable rows as unavailable, which is why the earlier one was removed.
  const [pagingToTarget, setPagingToTarget] = useState(false)
  const lastRemainingRef = useRef<number | null>(null)
  useEffect(() => {
    if (!pagingToTarget) { lastRemainingRef.current = null; return }
    if (forkIndex !== undefined || !onLoadEarlier) { setPagingToTarget(false); return }
    if (loadingOlder) return
    if (typeof earlierRemaining === 'number') {
      const prev = lastRemainingRef.current
      if (earlierRemaining <= 0 || (prev !== null && earlierRemaining >= prev)) {
        setPagingToTarget(false)
        return
      }
      lastRemainingRef.current = earlierRemaining
    }
    onLoadEarlier()
  }, [pagingToTarget, forkIndex, loadingOlder, earlierRemaining, onLoadEarlier])
  const [rawMode, setRawMode] = useState(false)
  // Entering raw view holds the bubble at the height the rendered view had, and
  // the source scrolls inside that box. Raw markdown wraps differently from its
  // rendering, so without this the footer row (and the toggle under the
  // pointer) jumped by the height difference on every flip. Freezing the height
  // also means the transcript virtualizer sees no row resize at all, so no
  // reprice or bottom re-pin fires. Cleared on the way back and while streaming.
  const [rawBoxHeight, setRawBoxHeight] = useState<number | null>(null)
  // The pin exists for the flip itself. A viewport resize or a content change
  // (variant switch, late edit) re-wraps the whole transcript anyway, so a
  // snapshot taken before either would leave a wrong-sized scroll box; release
  // it and let the raw view take its own height from then on.
  useEffect(() => {
    if (rawBoxHeight === null) return
    const release = () => setRawBoxHeight(null)
    window.addEventListener('resize', release)
    return () => window.removeEventListener('resize', release)
  }, [rawBoxHeight])
  useEffect(() => { setRawBoxHeight(null) }, [content, variantIdx])
  const [localIdx, setLocalIdx] = useState<number | null>(null)
  useEffect(() => { setLocalIdx(null) }, [content, variants?.length])

  const hasVariants = variants && variants.length > 1
  const activeIdx = onSwitchVariant ? (typeof variantIdx === 'number' ? variantIdx : (variants?.length ?? 1) - 1) : (localIdx ?? (typeof variantIdx === 'number' ? variantIdx : (variants?.length ?? 1) - 1))
  // The locally browsed variant, when the arrows move within an older row rather
  // than asking the server to switch. Text and blocked-link records come from the
  // SAME object: a record describes one text, so reading the row's records beside
  // a variant's content states a host that text never held.
  const localVariant = hasVariants && localIdx !== null && !onSwitchVariant ? variants[localIdx] : undefined
  const effectiveContent = localVariant ? (localVariant.content ?? content) : content
  const effectiveBlockedLinks = localVariant ? localVariant.blocked_links : blockedLinks
  const effectiveRedactions = localVariant ? localVariant.redactions : redactions
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
  const toggleRaw = () => {
    if (!rawMode) {
      // Fractional, not offsetHeight: a rounded integer moves the row by up to
      // half a pixel, which is exactly the jitter this exists to remove.
      const measured = contentRef.current?.getBoundingClientRect().height ?? 0
      setRawBoxHeight(measured > 0 ? measured : null)
    } else {
      setRawBoxHeight(null)
    }
    setRawMode(!rawMode)
  }
  const selectionActions = useSelectionActions(onQuote, onAsk)
  const touch = isTouchDevice()
  const toolbarActions = touch ? selectionActions.filter(a => a.id !== 'copy') : selectionActions

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

    // The highlights are Ranges registered on a page-wide CSS.highlights entry
    // (see domHighlight), so this bubble's ranges MUST be withdrawn when it
    // unmounts: a virtualized row that scrolls away would otherwise stay alive
    // through the ranges pointing into its detached subtree.
    const withdraw = () => clearSearchHighlights(el)

    // Code blocks use dangerouslySetInnerHTML — hljs runs in a child
    // useEffect and sets innerHTML asynchronously after this effect — and a
    // streaming message re-parses on every token. Either replaces text nodes
    // the ranges point into, which collapses them (they paint nothing, and
    // React is untouched). A MutationObserver re-runs the TreeWalker so the
    // fresh nodes are painted, batched per animation frame because a token
    // burst fires many mutation records for one visual update. Registering a
    // Range mutates no DOM, so the walk cannot trigger the observer itself.
    //
    // Performance: the observer fires on any subtree mutation (React
    // re-renders, hljs updates). Each firing runs one TreeWalker pass which is
    // sub-millisecond even for long messages, so the extra runs are negligible.
    if (!term) return () => { cancelScroll?.(); withdraw() }
    let disposed = false
    let scheduled = false
    const observer = new MutationObserver(() => {
      if (scheduled) return
      scheduled = true
      requestAnimationFrame(() => {
        scheduled = false
        if (disposed) return
        run()
      })
    })
    observer.observe(el, { childList: true, subtree: true, characterData: true })
    return () => { disposed = true; observer.disconnect(); cancelScroll?.(); withdraw() }
  }, [term, caseSensitive, currentOcc, effectiveContent, rawMode])

  // Four whole-sentence keys, one per combination of the two optional clauses,
  // rather than a base sentence with ` and used …` / ` (… API cost)` appended.
  // A translator handed those two fragments cannot place them: the credit clause
  // and the cost parenthetical bind to different parts of the sentence in other
  // languages, and several put the duration last. Interpolated values are
  // already locale-formatted by the `format.ts` seam.
  // Validated here rather than at the host, so the strip mounts only for a row
  // that really carries one and the hosts stay a one-property read.
  // Every decision this reply carries, not just the first: a turn can be decided
  // by more than one point, and each gets its own row.
  const decisionRecords = useMemo(() => readDecisionRecords(decisionsStrip), [decisionsStrip])
  // The memory record rides the same field and is found in it rather than read
  // from it: it is drawn by its own component, so the strip reader above declines
  // it and at most one of the two claims any given record.
  const memoryRecord = useMemo(() => readMemoryRecallInStrip(decisionsStrip), [decisionsStrip])
  const turnStatsTitle = (() => {
    if (!turnStats) return undefined
    const elapsed = fmtTurnElapsed(turnStats.elapsed_ms)
    const hasCredits = (turnStats.credits ?? 0) > 0
    const hasCost = (turnStats.cost_usd ?? 0) > 0
    const credits = hasCredits ? fmtCredits(turnStats.credits!) : ''
    const cost = hasCost
      ? fmtCurrency(turnStats.cost_usd!, 'USD', { maximumFractionDigits: 4, minimumFractionDigits: 4 })
      : ''
    const base = hasCredits && hasCost ? i18nT('pages.chat.assistantMessage.turn_took_credits_cost', { elapsed, credits, cost })
      : hasCredits ? i18nT('pages.chat.assistantMessage.turn_took_credits', { elapsed, credits })
      : hasCost ? i18nT('pages.chat.assistantMessage.turn_took_cost', { elapsed, cost })
      : i18nT('pages.chat.assistantMessage.turn_took', { elapsed })
    // The tooltip carries the FULL untrimmed model id (the inline label is
    // shortened by fmtTurnModel), so the profile/region routing detail stays
    // one hover away instead of widening the footer line.
    return turnStats.model ? `${base} · ${i18nT('pages.chat.assistantMessage.turn_model', { model: turnStats.model })}` : base
  })()

  // The overflow menu lives IN the footer action row, in EVERY state. Upstream
  // placed it below the row to keep the row from growing, but the below-row
  // placement is a SECOND `ACTIONS_REVEAL_CLS` row carrying its own `mt-1`, and
  // ICON_ACTION_ROW_CLS makes these rows permanently visible with 36x32
  // targets on touch -- so it added a full row of height to EVERY completed
  // turn's footer. Rows above a reader growing by that much is a page-scale
  // downward displacement the first time they re-measure (reported from a phone
  // at the moment a turn ended), and the two placements ALSO gave neighbouring
  // messages visibly different footers depending on which state they were in.
  // Share is present whenever the menu is; fork appears here only in its
  // unavailable state, because a loaded window keeps it as a row button above
  // where the everyday controls belong. No fork handler means an
  // embedded pane (co-author, artifact chat), so Share/fork stay out even
  // when its voice action needs a small Copy/Speak menu.
  // `shareEnabled` is the `capabilities.social_share` governance answer: pinned
  // off, the Share item is withdrawn, and a menu that would then hold nothing
  // (fork already rendered as a row button) is withdrawn with it rather than
  // opening empty.
  const forkItemsInMenu = forkIndex === undefined || !!forkMessageId
  const oldMenuContext = !!onFork && (shareEnabled || forkItemsInMenu)
  const hasSpeak = !!onSpeak && text.trim().length > 0
  // A crewmate's chat offers "Reply in thread" as a ROW button -- the one action
  // a thread starts from, so it stays visible. The row's cap is two peer
  // controls (the max-two-buttons rule): Reply takes one seat and More the
  // other, so on that surface the raw-view toggle moves into More, and Copy
  // does too unless Quote is offered (then Copy keeps its row button).
  const threadRow = !!onReplyInThread
  // With Quote offered the row reads seat + Copy + More. The seat is Quote
  // unless Reply in thread, Regenerate or Fork holds it; Copy always follows
  // the seat and never moves into More. That is one control over the
  // max-two-buttons rule, a deliberate product ruling: Copy is the most-used
  // action and must stay one click away.
  // Derived once and gated on, like `hasSpeak`: a reply whose whole content
  // parsing consumed (an options-only reply) has nothing to quote, so no
  // seat, no menu item and no context-menu entry offer a control that would
  // do nothing on click.
  const quotableText = stripKeepVisibleMarker(steerCleaned).trimEnd()
  const quoteOffered = !!onQuoteMessage && quotableText.length > 0
  const forkRow = !!onFork && forkIndex !== undefined && !forkMessageId
  const regenRow = !!onRegenerate && !slotRunning
  const quoteRow = quoteOffered && !threadRow && !regenRow && !forkRow
  // What the reader is looking at: the browsed variant, steer acks stripped
  // (they are the agent's receipt of a redirect, not part of its answer).
  // Same text Copy copies: the keep-visible marker is a transcript control,
  // not part of the reply, so a quote must not hand it to the model.
  const quoteShown = () => onQuoteMessage?.(quotableText)
  const menuAvailable = oldMenuContext || hasSpeak || threadRow || quoteOffered
  useEffect(() => {
    if (!menuAvailable || isStreaming || !showFooter) setOverflowOpen(false)
  }, [isStreaming, menuAvailable, showFooter])
  // A reply that previously had no overflow swaps Copy for More. That keeps the
  // footer's peer-control count unchanged while making Speak available for short
  // replies too. Existing overflow footers retain their familiar inline Copy.
  // With Quote offered, Copy always keeps its row button, right after the seat.
  const copyInMenu = !quoteOffered && (hasSpeak || threadRow) && !oldMenuContext
  const rawInMenu = threadRow || quoteOffered
  // With Quote offered, Copy link and Pin fold into More as well. Same order
  // inside the menu as UserMessage's: Quote, Copy link, Pin.
  const linkPinInMenu = quoteOffered
  // Markdown is the default (the row button's click, "Copy as Markdown"); rich
  // text writes clean semantic HTML for editors such as Outlook, with the same
  // Markdown as its plain-text flavour. Rich text is offered from the menus
  // only (More where the reply has one, and the right-click menu on every
  // finished reply): the footer row gains no button and loses none.
  const copyMessage = (format: 'markdown' | 'rich' = 'markdown', fromClosedMenu = false) => {
    const stripped = stripKeepVisibleMarker(steerCleaned)
    const markdown = stripped === steerCleaned ? stripped : stripped.trimEnd()
    const write = format === 'rich'
      ? copyRichToClipboard(markdownToCleanHtml(markdown), markdown)
      : copyToClipboard(markdown)
    write.then((ok) => {
      if (ok) {
        setCopyFailed(false)
        // A menu that has already closed leaves no item to confirm on, so a
        // copy made from one confirms on the row's Copy icon instead (none
        // when Copy itself sits in More: its item must not claim a rich copy).
        if (format === 'rich' && !fromClosedMenu) flashCopy(setRichCopied)(true)
        else if (!(format === 'rich' && copyInMenu)) { setRowCopiedRich(format === 'rich'); flashCopy(setCopied)(true) }
      } else {
        setCopied('idle')
        setRichCopied('idle')
        setCopyFailed(true)
        setOverflowOpen(false)
      }
    }, () => {
      setCopied('idle')
      setRichCopied('idle')
      setCopyFailed(true)
      setOverflowOpen(false)
    })
  }
  const overflowMenu = menuAvailable ? (
      <DropdownMenu open={overflowOpen} onOpenChange={setOverflowOpen}>
        <DropdownMenuTrigger asChild>
          <button
            className="text-muted hover:text-text p-0.5 rounded transition-colors"
            title={i18nT('pages.chat.assistantMessage.more_actions')}
            aria-label={i18nT('pages.chat.assistantMessage.more_actions')}
            data-testid="assistant-more-actions"
          >
            <MoreHorizontal size={14} />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="min-w-[210px]">
          {/* Listed whenever Quote is offered, row seat or not: the bubble's
              right-click menu leads with it, so the two look-alike menus on one
              message agree about what it can do. */}
          {quoteOffered && (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" data-testid="quote-message-menu-item" onSelect={quoteShown}>
              <span className="flex items-center gap-2">
                <Quote className="lucide-inline shrink-0" />
                <span>{i18nT('pages.chat.assistantMessage.quote_message')}</span>
              </span>
            </DropdownMenuItem>
          )}
          {copyInMenu && (
            <DropdownMenuItem
              data-testid="copy-message-menu-item"
              className="[@media(hover:none)]:min-h-10"
              onSelect={(e) => {
                // Keep the result visible while the asynchronous clipboard write settles.
                e.preventDefault()
                copyMessage()
              }}
            >
              <span className="flex flex-col gap-0.5">
                <span className="flex items-center gap-2">
                  {copyOutcomeIcon(copied, <Copy className="lucide-inline shrink-0" />)}
                  <span>{copyOutcomeLabel(copied, i18nT('pages.chat.assistantMessage.copy_as_markdown'), i18nT('pages.chat.assistantMessage.copied_as_markdown'))}</span>
                </span>
                {/* Kept through the outcome, so the open menu holds its size under the pointer. */}
                <span className="text-[11px] leading-4 text-muted pl-[21px]">{i18nT('pages.chat.assistantMessage.copy_as_markdown_hint')}</span>
              </span>
            </DropdownMenuItem>
          )}
          <DropdownMenuItem
            data-testid="copy-rich-menu-item"
            className="[@media(hover:none)]:min-h-10"
            onSelect={(e) => {
              // Same as Copy: keep the outcome visible while the write settles.
              e.preventDefault()
              copyMessage('rich')
            }}
          >
            <span className="flex flex-col gap-0.5">
              <span className="flex items-center gap-2">
                {/* Its own glyph, so the two copy items differ by more than their hints. */}
                {copyOutcomeIcon(richCopied, <ClipboardType className="lucide-inline shrink-0" />)}
                <span>{copyOutcomeLabel(richCopied, i18nT('pages.chat.assistantMessage.copy_as_rich_text'), i18nT('pages.chat.assistantMessage.copied_as_rich_text'))}</span>
              </span>
              <span className="text-[11px] leading-4 text-muted pl-[21px]">{i18nT('pages.chat.assistantMessage.copy_as_rich_text_hint')}</span>
            </span>
          </DropdownMenuItem>
          {linkPinInMenu && messageTs && slotKey && (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" data-testid="copy-link-menu-item" onSelect={(e) => { e.preventDefault(); copySessionLink(slotKey, slotTitle, messageTs, mode).then(ok => { flashCopy(setLinkCopied)(ok); if (!ok) setCopyFailed(true) }, () => { flashCopy(setLinkCopied)(false); setCopyFailed(true) }) }}>
              <span className="flex items-center gap-2">
                {copyOutcomeIcon(linkCopied, <Link2 className="lucide-inline shrink-0" />)}
                <span>{copyOutcomeLabel(linkCopied, i18nT('pages.chat.assistantMessage.copy_link_to_message'))}</span>
              </span>
            </DropdownMenuItem>
          )}
          {linkPinInMenu && messageTs && onTogglePin && (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" data-testid="pin-menu-item" aria-pressed={!!pinned} onSelect={onTogglePin}>
              <span className="flex items-center gap-2">
                {pinned ? <PinOff className="lucide-inline shrink-0" /> : <Pin className="lucide-inline shrink-0" />}
                <span>{pinned ? i18nT('pages.chat.assistantMessage.unpin_message') : i18nT('pages.chat.assistantMessage.pin_message')}</span>
              </span>
            </DropdownMenuItem>
          )}
          {rawInMenu && text.length > 20 && (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" data-testid="toggle-raw-view" aria-pressed={rawMode} onSelect={toggleRaw}>
              <span className="flex items-center gap-2">
                {rawMode ? <Eye className="lucide-inline shrink-0" /> : <Code className="lucide-inline shrink-0" />}
                <span>{rawMode ? i18nT('pages.chat.assistantMessage.rendered_view') : i18nT('pages.chat.assistantMessage.raw_markdown')}</span>
              </span>
            </DropdownMenuItem>
          )}
          {hasSpeak && (
            <DropdownMenuItem className="[@media(hover:none)]:min-h-10" data-testid="speak-message" aria-description={i18nT('pages.chat.assistantMessage.speak_message')} onSelect={() => onSpeak?.(content)}>
              <span className="flex items-center gap-2">
                <Volume2 className="lucide-inline shrink-0" />
                <span>{i18nT('pages.chat.assistantMessage.speak')}</span>
              </span>
            </DropdownMenuItem>
          )}
          {oldMenuContext && shareEnabled && (
          <DropdownMenuItem className="[@media(hover:none)]:min-h-10" data-testid="share-message" onSelect={() => setShareOpen(true)}>
            <span className="flex items-center gap-2">
              <Share2 size={13} className="shrink-0" />
              <span>{i18nT('pages.chat.assistantMessage.share_message')}</span>
            </span>
          </DropdownMenuItem>
          )}
          {oldMenuContext && forkItemsInMenu && (
            <DropdownMenuItem
              // Radix skips a `disabled` item in keyboard nav and kills pointer events,
              // so the unavailable reason stays reachable through aria-disabled.
              aria-disabled={forkIndex === undefined || busyAction !== null || undefined}
              aria-describedby={forkIndex === undefined ? `${reasonId}-fork` : undefined}
              // Same 40px touch floor as Speak, so the items sit at one rhythm on a phone.
              className="flex-col items-start justify-center gap-0.5 [@media(hover:none)]:min-h-10"
              data-testid="fork-from-here"
              onSelect={(e) => {
                if (busyAction !== null) { e.preventDefault(); return }
                if (forkIndex === undefined) {
                  e.preventDefault()
                  setPagingToTarget(true)
                  return
                }
                void runForkAction()
              }}
            >
              <span className={`flex items-center gap-2 ${forkIndex === undefined ? 'opacity-50' : ''}`}>
                {busyAction === 'fork' || loadingOlder ? <Loader2 size={13} className="shrink-0 animate-spin" /> : <GitFork size={13} className="shrink-0" />}
                <span>{forkLabel}</span>
              </span>
              {forkIndex === undefined && <span id={`${reasonId}-fork`} data-testid="fork-unavailable-reason" className="text-[11px] leading-4 text-muted pl-[21px]">
                {unavailableReason}
              </span>}
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
  ) : null

  // Right-click on the bubble. With Quote offered: Quote first, then the
  // everyday actions the row also offers. Without it, a finished reply still
  // arms the menu with its two copy formats, so rich text is reachable without
  // moving any row button; a streaming or footerless reply keeps the browser's
  // own menu, and so does a right-click on a text selection inside the reply
  // (`yieldToSelection`). (A touch device never draws this menu: see
  // MessageContextMenu.)
  const quoteContextItems: MessageMenuItem[] = quoteOffered ? [
    { id: 'quote', label: i18nT('pages.chat.assistantMessage.quote_message'), icon: <Quote size={14} />, onSelect: quoteShown },
    // A refused write from a menu that has closed leaves no icon to flip, so it
    // also raises the row's ErrorNotice (the same surface Copy uses).
    ...(messageTs && slotKey ? [{ id: 'copy-link', label: i18nT('pages.chat.assistantMessage.copy_link_to_message'), icon: <Link2 size={14} />, onSelect: () => { copySessionLink(slotKey, slotTitle, messageTs, mode).then(ok => { flashCopy(setLinkCopied)(ok); if (!ok) setCopyFailed(true) }, () => { flashCopy(setLinkCopied)(false); setCopyFailed(true) }) } }] : []),
    ...(messageTs && onTogglePin ? [{ id: 'pin', label: pinned ? i18nT('pages.chat.assistantMessage.unpin_message') : i18nT('pages.chat.assistantMessage.pin_message'), icon: pinned ? <PinOff size={14} /> : <Pin size={14} />, onSelect: () => onTogglePin() }] : []),
    // The same item set as More, so the two look-alike menus on one reply
    // never disagree (UX review): the raw toggle joins here on the same gate.
    ...(text.length > 20 ? [{ id: 'raw', label: rawMode ? i18nT('pages.chat.assistantMessage.rendered_view') : i18nT('pages.chat.assistantMessage.raw_markdown'), icon: rawMode ? <Eye size={14} /> : <Code size={14} />, onSelect: toggleRaw }] : []),
  ] : []
  // Same words as the More menu's items, so one message never offers the
  // same action under two names.
  const copyContextItems: MessageMenuItem[] = [
    { id: 'copy', label: i18nT('pages.chat.assistantMessage.copy_as_markdown'), hint: i18nT('pages.chat.assistantMessage.copy_as_markdown_hint'), icon: <Copy size={14} />, onSelect: () => copyMessage() },
    { id: 'copy-rich', label: i18nT('pages.chat.assistantMessage.copy_as_rich_text'), hint: i18nT('pages.chat.assistantMessage.copy_as_rich_text_hint'), icon: <ClipboardType size={14} />, onSelect: () => copyMessage('rich', true) },
  ]
  const finishedReply = !isStreaming && showFooter && quotableText.length > 0
  const contextItems: MessageMenuItem[] = quoteOffered
    ? [quoteContextItems[0], { ...copyContextItems[0], separatorBefore: true }, copyContextItems[1], ...quoteContextItems.slice(1)]
    : finishedReply ? copyContextItems : []
  // The row Copy's tooltip says where the other format is, wherever the
  // bubble's menu actually draws (never on touch: see MessageContextMenu).
  const richByRightClick = !touch && contextItems.some(item => item.id === 'copy-rich')
  const forkButton = forkRow ? <button className={ROW_ACTION_CLS} disabled={busyAction !== null} data-testid="fork-from-here" title={forkLabel} aria-label={forkLabel} onClick={() => { void runForkAction() }}>{busyAction === 'fork' ? <Loader2 size={14} className="animate-spin" /> : <GitFork size={14} />}</button> : null
  const regenButton = regenRow ? <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title={i18nT('pages.chat.assistantMessage.regenerate')} aria-label={i18nT('pages.chat.assistantMessage.regenerate_response')} onClick={onRegenerate}><RefreshCw size={14} /></button> : null

  return <div data-role="assistant" className="group/msg">
    {/* 'message-bubble' is a stable theming hook — see website/docs/theming-contract.md */}
    <MessageContextMenu items={contextItems} onCopyFailed={() => setCopyFailed(true)} yieldToSelection={!quoteOffered}>
    <div ref={contentRef} className={`message-bubble mc-message-font-scope msg-content group/bubble relative leading-relaxed text-text overflow-hidden${bubbleClassName ? ` ${bubbleClassName}` : ''}`} data-testid="message-bubble" data-bordered={bubbleClassName ? '' : undefined} style={rawMode && rawBoxHeight !== null && !isStreaming
      ? { overflowWrap: 'anywhere', wordBreak: 'break-word', height: rawBoxHeight, overflowY: 'auto', fontSize: 'var(--mc-message-font-size, 14px)' }
      : { overflowWrap: 'anywhere', wordBreak: 'break-word', fontSize: 'var(--mc-message-font-size, 14px)' }}>
      <MessageErrorBoundary rawContent={smoothedText}>
        <MarkdownRenderer content={smoothedText} streaming={isStreaming} onFileOpen={onFileOpen} onFolderOpen={onFolderOpen} onArtifactOpen={onArtifactOpen} onSessionOpen={onSessionOpen} sessions={sessions} activeSession={activeSession} rawMode={rawMode} messageTs={messageTs} slotKey={slotKey} glow={isStreaming} smooth={smooth} linkPreviews={linkPreviews && !draining} collapseDiffs mdCardToggle blockedLinks={effectiveBlockedLinks} redactions={effectiveRedactions} redactionCoach={showRedactionCoach} />
      </MessageErrorBoundary>
      {/* Render the steer ack the moment kiro-cli emits the [STEERING …] marker
          — including mid-stream — so the user sees the agent acknowledge the
          steer live, not only after the whole turn finishes.
          Suppressed for a turn a policy block explained in-band: the same
          mechanism carries that notice, so the chip would credit the PERSON with
          a steer the system sent — and the blocked-tool card in the same turn
          already says what happened. The marker is still stripped from the prose
          either way (steerCleaned), so nothing leaks as raw text. */}
      {!suppressSteerAck && steerAcks.length > 0 && (
        <div className="flex flex-col items-start gap-1 mb-2">
          {steerAcks.map((a, i) => <SteerAckChip key={i} summary={a} entrance={isStreaming} />)}
        </div>
      )}
      {/* Deliberately NOT gated on `isStreaming` (#7819): a reply can take minutes
          and the wait was dead time. The toolbar is inert until the reader selects
          something, and what an action receives cannot be invalidated by a
          mid-stream re-render, because `SelectionToolbar` snapshots it at
          selection time -- `selectedTextRef`/`selectionRectRef` are written in
          `checkSelection`, and `handleAction` reads those refs, never a live
          `window.getSelection()` range.
          Measured under real token arrival rather than assumed. Settled prose
          keeps its text in ONE large node, so a selection there holds: the
          toolbar appears and stays, and Quote returns byte-identical text after
          seconds of streaming. The still-growing tail is rendered by the glow as
          one text node PER CHARACTER, recreated per token, so a selection there
          has no stable anchor -- which is why this reads as "select the prose
          above the tail", and it is a property of the glow that already ships
          rather than of this gate. When the browser does drop such a range, the
          reader loses the highlight and NOT the text or the toolbar: on desktop
          `selectionchange` is gated to touch (see `SelectionToolbar`), so nothing
          re-checks the selection and the snapshot stays clickable.
          The three sibling gates below (file chips, turn stats, footer) stay
          `!isStreaming` -- those are end-of-turn summaries, with no partial form
          to show.
          On a touch device the row DOCKS above the composer instead of floating
          at the selection: there the platform draws its own handles, magnifier
          and Copy callout around the selection, and a row drawn on top of them
          took the taps meant for the handles. Copy is left to that callout, so
          the dock carries only what the platform cannot do (Quote, Ask). */}
      {toolbarActions.length > 0 && <SelectionToolbar containerRef={contentRef} actions={toolbarActions} dock={touch} />}
    </div>
    </MessageContextMenu>
    {/* Directly under the bubble, above the file chips: the strip says how THIS
        reply's skills were chosen, and a long chip list between the two would
        read as a receipt for something else. Not gated on `isStreaming` — unlike
        the end-of-turn summaries below it, the record is stamped whole or not at
        all, so there is no partial form to withhold. */}
    {decisionRecords.map(record => (
      // Keyed by POINT, not by index: the disclosure key is derived from it too, so
      // an index key would hand one row's remembered expansion to a different
      // decision the next time the list's order changed.
      <DecisionStrip
        key={record.point}
        record={record}
        disclosureKey={messageTs ? `dstrip-${record.point}-${messageTs}` : undefined}
      />
    ))}
    {memoryRecord && (
      <MemoryRecallStrip record={memoryRecord} disclosureKey={messageTs ? `mstrip-${messageTs}` : undefined} />
    )}
    {fileChanges && fileChanges.length > 0 && !isStreaming && (
      /* Pass `onFileOpen` by IDENTITY — a `(p) => onFileOpen(p)` wrapper here is
         a new function every render, which busts FileChangeChips' memo and
         cascades into Pierre re-initializing every diff row on each parent
         render (the "file chips flash while typing" defect). The prop types
         are directly compatible: extra optional params are ignored. */
      <FileChangeChips fileChanges={fileChanges} omittedFiles={fileChangesOmittedFiles} onOpenDiff={onOpenDiff} onFileOpen={onFileOpen} style={fileChipStyle} artifactPaths={artifactPaths} disclosureKey={messageTs ? `fcc-${messageTs}` : undefined} />
    )}
    {!isStreaming && showFooter && turnStats && turnStats.elapsed_ms > 0 && (
      /* No `font-mono`: "1.98 credits · 59s" is a labelled measurement, not
         code, and Tailwind's `font-mono` pins `var(--mono)` — a token the Font
         Family setting never writes, so this line ignored the user's choice.
         `tabular-nums` stays: fixed-width digits are what the mono was earning
         here, and it works in a proportional face too. */
      <div className="flex items-center gap-1 mt-1 text-[11px] leading-4 text-muted/60 tabular-nums" data-testid="turn-stats" title={turnStatsTitle}>
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
            {/* Model leads (what served), then cost (what it took), then time.
                Trimmed for width; the untrimmed id is in the footer tooltip. */}
            {turnStats.model && <span className="font-mono" data-testid="turn-model">{fmtTurnModel(turnStats.model)} ·</span>}
            {billed && <span>{billed} ·</span>}
            <Clock size={11} aria-hidden="true" />
            <span>{fmtTurnElapsed(turnStats.elapsed_ms)}</span>
          </>
        })()}
      </div>
    )}
    {/* Where the pointer cannot hover, the footer uses compact cells: 28px on
        pointer devices and 36×32px on touch, with 14px/16px glyphs. */}
    {!isStreaming && showFooter && (<>
      <div className={`${ACTIONS_REVEAL_CLS} has-[[data-state=open]]:opacity-100 ${revealActions && hasSpeak ? '!opacity-100 !delay-0' : ''}`}>
        {/* No `font-mono`: a formatted date is prose, and Tailwind's `font-mono`
            pins `var(--mono)` — a token the Font Family setting never writes, so
            it overrode the user's choice and put JetBrains Mono (no CJK
            coverage) under a date that a zh/ja dashboard renders WITH CJK
            characters. `tabular-nums` keeps digits fixed-width, which is the
            alignment the mono was actually there for — and it holds the action
            row below at the same x across messages. */}
        {timestamp && <span className="text-muted text-[12px] leading-5 tabular-nums mr-2" title={timestampTitle}>{timestamp}</span>}
        {onReplyInThread && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" data-testid="reply-in-thread" title={i18nT('pages.chat.thread.reply_in_thread')} aria-label={i18nT('pages.chat.thread.reply_in_thread')} onClick={onReplyInThread}><MessageSquare size={14} /></button>}
        {/* With Quote offered the seat comes first, then Copy (see quoteRow).
            Regenerate holds the seat over Fork; on the newest reply of a loaded
            window, where both draw, Fork follows Copy so Copy stays second. */}
        {quoteOffered && (regenButton ?? forkButton)}
        {quoteRow && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" data-testid="quote-message" title={i18nT('pages.chat.assistantMessage.quote_message')} aria-label={i18nT('pages.chat.assistantMessage.quote_message')} onClick={quoteShown}><Quote size={14} /></button>}
        {!copyInMenu && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" aria-label={copyOutcomeLabel(copied, i18nT('pages.chat.assistantMessage.copy'), i18nT(rowCopiedRich ? 'pages.chat.assistantMessage.copied_as_rich_text' : 'pages.chat.assistantMessage.copied_as_markdown'))} title={copied === 'ok' ? i18nT(rowCopiedRich ? 'pages.chat.assistantMessage.copied_as_rich_text' : 'pages.chat.assistantMessage.copied_as_markdown') : i18nT(richByRightClick ? 'pages.chat.assistantMessage.copy_as_markdown_right_click' : 'pages.chat.assistantMessage.copy_as_markdown')} onClick={() => copyMessage()}>{copyOutcomeIcon(copied, <Copy size={14} />)}</button>}
        {quoteOffered && regenButton && forkButton}
        {!linkPinInMenu && messageTs && slotKey && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title={i18nT('pages.chat.assistantMessage.copy_link_to_message')} aria-label={copyOutcomeLabel(linkCopied, i18nT('pages.chat.assistantMessage.copy_link_to_message'))} onClick={() => { copySessionLink(slotKey, slotTitle, messageTs, mode).then(ok => { flashCopy(setLinkCopied)(ok); if (!ok) setCopyFailed(true) }, () => { flashCopy(setLinkCopied)(false); setCopyFailed(true) }) }}>{copyOutcomeIcon(linkCopied, <Link2 size={14} />)}</button>}
        {!linkPinInMenu && messageTs && onTogglePin && <button className="text-muted hover:text-text p-0.5 rounded transition-colors" title={pinned ? i18nT('pages.chat.assistantMessage.unpin_message') : i18nT('pages.chat.assistantMessage.pin_message')} aria-label={pinned ? i18nT('pages.chat.assistantMessage.unpin_message') : i18nT('pages.chat.assistantMessage.pin_message')} aria-pressed={!!pinned} onClick={onTogglePin}>{pinned ? <PinOff size={14} /> : <Pin size={14} />}</button>}
        {/* A loaded window keeps fork as a row button, as on base: the menu below exists
            only to give the UNAVAILABLE state a visible reason, and relocating the everyday
            controls taxed chats the bound never touched. */}
        {!quoteOffered && forkButton}
        {/* Icon-only, like every other row action. State is carried the way the
            pin button carries it: the glyph names the view a click will GET
            (code brackets while rendered, an eye for "preview" while raw) and
            aria-pressed says which one is showing. Speak lives in More. */}
        {text.length > 20 && !rawInMenu && <button className={`p-0.5 rounded transition-colors ${rawMode ? 'text-text' : 'text-muted hover:text-text'}`} aria-pressed={rawMode} data-testid="toggle-raw-view" title={rawMode ? i18nT('pages.chat.assistantMessage.rendered_view') : i18nT('pages.chat.assistantMessage.raw_markdown')} aria-label={rawMode ? i18nT('pages.chat.assistantMessage.switch_to_rendered_view') : i18nT('pages.chat.assistantMessage.switch_to_raw_markdown_view')} onClick={toggleRaw}>{rawMode ? <Eye size={14} /> : <Code size={14} />}</button>}
        {!quoteOffered && regenButton}
        {hasVariants && (() => {
          const curIdx = activeIdx
          const switchFn = onSwitchVariant || ((i: number) => setLocalIdx(i))
          return (
            <div className="flex items-center gap-0.5 ml-1 text-[11px] leading-4 text-muted">
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
        {overflowMenu}
      </div>
    </>)}
    {/* No hand-off: AssistantMessage also renders inside ChatEmbed, whose composer
        draft lives only in useComposerDraft state; navigating away would discard
        that unsent text. */}
    <ErrorNotice
      message={copyFailed ? i18nT('pages.settings.remoteCrewPanel.copy_failed') : null}
      onDismiss={() => setCopyFailed(false)}
      className="mt-1 [@media(hover:none)]:[&_button]:min-h-10 [@media(hover:none)]:[&_button]:min-w-10"
    />
    {planSteps && onApplyPlan && !applied && !isRegenerating && (
      <button className="mt-1 px-3 py-2 rounded-md text-[13px] leading-5 font-medium border border-accent text-accent bg-transparent cursor-pointer hover:bg-accent hover:text-accent-fg transition-all" onClick={async () => { const ok = await onApplyPlan(planSteps); if (ok) setApplied(true) }}>
        <ClipboardList className="lucide-inline" /> {i18nT('pages.chat.assistantMessage.use_as_plan_count', { count: planSteps.length })}
      </button>
    )}
    {applied && <div className="mt-1 text-[13px] leading-5 text-ok"><CheckCircle className="lucide-inline" /> {i18nT('pages.chat.assistantMessage.applied_to_tasks')}</div>}
    {/* Radix portals the dialog to <body>; gating on shareOpen keeps the lazy
        chunk unfetched until the first share. Mounted HERE, outside the overflow
        menu and NOT gated on shareEnabled: a policy swap mid-compose withdraws
        the menu (and the entry), but must not unmount the dialog with the
        user's edits in it — the modal shows a notice and withdraws its actions
        instead, and the user closes it when ready. */}
    {shareOpen && <Suspense fallback={null}><LazyShareMessageModal onClose={() => setShareOpen(false)} messageText={steerCleaned} prevUserText={prevUserText} shareEnabled={shareEnabled} /></Suspense>}
  </div>
})

export default AssistantMessage
