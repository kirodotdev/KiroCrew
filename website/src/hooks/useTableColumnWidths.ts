// User-resizable widths for the columns of a FIXED-LAYOUT data table.
//
// The layout-column twin is hooks/useColumnResize (a rail beside a pane). A
// table differs in three ways, which is why it is its own hook rather than a
// mode of that one: there are many columns behind one storage key, a column
// never collapses, and the widths are a CONTRACT the table's min-width is
// derived from (see SchedulePage's jobs table) rather than a free number.
//
// The model it assumes is the one that contract already describes: every
// resizable column declares a px `base`, exactly one column declares no width
// and absorbs the spare, and the table's min-width is the sum of the px columns
// plus a floor for that residual. Under that model a resize is exact: widening
// a column by N px moves the table's min-width by the same N (`extra`), so the
// residual keeps its floor and the pixels come out of horizontal scroll instead
// of being silently taken from a neighbour.
//
// An untouched table renders byte-identically to one without this hook:
// `style()` returns undefined and `extra` is 0 until the user drags something,
// so the declared `w-[Npx]` classes stay the single source of the defaults.
import { useCallback, useMemo, useRef, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'

export interface TableColumnSpec {
  /** The declared default width in px: the same number as the column's
   *  `w-[Npx]` class. */
  base: number
  /** Narrowest the user may drag to. Defaults to `DEFAULT_MIN`. */
  min?: number
  /** Widest the user may drag to. Defaults to `DEFAULT_MAX`. */
  max?: number
}

/** Wide enough for the grip plus a couple of glyphs, so a column can be parked
 *  out of the way but never dragged to nothing. */
const DEFAULT_MIN = 48
/** A ceiling, not a target: past this a single column is wider than most
 *  viewports and the only way back is a long scroll to find its grip. */
const DEFAULT_MAX = 720

export interface ColumnResizerBinding {
  /** Current width in px (the override, or the base). */
  value: number
  min: number
  max: number
  /** Live update while dragging or nudging; `commit` persists. */
  onResize: (width: number, commit: boolean) => void
  /** Drop this column's override, returning it to its declared base. */
  onReset: () => void
}

export interface TableColumnWidths<K extends string> {
  /** Inline style for a column's header cell: undefined until overridden, so
   *  the declared class keeps owning the default. */
  style: (key: K) => { width: number } | undefined
  /** Sum of (override − base) across columns; add it to the table's declared
   *  min-width. May be negative when columns were narrowed. */
  extra: number
  /** Props for the column's `<ColumnResizer>`. */
  resizer: (key: K) => ColumnResizerBinding
  /** True when any column carries an override. */
  customized: boolean
  /** Drop every override. */
  reset: () => void
}

function clampTo(spec: TableColumnSpec, width: number): number {
  const min = spec.min ?? DEFAULT_MIN
  const max = spec.max ?? DEFAULT_MAX
  return Math.round(Math.min(max, Math.max(min, width)))
}

/** Read persisted overrides, keeping only entries that still make sense.
 *
 * An unknown key or an out-of-range width is DISCARDED rather than clamped, for
 * the reason lib/columnWidth gives: it was written under a different column set
 * or different bounds, so the declared default is a better guess than the
 * nearest legal value. A blocked store or malformed JSON falls back to no
 * overrides. */
export function loadTableColumnWidths<K extends string>(
  storageKey: string, specs: Record<K, TableColumnSpec>,
): Partial<Record<K, number>> {
  const out: Partial<Record<K, number>> = {}
  try {
    const raw = localStorage.getItem(storageKey)
    if (!raw) return out
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return out
    for (const [key, value] of Object.entries(parsed)) {
      if (!Object.prototype.hasOwnProperty.call(specs, key)) continue
      const spec = specs[key as K]
      if (typeof value !== 'number' || !Number.isFinite(value)) continue
      if (value < (spec.min ?? DEFAULT_MIN) || value > (spec.max ?? DEFAULT_MAX)) continue
      if (value !== spec.base) out[key as K] = value
    }
  } catch {
    /* storage unavailable or corrupt — the declared widths are still a usable table */
  }
  return out
}

/**
 * `specs` must be a stable reference (a module-level constant): it is read
 * through the returned callbacks and is a memo dependency.
 */
export function useTableColumnWidths<K extends string>(
  storageKey: string, specs: Record<K, TableColumnSpec>,
): TableColumnWidths<K> {
  const [widths, setWidths] = useState<Partial<Record<K, number>>>(
    () => loadTableColumnWidths(storageKey, specs),
  )
  // Latest-ref so a commit persists the value the same event just produced
  // instead of the one captured by the previous render's closure.
  const widthsRef = useRef(widths)
  widthsRef.current = widths

  const persist = useCallback((next: Partial<Record<K, number>>) => {
    // safeSetItem never throws; a blocked or full store means the layout
    // applies for this session only. An empty object is written as-is rather
    // than removed so the key stays owned by one code path.
    safeSetItem(storageKey, JSON.stringify(next))
  }, [storageKey])

  const update = useCallback((next: Partial<Record<K, number>>, commit: boolean) => {
    widthsRef.current = next
    setWidths(next)
    if (commit) persist(next)
  }, [persist])

  const setWidth = useCallback((key: K, width: number, commit: boolean) => {
    const spec = specs[key]
    const clamped = clampTo(spec, width)
    const next = { ...widthsRef.current }
    // Landing back on the base drops the override, so `customized` and the
    // stored object both describe what actually differs from the defaults.
    if (clamped === spec.base) delete next[key]
    else next[key] = clamped
    update(next, commit)
  }, [specs, update])

  const resetOne = useCallback((key: K) => {
    const next = { ...widthsRef.current }
    delete next[key]
    update(next, true)
  }, [update])

  const reset = useCallback(() => update({}, true), [update])

  const extra = useMemo(() => {
    let sum = 0
    for (const key of Object.keys(widths) as K[]) {
      const w = widths[key]
      if (w !== undefined) sum += w - specs[key].base
    }
    return sum
  }, [widths, specs])

  const style = useCallback((key: K) => {
    const w = widths[key]
    return w === undefined ? undefined : { width: w }
  }, [widths])

  const resizer = useCallback((key: K): ColumnResizerBinding => {
    const spec = specs[key]
    return {
      value: widths[key] ?? spec.base,
      min: spec.min ?? DEFAULT_MIN,
      max: spec.max ?? DEFAULT_MAX,
      onResize: (width, commit) => setWidth(key, width, commit),
      onReset: () => resetOne(key),
    }
  }, [specs, widths, setWidth, resetOne])

  return { style, extra, resizer, customized: Object.keys(widths).length > 0, reset }
}
