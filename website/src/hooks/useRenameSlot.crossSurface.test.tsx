/**
 * Cross-surface rename safety.
 *
 * A slot can be renamed from the sidebar row editor OR the ChatPage header, and
 * both go through `useRenameSlot`. When the per-slot acknowledgement lived in
 * component refs, each surface read the OTHER surface's optimistic paint as an
 * acknowledged title — so a header rename followed by a sidebar rename, both
 * rejected, rolled back to the title the server had just rejected.
 *
 * These tests drive two hook instances over one slot, which is what two surfaces
 * are, and assert the rollback lands on the pre-rename title rather than on a
 * rejected optimistic one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../test/helpers'
import { sseConnected, sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({ api: { renameSlot: vi.fn() } }))

import { api } from '../api/client'
import { useRenameSlot, __resetRenameSlotStateForTests } from './useRenameSlot'

const SLOT = 'chat-x'
const ORIGINAL = 'Original title'

/** Two independent hook instances over one store — the two surfaces. */
function Surfaces({ onReady }: { onReady: (a: ReturnType<typeof useRenameSlot>, b: ReturnType<typeof useRenameSlot>) => void }) {
  const header = useRenameSlot(() => {})
  const sidebar = useRenameSlot(() => {})
  onReady(header, sidebar)
  return null
}

function mount() {
  const store = createTestStore()
  store.dispatch(sseConnected())
  store.dispatch(sseSlots([{ key: SLOT, title: ORIGINAL, messages: 0, running: false } as ChatSlot]))
  let header!: ReturnType<typeof useRenameSlot>
  let sidebar!: ReturnType<typeof useRenameSlot>
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <Surfaces onReady={(h, s) => { header = h; sidebar = s }} />
      </Provider>
    </QueryClientProvider>,
  )
  const titleOf = () => store.getState().dashboard.slots.find(s => s.key === SLOT)?.title
  return { store, header: () => header, sidebar: () => sidebar, titleOf }
}

describe('useRenameSlot – one acknowledgement per slot across surfaces', () => {
  beforeEach(() => {
    __resetRenameSlotStateForTests()
    vi.mocked(api.renameSlot).mockReset()
  })

  it('refuses a second commit for one slot and SAYS so, so a caller can keep its draft', async () => {
    // Both editors close on a commit, so the refusal has to be visible in the
    // return value — a silent false would still discard the second draft.
    vi.mocked(api.renameSlot).mockImplementation(() => new Promise(() => {}))
    const { header, sidebar } = mount()

    let firstAccepted!: boolean
    await act(async () => { firstAccepted = header()({ key: SLOT, next: 'Header A' }) })
    await waitFor(() => expect(api.renameSlot).toHaveBeenCalledTimes(1))

    let secondAccepted!: boolean
    await act(async () => { secondAccepted = sidebar()({ key: SLOT, next: 'Sidebar B' }) })

    expect(firstAccepted).toBe(true)
    expect(secondAccepted).toBe(false)
    expect(api.renameSlot).toHaveBeenCalledTimes(1)
  })

  it('a request that never settles frees the slot instead of wedging it for the tab', async () => {
    // A double that ignores the abort signal would never settle, so it could
    // prove nothing about the budget a real fetch honours.
    vi.mocked(api.renameSlot).mockImplementation((_k, _t, signal) => new Promise((_res, rej) => {
      signal?.addEventListener('abort', () => rej(new Error('aborted')))
    }))
    // Installed before the request, so the abort timer it schedules is the fake
    // one this test advances.
    vi.useFakeTimers()
    try {
      const { header, sidebar } = mount()

      let first!: boolean
      await act(async () => { first = header()({ key: SLOT, next: 'Header A' }) })
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(api.renameSlot).toHaveBeenCalledTimes(1)

      // Control: inside the budget the lock really does hold, so the acceptance
      // after the budget is the release firing rather than no lock existing.
      let blocked!: boolean
      await act(async () => { blocked = sidebar()({ key: SLOT, next: 'Sidebar B' }) })
      expect(first).toBe(true)
      expect(blocked).toBe(false)

      await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })

      let afterBudget!: boolean
      await act(async () => { afterBudget = sidebar()({ key: SLOT, next: 'Sidebar C' }) })
      expect(afterBudget).toBe(true)
      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(api.renameSlot).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('a single failed rename still reverts to the title it replaced', async () => {
    let reject: (e: Error) => void = () => {}
    vi.mocked(api.renameSlot).mockImplementation(() => new Promise((_r, rej) => { reject = rej }))
    const { header, titleOf } = mount()
    await act(async () => { header()({ key: SLOT, next: 'Only one' }) })
    expect(titleOf()).toBe('Only one')
    await act(async () => { reject(new Error('nope')) })
    await waitFor(() => expect(titleOf()).toBe(ORIGINAL))
  })
})
