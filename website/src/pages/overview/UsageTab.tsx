import { BarChart3, AlertTriangle } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardTitle, Badge } from '../../components/ui'
import { useProvider } from '../../providers'
import type { NormalizedUsage } from '../../providers'
import { TokenDailyChart } from './TokenDailyChart'
import { formatCost } from '../../utils/formatCost'

import { i18nT } from '../../i18n/t'
function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

export default function UsageTab() {
  const provider = useProvider()
  const { data, error: queryErr } = useQuery<NormalizedUsage>({
    queryKey: ['provider-usage', provider.id],
    queryFn: () => provider.fetchUsage(),
    enabled: provider.capabilities.usageBilling,
  })
  const err = !provider.capabilities.usageBilling
    ? i18nT('pages.overview.usageTab.usage_tracking_is_not_available_for', { provider: provider.displayName })
    : queryErr ? (queryErr instanceof Error ? queryErr.message : String(queryErr)) : ''

  if (err) return (
    <Card>
      <div className="flex items-center gap-2 text-danger text-sm">
        <AlertTriangle className="lucide-inline" /> {err}
      </div>
    </Card>
  )

  if (!data) return <Card><div className="skeleton h-40 rounded" /></Card>

  const s = data.sessions
  const b = data.billing
  const pct = b?.percentUsed ?? null

  return (
    <div className="space-y-4">
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
        <div className="grid grid-cols-3 gap-4 max-[600px]:grid-cols-1 mb-4">
          <PeriodCard label={i18nT('pages.overview.usageTab.today')} p={s.today} />
          <PeriodCard label={i18nT('pages.overview.usageTab.this_week')} p={s.thisWeek} />
          <PeriodCard label={i18nT('pages.overview.usageTab.this_month')} p={s.thisMonth} />
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <Row label={i18nT('pages.overview.usageTab.total_sessions_30d')} value={s.total} />
          <Row label={i18nT('pages.overview.usageTab.avg_messages_session')} value={s.avgMsgsPerSession} />
        </div>
      </Card>

      {s.dailyHistory.length > 0 && (
        <Card>
          <CardTitle>{i18nT('pages.overview.usageTab.daily_history')}</CardTitle>
          <div className="max-h-64 overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-bg-elevated">
                <tr className="text-muted text-left">
                  <th className="pb-2 font-medium">{i18nT('pages.overview.usageTab.date')}</th>
                  <th className="pb-2 font-medium text-right">{i18nT('pages.overview.usageTab.sessions')}</th>
                  <th className="pb-2 font-medium text-right">{i18nT('pages.overview.usageTab.messages')}</th>
                  <th className="pb-2 font-medium text-right">{i18nT('pages.overview.usageTab.tool_calls')}</th>
                </tr>
              </thead>
              <tbody>
                {[...s.dailyHistory].reverse().map(d => (
                  <tr key={d.date} className="border-t border-border">
                    <td className="py-1.5 font-mono text-[13px]">{d.date}</td>
                    <td className="py-1.5 text-right">{d.sessions}</td>
                    <td className="py-1.5 text-right">{d.messages}</td>
                    <td className="py-1.5 text-right">{d.toolCalls}</td>
                  </tr>
                ))}
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
