import { forwardRef, type ReactNode } from 'react'
import { ListFilter, X } from 'lucide-react'

import { SearchInput } from './ui'
import { cn } from '../lib/utils'

/**
 * The list panel's search row — the Sessions sidebar's search field with its
 * inline trailing controls (the sort/filter menu, the flat-view toggle), the
 * clear button that appears once there is text, and the input inset that
 * keeps the typed text clear of those controls. Shared with the Crew Members
 * roster so both lists carry the same field; `src/test/searchFilterBar.parity.test.tsx`
 * pins both pages to it.
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
  /** Wrapper spacing; the sidebar's `px-2 pt-2 pb-1` by default. */
  className?: string
  inputTestId?: string
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'placeholder' | 'className'>) {
  const n = Math.min(trailingCount ?? (trailing ? 1 : 0), INPUT_PAD_RIGHT.length - 1)
  const padRight = INPUT_PAD_RIGHT[n] + (value ? CLEAR_WIDTH : 0)
  return (
    <div className={cn('px-2 pt-2 pb-1', className)}>
      <div className="relative">
        <SearchInput
          className="w-full"
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
      </div>
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

/** Section label inside the filter menu (`FILTER`, `SORT BY`). */
export const FILTER_MENU_LABEL_CLS = 'text-[11px] uppercase tracking-[.04em]'

/** Width bounds of the filter menu: wide enough for a row, never past a phone viewport. */
export const FILTER_MENU_CONTENT_CLS = 'min-w-[180px] max-w-[calc(100vw-1rem)]'

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
export function FilterChip({ label, color, aggregate, clearLabel, onClear, testId }: {
  label: string
  /** The filter's colour token, e.g. `var(--accent)` or `var(--warn)`; omit for an aggregate chip. */
  color?: string
  /** Neutral clear-all pill (see above). */
  aggregate?: boolean
  /** Accessible name and tooltip: what the click does ("Clear Starred filter"). */
  clearLabel: string
  onClear: () => void
  testId?: string
}) {
  return (
    <button
      type="button"
      className={cn(
        'inline-flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors',
        aggregate && 'max-w-full bg-bg-elevated/60 border border-border text-muted hover:text-text',
      )}
      style={aggregate || !color ? undefined : { background: `color-mix(in srgb, ${color} 10%, transparent)`, color, borderWidth: 1, borderColor: `color-mix(in srgb, ${color} 30%, transparent)` }}
      onClick={onClear}
      title={clearLabel}
      aria-label={clearLabel}
      data-testid={testId}
    >
      {aggregate ? <span className="truncate">{label}</span> : label}
      <X size={11} className="shrink-0" />
    </button>
  )
}
