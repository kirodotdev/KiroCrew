import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client'
import { Input, SendBtn } from './ui'
import { SettingsToggle } from './settings'
import AgentSelector, { type KiroCrewAgent } from './AgentSelector'
import SimpleSelect from './SimpleSelect'
import type { CronJob } from '../types'
import type { CronPrefill } from '../utils/schedulePresets'
import { SaveCreateLabel, expandDow } from '../utils/cronUtils'

import { i18nT } from '../i18n/t'
import { fmtWeekday } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
export const TIMEZONES = ['America/Los_Angeles','America/Phoenix','America/Denver','America/Chicago','America/New_York','America/Sao_Paulo','Europe/London','Europe/Berlin','Europe/Paris','Asia/Kolkata','Asia/Shanghai','Asia/Tokyo','Australia/Sydney','Pacific/Auckland','UTC']
/** Monday-first weekday labels. A function, not a module-level array: a const
 *  array of translated strings would freeze at the boot language. The index
 *  contract is unchanged — grid index `i` still maps through GRID_TO_CRON_DOW. */
const dayNames = () => [1, 2, 3, 4, 5, 6, 7].map((iso) => fmtWeekday(iso))
const GRID_TO_CRON_DOW = [0, 1, 2, 3, 4, 5, 6, 0] // grid 1-7 → cron dow
const CRON_DOW_TO_GRID: Record<number, number> = { 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 0: 7, 7: 7 }



/** The pinned crew, rendered as a fact rather than a disabled selector — a
 * greyed-out control asks to be re-enabled; a value does not. Shrink-wrapped
 * so it cannot read as an editable input among the real ones, and the hint
 * says WHY it is fixed, replacing the picker's own hint line. */
function LockedAgentValue({ name }: { name: string }) {
  const hint = i18nT('components.jobForm.agent_pinned_hint')
  return (
    <span className="flex flex-col items-start gap-1">
      <span className="text-[11px] text-muted/70">{hint}</span>
      <span
        className="inline-flex max-w-full break-all items-center rounded-full border border-border bg-bg-hover px-2.5 py-0.5 font-mono text-[12px] text-text-strong"
        title={hint}
        data-testid="jobform-locked-agent"
      >
        {name}
      </span>
    </span>
  )
}

/** Job execution kind. 'message' runs the agent; 'script'/'command' are
 * LLM-less (Python callable / shell) and have no message, agent, or approval. */
export type JobKind = 'message' | 'script' | 'command'

/** Derive the execution kind of a job from which field it carries. */
export function jobKindOf(job?: CronJob): JobKind {
  if (job?.script) return 'script'
  if (job?.command) return 'command'
  return 'message'
}

