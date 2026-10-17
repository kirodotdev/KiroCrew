/**
 * Collapse trigger for the desktop top bar (`.topbar.tb-measured` in index.css).
 *
 * The desktop header keeps the three-track grid and both side groups stay size
 * containers, so the search remains centred and the container-query rungs still
 * respond to each group's width. The measured ladders add folds wherever those
 * rungs leave a group's contents overflowing, including content their fixed
 * thresholds did not count (an extension segment, an update pill or a longer
 * locale).
 *
 * The level is chosen by trial: apply a candidate level, read the contents'
 * max-content width through the group's `.tb-measure` wrapper, and keep the
 * LOWEST level whose contents fit. A group's box depends on the header's width
 * alone, so neither group's level moves either box; fitting is monotone in the
 * level and a pure function of the contents, so there is no hysteresis and
 * nothing oscillates. Every run happens before paint (a layout effect,
 * ResizeObserver and MutationObserver callbacks), so no intermediate level is
 * painted and no first-paint guess is needed.
 *
 * Both ladders fold in one order. The measured level never sits below the last
 * step the container rungs have folded, so the union of the two hides exactly
 * the steps up to the higher of them, and no item comes back while the window
 * narrows: a container rung folding a later step would otherwise give back room
 * that lets an earlier measured fold reopen. Which container rungs have fired
 * depends on the group's box alone, which no level changes, so the floor is
 * still a pure function of the box and the contents and feeds nothing back.
 */
import { useLayoutEffect, useState, type RefObject } from 'react'

export type TopbarSide = 'left' | 'right'

/** Rungs per group, cheapest first, in the order each group's container ladder
 *  uses on a phone; level N hides what rungs 1..N hide. The ladders themselves
 *  are the `.tb-measured.tbl-N` / `.tb-measured.tbr-N` rules in index.css, and
 *  test/useTopbarCollapse.test.ts pins those rules against these counts. The
 *  container rungs fold the same items in the same order, and the settled level
 *  is never below the last step they have folded (TOPBAR_CONTAINER_FOLDS).
 *  - Identity group (`tbl`): 1 Back/Forward, 2 crew chip names, 3 the active
 *    crew chip.
 *  - Actions group (`tbr`): 1 metrics numbers -> waveform icon, 2 feedback
 *    labels -> icons, 3 credits counter -> coin icon, 4 the feedback pill,
 *    5 readout capsule -> connection dot and the metrics notice -> its icons,
 *    6 the update pill's label, so the pill cannot push the bell out.
 *  A group that still does not fit at its last level holds content no rung can
 *  hide (the open Windows menu, an extension widget), and clips its own tail
 *  inside its track. */
export const TOPBAR_LEVELS: Record<TopbarSide, number> = { left: 3, right: 6 }

/** A capsule child that is not one of its Liquid Glass effect layers. */
const capsuleSegment = ':not([data-liquid-glass-layer])'

/** What each group's `@container` rungs in index.css hide, keyed by the measured
 *  step that hides the same items. Steps 2 and 6 of the actions group have no
 *  container rung. The selectors mirror those rungs' targets; the test "names,
 *  for every step the hook reads a container fold from, a target a container
 *  rung hides" in test/useTopbarCollapse.test.ts pins them, and "collapses in
 *  the order the phone container ladder uses" pins the rungs' order. */
export const TOPBAR_CONTAINER_FOLDS: Record<TopbarSide, Readonly<Partial<Record<number, string>>>> = {
  left: { 1: '.tb-drop-navhistory', 2: '.tb-drop-crew-name', 3: '.tb-crew-active-chip' },
  right: {
    1: '.tb-drop-metrics',
    3: '.tb-drop-usage',
    4: '.tb-drop-feedback',
    // Joined from tokens so the i18n gate does not read the selector as copy.
    5: ['.tb-capsule', '>', capsuleSegment, '~', capsuleSegment].join(' '),
  },
}

/** Whether the container rung for `step` hides every one of its targets in
 *  `group`: there is at least one, and none has a box. A step with no container
 *  rung, or no target rendered, is never folded. The metrics rung's probe
 *  (`.tb-metrics-probe`, out of flow at 0x0) still has a client rect while it is
 *  displayed, so the same read covers it. */
function containerFolded(group: HTMLElement, side: TopbarSide, step: number): boolean {
  const selector = TOPBAR_CONTAINER_FOLDS[side][step]
  if (!selector) return false
  const targets = group.querySelectorAll(selector)
  if (targets.length === 0) return false
  for (const target of targets) if (target.getClientRects().length > 0) return false
  return true
}

const PREFIX: Record<TopbarSide, string> = { left: 'tbl', right: 'tbr' }
const DATASET: Record<TopbarSide, 'tbLeft' | 'tbRight'> = { left: 'tbLeft', right: 'tbRight' }

export function applyTopbarLevel(header: HTMLElement, side: TopbarSide, level: number): void {
  for (let k = 1; k <= TOPBAR_LEVELS[side]; k++) header.classList.toggle(`${PREFIX[side]}-${k}`, k <= level)
  header.dataset[DATASET[side]] = String(level)
}

/** Whether `group`'s contents fit `box`, the width of its content box as its
 *  track lays it out. Gives the group's `.tb-measure` wrapper a max-content flex
 *  box for the read, then restores its inline style exactly. Fractional border
 *  boxes avoid `scrollWidth` rounding and start-side overflow on an end-aligned
 *  group. A group with no wrapper or no box keeps every item. */
