import { useState, useEffect, useReducer, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, Play, Pause, Square, MessageCircle, ChevronDown, ChevronRight, Sparkles, ThumbsUp, ArrowRight, HelpCircle, XCircle, CheckCircle, AlertTriangle, Lock, X, Trash2, GitFork, Flame, BookOpen, FileText, RefreshCw, ExternalLink, Loader2 } from 'lucide-react'
import { api } from '../../api/client'
import Clickable from '../../components/Clickable'
import Modal from '../../components/Modal'
import { Btn } from '../../components/ui'
import SimpleSelect from '../../components/SimpleSelect'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import GrillTree from './GrillTree'
import { grillReducer, promotedResearch, answeredClarifiers, suggestedMaxCycles, GrillNode } from './grillTreeModel'

import { i18nT } from '../../i18n/t'
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
  if (s === 'strong') return <span className="text-xs px-1.5 py-0.5 rounded bg-bg-elevated text-ok inline-flex items-center gap-0.5"><ThumbsUp size={10} /> {i18nT('apps.autoResearch.researchLabPage.strong')}</span>
  if (s === 'moderate') return <span className="text-xs px-1.5 py-0.5 rounded bg-bg-elevated text-warn inline-flex items-center gap-0.5"><ArrowRight size={10} /> {i18nT('apps.autoResearch.researchLabPage.moderate')}</span>
  return <span className="text-xs px-1.5 py-0.5 rounded bg-bg-elevated text-muted inline-flex items-center gap-0.5"><HelpCircle size={10} /> {i18nT('apps.autoResearch.researchLabPage.weak')}</span>
}

// Maps every campaign status to a single, consistent state pill so the root
// list communicates working / failed / done at a glance. Unknown statuses fall
// back to a neutral pill showing the raw status text.
//
// The label lives in its own flat table of catalog KEYS, separate from the
// presentation fields: this module evaluates once at import, so an `i18nT()`
// call in the table would freeze the boot language and never re-resolve on a
// language switch (see `lib/effort.ts`). Flat, and indexed inline at the
// `i18nT()` call, because that is the shape `scripts/check-i18n-keys.mjs` can
// resolve statically. The status STRINGS stay code constants — they are the
// backend's enum, not copy.
export const STATE_LABEL_KEY: Record<string, string> = {
  running: 'apps.autoResearch.researchLabPage.state_working',
  needs_input: 'apps.autoResearch.researchLabPage.state_needs_input',
  paused: 'apps.autoResearch.researchLabPage.state_paused',
  stagnant: 'apps.autoResearch.researchLabPage.state_stalled',
  ready: 'apps.autoResearch.researchLabPage.state_ready',
  complete: 'apps.autoResearch.researchLabPage.state_done',
  failed: 'apps.autoResearch.researchLabPage.state_failed',
  stopped: 'apps.autoResearch.researchLabPage.state_stopped',
}

const STATE_META: Record<string, { color: string; Icon: typeof CheckCircle; spin?: boolean }> = {
  running: { color: 'text-accent', Icon: Loader2, spin: true },
  needs_input: { color: 'text-warn', Icon: HelpCircle },
  paused: { color: 'text-muted', Icon: Pause },
  stagnant: { color: 'text-warn', Icon: AlertTriangle },
  ready: { color: 'text-muted', Icon: Play },
  complete: { color: 'text-ok', Icon: CheckCircle },
  failed: { color: 'text-danger', Icon: XCircle },
  stopped: { color: 'text-muted', Icon: Square },
}

/**
 * Localised label for a campaign status. A status the backend reports that has
 * no entry above has no catalog entry either, so it is de-snaked and shown
 * verbatim rather than dressed up as English copy in every locale.
 *
 * `hasOwnProperty`, not `in` or a bare index: `status` is backend-supplied, so a
 * campaign reporting `toString` or `constructor` would otherwise resolve to an
 * inherited `Object.prototype` member and hand a function to i18next.
 */
function stateLabel(status: string): string {
  return Object.prototype.hasOwnProperty.call(STATE_LABEL_KEY, status)
    ? i18nT(STATE_LABEL_KEY[status])
    : status.replace(/_/g, ' ')
}

