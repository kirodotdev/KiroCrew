import { describe, expect, it } from 'vitest'
import { BEADS_MAX, beadsDone, leadOf, planOf } from './beads'
import { FIXTURE_VOICE_REPLY } from './fixture'

describe('a Bot\'s plan, as beads', () => {
  it('reads the steps from the first numbered list of what the Bot wrote', () => {
    expect(planOf('Tests on 812, then the cause.\n1. Run the tests\n2) Read the failure\n3. Report\n\nMore text.\n1. Not this list')).toEqual(['Run the tests', 'Read the failure', 'Report'])
    expect(planOf(FIXTURE_VOICE_REPLY)).toHaveLength(3)
  })

  it('makes no plan up: no list, a list of one, or a list that does not count from one has no beads', () => {
    expect(planOf('Passed. The failure was a stale cache.')).toEqual([])
    expect(planOf('1. Only one step')).toEqual([])
    expect(planOf('3. Third\n4. Fourth')).toEqual([])
    expect(planOf('')).toEqual([])
    expect(planOf('In 2024. 1.5 times faster')).toEqual([])
  })

  it('holds up at most a handful of beads, in order', () => {
    const many = Array.from({ length: 9 }, (_, i) => `${i + 1}. Step ${i + 1}`).join('\n')
    expect(planOf(many)).toEqual(['Step 1', 'Step 2', 'Step 3', 'Step 4', 'Step 5'])
    expect(planOf(many)).toHaveLength(BEADS_MAX)
  })

  it('says the one line that comes before the plan', () => {
    expect(leadOf(FIXTURE_VOICE_REPLY)).toBe('On it: three steps, about two minutes.')
    expect(leadOf('Just a line.')).toBe('Just a line.')
  })

  it('fills the scripted beads of the sample data one by one, and never past the last', () => {
    expect([0, 999, 1000, 2500, 9000].map(ms => beadsDone(3, ms, 1000))).toEqual([0, 0, 1, 2, 3])
  })
})
