/**
 * CategoryRail — left rail of the Discover "All apps" section.
 *
 * Two blocks:
 *  - CATEGORIES: canonical categories with app counts; selecting one filters
 *    the list ("All apps" resets).
 *  - SOURCES: where apps come from (trust provenance) — Built-in plus each
 *    configured external registry with its app count, and an Add-source
 *    action that opens the Sources popover.
 */
import { BadgeCheck, Database, Plus } from 'lucide-react'
import type { Category } from './categories'

import { i18nT } from '../../i18n/t'
export type SourceRow = { name: string; label: string; count: number; builtin: boolean }

export default function CategoryRail({ categories, total, selected, onSelect, sources, onAddSource }: {
  categories: { category: Category; count: number }[]
  total: number
  selected: Category | 'All'
  onSelect: (c: Category | 'All') => void
  sources: SourceRow[]
  onAddSource: () => void
}) {
  const item = (label: string, count: number, key: Category | 'All') => {
    const on = selected === key
    return (
      <button
        key={key}
        type="button"
        aria-pressed={on}
        className={`w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg text-[13px] text-left cursor-pointer border-0 bg-transparent transition-colors ${
          on ? 'bg-[var(--accent-subtle)] text-text-strong font-semibold' : 'text-text hover:bg-bg-hover'
        }`}
        onClick={() => onSelect(key)}
      >
        {label} <span className="text-muted text-[11.5px] font-normal">{count}</span>
      </button>
    )
  }

  return (
    <div className="flex flex-col gap-[18px] w-full">
      <div>
        <div className="text-[11px] font-bold tracking-[.1em] text-muted mb-2">{i18nT('components.appstore.categoryRail.categories')}</div>
        {item(i18nT('components.appstore.categoryRail.all_apps'), total, 'All')}
        {categories.map(({ category, count }) => item(category, count, category))}
      </div>
      <div>
        <div className="text-[11px] font-bold tracking-[.1em] text-muted mb-2">{i18nT('components.appstore.categoryRail.sources')}</div>
        {sources.map(s => (
          <div key={s.name} className="flex items-center gap-2 px-2.5 py-[7px] border border-border rounded-[9px] bg-card text-[12.5px] mb-1.5">
            {s.builtin
              ? <BadgeCheck size={14} className="text-accent shrink-0" aria-label={i18nT('components.appstore.categoryRail.first_party')} />
              : <Database size={14} className="text-muted shrink-0" />}
            <div className="min-w-0">
              <div className="text-text truncate">{s.label}</div>
              <div className="text-muted text-[11px]">{i18nT('components.appstore.categoryRail.app', { count: s.count })}</div>
            </div>
          </div>
        ))}
        <button
          type="button"
          className="flex items-center gap-1.5 text-[12.5px] text-accent cursor-pointer border-0 bg-transparent px-0.5 py-1 hover:underline"
          onClick={onAddSource}
        >
          <Plus size={13} /> {i18nT('components.appstore.categoryRail.add_source')}
        </button>
      </div>
    </div>
  )
}
