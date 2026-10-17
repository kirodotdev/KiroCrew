import { safeSetItem } from '../utils/safeStorage'
import { useState, useMemo } from 'react'

export interface SortState { key: string | null; dir: 'asc' | 'desc' | null }

export type Comparators<T> = Record<string, (a: T, b: T) => number>

/**
 * Apply a SortState to a list using the supplied comparator map. Returns the
 * input array unchanged when there is no active or recognised sort. Exposed
 * (in addition to being used internally by useSortableTable) so a table with
 * more than one row group that shares a single sort model — e.g. a file
 * browser that always keeps directories above files — can sort each group
 * consistently without re-deriving the direction logic.
 */
export function applySort<T>(
  data: T[],
  sort: SortState,
  comparators: Comparators<T>,
): T[] {
  const { key, dir } = sort
  if (!key || !dir || !comparators[key]) return data
  const cmp = comparators[key]
  return [...data].sort((a, b) => dir === 'asc' ? cmp(a, b) : cmp(b, a))
}

export function useSortableTable<T>(
  data: T[],
  tableId: string,
  comparators: Comparators<T>,
  defaultSort?: SortState,
  options?: {
    // Per-column direction to use the first time a column is selected.
    // Columns omitted here default to 'asc'. Lets a date column open
    // newest-first, for example, while name columns open A→Z.
    initialDirs?: Record<string, 'asc' | 'desc'>
    // When true, clicking a column header toggles only between asc and desc
    // and never deselects — every column is fully bidirectional. When false
    // (the default) the toggle is tri-state: a third click on a column
    // returns to defaultSort (or unsorted), and only the default column
    // flips both ways.
    bidirectional?: boolean
  },
) {
  const { initialDirs, bidirectional } = options ?? {}
  const [sort, setSort] = useState<SortState>(() => {
    try {
      const raw = localStorage.getItem(`sort:${tableId}`)
      if (raw) return JSON.parse(raw)
    } catch { /* ignore */ }
    return defaultSort ?? { key: null, dir: null }
  })

  const toggle = (key: string) => {
    setSort(prev => {
      const next: SortState =
        prev.key !== key ? { key, dir: initialDirs?.[key] ?? 'asc' }
        : bidirectional ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : prev.dir === 'asc' ? { key, dir: 'desc' }
        : prev.dir === 'desc' ? (defaultSort?.key === key && defaultSort?.dir === 'desc'
                                  ? { key, dir: 'asc' }
                                  : defaultSort ?? { key: null, dir: null })
        : { key, dir: 'asc' }
      try { safeSetItem(`sort:${tableId}`, JSON.stringify(next)) } catch { /* ignore */ }
      return next
    })
  }

  const sorted = useMemo(() => applySort(data, sort, comparators), [data, sort, comparators])

  return { sorted, sort, toggle }
}
