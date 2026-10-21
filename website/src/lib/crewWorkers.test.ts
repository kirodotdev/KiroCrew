import { describe, expect, it } from 'vitest'
import { crewWorkerGroup, crewWorkerGroupOfRow, groupCrewWorkers, summarizeCrewWorkers } from './crewWorkers'
import type { CrewWorker } from '../types'

describe('crewWorkers', () => {
  it('a live row alone never says done: replying is not finishing', () => {
    expect(crewWorkerGroupOfRow({ pending_approval: true, running: true })).toBe('needs_you')
    expect(crewWorkerGroupOfRow({ needs_input: true })).toBe('needs_you')
    expect(crewWorkerGroupOfRow({ running: true })).toBe('working')
    expect(crewWorkerGroupOfRow({ messages: 3 })).toBe('waiting')
    expect(crewWorkerGroupOfRow({ messages: 0 })).toBe('waiting')
  })

  it('takes the group the API row carries', () => {
    const w = (over: Partial<CrewWorker>) => ({ key: 'k', title: '', agent: '', status: 'idle', group: 'waiting', last_activity: '', ledger_item: '', created_by: '', ...over }) as CrewWorker
    expect(crewWorkerGroup(w({ group: 'done', status: 'idle' }))).toBe('done')
    expect(groupCrewWorkers([w({ key: 'a', group: 'done' }), w({ key: 'b', group: 'needs_you' })]).map(g => g.group)).toEqual(['needs_you', 'done'])
  })

  // The server's table (test_crew_mode_workers.py: status, messages -> group), restated
  // as live-row flags, so the client mirror cannot drift from it.
  it.each([
    [{ pending_approval: true, messages: 4 }, 'needs_you'],
    [{ running: true, messages: 0 }, 'working'],
    // The server says done only from a closed tab or the ledger; a row has neither.
    [{ messages: 2 }, 'waiting'],
    [{ messages: 0 }, 'waiting'],
  ] as const)('a live row %o groups as %s', (row, group) => {
    expect(crewWorkerGroupOfRow(row)).toBe(group)
  })

  it('summarizes the API groups, needs-you first', () => {
    const w = (group: CrewWorker['group']) => ({ key: group, title: '', agent: '', status: 'idle', group, last_activity: '', ledger_item: '', created_by: '' }) as CrewWorker
    expect(summarizeCrewWorkers([w('done'), w('needs_you'), w('waiting')])).toEqual({ groups: ['needs_you', 'waiting', 'done'], needsYou: 1, done: 1, total: 3 })
  })
})
