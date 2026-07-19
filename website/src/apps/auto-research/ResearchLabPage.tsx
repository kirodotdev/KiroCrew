import { useState, useEffect, useReducer, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, Play, Pause, Square, MessageCircle, ChevronDown, ChevronRight, Sparkles, ThumbsUp, ArrowRight, HelpCircle, XCircle, CheckCircle, AlertTriangle, Lock, X, Trash2, GitFork, Flame, BookOpen, FileText, RefreshCw, ExternalLink, Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import Clickable from '../../components/Clickable'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import GrillTree from './GrillTree'
import { grillReducer, promotedResearch, answeredClarifiers, suggestedMaxCycles, GrillNode } from './grillTreeModel'

const ACTIVE_STATUSES = ['running', 'paused', 'stagnant', 'needs_input']

interface Campaign { id: string; name: string; question: string; sub_questions: string; sources: string; max_cycles: number; idle_secs: number; status: string; total_cycles: number; findings?: Finding[]; error_message?: string; pending_question?: string; parent_id?: string; parallel_workers?: number }
interface Finding { cycle: number; summary: string; sources_checked: string[]; sources_empty: string[]; new_findings_count: number; evidence_strength: string; key_insight: string; verification?: { passed: boolean; detail?: string } }
interface Validation { can_start: boolean; errors: string[]; warnings: string[]; estimated_cycles: number; estimated_duration_min: number }

// Auto-growing, manually-resizable textarea used for sub-question / guidance
// entry. Grows with content (so multi-line input is fully visible) and supports
// Enter-to-submit / Shift+Enter-for-newline when an onSubmit handler is given.
function GrowTextarea({ value, onChange, onSubmit, placeholder, className = '', ariaLabel }: {
  value: string
  onChange: (v: string) => void
  onSubmit?: () => void
  placeholder?: string
  className?: string
  ariaLabel?: string
}) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const el = ref.current
    if (el) {
      el.style.height = 'auto'
      el.style.height = `${el.scrollHeight}px`
    }
  }, [value])
  return (
    <textarea
      ref={ref}
      rows={1}
      aria-label={ariaLabel}
      className={`resize-none overflow-hidden ${className}`}
      value={value}
      placeholder={placeholder}
      onChange={e => onChange(e.target.value)}
      onKeyDown={e => {
        if (onSubmit && e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault()
          onSubmit()
        }
      }}
    />
  )
}

function EvidenceBadge({ s }: { s: string }) {
  if (s === 'strong') return <span className="text-xs px-1.5 py-0.5 rounded bg-bg-elevated text-ok inline-flex items-center gap-0.5"><ThumbsUp size={10} /> Strong</span>
  if (s === 'moderate') return <span className="text-xs px-1.5 py-0.5 rounded bg-bg-elevated text-warn inline-flex items-center gap-0.5"><ArrowRight size={10} /> Moderate</span>
  return <span className="text-xs px-1.5 py-0.5 rounded bg-bg-elevated text-muted inline-flex items-center gap-0.5"><HelpCircle size={10} /> Weak</span>
}

// Maps every campaign status to a single, consistent state pill so the root
// list communicates working / failed / done at a glance. Unknown statuses fall
// back to a neutral pill showing the raw status text.
const STATE_META: Record<string, { label: string; color: string; Icon: typeof CheckCircle; spin?: boolean }> = {
  running: { label: 'Working', color: 'text-accent', Icon: Loader2, spin: true },
  needs_input: { label: 'Needs input', color: 'text-warn', Icon: HelpCircle },
  paused: { label: 'Paused', color: 'text-muted', Icon: Pause },
  stagnant: { label: 'Stalled', color: 'text-warn', Icon: AlertTriangle },
  ready: { label: 'Ready', color: 'text-muted', Icon: Play },
  complete: { label: 'Done', color: 'text-ok', Icon: CheckCircle },
  failed: { label: 'Failed', color: 'text-danger', Icon: XCircle },
  stopped: { label: 'Stopped', color: 'text-muted', Icon: Square },
}

