/** The memory kind pills, shared by the Memories list and the Graph tab. */
import { useEffect, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import InfoTip from '../../../components/InfoTip'
import { fmtNumber } from '../../../i18n/format'
import { useScrollEdges } from '../../../hooks/useScrollEdges'
import { kindTip } from './memoryTypeTips'
import type { KindTab } from './memoryListView'

const KINDS: [KindTab, string][] = [['all', 'memoryV2.filter_all'], ['fact', 'memoryV2.facts_label'], ['directive', 'memoryV2.rules_label'], ['episode', 'memoryV2.experiences_label']]
// The picked kind pill: filled with the text colour on both themes (mock S2).
export const PILL_ON = 'border-text bg-text font-medium text-bg'

/** The kind pills, one row with live counts; a phone scrolls the row sideways, its clipped edges faded. */
export function KindPills({ kind, counts, onChange, disabled, wrap = false, bot, marks }: { kind: KindTab; counts?: Record<string, number>; onChange: (kind: KindTab) => void; disabled?: boolean; wrap?: boolean; bot?: string
  /** A small mark per kind, the key to a view that draws kinds apart (the graph's shapes). */
  marks?: Partial<Record<KindTab, ReactNode>> }) {
  const { t } = useTranslation()
  const total = counts ? (counts.fact || 0) + (counts.directive || 0) + (counts.episode || 0) : undefined
  const [attachEdges, edges, remeasure] = useScrollEdges<HTMLDivElement>()
  // A count arriving widens the row without resizing the strip itself.
  useEffect(remeasure, [counts, remeasure])
  return <div ref={wrap ? undefined : attachEdges} data-fade-left={!wrap && edges.left ? '' : undefined} data-fade-right={!wrap && edges.right ? '' : undefined}
    role="radiogroup" aria-label={t('memoryV2.group_kind')} title={t('memoryV2.memory_kinds_explanation')} className={`-mx-1 flex min-w-0 gap-2 px-1 ${wrap ? 'flex-wrap' : 'edge-fade-x overflow-x-auto scrollbar-none'}`}>
    {KINDS.map(([key, label]) => {
      const on = key === kind
      const count = key === 'all' ? total : counts ? counts[key] || 0 : undefined
      const hint = key === 'all' ? undefined : kindTip(key, bot)
      // The (?) sits beside the pill, not in it, so opening the tip never picks the kind.
      return <span key={key} className="flex shrink-0 items-center gap-0.5">
        <button type="button" role="radio" aria-checked={on} disabled={disabled} onClick={() => onChange(key)} className="flex min-h-11 shrink-0 items-center disabled:opacity-40 sm:min-h-8">
          <span className={`flex h-8 items-center gap-1.5 rounded-full border px-3 text-[13px] ${on ? PILL_ON : 'border-border text-text'}`}>{marks?.[key] && <span aria-hidden="true" data-testid={`kind-mark-${key}`} className="flex shrink-0">{marks[key]}</span>}{t(label)}{count !== undefined && <span className={`tabular-nums ${on ? 'opacity-70' : 'text-muted'}`}>{fmtNumber(count)}</span>}</span>
        </button>
        {hint && <InfoTip text={hint} />}
      </span>
    })}
  </div>
}
