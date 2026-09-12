import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Zap, FolderOpen } from 'lucide-react'
import { api } from '../api/client'
import { Btn, Input, SendBtn } from './ui'
import { SettingsToggle } from './settings'
import AgentSelector, { type KiroCrewAgent } from './AgentSelector'
import ProjectPicker from './ProjectPicker'
import SimpleSelect from './SimpleSelect'
import type { CronJob } from '../types'
import type { CronPrefill } from '../utils/schedulePresets'
import { SaveCreateLabel, expandDow } from '../utils/cronUtils'
import { adviseCronMode } from '../utils/cronModeAdvice'

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
  if (!job) return { name: '', message: '', agent: '', model: '', channel: '', approvalMode: '', silent: false, strictSchedule: false, hideInChat: false, minimalContext: false, jobKind: 'message' as JobKind, schedMode: 'interval' as const, intVal: 1, intUnit: 'hours' as const, weekDays: [] as number[], weekTime: '09:00', cronExpr: '', projectPath: '' }
  const isInterval = !!(job.every_secs || (job.schedule || '').match(/^every\s+\d+/))
  const secs = job.every_secs || (() => { const m = (job.schedule || '').match(/^every\s+(\d+)\s*([smh])/); if (!m) return 3600; return parseInt(m[1]) * (m[2] === 'h' ? 3600 : m[2] === 'm' ? 60 : 1) })()
  // Largest unit that divides `secs` EVENLY, not the largest unit that is merely
  // <= `secs`. The magnitude test sent 5400s to 'hours', where Math.round(1.5) is
  // 2, and buildBody re-serialises `intVal * 3600` — so opening a 90-minute job
  // and saving an unrelated field silently rewrote its schedule to 2 hours. That
  // is the #8469 class on the interval side: 90 minutes IS representable here, and
  // the magnitude choice discarded that representation before rounding ever ran.
  const evenUnit = secs % 86400 === 0 ? 'days' as const
    : secs % 3600 === 0 ? 'hours' as const
    : secs % 60 === 0 ? 'minutes' as const
    : null
  // Nothing divides evenly (e.g. 90s): no unit offered here can represent that
  // schedule, so keep the pre-existing nearest-magnitude choice rather than widen
  // the unit set. Sub-minute precision is a separate question from this defect.
  const intUnit = evenUnit ?? (secs >= 86400 ? 'days' as const : secs >= 3600 ? 'hours' as const : 'minutes' as const)
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
  return { name: job.name, message: job.message, agent: job.agent || '', model: job.model || '', channel: job.channel || '', approvalMode: job.approval_mode || '', silent: job.silent || false, strictSchedule: job.strict_schedule || false, hideInChat: job.hide_in_chat || false, minimalContext: job.minimal_context || false, jobKind: jobKindOf(job), schedMode, intVal, intUnit, weekDays, weekTime, cronExpr: cronRaw, projectPath: job.project_path || '' }
}

/** Remap the backend's raw `project_path` validation errors (which use the
 *  wire field name and say nothing about the field being optional) to the
 *  UI's own field label, so a rejected path reads as a helpful correction
 *  rather than "failed to save" with no clue why. The backend's error
 *  vocabulary is shared with the CLI/MCP tool and other callers, so this
 *  stays a display-only remap here rather than a change to those strings.
 *  Falls through to the raw message for anything else (network errors,
 *  other 4xx/5xx) so nothing is silently swallowed. */
function friendlyProjectPathError(raw: string): string {
  if (raw.includes('project_path must be an absolute path')) {
    return i18nT('components.jobForm.working_directory_must_be_an_absolute_path')
  }
  if (raw.includes('project_path refers to a sensitive path')) {
    return i18nT('components.jobForm.working_directory_refers_to_a_protected_path')
  }
  if (raw.includes('project_path must be an existing directory')) {
    return i18nT('components.jobForm.working_directory_must_be_an_existing_directory')
  }
  return raw
}