function StateBadge({ status }: { status: string }) {
  const m = STATE_META[status] ?? { label: status.replace(/_/g, ' '), color: 'text-muted', Icon: HelpCircle }
  const { label, color, Icon, spin } = m
  return (
    <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded bg-bg-elevated inline-flex items-center gap-1 shrink-0 ${color}`} title={`Status: ${status}`}>
      <Icon size={10} className={spin ? 'animate-spin motion-reduce:animate-none' : undefined} /> {label}
    </span>
  )
}

function FindingCard({ f }: { f: Finding }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border border-border rounded-md p-3 mb-2 bg-card">
      <Clickable className="flex items-start gap-2" onClick={() => setOpen(!open)}>
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-xs text-muted">Cycle {f.cycle}</span>
            <EvidenceBadge s={f.evidence_strength} />
            {f.verification && <span className={`text-xs px-1.5 py-0.5 rounded inline-flex items-center gap-0.5 ${f.verification.passed ? 'bg-ok/15 text-ok' : 'bg-bg-elevated text-muted'}`}>{f.verification.passed ? <><CheckCircle size={10} /> Goal met</> : 'Goal: not yet'}</span>}
          </div>
          <div className="text-sm font-medium mt-0.5">&ldquo;{f.key_insight || f.summary}&rdquo;</div>
        </div>
      </Clickable>
      {open && (
        <div className="mt-2 pl-5 text-sm space-y-1">
          <p className="text-muted">{f.summary}</p>
          {f.sources_checked?.length > 0 && <div><span className="text-xs font-medium text-muted">Sources:</span>{f.sources_checked.map((s, i) => <div key={i} className="text-xs ml-2">• {s}</div>)}</div>}
          {f.sources_empty?.length > 0 && <div><span className="text-xs font-medium text-muted">Searched (empty):</span>{f.sources_empty.map((s, i) => <div key={i} className="text-xs ml-2 italic">• {s}</div>)}</div>}
        </div>
      )}
    </div>
  )
}

function SetupWizard({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [step, setStep] = useState(0)
  const [question, setQuestion] = useState('')
  const [subQs, setSubQs] = useState<string[]>([])
  const [newSub, setNewSub] = useState('')
  const [maxCycles, setMaxCycles] = useState(30)
  const [maxCyclesTouched, setMaxCyclesTouched] = useState(false)
  const [idleSecs, setIdleSecs] = useState(120)
  const [successCriteria, setSuccessCriteria] = useState('')
  const [autoApprove, setAutoApprove] = useState(false)
  const [parallelWorkers, setParallelWorkers] = useState(1)
  const [executionMode, setExecutionMode] = useState<'agent' | 'workflow'>('agent')
  const [validation, setValidation] = useState<Validation | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tree, dispatchTree] = useReducer(grillReducer, [] as GrillNode[])
  const [grilling, setGrilling] = useState(false)
  const [grillUnavailable, setGrillUnavailable] = useState(false)

  // Combined committed sub-questions: grill-promoted (depth-first, origin-tagged)
  // + manually-added ones. Scope constraints come from answered clarifiers.
  const buildSubs = () => [
    ...promotedResearch(tree),
    ...subQs.map(t => ({ text: t, origin: 'manual' })),
  ]
  const scopeConstraints = answeredClarifiers(tree)
  const subCount = promotedResearch(tree).length + subQs.length

  const grillMe = async () => {
    setGrilling(true); setGrillUnavailable(false)
    try {
      const r = await api.researchGrillExpand({ question, tree: [], node_id: null, mode: 'generate' })
      const nodes = r?.nodes || []
      if (nodes.length) dispatchTree({ type: 'addChildren', nodes })
      else setGrillUnavailable(true)
    } catch { setGrillUnavailable(true) }
    finally { setGrilling(false) }
  }

  const onExpand = async (nodeId: string) => {
    const r = await api.researchGrillExpand({ question, tree, node_id: nodeId, mode: 'generate' })
    if (r?.nodes?.length) dispatchTree({ type: 'addChildren', nodes: r.nodes })
    return { reason: r?.reason }
  }

  // Pre-fill max_cycles from committed sub-question count when reaching Limits,
  // unless the user has already edited it.
  useEffect(() => {
    if (step === 1 && !maxCyclesTouched && subCount > 0) setMaxCycles(suggestedMaxCycles(subCount))
  }, [step])  // eslint-disable-line react-hooks/exhaustive-deps

  const validate = async () => {
    setError(null)
    try {
      const r: Validation = await api.researchValidate({ question, sub_questions: buildSubs(), max_cycles: maxCycles })
      setValidation(r)
    } catch {
      setError('Validation failed — check your connection and try again.')
    }
  }
  useEffect(() => { if (step === 2) { validate() } }, [step])  // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const c = await api.researchCreate({ question, sub_questions: buildSubs(), scope_constraints: scopeConstraints, max_cycles: maxCycles, idle_secs: idleSecs, success_criteria: successCriteria, auto_approve: autoApprove, parallel_workers: parallelWorkers, execution_mode: executionMode })
      if (c?.id) { await api.researchAction(c.id, 'start'); onDone() }
    } catch {
      setError('Failed to start campaign — please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  const steps = ['Question', 'Limits', 'Review']
  return (
    <div className="max-w-2xl mx-auto">
      <div className="flex items-center gap-1 mb-6">{steps.map((s, i) => (
        <div key={s} className="flex items-center gap-1">
          <div className={`w-6 h-6 rounded-full flex items-center justify-center text-xs ${i <= step ? 'bg-accent text-accent-fg font-bold' : 'bg-border text-muted'}`}>{i + 1}</div>
          <span className={`text-xs ${i === step ? 'text-text' : 'text-muted'}`}>{s}</span>
          {i < 2 && <div className="w-8 h-px bg-border" />}
        </div>
      ))}</div>

      {step === 0 && <div className="space-y-4">
        <div className="p-3 rounded-md bg-bg border border-border">
          <div className="text-xs text-muted mb-2">How sub-agent execution is orchestrated. Both handle open-ended research. Choose once at setup.</div>
          <div className="grid grid-cols-2 gap-2">
            <button type="button" onClick={() => setExecutionMode('agent')} className={`text-left p-2 rounded border ${executionMode === 'agent' ? 'border-accent bg-accent/10' : 'border-border'}`}>
              <div className="font-medium text-sm">Agent <span className="text-muted font-normal">(adaptive)</span></div>
              <div className="text-xs text-muted mt-0.5">The AI drives every round itself — deciding what to investigate, managing sub-agents, and following new leads from findings as they emerge.</div>
            </button>
            <button type="button" onClick={() => setExecutionMode('workflow')} className={`text-left p-2 rounded border ${executionMode === 'workflow' ? 'border-accent bg-accent/10' : 'border-border'}`}>
              <div className="font-medium text-sm">Dynamic Workflow <span className="text-muted font-normal">(scripted)</span></div>
              <div className="text-xs text-muted mt-0.5">The AI writes an orchestration script up front; a deterministic runner manages sub-agent execution while the AI only plans, investigates, and synthesizes. Replayable, budget-capped.</div>
            </button>
          </div>
          {executionMode === 'workflow' && (
            <div className="text-xs text-warn mt-2 flex items-start gap-1">
              <AlertTriangle size={12} className="mt-0.5 shrink-0" />
              <span>Dynamic Workflow can fan out to many sub-agents, and its budget cap stops the run hard if hit (mid-run synthesis may be lost). Start with conservative cycle/worker limits.</span>
            </div>
          )}
        </div>
        <div>
          <label htmlFor="research-question" className="text-sm font-medium">What do you want to research?
            <textarea id="research-question" aria-label="What do you want to research?" className="w-full mt-1 p-2 rounded-md text-sm bg-bg border border-border resize-y" rows={3} value={question} onChange={e => setQuestion(e.target.value)} placeholder="How do other teams handle API rate limiting?" />
          </label>
          <div className="text-xs text-muted">Min 20 characters.</div>
        </div>
        <div>
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">Sub-questions</span>
            <button className="text-xs text-accent flex items-center gap-1 disabled:opacity-50" onClick={grillMe} disabled={question.length < 20 || grilling}><Sparkles size={12} /> {grilling ? 'Grilling…' : 'Grill me →'}</button>
          </div>
          {tree.some(n => n.status !== 'pruned') && <div className="text-xs text-muted mt-1">Answer clarifiers to refine, or just pick sub-questions and go.</div>}
          <div className="mt-2"><GrillTree tree={tree} dispatch={dispatchTree} onExpand={onExpand} /></div>
          {grillUnavailable && <div className="text-xs text-warn mt-2">Grill unavailable — add sub-questions manually below.</div>}
          {subQs.map((sq, i) => <div key={i} className="flex items-start gap-2 mt-1"><GrowTextarea ariaLabel={`Sub-question ${i + 1}`} className="flex-1 text-sm p-1.5 rounded bg-bg border border-border" value={sq} onChange={v => { const n = [...subQs]; n[i] = v; setSubQs(n) }} /><button className="text-xs text-danger mt-1.5" onClick={() => setSubQs(subQs.filter((_, j) => j !== i))} aria-label="Remove sub-question"><X size={12} /></button></div>)}
          <GrowTextarea ariaLabel="Add sub-question manually" className="w-full text-sm p-1.5 rounded bg-bg border border-border mt-2" placeholder="Add sub-question manually (Enter; Shift+Enter for newline)" value={newSub} onChange={setNewSub} onSubmit={() => { if (newSub.trim()) { setSubQs([...subQs, newSub.trim()]); setNewSub('') } }} />
        </div>
      </div>}

      {step === 1 && <div className="space-y-4">
        <span className="text-sm font-medium block">When should the agent stop?</span>
        <div className="text-xs text-muted">Stops at the cycle cap, when the Definition of Done is met, on stagnation, or when you Stop it.</div>
        <div className="flex items-center gap-2"><span className="text-sm">Max cycles:</span><input type="number" aria-label="Max cycles" min={5} max={100} value={maxCycles} className="w-20 text-sm p-1 rounded bg-bg border border-border" onChange={e => { setMaxCyclesTouched(true); setMaxCycles(Number(e.target.value)) }} />{subCount > 0 && !maxCyclesTouched && <span className="text-xs text-muted">suggested from {subCount} sub-questions</span>}</div>
        <div className="flex items-center gap-2"><span className="text-sm">Idle between cycles:</span><select aria-label="Idle between cycles" value={idleSecs} onChange={e => setIdleSecs(Number(e.target.value))} className="text-sm p-1 rounded bg-bg border border-border"><option value={30}>30s</option><option value={60}>60s</option><option value={120}>120s</option></select></div>
        <div>
          <span className="text-sm font-medium block">Definition of Done (optional)</span>
          <textarea aria-label="Definition of Done (optional)" className="w-full text-sm p-1.5 rounded bg-bg border border-border mt-1 resize-y" rows={2} placeholder="e.g. AI code review finds no blocking issues and the test build passes" value={successCriteria} onChange={e => setSuccessCriteria(e.target.value)} />
          <div className="text-xs text-muted mt-1">If set, the agent verifies against this each cycle and completes when met.</div>
        </div>
        <label htmlFor="auto-approve" className="flex items-center gap-2 text-sm">
          <input id="auto-approve" type="checkbox" aria-label="Run unattended (skip clarification questions)" checked={autoApprove} onChange={e => setAutoApprove(e.target.checked)} />
          Run unattended (skip clarification questions)
        </label>
        <div className="flex items-center gap-2"><span className="text-sm">Parallel workers:</span><input type="number" aria-label="Parallel workers" min={1} max={5} value={parallelWorkers} className="w-16 text-sm p-1 rounded bg-bg border border-border" onChange={e => setParallelWorkers(Math.min(5, Math.max(1, Number(e.target.value))))} /><span className="text-xs text-muted">{parallelWorkers > 1 ? `${parallelWorkers} sub-questions investigated in parallel each cycle` : 'sequential (default)'}</span></div>
      </div>}

      {step === 2 && <div className="space-y-3">
        <span className="text-sm font-medium block">Pre-flight Check:</span>
        {validation ? <>
          {validation.errors.map((e, i) => <div key={i} className="text-sm flex items-center gap-1"><XCircle size={14} className="text-danger" /> {e}</div>)}
          {validation.errors.length === 0 && <div className="text-sm text-ok flex items-center gap-1"><CheckCircle size={14} /> All checks passed</div>}
          {validation.warnings.map((w, i) => <div key={i} className="text-sm text-warn flex items-center gap-1"><AlertTriangle size={14} /> {w}</div>)}
          <div className="mt-3 p-3 rounded text-sm bg-bg border border-border">
            <div>Research &ldquo;{question.slice(0, 50)}{question.length > 50 ? '...' : ''}&rdquo;</div>
            <div className="text-muted">Up to {maxCycles} cycles, {idleSecs}s idle. ~{validation.estimated_duration_min} min</div>
            {successCriteria && <div className="text-muted">Done when: {successCriteria}</div>}
          </div>
        </> : <div className="text-sm text-muted">Validating...</div>}
        {error && <div className="text-sm text-danger flex items-center gap-1"><XCircle size={14} /> {error}</div>}
      </div>}

      <div className="flex justify-between mt-6">
        <button className="text-sm text-muted hover:text-text" onClick={step === 0 ? onCancel : () => setStep(step - 1)}>{step === 0 ? 'Cancel' : '← Back'}</button>
        {step < 2 ? <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={step === 0 && question.length < 20} onClick={() => setStep(step + 1)}>Next →</button>
          : <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={!validation?.can_start || submitting} onClick={submit}>{submitting ? 'Starting...' : 'Start Campaign'}</button>}
      </div>
    </div>
  )
}

// Persist the in-progress challenge across remounts/reloads. The dashboard can
// remount this view (e.g. on a WebSocket reconnect after the tab was backgrounded),
// which would otherwise wipe the local challenge tree. sessionStorage keyed by
// parentId survives both a remount and a full reload.
const FORK_TREE_KEY = (pid: string) => `mc-fork-tree:${pid}`
const FORK_PENDING_KEY = (pid: string) => `mc-fork-pending:${pid}`

function loadForkTree(pid: string): GrillNode[] {
  try {
    const raw = sessionStorage.getItem(FORK_TREE_KEY(pid))
    const v = raw ? JSON.parse(raw) : []
    return Array.isArray(v) ? (v as GrillNode[]) : []
  } catch { return [] }
}

function ForkFlow({ parentId, onCancel, onDone }: { parentId: string; onCancel: () => void; onDone: () => void }) {
  const [tree, dispatchTree] = useReducer(grillReducer, parentId, loadForkTree)
  const [grilling, setGrilling] = useState(false)
  const [forking, setForking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [manualSubs, setManualSubs] = useState<string[]>([])
  const [newSub, setNewSub] = useState('')
  const { data: parentCampaign } = useQuery<Campaign>({ queryKey: ['research-campaign', parentId], queryFn: () => api.researchCampaign(parentId) })

  const question = parentCampaign?.question || ''

  const clearPersisted = () => {
    try {
      sessionStorage.removeItem(FORK_TREE_KEY(parentId))
      sessionStorage.removeItem(FORK_PENDING_KEY(parentId))
    } catch { /* ignore */ }
  }

  // Mirror the tree into sessionStorage on every change so a remount rehydrates it.
  useEffect(() => {
    try {
      if (tree.length) sessionStorage.setItem(FORK_TREE_KEY(parentId), JSON.stringify(tree))
      else sessionStorage.removeItem(FORK_TREE_KEY(parentId))
    } catch { /* sessionStorage unavailable */ }
  }, [tree, parentId])

  const startChallenge = async () => {
    setGrilling(true)
    setError(null)
    try { sessionStorage.setItem(FORK_PENDING_KEY(parentId), '1') } catch { /* ignore */ }
    try {
      const r = await api.researchGrillExpand({ question, tree: [], node_id: null, mode: 'challenge', campaign_id: parentId })
      if (r?.nodes?.length) dispatchTree({ type: 'addChildren', nodes: r.nodes })
    } catch { setError('Could not generate challenges. Please try again.') }
    finally {
      setGrilling(false)
      try { sessionStorage.removeItem(FORK_PENDING_KEY(parentId)) } catch { /* ignore */ }
    }
  }

  // If a challenge was loading when the view remounted (tab-away mid-load), resume
  // it once the parent question is available — otherwise the user drops back to the
  // start button with the in-flight request lost.
  useEffect(() => {
    if (grilling || tree.length || !question) return
    let pending = false
    try { pending = sessionStorage.getItem(FORK_PENDING_KEY(parentId)) === '1' } catch { /* ignore */ }
    if (pending) void startChallenge()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question, tree.length, grilling, parentId])

  const onExpand = async (nodeId: string) => {
    const r = await api.researchGrillExpand({ question, tree, node_id: nodeId, mode: 'challenge', campaign_id: parentId })
    if (r?.nodes?.length) dispatchTree({ type: 'addChildren', nodes: r.nodes })
    return { reason: r?.reason }
  }

  const doFork = async () => {
    setForking(true)
    setError(null)
    try {
      const subs = [
        ...promotedResearch(tree),
        ...manualSubs.map(t => ({ text: t, origin: 'manual' })),
      ]
      const constraints = answeredClarifiers(tree)
      const maxCycles = suggestedMaxCycles(subs.length)
      const r = await api.researchAction(parentId, 'fork', {
        sub_questions: subs, scope_constraints: constraints, max_cycles: maxCycles,
        grill_tree: tree, question,
      })
      if (r?.id) {
        // Fork created. Try to start it, but navigate away regardless — a
        // retry after a successful fork would create a duplicate campaign.
        // If start failed, the unstarted campaign is on the list to start.
        try { await api.researchAction(r.id, 'start') } catch { /* start failed; campaign exists unstarted */ }
        clearPersisted()
        onDone()
      } else setError('Fork failed: no campaign was created.')
    } catch { setError('Fork failed. Please try again.') }
    finally { setForking(false) }
  }

  const subCount = promotedResearch(tree).length + manualSubs.length

  return <div className="max-w-2xl mx-auto space-y-4">
    <div className="text-sm text-muted">Challenge the findings from "{question?.slice(0, 60)}…", then fork into a new campaign.</div>
    {error && <div className="text-xs text-danger">{error}</div>}
    {tree.length === 0 ? (
      <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={grilling || !question} onClick={startChallenge}>{grilling ? 'Challenging…' : <><Flame size={12} className="inline" /> Challenge Findings →</>}</button>
    ) : (
      <>
        <div className="text-xs text-muted">Answer challenges to refine, or just pick sub-questions and fork.</div>
        <GrillTree tree={tree} dispatch={dispatchTree} onExpand={onExpand} />
        <div className="mt-3">
          {manualSubs.map((sq, i) => <div key={i} className="flex items-start gap-2 mt-1"><GrowTextarea ariaLabel={`Sub-question ${i + 1}`} className="flex-1 text-sm p-1.5 rounded bg-bg border border-border" value={sq} onChange={v => { const n = [...manualSubs]; n[i] = v; setManualSubs(n) }} /><button className="text-xs text-danger mt-1.5" onClick={() => setManualSubs(manualSubs.filter((_, j) => j !== i))} aria-label="Remove sub-question"><X size={12} /></button></div>)}
          <GrowTextarea ariaLabel="Add your own sub-question or guidance" className="w-full text-sm p-1.5 rounded bg-bg border border-border mt-1" placeholder="Add your own sub-question or guidance (Enter; Shift+Enter for newline)" value={newSub} onChange={setNewSub} onSubmit={() => { if (newSub.trim()) { setManualSubs([...manualSubs, newSub.trim()]); setNewSub('') } }} />
        </div>
        <div className="flex justify-between mt-4">
          <button className="text-sm text-muted" onClick={() => { clearPersisted(); onCancel() }}>Cancel</button>
          <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={subCount === 0 || forking} onClick={doFork}>{forking ? 'Forking…' : `Fork with ${subCount} sub-questions →`}</button>
        </div>
      </>
    )}
  </div>
}

function ExportArtifactButton({ id }: { id: string }) {
  const qc = useQueryClient()
  // Upfront status: has a report artifact already been exported (and still
  // exists)? Lets us show "View report" + "Regenerate" instead of a bare
  // "Export" on revisit.
  const { data: rstatus } = useQuery<{ slug: string | null }>({
    queryKey: ['research-report-status', id],
    queryFn: () => api.researchReportStatus(id),
  })
  const [loading, setLoading] = useState(false)
  const [localSlug, setLocalSlug] = useState<string | null>(null)
  const slug = localSlug ?? rstatus?.slug ?? null
  const go = async () => {
    setLoading(true)
    try {
      const r = await api.researchToArtifact(id)
      if (r?.slug) setLocalSlug(r.slug)
      qc.invalidateQueries({ queryKey: ['research-report-status', id] })
    } catch { alert('Failed to export as artifact') }
    finally { setLoading(false) }
  }
  if (slug) return (
    <span className="flex items-center gap-2">
      <a href={`/artifacts/${slug}`} target="_blank" rel="noopener noreferrer" className="text-xs text-ok flex items-center gap-1"><FileText size={12} /> View report <ExternalLink size={10} /></a>
      <button className="text-xs px-2 py-1 rounded bg-bg-elevated disabled:opacity-50" disabled={loading} onClick={go} title="Regenerate the report — updates the same artifact as a new version">
        <RefreshCw size={12} className="inline" /> {loading ? 'Regenerating…' : 'Regenerate'}
      </button>
    </span>
  )
  return <button className="text-xs px-2 py-1 rounded bg-bg-elevated disabled:opacity-50" disabled={loading} onClick={go}><FileText size={12} className="inline" /> {loading ? 'Exporting…' : 'Export as Artifact'}</button>
}

function AddToKnowledgeButton({ id }: { id: string }) {
  const qc = useQueryClient()
  // Upfront membership check so we render "Already in Knowledge" on mount
  // instead of only discovering it via a 409 after the user clicks.
  const { data: kstatus } = useQuery<{ in_library: boolean }>({
    queryKey: ['research-knowledge-status', id],
    queryFn: () => api.researchKnowledgeStatus(id),
  })
  const [status, setStatus] = useState<'idle' | 'loading' | 'done' | 'exists'>('idle')
  const go = async () => {
    setStatus('loading')
    try {
      await api.researchToKnowledge(id)
      setStatus('done')
      qc.invalidateQueries({ queryKey: ['research-knowledge-status', id] })
    } catch (e: unknown) {
      const err = e as { status?: number; message?: string } | null
      if (err?.status === 409) setStatus('exists')
      else { setStatus('idle'); alert(err?.message || 'Failed to add to Knowledge Library') }
    }
  }
  if (status === 'done') return <span className="text-xs text-ok flex items-center gap-1"><CheckCircle size={12} /> Added to Knowledge</span>
  if (status === 'exists' || kstatus?.in_library) return <span className="text-xs text-muted flex items-center gap-1"><BookOpen size={12} /> Already in Knowledge</span>
  return <button className="text-xs px-2 py-1 rounded bg-bg-elevated disabled:opacity-50" disabled={status === 'loading'} onClick={go}><BookOpen size={12} className="inline" /> {status === 'loading' ? 'Adding…' : 'Add to Knowledge'}</button>
}

function splitReportSections(md: string): string[] {
  // Split the report markdown into sections at level 1-3 headings so each
  // section gets its own copy button. Content before the first heading (and
  // a heading with no body) stays grouped with what follows sensibly.
  const lines = md.split('\n')
  const sections: string[] = []
  let cur: string[] = []
  for (const line of lines) {
    if (/^#{1,3}\s/.test(line) && cur.some(l => l.trim() !== '')) {
      sections.push(cur.join('\n').trim())
      cur = [line]
    } else {
      cur.push(line)
    }
  }
  if (cur.some(l => l.trim() !== '')) sections.push(cur.join('\n').trim())
  return sections.length ? sections : [md]
}

function ReportSections({ report }: { report: string }) {
  const [copied, setCopied] = useState<number | null>(null)
  const sections = splitReportSections(report)
  const copy = (text: string, i: number) => {
    navigator.clipboard?.writeText(text)
    setCopied(i)
    setTimeout(() => setCopied(c => (c === i ? null : c)), 1500)
  }
  return <>
    {sections.map((sec, i) => (
      <div key={i} className="relative border border-border rounded-md p-3 mb-2 bg-card">
        <button
          className="absolute top-2 right-2 text-[10px] px-2 py-0.5 rounded bg-bg-elevated"
          title="Copy this section's markdown to paste into chat or autopilot"
          onClick={() => copy(sec, i)}
        >{copied === i ? 'Copied!' : 'Copy'}</button>
        <MarkdownRenderer content={sec} />
      </div>
    ))}
  </>
}

function SubQuestionAdder({ id, campaign }: { id: string; campaign: Campaign }) {
  const [text, setText] = useState('')
  const [open, setOpen] = useState(false)
  const qc = useQueryClient()
  const addMut = useMutation({ mutationFn: (t: string) => api.researchAddQuestion(id, t), onSuccess: () => { setText(''); qc.invalidateQueries({ queryKey: ['research-campaign', id] }) } })
  const subs: Array<{ text: string; origin?: string; status?: string }> = (() => { try { return JSON.parse(campaign.sub_questions || '[]') } catch { return [] } })()
  // Only useful while the campaign is active (you can add guidance). On a
  // completed/stopped campaign the read-only list is redundant with the report.
  if (!ACTIVE_STATUSES.includes(campaign.status)) return null
  const originLabel = (o?: string) => o === 'manual' ? 'your guidance' : o === 'emergent' ? 'emergent' : (o || 'grill')
  return <div className="mb-4">
    <Clickable className="flex items-center gap-1 text-sm font-medium" onClick={() => setOpen(!open)}>
      {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Sub-questions &amp; guidance ({subs.length})
    </Clickable>
    {open && <div className="mt-2 pl-4 space-y-1">
      {subs.map((s, i) => <div key={i} className="text-xs flex items-center gap-1.5">
        {s.status === 'answered' ? <CheckCircle size={10} className="text-ok" /> : <HelpCircle size={10} className="text-muted" />}
        <span>{s.text}</span>
        <span className="text-muted italic">({originLabel(s.origin)})</span>
      </div>)}
      {ACTIVE_STATUSES.includes(campaign.status) && <div className="mt-2">
        <div className="flex items-center gap-2">
          <GrowTextarea ariaLabel="Add guidance or a sub-question" className="flex-1 text-xs p-1.5 rounded bg-bg border border-border" placeholder="Add guidance or a sub-question… (Enter; Shift+Enter for newline)" value={text} onChange={setText} onSubmit={() => { if (text.trim()) addMut.mutate(text.trim()) }} />
          <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg disabled:opacity-50" disabled={!text.trim() || addMut.isPending} onClick={() => addMut.mutate(text.trim())}>{addMut.isPending ? '…' : 'Add'}</button>
        </div>
        <div className="text-[10px] text-muted mt-1">Free-form — a sub-question or an instruction the agent should follow next cycle.</div>
      </div>}
    </div>}
  </div>
}

function CampaignDetail({ id, onBack, onFork, onOpen }: { id: string; onBack: () => void; onFork: (id: string) => void; onOpen: (id: string) => void }) {
  const qc = useQueryClient()
  const [sseFailed, setSseFailed] = useState(false)
  // Primary query: refreshed instantly via SSE invalidation; polls only as a
  // fallback when the SSE connection fails (react-query v5 has no useQuery
  // onSuccess, so we keep a single source-of-truth key instead of copying).
  const { data: campaign } = useQuery<Campaign>({
    queryKey: ['research-campaign', id],
    queryFn: () => api.researchCampaign(id),
    refetchInterval: sseFailed ? 5000 : false,
  })

  // SSE: instant updates; on connection error, fall back to polling above.
  useEffect(() => {
    setSseFailed(false)  // reset on id change so each campaign starts clean
    const es = new EventSource(`/api/apps/auto-research/campaigns/${id}/stream`)
    es.onmessage = () => {
      qc.invalidateQueries({ queryKey: ['research-campaign', id] })
    }
    es.onerror = () => { setSseFailed(true); es.close() }
    return () => { es.close() }
  }, [id, qc])
  const [showNudge, setShowNudge] = useState(false)
  const [nudgeText, setNudgeText] = useState('')
  const [answerText, setAnswerText] = useState('')
  const [questionExpanded, setQuestionExpanded] = useState(false)
  const [showReport, setShowReport] = useState(false)
  const { data: reportData } = useQuery<{ report: string }>({ queryKey: ['research-report', id], queryFn: () => api.researchReport(id), enabled: showReport })
  const actionMut = useMutation({ mutationFn: (action: string) => api.researchAction(id, action), onSuccess: () => qc.invalidateQueries({ queryKey: ['research-campaign', id] }) })
  const nudgeMut = useMutation({ mutationFn: (text: string) => api.researchNudge(id, text), onSuccess: () => { setShowNudge(false); setNudgeText(''); setAnswerText(''); qc.invalidateQueries({ queryKey: ['research-campaign', id] }) } })
  const deleteMut = useMutation({ mutationFn: () => api.researchDelete(id), onSuccess: () => { qc.invalidateQueries({ queryKey: ['research-campaigns'] }); onBack() } })

  if (!campaign) return <div className="text-sm text-muted">Loading...</div>
  const findings = campaign.findings || []
  const isActive = ACTIVE_STATUSES.includes(campaign.status)
  const sorted = isActive ? [...findings].reverse() : findings

  return <div>
    <div className="flex items-center gap-3 mb-4">
      <button className="text-sm text-accent" onClick={onBack}>← Back</button>
      <h2 className="text-lg font-semibold">{campaign.name}</h2>
      <span className="text-xs px-2 py-0.5 rounded bg-bg-elevated">{campaign.status}</span>
      <button className="text-xs px-2 py-1 rounded bg-bg-elevated text-danger ml-auto" onClick={() => { if (window.confirm('Delete this campaign and its report? This cannot be undone.')) deleteMut.mutate() }}><Trash2 size={12} className="inline" /> Delete</button>
    </div>
    {campaign.question && (() => {
      const isLong = campaign.question.length > 280
      return <div className="mb-4">
        <div className={`text-sm text-muted break-words ${isLong && !questionExpanded ? 'line-clamp-3' : ''}`}>{campaign.question}</div>
        {isLong && <button className="text-xs text-accent mt-1 inline-flex items-center gap-0.5" onClick={() => setQuestionExpanded(v => !v)}>
          {questionExpanded ? <><ChevronDown size={12} /> Show less</> : <><ChevronRight size={12} /> Show more</>}
        </button>}
      </div>
    })()}
    <div className="flex items-center justify-between mb-4">
      <div className="text-sm text-muted">Cycle {campaign.total_cycles}/{campaign.max_cycles} · {findings.filter(f => f.new_findings_count > 0).length} findings</div>
      {isActive && <div className="flex gap-2">
        {campaign.status === 'running' && <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('pause')}><Pause size={12} className="inline" /> Pause</button>}
        {campaign.status !== 'running' && <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('resume')}><Play size={12} className="inline" /> Resume</button>}
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('stop')}><Square size={12} className="inline" /> Stop</button>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => setShowNudge(true)}><MessageCircle size={12} className="inline" /> Nudge</button>
      </div>}
    </div>
    {campaign.status === 'stagnant' && <div className="p-3 rounded-md mb-4 border border-warn bg-warn/10">
      <div className="text-sm font-medium text-warn flex items-center gap-1"><AlertTriangle size={14} /> Research Stalled</div>
      <div className="text-xs mt-1">No new findings in the last 5 cycles.</div>
      <div className="flex gap-2 mt-2">
        <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg" onClick={() => setShowNudge(true)}>Give direction</button>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('stop')}>Stop</button>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('resume')}>Continue</button>
      </div>
    </div>}
    {campaign.status === 'needs_input' && <div className="p-3 rounded-md mb-4 border bg-bg-elevated" style={{ borderColor: 'color-mix(in srgb, var(--info) 45%, transparent)' }}>
      <div className="text-sm font-medium text-info flex items-center gap-1"><MessageCircle size={14} /> Agent needs input</div>
      <div className="text-sm mt-1">{campaign.pending_question || 'The agent is waiting for your direction.'}</div>
      <textarea aria-label="Your answer" className="w-full p-2 mt-2 rounded text-sm bg-bg border border-border resize-y" rows={2} value={answerText} onChange={e => setAnswerText(e.target.value)} placeholder="Your answer..." />
      <div className="flex gap-2 mt-2 justify-end">
        <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg disabled:opacity-50" onClick={() => nudgeMut.mutate(answerText)} disabled={!answerText || nudgeMut.isPending}>{nudgeMut.isPending ? 'Sending…' : 'Answer & resume'}</button>
      </div>
    </div>}
    {campaign.status === 'failed' && <div className="p-3 rounded-md mb-4 border border-danger bg-danger/10">
      <div className="text-sm font-medium text-danger flex items-center gap-1"><AlertTriangle size={14} /> Research stopped</div>
      <div className="text-xs mt-1">{campaign.error_message || 'The campaign stopped unexpectedly.'} Findings so far are preserved below.</div>
      <button className="text-xs px-2 py-1 mt-2 rounded bg-accent text-accent-fg" onClick={() => actionMut.mutate('resume')}><Play size={12} className="inline" /> Resume</button>
    </div>}
    {(campaign.status === 'complete' || campaign.status === 'stopped') && !isActive && (
      <div className="p-3 rounded-md mb-4 border border-accent bg-accent/5">
        <div className="text-sm font-medium flex items-center gap-1"><GitFork size={14} /> Continue Research</div>
        <div className="text-xs mt-1 text-muted">Pick up where this campaign left off.</div>
        <div className="flex gap-2 mt-2 flex-wrap">
          <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => onFork(id)}><GitFork size={12} className="inline" /> Fork & Challenge</button>
          <AddToKnowledgeButton id={id} />
          <ExportArtifactButton id={id} />
        </div>
      </div>
    )}
    {campaign.parent_id && <div className="text-xs text-muted mb-3">Forked from: <button className="text-accent underline" onClick={() => onOpen(campaign.parent_id!)}>{campaign.parent_id}</button></div>}
    <SubQuestionAdder id={id} campaign={campaign} />
    {showNudge && <div className="p-3 rounded-md mb-4 border border-border bg-card">
      <div className="text-sm font-medium mb-2 flex items-center gap-1"><MessageCircle size={14} /> Nudge Direction</div>
      <textarea aria-label="Nudge direction" className="w-full p-2 rounded text-sm bg-bg border border-border resize-y" rows={3} value={nudgeText} onChange={e => setNudgeText(e.target.value)} placeholder="Focus on..." />
      <div className="flex gap-2 mt-2 justify-end">
        <button className="text-xs text-muted" onClick={() => setShowNudge(false)}>Cancel</button>
        <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg disabled:opacity-50" onClick={() => nudgeMut.mutate(nudgeText)} disabled={!nudgeText || nudgeMut.isPending}>{nudgeMut.isPending ? 'Sending…' : 'Send'}</button>
      </div>
    </div>}
    <div className="mt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium">Findings ({findings.filter(f => f.new_findings_count > 0).length})</div>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => setShowReport(v => !v)}>{showReport ? 'Hide report' : 'View report'}</button>
      </div>
      {showReport && <div className="mb-3">
        {reportData?.report ? <ReportSections report={reportData.report} /> : <div className="text-sm text-muted">No report yet.</div>}
      </div>}
      {sorted.filter(f => f.new_findings_count > 0 || f.cycle === 1).map(f => <FindingCard key={f.cycle} f={f} />)}
      {findings.length === 0 && <div className="text-sm text-muted">First cycle in progress...</div>}
    </div>
  </div>
}

export default function ResearchLabPage() {
  const [view, setView] = useState<'list' | 'wizard' | 'detail' | 'fork'>('list')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [forkParentId, setForkParentId] = useState<string | null>(null)
  const qc = useQueryClient()
  const { data: campaigns = [], isLoading } = useQuery<Campaign[]>({
    queryKey: ['research-campaigns'],
    queryFn: () => api.researchCampaigns(),
    refetchInterval: (query) => {
      const data = query.state.data as Campaign[] | undefined
      return data?.some((c: Campaign) => ACTIVE_STATUSES.includes(c.status)) ? 10000 : false
    },
  })

  const active = campaigns.find((c: Campaign) => ACTIVE_STATUSES.includes(c.status))

  if (view === 'wizard') return <div className="px-6 py-4"><h1 className="text-lg font-semibold mb-4">New Campaign</h1><SetupWizard onCancel={() => setView('list')} onDone={() => { qc.invalidateQueries({ queryKey: ['research-campaigns'] }); setView('list') }} /></div>
  if (view === 'fork' && forkParentId) return <div className="px-6 py-4"><h1 className="text-lg font-semibold mb-4">Continue Research</h1><ForkFlow parentId={forkParentId} onCancel={() => setView('list')} onDone={() => { qc.invalidateQueries({ queryKey: ['research-campaigns'] }); setView('list') }} /></div>
  if (view === 'detail' && selectedId) return <div className="px-6 py-4"><CampaignDetail id={selectedId} onBack={() => setView('list')} onFork={(id) => { setForkParentId(id); setView('fork') }} onOpen={(pid) => setSelectedId(pid)} /></div>

  return <div className="px-6 py-4">
    <div className="flex items-center justify-between mb-4">
      <h1 className="text-lg font-semibold flex items-center gap-2"><FlaskConical size={20} /> Research Lab</h1>
      <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={!!active} onClick={() => setView('wizard')} title={active ? 'One campaign at a time' : ''}>+ New Campaign</button>
    </div>
    <div className="text-xs text-muted mb-4 flex items-start gap-1"><Lock size={12} className="mt-0.5 shrink-0" /> <span><span className="font-medium">Research-only.</span> Research Lab investigates and reports — it never takes actions on your systems (no writes, deployments, or code changes). Any next step that requires acting is handed off to the main agent for you to review and drive.</span></div>
    {isLoading ? <div className="text-sm text-muted">Loading...</div> : campaigns.length === 0 ? (
      <div className="text-center py-12">
        <FlaskConical size={48} className="mx-auto text-muted mb-3" />
        <div className="text-sm text-muted">Run autonomous research campaigns</div>
        <button className="mt-3 text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg" onClick={() => setView('wizard')}>+ New Campaign</button>
      </div>
    ) : <div className="space-y-3">
      {active && <div><div className="text-xs font-medium text-muted mb-1">ACTIVE</div>
        <Clickable className="border border-border rounded-md p-3 bg-card" onClick={() => { setSelectedId(active.id); setView('detail') }}>
          <div className="flex items-start gap-2">
            <StateBadge status={active.status} />
            <div className="font-medium text-sm line-clamp-2 flex-1" title={active.question}>{active.parent_id && <span className="text-[10px] font-medium text-accent bg-accent-subtle rounded px-1 py-0.5 mr-1 inline-flex items-center gap-0.5 align-middle"><GitFork size={10} /> Forked</span>}{active.question}</div>
          </div>
          <div className="text-xs text-muted mt-1">Cycle {active.total_cycles}/{active.max_cycles}</div>
        </Clickable></div>}
      {campaigns.filter((c: Campaign) => !ACTIVE_STATUSES.includes(c.status)).length > 0 && <div>
        <div className="text-xs font-medium text-muted mb-1">HISTORY</div>
        {campaigns.filter((c: Campaign) => !ACTIVE_STATUSES.includes(c.status)).map((c: Campaign) => (
          <Clickable key={c.id} className="border border-border rounded-md p-3 bg-card mb-2" onClick={() => { setSelectedId(c.id); setView('detail') }}>
            <div className="flex items-start gap-2">
              <StateBadge status={c.status} />
              <div className="text-sm line-clamp-2 flex-1" title={c.question}>{c.parent_id && <span className="text-[10px] font-medium text-accent bg-accent-subtle rounded px-1 py-0.5 mr-1 inline-flex items-center gap-0.5 align-middle"><GitFork size={10} /> Forked</span>}{c.question}</div>
            </div>
            <div className="text-xs text-muted mt-1">{c.total_cycles} cycles</div>
          </Clickable>
        ))}
      </div>}
      {active && <div className="text-xs text-muted flex items-center gap-1"><Lock size={12} /> One campaign at a time — research benefits from focused depth.</div>}
    </div>}
  </div>
}
