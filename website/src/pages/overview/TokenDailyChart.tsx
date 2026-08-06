import { useEffect, useMemo, useState } from 'react'
import { formatCost } from '../../utils/formatCost'
import SimpleSelect from '../../components/SimpleSelect'

import { i18nT } from '../../i18n/t'
export type TokenBucket = {
  input: number
  output: number
  cacheCreate: number
  cacheRead: number
  costUsd: number
}

export type TokenDay = {
  date: string
  input: number
  output: number
  cacheCreate: number
  cacheRead: number
  costUsd: number
  models?: Record<string, TokenBucket>
  providers?: Record<string, TokenBucket>
  /**
   * Per-day provider × model cross-tab. Used by filterDay when both
   * filters are set so we can return the true intersection bucket
   * instead of over-counting from independent provider/model totals.
   */
  providerModels?: Record<string, Record<string, TokenBucket>>
}

const ALL = '__all__'

function emptyBucket(): TokenBucket {
  return { input: 0, output: 0, cacheCreate: 0, cacheRead: 0, costUsd: 0 }
}

function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

/**
 * Apply provider/model filters to a daily entry.
 *
 * - Both = ALL: use the day's pre-aggregated totals.
 * - Only provider: read d.providers[provider].
 * - Only model:    read d.models[model].
 * - Both set:      read d.providerModels[provider][model] — the true
 *   intersection bucket. If the pair has no record on that day, return an
 *   empty bucket (this is what makes invalid pairings render as a flat
 *   chart instead of an inflated bar from over-counting).
 */
export function filterDay(
  d: TokenDay,
  providerSel: string,
  modelSel: string,
): TokenBucket {
  const noProvider = providerSel === ALL
  const noModel = modelSel === ALL
  if (noProvider && noModel) {
    return {
      input: d.input,
      output: d.output,
      cacheCreate: d.cacheCreate,
      cacheRead: d.cacheRead,
      costUsd: d.costUsd,
    }
  }
  if (noProvider) return d.models?.[modelSel] ?? emptyBucket()
  if (noModel) return d.providers?.[providerSel] ?? emptyBucket()
  return d.providerModels?.[providerSel]?.[modelSel] ?? emptyBucket()
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: string[]
}) {
  // The trigger is a <button>, not a <select>, so an external <label htmlFor>
  // no longer associates — the visible label text becomes the aria-label.
  return (
    <div className="flex items-center gap-2 text-[12px] text-muted">
      <span>{label}</span>
      <SimpleSelect
        aria-label={label}
        options={[ALL, ...options]}
        optionLabels={[i18nT('pages.overview.tokenDailyChart.all'), ...options]}
        value={value}
        onChange={onChange}
      />
    </div>
  )
}

/**
 * Stacked bar chart of daily token usage with cascading provider/model
 * filters. The model dropdown narrows to only models that have actually
 * appeared paired with the selected provider, preventing invalid
 * combinations like opencode + opus from being selectable.
 */
