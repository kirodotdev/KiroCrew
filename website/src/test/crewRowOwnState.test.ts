import { describe, expect, it } from 'vitest'
import { crewRowOwnState } from '../pages/chat-sidebar/SessionRow'

describe('crewRowOwnState — the Ultra Mode row\'s one word', () => {
  it('a passive patrol reads Waiting while nothing in the crew runs', () => {
    expect(crewRowOwnState('structured_monitor_passive', '')?.word).toBe('Waiting')
  })

  it('a passive patrol reads Running while one of its workers is mid-turn', () => {
    expect(crewRowOwnState('structured_monitor_passive', '', false, true)?.word).toBe('Running')
  })

  it('open work moving on the next check reads Waiting, gone quiet reads Stalled', () => {
    expect(crewRowOwnState('crew_waiting', '')?.word).toBe('Waiting')
    expect(crewRowOwnState('crew_stalled', '')?.word).toBe('Stalled')
  })

  it('an ask on the person still reads Needs you', () => {
    expect(crewRowOwnState('loop_waiting', '', false, true)?.word).toBe('Needs you')
  })
})
