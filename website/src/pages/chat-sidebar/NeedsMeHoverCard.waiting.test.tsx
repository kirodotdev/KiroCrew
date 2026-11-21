/** The needs-me card's `waiting` kind: a session whose agent pinned an ask
 *  earns a card, and the card answers the ask with the same plain chat send as
 *  the composer's stack, or dismisses it. */
import { useRef } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatSlot, WaitingOnYouAsk } from '../../types'

const api = vi.hoisted(() => ({
  sessionSummary: vi.fn(),
  chatSlotDetail: vi.fn(),
  dismissWaitingOnYou: vi.fn(),
  viewerOwnerOk: vi.fn(),
}))
vi.mock('../../api/client', async importActual => {
  const actual = await importActual<typeof import('../../api/client')>()
  return { ...actual, api }
})
const sendTurn = vi.hoisted(() => vi.fn())
vi.mock('../../chat-core/transport/sendTurn', async importActual => ({
  ...(await importActual<typeof import('../../chat-core/transport/sendTurn')>()),
  sendTurn,
}))

import NeedsMeHoverLayer, { FINE_POINTER_QUERY, OPEN_DELAY_MS } from './NeedsMeHoverCard'
import { collectAsks, slotNeedsMe } from './needsMeCard'
import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'

const ASK: WaitingOnYouAsk = {
  key: 'scope-override', ask_id: 'wy-1', title: 'Post the Security Scope override', detail: '',
  options: [{ label: 'I posted it', description: '' }, { label: 'Hold the override', description: '' }], ts: 1795550000,
}
const SLOTS = [
  { key: 'loop', title: 'Babysit #38289', messages: 9, running: false, waiting_on_you: [ASK] },
  { key: 'idle', title: 'Idle', messages: 3, running: false, waiting_on_you: [] },
] as ChatSlot[]

function stubMatchMedia() {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (q: string) => ({
      matches: q === FINE_POINTER_QUERY, media: q, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }),
  })
}

function Sidebar() {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div ref={ref}>
      {SLOTS.map(s => <div key={s.key} data-session-row={s.key} role="button" tabIndex={0} data-testid={`row-${s.key}`}>{s.title}</div>)}
      <NeedsMeHoverLayer rootRef={ref} />
    </div>
  )
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<Provider store={store}><QueryClientProvider client={qc}><Sidebar /></QueryClientProvider></Provider>)
}

function hover(key: string) {
  fireEvent.pointerOver(screen.getByTestId(`row-${key}`))
  act(() => { vi.advanceTimersByTime(OPEN_DELAY_MS + 10) })
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  stubMatchMedia()
  store.dispatch(sseSlots(SLOTS))
  api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  api.viewerOwnerOk.mockResolvedValue(true)
})
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('needs-me waiting kind', () => {
  it('a pinned ask makes a row need the person, and is listed after a question card', () => {
    expect(slotNeedsMe({ waiting_on_you: [ASK] }, false)).toBe(true)
    expect(slotNeedsMe({ waiting_on_you: [] }, false)).toBe(false)
    const asks = collectAsks([], [{ question: 'Which branch?', options: [] }], { waiting: [ASK] })
    expect(asks.map(a => a.kind)).toEqual(['question', 'waiting'])
    expect(collectAsks([], undefined, { waiting: [] })).toEqual([])
  })

  it('opens for a row with a pinned ask and answers it with a chat send to that session', async () => {
    sendTurn.mockResolvedValue({ status: 'dispatched', body: {} })
    mount()
    hover('loop')
    const card = await screen.findByTestId('needs-me-card')
    const ask = within(card).getByTestId('needs-me-ask')
    expect(ask).toHaveAttribute('data-kind', 'waiting')
    expect(await within(ask).findByTestId('waiting-on-you-title', {}, { timeout: 5000 })).toHaveTextContent('Post the Security Scope override')
    fireEvent.click(within(ask).getAllByTestId('waiting-on-you-option')[0])
    fireEvent.click(within(ask).getByTestId('waiting-on-you-send'))
    await waitFor(() => expect(sendTurn).toHaveBeenCalledTimes(1))
    const [sent] = sendTurn.mock.calls[0]
    expect(sent).toMatchObject({ message: 'I posted it', slot: 'loop', meta: { waitingOnYouAnswer: { key: 'scope-override', ask_id: 'wy-1' } } })
    expect(sent).not.toHaveProperty('steer')
  })

  it('a refused answer keeps the ask in the card with the send error', async () => {
    sendTurn.mockResolvedValue({ status: 'refused', body: {}, reason: 'Someone else is typing.' })
    mount()
    hover('loop')
    const card = await screen.findByTestId('needs-me-card')
    const waiting = within(card).getByTestId('needs-me-ask')
    await within(waiting).findAllByTestId('waiting-on-you-option', {}, { timeout: 5000 })
    fireEvent.click(within(waiting).getAllByTestId('waiting-on-you-option')[0])
    fireEvent.click(within(waiting).getByTestId('waiting-on-you-send'))
    expect(await within(waiting).findByTestId('waiting-on-you-error')).toHaveTextContent('Someone else is typing.')
    expect(within(waiting).getByTestId('waiting-on-you-card')).toBeInTheDocument()
  })

  it('dismisses the ask with ✕', async () => {
    api.dismissWaitingOnYou.mockResolvedValue({ ok: true })
    mount()
    hover('loop')
    const card = await screen.findByTestId('needs-me-card')
    const dismiss = await within(card).findByTestId('waiting-on-you-dismiss', {}, { timeout: 5000 })
    fireEvent.click(dismiss)
    await waitFor(() => expect(api.dismissWaitingOnYou).toHaveBeenCalledWith('loop', 'scope-override', 'wy-1'))
  })

  it('opens no card for a row with no asks', () => {
    mount()
    hover('idle')
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })
})
