import { describe, it, expect, afterEach, vi } from 'vitest'
import { START_DRAFT_SAVE_WAIT_MS, awaitDraftSaveForStart, saveDraftBeforeStart } from '../utils/goalDraftStartGate'
import { GOAL_DRAFT_SYNC_TIMEOUT_MS, type GoalDraftPersistResult } from '../utils/goalDrafts'

describe('goalDraftStartGate', () => {
  afterEach(() => { vi.useRealTimers() })

  it('maps each save result to what Start does', async () => {
    const cases: Array<[GoalDraftPersistResult, string]> = [
      ['persisted', 'start'], ['retained', 'start'],
      ['conflict', 'conflict'], ['superseded', 'conflict'], ['remote-failed', 'failed'],
    ]
    for (const [result, outcome] of cases) {
      expect(await awaitDraftSaveForStart(Promise.resolve(result))).toBe(outcome)
    }
  })

  it('covers an in-flight save and the one Start sends', () => {
    expect(START_DRAFT_SAVE_WAIT_MS).toBeGreaterThan(2 * GOAL_DRAFT_SYNC_TIMEOUT_MS)
  })

  it('ends a save that never answers as a failure, so Start never hangs', async () => {
    vi.useFakeTimers()
    const states: string[] = []
    let state = 'idle'
    const setState = (update: (current: string) => string) => { state = update(state); states.push(state) }
    const waiting = saveDraftBeforeStart(setState, () => new Promise<GoalDraftPersistResult>(() => {}))
    expect(states).toEqual(['saving'])
    await vi.advanceTimersByTimeAsync(START_DRAFT_SAVE_WAIT_MS)
    expect(await waiting).toBe(false)
    expect(states).toEqual(['saving', 'failed'])
  })

  it('leaves a conflict the save showed in place', async () => {
    let state = 'idle'
    const setState = (update: (current: string) => string) => { state = update(state) }
    const ok = await saveDraftBeforeStart(setState, async () => { state = 'conflict'; return 'conflict' })
    expect(ok).toBe(false)
    expect(state).toBe('conflict')
  })
})
