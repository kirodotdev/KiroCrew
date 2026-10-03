import { Fragment } from 'react'
import { BarChart3 } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardTitle, Badge } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { useProvider } from '../../providers'
import type { NormalizedUsage, TokenEstimate } from '../../providers'
import { providerUsageQuery } from '../../api/providerUsageQuery'
import { TokenDailyChart } from './TokenDailyChart'
import { formatCost } from '../../utils/formatCost'

import { fmtCompact, fmtNumber, fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

const EM_DASH = '\u2014'

/** A day's credits, localized, in the Billing card's two decimals; a dash when the day has no figure. */
function fmtCredits(credits: number | undefined): string {
  return credits == null ? EM_DASH : fmtNumber(credits, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

/**
 * A day's credits as a share of the CURRENT billing period's plan allowance --
 * the same `limit` the Billing card divides by, so the two reconcile. Not
 * clamped: a day can legitimately exceed 100% of the period allowance. A dash
 * when there is no allowance to divide by (no plan, or a zero/absent limit) or
 * no figure for the day, rather than a misleading 0%. The percent sign and its
 * spacing are the locale's (`fmtPercent`), not a hardcoded suffix.
 */
function fmtCreditsPct(credits: number | undefined, limit: number | undefined): string {
  if (credits == null || limit == null || !(limit > 0)) return EM_DASH
  return fmtPercent(credits / limit, { minimumFractionDigits: 1, maximumFractionDigits: 1 })
}

/** A day's estimated tokens in the locale's short form (`31M`); a dash when the day has no figure. */
function fmtTokens(tokens: number | undefined): string {
  return tokens == null ? EM_DASH : fmtCompact(tokens)
}

type EstimatedTokens = NonNullable<NormalizedUsage['sessions']['estimatedTokens']>

/**
 * Estimated token use for this calendar month and the whole previous one. Kiro
 * CLI records no token counts, so the backend builds these from each
 * turn's context size and request count; the note under the table says so,
 * because a figure shaped like a measurement would otherwise read as one.
 */
function EstimatedTokensCard({ e, refreshing }: { e: EstimatedTokens; refreshing: boolean }) {
  const rows: { label: string; value: (p: TokenEstimate) => string }[] = [
    { label: i18nT('pages.overview.usageTab.estimated_input_tokens'), value: p => fmtCompact(p.input) },
    { label: i18nT('pages.overview.usageTab.output_tokens'), value: p => fmtCompact(p.output) },
    { label: i18nT('pages.overview.usageTab.total_tokens'), value: p => fmtCompact(p.input + p.output) },
    { label: i18nT('pages.overview.usageTab.model_requests'), value: p => fmtNumber(p.requests) },
  ]
  return (
    <Card>
      <CardTitle><BarChart3 className="lucide-inline" /> {i18nT('pages.overview.usageTab.estimated_tokens')}</CardTitle>
      {refreshing ? (
        <div data-testid="usage-estimate-refreshing" className="skeleton h-32 rounded" />
      ) : (
        <>
          {e.incomplete && (
            <ErrorNotice
              variant="inline"
              askAgent
              className="mb-3"
              message={i18nT('pages.overview.usageTab.estimated_tokens_incomplete')}
            />
          )}
          <table className="w-full text-sm tabular-nums">
            <thead>
              <tr className="text-muted text-left">
                <th scope="col" className="pb-2"><span className="sr-only">{i18nT('pages.overview.usageTab.estimate_measure')}</span></th>
                <th scope="col" className="pb-2 font-medium text-right">{i18nT('pages.overview.usageTab.this_month')}</th>
                <th scope="col" className="pb-2 pl-4 font-medium text-right">{i18nT('pages.overview.usageTab.last_month')}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.label} className="border-t border-border">
                  <th scope="row" className="py-1.5 font-normal text-left text-muted">{r.label}</th>
                  <td className="py-1.5 text-right">{r.value(e.thisMonth)}</td>
                  <td className="py-1.5 pl-4 text-right">{r.value(e.lastMonth)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-[12px] text-muted">{i18nT('pages.overview.usageTab.estimated_tokens_note')}</p>
        </>
      )}
    </Card>
  )
}

export default function UsageTab() {
  const provider = useProvider()
  const { data, error: queryErr } = useQuery(providerUsageQuery(provider))
  const err = queryErr ? (queryErr instanceof Error ? queryErr.message : String(queryErr)) : ''

  if (!provider.capabilities.usageBilling) return (
    <Card>
      <div className="text-[13px] text-muted">
        {i18nT('pages.overview.usageTab.usage_tracking_is_not_available_for', { provider: provider.displayName })}
      </div>
    </Card>
  )

  if (err && !data) return (
    <Card><ErrorNotice message={err} askAgent /></Card>
  )

  if (!data) return <Card><div className="skeleton h-40 rounded" /></Card>

  const s = data.sessions
  const b = data.billing
  const pct = b?.percentUsed ?? null
  // The backend sends an all-zero estimate block to a dashboard with no kiro-cli
  // sessions, so the estimate shows only when it has something to say: a figure
  // in either month or on a day, or the unreadable-sessions warning.
  const e = s.estimatedTokens
  const hasEstimate = e != null && (
    e.incomplete
    || [e.thisMonth, e.lastMonth].some(p => p.input > 0 || p.output > 0 || p.requests > 0)
    || s.dailyHistory.some(d => d.estTokens != null && d.estTokens > 0)
  )
  // The column appears only with the card whose note explains the figures, and
  // only when the provider reports a per-day estimate, so an older gateway does
  // not show a column of dashes.
  const showTokens = hasEstimate && s.dailyHistory.some(d => d.estTokens != null)

  return (
    <div className="space-y-4">
      {err && <ErrorNotice title={i18nT('pages.sessionsTab.could_not_refresh')} message={err} askAgent />}
      {b && b.plan && (
        <Card>
          <CardTitle><BarChart3 className="lucide-inline" /> {i18nT('pages.overview.usageTab.billing')}</CardTitle>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
            <Row label={i18nT('pages.overview.usageTab.plan')} value={b.plan} />
            <Row label={b.unit === 'tokens' ? i18nT('pages.overview.usageTab.tokens') : b.unit === 'usd' ? i18nT('pages.overview.usageTab.spend') : i18nT('pages.overview.usageTab.credits')}
              value={b.limit ? `${b.used ?? 0} / ${b.limit}` : String(b.used ?? 0)}
              badge={pct != null ? (pct >= 90 ? 'err' : pct >= 70 ? 'warn' : 'ok') : undefined}
              badgeText={pct != null ? `${pct}%` : undefined} />
            {b.resets && <Row label={i18nT('pages.overview.usageTab.resets')} value={b.resets} />}
          </div>
        </Card>
      )}

      {hasEstimate && (
        <EstimatedTokensCard e={e} refreshing={data.refreshing === true} />
      )}

      {data.tokens && (
        <Card>
          <CardTitle><BarChart3 className="lucide-inline" /> {i18nT('pages.overview.usageTab.token_usage')}</CardTitle>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
            <Row label={i18nT('pages.overview.usageTab.input_tokens')} value={fmtNum(data.tokens.input)} />
            <Row label={i18nT('pages.overview.usageTab.output_tokens')} value={fmtNum(data.tokens.output)} />
            {data.tokens.cacheCreation > 0 && <Row label={i18nT('pages.overview.usageTab.cache_creation')} value={fmtNum(data.tokens.cacheCreation)} />}
            {data.tokens.cacheRead > 0 && <Row label={i18nT('pages.overview.usageTab.cache_read')} value={fmtNum(data.tokens.cacheRead)} />}
            <Row label={i18nT('pages.overview.usageTab.total_tokens')} value={fmtNum(data.tokens.total)} />
            {data.costUsd != null && <Row label={i18nT('pages.overview.usageTab.total_cost')} value={formatCost(data.costUsd)} />}
            {data.totalTurns != null && data.totalTurns > 0 && <Row label={i18nT('pages.overview.usageTab.total_turns')} value={data.totalTurns} />}
            {data.totalDurationMs != null && data.totalDurationMs > 0 && <Row label={i18nT('pages.overview.usageTab.total_api_time')} value={`${(data.totalDurationMs / 1000).toFixed(1)}s`} />}
          </div>
        </Card>
      )}

      {provider.id !== 'acp' && data.tokenDailyHistory && data.tokenDailyHistory.length > 0 && (
        <Card>
          <CardTitle><BarChart3 className="lucide-inline" /> {i18nT('pages.overview.usageTab.daily_token_usage')}</CardTitle>
          <TokenDailyChart
            history={data.tokenDailyHistory}
            providers={data.tokenProviders}
            models={data.tokenModels}
            providerModels={data.tokenProviderModels}
          />
        </Card>
      )}

      <Card>
        <CardTitle><BarChart3 className="lucide-inline" /> {i18nT('pages.overview.usageTab.session_activity_30_days')}</CardTitle>
        {data.refreshing ? (
          <div data-testid="usage-session-refreshing" className="skeleton h-32 rounded" />
        ) : (
          <>
            {s.refusedTranscripts > 0 && (
              <ErrorNotice
                variant="inline"
                askAgent
                className="mb-4"
                message={i18nT('pages.overview.usageTab.refused_transcripts_warning', { count: s.refusedTranscripts })}
              />
            )}
            <div className="grid grid-cols-3 gap-4 max-[600px]:grid-cols-1 mb-4">
              <PeriodCard label={i18nT('pages.overview.usageTab.today')} p={s.today} />
              <PeriodCard label={i18nT('pages.overview.usageTab.this_week')} p={s.thisWeek} />
              <PeriodCard label={i18nT('pages.overview.usageTab.this_month')} p={s.thisMonth} />
            </div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
              <Row label={i18nT('pages.overview.usageTab.total_sessions_30d')} value={s.total} />
              <Row label={i18nT('pages.overview.usageTab.avg_messages_session')} value={s.avgMsgsPerSession} />
            </div>
          </>
        )}
      </Card>

      {s.dailyHistory.length > 0 && (
        <Card>
          <CardTitle>{i18nT('pages.overview.usageTab.daily_history')}</CardTitle>
          <div className="max-h-64 overflow-y-auto">
            <table className="w-full text-sm tabular-nums">
              <thead className="sticky top-0 bg-bg-elevated">
                <tr className="text-muted text-left">
                  <th className="pb-2 font-medium">{i18nT('pages.overview.usageTab.date')}</th>
                  <th className="pb-2 font-medium text-right">{i18nT('pages.overview.usageTab.sessions')}</th>
                  <th className="pb-2 font-medium text-right">{i18nT('pages.overview.usageTab.messages')}</th>
                  <th className="pb-2 font-medium text-right">{i18nT('pages.overview.usageTab.tool_calls')}</th>
                  <th className="pb-2 font-medium text-right max-[600px]:hidden">{i18nT('pages.overview.usageTab.credits_used')}</th>
                  <th className="pb-2 font-medium text-right max-[600px]:hidden" title={i18nT('pages.overview.usageTab.credits_used_pct_title')}>
                    {i18nT('pages.overview.usageTab.credits_used_pct')}
                  </th>
                  {showTokens && (
                    <th className="pb-2 font-medium text-right max-[600px]:hidden" title={i18nT('pages.overview.usageTab.est_tokens_title')}>
                      {i18nT('pages.overview.usageTab.est_tokens')}
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {[...s.dailyHistory].reverse().map(d => {
                  const credits = fmtCredits(d.credits)
                  const pct = fmtCreditsPct(d.credits, b?.limit)
                  return (
                    <Fragment key={d.date}>
                      <tr className="border-t border-border">
                        <td className="py-1.5 font-mono text-[13px]">{d.date}</td>
                        <td className="py-1.5 text-right">{d.sessions}</td>
                        <td className="py-1.5 text-right">{d.messages}</td>
                        <td className="py-1.5 text-right">{d.toolCalls}</td>
                        <td className="py-1.5 text-right max-[600px]:hidden">{credits}</td>
                        <td className="py-1.5 text-right max-[600px]:hidden">{pct}</td>
                        {showTokens && <td className="py-1.5 text-right max-[600px]:hidden">{fmtTokens(d.estTokens)}</td>}
                      </tr>
                      {/* Phone: the extra columns would wrap every cell, and the repo forbids a
                          sideways-scrolling table, so the day's credit and token figures fold
                          onto a second line under the day instead of squeezing beside it. */}
                      <tr className="hidden max-[600px]:table-row text-muted text-[12px]" data-phone-line="">
                        <td colSpan={4} className="pb-1.5 text-right">
                          {showTokens
                            ? i18nT('pages.overview.usageTab.credits_used_tokens_compact', { credits, pct, tokens: fmtTokens(d.estTokens) })
                            : i18nT('pages.overview.usageTab.credits_used_compact', { credits, pct })}
                        </td>
                      </tr>
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  )
}

function Row({ label, value, badge, badgeText }: {
  label: string; value: string | number
  badge?: 'ok' | 'err' | 'warn'; badgeText?: string
}) {
  return (
    <div className="flex justify-between items-center gap-3 py-2 border-b border-border text-sm">
      <span className="text-muted">{label}</span>
      <span className="text-text font-mono text-[13px] flex items-center gap-2">
        {value}
        {badge && badgeText && <Badge variant={badge}>{badgeText}</Badge>}
      </span>
    </div>
  )
}

function PeriodCard({ label, p }: { label: string; p: { sessions: number; messages: number; toolCalls: number } }) {
  return (
    <div className="bg-bg-elevated rounded-lg p-3 text-center">
      <div className="text-muted text-[13px] mb-1">{label}</div>
      <div className="text-2xl font-bold text-text">{p.sessions}</div>
      <div className="text-muted text-[12px] mt-1">
        {p.messages} {i18nT('pages.overview.usageTab.msgs')} {p.toolCalls} {i18nT('pages.overview.usageTab.tools')}
      </div>
    </div>
  )
}