/** Parse a CronJob into initial form state */
function parseJobDefaults(job?: CronJob) {
  if (!job) return { name: '', message: '', agent: '', model: '', channel: '', approvalMode: '', silent: false, strictSchedule: false, hideInChat: false, jobKind: 'message' as JobKind, schedMode: 'interval' as const, intVal: 1, intUnit: 'hours' as const, weekDays: [] as number[], weekTime: '09:00', cronExpr: '' }
  const isInterval = !!(job.every_secs || (job.schedule || '').match(/^every\s+\d+/))
  const secs = job.every_secs || (() => { const m = (job.schedule || '').match(/^every\s+(\d+)\s*([sh])/); if (!m) return 3600; return m[2] === 'h' ? parseInt(m[1]) * 3600 : parseInt(m[1]) })()
  const intUnit = secs >= 86400 ? 'days' as const : secs >= 3600 ? 'hours' as const : 'minutes' as const
  const intVal = Math.max(1, Math.round(intUnit === 'days' ? secs / 86400 : intUnit === 'hours' ? secs / 3600 : secs / 60))
  const cronRaw = job.cron_expr || ''
  const cronParts = cronRaw.split(/\s+/)
  // Weekly mode can only represent a single plain minute/hour pair plus a day
  // set expandDow understands. A list, range, or step in the minute or hour
  // field (e.g. `0 9,12,15 * * 1-5`) must fall through to cron mode, where the
  // raw expression round-trips verbatim — parseInt would silently truncate
  // '9,12,15' to 9 and a save would drop the other run times (#8469).
  // /^\d{1,2}$/ matches cronClock's plain-field grammar in cronUtils so the
  // list view and the editor classify the same job the same way.
  const isPlainField = (s: string, max: number) => /^\d{1,2}$/.test(s) && parseInt(s, 10) <= max
  // The day-of-week field must be FULLY representable: expandDow drops
  // segments it cannot parse (`1,3-5/2` expands to just [1]), so a whole-field
  // non-empty check would still collapse the unsupported part on save. Each
  // comma segment must expand on its own, and numeric tokens must stay in
  // cron's 0-7 range — parseDowToken wraps 8 to Monday via % 7, which would
  // silently rewrite the expression.
  const isRepresentableDow = (field: string) => field.split(',').every(seg =>
    !seg.split('-').some(tok => /^\d+$/.test(tok) && parseInt(tok, 10) > 7) && expandDow(seg).length > 0)
  const isWeekly = !isInterval && cronParts.length === 5 && cronParts[4] !== '*' && cronParts[2] === '*' && cronParts[3] === '*'
    && isPlainField(cronParts[0], 59) && isPlainField(cronParts[1], 23) && isRepresentableDow(cronParts[4])
  const schedMode = isInterval ? 'interval' as const : isWeekly ? 'weekly' as const : 'cron' as const
  // Read cron time and days directly (stored in job timezone, not UTC)
  let weekDays: number[] = []
  let weekTime = '09:00'
  if (isWeekly) {
    const h = parseInt(cronParts[1]), m = parseInt(cronParts[0])
    weekDays = expandDow(cronParts[4]).map(d => CRON_DOW_TO_GRID[d] || 1)
    weekTime = `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`
  }
  return { name: job.name, message: job.message, agent: job.agent || '', model: job.model || '', channel: job.channel || '', approvalMode: job.approval_mode || '', silent: job.silent || false, strictSchedule: job.strict_schedule || false, hideInChat: job.hide_in_chat || false, jobKind: jobKindOf(job), schedMode, intVal, intUnit, weekDays, weekTime, cronExpr: cronRaw }
}

/** Build the API body from form state. Returns null if validation fails (sets error). */
function buildBody(
  f: ReturnType<typeof parseJobDefaults>,
  tz: string,
  setError: (e: string) => void,
  isEdit = false,
): Record<string, string | number | boolean> | null {
  const isLlmless = f.jobKind === 'script' || f.jobKind === 'command'
  // Script/command crons have no agent message — only the agent/message kind
  // requires one. For LLM-less jobs we omit message/agent/model/approval entirely
  // so the partial PATCH preserves the script/command binding (the update endpoint
  // does not accept script/command, so we never send them — only the fields it
  // supports: schedule, channel, silent, strict, hide-in-chat, timezone).
  if (!f.name) { setError(i18nT('components.jobForm.name_is_required')); return null }
  if (!isLlmless && !f.message) { setError(i18nT('components.jobForm.message_is_required')); return null }
  const body: Record<string, string | number | boolean> = { name: f.name }
  if (!isLlmless) {
    body.message = f.message
    body.agent = f.agent
    // Edit mode always sends model so clearing an override ("" = inherit)
    // persists; create mode omits it when empty like other optional fields.
    if (isEdit || f.model) body.model = f.model
    if (f.approvalMode) body.approval_mode = f.approvalMode
  }
  if (f.channel) body.channel = f.channel
  body.silent = f.silent
  body.strict_schedule = f.strictSchedule
  body.hide_in_chat = f.hideInChat
  if (f.schedMode === 'interval') {
    body.every = f.intVal * (f.intUnit === 'minutes' ? 60 : f.intUnit === 'hours' ? 3600 : 86400)
  } else if (f.schedMode === 'weekly') {
    if (f.weekDays.length === 0) { setError(i18nT('components.jobForm.select_at_least_one_day')); return null }
    const [h, m] = f.weekTime.split(':').map(Number)
    body.cron = `${m} ${h} * * ${f.weekDays.map(d => GRID_TO_CRON_DOW[d]).join(',')}`
    body.timezone = tz
  } else {
    const expr = f.cronExpr.trim()
    if (expr.split(/\s+/).length !== 5) { setError(i18nT('components.jobForm.enter_a_valid_5_field_cron_expression')); return null }
    body.cron = expr
    body.timezone = tz
  }
  return body
}

