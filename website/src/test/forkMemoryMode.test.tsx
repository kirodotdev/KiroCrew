import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ forkChatSlot: vi.fn(), dashboardConfig: vi.fn() }))
vi.mock('../api/client', async () => {
  const { ApiError } = await import('../api/apiError')
  return {
    SEARCH_MIN_CHARS: 2,
    ApiError,
    api: new Proxy(mocks as Record<string, unknown>, {
      get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
    }),
  }
})

import { store } from '../store'
import { sseSlots } from '../store/dashboardSlice'
import { setActiveSlot } from '../store/chatSlice'
import { useForkSession } from '../pages/useChatPageSlotSwitches'
import { stricterForkMemoryModes } from '../utils/forkMemoryModes'

const SOURCE = 'chat-mode-source'
const CHILD = 'chat-mode-child'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <Provider store={store}><QueryClientProvider client={qc}>{children}</QueryClientProvider></Provider>
}

const listedChild = () => store.getState().dashboard.slots.find(s => s.key === CHILD)

beforeEach(() => {
  const source: ChatSlot = { key: SOURCE, title: SOURCE, messages: 2, running: false, folder_id: '' }
  store.dispatch(sseSlots([source]))
  store.dispatch(setActiveSlot(SOURCE))
  mocks.dashboardConfig.mockResolvedValue({})
})
afterEach(() => {
  vi.clearAllMocks()
  store.dispatch(setActiveSlot(null))
  store.dispatch(sseSlots([]))
})

describe('stricterForkMemoryModes', () => {
  it('offers only the modes more private than the parent', () => {
    expect(stricterForkMemoryModes('persistent')).toEqual(['incognito', 'temporary'])
    expect(stricterForkMemoryModes(undefined)).toEqual(['incognito', 'temporary'])
    expect(stricterForkMemoryModes('incognito')).toEqual(['temporary'])
    expect(stricterForkMemoryModes('temporary')).toEqual([])
  })

  it('offers nothing for a mode it does not know', () => {
    expect(stricterForkMemoryModes('sideways')).toEqual([])
  })
})

describe('a transcript fork with a memory mode', () => {
  it('sends the mode to the gateway and lists the child with the mode it was born with', async () => {
    mocks.forkChatSlot.mockResolvedValue({ ok: true, key: CHILD, title: 'Fork', messages: 2, memory_mode: 'temporary' })
    const { result } = renderHook(() => useForkSession({ activeSlot: SOURCE, dispatch: store.dispatch, showActionError: vi.fn() }), { wrapper })

    await act(async () => { await result.current.handleFork(1, 'row-1', 'head', 'temporary') })

    expect(mocks.forkChatSlot).toHaveBeenCalledWith(SOURCE, 1, undefined, undefined, 'head', 'row-1', 'temporary')
    await waitFor(() => expect(listedChild()?.memory_mode).toBe('temporary'))
  })

  it('keeps the call without a mode exactly as it was', async () => {
    mocks.forkChatSlot.mockResolvedValue({ ok: true, key: CHILD, title: 'Fork', messages: 2, memory_mode: 'persistent' })
    const { result } = renderHook(() => useForkSession({ activeSlot: SOURCE, dispatch: store.dispatch, showActionError: vi.fn() }), { wrapper })

    await act(async () => { await result.current.handleFork(1, undefined, 'head') })

    expect(mocks.forkChatSlot.mock.calls[0]).toEqual([SOURCE, 1, undefined, undefined, 'head'])
  })
})
