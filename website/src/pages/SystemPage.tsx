import { type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PawPrint } from 'lucide-react'
import { useAppSelector } from '../store'
import { useUptime } from '../hooks/useUptime'
import { api } from '../api/client'
import { useProvider } from '../providers'
import { fmtSpeed } from '../api/helpers'
import { StatCard, PageHeader } from '../components/ui'
import InfoTip from '../components/InfoTip'
import McpGatewayCard from './McpGatewayCard'
import type { SystemData } from '../types'

import { i18nT } from '../i18n/t'
export default function SystemPage({ embedded }: { embedded?: boolean } = {}) {
  const providerAdapter = useProvider()
  const { data } = useQuery<SystemData>({
    queryKey: ['system'],
    queryFn: () => api.system(),
    refetchInterval: 2000,
  })
  const status = useAppSelector(s => s.dashboard.status)
  const statusUptime = useUptime()
  const statusSessions = status?.sessions || 0

  const d = data ?? null
  const mcpLabel = (() => {
    if (d?.mcp_total == null) return '—'
    const s = d.mcp_processes?.sandbox ?? 0, k = d.mcp_processes?.kiro_cli ?? 0, m = d.mcp_processes?.builder_mcp ?? 0
    const providerLabel = providerAdapter.labels.processCountLabel === 'kiro_cli' ? 'kiro' : providerAdapter.labels.processCountLabel
    return `${d.mcp_total}${s + k + m > d.mcp_total ? ' unique' : ''} (${s} sandbox · ${k} ${providerLabel} · ${m} mcp)`
  })()
  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.systemPage.system')} subtitle={i18nT('pages.systemPage.live_system_metrics_refreshes_every_2s')} />}
      <div className={`${embedded ? '' : 'px-6 pb-8'} overflow-y-auto flex-1 min-h-0`}>
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          {[
            { label: i18nT('pages.systemPage.cpu'), value: d?.cpu_pct != null ? d.cpu_pct + '%' : '—', accent: true },
            { label: i18nT('pages.systemPage.memory'), value: d?.mem_used_gb != null ? d.mem_used_gb + ' / ' + d.mem_total_gb + ' GB' : '—' },
            { label: i18nT('pages.systemPage.network_2'), value: d?.net_rx_kbs != null ? fmtSpeed(d.net_rx_kbs) : '—' },
            { label: i18nT('pages.systemPage.network_3'), value: d?.net_tx_kbs != null ? fmtSpeed(d.net_tx_kbs) : '—' },
          ].map(s => (
            <StatCard key={s.label} label={s.label} value={s.value} accent={s.accent} />
          ))}
        </div>
        <McpGatewayCard />
        <div className="grid grid-cols-2 gap-4 mb-6 max-[900px]:grid-cols-1">
          <div className="flex flex-col">
            <div className="card-glow border border-border border-l-[3px] border-l-accent bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm transition-all">
              <h3 className="text-sm font-semibold text-accent mb-3.5 flex items-center gap-1.5"><PawPrint className="lucide-inline" /> {i18nT('pages.systemPage.kirocrew_process')} <InfoTip text={i18nT('pages.systemPage.gateway_process_info_pid_uptime_python_version_a')} /></h3>
              <Info k={i18nT('pages.systemPage.pid')} v={d?.pid} /><Info k={i18nT('pages.systemPage.python')} v={d?.python} /><Info k={i18nT('pages.systemPage.uptime')} v={statusUptime} /><Info k={i18nT('pages.systemPage.sessions')} v={statusSessions} />
              <Info k={i18nT('pages.systemPage.process_memory_rss')} v={d?.proc_mem_mb ? d.proc_mem_mb + ' MB' : '—'} />
              <Info k={i18nT('pages.systemPage.child_processes')} v={d?.child_processes} /><Info k={i18nT('pages.systemPage.threads')} v={d?.thread_count} />
              <Info k={i18nT('pages.systemPage.mcp_processes')} v={mcpLabel} />
              <Info k={i18nT('pages.systemPage.cpu')} v={d?.proc_cpu_pct != null ? d.proc_cpu_pct + '%' : '—'} /><Info k={i18nT('pages.systemPage.cwd')} v={d?.cwd} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3.5 content-start max-[900px]:grid-cols-1">
            <SysCard title={i18nT('pages.systemPage.host')}><Info k={i18nT('pages.systemPage.hostname')} v={d?.hostname} /><Info k={i18nT('pages.systemPage.os')} v={d?.os} /><Info k={i18nT('pages.systemPage.arch')} v={d?.arch} /><Info k={i18nT('pages.systemPage.cpus')} v={d?.cpu_count} /><Info k={i18nT('pages.systemPage.load_1_5_15m')} v={d?.load_1m != null ? d.load_1m + ' / ' + d.load_5m + ' / ' + d.load_15m : '—'} /></SysCard>
            <SysCard title={i18nT('pages.systemPage.memory')}><Info k={i18nT('pages.systemPage.total')} v={d?.mem_total_gb ? d.mem_total_gb + ' GB' : '—'} /><Info k={i18nT('pages.systemPage.used')} v={d?.mem_used_gb ? d.mem_used_gb + ' GB' : '—'} /><Info k={i18nT('pages.systemPage.free')} v={d?.mem_free_gb ? d.mem_free_gb + ' GB' : '—'} /></SysCard>
            <SysCard title={i18nT('pages.systemPage.network')}><Info k={i18nT('pages.systemPage.ip_address')} v={d?.ip} /><Info k={i18nT('pages.systemPage.download')} v={d?.net_rx_kbs != null ? fmtSpeed(d.net_rx_kbs) : '—'} /><Info k={i18nT('pages.systemPage.upload')} v={d?.net_tx_kbs != null ? fmtSpeed(d.net_tx_kbs) : '—'} /></SysCard>
            <SysCard title={i18nT('pages.systemPage.storage')}><Info k={i18nT('pages.systemPage.total')} v={d?.disk_total_gb ? d.disk_total_gb + ' GB' : '—'} /><Info k={i18nT('pages.systemPage.free')} v={d?.disk_free_gb ? d.disk_free_gb + ' GB' : '—'} /></SysCard>
            <SysCard title={i18nT('pages.systemPage.ollama')}><Info k={i18nT('pages.systemPage.status')} v={d?.ollama_running ? (d?.ollama_remote ? <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--ok)]" /> {i18nT('pages.systemPage.remote')}</> : <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--ok)]" /> {i18nT('pages.systemPage.running')}</>) : <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--muted)]" /> {i18nT('pages.systemPage.stopped')}</>} />{d?.ollama_running && <><Info k={i18nT('pages.systemPage.pid')} v={d?.ollama_pid} /><Info k={i18nT('pages.systemPage.memory_rss')} v={d?.ollama_mem_mb ? d.ollama_mem_mb + ' MB' : '—'} /></>}</SysCard>
            <SysCard title={i18nT('pages.systemPage.slack')}><Info k={i18nT('pages.systemPage.status')} v={<span style={{ color: status?.slack_connected ? 'var(--ok)' : 'var(--muted)' }}>{status?.slack_connected ? i18nT('pages.systemPage.connected') : i18nT('pages.systemPage.not_connected')}</span>} /></SysCard>
            <SysCard title={i18nT('pages.systemPage.governance')}><Info k={i18nT('pages.systemPage.status')} v={<GovernanceStatus value={status?.governance} />} /></SysCard>
          </div>
        </div>
      </div>
    </>
  )
}

