import { useState } from 'react'
import { Trans } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Gauge, Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import { Card, Btn, Toggle, EmptyState } from '../../components/ui'
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '../../components/ui/select'
import type { SkillBudgetResponse, SkillBudgetRow } from '../../types'
import { fmtBytes, fmtCompact, fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

type SortKey = 'cost' | 'size' | 'recent'

/** Humanize a skill name for display.
 *
 *  Deliberately identical to `SkillsTab`'s helper, applied to the same field:
 *  deriving this from `row.key` instead dropped the category segment, so one
 *  skill read differently in the browse list and here. */
const displayName = (name: string) =>
  name.replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase())

/** Measured 30-day cost. null (an `always: true` skill, injected every turn but
 *  never recorded in the ledger) contributes nothing to sums or ordering. */
const spend = (r: SkillBudgetRow) => r.chars ?? 0

const comparators: Record<SortKey, (a: SkillBudgetRow, b: SkillBudgetRow) => number> = {
  cost: (a, b) => spend(b) - spend(a),
  size: (a, b) => b.size_bytes - a.size_bytes,
  recent: (a, b) => {
    // null idle_days = never fired → last
    if (a.idle_days === null && b.idle_days === null) return 0
    if (a.idle_days === null) return 1
    if (b.idle_days === null) return -1
    return a.idle_days - b.idle_days
  },
}

