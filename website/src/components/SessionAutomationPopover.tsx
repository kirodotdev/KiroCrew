import { useEffect, useId, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { Activity, ChevronDown, Goal, Play, Radar, RotateCw, Square, Trash2, X } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type MonitorWrite } from '../api/client'
import {
  deriveAutomationStatus,
  MONITOR_STATUS_KEYS,
  normalizePullRequestMonitorTarget,
  normalizeAutomationRecord,
  STRUCTURED_MONITOR_DEFAULTS,
  STRUCTURED_MONITOR_LIMITS,
  type AutomationRecord,
  type LegacyGoalLoop,
  type StructuredMonitor,
} from '../monitoring/automation'
import { fmtDateTimeNumeric, fmtNumber, fmtUnit, type FormatUnit } from '../i18n/format'
import { Badge, Btn, IconButton, Input, SendBtn } from './ui'
import { PopoverContent } from './ui/popover'
import AutoNudgePopover, { type AutoNudgeLoop } from './AutoNudgePopover'
import { i18nT } from '../i18n/t'
import MonitorRadar from './MonitorRadar'
import ErrorNotice from './ErrorNotice'
import { useQueryIsFetching } from '../hooks/useQueryIsFetching'
import { errMessage } from '../utils/thunkError'
import GoalProgressContent, { goalStatusLabel } from './GoalProgressContent'
import { AUTONUDGE_LOOPS_QUERY_KEY } from './autoNudgeLoop'
import { SettingsToggle } from './settings'

const MotionIconButton = motion.create(IconButton)

class GoalPauseRefreshError extends Error {
  constructor(cause: unknown) {
    super(i18nT('components.goalProgress.pause_refresh_failed'), { cause })
  }
}

type GoalRequest = {
  loop: LegacyGoalLoop
  slotKey: string
  action: 'change' | 'refresh' | 'refresh_pause' | 'dismiss'
}

interface Props {
  slotKey: string
  automation: AutomationRecord | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onChange: (automation: AutomationRecord | null) => void
  /** True only after both per-slot REST reads prove creation cannot replace an unseen record. */
  creationReady?: boolean
  /** The cold snapshot failed; create remains guarded while the server-backed legacy fallback stays reachable. */
  snapshotFailed?: boolean
  interrupted?: boolean
  /** Crew/member sessions route ingress through their crew and cannot host direct monitor turns. */
  sessionMode?: string
}

type Draft = {
  target: string
  cadence: string
  runtime: string
  turns: string
  tokens: string
  providerErrors: string
  wakeInstructions: string
}

type EditorState = {
  draft: Draft
  dirty: Partial<Record<keyof Draft, true>>
  sourceId: string | null
}

type FormErrors = Partial<Record<keyof Draft | 'request', string>>

type Mutation = ({ captured: AutomationRecord | null; slotKey: string; editorKey: string } & (
  | { action: 'create'; payload: Required<MonitorWrite> }
  | { action: 'update'; id: string; payload: MonitorWrite }
  | { action: 'stop'; id: string }
  | { action: 'clear'; id: string }
  | { action: 'restart'; id: string }
))

function monitorRequestError(failure: unknown): string {
  if (!(failure instanceof ApiError) || failure.status !== 400) {
    return i18nT('components.sessionAutomationPopover.request_failed')
  }
  let code = ''
  try {
    const body = JSON.parse(failure.body) as unknown
    if (body && typeof body === 'object' && !Array.isArray(body)) {
      const rawCode = (body as Record<string, unknown>).code
      code = typeof rawCode === 'string' ? rawCode : ''
    }
  } catch {
    return i18nT('components.sessionAutomationPopover.request_failed')
  }
  if (code === 'gitlab_host_not_allowed') {
    return i18nT('components.sessionAutomationPopover.gitlab_host_not_allowed')
  }
  if (code === 'invalid_pull_request_url') {
    return i18nT('components.sessionAutomationPopover.invalid_pull_request_url')
  }
  return i18nT('components.sessionAutomationPopover.request_failed')
}

const defaults = (): Draft => ({
  target: '',
  cadence: String(STRUCTURED_MONITOR_DEFAULTS.cadenceSecs),
  runtime: String(STRUCTURED_MONITOR_DEFAULTS.maxRuntimeSecs),
  turns: String(STRUCTURED_MONITOR_DEFAULTS.maxAgentTurns),
  tokens: String(STRUCTURED_MONITOR_DEFAULTS.maxTokens),
  providerErrors: String(STRUCTURED_MONITOR_DEFAULTS.maxProviderErrors),
  wakeInstructions: '',
})

const DRAFT_FIELDS = Object.keys(defaults()) as (keyof Draft)[]

function monitorDraft(monitor: StructuredMonitor): Draft {
  return {
    target: monitor.target,
    cadence: String(monitor.cadenceSecs),
    runtime: String(monitor.budgets.maxRuntimeSecs),
    turns: String(monitor.budgets.maxAgentTurns),
    tokens: String(monitor.budgets.maxTokens),
    providerErrors: String(monitor.budgets.maxProviderErrors),
    wakeInstructions: monitor.wakeInstructions,
  }
}

/** Rebuild the popover's loop shape from the record the session holds.
 *
 * Every field is named here, so a field the record gains is invisible to the
 * popover until it is named here too -- which is why the judge's three ride along
 * explicitly rather than by spread: the popover draws its judge line from them,
 * and their absence reads to it as a loop armed with no judge at all. */
function legacyWire(loop: LegacyGoalLoop): AutoNudgeLoop {
  return {
    id: loop.id,
    slot_key: loop.slotKey,
    message: loop.message,
    idle_secs: loop.idleSecs,
    max_cycles: loop.maxCycles,
    cycle_count: loop.cycleCount,
    active: loop.active,
    goal: loop.goal,
    last_fire_ts: loop.lastFireAt,
    next_due_ts: loop.nextDueAt ?? 0,
    // The goal editor words a paused loop by this field; dropping it rendered
    // every inactive loop, a fresh pause included, as a bare stop.
    stopped_reason: loop.stoppedReason,
    ...(loop.stopSentinelPath !== undefined ? { stop_sentinel_path: loop.stopSentinelPath } : {}),
    ...(loop.judge !== undefined ? { judge: loop.judge } : {}),
    ...(loop.judge_last_verdict !== undefined
      ? { judge_last_verdict: loop.judge_last_verdict }
      : {}),
  }
}

/** A fire landed (`onFired`): arm the due reading on the record the parent
 *  holds NOW, never on a snapshot. The fire route arms a zero-delay timer and
 *  writes no deadline, and its answer is the live loop serialized after an
 *  audit await, so it can already carry the delivery; a parent measured
 *  against it rolled a delivered cycle back to due for a whole interval.
 *  `pressed` is pre-request by construction, so a parent record that fired
 *  since the press leaves nothing to arm. Null too when the parent's record is
 *  not this loop (the closure outlived it) or is no longer running (another
 *  writer paused it; `fire_now` refuses an inactive loop). */
function armedNow(current: AutomationRecord | null, pressed: AutoNudgeLoop, nowTs: number): LegacyGoalLoop | null {
  if (current?.kind !== 'legacy_goal_loop') return null
  if (String(current.id) !== String(pressed.id) || current.slotKey !== pressed.slot_key) return null
  if (!current.active) return null
  if (current.lastFireAt > (pressed.last_fire_ts || 0)) return null
  return { ...current, nextDueAt: nowTs }
}

