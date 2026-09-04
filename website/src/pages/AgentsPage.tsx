import { useState, useEffect, useLayoutEffect, useMemo, useRef, type ReactNode } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Star, Brain, Plug, Pin, Package, Lock, Hourglass, Bot, ChevronDown, LayoutTemplate, X } from 'lucide-react'
import { createPortal } from 'react-dom'
import { api } from '../api/client'
import { defaultAgentQuery } from '../api/defaultAgentQuery'
import type { SubagentInfo } from '../types'
import Clickable from '../components/Clickable'
import { SourceBadge, PageHeader, EmptyState, Btn, Input, SearchInput, Card, CardTitle, Badge } from '../components/ui'
import ModelDropdownList from '../components/ModelDropdownList'
import AgentSkillsEditor from '../components/AgentSkillsEditor'
import SimpleSelect from '../components/SimpleSelect'
import CrewAvatar from '../components/CrewAvatar'
import type { KiroCrewAgent } from '../components/AgentSelector'
import InfoTip from '../components/InfoTip'
import ListDetailBack from '../components/ListDetailBack'
import { useProvider } from '../providers'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useListDetailView } from '../hooks/useListDetailView'
import { fmtPercent } from '../i18n/format'
import { formatCost } from '../utils/formatCost'

import { i18nT } from '../i18n/t'
function fmtTokens(n: number): string {
  return n >= 1_000_000 ? (n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1) + 'M' : (n / 1_000).toFixed(0) + 'K'
}

function barColor(pct: number): string {
  if (pct >= 90) return 'bg-danger'
  if (pct >= 70) return 'bg-warn'
  return 'bg-accent'
}

function barGlow(pct: number): string {
  if (pct >= 90) return 'shadow-[0_0_8px_var(--danger)]'
  if (pct >= 70) return 'shadow-[0_0_8px_var(--warn)]'
  return 'shadow-[0_0_8px_var(--accent-glow)]'
}

/** Text colour for the part of a label that lands ON `barColor`'s fill.
 *
 * Same thresholds as `barColor`, resolved to the fill's paired foreground token.
 * `themeFillForeground.test.ts` holds every BUILT-IN theme's pair to a 4.5:1
 * floor, and to being no worse than `--text-strong` on the same fill. An
 * installed theme pack can still declare a low-contrast `--accent-fg` and these
 * labels inherit it — the same exposure every `text-<tier>-fg` consumer has, not
 * a guarantee this component can make on its own.
 */
function barFg(pct: number): string {
  if (pct >= 90) return 'text-danger-fg'
  if (pct >= 70) return 'text-warn-fg'
  return 'text-accent-fg'
}

/** The percentage / amount labels drawn on top of a meter bar.
 *
 * A single static colour cannot work here: the labels overlap both the
 * saturated fill and the empty track, and which one is under a given glyph
 * moves with the value. `text-text-strong` alone is near-black in every light
 * theme, so at 62% the percentage read as dark-on-purple.
 *
 * So the row is painted twice with complementary clips — off-fill colour on
 * the track side, `barFg` on the fill side — cut at exactly the fill's edge.
 * A glyph straddling that edge is split down the middle, which keeps contrast
 * at every value with no measuring, no resize observer, and no threshold to
 * guess. `className` carries the bar's own transition duration so the cut
 * travels WITH the fill instead of jumping ahead of it.
 */
function BarLabels({ pct, left, right, className = '' }: { pct: number; left: React.ReactNode; right: React.ReactNode; className?: string }) {
  // Match the fill's own `Math.max(pct, 1)` floor so the two edges coincide,
  // and clamp the top end: `inset()` past 100% (or NaN from a malformed
  // context_pct) drops the clip entirely, which would paint BOTH layers over
  // each other and lose the split.
  const edge = Number.isFinite(pct) ? Math.min(Math.max(pct, 1), 100) : 1
  const row = `absolute inset-0 flex items-center justify-between ${className}`
  const labels = <><span className="drop-shadow-sm">{left}</span><span className="drop-shadow-sm">{right}</span></>
  return (
    <>
      <div className={`${row} text-text-strong`} style={{ clipPath: `inset(0 0 0 ${edge}%)` }}>{labels}</div>
      <div className={`${row} ${barFg(pct)}`} style={{ clipPath: `inset(0 ${100 - edge}% 0 0)` }} aria-hidden="true">{labels}</div>
    </>
  )
}

/** Strip the provider prefix and version suffix a model id carries. */
function shortModel(model: string): string {
  return model === 'auto' ? 'auto' : model.replace(/^(us\.|anthropic\.|amazon\.)/, '').replace(/-v\d+:\d+$/, '')
}

interface CtxSession { key: string; name: string; model: string; agent?: string; context_pct: number; context_window_tokens?: number; prompts: number }

/**
 * A safe display string for an agent's `model`.
 *
 * The backend now coerces `model` to a string on both the installed-list and
 * detail endpoints, but `api.agentDetail` is otherwise a pass-through of a
 * user-editable JSON spec from a SHARED directory that other tools write into.
 * A non-string that slips through (e.g. an ACP-style `{"id": "..."}`) rendered
 * as a JSX child throws React error #31 and puts the whole Agent Templates tab
 * into the error boundary — one bad file hiding every other agent. Belt and
 * braces: anything that is not a string degrades to `auto` for that one row.
 */
function modelLabel(model: unknown): string {
  return typeof model === 'string' && model ? model : 'auto'
}


/** Shape of an installed-agent list item (also the `api.agentsInstalled` element). */
interface InstalledAgent {
  name: string
  description: string
  source: string
  model: string
  skills: string[]
  mcp_servers: string[]
  package?: string
  filename?: string
}

/**
 * Fields the detail pane reads off the selected agent. It's populated from
 * `api.agentDetail` (rich, dynamic backend payload) or, as a fallback, from an
 * installed-list item — so detail-only fields are optional and the installed
 * item's fields are folded in.
 */
interface AgentDetail extends Partial<InstalledAgent> {
  name: string
  prompt?: string
  tools?: string[]
  allowedTools?: string[]
  mcpServers?: Record<string, { args?: string[] }>
  toolsSettings?: { execute_bash?: { deniedCommands?: string[] } }
  /** `skill://` resources the catalog editor cannot express (wildcards, foreign paths). */
  unmanaged_skills?: string[]
}

/** Which pane of the inspector is showing. */
type DetailTab = 'overview' | 'skills' | 'tools' | 'prompt' | 'guardrails'


/** One section inside a detail tab: a small caps heading over its content. */
function Section({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="m-0 text-[12px] font-semibold uppercase tracking-wider text-muted">{title}</h3>
      {children}
    </section>
  )
}

/** Label → value pair used by the provenance block. */
function Kv({ label, value }: { label: string; value: ReactNode }) {
  return (
    <>
      <span className="text-[11px] uppercase tracking-wider text-muted pt-[2px]">{label}</span>
      <span className="text-[12.5px] font-mono text-text break-all">{value}</span>
    </>
  )
}

