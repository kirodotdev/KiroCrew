import { describe, expect, it } from 'vitest'
import { conductorModeRunState, type ConductorModeRun } from './conductorModeRun'

const run = (settled: ConductorModeRun['settled'], open = 0): ConductorModeRun => ({ settled, open, accepted: 2, not_done: 0, at_limit: false })
const base = { paused: false, needsYou: false, working: false, run: run(''), hasGoal: true, items: 2 }

describe('conductorModeRunState', () => {
  it('draws nothing for a chat with no goal and no milestone', () => {
    expect(conductorModeRunState({ ...base, hasGoal: false, items: 0, paused: true })).toBeNull()
    expect(conductorModeRunState({ ...base, items: 0 })).toBeNull()
  })

  it('puts Paused, then what needs the person, then live work first', () => {
    const all = { ...base, paused: true, needsYou: true, working: true, run: run('done') }
    expect(conductorModeRunState(all)).toBe('paused')
    expect(conductorModeRunState({ ...all, paused: false })).toBe('waiting')
    expect(conductorModeRunState({ ...all, paused: false, needsYou: false })).toBe('working')
  })

  it('reads the gateway verdict once nothing live outranks it', () => {
    expect(conductorModeRunState({ ...base, run: run('done') })).toBe('done')
    expect(conductorModeRunState({ ...base, run: run('stopped', 1) })).toBe('stopped')
  })

  it('reads an unsettled board with milestones as working, and a goal alone as nothing', () => {
    expect(conductorModeRunState(base)).toBe('working')
    expect(conductorModeRunState({ ...base, run: undefined })).toBe('working')
    expect(conductorModeRunState({ ...base, items: 0, working: true })).toBe('working')
  })
})
