import { useState, useEffect, useCallback, useRef, type ReactNode } from 'react'
import { Hourglass, ClipboardList, ClipboardCheck, RefreshCw, CheckCircle, XCircle, Square, Sparkles, FileText, Settings, X, MessageSquare, Pencil, Clock, Pause, Play, RotateCcw, Plus } from 'lucide-react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAppSelector, useAppDispatch } from '../store'
import { setPendingInput, switchSlot } from '../store/chatSlice'
import { api } from '../api/client'
import type { TaskRunnerStatus, ProjectRun } from '../types'
import { SendBtn, Btn, Checkbox, Input } from '../components/ui'
import ResizeHandle from '../components/ResizeHandle'
import { useColumnResize, type CollapseConfig } from '../hooks/useColumnResize'
import AgentSelector from '../components/AgentSelector'
import type { KiroCrewAgent } from '../components/AgentSelector'
import ProjectDetailPage from './ProjectDetailPage'
import {
  COLLAPSED_RAIL_WIDTH, MAX_RAIL_WIDTH, MIN_RAIL_WIDTH,
  RAIL_COLLAPSED_KEY, RAIL_WIDTH_KEY, loadRailCollapsed, loadRailWidth,
} from './projectsLayout'

import { i18nT } from '../i18n/t'
type Mode = 'compose' | 'spec' | 'yaml'

// Module-level so the resize hook's memoised resolver isn't invalidated every render.
const RAIL_COLLAPSE: CollapseConfig = { width: COLLAPSED_RAIL_WIDTH, storageKey: RAIL_COLLAPSED_KEY }


function TextInputPanel({ text, setText, rows, placeholder, accept, onUpload, onRun, onPlan, disabled, isPlanning, onCancel, planError, banner }: {
  text: string; setText: (v: string) => void; rows: number; placeholder: string; accept: string
  onUpload: (e: React.ChangeEvent<HTMLInputElement>) => void; onRun: () => void; onPlan: () => void
  disabled: boolean; isPlanning: boolean; onCancel: () => void; planError: string | null; banner?: React.ReactNode
}) {
  return (
    <div className="space-y-3">
      {banner}
      <textarea aria-label={placeholder} className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2.5 text-text text-sm font-mono outline-none transition-colors focus-ring resize-y min-h-[120px]" rows={rows} placeholder={placeholder} value={text} onChange={e => setText(e.target.value)} disabled={disabled} />
      <div className="flex gap-2 items-center flex-wrap">
        <input type="file" aria-label={i18nT('pages.projectsPage.upload_a_file')} accept={accept} onChange={onUpload} disabled={disabled} className="text-sm text-muted file:mr-2 file:py-1 file:px-3 file:rounded-md file:border file:border-border file:bg-bg-elevated file:text-text file:text-sm file:cursor-pointer" />
        <SendBtn onClick={onRun} disabled={!text.trim() || disabled}><Play className="lucide-inline" /> {i18nT('pages.projectsPage.run')}</SendBtn>
        <Btn onClick={onPlan} disabled={!text.trim() || disabled}>{disabled ? <Hourglass className="lucide-inline" /> : <ClipboardList className="lucide-inline" />} {i18nT('pages.projectsPage.plan')}</Btn>
      </div>
      {isPlanning && <PlanningBanner onCancel={onCancel} />}
      {planError && <div className="rounded-lg border border-danger/50 bg-danger/10 px-4 py-2.5 mt-2 text-danger text-[13px]">{planError}</div>}
    </div>
  )
}

