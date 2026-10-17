import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { createTestStore, renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import type { RootState } from '../store'

/**
 * Driver #1 guard: the "Optimize prompt" context must be read from THIS pane's
 * slot at click time, not from a live subscription to the globally-active
 * chat's message array.
 *
 * The old code did `const chatMessages = useAppSelector(s => s.chat.messages)`
 * at the top of every ChatInput. That subscription re-rendered every mounted
 * composer on each streamed frame (Immer returns a fresh `state.messages`
 * reference per flush, so the `===` selector always tripped) — the split-view
 * lag driver. It also carried a correctness bug: a NON-active pane built its
 * optimizer context from the ACTIVE pane's conversation.
 *
 * The fix reads `selectSlotMessages(store.getState(), slotId)` at click time.
 * This test pins that behavior: a non-active pane must send its OWN slot's
 * messages as optimizer context. It fails against the old code, which would
 * send the active pane's messages instead.
 */

const OPTIMIZED = 'a much better prompt'

/** Capture the POST body sent to the optimizer so we can assert on `context`. */
function stubOptimizerCapturing(bodies: string[]) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
      if (init?.body) bodies.push(String(init.body))
      return Promise.resolve({
        ok: true,
        json: async () => ({ changed: true, optimized: OPTIMIZED }),
      })
    }
    return Promise.resolve({ ok: true, json: async () => [] })
  })
}

const clickOptimize = () =>
  fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))

describe('ChatInput optimize: context is read from the pane\'s own slot', () => {
  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
      cb(0)
      return 0
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends the NON-active pane\'s own slot messages, not the active pane\'s', async () => {
    // The active pane holds one conversation; a different, non-active slot holds
    // another. A composer mounted for the non-active slot must optimize against
    // ITS conversation.
    const base = createTestStore().getState()
    const store = createTestStore({
      chat: {
        ...base.chat,
        activeSlot: 'active-slot',
        messages: [{ role: 'user', content: 'ACTIVE-PANE-ONLY-MARKER' }],
        slotMessages: {
          'pane-slot': [{ role: 'user', content: 'PANE-SLOT-ONLY-MARKER' }],
        },
      } as unknown as RootState['chat'],
    })
    const bodies: string[] = []
    vi.stubGlobal('fetch', stubOptimizerCapturing(bodies))

    renderWithProviders(
      <SlotProvider slotId="pane-slot">
        <ChatInput value="please optimize me" onChange={vi.fn()} onSend={vi.fn()} connected={true} />
      </SlotProvider>,
      { store },
    )
    clickOptimize()

    await vi.waitFor(() => expect(bodies.length).toBe(1))
    const body = JSON.parse(bodies[0]) as { context: string }
    // The context must come from the pane's own slot...
    expect(body.context).toContain('PANE-SLOT-ONLY-MARKER')
    // ...and must NOT leak the active pane's conversation (the old bug).
    expect(body.context).not.toContain('ACTIVE-PANE-ONLY-MARKER')
  })

  it('falls back to the active mirror for the active slot (preserved behavior)', async () => {
    // A pane whose slot IS the active slot reads the active mirror — exactly
    // what the old global subscription returned. This guards that the fix does
    // not change the focused composer's behavior.
    const base = createTestStore().getState()
    const store = createTestStore({
      chat: {
        ...base.chat,
        activeSlot: 'active-slot',
        messages: [{ role: 'user', content: 'ACTIVE-MIRROR-MARKER' }],
        slotMessages: {},
      } as unknown as RootState['chat'],
    })
    const bodies: string[] = []
    vi.stubGlobal('fetch', stubOptimizerCapturing(bodies))

    renderWithProviders(
      <SlotProvider slotId="active-slot">
        <ChatInput value="please optimize me" onChange={vi.fn()} onSend={vi.fn()} connected={true} />
      </SlotProvider>,
      { store },
    )
    clickOptimize()

    await vi.waitFor(() => expect(bodies.length).toBe(1))
    const body = JSON.parse(bodies[0]) as { context: string }
    expect(body.context).toContain('ACTIVE-MIRROR-MARKER')
  })
})
