import { useState, useEffect, useRef, type ReactNode } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Bot, FolderOpen, Brain, Settings, Lock, Flame } from 'lucide-react'
import { api } from '../../api/client'
import { Card, CardTitle, Badge, EmptyState } from '../../components/ui'
import InfoTip from '../../components/InfoTip'
import { useProvider } from '../../providers'

import type { KiroCrewAgent } from '../../components/AgentSelector'

type KiroCrewAgentCfg = Omit<KiroCrewAgent, 'name'>
interface WorkspaceCfg { dir: string }
interface MemoryStoreCfg { description: string; embedding_provider: string }
interface KiroCrewCfg {
  agents: Record<string, KiroCrewAgentCfg>
  default_agent: string
  workspaces: Record<string, WorkspaceCfg>
  default_workspace: string
  memory_stores: Record<string, MemoryStoreCfg>
  default_memory_store: string
  agent: { default_agent: string; provider: string; model: string; approval_mode: string; sandbox: string; subagent_max_turns?: number; max_subagents?: number; subagent_auto_max?: number; conductor_skill?: boolean; tool_search?: boolean; max_channels: number; max_channel_agents: number; enforce_denied_commands: string }
  session: { timeout_secs: number; pool_size: number; pool_agent: string; pool_ttl_secs: number }
  memory: { embedding_provider: string }
  auto_update: boolean
}

function Tag({ children, active }: { children: React.ReactNode; active?: boolean }) {
  return <span className={`px-1.5 py-[1px] rounded text-[12px] font-mono ${active ? 'bg-accent/15 text-accent border border-accent/30' : 'bg-bg-elevated text-muted border border-border'}`}>{children}</span>
}

function UsedByTags({ names }: { names: string[] }) {
  return <div className="flex gap-1 flex-wrap">{names.length > 0 ? names.map(n => <Tag key={n} active>{n}</Tag>) : <span className="text-muted text-[13px]">—</span>}</div>
}

const rowCls = "flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm"
const inputCls = "h-7 min-w-[120px] bg-bg-elevated border border-border rounded px-2 py-0.5 text-[13px] font-mono text-text focus:border-accent focus:outline-none"
const readonlyCls = "flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm bg-bg-elevated/30 rounded px-1 -mx-1"

function useDirtyTrack<T>(value: T) {
  const [ok, setOk] = useState(false)
  const dirty = useRef(false)
  useEffect(() => { if (dirty.current) { setOk(true); dirty.current = false; const t = setTimeout(() => setOk(false), 2000); return () => clearTimeout(t) } }, [value])
  const markDirty = () => { dirty.current = true }
  return { ok, markDirty }
}

function CfgRow({ label, hint, ok, children }: { label: string; hint?: string; ok: boolean; children: React.ReactNode }) {
  return (
    <div className={rowCls}>
      <span className="text-muted inline-flex items-center gap-1">{label} {hint && <InfoTip text={hint} />}</span>
      <div className="flex items-center gap-1.5">
        {children}
        {ok && <span className="text-ok text-[11px]"><Check className="lucide-inline" /></span>}
      </div>
    </div>
  )
}

function CfgSelect({ label, path, value, options, hint, labels, onSave }: { label: string; path: string; value: string; options: string[]; hint?: string; labels?: Record<string, string>; onSave: (p: string, v: string) => void }) {
  const [local, setLocal] = useState(value)
  const { ok, markDirty } = useDirtyTrack(value)
  useEffect(() => { setLocal(value) }, [value])
  return (
    <CfgRow label={label} hint={hint} ok={ok}>
      <select className={inputCls} value={local} onChange={e => { markDirty(); setLocal(e.target.value); onSave(path, e.target.value) }}>
        {options.map(o => <option key={o} value={o}>{labels?.[o] ?? o}</option>)}
      </select>
    </CfgRow>
  )
}

