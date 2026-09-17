import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Zap, FolderOpen } from 'lucide-react'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { Btn, Input, SendBtn } from './ui'
import { SettingsToggle } from './settings'
import AgentSelector, { type KiroCrewAgent } from './AgentSelector'
import ProjectPicker from './ProjectPicker'
import SimpleSelect from './SimpleSelect'
import type { ChatFolder, CronJob } from '../types'
import { orderFoldersWithPaths } from '../utils/folderTree'
import type { CronPrefill } from '../utils/schedulePresets'
import { SaveCreateLabel, expandDow } from '../utils/cronUtils'
import { adviseCronMode } from '../utils/cronModeAdvice'
import { useDebouncedValue } from '../apps/file-explorer/hooks'

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
function LockedAgentValue({ name, member }: { name: string; member?: boolean }) {
  // The hint names the thing the HOST calls it. A member surface says "member"
  // throughout, so a hint saying "crew" there made one binding read as two — the
  // blind reader could not tell whether member, crew and agent were one thing or
  // three. Same sentence, the noun the reader already has.
  const hint = i18nT(member
    ? 'components.jobForm.member_pinned_hint'
    : 'components.jobForm.agent_pinned_hint')
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
  if (!job) return { name: '', message: '', agent: '', model: '', channel: '', approvalMode: '', silent: false, strictSchedule: false, hideInChat: false, minimalContext: false, chatFolderId: '', jobKind: 'message' as JobKind, schedMode: 'interval' as const, intVal: 1, intUnit: 'hours' as const, weekDays: [] as number[], weekTime: '09:00', cronExpr: '', projectPath: '' }
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
  return { name: job.name, message: job.message, agent: job.agent || '', model: job.model || '', channel: job.channel || '', approvalMode: job.approval_mode || '', silent: job.silent || false, strictSchedule: job.strict_schedule || false, hideInChat: job.hide_in_chat || false, minimalContext: job.minimal_context || false, chatFolderId: job.chat_folder_id || '', jobKind: jobKindOf(job), schedMode, intVal, intUnit, weekDays, weekTime, cronExpr: cronRaw, projectPath: job.project_path || '' }
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
    return i18nT('components.jobForm.project_directory_must_be_an_absolute_path')
  }
  if (raw.includes('project_path refers to a sensitive path')) {
    return i18nT('components.jobForm.project_directory_refers_to_a_protected_path')
  }
  if (raw.includes('project_path must be an existing directory')) {
    return i18nT('components.jobForm.project_directory_must_be_an_existing_directory')
  }
  return raw
}

/** Whether a backend save rejection is one of the three `project_path`
 *  validation refusals `friendlyProjectPathError` remaps. Kept in lock-step
 *  with the substrings above so the caller can route those messages to the
 *  notice beside the project-directory field rather than the form-wide notice
 *  at the very bottom — the field sits mid-form, so a reject rendered only at
 *  the foot of the form made the user scroll to find what to fix (UX Review
 *  span=d88c9e1f411b). Any OTHER rejection (a bad agent name, a transport
 *  failure) is form-wide, because it is not about this one field. */
function isProjectPathError(raw: string): boolean {
  return raw.includes('project_path must be an absolute path')
    || raw.includes('project_path refers to a sensitive path')
    || raw.includes('project_path must be an existing directory')
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
    if (isEdit || f.approvalMode) body.approval_mode = f.approvalMode
    // Only the agent kind has an injected context to trim. A script or command
    // job takes no agent turn, so sending this would store a flag that can
    // never do anything.
    body.minimal_context = f.minimalContext
  }
  if (isEdit || f.channel) body.channel = f.channel
  body.silent = f.silent
  body.strict_schedule = f.strictSchedule
  body.hide_in_chat = f.hideInChat
  // Always sent for a job that can HAVE a tab, create and edit alike: "" is the
  // real value for "do not file this job's runs", so omitting it when empty would
  // make clearing the picker a no-op on the PATCH and leave the job filed into a
  // folder the user just unset. The backend refuses an id naming no folder, which
  // is why the picker only ever offers folders the sidebar currently has.
  //
  // A script/command job sends "" because it never gets a tab for a folder to
  // point at, and storing a setting that cannot do anything is how a setting
  // starts lying -- the same reason `minimal_context` is omitted for it.
  //
  // `hide_in_chat` is deliberately NOT in that list. It SUSPENDS filing rather
  // than cancelling it: the runtime already ignores the folder for a hidden job
  // (`cron_run_gets_tab`), so keeping the value costs nothing, and wiping it would
  // make the form's own hint false -- "turn off Hide in chat to use this" promises
  // that unchecking restores what was there, and a reader who checks the box,
  // saves, and unchecks it later would instead find the folder silently gone.
  body.chat_folder_id = isLlmless ? '' : f.chatFolderId
  // "" is a valid, meaningful value here (clears the binding back to
  // global-agent-only on an edit), so it is sent unconditionally rather than
  // gated behind a truthiness check like the optional fields above.
  //
  // It is cleared for a script/command job for the same reason
  // `chat_folder_id` is: the field is rendered only under `!isLlmless`, and a
  // subprocess dispatch derives no cwd from it. Re-persisting the binding a
  // converted job no longer shows would leave a folder the form cannot
  // display, the dispatch never reads, and the owner gate still enforces.
  body.project_path = isLlmless ? '' : f.projectPath
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
  /** The host's own noun for the pinned identity: set it where the surface
   *  says "member" throughout (the Crew Members drawer), so the pinned-value
   *  hint speaks the reader's noun. Left unset, the hint keeps "crew" — the
   *  crew editor's own word. Deliberately a HOST flag rather than derived
   *  from `memberId`: every crew passes `memberId` for identity, so deriving
   *  flipped the crew editor's wording to "member" for plain agents. */
  memberNoun?: boolean
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
  /** A submit that FAILED, reported outward as well as rendered inline. The
   *  inline error is invisible to a host that has already unmounted this form —
   *  which is exactly the case worth reporting, since the user was told the save
   *  might still land and would otherwise never learn what happened.
   *
   *  `confirmed` says whether the failure is a VERDICT or an unknown. The server
   *  answering (any HTTP status) means it decided, so the schedule was not
   *  created. A request that never got an answer — the connection dropped, the
   *  tab went offline — proves nothing: the POST may well have been applied, and
   *  a host that reports "wasn't created" there states as fact something it
   *  cannot know. Validation refusals do not come through here at all: the form
   *  is on screen for those, so its own message is the right surface.
   *
   *  `jobName` is the name the user typed, so a host reporting the failure away
   *  from this form can say WHICH schedule it was — a member can have several
   *  in flight. Always supplied: the name is validated non-empty before the
   *  request is ever sent. */
  onSubmitError?: (message: string, confirmed: boolean, jobName?: string) => void
  /** Called when the form's TOUCHED state changes: true once any field has
   *  diverged from its initial value, false when they all match again (or
   *  after a successful create resets them). Hosts that guard destruction
   *  paths key on this rather than on mere open-ness, so looking at an empty
   *  form and backing out never triggers a "your typed work will be lost"
   *  confirm about work that does not exist. */
  onDirtyChange?: (dirty: boolean) => void
}