export default function ProjectsPage() {
  const refreshTrigger = useAppSelector(s => s.dashboard.refreshTrigger)
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const dispatch = useAppDispatch()
  const [data, setData] = useState<TaskRunnerStatus | null>(null)
  const [selectedRun, setSelectedRun] = useState<ProjectRun | null>(null)
  const [mode, setMode] = useState<Mode>(() => (sessionStorage.getItem('tr-mode') as Mode) || 'compose')
  const [userInput, setUserInput] = useState(() => sessionStorage.getItem('tr-input') || '')
  const [specText, setSpecText] = useState(() => sessionStorage.getItem('tr-spec') || '')
  const [yamlText, setYamlText] = useState(() => sessionStorage.getItem('tr-yaml') || '')
  const [agent, setAgent] = useState('')
  const [agents, setAgents] = useState<KiroCrewAgent[]>([])
  const [defaultAgentName, setDefaultAgentName] = useState('')
  // The user's explicit per-run workspace override. Starts empty and is only
  // populated when the user actually types — an untouched field means "no
  // override" so the backend keeps its per-run isolated scratch dir (and
  // Execute/Resume can never silently re-target an existing plan). The backend
  // default is shown as a placeholder only (defaultWorkspaceDir), never as a value.
  const [workspaceDir, setWorkspaceDir] = useState('')
  const [defaultWorkspaceDir, setDefaultWorkspaceDir] = useState('')
  const [isPlanning, setIsPlanning] = useState(() => sessionStorage.getItem('tr-planning') === '1')
  const [planError, setPlanError] = useState('')
  const [editingName, setEditingName] = useState(false)
  const [editNameValue, setEditNameValue] = useState('')
  const [refined, setRefined] = useState('')
  const [autoApprove, setAutoApprove] = useState(false)
  const [refineStatus, setRefineStatus] = useState<string>('idle')
  const [refineError, setRefineError] = useState('')
  const mountedRef = useRef(true)
  const loadingRef = useRef(false)
  const appliedRef = useRef<string | null>(null)
  const autoRunRef = useRef<string | null>(null)
  const activePlanRef = useRef(false)
  // Run rail geometry — a real resizable column with drag-past-minimum collapse,
  // the same primitive Issue Radar's rail uses.
  const rail = useColumnResize(
    RAIL_WIDTH_KEY, loadRailWidth, MIN_RAIL_WIDTH, MAX_RAIL_WIDTH, RAIL_COLLAPSE, loadRailCollapsed,
  )

  useEffect(() => { mountedRef.current = true; return () => { mountedRef.current = false } }, [])

  const load = useCallback(async () => {
    if (loadingRef.current) return
    loadingRef.current = true
    try {
      const d = await api.taskRunnerStatus()
      setData(d)
      // Surface the backend's default workspace folder as a PLACEHOLDER only —
      // never as the field's value — so an untouched field stays empty ("no
      // override") and doesn't collapse per-run workspace isolation.
      if (d.default_workspace_dir) setDefaultWorkspaceDir(d.default_workspace_dir)
      // If ?applied set a pending selection, pick it up here
      const pending = appliedRef.current
      if (pending) {
        appliedRef.current = null
        const found = d.runs?.find((r: ProjectRun) => r.task_id === pending)
        if (found) { setSelectedRun(found); return }
      }
      setSelectedRun(prev => prev ? d.runs?.find((r: ProjectRun) => r.task_id === prev.task_id) || null : null)
    } finally { loadingRef.current = false }
  }, [])

  useEffect(() => {
    load()
    api.kirocrewAgents().then(d => { setAgents(d.agents || []); setDefaultAgentName(d.default_agent || '') }).catch(() => {})
    const iv = setInterval(load, 3000)
    return () => clearInterval(iv)
  }, [load, refreshTrigger])

  // Recovery: poll for planned run if isPlanning was restored from sessionStorage
  useEffect(() => {
    if (!isPlanning || activePlanRef.current) return
    let cancelled = false
    const poll = async () => {
      for (let i = 0; i < 60 && !cancelled; i++) {
        await new Promise(r => setTimeout(r, 2000))
        if (cancelled || !mountedRef.current) return
        try {
          const d = await api.taskRunnerStatus()
          if (!mountedRef.current || cancelled) return
          setData(d)
          const found = d.runs?.findLast((r: ProjectRun) => r.status === 'planned')
          if (found) { setSelectedRun(found); setIsPlanning(false); sessionStorage.removeItem('tr-planning'); return }
          const latest = d.runs?.[d.runs.length - 1]
          if (latest?.status === 'failed' && latest.error) { setPlanError(latest.error); setIsPlanning(false); sessionStorage.removeItem('tr-planning'); return }
        } catch { /* retry */ }
      }
      if (!cancelled && mountedRef.current) { setIsPlanning(false); setPlanError(i18nT('pages.projectsPage.planning_timed_out_try_again')); sessionStorage.removeItem('tr-planning') }
    }
    poll()
    return () => { cancelled = true }
  }, [isPlanning])  

  // Handle ?applied=TASK_ID from "Use as Plan" in chat
  useEffect(() => {
    const appliedId = searchParams.get('applied')
    if (!appliedId) return
    const autoRun = searchParams.get('autoRun') === 'true'
    setSearchParams({}, { replace: true })
    appliedRef.current = appliedId
    if (autoRun) autoRunRef.current = appliedId
    // If load() is already in flight, it will pick up appliedRef when it resolves.
    // Otherwise, trigger a fresh load.
    if (!loadingRef.current) load()
  }, [searchParams, setSearchParams, load])

  useEffect(() => { sessionStorage.setItem('tr-mode', mode) }, [mode])

  // Auto-execute when a planned run is selected via ?autoRun=true
  useEffect(() => {
    const id = autoRunRef.current
    if (!id || !selectedRun || selectedRun.task_id !== id || selectedRun.status !== 'planned') return
    autoRunRef.current = null
    // Auto-run is a programmatic launch, NOT an affirmative per-run trust grant for
    // THIS run — always pass false. (Per-run trust requires an explicit dashboard
    // toggle + manual Execute.) Passing the component-wide `autoApprove` here would
    // also race the sync effect below and could leak a stale `true` from a
    // previously-selected run onto this one. Workspace is fixed at plan time
    // (planTask baked it into work_dir), so execute never re-sends it.
    api.executePlan(selectedRun.task_id, agent, false).then(r => { if (r.ok) load() })
  }, [selectedRun, agent, load])
  // Sync the per-run auto-approve toggle from the selected run (default false).
  // Reflect only a LIVE trust grant (not stale persisted intent), so resuming a
  // paused/planned run — whose grant was torn down — shows unchecked and requires an
  // affirmative re-grant rather than a single click on a pre-checked box.
  useEffect(() => { setAutoApprove((selectedRun?.auto_approve_remaining_secs ?? 0) > 0) }, [selectedRun?.task_id, selectedRun?.auto_approve_remaining_secs])
  useEffect(() => { sessionStorage.setItem('tr-input', userInput) }, [userInput])
  useEffect(() => { sessionStorage.setItem('tr-spec', specText) }, [specText])
  useEffect(() => { sessionStorage.setItem('tr-yaml', yamlText) }, [yamlText])

  const doPlan = async (input: string, source: string, spec: string | undefined, autoRun: boolean) => {
    setIsPlanning(true); setPlanError(''); sessionStorage.setItem('tr-planning', '1'); activePlanRef.current = true
    try {
      const r = await api.planTask(input, source, spec, agent, workspaceDir)
      if (r.ok) {
        if (autoRun && r.task_id) autoRunRef.current = r.task_id
        const d = await api.taskRunnerStatus()
        setData(d)
        const planned = d.runs?.find((run: ProjectRun) => run.task_id === r.task_id)
        if (planned) setSelectedRun(planned)
      } else setPlanError(r.error || i18nT('pages.projectsPage.failed_to_generate_plan'))
    } catch (e) { setPlanError(e instanceof Error ? e.message : i18nT('pages.projectsPage.planning_request_failed'))
    } finally { sessionStorage.removeItem('tr-planning'); activePlanRef.current = false; if (mountedRef.current) setIsPlanning(false) }
  }

  const generatePlan = (input: string, source: string, spec?: string) => doPlan(input, source, spec, false)

  const cancelPlan = async () => {
    try { await api.cancelPlan() } finally { setIsPlanning(false); sessionStorage.removeItem('tr-planning') }
  }

  const handleRun = (input: string, source: string) => doPlan(input, source, '', true)


  const handleFileUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    if (/\.ya?ml$/i.test(file.name)) { alert(i18nT('pages.projectsPage.yaml_files_should_be_uploaded_via_the_from_yaml')); e.target.value = ''; return }
    const reader = new FileReader()
    reader.onload = () => setSpecText(reader.result as string)
    reader.readAsText(file)
    e.target.value = ''
  }

  const handleYamlUpload = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    if (!/\.ya?ml$/i.test(file.name)) { alert(i18nT('pages.projectsPage.only_yaml_yml_files_are_accepted_here_use_the_fr')); e.target.value = ''; return }
    const reader = new FileReader()
    reader.onload = () => setYamlText(reader.result as string)
    reader.readAsText(file)
    e.target.value = ''
  }

  const pollingRef = useRef(false)
  const pollRefine = useCallback(async () => {
    if (pollingRef.current) return
    pollingRef.current = true
    try {
      const r = await api.refineStatus()
      setRefined(r.text || ''); setRefineStatus(r.status || 'idle'); setRefineError(r.error || '')
      if (r.input && !userInput) setUserInput(r.input)
    } catch { setRefineStatus('idle') }
    finally { pollingRef.current = false }
  }, [userInput])

  useEffect(() => { if (mode === 'compose') pollRefine() }, []) // eslint-disable-line
  useEffect(() => { pollRefine() }, [refreshTrigger]) // eslint-disable-line

  const refine = async () => {
    if (!userInput.trim() || refineStatus === 'running') return
    setRefineStatus('running'); setRefined(''); setRefineError('')
    await api.refineTaskInput(userInput)
  }

  const isRefining = refineStatus === 'running'

  const runs = data?.runs || []
  const anyPlanning = isPlanning || runs.some(r => r.status === 'planning')

  const projectList = (
    <div className="flex flex-col gap-1.5">
      {runs.map(r => {
        const name = r.name || r.spec_name || r.task_id
        const icon: ReactNode = r.running ? <RefreshCw className="lucide-inline" /> : r.status === 'completed' ? <CheckCircle className="lucide-inline" /> : r.status === 'failed' ? <XCircle className="lucide-inline" /> : r.status === 'cancelled' ? <Square className="lucide-inline" /> : r.status === 'planned' ? <ClipboardList className="lucide-inline" /> : <Square className="lucide-inline" />
        const pct = r.steps > 0 ? Math.round((r.completed / r.steps) * 100) : 0
        const isActive = selectedRun?.task_id === r.task_id
        return (
          <div
            key={r.task_id}
            role="button"
            tabIndex={0}
            aria-label={i18nT('pages.projectsPage.open_project', { name })}
            className={`flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-all ${isActive ? 'bg-accent/15 border border-accent/40' : 'hover:bg-bg-elevated border border-transparent'}`}
            onClick={() => { setSelectedRun(r); setEditingName(false) }}
            onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setSelectedRun(r); setEditingName(false) } }}
          >
            <span className="text-[14px]">{icon}</span>
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-semibold text-text-strong truncate">{name}</div>
              <div className="text-[11px] text-muted">{r.task_id} · {r.completed}/{r.steps} · {r.running ? 'running' : r.status}</div>
            </div>
            <div className="w-10 h-1 bg-bg-elevated rounded-full overflow-hidden shrink-0">
              <div className={`h-full rounded-full ${r.status === 'failed' ? 'bg-danger' : 'bg-accent'}`} style={{ width: `${pct}%` }} />
            </div>
            <button aria-label={r.running ? i18nT('pages.projectsPage.cancel') : i18nT('pages.projectsPage.delete')} className="px-1 text-muted text-[11px] cursor-pointer hover:text-danger transition-all shrink-0 bg-transparent border-none" onClick={e => { e.stopPropagation(); (r.running ? api.cancelTaskRunner(r.task_id) : api.deleteTaskRun(r.task_id)).then(load) }}>{r.running ? <Square className="lucide-inline" /> : <X className="lucide-inline" />}</button>
          </div>
        )
      })}
    </div>
  )

  const composePanel = (
    <div className="px-5 py-4">
      <div className="flex items-center gap-1 mb-4">
        <button onClick={() => setMode('compose')} disabled={anyPlanning} className={`px-3 py-1.5 rounded-md text-[13px] font-semibold transition-all ${mode === 'compose' ? 'bg-accent text-accent-fg shadow-sm' : 'text-muted hover:text-text hover:bg-bg-elevated'} ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`}><Sparkles className="lucide-inline" /> {i18nT('pages.projectsPage.compose')}</button>
        <button onClick={() => setMode('spec')} disabled={anyPlanning} className={`px-3 py-1.5 rounded-md text-[13px] font-semibold transition-all ${mode === 'spec' ? 'bg-accent text-accent-fg shadow-sm' : 'text-muted hover:text-text hover:bg-bg-elevated'} ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`}><FileText className="lucide-inline" /> {i18nT('pages.projectsPage.from_spec')}</button>
        <button onClick={() => setMode('yaml')} disabled={anyPlanning} className={`px-3 py-1.5 rounded-md text-[13px] font-semibold transition-all ${mode === 'yaml' ? 'bg-accent text-accent-fg shadow-sm' : 'text-muted hover:text-text hover:bg-bg-elevated'} ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`}><Settings className="lucide-inline" /> {i18nT('pages.projectsPage.from_yaml')}</button>
      </div>
      <div className="flex gap-2 items-center mb-3">
        <span className="text-[13px] text-muted font-medium">{i18nT('pages.projectsPage.agent')}</span>
        <AgentSelector agents={agents} defaultAgent={defaultAgentName} value={agent} onChange={(name) => setAgent(name)} />
        <span className="text-[13px] text-muted font-medium ml-2">{i18nT('pages.projectsPage.workspace')}</span>
        <Input
          type="text"
          aria-label={i18nT('pages.projectsPage.workspace_folder')}
          value={workspaceDir}
          onChange={e => setWorkspaceDir(e.target.value)}
          placeholder={defaultWorkspaceDir || i18nT('pages.projectsPage.default_workspace_folder')}
          title={i18nT('pages.projectsPage.root_folder_for_a_new_plan_leave_blank_to_use_th')}
          disabled={anyPlanning}
          className={`min-w-[200px] px-2.5 py-1.5 text-[13px] font-mono ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`}
        />
      </div>
      {mode === 'compose' ? (
        <div className="space-y-3">
          <textarea aria-label={i18nT('pages.projectsPage.describe_your_task')} className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2.5 text-text text-sm font-body outline-none transition-colors focus-ring resize-y min-h-[80px]" rows={3} placeholder={i18nT('pages.projectsPage.describe_your_task_2')} value={userInput} onChange={e => setUserInput(e.target.value)} disabled={isRefining || anyPlanning} />
          <div className="flex gap-2 items-center">
            {!isRefining && <button className={`btn-sweep bg-accent text-accent-fg border-none rounded-lg px-4 h-9 text-sm font-semibold cursor-pointer hover:bg-accent-hover transition-all font-body ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`} onClick={refine} disabled={!userInput.trim() || anyPlanning}><Sparkles className="lucide-inline" /> {i18nT('pages.projectsPage.refine_into_spec')}</button>}
            {!isRefining && <button className={`px-4 h-9 rounded-md border border-accent bg-transparent text-accent text-sm font-semibold cursor-pointer font-body hover:bg-accent hover:text-accent-fg transition-all ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`} onClick={() => generatePlan(userInput, 'text')} disabled={!userInput.trim() || anyPlanning}>{anyPlanning ? <Hourglass className="lucide-inline" /> : <ClipboardList className="lucide-inline" />} {i18nT('pages.projectsPage.plan')}</button>}
            {!isRefining && <button className={`px-4 h-9 rounded-lg border-none bg-ok text-ok-fg text-sm font-semibold cursor-pointer font-body hover:brightness-110 transition-all ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`} onClick={() => handleRun(userInput, 'text')} disabled={!userInput.trim() || anyPlanning}><Play className="lucide-inline" /> {i18nT('pages.projectsPage.run')}</button>}
            {isRefining && <>
              <button className="px-4 h-9 rounded-md border border-border bg-transparent text-muted text-sm cursor-pointer font-body hover:text-danger hover:border-danger transition-all" onClick={async () => { await api.refineCancel(); setRefineStatus('cancelled') }}><Square className="lucide-inline" /> {i18nT('pages.projectsPage.cancel')}</button>
              <span className="text-accent text-[13px]">{i18nT('pages.projectsPage.refining')}</span>
              {/* Shimmer bar rather than a pulsing label: the motion lives in a
                  placeholder shape, and the text stays legible while it runs. */}
              <span className="skeleton h-1.5 w-24 rounded-full" aria-hidden="true" />
            </>}
          </div>
          {isPlanning && <PlanningBanner onCancel={cancelPlan} />}
          {planError && <div className="rounded-lg border border-danger/50 bg-danger/10 px-4 py-2.5 mt-2 text-danger text-[13px]">{planError}</div>}
          {(refined || isRefining) && (
            <div>
              <textarea aria-label={i18nT('pages.projectsPage.refined_spec')} className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2.5 text-text text-sm font-mono outline-none transition-colors focus-ring resize-y min-h-[120px]" rows={8} value={refined} onChange={e => setRefined(e.target.value)} readOnly={isRefining} />
              {refineError && <div className="text-danger mt-1 text-[13px]">{i18nT('pages.projectsPage.error')} {refineError}</div>}
              {!isRefining && refined && (
                <div className="flex gap-2 mt-2">
                  <button className={`btn-sweep bg-accent text-accent-fg border-none rounded-lg px-4 h-9 text-sm font-semibold cursor-pointer hover:bg-accent-hover transition-all font-body ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`} onClick={() => generatePlan(refined, 'spec')} disabled={anyPlanning}><ClipboardList className="lucide-inline" /> {i18nT('pages.projectsPage.plan_from_spec')}</button>
                  <button className={`px-4 h-9 rounded-lg border-none bg-ok text-ok-fg text-sm font-semibold cursor-pointer font-body hover:brightness-110 transition-all ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`} onClick={() => handleRun(refined, 'spec')} disabled={anyPlanning}><Play className="lucide-inline" /> {i18nT('pages.projectsPage.run')}</button>
                  <button className={`px-4 h-9 rounded-md border border-border bg-transparent text-muted text-sm cursor-pointer font-body hover:text-text hover:border-border-strong transition-all ${anyPlanning ? 'opacity-50 cursor-not-allowed' : ''}`} onClick={() => { setRefined(''); setRefineStatus('idle'); setRefineError('') }} disabled={anyPlanning}><X className="lucide-inline" /> {i18nT('pages.projectsPage.discard')}</button>
                </div>
              )}
            </div>
          )}
        </div>
      ) : mode === 'spec' ? (
        <TextInputPanel text={specText} setText={setSpecText} rows={6} placeholder={i18nT('pages.projectsPage.paste_spec_content_or_upload_a_file')} accept=".md,.txt" onUpload={handleFileUpload} onRun={() => handleRun(specText, 'spec')} onPlan={() => generatePlan(specText, 'spec')} disabled={anyPlanning} isPlanning={isPlanning} onCancel={cancelPlan} planError={planError} />
      ) : (
        <TextInputPanel text={yamlText} setText={setYamlText} rows={8} placeholder={i18nT('pages.projectsPage.paste_yaml_workflow_or_upload_a_yaml_file')} accept=".yaml,.yml" onUpload={handleYamlUpload} onRun={() => handleRun(yamlText, 'yaml')} onPlan={() => generatePlan(yamlText, 'yaml')} disabled={anyPlanning} isPlanning={isPlanning} onCancel={cancelPlan} planError={planError}
          banner={<div className="rounded-md border bg-bg-elevated px-3 py-2 text-[12px] text-text" style={{ borderColor: 'color-mix(in srgb, var(--accent) 40%, transparent)' }}><Settings className="lucide-inline" /> {i18nT('pages.projectsPage.yaml_workflows_bypass_the_llm_decomposer')} <code>{i18nT('pages.projectsPage.depends_on')}</code> {i18nT('pages.projectsPage.is_enforced_as_a_hard_dag_constraint')}</div>} />
      )}
    </div>
  )

  // Three-part workspace shell, matching Issue Radar: a resizable/collapsible
  // rail, its drag handle, then a flush main column. The rail is present in
  // every state (including "no runs yet") so the page never reflows out from
  // under the pointer the moment the first run appears, and the main column owns
  // its own padding rather than inheriting page gutters.
  return (
    <div className="flex h-full bg-bg text-text">
      {rail.collapsed ? (
        <CollapsedRail width={rail.width} onExpand={rail.expand} />
      ) : (
        <aside style={{ width: rail.width }} className="flex-shrink-0 flex flex-col min-h-0 border-r border-border">
          <div className="shrink-0 h-11 px-3 flex items-center gap-2 border-b border-border">
            <ClipboardCheck className="lucide-inline text-accent" />
            <span className="text-[13px] font-semibold text-text-strong truncate min-w-0">{i18nT('pages.projectsPage.task_runner')}</span>
          </div>
          <div className="shrink-0 px-3 pt-3">
            <button onClick={() => setSelectedRun(null)} className="w-full px-3 py-2 rounded-lg text-[13px] font-semibold border cursor-pointer transition-all text-accent bg-accent/10 border-accent/30 hover:bg-accent/20"><Plus className="lucide-inline" /> {i18nT('pages.projectsPage.new_task')}</button>
          </div>
          <div className="flex-1 min-h-0 overflow-y-auto p-3">
            {runs.length > 0
              ? projectList
              : <div className="text-[12px] text-muted px-1">{i18nT('pages.projectsPage.no_runs_yet')}</div>}
          </div>
        </aside>
      )}

      {/* Drag handle — resize the run rail. Dragging well past the minimum collapses it. */}
      <ResizeHandle
        handleProps={rail.handleProps}
        label={i18nT('pages.projectsPage.resize_sidebar')}
        onNudge={rail.nudge}
        value={rail.width}
        min={MIN_RAIL_WIDTH}
        max={MAX_RAIL_WIDTH}
      />

      <main className="flex-1 min-w-0 min-h-0 flex flex-col">
        {selectedRun ? (
          <>
            <div className="px-4 py-2 flex items-center gap-2 border-b border-border shrink-0">
              {editingName ? (
                <input aria-label={i18nT('pages.projectsPage.project_name')} className="text-[13px] font-semibold bg-transparent border border-accent rounded px-1 py-0 text-text-strong outline-none min-w-[120px]" autoFocus maxLength={200} value={editNameValue} onChange={e => setEditNameValue(e.target.value)} onBlur={() => { const v = editNameValue.trim(); if (v && v !== (selectedRun.name || selectedRun.spec_name || '')) { api.renameTaskRun(selectedRun.task_id, v).then(load).catch(() => {}) }; setEditingName(false) }} onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); else if (e.key === 'Escape') setEditingName(false) }} />
              ) : (
                <span
                  role="button"
                  tabIndex={0}
                  aria-label={i18nT('pages.projectsPage.rename_project')}
                  className="text-[13px] font-semibold text-text-strong truncate cursor-pointer hover:text-accent transition-all"
                  onClick={() => { setEditingName(true); setEditNameValue(selectedRun.name || selectedRun.spec_name || 'Project') }}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setEditingName(true); setEditNameValue(selectedRun.name || selectedRun.spec_name || 'Project') } }}
                >{selectedRun.name || selectedRun.spec_name || i18nT('pages.projectsPage.project')}</span>
              )}
              {!editingName && <span
                role="button"
                tabIndex={0}
                className="text-[11px] text-muted cursor-pointer opacity-40 hover:opacity-100 hover:text-accent transition-all"
                title={i18nT('pages.projectsPage.rename_project')}
                aria-label={i18nT('pages.projectsPage.rename_project')}
                onClick={() => { setEditingName(true); setEditNameValue(selectedRun.name || selectedRun.spec_name || 'Project') }}
                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setEditingName(true); setEditNameValue(selectedRun.name || selectedRun.spec_name || 'Project') } }}
              ><Pencil className="lucide-inline" /></span>}
              <span className="text-[12px] text-muted">{selectedRun.status === 'planning' ? <><Hourglass className="lucide-inline" /> {i18nT('pages.projectsPage.planning')}</> : selectedRun.running ? <><RefreshCw className="lucide-inline" /> {i18nT('pages.projectsPage.running')}</> : selectedRun.status}</span>
              <div className="flex-1" />
              {selectedRun.status === 'planned' && <>
                <label className="flex items-center gap-1.5 text-[12px] text-muted cursor-pointer select-none" title={i18nT('pages.projectsPage.run_unattended_auto_approve_this_run_s_tool_call')}>
                  <Checkbox checked={autoApprove} onChange={e => setAutoApprove(e.target.checked)} />
                  {i18nT('pages.projectsPage.auto_approve_tool_calls')}
                </label>
                <button className="btn-sweep bg-accent text-accent-fg border-none rounded-lg px-4 h-8 text-[13px] font-semibold cursor-pointer hover:bg-accent-hover transition-all" onClick={async () => { const r = await api.executePlan(selectedRun.task_id, agent, autoApprove); if (r.ok) load() }}><Play className="lucide-inline" /> {i18nT('pages.projectsPage.execute')}</button>
                <button className="px-3 h-8 rounded-md border border-border text-muted text-[13px] cursor-pointer hover:text-accent hover:border-accent transition-all" onClick={async () => { const res = await api.planContext(selectedRun.task_id); if (res.ok && res.context) { dispatch(setPendingInput("Let's optimize this plan:\n\n" + res.context)); navigate('/chat?autoSend=1&newSession=1') } }}><MessageSquare className="lucide-inline" /> {i18nT('pages.projectsPage.chat')}</button>
                <button className="px-3 h-8 rounded-md border border-border text-muted text-[13px] cursor-pointer hover:text-danger hover:border-danger transition-all" onClick={async () => { await api.deleteTaskRun(selectedRun.task_id); setSelectedRun(null); load() }}><X className="lucide-inline" /> {i18nT('pages.projectsPage.discard')}</button>
              </>}
              {selectedRun.status === 'planning' && <button className="px-3 h-8 rounded-md border border-border text-muted text-[13px] cursor-pointer hover:text-danger hover:border-danger transition-all" onClick={async () => { await api.cancelPlan(); setSelectedRun(null) }}><X className="lucide-inline" /> {i18nT('pages.projectsPage.cancel')}</button>}
              {selectedRun.running && <button className="px-3 h-8 rounded-md border border-border text-muted text-[13px] cursor-pointer hover:text-warning hover:border-warning transition-all" onClick={async () => { await api.pauseTaskRun(selectedRun.task_id); load() }}><Pause className="lucide-inline" /> {i18nT('pages.projectsPage.pause')}</button>}
              {selectedRun.running && <button className="px-3 h-8 rounded-md border border-border text-muted text-[13px] cursor-pointer hover:text-danger hover:border-danger transition-all" onClick={async () => { await api.cancelTaskRunner(selectedRun.task_id); load() }}><Square className="lucide-inline" /> {i18nT('pages.projectsPage.cancel')}</button>}
              {!selectedRun.running && selectedRun.status !== 'planned' && selectedRun.status !== 'planning' && <>
                {selectedRun.status === 'paused' && (
                  <>
                    <label className="flex items-center gap-1.5 text-[12px] text-muted cursor-pointer select-none" title={i18nT('pages.projectsPage.run_unattended_auto_approve_this_run_s_tool_call')}>
                      <Checkbox checked={autoApprove} onChange={e => setAutoApprove(e.target.checked)} />
                      {i18nT('pages.projectsPage.auto_approve_tool_calls')}
                    </label>
                    <button className="btn-sweep bg-accent text-accent-fg border-none rounded-lg px-4 h-8 text-[13px] font-semibold cursor-pointer hover:bg-accent-hover transition-all" onClick={async () => { const r = await api.executePlan(selectedRun.task_id, agent, autoApprove); if (r.ok) load() }}><Play className="lucide-inline" /> {i18nT('pages.projectsPage.resume')}</button>
                  </>
                )}
                {(selectedRun.status === 'completed' || selectedRun.status === 'cancelled') && (
                  <button className="px-3 h-8 rounded-md border border-accent bg-transparent text-accent text-[13px] font-semibold cursor-pointer hover:bg-accent hover:text-accent-fg transition-all" onClick={async () => {
                    const res = await api.taskRunToChat(selectedRun.task_id)
                    if (res.slot) { dispatch(switchSlot(res.slot)); navigate('/chat') }
                  }}><MessageSquare className="lucide-inline" /> {i18nT('pages.projectsPage.chat')}</button>
                )}
                {selectedRun.status !== 'paused' && <button className="px-3 h-8 rounded-md border border-accent bg-transparent text-accent text-[13px] font-semibold cursor-pointer hover:bg-accent hover:text-accent-fg transition-all" onClick={async () => { await api.retryTaskRun(selectedRun.task_id, 1); load() }}><RotateCcw className="lucide-inline" /> {i18nT('pages.projectsPage.restart')}</button>}
                <button className="px-3 h-8 rounded-md border border-border text-muted text-[13px] cursor-pointer hover:text-accent hover:border-accent transition-all" onClick={async () => {
                  const name = selectedRun.name || selectedRun.spec_name || selectedRun.task_id
                  const spec = selectedRun.spec_content || selectedRun.original_input || ''
                  if (!spec) { alert(i18nT('pages.projectsPage.no_spec_idea_to_schedule')); return }
                  await api.createCron({ name: `Project: ${name}`, message: `run __inline__:${spec}`, every: 86400 })
                  alert(i18nT('pages.projectsPage.scheduled_as_daily_cron_job'))
                }}><Clock className="lucide-inline" /> {i18nT('pages.projectsPage.schedule')}</button>
              </>}
            </div>
            <div className="flex-1 min-h-0 min-w-0 flex">
              <ProjectDetailPage run={selectedRun} onRetry={async (idx) => { await api.retryTaskRun(selectedRun.task_id, idx); load() }} onRefresh={load} />
            </div>
          </>
        ) : (
          <div className="flex-1 min-h-0 overflow-y-auto">
            {composePanel}
          </div>
        )}
      </main>
    </div>
  )
}

