import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { addTab, getBottomTerminalSnapshot } from '../hooks/useBottomTerminal'

/** The shared beforeEach must finish before a file's own beforeEach can
 * restore its clock. A fresh factory needs no timer-driven database deletion. */
describe('terminal fixture with a suite-owned fake clock', () => {
  let previousFactory: IDBFactory
  let previousTime: number

  beforeAll(() => { vi.useFakeTimers({ now: new Date('2026-09-11T00:00:00Z') }) })
  afterAll(() => { vi.useRealTimers() })

  it('preserves the fake clock and lets the test drive its own database work', async () => {
    expect(vi.isFakeTimers()).toBe(true)
    previousFactory = indexedDB
    const pending = addTab('/timer-fixture', 'A')
    await vi.advanceTimersByTimeAsync(100)
    expect(await pending).not.toBeNull()
    expect(getBottomTerminalSnapshot('A').tabs).toHaveLength(1)
    previousTime = Date.now()
  })

  it('clears renderer state with a fresh factory without advancing or replacing the clock', () => {
    expect(vi.isFakeTimers()).toBe(true)
    expect(Date.now()).toBe(previousTime)
    expect(indexedDB).not.toBe(previousFactory)
    expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
    expect(getBottomTerminalSnapshot('A').open).toBe(false)
  })
})
