import { useState, useMemo, useRef, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Plug, AlertTriangle, Check, ChevronRight, Zap, X, Download, Braces } from 'lucide-react'
import { motion, AnimatePresence } from 'framer-motion'
import { Trans } from 'react-i18next'
import { Link } from 'react-router-dom'
import { api } from '../../api/client'
import { Card, Btn, Badge, SearchInput, ContentSkeleton } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { useProvider } from '../../providers'
import McpBrowserModal from '../../components/McpBrowserModal'
import McpCustomServerModal from '../../components/McpCustomServerModal'
import type { McpServer, McpApplyChange, McpScopePresence, McpGlobalScope } from '../../types'
import { useSortableTable } from '../../hooks/useSortableTable'
import { useScrollEdges } from '../../hooks/useScrollEdges'
import { fmtDateTime, fmtTime } from '../../i18n/format'

/** Same calendar day as now — the boundary where a bare time stops being honest. */
function isToday(epochSecs: number): boolean {
  return new Date(epochSecs * 1000).toDateString() === new Date().toDateString()
}
import SortableHeader from '../../components/SortableHeader'
import { connectionProviderForServer } from '../connections/registry'
import McpRowSignIn from './McpRowSignIn'
import { useConnectionsUiEnabled } from '../../hooks/useConnectionsUi'

import { i18nT } from '../../i18n/t'
async function fetchServers(): Promise<McpServer[]> {
  return await api.mcpServers()
}

// A scope key: the core scopes 'kirocrew' / 'kiroGlobal', or a provider scope
// id like 'ccGlobal' contributed at runtime via the extra_mcp_scopes() seam.
type ScopeKey = string

// Per-server pending overrides keyed by server name.
type PendingChange = {
  scopes?: Partial<McpScopePresence>
  uninstall?: boolean
}

const DEFAULT_PRESENCE: McpScopePresence = { kirocrew: true, kiroGlobal: false }

function effectivePresence(s: McpServer, pending: PendingChange | undefined): McpScopePresence {
  // Spread so provider scopes (e.g. ccGlobal) carried on the server's presence
  // survive; DEFAULT_PRESENCE guarantees the core scopes are always defined.
  const base = s.presence || DEFAULT_PRESENCE
  return { ...DEFAULT_PRESENCE, ...base, ...(pending?.scopes || {}) } as McpScopePresence
}

function hasPendingScopeChange(s: McpServer, pending: PendingChange | undefined): boolean {
  if (!pending?.scopes) return false
  const base = s.presence || DEFAULT_PRESENCE
  return Object.entries(pending.scopes).some(
    ([k, v]) => base[k as ScopeKey] !== v
  )
}

function ScopeBadge({
  label,
  scope,
  active,
  pendingChange,
  disabled,
  onClick,
}: {
  label: string
  scope: ScopeKey
  active: boolean
  pendingChange: boolean
  disabled: boolean
  onClick: () => void
}) {
  const title = disabled
    ? i18nT('pages.overview.mcpTab.pending_uninstall', { label })
    : pendingChange
      ? `${label}: ${active ? 'pending enable' : 'pending disable'} (click to revert)`
      : `${label}: ${active ? 'on' : 'off'} (click to ${active ? 'disable' : 'enable'})`
  const bg = disabled
    ? 'bg-bg-elevated text-muted'
    : active
      ? 'bg-ok/20 text-ok border border-ok/40'
      : 'bg-bg-elevated text-muted border border-border'
  const pendingRing = pendingChange
    ? 'ring-1 ring-[var(--warn)] ring-offset-1 ring-offset-bg border-dashed'
    : ''
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={title}
      aria-label={title}
      className={`px-1.5 py-0.5 rounded text-[11px] font-mono cursor-pointer transition-colors ${bg} ${pendingRing}`}
      data-scope={scope}
    >
      {label}
    </button>
  )
}

/**
 * Badge label for a probe status.
 *
 * `needs_auth` is not a failure. The status probe runs WITHOUT the OAuth token
 * kiro-cli holds (Kiro Crew keeps no credentials), so a remote OAuth server
 * answers it with 401 while the agent runtime calls the same server fine —
 * reported as "Error / HTTP 401" in #1853.
 *
 * Which of the three `needs_auth` labels applies turns on evidence the probe now
 * collects: the server's own challenge says it wants OAuth, and the runtime's
 * grant artifacts say whether anyone has completed it. See `mcpAuthState`.
 *
 * An unrecognised status stays "Unknown" — a newer gateway may report a state
 * this build predates, and inventing a label for it would be a guess.
 */