function boundedInteger(
  raw: string,
  limits: { minimum: number; maximum: number },
): number | null {
  if (!/^\d+$/.test(raw)) return null
  const value = Number(raw)
  return Number.isSafeInteger(value)
    && value >= limits.minimum
    && value <= limits.maximum
    ? value
    : null
}

const fieldClass = 'space-y-1 min-w-0'
const labelClass = 'block text-[11px] font-medium text-muted'
const errorClass = 'text-[11px] text-danger'
// Fail closed to the operator ceiling shipped by the backend when its live read is unavailable.
const SHIPPED_RUNTIME_CEILING_SECS = 604_800

/* The runtime bound is typed in seconds, and a seven-digit second count says
   nothing about how long the reader is granting. Glossed with the largest
   whole unit that divides the bound exactly, so the gloss never rounds a
   bound the reader is held to; anything else stays in seconds. */
const DURATION_UNITS: ReadonlyArray<[number, FormatUnit]> = [[86_400, 'day'], [3_600, 'hour'], [60, 'minute']]

function describeDuration(secs: number): string {
  for (const [unitSecs, unit] of DURATION_UNITS) {
    if (secs >= unitSecs && secs % unitSecs === 0) {
      return fmtUnit(secs / unitSecs, unit, { unitDisplay: 'long' })
    }
  }
  return fmtUnit(secs, 'second', { unitDisplay: 'long' })
}

function FieldError({ id, message }: { id: string; message?: string }) {
  return message ? <p id={id} role="status" aria-live="polite" className={errorClass}>{message}</p> : null
}

