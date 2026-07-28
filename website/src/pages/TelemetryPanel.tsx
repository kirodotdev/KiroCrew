import { useQuery } from '@tanstack/react-query'
import { Activity, Rocket, Zap } from 'lucide-react'
import { api } from '../api/client'

import { i18nT } from '../i18n/t'
// ── GET /api/telemetry/startup shape (dashboard/handlers/telemetry.py) ──
type Stat = {
  count: number
  mean_ms: number
  p50_ms: number
  p90_ms: number
  min_ms: number
  max_ms: number
}
type Startup = {
  overall: Stat
  cold: Stat
  warm: Stat
  outcome: Record<string, number>
  daily: { date: string; count: number; cold_p50_ms: number; cold_p90_ms: number; warm_p50_ms: number }[]
  distribution: { buckets: number[]; bounds: number[] }
}
type Turn = Stat & { outcome: Record<string, number>; fault_rate: number }
type Other = {
  name: string
  kind: string
  count?: number
  p50_ms?: number
  p90_ms?: number
  total?: number
  by_attr?: Record<string, number>
}
type Resp = {
  enabled: boolean
  window_days: number
  shard_count: number
  metrics_dir: string
  startup: Startup | null
  turn: Turn | null
  other: Other[]
}

const fmtMs = (ms?: number | null): string =>
  ms == null ? '—' : ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'
const pct = (n: number, d: number): number => (d > 0 ? Math.round((n / d) * 100) : 0)

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="text-muted text-sm py-12 text-center leading-relaxed">{children}</div>
}

function Section({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="mb-7">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted mb-3 flex items-center gap-1.5">
        {icon}
        {title}
      </h3>
      {children}
    </div>
  )
}

// One KPI tile: label + big value + optional sub + optional accent color.
function Tile({
  label,
  value,
  unit,
  sub,
  color,
}: {
  label: string
  value: string
  unit?: string
  sub?: string
  color?: string
}) {
  return (
    <div className="card-glow border border-border bg-card rounded-xl p-3.5">
      <div className="text-[10px] text-muted mb-1">{label}</div>
      <div className="text-[22px] font-bold leading-none" style={color ? { color } : undefined}>
        {value}
        {unit && <span className="text-[11px] text-muted font-normal ml-0.5">{unit}</span>}
      </div>
      {sub && <div className="text-[10px] text-muted mt-1.5">{sub}</div>}
    </div>
  )
}

function Card({ title, meaning, children }: { title: string; meaning: string; children: React.ReactNode }) {
  return (
    <div className="card-glow border border-border bg-card rounded-xl p-3.5">
      <div className="text-[13px] font-semibold">{title}</div>
      <div className="text-[10px] text-muted mb-2">{meaning}</div>
      {children}
    </div>
  )
}

