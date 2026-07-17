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
import type { SystemData } from '../types'

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
      {!embedded && <PageHeader title="System" subtitle="Live system metrics · refreshes every 2s" />}
      <div className={`${embedded ? '' : 'px-6 pb-8'} overflow-y-auto flex-1 min-h-0`}>
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          {[
            { label: 'CPU %', value: d?.cpu_pct != null ? d.cpu_pct + '%' : '—', accent: true },
            { label: 'Memory', value: d?.mem_used_gb != null ? d.mem_used_gb + ' / ' + d.mem_total_gb + ' GB' : '—' },
            { label: 'Network ↓', value: d?.net_rx_kbs != null ? fmtSpeed(d.net_rx_kbs) : '—' },
            { label: 'Network ↑', value: d?.net_tx_kbs != null ? fmtSpeed(d.net_tx_kbs) : '—' },
          ].map(s => (
            <StatCard key={s.label} label={s.label} value={s.value} accent={s.accent} />
          ))}
        </div>
        <div className="grid grid-cols-2 gap-4 mb-6 max-[900px]:grid-cols-1">
          <div className="flex flex-col">
            <div className="card-glow border border-border border-l-[3px] border-l-accent bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm transition-all">
              <h3 className="text-sm font-semibold text-accent mb-3.5 flex items-center gap-1.5"><PawPrint className="lucide-inline" /> KiroCrew Process <InfoTip text="Gateway process info: PID, uptime, Python version, and runtime stats (messages, tool calls, sessions)." /></h3>
              <Info k="PID" v={d?.pid} /><Info k="Python" v={d?.python} /><Info k="Uptime" v={statusUptime} /><Info k="Sessions" v={statusSessions} />
              <Info k="Process Memory (RSS)" v={d?.proc_mem_mb ? d.proc_mem_mb + ' MB' : '—'} />
              <Info k="Child Processes" v={d?.child_processes} /><Info k="Threads" v={d?.thread_count} />
              <Info k="MCP Processes" v={mcpLabel} />
              <Info k="CPU %" v={d?.proc_cpu_pct != null ? d.proc_cpu_pct + '%' : '—'} /><Info k="CWD" v={d?.cwd} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3.5 content-start max-[900px]:grid-cols-1">
            <SysCard title="Host"><Info k="Hostname" v={d?.hostname} /><Info k="OS" v={d?.os} /><Info k="Arch" v={d?.arch} /><Info k="CPUs" v={d?.cpu_count} /><Info k="Load (1/5/15m)" v={d?.load_1m != null ? d.load_1m + ' / ' + d.load_5m + ' / ' + d.load_15m : '—'} /></SysCard>
            <SysCard title="Memory"><Info k="Total" v={d?.mem_total_gb ? d.mem_total_gb + ' GB' : '—'} /><Info k="Used" v={d?.mem_used_gb ? d.mem_used_gb + ' GB' : '—'} /><Info k="Free" v={d?.mem_free_gb ? d.mem_free_gb + ' GB' : '—'} /></SysCard>
            <SysCard title="Network"><Info k="IP Address" v={d?.ip} /><Info k="Download" v={d?.net_rx_kbs != null ? fmtSpeed(d.net_rx_kbs) : '—'} /><Info k="Upload" v={d?.net_tx_kbs != null ? fmtSpeed(d.net_tx_kbs) : '—'} /></SysCard>
            <SysCard title="Storage"><Info k="Total" v={d?.disk_total_gb ? d.disk_total_gb + ' GB' : '—'} /><Info k="Free" v={d?.disk_free_gb ? d.disk_free_gb + ' GB' : '—'} /></SysCard>
            <SysCard title="Ollama"><Info k="Status" v={d?.ollama_running ? (d?.ollama_remote ? <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--ok)]" /> Remote</> : <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--ok)]" /> Running</>) : <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--muted)]" /> Stopped</>} />{d?.ollama_running && <><Info k="PID" v={d?.ollama_pid} /><Info k="Memory (RSS)" v={d?.ollama_mem_mb ? d.ollama_mem_mb + ' MB' : '—'} /></>}</SysCard>
            <SysCard title="Slack"><Info k="Status" v={<span style={{ color: status?.slack_connected ? 'var(--ok)' : 'var(--muted)' }}>{status?.slack_connected ? 'Connected' : 'Not connected'}</span>} /></SysCard>
            <SysCard title="Governance"><Info k="Status" v={<GovernanceStatus value={status?.governance} />} /></SysCard>
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

/** Governance enforcement health indicator (AVP-23427). Minimal colored text. */
function GovernanceStatus({ value }: { value?: 'active' | 'degraded' | 'disabled' | 'unknown' }) {
  const map = {
    active: { label: 'Active', color: 'var(--ok)', tip: 'Governance is enforcing an admission policy; no degradation detected.' },
    degraded: { label: 'Degraded', color: 'var(--danger)', tip: 'A governance check failed closed, an integrity mismatch was detected, or the admission policy is unverified (absent/unreadable). Investigate the SEL audit log.' },
    disabled: { label: 'Disabled', color: 'var(--muted)', tip: 'No enforcing admission policy is configured (permissive default). Plugins are admitted unless explicitly banned.' },
    unknown: { label: 'Unknown', color: 'var(--muted)', tip: 'Governance status not yet determined this session.' },
  } as const
  const s = map[value ?? 'unknown'] ?? map.unknown
  return <span style={{ color: s.color }} className="inline-flex items-center gap-1">{s.label}<InfoTip text={s.tip} /></span>
}
