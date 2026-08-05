import type { SortState } from '../hooks/useSortableTable'

const TH_CLS = 'text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium'

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