export default function TelemetryPanel() {
  const { data, isLoading } = useQuery<Resp>({
    queryKey: ['telemetry-startup'],
    queryFn: () => api.telemetryStartup(),
    refetchInterval: 5000,
  })

  if (isLoading && !data) return <Notice>{i18nT('pages.telemetryPanel.loading_telemetry')}</Notice>
  if (data && !data.enabled) {
    return (
      <Notice>
        <div className="text-text font-medium mb-1">{i18nT('pages.telemetryPanel.telemetry_is_off')}</div>
        {i18nT('pages.telemetryPanel.enable_with')} <code className="text-accent">{i18nT('pages.telemetryPanel.telemetry_enabled_true')}</code>{i18nT('pages.telemetryPanel.metrics_stay_local')}
        <code className="text-accent">{data.metrics_dir}</code>{i18nT('pages.telemetryPanel.nothing_leaves_this_machine')}
      </Notice>
    )
  }

  const s = data?.startup ?? null
  const t = data?.turn ?? null
  const other = data?.other ?? []
  const hasData = !!(s && s.overall.count) || !!(t && t.count) || other.length > 0
  if (!data || !hasData) {
    return <Notice>{i18nT('pages.telemetryPanel.no_telemetry_recorded_yet_in_the_last')} {data?.window_days ?? 14} {i18nT('pages.telemetryPanel.days')}</Notice>
  }

  const oh = (name: string) => other.find(o => o.name === name && o.kind === 'histogram')
  const oc = (name: string) => other.find(o => o.name === name && o.kind === 'counter')
  const warm = oc('kirocrew.mcp.warm_pool.acquire')
  const warmHit = warm?.by_attr?.['result=hit'] ?? 0
  const warmMiss = warm?.by_attr?.['result=miss'] ?? 0
  const warmRate = pct(warmHit, warmHit + warmMiss)
  const acquire = oh('kirocrew.mcp.backend.acquire.duration')
  const mcpLazy = oh('kirocrew.mcp.lazy_load.duration')
  const mcpLazyN = oc('kirocrew.mcp.lazy_load.count')?.total ?? 0
  const skillLazy = oh('kirocrew.skill.lazy_load.duration')
  const skillLazyN = oc('kirocrew.skill.lazy_load.count')?.total ?? 0

  const readyRate = s ? pct(s.outcome.ready ?? 0, Object.values(s.outcome).reduce((a, b) => a + b, 0)) : 0
  const faultPct = t ? Math.round(t.fault_rate * 100) : null
  const faultColor =
    faultPct == null ? undefined : faultPct < 2 ? 'var(--ok)' : faultPct < 10 ? 'var(--warn)' : 'var(--danger)'
  const turnFaults = t ? (t.outcome.error ?? 0) + (t.outcome.timeout ?? 0) : 0

  // Startup latency distribution: only render non-empty buckets.
  const distRows: { label: string; count: number }[] = []
  if (s?.distribution?.buckets?.length) {
    const { buckets, bounds } = s.distribution
    buckets.forEach((c, i) => {
      if (c > 0) {
        const label = i >= bounds.length ? `> ${fmtMs(bounds[bounds.length - 1])}` : `≤ ${fmtMs(bounds[i])}`
        distRows.push({ label, count: c })
      }
    })
  }
  const distMax = Math.max(1, ...distRows.map(r => r.count))

  return (
    <div className="overflow-y-auto flex-1 min-h-0 pb-8">
      {/* ── Section 1: Key metrics ─────────────────────────────── */}
      <Section title={i18nT('pages.telemetryPanel.key_metrics')} icon={<Activity size={13} />}>
        <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
          <Tile
            label={i18nT('pages.telemetryPanel.turn_latency_p50')}
            value={t ? fmtMs(t.p50_ms) : '—'}
            sub={t ? `p90 ${fmtMs(t.p90_ms)}` : 'no turns yet'}
            color="var(--accent)"
          />
          <Tile
            label={i18nT('pages.telemetryPanel.fault_rate')}
            value={faultPct == null ? '—' : String(faultPct)}
            unit={faultPct == null ? undefined : '%'}
            sub={t ? `${turnFaults} faults / ${t.count} turns` : 'no turns yet'}
            color={faultColor}
          />
          <Tile label={i18nT('pages.telemetryPanel.throughput')} value={t ? String(t.count) : '—'} unit={t ? 'turns' : undefined} sub={`last ${data.window_days}d`} />
          <Tile
            label={i18nT('pages.telemetryPanel.ready_rate')}
            value={s ? String(readyRate) : '—'}
            unit={s ? '%' : undefined}
            sub={s ? `${s.overall.count} startups` : undefined}
            color={s && readyRate >= 95 ? 'var(--ok)' : undefined}
          />
        </div>
        {t && (t.outcome.ok ?? 0) + turnFaults > 0 && (
          <div className="mt-2.5">
            <div className="flex h-2 rounded-md overflow-hidden border border-border">
              <span style={{ flex: t.outcome.ok ?? 0, background: 'var(--ok)' }} />
              <span style={{ flex: t.outcome.error ?? 0, background: 'var(--danger)' }} />
              <span style={{ flex: t.outcome.timeout ?? 0, background: 'var(--warn)' }} />
            </div>
            <div className="flex gap-4 mt-1 text-[10px] text-muted">
              <span>{i18nT('pages.telemetryPanel.ok')} {t.outcome.ok ?? 0}</span>
              <span>{i18nT('pages.telemetryPanel.error')} {t.outcome.error ?? 0}</span>
              <span>{i18nT('pages.telemetryPanel.timeout')} {t.outcome.timeout ?? 0}</span>
            </div>
          </div>
        )}
        {!t && (
          <div className="text-muted text-[11px] mt-2">
            {i18nT('pages.telemetryPanel.agent_turn_latency_fault_rate_populate_after_the')}
          </div>
        )}
      </Section>

      {/* ── Section 2: Session startup ─────────────────────────── */}
      {s && s.overall.count > 0 && (
        <Section title={i18nT('pages.telemetryPanel.session_startup')} icon={<Rocket size={13} />}>
          <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(140px,1fr))] mb-3">
            <Tile label={i18nT('pages.telemetryPanel.cold_start_p50')} value={fmtMs(s.cold.p50_ms)} color="var(--accent)" sub={`${s.cold.count} cold`} />
            <Tile label={i18nT('pages.telemetryPanel.cold_start_p90')} value={fmtMs(s.cold.p90_ms)} color="var(--warn)" />
            <Tile label={i18nT('pages.telemetryPanel.warm_start_p50')} value={fmtMs(s.warm.p50_ms)} color="var(--ok)" sub={`${s.warm.count} warm`} />
            <Tile label={i18nT('pages.telemetryPanel.overall_mean')} value={fmtMs(s.overall.mean_ms)} sub={`min ${fmtMs(s.overall.min_ms)} · max ${fmtMs(s.overall.max_ms)}`} />
          </div>
          {distRows.length > 0 && (
            <div className="card-glow border border-border bg-card rounded-xl p-3.5">
              <div className="text-[10px] text-muted mb-2">{i18nT('pages.telemetryPanel.startup_latency_distribution_from_otel_histogram')}</div>
              <div className="flex flex-col gap-1.5">
                {distRows.map(r => (
                  <div key={r.label} className="flex items-center gap-2 text-[10px]">
                    <span className="text-muted w-16 shrink-0 text-right font-mono">{r.label}</span>
                    <div className="flex-1 h-3 bg-[var(--bg)] rounded overflow-hidden">
                      <span className="block h-full rounded" style={{ width: `${(r.count / distMax) * 100}%`, background: 'var(--accent)' }} />
                    </div>
                    <span className="text-text font-mono w-6 text-right shrink-0">{r.count}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Section>
      )}

      {/* ── Section 3: Acceleration internals ──────────────────── */}
      <Section title={i18nT('pages.telemetryPanel.acceleration_internals')} icon={<Zap size={13} />}>
        <div className="grid grid-cols-2 gap-2.5 max-[720px]:grid-cols-1">
          <Card title={i18nT('pages.telemetryPanel.warm_pool_efficiency')} meaning="Sessions reusing a warm MCP backend vs a cold spawn">
            {warmHit + warmMiss > 0 ? (
              <>
                <div className="text-[18px] font-bold" style={{ color: warmRate >= 50 ? 'var(--ok)' : 'var(--warn)' }}>
                  {warmRate}
                  <span className="text-[10px] text-muted font-normal">{i18nT('pages.telemetryPanel.hit')}</span>
                </div>
                <div className="flex h-3.5 rounded-md overflow-hidden border border-border mt-1.5">
                  <span style={{ flex: warmHit, background: 'var(--ok)' }} title={`hit ${warmHit}`} />
                  <span style={{ flex: warmMiss, background: 'var(--muted)' }} title={`miss ${warmMiss}`} />
                </div>
                <div className="text-[10px] text-muted mt-1">{warmHit} {i18nT('pages.telemetryPanel.hit_2')} {warmMiss} {i18nT('pages.telemetryPanel.cold_spawn')}</div>
              </>
            ) : (
              <div className="text-muted text-[11px]">{i18nT('pages.telemetryPanel.no_acquisitions_yet')}</div>
            )}
          </Card>
          <Card title={i18nT('pages.telemetryPanel.mcp_backend_acquire')} meaning="Time to hand a pooled MCP backend to a session">
            {acquire ? (
              <div className="text-[18px] font-bold">
                {fmtMs(acquire.p50_ms)}
                <span className="text-[10px] text-muted font-normal"> {i18nT('pages.telemetryPanel.typ_p90')} {fmtMs(acquire.p90_ms)} {i18nT('pages.telemetryPanel.n')}{acquire.count ?? 0}</span>
              </div>
            ) : (
              <div className="text-muted text-[11px]">{i18nT('pages.telemetryPanel.no_data_yet')}</div>
            )}
          </Card>
          <Card title={i18nT('pages.telemetryPanel.mcp_cold_load_first_use')} meaning="First-use spawn of an MCP server backend">
            {mcpLazy ? (
              <div className="text-[18px] font-bold">
                {fmtMs(mcpLazy.p50_ms)}
                <span className="text-[10px] text-muted font-normal"> · {mcpLazyN} {i18nT('pages.telemetryPanel.loads')}</span>
              </div>
            ) : (
              <div className="text-muted text-[11px]">{i18nT('pages.telemetryPanel.no_data_yet')}</div>
            )}
          </Card>
          <Card title={i18nT('pages.telemetryPanel.skill_load')} meaning="On-demand read of a skill body from disk">
            {skillLazy ? (
              <div className="text-[18px] font-bold" style={{ color: 'var(--ok)' }}>
                {fmtMs(skillLazy.p50_ms)}
                <span className="text-[10px] text-muted font-normal"> · {skillLazyN} {i18nT('pages.telemetryPanel.loads')}</span>
              </div>
            ) : (
              <div className="text-muted text-[11px]">{i18nT('pages.telemetryPanel.no_data_yet')}</div>
            )}
          </Card>
        </div>
      </Section>

      <div className="text-muted text-[11px] mt-2">
        {i18nT('pages.telemetryPanel.window_last')} {data.window_days}{i18nT('pages.telemetryPanel.d')} {data.shard_count} {i18nT('pages.telemetryPanel.shard_s_source')} <code>{data.metrics_dir}</code> {i18nT('pages.telemetryPanel.local_only_no_egress')}
      </div>
    </div>
  )
}
