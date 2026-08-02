import { type ReactNode, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, BarChart3, Brain, CheckCircle, Zap } from 'lucide-react'
import { useAppSelector } from '../store'
import { useUptime } from '../hooks/useUptime'
import { api } from '../api/client'
import { Card, CardTitle, StatCard } from '../components/ui'
import { TunnelStatus } from '../components/TunnelStatus'
import ErrorBoundary from '../components/ErrorBoundary'
import { getOverviewStatCards } from './overviewStatCards'
import { MemoryTab, UsageTab } from './overview'
import { useProvider } from '../providers'
import type { NormalizedUsage } from '../providers'

import { i18nT } from '../i18n/t'
/**
 * Settings > Overview — mission control.
 *
 * One scrollable dashboard, no nested tab bar: a health hero, the stat-tile
 * grid, and summary cards that drill into the two deep surfaces (memory
 * browser, usage report) via a URL-backed `?view=` param — the same
 * list-detail pattern as the Channels tab. KiroCrew/agent config viewers and
 * the memory graph live on the Developer page; configuration import/export is
 * on the Import tab.
 */

const DRILL_VIEWS = ['memory', 'usage'] as const
type DrillView = (typeof DRILL_VIEWS)[number]

function fmtNum(n: number | undefined | null): string {
  if (n == null) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

/** Back link + drill-in content, mirroring the Channels back affordance. */
function DrillIn({ title, onBack, children }: { title: string; onBack: () => void; children: ReactNode }) {
  return (
    <div>
      <button
        onClick={onBack}
        aria-label={i18nT('pages.overviewPage.back_to_overview')}
        className="flex items-center gap-1.5 text-[13px] font-medium text-accent bg-transparent border-none cursor-pointer px-0 py-1 mb-2 hover:underline"
      >
        <ArrowLeft size={14} />
        {i18nT('pages.overviewPage.overview')}
      </button>
      <div className="text-xl font-bold tracking-tight text-text-strong mb-3">{title}</div>
      {children}
    </div>
  )
}

/** Usage summary card — shares the query cache with the Usage drill-in. */
function UsageSummaryCard({ onOpen }: { onOpen: () => void }) {
  const provider = useProvider()
  const { data } = useQuery<NormalizedUsage>({
    queryKey: ['provider-usage', provider.id],
    queryFn: () => provider.fetchUsage(),
    enabled: provider.capabilities.usageBilling,
  })
  const b = data?.billing
  const today = data?.sessions.today
  return (
    <Card>
      <CardTitle>
        <BarChart3 className="lucide-inline" /> {i18nT('pages.overviewPage.usage')}
        <button onClick={onOpen} className="ml-auto inline-flex items-center gap-1 text-[12px] font-medium text-accent bg-transparent border-none cursor-pointer hover:underline">
          {i18nT('pages.overviewPage.view_details')} <ArrowRight size={12} />
        </button>
      </CardTitle>
      {!provider.capabilities.usageBilling ? (
        <div className="text-[13px] text-muted">{i18nT('pages.overviewPage.usage_tracking_is_not_available_for')} {provider.displayName}.</div>
      ) : !data ? (
        <div className="skeleton h-14 rounded" />
      ) : (
        <div className="flex flex-col gap-2">
          <div className="text-[13px] text-muted">
            {i18nT('pages.overviewPage.today')} {fmtNum(today?.sessions)} {i18nT('pages.overviewPage.sessions')} {fmtNum(today?.messages)} {i18nT('pages.overviewPage.messages')}
            {data.tokens?.total != null && <> · {fmtNum(data.tokens.total)} {i18nT('pages.overviewPage.tokens')}</>}
            {data.costUsd != null && <> · ${data.costUsd.toFixed(2)}</>}
          </div>
          {b?.plan && (
            <div className="flex items-center gap-2 text-[12px] text-muted">
              <span>{b.plan}</span>
              {typeof b.percentUsed === 'number' && (
                <>
                  <div className="flex-1 h-1.5 rounded-full bg-bg-elevated overflow-hidden max-w-[220px]">
                    <div className="h-full rounded-full bg-accent" style={{ width: `${Math.min(100, Math.max(0, b.percentUsed))}%` }} />
                  </div>
                  <span>{Math.round(b.percentUsed)}%</span>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </Card>
  )
}

/** Memory summary card — consolidation cadence + retention at a glance. */
function MemorySummaryCard({ onOpen }: { onOpen: () => void }) {
  const { data } = useQuery<{ history_idle_hours?: number; history_max_days?: number; migrated?: boolean }>({
    queryKey: ['memory-settings'],
    queryFn: () => api.memorySettings(),
  })
  return (
    <Card>
      <CardTitle>
        <Brain className="lucide-inline" /> {i18nT('pages.overviewPage.memory')}
        <button onClick={onOpen} className="ml-auto inline-flex items-center gap-1 text-[12px] font-medium text-accent bg-transparent border-none cursor-pointer hover:underline">
          {i18nT('pages.overviewPage.view_details')} <ArrowRight size={12} />
        </button>
      </CardTitle>
      {!data ? (
        <div className="skeleton h-14 rounded" />
      ) : (
        <div className="flex flex-col gap-1 text-[13px] text-muted">
          <span>
            {i18nT('pages.overviewPage.summarizes_chats_into_memory_after')} {data.history_idle_hours ?? 3}{i18nT('pages.overviewPage.h_idle')}
            {!data.migrated && <> {i18nT('pages.overviewPage.keeps')} {data.history_max_days ?? 90} {i18nT('pages.overviewPage.days_of_history')}</>}
            {data.migrated && <> {i18nT('pages.overviewPage.semantic_memory_active')}</>}
          </span>
          <span>{i18nT('pages.overviewPage.memory_graph_and_store_internals_live_on_the_dev')}</span>
        </div>
      )}
    </Card>
  )
}

type StatId = 'uptime' | 'sessions' | 'messages' | 'cronJobs' | 'subagents' | 'lessons'
/**
 * Catalog key per status tile. A flat `Record` of full literal keys, indexed
 * inline at the `i18nT()` call — the shape `scripts/check-i18n-keys.mjs` can
 * resolve. These six labels were raw English in the tile array, so they rendered
 * `UPTIME` / `SESSIONS` / `MESSAGES` above translated copy in every locale.
 */
export const STAT_LABEL_KEY: Record<StatId, string> = {
  uptime: 'pages.overviewPage.stat_uptime',
  sessions: 'pages.overviewPage.stat_sessions',
  messages: 'pages.overviewPage.stat_messages',
  cronJobs: 'pages.overviewPage.stat_cron_jobs',
  subagents: 'pages.overviewPage.stat_subagents',
  lessons: 'pages.overviewPage.stat_lessons',
}

export default function OverviewPage() {
  const status = useAppSelector(s => s.dashboard.status)
  const connected = useAppSelector(s => s.dashboard.connected)
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const uptime = useUptime()
  const [params, setParams] = useSearchParams()
  const [restarting, setRestarting] = useState(false)
  const [restartMsg, setRestartMsg] = useState<ReactNode>('')

  const rawView = params.get('view')
  const view: DrillView | null = DRILL_VIEWS.includes(rawView as DrillView) ? (rawView as DrillView) : null
  const setView = (v: DrillView | null) => setParams(prev => {
    const next = new URLSearchParams(prev)
    if (v) next.set('view', v)
    else next.delete('view')
    return next
  }, { replace: true })

  const restart = async () => {
    setRestarting(true)
    await api.restartSessions()
    setRestartMsg(<><CheckCircle className="lucide-inline" /> {i18nT('pages.overviewPage.sessions_restarted')}</>)
    setRestarting(false)
    setTimeout(() => setRestartMsg(''), 5000)
  }

  if (view === 'memory') {
    return <DrillIn title={i18nT('pages.overviewPage.memory')} onBack={() => setView(null)}><MemoryTab refreshTrigger={refreshTrigger} /></DrillIn>
  }
  if (view === 'usage') {
    return <DrillIn title={i18nT('pages.overviewPage.usage')} onBack={() => setView(null)}><UsageTab /></DrillIn>
  }

  return (
    <>
      {/* Health hero */}
      <div className="flex items-center justify-between gap-4 mb-5">
        <div>
          <div className="text-lg font-bold text-text-strong flex items-center gap-2">
            <span className={`w-2 h-2 rounded-full shrink-0 ${connected && status ? 'bg-ok' : 'bg-warn'}`} />
            {connected && status ? i18nT('pages.overviewPage.all_systems_running') : status ? i18nT('pages.overviewPage.reconnecting') : i18nT('pages.overviewPage.connecting')}
          </div>
          <div className="text-[12.5px] text-muted mt-0.5">
            {i18nT('pages.overviewPage.up')} {uptime}{status?.version ? <> {i18nT('pages.overviewPage.v')}{status.version}</> : null}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {restartMsg && <span className="text-ok text-[13px] animate-rise">{restartMsg}</span>}
          <button
            onClick={restart}
            disabled={restarting}
            title={i18nT('pages.overviewPage.apply_config_changes_by_restarting_all_sessions')}
            className={`group relative inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-[13px] font-semibold font-body cursor-pointer transition-all duration-300 overflow-hidden border-none ${
              restarting
                ? 'bg-accent/60 text-accent-fg/80 cursor-wait'
                : 'bg-gradient-to-r from-accent to-accent-hover text-accent-fg shadow-[0_2px_8px_var(--accent-glow)] hover:shadow-[0_4px_20px_var(--accent-glow)] hover:-translate-y-0.5 active:translate-y-0'
            }`}
          >
            {restarting && <span className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer" />}
            <span className={`transition-transform duration-300 ${restarting ? 'animate-spin' : 'group-hover:rotate-12'}`}><Zap className="lucide-inline" /></span>
            {restarting
              ? <span className="hidden sm:inline">{i18nT('pages.overviewPage.restarting')}</span>
              : <><span className="hidden lg:inline">{i18nT('pages.overviewPage.apply_restart')}</span><span className="hidden sm:inline lg:hidden">{i18nT('pages.overviewPage.restart')}</span></>
            }
          </button>
        </div>
      </div>

      {/* Stat tiles */}
      <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
        {([
          { id: 'uptime', value: uptime, accent: true },
          { id: 'sessions', value: status?.sessions },
          { id: 'messages', value: status?.messages },
          { id: 'cronJobs', value: status?.cron_jobs },
          { id: 'subagents', value: status?.subagents },
          { id: 'lessons', value: status?.lessons },
        ] as { id: StatId; value?: string | number | null; accent?: boolean }[]).map((s, i) => (
          // Keyed on the stable id, not the label: a language switch changes the
          // label, which would remount every tile and replay the stagger animation.
          <StatCard key={s.id} label={i18nT(STAT_LABEL_KEY[s.id])} value={s.value} accent={s.accent} delay={i * 60} />
        ))}
        <TunnelStatus delay={6 * 60} />
        {/* Extension slot: downstream-registered status cards (e.g. an edition
            credential-TTL card). Empty in the stock build. Each is isolated in
            its own ErrorBoundary so a throwing card disables only itself. */}
        {getOverviewStatCards().map((c, i) => {
          const CardComp = c.component
          return (
            <ErrorBoundary key={c.id} scope={`overview-stat-card:${c.id}`} fallback={null}>
              <CardComp delay={(7 + i) * 60} />
            </ErrorBoundary>
          )
        })}
      </div>

      {/* Deep-surface summary cards */}
      <div className="grid gap-3.5 grid-cols-2 max-[760px]:grid-cols-1">
        <UsageSummaryCard onOpen={() => setView('usage')} />
        <MemorySummaryCard onOpen={() => setView('memory')} />
      </div>
    </>
  )
}