/** One row of `GET /api/models`. The payload is kiro-cli's own `--list-models`
 *  output after the backend's filtering, so nothing here is guaranteed: the
 *  current spelling is `model_name`, `name` is the legacy one, and a row that
 *  carries neither is unusable. */
type ModelRow = { model_name?: string; name?: string; display_name?: string }

interface Props {
  job?: CronJob // if provided, edit mode
  /** Seed values for a NEW job (create mode). Ignored when `job` is set. */
  prefill?: CronPrefill
  agents: KiroCrewAgent[]
  defaultAgent: string
  /** The roster fetch failed — see AgentSelector's prop of the same name. */
  rosterFailure?: { reloading: boolean; onReload: () => void }
  /** Pin the job to ONE crew: the agent field renders as a fixed value instead
   *  of a selector, and the submit body always carries this name. For hosts
   *  that embed the form inside a single crew's own surface, where offering a
   *  crew picker would just be a way to file the job in the wrong place. */
  lockedAgent?: string
  onSaved: () => void
  /** Vertical layout for side panel, horizontal for inline create */
  layout?: 'vertical' | 'horizontal'
  /** If true, the component won't render its own submit button (parent renders it) */
  externalSubmit?: boolean
  /** Ref callback — parent can call this to trigger submit */
  submitRef?: React.MutableRefObject<(() => void) | null>
  /** Called when saving state changes */
  onSavingChange?: (saving: boolean) => void
  /** Called when the form's TOUCHED state changes: true once any field has
   *  diverged from its initial value, false when they all match again (or
   *  after a successful create resets them). Hosts that guard destruction
   *  paths key on this rather than on mere open-ness, so looking at an empty
   *  form and backing out never triggers a "your typed work will be lost"
   *  confirm about work that does not exist. */
  onDirtyChange?: (dirty: boolean) => void
}

