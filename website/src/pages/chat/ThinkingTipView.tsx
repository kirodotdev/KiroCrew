/**
 * The exploratory tip line beside "Thinking…" (picked by `useThinkingTip` in
 * ThinkingTip.tsx). Loaded with ThinkingTip.tsx, after the first paint, so it
 * stays out of the dashboard's first load.
 */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { motion } from 'framer-motion'
import { X } from 'lucide-react'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import type { ExploratoryTip } from './ThinkingTip'

/** Split a tip body into plain, bold and code runs. Shipped copy only, so a
 *  run is plain text: nothing here is ever parsed as HTML or markdown. */
export function tipRuns(body: string): { kind: 'text' | 'bold' | 'code'; text: string }[] {
  const out: { kind: 'text' | 'bold' | 'code'; text: string }[] = []
  const re = /\*\*([^*]+)\*\*|`([^`]+)`/g
  let last = 0
  for (let m = re.exec(body); m; m = re.exec(body)) {
    if (m.index > last) out.push({ kind: 'text', text: body.slice(last, m.index) })
    out.push(m[1] !== undefined ? { kind: 'bold', text: m[1] } : { kind: 'code', text: m[2] })
    last = m.index + m[0].length
  }
  if (last < body.length) out.push({ kind: 'text', text: body.slice(last) })
  return out
}

/** The divider and the tip line, placed after "Thinking…" in ChatFooter. */
export function ThinkingTip({ tip, onShown, onDismiss, dismissError = null }: {
  tip: ExploratoryTip
  onShown: () => void
  onDismiss: () => void
  /** Why the last dismiss did not save; the tip stays so it can be tried again. */
  dismissError?: string | null
}) {
  const reduceMotion = useReducedMotion()
  // Below the phone breakpoint the tip takes its own full-width line under
  // "Thinking...", and shows only when it fits on that one line: it never
  // wraps or ends in an ellipsis there. A tip that does not fit is not shown
  // (and not counted as shown) until a wider screen makes room.
  const phone = useIsMobile()
  const [fits, setFits] = useState(!phone)
  useEffect(() => { if (!phone || fits) onShown() }, [onShown, tip.id, phone, fits])
  // Desktop: the row wraps (ChatFooter), so a tip too long for the space after
  // "Thinking..." drops to its own line and wraps there. The divider separates
  // the two only while they share a line; on its own line it is hidden but
  // keeps its space, so hiding it cannot change the width that decided the wrap.
  const ref = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const [ownLine, setOwnLine] = useState(false)
  useLayoutEffect(() => {
    const el = ref.current
    const row = el?.parentElement
    if (!el || !row) return
    const measure = phone
      ? () => {
          const content = contentRef.current
          setFits(!!content && content.offsetWidth <= el.clientWidth)
        }
      : () => {
          const status = el.previousElementSibling as HTMLElement | null
          setOwnLine(!!status && el.offsetTop > status.offsetTop + status.offsetHeight / 2)
        }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(row)
    return () => ro.disconnect()
  }, [tip.id, phone])
  const text = (
    <span className={phone ? 'whitespace-nowrap' : 'min-w-0 break-words'} data-testid="thinking-tip-text">
      {i18nT('components.markdownPanel.tip')}{' '}
      {tipRuns(tip.body).map((run, i) => run.kind === 'bold'
        ? <strong key={i} className="font-semibold text-text">{run.text}</strong>
        : run.kind === 'code'
          ? <code key={i} className="font-mono text-[12px] px-1 py-px rounded border border-border">{run.text}</code>
          : <span key={i}>{run.text}</span>)}
    </span>
  )
  const dismissButton = (
    <button
      type="button"
      onClick={onDismiss}
      aria-label={i18nT('components.tipCard.dismiss_tip')}
      className="shrink-0 inline-flex items-center justify-center w-5 h-5 rounded bg-transparent border-none cursor-pointer text-muted hover:text-text hover:bg-bg-hover opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 focus-visible:opacity-100 transition-opacity"
    >
      <X size={12} aria-hidden="true" />
    </button>
  )
  // No hand-off: this row sits above the chat composer, whose draft is
  // unsaved local state the hand-off's navigation would discard.
  const errorNotice = <ErrorNotice variant="inline" message={dismissError} testId="thinking-tip-dismiss-error" />
  const motionProps = {
    initial: reduceMotion ? false as const : { opacity: 0 },
    animate: { opacity: 1 },
    transition: reduceMotion ? { duration: 0 } : { duration: 0.2, ease: 'easeOut' as const },
  }
  if (phone) {
    return (
      <motion.div
        ref={ref}
        data-testid="thinking-tip"
        data-fits={fits}
        className="group relative basis-full w-full min-w-0 overflow-hidden text-[13px] text-muted"
        {...motionProps}
      >
        {/* Measured at its natural width; out of the flow and unseen until it fits. */}
        <div
          ref={contentRef}
          className={`inline-flex items-center gap-2${fits ? '' : ' absolute left-0 top-0 invisible'}`}
          aria-hidden={fits ? undefined : true}
        >
          {text}
          {dismissButton}
        </div>
        {fits && errorNotice}
      </motion.div>
    )
  }
  return (
    <motion.div
      ref={ref}
      data-testid="thinking-tip"
      data-own-line={ownLine || undefined}
      className="group flex items-start gap-2 min-w-0 max-w-full text-[13px] text-muted"
      {...motionProps}
    >
      <span className={`w-px h-3.5 mt-[3px] bg-border shrink-0${ownLine ? ' invisible' : ''}`} aria-hidden="true" />
      {text}
      {dismissButton}
      {errorNotice}
    </motion.div>
  )
}

/** The "Thinking…" row with the tip after it, or the failed tip load when there
 *  is no tip. ChatFooter draws it through `ThinkingTipController`'s row builder. */
export function ThinkingTipRow({ statusRow, tip, loadError, onShown, onDismiss, dismissError }: {
  statusRow: ReactNode
  tip: ExploratoryTip | null
  /** Why a tips read failed this turn; named beside a tip already shown too. */
  loadError: string | null
  onShown: () => void
  onDismiss: () => void
  dismissError: string | null
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 min-w-0 max-w-full">
      {statusRow}
      {tip && <ThinkingTip tip={tip} onShown={onShown} onDismiss={onDismiss} dismissError={dismissError} />}
      {/* No hand-off: this row sits above the chat composer, whose draft is
          unsaved local state the hand-off's navigation would discard. */}
      <ErrorNotice variant="inline" impact="minor" message={loadError} testId="thinking-tip-load-error" />
    </div>
  )
}

