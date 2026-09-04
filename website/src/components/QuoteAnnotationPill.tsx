import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Quote, X } from 'lucide-react'
import { i18nT } from '../i18n/t'
import { fmtList, fmtNumber } from '../i18n/format'
import { distinctRoles, quoteExcerpt, type QuoteRef } from '../utils/quoteRefs'
import { Btn, IconButton } from './ui'

/**
 * Staged text quotes, collapsed into one pill above the composer.
 *
 * Sits in the same strip position as `SessionRefStrip` / `FilePreviewStrip` and
 * borrows their container geometry, so a staged quote reads as the same class of
 * thing as a staged attachment. Height is MEASURED by the composer through
 * `rootRef`, never predicted from these classes.
 *
 * Two decisions are load-bearing:
 *
 * **The resting label is adaptive, not a bare count.** At one quote it shows the
 * role plus an excerpt, because "1 quote" answers a question nobody asked and
 * hides the only thing the user wants to confirm before sending. At two or more
 * it collapses to a count plus the distinct roles — the sources stay visible even
 * when the text cannot.
 *
 * **Hover is an enhancement; click is the contract.** The pill is a real button
 * owning a disclosure popover. Hover opens it transiently on pointer devices,
 * but click / tap / Enter / Space PINS it open and moves focus inside, which is
 * the only path that works on touch and with a keyboard. Removing hover entirely
 * would break nothing.
 */
/** Popover width, and the gap/edge inset used when anchoring it to the pill. */
const POPOVER_W = 340
const GAP = 8
const MARGIN = 16

