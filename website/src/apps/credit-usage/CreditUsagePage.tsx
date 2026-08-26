// Credit Usage — dashboard page (/credit-usage).
//
// A real-time view of the credits Kiro Crew agents consume. The gateway writes
// one row per agent turn to <data home>/usage/tokens/<date>.jsonl; the app's
// backend rolls those rows up and this page polls the rollup on a short interval
// (there is no usage push channel on the dashboard ws, so we poll — the backend
// memoizes reads by shard signature, so a poll between turns is essentially
// free). Credits are provider-reported, not derived from tokens, so everything
// here is a straight sum of the `credits` field.
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Coins, Cpu, Layers, RefreshCw, Users } from 'lucide-react'

import { Badge, Card, CardTitle, StatCard } from '../../components/ui'
import { i18nT } from '../../i18n/t'

import { creditUsageApi } from './api'
import {
  DEFAULT_WINDOW_DAYS,
  POLL_MS,
  RECENT_LIMIT,
  STORAGE_KEY,
  WINDOW_CHOICES,
} from './constants'
import type { BreakdownRow, RecentRow, SummaryResponse, TopSession } from './types'

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

function fmtCredits(n: number | null | undefined): string {
  const v = typeof n === 'number' && isFinite(n) ? n : 0
  if (v >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
  if (v >= 100) return v.toFixed(1)
  return v.toFixed(2)
}

function fmtInt(n: number | null | undefined): string {
  const v = typeof n === 'number' && isFinite(n) ? n : 0
  return v.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

function pct(part: number, whole: number): number {
  if (!whole) return 0
  return Math.round((part / whole) * 100)
}

function relTime(iso: string): string {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (isNaN(t)) return ''
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (secs < 60) return i18nT('creditUsage.agoSeconds', { n: secs })
  const mins = Math.round(secs / 60)
  if (mins < 60) return i18nT('creditUsage.agoMinutes', { n: mins })
  const hrs = Math.round(mins / 60)
  if (hrs < 48) return i18nT('creditUsage.agoHours', { n: hrs })
  const days = Math.round(hrs / 24)
  return i18nT('creditUsage.agoDays', { n: days })
}

function surfaceVariant(surface: string): 'ok' | 'err' | 'warn' | 'aim' | 'muted' {
  switch (surface) {
    case 'dashboard':
      return 'aim'
    case 'subagent':
      return 'ok'
    case 'cron':
      return 'warn'
    case 'webhook':
      return 'warn'
    default:
      return 'muted'
  }
}

function shortSlot(slot: string): string {
  // dashboard:chat-69-1785905004 -> chat-69 ; subagent:5e240c42 -> subagent 5e240c42
  const s = slot.replace(/^dashboard:/, '')
  const m = s.match(/^(chat-\d+)/)
  if (m) return m[1]
  return s.length > 28 ? s.slice(0, 27) + '…' : s
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function TrendChart({ data }: { data: SummaryResponse['trend'] }) {
  if (!data.length) return null
  const max = Math.max(1, ...data.map((d) => d.credits))
  const W = 720
  const H = 160
  const pad = 4
  const bw = (W - pad * 2) / data.length
  // Label at most ~8 ticks so the axis stays readable across window sizes.
  const step = Math.max(1, Math.ceil(data.length / 8))
  return (
    <div style={{ width: '100%', overflowX: 'auto' }}>
      <svg
        viewBox={`0 0 ${W} ${H + 22}`}
        width="100%"
        height={H + 22}
        role="img"
        aria-label={i18nT('creditUsage.trendTitle')}
        preserveAspectRatio="none"
      >
        {data.map((d, i) => {
          const h = Math.max(1, (d.credits / max) * H)
          const x = pad + i * bw
          const y = H - h
          return (
            <g key={d.date}>
              <rect
                x={x + 1}
                y={y}
                width={Math.max(1, bw - 2)}
                height={h}
                rx={2}
                fill="var(--accent, #6366f1)"
                opacity={d.credits > 0 ? 0.85 : 0.15}
              >
                <title>{`${d.date} — ${fmtCredits(d.credits)} credits`}</title>
              </rect>
              {i % step === 0 && (
                <text
                  x={x + bw / 2}
                  y={H + 15}
                  textAnchor="middle"
                  fontSize="9"
                  fill="var(--text-muted, #888)"
                >
                  {d.date.slice(5)}
                </text>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}

function BreakdownBars({ rows, total }: { rows: BreakdownRow[]; total: number }) {
  if (!rows.length) {
    return <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>{i18nT('creditUsage.noData')}</div>
  }
  const top = rows.slice(0, 8)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {top.map((r) => {
        const p = pct(r.credits, total)
        return (
          <div key={r.name} style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
              <span style={{ fontWeight: 600 }}>{r.name}</span>
              <span style={{ color: 'var(--text-muted, #888)' }}>
                {fmtCredits(r.credits)} · {p}% · {fmtInt(r.turns)}
                {' '}
                {i18nT('creditUsage.turnsShort')}
              </span>
            </div>
            <div
              style={{
                height: 8,
                borderRadius: 4,
                background: 'var(--surface-2, rgba(127,127,127,0.15))',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${p}%`,
                  height: '100%',
                  background: 'var(--accent, #6366f1)',
                  borderRadius: 4,
                }}
              />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function TopSessionsTable({ rows }: { rows: TopSession[] }) {
  if (!rows.length) {
    return <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>{i18nT('creditUsage.noData')}</div>
  }
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
        <thead>
          <tr style={{ textAlign: 'left', color: 'var(--text-muted, #888)' }}>
            <th style={{ padding: '6px 8px' }}>{i18nT('creditUsage.colSession')}</th>
            <th style={{ padding: '6px 8px' }}>{i18nT('creditUsage.colSurface')}</th>
            <th style={{ padding: '6px 8px', textAlign: 'right' }}>{i18nT('creditUsage.colCredits')}</th>
            <th style={{ padding: '6px 8px', textAlign: 'right' }}>{i18nT('creditUsage.colTurns')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.slot} style={{ borderTop: '1px solid var(--border, rgba(127,127,127,0.2))' }}>
              <td style={{ padding: '6px 8px' }} title={r.slot}>
                <div style={{ fontWeight: 500, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 340 }}>
                  {r.title && r.title !== r.slot ? r.title : shortSlot(r.slot)}
                </div>
                {r.title && r.title !== r.slot && (
                  <div style={{ fontSize: 11, color: 'var(--text-muted, #888)', fontFamily: 'var(--font-mono, monospace)' }}>
                    {shortSlot(r.slot)}
                  </div>
                )}
              </td>
              <td style={{ padding: '6px 8px' }}>
                <Badge variant={surfaceVariant(r.surface)}>{r.surface}</Badge>
              </td>
              <td style={{ padding: '6px 8px', textAlign: 'right', fontWeight: 600 }}>{fmtCredits(r.credits)}</td>
              <td style={{ padding: '6px 8px', textAlign: 'right', color: 'var(--text-muted, #888)' }}>
                {fmtInt(r.turns)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function RecentFeed({ rows }: { rows: RecentRow[] }) {
  if (!rows.length) {
    return <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>{i18nT('creditUsage.noData')}</div>
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {rows.map((r, i) => {
        const ctxPct = r.contextWindow ? pct(r.contextUsed, r.contextWindow) : 0
        return (
          <div
            key={`${r.ts}-${i}`}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              padding: '6px 8px',
              borderRadius: 6,
              background: 'var(--surface-2, rgba(127,127,127,0.06))',
              fontSize: 12,
            }}
          >
            <Badge variant={surfaceVariant(r.surface)}>{r.surface}</Badge>
            <span
              style={{ minWidth: 90, maxWidth: 260, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}
              title={r.slot}
            >
              {r.title && r.title !== r.slot ? r.title : shortSlot(r.slot)}
            </span>
            <span style={{ color: 'var(--text-muted, #888)' }}>{r.model}</span>
            <span style={{ flex: 1 }} />
            {r.contextWindow > 0 && (
              <span style={{ color: 'var(--text-muted, #888)' }} title={i18nT('creditUsage.contextTip')}>
                {ctxPct}% {i18nT('creditUsage.ctxShort')}
              </span>
            )}
            <span style={{ fontWeight: 700, minWidth: 56, textAlign: 'right' }}>{fmtCredits(r.credits)}</span>
            <span style={{ color: 'var(--text-muted, #888)', minWidth: 64, textAlign: 'right' }}>
              {relTime(r.ts)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function CreditUsagePage() {
  const [days, setDays] = useState<number>(() => {
    try {
      const v = parseInt(localStorage.getItem(STORAGE_KEY) || '', 10)
      return WINDOW_CHOICES.includes(v as (typeof WINDOW_CHOICES)[number]) ? v : DEFAULT_WINDOW_DAYS
    } catch {
      return DEFAULT_WINDOW_DAYS
    }
  })

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, String(days))
    } catch {
      /* ignore quota / private-mode failures */
    }
  }, [days])

  // Tick once a second so the "updated Ns ago" label counts up between polls.
  const [, setNowTick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setNowTick((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [])

  const summaryQ = useQuery({
    queryKey: ['credit-usage', 'summary', days],
    queryFn: () => creditUsageApi.summary(days),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  })

  const recentQ = useQuery({
    queryKey: ['credit-usage', 'recent'],
    queryFn: () => creditUsageApi.recent(RECENT_LIMIT),
    refetchInterval: POLL_MS,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  })

  const s = summaryQ.data
  const totals = s?.totals
  const windowTotal = totals?.windowCredits ?? 0

  const qc = useQueryClient()
  const refreshNow = () => {
    void qc.invalidateQueries({ queryKey: ['credit-usage'] })
  }

  return (
    <div style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 16, maxWidth: 1100, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <Coins size={22} />
        <h2 style={{ margin: 0, fontSize: 20 }}>{i18nT('creditUsage.title')}</h2>
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: 12, color: 'var(--text-muted, #888)' }} title={i18nT('creditUsage.pollNote')}>
          {relTime(new Date(summaryQ.dataUpdatedAt || Date.now()).toISOString()) || i18nT('creditUsage.updatedNow')}
        </span>
        {(summaryQ.isFetching || recentQ.isFetching) && (
          <RefreshCw size={14} className="animate-spin" style={{ opacity: 0.6 }} />
        )}
        <button
          type="button"
          onClick={refreshNow}
          disabled={summaryQ.isFetching || recentQ.isFetching}
          title={i18nT('creditUsage.refresh')}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 10px',
            borderRadius: 6,
            border: '1px solid var(--border, rgba(127,127,127,0.3))',
            background: 'transparent',
            color: 'inherit',
            cursor: summaryQ.isFetching || recentQ.isFetching ? 'default' : 'pointer',
            fontSize: 12,
            opacity: summaryQ.isFetching || recentQ.isFetching ? 0.6 : 1,
          }}
        >
          <RefreshCw size={13} />
          {i18nT('creditUsage.refresh')}
        </button>
        <div style={{ display: 'flex', gap: 4 }}>
          {WINDOW_CHOICES.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setDays(c)}
              style={{
                padding: '4px 10px',
                borderRadius: 6,
                border: '1px solid var(--border, rgba(127,127,127,0.3))',
                background: c === days ? 'var(--accent, #6366f1)' : 'transparent',
                color: c === days ? '#fff' : 'inherit',
                cursor: 'pointer',
                fontSize: 12,
              }}
            >
              {i18nT('creditUsage.daysChoice', { n: c })}
            </button>
          ))}
        </div>
      </div>

      {summaryQ.isError && (
        <Card>
          <div style={{ color: 'var(--danger, #e11d48)', fontSize: 13 }}>
            {i18nT('creditUsage.loadError')}{' '}
            {summaryQ.error instanceof Error ? summaryQ.error.message : ''}
          </div>
        </Card>
      )}

      {/* KPI cards */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
          gap: 12,
        }}
      >
        <StatCard label={i18nT('creditUsage.statToday')} value={fmtCredits(totals?.today)} accent />
        <StatCard label={i18nT('creditUsage.statWeek')} value={fmtCredits(totals?.thisWeek)} />
        <StatCard label={i18nT('creditUsage.statMonth')} value={fmtCredits(totals?.thisMonth)} />
        <StatCard
          label={i18nT('creditUsage.statWindow', { n: days })}
          value={fmtCredits(totals?.windowCredits)}
        />
        <StatCard label={i18nT('creditUsage.statAllTime')} value={fmtCredits(totals?.allTimeCredits)} />
        <StatCard
          label={i18nT('creditUsage.statTurns')}
          value={fmtInt(totals?.allTimeTurns)}
          title={i18nT('creditUsage.statTurnsTip')}
        />
      </div>

      {/* Trend */}
      <Card>
        <CardTitle>{i18nT('creditUsage.trendTitle')}</CardTitle>
        <TrendChart data={s?.trend ?? []} />
      </Card>

      {/* Breakdowns */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 12 }}>
        <Card>
          <CardTitle>
            <Cpu size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            {i18nT('creditUsage.byModel')}
          </CardTitle>
          <BreakdownBars rows={s?.byModel ?? []} total={windowTotal} />
        </Card>
        <Card>
          <CardTitle>
            <Layers size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            {i18nT('creditUsage.bySurface')}
          </CardTitle>
          <BreakdownBars rows={s?.bySurface ?? []} total={windowTotal} />
        </Card>
        <Card>
          <CardTitle>
            <Users size={15} style={{ verticalAlign: '-2px', marginRight: 6 }} />
            {i18nT('creditUsage.byAgent')}
          </CardTitle>
          <BreakdownBars rows={s?.byAgent ?? []} total={windowTotal} />
        </Card>
      </div>

      {/* Top sessions */}
      <Card>
        <CardTitle>
          {i18nT('creditUsage.topSessions')}
          {s?.sessionCount ? (
            <span style={{ color: 'var(--text-muted, #888)', fontWeight: 400, marginLeft: 8, fontSize: 13 }}>
              {i18nT('creditUsage.ofSessions', { n: s.sessionCount })}
            </span>
          ) : null}
        </CardTitle>
        <TopSessionsTable rows={s?.topSessions ?? []} />
      </Card>

      {/* Live recent feed */}
      <Card>
        <CardTitle>{i18nT('creditUsage.recentTitle')}</CardTitle>
        <RecentFeed rows={recentQ.data?.rows ?? []} />
      </Card>
    </div>
  )
}