/** Build the API body from form state. Returns null if validation fails (sets error). */
function buildBody(
  f: ReturnType<typeof parseJobDefaults>,
  tz: string,
  setError: (e: string) => void,
  isEdit = false,
  prefill?: CronPrefill,
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
    // Only the agent kind has an injected context to trim. A script or command
    // job takes no agent turn, so sending this would store a flag that can
    // never do anything.
    body.minimal_context = f.minimalContext
  }
  if (f.channel) body.channel = f.channel
  body.silent = f.silent
  body.strict_schedule = f.strictSchedule
  body.hide_in_chat = f.hideInChat
  // "" is a valid, meaningful value here (clears the binding back to
  // global-agent-only on an edit), so it is sent unconditionally rather than
  // gated behind a truthiness check like the optional fields above.
  body.project_path = f.projectPath
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
  // Provenance stamp, create-only: the template this job was seeded from, plus
  // the template's prompt AS IT WAS when picked (the snapshot the Schedule page
  // compares against the template's current prompt to detect a template change,
  // independent of any edit the user makes to the Message field below). The
  // PATCH endpoint does not accept either (provenance is fixed at creation), so
  // they are never sent on edit.
  if (!isEdit && prefill?.sourcePreset) {
    body.source_preset = prefill.sourcePreset
    body.source_template_prompt = prefill.sourceTemplatePrompt ?? ''
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
  /** Durable member identity, distinct from its provider template. */
  memberId?: string
  providerAgent?: string
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

export default function JobForm({ job, prefill, agents, defaultAgent, rosterFailure, lockedAgent, memberId, providerAgent, onSaved, layout = 'horizontal', externalSubmit, submitRef, onSavingChange, onDirtyChange }: Props) {
  // "" and undefined both mean unlocked, so render and submit share one truth.
  const boundMember = job?.member_id || memberId
  const privateMember = !!boundMember && boundMember !== 'default'
  const locked = boundMember || lockedAgent || undefined
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
  const [minimalContext, setMinimalContext] = useState(defaults.minimalContext)
  const [schedMode, setSchedMode] = useState(init.schedMode)
  const [intVal, setIntVal] = useState(init.intVal)
  const [intUnit, setIntUnit] = useState(init.intUnit)
  const [weekDays, setWeekDays] = useState(init.weekDays)
  const [weekTime, setWeekTime] = useState(init.weekTime)
  const [tz, setTz] = useState(() => job ? (job.timezone || 'UTC') : Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [cronExpr, setCronExpr] = useState(init.cronExpr)
  const [projectPath, setProjectPath] = useState(init.projectPath)
  const [pickerOpen, setPickerOpen] = useState(false)
  const browseRef = useRef<HTMLButtonElement>(null)
  // Project-scoped roster for THIS job's project_path — a raw path, no live
  // chat slot behind it, so this is the project_path fallback (Decision 1).
  // A local effect rather than useAgents(): that hook unconditionally syncs +
  // fetches on every mount regardless of its args, which would double every
  // JobForm's roster work even for the common case of no project_path set.
  // This only does anything once a path is actually present.
  const [projectAgents, setProjectAgents] = useState<KiroCrewAgent[]>([])
  // Separate from the form-wide `error` (validation failures on Save): a
  // background roster fetch failing must not borrow that channel, which
  // (1) auto-scrolls the page to the bottom-of-form notice on every set,
  // interrupting a user who is calmly typing elsewhere in the form for a
  // failure unrelated to what they are doing, and (2) shares one string with
  // Save-time validation, so a stale roster error can sit through an
  // otherwise-successful save (only `handleSave`'s `setError('')` clears it),
  // or a validation error can be silently clobbered by a late-resolving
  // roster retry. Rendered beside the working-directory field itself instead.
  const [projectRosterError, setProjectRosterError] = useState('')
  useEffect(() => {
    if (!projectPath) {
      setProjectAgents([])
      setProjectRosterError('')
      // The selected agent may have been a project-scoped one that only
      // existed because THIS folder was open (effectiveAgents merged it in
      // from projectAgents, now cleared above). Left alone, that name is
      // still sent on save (`agent: locked ?? agent`) with an empty
      // project_path -- Kiro cannot resolve it without the project scope
      // and silently falls back to the default agent's prompt, tools, and
      // permissions, with no error surfaced anywhere. Clear it back to the
      // global default whenever it is not a name the global roster itself
      // recognizes; a global agent that happens to share a name is left
      // untouched, matching effectiveAgents' own dedup-by-name rule.
      setAgent(a => (a && !agents.some(g => g.name === a) ? '' : a))
      return
    }
    // Clear immediately on folder change, before the fetch even starts: the
    // `cancelled` flag below only stops a SUPERSEDED fetch's result from
    // overwriting a newer one, but leaves the PREVIOUS folder's now-stale
    // agents selectable in the picker for the whole in-flight gap. A user who
    // switches folders and picks an agent in that gap would get an agent from
    // the folder they just left.
    setProjectAgents([])
    setProjectRosterError('')
    let cancelled = false
    api.kirocrewAgents(undefined, projectPath).then(d => {
      if (!cancelled) setProjectAgents(d.agents || [])
    }).catch(e => {
      if (cancelled) return
      // A roster-fetch failure must be VISIBLE, not a silent empty list: the
      // user picked this folder specifically to see its agents, and an empty
      // roster with no explanation reads as "this folder has none" rather
      // than "the request failed" -- indistinguishable failure modes that
      // need different next actions (retry vs. pick a different folder).
      // api.kirocrewAgents throws ApiError/Error with an already-friendly
      // message (apiFailure's friendlyErrText), so no remap is needed here --
      // friendlyProjectPathError is for the three raw project_path validation
      // strings the SAVE path can surface, which this read endpoint does not.
      setProjectAgents([])
      // Suffixed with the hand-off this failure causes: the agent picker
      // below falls back to the global roster (effectiveAgents returns
      // `agents` when projectAgents is empty), which the raw fetch error
      // alone does not say -- without it, an empty-looking roster and a
      // silently-substituted one are indistinguishable.
      const rosterErr = e instanceof Error ? e.message : String(e)
      setProjectRosterError(
        `${rosterErr} ${i18nT('components.jobForm.project_roster_error_falls_back_to_global_agents')}`,
      )
    })
    return () => { cancelled = true }
  }, [projectPath, agents])
  // Global roster (the `agents` prop) plus this job's own project-scoped
  // agents, deduped by name — a project agent that happens to share a global
  // agent's name is not offered twice. Project rows arrive tagged
  // `source: 'project'` by the server, and are shown as such without a
  // per-folder relabel: this form only ever merges ONE folder's agents at a
  // time, so the generic "project" badge already identifies where an agent
  // came from unambiguously — a folder-name badge would only earn its keep
  // if more than one folder's agents could appear in the same dropdown at
  // once, which does not happen here. Merging here is the only way a per-job
  // path (not known to the page-level roster) can ever appear in this picker
  // at all.
  const effectiveAgents = useMemo(() => {
    if (!projectPath || projectAgents.length === 0) return agents
    const globalNames = new Set(agents.map(a => a.name))
    const labeled = projectAgents.filter(a => !globalNames.has(a.name))
    return [...agents, ...labeled]
  }, [agents, projectAgents, projectPath])
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
    minimalContext !== defaults.minimalContext ||
    intVal !== init.intVal || intUnit !== init.intUnit ||
    weekTime !== init.weekTime || cronExpr !== init.cronExpr ||
    projectPath !== init.projectPath ||
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

  // Recomputed as the prompt is typed, which is why it is a local regex pass
  // and not a round trip. Reads minimalContext too, so the hint stops once the
  // reader has acted on it.
  const advice = useMemo(() => privateMember ? 'none' : adviseCronMode(msg, minimalContext), [msg, minimalContext, privateMember])

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
    const f = { name, message: msg, agent: locked ?? agent, model, channel, approvalMode, silent, strictSchedule, hideInChat, minimalContext, jobKind, schedMode, intVal, intUnit, weekDays, weekTime, cronExpr, projectPath }
    const body = buildBody(f, tz, setError, !!job, job ? undefined : prefill)
    if (!body) { setSaving(false); return }
    if (boundMember && !isLlmless) {
      body.member_id = boundMember
      body.agent = providerAgent || job?.agent || ''
    }
    try {
      const res = job
        ? await api.updateCron(job.id, body).catch((e: Error) => ({ error: e.message }))
        : await api.createCron(body).catch((e: Error) => ({ error: e.message }))
      if (res.error) { setError(friendlyProjectPathError(res.error)); setSaving(false); return }
      if (!job) { setName(''); setMsg(''); setWeekDays([]); setIntVal(1); setChannel(''); setModel(''); setApprovalMode(''); setSilent(false); setStrictSchedule(false); setHideInChat(false); setMinimalContext(false); setProjectPath('') }
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
            : <AgentSelector agents={effectiveAgents} defaultAgent={defaultAgent} value={agent} onChange={(name) => setAgent(name)} rosterFailure={rosterFailure} modal />}
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
          <label htmlFor="jobform-minimal-context" className="flex items-center gap-1.5 text-muted text-[13px] cursor-pointer"><input id="jobform-minimal-context" aria-label={i18nT('components.jobForm.minimal_context')} type="checkbox" checked={minimalContext} onChange={e => setMinimalContext(e.target.checked)} /> {i18nT('components.jobForm.minimal_context')}</label>
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
        {/* Working directory (like Agent/Approval below) is an agent/message
            concept: it is only ever read at fire time by the LLM-agent cron
            paths in gateway.py (single-agent and sequential), never by a
            script/command job's subprocess dispatch in cron.py, which passes
            no cwd derived from it. Showing the field for script/command jobs
            would display help text that talks about "this job's agent" when
            that job kind has none, and — since save-time validation runs
            unconditionally on any non-empty project_path — could 400 the
            save over a value the job would never actually use. Guarding it
            the same as Agent/Approval keeps the field's presence consistent
            with what fire time actually reads. */}
        {!isLlmless && (<>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.working_directory')} <span className="text-muted/60 font-normal">({i18nT('components.jobForm.optional')})</span></span>
          <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.run_this_job_s_agent_in_this_folder_and_offer')}</span>
          <div className="flex gap-2">
            <Input
              className="flex-1 min-w-0 font-mono text-[12px]"
              id="jobform-project-path"
              aria-label={i18nT('components.jobForm.working_directory')}
              value={projectPath}
              onChange={e => setProjectPath(e.target.value)}
              placeholder="/Users/you/projects/myrepo"
            />
            <Btn ref={browseRef} onClick={() => setPickerOpen(true)}>
              <FolderOpen size={13} /> {i18nT('components.jobForm.browse')}
            </Btn>
          </div>
          {/* No hand-off: this notice sits inside the job form whose fields
              (name, message, schedule, working directory) are still live —
              the hand-off navigates to chat and would discard them. */}
          {projectRosterError && (
            <ErrorNotice variant="inline" testId="jobform-project-roster-error" message={projectRosterError} />
          )}
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.agent')}</span>
          {locked
            ? <LockedAgentValue name={locked} />
            : (<>
              <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.which_agent_handles_this_job_leave_default_for_t')}</span>
              <AgentSelector agents={effectiveAgents} defaultAgent={defaultAgent} value={agent} onChange={(name) => setAgent(name)} rosterFailure={rosterFailure} modal />
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
        {!isLlmless && (
          <div className="flex flex-col gap-1">
            {/* Advice, not a warning, so accent rather than warn. Sits directly
                above the control it refers to: a hint that names a setting the
                reader then has to hunt for is a worse hint. */}
            {advice !== 'none' && (
              <div
                className="flex items-start gap-2 px-3 py-2 rounded-lg bg-accent-subtle text-[12.5px] text-accent"
                role="note"
                aria-live="polite"
                data-testid="jobform-mode-advice"
              >
                <Zap size={14} className="shrink-0 mt-0.5" aria-hidden="true" />
                <span>
                  {advice === 'script'
                    ? i18nT('components.jobForm.mode_advice_script')
                    : i18nT('components.jobForm.mode_advice_minimal_context')}
                </span>
              </div>
            )}
            <SettingsToggle
              label={i18nT('components.jobForm.minimal_context')}
              description={i18nT(privateMember ? 'components.jobForm.private_minimal_context_description' : 'components.jobForm.minimal_context_description')}
              checked={minimalContext}
              onChange={setMinimalContext}
            />
          </div>
        )}
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
      {/* Portals at z-[9999] via createPortal — reused rather than
       *  reimplemented so this folder picker is IDENTICAL to every other
       *  project-directory picker in the app (chat's own, FolderConfigModal's). */}
      {pickerOpen && (
        <ProjectPicker
          open={true}
          onOpenChange={o => { if (!o) setPickerOpen(false) }}
          anchorRef={browseRef}
          onSelect={path => { setProjectPath(path); setPickerOpen(false) }}
        />
      )}
    </div>
  )
}

export { buildBody, parseJobDefaults }