/** The rail turned on its side: app mark plus the name rotated, and the whole
 * strip is the button that reopens it. Mirrors Issue Radar's collapsed rail so a
 * collapsed column looks the same wherever you meet one. */
function CollapsedRail({ width, onExpand }: { width: number; onExpand?: () => void }) {
  return (
    <aside style={{ width }} className="flex-shrink-0 flex flex-col min-h-0 py-2 px-1">
      <div className="flex-1 min-h-0 flex flex-col overflow-hidden rounded-xl border border-border-strong bg-bg-elevated shadow-sm">
        <button
          type="button"
          onClick={onExpand}
          title={i18nT('pages.projectsPage.expand_sidebar')}
          aria-label={i18nT('pages.projectsPage.expand_sidebar')}
          className="flex-1 min-h-0 w-full flex flex-col items-center gap-3 py-3.5 cursor-pointer text-muted hover:text-text hover:bg-bg-hover transition-colors bg-transparent border-none focus-ring"
        >
          <ClipboardCheck size={18} className="flex-shrink-0 text-accent" />
          {/* Rotated clockwise (writing-mode alone) so the name reads top-to-bottom
              starting under the mark, and truncates at the strip's height rather
              than overflowing it. */}
          <span
            className="min-h-0 max-w-full overflow-hidden text-ellipsis whitespace-nowrap text-[13px] font-medium tracking-[.02em]"
            style={{ writingMode: 'vertical-rl' }}
          >
            {i18nT('pages.projectsPage.task_runner')}
          </span>
        </button>
      </div>
    </aside>
  )
}