function SysCard({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="card-glow border border-border bg-card rounded-lg p-5 animate-rise shadow-sm transition-all"><h3 className="text-sm font-semibold text-text-strong mb-3.5">{title}</h3>{children}</div>
}

function Info({ k, v }: { k: string; v?: ReactNode }) {
  return <div className="flex justify-between gap-3 py-2 border-b border-border text-sm last:border-b-0"><span className="text-muted shrink-0">{k}</span><span className="text-text font-medium font-mono text-[13px] break-all text-right">{v ?? '—'}</span></div>
}

/** Governance enforcement health indicator. Minimal colored text. */
function GovernanceStatus({ value }: { value?: 'active' | 'degraded' | 'disabled' | 'unknown' }) {
  const map = {
    active: { label: i18nT('pages.systemPage.status_active'), color: 'var(--ok)', tip: i18nT('pages.systemPage.governance_is_enforcing_an_admission_policy_no_d') },
    degraded: { label: i18nT('pages.systemPage.status_degraded'), color: 'var(--danger)', tip: i18nT('pages.systemPage.a_governance_check_failed_closed_an_integrity_mi') },
    disabled: { label: i18nT('pages.systemPage.status_disabled'), color: 'var(--muted)', tip: i18nT('pages.systemPage.no_enforcing_admission_policy_is_configured_perm') },
    unknown: { label: i18nT('pages.systemPage.status_unknown'), color: 'var(--muted)', tip: i18nT('pages.systemPage.governance_status_not_yet_determined_this_sessio') },
  } as const
  const s = map[value ?? 'unknown'] ?? map.unknown
  return <span style={{ color: s.color }} className="inline-flex items-center gap-1">{s.label}<InfoTip text={s.tip} /></span>
}
