import monitorContract from './contract.json'

export const STRUCTURED_MONITOR_LIMITS = monitorContract.limits

export const STRUCTURED_MONITOR_DEFAULTS = {
  cadenceSecs: STRUCTURED_MONITOR_LIMITS.cadenceSecs.defaultValue,
  maxRuntimeSecs: STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs.defaultValue,
  maxAgentTurns: STRUCTURED_MONITOR_LIMITS.maxAgentTurns.defaultValue,
  maxTokens: STRUCTURED_MONITOR_LIMITS.maxTokens.defaultValue,
  maxProviderErrors: STRUCTURED_MONITOR_LIMITS.maxProviderErrors.defaultValue,
} as const

export type PullRequestMonitorKind =
  | 'azure_devops_pull_request'
  | 'bitbucket_pull_request'
  | 'github_pull_request'
  | 'gitlab_merge_request'

export const PULL_REQUEST_MONITOR_KINDS = (
  monitorContract.pullRequestMonitorKinds as PullRequestMonitorKind[]
)

export type NormalizedPullRequestMonitorTarget = {
  kind: PullRequestMonitorKind
  target: string
}

export function normalizePullRequestMonitorTarget(
  target: string,
): NormalizedPullRequestMonitorTarget | null {
  let url: URL
  try {
    url = new URL(target)
  } catch {
    return null
  }
  if (url.protocol !== 'https:' || url.username || url.password) return null
  const parts = url.pathname.split('/').filter(Boolean)
  const canonical = (kind: PullRequestMonitorKind, canonicalParts: string[]) => ({
    kind,
    target: `${url.origin}/${canonicalParts.join('/')}`,
  })
  const githubParts = parts.at(-1) === 'files' ? parts.slice(0, -1) : parts
  if (!url.port && ['github.com', 'www.github.com'].includes(url.hostname)
    && githubParts.length === 4 && githubParts[2] === 'pull'
    && /^[1-9]\d*$/.test(githubParts[3])) {
    return canonical('github_pull_request', githubParts)
  }
  const gitlabParts = parts.at(-1) === 'diffs' ? parts.slice(0, -1) : parts
  if (gitlabParts.length >= 5 && gitlabParts.at(-3) === '-'
    && gitlabParts.at(-2) === 'merge_requests'
    && /^[1-9]\d*$/.test(gitlabParts.at(-1) ?? '')) {
    return canonical('gitlab_merge_request', gitlabParts)
  }
  if (url.hostname === 'dev.azure.com' && parts.length === 6 && parts[2] === '_git'
    && !url.port && parts[4] === 'pullrequest' && /^[1-9]\d*$/.test(parts[5])) {
    return canonical('azure_devops_pull_request', parts)
  }
  const bitbucketParts = parts.at(-1) === 'diff' ? parts.slice(0, -1) : parts
  if (url.hostname === 'bitbucket.org' && !url.port && bitbucketParts.length === 4
    && bitbucketParts[2] === 'pull-requests' && /^[1-9]\d*$/.test(bitbucketParts[3])) {
    return canonical('bitbucket_pull_request', bitbucketParts)
  }
  return null
}

export type MonitorStatus =
  | 'arm_pending'
  | 'active'
  | 'backing_off'
  | 'action_running'
  | 'success'
  | 'blocked'
  | 'budget_stopped'
  | 'user_stopped'

export const MONITOR_STATUS_KEYS: Record<MonitorStatus, string> = {
  action_running: 'components.sessionAutomationPopover.statuses.action_running',
  active: 'components.sessionAutomationPopover.statuses.active',
  arm_pending: 'components.sessionAutomationPopover.statuses.arm_pending',
  backing_off: 'components.sessionAutomationPopover.statuses.backing_off',
  blocked: 'components.sessionAutomationPopover.statuses.blocked',
  budget_stopped: 'components.sessionAutomationPopover.statuses.budget_stopped',
  success: 'components.sessionAutomationPopover.statuses.success',
  user_stopped: 'components.sessionAutomationPopover.statuses.user_stopped',
}

export interface LegacyGoalLoop {
  kind: 'legacy_goal_loop'
  id: string
  slotKey: string
  message: string
  idleSecs: number
  maxCycles: number
  cycleCount: number
  active: boolean
  lastFireAt: number
  nextDueAt?: number
  maxRuntimeSecs?: number
  stoppedReason: string
}