function CfgNumber({ label, path, value, suffix, min, max, hint, onSave }: { label: string; path: string; value: number; suffix?: string; min?: number; max?: number; hint?: string; onSave: (p: string, v: number) => void }) {
  const [local, setLocal] = useState(String(value))
  const { ok, markDirty } = useDirtyTrack(value)
  const [err, setErr] = useState('')
  useEffect(() => { setLocal(String(value)); setErr('') }, [value])
  const commit = () => {
    const n = parseInt(local)
    if (isNaN(n)) { setErr('invalid'); return }
    if (min !== undefined && n < min) { setErr(`min ${min}`); return }
    if (max !== undefined && n > max) { setErr(`max ${max}`); return }
    if (n !== value) { markDirty(); setErr(''); onSave(path, n) }
  }
  return (
    <CfgRow label={label} hint={hint} ok={ok && !err}>
      <input type="number" aria-label={label} min={min} max={max} placeholder={min !== undefined && max !== undefined ? `${min}–${max}` : undefined}
        className={`${inputCls} text-right ${err ? 'border-danger' : ''}`}
        value={local}
        onChange={e => { setLocal(e.target.value); setErr('') }}
        onBlur={commit}
        onKeyDown={e => { if (e.key === 'Enter') commit() }}
      />
      {suffix && <span className="text-muted text-[12px]">{suffix}</span>}
      {err && <span className="text-danger text-[11px]">{err}</span>}
    </CfgRow>
  )
}

function CfgToggle({ label, path, value, hint, onSave }: { label: string; path: string; value: boolean; hint?: string; onSave: (p: string, v: boolean) => void }) {
  const [local, setLocal] = useState(value)
  const { ok, markDirty } = useDirtyTrack(value)
  useEffect(() => { setLocal(value) }, [value])
  return (
    <CfgRow label={label} hint={hint} ok={ok}>
      <button className={`h-7 min-w-[120px] px-2 py-0.5 rounded text-[13px] font-mono ${local ? 'bg-ok/15 text-ok border border-ok/30' : 'bg-bg-elevated text-muted border border-border'}`} onClick={() => { markDirty(); const v = !local; setLocal(v); onSave(path, v) }}>
        {local ? 'on' : 'off'}
      </button>
    </CfgRow>
  )
}