export default function SessionAutomationPopover({
  slotKey,
  automation,
  open,
  onOpenChange,
  onChange,
  creationReady = true,
  snapshotFailed = false,
  interrupted = false,
  sessionMode = '',
}: Props) {
  const reducedMotion = useReducedMotion()
  const monitor = automation?.kind === 'structured_monitor' ? automation : null
  /* WHICH VIEW THIS OPENS ON, and the goal loop is the default. The bounded
     monitor accepts exactly one thing -- a pull request URL, validated against
     four code hosts -- so opening on it put every session that is not about a
     pull request in front of a form it cannot fill, with the surface that
     accepts any objective one unlabelled click away. The monitor is reached by
     asking for it, or by this slot already holding one: an armed record
     outranks the default in either direction, because whichever view is hidden
     is a running automation nothing on screen would report. */
  const [boundedModeSlot, setBoundedModeSlot] = useState<string | null>(
    automation?.kind === 'structured_monitor' ? slotKey : null,
  )
  const editorKey = monitor?.id ?? `new:${slotKey}`
  const incomingEditor = (): EditorState => ({
    draft: monitor ? monitorDraft(monitor) : defaults(),
    dirty: {},
    sourceId: editorKey,
  })
  const [editors, setEditors] = useState<Record<string, EditorState>>(() => ({
    [editorKey]: incomingEditor(),
  }))
  const [errorsByEditor, setErrorsByEditor] = useState<Record<string, FormErrors>>({})
  const editor = editors[editorKey] ?? incomingEditor()
  const errors = errorsByEditor[editorKey] ?? {}
  const [confirmStop, setConfirmStop] = useState(false)
  /* Separate from confirmStop: the two act on different states and one erases.
     A shared flag would let a stop confirmation land on the clear. */
  const [confirmClear, setConfirmClear] = useState(false)
  const id = useId()
  const queryClient = useQueryClient()
  // Subscribe without another request. A concurrent Start must also retire an
  // open watch form while the parent still holds the suggested Redux record.
  const { data: goalSnapshot } = useQuery<AutomationRecord | null>({
    queryKey: ['session-automation', slotKey], enabled: false,
  })
  const liveLoop = automation?.kind === 'legacy_goal_loop' ? automation : null
  const newerGoalSnapshot = liveLoop?.goal && goalSnapshot?.kind === 'legacy_goal_loop'
    && goalSnapshot.goal && goalSnapshot.id === liveLoop.id && goalSnapshot.slotKey === liveLoop.slotKey
    && ((goalSnapshot.goalGeneration !== undefined
      && (liveLoop.goalGeneration === undefined || goalSnapshot.goalGeneration > liveLoop.goalGeneration))
      || goalSnapshot.goal.status === 'complete' || goalSnapshot.goal.status === 'ended')
  const legacyLoop = newerGoalSnapshot ? goalSnapshot : liveLoop
  const replaceableSuggestion = legacyLoop?.goal?.status === 'suggested'
    && !legacyLoop.active && legacyLoop.cycleCount === 0 && legacyLoop.lastFireAt === 0
    && !legacyLoop.stoppedReason
  const recognition = useQuery<{ monitoring?: { goal_suggestions?: boolean } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled: open || (automation?.kind === 'legacy_goal_loop' && automation.goal?.status === 'suggested'),
  })
  const recognitionMutation = useMutation({
    retry: false,
    mutationFn: (enabled: boolean) => api.patchConfig('monitoring.goal_suggestions', enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['kirocrewConfig'] }),
  })
  const suggestionsEnabled = recognition.data?.monitoring?.goal_suggestions !== false
  const recognitionError = recognitionMutation.isError ? recognitionMutation.error
    : recognition.isError ? recognition.error : null
  const recognitionErrorText = recognitionError ? errMessage(recognitionError) : ''
  const recognitionErrorTitle = recognitionMutation.isError ? i18nT('components.goalProgress.recognition_save_failed')
    : recognition.isError ? i18nT('components.goalProgress.recognition_load_failed') : ''
  const suggestionsSetting = (
    <div className="border-t border-border pt-3 mt-3 space-y-2">
      <SettingsToggle
        configKey="monitoring.goal_suggestions"
        label={i18nT('components.goalProgress.recognition')}
        description={i18nT('components.goalProgress.recognition_help')}
        checked={suggestionsEnabled}
        disabled={!recognition.isSuccess || recognitionMutation.isPending}
        onChange={enabled => recognitionMutation.mutate(enabled)}
      />
      {/* No hand-off beside the unsaved manual-loop draft. Typed goals are already saved. */}
      {/* The rejection's own text is the journal key Ask Agent recovers the
          endpoint, status and code from; the translated cause leads as title. */}
      <ErrorNotice
        variant="inline" askAgent={automation?.kind === 'legacy_goal_loop' && Boolean(automation.goal)}
        title={recognitionErrorText ? recognitionErrorTitle : undefined}
        message={recognitionErrorText || recognitionErrorTitle}
      />
      {recognition.isError && (
        <Btn onClick={() => { void recognition.refetch() }} disabled={recognition.isFetching}>
          {i18nT('components.goalProgress.recognition_retry')}
        </Btn>
      )}
    </div>
  )
  const snapshotFetching = useQueryIsFetching(['session-automation', slotKey])
  const automationRef = useRef(automation)
  automationRef.current = automation
  const slotKeyRef = useRef(slotKey)
  slotKeyRef.current = slotKey
  const sessionModeUnsupported = sessionMode === 'crew' || sessionMode === 'member'
  const legacyView = (legacyLoop !== null && !(replaceableSuggestion && boundedModeSlot === slotKey))
    || (!monitor && boundedModeSlot !== slotKey)
  /* THE RUNTIME INPUT'S REAL CEILING. `contract.json` carries the ABSOLUTE
     maximum any install may configure (30 days), while the create/update
     handlers enforce the LIVE operator ceiling. The form validates against
     the smaller value after the per-slot read lands. Until then, or if the
     read fails, it uses the shipped operator ceiling rather than accepting a
     value a default server will reject after submit. */
  const liveCeiling = useQuery({
    queryKey: ['monitor-runtime-ceiling', slotKey],
    enabled: open && !legacyView,
    queryFn: () => api.monitorForSlot(slotKey),
    select: response => response.max_runtime_ceiling_secs,
    staleTime: 60_000,
    retry: false,
  })
  const ceiling = liveCeiling.isError ? undefined : liveCeiling.data
  const runtimeLimits = {
    minimum: STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs.minimum,
    maximum: typeof ceiling === 'number' && Number.isSafeInteger(ceiling) && ceiling >= 1
      ? Math.min(STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs.maximum, ceiling)
      : SHIPPED_RUNTIME_CEILING_SECS,
  }
  /* Both read-failure notices sit behind the request error: a failed save is
     what the reader must act on first, and stacking a read alert under it puts
     two unrelated errors in front of them at once. */
  const ceilingFailed = !errors.request && liveCeiling.isError
  const snapshotNoticeDue = !errors.request && snapshotFailed

  useEffect(() => {
    if (!open) return
    /* Each open re-derives the view from the RECORD, so nothing is left
       selected from a previous open. `else if` was wrong here: a slot with no
       automation kept whatever the reader last switched to, so pressing the
       bounded offer and closing made the next open of an unarmed slot show the
       pull-request form -- the exact default this change exists to remove,
       reachable again through the popover's own history. The unsaved bounded
       draft is not lost by this; it stays keyed on the editor and is there
       again the moment the offer is pressed. */
    setBoundedModeSlot(automation?.kind === 'structured_monitor' ? slotKey : null)
    setConfirmStop(false)
    setConfirmClear(false)
  }, [open, automation?.id, automation?.kind, slotKey])

  /* A primed confirmation belongs to the record the reader was LOOKING at. This
     popover re-renders from websocket state without closing, so another client
     can swap that record underneath it -- edit and restart the same monitor,
     then a spent budget stops it again -- and the primed press would act on a
     record the confirmation never described. The open-edge reset above cannot
     see that: the popover never closed. Keyed on the state the two confirms
     each depend on, so either one is dropped the moment its premise moves. */
  useEffect(() => {
    setConfirmStop(false)
    setConfirmClear(false)
  }, [monitor?.id, monitor?.active, monitor?.terminal?.outcome, monitor?.target])

  useEffect(() => {
    if (!open) return
    const incoming = monitor ? monitorDraft(monitor) : defaults()
    setEditors(current => {
      const currentEditor = current[editorKey]
      if (!currentEditor || currentEditor.sourceId !== editorKey) {
        return {
          ...current,
          [editorKey]: { draft: incoming, dirty: {}, sourceId: editorKey },
        }
      }
      const draft = { ...currentEditor.draft }
      for (const field of DRAFT_FIELDS) {
        if (!currentEditor.dirty[field]) draft[field] = incoming[field]
      }
      return { ...current, [editorKey]: { ...currentEditor, draft } }
    })
  }, [editorKey, monitor, open])

  const mutation = useMutation({
    mutationFn: (request: Mutation) => {
      if (request.action === 'create') return api.monitorCreate(request.payload)
      if (request.action === 'update') return api.monitorUpdate(request.id, request.payload)
      if (request.action === 'stop') return api.monitorStop(request.id)
      if (request.action === 'clear') return api.monitorClear(request.id)
      return api.monitorRestart(request.id)
    },
    onSuccess: (result, request) => {
      const next = normalizeAutomationRecord(result.monitor)
      const current = automationRef.current
      const cached = queryClient.getQueryData<AutomationRecord | null>(['session-automation', request.slotKey])
      const replacingSuggestion = request.action === 'create'
        && request.captured?.kind === 'legacy_goal_loop' && request.captured.goal?.status === 'suggested'
      const suggestionChanged = replacingSuggestion
        && cached !== undefined && (cached?.kind !== 'legacy_goal_loop'
          || cached.id !== request.captured?.id || cached.slotKey !== request.captured?.slotKey
          || cached.goal?.status !== 'suggested' || cached.active)
      const responseIsCurrent = current === request.captured && !suggestionChanged
      if (responseIsCurrent && request.captured !== null && next?.kind === 'structured_monitor') {
        // Publish the successor before React delivers new props. An outstanding
        // goal response must already see that this slot now holds a watch.
        if (replacingSuggestion) queryClient.setQueryData(['session-automation', request.slotKey], next)
        onChange(next)
      }
      // Refetch remains authoritative. Only an existing captured identity can
      // safely accept the bounded response; a null create identity cannot be
      // distinguished from a later clear tombstone.
      queryClient.invalidateQueries({ queryKey: ['session-automation', request.slotKey] })
      if (replacingSuggestion && !responseIsCurrent) return
      setEditors(current => {
        const next = { ...current }
        delete next[request.editorKey]
        return next
      })
      setErrorsByEditor(current => {
        const next = { ...current }
        delete next[request.editorKey]
        return next
      })
      if (slotKeyRef.current === request.slotKey) onOpenChange(false)
    },
    onError: (failure, request) => {
      setErrorsByEditor(current => ({
        ...current,
        [request.editorKey]: {
          request: monitorRequestError(failure),
        },
      }))
    },
  })

  const goalMutation = useMutation({
    retry: false,
    mutationFn: async ({ loop: captured, slotKey: capturedSlot, action }: GoalRequest) => {
      if (action === 'refresh') return api.autonudgeForSlot(captured.slotKey)
      if (action === 'dismiss') {
        if (captured.goalGeneration === undefined) return api.autonudgeForSlot(captured.slotKey)
        await api.autonudgeDismiss(captured.id, captured.goalGeneration)
        return { loop: null }
      }
      if (action === 'refresh_pause' || captured.active || captured.stoppedReason === 'goal_pause_unsaved') {
        if (action !== 'refresh_pause') {
          const result = await api.stopChatSlot(capturedSlot)
          if (result?.ok !== true || result?.compacting === true) throw new Error(i18nT('components.goalProgress.pause_failed'))
        }
        try {
          return await api.autonudgeForSlot(captured.slotKey)
        } catch (error) {
          throw new GoalPauseRefreshError(error)
        }
      }
      // A typed goal without a revision must be read before a separate resume click.
      if (captured.goalGeneration === undefined) return api.autonudgeForSlot(captured.slotKey)
      return api.autonudgeResume(captured.id, captured.goalGeneration)
    },
    onSuccess: (result, captured) => {
      if (slotKeyRef.current !== captured.slotKey) return
      const next = result.loop ? normalizeAutomationRecord(result.loop) : null
      const cached = queryClient.getQueryData<AutomationRecord | null>(['session-automation', captured.slotKey])
      // Check both live props and the cache: a late response must not republish
      // older active work into Redux, where it would outrank subsequent refetches.
      for (const current of [automationRef.current, cached]) {
        if (current === null || (current && (current.kind !== 'legacy_goal_loop'
          || current.id !== captured.loop.id || current.slotKey !== captured.loop.slotKey))) return
        if (current?.kind === 'legacy_goal_loop') {
          if (current.goal?.status === 'complete' || current.goal?.status === 'ended') return
          const responseGeneration = next?.kind === 'legacy_goal_loop' ? next.goalGeneration : captured.loop.goalGeneration
          if (current.goalGeneration !== undefined && responseGeneration !== undefined
            && current.goalGeneration > responseGeneration) return
        }
      }
      onChange(next)
      // A dismissed suggestion leaves nothing to show; do not fall through to the create form.
      if (captured.action === 'dismiss') onOpenChange(false)
    },
    onSettled: (_result, _error, captured) => {
      void queryClient.invalidateQueries({ queryKey: ['session-automation', captured.slotKey] })
      void queryClient.invalidateQueries({ queryKey: AUTONUDGE_LOOPS_QUERY_KEY })
    },
  })
  const capturedGoal = goalMutation.variables
  const goalRequestMatches = capturedGoal?.slotKey === slotKey
    && capturedGoal.loop.id === legacyLoop?.id && capturedGoal.loop.slotKey === legacyLoop.slotKey
  const unchangedGoalState = (current: AutomationRecord | null | undefined) => (
    current?.kind === 'legacy_goal_loop' && current.id === capturedGoal?.loop.id
    && current.slotKey === capturedGoal.loop.slotKey
    && current.goalGeneration === capturedGoal.loop.goalGeneration
    && current.active === capturedGoal.loop.active
    && current.stoppedReason === capturedGoal.loop.stoppedReason
    && current.goal?.status === capturedGoal.loop.goal?.status
    && current.goal?.status !== 'complete' && current.goal?.status !== 'ended'
  )
  // Reconnect may advance Redux while leaving this same-goal cache behind.
  // Only a strictly older, nonterminal snapshot is irrelevant to the request.
  const olderGoalSnapshot = goalSnapshot?.kind === 'legacy_goal_loop' && goalSnapshot.goal
    && goalSnapshot.id === capturedGoal?.loop.id && goalSnapshot.slotKey === capturedGoal.loop.slotKey
    && goalSnapshot.goalGeneration !== undefined && capturedGoal.loop.goalGeneration !== undefined
    && goalSnapshot.goalGeneration < capturedGoal.loop.goalGeneration
    && goalSnapshot.goal.status !== 'complete' && goalSnapshot.goal.status !== 'ended'
  const goalStateUnchanged = Boolean(goalRequestMatches
    && unchangedGoalState(legacyLoop)
    && (goalSnapshot === undefined || olderGoalSnapshot || unchangedGoalState(goalSnapshot)))
  const pauseUnconfirmed = Boolean(goalStateUnchanged
    && (goalMutation.error instanceof GoalPauseRefreshError
      || (capturedGoal?.action === 'refresh_pause' && !goalMutation.isSuccess)))
  let goalChangeFailure: string | undefined
  if (goalMutation.error && goalRequestMatches) {
    if (goalMutation.error instanceof GoalPauseRefreshError) {
      if (pauseUnconfirmed) goalChangeFailure = i18nT('components.goalProgress.pause_refresh_failed')
    } else if (capturedGoal.action === 'refresh') {
      if (goalStateUnchanged) goalChangeFailure = i18nT('components.goalProgress.refresh_failed')
    } else if (capturedGoal.action === 'dismiss') {
      goalChangeFailure = i18nT('components.goalProgress.dismiss_failed')
    } else if (capturedGoal.loop.stoppedReason === 'goal_pause_unsaved') {
      goalChangeFailure = i18nT('components.goalProgress.retry_pause_failed')
    } else if (capturedGoal.loop.active) {
      goalChangeFailure = i18nT('components.goalProgress.pause_failed')
    } else if (capturedGoal.loop.goal?.status === 'suggested') {
      goalChangeFailure = i18nT('components.goalProgress.start_failed')
    } else {
      goalChangeFailure = i18nT('components.goalProgress.resume_failed')
    }
  }
  // The rejection's own text, so the notice keeps the journal key Ask Agent
  // recovers the request's context from; a refresh failure wraps its cause.
  const goalChangeDetail = goalChangeFailure && goalMutation.error
    ? errMessage(goalMutation.error instanceof GoalPauseRefreshError ? goalMutation.error.cause : goalMutation.error)
    : ''

  const terminal = monitor?.terminal ?? null
  const status = monitor ? deriveAutomationStatus(monitor) : 'arm_pending'
  const statusLabel = i18nT(MONITOR_STATUS_KEYS[status])
  const legacyCycle = legacyLoop?.maxCycles
    ? `${legacyLoop.cycleCount}/${legacyLoop.maxCycles}`
    : String(legacyLoop?.cycleCount ?? 0)
  const pursuedGoal = legacyLoop?.goal
  const suggested = pursuedGoal?.status === 'suggested'
  const pursuedStatusLabel = pauseUnconfirmed ? i18nT('components.goalProgress.pause_unconfirmed')
    : legacyLoop ? goalStatusLabel(legacyLoop) : ''
  const triggerLabel = pursuedGoal && legacyLoop
    ? `${suggested ? i18nT('components.goalProgress.suggestion_question') : pursuedStatusLabel}: ${pursuedGoal.objective}`
    : legacyLoop?.active
    ? i18nT(
      interrupted
        ? 'components.autoNudgePopover.goal_interrupted_cycle'
        : 'components.autoNudgePopover.goal_active_cycle',
      { cycle: legacyCycle },
    )
    : monitor
      ? i18nT('components.sessionAutomationPopover.monitor_status', { status: statusLabel })
      : i18nT('components.autoNudgePopover.set_a_goal')
  const busy = mutation.isPending && mutation.variables?.editorKey === editorKey
  const draft = editor.draft
  const hasDirtyFields = Object.keys(editor.dirty).length > 0
  const editedMonitor = monitor

  function requestOpenChange(nextOpen: boolean) {
    if (!nextOpen && busy) return
    onOpenChange(nextOpen)
  }

  function updateDraft(field: keyof Draft, value: string) {
    setEditors(current => ({
      ...current,
      [editorKey]: {
        ...(current[editorKey] ?? incomingEditor()),
        draft: { ...(current[editorKey] ?? incomingEditor()).draft, [field]: value },
        dirty: { ...(current[editorKey] ?? incomingEditor()).dirty, [field]: true },
      },
    }))
    setErrorsByEditor(current => {
      const slotErrors = current[editorKey] ?? {}
      if (!slotErrors[field] && !slotErrors.request) return current
      const next = { ...slotErrors }
      delete next[field]
      delete next.request
      return { ...current, [editorKey]: next }
    })
  }

  function writeMonitor() {
    if (sessionModeUnsupported) return
    if (legacyLoop?.goal && !replaceableSuggestion) return
    if (goalMutation.isPending && goalRequestMatches) return
    if (!editedMonitor && !creationReady) return
    if (editedMonitor && !hasDirtyFields) return
    const cadence = boundedInteger(draft.cadence, STRUCTURED_MONITOR_LIMITS.cadenceSecs)
    const runtime = boundedInteger(draft.runtime, runtimeLimits)
    const turns = boundedInteger(draft.turns, STRUCTURED_MONITOR_LIMITS.maxAgentTurns)
    const tokens = boundedInteger(draft.tokens, STRUCTURED_MONITOR_LIMITS.maxTokens)
    const providerErrors = boundedInteger(
      draft.providerErrors,
      STRUCTURED_MONITOR_LIMITS.maxProviderErrors,
    )
    const nextErrors: FormErrors = {}
    const validates = (field: keyof Draft) => !editedMonitor || !!editor.dirty[field]
    const rangeError = (limits: { minimum: number; maximum: number }) => i18nT(
      'components.sessionAutomationPopover.limit_range',
      { min: fmtNumber(limits.minimum), max: fmtNumber(limits.maximum) },
    )
    if (validates('target') && !draft.target.trim()) {
      nextErrors.target = i18nT('components.sessionAutomationPopover.enter_pull_request_url')
    }
    const normalizedTarget = normalizePullRequestMonitorTarget(draft.target.trim())
    if (validates('target') && draft.target.trim() && !normalizedTarget) {
      nextErrors.target = i18nT('components.sessionAutomationPopover.invalid_pull_request_url')
    } else if (validates('target') && editedMonitor && normalizedTarget
      && normalizedTarget.kind !== editedMonitor.monitorKind) {
      nextErrors.target = i18nT(
        'components.sessionAutomationPopover.provider_change_requires_new_monitor',
      )
    }
    if (validates('cadence') && cadence === null) {
      nextErrors.cadence = rangeError(STRUCTURED_MONITOR_LIMITS.cadenceSecs)
    }
    if (validates('runtime') && runtime === null) {
      nextErrors.runtime = i18nT(
        'components.sessionAutomationPopover.limit_range_duration',
        {
          min: fmtNumber(runtimeLimits.minimum),
          max: fmtNumber(runtimeLimits.maximum),
          duration: describeDuration(runtimeLimits.maximum),
        },
      )
    }
    if (validates('turns') && turns === null) {
      nextErrors.turns = rangeError(STRUCTURED_MONITOR_LIMITS.maxAgentTurns)
    }
    if (validates('tokens') && tokens === null) {
      nextErrors.tokens = rangeError(STRUCTURED_MONITOR_LIMITS.maxTokens)
    }
    if (validates('providerErrors') && providerErrors === null) {
      nextErrors.providerErrors = rangeError(STRUCTURED_MONITOR_LIMITS.maxProviderErrors)
    }
    if (validates('wakeInstructions') && draft.wakeInstructions.length
      > STRUCTURED_MONITOR_LIMITS.wakeInstructions.maximumLength) {
      nextErrors.wakeInstructions = i18nT(
        'components.sessionAutomationPopover.wake_instructions_too_long',
        { max: fmtNumber(STRUCTURED_MONITOR_LIMITS.wakeInstructions.maximumLength) },
      )
    }
    if (Object.keys(nextErrors).length > 0) {
      setErrorsByEditor(current => ({ ...current, [editorKey]: nextErrors }))
      const first = Object.keys(nextErrors)[0] as keyof Draft
      const suffix = first === 'providerErrors' ? 'errors' : first
      document.getElementById(`${id}-${suffix}`)?.focus()
      return
    }
    setErrorsByEditor(current => ({ ...current, [editorKey]: {} }))
    if (!editedMonitor) {
      mutation.mutate({
        action: 'create',
        payload: {
          slot_key: slotKey,
          kind: normalizedTarget!.kind,
          objective: 'review_ready',
          target: normalizedTarget!.target,
          cadence_secs: cadence!,
          max_runtime_secs: runtime!,
          max_agent_turns: turns!,
          max_tokens: tokens!,
          max_provider_errors: providerErrors!,
          wake_instructions: draft.wakeInstructions.trim(),
        },
        captured: automation,
        slotKey,
        editorKey,
      })
      return
    }
    const payload: MonitorWrite = {}
    if (editor.dirty.target) payload.target = normalizedTarget!.target
    if (editor.dirty.cadence) payload.cadence_secs = cadence!
    if (editor.dirty.runtime) payload.max_runtime_secs = runtime!
    if (editor.dirty.turns) payload.max_agent_turns = turns!
    if (editor.dirty.tokens) payload.max_tokens = tokens!
    if (editor.dirty.providerErrors) {
      payload.max_provider_errors = providerErrors!
    }
    if (editor.dirty.wakeInstructions) {
      payload.wake_instructions = draft.wakeInstructions.trim()
    }
    mutation.mutate({
      action: 'update', id: editedMonitor.id, payload, captured: automation, slotKey, editorKey,
    })
  }

  return (
    <div data-goal-suggestion={suggested ? '' : undefined} className={`flex items-center gap-1 min-w-0 max-w-full ${suggested ? 'w-full' : ''}`}>
    <AutoNudgePopover
      key={legacyLoop && !legacyLoop.goal ? legacyLoop.id : `bounded:${slotKey}`}
      slotKey={slotKey}
      loop={legacyLoop ? legacyWire(legacyLoop) : null}
      open={open}
      onOpenChange={requestOpenChange}
      onChange={loop => {
        // Applied only while the parent still holds the record this press was
        // rendered against (object identity, as `responseIsCurrent` above). The
        // record carries no revision, and a response arriving after a frame is a
        // snapshot the frame may have moved past: a PATCH answered before another
        // tab's pause but delivered after the pause's frame put the active record
        // back over the paused one, with nothing to correct it short of a
        // reconnect. Dropped, and the slot's cold read invalidated instead.
        if (automationRef.current !== automation) {
          queryClient.invalidateQueries({ queryKey: ['session-automation', slotKey] })
          return
        }
        onChange(loop ? normalizeAutomationRecord(loop) : null)
      }}
      onFired={pressed => {
        const armed = armedNow(automationRef.current, pressed, Date.now() / 1000)
        if (armed) onChange(armed)
      }}
      onSetUpBoundedMonitor={legacyLoop ? undefined : () => setBoundedModeSlot(slotKey)}
      writeDisabled={sessionModeUnsupported}
      interrupted={interrupted}
      footer={suggestionsSetting}
      trigger={(
        <MotionIconButton
          layout={!reducedMotion}
          transition={{ duration: reducedMotion ? 0 : 0.18 }}
          aria-label={triggerLabel}
          variant={!pauseUnconfirmed && (monitor?.active || legacyLoop?.active) ? 'active' : 'default'}
          /* IconButton is a plain block button, so without a flex row the
             inline glyph sits on the text baseline of this 32px box rather
             than at its centre, and the count would trail it without a gap.
             Same row layout the legacy goal trigger has always used. */
          className={`px-2 rounded-lg flex items-center ${pursuedGoal ? 'min-h-10 min-w-0 w-full max-w-96 py-1 text-left gap-2' : 'h-8 shrink-0 gap-1'}`}
        >
          {/* The glyph promises the same thing the label does. With nothing
              armed this button opens "Set a goal", whose own panel is headed by
              the Goal icon, so a radar here is the promise-mismatch this change
              fixes in the label -- and there is no probing to depict. Once
              anything IS armed the radar is accurate and carries the
              action-running pulse. */}
          {pursuedGoal ? (
            <Goal className="lucide-inline shrink-0" aria-hidden />
          ) : monitor || legacyLoop ? (
            <MonitorRadar actionRunning={status === 'action_running'} />
          ) : (
            <Goal className="lucide-inline shrink-0" aria-hidden />
          )}
          {pursuedGoal && legacyLoop ? (
            <span className="min-w-0 flex-1">
              <span className="block whitespace-normal break-words text-[11px]" role="status" aria-live="polite">{suggested ? i18nT('components.goalProgress.suggestion_question') : pursuedStatusLabel}</span>
              <span className="block truncate text-[12px] text-text">{pursuedGoal.objective}</span>
              {suggested && (
                <span id={`${id}-start-limit`} className="block whitespace-normal break-words text-[11px] text-muted">
                  {legacyLoop.maxCycles > 0
                    ? i18nT('components.goalProgress.start_cycle_limit', { limit: fmtNumber(legacyLoop.maxCycles) })
                    : i18nT('components.goalProgress.start_no_cycle_limit')}
                </span>
              )}
            </span>
          ) : monitor ? (
            <span className="text-[11px] font-mono">{fmtNumber(monitor.usage.probes)}</span>
          ) : legacyLoop?.cycleCount ? (
            <span className="text-[11px] font-mono">{legacyCycle}</span>
          ) : null}
          {pursuedGoal && <ChevronDown className={`lucide-inline shrink-0 text-muted ${open ? 'rotate-180' : ''}`} aria-hidden />}
        </MotionIconButton>
      )}
      content={legacyLoop?.goal && legacyView ? (
        <GoalProgressContent
          loop={legacyLoop}
          statusLabel={pursuedStatusLabel}
          changeFailure={goalChangeFailure}
          changeFailureDetail={goalChangeDetail}
          pauseUnconfirmed={pauseUnconfirmed}
          pending={busy || (goalMutation.isPending && Boolean(goalRequestMatches))}
          settings={suggestionsSetting}
          onSetUpBoundedMonitor={replaceableSuggestion ? () => setBoundedModeSlot(slotKey) : undefined}
          onDismiss={legacyLoop.goal.status === 'suggested' && legacyLoop.goalGeneration !== undefined
            ? () => goalMutation.mutate({ loop: legacyLoop, slotKey, action: 'dismiss' })
            : undefined}
          onAction={() => goalMutation.mutate({
            loop: legacyLoop, slotKey,
            action: pauseUnconfirmed ? 'refresh_pause'
              : !legacyLoop.active && legacyLoop.stoppedReason !== 'goal_pause_unsaved' && legacyLoop.goalGeneration === undefined
                ? 'refresh' : 'change',
          })}
        />
      ) : legacyView ? undefined : (
        <PopoverContent
          side="top"
          align="start"
          collisionPadding={8}
          className="w-[min(calc(100vw-1rem),32rem)] max-h-[min(80vh,42rem,var(--radix-popover-content-available-height))] overflow-y-auto p-4 text-[12px]"
        >
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-sm font-semibold text-text">
              <Radar className="lucide-inline text-accent shrink-0" aria-hidden />
              {i18nT('components.sessionAutomationPopover.title')}
            </h2>
            <p className="mt-1 text-[11px] leading-relaxed text-muted">
              {i18nT('components.sessionAutomationPopover.description')}
            </p>
          </div>
          <IconButton aria-label={i18nT('components.sessionAutomationPopover.close')} onClick={() => requestOpenChange(false)}>
            <X className="lucide-inline" aria-hidden />
          </IconButton>
        </div>

        {sessionModeUnsupported ? (
          <p
            role="status"
            className="mb-3 rounded-md border border-border bg-bg px-3 py-2 text-[11px] leading-relaxed text-muted"
          >
            {i18nT('components.sessionAutomationPopover.session_mode_unavailable')}
          </p>
        ) : null}

        {monitor ? (
          <div className="mb-4 rounded-lg border border-border bg-bg p-3 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={terminal ? (status === 'success' ? 'ok' : status === 'blocked' ? 'err' : 'warn') : 'aim'}>
                {statusLabel}
              </Badge>
              <span className="min-w-0 truncate text-muted" translate="no">{monitor.target}</span>
            </div>
            <dl className="grid grid-cols-1 min-[390px]:grid-cols-2 gap-x-3 gap-y-2 text-[11px]">
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.objective')}</dt><dd>{i18nT('components.sessionAutomationPopover.review_ready')}</dd></div>
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.next_probe')}</dt><dd>{monitor.nextProbeAt ? fmtDateTimeNumeric(monitor.nextProbeAt) : i18nT('components.sessionAutomationPopover.not_scheduled')}</dd></div>
              <div>
                <dt className="text-muted">{i18nT('components.sessionAutomationPopover.latest_classification')}</dt>
                <dd translate="no">
                  {monitor.latest.classification || i18nT('components.sessionAutomationPopover.awaiting_first_probe')}
                  {monitor.latest.reasonCode ? ` · ${monitor.latest.reasonCode}` : null}
                </dd>
              </div>
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.latest_decision')}</dt><dd translate="no">{monitor.latest.decision || i18nT('components.sessionAutomationPopover.none_yet')}</dd></div>
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.probe_cadence')}</dt><dd>{fmtNumber(monitor.cadenceSecs)}</dd></div>
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.maximum_runtime')}</dt><dd>{fmtNumber(monitor.budgets.maxRuntimeSecs)}</dd></div>
              {/* 0 is this budget's unlimited sentinel, so the number is not the
                  reading: "Maximum agent turns: 0" says the opposite of what it
                  means. Same treatment the token figure gets when usage is
                  unreported -- a word where no number is the truth. */}
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.maximum_agent_turns')}</dt><dd>{monitor.budgets.maxAgentTurns === 0 ? i18nT('components.sessionAutomationPopover.unlimited') : fmtNumber(monitor.budgets.maxAgentTurns)}</dd></div>
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.maximum_tokens')}</dt><dd>{fmtNumber(monitor.budgets.maxTokens)}</dd></div>
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.maximum_provider_errors')}</dt><dd>{fmtNumber(monitor.budgets.maxProviderErrors)}</dd></div>
            </dl>
            <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted">
              <span>{i18nT('components.sessionAutomationPopover.probes', { count: fmtNumber(monitor.usage.probes) })}</span>
              <span>{i18nT('components.sessionAutomationPopover.wakes', { count: fmtNumber(monitor.usage.wakes) })}</span>
              <span>{i18nT('components.sessionAutomationPopover.agent_turns', { count: fmtNumber(monitor.usage.agentTurns) })}</span>
              <span>{i18nT('components.sessionAutomationPopover.tokens', { count: monitor.usage.tokenUsageKnown ? fmtNumber(monitor.usage.inputTokens + monitor.usage.outputTokens) : i18nT('components.sessionAutomationPopover.unknown') })}</span>
              {monitor.usage.tokenUsageKnown ? (
                <>
                  <span>{i18nT('pages.overview.usageTab.input_tokens')}: {fmtNumber(monitor.usage.inputTokens)}</span>
                  <span>{i18nT('pages.overview.usageTab.output_tokens')}: {fmtNumber(monitor.usage.outputTokens)}</span>
                </>
              ) : null}
              <span>{i18nT('components.sessionAutomationPopover.provider_errors', { count: fmtNumber(monitor.usage.providerErrors) })}</span>
            </div>
            {terminal ? (
              <div className="space-y-2 border-t border-border pt-2">
                {monitor.wakeInstructions ? (
                  <div>
                    <div className="text-muted">{i18nT('components.sessionAutomationPopover.wake_instructions')}</div>
                    <p className="mt-0.5 break-words text-text leading-relaxed">
                      {monitor.wakeInstructions}
                    </p>
                  </div>
                ) : null}
                <div>
                  <div className="text-muted">{i18nT('components.sessionAutomationPopover.terminal_reason')}</div>
                  <div className="mt-0.5 text-text">{statusLabel}</div>
                  <div className="mt-0.5 font-mono break-words text-muted" translate="no">
                    {terminal.reason || terminal.outcome}
                  </div>
                  {terminal.stoppedAt > 0 ? (
                    <div className="mt-0.5 text-muted">{fmtDateTimeNumeric(terminal.stoppedAt)}</div>
                  ) : null}
                </div>
                  {/* The two exits, named where the terminal state is described.
                    Restart alone is not a way out: this record keeps occupying
                    the session and a stopped one refuses a new monitor, so a
                    reader who only sees Restart cannot tell how to watch
                    something else -- and the other exit is irreversible, which
                    nothing else on this surface says. While confirming, it
                    BECOMES the question instead: the buttons it names have left
                    the row, and the confirmation renders no question of its own. */}
                <p data-testid="monitor-terminal-exits" className="text-muted leading-relaxed">
                  {confirmClear
                    ? i18nT('components.sessionAutomationPopover.clear_monitor_question')
                    : i18nT('components.sessionAutomationPopover.terminal_exits')}
                </p>
              </div>
            ) : null}
          </div>
        ) : null}

        {!terminal ? (
          <fieldset disabled={busy || sessionModeUnsupported} className="m-0 min-w-0 space-y-3 border-0 p-0">
            <div className={fieldClass}>
              <div id={`${id}-target-label`} className={labelClass}>{i18nT('components.sessionAutomationPopover.pull_request_url')}</div>
              <Input
                id={`${id}-target`}
                name="monitor-target"
                type="url"
                autoComplete="url"
                value={draft.target}
                onChange={event => updateDraft('target', event.target.value)}
                placeholder={i18nT('components.sessionAutomationPopover.pull_request_url_placeholder')}
                aria-labelledby={`${id}-target-label`}
                aria-invalid={!!errors.target}
                aria-describedby={`${id}-target-help${errors.target ? ` ${id}-target-error` : ''}`}
              />
              <p id={`${id}-target-help`} className="text-[11px] text-muted">
                {i18nT('components.sessionAutomationPopover.supported_source_providers')}
              </p>
              <FieldError id={`${id}-target-error`} message={errors.target} />
            </div>
            <div className="grid grid-cols-1 min-[390px]:grid-cols-2 gap-3">
              <div className={fieldClass}>
                <div id={`${id}-cadence-label`} className={labelClass}>{i18nT('components.sessionAutomationPopover.probe_cadence')}</div>
                <Input
                  id={`${id}-cadence`}
                  name="monitor-cadence"
                  type="number"
                  inputMode="numeric"
                  autoComplete="off"
                  min={STRUCTURED_MONITOR_LIMITS.cadenceSecs.minimum}
                  max={STRUCTURED_MONITOR_LIMITS.cadenceSecs.maximum}
                  step={1}
                  value={draft.cadence}
                  aria-labelledby={`${id}-cadence-label`}
                  onChange={event => updateDraft('cadence', event.target.value)}
                  aria-invalid={!!errors.cadence}
                  aria-describedby={errors.cadence ? `${id}-cadence-error` : undefined}
                />
                <FieldError id={`${id}-cadence-error`} message={errors.cadence} />
              </div>
              <div className={fieldClass}>
                <div id={`${id}-runtime-label`} className={labelClass}>{i18nT('components.sessionAutomationPopover.maximum_runtime')}</div>
                <Input
                  id={`${id}-runtime`}
                  name="monitor-runtime"
                  type="number"
                  inputMode="numeric"
                  autoComplete="off"
                  min={runtimeLimits.minimum}
                  max={runtimeLimits.maximum}
                  step={1}
                  value={draft.runtime}
                  aria-labelledby={`${id}-runtime-label`}
                  onChange={event => updateDraft('runtime', event.target.value)}
                  aria-invalid={!!errors.runtime}
                  aria-describedby={errors.runtime ? `${id}-runtime-error` : undefined}
                />
                <FieldError id={`${id}-runtime-error`} message={errors.runtime} />
              </div>
              <div className={fieldClass}>
                <div id={`${id}-turns-label`} className={labelClass}>{i18nT('components.sessionAutomationPopover.maximum_agent_turns')}</div>
                <Input
                  id={`${id}-turns`}
                  name="monitor-turns"
                  type="number"
                  inputMode="numeric"
                  autoComplete="off"
                  min={STRUCTURED_MONITOR_LIMITS.maxAgentTurns.minimum}
                  max={STRUCTURED_MONITOR_LIMITS.maxAgentTurns.maximum}
                  step={1}
                  value={draft.turns}
                  aria-labelledby={`${id}-turns-label`}
                  onChange={event => updateDraft('turns', event.target.value)}
                  aria-invalid={!!errors.turns}
                  aria-describedby={
                    errors.turns ? `${id}-turns-error` : `${id}-turns-hint`
                  }
                />
                {/* On a "maximum" field, entering 0 reads as "none allowed".
                    The hint carries the sentinel's meaning, the way the legacy
                    cycle cap spells it in its own label. */}
                <div id={`${id}-turns-hint`} className="text-[11px] text-muted">
                  {i18nT('components.sessionAutomationPopover.wake_budget_zero_hint')}
                </div>
                <FieldError id={`${id}-turns-error`} message={errors.turns} />
              </div>
              <div className={fieldClass}>
                <div id={`${id}-tokens-label`} className={labelClass}>{i18nT('components.sessionAutomationPopover.maximum_tokens')}</div>
                <Input
                  id={`${id}-tokens`}
                  name="monitor-tokens"
                  type="number"
                  inputMode="numeric"
                  autoComplete="off"
                  min={STRUCTURED_MONITOR_LIMITS.maxTokens.minimum}
                  max={STRUCTURED_MONITOR_LIMITS.maxTokens.maximum}
                  step={1}
                  value={draft.tokens}
                  aria-labelledby={`${id}-tokens-label`}
                  onChange={event => updateDraft('tokens', event.target.value)}
                  aria-invalid={!!errors.tokens}
                  aria-describedby={errors.tokens ? `${id}-tokens-error` : undefined}
                />
                <FieldError id={`${id}-tokens-error`} message={errors.tokens} />
              </div>
              <div className={fieldClass}>
                <div id={`${id}-errors-label`} className={labelClass}>{i18nT('components.sessionAutomationPopover.maximum_provider_errors')}</div>
                <Input
                  id={`${id}-errors`}
                  name="monitor-provider-errors"
                  type="number"
                  inputMode="numeric"
                  autoComplete="off"
                  min={STRUCTURED_MONITOR_LIMITS.maxProviderErrors.minimum}
                  max={STRUCTURED_MONITOR_LIMITS.maxProviderErrors.maximum}
                  step={1}
                  value={draft.providerErrors}
                  aria-labelledby={`${id}-errors-label`}
                  onChange={event => updateDraft('providerErrors', event.target.value)}
                  aria-invalid={!!errors.providerErrors}
                  aria-describedby={errors.providerErrors ? `${id}-errors-error` : undefined}
                />
                <FieldError id={`${id}-errors-error`} message={errors.providerErrors} />
              </div>
            </div>
            <div className={fieldClass}>
              <div id={`${id}-wake-label`} className={labelClass}>{i18nT('components.sessionAutomationPopover.wake_instructions')}</div>
              <textarea
                id={`${id}-wake`}
                name="monitor-wake-instructions"
                autoComplete="off"
                rows={3}
                maxLength={STRUCTURED_MONITOR_LIMITS.wakeInstructions.maximumLength}
                value={draft.wakeInstructions}
                aria-labelledby={`${id}-wake-label`}
                onChange={event => updateDraft('wakeInstructions', event.target.value)}
                aria-invalid={!!errors.wakeInstructions}
                aria-describedby={errors.wakeInstructions ? `${id}-wake-error` : undefined}
                className="focus-ring w-full resize-y rounded-md border border-border bg-bg-elevated px-3 py-2 text-sm text-text"
              />
              <FieldError id={`${id}-wake-error`} message={errors.wakeInstructions} />
            </div>
          </fieldset>
        ) : null}

        {/* No hand-off: navigating away would discard the unsaved monitor draft. */}
        <ErrorNotice message={errors.request} className="mt-3" />
        {replaceableSuggestion && <p className="mt-3 text-[12px] text-muted">
          {i18nT('components.goalProgress.watch_replaces_suggestion')}
        </p>}
        {ceilingFailed || snapshotNoticeDue ? (
          <div className="mt-3 space-y-2">
            {/* ONE notice for the two reads that can fail together. The snapshot
               and the live ceiling are separate queries with the same failure
               mode and the same wording, so a notice per read puts two
               identical alerts with two identical buttons in front of the
               reader. One button retries whichever reads failed. */}
            {/* No hand-off: a failed refresh must preserve the unsaved monitor draft. */}
            <ErrorNotice
              message={i18nT('components.sessionAutomationPopover.snapshot_failed')}
              testId="monitor-read-error"
            />
            <Btn
              type="button"
              disabled={(ceilingFailed && liveCeiling.isFetching) || (snapshotNoticeDue && snapshotFetching)}
              onClick={() => {
                if (ceilingFailed) void liveCeiling.refetch()
                if (snapshotNoticeDue) {
                  void queryClient.refetchQueries({ queryKey: ['session-automation', slotKey], exact: true })
                }
              }}
            >
              {i18nT('components.sessionAutomationPopover.retry_snapshot')}
            </Btn>
          </div>
        ) : null}

        <div className="mt-4 flex flex-wrap justify-end gap-2">
          {!monitor ? (
            <>
              <Btn
                type="button"
                /* NOT gated on the session mode. This button only changes which
                   view is showing -- it writes nothing, so there is nothing for
                   an unsupported mode to refuse -- and it is the only labelled
                   way back to the default view. Disabling it stranded a
                   crew/member reader on the bounded form: the offer that brings
                   them here carries no mode gate, so they could arrive and then
                   find the exit dead, with Close as the only move. Every
                   control that WRITES on this form stays gated. */
                onClick={() => setBoundedModeSlot(null)}
              >
                {i18nT(replaceableSuggestion ? 'components.goalProgress.back_to_suggestion'
                  : 'components.sessionAutomationPopover.use_legacy_costly')}
              </Btn>
              <SendBtn
                type="button"
                disabled={busy || !creationReady || sessionModeUnsupported
                  || (goalMutation.isPending && Boolean(goalRequestMatches))}
                onClick={writeMonitor}
              >
                {i18nT('components.sessionAutomationPopover.start_monitor')}
              </SendBtn>
            </>
          ) : terminal ? (
            confirmClear ? (
              <>
                <Btn type="button" disabled={busy} onClick={() => setConfirmClear(false)}>{i18nT('components.sessionAutomationPopover.cancel')}</Btn>
                <Btn
                  type="button"
                  danger
                  disabled={busy}
                  data-testid="monitor-confirm-clear"
                  onClick={() => mutation.mutate({
                    action: 'clear',
                    id: monitor.id,
                    captured: automation,
                    slotKey,
                    editorKey,
                  })}
                >
                  {i18nT('components.sessionAutomationPopover.confirm_clear')}
                </Btn>
              </>
            ) : (
              <>
                {/* A stopped monitor's record keeps occupying the session, and a
                    retained stop REFUSES a re-arm, so Restart alone leaves the
                    session able to watch only the subject the user stopped
                    watching. Clearing is the other exit, and the only one that
                    frees the slot. Behind a confirm because it is irreversible,
                    matching the stop control's own confirm. */}
                <Btn
                  type="button"
                  danger
                  disabled={busy || sessionModeUnsupported}
                  data-testid="monitor-clear"
                  onClick={() => setConfirmClear(true)}
                >
                  <Trash2 className="lucide-inline" aria-hidden /> {i18nT('components.sessionAutomationPopover.clear_monitor')}
                </Btn>
                <SendBtn
                  type="button"
                  disabled={busy || !monitor.actionable || sessionModeUnsupported}
                  onClick={() => mutation.mutate({
                    action: 'restart',
                    id: monitor.id,
                    captured: automation,
                    slotKey,
                    editorKey,
                  })}
                >
                  <RotateCw className="lucide-inline" aria-hidden /> {i18nT('components.sessionAutomationPopover.restart_monitor')}
                </SendBtn>
              </>
            )
          ) : confirmStop ? (
            <>
              <Btn type="button" disabled={busy} onClick={() => setConfirmStop(false)}>{i18nT('components.sessionAutomationPopover.cancel')}</Btn>
              <Btn type="button" danger disabled={busy} onClick={() => mutation.mutate({ action: 'stop', id: monitor.id, captured: automation, slotKey, editorKey })}>{i18nT('components.sessionAutomationPopover.confirm_stop')}</Btn>
            </>
          ) : (
            <>
              <Btn type="button" danger disabled={busy} onClick={() => setConfirmStop(true)}><Square className="lucide-inline" aria-hidden /> {i18nT('components.sessionAutomationPopover.stop_monitor')}</Btn>
              <SendBtn
                type="button"
                disabled={busy || !monitor.actionable || !hasDirtyFields || sessionModeUnsupported}
                onClick={writeMonitor}
              >
                <Activity className="lucide-inline" aria-hidden />{' '}
                {i18nT('components.sessionAutomationPopover.save_changes')}
              </SendBtn>
            </>
          )}
        </div>
        </PopoverContent>
      )}
    />
    {/* One Start per layout: the open details card carries its own. */}
    {suggested && legacyLoop && !open && (
      <Btn
        aria-describedby={`${id}-start-limit`}
        disabled={busy || (goalMutation.isPending && Boolean(goalRequestMatches))}
        onClick={() => {
          setBoundedModeSlot(null)
          onOpenChange(true)
          goalMutation.mutate({
            loop: legacyLoop, slotKey,
            action: legacyLoop.goalGeneration === undefined ? 'refresh' : 'change',
          })
        }}
      >
        {legacyLoop.goalGeneration === undefined ? <RotateCw className="lucide-inline" aria-hidden /> : <Play className="lucide-inline" aria-hidden />}
        {i18nT(legacyLoop.goalGeneration === undefined ? 'components.goalProgress.refresh_status' : 'components.goalProgress.start')}
      </Btn>
    )}
    </div>
  )
}
