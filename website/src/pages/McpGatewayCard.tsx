import { useQuery } from '@tanstack/react-query'
import { Circle } from 'lucide-react'
import { api } from '../api/client'
import { StatCard, Card } from '../components/ui'

type Backend = { server: string; agent: string; pid: number | null; sessions: number; idle_s: number; rss_kb: number }
type Metrics = {
  running: boolean; size?: number; max_backends?: number; backends: Backend[]
  // Present only when prewarming is enabled (gatewayd folds in the warm-pool
  // hit tally); absent otherwise, so the hit-rate StatCard is hidden.
  warm_pool_hits?: number; warm_pool_misses?: number; warm_pool_hit_rate_pct?: number
}
type Status = { enabled: boolean; running: boolean; ping_ok: boolean }

const SIZE_UNITS = ['KB', 'MB', 'GB', 'TB', 'PB'] as const

/** Format a KB value in the largest sensible unit. KB/MB render as integers;
 *  GB and up keep one decimal (e.g. 6086 MB -> "5.9 GB"). Non-positive -> "—".
 *  Exported for unit testing. */
export const formatKb = (kb: number): string => {
  if (!(kb > 0)) return '—'
  let value = kb
  let unit = 0
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024
    unit += 1
  }
  let decimals = unit >= 2 ? 1 : 0
  // Rounding can push the display to "1024" (e.g. 1023.96 MB -> "1024 MB");
  // promote one more unit so it reads "1.0 GB" instead.
  if (Number(value.toFixed(decimals)) >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024
    unit += 1
    decimals = unit >= 2 ? 1 : 0
  }
  return `${value.toFixed(decimals)} ${SIZE_UNITS[unit]}`
}

// A backend's pid can be null (just-spawned / not yet reported). Keying rows on
// `server-${pid}` alone would collide to `server-null` for two such rows, so
// React would warn about duplicate keys and may reuse the wrong row's DOM.
// Fall back to the row index to keep keys unique. Exported for unit testing.
export const backendRowKey = (b: { server: string; pid: number | null }, i: number) =>
  `${b.server}-${b.pid ?? `i${i}`}`

/** Live metrics for the shared MCP gateway. Renders only when the gateway is enabled. */
export default function McpGatewayCard() {
  const statusQ = useQuery<Status>({ queryKey: ['mcpGatewayStatus'], queryFn: () => api.mcpGatewayStatus(), refetchInterval: 5000 })
  const enabled = statusQ.data?.enabled ?? false

  const metricsQ = useQuery<Metrics>({
    queryKey: ['mcpGatewayMetrics'],
    queryFn: () => api.mcpGatewayMetrics(),
    enabled,
    refetchInterval: enabled ? 3000 : false,
  })

  if (!enabled) return null

  const backends = metricsQ.data?.backends ?? []
  const sessions = backends.reduce((n, b) => n + b.sessions, 0)
  const poolKb = backends.reduce((n, b) => n + Math.max(0, b.rss_kb), 0)
  const unpooledKb = backends.reduce((n, b) => n + Math.max(0, b.rss_kb) * Math.max(1, b.sessions), 0)
  const savedKb = Math.max(0, unpooledKb - poolKb)
  const savedPct = unpooledKb > 0 ? Math.round((savedKb / unpooledKb) * 100) : 0
  const withPct = unpooledKb > 0 ? Math.max(4, Math.round((poolKb / unpooledKb) * 100)) : 0

  const running = statusQ.data?.running ?? false
  const healthy = running && (statusQ.data?.ping_ok ?? false)

  // Pool warm-hit rate: shown only when gatewayd reports it (prewarming on).
  // `hits + misses` is the observed register count it's measured over.
  const warmHits = metricsQ.data?.warm_pool_hits
  const warmMisses = metricsQ.data?.warm_pool_misses
  const warmEnabled = warmHits !== undefined && warmMisses !== undefined
  const warmTotal = warmEnabled ? warmHits + warmMisses : 0

  return (
    <Card className="mb-6">
      <div className="flex items-center justify-between mb-3">
        <span className="text-[15px] font-semibold text-text-strong">Shared MCP gateway</span>
        <span className={`inline-flex items-center gap-1 text-[12px] ${healthy ? 'text-ok' : 'text-danger'}`}>
          <Circle className="lucide-inline w-2 h-2 fill-current" />
          {healthy ? 'active' : running ? 'unhealthy' : 'stopped'}
        </span>
      </div>

      <div className="grid gap-3 grid-cols-[repeat(auto-fit,minmax(130px,1fr))] mb-4">
        <StatCard label="Backends" value={`${backends.length}${metricsQ.data?.max_backends ? ` / ${metricsQ.data.max_backends}` : ''}`} />
        <StatCard label="Active sessions" value={sessions} />
        <StatCard label="Pool RAM" value={formatKb(poolKb)} />
        <StatCard label="RAM saved" value={`${formatKb(savedKb)} (${savedPct}%)`} accent />
        {warmEnabled && (
          <StatCard
            label="Pool warm-hit rate"
            value={warmTotal > 0 ? `${metricsQ.data?.warm_pool_hit_rate_pct ?? 0}% (${warmHits}/${warmTotal})` : '—'}
          />
        )}
      </div>

      {/* Before/after RAM comparison */}
      <div className="mb-4">
        <div className="flex justify-between text-[11px] text-muted mb-1"><span>Without gateway (per-session copies)</span><span>{formatKb(unpooledKb)}</span></div>
        <div className="h-[18px] rounded-md mb-2" style={{ background: 'var(--danger)', width: '100%' }} />
        <div className="flex justify-between text-[11px] text-muted mb-1"><span>With shared gateway (pooled)</span><span>{formatKb(poolKb)}</span></div>
        <div className="h-[18px] rounded-md" style={{ background: 'var(--ok)', width: `${withPct}%` }} />
      </div>

      {backends.length === 0 ? (
        <div className="text-[12px] text-muted py-1">No backends spawned yet — start a session that calls an MCP tool to populate the pool.</div>
      ) : (
        <table className="w-full text-[12px]">
          <thead>
            <tr className="text-muted text-left">
              <th className="font-normal py-1 pr-3">Server</th>
              <th className="font-normal py-1 pr-3">PID</th>
              <th className="font-normal py-1 pr-3">Sessions</th>
              <th className="font-normal py-1 pr-3">Idle</th>
              <th className="font-normal py-1">RSS</th>
            </tr>
          </thead>
          <tbody className="text-text">
            {backends.map((b, i) => (
              <tr key={backendRowKey(b, i)} className="border-t border-border">
                <td className="py-1 pr-3">{b.server}</td>
                <td className="py-1 pr-3">{b.pid ?? '—'}</td>
                <td className="py-1 pr-3">{b.sessions}</td>
                <td className="py-1 pr-3">{b.idle_s}s</td>
                <td className="py-1">{formatKb(b.rss_kb)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  )
}
