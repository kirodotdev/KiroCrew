import type { SortState } from '../hooks/useSortableTable'
import { TableHead } from './ui/table'

/**
 * `break-keep` (`word-break: keep-all`) is load-bearing, not cosmetic.
 *
 * The fixed `w-[80px]` widths callers pass are a hint an auto-layout table is
 * free to shrink below, and a header has no minimum content width of its own once
 * it may break anywhere. CJK breaks between CHARACTERS rather than at spaces, so
 * Korean `마지막 실행` collapsed to `마 지 막 실 행` — one syllable per line, the
 * width of a single glyph.
 *
 * `keep-all` forbids only the intra-word break, which is the defect; a space is
 * still a break opportunity. `whitespace-nowrap` would fix Korean too, but by
 * giving EVERY header a hard minimum — and German's `NÄCHSTE AUSFÜHRUNG`, which
 * legitimately wraps at its space today, then widens the table until the Aktionen
 * column is clipped. One property is right for all twelve locales; the other
 * trades one locale's defect for another's.
 */
const TH_CLS = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium break-keep'

/**
 * Sortable header for a table built out of `ui/table` (shadcn) primitives.
 *
 * Kept SEPARATE from the default export rather than adding a variant flag: the
 * two carry different typography (`TableHead` is 10px/semibold/uppercase-wide,
 * `TH_CLS` is 12px/medium), and the four pages still on a raw `<table>`
 * (HooksPage, McpTab, MemoryTab, papyrus/ProjectList) mix `SortableHeader` with
 * hand-written sibling `<th>`s. Changing the shared component's classes would
 * silently restyle those headers out of alignment with their own siblings.
 *
 * The button restates `TableHead`'s type scale rather than trying to inherit
 * it: Tailwind's preflight resets `font-size` on `button` to `100%` of the
 * BROWSER default, not of the cell, and an arbitrary `[font:inherit]` did not
 * survive the build — both leave a sortable header visibly out of step with its
 * plain-`TableHead` neighbours (observed in the capture).
 * Keep these classes in sync with `TableHead` in `ui/table.tsx`.
 */
export function SortableTableHead({ label, sortKey, sort, onToggle, className = '' }: {
  label: string; sortKey: string; sort: SortState; onToggle: (key: string) => void; className?: string
}) {
  const active = sort.key === sortKey
  return (
    <TableHead
      className={className}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onToggle(sortKey)}
        className="cursor-pointer border-none bg-transparent p-0 text-sm font-medium text-text-strong hover:text-accent"
      >
        {label}
      </button>
    </TableHead>
  )
}

export default function SortableHeader({ label, sortKey, sort, onToggle, className = '' }: {
  label: string; sortKey: string; sort: SortState; onToggle: (key: string) => void; className?: string
}) {
  const active = sort.key === sortKey
  return (
    <th
      className={`${TH_CLS} ${className}`}
      aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
    >
      <button
        type="button"
        onClick={() => onToggle(sortKey)}
        className="cursor-pointer bg-transparent border-none p-0 font-medium text-[12px] uppercase tracking-[.04em] text-muted hover:text-text"
      >
        {label}
      </button>
    </th>
  )
}
