import { describe, it, expect, vi } from 'vitest'
import reducer, { sseSideResult, sideClose } from '../store/chatSlice'

vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = vi.fn().mockResolvedValue(
        prop === 'sideOpen' ? { ok: true, open: true, messages: 0, last_run_id: '', created_at: '' }
          : prop === 'sideTurn' ? { ok: true, run_id: 'r1', messages: 1 }
            : prop === 'sideClose' ? { ok: true, was_open: true }
              : prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 }
                : {},
      )
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

const SLOT = 'parent-slot-1'

describe('sideClose isolation from parent state', () => {
  const initial = reducer(undefined, { type: '@@INIT' })

  it('leaves parent messages byte-equal after multi-turn side activity', () => {
    const seeded = {
      ...initial,
      activeSlot: SLOT,
      messages: [
        { role: 'user', content: 'main question', cls: 'msg msg-u' },
        { role: 'assistant', content: 'main answer', cls: 'msg msg-a' },
      ],
    } as ReturnType<typeof reducer>

    let state = reducer(seeded, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'side q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'side a' }))
    state = reducer(state, sideClose(SLOT))

    expect(state.slotSide[SLOT]).toBeUndefined()
    expect(state.messages).toHaveLength(2)
    expect(state.messages[0]).toMatchObject({ role: 'user', content: 'main question' })
    expect(state.messages[1]).toMatchObject({ role: 'assistant', content: 'main answer' })
  })

  it('does not corrupt parent messages when fired mid-stream', () => {
    const seeded = {
      ...initial,
      activeSlot: SLOT,
      messages: [{ role: 'user', content: 'main', cls: 'msg msg-u' }],
    } as ReturnType<typeof reducer>

    let state = reducer(seeded, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial' }))
    state = reducer(state, sideClose(SLOT))

    expect(state.slotSide[SLOT]).toBeUndefined()
    expect(state.messages).toHaveLength(1)
    expect(state.messages[0].content).toBe('main')
  })
})