export default function JobForm({ job, prefill, agents, defaultAgent, rosterFailure, lockedAgent, memberId, memberNoun, providerAgent, onSaved, layout = 'horizontal', externalSubmit, submitRef, onSavingChange, onSubmitError, onDirtyChange }: Props) {
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
  const [chatFolderId, setChatFolderId] = useState(defaults.chatFolderId)
  const [schedMode, setSchedMode] = useState(init.schedMode)
  const [intVal, setIntVal] = useState(init.intVal)
  const [intUnit, setIntUnit] = useState(init.intUnit)
  const [weekDays, setWeekDays] = useState(init.weekDays)
  const [weekTime, setWeekTime] = useState(init.weekTime)
  const [tz, setTz] = useState(() => job ? (job.timezone || 'UTC') : Intl.DateTimeFormat().resolvedOptions().timeZone)
  const [cronExpr, setCronExpr] = useState(init.cronExpr)
  const [projectPath, setProjectPath] = useState(init.projectPath)
  // Keep the input itself immediate, but wait for typing to settle before the
  // path becomes a server-state identity. Querying every intermediate path can
  // load an empty roster and reconcile a deliberate project-agent pick away.
  const debouncedProjectPath = useDebouncedValue(projectPath, 250)
  const [pickerOpen, setPickerOpen] = useState(false)
  const browseRef = useRef<HTMLButtonElement>(null)
  // Project-scoped roster for THIS job's project_path — a raw path, no live
  // chat slot behind it, so this is the project_path fallback (Decision 1).
  // A local effect rather than useAgents(): that hook unconditionally syncs +
  // fetches on every mount regardless of its args, which would double every
  // JobForm's roster work even for the common case of no project_path set.
  // This only does anything once a path is actually present.
  const [projectAgents, setProjectAgents] = useState<KiroCrewAgent[]>([])
  // Mirror of `projectAgents` for the clear-on-unbind effect below. That effect
  // must know which names the FOLDER contributed, but it cannot depend on the
  // state: its `!projectPath` branch calls `setProjectAgents([])` with a fresh
  // array every run, so listing `projectAgents` as a dependency would re-fire
  // it forever. A ref is never stale and needs no dependency entry.
  const projectAgentsRef = useRef<KiroCrewAgent[]>([])
  useEffect(() => {
    projectAgentsRef.current = projectAgents
  }, [projectAgents])
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
  // The agent name a project-directory change cleared, or '' when nothing was
  // cleared. Its own state rather than a flag on the roster error: a reset is not
  // a failure, and folding the two would make one clear the other. The reason
  // selects clear-specific wording while keeping one notice mechanism.
  const [agentResetFrom, setAgentResetFrom] = useState('')
  const [agentResetReason, setAgentResetReason] = useState<'project-cleared' | 'not-in-project'>('not-in-project')
  // Whether the clear-on-unbind branch has already run for the CURRENT empty
  // field. It must act on the TRANSITION into the empty state, not on every
  // re-run: clearing the field fires the effect twice -- once when
  // `projectPath` empties, then again when `debouncedProjectPath` settles and
  // `enabled: !!debouncedProjectPath` turns the roster query off, which moves
  // `projectAgentsData` to `undefined` and so changes a dependency. The second
  // run sees the agent the first one just cleared, computes `cleared === false`
  // and writes `setAgentResetFrom('')`, wiping the notice the first run set --
  // it rendered for ~200ms and then vanished, so the acknowledgment UX Review
  // asked for was unreadable in practice while every test asserting it
  // synchronously still passed.
  const unbindHandledRef = useRef(false)
  // The project agent a directory EDIT reconciled away, held until either the
  // user picks something themselves or a settled roster defines that name again.
  //
  // The debounce above only protects a path typed faster than 250ms. Hand-editing
  // a bound job's directory pauses longer than that, so an intermediate path
  // becomes a roster identity of its own -- and `/api/agents?project_path=`
  // answers an unusable path with HTTP 200 and the configured agents alone, so
  // that roster legitimately lacks the pick and the reconcile below clears it.
  // Clearing on what is known is right; leaving it cleared once the FINISHED path
  // loads a roster that DOES define the name is not, and makes every path edit on
  // a bound job force a re-pick (UX Review). Restoring needs no knowledge of
  // whether a path was "half typed" -- a question the endpoint cannot answer
  // anyway, since an unusable path and a usable one with no agents are the same
  // 200 -- only that the name is resolvable again and the user has not chosen
  // since. A ref rather than state: the effect that writes it must not re-run on
  // it, exactly as `projectAgentsRef` above.
  const editClearedAgentRef = useRef('')
  // A deliberate pick supersedes the reset bookkeeping above: the notice now
  // describes nothing the user can still act on, and the remembered name is no
  // longer theirs to restore -- a later settled roster defining it must not
  // override the choice they just made.
  const pickAgent = (name: string) => {
    editClearedAgentRef.current = ''
    setAgent(name)
    setAgentResetFrom('')
  }
  // The fetch itself lives in useQuery (per-frontend convention: server state
  // goes through React Query, not manual useState+useEffect+fetch) — keyed on
  // the DEBOUNCED mirror so a half-typed path never becomes a roster identity.
  // Once typing settles, switching folders naturally supersedes an in-flight
  // fetch the same way the old `cancelled` flag did: a stale response for a
  // FORMER key can never land against the current one. `enabled` skips the
  // request entirely while no settled path is set, matching the effect below's
  // own early-return for an empty input.
  const {
    data: projectAgentsData,
    error: projectAgentsQueryError,
    refetch: refetchProjectAgents,
  } = useQuery<{ agents?: KiroCrewAgent[] }, Error>({
    queryKey: ['project-agents', debouncedProjectPath],
    queryFn: () => api.kirocrewAgents(undefined, debouncedProjectPath),
    enabled: !!debouncedProjectPath,
  })
  // Same shape as `retryChatFolders`/`rosterFailure.onReload`: refetch the ONE
  // query in place, no page reload and no hand-off, because the notice sits
  // beside unsaved form input. Held state rather than reading `isFetching` for
  // the same reason it is below the chat-folder retry — the notice's own
  // message survives from the effect, but the button needs its own pending
  // flag. The prior notice offered no retry at all, so the only recovery was
  // re-typing the path to re-key the query (UX Review span=6409bbff1088).
  const [projectRosterRetrying, setProjectRosterRetrying] = useState(false)
  const retryProjectRoster = () => {
    setProjectRosterRetrying(true)
    void refetchProjectAgents().finally(() => setProjectRosterRetrying(false))
  }
  useEffect(() => {
    if (!projectPath) {
      setProjectAgents([])
      setProjectRosterError('')
      // An emptied field ends the edit, so there is no later roster for a
      // path-edit restore to land against: the clear below is its own
      // announcement and stands. Written on every re-run for this empty field,
      // which is safe -- unlike the reset below, writing '' twice is idempotent.
      editClearedAgentRef.current = ''
      // Idempotent past the first run for this empty field -- see
      // `unbindHandledRef`. The two statements above are safe to repeat; the
      // reset below is not, because it would clear its own notice.
      if (unbindHandledRef.current) return
      unbindHandledRef.current = true
      // The selected agent may have been a project-scoped one that only
      // existed because THIS folder was open (effectiveAgents merged it in
      // from projectAgents, now cleared above). Left alone, that name is
      // still sent on save (`agent: locked ?? agent`) with an empty
      // project_path -- a project agent EXISTS only inside its folder, so
      // without the folder the name resolves to nothing and the run would
      // silently take the default agent's prompt, tools, and permissions.
      //
      // Decide on POSITIVE knowledge, not absence: clear the name when
      // `projectAgents` -- the folder's own roster, still holding its
      // pre-clear value on this render because React state updates are not
      // synchronous -- is what contributed it. Testing "not in the global
      // roster" instead was wrong twice over: an empty global roster is
      // legitimate (a project-only install, and `CrewWakeSection` passes
      // `agents={[]}` deliberately), and app agents under `~/.kiro/agents/`
      // are dispatchable without ever appearing in it, so absence proves
      // nothing. A project agent that shares a global agent's name is left
      // alone -- the global one survives the folder being cleared, matching
      // effectiveAgents' own dedup-by-name rule.
      setAgent(a => {
        const cleared = !!(
          a
          && projectAgentsRef.current.some(p => p.scope === 'project' && p.name === a)
          && !agents.some(g => g.name === a)
        )
        setAgentResetReason('project-cleared')
        setAgentResetFrom(cleared ? a : '')
        return cleared ? '' : a
      })
      return
    }
    // A bound path re-arms the guard, so a LATER clear announces its reset
    // again rather than staying silent after the first one.
    unbindHandledRef.current = false
    if (projectAgentsQueryError) {
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
      // silently-substituted one are indistinguishable. A fixed lead-in
      // sentence names what failed instead of gluing the raw backend text
      // onto the fallback notice with no sentence boundary (UX Review):
      // "<raw error> Showing the global agent list instead." reads as one
      // run-on fragment, not two facts. Keeps the existing (already
      // translated in every locale) fallback-notice string as its own
      // sentence rather than inventing a new untranslated key for it.
      const rosterErr = projectAgentsQueryError.message
      // A detail is only worth showing when it is PROSE. `friendlyErrText`
      // unwraps `error`/`detail`/`message` when present, returns '' for an HTML
      // error page, and otherwise hands back the raw body — so a body with no
      // message field (a bare `{}`) arrives verbatim and interpolated as
      // "Couldn't load this folder's agents: {}." and an HTML page as
      // "…agents: ." Bare punctuation is worse than no clause at all (UX
      // Review), so the two non-prose shapes take the detail-less sentence.
      // Keyed on the same '{' test friendlyErrText uses to decide it found no
      // message, rather than a second guess at what a message looks like.
      const detail = rosterErr.trim()
      const hasProse = detail !== '' && !detail.startsWith('{')
      setProjectRosterError(
        (hasProse
          ? i18nT('components.jobForm.couldnt_load_this_folders_agents_detail', { detail })
          : i18nT('components.jobForm.couldnt_load_this_folders_agents'))
        + ' '
        + i18nT('components.jobForm.project_roster_error_falls_back_to_global_agents'),
      )
      return
    }
    if (projectAgentsData === undefined) {
      // Still in flight for this projectPath (useQuery hasn't resolved yet).
      // Clear immediately on folder change, before the fetch settles: the
      // query-key supersession above only stops a SUPERSEDED fetch's result
      // from overwriting a newer one, but leaves the PREVIOUS folder's
      // now-stale agents selectable in the picker for the whole in-flight
      // gap. A user who switches folders and picks an agent in that gap
      // would get an agent from the folder they just left.
      setProjectAgents([])
      setProjectRosterError('')
      return
    }
    const newProjectAgents: KiroCrewAgent[] = projectAgentsData.agents || []
    setProjectAgents(newProjectAgents)
    setProjectRosterError('')
    // Switching from folder A to folder B: the agent selected under A may
    // not exist under B at all. Reconcile against the union of the NEW
    // project roster and the global roster (mirrors the `!projectPath`
    // branch's own rule above) -- a name recognized by either is left
    // alone, everything else is cleared back to default. Without this,
    // save persists an agent name B's project cannot resolve, and the
    // scheduled fire silently falls back to the default agent's prompt,
    // tools, and permissions with no error surfaced anywhere.
    setAgent(a => {
      // A name either roster recognizes is resolvable under this project --
      // `newProjectAgents` is the successfully loaded WHOLE roster for it,
      // including global rows, so this also preserves a valid global pick when
      // the separate global-catalog request failed and the `agents` prop is
      // empty. The old `agents.length > 0` guard tried to protect that transient
      // failure, but also retained a project-A-only pick after project B had
      // authoritatively loaded without it.
      const known = (n: string) =>
        agents.some(g => g.name === n) || newProjectAgents.some(g => g.name === n)
      const restorable = editClearedAgentRef.current
      // Restore BEFORE considering a clear, so the two can never both fire on
      // one run: an earlier, unfinished path took this name away, this roster
      // settled with it defined again, and `!a` says the user has not chosen
      // anything since -- so the pick is still theirs to have back. The ref is
      // deliberately not cleared here: it holds the same name that is now
      // selected, so a further edit re-reaches this branch with nothing stale,
      // and leaving it untouched keeps this updater idempotent if React invokes
      // it twice.
      if (!a && restorable && known(restorable)) {
        // Nothing was lost after all, so the acknowledgment must not linger.
        setAgentResetFrom('')
        return restorable
      }
      const cleared = !!(a && !known(a))
      if (cleared) editClearedAgentRef.current = a
      setAgentResetReason('not-in-project')
      // Announced, not just performed (UX Review): the reset is correct, but
      // doing it silently meant the job saved under "default" with no
      // acknowledgment anywhere -- the user's deliberate pick vanished between
      // one keystroke in the directory field and pressing Save. Recording the
      // NAME rather than a boolean so the notice can say which pick went.
      if (cleared) {
        setAgentResetFrom(a)
      } else if (!(a === '' && restorable)) {
        // Withheld while a restore is pending: the reset that notice describes is
        // still in force, and a FURTHER unfinished path settling must not erase
        // its acknowledgment -- erasing it is the same defect as the double-run
        // the `unbindHandledRef` guard above covers, and if the operator's
        // finished path never does define the name they are left with the silent
        // reset the notice exists against.
        setAgentResetFrom('')
      }
      return cleared ? '' : a
    })
  }, [projectPath, agents, projectAgentsData, projectAgentsQueryError])
  // Global roster (the `agents` prop) plus this job's own project-scoped
  // agents, deduped by name. Project rows arrive tagged `source: 'project'`
  // by the server, and are shown as such without a per-folder relabel: this
  // form only ever merges ONE folder's agents at a time, so the generic
  // "project" badge already identifies where an agent came from
  // unambiguously — a folder-name badge would only earn its keep if more than
  // one folder's agents could appear in the same dropdown at once, which does
  // not happen here. Merging here is the only way a per-job path (not known
  // to the page-level roster) can ever appear in this picker at all.
  //
  // On a NAME COLLISION the project row is the one kept, because it is the one
  // dispatch runs: inside a bound folder a project definition outranks a
  // same-named configured agent (see `_resolve_agent_selection`). Keeping the
  // global row instead would advertise an agent that cannot answer — the
  // picker would name the configured one while the fire resolved the project
  // file. The displaced names are reported separately so the surviving row can
  // say so out loud rather than leaving the user to infer it from a badge:
  // "this is a project agent" and "this project agent took over a global name"
  // are different facts, and only the second explains why the global one has
  // vanished from the list.
  //
  // One row per name, not two. `agent` is a bare string, so the form cannot
  // record WHICH of two same-named rows was picked; a second row would be an
  // option that could neither be selected nor persisted. It could not be
  // honoured downstream either — kiro-cli resolves `--agent` against its cwd
  // and searches the project scope first, and the job runs with the bound
  // folder as cwd, so "use the global one here" is not ours to guarantee.
  const effectiveAgents = useMemo(() => {
    if (!projectPath || projectAgents.length === 0) return agents
    const projectNames = new Set(projectAgents.map(a => a.name))
    const kept = agents.filter(a => !projectNames.has(a.name))
    return [...kept, ...projectAgents]
  }, [agents, projectAgents, projectPath])
  // Names where a project agent displaced a same-named global one. Derived from
  // the same two rosters `effectiveAgents` merges, so the marker cannot drift
  // from the dedup that produced the list.
  //
  // Filtered on `scope === 'project'` because the project-scoped response is the
  // WHOLE roster, not just the folder's half: it carries the configured agents
  // too. Matching on the name alone would therefore mark every configured agent
  // as overridden the moment any folder is bound, since each one appears in both
  // the `agents` prop and the fetched payload.
  const shadowedGlobals = useMemo(() => {
    if (!projectPath || projectAgents.length === 0) return undefined
    const globalNames = new Set(agents.map(a => a.name))
    const shadowed = projectAgents
      .filter(a => a.scope === 'project' && globalNames.has(a.name))
      .map(a => a.name)
    return shadowed.length ? new Set(shadowed) : undefined
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
    chatFolderId !== defaults.chatFolderId ||
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
  // A project_path SAVE rejection rendered beside the field it names, not in
  // the form-wide notice at the foot of the form (UX Review span=d88c9e1f411b).
  // Its own scroll-into-view (mirroring `errorRef` above) because the field
  // sits mid-form while Save is at the bottom, so without it a reject would
  // land off-screen above the button the user just pressed. The tick makes a
  // REPEATED identical reject scroll again, exactly as the form-wide notice does.
  const projectPathErrorRef = useRef<HTMLDivElement | null>(null)
  const [projectPathSaveError, setProjectPathSaveErrorState] = useState('')
  const [projectPathErrorTick, setProjectPathErrorTick] = useState(0)
  const setProjectPathSaveError = (e: string) => { setProjectPathSaveErrorState(e); if (e) setProjectPathErrorTick(t => t + 1) }
  useEffect(() => {
    if (projectPathSaveError) projectPathErrorRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [projectPathSaveError, projectPathErrorTick])
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

  /** The sidebar's own folder tree, for the "file runs in" picker.
   *
   *  Read through the shared `['chat-folders']` query key the sidebar already
   *  owns, so opening this form costs no extra request when the sidebar is
   *  mounted (which is the ordinary case) and one when it is not. A folder
   *  created or renamed while the form is open arrives through the same cache
   *  invalidation the sidebar listens to. */
  const {
    data: chatFolders = [],
    isError: chatFoldersFailed,
    isSuccess: chatFoldersLoaded,
    refetch: refetchChatFolders,
  } = useQuery<ChatFolder[]>({
    queryKey: ['chat-folders'],
    queryFn: () => api.chatFolders(),
    enabled: !isLlmless,
  })
  /** Held here rather than read off `isFetching`: a query with no data goes back
   *  to `pending` while it refetches, so `isError` drops and the notice would
   *  vanish mid-retry, leaving nothing on screen to say a retry is happening.
   *  Same shape as `rosterFailure.reloading`, which the parent holds for the
   *  agent roster's retry above. */
  const [chatFoldersRetrying, setChatFoldersRetrying] = useState(false)
  const retryChatFolders = () => {
    setChatFoldersRetrying(true)
    void refetchChatFolders().finally(() => setChatFoldersRetrying(false))
  }
  const chatFoldersFailure = chatFoldersFailed || chatFoldersRetrying
  /** A job may name a folder that has since been deleted -- the backend treats a
   *  dangling id as "not filed" rather than failing the run. Showing the picker
   *  as empty in that state would be accurate, so it is left to resolve to the
   *  clear row: re-saving then clears the stale id, which is the outcome the
   *  reader is looking at the form to get.
   *
   *  `orderFoldersWithPaths` is the sidebar's own ordering helper, shared with the
   *  move-to-folder submenu and the launcher's folder rows, so this picker lists
   *  folders in the order and with the ancestry labels the reader already knows --
   *  and a fix to either (a cycle guard, a non-string name off disk) reaches all
   *  of them at once. */
  const folderOptions = useMemo(() => {
    const ordered = orderFoldersWithPaths(chatFolders)
    return { values: ordered.map(f => f.folder.id), labels: ordered.map(f => f.path) }
  }, [chatFolders])
  /** `hide_in_chat` is the explicit "this job gets no tab" opt-out, and a run with
   *  no tab has nothing to file -- so with it on the picker cannot do anything.
   *  Saying that and refusing input beats accepting a setting whose only
   *  observable effect would be a folder that never fills up.
   *
   *  The stored folder is KEPT while the flag is on, not wiped: the runtime already
   *  ignores it, so suspending the setting costs nothing, and the hint says
   *  "turn off Hide in chat to use this" -- which would be a lie if unchecking
   *  came back to an empty picker. */
  const chatFolderUnavailable = hideInChat || job?.persistent_session === false
  /** Which of the two reasons the picker is refusing input. A stateless job
   *  (`persistent_session=false`, set from the API or CLI -- this form never
   *  edits it) has no job-wide tab to file, so the backend refuses the pair at
   *  save time; saying so here is cheaper than a 400 under Save. */
  const chatFolderHint = hideInChat
    ? i18nT('components.jobForm.chat_folder_hidden_in_chat')
    : job?.persistent_session === false
      ? i18nT('components.jobForm.chat_folder_needs_persistent_session')
      : i18nT('components.jobForm.chat_folder_description')
  /** The job's saved folder has been DELETED since it was saved (the list loaded,
   *  and does not offer that id).
   *
   *  The backend treats a dangling id as "not filed" at run time, but REFUSES one
   *  at save time -- so left in state it would ride along on the next unrelated
   *  edit and turn a rename into a 400 the reader cannot act on. The trigger
   *  already reads "Do not file runs" in this state, because `SimpleSelect` falls
   *  back to `clearLabel` for a value it has no option for, so submitting `''`
   *  makes the request agree with what the reader is looking at. Gated on a
   *  SUCCESSFUL load: while the fetch is in flight or failed, every id looks
   *  missing, and clearing on that would unfile a job for being offline. */
  const savedFolderIsGone =
    !!chatFolderId && chatFoldersLoaded && !folderOptions.values.includes(chatFolderId)
  const submittedChatFolderId = savedFolderIsGone ? '' : chatFolderId

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
    setError(''); setProjectPathSaveError(''); setSaving(true)
    const f = { name, message: msg, agent: locked ?? agent, model, channel, approvalMode, silent, strictSchedule, hideInChat, minimalContext, chatFolderId: submittedChatFolderId, jobKind, schedMode, intVal, intUnit, weekDays, weekTime, cronExpr, projectPath }
    const body = buildBody(f, tz, setError, !!job, job ? undefined : prefill)
    if (!body) { setSaving(false); return }
    if (privateMember && !isLlmless) {
      // Keyed on `privateMember`, not on `boundMember`, and BOTH lines depend on
      // it. The backend refuses `member_id: "default"` as a V1 identity, so the
      // rule lives here rather than in each host — `CrewWakeSection.tsx` was
      // spelling `crew === 'default' ? undefined : crew` to avoid it, and this
      // diff removes that one site rather than adding a second copy of it.
      // The `agent` override belongs to the same condition: for a real member,
      // `agent` carries the provider TEMPLATE while `member_id` carries identity,
      // but the default crew is not a member — overriding its `agent` from
      // "default" to the template would break the attribution `wakesCrew` reads,
      // and its schedule would vanish from the pane that created it.
      body.member_id = boundMember
      body.agent = providerAgent || job?.agent || ''
    }
    try {
      // `confirmed` rides with the message rather than being inferred later: only
      // here is it still known what the server said. A 4xx is a DECISION about
      // this request, so the schedule was not created. A 5xx is not: a proxy or
      // gateway error can be raised before the app ever saw the POST, or after it
      // applied it and the response was lost. Anything without a status (a dropped
      // connection, an offline tab) reached no verdict either.
      // Both routes carry the same catch, not just create: the project-binding
      // validation refuses an UPDATE with the same field-scoped 400 (an agent the
      // bound directory does not declare, a relative or sensitive path), and
      // without it that reason fell to the outer handler and showed the generic
      // "failed to save" instead of the sentence naming the field to fix.
      const classify = (e: unknown) => ({
        error: e instanceof Error ? e.message : String(e),
        errorConfirmed: e instanceof ApiError && e.status >= 400 && e.status < 500,
      })
      const res = job
        ? await api.updateCron(job.id, body).catch(classify)
        : await api.createCron(body).catch(classify)
      if (res.error) {
        // Rewritten for the field it belongs to before BOTH consumers see it:
        // the host's onSubmitError gets the same sentence the user reads, so a
        // dialog that surfaces the message itself cannot show the raw backend
        // wording while the inline notice shows the friendly one.
        const shown = friendlyProjectPathError(res.error)
        // A project_path refusal names the project-directory field, so it is
        // rendered beside that field rather than the form-wide notice at the
        // foot of the form (UX Review span=d88c9e1f411b). Any other rejection
        // is not about that one field and stays form-wide.
        if (isProjectPathError(res.error)) setProjectPathSaveError(shown)
        else setError(shown)
        onSubmitError?.(shown, 'errorConfirmed' in res ? !!res.errorConfirmed : true, f.name)
        setSaving(false)
        return
      }
      if (!job) { setName(''); setMsg(''); setWeekDays([]); setIntVal(1); setChannel(''); setModel(''); setApprovalMode(''); setSilent(false); setStrictSchedule(false); setHideInChat(false); setMinimalContext(false); setChatFolderId(''); setProjectPath('') }
      // Cleared BEFORE onSaved, so `onSavingChange` is symmetric: it reports
      // false on EVERY outcome, not only on failure. An asymmetric version made
      // the flag a host's problem to unlearn — a host that lifts it out of its
      // own dialog (to refuse a dismissal mid-save, say) never heard about
      // success, so one successful save left it stuck saving forever. Ordering
      // matters: onSaved typically unmounts this form, so a clear after it
      // would not run.
      setSaving(false)
      onSaved()
    } catch (e: unknown) {
      const msg = i18nT('components.jobForm.failed_to_save')
      // Reached only when the call threw past the inner handler. Same rule as
      // above: a 4xx is the server deciding, a 5xx or a transport failure is not.
      const decided = e instanceof ApiError && e.status >= 400 && e.status < 500
      setError(msg); onSubmitError?.(msg, decided, f.name); setSaving(false)
    }
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
            <textarea id="jobform-message" aria-label={i18nT('components.jobForm.message')} className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-hidden resize-y min-h-[60px] focus-ring" value={msg} onChange={e => setMsg(e.target.value)} />
          </div>)}
        </div>
      </>) : (
        <div className="flex gap-2 items-center flex-wrap">
          <Input placeholder={i18nT('components.jobForm.job_name')} value={name} onChange={e => setName(e.target.value)} />
          <Input placeholder={i18nT('components.jobForm.message_task')} style={{ flex: 2 }} value={msg} onChange={e => setMsg(e.target.value)} />
          {locked
            ? <LockedAgentValue name={locked} member={memberNoun} />
            : <AgentSelector agents={effectiveAgents} defaultAgent={defaultAgent} value={agent} onChange={pickAgent} rosterFailure={rosterFailure} shadowedGlobals={shadowedGlobals} modal />}
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
          <SimpleSelect
            options={folderOptions.values}
            optionLabels={folderOptions.labels}
            value={submittedChatFolderId}
            onChange={setChatFolderId}
            disabled={chatFolderUnavailable}
            clearLabel={i18nT('components.jobForm.chat_folder_none')}
            aria-label={i18nT('components.jobForm.chat_folder')}
          />
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
            that job kind has none, and — since save-time validation holds
            any non-empty project_path that differs from the stored one to
            the existing-directory bar — could 400 the
            save over a value the job would never actually use. Guarding it
            the same as Agent/Approval keeps the field's presence consistent
            with what fire time actually reads. */}
        {!isLlmless && (<>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.project_directory')} <span className="text-muted/60 font-normal">({i18nT('components.jobForm.optional')})</span></span>
          <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.run_this_job_s_agent_in_this_folder_and_offer')}</span>
          <div className="flex gap-2">
            <Input
              className="flex-1 min-w-0 font-mono text-[12px]"
              id="jobform-project-path"
              aria-label={i18nT('components.jobForm.project_directory')}
              value={projectPath}
              onChange={e => { setProjectPath(e.target.value); setProjectPathSaveError('') }}
              placeholder={i18nT('components.jobForm.project_directory_placeholder')}
            />
            <Btn ref={browseRef} onClick={() => setPickerOpen(true)}>
              <FolderOpen size={13} /> {i18nT('components.jobForm.browse')}
            </Btn>
          </div>
          {/* No hand-off: this notice sits inside the job form whose fields
              (name, message, schedule, working directory) are still live —
              the hand-off navigates to chat and would discard them. The retry
              refetches the ONE roster query in place, the same shape as the
              chat-folder retry below and the agent roster's `onReload`: the
              failure "need[s] different next actions (retry vs. pick a
              different folder)" and now offers both, not just re-typing the
              path (UX Review span=6409bbff1088). */}
          {projectRosterError && (
            <div className="flex items-center justify-between gap-2">
              <ErrorNotice variant="inline" testId="jobform-project-roster-error" message={projectRosterError} />
              <Btn
                type="button"
                onClick={retryProjectRoster}
                disabled={projectRosterRetrying}
                aria-busy={projectRosterRetrying}
                className="text-[12px] px-2 py-1 shrink-0"
              >
                {projectRosterRetrying
                  ? i18nT('components.jobForm.project_roster_retrying')
                  : i18nT('components.jobForm.project_roster_retry')}
              </Btn>
            </div>
          )}
          {/* A save-time project_path rejection, beside the field it names
              rather than the form-wide notice at the foot of the form (UX
              Review span=d88c9e1f411b). Same inline ErrorNotice the roster
              failure above uses — no new notice system. No hand-off: this is
              a save refusal inside a form whose unsaved fields would be lost
              when the hand-off navigates to chat. */}
          <div ref={projectPathErrorRef}>
            {projectPathSaveError && (
              <ErrorNotice variant="inline" testId="jobform-project-path-save-error" message={projectPathSaveError} askAgent={false} />
            )}
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.agent')}</span>
          {locked
            ? <LockedAgentValue name={locked} member={memberNoun} />
            : (<>
              <span className="text-[11px] text-muted/70">{i18nT('components.jobForm.which_agent_handles_this_job_leave_default_for_t')}</span>
              <AgentSelector agents={effectiveAgents} defaultAgent={defaultAgent} value={agent} onChange={pickAgent} rosterFailure={rosterFailure} shadowedGlobals={shadowedGlobals} modal />
              {/* Beside the Agent field rather than in the form-wide notice: the
                  reset happened to THIS control, and it is information, not an
                  error -- the directory change was legitimate and so was the
                  reset. Cleared as soon as the user picks an agent again, since
                  by then the sentence describes nothing they can still act on.

                  role="status" for the same reason the sibling
                  `chat_folder_was_deleted` notice below carries it: the system
                  discarded a choice the reader made deliberately, so a reader on
                  assistive tech must hear that it happened rather than discover a
                  silently different agent later. A status (polite) rather than an
                  alert, because the reset itself was legitimate and there is
                  nothing to interrupt for. */}
              {agentResetFrom && (
                <span role="status" className="text-[11px] text-warn" data-testid="jobform-agent-reset-note">
                  {i18nT(
                    agentResetReason === 'project-cleared'
                      ? 'components.jobForm.agent_reset_project_cleared'
                      : 'components.jobForm.agent_reset_not_in_folder',
                    { name: agentResetFrom },
                  )}
                </span>
              )}
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
          <span className="text-[12px] text-muted font-medium">{i18nT('components.jobForm.chat_folder')}</span>
          <span className="text-[11px] text-muted/70">{chatFolderHint}</span>
          <SimpleSelect
            options={folderOptions.values}
            optionLabels={folderOptions.labels}
            /* Shows the KEPT folder while disabled rather than the clear row: the
               hint under it says the folder is kept, and a trigger reading "Do not
               file runs" would contradict it on the one screen where both are
               visible at once. `disabled` already greys it, which is what says the
               setting is suspended. */
            value={submittedChatFolderId}
            onChange={setChatFolderId}
            disabled={chatFolderUnavailable}
            clearLabel={i18nT('components.jobForm.chat_folder_none')}
            aria-label={i18nT('components.jobForm.chat_folder')}
          />
          {/* A failed folder load renders an empty list, which reads as "you have
              no folders" -- indistinguishable from the real empty tree, and the
              reader would conclude the feature needs a folder they already have.
              The retry refetches the ONE query in place, the same shape as the agent
              roster's `rosterFailure.onReload` above it: no page reload and no
              hand-off, because the notice sits beside unsaved form input and either
              of those would discard what they typed. */}
          {chatFoldersFailure && !chatFolderUnavailable && (
            <div className="flex items-center justify-between gap-2">
              <ErrorNotice
                variant="inline"
                message={i18nT('components.jobForm.chat_folder_list_unavailable')}
              />
              <Btn
                type="button"
                onClick={retryChatFolders}
                disabled={chatFoldersRetrying}
                aria-busy={chatFoldersRetrying}
                className="text-[12px] px-2 py-1 shrink-0"
              >
                {chatFoldersRetrying
                  ? i18nT('components.jobForm.chat_folder_retrying')
                  : i18nT('components.jobForm.chat_folder_retry')}
              </Btn>
            </div>
          )}
          {/* A genuinely empty tree is not an error, but it IS a dead end without
              this: the picker offers nothing and says nothing about where folders
              come from. */}
          {chatFoldersLoaded && folderOptions.values.length === 0 && !chatFolderUnavailable && (
            <span className="text-[11px] text-muted/70">
              {i18nT('components.jobForm.chat_folder_none_yet')}
            </span>
          )}
          {/* The trigger reads "Do not file runs" here, because the saved id names
              no folder the list offers. Without this line a reader who opened the
              job to change something unrelated sees a setting they never cleared
              and concludes they cleared it. Styled as a warning and announced as a
              status, not dressed as the hint under the picker: the system made this
              reversion, and a line in the hint's own muted grey reads as the reader's
              own choice. */}
          {savedFolderIsGone && !chatFolderUnavailable && (
            <span role="status" className="text-[11px] text-warn">
              {i18nT('components.jobForm.chat_folder_was_deleted')}
            </span>
          )}
        </div>
        )}
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
