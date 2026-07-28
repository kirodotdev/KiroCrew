import { useState, useEffect, useCallback } from 'react'
import Clickable from '../components/Clickable'
import { useQuery, useMutation } from '@tanstack/react-query'
import { useAppSelector } from '../store'
import { api } from '../api/client'
import { useProvider } from '../providers'
import { Card, CardTitle, Btn, SendBtn, Input, Badge, SearchInput, StatCard, PageHeader } from '../components/ui'
import InfoTip from '../components/InfoTip'
import SimpleSelect from '../components/SimpleSelect'
import type { KiroCrewAgent } from '../components/AgentSelector'
import { SourceBadge } from '../components/SourceBadge'

import { i18nT } from '../i18n/t'
/** Common shape returned by the agent/workspace mutation endpoints. */
interface AgentMutationResult {
  error?: string
  name?: string
}

/** Editable fields sent when updating an existing agent binding. */
interface AgentUpdatePayload {
  kiro_agent: string
  workspace: string
  memory_store: string
}

/* ── Workspace Creation Modal ── */
function WorkspaceModal({
  workspaceOptions,
  onCreated,
  onClose,
}: {
  workspaceOptions: string[]
  onCreated: (name: string) => void
  onClose: () => void
}) {
  const [wsName, setWsName] = useState('')
  const [wsDir, setWsDir] = useState('workspace')
  const [dirTouched, setDirTouched] = useState(false)
  const [copyFrom, setCopyFrom] = useState('')
  const [wsError, setWsError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Auto-fill directory from workspace name (unless user manually edited it)
  const handleNameChange = (v: string) => {
    setWsName(v)
    if (!dirTouched) {
      const slug = v.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '')
      setWsDir(slug ? `workspace-${slug}` : 'workspace')
    }
  }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const submit = async () => {
    setWsError('')
    const n = wsName.trim()
    if (!n) { setWsError('Workspace name is required'); return }
    setSubmitting(true)
    try {
      const body: Record<string, string> = { name: n, dir: wsDir }
      if (copyFrom) body.copy_from = copyFrom
      const r: AgentMutationResult = await api.createWorkspace(body)
      if (r.error) { setWsError(r.error); setSubmitting(false); return }
      onCreated(r.name || n)
    } catch (e) {
      setWsError(e instanceof Error ? e.message : 'Failed to create workspace')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center">
      <Clickable aria-label={i18nT('pages.kiroCrewAgentsPage.close_dialog')} className="absolute inset-0 bg-black/50" onClick={onClose} />
      <div role="dialog" aria-modal="true" aria-label={i18nT('pages.kiroCrewAgentsPage.create_workspace')} className="relative z-10 w-full max-w-md">
        <Card className="!mb-0">
          <CardTitle>{i18nT('pages.kiroCrewAgentsPage.create_workspace')}</CardTitle>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
              <label htmlFor="ws-name" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.name')}</label>
              <InfoTip text="A unique identifier for this workspace. Agents reference workspaces by name." />
            </div>
            <Input id="ws-name" placeholder={i18nT('pages.kiroCrewAgentsPage.e_g_oncall')} value={wsName} onChange={e => handleNameChange(e.target.value)} autoFocus />
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
              <label htmlFor="ws-dir" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.directory')}</label>
              <InfoTip text="Subdirectory inside ~/.kiro/crew where this workspace stores its data (chat history, lessons, projects). Each workspace gets its own isolated directory." />
            </div>
            <Input id="ws-dir" placeholder={i18nT('pages.kiroCrewAgentsPage.workspace')} value={wsDir} onChange={e => { setDirTouched(true); setWsDir(e.target.value) }} />
          </div>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.copy_from_optional')}</span>
              <InfoTip text="Copy the contents of an existing workspace into the new one. Leave as '— none —' to start fresh." />
            </div>
            <SimpleSelect
              options={workspaceOptions}
              value={copyFrom}
              onChange={setCopyFrom}
              clearLabel={i18nT('pages.kiroCrewAgentsPage.none')}
              aria-label={i18nT('pages.kiroCrewAgentsPage.copy_from_workspace')}
            />
          </div>
          {wsError && <div className="text-danger text-[13px]">{wsError}</div>}
          <div className="flex gap-2 justify-end mt-1">
            <Btn onClick={onClose}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
            <SendBtn onClick={submit} disabled={submitting}>{submitting ? 'Creating…' : 'Create'}</SendBtn>
          </div>
        </div>
      </Card>
      </div>
    </div>
  )
}

export default function KiroCrewAgentsPage({ embedded }: { embedded?: boolean } = {}) {
  const provider = useProvider()
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)

  const { data: agentsData, refetch: refetchAgents } = useQuery({
    queryKey: ['kirocrew-agents', refreshTrigger],
    queryFn: () => api.kirocrewAgents(),
  })
  const agents: KiroCrewAgent[] = agentsData?.agents || []
  const defaultAgent = agentsData?.default_agent || ''

  const { data: installedAgents } = useQuery({
    queryKey: ['agents-installed', refreshTrigger],
    queryFn: () => api.agentsInstalled(),
  })
  const kiroAgentOptions = Array.isArray(installedAgents) ? installedAgents.map((x: { name: string }) => x.name).filter(Boolean) : ['kirocrew']

  const { data: workspacesData, refetch: refetchWorkspaces } = useQuery({
    queryKey: ['workspaces', refreshTrigger],
    queryFn: () => api.workspaces(),
  })
  const workspaceOptions = workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default']

  const { data: kirocrewCfg } = useQuery({
    queryKey: ['kirocrewConfig', refreshTrigger],
    queryFn: () => api.kirocrewConfig(),
  })
  const memoryStoreOptions = kirocrewCfg?.memory_stores ? Object.keys(kirocrewCfg.memory_stores) : ['default']

  const [filter, setFilter] = useState('')
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [kiroAgent, setKiroAgent] = useState('kirocrew')
  const [workspace, setWorkspace] = useState('default')
  const [memoryStore, setMemoryStore] = useState('default')
  const [editing, setEditing] = useState<string | null>(null)
  const [editKiro, setEditKiro] = useState('')
  const [editWs, setEditWs] = useState('')
  const [editMs, setEditMs] = useState('')
  const [wsModalTarget, setWsModalTarget] = useState<'create' | 'edit' | null>(null)

  const handleWsCreated = useCallback((newName: string) => {
    const target = wsModalTarget
    setWsModalTarget(null)
    refetchWorkspaces().then(() => {
      if (target === 'create') setWorkspace(newName)
      else if (target === 'edit') setEditWs(newName)
    })
  }, [wsModalTarget, refetchWorkspaces])

  const createMut = useMutation({
    mutationFn: (data: { name: string; kiro_agent: string; workspace: string; memory_store: string }) => api.createKirocrewAgent(data),
    onSuccess: (r: AgentMutationResult) => { if (r.error) { setError(r.error); return }; setName(''); setKiroAgent('kirocrew'); setWorkspace('default'); setMemoryStore('default'); refetchAgents() },
    onError: (e: Error) => setError(e.message || 'Failed to create agent'),
  })
  const updateMut = useMutation({
    mutationFn: ({ name, data }: { name: string; data: AgentUpdatePayload }) => api.updateKirocrewAgent(name, data),
    onSuccess: (r: AgentMutationResult) => { if (r.error) { setError(r.error); return }; setEditing(null); refetchAgents() },
    onError: (e: Error) => setError(e.message || 'Failed to update agent'),
  })
  const deleteMut = useMutation({
    mutationFn: (n: string) => api.deleteKirocrewAgent(n),
    onSuccess: (r: AgentMutationResult) => { if (r.error) { setError(r.error); return }; refetchAgents() },
    onError: (e: Error) => setError(e.message || 'Failed to delete agent'),
  })

  const create = () => {
    setError('')
    const n = name.trim()
    if (!n) { setError('Name is required'); return }
    createMut.mutate({ name: n, kiro_agent: kiroAgent, workspace, memory_store: memoryStore })
  }

  const startEdit = (a: KiroCrewAgent) => {
    setEditing(a.name); setEditKiro(a.kiro_agent); setEditWs(a.workspace); setEditMs(a.memory_store)
  }

  const saveEdit = () => {
    if (!editing) return
    setError('')
    updateMut.mutate({ name: editing, data: { kiro_agent: editKiro, workspace: editWs, memory_store: editMs } })
  }

  const remove = (n: string) => {
    setError('')
    deleteMut.mutate(n)
  }

  const filtered = agents.filter(a =>
    !filter || (a.name + ' ' + a.kiro_agent + ' ' + a.workspace + ' ' + a.memory_store).toLowerCase().includes(filter.toLowerCase())
  )

  return (
    <>
      {!embedded && <PageHeader title={i18nT('pages.kiroCrewAgentsPage.agents')} subtitle={i18nT('pages.kiroCrewAgentsPage.manage_agent_workspace_memory_store_bindings')} />}
      <div className={`${embedded ? '' : 'px-6'} pb-8 overflow-y-auto flex-1 min-h-0`}>
        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard label={i18nT('pages.kiroCrewAgentsPage.total_agents')} value={agents.length} accent />
          <StatCard label={i18nT('pages.kiroCrewAgentsPage.default')} value={defaultAgent || '—'} />
        </div>

        <Card>
          <CardTitle>{i18nT('pages.kiroCrewAgentsPage.create_agent')} <InfoTip text={`Create a new agent binding. Each agent maps a name to a ${provider.labels.agentTemplateField.toLowerCase()}, workspace, and memory store.`} /></CardTitle>
          <div className="flex gap-2 items-end flex-wrap">
            <div className="flex flex-col gap-1">
              {/* Native input associated via htmlFor+id; label-has-for's nesting requirement is a false positive. */}
              {/* eslint-disable-next-line jsx-a11y/label-has-for */}
              <label htmlFor="agent-name" className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.name')}</label>
              <Input id="agent-name" placeholder={i18nT('pages.kiroCrewAgentsPage.e_g_oncall')} value={name} onChange={e => setName(e.target.value)} style={{ width: 140 }} />
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{provider.labels.agentTemplateField}</span>
              <SimpleSelect options={kiroAgentOptions} value={kiroAgent} onChange={setKiroAgent} aria-label={provider.labels.agentTemplateField} style={{ width: 160 }} />
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.workspace_2')}</span>
              <SimpleSelect
                options={workspaceOptions}
                value={workspace}
                onChange={setWorkspace}
                action={{ label: '+ New workspace…', onSelect: () => setWsModalTarget('create') }}
                aria-label={i18nT('pages.kiroCrewAgentsPage.workspace_2')}
                style={{ width: 160 }}
              />
            </div>
            <div className="flex flex-col gap-1">
              <span className="text-[11px] text-muted uppercase tracking-wider font-medium">{i18nT('pages.kiroCrewAgentsPage.memory_store')}</span>
              <SimpleSelect options={memoryStoreOptions} value={memoryStore} onChange={setMemoryStore} aria-label={i18nT('pages.kiroCrewAgentsPage.memory_store')} style={{ width: 160 }} />
            </div>
            <SendBtn onClick={create}>{i18nT('pages.kiroCrewAgentsPage.create')}</SendBtn>
          </div>
          {error && <div className="text-danger text-[13px] mt-2">{error}</div>}
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-2">
            <CardTitle>{i18nT('pages.kiroCrewAgentsPage.agents')}</CardTitle>
          </div>
          <div className="mb-3"><SearchInput placeholder={i18nT('pages.kiroCrewAgentsPage.filter_agents')} value={filter} onChange={e => setFilter(e.target.value)} /></div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse table-striped">
              <thead>
                <tr>
                  {['Name', provider.labels.agentTemplateField, 'Source', 'Workspace', 'Memory Store', 'Actions'].map(h => (
                    <th key={h} className="text-left text-muted text-[12px] uppercase tracking-[.04em] px-2.5 py-2 border-b border-border font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.length === 0 ? (
                  <tr><td colSpan={6} className="text-muted italic px-2.5 py-3.5 text-sm">{i18nT('pages.kiroCrewAgentsPage.no_agents')}</td></tr>
                ) : filtered.map((a) => (
                  <tr key={a.name} className="hover:bg-bg-hover transition-colors">
                    <td className="px-2.5 py-2 border-b border-border text-sm font-mono font-semibold">
                      {a.name}
                      {a.name === defaultAgent && <> <Badge variant="ok">{i18nT('pages.kiroCrewAgentsPage.default_2')}</Badge></>}
                    </td>
                    {editing === a.name ? (
                      <>
                        <td className="px-2.5 py-2 border-b border-border">
                          <SimpleSelect options={[...kiroAgentOptions, ...(!kiroAgentOptions.includes(editKiro) ? [editKiro] : [])]} value={editKiro} onChange={setEditKiro} aria-label={i18nT('pages.kiroCrewAgentsPage.edit_agent_template')} style={{ width: 140 }} />
                        </td>
                        <td className="px-2.5 py-2 border-b border-border text-sm"><SourceBadge source={a.source || 'kirocrew'} /></td>
                        <td className="px-2.5 py-2 border-b border-border">
                          <SimpleSelect
                            options={[...workspaceOptions, ...(!workspaceOptions.includes(editWs) ? [editWs] : [])]}
                            value={editWs}
                            onChange={setEditWs}
                            action={{ label: '+ New workspace…', onSelect: () => setWsModalTarget('edit') }}
                            aria-label={i18nT('pages.kiroCrewAgentsPage.edit_workspace')}
                            style={{ width: 140 }}
                          />
                        </td>
                        <td className="px-2.5 py-2 border-b border-border">
                          <SimpleSelect options={[...memoryStoreOptions, ...(!memoryStoreOptions.includes(editMs) ? [editMs] : [])]} value={editMs} onChange={setEditMs} aria-label={i18nT('pages.kiroCrewAgentsPage.edit_memory_store')} style={{ width: 140 }} />
                        </td>
                        <td className="px-2.5 py-2 border-b border-border text-sm">
                          <Btn onClick={saveEdit}>{i18nT('pages.kiroCrewAgentsPage.save')}</Btn>{' '}
                          <Btn onClick={() => setEditing(null)}>{i18nT('pages.kiroCrewAgentsPage.cancel')}</Btn>
                        </td>
                      </>
                    ) : (
                      <>
                        <td className="px-2.5 py-2 border-b border-border text-sm font-mono">{a.kiro_agent}</td>
                        <td className="px-2.5 py-2 border-b border-border text-sm"><SourceBadge source={a.source || 'kirocrew'} /></td>
                        <td className="px-2.5 py-2 border-b border-border text-sm font-mono">{a.workspace}</td>
                        <td className="px-2.5 py-2 border-b border-border text-sm font-mono">{a.memory_store}</td>
                        <td className="px-2.5 py-2 border-b border-border text-sm">
                          <Btn onClick={() => startEdit(a)}>{i18nT('pages.kiroCrewAgentsPage.edit')}</Btn>{' '}
                          {a.name !== defaultAgent && <Btn danger onClick={() => remove(a.name)}>{i18nT('pages.kiroCrewAgentsPage.delete')}</Btn>}
                        </td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
      {wsModalTarget && (
        <WorkspaceModal
          workspaceOptions={workspaceOptions}
          onCreated={handleWsCreated}
          onClose={() => setWsModalTarget(null)}
        />
      )}
    </>
  )
}