/** A count chip in "At a glance" — clicking it opens the tab that owns the detail. */
function GlanceChip({ label, count, tone, onClick }: { label: string; count: number; tone?: 'ok' | 'aim' | 'danger'; onClick: () => void }) {
  const toneCls = tone === 'ok'
    ? 'text-ok border-ok/35 bg-ok-subtle'
    : tone === 'aim'
      ? 'text-aim border-aim/35 bg-aim-subtle'
      : tone === 'danger'
        ? 'text-danger border-danger/35 bg-danger-subtle'
        : 'text-text border-border bg-bg-elevated'
  return (
    <Clickable
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-[3px] text-[12px] transition-colors hover:border-border-strong ${toneCls}`}
    >
      <span className="font-mono font-semibold">{count}</span>
      <span>{label}</span>
    </Clickable>
  )
}

/** Live context-window utilisation per active session. */
function ContextUsageCard({ ctx, installed }: { ctx: CtxSession[]; installed: InstalledAgent[] }) {
  const provider = useProvider()
  return (
    <Card>
      <CardTitle className="gap-1.5">{i18nT('pages.agentsPage.context_window_usage')} <InfoTip text={i18nT('pages.agentsPage.context_window_usage_tip', { label: provider.labels.sessionProcess })} /></CardTitle>
      {ctx.length === 0 ? <p className="text-muted italic text-sm">{i18nT('pages.agentsPage.no_active_sessions')}</p> : (
        <div className="space-y-4">
          {ctx.map(s => {
            // Prefer the real served window the backend reports (the one
            // the pct was actually computed against). Fall back to the
            // model-id heuristic only when the backend hasn't reported a
            // window yet (e.g. before the first usage_update). Using the
            // heuristic unconditionally can inflate the token text ~5x when
            // a "[1m]" model is actually served at 200k by the backend.
            const maxTokens = s.context_window_tokens && s.context_window_tokens > 0
              ? s.context_window_tokens
              : provider.getContextWindow(s.model)
            const usedTokens = Math.round(maxTokens * s.context_pct / 100)
            const pct = Math.min(s.context_pct, 100)
            const awaiting = s.context_pct === 0 && s.prompts === 0
            return (
              <div key={s.key}>
                <div className="flex items-center justify-between mb-1.5">
                  <div className="text-sm font-medium text-text">{s.name}</div>
                  <div className="text-[13px] text-muted font-mono">{s.agent && s.agent !== 'kirocrew' ? <span className={`mr-1.5 ${installed.find(a => a.name === s.agent)?.source === 'kirocrew' ? 'text-accent' : 'text-aim'}`}>{s.agent}</span> : null}{shortModel(s.model)}</div>
                </div>
                <div className="relative h-5 bg-bg-elevated rounded-full overflow-hidden border border-border">
                  {awaiting ? (
                    <div className="absolute inset-0 flex items-center justify-center text-[12px] font-mono text-muted">{i18nT('pages.agentsPage.awaiting_first_prompt')}</div>
                  ) : (<>
                    <div
                      className={`absolute inset-y-0 left-0 rounded-full transition-all duration-700 ease-out ${barColor(pct)} ${pct > 5 ? barGlow(pct) : ''}`}
                      style={{ width: `${Math.max(pct, 1)}%` }}
                    />
                    <BarLabels
                      pct={pct}
                      className="px-2.5 text-[12px] font-mono font-medium transition-[clip-path,color] duration-700 ease-out"
                      left={fmtPercent(pct / 100, { maximumFractionDigits: 1, minimumFractionDigits: 1 })}
                      right={<>{fmtTokens(usedTokens)} / {fmtTokens(maxTokens)}</>}
                    />
                  </>)}
                </div>
                <div className="flex justify-between mt-1 text-[12px] text-muted">
                  <span>{i18nT('pages.agentsPage.prompt', { count: s.prompts })}</span>
                  <span>{awaiting ? <><Hourglass className="lucide-inline" /> {i18nT('pages.agentsPage.idle')}</> : pct >= 90 ? <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--danger)]" /> {i18nT('pages.agentsPage.critical')}</> : pct >= 70 ? <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--warn)]" /> {i18nT('pages.agentsPage.high')}</> : <><span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--ok)]" /> {i18nT('pages.agentsPage.healthy')}</>}</span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}

interface SessionsUsage {
  plan?: string
  resets?: string
  credits_used?: number | null
  credits_plan?: number | null
  credits_overage?: number | null
  cost_usd?: number | null
  overage_rate?: number | string | null
  email?: string
  account?: string
  account_type?: string
}

/** Mask an account email down to a recognizable-but-not-readable form.
 *
 * The Agents page is a common screen-share surface, so the full address is not
 * rendered: the first local-part character plus the domain is enough to confirm
 * WHICH account is signed in, which is the only question this row answers.
 * Returns null for input that cannot be masked, so callers omit the row rather
 * than showing a placeholder that reads as a real value.
 */
export function maskAccountEmail(raw: string | undefined): string | null {
  const value = (raw || '').trim()
  if (!value) return null
  const at = value.lastIndexOf('@')
  // Nothing before the '@' means there is no local-part character to keep, so
  // there is no recognizable-but-masked form to show at all.
  if (at === 0) return null
  // No domain to anchor on (whoami reported a bare handle): mask all but the
  // first character rather than leaking the whole string.
  if (at < 0) return `${value[0]}•••`
  return `${value[0]}•••${value.slice(at)}`
}

/** Render guard for a provider-supplied label that may or may not be an address.
 *
 * Masks anything address-shaped and passes everything else through unchanged, so
 * a label's own semantics decide nothing about whether an address can reach the
 * DOM. Returns null for an empty value so callers omit the row or segment.
 */
export function addressSafeLabel(raw: string | undefined): string | null {
  const value = (raw || '').trim()
  if (!value) return null
  return value.includes('@') ? maskAccountEmail(value) : value
}

/** Localized label for the auth-type enum `whoami` reports.
 *
 * `account_type` arrives as a code identifier (`IamIdentityCenter`, `BuilderId`,
 * `Social`). The top-bar credit modal already maps these to localized prose, so
 * the same keys are reused here — otherwise one surface shows a camelCase
 * identifier while the other shows prose for the same account. An unrecognized
 * value falls through to the address guard, since a value outside the known set
 * is not covered by the field's contract.
 */
export function authTypeLabel(raw: string | undefined): string | null {
  switch (raw) {
    case 'IamIdentityCenter':
      return i18nT('app.iam_identity_center')
    case 'BuilderId':
      return i18nT('app.builder_id')
    case 'Social':
      return i18nT('app.social_login')
    default:
      return addressSafeLabel(raw)
  }
}

/** The Account row's display value, preferring the email over the profile name.
 *
 * `usage.account` is the org profile's display name straight from the provider
 * (`profileName` / `profileDisplayName`), i.e. an arbitrary server-supplied
 * label — and orgs do name profiles after a person's address. Routing it through
 * the same guard is what stops a profile name being a way around the masking the
 * email path applies.
 */
export function accountDisplayValue(usage: Pick<SessionsUsage, 'email' | 'account'>): string | null {
  return maskAccountEmail(usage.email) || addressSafeLabel(usage.account)
}

/** Plan credits and spend for the current billing period. */
function ProviderUsageCard({ usage }: { usage: SessionsUsage }) {
  const provider = useProvider()
  // The backend attaches identity only when it can tie the signed-in account to
  // the one these credits are billed to. The API path gates that merge on a
  // matching profile ARN; the text-scrape path establishes it instead by
  // resolving whoami adjacently to the scrape. Neither always succeeds, so an
  // absent account is a legitimate steady state, not an error to retry.
  const account = accountDisplayValue(usage)
  // `account_type` is a code identifier from whoami, mapped to localized prose so
  // this row reads the same as the credit modal. The mapper address-guards any
  // value outside the known enum.
  const authType = authTypeLabel(usage.account_type)
  return (
    <Card>
      <div className="flex items-center justify-between mb-4">
        <CardTitle className="mb-0 gap-1.5">{provider.displayName} {i18nT('pages.agentsPage.usage')} <InfoTip text={i18nT('pages.agentsPage.consumption_for_current_billing_period', { provider: provider.displayName })} /></CardTitle>
        <div className="flex items-center gap-2">
          {usage.plan && <span className="px-2 py-0.5 rounded-full text-[12px] font-bold font-mono bg-accent/15 text-accent border border-accent/30">{usage.plan}</span>}
          {usage.resets && <span className="text-[12px] text-muted">{i18nT('pages.agentsPage.resets')} {usage.resets}</span>}
        </div>
      </div>
      <dl className="mb-4 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px]">
        <dt className="text-muted">{i18nT('pages.agentsPage.harness')}</dt>
        <dd className="text-text-strong font-medium">
          {provider.displayName}
          {authType && <span className="text-muted font-normal"> · {authType}</span>}
        </dd>
        <dt className="text-muted">{i18nT('pages.agentsPage.account')}</dt>
        <dd
          className={account ? 'text-text-strong font-mono min-w-0 truncate' : 'text-muted'}
          title={account || undefined}
        >
          {account || (
            <span className="inline-flex items-center gap-1.5">
              {i18nT('pages.agentsPage.account_not_reported')}
              <InfoTip text={i18nT('pages.agentsPage.account_not_reported_help')} />
            </span>
          )}
        </dd>
      </dl>
      {usage.credits_used != null && usage.credits_plan != null && (() => {
        const creditsUsed = usage.credits_used as number
        const creditsPlan = usage.credits_plan as number
        const pctRaw = creditsPlan > 0 ? (creditsUsed / creditsPlan) * 100 : 0
        const pct = Math.min(pctRaw, 100)
        // credits_used is the true total; credits_overage the amount above
        // plan (fall back to used - plan when the source omits it).
        const overage = usage.credits_overage ?? Math.max(0, creditsUsed - creditsPlan)
        const hasOverage = overage > 0
        // barColor/barGlow already encode this exact ladder for the context bars.
        const color = barColor(pct)
        const glow = barGlow(pct)
        return (
          <div>
            <div className="text-[13px] text-muted mb-1.5">{i18nT('pages.agentsPage.plan_credits')}</div>
            <div className="relative h-6 bg-bg-elevated rounded-full overflow-hidden border border-border mb-3">
              <div className={`absolute inset-y-0 left-0 rounded-full transition-all duration-1000 ease-out ${color} ${pct > 5 ? glow : ''}`} style={{ width: `${Math.max(pct, 1)}%` }} />
              <BarLabels
                pct={pct}
                className="px-3 text-[13px] font-mono font-bold transition-[clip-path,color] duration-1000 ease-out"
                left={fmtPercent(pctRaw / 100, { maximumFractionDigits: 0 })}
                right={<>{creditsUsed.toFixed(0)} / {creditsPlan.toFixed(0)}</>}
              />
            </div>
            <div className="flex justify-between text-[13px]">
              <div>
                {hasOverage && <span className="text-muted">{i18nT('pages.agentsPage.overage_credits')} <span className="text-warn font-medium">{overage.toFixed(1)}</span></span>}
              </div>
              <div className="flex gap-3">
                <span className="text-muted">{i18nT('pages.agentsPage.est_cost')} <span className="text-text-strong font-medium">{formatCost(usage.cost_usd)}</span></span>
                {usage.overage_rate && <span className="text-muted">${usage.overage_rate}{i18nT('pages.agentsPage.req')}</span>}
              </div>
            </div>
          </div>
        )
      })()}
    </Card>
  )
}

/** Spawned subagent runs and their state. */
function SubagentsCard({ agents, onClear, onDelete }: { agents: SubagentInfo[]; onClear: () => void; onDelete: (id: string) => void }) {
  return (
    <Card>
      <CardTitle>{i18nT('pages.agentsPage.subagents')} {agents.some(a => a.done) && <Btn danger type="button" className="px-2 py-0.5" onClick={onClear}>{i18nT('pages.agentsPage.clear_completed')}</Btn>}</CardTitle>
      <table className="w-full border-collapse table-striped"><thead><tr>{[i18nT('pages.agentsPage.id'), i18nT('pages.agentsPage.task'), i18nT('pages.agentsPage.status'), ''].map(h => <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>)}</tr></thead>
        <tbody>{agents.length === 0 ? <tr><td colSpan={4}><EmptyState icon={<Bot className="lucide-inline" />} title={i18nT('pages.agentsPage.no_subagents')} subtitle={i18nT('pages.agentsPage.spawn_tasks_from_chat_or_cli')} /></td></tr> : agents.map(a => (
          <tr key={a.id} className="hover:bg-bg-hover transition-colors"><td className="px-2.5 py-2 border-b border-border text-sm"><code>{a.id}</code></td><td className="px-2.5 py-2 border-b border-border text-sm">{a.task}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm">{a.done ? (a.error ? <Badge variant="err">{i18nT('pages.agentsPage.failed')}</Badge> : <Badge variant="ok">{i18nT('pages.agentsPage.done')}</Badge>) : <Badge variant="warn">{i18nT('pages.agentsPage.running')}</Badge>}</td>
            <td className="px-2.5 py-2 border-b border-border text-sm text-right"><Btn danger type="button" className="px-1.5 py-0.5" aria-label={i18nT('pages.agentsPage.delete_subagent', { id: a.id })} onClick={() => onDelete(a.id)}><X className="lucide-inline" /></Btn></td></tr>
        ))}</tbody></table>
    </Card>
  )
}

export default function AgentsPage({ embedded }: { embedded?: boolean } = {}) {
  const { data: spawnData, refetch: refetchSpawn } = useQuery({
    queryKey: ['spawn-list'],
    queryFn: () => api.spawnList(),
  })
  const agents: SubagentInfo[] = spawnData?.agents || []

  const { data: ctx = [] } = useQuery<CtxSession[]>({
    queryKey: ['sessions-context'],
    queryFn: () => api.sessionsContext().then(d => d.sessions || []),
    refetchInterval: 15000,
  })

  const { data: usage = null } = useQuery({
    queryKey: ['sessions-usage'],
    queryFn: () => api.sessionsUsage().then(d => (d.usage && Number.isFinite(d.usage.credits_plan)) ? d.usage : null).catch(() => null),
  })

  const { data: installed = [], isPending: installedLoading, refetch: refetchInstalled } = useQuery({
    queryKey: ['agents-installed'],
    queryFn: async () => {
      const a = await api.agentsInstalled()
      if (!Array.isArray(a)) return []
      ;(a as InstalledAgent[]).sort((x, y) => {
        if (x.name === 'kirocrew') return -1; if (y.name === 'kirocrew') return 1
        if (x.name === 'kirocrew-lite') return -1; if (y.name === 'kirocrew-lite') return 1
        return x.name.localeCompare(y.name)
      })
      return a as InstalledAgent[]
    },
  })

  const { data: mcpTools = {} } = useQuery({
    queryKey: ['mcp-tools'],
    queryFn: async () => {
      const probed = await api.mcpProbeCache()
      if (!Array.isArray(probed)) return {}
      const map: Record<string, string[]> = {}
      for (const s of probed) if (s.name && s.tools?.length) map[s.name] = s.tools
      return map
    },
  })

  /** Crews, so a template can name who it affects. Read-only here — a crew's
   *  own bindings are edited on the Crews tab.
   *
   *  Deliberately the SAME key and the SAME cached shape as the Crews tab
   *  (`KiroCrewAgentsPage.tsx`): one shared QueryClient keeps one value per
   *  key, so an observer that stored the unwrapped array here would be handed
   *  to that page as `agentsData` (rendering an empty roster) and its object to
   *  this one (`crews.filter` on an object throws during render). Storing the
   *  response verbatim and selecting from it also dedupes the fetch. */
  const { data: crewsData, isError: crewsFailed, refetch: refetchCrews } = useQuery<{ agents?: KiroCrewAgent[]; default_agent?: string }>({
    queryKey: ['kirocrew-agents'],
    queryFn: () => api.kirocrewAgents(),
  })
  const crews = useMemo(() => crewsData?.agents ?? [], [crewsData])

  const { data: defaultAgentData, isError: defaultFailed, refetch: refetchDefault } = useQuery(defaultAgentQuery)
  const defaultAgent = defaultAgentData ?? ''

  const [selectedAgent, setSelectedAgent] = useState<AgentDetail | null>(null)
  // Narrow viewport shows one pane at a time; a desktop shows both.
  const { isMobile, showList, showDetail, openDetail, closeDetail } = useListDetailView()
  const [tab, setTab] = useState<DetailTab>('overview')
  const [filter, setFilter] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const modelOptions = useAvailableModels()
  const { open: modelDropOpen, setOpen: setModelDropOpen, filter: modelFilter, setFilter: setModelFilter, dropdownRef: modelDropRef, inputRef: modelInputRef, filtered: filteredModels } = useFilteredDropdown(modelOptions)
  // Roving-focus keyboard nav for the model dropdown (shared with StyledSelect/AgentSelector).
  const { onListKeyDown: onModelListKeyDown } = useListboxKeyboard({
    open: modelDropOpen,
    dropdownRef: modelDropRef,
    inputRef: modelInputRef,
    hasFilterInput: true,
    filteredCount: filteredModels.length,
    onEnterSingleMatch: () => {
      if (!selectedAgent) return
      const m = filteredModels[0].name === 'auto' ? '' : filteredModels[0].name
      patchModelMut.mutate({ name: selectedAgent.name, model: m })
      setModelDropOpen(false)
      modelBtnRef.current?.focus()
    },
    closeToTrigger: () => { setModelDropOpen(false); modelBtnRef.current?.focus() },
  })
  const modelBtnRef = useRef<HTMLButtonElement>(null)
  const patchModelMut = useMutation({
    mutationFn: ({ name, model }: { name: string; model: string }) => api.agentPatch(name, { model }),
    onSuccess: (_r, { model }) => { setSelectedAgent(prev => (prev ? { ...prev, model } : prev)); refetchInstalled() },
  })
  const deleteAgentMut = useMutation({
    mutationFn: (name: string) => api.agentDelete(name),
    // Deleting the agent you were looking at must also leave its detail pane.
    // On a phone the panes are exclusive, so clearing the selection alone left
    // `detailOpen` true with nothing selected: the placeholder branch renders
    // without the Back control (that lives in the `selectedAgent` branch) and
    // the roster — including its filter — is inside the hidden list pane, so
    // there was no in-page way back.
    onSuccess: (_r, name) => {
      if (selectedAgent?.name === name) { setSelectedAgent(null); closeDetail() }
      setDeleteError(null)
      refetchInstalled()
    },
    /*  The handler — not this page — is the authority on references: it checks
     *  the fallback and crew bindings under the same config lock the writers
     *  take, so it refuses (409) races this cached snapshot cannot see. Show its
     *  reason rather than failing silently, and refetch both reference queries:
     *  that re-derives `blockedBy`, so the control the user just used disappears
     *  on its own instead of inviting a second identical attempt. */
    onError: (err: unknown) => {
      setDeleteError(err instanceof Error ? err.message : String(err))
      setConfirmDelete(false)
      refetchDefault()
      refetchCrews()
    },
  })
  const spawnClearMut = useMutation({ mutationFn: () => api.spawnClear(), onSuccess: () => refetchSpawn() })
  const spawnDeleteMut = useMutation({ mutationFn: (id: string) => api.spawnDelete(id), onSuccess: () => refetchSpawn() })

  const setDefaultMut = useMutation({
    mutationFn: (next: string) => api.setDefaultAgent(next),
    onSuccess: () => refetchDefault(),
  })

  /** Monotonic id for the in-flight detail fetch. Without it the LAST response
   *  wins rather than the LAST CLICK: a slow fetch for B landing after C was
   *  selected would swap the pane to B while `confirmDelete` — cleared only by
   *  the name-change layout effect below — is still armed, so the confirm names and
   *  targets B for a commit, and a click there deletes B's config file. */
  const selectSeq = useRef(0)

  const select = async (a: InstalledAgent) => {
    const seq = ++selectSeq.current
    // Drill into the detail before awaiting: while narrow the detail pane
    // replaces the list, so waiting for the fetch would leave the tap with no
    // visible effect for as long as it takes.
    openDetail()
    // Synchronous, not effect-deferred: arming is destructive, so it must be
    // dropped the moment a different template is asked for.
    setConfirmDelete(false)
    // The refusal named the PREVIOUS template; keeping it would misattribute.
    setDeleteError(null)
    setTab('overview')
    try {
      const d = await api.agentDetail(a.name)
      if (seq !== selectSeq.current) return
      setSelectedAgent(d)
    } catch {
      if (seq !== selectSeq.current) return
      // List rows carry display NAMES; the editor round-trips catalog KEYS, so
      // drop them rather than offer unsavable chips.
      setSelectedAgent({ ...a, skills: undefined })
    }
  }

  // Auto-open detail for first installed agent
  const { data: initialAgentDetail } = useQuery({
    queryKey: ['agent-detail', installed[0]?.name],
    queryFn: () => api.agentDetail(installed[0]!.name),
    enabled: !selectedAgent && installed.length > 0,
  })
  // `selectSeq.current === 0` matters as much as `!selectedAgent`: once the user
  // has clicked a row, a slow first-template fetch resolving late must not take
  // the pane back (which could seat an armed confirm on the wrong template).
  useEffect(() => {
    if (initialAgentDetail && !selectedAgent && selectSeq.current === 0) setSelectedAgent(initialAgentDetail)
  }, [initialAgentDetail, selectedAgent])

  // Backstop for the selections `select()` does not make — the auto-open query
  // above, and a delete that clears the pane. A layout effect resets the pane
  // before its newly rendered tabs can be activated; `select()` disarms
  // synchronously before awaiting detail.
  const selectedName = selectedAgent?.name
  useLayoutEffect(() => { setTab('overview'); setConfirmDelete(false); setDeleteError(null) }, [selectedName])

  /** Arming the delete unmounts the button that was focused, which would drop
   *  focus to <body> and leave a keyboard user nowhere. Focus moves onto
   *  CANCEL, not the confirm: Enter fires a native button's click on keydown
   *  and auto-repeats, so parking focus on "Yes, delete it" would let one held
   *  Enter arm and then delete in a single gesture — defeating the two-step
   *  this replaced a native confirm() with. The bar is role="alert", so its
   *  copy is announced either way. */
  const cancelBtnRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { if (confirmDelete) cancelBtnRef.current?.focus() }, [confirmDelete])

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return installed
    return installed.filter(a => [a.name, a.description, a.package, modelLabel(a.model), ...(a.skills || []), ...(a.mcp_servers || [])]
      .filter(Boolean).join(' ').toLowerCase().includes(q))
  }, [installed, filter])

  /** Non-package templates first, then one group per package. */
  const groups = useMemo(() => {
    const loose = filtered.filter(a => a.source !== 'package')
    const byPackage = filtered.filter(a => a.source === 'package').reduce<Record<string, InstalledAgent[]>>((g, a) => {
      const key = a.package || a.name; (g[key] ||= []).push(a); return g
    }, {})
    return { loose, byPackage }
  }, [filtered])

  const listed = installed.find(a => a.name === selectedAgent?.name)
  const usedBy = useMemo(
    () => (selectedName ? crews.filter(c => c.kiro_agent === selectedName) : []),
    [crews, selectedName],
  )
  /** Deleting a template something still points at leaves a dangling reference,
   *  and the next session that resolves it fails to start — a crew bound to it
   *  boots from nothing, and the fallback name is handed to kiro-cli as an
   *  agent id. So the guard is not just "who owns this file" but "is anything
   *  still pointing at it", which this page can finally answer because it reads
   *  both the fallback setting and the crew roster.
   *
   *  Unresolved data counts as "pointing at it", not as "nothing points at it":
   *  an unresolved query leaves `usedBy` empty and `defaultAgent` '', which
   *  would read as clear. An in-flight `setDefaultMut` is the same hazard with a
   *  narrower window — the PUT can land before the refetch, so `defaultAgent`
   *  still names the OLD template while config already names this one. */
  /*  `isError` is as load-bearing as the undefined check. A REFETCH that fails
   *  keeps the last good data, so `crewsData`/`defaultAgentData` stay defined
   *  and the mutation settles — the guard would then compare against a name the
   *  server no longer has. Stale-and-known-stale is unknown, so it blocks. */
  const crewsLoaded = crewsData !== undefined && !crewsFailed
  const defaultLoaded = defaultAgentData !== undefined && !defaultFailed
  const blockedBy: 'owner' | 'default' | 'crews' | 'unloaded' | null =
    !listed ? 'owner'
      : (listed.source === 'kirocrew' || listed.source === 'package') ? 'owner'
        : (!defaultLoaded || !crewsLoaded || setDefaultMut.isPending) ? 'unloaded'
          : defaultAgent === listed.name ? 'default'
            : usedBy.length > 0 ? 'crews'
              : null
  const deletable = blockedBy === null
  const modelPinned = modelLabel(selectedAgent?.model) !== 'auto'

  const renderRow = (a: InstalledAgent) => (
    // The shared control, not a raw <button>: website/AUTOSDE.yaml's
    // `page-layout-pattern` requires Input/Btn/SendBtn/SearchInput over raw
    // elements, and Btn's twMerge lets the row's own display, spacing and
    // colour win while its focus ring, transition and press affordance are
    // inherited rather than re-declared here.
    <Btn
      key={a.name}
      type="button"
      role="option"
      aria-selected={selectedAgent?.name === a.name}
      /* Named explicitly rather than by its composed text: the metadata line
         below is decoration (a model id and two glyph counts), and announcing
         "kirocrew claude-opus-5 3 2" buries the one word that identifies the
         row. The default marker is folded in because the star carries it
         visually and nothing else in the row would say it. */
      aria-label={defaultAgent === a.name ? `${a.name} — ${i18nT('pages.agentsPage.default')}` : a.name}
      className={`flex w-full items-start gap-2 px-3 py-2 text-left text-text mb-1 ${selectedAgent?.name === a.name ? 'list-selected bg-accent-subtle border-accent/40' : 'bg-bg-elevated border-transparent hover:bg-bg-hover hover:border-border-strong'}`}
      onClick={() => { void select(a) }}
    >
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className="text-[13px] font-mono font-semibold text-text truncate" title={a.name}>{a.name}</span>
          {defaultAgent === a.name && <Star className="lucide-inline text-accent shrink-0" aria-hidden="true" />}
        </div>
        <div aria-hidden="true" className="flex gap-2 mt-0.5">
          <span className="text-[11px] text-muted font-mono truncate">{shortModel(modelLabel(a.model))}</span>
          {a.skills.length > 0 && <span className="text-[11px] text-muted shrink-0"><Brain className="lucide-inline" />{a.skills.length}</span>}
          {a.mcp_servers.length > 0 && <span className="text-[11px] text-muted shrink-0"><Plug className="lucide-inline" />{a.mcp_servers.length}</span>}
        </div>
      </div>
    </Btn>
  )

  const tabs: { key: DetailTab; label: string; count?: number }[] = [
    { key: 'overview', label: i18nT('pages.agentsPage.overview') },
    { key: 'skills', label: i18nT('pages.agentsPage.skills'), count: selectedAgent?.skills?.length },
    { key: 'tools', label: i18nT('pages.agentsPage.tools_and_mcp'), count: selectedAgent?.tools?.length },
    { key: 'prompt', label: i18nT('pages.agentsPage.system_prompt') },
    { key: 'guardrails', label: i18nT('pages.agentsPage.guardrails'), count: selectedAgent?.toolsSettings?.execute_bash?.deniedCommands?.length },
  ]

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.agentsPage.agent_templates')} subtitle={i18nT('pages.agentsPage.what_a_crew_starts_from_its_model_skills_tools_mc')} />}
      <div className={`${embedded ? '' : 'px-4 md:px-6 pb-8'} overflow-y-auto flex-1 min-h-0`}>
        {/* Which template a session boots from when nothing names one — CLI
            chat, a chat-channel thread, a warm-pool process. A dashboard chat
            goes through a crew and never reads this, so the bar says what the
            setting actually governs rather than "new sessions". The per-row
            star it replaces was both the state readout and the control, so the
            same glyph had to mean "this is the default" and "make this the
            default". */}
        {installed.length > 0 && (
          <div className="mb-3.5 flex flex-wrap items-center gap-2.5 rounded-lg border border-border bg-bg-accent px-3 py-2.5">
            <Star className="lucide-inline text-accent" aria-hidden="true" />
            <span className="text-[13px]">{i18nT('pages.agentsPage.when_nothing_names_a_template_use')}</span>
            <SimpleSelect
              options={installed.map(a => a.name)}
              value={defaultAgent}
              clearLabel={i18nT('pages.agentsPage.no_default_template')}
              /* A configured name that is no longer installed would otherwise
                 render as "No default template" — i.e. a stale config reading
                 as unset. Show the name it actually holds. */
              triggerFallback={defaultAgent && !installed.some(a => a.name === defaultAgent) ? defaultAgent : undefined}
              onChange={next => {
                // Repointing the fallback can make the armed template the one
                // config names. Disarm at the source rather than relying on the
                // guard to notice after the write settles.
                setConfirmDelete(false)
                setDefaultMut.mutate(next)
              }}
              aria-label={i18nT('pages.agentsPage.when_nothing_names_a_template_use')}
              style={{ width: 210 }}
            />
            <span className="w-full text-[12px] text-muted">{i18nT('pages.agentsPage.cli_chat_chat_channel_threads_and_warm_pool_sessio')}</span>
          </div>
        )}

        {installedLoading ? (
          <div className="card-glow border border-border bg-card rounded-lg mb-4 shadow-sm flex items-center justify-center py-10 gap-2 text-muted text-sm">
            <Hourglass className="lucide-inline animate-pulse" /> {i18nT('pages.agentsPage.loading_agents')}
          </div>
        ) : installed.length === 0 ? (
          <div className="card-glow border border-border bg-card rounded-lg mb-4 shadow-sm py-6">
            <EmptyState
              icon={<LayoutTemplate className="lucide-inline" aria-hidden="true" />}
              title={i18nT('pages.agentsPage.no_agent_templates_yet')}
              subtitle={i18nT('pages.agentsPage.templates_come_with_kirocrew_or_with_an_app_you_i')}
            />
          </div>
        ) : (
          /* Inspector: a scrollable roster on the left, the selected template's
             full spec on the right. Sized against the viewport rather than a
             fixed pixel height so a tall window shows more of a prompt or a
             denied-command list instead of scrolling it inside a short box.
             `svh` (chrome showing) not `vh` (chrome retracted), or on a phone —
             where this is the only visible pane — the bottom runs under the
             address bar. Identical on a desktop; `vh` stays as the fallback. */
          <div className="card-glow border border-border bg-card rounded-lg mb-4 animate-rise shadow-sm transition-all overflow-hidden flex h-[62vh] supports-[height:100svh]:h-[62svh] min-h-[420px] max-h-[760px]">
            {showList && <div className={`${isMobile ? 'w-full border-r-0' : 'w-[288px] border-r'} shrink-0 border-border flex flex-col bg-bg-accent`}>
              <div className="p-2.5 border-b border-border">
                <SearchInput
                  className="w-full"
                  placeholder={i18nT('pages.agentsPage.filter_templates')}
                  aria-label={i18nT('pages.agentsPage.filter_templates')}
                  value={filter}
                  onChange={e => setFilter(e.target.value)}
                />
              </div>
              <div className="flex-1 overflow-y-auto p-2">
                {filtered.length === 0 ? (
                  <p className="px-2 py-6 text-center text-[12.5px] text-muted">{i18nT('pages.agentsPage.no_templates_match_your_filter')}</p>
                ) : (
                  /* ARIA 1.2 admits only `option` and `group` as listbox
                     children, so each heading rides on its own group's label
                     instead of sitting between the options as a bare div. The
                     visual heading is then aria-hidden to avoid announcing the
                     same words twice. */
                  <div role="listbox" aria-label={i18nT('pages.agentsPage.installed_agents')}>
                    {groups.loose.length > 0 && (
                      /* Unlabelled on purpose: repeating the listbox's own
                         name here reads as "Installed Agents, listbox …
                         Installed Agents, group". */
                      <div role="group">
                        <div aria-hidden="true" className="px-2 pt-1 pb-1.5 text-[10.5px] uppercase tracking-[.09em] text-muted">{i18nT('pages.agentsPage.installed_agents')}</div>
                        {groups.loose.map(renderRow)}
                      </div>
                    )}
                    {Object.entries(groups.byPackage).map(([pkg, pkgAgents]) => {
                      const isLocal = pkgAgents[0]?.filename?.startsWith('local-')
                      return (
                        <div key={pkg} role="group" aria-label={pkg} className="mt-2">
                          <div aria-hidden="true" className="flex items-center gap-1.5 px-2 pt-1 pb-1.5 min-w-0">
                            <span className="text-[10.5px] uppercase tracking-[.09em] text-muted shrink-0">{i18nT('pages.agentsPage.from_package')}</span>
                            <span className="text-[11.5px] text-aim font-mono min-w-0 truncate" title={pkg}>{isLocal ? <Pin className="lucide-inline" /> : <Package className="lucide-inline" />} {pkg}</span>
                          </div>
                          {pkgAgents.map(renderRow)}
                        </div>
                      )
                    })}
                  </div>
                )}
              </div>
            </div>}

            {showDetail && <div className="flex-1 min-w-0 flex flex-col">
              {selectedAgent ? (<>
                <div className="px-4 pt-3.5 border-b border-border">
                  <div className="flex items-center gap-2 flex-wrap">
                    {isMobile && <ListDetailBack label={i18nT('pages.agentsPage.installed_agents')} onBack={closeDetail} />}
                    <span className="text-[15px] font-mono font-bold text-text-strong">{selectedAgent.name}</span>
                    {listed?.source && <SourceBadge source={listed.source} />}
                    {defaultAgent === selectedAgent.name && (
                      <span className="inline-flex items-center gap-1 rounded-full border border-accent/40 bg-accent-subtle px-2 py-[1.5px] text-[11px] text-accent">
                        <Star className="lucide-inline" aria-hidden="true" />{i18nT('pages.agentsPage.default')}
                      </span>
                    )}
                    {listed?.package && (
                      <span className="text-[11px] text-aim bg-aim/10 px-2 py-0.5 rounded-md border border-aim/30 font-mono">
                        {listed.filename?.startsWith('local-') ? <Pin className="lucide-inline" /> : <Package className="lucide-inline" />} {listed.package}
                      </span>
                    )}
                    <div className="flex-1" />
                    {/* A button that silently disappears teaches nothing, so the
                        two states the user can act on say what to do first. The
                        owner cases (built-in, package) stay silent: nothing the
                        user does here makes them deletable. */}
                    {blockedBy === 'default' && (
                      <span className="text-[11.5px] text-muted">{i18nT('pages.agentsPage.this_is_the_fallback_template_point_the_picker_a')}</span>
                    )}
                    {blockedBy === 'crews' && (
                      <span className="text-[11.5px] text-muted">
                        {i18nT('pages.agentsPage.used_by_crews_repoint_them_on_the_crews_tab_firs', { crews: usedBy.map(c => c.name).join(', ') })}
                      </span>
                    )}
                    {deletable && !confirmDelete && (
                      <Btn danger onClick={() => setConfirmDelete(true)} disabled={deleteAgentMut.isPending} data-testid="delete-template">
                        {i18nT('pages.agentsPage.delete_template')}
                      </Btn>
                    )}
                  </div>
                  {/* `typeof` guard, not a bare truthiness check: an object is
                      truthy, so a foreign spec's structured `description` would
                      pass `&&` and then throw React error #31 as a JSX child —
                      the same whole-tab crash `modelLabel` guards on `model`. */}
                  {typeof selectedAgent.description === 'string' && selectedAgent.description && <p className="mt-1.5 mb-0 text-[13px] text-muted leading-relaxed max-w-[70ch]">{selectedAgent.description}</p>}
                  {/* Two-step rather than a one-click destructive button — the
                      previous row control deleted the config file behind a
                      native confirm(), which is unthemed and easy to dismiss
                      without reading. */}
                  {deleteError && (
                    <div role="alert" className="mt-2.5 rounded-md border border-danger/35 bg-danger-subtle px-3 py-2.5">
                      <p className="m-0 text-[12px] leading-relaxed text-muted">{deleteError}</p>
                    </div>
                  )}
                  {deletable && confirmDelete && (
                    <div role="alert" className="mt-2.5 flex flex-wrap items-center gap-2 rounded-md border border-danger/35 bg-danger-subtle px-3 py-2.5">
                      <p className="m-0 flex-1 min-w-[220px] text-[12px] leading-relaxed text-muted">
                        {i18nT('pages.agentsPage.delete_the_template_named_confirm', { name: selectedAgent.name })}
                      </p>
                      <Btn ref={cancelBtnRef} onClick={() => setConfirmDelete(false)} data-testid="cancel-delete-template">{i18nT('pages.agentsPage.cancel')}</Btn>
                      <Btn danger onClick={() => deleteAgentMut.mutate(selectedAgent.name)} disabled={deleteAgentMut.isPending} data-testid="confirm-delete-template">
                        {i18nT('pages.agentsPage.yes_delete_it')}
                      </Btn>
                    </div>
                  )}
                  <div role="tablist" aria-label={i18nT('pages.agentsPage.agent_templates')} className="mt-2.5 flex items-center gap-1 overflow-x-auto">
                    {tabs.map(t => (
                      <Btn
                        key={t.key}
                        type="button"
                        role="tab"
                        aria-selected={tab === t.key}
                        onClick={() => setTab(t.key)}
                        className={`shrink-0 rounded-none rounded-t-md border-none border-b-2 px-3 py-2 text-[12.5px] transition-colors ${tab === t.key ? 'border-b-accent bg-transparent text-accent' : 'border-b-transparent bg-transparent text-muted hover:text-text'}`}
                      >
                        {t.label}
                        {t.count ? <span className="ml-1.5 font-mono text-[11px] text-muted">{t.count}</span> : null}
                      </Btn>
                    ))}
                  </div>
                </div>

                <div className="flex-1 min-h-0 overflow-y-auto p-4 flex flex-col gap-5">
                  {tab === 'overview' && (<>
                    <Section title={i18nT('pages.agentsPage.model')}>
                      <div className="relative w-fit">
                        <Btn ref={modelBtnRef} className="flex items-center gap-1 px-2 py-1 text-[12.5px] font-mono font-medium" onClick={() => setModelDropOpen(!modelDropOpen)}>
                          <span><Brain className="lucide-inline" /></span> {modelLabel(selectedAgent.model)} <span className="text-muted text-[10px]"><ChevronDown className="lucide-inline" /></span>
                        </Btn>
                        {modelDropOpen && modelBtnRef.current && createPortal(
                          // Presentational positioning wrapper: the interactive semantics live
                          // on the inner role="listbox" and its option buttons. This element only
                          // hosts the roving-focus keydown handler for the composite widget, so it
                          // has no ARIA role of its own (mirrors AgentSelector's dropdown).
                          // eslint-disable-next-line jsx-a11y/no-static-element-interactions
                          <div ref={modelDropRef} tabIndex={-1} onKeyDown={onModelListKeyDown} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg min-w-[260px] max-w-[340px] max-h-[320px] flex flex-col overflow-hidden animate-slide-up" style={(() => { const r = modelBtnRef.current!.getBoundingClientRect(); const dropH = 320; const top = r.bottom + 4 + dropH > window.innerHeight ? r.top - dropH - 4 : r.bottom + 4; const left = Math.max(8, Math.min(r.left, window.innerWidth - 348)); return { top, left } })()}>
                            <div className="p-2 border-b border-border">
                              <Input ref={modelInputRef} type="text" aria-label={i18nT('pages.agentsPage.filter_models')} placeholder={i18nT('pages.agentsPage.type_to_filter')} value={modelFilter} onChange={e => setModelFilter(e.target.value)} className="w-full px-2 py-1 text-[13px]" />
                            </div>
                            <div role="listbox" aria-label={i18nT('pages.agentsPage.model_list')} className="overflow-y-auto flex-1 min-h-0">
                              <ModelDropdownList models={filteredModels} activeModel={modelLabel(selectedAgent.model)} onSelect={name => { const val = name === 'auto' ? '' : name; patchModelMut.mutate({ name: selectedAgent.name, model: val }); setModelDropOpen(false) }} />
                            </div>
                          </div>,
                          document.body
                        )}
                      </div>
                      {/* Where the pin actually lands, in words. "auto" told a
                          first-run reader nothing about which model runs. */}
                      <p className="m-0 rounded-md border border-border bg-bg-accent px-3 py-2.5 text-[11.5px] leading-relaxed text-muted">
                        {modelPinned
                          ? i18nT('pages.agentsPage.pinned_here_a_crew_set_to_inherit_uses_this_model')
                          : i18nT('pages.agentsPage.no_model_pinned_here_a_crew_set_to_inherit_falls_')}
                      </p>
                    </Section>

                    <Section title={i18nT('pages.agentsPage.where_it_comes_from')}>
                      <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5">
                        <Kv label={i18nT('pages.agentsPage.source')} value={listed?.source || '—'} />
                        {listed?.package && <Kv label={i18nT('pages.agentsPage.package')} value={listed.package} />}
                        {listed?.filename && <Kv label={i18nT('pages.agentsPage.config_file')} value={listed.filename} />}
                      </div>
                    </Section>

                    <Section title={i18nT('pages.agentsPage.used_by')}>
                      {usedBy.length === 0 ? (
                        <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.agentsPage.no_crews_use_this_template_yet')}</p>
                      ) : (<>
                        <div className="flex flex-wrap gap-1.5">
                          {usedBy.map(c => (
                            <span key={c.name} className="inline-flex items-center gap-1.5 rounded-full border border-border-strong py-[2px] pl-[3px] pr-2.5 text-[11.5px] text-text">
                              <CrewAvatar seed={c.name} avatar={c.avatar} size={18} />{c.name}
                            </span>
                          ))}
                        </div>
                        <p className="m-0 text-[11.5px] leading-relaxed text-muted">{i18nT('pages.agentsPage.editing_this_template_changes_how_they_behave_in_')}</p>
                      </>)}
                    </Section>

                    <Section title={i18nT('pages.agentsPage.at_a_glance')}>
                      <div className="flex flex-wrap gap-1.5">
                        {selectedAgent.tools && <GlanceChip label={i18nT('pages.agentsPage.tools')} count={selectedAgent.tools.length} onClick={() => setTab('tools')} />}
                        {selectedAgent.allowedTools && <GlanceChip label={i18nT('pages.agentsPage.auto_approved')} count={selectedAgent.allowedTools.length} tone="ok" onClick={() => setTab('tools')} />}
                        {selectedAgent.mcpServers && <GlanceChip label={i18nT('pages.agentsPage.mcp_servers')} count={Object.keys(selectedAgent.mcpServers).length} tone="aim" onClick={() => setTab('tools')} />}
                        {selectedAgent.skills && <GlanceChip label={i18nT('pages.agentsPage.skills')} count={selectedAgent.skills.length} onClick={() => setTab('skills')} />}
                        {selectedAgent.toolsSettings?.execute_bash?.deniedCommands && (
                          <GlanceChip label={i18nT('pages.agentsPage.denied_commands')} count={selectedAgent.toolsSettings.execute_bash.deniedCommands.length} tone="danger" onClick={() => setTab('guardrails')} />
                        )}
                      </div>
                    </Section>
                  </>)}

                  {tab === 'skills' && (
                    selectedAgent.skills === undefined ? (
                      /* The agent-detail fetch failed, so the real mapping is
                       * UNKNOWN. An empty-but-enabled editor here is destructive:
                       * add/remove PATCH the complete desired key list and the
                       * backend fully replaces the managed skill:// set, so one
                       * "Add skill" click over unknown state would silently delete
                       * every real mapping from the agent's spec on disk. Show why
                       * it is unavailable instead of offering a write. */
                      <div className="text-[12px] text-warn">
                        {i18nT('pages.agentsPage.could_not_load_this_agent_s_configuration_skills')}
                      </div>
                    ) : (
                      <AgentSkillsEditor
                        key={selectedAgent.name}
                        agentName={selectedAgent.name}
                        skills={selectedAgent.skills}
                        unmanaged={selectedAgent.unmanaged_skills}
                        onChange={(agentName, next) => {
                          // Ignore a save that resolved after the selection moved on —
                          // otherwise agent A's skills render under agent B and the
                          // next edit writes them into B's spec.
                          setSelectedAgent(prev =>
                            prev && prev.name === agentName ? { ...prev, skills: next } : prev)
                          refetchInstalled()
                        }}
                      />
                    )
                  )}

                  {tab === 'tools' && (<>
                    {selectedAgent.tools && (
                      <Section title={i18nT('pages.agentsPage.tools')}>
                        <div className="flex flex-wrap gap-1.5">{selectedAgent.tools.map(t => <span key={t} className="px-2 py-1 rounded-full text-[12px] font-mono bg-bg-elevated border border-border text-text">{t}</span>)}</div>
                      </Section>
                    )}
                    {selectedAgent.allowedTools && (
                      <Section title={i18nT('pages.agentsPage.auto_approved')}>
                        <div className="flex flex-wrap gap-1.5">{selectedAgent.allowedTools.map(t => <span key={t} className="px-2 py-1 rounded-full text-[12px] font-mono bg-ok/10 border border-ok/30 text-ok">{t}</span>)}</div>
                      </Section>
                    )}
                    {selectedAgent.mcpServers && (
                      <Section title={i18nT('pages.agentsPage.mcp_servers')}>
                        <div className="flex flex-wrap gap-1.5">{Object.keys(selectedAgent.mcpServers).map((s: string) => {
                          const srv = (selectedAgent.mcpServers as Record<string, { args?: string[] }>)[s]
                          const args = srv?.args || []
                          const idx = args.indexOf('--include-tools')
                          const restricted = idx >= 0 && args[idx + 1] ? args[idx + 1].split(',') : null
                          const tools = restricted || mcpTools[s] || null
                          const label = restricted ? `${tools.length} restricted` : tools ? `${tools.length}` : null
                          return <span key={s} className="group relative px-2 py-1 rounded-full text-[12px] font-mono bg-aim-subtle border border-aim/30 text-aim cursor-help">{s}{label && <span className={`text-[11px] ml-0.5 ${restricted ? 'text-warn' : 'text-muted'}`}>({label})</span>}{tools && <span className="invisible group-hover:visible absolute left-0 top-full mt-1 z-50 bg-card border border-border rounded-lg shadow-lg p-3 min-w-[220px] max-w-[360px] max-h-[200px] overflow-y-auto"><span className="block text-[13px] text-muted font-medium mb-1.5">{s} {i18nT('pages.agentsPage.tools_2')}{restricted ? ' (restricted)' : ''}:</span>{tools.map(t => <span key={t} className="block text-[13px] text-text font-mono py-0.5">{t}</span>)}</span>}</span>
                        })}</div>
                      </Section>
                    )}
                    {!selectedAgent.tools && !selectedAgent.allowedTools && !selectedAgent.mcpServers && (
                      <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.agentsPage.not_set_for_this_template')}</p>
                    )}
                  </>)}

                  {tab === 'prompt' && (
                    selectedAgent.prompt ? (
                      <pre className="m-0 text-[12px] text-text font-mono bg-bg-elevated rounded-md p-3 border border-border overflow-auto whitespace-pre-wrap leading-relaxed">{typeof selectedAgent.prompt === 'string' && selectedAgent.prompt.startsWith('file://') ? selectedAgent.prompt : (selectedAgent.prompt || '').slice(0, 2000)}</pre>
                    ) : (
                      <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.agentsPage.not_set_for_this_template')}</p>
                    )
                  )}

                  {tab === 'guardrails' && (
                    selectedAgent.toolsSettings?.execute_bash?.deniedCommands ? (
                      <Section title={i18nT('pages.agentsPage.denied_commands')}>
                        <p className="m-0 flex items-center gap-1.5 text-[11.5px] text-muted">
                          <Lock className="lucide-inline text-danger" aria-hidden="true" />
                          {i18nT('pages.agentsPage.comes_with_the_template_read_only_here')}
                        </p>
                        <div className="rounded-md border border-border bg-bg-elevated p-2.5 space-y-0.5">
                          {selectedAgent.toolsSettings.execute_bash.deniedCommands.map((p: string, i: number) => (
                            <div key={i} className="text-[12px] font-mono text-danger/60">{p}</div>
                          ))}
                        </div>
                      </Section>
                    ) : (
                      <p className="m-0 text-[12.5px] text-muted">{i18nT('pages.agentsPage.not_set_for_this_template')}</p>
                    )
                  )}
                </div>
              </>) : (
                <div className="flex items-center justify-center h-full text-muted text-[13px]">{i18nT('pages.agentsPage.select_an_agent_to_view_details')}</div>
              )}
            </div>}
          </div>
        )}

        <ContextUsageCard ctx={ctx} installed={installed} />
        {usage && <ProviderUsageCard usage={usage} />}
        <SubagentsCard agents={agents} onClear={() => spawnClearMut.mutate()} onDelete={id => spawnDeleteMut.mutate(id)} />
      </div>
    </>
  )
}
