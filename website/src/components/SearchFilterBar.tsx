import { forwardRef, useId, type ComponentProps, type ReactNode } from 'react'
import { ListFilter, Pause, Search, X } from 'lucide-react'

import { Glass } from './Glass'
import { DropdownMenuContent, DropdownMenuLabel } from './ui/dropdown-menu'
import { cn } from '../lib/utils'

/**
 * The list panel's search row — the Sessions sidebar's search field with its
 * inline trailing controls (the sort/filter menu, the flat-view toggle), the
 * clear button that appears once there is text, and the input inset that
 * keeps the typed text clear of those controls. Shared with the Crew Members
 * roster so both lists carry the same field; `src/test/searchFilterBar.parity.test.tsx`
 * pins both pages to it.
 *
 * The field is a Liquid Glass pane (components/Glass.tsx, the chip recipe at a
 * 10px radius) and floats over the list inside a `ListDock`,
 * so the rows scroll under it the way the transcript scrolls under the
 * composer. The `<input>` inside is transparent: the pane IS the field's
 * surface. Like every glass pane (the composer, the mobile Settings capsule)
 * it does not change on focus -- maintainer decision, no ring, no tint or edge
 * step; the caret is the indicator -- so the input carries no ring of its own.
 *
 * Trailing controls are 24px (`w-6`) buttons in a `gap-0.5` row 4px from the
 * field's right edge. The input's right padding and the clear button's
 * offset step with how many there are — these are the sidebar's own values,
 * carried as a table so the field is pixel-identical wherever it is mounted.
 * Three entries — none, one, two controls — because two is the most any
 * mount has (the sidebar's menu + folder toggle); a wider row adds its entry.
 */
const INPUT_PAD_RIGHT = [12, 36, 56] as const
const CLEAR_RIGHT = [8, 32, 56] as const
const CLEAR_WIDTH = 20
/** Rounded, not a capsule: the list rows under it are `rounded-md` boxes, and a
 *  32px field at the composer's 16px would read as a pill among rectangles. */
const FIELD_RADIUS = 10

