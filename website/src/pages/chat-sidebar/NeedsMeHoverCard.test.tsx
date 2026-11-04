/** The sidebar's needs-me hover card: when it opens, what it lists, and the two
 *  writes it can make (an approval decision on the strict slot route, and a
 *  quick reply through the composer's send). */
import { useRef } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ChatMessage, ChatSlot } from '../../types'

const sendTurn = vi.hoisted(() => vi.fn())
vi.mock('../../chat-core/transport/sendTurn', () => ({ sendTurn }))

const api = vi.hoisted(() => ({
  approveChatSlot: vi.fn(),
  resolveApproval: vi.fn(),
  sessionSummary: vi.fn(),
  chatSlotDetail: vi.fn(),
}))
vi.mock('../../api/client', async importActual => {
  const actual = await importActual<typeof import('../../api/client')>()
  return { ...actual, api }
})

import NeedsMeHoverLayer, { CLOSE_GRACE_MS, FINE_POINTER_QUERY, OPEN_DELAY_MS } from './NeedsMeHoverCard'
import { collectAsks, pickRecap } from './needsMeCard'
import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'
import { clearQuestionCard, setQuestionCard } from '../../store/chatSlice'

const LONG_COMMAND = "npx playwright test tests/auth --project=chromium --workers=2 --reporter=line --grep '@redirect' && git log --oneline -3"

function permission(over: Record<string, unknown> = {}): ChatMessage {
  return {
    role: 'permission', content: '🔧 Shell', cls: '',
    meta: { approval_id: 'req-1', mid: 'mid-1', tool_name: 'Shell', full_command: LONG_COMMAND, ...over },
  } as ChatMessage
}

const SLOTS: ChatSlot[] = [
  { key: 'gate', title: 'Fix login redirect', messages: 9, running: true, pending_approval: true, last_message: 'Ready to push.' },
  { key: 'blocked', title: 'Blocked only', messages: 4, running: true, pending_approval: true },
  { key: 'idle', title: 'Idle', messages: 3, running: false },
  { key: 'ask', title: 'Release notes', messages: 5, running: true, needs_input: true },
] as ChatSlot[]

let fine = true
function stubMatchMedia() {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true, writable: true,
    value: (q: string) => ({
      matches: q === FINE_POINTER_QUERY ? fine : false, media: q, onchange: null,
      addListener: () => {}, removeListener: () => {}, addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
    }),
  })
}