export default function SkillContextBudget({ onBack }: { onBack: () => void }) {
  const queryClient = useQueryClient()
  const [sortBy, setSortBy] = useState<SortKey>('cost')
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(new Set())
  const [failedKey, setFailedKey] = useState<string | null>(null)

  const { data, isLoading } = useQuery<SkillBudgetResponse>({
    queryKey: ['skills-budget'],
    queryFn: () => api.skillsBudget(),
    staleTime: 0,
    refetchOnMount: 'always',
  })

  const rows = data?.rows ?? []
  const totalChars = data?.total_chars ?? 0

  // An `always: true` skill reports chars === null: it is injected every turn,
  // but that injection is never recorded in the ledger, so its magnitude is
  // unknown. It still counts toward context, so it heads the counting group
  // rather than falling through every filter and vanishing from the table.
  const unmeasured = rows.filter(r => r.inject_on_trigger && r.chars === null)
  const hotRows = rows.filter(r => r.inject_on_trigger && r.chars !== null && r.chars > 0).sort(comparators[sortBy])
  const coldRows = rows.filter(r => r.inject_on_trigger && r.chars === 0).sort((a, b) => b.size_bytes - a.size_bytes)
  const frozenRows = rows.filter(r => !r.inject_on_trigger)
  const countingRows = [...unmeasured, ...hotRows]

  const maxChars = rows.length > 0 ? Math.max(...rows.map(spend)) : 1
  const hotSpend = hotRows.reduce((s, r) => s + spend(r), 0)
  const hotPct = totalChars > 0 ? Math.round(hotSpend / totalChars * 100) : 0

  // Top 3 percentage (for danger color)
  const top3Sorted = [...rows].sort((a, b) => spend(b) - spend(a)).slice(0, 3)
  const top3Chars = top3Sorted.reduce((s, r) => s + spend(r), 0)
  const top3Ratio = totalChars > 0 ? top3Chars / totalChars : 0

  const coldKB = fmtBytes(coldRows.reduce((s, r) => s + r.size_bytes, 0))
  const frozenChars = fmtCompact(frozenRows.reduce((s, r) => s + spend(r), 0))

  const flip = async (row: SkillBudgetRow, next: boolean) => {
    setPendingKeys(prev => new Set(prev).add(row.key))
    setFailedKey(null)
    try {
      await api.setSkillInjectOnTrigger(row.key, next)
    } catch {
      // Surface it: a toggle that silently does nothing reads as a broken
      // control, and the refetch below would put the row back where it was
      // with no explanation for why.
      setFailedKey(row.key)
      setPendingKeys(prev => {
        const undo = new Set(prev)
        undo.delete(row.key)
        return undo
      })
      return
    }
    await queryClient.invalidateQueries({ queryKey: ['skills'] })
    await queryClient.invalidateQueries({ queryKey: ['skills-budget'] })
    setPendingKeys(prev => {
      const next2 = new Set(prev)
      next2.delete(row.key)
      return next2
    })
  }

  const canToggle = (r: SkillBudgetRow) => r.owned && !r.always

  if (isLoading) {
    return (
      <div className="mt-4">
        <Btn onClick={onBack} className="mb-3 text-muted">
          <ArrowLeft size={14} /> {i18nT('pages.overview.skillsTab.budget_back')}
        </Btn>
        <Card>
          <div className="flex items-center justify-center py-16 text-muted">
            <Loader2 size={20} className="animate-spin" />
          </div>
        </Card>
      </div>
    )
  }

  if (rows.length === 0) {
    return (
      <div className="mt-4">
        <Btn onClick={onBack} className="mb-3 text-muted">
          <ArrowLeft size={14} /> {i18nT('pages.overview.skillsTab.budget_back')}
        </Btn>
        <h4 className="text-lg font-semibold text-text-strong mb-1">{i18nT('pages.overview.skillsTab.budget_title')}</h4>
        <p className="text-[12.5px] text-muted mb-3">{i18nT('pages.overview.skillsTab.budget_subtitle')}</p>
        <Card>
          <EmptyState
            icon={<Gauge className="lucide-inline" />}
            title={i18nT('pages.overview.skillsTab.budget_empty_title')}
            subtitle={i18nT('pages.overview.skillsTab.budget_empty_subtitle')}
          />
        </Card>
      </div>
    )
  }

  return (
    <div className="mt-4">
      <Btn onClick={onBack} className="mb-3 text-muted">
        <ArrowLeft size={14} /> {i18nT('pages.overview.skillsTab.budget_back')}
      </Btn>
      <h4 className="text-lg font-semibold text-text-strong mb-1">{i18nT('pages.overview.skillsTab.budget_title')}</h4>
      <p className="text-[12.5px] text-muted mb-3">{i18nT('pages.overview.skillsTab.budget_subtitle')}</p>
      <Card>
        {/* Header row */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          {/* One catalog key, not a shard: the total and the top-3 share are
           *  styled differently but the sentence around them must stay whole,
           *  or the 10 non-English locales cannot reorder it. */}
          <div className="text-[12.5px] text-muted">
            <Trans
              i18nKey="pages.overview.skillsTab.budget_header"
              values={{ total: fmtCompact(totalChars), pct: fmtPercent(top3Ratio) }}
              components={{
                total: <span className="text-accent font-bold" />,
                pct: <span className="text-danger font-semibold" />,
              }}
            />
          </div>
          <div className="flex items-center gap-2 text-[11.5px] text-muted">
            {i18nT('pages.overview.skillsTab.budget_sort')}
            <Select value={sortBy} onValueChange={v => setSortBy(v as SortKey)}>
              <SelectTrigger className="w-[140px] h-7 text-[11.5px] px-2 py-1">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="cost">{i18nT('pages.overview.skillsTab.budget_sort_cost')}</SelectItem>
                <SelectItem value="size">{i18nT('pages.overview.skillsTab.budget_sort_size')}</SelectItem>
                <SelectItem value="recent">{i18nT('pages.overview.skillsTab.budget_sort_recent')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* Group 1: Counting toward context */}
        <GroupHeader
          label={i18nT('pages.overview.skillsTab.budget_group_counting')}
          right={i18nT('pages.overview.skillsTab.budget_group_counting_detail', { count: String(countingRows.length), pct: String(hotPct) })}
        />
        {countingRows.map(row => (
          <BudgetRow
            key={row.key}
            row={row}
            maxChars={maxChars}
            pending={pendingKeys.has(row.key)}
            failed={failedKey === row.key}
            canToggle={canToggle(row)}
            onFlip={flip}
          />
        ))}

        {/* Group 2: Never fired in 30 days */}
        <GroupHeader
          label={i18nT('pages.overview.skillsTab.budget_group_cold')}
          right={i18nT('pages.overview.skillsTab.budget_group_cold_detail', { count: String(coldRows.length), size: coldKB })}
        />
        {coldRows.map(row => (
          <BudgetRow
            key={row.key}
            row={row}
            maxChars={maxChars}
            pending={pendingKeys.has(row.key)}
            failed={failedKey === row.key}
            canToggle={canToggle(row)}
            onFlip={flip}
            cold
          />
        ))}

        {/* Group 3: Already pointer-only */}
        <GroupHeader
          label={i18nT('pages.overview.skillsTab.budget_group_frozen')}
          right={i18nT('pages.overview.skillsTab.budget_group_frozen_detail', { count: String(frozenRows.length), chars: frozenChars })}
          accent
        />
        {frozenRows.map(row => (
          <BudgetRow
            key={row.key}
            row={row}
            maxChars={maxChars}
            pending={pendingKeys.has(row.key)}
            failed={failedKey === row.key}
            canToggle={canToggle(row)}
            onFlip={flip}
            frozen
          />
        ))}

        {/* Footnote */}
        <div className="px-4 py-2.5 text-[11px] text-muted border-t border-border" style={{ background: 'var(--bg-elevated, var(--bg))' }}>
          {i18nT('pages.overview.skillsTab.budget_footnote')}
        </div>
      </Card>
    </div>
  )
}

function GroupHeader({ label, right, accent }: { label: string; right: string; accent?: boolean }) {
  return (
    <div className="flex items-center justify-between px-4 py-2 border-t border-b border-border text-[10.5px] tracking-wider uppercase"
      style={{ background: 'var(--bg-elevated, var(--bg))' }}>
      <span className="text-muted">{label}</span>
      <span className={accent ? 'text-accent' : 'text-muted'}>{right}</span>
    </div>
  )
}

function BudgetRow({
  row,
  maxChars,
  pending,
  canToggle: toggleable,
  onFlip,
  cold,
  frozen,
  failed,
}: {
  row: SkillBudgetRow
  maxChars: number
  pending: boolean
  canToggle: boolean
  onFlip: (row: SkillBudgetRow, next: boolean) => void
  cold?: boolean
  frozen?: boolean
  failed?: boolean
}) {
  const barPct = maxChars > 0 ? Math.max(0.5, ((row.chars ?? 0) / maxChars) * 100) : 0
  const inject = row.inject_on_trigger

  return (
    <div
      className={`flex items-center gap-3.5 px-4 py-2 border-b border-border/50 hover:bg-bg-hover transition-colors ${frozen ? 'opacity-80' : ''}`}
    >
      {/* Column 1: name + key */}
      <div className="flex-1 min-w-0">
        <div className={`text-[12.5px] truncate ${frozen ? 'text-muted' : 'text-text'}`}>
          {displayName(row.name)}
        </div>
        <div className="text-[10.5px] text-muted font-mono truncate">
          {row.key}
          {row.folded_from && row.folded_from.length > 0 && (
            <span className="text-muted"> + {row.folded_from.join(', ')} {i18nT('pages.overview.skillsTab.budget_renamed')}</span>
          )}
        </div>
      </div>

      {/* Column 2: bar + chars. A row whose cost is unmeasurable (always: true)
       *  gets neither -- a bar length would imply a magnitude we do not have. */}
      <div className="w-[280px] shrink-0">
        {cold ? (
          <div className="text-[10.5px] text-muted font-mono">
            {i18nT('pages.overview.skillsTab.budget_if_fires', { size: fmtBytes(row.size_bytes) })}
          </div>
        ) : row.chars === null ? (
          <div className="text-[10.5px] text-muted font-mono">
            {i18nT('pages.overview.skillsTab.budget_every_turn', { size: fmtBytes(row.size_bytes) })}
          </div>
        ) : (
          <>
            <div
              className="h-[5px] rounded-full"
              style={{ width: `${barPct}%`, background: 'var(--border-strong)', minWidth: '2px' }}
            />
            <div className="text-[10.5px] font-mono mt-1 text-muted">
              {fmtCompact(row.chars)} {i18nT('pages.overview.skillsTab.budget_chars')}
            </div>
          </>
        )}
      </div>

      {/* Column 3: deliveries, or why the last flip did not land. A cold row's
       *  size is already stated in column 2, so repeating it here says the same
       *  number twice on one line. */}
      <div className="w-[140px] shrink-0 text-[10.5px] font-mono">
        {failed
          ? <span className="text-danger font-sans">{i18nT('pages.overview.skillsTab.injection_update_failed')}</span>
          : cold
            ? null
            : <span className="text-muted">{fmtBytes(row.size_bytes)}{row.deliveries != null ? ` × ${row.deliveries}` : ''}</span>}
      </div>

      {/* Column 4: toggle */}
      <div className="w-[42px] shrink-0 flex items-center justify-end">
        {pending ? (
          <Loader2 size={14} className="animate-spin text-muted" />
        ) : toggleable ? (
          <Toggle
            checked={inject}
            onChange={next => onFlip(row, next)}
            disabled={false}
            tone="muted"
            label={i18nT('pages.overview.skillsTab.inject_full_content_on_match')}
          />
        ) : null}
      </div>
    </div>
  )
}
