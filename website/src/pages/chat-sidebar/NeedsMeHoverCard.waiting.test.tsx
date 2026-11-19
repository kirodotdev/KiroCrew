/** The needs-me card's `waiting` kind: a session whose agent pinned an ask
 *  earns a card, and the card answers or dismisses the ask through the same
 *  routes as the composer's stack. */
import { useRef } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatSlot, WaitingOnYouAsk } from '../../types'
import { ApiError } from '../../api/apiError'

const api = vi.hoisted(() => ({
  sessionSummary: vi.fn(),
  chatSlotDetail: vi.fn(),
  answerWaitingOnYou: vi.fn(),
  dismissWaitingOnYou: vi.fn(),
  viewerOwnerOk: vi.fn(),
}))
vi.mock('../../api/client', async importActual => {
  const actual = await importActual<typeof import('../../api/client')>()
  return { ...actual, api }
})

import NeedsMeHoverLayer, { FINE_POINTER_QUERY, OPEN_DELAY_MS } from './NeedsMeHoverCard'
import { collectAsks, slotNeedsMe } from './needsMeCard'
import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'
import { __resetAnswerIdsForTests } from '../../components/waitingOnYou/answerIds'
import { registerMainComposer } from '../../utils/composerRestore'

const ASK: WaitingOnYouAsk = {
  key: 'scope-override', ask_id: 'wy-1', title: 'Post the Security Scope override', detail: '',
  options: [{ label: 'I posted it', description: '' }, { label: 'Hold the override', description: '' }], ts: 1795550000, state: 'pinned',
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
  __resetAnswerIdsForTests()
})

describe('needs-me waiting kind', () => {
  it('a pinned ask makes a row need the person, and is listed after a question card', () => {
    expect(slotNeedsMe({ waiting_on_you: [ASK] }, false)).toBe(true)
    expect(slotNeedsMe({ waiting_on_you: [] }, false)).toBe(false)
    const asks = collectAsks([], [{ question: 'Which branch?', options: [] }], { waiting: [ASK] })
    expect(asks.map(a => a.kind)).toEqual(['question', 'waiting'])
    expect(collectAsks([], undefined, { waiting: [] })).toEqual([])
  })

  it('opens for a row with a pinned ask and answers it on the session route', async () => {
    api.answerWaitingOnYou.mockResolvedValue({ ok: true, outcome: 'sent', answer_id: 'x' })
    mount()
    hover('loop')
    const card = await screen.findByTestId('needs-me-card')
    const ask = within(card).getByTestId('needs-me-ask')
    expect(ask).toHaveAttribute('data-kind', 'waiting')
    expect(within(ask).getByTestId('waiting-on-you-title')).toHaveTextContent('Post the Security Scope override')
    fireEvent.click(within(ask).getAllByTestId('waiting-on-you-option')[0])
    fireEvent.click(within(ask).getByTestId('waiting-on-you-send'))
    await waitFor(() => expect(api.answerWaitingOnYou).toHaveBeenCalledTimes(1))
    expect(api.answerWaitingOnYou).toHaveBeenCalledWith('loop', 'scope-override', expect.objectContaining({ ask_id: 'wy-1', label: 'I posted it' }))
  })

  it('returns a cleared choice to that session draft', async () => {
    api.answerWaitingOnYou.mockRejectedValue(new ApiError(
      409,
      'cleared',
      JSON.stringify({ ok: false, code: 'cleared' }),
    ))
    const restored: Array<[string, string]> = []
    const unregister = registerMainComposer((slot, text) => {
      restored.push([slot, text])
      return true
    })
    try {
      mount()
      hover('loop')
      const card = await screen.findByTestId('needs-me-card')
      const waiting = within(card).getByTestId('needs-me-ask')
      fireEvent.click(within(waiting).getAllByTestId('waiting-on-you-option')[0])
      fireEvent.click(within(waiting).getByTestId('waiting-on-you-send'))

      await waitFor(() => expect(restored).toEqual([['loop', 'I posted it']]))
      expect(within(waiting).getByTestId('waiting-on-you-stale')).toHaveTextContent(
        'Your answer is in the message box. Nothing was sent.',
      )
    } finally {
      unregister()
    }
  })

  it('dismisses the ask with ✕', async () => {
    api.dismissWaitingOnYou.mockResolvedValue({ ok: true })
    mount()
    hover('loop')
    const card = await screen.findByTestId('needs-me-card')
    fireEvent.click(within(card).getByTestId('waiting-on-you-dismiss'))
    await waitFor(() => expect(api.dismissWaitingOnYou).toHaveBeenCalledWith('loop', 'scope-override', 'wy-1'))
  })

  it('opens no card for a row with no asks', () => {
    mount()
    hover('idle')
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })
})
