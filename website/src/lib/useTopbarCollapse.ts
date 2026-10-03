/**
 * Collapse trigger for the desktop top bar's FLOW layout (`.topbar.tb-flow` in
 * index.css).
 *
 * In the flow layout each side group is as wide as its content and the search
 * takes what is left, so the collapse ladder has to answer "does the row still
 * fit with the search at its minimum", not "how wide is my group". That is a
 * fact about a SIBLING of the groups, and a container query can only measure an
 * ancestor, so it is measured here.
 *
 * The level is chosen by trial: apply a candidate level, read whether the three
 * tracks fit the header's content box, and keep the LOWEST level that fits.
 * Fitting is monotone in the level and a pure function of the layout, so there is
 * no hysteresis and nothing oscillates. Every run happens before paint (a layout
 * effect, ResizeObserver and MutationObserver callbacks), so no intermediate level
 * is painted and no first-paint guess is needed.
 */
import { useLayoutEffect, useState, type RefObject } from 'react'

/** Rungs, cheapest first: level N hides what rungs 1..N hide. The ladder itself is
 *  the `.tb-flow.tbc-N` rules in index.css -- 1 metrics numbers -> waveform icon,
 *  2 feedback labels -> icons, 3 Back/Forward, 4 credits counter -> coin icon,
 *  5 the feedback pill, 6 crew chip names, 7 readout capsule -> connection dot,
 *  8 the active crew chip -- and test/useTopbarCollapse.test.ts pins those rules
 *  against this count. Level 9 hides nothing more: it is the floor for content no
 *  rung can hide (the open Windows menu, an extension widget), where the side
 *  tracks go back to equal halves and each group clips its own tail, so the row
 *  never overflows the header. It always fits. */
export const TOPBAR_MAX_LEVEL = 9

/** Below this level the search must stay on the header's centre line: the metrics
 *  numbers and the feedback labels give way before the search moves. From this
 *  level on the search may slide, then shrink, as the tracks allow. */
export const TOPBAR_CENTRED_UNTIL = 2

export function applyTopbarLevel(header: HTMLElement, level: number): void {
  for (let k = 1; k <= TOPBAR_MAX_LEVEL; k++) header.classList.toggle(`tbc-${k}`, k <= level)
  header.dataset.tbLevel = String(level)
}

/** Whether the resolved tracks plus gaps fit the header's content box. Reads
 *  `grid-template-columns`, which resolves to px track sizes, rather than
 *  `scrollWidth`, which is integer-rounded and folds in the header padding. A
 *  header with no layout (display:none, or a DOM without a layout engine) has
 *  nothing to fit against, so it keeps every item. A laid-out header whose
 *  resolved tracks cannot be read fits only at the floor: its `minmax(0,1fr)`
 *  sides clip at the tail by construction, where a stuck level 0 under
 *  `minmax(max-content,1fr)` sides would overflow the header. */
export function topbarFits(header: HTMLElement, level: number): boolean {
  if (header.clientWidth === 0) return true
  const cs = getComputedStyle(header)
  const tracks = cs.gridTemplateColumns.split(' ').map(parseFloat)
  if (tracks.length !== 3 || tracks.some(Number.isNaN)) return level >= TOPBAR_MAX_LEVEL
  const gap = parseFloat(cs.columnGap) || 0
  const used = tracks[0] + tracks[1] + tracks[2] + gap * 2
  const avail = header.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight)
  if (used > avail + 0.5) return false
  // Equal side tracks = search on the centre line.
  return level >= TOPBAR_CENTRED_UNTIL || Math.abs(tracks[0] - tracks[2]) <= 1
}

/** Settle on the lowest fitting level, starting from the current one so a steady
 *  resize costs one or two layouts rather than a full sweep. */
export function settleTopbarLevel(header: HTMLElement): number {
  const stored = Number(header.dataset.tbLevel)
  let level = Number.isInteger(stored) && stored >= 0 && stored <= TOPBAR_MAX_LEVEL ? stored : 0
  applyTopbarLevel(header, level)
  if (topbarFits(header, level)) {
    while (level > 0) {
      applyTopbarLevel(header, level - 1)
      if (!topbarFits(header, level - 1)) { applyTopbarLevel(header, level); break }
      level--
    }
  } else {
    while (level < TOPBAR_MAX_LEVEL) {
      applyTopbarLevel(header, ++level)
      if (topbarFits(header, level)) break
    }
  }
  return level
}

/**
 * Drive the ladder for `header` while `enabled` and return the settled level, so a
 * caller that reads a rung's verdict off the DOM (the metrics probe in
 * shell/topbar/metricsReadout.tsx) can re-read it when the level moves.
 *
 * Re-settles when the header resizes (window width), when a group's box resizes,
 * when a group gains or loses an element (a pill mounting or unmounting: a group
 * whose track holds a share of the spare room keeps its box, so only the mutation
 * says a lower level may fit again) and when a late font load changes text widths.
 * Text-only updates (a reading ticking over, a clock) are deliberately not watched:
 * they rarely move the level, and a change that grows a group past its share
 * resizes its box anyway.
 *
 * The level must stay a function of the layout alone. Nothing a caller renders
 * may change with the returned level, or the measurement feeds on itself: the
 * metrics control (`metricsSegment`, shell/topbar/metricsReadout.tsx) changes
 * only its behaviour with it, never its contents.
 */
export function useTopbarCollapse(header: RefObject<HTMLElement | null>, enabled: boolean): number {
  const [level, setLevel] = useState(0)
  useLayoutEffect(() => {
    const el = header.current
    if (!el) return
    if (!enabled) { applyTopbarLevel(el, 0); setLevel(0); return }
    const run = () => setLevel(settleTopbarLevel(el))
    run()
    const groups = [...el.children].filter(c => c.classList.contains('tb-left') || c.classList.contains('tb-right'))
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(run)
    ro?.observe(el)
    const mo = typeof MutationObserver === 'undefined' ? null : new MutationObserver(run)
    for (const g of groups) {
      ro?.observe(g)
      mo?.observe(g, { childList: true, subtree: true })
    }
    const fonts = typeof document === 'undefined' ? undefined : document.fonts
    fonts?.addEventListener?.('loadingdone', run)
    return () => {
      ro?.disconnect()
      mo?.disconnect()
      fonts?.removeEventListener?.('loadingdone', run)
    }
  }, [header, enabled])
  return level
}
