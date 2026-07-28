import { BarChart3, AlertTriangle } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardTitle, Badge } from '../../components/ui'
import { useProvider } from '../../providers'
import type { NormalizedUsage } from '../../providers'
import { TokenDailyChart } from './TokenDailyChart'
import { formatCost } from '../../utils/formatCost'

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
    ? `Usage tracking is not available for ${provider.displayName}.`
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
          <CardTitle><BarChart3 className="lucide-inline" /> Billing</CardTitle>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
            <Row label="Plan" value={b.plan} />
            <Row label={b.unit === 'tokens' ? 'Tokens' : b.unit === 'usd' ? 'Spend' : 'Credits'}
              value={b.limit ? `${b.used ?? 0} / ${b.limit}` : String(b.used ?? 0)}
              badge={pct != null ? (pct >= 90 ? 'err' : pct >= 70 ? 'warn' : 'ok') : undefined}
              badgeText={pct != null ? `${pct}%` : undefined} />
            {b.resets && <Row label="Resets" value={b.resets} />}
          </div>
        </Card>
      )}

      {data.tokens && (
        <Card>
          <CardTitle><BarChart3 className="lucide-inline" /> Token Usage</CardTitle>
          <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
            <Row label="Input tokens" value={fmtNum(data.tokens.input)} />
            <Row label="Output tokens" value={fmtNum(data.tokens.output)} />
            {data.tokens.cacheCreation > 0 && <Row label="Cache creation" value={fmtNum(data.tokens.cacheCreation)} />}
            {data.tokens.cacheRead > 0 && <Row label="Cache read" value={fmtNum(data.tokens.cacheRead)} />}
            <Row label="Total tokens" value={fmtNum(data.tokens.total)} />
            {data.costUsd != null && <Row label="Total cost" value={formatCost(data.costUsd)} />}
            {data.totalTurns != null && data.totalTurns > 0 && <Row label="Total turns" value={data.totalTurns} />}
            {data.totalDurationMs != null && data.totalDurationMs > 0 && <Row label="Total API time" value={`${(data.totalDurationMs / 1000).toFixed(1)}s`} />}
          </div>
        </Card>
      )}

      {provider.id !== 'acp' && data.tokenDailyHistory && data.tokenDailyHistory.length > 0 && (
        <Card>
          <CardTitle><BarChart3 className="lucide-inline" /> Daily Token Usage</CardTitle>
          <TokenDailyChart
            history={data.tokenDailyHistory}
            providers={data.tokenProviders}
            models={data.tokenModels}
            providerModels={data.tokenProviderModels}
          />
        </Card>
      )}

      <Card>
        <CardTitle><BarChart3 className="lucide-inline" /> Session Activity (30 days)</CardTitle>
        <div className="grid grid-cols-3 gap-4 max-[600px]:grid-cols-1 mb-4">
          <PeriodCard label="Today" p={s.today} />
          <PeriodCard label="This Week" p={s.thisWeek} />
          <PeriodCard label="This Month" p={s.thisMonth} />
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <Row label="Total sessions (30d)" value={s.total} />
          <Row label="Avg messages/session" value={s.avgMsgsPerSession} />
        </div>
      </Card>

      {s.dailyHistory.length > 0 && (
        <Card>
          <CardTitle>Daily History</CardTitle>
          <div className="max-h-64 overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-bg-elevated">
                <tr className="text-muted text-left">
                  <th className="pb-2 font-medium">Date</th>
                  <th className="pb-2 font-medium text-right">Sessions</th>
                  <th className="pb-2 font-medium text-right">Messages</th>
                  <th className="pb-2 font-medium text-right">Tool Calls</th>
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
        {p.messages} msgs / {p.toolCalls} tools
      </div>
    </div>
  )
}
