import { useLayoutEffect, useRef, type RefObject } from 'react'
import { ChevronDown } from 'lucide-react'
import { i18nT } from '../i18n/t'

interface MoreBelowCueProps {
  /** Rows sit below the fold. When false the strip keeps its height and only
   *  its text hides, so reaching the last row does not resize the list. */
  visible: boolean
  testId: string
  /** The scrolling list the strip sits under. */
  scrollerRef: RefObject<HTMLElement | null>
}

/** A "More below" button in its own strip under a list that scrolls inside a
 *  fixed-height box, so the list reads as clipped rather than complete. A click
 *  scrolls the list down by most of its height; the rows that were at the
 *  bottom stay in view for continuity. Hidden at the end of the list, so it is
 *  never a tab stop that does nothing. Accent text and a hairline because a
 *  muted caption was easy to miss. */
export function MoreBelowCue({ visible, testId, scrollerRef }: MoreBelowCueProps) {
  const buttonRef = useRef<HTMLButtonElement>(null)
  // Hiding the focused button would drop keyboard focus to the page, so hand
  // it to the last row the click scrolled into view. A layout effect runs
  // before the browser paints the hidden button and blurs it.
  useLayoutEffect(() => {
    if (visible || document.activeElement !== buttonRef.current) return
    const rows = scrollerRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')
    rows?.[rows.length - 1]?.focus()
  }, [visible, scrollerRef])
  const scrollDown = () => {
    const el = scrollerRef.current
    const reduce = !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    el?.scrollBy({ top: el.clientHeight * 0.8, behavior: reduce ? 'auto' : 'smooth' })
  }
  return (
    <button
      ref={buttonRef}
      type="button"
      onClick={scrollDown}
      data-testid={testId}
      className={`shrink-0 w-full flex items-center justify-center gap-1 pt-1.5 pb-1 border-0 border-t border-solid border-border bg-transparent text-[12px] font-medium text-accent cursor-pointer select-none ${visible ? '' : 'invisible'}`}
    >
      {i18nT('components.sidePanelLayout.more_below')}
      <ChevronDown size={14} />
    </button>
  )
}
