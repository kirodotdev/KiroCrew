/**
 * Services — the long-lived infrastructure that sessions consume.
 *
 * Shaped after Task Manager's Services tab: these are NOT sessions, they are the
 * processes and integrations that serve sessions. Gateway, MCP pool, embeddings,
 * Slack transport, and governance enforcement.
 *
 * Layout uses CSS multi-column with break-inside:avoid per section to pack tight
 * and eliminate the ~400px dead space the old card grid left.
 */
import { useQuery } from '@tanstack/react-query'
import { useAppSelector } from '../../store'
import { useUptime } from '../../hooks/useUptime'
import { api } from '../../api/client'
import { useProvider } from '../../providers'
import { Card, CardTitle } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import McpGatewayCard from '../McpGatewayCard'
import { fmtNumber, fmtPercent, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import type { SystemData } from '../../types'

/* ── Section data model ── */

interface Row {
  label: string
  value: string | React.ReactNode
}

interface Section {
  title: string
  rows: Row[]
}

/* ── Governance status (copied from SystemPage — that file is being rewritten) ── */

function GovernanceStatus({ value }: { value?: 'active' | 'degraded' | 'disabled' | 'unknown' }) {
  const map = {
    active: { label: i18nT('pages.servicesTab.status_active'), color: 'var(--ok)', tip: i18nT('pages.servicesTab.governance_tip_active') },
    degraded: { label: i18nT('pages.servicesTab.status_degraded'), color: 'var(--danger)', tip: i18nT('pages.servicesTab.governance_tip_degraded') },
    disabled: { label: i18nT('pages.servicesTab.status_disabled'), color: 'var(--muted)', tip: i18nT('pages.servicesTab.governance_tip_disabled') },
    unknown: { label: i18nT('pages.servicesTab.status_unknown'), color: 'var(--muted)', tip: i18nT('pages.servicesTab.governance_tip_unknown') },
  } as const
  const s = map[value ?? 'unknown'] ?? map.unknown
  return <span style={{ color: s.color }} className="inline-flex items-center gap-1">{s.label}<InfoTip text={s.tip} /></span>
}

/* ── Status dot (span, not SVG — CI blocks inline SVG) ── */

function StatusDot({ on }: { on: boolean }) {
  return (
    <span
      className="inline-block w-2.5 h-2.5 rounded-full shrink-0"
      style={{ backgroundColor: on ? 'var(--ok)' : 'var(--muted)' }}
      aria-hidden="true"
    />
  )
}

/* ── Main component ── */

export default function ServicesTab() {
  const { data } = useQuery<SystemData>({
    queryKey: ['system'],
    queryFn: () => api.system(),
    refetchInterval: 2000,
  })
  const status = useAppSelector(s => s.dashboard.status)
  const statusUptime = useUptime()
  const providerAdapter = useProvider()

  const d = data ?? null

  // The breakdown is ONE interpolated catalog string, not a template literal:
  // the separators and the word order between the counts are translatable copy,
  // and the provider label is spelled by the active provider adapter rather than
  // hardcoded, so a non-kiro backend does not read as "kiro".
  const mcpBreakdown = (() => {
    if (d?.mcp_total == null) return '—'
    const s = d.mcp_processes?.sandbox ?? 0
    const k = d.mcp_processes?.kiro_cli ?? 0
    const m = d.mcp_processes?.builder_mcp ?? 0
    const providerLabel =
      providerAdapter.labels.processCountLabel === 'kiro_cli'
        ? 'kiro'
        : providerAdapter.labels.processCountLabel
    const unique = s + k + m > d.mcp_total
    return i18nT(
      unique
        ? 'pages.servicesTab.mcp_process_breakdown_unique'
        : 'pages.servicesTab.mcp_process_breakdown',
      {
        total: fmtNumber(d.mcp_total),
        sandbox: fmtNumber(s),
        provider: fmtNumber(k),
        providerLabel,
        mcp: fmtNumber(m),
      },
    )
  })()

  // child_processes reads /proc/<pid>/task (threads), contradicting thread_count.
  // Excluded deliberately — thread_count is the accurate metric.
  const gatewaySections: Section[] = [
    {
      title: i18nT('pages.servicesTab.gateway_process'),
      rows: [
        { label: i18nT('pages.servicesTab.pid'), value: d?.pid != null ? String(d.pid) : '—' },
        { label: i18nT('pages.servicesTab.python'), value: d?.python ?? '—' },
        { label: i18nT('pages.servicesTab.uptime'), value: statusUptime },
        // No session count here. The Sessions plane owns that quantity: it counts
        // sessions holding a runtime, while status.sessions counts chat slots, so
        // the two legitimately disagree. Surfacing both is the "one quantity, two
        // numbers" contradiction this page was restructured to remove.
        { label: i18nT('pages.servicesTab.memory_rss'), value: d?.proc_mem_mb != null ? fmtUnit(d.proc_mem_mb, 'megabyte', { maximumFractionDigits: 0 }) : '—' },
        { label: i18nT('pages.servicesTab.threads'), value: d?.thread_count != null ? fmtNumber(d.thread_count) : '—' },
        { label: i18nT('pages.servicesTab.cpu'), value: d?.proc_cpu_pct != null ? fmtPercent(d.proc_cpu_pct / 100, { maximumFractionDigits: 1 }) : '—' },
        { label: i18nT('pages.servicesTab.mcp_processes'), value: mcpBreakdown },
      ],
    },
  ]

  // Embedder runs inside the gateway process; null pid/mem is expected, not an error.
  const embeddingSections: Section[] = [
    {
      title: i18nT('pages.servicesTab.embeddings'),
      rows: [
        {
          label: i18nT('pages.servicesTab.status'),
          value: d?.ollama_running
            ? (d.ollama_remote
              ? <span className="inline-flex items-center gap-1.5"><StatusDot on />{i18nT('pages.servicesTab.remote')}</span>
              : <span className="inline-flex items-center gap-1.5"><StatusDot on />{i18nT('pages.servicesTab.running')}</span>)
            : <span className="inline-flex items-center gap-1.5"><StatusDot on={false} />{i18nT('pages.servicesTab.stopped')}</span>,
        },
        ...(d?.ollama_running ? [
          { label: i18nT('pages.servicesTab.pid'), value: d?.ollama_pid != null ? String(d.ollama_pid) : '—' },
          { label: i18nT('pages.servicesTab.memory_rss'), value: d?.ollama_mem_mb != null ? fmtUnit(d.ollama_mem_mb, 'megabyte', { maximumFractionDigits: 0 }) : '—' },
        ] : []),
      ],
    },
  ]

  const integrationSections: Section[] = [
    {
      title: i18nT('pages.servicesTab.slack'),
      rows: [
        {
          label: i18nT('pages.servicesTab.status'),
          value: (
            <span style={{ color: status?.slack_connected ? 'var(--ok)' : 'var(--muted)' }}>
              {status?.slack_connected ? i18nT('pages.servicesTab.connected') : i18nT('pages.servicesTab.not_connected')}
            </span>
          ),
        },
      ],
    },
    {
      title: i18nT('pages.servicesTab.governance'),
      rows: [
        {
          label: i18nT('pages.servicesTab.status'),
          value: <GovernanceStatus value={status?.governance} />,
        },
      ],
    },
  ]

  return (
    <>
      {/* Gateway — cwd is the live checkout, prominent so the user can see which worktree serves */}
      <Card>
        <CardTitle>{i18nT('pages.servicesTab.gateway_process')}</CardTitle>
        {d?.cwd && (
          <div className="mb-3 px-2 py-1.5 rounded bg-bg-elevated border border-border">
            <span className="text-[11px] text-muted mr-2">{i18nT('pages.servicesTab.working_directory')}</span>
            <span className="text-[12.5px] font-mono text-text-strong break-all">{d.cwd}</span>
          </div>
        )}
        <div style={{ columns: 3 }} className="gap-6 max-[900px]:columns-2 max-[600px]:columns-1">
          {gatewaySections.map(sec => (
            <SectionBlock key={sec.title} section={sec} />
          ))}
          {embeddingSections.map(sec => (
            <SectionBlock key={sec.title} section={sec} />
          ))}
          {integrationSections.map(sec => (
            <SectionBlock key={sec.title} section={sec} />
          ))}
        </div>
      </Card>

      {/* MCP Gateway — imported unchanged, self-hides when disabled */}
      <McpGatewayCard />
    </>
  )
}

/* ── Section renderer ── */

function SectionBlock({ section }: { section: Section }) {
  return (
    <div className="mb-4" style={{ breakInside: 'avoid' }}>
      <h4 className="text-[11.5px] font-semibold text-muted uppercase tracking-wide mb-2">
        {section.title}
      </h4>
      {section.rows.map(row => (
        <div
          key={row.label}
          className="flex justify-between gap-3 py-1.5 border-b border-border text-[12.5px] last:border-b-0"
        >
          <span className="text-muted shrink-0">{row.label}</span>
          <span className="text-text-strong font-mono text-right break-all">{row.value}</span>
        </div>
      ))}
    </div>
  )
}
