/** A Crew Mode root row's worker counts: one glyph and a number per bucket
 *  (needs you, working, waiting), the words in each tooltip and the whole
 *  sentence as the label. Each glyph has its own shape, so it reads without
 *  colour. */
import { useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from 'react'
import { Hourglass, Loader, TriangleAlert } from 'lucide-react'
import { i18nT } from '../../i18n/t'
import { fmtList, fmtNumber } from '../../i18n/format'
import type { CrewRowCount } from '../../lib/crewWorkers'

type Bucket = CrewRowCount['bucket']

/** The glyph size and stroke of the row's session count (`MessageSquare`), so
 *  the counts read as one set with it. */
const ICON_PX = 11

/** Each glyph wears its group's colour, as the session count's bubble wears the
 *  accent; the number stays muted. Working is the green of the row's old
 *  running dot; needs you is the warm tone. */
const ICON_TONE: Record<Bucket, string> = {
  needs_you: 'crew-tone-warn',
  working: 'text-[var(--ok)]',
  waiting: 'crew-tone-muted',
}

/** The loader of a running row, the hourglass of queued work, and an
 *  exclamation mark for what the person owes. */
function bucketIcon(bucket: Bucket): ReactNode {
  const cls = `inline-block shrink-0 ${ICON_TONE[bucket]}`
  // The loader is still under reduced motion.
  if (bucket === 'working') return <Loader size={ICON_PX} aria-hidden="true" className={`${cls} motion-safe:animate-spin`} />
  if (bucket === 'waiting') return <Hourglass size={ICON_PX} aria-hidden="true" className={cls} />
  return <TriangleAlert size={ICON_PX} aria-hidden="true" className={cls} />
}

/** Each count's words: "2 working", "1 waiting", "1 needs you". */
const COUNT_KEY: Record<Bucket, string> = {
  working: 'components.crewMode.ultra_row_count_working',
  waiting: 'components.crewMode.ultra_row_count_waiting',
  needs_you: 'components.crewMode.ultra_row_count_needs_you',
}

/** How many of a wrapping one-line box's items sit below its first line: the
 *  counts a narrow row could not fit. Re-measured when the box resizes and
 *  whenever `content` changes (a wider number can wrap one away while the box
 *  keeps its size). */
function useWrappedAwayCount<T extends HTMLElement>(content: string): { ref: RefObject<T>; hidden: number } {
  const ref = useRef<T>(null)
  const [hidden, setHidden] = useState(0)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') { setHidden(0); return }
    // The first item (the zero-width holder) sits on the visible line; any item
    // below it wrapped away.
    const measure = () => {
      const [first, ...rest] = [...el.children] as HTMLElement[]
      setHidden(first ? rest.filter(c => c.offsetTop > first.offsetTop).length : 0)
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [content])
  return { ref, hidden }
}

export function CrewRowCounts({ counts }: { counts: readonly CrewRowCount[] }) {
  const parts = counts.map(c => ({ ...c, text: i18nT(COUNT_KEY[c.bucket], { count: c.count }) }))
  const label = fmtList(parts.map(c => c.text))
  const { ref, hidden } = useWrappedAwayCount<HTMLSpanElement>(label)
  if (parts.length === 0) return null
  return (
    <>
      {/* Whole counts only: one that does not fit wraps onto a hidden second
          line, and an ellipsis after the line says some are hidden, so a hidden
          count never looks like none. It is narrow, so the urgent first count
          keeps its room. The full sentence is the label. */}
      <span ref={ref} className="min-w-0 flex flex-wrap content-start items-center gap-x-2 h-4 overflow-hidden" data-testid="crew-root-breakdown" role="img" aria-label={label} title={label}>
        {/* A zero-width first item holds the first line, so even the first
            count wraps away whole when the line is too narrow for it. Its
            negative margin cancels the gap, and no count carries a trailing
            margin, so the first count needs only its own width. */}
        <span aria-hidden="true" className="w-0 h-4 -mr-2" />
        {parts.map(c => (
          <span key={c.bucket} title={c.text} data-testid="crew-root-count" data-bucket={c.bucket}
            className="inline-flex items-center gap-1 h-4 text-[12px] whitespace-nowrap tabular-nums crew-tone-muted">
            {bucketIcon(c.bucket)}<span>{fmtNumber(c.count)}</span>
          </span>
        ))}
      </span>
      {hidden > 0 && <span aria-hidden="true" className="shrink-0 text-[12px] text-muted tabular-nums" data-testid="crew-root-hidden" title={label}>…</span>}
    </>
  )
}
