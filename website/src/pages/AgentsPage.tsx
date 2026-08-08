import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Star, StarOff, Brain, Plug, X, Pin, Package, Lock, Hourglass, Bot, ChevronDown } from 'lucide-react'
import { createPortal } from 'react-dom'
import { useAppSelector } from '../store'
import { api } from '../api/client'
import type { SubagentInfo } from '../types'
import Clickable from '../components/Clickable'
import { SourceBadge, StatCard, PageHeader, EmptyState, Btn, Input } from '../components/ui'
import ModelDropdownList from '../components/ModelDropdownList'
import AgentSkillsEditor from '../components/AgentSkillsEditor'
import { LAYOUT } from '../components/layout'
import InfoTip from '../components/InfoTip'
import { useProvider } from '../providers'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { useAvailableModels } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
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
 * Fields the detail panel reads off the selected agent. It's populated from
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

export default function AgentsPage({ embedded }: { embedded?: boolean } = {}) {
  const provider = useProvider()
  const status = useAppSelector(s => s.dashboard.status)
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)

  const { data: spawnData, refetch: refetchSpawn } = useQuery({
    queryKey: ['spawn-list', refreshTrigger],
    queryFn: () => api.spawnList(),
  })
  const agents: SubagentInfo[] = spawnData?.agents || []

  const { data: ctx = [] } = useQuery<CtxSession[]>({
    queryKey: ['sessions-context', refreshTrigger],
    queryFn: () => api.sessionsContext().then(d => d.sessions || []),
    refetchInterval: 15000,
  })

  const { data: usage = null } = useQuery({
    queryKey: ['sessions-usage', refreshTrigger],
    queryFn: () => api.sessionsUsage().then(d => (d.usage && Number.isFinite(d.usage.credits_plan)) ? d.usage : null).catch(() => null),
  })

  const { data: installed = [], isPending: installedLoading, refetch: refetchInstalled } = useQuery({
    queryKey: ['agents-installed', refreshTrigger],
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
    queryKey: ['mcp-tools', refreshTrigger],
    queryFn: async () => {
      const probed = await api.mcpProbeCache()
      if (!Array.isArray(probed)) return {}
      const map: Record<string, string[]> = {}
      for (const s of probed) if (s.name && s.tools?.length) map[s.name] = s.tools
      return map
    },
  })

  const { data: defaultAgentData, refetch: refetchDefault } = useQuery({
    queryKey: ['default-agent', refreshTrigger],
    queryFn: () => api.defaultAgent().then(d => d.default_agent || ''),
  })
  const defaultAgent = defaultAgentData ?? ''

  const [selectedAgent, setSelectedAgent] = useState<AgentDetail | null>(null)
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
    onSuccess: (_r, name) => { if (selectedAgent?.name === name) setSelectedAgent(null); refetchInstalled() },
  })
  const spawnClearMut = useMutation({ mutationFn: () => api.spawnClear(), onSuccess: () => refetchSpawn() })
  const spawnDeleteMut = useMutation({ mutationFn: (id: string) => api.spawnDelete(id), onSuccess: () => refetchSpawn() })

  const toggleDefaultMut = useMutation({
    mutationFn: (next: string) => api.setDefaultAgent(next),
    onSuccess: () => refetchDefault(),
  })
  const toggleDefault = (agentName: string) => {
    toggleDefaultMut.mutate(defaultAgent === agentName ? '' : agentName)
  }

  // Auto-open detail for first installed agent
  const { data: initialAgentDetail } = useQuery({
    queryKey: ['agent-detail', installed[0]?.name],
    queryFn: () => api.agentDetail(installed[0]!.name),
    enabled: !selectedAgent && installed.length > 0,
  })
  useEffect(() => { if (initialAgentDetail && !selectedAgent) setSelectedAgent(initialAgentDetail) }, [initialAgentDetail, selectedAgent])

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.agentsPage.agent_templates')} subtitle={i18nT('pages.agentsPage.active_sessions_and_subagent_tasks')} />}
      <div className={`${embedded ? '' : 'px-6 pb-8'} overflow-y-auto flex-1 min-h-0`}>
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label={i18nT('pages.agentsPage.default_for_new_sessions')} value={defaultAgent || '—'} />
          <StatCard label={i18nT('pages.agentsPage.sessions')} value={status?.sessions} />
          <StatCard label={i18nT('pages.agentsPage.subagents')} value={status?.subagents} accent />
        </div>
        {/* Installed Agents — fixed left list, fixed right detail */}
        {installedLoading ? (
          <div className="card-glow border border-border bg-card rounded-lg mb-4 shadow-sm flex items-center justify-center py-10 gap-2 text-muted text-sm">
            <Hourglass className="lucide-inline animate-pulse" /> {i18nT('pages.agentsPage.loading_agents')}
          </div>
        ) : installed.length > 0 && (
          <div className="card-glow border border-border bg-card rounded-lg mb-4 animate-rise shadow-sm hover:border-border-strong hover:shadow-md transition-all overflow-hidden">
            <div className="px-5 pt-5 pb-3"><h3 className="text-sm font-semibold text-text-strong flex items-center gap-1.5">{i18nT('pages.agentsPage.installed_agents')} <InfoTip text={i18nT('pages.agentsPage.agent_templates_grouped_by_package_update_and_un')} /></h3></div>
            <div className="flex" style={{ height: `${LAYOUT.AGENT_LIST_HEIGHT}px` }}>
              {/* Agent list — scrollable */}
              <div className="w-[280px] shrink-0 border-r border-border overflow-y-auto p-2">
                {(() => {
                  // Group: non-package agents first, then package agents grouped by package
                  const nonPackage = installed.filter(a => a.source !== 'package')
                  const packageGrouped = installed.filter(a => a.source === 'package').reduce<Record<string, typeof installed>>((g, a) => {
                    const key = a.package || a.name; (g[key] ||= []).push(a); return g
                  }, {})
                  const renderAgent = (a: typeof installed[0], showDelete?: boolean) => (
                    <Clickable key={a.name} className={`flex flex-col gap-1.5 px-3 py-2.5 rounded-md border transition-all cursor-pointer mb-1 ${selectedAgent?.name === a.name ? 'list-selected bg-accent-subtle border-accent/40' : 'bg-bg-elevated border-transparent hover:bg-bg-hover hover:border-border-strong'}`} onClick={async () => { try { const d = await api.agentDetail(a.name); setSelectedAgent(d) } catch { /* List rows carry display NAMES; the editor round-trips catalog KEYS, so drop them rather than offer unsavable chips. */ setSelectedAgent({ ...a, skills: undefined }) } }}>
                      {/* Row 1: the name owns the full column width; badge + delete are pinned right and never steal the name's space. */}
                      <div className="flex items-center gap-1.5 min-w-0">
                        <span className="text-[13px] font-mono font-semibold text-text truncate flex-1 min-w-0" title={a.name}>{a.name}</span>
                        <SourceBadge source={a.source} />
                        {showDelete && <button className="text-[10px] text-muted hover:text-danger-fg hover:bg-danger px-1 py-0.5 rounded border border-border hover:border-danger/40 transition-all shrink-0" title={i18nT('pages.agentsPage.delete_agent', { name: a.name })} aria-label={i18nT('pages.agentsPage.delete_agent', { name: a.name })} onClick={e => { e.stopPropagation(); if (confirm(`Delete agent "${a.name}"? This removes the config file.`)) deleteAgentMut.mutate(a.name) }}><X className="lucide-inline" /></button>}
                      </div>
                      {/* Row 2: model metadata on the left, the labeled default control on the right. */}
                      <div className="flex items-center justify-between gap-2 min-w-0">
                        <div className="flex items-center gap-2 min-w-0">
                          {a.skills.length > 0 && <span className="text-[11px] text-muted shrink-0"><Brain className="lucide-inline" />{a.skills.length}</span>}
                          {a.mcp_servers.length > 0 && <span className="text-[11px] text-muted shrink-0"><Plug className="lucide-inline" />{a.mcp_servers.length}</span>}
                          <span className="text-[11px] text-muted font-mono truncate min-w-0" title={modelLabel(a.model)}>{modelLabel(a.model)}</span>
                        </div>
                        {/* The word carries the state: a bare star glyph gives a first-time
                            user nothing to read, so the default-agent control is labeled. */}
                        <span
                          role="button"
                          tabIndex={0}
                          aria-label={defaultAgent === a.name ? i18nT('pages.agentsPage.remove_default_agent') : i18nT('pages.agentsPage.set_as_default_agent')}
                          className={`shrink-0 inline-flex items-center gap-1 px-2 py-[3px] rounded-md border text-[11px] font-medium transition-colors cursor-pointer ${defaultAgent === a.name ? 'text-warn border-warn/45 bg-warn-subtle' : 'text-muted border-border-strong hover:text-warn hover:border-warn hover:bg-warn-subtle'}`}
                          title={defaultAgent === a.name ? i18nT('pages.agentsPage.remove_default_agent') : i18nT('pages.agentsPage.set_as_default_agent')}
                          onClick={e => { e.stopPropagation(); toggleDefault(a.name) }}
                          onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); toggleDefault(a.name) } }}
                        >
                          {defaultAgent === a.name
                            ? <><Star className="lucide-inline" />{i18nT('pages.agentsPage.default')}</>
                            : <><StarOff className="lucide-inline" />{i18nT('pages.agentsPage.set_as_default')}</>}
                        </span>
                      </div>
                    </Clickable>
                  )
                  return (<>
                    {nonPackage.map(a => renderAgent(a, a.source !== 'kirocrew'))}
                    {Object.entries(packageGrouped).map(([pkg, agents]) => {
                      const isLocal = agents[0]?.filename?.startsWith('local-')
                      return (
                        <div key={pkg} className="mt-2">
                          <div className="flex items-center gap-1.5 px-2 py-1.5 bg-bg-hover rounded-md mb-1 min-w-0">
                            <span className="text-[11px] text-aim font-semibold tracking-wider min-w-0 truncate flex-1" title={pkg}>{isLocal ? <Pin className="lucide-inline" /> : <Package className="lucide-inline" />} {pkg}</span>
                          </div>
                          {agents.map(a => renderAgent(a))}
                        </div>
                      )
                    })}
                  </>)
                })()}
              </div>
              {/* Detail panel — scrollable */}
              <div className="flex-1 min-w-0 overflow-y-auto p-4">
                {selectedAgent ? (<>
                  <div className="flex items-center justify-between mb-3">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-mono font-bold text-text-strong">{selectedAgent.name}</span>
                      <div className="relative">
                        <Btn ref={modelBtnRef} className="flex items-center gap-1 px-2 py-0.5 text-[12px] font-mono font-medium" onClick={() => setModelDropOpen(!modelDropOpen)}>
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
                              <Input ref={modelInputRef} type="text" aria-label={i18nT('pages.agentsPage.filter_models')} placeholder={i18nT('pages.agentsPage.type_to_filter')} value={modelFilter} onChange={e => setModelFilter(e.target.value)} className="w-full px-2 py-1 text-[13px] font-mono" />
                            </div>
                            <div role="listbox" aria-label={i18nT('pages.agentsPage.model_list')} className="overflow-y-auto flex-1 min-h-0">
                            <ModelDropdownList models={filteredModels} activeModel={modelLabel(selectedAgent.model)} onSelect={name => { const val = name === 'auto' ? '' : name; patchModelMut.mutate({ name: selectedAgent.name, model: val }); setModelDropOpen(false) }} />
                            </div>
                          </div>,
                          document.body
                        )}
                      </div>
                      {(() => { const a = installed.find(a => a.name === selectedAgent.name); return a?.package ? <span className="text-[11px] text-aim bg-aim/10 px-2 py-0.5 rounded-md border border-aim/30">{a.filename?.startsWith('local-') ? <Pin className="lucide-inline" /> : <Package className="lucide-inline" />} {a.package}</span> : null })()}
                    </div>
                  </div>
                  {/* `typeof` guard, not a bare truthiness check: an object is
                      truthy, so a foreign spec's structured `description` would
                      pass `&&` and then throw React error #31 as a JSX child —
                      the same whole-tab crash `modelLabel` guards on `model`. */}
                  {typeof selectedAgent.description === 'string' && selectedAgent.description && <div className="text-[13px] text-muted mb-3 leading-relaxed">{selectedAgent.description}</div>}
                  {selectedAgent.skills === undefined ? (
                    /* The agent-detail fetch failed, so the real mapping is
                     * UNKNOWN. An empty-but-enabled editor here is destructive:
                     * add/remove PATCH the complete desired key list and the
                     * backend fully replaces the managed skill:// set, so one
                     * "Add skill" click over unknown state would silently delete
                     * every real mapping from the agent's spec on disk. Show why
                     * it is unavailable instead of offering a write. */
                    <div className="mb-3">
                      <div className="text-[12px] text-muted font-medium uppercase tracking-wider mb-1">{i18nT('pages.agentsPage.skills')}</div>
                      <div className="text-[12px] text-warn">
                        {i18nT('pages.agentsPage.could_not_load_this_agent_s_configuration_skills')}
                      </div>
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
                  )}
                  {selectedAgent.prompt && <div className="mb-3"><div className="text-[12px] text-muted font-medium uppercase tracking-wider mb-1">{i18nT('pages.agentsPage.system_prompt')}</div><pre className="text-[12px] text-text font-mono bg-bg-elevated rounded-md p-2.5 border border-border overflow-x-auto max-h-[160px] overflow-y-auto whitespace-pre-wrap leading-relaxed">{typeof selectedAgent.prompt === 'string' && selectedAgent.prompt.startsWith('file://') ? selectedAgent.prompt : (selectedAgent.prompt || '').slice(0, 2000)}</pre></div>}
                  {selectedAgent.tools && <div className="mb-3"><div className="text-[12px] text-muted font-medium uppercase tracking-wider mb-1">{i18nT('pages.agentsPage.tools')}</div><div className="flex flex-wrap gap-1.5">{(selectedAgent.tools as string[]).map((t: string) => <span key={t} className="px-2 py-1 rounded-full text-[12px] font-mono bg-bg-elevated border border-border text-text">{t}</span>)}</div></div>}
                  {selectedAgent.allowedTools && <div className="mb-3"><div className="text-[12px] text-muted font-medium uppercase tracking-wider mb-1">{i18nT('pages.agentsPage.auto_approved')}</div><div className="flex flex-wrap gap-1.5">{(selectedAgent.allowedTools as string[]).map((t: string) => <span key={t} className="px-2 py-1 rounded-full text-[12px] font-mono bg-ok/10 border border-ok/30 text-ok">{t}</span>)}</div></div>}
                  {selectedAgent.mcpServers && <div className="mb-3"><div className="text-[12px] text-muted font-medium uppercase tracking-wider mb-1">{i18nT('pages.agentsPage.mcp_servers')}</div><div className="flex flex-wrap gap-1.5">{Object.keys(selectedAgent.mcpServers).map((s: string) => {
                    const srv = (selectedAgent.mcpServers as Record<string, {args?: string[]}>)[s]
                    const args = srv?.args || []
                    const idx = args.indexOf('--include-tools')
                    const restricted = idx >= 0 && args[idx + 1] ? args[idx + 1].split(',') : null
                    const tools = restricted || mcpTools[s] || null
                    const label = restricted ? `${tools.length} restricted` : tools ? `${tools.length}` : null
                    return <span key={s} className="group relative px-2 py-1 rounded-full text-[12px] font-mono bg-aim-subtle border border-aim/30 text-aim cursor-help">{s}{label && <span className={`text-[11px] ml-0.5 ${restricted ? 'text-warn' : 'text-muted'}`}>({label})</span>}{tools && <span className="invisible group-hover:visible absolute left-0 top-full mt-1 z-50 bg-card border border-border rounded-lg shadow-lg p-3 min-w-[220px] max-w-[360px] max-h-[200px] overflow-y-auto"><span className="block text-[13px] text-muted font-medium mb-1.5">{s} {i18nT('pages.agentsPage.tools_2')}{restricted ? ' (restricted)' : ''}:</span>{tools.map(t => <span key={t} className="block text-[13px] text-text font-mono py-0.5">{t}</span>)}</span>}</span>
                  })}</div></div>}
                  {selectedAgent.toolsSettings?.execute_bash?.deniedCommands && <div><div className="text-[12px] text-muted font-medium uppercase tracking-wider mb-1">{i18nT('pages.agentsPage.denied_commands')} <span className="text-danger"><Lock className="lucide-inline" /></span></div><details className="text-[12px]"><summary className="text-danger/70 font-mono cursor-pointer hover:text-danger transition-colors">{(selectedAgent.toolsSettings.execute_bash.deniedCommands as string[]).length} {i18nT('pages.agentsPage.patterns_blocked')}</summary><div className="mt-1.5 max-h-[200px] overflow-y-auto bg-bg-elevated rounded-md border border-border p-2 space-y-0.5">{(selectedAgent.toolsSettings.execute_bash.deniedCommands as string[]).map((p: string, i: number) => <div key={i} className="text-danger/60 font-mono">{p}</div>)}</div></details></div>}
                </>) : (
                  <div className="flex items-center justify-center h-full text-muted text-[13px]">{i18nT('pages.agentsPage.select_an_agent_to_view_details')}</div>
                )}
              </div>
            </div>
          </div>
        )}
        {/* Context Window Usage */}
        <div className="card-glow border border-border bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm transition-all">
          <h3 className="text-sm font-semibold text-text-strong mb-3.5 flex items-center gap-1.5">{i18nT('pages.agentsPage.context_window_usage')} <InfoTip text={i18nT('pages.agentsPage.context_window_usage_tip', { label: provider.labels.sessionProcess })} /></h3>
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
                const modelShort = s.model === 'auto' ? 'auto' : s.model.replace(/^(us\.|anthropic\.|amazon\.)/, '').replace(/-v\d+:\d+$/, '')
                return (
                  <div key={s.key}>
                    <div className="flex items-center justify-between mb-1.5">
                      <div className="text-sm font-medium text-text">{s.name}</div>
                      <div className="text-[13px] text-muted font-mono">{s.agent && s.agent !== 'kirocrew' ? <span className={`mr-1.5 ${installed.find(a => a.name === s.agent)?.source === 'kirocrew' ? 'text-accent' : 'text-aim'}`}>{s.agent}</span> : null}{modelShort}</div>
                    </div>
                    <div className="relative h-5 bg-bg-elevated rounded-full overflow-hidden border border-border">
                      {awaiting ? (
                        <div className="absolute inset-0 flex items-center justify-center text-[12px] font-mono text-muted">{i18nT('pages.agentsPage.awaiting_first_prompt')}</div>
                      ) : (<>
                        <div
                          className={`absolute inset-y-0 left-0 rounded-full transition-all duration-700 ease-out ${barColor(pct)} ${pct > 5 ? barGlow(pct) : ''}`}
                          style={{ width: `${Math.max(pct, 1)}%` }}
                        />
                        <div className="absolute inset-0 flex items-center justify-between px-2.5 text-[12px] font-mono font-medium">
                          <span className="text-text-strong drop-shadow-sm">{pct.toFixed(1)}%</span>
                          <span className="text-text-strong drop-shadow-sm">{fmtTokens(usedTokens)} / {fmtTokens(maxTokens)}</span>
                        </div>
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
        </div>
        {/* Kiro Usage */}
        {usage && (
          <div className="card-glow border border-border bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm transition-all">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-text-strong flex items-center gap-1.5">{provider.displayName} {i18nT('pages.agentsPage.usage')} <InfoTip text={i18nT('pages.agentsPage.consumption_for_current_billing_period', { provider: provider.displayName })} /></h3>
              <div className="flex items-center gap-2">
                {usage.plan && <span className="px-2 py-0.5 rounded-full text-[12px] font-bold font-mono bg-accent/15 text-accent border border-accent/30">{usage.plan}</span>}
                {usage.resets && <span className="text-[12px] text-muted">{i18nT('pages.agentsPage.resets')} {usage.resets}</span>}
              </div>
            </div>
            {usage.credits_used != null && usage.credits_plan != null && (() => {
              const pctRaw = usage.credits_plan > 0 ? (usage.credits_used / usage.credits_plan) * 100 : 0
              const pct = Math.min(pctRaw, 100)
              // credits_used is the true total; credits_overage the amount above
              // plan (fall back to used - plan when the source omits it).
              const overage = usage.credits_overage ?? Math.max(0, usage.credits_used - usage.credits_plan)
              const hasOverage = overage > 0
              const color = pct >= 90 ? 'bg-danger' : pct >= 70 ? 'bg-warn' : 'bg-accent'
              const glow = pct >= 90 ? 'shadow-[0_0_8px_var(--danger)]' : pct >= 70 ? 'shadow-[0_0_8px_var(--warn)]' : 'shadow-[0_0_8px_var(--accent-glow)]'
              return (
                <div>
                  <div className="text-[13px] text-muted mb-1.5">{i18nT('pages.agentsPage.plan_credits')}</div>
                  <div className="relative h-6 bg-bg-elevated rounded-full overflow-hidden border border-border mb-3">
                    <div className={`absolute inset-y-0 left-0 rounded-full transition-all duration-1000 ease-out ${color} ${pct > 5 ? glow : ''}`} style={{ width: `${Math.max(pct, 1)}%` }} />
                    <div className="absolute inset-0 flex items-center justify-between px-3 text-[13px] font-mono font-bold">
                      <span className="text-text-strong drop-shadow-sm">{pctRaw.toFixed(0)}%</span>
                      <span className="text-text-strong drop-shadow-sm">{usage.credits_used.toFixed(0)} / {usage.credits_plan.toFixed(0)}</span>
                    </div>
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
          </div>
        )}
        <div className="card-glow border border-border bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm transition-all">
          <h3 className="text-sm font-semibold text-text-strong mb-3.5 flex items-center gap-2">{i18nT('pages.agentsPage.subagents')} {agents.some(a => a.done) && <button className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[13px] cursor-pointer hover:text-danger hover:border-danger transition-all" onClick={() => spawnClearMut.mutate()}>{i18nT('pages.agentsPage.clear_completed')}</button>}</h3>
          <table className="w-full border-collapse table-striped"><thead><tr>{['ID','Task','Status',''].map(h => <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>)}</tr></thead>
            <tbody>{agents.length === 0 ? <tr><td colSpan={4}><EmptyState icon={<Bot className="lucide-inline" />} title={i18nT('pages.agentsPage.no_subagents')} subtitle={i18nT('pages.agentsPage.spawn_tasks_from_chat_or_cli')} /></td></tr> : agents.map(a => (
              <tr key={a.id} className="hover:bg-bg-hover transition-colors"><td className="px-2.5 py-2 border-b border-border text-sm"><code>{a.id}</code></td><td className="px-2.5 py-2 border-b border-border text-sm">{a.task}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm">{a.done ? (a.error ? <span className="inline-flex items-center gap-1 px-2 py-[2px] rounded-full text-[13px] font-medium font-mono bg-danger-subtle text-danger">{i18nT('pages.agentsPage.failed')}</span> : <span className="inline-flex items-center gap-1 px-2 py-[2px] rounded-full text-[13px] font-medium font-mono bg-ok-subtle text-ok">{i18nT('pages.agentsPage.done')}</span>) : <span className="inline-flex items-center gap-1 px-2 py-[2px] rounded-full text-[13px] font-medium font-mono bg-warn-subtle text-warn">{i18nT('pages.agentsPage.running')}</span>}</td>
                <td className="px-2.5 py-2 border-b border-border text-sm text-right"><button className="px-1.5 py-0.5 rounded border border-border bg-transparent text-muted text-[13px] cursor-pointer hover:text-danger hover:border-danger transition-all" aria-label={i18nT('pages.agentsPage.delete_subagent', { id: a.id })} onClick={() => spawnDeleteMut.mutate(a.id)}><X className="lucide-inline" /></button></td></tr>
            ))}</tbody></table>
        </div>
      </div>
    </>
  )
}
