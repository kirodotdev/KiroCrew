import { describe, expect, it } from 'vitest'
import {
  deriveAutomationStatus,
  normalizeAutomationRecord,
  normalizePullRequestMonitorTarget,
  type StructuredMonitor,
} from '../monitoring/automation'
import { canonicalGitHubObservation, structuredMonitorLoop as structuredLoop } from './monitorFixtures'

describe('automation transport normalizer', () => {
  it.each([
    ['https://github.com/acme/widgets/pull/1', 'github_pull_request'],
    ['https://gitlab.com/acme/widgets/-/merge_requests/2', 'gitlab_merge_request'],
    ['https://git.example/acme/widgets/-/merge_requests/2', 'gitlab_merge_request'],
    ['https://git.example:8443/acme/widgets/-/merge_requests/2', 'gitlab_merge_request'],
    ['https://dev.azure.com/acme/project/_git/widgets/pullrequest/3', 'azure_devops_pull_request'],
    ['https://bitbucket.org/acme/widgets/pull-requests/4', 'bitbucket_pull_request'],
  ])('infers the structured provider kind for %s', (target, kind) => {
    expect(normalizePullRequestMonitorTarget(target)?.kind).toBe(kind)
  })

  it.each([
    ['https://github.com/acme/widgets/pull/1/files', 'github_pull_request'],
    ['https://gitlab.com/acme/widgets/-/merge_requests/2/diffs', 'gitlab_merge_request'],
    ['https://bitbucket.org/acme/widgets/pull-requests/4/diff', 'bitbucket_pull_request'],
  ])('recognizes a common provider subtab URL for %s', (target, kind) => {
    expect(normalizePullRequestMonitorTarget(target)?.kind).toBe(kind)
  })

  it.each([
    'https://github.com:8443/acme/widgets/pull/1',
    'https://dev.azure.com:8443/acme/project/_git/widgets/pullrequest/3',
    'https://bitbucket.org:8443/acme/widgets/pull-requests/4',
  ])('rejects a custom port outside the self-managed GitLab provider: %s', target => {
    expect(normalizePullRequestMonitorTarget(target)).toBeNull()
  })

  it('keeps legacy goal loops distinct and preserves zero as unlimited', () => {
    const record = normalizeAutomationRecord({
      id: 'legacy-1', slot_key: 'chat-1', message: 'Keep going', idle_secs: 60,
      max_cycles: 0, cycle_count: 7, active: true, last_fire_ts: 123,
    })

    expect(record).toEqual({
      kind: 'legacy_goal_loop', id: 'legacy-1', slotKey: 'chat-1',
      message: 'Keep going', idleSecs: 60, maxCycles: 0, cycleCount: 7,
      active: true, lastFireAt: 123, nextDueAt: 0, maxRuntimeSecs: 0,
      stoppedReason: '',
    })
  })

  it('folds channel session keys into dashboard slot keys', () => {
    const record = normalizeAutomationRecord({
      id: 'legacy-1', slot_key: 'slack:1785370133.085469', message: 'Keep going',
      idle_secs: 60, max_cycles: 24, cycle_count: 7, active: true, last_fire_ts: 123,
    })

    expect(record?.slotKey).toBe('slack_1785370133.085469')
  })

  it('normalizes the authoritative structured budgets and usage once for REST and WS', () => {
    const fromRest = normalizeAutomationRecord(structuredLoop())
    const fromWs = normalizeAutomationRecord({ event: 'updated', slot: 'chat-1', loop: structuredLoop() })

    expect(fromRest).toEqual(fromWs)
    expect(fromRest?.kind).toBe('structured_monitor')
    const monitor = fromRest as StructuredMonitor
    expect(monitor.budgets).toEqual({
      maxRuntimeSecs: 14_400,
      maxAgentTurns: 8,
      maxTokens: 250_000,
      maxProviderErrors: 3,
    })
    expect(monitor.usage).toEqual({
      probes: 5,
      wakes: 2,
      agentTurns: 2,
      inputTokens: 1200,
      outputTokens: 300,
      providerErrors: 1,
      tokenUsageKnown: true,
    })
    expect(monitor.latest).toEqual({
      classification: 'pending',
      reasonCode: 'checks_pending',
      observedAt: 1_800_000_000,
      decision: 'no_change',
    })
    expect(structuredLoop().monitor.last_observation).toEqual(canonicalGitHubObservation)
  })

  it('fails closed when a structured marker is malformed or from a future version', () => {
    const malformed = normalizeAutomationRecord({ ...structuredLoop(), monitor: { version: 1 } })
    const future = normalizeAutomationRecord(structuredLoop({ version: 2 }))
    const invalidVersion = normalizeAutomationRecord(structuredLoop({ version: 0 }))

    expect(malformed).toMatchObject({ kind: 'structured_monitor', actionable: false })
    expect(future).toMatchObject({ kind: 'structured_monitor', actionable: false, version: 2 })
    expect(invalidVersion).toMatchObject({ kind: 'structured_monitor', actionable: false })
    expect(deriveAutomationStatus(malformed!)).toBe('blocked')
    expect(deriveAutomationStatus(future!)).toBe('blocked')
    expect(deriveAutomationStatus(invalidVersion!)).toBe('blocked')
  })

  it('ignores unrendered canonical evidence from another provider kind', () => {
    const record = normalizeAutomationRecord(structuredLoop({
      last_observation: {
        ...canonicalGitHubObservation,
        kind: 'gitlab_merge_request',
      },
    }))

    expect(record).toMatchObject({ kind: 'structured_monitor', actionable: true })
  })

  it('ignores unrendered canonical check-name evidence beyond backend bounds', () => {
    const record = normalizeAutomationRecord(structuredLoop({
      last_observation: {
        ...canonicalGitHubObservation,
        checks: {
          ...canonicalGitHubObservation.checks,
          passed: ['x'.repeat(201)],
        },
      },
    }))

    expect(record).toMatchObject({ kind: 'structured_monitor', actionable: true })
  })

  it('fails closed instead of repairing non-positive structured limits', () => {
    const record = normalizeAutomationRecord(structuredLoop({
      budgets: {
        max_runtime_secs: 14_400,
        max_agent_turns: 8,
        max_tokens: 0,
        max_provider_errors: 3,
      },
    }))

    expect(record).toMatchObject({ kind: 'structured_monitor', actionable: false })
    expect(deriveAutomationStatus(record!)).toBe('blocked')
  })

  it('keeps quarantined current-schema monitor records non-actionable', () => {
    const record = normalizeAutomationRecord(structuredLoop({
      active: false,
      outcome: 'blocked',
      stopped_reason: 'invalid_monitor_record',
    }))

    expect(record).toMatchObject({
      kind: 'structured_monitor',
      active: false,
      actionable: false,
      terminal: {
        outcome: 'blocked',
        reason: 'invalid_monitor_record',
      },
    })
    expect(deriveAutomationStatus(record!)).toBe('blocked')
  })

  it('keeps session-close tombstones non-actionable', () => {
    const record = normalizeAutomationRecord(structuredLoop({
      active: false,
      outcome: 'session_close',
      stopped_reason: 'session_closed',
    }))

    expect(record).toMatchObject({
      kind: 'structured_monitor',
      active: false,
      actionable: false,
      terminal: {
        outcome: 'session_close',
        reason: 'session_closed',
      },
    })
  })

  it.each([
    { cadence_secs: 86_401 },
    { budgets: { max_runtime_secs: 604_801, max_agent_turns: 8, max_tokens: 250_000, max_provider_errors: 3 } },
    { budgets: { max_runtime_secs: 14_400, max_agent_turns: 9, max_tokens: 250_000, max_provider_errors: 3 } },
    { budgets: { max_runtime_secs: 14_400, max_agent_turns: 8, max_tokens: 1_000_001, max_provider_errors: 3 } },
    { budgets: { max_runtime_secs: 14_400, max_agent_turns: 8, max_tokens: 250_000, max_provider_errors: 21 } },
    { wake_instructions: 'x'.repeat(1001) },
  ])('fails closed when a structured record exceeds a backend bound', patch => {
    const record = normalizeAutomationRecord(structuredLoop(patch))

    expect(record).toMatchObject({ kind: 'structured_monitor', actionable: false })
  })

  it.each([
    ['token usage boolean', { token_usage_known: 'yes' }],
    ['wake-in-flight boolean', { wake_in_flight: 1 }],
    ['wake delivery enum', { wake_delivery: 'later' }],
    ['completion disposition enum', { last_completion_disposition: 'maybe' }],
    ['decision enum', { last_decision: 'wait' }],
    ['provider error enum', { last_provider_error: 'dns' }],
    ['outcome enum', { active: false, outcome: 'celebrated' }],
    ['config generation', { config_generation: 0 }],
    ['last fingerprint', { last_fingerprint: 42 }],
    ['last wake fingerprint', { last_wake_fingerprint: false }],
    ['completion fingerprint', { last_completion_fingerprint: {} }],
    ['terminal reason', { stopped_reason: null }],
    ['next-probe timestamp', { next_probe_at: Number.POSITIVE_INFINITY }],
    ['last-observed timestamp', { last_observed_at: -1 }],
    ['observation status', { last_observation_status: 'maybe' }],
    ['observation reason', { last_observation_reason_code: null }],
  ])('fails closed for a malformed current-schema %s', (_name, patch) => {
    const record = normalizeAutomationRecord(structuredLoop(patch))
    const expectedActive = !('active' in patch) || patch.active !== false

    expect(record).toMatchObject({
      kind: 'structured_monitor',
      actionable: false,
      active: expectedActive,
    })
    if (expectedActive) expect(record).toMatchObject({ terminal: null })
  })

  it.each([
    ['blocking review', { ...canonicalGitHubObservation, blocking_review: 'blocked' }],
    ['nullable blocking review', { ...canonicalGitHubObservation, blocking_review: null }],
    ['extra root key', {
      ...canonicalGitHubObservation, raw_provider_payload: { secret: 'must-not-render' },
    }],
    ['extra checks bucket', {
      ...canonicalGitHubObservation,
      checks: {
        ...canonicalGitHubObservation.checks,
        provider_diagnostics: ['must-not-render'],
      },
    }],
    ['check buckets', {
      ...canonicalGitHubObservation,
      checks: { ...canonicalGitHubObservation.checks, failed: [{ name: 'raw object' }] },
    }],
    ['draft', { ...canonicalGitHubObservation, draft: 'false' }],
    ['head revision', { ...canonicalGitHubObservation, head_revision: null }],
    ['kind', { ...canonicalGitHubObservation, kind: 'pull_request' }],
    ['mergeability', { ...canonicalGitHubObservation, mergeability: 'clean' }],
    ['review decision', { ...canonicalGitHubObservation, review_decision: 'waiting' }],
    ['review threads complete', {
      ...canonicalGitHubObservation, review_threads_complete: 1,
    }],
    ['state', { ...canonicalGitHubObservation, state: 'running' }],
    ['target', { ...canonicalGitHubObservation, target: false }],
    ['unresolved count', {
      ...canonicalGitHubObservation, unresolved_review_threads: -1,
    }],
  ])('ignores unrendered canonical GitHub %s facts', (_name, lastObservation) => {
    const record = normalizeAutomationRecord(structuredLoop({
      last_observation: lastObservation,
    }))

    expect(record).toMatchObject({
      kind: 'structured_monitor', actionable: true, active: true,
    })
    expect(JSON.stringify(record)).not.toContain('must-not-render')
  })

  it.each([
    'gitlab_merge_request',
    'azure_devops_pull_request',
    'bitbucket_pull_request',
  ])('accepts the exact shared observation for %s', monitorKind => {
    const record = normalizeAutomationRecord(structuredLoop({
      kind: monitorKind,
      last_observation: { ...canonicalGitHubObservation, kind: monitorKind },
    }))

    expect(record).toMatchObject({
      kind: 'structured_monitor', actionable: true, monitorKind,
    })
  })

  it.each([
    ['inactive without an outcome', { active: false, outcome: null }],
    ['active with a terminal outcome', { active: true, outcome: 'success' }],
  ])('fails closed when a structured record is %s', (_name, patch) => {
    const { active, ...monitorPatch } = patch
    const record = normalizeAutomationRecord({
      ...structuredLoop(monitorPatch),
      active,
    })

    expect(record).toMatchObject({ kind: 'structured_monitor', actionable: false })
    expect(deriveAutomationStatus(record!)).toBe('blocked')
  })

  it('does not turn a removed structured record into a phantom blocked monitor', () => {
    const record = normalizeAutomationRecord({
      event: 'removed', slot: 'chat-1', loop: structuredLoop({ probe_count: 1 }),
    })
    expect(record).toBeNull()
  })

  it.each([
    [{ probe_count: 0 }, 'arm_pending'],
    [{ probe_count: 1 }, 'active'],
    [{ probe_count: 1, last_decision: 'retry_provider' }, 'backing_off'],
    [{ probe_count: 1, wake_in_flight: true, wake_delivery: 'dispatched' }, 'action_running'],
    [{ active: false, outcome: 'success', stopped_reason: 'success' }, 'success'],
    [{ active: false, outcome: 'blocked', stopped_reason: 'approval_stall' }, 'blocked'],
    [{ active: false, outcome: 'budget', stopped_reason: 'token_budget' }, 'budget_stopped'],
    [{ active: false, outcome: 'user_stop', stopped_reason: 'user_stop' }, 'user_stopped'],
  ] as const)('reports monitor state %s as %s', (patch, expected) => {
    const record = normalizeAutomationRecord(structuredLoop(patch))
    expect(deriveAutomationStatus(record!)).toBe(expected)
  })

  it('does not retain a stale busy delivery after the wake is no longer in flight', () => {
    const record = normalizeAutomationRecord(structuredLoop({
      probe_count: 1,
      wake_in_flight: false,
      wake_delivery: 'busy',
      last_decision: 'no_change',
    }))

    expect(deriveAutomationStatus(record!)).toBe('active')
  })
})
