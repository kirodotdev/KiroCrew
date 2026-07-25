import { useState, type ReactNode } from 'react'
import { CheckCircle, Zap } from 'lucide-react'
import { useAppSelector } from '../store'
import { useUptime } from '../hooks/useUptime'
import { api } from '../api/client'
import { StatCard } from '../components/ui'
import { TunnelStatus } from '../components/TunnelStatus'
import ErrorBoundary from '../components/ErrorBoundary'
import { getOverviewStatCards } from './overviewStatCards'
import { MemoryTab, AgentCfgTab, KiroCrewCfgTab, UsageTab, PortabilityTab } from './overview'

const tabs = ['memory', 'usage', 'kirocrewcfg', 'agentcfg', 'portability']

export default function OverviewPage() {
  const status = useAppSelector(s => s.dashboard.status)
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const uptime = useUptime()
  const [tab, setTab] = useState('memory')
  const [restarting, setRestarting] = useState(false); const [restartMsg, setRestartMsg] = useState<ReactNode>('')
  const restart = async () => { setRestarting(true); await api.restartSessions(); setRestartMsg(<><CheckCircle className="lucide-inline" /> Sessions restarted — config applied.</>); setRestarting(false); setTimeout(() => setRestartMsg(''), 5000) }
  const tabLabel = (t: string) => ({ agentcfg: 'Agent Config', kirocrewcfg: 'KiroCrew Config', usage: 'Usage', portability: 'Import/Export' }[t] || t.charAt(0).toUpperCase() + t.slice(1))

  const content = (
    <>
      <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
        {([
          { label: 'Uptime', value: uptime, accent: true },
          { label: 'Sessions', value: status?.sessions },
          { label: 'Messages', value: status?.messages },
          { label: 'Cron Jobs', value: status?.cron_jobs },
          { label: 'Subagents', value: status?.subagents },
          { label: 'Lessons', value: status?.lessons },
        ] as { label: string; value?: string | number | null; accent?: boolean }[]).map((s, i) => (
          <StatCard key={s.label} label={s.label} value={s.value} accent={s.accent} delay={i * 60} />
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
        <div className="flex gap-1 mb-4 border-b border-border">
          {tabs.map(t => (
            <button key={t} aria-current={tab === t ? 'page' : undefined} className={`px-4 py-2 border-none bg-transparent text-sm font-medium font-body cursor-pointer border-b-2 -mb-px transition-all ${tab === t ? 'text-accent border-b-accent' : 'text-muted border-b-transparent hover:text-text'}`} onClick={() => setTab(t)}>{tabLabel(t)}</button>
          ))}
          <div className="ml-auto flex items-center gap-2 pb-1">
            {restartMsg && <span className="text-ok text-[13px] animate-rise">{restartMsg}</span>}
            <button
              onClick={restart}
              disabled={restarting}
              className={`group relative inline-flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-[13px] font-semibold font-body cursor-pointer transition-all duration-300 overflow-hidden border-none ${
                restarting
                  ? 'bg-accent/60 text-accent-fg/80 cursor-wait'
                  : 'bg-gradient-to-r from-accent to-accent-hover text-accent-fg shadow-[0_2px_8px_var(--accent-glow)] hover:shadow-[0_4px_20px_var(--accent-glow)] hover:-translate-y-0.5 active:translate-y-0'
              }`}
            >
              {restarting && <span className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent animate-shimmer" />}
              <span className={`transition-transform duration-300 ${restarting ? 'animate-spin' : 'group-hover:rotate-12'}`}><Zap className="lucide-inline" /></span>
              {restarting
                ? <><span className="hidden sm:inline">Restarting…</span></>
                : <><span className="hidden lg:inline">Apply & Restart</span><span className="hidden sm:inline lg:hidden">Restart</span></>
              }
            </button>
          </div>
        </div>
        {tab === 'memory' && <MemoryTab refreshTrigger={refreshTrigger} />}
        {tab === 'usage' && <UsageTab />}
        {tab === 'kirocrewcfg' && <KiroCrewCfgTab />}
        {tab === 'agentcfg' && <AgentCfgTab />}
        {tab === 'portability' && <PortabilityTab />}
    </>
  )

  return content
}