function mcpStatusLabel(status: string, auth: McpAuthState): string {
  switch (status) {
    case 'ok':
      return i18nT('pages.overview.mcpTab.online')
    case 'error':
      return i18nT('pages.overview.mcpTab.error')
    case 'outdated':
      return i18nT('pages.overview.mcpTab.outdated')
    case 'disabled':
      return i18nT('pages.overview.mcpTab.disabled')
    case 'needs_auth':
      if (auth === 'sign_in_required') return i18nT('pages.overview.mcpTab.sign_in_required')
      if (auth === 'signed_in') return i18nT('pages.overview.mcpTab.signed_in')
      return i18nT('pages.overview.mcpTab.not_verified')
    default:
      return i18nT('pages.overview.mcpTab.unknown')
  }
}

/** Which wording the probe's authorization evidence actually supports. */
type McpAuthState = 'sign_in_required' | 'signed_in' | 'unknown'

/**
 * The `needs_auth` row's authorization state, from evidence rather than inference.
 *
 * One tri-state rather than a pair of booleans, because the two observations and
 * the absence of an observation are three outcomes of one question, and pairing
 * booleans is how a caller ends up rendering "signed in" for a row that reported
 * nothing.
 *
 * Both branches test an EXPLICIT boolean. `authGrantPresent` is absent whenever
 * the probe could not answer — an older gateway, or a cache home that would not
 * read — and a falsy check would turn that silence into "no grant", telling the
 * owner of a working server to sign in again. `undefined` therefore falls through
 * to `unknown`, whose wording claims nothing either way.
 */
function mcpAuthState(s: McpServer): McpAuthState {
  if (s.status !== 'needs_auth' || !s.authChallenge) return 'unknown'
  if (s.authGrantPresent === true) return 'signed_in'
  if (s.authGrantPresent === false) return 'sign_in_required'
  return 'unknown'
}

/**
 * Only a hard failure is toned as an error; `needs_auth` rides the warn default.
 *
 * A held grant is the exception, and it is toned `muted` rather than `warn`: amber
 * is the panel's "you need to act" colour, and a scan by colour alone could not
 * tell a resolved row from the one still asking to be signed in. It is not `ok`
 * either — green would claim the server answers, which the probe cannot check
 * without the runtime's token. Muted is the honest third reading: nothing here
 * needs you.
 */
function mcpStatusVariant(status: string, auth: McpAuthState): 'ok' | 'err' | 'warn' | 'muted' {
  if (status === 'ok') return 'ok'
  if (status === 'error') return 'err'
  return auth === 'signed_in' ? 'muted' : 'warn'
}

/**
 * Hover text explaining an unverifiable status, or `undefined` for every status
 * whose label already says everything.
 *
 * A three-word badge cannot explain why the dashboard is unsure, and the badge is
 * exactly where someone meets that surprise — so the explanation belongs here,
 * one hover away. The `unknown` case reuses the Connections card's string rather
 * than paraphrasing it: this table renders inside the Connections page, and two
 * wordings of one limitation would drift and would cost every locale a second
 * translation.
 *
 * The sign-in case splits its guidance across the two surfaces by what the reader
 * needs BEFORE acting versus after. The row states the step, because a step nobody
 * hovers to find is the bug this panel exists to fix; the outcome — refresh, and
 * what the row becomes — waits here, because it is only useful once the sign-in is
 * done, and an always-visible cell in a dense table pays for every sentence.
 */
function mcpStatusHint(status: string, serverName: string, auth: McpAuthState): string | undefined {
  // "Online" is the gateway's OWN probe result: it started the server in the
  // gateway process, under the gateway's client identity. It says nothing about
  // whether any particular agent session mounted it, and reading it as if it did
  // is a documented divergence (a server can probe fine and still fail in a
  // session, and the reverse). The badge cannot carry that caveat in two words,
  // so it carries it here rather than leaving the reader to assume the stronger
  // claim.
  if (status === 'ok') return i18nT('pages.overview.mcpTab.online_help')
  if (status !== 'needs_auth') return undefined
  if (auth === 'sign_in_required') return i18nT('pages.overview.mcpTab.sign_in_required_next')
  if (auth === 'signed_in') return i18nT('pages.overview.mcpTab.signed_in_help', { provider: serverName })
  return i18nT('pages.connectionsPage.not_verified_help', { provider: serverName })
}

interface McpTabProps {
  onManagedProviderClick?: (slug: string) => void
}