export default function KiroCrewCfgTab() {
  const provider = useProvider()
  const queryClient = useQueryClient()
  const { data: cfg = null, error: queryErr } = useQuery<KiroCrewCfg>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const err = queryErr ? (queryErr instanceof Error ? queryErr.message : String(queryErr)) : ''
  const [saveErr, setSaveErr] = useState('')
  const [rev, setRev] = useState(0)

  const reqId = useRef(0)

  const patchMut = useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) => api.patchConfig(path, value),
    onSuccess: (updated) => { queryClient.setQueryData(['kirocrewConfig'], updated) },
    onError: (e: Error) => {
      setSaveErr(e.message)
      setTimeout(() => setSaveErr(''), 4000)
      queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      setRev(r => r + 1)
    },
  })

  const save = (path: string, value: unknown) => {
    ++reqId.current
    patchMut.mutate({ path, value })
  }

  if (err) return <Card><p className="text-danger text-sm">{err}</p></Card>
  if (!cfg) return <Card><div className="skeleton h-40 rounded" /></Card>

  const agents = Object.entries(cfg.agents)
  const workspaces = Object.entries(cfg.workspaces)
  const stores = Object.entries(cfg.memory_stores)

  return (
    <>
      {/* Agents */}
      <Card>
        <CardTitle><Bot className="lucide-inline" /> KiroCrew Agents <InfoTip text={`Named agent definitions that bind a ${provider.labels.agentTemplateField.toLowerCase()}, workspace, and memory store together. Edit config.json to add or modify agents.`} /></CardTitle>
        {agents.length === 0 ? (
          <EmptyState icon={<Bot className="lucide-inline" />} title="No agents defined" subtitle="Using legacy mode — agent.default_agent as agent template" />
        ) : (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Name</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{provider.labels.agentTemplateField}</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Workspace</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Memory Store</th>
              </tr>
            </thead>
            <tbody>
              {agents.map(([name, a]) => (
                <tr key={name}>
                  <td className="px-2.5 py-2 text-sm text-text font-medium">
                    {name} {name === cfg.default_agent && <Badge variant="aim">default</Badge>}
                  </td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.kiro_agent || '—'}</td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.workspace}</td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{a.memory_store}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {/* Workspaces */}
      <Card>
        <CardTitle><FolderOpen className="lucide-inline" /> Workspaces <InfoTip text="Named workspace directories. Each agent binds to one workspace." /></CardTitle>
        <table className="w-full border-collapse table-striped">
          <thead>
            <tr>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Name</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Directory</th>
              <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Used By</th>
            </tr>
          </thead>
          <tbody>
            {workspaces.map(([name, ws]) => {
              const usedBy = agents.filter(([, a]) => a.workspace === name).map(([n]) => n)
              return (
                <tr key={name}>
                  <td className="px-2.5 py-2 text-sm text-text font-medium">
                    {name} {name === cfg.default_workspace && <Badge variant="ok">default</Badge>}
                  </td>
                  <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{ws.dir}</td>
                  <td className="px-2.5 py-2"><UsedByTags names={usedBy} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Card>

      {/* Memory Stores */}
      <Card>
        <CardTitle><Brain className="lucide-inline" /> Memory Stores <InfoTip text="Named memory stores with optional per-store embedding overrides. Unset fields inherit from the top-level memory section." /></CardTitle>
        {stores.length === 0 ? (
          <EmptyState icon={<Brain className="lucide-inline" />} title="No memory stores" subtitle="Using global memory settings" />
        ) : (
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Name</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Description</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Embedding</th>
                <th className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">Used By</th>
              </tr>
            </thead>
            <tbody>
              {stores.map(([name, ms]) => {
                const usedBy = agents.filter(([, a]) => a.memory_store === name).map(([n]) => n)
                return (
                  <tr key={name}>
                    <td className="px-2.5 py-2 text-sm text-text font-medium">
                      {name} {name === cfg.default_memory_store && <Badge variant="ok">default</Badge>}
                    </td>
                    <td className="px-2.5 py-2 text-[13px] text-muted">{ms.description || '—'}</td>
                    <td className="px-2.5 py-2 text-[13px] font-mono text-muted">{ms.embedding_provider || <span className="italic">inherited ({cfg.memory.embedding_provider})</span>}</td>
                    <td className="px-2.5 py-2"><UsedByTags names={usedBy} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Card>

      {/* Subagent Settings */}
      <SubagentSettings cfg={cfg} onSaved={() => queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] })} />

      {/* Warm Pool */}
      {provider.capabilities.warmPool && (
      <Card>
        <CardTitle><Flame className="lucide-inline" /> Warm Pool <InfoTip text={`${provider.labels.warmPoolDescription} Restart required to apply changes.`} /></CardTitle>
        {saveErr && <p className="text-danger text-[13px] mb-2">{saveErr}</p>}
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <CfgNumber key={`poolsize-${rev}`} label="Pool Size" path="session.pool_size" value={cfg.session.pool_size ?? 0} min={0} max={10} hint="Number of pre-spawned processes. 0 disables. Restart required." onSave={save} />
          <CfgSelect key={`poolagent-${rev}`} label="Pool Agent" path="session.pool_agent" value={cfg.session.pool_agent ?? ''} options={['', ...Object.keys(cfg.agents)]} labels={{'': `(${cfg.default_agent || 'default agent'})`}} hint="Agent for pool processes. Empty uses default agent. Restart required." onSave={save} />
          <CfgNumber key={`poolttl-${rev}`} label="Pool TTL" path="session.pool_ttl_secs" value={cfg.session.pool_ttl_secs} suffix="s" min={0} max={7200} hint="Max age for pooled processes. 0 disables expiry. Restart required." onSave={save} />
        </div>
      </Card>
      )}

      {/* Quick Info */}
      <Card>
        <CardTitle><Settings className="lucide-inline" /> Config Summary</CardTitle>
        {saveErr && <p className="text-danger text-[13px] mb-2">{saveErr}</p>}
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 max-[600px]:grid-cols-1">
          <div className={readonlyCls}><span className="text-muted"><Lock className="lucide-inline" /> Provider</span><span className="text-text font-mono text-[13px]">{cfg.agent.provider}</span></div>
          <CfgSelect key={`approval-${rev}`} label="Approval Mode" path="agent.approval_mode" value={cfg.agent.approval_mode} options={['auto', 'interactive']} hint="Immediate. 'auto' approves all tools; 'interactive' asks before each." onSave={save} />
          <CfgNumber key={`timeout-${rev}`} label="Session Timeout" path="session.timeout_secs" value={cfg.session.timeout_secs} suffix="s" min={60} max={86400} hint="Takes effect on next session. Range: 60–86400s." onSave={save} />
          <CfgSelect key={`sandbox-${rev}`} label="Sandbox" path="agent.sandbox" value={cfg.agent.sandbox} options={['auto', 'off']} hint="Immediate. 'auto' enables sandbox for untrusted tools." onSave={save} />
          <CfgSelect key={`enforce-${rev}`} label="Enforce Denied Commands" path="agent.enforce_denied_commands" value={cfg.agent.enforce_denied_commands ?? 'all'} options={['all', 'kirocrew']} hint="Immediate. 'all' enforces on every agent; 'kirocrew' only on kirocrew.json." onSave={save} />
          <div className={readonlyCls}><span className="text-muted"><Lock className="lucide-inline" /> Embedding Provider</span><span className="text-text font-mono text-[13px]">{cfg.memory.embedding_provider}</span></div>
          <CfgToggle key={`autoupdate-${rev}`} label="Auto Update" path="auto_update" value={cfg.auto_update} hint="Next update check cycle." onSave={save} />
          <CfgToggle key={`toolsearch-${rev}`} label="MCP Tool Search" path="agent.tool_search" value={cfg.agent.tool_search ?? true} hint="Enable dynamic MCP tool discovery via kiro-cli. Takes effect on next session." onSave={save} />
          <div className={readonlyCls}><span className="text-muted">Max Channels</span><span className="text-text font-mono text-[13px]">{cfg.agent.max_channels}</span></div>
          <div className={readonlyCls}><span className="text-muted">Max Channel Agents</span><span className="text-text font-mono text-[13px]">{cfg.agent.max_channel_agents}</span></div>
        </div>
      </Card>
    </>
  )
}

function SubagentSettings({ cfg, onSaved }: { cfg: KiroCrewCfg; onSaved: () => void }) {
  const [maxTurns, setMaxTurns] = useState(cfg.agent.subagent_max_turns ?? 100)
  const [maxSubs, setMaxSubs] = useState(cfg.agent.max_subagents ?? 3)
  const [autoMax, setAutoMax] = useState(cfg.agent.subagent_auto_max ?? 16)
  const hardCap = autoMax
  const [conductor, setConductor] = useState(cfg.agent.conductor_skill ?? false)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<ReactNode>('')
  const [msgOk, setMsgOk] = useState(false)

  useEffect(() => {
    setMaxTurns(cfg.agent.subagent_max_turns ?? 100)
    setMaxSubs(cfg.agent.max_subagents ?? 3)
    setAutoMax(cfg.agent.subagent_auto_max ?? 16)
    setConductor(cfg.agent.conductor_skill ?? false)
  }, [cfg])

  const dirty = maxTurns !== (cfg.agent.subagent_max_turns ?? 100) || maxSubs !== (cfg.agent.max_subagents ?? 3) || autoMax !== (cfg.agent.subagent_auto_max ?? 16) || conductor !== (cfg.agent.conductor_skill ?? false)

  const save = async () => {
    setSaving(true); setMsg('')
    try {
      const res = await api.saveKirocrewConfig({ subagent_max_turns: maxTurns, max_subagents: maxSubs, subagent_auto_max: autoMax, conductor_skill: conductor })
      if (res.error) { setMsg(res.error); setMsgOk(false) } else { setMsg(<><Check className="lucide-inline" /> Saved</>); setMsgOk(true); onSaved() }
    } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); setMsgOk(false) }
    finally { setSaving(false) }
  }

  return (
    <Card>
      <CardTitle><Bot className="lucide-inline" /> Subagent Settings <InfoTip text="Controls how many subagents can run concurrently and how many tool calls each subagent is allowed. Changes take effect on the next subagent spawn." /></CardTitle>
      <div className="grid grid-cols-2 gap-x-6 gap-y-3 max-[600px]:grid-cols-1">
        {/* label-has-for flags a label whose only control is a <button>; the
            toggle button is self-labeling (its text is the value) and the label
            wrapper only extends the click target to the row text — intentional. */}
        {/* eslint-disable-next-line jsx-a11y/label-has-for */}
        <label htmlFor="subagent-orchestrator-mode" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">Orchestrator Mode <InfoTip text="Enable conductor skill for multi-agent orchestration. Restart required." /></span>
          <button id="subagent-orchestrator-mode" aria-label="Orchestrator Mode" onClick={() => setConductor(!conductor)}
            className={`px-3 py-1 rounded text-[13px] font-medium border cursor-pointer transition-all ${conductor ? 'bg-accent/10 border-accent text-accent' : 'bg-transparent border-border text-muted'}`}>
            {conductor ? 'Enabled' : 'Disabled'}
          </button>
        </label>
        <label htmlFor="subagent-max-turns" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">Max Turns per Subagent <InfoTip text="Tool-call budget per subagent (1–200). Default: 100." /></span>
          <input id="subagent-max-turns" aria-label="Max Turns per Subagent" type="number" min={1} max={200} value={maxTurns} onChange={e => setMaxTurns(parseInt(e.target.value) || 1)}
            className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
        </label>
        <label htmlFor="subagent-max-concurrent" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
          <span className="text-muted inline-flex items-center gap-1">Max Concurrent Subagents <InfoTip text={`Maximum subagents running at once. 0 = auto-size from host memory/CPU (capped at ${hardCap}). Default: 3.`} /></span>
          <span className="inline-flex items-center gap-2">
            {maxSubs === 0 && <span className="text-[11px] text-muted">auto</span>}
            <input id="subagent-max-concurrent" aria-label="Max Concurrent Subagents" type="number" min={0} max={hardCap} value={maxSubs} onChange={e => { const v = parseInt(e.target.value); setMaxSubs(Number.isNaN(v) ? 0 : Math.max(0, v)) }}
              className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
          </span>
        </label>
        {maxSubs === 0 && (
          <label htmlFor="subagent-auto-size-max" className="flex justify-between items-center gap-3 py-1.5 border-b border-border text-sm">
            <span className="text-muted inline-flex items-center gap-1">Auto-Size Max <InfoTip text="Ceiling on the auto-sized concurrent subagent count (only applies when Max Concurrent Subagents = 0). The host memory/CPU formula never exceeds this. Range 1–64. Default: 16." /></span>
            <input id="subagent-auto-size-max" aria-label="Auto-Size Max" type="number" min={1} max={64} value={autoMax} onChange={e => { const v = parseInt(e.target.value); setAutoMax(Number.isNaN(v) ? 1 : Math.min(64, Math.max(1, v))) }}
              className="w-20 px-2 py-1 rounded border border-border bg-bg-elevated text-text font-mono text-[13px] text-right" />
          </label>
        )}
      </div>
      <div className="flex items-center gap-3 mt-3">
        <button onClick={save} disabled={!dirty || saving}
          className="px-3 py-1.5 rounded text-sm font-medium bg-accent text-accent-fg hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed">
          {saving ? 'Saving…' : 'Save'}
        </button>
        {msg && <span className={`text-[13px] ${msgOk ? 'text-ok' : 'text-danger'}`}>{msg}</span>}
      </div>
    </Card>
  )
}
