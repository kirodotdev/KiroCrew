import { useState, useMemo, useRef } from 'react'

/** Responsive column count from the container width (~300px target column). */
/** The page column's own horizontal padding at narrow widths (`px-4`, one side).
 *  Only used to seed the column estimate below; the real width is measured. */
const PAGE_GUTTER = 16

export function useColumnCount(
  minColWidth = 300,
): readonly [React.RefCallback<HTMLDivElement>, number] {
  // Seeded from the viewport instead of a constant because the page reads this
  // count to decide WHO OWNS THE SCROLL AXIS: a wrong first value hands the axis
  // over and takes it back a frame later, which reads as a jump. This is only an
  // estimate (it assumes the narrow gutter); the ResizeObserver corrects it
  // against the real element on attach either way.
  const [cols, setCols] = useState(() =>
    typeof window === 'undefined'
      ? 2
      : Math.max(1, Math.floor((window.innerWidth - PAGE_GUTTER * 2) / minColWidth)),
  )
  const observerRef = useRef<ResizeObserver | null>(null)
  // A CALLBACK ref, not a mount-time effect: an effect that reads `ref.current`
  // once on mount misses any grid rendered conditionally (behind query data or a
  // view-mode branch), leaving the viewport seed uncorrected for the life of the
  // component (#7193). The callback runs whenever the observed element actually
  // mounts or unmounts, so the observer attaches exactly when there is something
  // to observe. Recreated when `minColWidth` changes: React then detaches the old
  // callback (null) and attaches the new one, which re-measures with the current
  // width — the same re-run the old effect got from its `[minColWidth]` deps.
  const refFn = useMemo<React.RefCallback<HTMLDivElement>>(
    () => el => {
      observerRef.current?.disconnect()
      observerRef.current = null
      if (!el || typeof ResizeObserver === 'undefined') return
      // A zero clientWidth means the element has no layout yet (hidden, or a
      // layout-less test DOM): `floor(0 / minColWidth)` would collapse the
      // masonry to one column on a width nobody measured. Keep the current
      // estimate instead; the observer corrects it as soon as a real width
      // exists.
      const measure = () => {
        const w = el.clientWidth
        if (w > 0) setCols(Math.max(1, Math.floor(w / minColWidth)))
      }
      measure()
      const ro = new ResizeObserver(measure)
      ro.observe(el)
      observerRef.current = ro
    },
    [minColWidth],
  )
  return [refFn, cols] as const
}
