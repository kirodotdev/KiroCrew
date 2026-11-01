import { describe, expect, it } from 'vitest'
import { crewLaneSets, laneExtrasFor } from '../pages/chat-sidebar/board'
import { inferLane } from '../pages/chat/sessionLane'

const NOW = 1_800_000_000_000
const signals = (sets: ReturnType<typeof crewLaneSets>) => ({
  subagentStartedCounts: {}, subagentApprovalCounts: {}, workflowActiveSet: new Set<string>(), automationRunningSet: new Set<string>(),
  crewBusySet: sets.busy, crewOpenSet: sets.open, crewStalledSet: sets.stalled, crewPersonSet: sets.person, crewApprovalSet: sets.approval,
})

describe('crewLaneSets — the board lane of a conductor', () => {
  it('a conductor with an [OPTIONS:] line and a worker mid-turn files under Working', () => {
    const slots = [
      { key: 'k', running: false, has_options: true },
      { key: 'w', running: true, created_by: 'k' },
    ]
    const sets = crewLaneSets(slots, NOW)
    expect(inferLane(slots[0], laneExtrasFor('k', signals(sets)))).toBe('working')
  })

  it('a parked worker\'s open item files its conductor under Idle, not Waiting on you', () => {
    const slots = [
      { key: 'k', running: false, has_options: true,
        crew_work: { open: 1, person: 0, items: [{ worker: 'w', stale_at_ms: NOW + 60_000 }] } },
      { key: 'w', running: false, created_by: 'k' },
    ]
    expect(inferLane(slots[0], laneExtrasFor('k', signals(crewLaneSets(slots, NOW))))).toBe('idle')
  })

  it('an item on a person files the conductor under Waiting on you', () => {
    const slots = [{ key: 'k', running: false, crew_work: { open: 1, person: 1, items: [] } }]
    expect(inferLane(slots[0], laneExtrasFor('k', signals(crewLaneSets(slots, NOW))))).toBe('waiting')
  })

  it('a crew worker waiting on a tool approval files its busy conductor under Needs approval', () => {
    const slots = [
      { key: 'k', running: false, has_options: true },
      { key: 'w', running: true, pending_approval: true, created_by: 'k' },
    ]
    expect(inferLane(slots[0], laneExtrasFor('k', signals(crewLaneSets(slots, NOW))))).toBe('needs_approval')
  })

  it('a conductor whose ledger work went quiet files under Waiting on you, like its Stalled row', () => {
    const slots = [
      { key: 'k', running: false, has_options: true,
        crew_work: { open: 1, person: 0, items: [{ worker: 'w', stale_at_ms: NOW - 1 }] } },
      { key: 'w', running: false, created_by: 'k' },
    ]
    expect(inferLane(slots[0], laneExtrasFor('k', signals(crewLaneSets(slots, NOW))))).toBe('waiting')
  })

  it('with no crew at all, an [OPTIONS:] line still files under Waiting on you', () => {
    const slots = [{ key: 'k', running: false, has_options: true }]
    expect(inferLane(slots[0], laneExtrasFor('k', signals(crewLaneSets(slots, NOW))))).toBe('waiting')
  })
})