export default function McpTab({ onManagedProviderClick }: McpTabProps = {}) {
  const provider = useProvider()
  const queryClient = useQueryClient()
  // The in-place sign-in reuses the Connections mint engine, which is launch-held
  // behind the `connections_ui` flag (the gallery it belongs to is not yet
  // released). When the flag is off, a managed row falls through to the SAME chat
  // guidance a non-registry row shows — chat stays the only authorize prompt while
  // the gallery is closed (see useConnectionsUi docstring).
  const connectionsUi = useConnectionsUiEnabled()
  const [mcpFilter, setMcpFilter] = useState('')
  // Multi-provider server browser (Add Server button) — discovery lives in
  // the modal so the installed config stays the page's primary content.
  const [browserOpen, setBrowserOpen] = useState(false)
  // Manual JSON management: add-custom modal + per-server spec editor.
  const [customOpen, setCustomOpen] = useState(false)
  const [editTarget, setEditTarget] = useState<string | null>(null)
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set())
  const [pending, setPending] = useState<Record<string, PendingChange>>({})
  // Per-server per-tool pending overrides.  Key = server name, value = map
  // from tool name to desired enabled state.  Staged via the same Apply
  // button as scope toggles so rapid clicks don't race.
  const [pendingTools, setPendingTools] = useState<Record<string, Record<string, boolean>>>({})
  const [applyMsg, setApplyMsg] = useState('')

  // Auto-dismiss timers for the success banners. Held in refs and cleared on
  // unmount so a pending setTimeout never fires a state update after the
  // component is gone — that throws "window is not defined" once the test
  // environment (jsdom) is torn down and fails the build.
  const applyMsgTimer = useRef<ReturnType<typeof setTimeout>>()
  useEffect(
    () => () => {
      clearTimeout(applyMsgTimer.current)
    },
    []
  )

  const { data: servers = [], isLoading, refetch } = useQuery<McpServer[]>({
    queryKey: ['mcp-servers'],
    queryFn: fetchServers,
  })

  // Provider-specific global scopes contributed via the extra_mcp_scopes()
  // seam (CPP). Public build returns [] → the Globals column shows only the
  // core "Kiro" badge; a companion returns e.g. [{id:'ccGlobal',label:'Claude'}]
  // and the column re-surfaces that scope's toggle without any core edit.
  const { data: extraScopes = [] } = useQuery<McpGlobalScope[]>({
    queryKey: ['mcp-global-scopes'],
    queryFn: async () => (await api.mcpGlobalScopes()).scopes || [],
    staleTime: Infinity,
  })

  const pendingCount = useMemo(() => {
    return servers.reduce((count, s) => {
      const p = pending[s.name]
      const toolOverrides = pendingTools[s.name]
      const hasTools = toolOverrides && Object.keys(toolOverrides).length > 0
      if (!p && !hasTools) return count
      if (p?.uninstall) return count + 1
      if ((p && hasPendingScopeChange(s, p)) || hasTools) return count + 1
      return count
    }, 0)
  }, [servers, pending, pendingTools])

  // Toggle a tool override.  If the new value matches the server's current
  // on-disk state (i.e. this is a revert), drop the override.  If all
  // overrides on a server are reverts, drop the server key.
  const toggleToolPending = (serverName: string, tool: string, enabled: boolean) => {
    const server = servers.find(s => s.name === serverName)
    if (!server) return
    const currentlyDisabled = (server.disabledTools || []).includes(tool)
    const currentlyEnabled = !currentlyDisabled
    setPendingTools(prev => {
      const overrides = { ...(prev[serverName] || {}) }
      if (enabled === currentlyEnabled) {
        // Matches on-disk — remove override
        delete overrides[tool]
      } else {
        overrides[tool] = enabled
      }
      if (Object.keys(overrides).length === 0) {
        const { [serverName]: _removed, ...rest } = prev
        return rest
      }
      return { ...prev, [serverName]: overrides }
    })
  }

  const toggleScope = (name: string, scope: ScopeKey, newValue: boolean, serverPresence: McpScopePresence) => {
    setPending(prev => {
      const current = prev[name] || {}
      // If the uninstall flag is set, scope toggles are disabled — ignore.
      if (current.uninstall) return prev
      const scopes = { ...(current.scopes || {}) }
      // If the new value matches the server's ORIGINAL presence, this is a revert.
      if (newValue === serverPresence[scope]) {
        delete scopes[scope]
      } else {
        scopes[scope] = newValue
      }
      // Clean up: if no scope overrides remain AND not uninstalling, drop the entry.
      if (Object.keys(scopes).length === 0) {
        const { [name]: _removed, ...rest } = prev
        return rest
      }
      return { ...prev, [name]: { ...current, scopes } }
    })
  }

  const stageUninstall = (name: string) => {
    setPending(prev => ({ ...prev, [name]: { uninstall: true } }))
  }

  const revertRow = (name: string) => {
    setPending(prev => {
      const { [name]: _removed, ...rest } = prev
      return rest
    })
  }

  const apply = useMutation({
    mutationFn: async () => {
      const changes: McpApplyChange[] = []
      // Union of server names that have either a scope change or a tool override.
      const affected = new Set<string>()
      for (const n of Object.keys(pending)) affected.add(n)
      for (const n of Object.keys(pendingTools)) affected.add(n)

      for (const s of servers) {
        if (!affected.has(s.name)) continue
        const p = pending[s.name]
        if (p?.uninstall) {
          changes.push({ name: s.name, uninstall: true })
          continue
        }
        const change: McpApplyChange = { name: s.name }
        // Always include effective presence so the backend never sees
        // undefined scope fields and defaults them (which would remove
        // the server from globals even if the user only edited tools).
        // effectivePresence(s, undefined) correctly returns the server's
        // current on-disk presence.
        const eff = effectivePresence(s, p)
        change.kirocrew = eff.kirocrew
        change.kiroGlobal = eff.kiroGlobal
        // Seam scopes: send each provider scope's effective presence so the
        // backend preserves/updates it. Omitting one means "preserve" backend
        // side, but we send it explicitly to reflect any pending toggle.
        for (const sc of extraScopes) {
          change[sc.id as `${string}Global`] = !!eff[sc.id]
        }
        const tools = pendingTools[s.name]
        if (tools && Object.keys(tools).length > 0) {
          change.toolOverrides = { ...tools }
        }
        changes.push(change)
      }
      if (changes.length === 0) return { ok: true, applied: 0 }
      const r = await api.mcpApply(changes)
      if (r.error) throw new Error(r.error)
      return r
    },
    onSuccess: (r) => {
      setApplyMsg(i18nT('pages.overview.mcpTab.applied_change', { count: r.applied ?? 0 }))
      setPending({})
      setPendingTools({})
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
      clearTimeout(applyMsgTimer.current)
      applyMsgTimer.current = setTimeout(() => setApplyMsg(''), 5000)
    },
  })

  const discard = () => {
    setPending({})
    setPendingTools({})
    setApplyMsg('')
  }

  const probe = useMutation({
    mutationFn: () => api.mcpProbe(),
    onSuccess: (data) => {
      queryClient.setQueryData<McpServer[]>(['mcp-servers'], data)
    },
    onError: () => {
      refetch()
    },
  })

  // Release is a REFETCH, not an optimistic edit: the badge has to disappear
  // because the server actually came back, and the same request rebuilt the
  // agent config. Painting the row clean locally would claim a remount the
  // backend may have failed to perform (it answers 500 in that case).
  const resetFailures = useMutation({
    mutationFn: (name: string) => api.mcpResetProbeFailures(name),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
    },
  })

  const filtered = useMemo(
    () => servers.filter(s => !mcpFilter || (s.name + (s.command || '') + (s.tools || []).join(' ')).toLowerCase().includes(mcpFilter.toLowerCase())),
    [servers, mcpFilter]
  )
  const mcpComparators = useMemo(() => ({
    name: (a: McpServer, b: McpServer) => a.name.localeCompare(b.name),
    status: (a: McpServer, b: McpServer) => (a.status || '').localeCompare(b.status || ''),
    tools: (a: McpServer, b: McpServer) => (a.tools?.length || 0) - (b.tools?.length || 0),
  }), [])
  const { sorted: sortedServers, sort: mcpSort, toggle: toggleMcpSort } = useSortableTable(filtered, 'mcp-overview', mcpComparators, { key: 'name', dir: 'asc' })
  // Measured overflow state for the servers table's scroller — gates the pinned
  // Actions column's seam (border + fade). Measured, not breakpoint-inferred:
  // the table overflows whenever its container is narrower than its content,
  // which a resizable nav rail can cause at any viewport size.
  const [attachMcpScroller, mcpTableEdges, , attachMcpTable] = useScrollEdges<HTMLDivElement>()

  return (<>
    <h4 className="text-sm font-semibold text-text-strong mt-4 mb-2 flex items-center gap-2">
      {i18nT('pages.overview.mcpTab.mcp_servers_count', { count: servers.length })}
      <InfoTip text={i18nT('pages.overview.mcpTab.servers_scope_tip', { provider: provider.displayName })} />
      <span className="ml-auto flex items-center gap-2">
        <Btn onClick={() => setCustomOpen(true)}><Braces size={14} /> {i18nT('pages.overview.mcpTab.add_custom')}</Btn>
        <Btn primary onClick={() => setBrowserOpen(true)}><Download size={14} /> {i18nT('pages.overview.mcpTab.add_server')}</Btn>
      </span>
    </h4>
    <Card>
      {apply.error && <div className="mb-3 text-[13px] text-danger">{(apply.error as Error).message}</div>}
      {applyMsg && <div className="mb-3 text-[13px] text-ok animate-rise"><Check className="lucide-inline" /> {applyMsg}</div>}

      {/* Pending-changes banner */}
      {pendingCount > 0 && (
        <div className="mb-3 p-3 rounded border border-[var(--warn)] bg-warn/20 flex items-center justify-between">
          <div className="text-[13px] text-[var(--warn)]">
            <AlertTriangle className="lucide-inline" /> {i18nT('pages.overview.mcpTab.pending_change', { count: pendingCount })}
            <span className="ml-2 text-muted">{i18nT('pages.overview.mcpTab.apply_commits_to_kirocrew_mcp_json_provider_glob')}</span>
          </div>
          <div className="flex gap-2">
            <Btn onClick={() => apply.mutate()} disabled={apply.isPending}>
              {apply.isPending ? <><Zap className="lucide-inline animate-pulse" /> {i18nT('pages.overview.mcpTab.applying')}</> : <><Zap className="lucide-inline" /> {i18nT('pages.overview.mcpTab.apply')}</>}
            </Btn>
            <Btn onClick={discard} disabled={apply.isPending}><X className="lucide-inline" /> {i18nT('pages.overview.mcpTab.discard')}</Btn>
          </div>
        </div>
      )}

      <div className="flex items-center gap-2 mb-3">
        <div className="relative max-w-[480px] flex-1">
          <SearchInput placeholder={i18nT('pages.overview.mcpTab.filter_servers_or_tools')} value={mcpFilter} onChange={e => setMcpFilter(e.target.value)} />
          {mcpFilter && <button className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text transition-colors cursor-pointer" onClick={() => setMcpFilter('')} aria-label={i18nT('pages.overview.mcpTab.clear_search')}>{"\u00d7"}</button>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          {/* `title` as well as `aria-label`: the sign-in guidance names this control
              by that string, and an icon-only button renders none of it — without a
              hover the reader cannot match the instruction to the button it means. */}
          <Btn onClick={() => probe.mutate()} disabled={probe.isPending} aria-label={i18nT('pages.overview.mcpTab.probe_mcp_servers')} title={i18nT('pages.overview.mcpTab.probe_mcp_servers')}><RefreshCw size={14} className={probe.isPending ? 'animate-spin' : ''} /></Btn>
        </div>
      </div>
      <div className="flex gap-2 flex-wrap mb-3">
        {/* Same tone rule as the row badge, from the same evidence: these chips carry
            status by colour alone, so a signed-in server wearing the amber "act now"
            tone here would undo the distinction the row badge just made. */}
        {filtered.map(s => <Badge key={s.name} variant={mcpStatusVariant(s.status, mcpAuthState(s))}><Plug className="lucide-inline" /> {s.name}</Badge>)}
      </div>
      {isLoading ? <ContentSkeleton rows={6} /> : (
        <div ref={attachMcpScroller} className="overflow-x-auto">
        {/* This table is AUTO layout (`w-full border-collapse`, no table-fixed),
            so column edges depend on content and a wrapper-anchored cue cannot
            know where the pinned column starts. The seam therefore lives INSIDE
            the pinned cells — but NOT as a cell border: under
            `border-collapse: collapse` a cell border belongs to the collapsed
            table grid and paints at the cell's LAYOUT slot, so it stays behind
            while the sticky cell travels. It is a 1px child div instead
            (`left-0 w-px bg-border`), which the sticky cell carries, painted
            alongside a `right-full` gradient child hanging just left of it —
            both gated on the measured overflow flag, so a table that fits
            renders neither. Same treatment as the Hooks and Schedule tables.
            The table itself is the observed content node: auto layout means the
            ROWS set scrollWidth, which the scroller's own box never reports. */}
        <table ref={attachMcpTable} className="w-full border-collapse table-striped"><thead><tr>
          <SortableHeader label={i18nT('pages.overview.mcpTab.name')} sortKey="name" sort={mcpSort} onToggle={toggleMcpSort} />
          <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.mcpTab.kirocrew')}</th>
          <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{i18nT('pages.overview.mcpTab.globals')}</th>
          <SortableHeader label={i18nT('pages.overview.mcpTab.status')} sortKey="status" sort={mcpSort} onToggle={toggleMcpSort} />
          <SortableHeader label={i18nT('pages.overview.mcpTab.tools')} sortKey="tools" sort={mcpSort} onToggle={toggleMcpSort} />
          {/* Actions is the LAST column, so in a container narrower than the
              table's content it starts past the scroll edge and every
              Edit/Uninstall costs a horizontal scroll. `sticky right-0` pins it
              to the scrollport's right edge while the other columns scroll
              under it — which is why the cell needs an OPAQUE `bg-card` (the
              Card's own surface): the default cell background is transparent
              and the scrolling columns would show through. */}
          <th className="sticky right-0 bg-card text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">
            {mcpTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
            {mcpTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent" />}
            {i18nT('pages.overview.mcpTab.actions')}
          </th>
        </tr></thead>
          <tbody>{servers.length === 0 ? <tr><td colSpan={6} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.overview.mcpTab.no_mcp_servers_configured')}</td></tr> : sortedServers.length === 0 ? <tr><td colSpan={6} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.overview.mcpTab.no_matching_servers')}</td></tr> : sortedServers.map((s, i) => {
            const p = pending[s.name]
            const pendingUninstall = p?.uninstall === true
            const eff = effectivePresence(s, p)
            const base = s.presence || DEFAULT_PRESENCE
            const hasToolOverrides = pendingTools[s.name] && Object.keys(pendingTools[s.name]).length > 0
            const managedProvider = connectionProviderForServer(s)
            const rowBorder = pendingUninstall
              ? 'border-l-2 border-[var(--danger)]'
              : (hasPendingScopeChange(s, p) || hasToolOverrides)
                ? 'border-l-2 border-[var(--warn)]'
                : ''
            return (
              <tr key={s.name} className={`group/mcprow hover:bg-bg-hover transition-colors align-top ${rowBorder}`} style={{ opacity: pendingUninstall ? 0.5 : 1 }}>
                <td className="px-2.5 py-2 border-b border-border text-sm min-w-[180px]">
                  <div className="flex items-center gap-1.5">
                    <code className={`font-semibold ${pendingUninstall ? 'line-through' : ''}`}>{s.name}</code>
                    {managedProvider && (onManagedProviderClick ? (
                      <button
                        type="button"
                        className="border-none bg-transparent p-0 cursor-pointer"
                        onClick={() => onManagedProviderClick(managedProvider.slug)}
                        aria-label={i18nT('pages.connectionsPage.open_managed_connection', { provider: managedProvider.name })}
                      >
                        <Badge variant="aim" className="text-[10px] px-1.5 py-0.5 font-body">
                          {i18nT('pages.connectionsPage.managed_by_connections')}
                        </Badge>
                      </button>
                    ) : (
                      <Badge variant="aim" className="text-[10px] px-1.5 py-0.5 font-body">
                        {i18nT('pages.connectionsPage.managed_by_connections')}
                      </Badge>
                    ))}
                  </div>
                  <span className="text-muted text-[12px] block truncate max-w-[240px]" title={s.command}>{s.command || s.url || '—'}</span>
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm whitespace-nowrap">
                  <ScopeBadge
                    label={i18nT('pages.overview.mcpTab.kirocrew')}
                    scope="kirocrew"
                    active={eff.kirocrew}
                    pendingChange={!pendingUninstall && !!p?.scopes && 'kirocrew' in p.scopes}
                    disabled={pendingUninstall}
                    onClick={() => toggleScope(s.name, 'kirocrew', !eff.kirocrew, base)}
                  />
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm whitespace-nowrap">
                  <div className="flex gap-1">
                    <ScopeBadge
                      label={i18nT('pages.overview.mcpTab.kiro')}
                      scope="kiroGlobal"
                      active={eff.kiroGlobal}
                      pendingChange={!pendingUninstall && !!p?.scopes && 'kiroGlobal' in p.scopes}
                      disabled={pendingUninstall}
                      onClick={() => toggleScope(s.name, 'kiroGlobal', !eff.kiroGlobal, base)}
                    />
                    {extraScopes.map(sc => (
                      <ScopeBadge
                        key={sc.id}
                        label={sc.label}
                        scope={sc.id}
                        active={!!eff[sc.id]}
                        pendingChange={!pendingUninstall && !!p?.scopes && sc.id in p.scopes}
                        disabled={pendingUninstall}
                        onClick={() => toggleScope(s.name, sc.id, !eff[sc.id], base)}
                      />
                    ))}
                  </div>
                </td>
                <td className="px-2.5 py-2 border-b border-border text-sm">
                  {/* A "declared" row's tool list is the package's own static
                      declaration — nothing spawned the server. The COLOR carries
                      that, not a tiny word next to a green badge: green asserts
                      "it is up", and a peripheral scan of a 12-row table reads
                      colour long before it reads 11px text. */}
                  {s.probeMode === 'declared' && s.status === 'ok' ? (
                    <Badge variant="warn" title={i18nT('pages.overview.mcpTab.tool_list_read_from_the_package_s_declaration_th')}>
                      {i18nT('pages.overview.mcpTab.declared')}
                    </Badge>
                  ) : (
                    <Badge variant={mcpStatusVariant(s.status, mcpAuthState(s))} title={mcpStatusHint(s.status, s.name, mcpAuthState(s))}>
                      {mcpStatusLabel(s.status, mcpAuthState(s))}
                    </Badge>
                  )}
                  {s.probeFailing && (
                    /* A SECOND badge rather than a replacement status: the probe
                       status is still the true reading ("error"), and the error
                       detail below it is keyed on that. This says what was DONE
                       about it, which is a different fact.

                       The release control sits HERE, next to the badge, not in
                       the row's action cell. Two reasons, and the design one is
                       the stronger: a badge that explains a state and the button
                       that undoes that state belong adjacent. The other is the
                       `max-two-buttons-per-row` cap -- Edit JSON and Uninstall
                       already fill the action cell, and a third control there
                       would need an overflow menu this table does not have. */
                    <div className="mt-0.5 flex items-center gap-1.5">
                      <Badge variant="err" title={i18nT('pages.overview.mcpTab.probe_failing_help', { failures: s.probeFailures ?? 0 })}>
                        {i18nT('pages.overview.mcpTab.probe_failing')}
                      </Badge>
                      <button
                        className="whitespace-nowrap text-[11px] text-accent hover:text-accent-hover cursor-pointer transition-colors disabled:cursor-not-allowed disabled:text-muted"
                        onClick={() => resetFailures.mutate(s.name)}
                        disabled={resetFailures.isPending}
                        title={i18nT('pages.overview.mcpTab.reset_failures_help')}
                      >
                        {i18nT('pages.overview.mcpTab.reset_failures')}
                      </button>
                    </div>
                  )}
                  {!!s.probedAt && (                    /* nowrap: the column is narrow enough that "Last probed: 7:47 PM"
                       wraps to three lines and triples every row's height.
                       The title carries the FULL date+time, so the hover earns
                       its place instead of restating the visible label. */
                    <div
                      className="text-[11px] text-muted mt-0.5 whitespace-nowrap"
                      title={`${i18nT('pages.overview.mcpTab.last_probed')}: ${fmtDateTime(s.probedAt * 1000)}`}
                    >
                      {i18nT('pages.overview.mcpTab.last_probed')}: {isToday(s.probedAt) ? fmtTime(s.probedAt * 1000) : fmtDateTime(s.probedAt * 1000)}
                    </div>
                  )}
                </td>
                <td className="px-2.5 py-2 border-b border-border text-[13px] w-full">
                  {s.status === 'error' && s.error ? (
                    <span className="text-danger text-[12px]">
                      <AlertTriangle className="lucide-inline" /> {s.error}
                      {/* A configured Authorization header that the server rejected reads as
                          an opaque HTTP code. When the same response advertised OAuth, the
                          actionable fact is that no static token can satisfy this server. */}
                      {!!s.authChallenge && !!s.headers && (
                        /* Not `text-muted`: this line is the only actionable thing in the
                           cell, and muting it under a prominent red status code emphasised
                           the raw failure over the remedy. */
                        <span className="block text-warn mt-0.5">{i18nT('pages.overview.mcpTab.oauth_not_a_static_token')}</span>
                      )}
                    </span>
                  ) : mcpAuthState(s) === 'sign_in_required' ? (
                    /* Two paths, split on whether the row resolves to a curated
                       Connections provider (`managedProvider`, computed at row top)
                       AND the Connections UI is unlocked (`connectionsUi`).

                       RESOLVABLE + FLAG ON: the panel now CAN start the sign-in in
                       place — it reuses the SAME headless mint engine the Connections
                       cards drive (mint → poll for the approval URL → authorize →
                       paste-back relay). Minting is fenced to registry providers
                       only; arbitrary URLs are never minted (parked maintainer
                       decision #4286).

                       EITHER FALSE (non-registry row, OR the gallery still held
                       behind `connections_ui`): unchanged chat guidance. Nothing the
                       dashboard can call starts a sign-in for a user-added/self-hosted
                       server, and while the flag is closed the mint engine is not a
                       released surface either — so the cell stays a sentence that
                       routes the user to chat (a link, because navigating IS something
                       the panel can do) and names the probe step that repaints the row. */
                    managedProvider && connectionsUi ? (
                      <McpRowSignIn slug={managedProvider.slug} serverName={s.name} />
                    ) : (
                      <span className="text-warn text-[12px]">
                        <Trans
                          i18nKey="pages.overview.mcpTab.sign_in_required_help"
                          components={{ chatLink: <Link to="/chat" className="underline hover:text-accent transition-colors" /> }}
                        />
                      </span>
                    )
                  ) : s.tools?.length ? (<div>
                    <button className="flex items-center gap-1 text-[12px] text-accent hover:text-accent-hover cursor-pointer transition-colors mb-1" onClick={() => setExpandedTools(prev => { const next = new Set(prev); if (next.has(s.name)) next.delete(s.name); else next.add(s.name); return next })}>{s.tools.length} {i18nT('pages.overview.mcpTab.tools_2')}{!expandedTools.has(s.name) && (s.disabledTools?.length || 0) > 0 && <span className="text-muted ml-1">{i18nT('pages.overview.mcpTab.off_count', { count: s.disabledTools!.length })}</span>}<ChevronRight size={14} className={`transition-transform duration-200 ${expandedTools.has(s.name) ? 'rotate-90' : ''}`} /></button>
                    <AnimatePresence>{expandedTools.has(s.name) && <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.2 }} className="overflow-hidden"><div className="space-y-0.5">{s.tools.map(t => {
                      const currentlyDisabled = (s.disabledTools || []).includes(t)
                      const toolOverride = pendingTools[s.name]?.[t]
                      const effectivelyEnabled = toolOverride !== undefined ? toolOverride : !currentlyDisabled
                      const hasPending = toolOverride !== undefined
                      return <button
                        type="button"
                        key={t}
                        className={`flex items-center gap-1.5 text-[12px] font-mono cursor-pointer transition-colors bg-transparent border-none p-0 text-left ${effectivelyEnabled ? 'text-text hover:text-accent' : 'text-muted line-through opacity-60 hover:opacity-100'} ${hasPending ? 'ring-1 ring-[var(--warn)] rounded px-1' : ''}`}
                        disabled={pendingUninstall || s.enabled === false}
                        onClick={() => toggleToolPending(s.name, t, !effectivelyEnabled)}
                        title={hasPending
                          ? (effectivelyEnabled ? i18nT('pages.overview.mcpTab.pending_enable_click_to_revert') : i18nT('pages.overview.mcpTab.pending_disable_click_to_revert'))
                          : effectivelyEnabled ? i18nT('pages.overview.mcpTab.click_to_disable_pending_until_apply') : i18nT('pages.overview.mcpTab.click_to_enable_pending_until_apply')}
                      ><span className={`w-1.5 h-1.5 rounded-full shrink-0 ${effectivelyEnabled ? 'bg-ok' : 'bg-muted'}`} />{t}</button>
                    })}</div></motion.div>}</AnimatePresence>
                  </div>) : '—'}
                </td>
                {/* Pinned like the header cell, on an OPAQUE `bg-card`. The row
                    states live on the <tr>, which the opaque base would hide, so
                    the overlay re-applies them: even rows mirror
                    `.table-striped`'s translucent `--card-hl` zebra (which
                    outranks the row's hover utility by specificity, so hover is
                    deliberately NOT mirrored there), odd rows mirror the hover
                    tint via the named row group. */}
                <td className="sticky right-0 bg-card px-2.5 py-2 border-b border-border text-sm text-right whitespace-nowrap">
                  <div aria-hidden className={`absolute inset-0 -z-10 transition-colors ${i % 2 === 1 ? 'bg-[var(--card-hl)]' : 'group-hover/mcprow:bg-bg-hover'}`} />
                  {mcpTableEdges.right && <div aria-hidden="true" className="pointer-events-none absolute left-0 top-0 bottom-0 w-px bg-border" />}
                  {/* The fade must ramp toward the surface it abuts: on a hovered
                      odd row that is the hover tint, not the card (even rows
                      keep from-card — zebra outranks the row's hover utility,
                      so their surface never changes). */}
                  {mcpTableEdges.right && <div aria-hidden="true" className={`pointer-events-none absolute right-full top-0 bottom-0 w-6 bg-gradient-to-l from-card to-transparent ${i % 2 === 1 ? '' : 'group-hover/mcprow:from-bg-hover'}`} />}
                  {pendingUninstall ? (
                    <Btn onClick={() => revertRow(s.name)}>{i18nT('pages.overview.mcpTab.undo')}</Btn>
                  ) : (
                    <div className="flex gap-1 justify-end">
                      {s.kirocrewManaged && (
                        <Btn onClick={() => setEditTarget(s.name)} aria-label={i18nT('pages.overview.mcpTab.edit_json_for', { name: s.name })} title={i18nT('pages.overview.mcpTab.edit_the_server_s_json_spec')}><Braces size={13} /></Btn>
                      )}
                      <Btn danger onClick={() => stageUninstall(s.name)}>{i18nT('pages.overview.mcpTab.uninstall')}</Btn>
                    </div>
                  )}
                </td>
              </tr>
            )
          })}</tbody></table>
        </div>
      )}
    </Card>

    <McpBrowserModal open={browserOpen} onClose={() => setBrowserOpen(false)} />
    <McpCustomServerModal open={customOpen} onClose={() => setCustomOpen(false)} />
    <McpCustomServerModal open={editTarget !== null} onClose={() => setEditTarget(null)} editName={editTarget} />
  </>)
}
