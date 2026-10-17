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
      .toEqual({ affected: 0, onTarget: 1, skippedRunning: 0, runningOffTarget: 0 })
  })

  it('counts a running off-target slot as skipped only while skipping', () => {
    const s = [{ key: 'a', model: 'sonnet-4.7', running: true }]
    expect(partitionBulkSwitch(s, PICK, true)).toEqual({ affected: 0, onTarget: 0, skippedRunning: 1, runningOffTarget: 1 })
    expect(partitionBulkSwitch(s, PICK, false)).toEqual({ affected: 1, onTarget: 0, skippedRunning: 0, runningOffTarget: 1 })
  })

  it('treats an unset model as on-target only for an unset pick', () => {
    expect(partitionBulkSwitch([{ key: 'a' }], PICK, true).affected).toBe(1)
    expect(partitionBulkSwitch([{ key: 'a' }], '', true).onTarget).toBe(1)
  })
})
