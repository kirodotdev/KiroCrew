import { useEffect, useId, useRef, useState } from 'react'
import { Activity, Radar, RotateCw, Square, Trash2, X } from 'lucide-react'
import { useIsFetching, useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type MonitorWrite } from '../api/client'
import {
  deriveAutomationStatus,
  MONITOR_STATUS_KEYS,
  normalizeAutomationRecord,
  STRUCTURED_MONITOR_DEFAULTS,
  STRUCTURED_MONITOR_LIMITS,
  type AutomationRecord,
  type LegacyGoalLoop,
  type StructuredMonitor,
} from '../monitoring/automation'
import { fmtDateTimeNumeric, fmtNumber } from '../i18n/format'
import { Badge, Btn, IconButton, Input, SendBtn } from './ui'
import { PopoverContent } from './ui/popover'
import AutoNudgePopover, { type AutoNudgeLoop } from './AutoNudgePopover'
import { i18nT } from '../i18n/t'
import MonitorRadar from './MonitorRadar'
import ErrorNotice from './ErrorNotice'

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

function legacyWire(loop: LegacyGoalLoop): AutoNudgeLoop {
  return {
    id: loop.id,
    slot_key: loop.slotKey,
    message: loop.message,
    idle_secs: loop.idleSecs,
    max_cycles: loop.maxCycles,
    cycle_count: loop.cycleCount,
    active: loop.active,
    last_fire_ts: loop.lastFireAt,
    next_due_ts: loop.nextDueAt ?? 0,
  }
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
  const monitor = automation?.kind === 'structured_monitor' ? automation : null
  const [legacyModeSlot, setLegacyModeSlot] = useState<string | null>(
    automation?.kind === 'legacy_goal_loop' ? slotKey : null,
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
  const snapshotFetching = useIsFetching({ queryKey: ['session-automation', slotKey], exact: true }) > 0
  const automationRef = useRef(automation)
  automationRef.current = automation
  const slotKeyRef = useRef(slotKey)
  slotKeyRef.current = slotKey
  const sessionModeUnsupported = sessionMode === 'crew' || sessionMode === 'member'
  const legacyView = automation?.kind === 'legacy_goal_loop' || legacyModeSlot === slotKey

  useEffect(() => {
    if (!open) return
    if (automation?.kind === 'legacy_goal_loop') setLegacyModeSlot(slotKey)
    setConfirmStop(false)
    setConfirmClear(false)
  }, [open, automation?.id, automation?.kind, slotKey])

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
      if (request.captured !== null
        && automationRef.current === request.captured
        && next?.kind === 'structured_monitor') {
        onChange(next)
      }
      // Refetch remains authoritative. Only an existing captured identity can
      // safely accept the bounded response; a null create identity cannot be
      // distinguished from a later clear tombstone.
      queryClient.invalidateQueries({ queryKey: ['session-automation', request.slotKey] })
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
    onError: (_error, request) => {
      setErrorsByEditor(current => ({
        ...current,
        [request.editorKey]: {
          request: i18nT('components.sessionAutomationPopover.request_failed'),
        },
      }))
    },
  })

  const terminal = monitor?.terminal ?? null
  const status = monitor ? deriveAutomationStatus(monitor) : 'arm_pending'
  const statusLabel = i18nT(MONITOR_STATUS_KEYS[status])
  const legacyLoop = automation?.kind === 'legacy_goal_loop' ? automation : null
  const legacyCycle = legacyLoop?.maxCycles
    ? `${legacyLoop.cycleCount}/${legacyLoop.maxCycles}`
    : String(legacyLoop?.cycleCount ?? 0)
  const triggerLabel = legacyLoop?.active
    ? i18nT(
      interrupted
        ? 'components.autoNudgePopover.goal_interrupted_cycle'
        : 'components.autoNudgePopover.goal_active_cycle',
      { cycle: legacyCycle },
    )
    : monitor
      ? i18nT('components.sessionAutomationPopover.monitor_status', { status: statusLabel })
      : i18nT('components.sessionAutomationPopover.set_up_bounded_monitor')
  const busy = mutation.isPending && mutation.variables?.editorKey === editorKey
  const draft = editor.draft
  const hasDirtyFields = Object.keys(editor.dirty).length > 0

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
    if (!monitor && !creationReady) return
    if (monitor && !hasDirtyFields) return
    const cadence = boundedInteger(draft.cadence, STRUCTURED_MONITOR_LIMITS.cadenceSecs)
    const runtime = boundedInteger(draft.runtime, STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs)
    const turns = boundedInteger(draft.turns, STRUCTURED_MONITOR_LIMITS.maxAgentTurns)
    const tokens = boundedInteger(draft.tokens, STRUCTURED_MONITOR_LIMITS.maxTokens)
    const providerErrors = boundedInteger(
      draft.providerErrors,
      STRUCTURED_MONITOR_LIMITS.maxProviderErrors,
    )
    const nextErrors: FormErrors = {}
    const validates = (field: keyof Draft) => !monitor || !!editor.dirty[field]
    const rangeError = (limits: { minimum: number; maximum: number }) => i18nT(
      'components.sessionAutomationPopover.limit_range',
      { min: fmtNumber(limits.minimum), max: fmtNumber(limits.maximum) },
    )
    if (validates('target') && !draft.target.trim()) {
      nextErrors.target = i18nT('components.sessionAutomationPopover.enter_pull_request_url')
    }
    if (validates('cadence') && cadence === null) {
      nextErrors.cadence = rangeError(STRUCTURED_MONITOR_LIMITS.cadenceSecs)
    }
    if (validates('runtime') && runtime === null) {
      nextErrors.runtime = rangeError(STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs)
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
    const createPayload = {
      kind: 'github_pull_request' as const,
      objective: 'review_ready' as const,
      target: draft.target.trim(),
      cadence_secs: cadence!,
      max_runtime_secs: runtime!,
      max_agent_turns: turns!,
      max_tokens: tokens!,
      max_provider_errors: providerErrors!,
      wake_instructions: draft.wakeInstructions.trim(),
    }
    if (!monitor) {
      mutation.mutate({
        action: 'create',
        payload: { ...createPayload, slot_key: slotKey },
        captured: automation,
        slotKey,
        editorKey,
      })
      return
    }
    const payload: MonitorWrite = {}
    if (editor.dirty.target) payload.target = createPayload.target
    if (editor.dirty.cadence) payload.cadence_secs = createPayload.cadence_secs
    if (editor.dirty.runtime) payload.max_runtime_secs = createPayload.max_runtime_secs
    if (editor.dirty.turns) payload.max_agent_turns = createPayload.max_agent_turns
    if (editor.dirty.tokens) payload.max_tokens = createPayload.max_tokens
    if (editor.dirty.providerErrors) {
      payload.max_provider_errors = createPayload.max_provider_errors
    }
    if (editor.dirty.wakeInstructions) {
      payload.wake_instructions = createPayload.wake_instructions
    }
    mutation.mutate({
      action: 'update', id: monitor.id, payload, captured: automation, slotKey, editorKey,
    })
  }

  return (
    <AutoNudgePopover
      key={legacyLoop?.id ?? `bounded:${slotKey}`}
      slotKey={slotKey}
      loop={legacyLoop ? legacyWire(legacyLoop) : null}
      open={open}
      onOpenChange={requestOpenChange}
      onChange={loop => {
        if (automationRef.current !== automation) return
        onChange(loop ? normalizeAutomationRecord(loop) : null)
      }}
      onBackToBoundedMonitor={legacyLoop ? undefined : () => setLegacyModeSlot(null)}
      writeDisabled={sessionModeUnsupported}
      interrupted={interrupted}
      trigger={(
        <IconButton
          aria-label={triggerLabel}
          variant={monitor?.active || legacyLoop?.active ? 'active' : 'default'}
          className="h-8 px-2 rounded-lg shrink-0"
        >
          <MonitorRadar actionRunning={status === 'action_running'} />
          {monitor ? (
            <span className="text-[11px] font-mono">{fmtNumber(monitor.usage.probes)}</span>
          ) : legacyLoop?.cycleCount ? (
            <span className="text-[11px] font-mono">{legacyCycle}</span>
          ) : null}
        </IconButton>
      )}
      content={legacyView ? undefined : (
        <PopoverContent
          side="top"
          align="start"
          className="w-[min(calc(100vw-1rem),32rem)] max-h-[min(80vh,42rem)] overflow-y-auto p-4 text-[12px]"
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
              <div><dt className="text-muted">{i18nT('components.sessionAutomationPopover.maximum_agent_turns')}</dt><dd>{fmtNumber(monitor.budgets.maxAgentTurns)}</dd></div>
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
                    nothing else on this surface says. */}
                <p data-testid="monitor-terminal-exits" className="text-muted leading-relaxed">
                  {i18nT('components.sessionAutomationPopover.terminal_exits')}
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
                aria-describedby={errors.target ? `${id}-target-error` : undefined}
              />
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
                  min={STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs.minimum}
                  max={STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs.maximum}
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
                  aria-describedby={errors.turns ? `${id}-turns-error` : undefined}
                />
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
        {!errors.request && snapshotFailed ? (
          <div className="mt-3 space-y-2">
            {/* No hand-off: a failed refresh must preserve the unsaved monitor draft. */}
            <ErrorNotice message={i18nT('components.sessionAutomationPopover.snapshot_failed')} />
            <Btn
              type="button"
              disabled={snapshotFetching}
              onClick={() => { void queryClient.refetchQueries({ queryKey: ['session-automation', slotKey], exact: true }) }}
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
                disabled={sessionModeUnsupported}
                onClick={() => setLegacyModeSlot(slotKey)}
              >
                {i18nT('components.sessionAutomationPopover.use_legacy_costly')}
              </Btn>
              <SendBtn
                type="button"
                disabled={busy || !creationReady || sessionModeUnsupported}
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
  )
}