export default function QuoteAnnotationPill({ quotes, onRemove, onClearAll, onJumpToSource, rootRef }: {
  quotes: QuoteRef[]
  onRemove?: (key: string) => void
  onClearAll?: () => void
  /** Scroll the transcript to the quote's source message and flash it. */
  onJumpToSource?: (quote: QuoteRef) => void
  /** Measured by the composer to reserve the strip's height. */
  rootRef?: (node: HTMLDivElement | null) => void
}) {
  // `pinned` survives pointer-leave; `hovering` does not. Keeping them separate
  // is what lets a click while hovering promote a transient peek into a stable
  // panel instead of dismissing it on the next mouse move.
  const [pinned, setPinned] = useState(false)
  const [hovering, setHovering] = useState(false)
  const pillRef = useRef<HTMLButtonElement>(null)
  const popRef = useRef<HTMLDivElement>(null)
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const popId = useId()
  const open = pinned || hovering

  /**
   * Viewport anchor for the popover, which is PORTALED to `document.body`.
   *
   * It cannot be a plain `absolute bottom-full` child: ChatInput's
   * `input-wrapper` owns `overflow-hidden` (its rounded frame and drag-resize
   * depend on it), and the popover opens ABOVE the strip — so as a descendant it
   * was clipped to zero visible pixels while still being present and focusable.
   * Every unit test passed because jsdom performs no layout, which is exactly
   * the class of defect a rendered check catches and a DOM assertion cannot.
   */
  const [anchor, setAnchor] = useState<{ left: number; bottom: number } | null>(null)
  const measure = useCallback(() => {
    const el = pillRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    const width = Math.min(POPOVER_W, window.innerWidth - MARGIN * 2)
    setAnchor({
      left: Math.max(MARGIN, Math.min(r.left, window.innerWidth - width - MARGIN)),
      bottom: Math.max(MARGIN, window.innerHeight - r.top + GAP),
    })
  }, [])

  const clearLeaveTimer = () => {
    if (leaveTimer.current) { clearTimeout(leaveTimer.current); leaveTimer.current = null }
  }
  // A grace period on leave: the popover sits above the pill with a gap, so the
  // pointer necessarily crosses dead space on its way in. Closing instantly
  // would make the panel unreachable by mouse.
  const scheduleClose = useCallback(() => {
    clearLeaveTimer()
    leaveTimer.current = setTimeout(() => setHovering(false), 220)
  }, [])
  useEffect(() => clearLeaveTimer, [])

  // ChatInput keeps this component mounted while the collection is empty so its
  // measured-height ref remains stable. Reset hidden disclosure state here:
  // removing the final row must not make the next staged quote reopen an old,
  // pinned popover (or inherit a hover that could no longer receive mouseleave).
  useEffect(() => {
    if (quotes.length) return
    if (leaveTimer.current) {
      clearTimeout(leaveTimer.current)
      leaveTimer.current = null
    }
    setPinned(false)
    setHovering(false)
  }, [quotes.length])

  // Escape closes and returns focus to the pill, and an outside click dismisses
  // — the disclosure contract a keyboard user expects. Only bound while pinned:
  // a transient hover panel closes on its own.
  useEffect(() => {
    if (!pinned) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setPinned(false)
      setHovering(false)
      pillRef.current?.focus()
    }
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (popRef.current?.contains(t) || pillRef.current?.contains(t)) return
      setPinned(false)
      setHovering(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onDown)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onDown)
    }
  }, [pinned])

  // Focus moves into the panel on pin so the rows, their remove buttons and the
  // jump links are reachable by Tab without hunting.
  useEffect(() => {
    if (!pinned) return
    popRef.current?.querySelector<HTMLElement>('[data-quote-row-jump]')?.focus()
  }, [pinned])

  // The anchor is viewport-relative, so it has to be re-measured while the panel
  // is open: the composer grows as the textarea does, and a scroll or resize
  // moves the pill out from under a stale position.
  useEffect(() => {
    if (!open) return
    measure()
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [open, quotes.length, measure])

  if (!quotes.length) return null

  const single = quotes.length === 1
  const label = single
    ? i18nT('components.quoteAnnotationPill.one_quote', {
      role: quotes[0].role,
      excerpt: quoteExcerpt(quotes[0].text),
    })
    : i18nT('components.quoteAnnotationPill.n_quotes', {
      n: fmtNumber(quotes.length),
      roles: fmtList(distinctRoles(quotes)),
    })

  return (
    // Hover only previews the disclosure; click, touch, and keyboard activation
    // remain on the real button below, so this wrapper is not an action target.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      className="relative"
      ref={rootRef}
      data-testid="quote-annotation-strip"
      onMouseEnter={() => { clearLeaveTimer(); setHovering(true) }}
      onMouseLeave={scheduleClose}
    >
      <div className="flex gap-2 px-4 py-2 border-t border-border bg-chrome/50 items-center">
        <div className="relative shrink-0 flex items-center gap-1 max-w-full px-2 py-1 rounded-full border border-border bg-bg-hover text-[12px] text-text">
          <Btn
            ref={pillRef}
            type="button"
            data-testid="quote-annotation-pill"
            className="min-w-0 bg-transparent border-none p-0 text-[12px] shadow-none hover:border-transparent hover:bg-transparent"
            aria-expanded={open}
            aria-controls={popId}
            aria-haspopup="dialog"
            onClick={() => {
              if (pinned) {
                setPinned(false)
                setHovering(false)
              } else {
                setPinned(true)
              }
            }}
            title={i18nT('components.quoteAnnotationPill.review_quotes')}
          >
            <Quote className="lucide-inline shrink-0 text-accent" aria-hidden="true" />
            <span className="truncate">{label}</span>
          </Btn>
          {onClearAll && (
            <IconButton
              data-testid="quote-annotation-clear"
              className="shrink-0 w-7 h-7 -my-1.5 -mr-1.5 inline-flex items-center justify-center"
              variant="danger"
              onClick={() => { setPinned(false); setHovering(false); onClearAll() }}
              title={i18nT('components.quoteAnnotationPill.clear_all')}
              aria-label={i18nT('components.quoteAnnotationPill.clear_all')}
            >
              <X className="lucide-inline" aria-hidden="true" />
            </IconButton>
          )}
        </div>
      </div>

      {open && anchor && createPortal(
        // Hover keeps a transient peek open; the dialog itself is not an action
        // target, so the pointer handlers here are presentational only.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <div
          ref={popRef}
          id={popId}
          role="dialog"
          aria-label={i18nT('components.quoteAnnotationPill.review_quotes')}
          data-testid="quote-annotation-popover"
          // Hover has to be re-armed here: portaled out of the pill's wrapper,
          // this panel no longer sits inside the element whose mouseleave keeps
          // a transient peek alive, so without these the pointer crossing into
          // it would dismiss the thing it is reaching for.
          onMouseEnter={() => { clearLeaveTimer(); setHovering(true) }}
          onMouseLeave={scheduleClose}
          style={{ position: 'fixed', left: anchor.left, bottom: anchor.bottom, width: POPOVER_W }}
          className="z-50 max-w-[calc(100vw-2rem)] max-h-[260px] overflow-y-auto rounded-lg border border-border bg-bg-elevated shadow-lg p-2"
        >
          {quotes.map((q, i) => (
            <div
              key={q.key}
              data-testid="quote-annotation-row"
              className="flex gap-2 items-start px-1 py-1.5 rounded hover:bg-bg-hover"
            >
              <div className="min-w-0 flex-1">
                {/* The attribution IS the jump control: "wait, what was the
                    context?" is the gesture, so it lives on the label that
                    names the source rather than a separate icon. */}
                <Btn
                  type="button"
                  data-quote-row-jump=""
                  data-testid="quote-annotation-jump"
                  className="block w-full text-left text-[11px] text-accent hover:underline bg-transparent border-none p-0 shadow-none hover:border-transparent hover:bg-transparent truncate"
                  onClick={() => onJumpToSource?.(q)}
                  disabled={!onJumpToSource || (!q.mid && !q.ts)}
                  title={i18nT('components.quoteAnnotationPill.jump_to_source')}
                >
                  {i18nT('components.quoteAnnotationPill.row_attribution', {
                    n: fmtNumber(i + 1),
                    role: q.role,
                    time: q.time,
                  })}
                </Btn>
                <div className={`text-[12px] text-text opacity-90 line-clamp-2 ${q.code ? 'font-mono' : ''}`}>
                  {q.text}
                </div>
              </div>
              {onRemove && (
                <IconButton
                  data-testid="quote-annotation-row-remove"
                  className="shrink-0 w-7 h-7 -my-1 -mr-1 inline-flex items-center justify-center"
                  variant="danger"
                  onClick={() => onRemove(q.key)}
                  title={i18nT('components.quoteAnnotationPill.remove_quote')}
                  aria-label={i18nT('components.quoteAnnotationPill.remove_quote')}
                >
                  <X className="lucide-inline" aria-hidden="true" />
                </IconButton>
              )}
            </div>
          ))}
        </div>,
        document.body,
      )}
    </div>
  )
}