function StateBadge({ status }: { status: string }) {
  // Same prototype-chain guard as `stateLabel`: `STATE_META['toString']` is a
  // function, which `??` would not replace, and destructuring it yields an
  // undefined `Icon` that crashes the render.
  const { color, Icon, spin } = Object.prototype.hasOwnProperty.call(STATE_META, status)
    ? STATE_META[status]
    : { color: 'text-muted', Icon: HelpCircle, spin: undefined }
  return (
    <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded bg-bg-elevated inline-flex items-center gap-1 shrink-0 ${color}`} title={i18nT('apps.autoResearch.researchLabPage.status', { status })}>
      <Icon size={10} className={spin ? 'animate-spin motion-reduce:animate-none' : undefined} /> {stateLabel(status)}
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
            <span className="text-xs text-muted">{i18nT('apps.autoResearch.researchLabPage.cycle')} {f.cycle}</span>
            <EvidenceBadge s={f.evidence_strength} />
            {f.verification && <span className={`text-xs px-1.5 py-0.5 rounded inline-flex items-center gap-0.5 ${f.verification.passed ? 'bg-ok/15 text-ok' : 'bg-bg-elevated text-muted'}`}>{f.verification.passed ? <><CheckCircle size={10} /> {i18nT('apps.autoResearch.researchLabPage.goal_met')}</> : i18nT('apps.autoResearch.researchLabPage.goal_not_yet')}</span>}
          </div>
          <div className="text-sm font-medium mt-0.5">{"\u201c"}{f.key_insight || f.summary}{"\u201d"}</div>
        </div>
      </Clickable>
      {open && (
        <div className="mt-2 pl-5 text-sm space-y-1">
          <p className="text-muted">{f.summary}</p>
          {f.sources_checked?.length > 0 && <div><span className="text-xs font-medium text-muted">{i18nT('apps.autoResearch.researchLabPage.sources')}</span>{f.sources_checked.map((s, i) => <div key={i} className="text-xs ml-2">• {s}</div>)}</div>}
          {f.sources_empty?.length > 0 && <div><span className="text-xs font-medium text-muted">{i18nT('apps.autoResearch.researchLabPage.searched_empty')}</span>{f.sources_empty.map((s, i) => <div key={i} className="text-xs ml-2 italic">• {s}</div>)}</div>}
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
      setError(i18nT('apps.autoResearch.researchLabPage.validation_failed_check_your_connection_and_try'))
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
      setError(i18nT('apps.autoResearch.researchLabPage.failed_to_start_campaign_please_try_again'))
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
          <div className="text-xs text-muted mb-2">{i18nT('apps.autoResearch.researchLabPage.how_sub_agent_execution_is_orchestrated_both_han')}</div>
          <div className="grid grid-cols-2 gap-2">
            <button type="button" onClick={() => setExecutionMode('agent')} className={`text-left p-2 rounded border ${executionMode === 'agent' ? 'border-accent bg-accent/10' : 'border-border'}`}>
              <div className="font-medium text-sm">{i18nT('apps.autoResearch.researchLabPage.agent')} <span className="text-muted font-normal">{i18nT('apps.autoResearch.researchLabPage.adaptive')}</span></div>
              <div className="text-xs text-muted mt-0.5">{i18nT('apps.autoResearch.researchLabPage.the_ai_drives_every_round_itself_deciding_what_t')}</div>
            </button>
            <button type="button" onClick={() => setExecutionMode('workflow')} className={`text-left p-2 rounded border ${executionMode === 'workflow' ? 'border-accent bg-accent/10' : 'border-border'}`}>
              <div className="font-medium text-sm">{i18nT('apps.autoResearch.researchLabPage.dynamic_workflow')} <span className="text-muted font-normal">{i18nT('apps.autoResearch.researchLabPage.scripted')}</span></div>
              <div className="text-xs text-muted mt-0.5">{i18nT('apps.autoResearch.researchLabPage.the_ai_writes_an_orchestration_script_up_front_a')}</div>
            </button>
          </div>
          {executionMode === 'workflow' && (
            <div className="text-xs text-warn mt-2 flex items-start gap-1">
              <AlertTriangle size={12} className="mt-0.5 shrink-0" />
              <span>{i18nT('apps.autoResearch.researchLabPage.dynamic_workflow_can_fan_out_to_many_sub_agents')}</span>
            </div>
          )}
        </div>
        <div>
          <label htmlFor="research-question" className="text-sm font-medium">{i18nT('apps.autoResearch.researchLabPage.what_do_you_want_to_research')}
            <textarea id="research-question" aria-label={i18nT('apps.autoResearch.researchLabPage.what_do_you_want_to_research')} className="w-full mt-1 p-2 rounded-md text-sm bg-bg border border-border resize-y" rows={3} value={question} onChange={e => setQuestion(e.target.value)} placeholder={i18nT('apps.autoResearch.researchLabPage.how_do_other_teams_handle_api_rate_limiting')} />
          </label>
          <div className="text-xs text-muted">{i18nT('apps.autoResearch.researchLabPage.min_20_characters')}</div>
        </div>
        <div>
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">{i18nT('apps.autoResearch.researchLabPage.sub_questions')}</span>
            <button className="text-xs text-accent flex items-center gap-1 disabled:opacity-50" onClick={grillMe} disabled={question.length < 20 || grilling}><Sparkles size={12} /> {grilling ? i18nT('apps.autoResearch.researchLabPage.grilling') : i18nT('apps.autoResearch.researchLabPage.grill_me')}</button>
          </div>
          {tree.some(n => n.status !== 'pruned') && <div className="text-xs text-muted mt-1">{i18nT('apps.autoResearch.researchLabPage.answer_clarifiers_to_refine_or_just_pick_sub_que')}</div>}
          <div className="mt-2"><GrillTree tree={tree} dispatch={dispatchTree} onExpand={onExpand} /></div>
          {grillUnavailable && <div className="text-xs text-warn mt-2">{i18nT('apps.autoResearch.researchLabPage.grill_unavailable_add_sub_questions_manually_bel')}</div>}
          {subQs.map((sq, i) => <div key={i} className="flex items-start gap-2 mt-1"><GrowTextarea ariaLabel={i18nT('apps.autoResearch.researchLabPage.sub_question', { n: i + 1 })} className="flex-1 text-sm p-1.5 rounded bg-bg border border-border" value={sq} onChange={v => { const n = [...subQs]; n[i] = v; setSubQs(n) }} /><button className="text-xs text-danger mt-1.5" onClick={() => setSubQs(subQs.filter((_, j) => j !== i))} aria-label={i18nT('apps.autoResearch.researchLabPage.remove_sub_question')}><X size={12} /></button></div>)}
          <GrowTextarea ariaLabel={i18nT('apps.autoResearch.researchLabPage.add_sub_question_manually')} className="w-full text-sm p-1.5 rounded bg-bg border border-border mt-2" placeholder={i18nT('apps.autoResearch.researchLabPage.add_sub_question_manually_enter_shift_enter_for')} value={newSub} onChange={setNewSub} onSubmit={() => { if (newSub.trim()) { setSubQs([...subQs, newSub.trim()]); setNewSub('') } }} />
        </div>
      </div>}

      {step === 1 && <div className="space-y-4">
        <span className="text-sm font-medium block">{i18nT('apps.autoResearch.researchLabPage.when_should_the_agent_stop')}</span>
        <div className="text-xs text-muted">{i18nT('apps.autoResearch.researchLabPage.stops_at_the_cycle_cap_when_the_definition_of_do')}</div>
        <div className="flex items-center gap-2"><span className="text-sm">{i18nT('apps.autoResearch.researchLabPage.max_cycles')}</span><input type="number" aria-label={i18nT('apps.autoResearch.researchLabPage.max_cycles_2')} min={5} max={100} value={maxCycles} className="w-20 text-sm px-3 py-2 rounded-md bg-bg-elevated border border-border text-text outline-none focus-ring" onChange={e => { setMaxCyclesTouched(true); setMaxCycles(Number(e.target.value)) }} />{subCount > 0 && !maxCyclesTouched && <span className="text-xs text-muted">{i18nT('apps.autoResearch.researchLabPage.suggested_from')} {subCount} {i18nT('apps.autoResearch.researchLabPage.sub_questions_2')}</span>}</div>
        {/* Values are seconds; SimpleSelect is string-only, so they round-trip through
            String/Number. `options` and `optionLabels` are positional — keep them in step. */}
        <div className="flex items-center gap-2"><span className="text-sm">{i18nT('apps.autoResearch.researchLabPage.idle_between_cycles')}</span><SimpleSelect aria-label={i18nT('apps.autoResearch.researchLabPage.idle_between_cycles_2')} options={['30', '60', '120']} optionLabels={[i18nT('apps.autoResearch.researchLabPage.30s'), i18nT('apps.autoResearch.researchLabPage.60s'), i18nT('apps.autoResearch.researchLabPage.120s')]} value={String(idleSecs)} onChange={v => setIdleSecs(Number(v))} /></div>
        <div>
          <span className="text-sm font-medium block">{i18nT('apps.autoResearch.researchLabPage.definition_of_done_optional')}</span>
          <textarea aria-label={i18nT('apps.autoResearch.researchLabPage.definition_of_done_optional')} className="w-full text-sm p-1.5 rounded bg-bg border border-border mt-1 resize-y" rows={2} placeholder={i18nT('apps.autoResearch.researchLabPage.e_g_ai_code_review_finds_no_blocking_issues_and')} value={successCriteria} onChange={e => setSuccessCriteria(e.target.value)} />
          <div className="text-xs text-muted mt-1">{i18nT('apps.autoResearch.researchLabPage.if_set_the_agent_verifies_against_this_each_cycl')}</div>
        </div>
        <label htmlFor="auto-approve" className="flex items-center gap-2 text-sm">
          <input id="auto-approve" type="checkbox" aria-label={i18nT('apps.autoResearch.researchLabPage.run_unattended_skip_clarification_questions')} checked={autoApprove} onChange={e => setAutoApprove(e.target.checked)} />
          {i18nT('apps.autoResearch.researchLabPage.run_unattended_skip_clarification_questions')}
        </label>
        <div className="flex items-center gap-2"><span className="text-sm">{i18nT('apps.autoResearch.researchLabPage.parallel_workers')}</span><input type="number" aria-label={i18nT('apps.autoResearch.researchLabPage.parallel_workers_2')} min={1} max={5} value={parallelWorkers} className="w-16 text-sm px-3 py-2 rounded-md bg-bg-elevated border border-border text-text outline-none focus-ring" onChange={e => setParallelWorkers(Math.min(5, Math.max(1, Number(e.target.value))))} /><span className="text-xs text-muted">{parallelWorkers > 1 ? `${parallelWorkers} sub-questions investigated in parallel each cycle` : 'sequential (default)'}</span></div>
      </div>}

      {step === 2 && <div className="space-y-3">
        <span className="text-sm font-medium block">{i18nT('apps.autoResearch.researchLabPage.pre_flight_check')}</span>
        {validation ? <>
          {validation.errors.map((e, i) => <div key={i} className="text-sm flex items-center gap-1"><XCircle size={14} className="text-danger" /> {e}</div>)}
          {validation.errors.length === 0 && <div className="text-sm text-ok flex items-center gap-1"><CheckCircle size={14} /> {i18nT('apps.autoResearch.researchLabPage.all_checks_passed')}</div>}
          {validation.warnings.map((w, i) => <div key={i} className="text-sm text-warn flex items-center gap-1"><AlertTriangle size={14} /> {w}</div>)}
          <div className="mt-3 p-3 rounded text-sm bg-bg border border-border">
            <div>{i18nT('apps.autoResearch.researchLabPage.research_question', { question: `${question.slice(0, 50)}${question.length > 50 ? '...' : ''}` })}</div>
            <div className="text-muted">{i18nT('apps.autoResearch.researchLabPage.up_to')} {maxCycles} {i18nT('apps.autoResearch.researchLabPage.cycles')} {idleSecs}{i18nT('apps.autoResearch.researchLabPage.s_idle')}{validation.estimated_duration_min} {i18nT('apps.autoResearch.researchLabPage.min')}</div>
            {successCriteria && <div className="text-muted">{i18nT('apps.autoResearch.researchLabPage.done_when')} {successCriteria}</div>}
          </div>
        </> : <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.validating')}</div>}
        {error && <div className="text-sm text-danger flex items-center gap-1"><XCircle size={14} /> {error}</div>}
      </div>}

      <div className="flex justify-between mt-6">
        <button className="text-sm text-muted hover:text-text" onClick={step === 0 ? onCancel : () => setStep(step - 1)}>{step === 0 ? i18nT('apps.autoResearch.researchLabPage.cancel') : i18nT('apps.autoResearch.researchLabPage.back')}</button>
        {step < 2 ? <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={step === 0 && question.length < 20} onClick={() => setStep(step + 1)}>{i18nT('apps.autoResearch.researchLabPage.next')}</button>
          : <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={!validation?.can_start || submitting} onClick={submit}>{submitting ? i18nT('apps.autoResearch.researchLabPage.starting') : i18nT('apps.autoResearch.researchLabPage.start_campaign')}</button>}
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
    } catch { setError(i18nT('apps.autoResearch.researchLabPage.could_not_generate_challenges_please_try_again')) }
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
      } else setError(i18nT('apps.autoResearch.researchLabPage.fork_failed_no_campaign_was_created'))
    } catch { setError(i18nT('apps.autoResearch.researchLabPage.fork_failed_please_try_again')) }
    finally { setForking(false) }
  }

  const subCount = promotedResearch(tree).length + manualSubs.length

  return <div className="max-w-2xl mx-auto space-y-4">
    <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.challenge_the_findings_from_question', { question: question?.slice(0, 60) })}</div>
    {error && <div className="text-xs text-danger">{error}</div>}
    {tree.length === 0 ? (
      <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={grilling || !question} onClick={startChallenge}>{grilling ? i18nT('apps.autoResearch.researchLabPage.challenging') : <><Flame size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.challenge_findings')}</>}</button>
    ) : (
      <>
        <div className="text-xs text-muted">{i18nT('apps.autoResearch.researchLabPage.answer_challenges_to_refine_or_just_pick_sub_que')}</div>
        <GrillTree tree={tree} dispatch={dispatchTree} onExpand={onExpand} />
        <div className="mt-3">
          {manualSubs.map((sq, i) => <div key={i} className="flex items-start gap-2 mt-1"><GrowTextarea ariaLabel={i18nT('apps.autoResearch.researchLabPage.sub_question', { n: i + 1 })} className="flex-1 text-sm p-1.5 rounded bg-bg border border-border" value={sq} onChange={v => { const n = [...manualSubs]; n[i] = v; setManualSubs(n) }} /><button className="text-xs text-danger mt-1.5" onClick={() => setManualSubs(manualSubs.filter((_, j) => j !== i))} aria-label={i18nT('apps.autoResearch.researchLabPage.remove_sub_question')}><X size={12} /></button></div>)}
          <GrowTextarea ariaLabel={i18nT('apps.autoResearch.researchLabPage.add_your_own_sub_question_or_guidance')} className="w-full text-sm p-1.5 rounded bg-bg border border-border mt-1" placeholder={i18nT('apps.autoResearch.researchLabPage.add_your_own_sub_question_or_guidance_enter_shif')} value={newSub} onChange={setNewSub} onSubmit={() => { if (newSub.trim()) { setManualSubs([...manualSubs, newSub.trim()]); setNewSub('') } }} />
        </div>
        <div className="flex justify-between mt-4">
          <button className="text-sm text-muted" onClick={() => { clearPersisted(); onCancel() }}>{i18nT('apps.autoResearch.researchLabPage.cancel')}</button>
          <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={subCount === 0 || forking} onClick={doFork}>{forking ? i18nT('apps.autoResearch.researchLabPage.forking') : `Fork with ${subCount} sub-questions →`}</button>
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
    } catch { alert(i18nT('apps.autoResearch.researchLabPage.failed_to_export_as_artifact')) }
    finally { setLoading(false) }
  }
  if (slug) return (
    <span className="flex items-center gap-2">
      <a href={`/artifacts/${slug}`} target="_blank" rel="noopener noreferrer" className="text-xs text-ok flex items-center gap-1"><FileText size={12} /> {i18nT('apps.autoResearch.researchLabPage.view_report')} <ExternalLink size={10} /></a>
      <button className="text-xs px-2 py-1 rounded bg-bg-elevated disabled:opacity-50" disabled={loading} onClick={go} title={i18nT('apps.autoResearch.researchLabPage.regenerate_the_report_updates_the_same_artifact')}>
        <RefreshCw size={12} className="inline" /> {loading ? i18nT('apps.autoResearch.researchLabPage.regenerating') : i18nT('apps.autoResearch.researchLabPage.regenerate')}
      </button>
    </span>
  )
  return <button className="text-xs px-2 py-1 rounded bg-bg-elevated disabled:opacity-50" disabled={loading} onClick={go}><FileText size={12} className="inline" /> {loading ? i18nT('apps.autoResearch.researchLabPage.exporting') : i18nT('apps.autoResearch.researchLabPage.export_as_artifact')}</button>
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
      else { setStatus('idle'); alert(err?.message || i18nT('apps.autoResearch.researchLabPage.failed_to_add_to_knowledge_library')) }
    }
  }
  if (status === 'done') return <span className="text-xs text-ok flex items-center gap-1"><CheckCircle size={12} /> {i18nT('apps.autoResearch.researchLabPage.added_to_knowledge')}</span>
  if (status === 'exists' || kstatus?.in_library) return <span className="text-xs text-muted flex items-center gap-1"><BookOpen size={12} /> {i18nT('apps.autoResearch.researchLabPage.already_in_knowledge')}</span>
  return <button className="text-xs px-2 py-1 rounded bg-bg-elevated disabled:opacity-50" disabled={status === 'loading'} onClick={go}><BookOpen size={12} className="inline" /> {status === 'loading' ? i18nT('apps.autoResearch.researchLabPage.adding') : i18nT('apps.autoResearch.researchLabPage.add_to_knowledge')}</button>
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
          title={i18nT('apps.autoResearch.researchLabPage.copy_this_section_s_markdown_to_paste_into_chat')}
          onClick={() => copy(sec, i)}
        >{copied === i ? i18nT('apps.autoResearch.researchLabPage.copied') : i18nT('apps.autoResearch.researchLabPage.copy')}</button>
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
  const originLabel = (o?: string) => o === 'manual' ? i18nT('apps.autoResearch.researchLabPage.your_guidance') : o === 'emergent' ? i18nT('apps.autoResearch.researchLabPage.emergent') : (o || 'grill')
  return <div className="mb-4">
    <Clickable className="flex items-center gap-1 text-sm font-medium" onClick={() => setOpen(!open)}>
      {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />} {i18nT('apps.autoResearch.researchLabPage.sub_questions_guidance_count', { count: subs.length })}
    </Clickable>
    {open && <div className="mt-2 pl-4 space-y-1">
      {subs.map((s, i) => <div key={i} className="text-xs flex items-center gap-1.5">
        {s.status === 'answered' ? <CheckCircle size={10} className="text-ok" /> : <HelpCircle size={10} className="text-muted" />}
        <span>{s.text}</span>
        <span className="text-muted italic">({originLabel(s.origin)})</span>
      </div>)}
      {ACTIVE_STATUSES.includes(campaign.status) && <div className="mt-2">
        <div className="flex items-center gap-2">
          <GrowTextarea ariaLabel={i18nT('apps.autoResearch.researchLabPage.add_guidance_or_a_sub_question')} className="flex-1 text-xs p-1.5 rounded bg-bg border border-border" placeholder={i18nT('apps.autoResearch.researchLabPage.add_guidance_or_a_sub_question_enter_shift_enter')} value={text} onChange={setText} onSubmit={() => { if (text.trim()) addMut.mutate(text.trim()) }} />
          <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg disabled:opacity-50" disabled={!text.trim() || addMut.isPending} onClick={() => addMut.mutate(text.trim())}>{addMut.isPending ? '…' : i18nT('apps.autoResearch.researchLabPage.add')}</button>
        </div>
        <div className="text-[10px] text-muted mt-1">{i18nT('apps.autoResearch.researchLabPage.free_form_a_sub_question_or_an_instruction_the_a')}</div>
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
  // In-app dialog, NOT window.confirm: the native confirm is synchronous and
  // blocks the renderer's event loop, so a Quit event arriving while it is open
  // queues behind it and fires the instant it dismisses — tearing the app down
  // before the DELETE request below is ever sent.
  const [confirmDelete, setConfirmDelete] = useState(false)
  const { data: reportData } = useQuery<{ report: string }>({ queryKey: ['research-report', id], queryFn: () => api.researchReport(id), enabled: showReport })
  const actionMut = useMutation({ mutationFn: (action: string) => api.researchAction(id, action), onSuccess: () => qc.invalidateQueries({ queryKey: ['research-campaign', id] }) })
  const nudgeMut = useMutation({ mutationFn: (text: string) => api.researchNudge(id, text), onSuccess: () => { setShowNudge(false); setNudgeText(''); setAnswerText(''); qc.invalidateQueries({ queryKey: ['research-campaign', id] }) } })
  const deleteMut = useMutation({ mutationFn: () => api.researchDelete(id), onSuccess: () => { setConfirmDelete(false); qc.invalidateQueries({ queryKey: ['research-campaigns'] }); onBack() } })

  if (!campaign) return <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.loading')}</div>
  const findings = campaign.findings || []
  const isActive = ACTIVE_STATUSES.includes(campaign.status)
  const sorted = isActive ? [...findings].reverse() : findings

  return <div>
    <div className="flex items-center gap-3 mb-4">
      <button className="text-sm text-accent" onClick={onBack}>{i18nT('apps.autoResearch.researchLabPage.back')}</button>
      <h2 className="text-lg font-semibold">{campaign.name}</h2>
      <span className="text-xs px-2 py-0.5 rounded bg-bg-elevated">{campaign.status}</span>
      <button className="text-xs px-2 py-1 rounded bg-bg-elevated text-danger ml-auto" onClick={() => { deleteMut.reset(); setConfirmDelete(true) }}><Trash2 size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.delete')}</button>
    </div>
    <Modal
      open={confirmDelete}
      onClose={() => { if (!deleteMut.isPending) setConfirmDelete(false) }}
      title={i18nT('apps.autoResearch.researchLabPage.delete_campaign')}
      maxWidth={400}
      footer={<>
        <Btn disabled={deleteMut.isPending} onClick={() => setConfirmDelete(false)}>{i18nT('apps.autoResearch.researchLabPage.cancel')}</Btn>
        {/* Close only on success (see deleteMut.onSuccess): dismissing before the
            request resolves would make a failed DELETE silent — the campaign
            just looks un-deleted with no message and no retry cue. */}
        <Btn danger disabled={deleteMut.isPending} onClick={() => deleteMut.mutate()}>{deleteMut.isPending ? i18nT('apps.autoResearch.researchLabPage.deleting') : i18nT('apps.autoResearch.researchLabPage.delete_campaign_button')}</Btn>
      </>}
    >
      <p className="text-sm text-muted m-0">{i18nT('apps.autoResearch.researchLabPage.delete_this_campaign_and_its_report_this_cannot')}</p>
      {deleteMut.isError && <p className="text-danger text-[12px] mt-2 m-0">{deleteMut.error instanceof Error && deleteMut.error.message ? deleteMut.error.message : i18nT('apps.autoResearch.researchLabPage.delete_failed')}</p>}
    </Modal>
    {campaign.question && (() => {
      const isLong = campaign.question.length > 280
      return <div className="mb-4">
        <div className={`text-sm text-muted break-words ${isLong && !questionExpanded ? 'line-clamp-3' : ''}`}>{campaign.question}</div>
        {isLong && <button className="text-xs text-accent mt-1 inline-flex items-center gap-0.5" onClick={() => setQuestionExpanded(v => !v)}>
          {questionExpanded ? <><ChevronDown size={12} /> {i18nT('apps.autoResearch.researchLabPage.show_less')}</> : <><ChevronRight size={12} /> {i18nT('apps.autoResearch.researchLabPage.show_more')}</>}
        </button>}
      </div>
    })()}
    <div className="flex items-center justify-between mb-4">
      <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.cycle')} {campaign.total_cycles}/{campaign.max_cycles} · {findings.filter(f => f.new_findings_count > 0).length} {i18nT('apps.autoResearch.researchLabPage.findings')}</div>
      {isActive && <div className="flex gap-2">
        {campaign.status === 'running' && <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('pause')}><Pause size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.pause')}</button>}
        {campaign.status !== 'running' && <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('resume')}><Play size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.resume')}</button>}
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('stop')}><Square size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.stop')}</button>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => setShowNudge(true)}><MessageCircle size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.nudge')}</button>
      </div>}
    </div>
    {campaign.status === 'stagnant' && <div className="p-3 rounded-md mb-4 border border-warn bg-warn/10">
      <div className="text-sm font-medium text-warn flex items-center gap-1"><AlertTriangle size={14} /> {i18nT('apps.autoResearch.researchLabPage.research_stalled')}</div>
      <div className="text-xs mt-1">{i18nT('apps.autoResearch.researchLabPage.no_new_findings_in_the_last_5_cycles')}</div>
      <div className="flex gap-2 mt-2">
        <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg" onClick={() => setShowNudge(true)}>{i18nT('apps.autoResearch.researchLabPage.give_direction')}</button>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('stop')}>{i18nT('apps.autoResearch.researchLabPage.stop')}</button>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => actionMut.mutate('resume')}>{i18nT('apps.autoResearch.researchLabPage.continue')}</button>
      </div>
    </div>}
    {campaign.status === 'needs_input' && <div className="p-3 rounded-md mb-4 border bg-bg-elevated" style={{ borderColor: 'color-mix(in srgb, var(--info) 45%, transparent)' }}>
      <div className="text-sm font-medium text-info flex items-center gap-1"><MessageCircle size={14} /> {i18nT('apps.autoResearch.researchLabPage.agent_needs_input')}</div>
      <div className="text-sm mt-1">{campaign.pending_question || i18nT('apps.autoResearch.researchLabPage.the_agent_is_waiting_for_your_direction')}</div>
      <textarea aria-label={i18nT('apps.autoResearch.researchLabPage.your_answer')} className="w-full p-2 mt-2 rounded text-sm bg-bg border border-border resize-y" rows={2} value={answerText} onChange={e => setAnswerText(e.target.value)} placeholder={i18nT('apps.autoResearch.researchLabPage.your_answer_2')} />
      <div className="flex gap-2 mt-2 justify-end">
        <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg disabled:opacity-50" onClick={() => nudgeMut.mutate(answerText)} disabled={!answerText || nudgeMut.isPending}>{nudgeMut.isPending ? i18nT('apps.autoResearch.researchLabPage.sending') : i18nT('apps.autoResearch.researchLabPage.answer_resume')}</button>
      </div>
    </div>}
    {campaign.status === 'failed' && <div className="p-3 rounded-md mb-4 border border-danger bg-danger/10">
      <div className="text-sm font-medium text-danger flex items-center gap-1"><AlertTriangle size={14} /> {i18nT('apps.autoResearch.researchLabPage.research_stopped')}</div>
      <div className="text-xs mt-1">{campaign.error_message || i18nT('apps.autoResearch.researchLabPage.the_campaign_stopped_unexpectedly')} {i18nT('apps.autoResearch.researchLabPage.findings_so_far_are_preserved_below')}</div>
      <button className="text-xs px-2 py-1 mt-2 rounded bg-accent text-accent-fg" onClick={() => actionMut.mutate('resume')}><Play size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.resume')}</button>
    </div>}
    {(campaign.status === 'complete' || campaign.status === 'stopped') && !isActive && (
      <div className="p-3 rounded-md mb-4 border border-accent bg-accent/5">
        <div className="text-sm font-medium flex items-center gap-1"><GitFork size={14} /> {i18nT('apps.autoResearch.researchLabPage.continue_research')}</div>
        <div className="text-xs mt-1 text-muted">{i18nT('apps.autoResearch.researchLabPage.pick_up_where_this_campaign_left_off')}</div>
        <div className="flex gap-2 mt-2 flex-wrap">
          <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => onFork(id)}><GitFork size={12} className="inline" /> {i18nT('apps.autoResearch.researchLabPage.fork_challenge')}</button>
          <AddToKnowledgeButton id={id} />
          <ExportArtifactButton id={id} />
        </div>
      </div>
    )}
    {campaign.parent_id && <div className="text-xs text-muted mb-3">{i18nT('apps.autoResearch.researchLabPage.forked_from')} <button className="text-accent underline" onClick={() => onOpen(campaign.parent_id!)}>{campaign.parent_id}</button></div>}
    <SubQuestionAdder id={id} campaign={campaign} />
    {showNudge && <div className="p-3 rounded-md mb-4 border border-border bg-card">
      <div className="text-sm font-medium mb-2 flex items-center gap-1"><MessageCircle size={14} /> {i18nT('apps.autoResearch.researchLabPage.nudge_direction')}</div>
      <textarea aria-label={i18nT('apps.autoResearch.researchLabPage.nudge_direction_2')} className="w-full p-2 rounded text-sm bg-bg border border-border resize-y" rows={3} value={nudgeText} onChange={e => setNudgeText(e.target.value)} placeholder={i18nT('apps.autoResearch.researchLabPage.focus_on')} />
      <div className="flex gap-2 mt-2 justify-end">
        <button className="text-xs text-muted" onClick={() => setShowNudge(false)}>{i18nT('apps.autoResearch.researchLabPage.cancel')}</button>
        <button className="text-xs px-2 py-1 rounded bg-accent text-accent-fg disabled:opacity-50" onClick={() => nudgeMut.mutate(nudgeText)} disabled={!nudgeText || nudgeMut.isPending}>{nudgeMut.isPending ? i18nT('apps.autoResearch.researchLabPage.sending') : i18nT('apps.autoResearch.researchLabPage.send')}</button>
      </div>
    </div>}
    <div className="mt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium">{i18nT('apps.autoResearch.researchLabPage.findings_count', { count: findings.filter(f => f.new_findings_count > 0).length })}</div>
        <button className="text-xs px-2 py-1 rounded bg-bg-elevated" onClick={() => setShowReport(v => !v)}>{showReport ? i18nT('apps.autoResearch.researchLabPage.hide_report') : i18nT('apps.autoResearch.researchLabPage.view_report')}</button>
      </div>
      {showReport && <div className="mb-3">
        {reportData?.report ? <ReportSections report={reportData.report} /> : <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.no_report_yet')}</div>}
      </div>}
      {sorted.filter(f => f.new_findings_count > 0 || f.cycle === 1).map(f => <FindingCard key={f.cycle} f={f} />)}
      {findings.length === 0 && <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.first_cycle_in_progress')}</div>}
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

  if (view === 'wizard') return <div className="px-6 py-4"><h1 className="text-lg font-semibold mb-4">{i18nT('apps.autoResearch.researchLabPage.new_campaign')}</h1><SetupWizard onCancel={() => setView('list')} onDone={() => { qc.invalidateQueries({ queryKey: ['research-campaigns'] }); setView('list') }} /></div>
  if (view === 'fork' && forkParentId) return <div className="px-6 py-4"><h1 className="text-lg font-semibold mb-4">{i18nT('apps.autoResearch.researchLabPage.continue_research')}</h1><ForkFlow parentId={forkParentId} onCancel={() => setView('list')} onDone={() => { qc.invalidateQueries({ queryKey: ['research-campaigns'] }); setView('list') }} /></div>
  if (view === 'detail' && selectedId) return <div className="px-6 py-4"><CampaignDetail id={selectedId} onBack={() => setView('list')} onFork={(id) => { setForkParentId(id); setView('fork') }} onOpen={(pid) => setSelectedId(pid)} /></div>

  return <div className="px-6 py-4">
    <div className="flex items-center justify-between mb-4">
      <h1 className="text-lg font-semibold flex items-center gap-2"><FlaskConical size={20} /> {i18nT('apps.autoResearch.researchLabPage.research_lab')}</h1>
      <button className="text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg disabled:opacity-50" disabled={!!active} onClick={() => setView('wizard')} title={active ? i18nT('apps.autoResearch.researchLabPage.one_campaign_at_a_time') : ''}>{i18nT('apps.autoResearch.researchLabPage.new_campaign_2')}</button>
    </div>
    <div className="text-xs text-muted mb-4 flex items-start gap-1"><Lock size={12} className="mt-0.5 shrink-0" /> <span><span className="font-medium">{i18nT('apps.autoResearch.researchLabPage.research_only')}</span> {i18nT('apps.autoResearch.researchLabPage.research_lab_investigates_and_reports_it_never_t')}</span></div>
    {isLoading ? <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.loading')}</div> : campaigns.length === 0 ? (
      <div className="text-center py-12">
        <FlaskConical size={48} className="mx-auto text-muted mb-3" />
        <div className="text-sm text-muted">{i18nT('apps.autoResearch.researchLabPage.run_autonomous_research_campaigns')}</div>
        <button className="mt-3 text-sm px-3 py-1.5 rounded-md bg-accent text-accent-fg" onClick={() => setView('wizard')}>{i18nT('apps.autoResearch.researchLabPage.new_campaign_2')}</button>
      </div>
    ) : <div className="space-y-3">
      {active && <div><div className="text-xs font-medium text-muted mb-1">{i18nT('apps.autoResearch.researchLabPage.active')}</div>
        <Clickable className="border border-border rounded-md p-3 bg-card" onClick={() => { setSelectedId(active.id); setView('detail') }}>
          <div className="flex items-start gap-2">
            <StateBadge status={active.status} />
            <div className="font-medium text-sm line-clamp-2 flex-1" title={active.question}>{active.parent_id && <span className="text-[10px] font-medium text-accent bg-accent-subtle rounded px-1 py-0.5 mr-1 inline-flex items-center gap-0.5 align-middle"><GitFork size={10} /> {i18nT('apps.autoResearch.researchLabPage.forked')}</span>}{active.question}</div>
          </div>
          <div className="text-xs text-muted mt-1">{i18nT('apps.autoResearch.researchLabPage.cycle')} {active.total_cycles}/{active.max_cycles}</div>
        </Clickable></div>}
      {campaigns.filter((c: Campaign) => !ACTIVE_STATUSES.includes(c.status)).length > 0 && <div>
        <div className="text-xs font-medium text-muted mb-1">{i18nT('apps.autoResearch.researchLabPage.history')}</div>
        {campaigns.filter((c: Campaign) => !ACTIVE_STATUSES.includes(c.status)).map((c: Campaign) => (
          <Clickable key={c.id} className="border border-border rounded-md p-3 bg-card mb-2" onClick={() => { setSelectedId(c.id); setView('detail') }}>
            <div className="flex items-start gap-2">
              <StateBadge status={c.status} />
              <div className="text-sm line-clamp-2 flex-1" title={c.question}>{c.parent_id && <span className="text-[10px] font-medium text-accent bg-accent-subtle rounded px-1 py-0.5 mr-1 inline-flex items-center gap-0.5 align-middle"><GitFork size={10} /> {i18nT('apps.autoResearch.researchLabPage.forked')}</span>}{c.question}</div>
            </div>
            <div className="text-xs text-muted mt-1">{c.total_cycles} {i18nT('apps.autoResearch.researchLabPage.cycles_2')}</div>
          </Clickable>
        ))}
      </div>}
      {active && <div className="text-xs text-muted flex items-center gap-1"><Lock size={12} /> {i18nT('apps.autoResearch.researchLabPage.one_campaign_at_a_time_research_benefits_from_fo')}</div>}
    </div>}
  </div>
}