export interface StructuredMonitor {
  kind: 'structured_monitor'
  id: string
  slotKey: string
  active: boolean
  actionable: boolean
  version: number
  monitorKind: PullRequestMonitorKind | string
  objective: 'review_ready' | string
  target: string
  cadenceSecs: number
  nextProbeAt: number
  wakeInstructions: string
  budgets: {
    maxRuntimeSecs: number
    maxAgentTurns: number
    maxTokens: number
    maxProviderErrors: number
  }
  latest: {
    classification: string
    reasonCode: string
    observedAt: number
    decision: string
  }
  usage: {
    probes: number
    wakes: number
    agentTurns: number
    inputTokens: number
    outputTokens: number
    providerErrors: number
    tokenUsageKnown: boolean
  }
  action: {
    wakeInFlight: boolean
    wakeDelivery: string
  }
  terminal: null | {
    outcome: string
    reason: string
    stoppedAt: number
  }
}

export type AutomationRecord = LegacyGoalLoop | StructuredMonitor

type JsonObject = Record<string, unknown>

/** Match the backend's persisted dashboard-slot filename fold. Channel session
 * keys use transport punctuation (`slack:<ts>`) while dashboard slots use the
 * corresponding safe stem (`slack_<ts>`). */
export function dashboardAutomationSlotKey(key: string): string {
  let folded = key
  if (folded.startsWith('dashboard:')) folded = folded.slice('dashboard:'.length)
  while (folded.startsWith('dashboard_')) folded = folded.slice('dashboard_'.length)
  return folded.replace(/[^a-zA-Z0-9_.-]/g, '_')
}

