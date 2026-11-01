/** Settings > People > Advanced: the owner's Shared Crew limits.
 *
 *  A collapsed section at the bottom of People. Each control writes one
 *  `dashboard.shared_crew.<field>` config path through the same config PATCH
 *  the other settings rows use. Only the owner sees it; the PATCH route is
 *  owner-only on the server too. A number is written when its field loses focus
 *  or on Enter, and only while it is inside the bounds the gateway enforces. */
import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { Input, Toggle } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import { i18nT } from '../../i18n/t'
import { setConfigPathValue, useOptimisticConfigPaths } from './useOptimisticConfigPaths'

const PATH = {
  admission_enabled: 'dashboard.shared_crew.admission_enabled',
  principal_concurrency: 'dashboard.shared_crew.principal_concurrency',
  crew_concurrency: 'dashboard.shared_crew.crew_concurrency',
  typing_idle_seconds: 'dashboard.shared_crew.typing_idle_seconds',
  tailnet_direct: 'dashboard.shared_crew.tailnet_direct',
} as const

type Field = keyof typeof PATH
type NumberField = 'principal_concurrency' | 'crew_concurrency' | 'typing_idle_seconds'

/** The gateway's bounds (`SHARED_CREW_BOUNDS`) and defaults. */
const NUMBERS: Record<NumberField, { min: number; max: number; fallback: number }> = {
  principal_concurrency: { min: 1, max: 256, fallback: 4 },
  crew_concurrency: { min: 0, max: 1024, fallback: 0 },
  typing_idle_seconds: { min: 3, max: 60, fallback: 10 },
}

const CONFIG_KEY = ['kirocrewConfig'] as const

type SharedCrewSection = Partial<Record<Field, unknown>>

function sectionOf(config: unknown): SharedCrewSection {
  const dashboard = (config as { dashboard?: { shared_crew?: unknown } } | null | undefined)?.dashboard
  const section = dashboard?.shared_crew
  return section && typeof section === 'object' ? section as SharedCrewSection : {}
}

function parseWhole(raw: string): number | null {
  return /^\d+$/.test(raw.trim()) ? Number(raw.trim()) : null
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 sm:gap-4 py-1.5 min-w-0">
      <span className="text-[13px] text-text min-w-0">{label}</span>
      <div className="flex flex-wrap items-center gap-2 sm:justify-end">
        {children}
      </div>
    </div>
  )
}

export function PeopleAdvanced() {
  const qc = useQueryClient()
  const config = useQuery({ queryKey: CONFIG_KEY, queryFn: () => api.kirocrewConfig(), retry: false })
  const [drafts, setDrafts] = useState<Partial<Record<NumberField, string>>>({})
  const overlay = useOptimisticConfigPaths(qc)
  const save = useMutation(overlay.mutationOpts<{ field: Field; value: boolean | number }>({
    queryKey: CONFIG_KEY,
    mutationFn: ({ field, value }) => api.patchConfig(PATH[field], value),
    path: ({ field }) => PATH[field],
    displayValue: ({ value }) => value,
    applyToCache: (cached, { field, value }) => setConfigPathValue(cached, PATH[field], value),
  }))
  const section = sectionOf(config.data)
  // Controls hold while a save is in flight, so its failure stays the one shown.
  const locked = config.isPending || config.isError || save.isPending

  const commit = (field: NumberField, current: number) => {
    const raw = drafts[field]
    if (raw === undefined) return
    const value = parseWhole(raw)
    const { min, max } = NUMBERS[field]
    setDrafts(d => { const next = { ...d }; delete next[field]; return next })
    if (value === null || value < min || value > max || value === current) return
    save.mutate({ field, value })
  }
  const numberRow = (field: NumberField, label: string) => {
    const { min, max, fallback } = NUMBERS[field]
    const stored = section[field]
    const shown = overlay.shown(PATH[field], typeof stored === 'number' ? stored : fallback)
    const draft = drafts[field]
    const parsed = draft === undefined ? shown : parseWhole(draft)
    const invalid = parsed === null || parsed < min || parsed > max
    return (
      <Row label={label}>
        <SchemaRestartBadge configKey={PATH[field]} />
        <Input
          type="number"
          inputMode="numeric"
          min={min}
          max={max}
          step={1}
          className="w-24 flex-none text-right"
          aria-label={label}
          aria-invalid={invalid || undefined}
          value={draft ?? String(shown)}
          disabled={locked}
          onChange={e => setDrafts(d => ({ ...d, [field]: e.target.value }))}
          onBlur={() => commit(field, shown)}
          onKeyDown={e => { if (e.key === 'Enter') commit(field, shown) }}
          data-testid={`people-advanced-${field}`}
        />
      </Row>
    )
  }
  return (
    <details className="rounded-lg border border-border px-4 py-2" data-testid="people-advanced">
      <summary className="cursor-pointer text-sm font-medium text-text-strong py-1">{i18nT('sharedCrew.adv_title')}</summary>
      <div className="flex flex-col divide-y divide-border pt-1">
        {config.isError && (
          <div className="py-2">
            {/* No hand-off: People holds unsaved invite emails, name, picture and API key. */}
            <ErrorNotice variant="inline" message={i18nT('sharedCrew.adv_load_failed')} testId="people-advanced-load-error" />
          </div>
        )}
        <Row label={i18nT('sharedCrew.adv_admission')}>
          <Toggle
            checked={overlay.shown(PATH.admission_enabled, section.admission_enabled === true)}
            onChange={v => save.mutate({ field: 'admission_enabled', value: v })}
            disabled={locked}
            label={i18nT('sharedCrew.adv_admission')}
          />
        </Row>
        {numberRow('principal_concurrency', i18nT('sharedCrew.adv_principal'))}
        {numberRow('crew_concurrency', i18nT('sharedCrew.adv_crew'))}
        {/* The lease registry reads the window once, on first use. */}
        {numberRow('typing_idle_seconds', i18nT('sharedCrew.adv_typing'))}
        <Row label={i18nT('sharedCrew.adv_direct')}>
          <SchemaRestartBadge configKey={PATH.tailnet_direct} />
          <Toggle
            checked={overlay.shown(PATH.tailnet_direct, section.tailnet_direct === true)}
            onChange={v => save.mutate({ field: 'tailnet_direct', value: v })}
            disabled={locked}
            label={i18nT('sharedCrew.adv_direct')}
          />
        </Row>
        {save.isError && (
          <div className="py-2">
            {/* No hand-off: People holds unsaved invite emails, name, picture and API key. */}
            <ErrorNotice variant="inline" message={i18nT('sharedCrew.adv_save_failed')} testId="people-advanced-save-error" />
          </div>
        )}
      </div>
    </details>
  )
}
