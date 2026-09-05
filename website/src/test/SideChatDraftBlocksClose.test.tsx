/**
 * A draft in an EMBEDDED surface must block a close fired from a different host.
 *
 * SideChat and ChatEmbed both drop the action chip, so neither ever reaches the
 * dispatcher — and the gate used to see only hosts that DID reach it. The host that
 * loses the draft is not the host that was clicked, so a side-chat draft plus a
 * main-chat action click deleted the slot and discarded it, with the gate reading clean.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

const mockDeleteSlot = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    chatSlotNote: vi.fn().mockResolvedValue({ appended: true }),
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideQueueCancel: vi.fn().mockResolvedValue({ ok: true, content: '', depth: 0 }),
    sideQueueEdit: vi.fn().mockResolvedValue({ ok: true, depth: 1 }),
    planAction: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
}))

vi.mock('../store/chatSlice', async (orig) => {
  const actual = (await orig()) as Record<string, unknown>
  return {
    ...actual,
    deleteSlot: (key: string) => {
      mockDeleteSlot(key)
      return { type: 'test/deleteSlot', payload: key }
    },
  }
})

import SideChat from '../pages/chat/SideChat'
import { useOptionActionDispatch } from '../hooks/useOptionActionDispatch'
import { createTestStore, renderWithProviders } from './helpers'
import reducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import type { ComposerWork } from '../utils/composerWork'
import type { OptionAction } from '../app-sdk/protocol/options'

const SLOT = 'slot-side-draft-1'
const ACTION: OptionAction = { action: 'close', label: "That's all" }

const EMPTY: ComposerWork = {
  text: '',
  files: [],
  dirs: [],
  sessionRefs: [],
  pasteBlocks: [],
  knowledge: false,
  uploading: false,
  voiceCapture: false,
}

const initial = reducer(undefined, { type: '@@INIT' })
const dashInitial = { ...dashboardReducer(undefined, { type: '@@INIT' }), connected: true }

function sideStore() {
  return createTestStore({
    dashboard: dashInitial,
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'hi', ts: '2026-05-22T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'hello', ts: '2026-05-22T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: false,
          streaming: false,
          openedAtTurnCount: 0,
          createdAt: '2026-05-22T00:00:00Z',
        },
      },
    },
  })
}

/** The MAIN-chat host: same slot, its OWN composer empty. */
function mainChatHost() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <Provider store={createTestStore({})}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
  return renderHook(
    () => useOptionActionDispatch({ resolveSlot: () => SLOT, composerWork: EMPTY, sourceKey: 'row-1' }),
    { wrapper },
  )
}

describe('an embedded surface draft blocks a close fired elsewhere', () => {
  let confirmSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.clearAllMocks()
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  afterEach(() => {
    confirmSpy.mockRestore()
  })

  it('closes when the side composer is EMPTY', async () => {
    // Positive control: without this, the abort below could pass because the close
    // never works in this harness rather than because the draft was seen.
    renderWithProviders(<SideChat slot={SLOT} />, { store: sideStore() })
    const host = mainChatHost()

    await act(async () => {
      await host.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).toHaveBeenCalledWith(SLOT)
  })

  it('ABORTS when SideChat holds an unsent draft on the same slot', async () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: sideStore() })

    // Type into the REAL side composer, so the registration is exercised through the
    // shipped component rather than a stand-in for it.
    const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'half-written side question' } })
    expect(box.value).toBe('half-written side question')

    const host = mainChatHost()
    await act(async () => {
      await host.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).not.toHaveBeenCalled()
  })

  it('unmounting the side panel releases its claim', async () => {
    const view = renderWithProviders(<SideChat slot={SLOT} />, { store: sideStore() })
    const box = screen.getByLabelText('Ask a side question') as HTMLTextAreaElement
    fireEvent.change(box, { target: { value: 'draft that leaves with the panel' } })
    view.unmount()

    const host = mainChatHost()
    await act(async () => {
      await host.result.current.dispatchFollowUpAction(ACTION, 'row-1')
    })

    expect(mockDeleteSlot).toHaveBeenCalledWith(SLOT)
  })
})