export function SearchFilterBar({
  value, onChange, placeholder, clearLabel, trailing, trailingCount, className, inputTestId, ...inputProps
}: {
  value: string
  onChange: (next: string) => void
  placeholder: string
  /** Accessible name of the clear button (shown only while `value` is non-empty). */
  clearLabel: string
  /** Inline controls docked in the field's right edge — `FilterMenuButton` and
   *  friends. Pass `trailingCount` when the count is not one control per child. */
  trailing?: ReactNode
  trailingCount?: number
  /** Wrapper spacing; `px-2 pt-2` by default. No bottom padding: inside the
   *  ListDock the pane's bottom edge IS the dock's edge, so rows scrolling
   *  under emerge at the glass, not from a strip of gap below it. */
  className?: string
  inputTestId?: string
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'placeholder' | 'className'>) {
  const n = Math.min(trailingCount ?? (trailing ? 1 : 0), INPUT_PAD_RIGHT.length - 1)
  const padRight = INPUT_PAD_RIGHT[n] + (value ? CLEAR_WIDTH : 0)
  return (
    <div className={cn('px-2 pt-2', className)}>
      <Glass variant="chip" radius={FIELD_RADIUS} className="glass-shadow w-full" data-testid="search-field-glass">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted pointer-events-none" aria-hidden="true" />
        <input
          /* focus-cue-ok: maintainer decision -- the Glass host does not change on focus (no ring, no tint or edge step, never the accent), the same rule as the composer and the mobile Settings capsule; the caret is the indicator, and a ring on the input itself is not wanted. */
          className="w-full bg-transparent border-none rounded-full pl-8 py-1.5 text-text text-[13px] font-body outline-hidden placeholder:text-muted/70"
          placeholder={placeholder}
          value={value}
          onChange={e => onChange(e.target.value)}
          style={{ paddingRight: padRight }}
          data-testid={inputTestId}
          {...inputProps}
        />
        {value && (
          <button
            type="button"
            className="absolute top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors"
            style={{ right: CLEAR_RIGHT[n] }}
            onClick={() => onChange('')}
            aria-label={clearLabel}
            data-testid={inputTestId ? `${inputTestId}-clear` : undefined}
          >
            <X size={13} />
          </button>
        )}
        {trailing && (
          <div className="absolute right-1 inset-y-0 flex items-center gap-0.5">
            {trailing}
          </div>
        )}
      </Glass>
    </div>
  )
}

/** The 24px icon button that opens a list's sort/filter menu, with the
 *  sidebar's count badge at its corner. Render it as the `asChild` child of a
 *  `DropdownMenuTrigger`; the ref forwards so Radix can anchor the menu. */
export const FilterMenuButton = forwardRef<HTMLButtonElement, {
  title: string
  badge?: number
  testId?: string
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'title' | 'children'>>(
  function FilterMenuButton({ title, badge = 0, testId, className, ...rest }, ref) {
    return (
      <button
        ref={ref}
        type="button"
        className={cn('relative w-6 h-6 rounded text-muted flex items-center justify-center cursor-pointer transition-colors hover:text-text hover:bg-bg-hover bg-transparent border-none', className)}
        title={title}
        aria-label={rest['aria-label'] ?? title}
        data-testid={testId}
        {...rest}
      >
        <ListFilter size={14} />
        {badge > 0 && (
          <span
            aria-hidden="true"
            className="absolute -top-1 -right-1 min-w-[14px] h-[14px] px-[3px] rounded-full bg-accent text-accent-fg text-[10px] font-semibold leading-[14px] text-center pointer-events-none shadow-[0_0_4px_var(--accent-glow)]"
          >
            {badge > 99 ? '99+' : badge}
          </span>
        )}
      </button>
    )
  },
)

/** Section label inside the filter menu (`FILTER`, `SORT BY`). A component
 *  rather than an exported class string so the design-system lint can read the
 *  classes where they are applied: it follows a className only within a file. */
export function FilterMenuLabel({ className, ...props }: ComponentProps<typeof DropdownMenuLabel>) {
  return <DropdownMenuLabel className={cn('text-[11px] uppercase tracking-[.04em]', className)} {...props} />
}

/** The filter menu's panel: wide enough for a row, never past a phone viewport.
 *  Radix sizes the popper wrapper to `max-content`, so without the cap the
 *  inline pickers' caption sentences (a phone renders them here instead of in
 *  a flyout) would stretch the menu past the screen edge. */
export function FilterMenuContent({ className, ...props }: ComponentProps<typeof DropdownMenuContent>) {
  return <DropdownMenuContent className={cn('min-w-[180px] max-w-[calc(100vw-1rem)]', className)} {...props} />
}

/** The row of dismissible chips under a list's search row while any filter is
 *  on — the at-rest marker that the list is narrowed. `px-3` is one step wider
 *  than the search row's `px-2` so the pills sit inside the field's edges. */
export const FILTER_CHIP_ROW_CLS = 'px-3 pb-1 flex items-center gap-1.5 flex-wrap'

/** One filter pill under a list's search row; the click clears what it names.
 *  Two shapes, two meanings, the same on every list:
 *  - coloured (pass `color`): ONE filter in its own colour — the click clears
 *    that filter only (the sidebar's per-filter pills);
 *  - `aggregate`: a neutral pill naming SEVERAL filters at once — the click
 *    clears all of them (the sidebar's tag filter, the roster's filter chip).
 *  `label` is the caller's — the sidebar appends its window and count, the
 *  roster its counts — so one recipe carries whatever a list has to say about
 *  the filter without knowing what it is. */
export function FilterChip({ label, color, aggregate, paused, pausedLabel, clearLabel, onClear, testId }: {
  label: string
  /** The filter's colour token, e.g. `var(--accent)` or `var(--warn)`; omit for an aggregate chip. */
  color?: string
  /** Neutral clear-all pill (see above). */
  aggregate?: boolean
  /** The filter is still set but lifted, so it is not narrowing the list right
   *  now (the sidebar's menu can pause every status filter at once). A mark on
   *  the chip, never a second meaning for the click: the click still clears. */
  paused?: boolean
  /** WHY the filter is lifted, when the person did not lift it themselves (the
   *  sidebar's chips while a search is typed: "Paused while searching"). Implies
   *  `paused`, so the chip wears the one paused mark, and adds the reason where
   *  the mark alone cannot say it: ahead of the clear action in the tooltip,
   *  and as an `aria-describedby` text for focus (a native tooltip never shows
   *  on focus). The click still clears, so the accessible name stays
   *  `clearLabel`. Omit it for a pause the person chose from the menu: that
   *  pause needs no explaining. */
  pausedLabel?: string
  /** Accessible name and tooltip: what the click does ("Clear Starred filter"). */
  clearLabel: string
  onClear: () => void
  testId?: string
}) {
  const pausedId = useId()
  const isPaused = paused || !!pausedLabel
  return (
    <button
      type="button"
      className={cn(
        'inline-flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors',
        aggregate && 'max-w-full bg-bg-elevated/60 border border-border text-muted hover:text-text',
        // Same colour token paused or not, so the chip still reads as ITS
        // filter; the glyph and the dashed border say "not narrowing right
        // now" without leaning on contrast alone.
        isPaused && 'border-dashed',
      )}
      style={aggregate || !color ? undefined : { background: `color-mix(in srgb, ${color} 10%, transparent)`, color, borderWidth: 1, borderColor: `color-mix(in srgb, ${color} 30%, transparent)` }}
      onClick={onClear}
      // Paused keeps the clear action in the hover text: the X glyph alone does
      // not say a click clears, and the pause text alone would drop that.
      title={pausedLabel ? `${pausedLabel} · ${clearLabel}` : clearLabel}
      aria-label={clearLabel}
      aria-describedby={pausedLabel ? pausedId : undefined}
      data-testid={testId}
    >
      {isPaused && <Pause size={11} className="shrink-0" aria-hidden="true" />}
      {pausedLabel && <span id={pausedId} className="sr-only">{pausedLabel}</span>}
      {aggregate ? <span className="truncate">{label}</span> : label}
      <X size={11} className="shrink-0" />
    </button>
  )
}
