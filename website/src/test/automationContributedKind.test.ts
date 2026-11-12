import { afterEach, describe, expect, it } from 'vitest'
import {
  deriveAutomationStatus,
  isKnownMonitorKind,
  normalizeAutomationRecord,
  resetRuntimeMonitorContract,
  setRuntimeMonitorContract,
  type StructuredMonitor,
} from '../monitoring/automation'
import { isPullRequestWatchKind } from '../components/autoNudgeLoop'
import { structuredMonitorLoop as structuredLoop } from './monitorFixtures'

// A kind contributed at boot via `register_monitor_kind()`. It is a
// valid, probing monitor on the backend, but it is absent from the checked-in
// `contract.json` that the dashboard bundle ships.
const CONTRIBUTED_KIND = 'salesforce_pull_request'

describe('contributed monitor kind (runtime contract)', () => {
  afterEach(() => {
    resetRuntimeMonitorContract()
  })

  it('without the runtime contract a contributed kind reads as blocked and non-actionable', () => {
    // This is the user-visible bug on origin/main: the bundled contract has no
    // entry for the contributed kind, so the record fails the `supported` gate,
    // falls back to a non-actionable record, and the popover shows "blocked"
    // with its save and restart controls disabled -- even while it is probing.
    const record = normalizeAutomationRecord(
      structuredLoop({ kind: CONTRIBUTED_KIND }),
    ) as StructuredMonitor
    expect(record.kind).toBe('structured_monitor')
    expect(record.actionable).toBe(false)
    expect(deriveAutomationStatus(record)).toBe('blocked')
  })

  it('with the runtime contract the same contributed kind reads as its real state and is actionable', () => {
    setRuntimeMonitorContract({
      pullRequestMonitorKinds: [CONTRIBUTED_KIND],
    })
    const record = normalizeAutomationRecord(
      structuredLoop({ kind: CONTRIBUTED_KIND }),
    ) as StructuredMonitor
    expect(record.kind).toBe('structured_monitor')
    expect(record.actionable).toBe(true)
    // The fixture is a probing monitor with prior probes -> active, not blocked.
    expect(deriveAutomationStatus(record)).toBe('active')
    // The pull-request watch set (used to decide which watches get the PR
    // readout) also recognizes the contributed kind at runtime.
    expect(isPullRequestWatchKind(CONTRIBUTED_KIND)).toBe(true)
  })

  it('built-in kinds are unchanged with no contributed kinds registered', () => {
    const record = normalizeAutomationRecord(structuredLoop()) as StructuredMonitor
    expect(record.kind).toBe('structured_monitor')
    expect(record.actionable).toBe(true)
    expect(deriveAutomationStatus(record)).toBe('active')
    expect(isPullRequestWatchKind('github_pull_request')).toBe(true)
    expect(isPullRequestWatchKind('gh-pr')).toBe(true)
  })

  it('a runtime contract missing the kind list leaves the bundled kinds in force (older gateway / fallback)', () => {
    // An endpoint error or an older gateway never calls the setter; a partial
    // payload must not erase the bundled vocabulary either.
    setRuntimeMonitorContract({})
    const builtIn = normalizeAutomationRecord(structuredLoop()) as StructuredMonitor
    expect(builtIn.actionable).toBe(true)
    const contributed = normalizeAutomationRecord(
      structuredLoop({ kind: CONTRIBUTED_KIND }),
    ) as StructuredMonitor
    expect(contributed.actionable).toBe(false)
  })

  it('a runtime list that omits a bundled kind keeps that kind known (add-only)', () => {
    // A gateway payload can only ADD kinds; it can never shrink the built-in
    // vocabulary, so a built-in absent from the payload still reads as known.
    const changed = setRuntimeMonitorContract({ pullRequestMonitorKinds: [CONTRIBUTED_KIND] })
    expect(changed).toBe(true)
    expect(isKnownMonitorKind(CONTRIBUTED_KIND)).toBe(true)
    // github_pull_request is a bundled kind the payload did not name.
    expect(isKnownMonitorKind('github_pull_request')).toBe(true)
    const builtIn = normalizeAutomationRecord(structuredLoop()) as StructuredMonitor
    expect(builtIn.actionable).toBe(true)
  })

  it('reports no change when the payload adds nothing beyond the bundled kinds', () => {
    // Installing only built-ins is a no-op the caller can skip repainting on.
    expect(setRuntimeMonitorContract({ pullRequestMonitorKinds: ['github_pull_request'] })).toBe(
      false,
    )
  })
})
