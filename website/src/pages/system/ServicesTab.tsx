/**
 * Services — the long-lived infrastructure that sessions consume.
 *
 * Shaped after Task Manager's Services tab: these are NOT sessions, they are the
 * processes and integrations that serve sessions. Gateway, MCP pool, embeddings,
 * the messaging channel transports, and governance enforcement.
 *
 * Layout uses CSS multi-column with break-inside:avoid per section to pack tight
 * and eliminate the ~400px dead space the old card grid left.
 */
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import { useAppSelector } from '../../store'
import { useUptime } from '../../hooks/useUptime'
import { api } from '../../api/client'
import { useProvider } from '../../providers'
import { Card, CardTitle } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import McpGatewayCard from '../McpGatewayCard'
import HostRuntimeCard from './HostRuntimeCard'
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

/* ── Channels ── */

/**
 * Catalog key per channel type, in the gateway's own roster order.
 *
 * A flat map of FULL literal keys, indexed inline at the `i18nT()` call below:
 * that is the one indirection `scripts/check-i18n-keys.mjs` resolves (it
 * validates the union of a map's values), so every label here is gated for
 * existence. A key assembled from the wire's channel type would be invisible to
 * that gate and would render the raw dotted path the day a channel is renamed.
 *
 * The render walks THIS map rather than the payload's own key order, so the
 * section keeps a stable order and a channel the gateway grows is a one-line
 * data change here plus its label.
 */
const CHANNEL_LABEL_KEY: Record<string, string> = {
  slack: 'pages.servicesTab.slack',
  wecom: 'pages.servicesTab.wecom',
  telegram: 'pages.servicesTab.telegram',
  discord: 'pages.servicesTab.discord',
  webex: 'pages.servicesTab.webex',
  teams: 'pages.servicesTab.teams',
  weixin: 'pages.servicesTab.weixin',
  imessage: 'pages.servicesTab.imessage',
  whatsapp: 'pages.servicesTab.whatsapp',
  feishu: 'pages.servicesTab.feishu',
}

const CHANNEL_TYPES = Object.keys(CHANNEL_LABEL_KEY)

/**
 * One channel's status cell: the connected/not-connected span this page has
 * always used for Slack, plus the gateway's connect reason when it has one.
 *
 * Surfacing the reason is the whole point of the payload — a channel that failed
 * `invalid_auth` at boot reads as an ordinary "Not connected" without it, which
 * sends the operator to look at their network instead of their token.
 */
function ChannelStatus({ connected, error }: { connected: boolean; error: string }) {
  return (
    <span className="inline-flex flex-col items-end gap-0.5">
      <span style={{ color: connected ? 'var(--ok)' : 'var(--muted)' }}>
        {connected ? i18nT('pages.servicesTab.connected') : i18nT('pages.servicesTab.not_connected')}
      </span>
      {/* `min-w-0` so the reason (up to 120 chars from the gateway) wraps inside
          the value column instead of pushing the row wider than its
          multi-column track. */}
      {error !== '' && (
        <span
          className="inline-flex items-baseline gap-1 min-w-0"
          style={{ color: 'var(--danger)' }}
        >
          <AlertTriangle
            className="lucide-inline"
            role="img"
            aria-label={i18nT('pages.servicesTab.connect_error')}
          />
          {error}
        </span>
      )}
    </span>
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

  // One row per channel, read off `status.channels` — the same
  // `<channel>_connected` / `<channel>_connect_error` flags each channel's own
  // settings badge reports, so this page cannot disagree with that one.
  //
  // WHICH channels appear: only those with something to report — connected, or
  // carrying a connect error. `{ connected: false, error: '' }` is exactly what an
  // UNCONFIGURED channel looks like, so rendering every key would put seven
  // meaningless "Not connected" rows on a Slack-only install. Settings > Channels
  // is the surface that knows `configured` (it asks each channel's own config
  // endpoint) and is where "did I set this up?" belongs; this page answers "is
  // what I set up running, and if not, why not?".
  //
  // An ENABLED channel that could not start is NOT filtered out, because it does
  // not have that shape: the gateway badges it with a reason naming the missing
  // credential (`_badge_unready_channels`), so it arrives carrying an `error` and
  // renders through the same branch a crashed channel does. That distinction lives
  // there rather than here on purpose: "enabled but not started" is a fact only
  // the backend can establish, and the operator needs the reason, not just a row.
  //
  // Slack is always rendered, and is the back-compatible read: an older gateway
  // sends no `channels` at all, so `slack_connected` remains the source for that
  // row and the section never collapses to nothing against an absent map.
  const channelRows: Row[] = CHANNEL_TYPES
    .filter(type => {
      const ch = status?.channels?.[type]
      return type === 'slack' || Boolean(ch?.connected) || Boolean(ch?.error)
    })
    .map(type => {
      const ch = status?.channels?.[type]
      return {
        label: i18nT(CHANNEL_LABEL_KEY[type]),
        value: (
          <ChannelStatus
            connected={Boolean(
              type === 'slack' ? ch?.connected ?? status?.slack_connected : ch?.connected,
            )}
            error={ch?.error ?? ''}
          />
        ),
      }
    })

  const integrationSections: Section[] = [
    {
      title: i18nT('pages.servicesTab.channels'),
      rows: channelRows,
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
        {/* `columns-3` as a CLASS, not `style={{ columns: 3 }}`. An inline style
            outranks any stylesheet rule, so the two responsive overrides beside
            it were dead: measured at a 390px viewport the container still
            reported `column-count: 3` and each card got 119px, which is what
            squeezed the value column to roughly 24px. As classes all three
            participate in the cascade, so 390px resolves to one column. */}
        <div className="columns-3 gap-6 max-[900px]:columns-2 max-[600px]:columns-1">
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

      {/* Host runtime — self-hides outside the Windows desktop shell */}
      <HostRuntimeCard />
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
          {/* `break-words`, not `break-all`. `break-all` breaks between letters
              whether or not anything would overflow, so it split values mid-digit:
              a 5,289 MB reading rendered as `5,` / `28` / `9 MB`, and an uptime of
              4h4m17s over three lines. A number broken across lines reads as
              corrupted data. `break-words` breaks only where a line cannot
              otherwise fit, which still contains a long value such as a path.
              `tabular-nums` keeps the column from shifting as values update. */}
          <span className="text-text-strong font-mono tabular-nums text-right break-words">{row.value}</span>
        </div>
      ))}
    </div>
  )
}
