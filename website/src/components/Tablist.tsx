/**
 * Tablist — the pill switcher as a WAI-ARIA tablist and NOTHING ELSE: no
 * `aria-controls`, because there is no per-tab panel to point at.
 *
 * Use `ui/tabs.tsx` (Radix) by default. Reach for this one only when the body
 * below the rail is ONE shared subtree parameterised by the active tab rather
 * than a panel per tab — the Webhooks page is the case: its rail, resize handle,
 * mobile collapse and banners are shared, and `plane` merely parameterises them
 * at a dozen points. Radix's `TabsTrigger` emits `aria-controls`
 * UNCONDITIONALLY, so putting that page on Radix without a `TabsContent` would
 * leave every trigger referencing an element that does not exist — trading one
 * accessibility defect for another. A tablist with no panel linkage is the
 * correct shape there, and `aria-controls` is recommended by WAI-ARIA rather
 * than required.
 *
 * Visually identical to `ui/tabs.tsx` by construction, not by promise: both
 * import the one class recipe in `ui/tabsPill.ts`, so there is no second copy to
 * drift. A user cannot see which accessibility shape is underneath, so they must
 * not be able to see a difference either.
 *
 * What this carries that a row of plain buttons does not:
 *
 *   - **Keyboard.** Arrow keys move between tabs under a roving tabindex, so the
 *     whole rail is ONE tab stop rather than one stop per tab.
 *   - **`aria-selected`.** A coloured pill is invisible to a screen reader.
 *   - **A shared indicator.** One `layoutId` element slides between segments, so
 *     the movement reads as a position change rather than two colour flips.
 */
import { useRef, type ReactNode } from 'react'
import { motion, useReducedMotion } from 'framer-motion'

import {
  TABS_COUNT_BASE_CLASS,
  TABS_INDICATOR_CLASS,
  TABS_INDICATOR_SPRING,
  TABS_SEGMENT_ACTIVE_CLASS,
  TABS_SEGMENT_CLASS,
  TABS_SEGMENT_DISABLED_CLASS,
  TABS_TRACK_CLASS,
} from './ui/tabsPill'

export interface TablistTab<T extends string = string> {
  key: T
  label: string
  icon?: ReactNode
  /**
   * Render the tab but refuse selection — for a plane the page knows about and
   * cannot serve yet. `aria-disabled` rather than the `disabled` attribute keeps
   * it focusable so its tooltip, which carries the reason, stays reachable.
   */
  disabled?: boolean
  tooltip?: string
  /** Trailing count, for a rail whose tabs each own a collection. Omitted and
   *  zero both render nothing — a "0" badge is noise, not information. */
  count?: number
}

interface Props<T extends string = string> {
  tabs: Array<TablistTab<T>>
  value: T
  onChange: (value: T) => void
  /** Names the tablist for assistive tech. Required: an unlabelled tablist on a
   *  page with more than one is ambiguous to a screen-reader user. */
  ariaLabel: string
  /** Distinguishes this rail's sliding indicator from another one on the same
   *  page — two rails sharing an id animate into each other. */
  layoutId?: string
}

/** Next enabled index in `dir`, wrapping. Disabled tabs are skipped rather than
 *  focused-and-refused, which would strand the caret on a dead control. */
export function nextEnabledIndex<T extends string>(
  tabs: Array<TablistTab<T>>,
  from: number,
  dir: 1 | -1,
): number {
  const n = tabs.length
  for (let step = 1; step <= n; step += 1) {
    const i = (from + dir * step + n * (step + 1)) % n
    if (!tabs[i]?.disabled) return i
  }
  return from
}

/** First or last enabled index, for Home / End. */
export function edgeEnabledIndex<T extends string>(
  tabs: Array<TablistTab<T>>,
  edge: 'first' | 'last',
): number {
  const order = edge === 'first' ? tabs.map((_, i) => i) : tabs.map((_, i) => tabs.length - 1 - i)
  for (const i of order) {
    if (!tabs[i]?.disabled) return i
  }
  return 0
}

export default function Tablist<T extends string = string>({
  tabs,
  value,
  onChange,
  ariaLabel,
  layoutId = 'tablist-indicator',
}: Props<T>) {
  const reduceMotion = useReducedMotion()
  const refs = useRef<Array<HTMLButtonElement | null>>([])

  const move = (to: number) => {
    const tab = tabs[to]
    if (!tab || tab.disabled) return
    onChange(tab.key)
    // Follow the selection with focus so the arrow keys keep working from the
    // tab the user just landed on.
    refs.current[to]?.focus()
  }

  return (
    <div className={TABS_TRACK_CLASS} role="tablist" aria-label={ariaLabel}>
      {tabs.map((tab, i) => {
        const isActive = tab.key === value
        const isDisabled = tab.disabled === true
        return (
          <button
            key={tab.key}
            ref={el => {
              refs.current[i] = el
            }}
            type="button"
            role="tab"
            aria-selected={isActive}
            aria-disabled={isDisabled || undefined}
            // Roving tabindex: only the active tab is in the tab order, so the
            // rail costs one Tab press instead of one per plane.
            tabIndex={isActive ? 0 : -1}
            title={tab.tooltip || tab.label}
            onClick={() => {
              if (!isDisabled) onChange(tab.key)
            }}
            onKeyDown={e => {
              if (e.key === 'ArrowRight') {
                e.preventDefault()
                move(nextEnabledIndex(tabs, i, 1))
              } else if (e.key === 'ArrowLeft') {
                e.preventDefault()
                move(nextEnabledIndex(tabs, i, -1))
              } else if (e.key === 'Home') {
                e.preventDefault()
                move(edgeEnabledIndex(tabs, 'first'))
              } else if (e.key === 'End') {
                e.preventDefault()
                move(edgeEnabledIndex(tabs, 'last'))
              }
            }}
            className={[
              TABS_SEGMENT_CLASS,
              isDisabled ? TABS_SEGMENT_DISABLED_CLASS : '',
              isActive && !isDisabled ? TABS_SEGMENT_ACTIVE_CLASS : '',
            ].filter(Boolean).join(' ')}
          >
            {isActive && !isDisabled && (
              <motion.span
                layoutId={layoutId}
                aria-hidden="true"
                className={TABS_INDICATOR_CLASS}
                transition={reduceMotion ? { duration: 0 } : TABS_INDICATOR_SPRING}
              />
            )}
            {tab.icon}
            <span className="whitespace-nowrap">{tab.label}</span>
            {(tab.count ?? 0) > 0 && (
              <span
                className={`${TABS_COUNT_BASE_CLASS} ${isActive ? 'text-accent/60' : 'text-muted/40'}`}
              >
                {tab.count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
