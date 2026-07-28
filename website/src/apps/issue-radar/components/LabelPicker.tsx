import { useMemo, useState } from 'react'
import { Search, Check, Sparkles, X } from 'lucide-react'
import { readableText, hexToRgba } from '../lib/format'
import type { RepoLabel } from '../api'

import { i18nT } from '../../../i18n/t'
/** A wrap-grid label multi-select: tap a label chip to add/remove it from a
 * role set (triage, good-first-issue, …). Selected chips fill to the label's
 * real GitHub colour; unselected show a light tint. When a `suggestPattern` is
 * given, likely-matching labels are ringed + get a one-click "Add N suggested"
 * button. Labels saved in the set but no longer present on the repo (renamed /
 * deleted upstream) surface separately so they can be cleared. */
export default function LabelPicker({
  labels, selected, onToggle, onAddMany, countByLabel, suggestPattern, loading, error, emptyText,
}: {
  labels: RepoLabel[]
  selected: string[]
  onToggle: (name: string) => void
  onAddMany?: (names: string[]) => void
  countByLabel?: Map<string, number>
  suggestPattern?: RegExp
  loading?: boolean
  error?: Error | null
  emptyText?: string
}) {
  const [q, setQ] = useState('')
  const sel = useMemo(() => new Set(selected), [selected])

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    const arr = needle ? labels.filter((l) => l.name.toLowerCase().includes(needle)) : labels
    // Selected first, then by name — keeps the current set together at the top.
    return [...arr].sort((a, b) => {
      const sa = sel.has(a.name) ? 0 : 1
      const sb = sel.has(b.name) ? 0 : 1
      if (sa !== sb) return sa - sb
      return a.name.localeCompare(b.name)
    })
  }, [labels, q, sel])

  const suggestions = useMemo(
    () => (suggestPattern ? labels.filter((l) => suggestPattern.test(l.name) && !sel.has(l.name)) : []),
    [labels, suggestPattern, sel],
  )

  const orphans = useMemo(
    () => selected.filter((n) => !labels.some((l) => l.name === n)),
    [selected, labels],
  )

  if (loading) return <div className="text-[12px] text-muted py-2">{i18nT('apps.issueRadar.components.labelPicker.loading_labels')}</div>
  if (error) return <div className="text-[12px] text-danger py-2">{error.message}</div>
  if (labels.length === 0) return <div className="text-[12px] text-muted py-2">{emptyText ?? 'This repo has no labels.'}</div>

  return (
    <div>
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <div className="relative flex-1 min-w-[180px]">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted pointer-events-none" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={i18nT('apps.issueRadar.components.labelPicker.filter_labels')}
            aria-label={i18nT('apps.issueRadar.components.labelPicker.filter_labels_2')}
            className="w-full pl-8 pr-2 py-1.5 text-[13px] rounded-md border border-border bg-bg text-text placeholder:text-muted outline-none focus:border-accent"
          />
        </div>
        {onAddMany && suggestions.length > 0 && (
          <button
            onClick={() => onAddMany(suggestions.map((s) => s.name))}
            className="inline-flex items-center gap-1 text-[12px] px-2.5 py-1.5 rounded-md border border-accent/40 text-accent hover:bg-accent-subtle cursor-pointer bg-transparent whitespace-nowrap"
          >
            <Sparkles size={12} /> {i18nT('apps.issueRadar.components.labelPicker.add')} {suggestions.length} {i18nT('apps.issueRadar.components.labelPicker.suggested')}
          </button>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        {filtered.map((l) => {
          const isSel = sel.has(l.name)
          const isSuggested = !isSel && !!suggestPattern?.test(l.name)
          const count = countByLabel?.get(l.name)
          const style: React.CSSProperties = isSel
            ? { backgroundColor: `#${l.color}`, color: readableText(l.color) }
            : { backgroundColor: hexToRgba(l.color, 0.16), color: 'var(--text)' }
          return (
            <button
              key={l.name}
              onClick={() => onToggle(l.name)}
              title={l.description || l.name}
              style={style}
              className={`inline-flex items-center gap-1.5 max-w-full rounded-full px-3 py-1 text-[13px] cursor-pointer transition-all ${isSel ? 'font-bold' : 'font-medium'} ${isSuggested ? 'ring-1 ring-accent/50' : ''}`}
            >
              {isSel && <Check size={13} className="flex-shrink-0" />}
              {isSuggested && <Sparkles size={12} className="flex-shrink-0 text-accent" />}
              <span className="truncate">{l.name}</span>
              {typeof count === 'number' && count > 0 && (
                <span className={`tabular-nums text-[11px] flex-shrink-0 ${isSel ? 'opacity-90' : 'opacity-60'}`}>{count}</span>
              )}
            </button>
          )
        })}
      </div>

      {orphans.length > 0 && (
        <div className="mt-3">
          <div className="text-[11px] text-muted mb-1.5">{i18nT('apps.issueRadar.components.labelPicker.saved_but_no_longer_on_this_repo')}</div>
          <div className="flex flex-wrap gap-2">
            {orphans.map((n) => (
              <button
                key={n}
                onClick={() => onToggle(n)}
                title={i18nT('apps.issueRadar.components.labelPicker.remove_this_label_no_longer_exists_on_the_repo')}
                className="inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[13px] border border-dashed border-border text-muted hover:text-text cursor-pointer bg-transparent"
              >
                <X size={12} /> <span className="truncate">{n}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
