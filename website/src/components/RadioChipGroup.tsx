import React, { useLayoutEffect, useRef } from 'react'

/**
 * One-of-N choice drawn as a row of buttons (chips, swatches, mode buttons),
 * announced and operated as a radio group (WAI-ARIA radio pattern).
 *
 * Why not `aria-pressed`: `aria-pressed` reads as N independent on/off toggles,
 * so a screen-reader user hears "pressed" / "not pressed" per button and is
 * never told the buttons are one single-select set (#27459). A genuine two-state
 * toggle (one button that is on or off, or a pair where pressing the active one
 * clears it) keeps `aria-pressed` and does not use this group.
 *
 * How to use: wrap the options in `<RadioChipGroup>` with a name
 * (`ariaLabel` or `ariaLabelledBy`), and give each option `{...radioChip(checked)}`
 * instead of `aria-pressed`. Options must be focusable elements (`<button>`).
 *
 * The group owns the keyboard behaviour so no call site re-implements it:
 * - One Tab stop. While focus is inside the group it is the focused option;
 *   otherwise it is the checked option, or the first enabled one when none is
 *   checked. The group sets each option's `tabIndex` itself, so options must
 *   NOT pass their own `tabIndex` prop.
 * - Arrow keys (Right/Down next, Left/Up previous, wrapping) and Home/End move
 *   focus; disabled options are skipped. By default they do NOT select: many
 *   consumers save on every change (settings write config, a palette saves and
 *   closes, the guide's pick list commits and unmounts), so passing over an
 *   option must never persist it. Space/Enter (the native button click)
 *   chooses. This is the rule `SegmentedControl` already follows.
 * - `selectOnArrow` opts a group into "arrow moves AND selects" (the native
 *   radio behaviour), for a group where a pick is cheap and reversible and
 *   that behaviour is already established (the sidebar duration chips, #22330).
 * - The key is consumed (`stopPropagation`) so a host Radix menu does not also
 *   treat it as its own item-to-item navigation and pull focus out.
 */
export function RadioChipGroup({ ariaLabel, ariaLabelledBy, className, style, children, onKeyDown: ownKeyDown, selectOnArrow = false, 'data-testid': testId }: {
  ariaLabel?: string
  ariaLabelledBy?: string
  className?: string
  style?: React.CSSProperties
  children: React.ReactNode
  /** The caller's own key handling (e.g. Escape closes a popover palette).
   *  Runs first; arrow/Home/End handling is skipped when it calls
   *  `preventDefault()`. */
  onKeyDown?: (e: React.KeyboardEvent<HTMLDivElement>) => void
  /** Arrow/Home/End also select (click) the option they move to. Off by
   *  default; see the component comment for when it is safe. */
  selectOnArrow?: boolean
  'data-testid'?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  // No dependency list on purpose: the checked option can change on any render
  // of the owner, and this group re-renders with it. Writing the same tabIndex
  // again is a no-op for the browser.
  useLayoutEffect(() => {
    if (ref.current) placeTabStop(ref.current)
  })

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    ownKeyDown?.(e)
    if (e.defaultPrevented) return
    const isEdge = e.key === 'Home' || e.key === 'End'
    if (!isEdge && RADIO_KEY_STEP[e.key] === undefined) return
    const radios = ownRadios(e.currentTarget).filter(r => !isDisabled(r))
    if (radios.length === 0) return
    e.preventDefault()
    e.stopPropagation()
    const current = radios.indexOf(document.activeElement as HTMLElement)
    const next = e.key === 'Home' ? 0
      : e.key === 'End' ? radios.length - 1
        : (Math.max(current, 0) + RADIO_KEY_STEP[e.key] + radios.length) % radios.length
    radios[next].focus()
    if (selectOnArrow) radios[next].click()
  }

  return (
    // eslint-disable-next-line jsx-a11y/interactive-supports-focus -- a radiogroup is not itself a Tab stop; focus lives on its one tabbable radio (roving tabindex), and this handler only routes arrow keys between them
    <div ref={ref} role="radiogroup" aria-label={ariaLabel} aria-labelledby={ariaLabelledBy}
      className={className} style={style} onKeyDown={onKeyDown} data-testid={testId}
      // The Tab stop follows focus while it is inside the group, so Tab from an
      // option the arrows moved to leaves the group instead of stepping back to
      // the checked one; once focus leaves, the stop returns to the checked one.
      onFocus={e => placeTabStop(e.currentTarget)}
      onBlur={e => {
        const group = e.currentTarget
        if (!group.contains(e.relatedTarget as Node | null)) placeTabStop(group, true)
      }}>
      {children}
    </div>
  )
}

/** The attributes an option inside a `RadioChipGroup` carries in place of
 *  `aria-pressed`. */
export function radioChip(checked: boolean): { role: 'radio'; 'aria-checked': boolean } {
  return { role: 'radio', 'aria-checked': checked }
}

/** Arrow-key step through a radio group (WAI-ARIA radio pattern: Right/Down is
 *  the next radio, Left/Up the previous, wrapping at either end). */
const RADIO_KEY_STEP: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }

/** Give exactly one of the group's own enabled radios `tabIndex` 0: the
 *  focused one while focus is inside (unless `leaving`), else the checked one,
 *  else the first. */
function placeTabStop(group: HTMLElement, leaving = false) {
  const radios = ownRadios(group)
  const enabled = radios.filter(r => !isDisabled(r))
  const focused = leaving ? undefined : enabled.find(r => r === document.activeElement)
  const stop = focused ?? enabled.find(r => r.getAttribute('aria-checked') === 'true') ?? enabled[0]
  for (const r of radios) r.tabIndex = r === stop ? 0 : -1
}

/** This group's own radios, not those of a radio group nested inside it. */
function ownRadios(group: HTMLElement): HTMLElement[] {
  return Array.from(group.querySelectorAll<HTMLElement>('[role="radio"]'))
    .filter(r => r.closest('[role="radiogroup"]') === group)
}

function isDisabled(el: HTMLElement): boolean {
  return (el as HTMLButtonElement).disabled === true || el.getAttribute('aria-disabled') === 'true'
}
