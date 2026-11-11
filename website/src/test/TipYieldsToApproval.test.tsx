import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider, useSelector } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { setActiveSlot, sseChatMessage, sseSubagentPending, resolveApprovalRow } from '../store/chatSlice'
import type { RootState } from '../store'
import { permissionRowTarget } from '../types/approvalTarget'
import { useTipTrigger } from '../components/TipCard'
import { selectTipYieldsToComposerSurface } from '../pages/chat/page/activeSlotState'

// The approval bar fuses to the top of the composer, right under the tip's
// band. A tip left above it reads as the thing being approved, so the tip
// yields while a request waits. Driven through the REAL reducer and the page's
// own selector: the rows arrive the way a WS frame delivers them.

const mockTip = {
  id: 'test-tip',
  feature: 'Memory',
  title: 'Save a writing rule to memory',
  body: 'Memory keeps preferences across sessions.',
  why: '',
  doc: 'memory.md',
  cta_prompt: '',
}

vi.mock('../api/client', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  api: {
    tipsFeedback: vi.fn().mockResolvedValue({ ok: true }),
    tipsNext: vi.fn().mockResolvedValue(null),
    tipsStatus: vi.fn().mockResolvedValue({ enabled_config: true, opted_out: false, cadence_hours: 6 }),
  },
}))

const APPROVAL_META = { approval_id: 'ap-1', mid: 'mid-1', tool_input: '{"command":"ls /tmp"}', tool_call_id: 'tc-1' }

function makeStore() {
  const store = configureStore({ reducer: { chat: chatReducer } })
  store.dispatch(setActiveSlot('slot-a'))
  store.dispatch(sseChatMessage({ slot: 'slot-a', role: 'user', content: 'list files', meta: { mid: 'mid-0' } }))
  return store
}

const yields = (store: ReturnType<typeof makeStore>) =>
  selectTipYieldsToComposerSurface(store.getState() as unknown as RootState)

function Harness() {
  const suppressed = useSelector(selectTipYieldsToComposerSurface)
  const { tip } = useTipTrigger(true, suppressed, 'slot-a')
  return <div data-testid="tip-out">{tip ? tip.title : 'none'}</div>
}

describe('the tip yields to a pending tool approval', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('yields while the active session has an unanswered request, and stops once it is answered', () => {
    const store = makeStore()
    expect(yields(store)).toBe(false)

    store.dispatch(sseChatMessage({ slot: 'slot-a', role: 'permission', content: 'Running: ls /tmp', meta: APPROVAL_META }))
    expect(yields(store)).toBe(true)

    store.dispatch(resolveApprovalRow({ target: permissionRowTarget(APPROVAL_META, 'slot-a')!, decision: 'allow' }))
    expect(yields(store)).toBe(false)
  })

  it('does not yield to a request waiting in another session', () => {
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: 'slot-b', role: 'permission', content: 'Running: ls /tmp', meta: APPROVAL_META }))
    expect(yields(store)).toBe(false)
  })

  it('yields to a coordinator-registry request that sits before the latest user message', () => {
    // The registry's card waits on the registry, not on the turn, so a later
    // send leaves its bar up; the tip must stay out of the way the same.
    const store = makeStore()
    store.dispatch(sseChatMessage({
      slot: 'slot-a', role: 'permission', content: '[subagent] Write File',
      meta: {
        approval_id: 'co-1', tool_input: '{}', source: 'subagent', registry: 'coordinator',
        approval_target: { origin: 'coordinator', id: 'co-1', slot: 'slot-a', instance: 'inst-co-1' },
      },
    }))
    store.dispatch(sseChatMessage({ slot: 'slot-a', role: 'user', content: 'keep going', meta: { mid: 'mid-2' } }))
    expect(yields(store)).toBe(true)
  })

  it('yields to a pending sub-agent spawn approval in the active session', () => {
    const store = makeStore()
    store.dispatch(sseSubagentPending({ slot: 'slot-a', id: 'p0', task: 'parked', approval_id: 'spawn:p0' }))
    expect(yields(store)).toBe(true)
  })

  it('hides a visible tip when a request arrives', async () => {
    const { api: mockApi } = await import('../api/client')
    ;(mockApi.tipsNext as ReturnType<typeof vi.fn>).mockResolvedValue({ tip: mockTip, glow: true })
    const store = makeStore()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <Harness />
        </QueryClientProvider>
      </Provider>,
    )

    // The tip becomes visible after the 10s gate.
    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('Save a writing rule to memory')

    await act(async () => {
      store.dispatch(sseChatMessage({ slot: 'slot-a', role: 'permission', content: 'Running: ls /tmp', meta: APPROVAL_META }))
    })
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
  })

  it('does not fetch a tip while a request is waiting', async () => {
    const { api: mockApi } = await import('../api/client')
    const tipsNext = mockApi.tipsNext as ReturnType<typeof vi.fn>
    tipsNext.mockClear()
    tipsNext.mockResolvedValue({ tip: mockTip, glow: true })
    const store = makeStore()
    store.dispatch(sseChatMessage({ slot: 'slot-a', role: 'permission', content: 'Running: ls /tmp', meta: APPROVAL_META }))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <Harness />
        </QueryClientProvider>
      </Provider>,
    )

    await act(async () => {
      vi.advanceTimersByTime(11000)
    })
    await act(async () => {
      await vi.runOnlyPendingTimersAsync()
    })
    expect(tipsNext).not.toHaveBeenCalled()
    expect(screen.getByTestId('tip-out').textContent).toBe('none')
  })
})
