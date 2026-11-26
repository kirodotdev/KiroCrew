/** The relay's exact entry point. Its own file, so the read cover it binds is not one the dashboard's modules bound
 *  at import, and resetting it to the default restores the real state. */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { bindReadCover, bindSlotReadSender, emitExactSlotRead, emitSlotRead, noteUnsavedRowTs, _resetSlotReadRelayForTest } from '../lib/slotReadRelay'

describe('emitExactSlotRead', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    _resetSlotReadRelayForTest()
  })
  afterEach(() => {
    _resetSlotReadRelayForTest()
    bindReadCover(() => undefined)
    vi.useRealTimers()
  })

  it('keeps an exact read exact and coalesces mixed callers at the newest watermark either passed', () => {
    const sent: Array<[string, string | undefined]> = []
    bindSlotReadSender((s, ts) => sent.push([s, ts]))
    noteUnsavedRowTs('k1', '2026-01-01T00:00:05Z')
    bindReadCover(() => '2026-01-01T00:00:07Z')
    emitExactSlotRead('k1', '2026-01-01T00:00:01Z')
    emitSlotRead('k1', '2026-01-01T00:00:03Z')
    vi.advanceTimersByTime(1_000)
    expect(sent).toEqual([
      ['k1', '2026-01-01T00:00:01Z'],
      ['k1', '2026-01-01T00:00:07Z'],
    ])
  })
})
