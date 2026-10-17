import { describe, it, expect, vi } from 'vitest'
import { act, screen, fireEvent } from '@testing-library/react'
import reducer, { sseSideResult, stageToMainComposer } from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: new Proxy({}, {
    get: (_t, prop) => {
      const fn = vi.fn().mockResolvedValue(
        prop === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {},
      )
      Object.defineProperty(_t, prop, { value: fn, writable: true, configurable: true })
      return fn
    },
  }),
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'

const SLOT = 'test-slot-1'
const initial = reducer(undefined, { type: '@@INIT' })

function storeWith(sideOver: Record<string, unknown>) {
  return createTestStore({
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'Remove the per-service limiter.', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          ...sideOver,
        },
      },
    },
  })
}

describe('SideChat send to main chat', () => {
  it('offers the button on a settled assistant answer', () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: storeWith({}) })
    expect(screen.getByTestId('side-chat-send-to-main')).toBeInTheDocument()
  })

  it('clicking stages the answer into the main composer (append, not send)', () => {
    const store = storeWith({})
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    fireEvent.click(screen.getByTestId('side-chat-send-to-main'))
    expect(store.getState().chat.mainComposerAppend).toEqual({ slot: SLOT, text: 'Remove the per-service limiter.' })
  })

  it('stages rendered answer text without a trailing option marker', () => {
    const store = storeWith({
      messages: [
        { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
        { role: 'assistant' as const, content: 'Apply the narrow fix.\n\n[OPTIONS: Fix | Skip]', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
      ],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    fireEvent.click(screen.getByTestId('side-chat-send-to-main'))
    expect(store.getState().chat.mainComposerAppend).toEqual({ slot: SLOT, text: 'Apply the narrow fix.' })
  })

  it('keys the hand-off to its OWN slot, not the dashboard active slot', () => {
    // A Members-thread Side Chat runs on a slot that is NOT the dashboard's
    // active slot. The hand-off must name the panel's own slot so it lands in
    // that member's composer and never leaks into the dashboard composer.
    const memberSlot = 'member-abc'
    const store = createTestStore({
      chat: {
        ...initial,
        activeSlot: SLOT, // dashboard is showing a different slot
        slotSide: {
          [memberSlot]: {
            messages: [
              { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
              { role: 'assistant' as const, content: 'Fix it here.', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
            ],
            lastRunId: 'r1',
          },
        },
      },
    })
    renderWithProviders(<SideChat slot={memberSlot} />, { store })
    fireEvent.click(screen.getByTestId('side-chat-send-to-main'))
    expect(store.getState().chat.mainComposerAppend).toEqual({ slot: memberSlot, text: 'Fix it here.' })
  })

  it('is hidden while the answer is still streaming', () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: storeWith({ streaming: true }) })
    expect(screen.queryByTestId('side-chat-send-to-main')).not.toBeInTheDocument()
  })

  it('is hidden when the last answer is an error', () => {
    const store = createTestStore({
      chat: {
        ...initial,
        activeSlot: SLOT,
        slotSide: {
          [SLOT]: {
            messages: [
              { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
              { role: 'assistant' as const, content: 'boom', ts: '2026-05-20T00:00:01Z', run_id: 'r1', is_error: true },
            ],
            lastRunId: 'r1',
          },
        },
      },
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    expect(screen.queryByTestId('side-chat-send-to-main')).not.toBeInTheDocument()
  })

  it('preserves leading indentation while removing trailing whitespace', () => {
    const indented = '    const x = 1\n    return x\n'
    const store = createTestStore({
      chat: {
        ...initial,
        activeSlot: SLOT,
        slotSide: {
          [SLOT]: {
            messages: [
              { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
              { role: 'assistant' as const, content: indented, ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
            ],
            lastRunId: 'r1',
          },
        },
      },
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })
    fireEvent.click(screen.getByTestId('side-chat-send-to-main'))
    expect(store.getState().chat.mainComposerAppend).toEqual({ slot: SLOT, text: indented.trimEnd() })
  })

  it('hides the button when the rendered answer contains only an option marker', () => {
    renderWithProviders(<SideChat slot={SLOT} />, {
      store: storeWith({
        messages: [
          { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
          { role: 'assistant' as const, content: '[OPTIONS: Fix | Skip]', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
        ],
      }),
    })
    expect(screen.queryByTestId('side-chat-send-to-main')).not.toBeInTheDocument()
  })

  it('treats a whitespace-only answer as empty (button hidden)', () => {
    renderWithProviders(<SideChat slot={SLOT} />, {
      store: storeWith({
        messages: [
          { role: 'user' as const, content: 'q', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
          { role: 'assistant' as const, content: '   \n  ', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
        ],
      }),
    })
    expect(screen.queryByTestId('side-chat-send-to-main')).not.toBeInTheDocument()
  })

  it('acknowledges one answer without blocking a newer answer', () => {
    vi.useFakeTimers()
    try {
      const store = storeWith({})
      renderWithProviders(<SideChat slot={SLOT} />, { store })

      const button = screen.getByTestId('side-chat-send-to-main')
      fireEvent.click(button)
      expect(button).toHaveTextContent('Added to main chat')
      expect(button).toHaveAttribute('aria-disabled', 'true')

      act(() => { store.dispatch(stageToMainComposer(null)) })
      fireEvent.click(button)
      expect(store.getState().chat.mainComposerAppend).toBeNull()

      act(() => {
        store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'user', content: 'new q', ts: 1_779_235_202 }))
        store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r2', role: 'assistant', content: 'Use the newer answer.', ts: 1_779_235_203, final: true }))
      })
      expect(button).toHaveTextContent('Add to main chat')
      expect(button).toHaveAttribute('aria-disabled', 'false')
      fireEvent.click(button)
      expect(store.getState().chat.mainComposerAppend).toEqual({ slot: SLOT, text: 'Use the newer answer.' })
      expect(button).toHaveTextContent('Added to main chat')

      act(() => { vi.advanceTimersByTime(2_000) })
      expect(button).toHaveTextContent('Add to main chat')
      expect(button).toHaveAttribute('aria-disabled', 'false')
    } finally {
      vi.useRealTimers()
    }
  })
})
