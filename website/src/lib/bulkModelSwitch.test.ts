/**
 * #11103: every session the Switch All count leaves out must land in a named
 * bucket the panel renders. The partition is exhaustive and disjoint over the
 * whole input space, so a future exclusion added without its own bucket makes
 * `affected + onTarget + skippedRunning` fall short of the slot count here.
 */
import { describe, it, expect } from 'vitest'
import { partitionBulkSwitch, type BulkSwitchSlot } from './bulkModelSwitch'

const PICK = 'opus-4.8'

describe('partitionBulkSwitch', () => {
  // Every combination of the two inputs a predicate reads, under both skip modes.
  const models: (string | null | undefined)[] = [PICK, 'sonnet-4.7', '', null, undefined]
  const running = [false, true]
  const all: BulkSwitchSlot[] = []
  let i = 0
  for (const model of models) for (const r of running) all.push({ key: `k${i++}`, model, running: r })

  for (const skip of [true, false]) {
    it(`accounts for every slot exactly once (skipRunning=${skip})`, () => {
      const p = partitionBulkSwitch(all, PICK, skip)
      expect(p.affected + p.onTarget + p.skippedRunning).toBe(all.length)
      for (const s of all) {
        const one = partitionBulkSwitch([s], PICK, skip)
        expect(one.affected + one.onTarget + one.skippedRunning).toBe(1)
      }
    })
  }

  it('never counts a slot in both onTarget and runningOffTarget', () => {
    // runningOffTarget is what the skip checkbox renders beside the on-target
    // line, so an overlap would count one session twice on screen.
    for (const s of all) {
      const one = partitionBulkSwitch([s], PICK, true)
      expect(one.onTarget + one.runningOffTarget).toBeLessThanOrEqual(1)
    }
    const p = partitionBulkSwitch(all, PICK, true)
    expect(p.runningOffTarget).toBe(p.skippedRunning)
  })

  it('puts a running slot already on the pick in onTarget, as the server does', () => {
    expect(partitionBulkSwitch([{ key: 'a', model: PICK, running: true }], PICK, true))
      .toEqual({ affected: 0, onTarget: 1, onTargetKeys: ['a'], skippedRunning: 0, runningOffTarget: 0, runningModelChange: 0 })
  })

  it('counts a running off-target slot as skipped only while skipping', () => {
    const s = [{ key: 'a', model: 'sonnet-4.7', running: true }]
    expect(partitionBulkSwitch(s, PICK, true)).toEqual({ affected: 0, onTarget: 0, onTargetKeys: [], skippedRunning: 1, runningOffTarget: 1, runningModelChange: 1 })
    expect(partitionBulkSwitch(s, PICK, false)).toEqual({ affected: 1, onTarget: 0, onTargetKeys: [], skippedRunning: 0, runningOffTarget: 1, runningModelChange: 1 })
  })

  it('treats an unset model as on-target only for an unset pick', () => {
    expect(partitionBulkSwitch([{ key: 'a' }], PICK, true).affected).toBe(1)
    expect(partitionBulkSwitch([{ key: 'a' }], '', true).onTarget).toBe(1)
  })

  it('counts a session on the pick but not on the picked effort as affected', () => {
    const s = [
      { key: 'low', model: PICK, reasoning_effort: 'low' },
      { key: 'high', model: PICK, reasoning_effort: 'high' },
      { key: 'unset', model: PICK },
    ]
    const p = partitionBulkSwitch(s, PICK, true, 'high')
    expect(p.affected).toBe(2)
    expect(p.onTargetKeys).toEqual(['high'])
    // No effort picked: every session already on the model is on target.
    expect(partitionBulkSwitch(s, PICK, true).onTarget).toBe(3)
    // A relay archive keeps its effort, as the backend leaves it alone.
    expect(partitionBulkSwitch([{ key: 'r', model: PICK, reasoning_effort: 'low', executor: 'remote' }], PICK, true, 'high').onTarget).toBe(1)
  })

  it('never counts an effort-only running session as one whose reply stops', () => {
    const s = [{ key: 'a', model: PICK, reasoning_effort: 'low', running: true }]
    expect(partitionBulkSwitch(s, PICK, false, 'high'))
      .toEqual({ affected: 1, onTarget: 0, onTargetKeys: [], skippedRunning: 0, runningOffTarget: 1, runningModelChange: 0 })
  })

  for (const skip of [true, false]) {
    it(`accounts for every slot exactly once with an effort picked (skipRunning=${skip})`, () => {
      const efforts: (string | undefined)[] = ['high', 'low', '', undefined]
      const withEffort: BulkSwitchSlot[] = []
      let j = 0
      for (const s of all) for (const e of efforts) withEffort.push({ ...s, key: `e${j++}`, reasoning_effort: e })
      for (const pick of ['high', 'low']) {
        const p = partitionBulkSwitch(withEffort, PICK, skip, pick)
        expect(p.affected + p.onTarget + p.skippedRunning).toBe(withEffort.length)
        expect(p.onTargetKeys).toHaveLength(p.onTarget)
      }
    })
  }
})