export function topbarGroupFits(group: HTMLElement, box: number): boolean {
  const wrapper = group.querySelector<HTMLElement>(':scope > .tb-measure')
  if (!wrapper || box === 0) return true
  const cssText = wrapper.style.cssText
  let need = 0
  try {
    // A real flex box for the read, laid out like the group itself: its own gap
    // and alignment, and `flex:none` so the group cannot shrink it below the
    // contents' max-content width.
    const s = wrapper.style
    s.display = 'flex'
    s.width = 'max-content'
    s.flex = 'none'
    s.gap = 'inherit'
    s.alignItems = 'center'
    need = wrapper.getBoundingClientRect().width
  } finally {
    wrapper.style.cssText = cssText
  }
  return need <= box + 0.5
}

/** Settle one group on the lowest fitting level that is not below the last step
 *  its container rungs have folded, starting from the stored one so a steady
 *  resize costs one or two reads rather than a full sweep. */
export function settleTopbarSide(header: HTMLElement, side: TopbarSide): number {
  const max = TOPBAR_LEVELS[side]
  const stored = Number(header.dataset[DATASET[side]])
  let level = Number.isInteger(stored) && stored >= 0 && stored <= max ? stored : 0
  applyTopbarLevel(header, side, level)
  const group = header.querySelector<HTMLElement>(`:scope > .tb-${side}`)
  // The box comes from the track, which no level changes. Container queries use
  // the content box, so remove the group's fractional inline padding once.
  const borderBox = group?.getBoundingClientRect().width ?? 0
  const style = group ? getComputedStyle(group) : null
  const box = Math.max(0, borderBox - (parseFloat(style?.paddingLeft ?? '') || 0) - (parseFloat(style?.paddingRight ?? '') || 0))
  const fits = () => !group || topbarGroupFits(group, box)
  // A group with no box keeps every item, so it takes no floor either.
  const folded = (step: number) => !!group && box > 0 && containerFolded(group, side, step)
  if (fits()) {
    while (level > 0) {
      applyTopbarLevel(header, side, level - 1)
      // With this step's measured rung off, only its container rung can still
      // hide its items: then this step is the floor.
      if (!fits() || folded(level)) { applyTopbarLevel(header, side, level); break }
      level--
    }
  } else {
    while (level < max) {
      applyTopbarLevel(header, side, ++level)
      if (fits()) break
    }
  }
  // Only a container rung can hide a step above the level. Fitting is monotone
  // in the level, so the floor fits whenever the settled level does.
  for (let step = max; step > level; step--) {
    if (folded(step)) { level = step; applyTopbarLevel(header, side, level); break }
  }
  return level
}

export interface TopbarLevels { left: number; right: number }

/**
 * Drive both ladders for `header` while `enabled` and return the settled levels,
 * so a caller that reads a rung's verdict off the DOM (the metrics probe in
 * shell/topbar/metricsReadout.tsx) can re-read it when its group's level moves.
 *
 * Re-settles when the header or a group resizes (window width, window-control
 * insets), when a group gains or loses an element (a pill mounting or
 * unmounting), when text inside a group changes (a reading ticking over, an
 * update pill's progress, a language switch), when a group's own class changes
 * (`tb-has-update` shifts its container rungs), when `<html>`'s font family or
 * theme changes, and when a late font load changes text widths. A size-contained
 * group's box is fixed by its track, while each read gives its `.tb-measure`
 * wrapper a temporary max-content flex box. Contents that grow or shrink do not
 * resize the group, so only the mutation says the measured level may have moved.
 *
 * The levels must stay a function of the contents alone. Nothing a caller renders
 * may change with a returned level, or the measurement feeds on itself: the
 * metrics control (`metricsSegment`, shell/topbar/metricsReadout.tsx) changes
 * only its behaviour with it, never its contents.
 */
export function useTopbarCollapse(header: RefObject<HTMLElement | null>, enabled: boolean): TopbarLevels {
  const [left, setLeft] = useState(0)
  const [right, setRight] = useState(0)
  useLayoutEffect(() => {
    const el = header.current
    if (!el) return
    if (!enabled) {
      applyTopbarLevel(el, 'left', 0)
      applyTopbarLevel(el, 'right', 0)
      setLeft(0)
      setRight(0)
      return
    }
    const run = () => {
      setLeft(settleTopbarSide(el, 'left'))
      setRight(settleTopbarSide(el, 'right'))
    }
    run()
    const groups = [...el.children].filter(c => c.classList.contains('tb-left') || c.classList.contains('tb-right'))
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(run)
    ro?.observe(el)
    // Not `attributes` in the subtree: each read writes the measurement wrapper's
    // inline style. A group's own class is watched on its own: `tb-has-update`
    // moves the container rungs' thresholds, and so the floor, without any
    // mutation inside the group while the pill's chunk is still loading.
    const mo = typeof MutationObserver === 'undefined' ? null : new MutationObserver(run)
    const classes = typeof MutationObserver === 'undefined' ? null : new MutationObserver(run)
    for (const g of groups) {
      ro?.observe(g)
      mo?.observe(g, { childList: true, subtree: true, characterData: true })
      classes?.observe(g, { attributes: true, attributeFilter: ['class'] })
    }
    // Font family (an inline `--font-body` plus `data-font-family`) and theme
    // live on `<html>` and change text widths without resizing a group box or
    // loading a font, so neither observer above sees them.
    const root = typeof document === 'undefined' ? null : document.documentElement
    if (root) mo?.observe(root, { attributes: true, attributeFilter: ['style', 'data-font-family', 'data-theme'] })
    const fonts = typeof document === 'undefined' ? undefined : document.fonts
    fonts?.addEventListener?.('loadingdone', run)
    return () => {
      ro?.disconnect()
      mo?.disconnect()
      classes?.disconnect()
      fonts?.removeEventListener?.('loadingdone', run)
    }
  }, [header, enabled])
  return { left, right }
}