export default function JobForm({ job, prefill, agents, defaultAgent, rosterFailure, lockedAgent, onSaved, layout = 'horizontal', externalSubmit, submitRef, onSavingChange, onDirtyChange }: Props) {
  // "" and undefined both mean unlocked, so render and submit share one truth.
  const locked = lockedAgent || undefined
  const defaults = parseJobDefaults(job)
  // In create mode (no job), a preset can seed the prompt + schedule fields.
  // Edit mode always reflects the job as-stored and ignores any prefill.
  const init = !job && prefill
    ? {
      ...defaults,
      name: prefill.name,
      message: prefill.message,
      schedMode: prefill.schedMode,
      intVal: prefill.intVal ?? defaults.intVal,
      intUnit: prefill.intUnit ?? defaults.intUnit,
      weekDays: prefill.weekDays ?? defaults.weekDays,
      weekTime: prefill.weekTime ?? defaults.weekTime,
      cronExpr: prefill.cronExpr ?? defaults.cronExpr,
      silent: prefill.silent ?? defaults.silent,
    }
    : defaults
  const [name, setName] = useState(init.name)
  const [msg, setMsg] = useState(init.message)
  const [agent, setAgent] = useState(defaults.agent)
  const [model, setModel] = useState(defaults.model)
  const { data: modelList = [] } = useQuery<{ name: string; description?: string }[]>({
    queryKey: ['models'],
    queryFn: async () => {
      const m = await api.models()
      // A row carrying neither spelling is dropped, not mapped to '': '' is
      // this form's own value for "inherit" (the `clearLabel` row, see
      // `modelOptions` below), so aliasing an unusable row onto it would render
      // a second, duplicate inherit option that silently clears the override.
      if (!Array.isArray(m)) return []
      return m.flatMap((x: ModelRow) => {
        const name = x.model_name || x.name
        return name ? [{ name, description: x.display_name || '' }] : []
      })
    },
  })
  const [channel, setChannel] = useState(defaults.channel)
  const [approvalMode, setApprovalMode] = useState(defaults.approvalMode)
  const [silent, setSilent] = useState(init.silent)
  const [strictSchedule, setStrictSchedule] = useState(defaults.strictSchedule)
  const [hideInChat, setHideInChat] = useState(defaults.hideInChat)
  const [schedMode, setSchedMode] = useState(init.schedMode)
  const [intVal, setIntVal] = useState(init.intVal)
  const [intUnit, setIntUnit] = useState(init.intUnit)
  const [weekDays, setWeekDays] = useState(init.weekDays)
  const [weekTime, setWeekTime] = useState(init.weekTime)
  const [tz, setTz] = useState(() => job ? (job.timezone || 'UTC') : Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [cronExpr, setCronExpr] = useState(init.cronExpr)
  // Touched = any field diverged from what the form OPENED with. Compared
  // against `init`/`defaults` (the same sources the state seeded from), so a
  // value typed and then typed back reads as untouched again — the same rule
  // the crew editor's own dirtyPanes uses. tz is excluded: its initial value
  // is the machine's zone, an environment fact rather than user work worth a
  // discard confirm. Reported through an effect keyed on the recomputed
  // boolean, so hosts only hear about EDGES, not every keystroke.
  const dirty =
    name !== init.name || msg !== init.message ||
    agent !== defaults.agent || model !== defaults.model ||
    channel !== defaults.channel || approvalMode !== defaults.approvalMode ||
    silent !== init.silent || strictSchedule !== defaults.strictSchedule ||
    hideInChat !== defaults.hideInChat || schedMode !== init.schedMode ||
    intVal !== init.intVal || intUnit !== init.intUnit ||
    weekTime !== init.weekTime || cronExpr !== init.cronExpr ||
    weekDays.length !== init.weekDays.length || weekDays.some((d, i) => d !== init.weekDays[i])
  const dirtyChangeRef = useRef(onDirtyChange)
  dirtyChangeRef.current = onDirtyChange
  useEffect(() => { dirtyChangeRef.current?.(dirty) }, [dirty])
  // Unmount clears the flag for the same reason CrewWakeSection's own
  // cleanup does: the work no longer exists, so no host may keep gating on it.
  useEffect(() => () => { dirtyChangeRef.current?.(false) }, [])
  const [error, setErrorState] = useState('')
  // When the submit control lives in a host's header (`externalSubmit`) the
  // form can be taller than its pane, so a failed submit's notice — rendered
  // at the form's bottom — lands below the fold and the click looks like a
  // dead button. Bring the notice to the failed click, whichever layout. The
  // tick makes a REPEATED identical failure scroll again (batching collapses
  // `setError('')` + same message into no state change); the optional call
  // guards jsdom, which has no scrollIntoView.
  const errorRef = useRef<HTMLDivElement | null>(null)
  const [errorTick, setErrorTick] = useState(0)
  const setError = (e: string) => { setErrorState(e); if (e) setErrorTick(t => t + 1) }
  useEffect(() => {
    if (error) errorRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [error, errorTick])
  const [saving, setSavingState] = useState(false)
  const setSaving = (v: boolean) => { setSavingState(v); onSavingChange?.(v) }

  // Execution kind is fixed by the job being edited (script/command/message);
  // the create form has no job, so it is always the agent-message kind.
  const jobKind = defaults.jobKind
  const isLlmless = jobKind === 'script' || jobKind === 'command'

  /** Model-override rows as the two parallel arrays `SimpleSelect` takes.
   *
   *  "" (inherit) is the `clearLabel` row rather than an option, so `options`
   *  holds only real model names. A model already saved on the job that the
   *  backend no longer advertises is prepended — same position the old
   *  `<option>` held — so an existing override never silently disappears from
   *  the picker. Both layouts render this list, so it is built once. */
  const modelOptions = useMemo(() => {
    const values = modelList.map(m => m.name)
    const labels = modelList.map(m => m.description || m.name)
    if (model && !values.includes(model)) { values.unshift(model); labels.unshift(model) }
    return { values, labels }
  }, [modelList, model])

  const submit = async () => {
    setError(''); setSaving(true)
    const f = { name, message: msg, agent: locked ?? agent, model, channel, approvalMode, silent, strictSchedule, hideInChat, jobKind, schedMode, intVal, intUnit, weekDays, weekTime, cronExpr }
    const body = buildBody(f, tz, setError, !!job)
    if (!body) { setSaving(false); return }
    try {
      const res = job
        ? await api.updateCron(job.id, body)
        : await api.createCron(body).catch((e: Error) => ({ error: e.message }))
      if (res.error) { setError(res.error); setSaving(false); return }
      if (!job) { setName(''); setMsg(''); setWeekDays([]); setIntVal(1); setChannel(''); setModel(''); setApprovalMode(''); setSilent(false); setStrictSchedule(false); setHideInChat(false) }
      onSaved()
    } catch { setError(i18nT('components.jobForm.failed_to_save')); setSaving(false) }
  }

  const toggleDay = (d: number) => setWeekDays(prev => prev.includes(d) ? prev.filter(x => x !== d) : [...prev, d].sort())

  // Expose submit to parent via ref
  if (submitRef) submitRef.current = submit

  const vertical = layout === 'vertical'

  /** The job's timezone picker, rendered identically by the weekly and the
   *  cron-expression branch (it was the same markup twice).
   *
   *  `TIMEZONES` is a curated 15-zone fast-pick list, not the IANA set, so this
   *  is a `SimpleSelect` — the searchable variant is for the full host list
   *  (see `TimezoneSelect`). The stored zone is unioned in at the front so a
   *  job saved with a zone outside the curated list keeps it. */
  const tzOptions = Array.from(new Set([tz, ...TIMEZONES]))
  const tzSelect = (
    <SimpleSelect
      aria-label={i18nT('components.jobForm.timezone')}
      options={tzOptions}
      optionLabels={tzOptions.map(z => z.replace(/_/g, ' '))}
      value={tz}
      onChange={setTz}
      // The vertical (Schedule sidebar) layout runs its row at 12px; without this
      // the trigger would sit at the shared `text-sm` default while every sibling
      // stayed 12px. The horizontal layout keeps the default and takes a fixed
      // flex basis instead.
      className={vertical ? 'text-[12px]' : undefined}
      style={vertical ? {} : { flex: '0 0 200px' }}
    />
  )

  return (
    <div className="flex flex-col gap-3">
      {vertical ? (<>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.name')}</span>
          <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.a_short_label_for_this_job')}</span>
          <Input id="jobform-name" aria-label={i18nT('components.jobForm.name')} value={name} onChange={e => setName(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          {job?.script ? (<>
            <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.script')}</span>
            <code className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[12px] font-mono break-all">{job.script}</code>
          </>) : job?.command ? (<>
            <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.command')}</span>
            <code className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-[12px] font-mono break-all">{job.command}</code>
          </>) : (
          <div className="flex flex-col gap-1">
            <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.message')}</span>
            <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.the_prompt_or_task_sent_to_the_agent_when_this_j')}</span>
            <textarea id="jobform-message" aria-label={i18nT('components.jobForm.message')} className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none resize-y min-h-[60px] focus-ring" value={msg} onChange={e => setMsg(e.target.value)} />
          </div>)}
        </div>
      </>) : (
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder={i18nT('components.jobForm.job_name')} value={name} onChange={e => setName(e.target.value)} />
          <Input placeholder={i18nT('components.jobForm.message_task')} style={{ flex: 2 }} value={msg} onChange={e => setMsg(e.target.value)} />
          {locked
            ? <LockedAgentValue name={locked} />
            : <AgentSelector agents={agents} defaultAgent={defaultAgent} value={agent} onChange={(name) => setAgent(name)} rosterFailure={rosterFailure} modal />}
          <SimpleSelect
            options={modelOptions.values}
            optionLabels={modelOptions.labels}
            value={model}
            onChange={setModel}
            clearLabel={i18nT('components.jobForm.model_inherit')}
            aria-label={i18nT('components.jobForm.model')}
          />
          <Input placeholder={i18nT('components.jobForm.channel_id_optional')} style={{ flex: '0 0 170px' }} value={channel} onChange={e => setChannel(e.target.value)} />
          <SimpleSelect
            aria-label={i18nT('components.jobForm.approval')}
            options={['auto']}
            optionLabels={[i18nT('components.jobForm.auto')]}
            value={approvalMode}
            onChange={setApprovalMode}
            clearLabel={i18nT('components.jobForm.approval_default')}
          />
          <label htmlFor="jobform-silent" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="jobform-silent" aria-label={i18nT('components.jobForm.silent')} type="checkbox" checked={silent} onChange={e => setSilent(e.target.checked)} /> {i18nT('components.jobForm.silent')}</label>
          <label htmlFor="jobform-strict-schedule" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="jobform-strict-schedule" aria-label={i18nT('components.jobForm.strict_schedule')} type="checkbox" checked={strictSchedule} onChange={e => setStrictSchedule(e.target.checked)} /> {i18nT('components.jobForm.strict_schedule')}</label>
          <label htmlFor="jobform-hide-in-chat" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="jobform-hide-in-chat" aria-label={i18nT('components.jobForm.hide_in_chat')} type="checkbox" checked={hideInChat} onChange={e => setHideInChat(e.target.checked)} /> {i18nT('components.jobForm.hide_in_chat')}</label>
        </div>
      )}

      {/* Schedule */}
      {vertical && <div className="flex flex-col gap-0.5"><span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.schedule')}</span><span className="text-[11px] text-muted/70">{i18nT('components.jobForm.how_often_this_job_runs')}</span></div>}
      <div className={`flex gap-2 items-center flex-wrap ${vertical ? '' : ''}`}>
        <SimpleSelect
          options={['interval', 'weekly', 'cron']}
          optionLabels={[i18nT('components.jobForm.every_interval'), i18nT('components.jobForm.weekly_schedule'), i18nT('components.jobForm.cron_expression')]}
          value={schedMode}
          onChange={v => setSchedMode(v as 'interval' | 'weekly' | 'cron')}
          aria-label={i18nT('components.jobForm.schedule')}
        />
        {schedMode === 'interval' ? (<>
          <Input type="number" min={1} style={{ flex: '0 0 70px' }} value={intVal} onChange={e => setIntVal(Math.max(1, parseInt(e.target.value) || 1))} />
          <SimpleSelect
            aria-label={i18nT('components.jobForm.every_interval')}
            options={['minutes', 'hours', 'days']}
            optionLabels={[i18nT('components.jobForm.minutes'), i18nT('components.jobForm.hours'), i18nT('components.jobForm.days')]}
            value={intUnit}
            onChange={v => setIntUnit(v as 'minutes' | 'hours' | 'days')}
          />
        </>) : schedMode === 'weekly' ? (<>
          <div className="flex gap-1 flex-wrap">{dayNames().map((d, i) => (
            <button key={d} type="button" onClick={() => toggleDay(i + 1)} className={`px-2 py-1 rounded-md text-[12px] font-medium border cursor-pointer transition-all ${weekDays.includes(i + 1) ? 'bg-accent text-accent-fg border-accent' : 'bg-bg-elevated text-muted border-border hover:border-border-strong'}`}>{d}</button>
          ))}</div>
          <span className="text-muted text-[13px]">{i18nT('components.jobForm.at')}</span>
          <Input type="time" style={{ flex: '0 0 100px' }} value={weekTime} onChange={e => setWeekTime(e.target.value)} />
          {tzSelect}
        </>) : (<>
          <Input value={cronExpr} onChange={e => setCronExpr(e.target.value)} placeholder="0 9 * * 1-5" />
          {tzSelect}
        </>)}
        {!vertical && !externalSubmit && <SendBtn onClick={submit} disabled={saving}>{saving ? i18nT('components.jobForm.saving') : (job ? i18nT('components.jobForm.save') : i18nT('components.jobForm.add'))}</SendBtn>}
      </div>

      {/* Vertical-only: agent, channel, actions */}
      {vertical && (<>
        {/* Agent and Approval are agent/message concepts — script/command crons
            run no LLM, so hide them (consistent with the LLM-less create surface). */}
        {!isLlmless && (<>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.agent')}</span>
          {locked
            ? <LockedAgentValue name={locked} />
            : (<>
              <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.which_agent_handles_this_job_leave_default_for_t')}</span>
              <AgentSelector agents={agents} defaultAgent={defaultAgent} value={agent} onChange={(name) => setAgent(name)} rosterFailure={rosterFailure} modal />
            </>)}
        </div>
        </>)}
        {!isLlmless && (
        <div className="flex flex-col gap-1">
          {/* A <span>, not a <label>: the control below renders a button, which
              a <label> cannot associate with — the accessible name rides on
              aria-label instead. Matches every sibling field in this form. */}
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.model')}</span>
          <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.override_the_model_for_this_job_leave_on_inherit')}</span>
          <SimpleSelect
            options={modelOptions.values}
            optionLabels={modelOptions.labels}
            value={model}
            onChange={setModel}
            clearLabel={i18nT('components.jobForm.inherit_from_agent')}
            aria-label={i18nT('components.jobForm.model')}
          />
        </div>
        )}
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.channel_id')}</span>
          <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.slack_channel_to_post_results_to_leave_empty_for')}</span>
          <Input id="jobform-channel" aria-label={i18nT('components.jobForm.channel_id')} value={channel} onChange={e => setChannel(e.target.value)} placeholder={i18nT('components.jobForm.optional')} />
        </div>
        {!isLlmless && (
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.approval')}</span>
          <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.how_tool_calls_are_approved_during_execution')}</span>
          <SimpleSelect
            options={['auto']}
            optionLabels={[i18nT('components.jobForm.auto_approve')]}
            value={approvalMode}
            onChange={setApprovalMode}
            clearLabel={i18nT('components.jobForm.default')}
            aria-label={i18nT('components.jobForm.approval')}
          />
        </div>
        )}
        <SettingsToggle
          label={i18nT('components.jobForm.silent_mode')}
          description={i18nT('components.jobForm.suppress_automatic_message_delivery_the_agent_co')}
          checked={silent}
          onChange={setSilent}
        />
        <SettingsToggle
          label={i18nT('components.jobForm.strict_schedule')}
          description={i18nT('components.jobForm.fire_exactly_on_schedule_with_no_jitter_by_defau')}
          checked={strictSchedule}
          onChange={setStrictSchedule}
        />
        <SettingsToggle
          label={i18nT('components.jobForm.hide_in_chat')}
          description={i18nT('components.jobForm.keep_this_job_s_runs_out_of_the_active_session_l')}
          checked={hideInChat}
          onChange={setHideInChat}
        />
        {vertical && !externalSubmit && (
          <SendBtn onClick={submit} disabled={saving}>
            <SaveCreateLabel isEdit={!!job} saving={saving} />
          </SendBtn>
        )}
      </>)}

      {/* No hand-off: the notice sits beside unsaved form input, and the button
          navigates away — which would discard what the user typed. */}
      <div ref={errorRef}>
        <ErrorNotice message={error} />
      </div>
    </div>
  )
}

export { buildBody, parseJobDefaults }