function Sidebar() {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div ref={ref}>
      {SLOTS.map(s => (
        <div key={s.key} data-slot-key={s.key}>
          <div data-session-row={s.key} role="button" tabIndex={0} data-testid={`row-${s.key}`}>{s.title}</div>
        </div>
      ))}
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
  fine = true
  stubMatchMedia()
  store.dispatch(sseSlots(SLOTS))
  store.dispatch(clearQuestionCard({ slot: 'ask' }))
  api.sessionSummary.mockResolvedValue({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null })
  api.chatSlotDetail.mockImplementation(async (slot: string) => ({
    key: slot, running: true, total: 2, has_more: false, next_before: 0,
    messages: slot === 'blocked'
      ? [permission({ approval_id: 'req-9', mid: 'mid-9', deny_override_rule: 'rm-rf' })]
      : [permission()],
  }))
  api.approveChatSlot.mockResolvedValue({ ok: true })
  sendTurn.mockResolvedValue({ status: 'dispatched' })
})
afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('NeedsMeHoverCard', () => {
  it('opens on hover for a needs-me row and lists the full command, with no Open control', async () => {
    mount()
    hover('gate')
    const card = await screen.findByTestId('needs-me-card')
    expect(card.getAttribute('role')).toBe('dialog')
    expect(screen.getByTestId('needs-me-command').textContent).toBe(LONG_COMMAND)
    expect(screen.getByTestId('needs-me-recap').textContent).toBe('Ready to push.')
    expect(screen.queryByRole('button', { name: /^open/i })).toBeNull()
    expect(screen.getByText('A reply does not answer an approval.')).toBeTruthy()
  })

  it('does not open for a row that owes nothing', () => {
    mount()
    hover('idle')
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
  })

  it('does not open on a touch device', async () => {
    fine = false
    mount()
    hover('gate')
    await act(async () => { await Promise.resolve() })
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })

  it('shows no card for a session that is only blocked by a safety rule', async () => {
    mount()
    hover('blocked')
    await waitFor(() => expect(api.chatSlotDetail).toHaveBeenCalledWith('blocked', 80))
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('needs-me-card')).toBeNull()
  })

  it('Approve posts the strict slot route naming the row, and the row reads Approved in place', async () => {
    mount()
    hover('gate')
    fireEvent.click(await screen.findByRole('button', { name: 'Approve Shell' }))
    await screen.findByText('Approved')
    expect(api.approveChatSlot).toHaveBeenCalledTimes(1)
    const [slot, action, extra] = api.approveChatSlot.mock.calls[0]
    expect(slot).toBe('gate')
    expect(action).toBe('approved')
    expect(extra).toMatchObject({ origin: 'native', request_id: 'req-1', request_mid: 'mid-1' })
    expect(typeof extra.response_id).toBe('string')
    expect(api.resolveApproval).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Approve Shell' })).toBeNull()
  })

  it('Deny posts rejected on the strict route', async () => {
    mount()
    hover('gate')
    fireEvent.click(await screen.findByRole('button', { name: 'Deny Shell' }))
    await screen.findByText('Denied')
    expect(api.approveChatSlot.mock.calls[0][1]).toBe('rejected')
    expect(api.approveChatSlot.mock.calls[0][2]).toMatchObject({ origin: 'native', request_mid: 'mid-1' })
  })

  it('lists a question card in full with no action button', async () => {
    store.dispatch(setQuestionCard({ slot: 'ask', ask_id: 'a1', questions: [{ question: 'Which tone should the notes use for the release?', options: [{ label: 'Short list' }, { label: 'Paragraphs' }] }] }))
    mount()
    hover('ask')
    await screen.findByText('Which tone should the notes use for the release?')
    await waitFor(() => expect(api.sessionSummary).toHaveBeenCalledWith('ask'))
    // No approval is pending, so the transcript is never fetched.
    expect(api.chatSlotDetail).not.toHaveBeenCalled()
    const q = screen.getAllByTestId('needs-me-ask').find(el => el.dataset.kind === 'question')!
    expect(q.textContent).toContain('Short list')
    expect(q.querySelector('button')).toBeNull()
  })

  it('Enter in the quick reply sends to that slot through sendTurn', async () => {
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'push after the tests pass' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await screen.findByText('Sent.')
    expect(sendTurn).toHaveBeenCalledWith({ message: 'push after the tests pass', slot: 'gate' })
  })

  it('Shift+Enter does not send', async () => {
    mount()
    hover('gate')
    const box = await screen.findByTestId('needs-me-reply')
    fireEvent.change(box, { target: { value: 'line one' } })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(sendTurn).not.toHaveBeenCalled()
  })

  it('Esc closes the card and returns focus to the row', async () => {
    mount()
    const row = screen.getByTestId('row-gate')
    row.focus()
    fireEvent.keyDown(row, { key: 'ArrowRight' })
    const box = await screen.findByTestId('needs-me-reply')
    await waitFor(() => expect(box).toHaveFocus())
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
    expect(row).toHaveFocus()
  })

  it('closes when the pointer leaves the sidebar', async () => {
    const { container } = mount()
    hover('gate')
    await screen.findByTestId('needs-me-card')
    fireEvent.pointerLeave(container.firstElementChild!)
    act(() => { vi.advanceTimersByTime(CLOSE_GRACE_MS + 10) })
    await waitFor(() => expect(screen.queryByTestId('needs-me-card')).toBeNull())
  })
})

describe('needsMeCard model', () => {
  it('drops a safety-rule refusal and a resolved row, keeps a row this card settled', () => {
    const asks = collectAsks([
      permission({ deny_override_rule: 'rm-rf' }),
      permission({ approval_id: 'old', mid: 'm-old', resolved: 'approved' }),
      permission({ approval_id: 'mine', mid: 'm-mine', resolved: 'approved' }),
    ], undefined, { settled: new Set(['mine\u0000m-mine']) })
    expect(asks).toHaveLength(1)
    expect(asks[0]).toMatchObject({ kind: 'approval', requestId: 'mine' })
  })

  it('recaps from the newest live intent, else the last reply', () => {
    const intent = (title: string, turn: number, state = 'in-progress') => ({ title, progress: ['Tests pass'], state, last_touched_turn: turn })
    const summary = { enabled: true, intents: [intent('Old', 1), intent('New goal', 5), intent('Dropped', 9, 'dropped')] }
    expect(pickRecap(summary as never, 'last')).toEqual({ text: 'New goal. Tests pass', fromSummary: true })
    expect(pickRecap(undefined, 'last line')).toEqual({ text: 'last line', fromSummary: false })
    expect(pickRecap(undefined, '')).toBeNull()
  })
})
