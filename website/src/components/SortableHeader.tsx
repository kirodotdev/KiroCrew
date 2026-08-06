import type { SortState } from '../hooks/useSortableTable'

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