export function TokenDailyChart({
  history,
  providers,
  models,
  providerModels,
}: {
  history: TokenDay[]
  providers?: string[]
  models?: string[]
  providerModels?: Record<string, string[]>
}) {
  const [providerSel, setProviderSel] = useState<string>(ALL)
  const [modelSel, setModelSel] = useState<string>(ALL)

  // Provider option list: prefer the explicit API list, else derive from
  // whatever shows up in the daily entries (back-compat with old payloads).
  const providerOpts = useMemo(() => {
    if (providers && providers.length > 0) return providers
    const set = new Set<string>()
    for (const d of history) if (d.providers) for (const k of Object.keys(d.providers)) set.add(k)
    return Array.from(set).sort()
  }, [providers, history])

  // Model option list. When a provider is selected, scope the dropdown to
  // models that have actually appeared paired with that provider so users
  // can't pick invalid combinations (e.g. opencode + opus). Falls back to
  // the global model list when no provider is selected, or when the
  // backend hasn't shipped providerModels yet.
  const modelOpts = useMemo(() => {
    if (providerSel !== ALL && providerModels && providerModels[providerSel]) {
      return [...providerModels[providerSel]].sort()
    }
    if (models && models.length > 0) return models
    const set = new Set<string>()
    for (const d of history) if (d.models) for (const k of Object.keys(d.models)) set.add(k)
    return Array.from(set).sort()
  }, [providerSel, providerModels, models, history])

  // If the active model is no longer valid under the selected provider,
  // reset it to ALL rather than silently displaying misleading data.
  useEffect(() => {
    if (modelSel !== ALL && !modelOpts.includes(modelSel)) {
      setModelSel(ALL)
    }
  }, [modelOpts, modelSel])

  // Derive the effective model synchronously during render so the chart never
  // shows a one-frame flash of empty data when the user switches provider
  // while a now-invalid model is still selected. The useEffect above still
  // runs to reset the dropdown's visible state on the next tick.
  const effectiveModel =
    modelSel !== ALL && !modelOpts.includes(modelSel) ? ALL : modelSel

  const recent = useMemo(
    () => history.slice(-14).map(d => ({ date: d.date, ...filterDay(d, providerSel, effectiveModel) })),
    [history, providerSel, effectiveModel],
  )
  const maxTokens = Math.max(
    ...recent.map(d => d.input + d.output + d.cacheCreate + d.cacheRead),
    1,
  )

  return (
    <div>
      {/* Filters */}
      {(providerOpts.length > 0 || modelOpts.length > 0) && (
        <div className="flex flex-wrap gap-3 mb-3">
          {providerOpts.length > 0 && (
            <FilterSelect label={i18nT('pages.overview.tokenDailyChart.provider')} value={providerSel} onChange={setProviderSel} options={providerOpts} />
          )}
          {modelOpts.length > 0 && (
            <FilterSelect label={i18nT('pages.overview.tokenDailyChart.model')} value={modelSel} onChange={setModelSel} options={modelOpts} />
          )}
        </div>
      )}
      {/* Bar chart */}
      <div className="flex items-stretch gap-1 h-32 mb-3">
        {recent.map(d => {
          const total = d.input + d.output + d.cacheCreate + d.cacheRead
          const pct = (total / maxTokens) * 100
          const inputPct = total > 0 ? (d.input / total) * pct : 0
          const outputPct = total > 0 ? (d.output / total) * pct : 0
          const cacheReadPct = total > 0 ? (d.cacheRead / total) * pct : 0
          const cacheCreatePct = total > 0 ? (d.cacheCreate / total) * pct : 0
          return (
            <div key={d.date} className="flex-1 h-full flex flex-col items-center gap-0.5 group relative">
              <div className="w-full flex flex-col justify-end" style={{ height: '100%' }}>
                <div className="w-full rounded-t-sm" style={{ height: `${cacheReadPct}%`, background: 'var(--muted)' }} />
                <div className="w-full" style={{ height: `${cacheCreatePct}%`, background: 'var(--danger)' }} />
                <div className="w-full" style={{ height: `${outputPct}%`, background: 'var(--warn)' }} />
                <div className="w-full rounded-b-sm bg-accent" style={{ height: `${inputPct}%` }} />
              </div>
              {/* Tooltip -- positioned below top to avoid overflow clipping */}
              <div className="absolute top-0 left-1/2 -translate-x-1/2 mt-1 hidden group-hover:block bg-bg-elevated border border-border rounded px-2 py-1 text-[11px] whitespace-nowrap z-50 shadow-lg pointer-events-none">
                <div className="font-medium">{d.date}</div>
                <div>{i18nT('pages.overview.tokenDailyChart.in')} {fmtNum(d.input)} {i18nT('pages.overview.tokenDailyChart.out')} {fmtNum(d.output)}</div>
                {d.cacheRead > 0 && <div>{i18nT('pages.overview.tokenDailyChart.cache_read')} {fmtNum(d.cacheRead)}</div>}
                {d.cacheCreate > 0 && <div>{i18nT('pages.overview.tokenDailyChart.cache_create')} {fmtNum(d.cacheCreate)}</div>}
                {d.costUsd > 0 && <div>{i18nT('pages.overview.tokenDailyChart.cost')} {formatCost(d.costUsd)}</div>}
                {(providerSel !== ALL || effectiveModel !== ALL) && (
                  <div className="mt-1 pt-1 border-t border-border text-muted">
                    {providerSel !== ALL && <div>{i18nT('pages.overview.tokenDailyChart.provider_2')} {providerSel}</div>}
                    {effectiveModel !== ALL && <div>{i18nT('pages.overview.tokenDailyChart.model_2')} {effectiveModel}</div>}
                  </div>
                )}
              </div>
            </div>
          )
        })}
      </div>
      {/* X-axis labels */}
      <div className="flex gap-1 text-[10px] text-muted">
        {recent.map((d, i) => (
          <div key={d.date} className="flex-1 text-center truncate">
            {i === 0 || i === recent.length - 1 || i === Math.floor(recent.length / 2)
              ? d.date.slice(5) : ''}
          </div>
        ))}
      </div>
      {/* Legend */}
      <div className="flex gap-4 mt-3 text-[12px] text-muted justify-center flex-wrap">
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm bg-accent inline-block" /> {i18nT('pages.overview.tokenDailyChart.input')}</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm inline-block" style={{ background: 'var(--warn)' }} /> {i18nT('pages.overview.tokenDailyChart.output')}</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm inline-block" style={{ background: 'var(--danger)' }} /> {i18nT('pages.overview.tokenDailyChart.cache_create_2')}</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-sm inline-block" style={{ background: 'var(--muted)' }} /> {i18nT('pages.overview.tokenDailyChart.cache_read_2')}</span>
      </div>
    </div>
  )
}

export default TokenDailyChart
