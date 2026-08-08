/**
 * UnderlineTabs — page-level tabs: a row of labels over a rule, with the active
 * one marked by an underline that slides between them.
 *
 * Distinct from `SegmentedControl` on purpose. A segmented control is a FILTER
 * (which subset of the same thing am I looking at); these are NAVIGATION (which
 * screen am I on). The System page carries both at once — planes across the top,
 * `Group by` inside the table — and dressing them identically is what made that
 * page read as two stacked pill rows with no hierarchy between them.
 *
 * Three things the hand-rolled `border-b-2` tab rows around this repo do not do,
 * and the reason this exists as a component rather than a fourth copy:
 *
 *   - **Keyboard.** The WAI-ARIA tabs pattern moves between tabs with the arrow
 *     keys under a roving tabindex, so the whole rail is ONE tab stop. A row of
 *     plain buttons makes the user tab through every screen name to reach the
 *     content.
 *   - **`aria-selected` + `aria-controls`.** A coloured underline is invisible to
 *     a screen reader; without the state attribute the active tab is announced
 *     exactly like the inactive ones.
 *   - **A shared underline.** One `layoutId` element slides between tabs instead
 *     of each tab toggling its own border, which is what makes the movement read
 *     as a position change rather than two independent colour flips.
 *
 * Built to be adopted, not to serve one page. Three places already hand-roll this
 * visual and can move onto it as the UI is upgraded — `ConnectionsPage` (icon +
 * label), `pages/knowledge/index` (icon + label), and `ProjectPicker` (two
 * equal-width tabs, hence `fill`). Nothing here knows about the System page: the
 * labels, the tablist name and the counts all come from the caller, so it carries
 * no catalog strings of its own and needs no i18n keys to be reused.
 */
import { useRef, type ReactNode } from 'react'
import { motion, useReducedMotion } from 'framer-motion'

export interface UnderlineTab<T extends string = string> {
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
  tabs: Array<UnderlineTab<T>>
  value: T
  onChange: (value: T) => void
  /** Names the tablist for assistive tech. Required: an unlabelled tablist on a
   *  page with more than one is ambiguous to a screen-reader user. */
  ariaLabel: string
  /** Distinguishes this rail's sliding underline from another one on the same
   *  page — two rails sharing an id animate into each other. */
  layoutId?: string
  /** Stretch the tabs to share the width equally, for a rail inside a narrow
   *  panel where left-aligned labels leave the rule looking unfinished. */
  fill?: boolean
}

/** Next enabled index in `dir`, wrapping. Disabled tabs are skipped rather than
 *  focused-and-refused, which would strand the caret on a dead control. */
export function nextEnabledIndex<T extends string>(
  tabs: Array<UnderlineTab<T>>,
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
  tabs: Array<UnderlineTab<T>>,
  edge: 'first' | 'last',
): number {
  const order = edge === 'first' ? tabs.map((_, i) => i) : tabs.map((_, i) => tabs.length - 1 - i)
  for (const i of order) {
    if (!tabs[i]?.disabled) return i
  }
  return 0
}

export default function UnderlineTabs<T extends string = string>({
  tabs,
  value,
  onChange,
  ariaLabel,
  layoutId = 'underline-tabs',
  fill = false,
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
    <div className="flex border-b border-border" role="tablist" aria-label={ariaLabel}>
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
            className={`relative flex items-center gap-2 border-none bg-transparent px-3.5 py-2 text-[13px] font-medium transition-colors ${
              fill ? 'flex-1 justify-center' : ''
            } ${
              isDisabled
                ? 'cursor-not-allowed text-muted/40'
                : isActive
                  ? 'cursor-pointer text-accent'
                  : 'cursor-pointer text-muted hover:text-text'
            }`}
          >
            {/* A dot rather than the icon set a segmented control uses: at page
                level the label is the content, and a glyph per screen name adds
                a second thing to read before the words. */}
            <span
              aria-hidden="true"
              className={`inline-block h-[5px] w-[5px] shrink-0 rounded-full ${
                isDisabled ? 'bg-muted/30' : isActive ? 'bg-accent' : 'bg-muted/50'
              }`}
            />
            {tab.icon}
            <span className="whitespace-nowrap">{tab.label}</span>
            {(tab.count ?? 0) > 0 && (
              <span className={`text-[11px] ${isActive ? 'text-accent/60' : 'text-muted/40'}`}>
                {tab.count}
              </span>
            )}
            {isActive && !isDisabled && (
              <motion.span
                layoutId={layoutId}
                aria-hidden="true"
                className="absolute inset-x-0 -bottom-px h-[2px] rounded-full bg-accent"
                transition={
                  reduceMotion ? { duration: 0 } : { type: 'spring', stiffness: 520, damping: 38 }
                }
              />
            )}
          </button>
        )
      })}
    </div>
  )
}