function object(value: unknown): JsonObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : null
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function finite(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function count(value: unknown, fallback = 0): number {
  const n = finite(value, fallback)
  return Number.isInteger(n) && n >= 0 ? n : fallback
}

function positive(value: unknown, fallback: number): number {
  const n = finite(value, fallback)
  return Number.isInteger(n) && n > 0 ? n : fallback
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
}

function isBoundedInteger(
  value: unknown,
  limits: { minimum: number; maximum: number },
): value is number {
  return isPositiveInteger(value) && value >= limits.minimum && value <= limits.maximum
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isTimestamp(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function isEnum(value: unknown, values: readonly string[]): value is string {
  return typeof value === 'string' && values.includes(value)
}

function isNullableEnum(value: unknown, values: readonly string[]): boolean {
  return value === null || (typeof value === 'string' && values.includes(value))
}

function owns(value: JsonObject, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key)
}

function structuredFallback(
  loop: JsonObject,
  monitor: JsonObject | null,
  slotKey: string,
): StructuredMonitor {
  const budgets = object(monitor?.budgets)
  const outcome = text(monitor?.outcome)
  const active = loop.active === true && !outcome
  return {
    kind: 'structured_monitor',
    id: text(loop.id),
    slotKey,
    active,
    actionable: false,
    version: positive(monitor?.version, 1),
    monitorKind: text(monitor?.kind, 'unknown'),
    objective: text(monitor?.objective, 'unknown'),
    target: text(monitor?.target),
    cadenceSecs: positive(monitor?.cadence_secs, STRUCTURED_MONITOR_DEFAULTS.cadenceSecs),
    nextProbeAt: finite(monitor?.next_probe_at),
    wakeInstructions: text(monitor?.wake_instructions),
    budgets: {
      maxRuntimeSecs: positive(budgets?.max_runtime_secs, STRUCTURED_MONITOR_DEFAULTS.maxRuntimeSecs),
      maxAgentTurns: positive(budgets?.max_agent_turns, STRUCTURED_MONITOR_DEFAULTS.maxAgentTurns),
      maxTokens: positive(budgets?.max_tokens, STRUCTURED_MONITOR_DEFAULTS.maxTokens),
      maxProviderErrors: positive(budgets?.max_provider_errors, STRUCTURED_MONITOR_DEFAULTS.maxProviderErrors),
    },
    latest: {
      classification: text(monitor?.last_observation_status),
      reasonCode: text(monitor?.last_observation_reason_code),
      observedAt: finite(monitor?.last_observed_at),
      decision: text(monitor?.last_decision),
    },
    usage: {
      probes: count(monitor?.probe_count),
      wakes: count(monitor?.wake_count),
      agentTurns: count(monitor?.agent_turns),
      inputTokens: count(monitor?.input_tokens),
      outputTokens: count(monitor?.output_tokens),
      providerErrors: count(monitor?.provider_error_count),
      tokenUsageKnown: monitor?.token_usage_known === true,
    },
    action: {
      wakeInFlight: monitor?.wake_in_flight === true,
      wakeDelivery: text(monitor?.wake_delivery),
    },
    terminal: outcome ? {
      outcome,
      reason: text(monitor?.stopped_reason, text(loop.stopped_reason)),
      stoppedAt: finite(monitor?.stopped_at),
    } : null,
  }
}

/**
 * Normalize either a REST loop record or an `autonudge_state` WS envelope.
 * A structured marker is never downgraded to legacy when its payload is
 * malformed or from a newer schema: it remains inspectable and inert.
 */
export function normalizeAutomationRecord(raw: unknown): AutomationRecord | null {
  const envelope = object(raw)
  if (!envelope) return null
  const loop = object(envelope.loop) ?? envelope
  const removed = object(envelope.loop) !== null && envelope.event === 'removed'
  if (removed && owns(loop, 'monitor')) return null
  const id = text(loop.id)
  const slotKey = dashboardAutomationSlotKey(text(loop.slot_key, text(envelope.slot)))
  if (!id || !slotKey) return null

  if (!owns(loop, 'monitor')) {
    return {
      kind: 'legacy_goal_loop',
      id,
      slotKey,
      message: text(loop.message),
      idleSecs: count(loop.idle_secs),
      maxCycles: count(loop.max_cycles),
      cycleCount: count(loop.cycle_count),
      active: !removed && loop.active === true,
      lastFireAt: finite(loop.last_fire_ts),
      nextDueAt: finite(loop.next_due_ts),
      maxRuntimeSecs: count(loop.max_runtime_secs),
      stoppedReason: text(loop.stopped_reason),
    }
  }

  const monitor = object(loop.monitor)
  const budgets = object(monitor?.budgets)
  const version = positive(monitor?.version, 1)
  const requiredCountsValid = [
    monitor?.wake_count,
    monitor?.agent_turns,
    monitor?.input_tokens,
    monitor?.output_tokens,
    monitor?.probe_count,
    monitor?.provider_error_count,
    monitor?.consecutive_provider_errors,
  ].every(isCount)
  const requiredLimitsValid = !!budgets
    && isBoundedInteger(monitor?.cadence_secs, STRUCTURED_MONITOR_LIMITS.cadenceSecs)
    && isBoundedInteger(budgets.max_runtime_secs, STRUCTURED_MONITOR_LIMITS.maxRuntimeSecs)
    && isBoundedInteger(budgets.max_agent_turns, STRUCTURED_MONITOR_LIMITS.maxAgentTurns)
    && isBoundedInteger(budgets.max_tokens, STRUCTURED_MONITOR_LIMITS.maxTokens)
    && isBoundedInteger(
      budgets.max_provider_errors,
      STRUCTURED_MONITOR_LIMITS.maxProviderErrors,
    )
  const requiredTimestampsValid = [
    loop.last_fire_ts,
    loop.next_due_ts,
    monitor?.last_observed_at,
    monitor?.completion_evidence_deadline,
    monitor?.last_completed_at,
    monitor?.last_probe_at,
    monitor?.next_probe_at,
    monitor?.stopped_at,
  ].every(isTimestamp)
  const requiredStringsValid = [
    loop.stopped_reason,
    monitor?.last_fingerprint,
    monitor?.last_wake_fingerprint,
    monitor?.last_completion_fingerprint,
    monitor?.last_observation_reason_code,
    monitor?.stopped_reason,
  ].every(value => typeof value === 'string')
  const enumsValid = isNullableEnum(
    monitor?.wake_delivery,
    monitorContract.enums.wakeDelivery,
  ) && isNullableEnum(
    monitor?.last_completion_disposition,
    monitorContract.enums.lastCompletionDisposition,
  ) && isNullableEnum(
    monitor?.last_decision,
    monitorContract.enums.lastDecision,
  ) && isNullableEnum(
    monitor?.last_provider_error,
    monitorContract.enums.lastProviderError,
  ) && isNullableEnum(
    monitor?.last_observation_status,
    monitorContract.enums.lastObservationStatus,
  ) && isNullableEnum(monitor?.outcome, monitorContract.enums.outcome)
  const outcome = typeof monitor?.outcome === 'string' ? monitor.outcome : null
  const active = !removed && loop.active === true
  const lifecycleValid = typeof loop.active === 'boolean' && active !== (outcome !== null)
  const supported = !!monitor
    && monitor.version === monitorContract.monitorStateVersion
    && isEnum(monitor.kind, monitorContract.pullRequestMonitorKinds)
    && text(monitor.objective) === 'review_ready'
    && !!text(monitor.target)
    && !!budgets
    && requiredLimitsValid
    && requiredCountsValid
    && requiredTimestampsValid
    && requiredStringsValid
    && isPositiveInteger(monitor.config_generation)
    && enumsValid
    && typeof monitor.wake_in_flight === 'boolean'
    && typeof monitor.token_usage_known === 'boolean'
    && typeof monitor.wake_instructions === 'string'
    && monitor.wake_instructions.length
      <= STRUCTURED_MONITOR_LIMITS.wakeInstructions.maximumLength
    && lifecycleValid
  if (!supported) return structuredFallback(loop, monitor, slotKey)

  return {
    kind: 'structured_monitor',
    id,
    slotKey,
    active,
    actionable: monitor.stopped_reason !== 'invalid_monitor_record'
      && outcome !== 'session_close',
    version,
    monitorKind: text(monitor.kind),
    objective: text(monitor.objective),
    target: text(monitor.target),
    cadenceSecs: positive(monitor.cadence_secs, STRUCTURED_MONITOR_DEFAULTS.cadenceSecs),
    nextProbeAt: finite(monitor.next_probe_at) || finite(loop.next_due_ts),
    wakeInstructions: text(monitor.wake_instructions),
    budgets: {
      maxRuntimeSecs: positive(budgets.max_runtime_secs, STRUCTURED_MONITOR_DEFAULTS.maxRuntimeSecs),
      maxAgentTurns: positive(budgets.max_agent_turns, STRUCTURED_MONITOR_DEFAULTS.maxAgentTurns),
      maxTokens: positive(budgets.max_tokens, STRUCTURED_MONITOR_DEFAULTS.maxTokens),
      maxProviderErrors: positive(budgets.max_provider_errors, STRUCTURED_MONITOR_DEFAULTS.maxProviderErrors),
    },
    latest: {
      classification: text(monitor.last_observation_status),
      reasonCode: text(monitor.last_observation_reason_code),
      observedAt: finite(monitor.last_observed_at),
      decision: text(monitor.last_decision),
    },
    usage: {
      probes: count(monitor.probe_count),
      wakes: count(monitor.wake_count),
      agentTurns: count(monitor.agent_turns),
      inputTokens: count(monitor.input_tokens),
      outputTokens: count(monitor.output_tokens),
      providerErrors: count(monitor.provider_error_count),
      tokenUsageKnown: monitor.token_usage_known === true,
    },
    action: {
      wakeInFlight: monitor.wake_in_flight === true,
      wakeDelivery: text(monitor.wake_delivery),
    },
    terminal: outcome ? {
      outcome,
      reason: text(monitor.stopped_reason, text(loop.stopped_reason)),
      stoppedAt: finite(monitor.stopped_at),
    } : null,
  }
}

export function deriveAutomationStatus(record: AutomationRecord): MonitorStatus {
  if (record.kind === 'legacy_goal_loop') return record.active ? 'active' : 'user_stopped'
  if (!record.actionable) return 'blocked'
  if (record.terminal) {
    if (record.terminal.outcome === 'success') return 'success'
    if (record.terminal.outcome === 'budget') return 'budget_stopped'
    if (record.terminal.outcome === 'user_stop') return 'user_stopped'
    return 'blocked'
  }
  if (!record.active) return 'blocked'
  if (record.action.wakeInFlight && record.action.wakeDelivery === 'dispatched') {
    return 'action_running'
  }
  if (record.latest.decision === 'retry_provider'
    || (record.action.wakeInFlight && record.action.wakeDelivery === 'busy')) {
    return 'backing_off'
  }
  return record.usage.probes === 0 ? 'arm_pending' : 'active'
}

/** Read a slot-indexed collection without walking Object.prototype. */
export function automationForSlot(
  automations: Record<string, AutomationRecord> | undefined,
  slotKey: string,
): AutomationRecord | null {
  if (!automations || !Object.prototype.hasOwnProperty.call(automations, slotKey)) return null
  return automations[slotKey] ?? null
}