function PlanningBanner({ onCancel }: { onCancel: () => void }) {
  const [dots, setDots] = useState('')
  useEffect(() => {
    const iv = setInterval(() => setDots(d => d.length >= 3 ? '' : d + '.'), 500)
    return () => clearInterval(iv)
  }, [])
  return (
    <div className="relative overflow-hidden rounded-lg border border-accent/50 bg-accent/10 px-4 py-3 mt-2">
      <div className="absolute inset-0 bg-gradient-to-r from-transparent via-accent/10 to-transparent animate-shimmer" />
      <div className="relative flex items-center gap-3">
        {/* Static glyph: the shimmer sweep and the ticking dots already carry the
            "work in flight" signal, so nothing here needs to spin. */}
        <Hourglass size={18} className="text-accent shrink-0" />
        <div className="flex-1">
          <div className="text-accent text-[14px] font-semibold">{i18nT('pages.projectsPage.generating_execution_plan')}{dots}</div>
          <div className="text-muted text-[12px] mt-0.5">{i18nT('pages.projectsPage.analyzing_task_and_building_step_by_step_plan')}</div>
        </div>
        <button className="px-3 h-7 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer font-body hover:text-danger hover:border-danger transition-all shrink-0" onClick={onCancel}><X className="lucide-inline" /> {i18nT('pages.projectsPage.cancel')}</button>
      </div>
    </div>
  )
}
