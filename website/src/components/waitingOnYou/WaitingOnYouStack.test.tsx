/** The composer's stack of pinned asks: what it shows, what each click sends,
 *  and what each send receipt or dismiss refusal leaves on screen. An answer is
 *  the ordinary chat send of the label or note, naming its ask, and never a steer. */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import { sendTurn, type SendReceipt, type SendTurnOptions } from '../../chat-core/transport/sendTurn'
import { appendQueuedMessage, cancelQueuedMessage } from '../../store/chatSlice'
import type { WaitingOnYouAsk } from '../../types'
import WaitingOnYouStack from './WaitingOnYouStack'
import { __resetWaitingDraftsForTests, waitingDraftText } from './drafts'

vi.mock('../../api/client', () => ({
  api: { dismissWaitingOnYou: vi.fn(), viewerOwnerOk: vi.fn() },
}))
vi.mock('../../chat-core/transport/sendTurn', async importOriginal => ({
  ...(await importOriginal<typeof import('../../chat-core/transport/sendTurn')>()),
  sendTurn: vi.fn(),
}))

const SLOT = 'chat-1'

function ask(over: Partial<WaitingOnYouAsk> = {}): WaitingOnYouAsk {
  return {
    key: 'scope-override',
    ask_id: 'wy-0000000000000001',
    title: 'Post the Security Scope override on #38289',
    detail: '',
    options: [{ label: 'I posted it', description: 'I re-check the lane' }, { label: 'Hold the override', description: '' }],
    ts: 1795550000,
    ...over,
  }
}

const actionAsk = (over: Partial<WaitingOnYouAsk> = {}) => ask({ key: 'run-migration', ask_id: 'wy-00000000000000a1', title: 'Run the migration', options: [], ...over })

const receipt = (status: SendReceipt['status'], reason?: string): SendReceipt => ({ status, body: {}, ...(reason ? { reason } : {}) })

function refusal(status: number, code: string): ApiError {
  return new ApiError(status, code, JSON.stringify({ ok: false, code }))
}

function mount(asks: WaitingOnYouAsk[]) {
  const ui = (next: readonly WaitingOnYouAsk[]) => <WaitingOnYouStack slotKey={SLOT} asks={next} agent="kirocrew" />
  const view = renderWithProviders(ui(asks))
  // The slot push: the caller re-renders the stack with the row's new asks.
  const push = (next: WaitingOnYouAsk[]) => act(() => { view.rerender(ui(next)) })
  return { ...view, push }
}

/** The composer stack and the Questions tab list, sharing one slot's drafts. */
function mountShared(asks: WaitingOnYouAsk[]) {
  const ui = (next: readonly WaitingOnYouAsk[]) => (
    <>
      <div data-testid="composer-stack"><WaitingOnYouStack slotKey={SLOT} asks={next} agent="kirocrew" /></div>
      <div data-testid="questions-stack"><WaitingOnYouStack slotKey={SLOT} asks={next} agent="kirocrew" layout="list" /></div>
    </>
  )
  const view = renderWithProviders(ui(asks))
  return {
    ...view,
    composer: () => within(screen.getByTestId('composer-stack')),
    questions: () => within(screen.getByTestId('questions-stack')),
  }
}

const sends = () => vi.mocked(sendTurn).mock.calls.map(([opts]) => opts as SendTurnOptions)
const chooseAndSend = (index = 0) => {
  fireEvent.click(screen.getAllByTestId('waiting-on-you-option')[index])
  fireEvent.click(screen.getByTestId('waiting-on-you-send'))
}

beforeEach(() => {
  vi.mocked(api.viewerOwnerOk).mockResolvedValue(true as never)
  vi.mocked(sendTurn).mockResolvedValue(receipt('dispatched'))
  vi.setSystemTime(new Date(1795550000 * 1000 + 3 * 3600 * 1000))
})

afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
  __resetWaitingDraftsForTests()
})

describe('WaitingOnYouStack', () => {
  it('renders the newest ask with its label, agent, title, options and age', () => {
    mount([ask()])
    const card = screen.getByTestId('waiting-on-you-card')
    expect(within(card).getByText('Action for you')).toBeInTheDocument()
    expect(within(card).getByText(/kirocrew/)).toBeInTheDocument()
    expect(within(card).getByTestId('waiting-on-you-title')).toHaveTextContent('Post the Security Scope override on #38289')
    expect(within(card).getAllByTestId('waiting-on-you-option').map(b => b.textContent)).toEqual(['I posted itI re-check the lane', 'Hold the override'])
    expect(within(card).getByTestId('waiting-on-you-age')).toHaveTextContent(/^Pinned 3h ago$/)
  })

  it('shows six choices as one selectable list and sends only the selected choice', async () => {
    const options = Array.from({ length: 6 }, (_, i) => ({ label: `Choice ${i + 1}`, description: '' }))
    mount([ask({ options })])

    const list = screen.getByTestId('waiting-on-you-options')
    expect(list).toHaveClass('flex-col')
    expect(within(list).getAllByTestId('waiting-on-you-option')).toHaveLength(6)
    expect(screen.getByTestId('waiting-on-you-send')).toBeDisabled()

    fireEvent.click(within(list).getByText('Choice 5'))
    expect(sends()).toHaveLength(0)
    expect(within(list).getByText('Choice 5').closest('button')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByTestId('waiting-on-you-send'))

    await waitFor(() => expect(sends()).toHaveLength(1))
    expect(sends()[0].message).toBe('Choice 5')
  })

  it('wraps an unbroken option label and description anywhere so a narrow card does not overflow', async () => {
    const unbroken = 'src/kiro_crew/dashboard/waiting_on_you_events_and_their_very_long_module_path.py'
    mount([ask({ options: [{ label: unbroken, description: unbroken }] })])
    const option = screen.getByTestId('waiting-on-you-option')
    expect(within(option).getAllByText(unbroken)[0].parentElement).toHaveClass('min-w-0', '[overflow-wrap:anywhere]')
  })

  it('wraps an unbroken option label anywhere in the read-only list', async () => {
    const unbroken = 'src/kiro_crew/dashboard/waiting_on_you_events_and_their_very_long_module_path.py'
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(false as never)
    mount([ask({ options: [{ label: unbroken, description: '' }] })])
    await screen.findByTestId('waiting-on-you-read-only')
    expect(screen.getByText(unbroken)).toHaveClass('[overflow-wrap:anywhere]')
  })

  it('renders nothing for a slot with no asks', () => {
    mount([])
    expect(screen.queryByTestId('waiting-on-you-stack')).toBeNull()
  })

  it('shows the newest ask in full and expands the older ones in a bounded scroll region', () => {
    mount([ask({ key: 'a', ask_id: 'wy-a', title: 'First' }), ask({ key: 'b', ask_id: 'wy-b', title: 'Second' }), ask({ key: 'c', ask_id: 'wy-c', title: 'Third' })])
    expect(screen.getAllByTestId('waiting-on-you-title').map(e => e.textContent)).toEqual(['Third'])
    const more = screen.getByTestId('waiting-on-you-more')
    expect(more).toHaveTextContent('+2 more')
    fireEvent.click(more)
    expect(screen.getAllByTestId('waiting-on-you-title').map(e => e.textContent)).toEqual(['First', 'Second', 'Third'])
    expect(screen.getByTestId('waiting-on-you-more')).toHaveTextContent('Show fewer')
    expect(screen.getByTestId('waiting-on-you-stack')).toHaveClass(
      'max-h-[min(50svh,32rem)]',
      'overflow-y-auto',
      'overscroll-contain',
    )
  })

  it('answers a decision card through the plain chat send: the exact label, its ask named, no steer', async () => {
    mount([ask()])
    chooseAndSend(1)
    await waitFor(() => expect(screen.queryByTestId('waiting-on-you-card')).toBeNull())
    const [sent] = sends()
    expect(sent.message).toBe('Hold the override')
    expect(sent.slot).toBe(SLOT)
    expect(sent.meta).toEqual({
      sendId: expect.any(String),
      waitingOnYouAnswer: { key: 'scope-override', ask_id: 'wy-0000000000000001' },
    })
    expect(sent).not.toHaveProperty('steer')
    expect(sent).not.toHaveProperty('wire')
  })

  it('answers an action card with the typed note, or Done for an empty note', async () => {
    vi.mocked(sendTurn).mockResolvedValue(receipt('queued'))
    mount([actionAsk(), actionAsk({ key: 'second', ask_id: 'wy-00000000000000a2' })])
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: '  ran it on prod  ' } })
    fireEvent.click(screen.getByTestId('waiting-on-you-done'))
    await waitFor(() => expect(sends()).toHaveLength(1))
    expect(sends()[0].message).toBe('ran it on prod')
    expect(sends()[0].meta?.waitingOnYouAnswer).toEqual({ key: 'second', ask_id: 'wy-00000000000000a2' })
    await waitFor(() => expect(screen.getByTestId('waiting-on-you-title')).toHaveTextContent('Run the migration'))
    fireEvent.click(screen.getByTestId('waiting-on-you-done'))
    await waitFor(() => expect(sends()).toHaveLength(2))
    expect(sends()[1].message).toBe('Done')
  })

  it.each(['dispatched', 'queued'] as const)('deletes the submitted draft on a %s receipt', async status => {
    vi.mocked(sendTurn).mockResolvedValue(receipt(status))
    mount([actionAsk()])
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: 'ran it' } })
    fireEvent.click(screen.getByTestId('waiting-on-you-done'))
    await waitFor(() => expect(waitingDraftText(SLOT, actionAsk().ask_id)).toBe(''))
    if (status === 'dispatched') expect(screen.queryByTestId('waiting-on-you-card')).toBeNull()
    else expect(screen.getByTestId('waiting-on-you-card')).toBeInTheDocument()
  })

  it.each([
    ['refused', 'The session did not take the answer, so the ask stays pinned. Someone else is typing.'],
    ['transport-error', 'Your answer did not reach the session. Try again.'],
    ['response-late', 'Your answer did not reach the session. Try again.'],
    ['unknown', 'Your answer did not reach the session. Try again.'],
  ] as const)('keeps the draft and shows the send error on a %s receipt', async (status, sentence) => {
    vi.mocked(sendTurn).mockResolvedValue(receipt(status, status === 'refused' ? 'Someone else is typing.' : undefined))
    mount([actionAsk()])
    const note = screen.getByTestId('waiting-on-you-note')
    fireEvent.change(note, { target: { value: 'ran it on prod' } })
    fireEvent.click(screen.getByTestId('waiting-on-you-done'))
    expect(await screen.findByTestId('waiting-on-you-error')).toHaveTextContent(sentence)
    expect(screen.getByTestId('waiting-on-you-card')).toBeInTheDocument()
    expect(note).toHaveValue('ran it on prod')
    expect(waitingDraftText(SLOT, actionAsk().ask_id)).toBe('ran it on prod')
    expect(screen.getByTestId('waiting-on-you-done')).toBeEnabled()
  })

  it.each(['queued', 'refused'] as const)('in the Questions tab: a %s receipt deletes the draft only when the send was accepted', async status => {
    vi.mocked(sendTurn).mockResolvedValue(receipt(status, status === 'refused' ? 'no' : undefined))
    const { questions, unmount } = mountShared([actionAsk()])
    fireEvent.change(questions().getByTestId('waiting-on-you-note'), { target: { value: 'from the panel' } })
    fireEvent.click(questions().getByTestId('waiting-on-you-done'))
    await waitFor(() => expect(sends()).toHaveLength(1))
    if (status === 'queued') {
      await waitFor(() => expect(waitingDraftText(SLOT, actionAsk().ask_id)).toBe(''))
    } else {
      expect(await questions().findByTestId('waiting-on-you-error')).toBeInTheDocument()
      expect(waitingDraftText(SLOT, actionAsk().ask_id)).toBe('from the panel')
    }
    unmount()
  })

  it('keeps every surface disabled while an answer is queued and restores them on cancel', async () => {
    vi.mocked(sendTurn).mockResolvedValue(receipt('queued'))
    const view = mountShared([actionAsk()])

    fireEvent.click(view.composer().getByTestId('waiting-on-you-done'))
    await waitFor(() => expect(sends()).toHaveLength(1))
    act(() => {
      view.store.dispatch(appendQueuedMessage({
        slot: SLOT,
        content: 'Done',
        ts: '2026-11-20T04:00:00Z',
        queue_id: 'q-answer',
        waiting_on_you_answer: { key: actionAsk().key, ask_id: actionAsk().ask_id },
      }))
    })

    for (const surface of [view.composer(), view.questions()]) {
      expect(surface.getByTestId('waiting-on-you-answer-queued')).toHaveTextContent('Your answer is queued and goes when the agent finishes.')
      expect(surface.getByTestId('waiting-on-you-note')).toBeDisabled()
      expect(surface.getByTestId('waiting-on-you-done')).toBeDisabled()
      expect(surface.getByTestId('waiting-on-you-dismiss')).toBeDisabled()
    }
    fireEvent.click(view.questions().getByTestId('waiting-on-you-done'))
    expect(sends()).toHaveLength(1)

    act(() => { view.store.dispatch(cancelQueuedMessage({ slot: SLOT, queue_id: 'q-answer' })) })
    for (const surface of [view.composer(), view.questions()]) {
      expect(surface.queryByTestId('waiting-on-you-answer-queued')).toBeNull()
      expect(surface.getByTestId('waiting-on-you-note')).toBeEnabled()
      expect(surface.getByTestId('waiting-on-you-done')).toBeEnabled()
      expect(surface.getByTestId('waiting-on-you-dismiss')).toBeEnabled()
    }
  })

  it('preserves a newer shared draft after an answer is accepted', async () => {
    let settle: (value: SendReceipt) => void = () => {}
    vi.mocked(sendTurn).mockReturnValue(new Promise(r => { settle = r }))
    const { composer, questions, unmount } = mountShared([actionAsk()])
    fireEvent.change(composer().getByTestId('waiting-on-you-note'), { target: { value: 'submitted note' } })
    fireEvent.click(composer().getByTestId('waiting-on-you-done'))
    fireEvent.change(questions().getByTestId('waiting-on-you-note'), { target: { value: 'newer note' } })

    await act(async () => { settle(receipt('dispatched')) })

    expect(waitingDraftText(SLOT, actionAsk().ask_id)).toBe('newer note')
    unmount()
  })

  it.each(['dismissed', 'gone'] as const)('preserves a newer shared draft after a dismiss settles as %s', async outcome => {
    let resolveRequest: (value: { ok: true }) => void = () => {}
    let rejectRequest: (reason: unknown) => void = () => {}
    vi.mocked(api.dismissWaitingOnYou).mockReturnValue(new Promise((resolve, reject) => {
      resolveRequest = resolve
      rejectRequest = reject
    }))
    const { composer, questions, unmount } = mountShared([actionAsk()])
    fireEvent.change(composer().getByTestId('waiting-on-you-note'), { target: { value: 'submitted note' } })
    fireEvent.click(composer().getByTestId('waiting-on-you-dismiss'))
    fireEvent.change(questions().getByTestId('waiting-on-you-note'), { target: { value: 'newer note' } })

    await act(async () => {
      if (outcome === 'dismissed') resolveRequest({ ok: true })
      else rejectRequest(refusal(409, 'cleared'))
    })

    expect(waitingDraftText(SLOT, actionAsk().ask_id)).toBe('newer note')
    unmount()
  })

  it('sends one message for a double click', async () => {
    let settle: (value: SendReceipt) => void = () => {}
    vi.mocked(sendTurn).mockReturnValue(new Promise(r => { settle = r }))
    mount([ask()])
    fireEvent.click(screen.getAllByTestId('waiting-on-you-option')[0])
    const send = screen.getByTestId('waiting-on-you-send')
    fireEvent.click(send)
    fireEvent.click(send)
    expect(sends()).toHaveLength(1)
    await act(async () => { settle(receipt('dispatched')) })
  })

  it('a retired ask with a typed note becomes its closed card in place, showing the note', () => {
    const { push } = mount([actionAsk()])
    const card = screen.getByTestId('waiting-on-you-card')
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: 'half typed' } })
    push([])
    const closed = screen.getByTestId('waiting-on-you-closed')
    expect(closed).toBe(card)
    expect(closed).toHaveTextContent('This ask was closed. Your note was not sent.')
    expect(closed).toHaveTextContent('Run the migration')
    expect(screen.getByTestId('waiting-on-you-closed-note')).toHaveTextContent('half typed')
    expect(screen.queryByTestId('waiting-on-you-card')).toBeNull()
    expect(sends()).toHaveLength(0)
  })

  it('a kept note can be discarded', () => {
    const { push } = mount([actionAsk()])
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: 'half typed' } })
    push([])
    fireEvent.click(screen.getByTestId('waiting-on-you-discard-draft'))
    expect(screen.queryByTestId('waiting-on-you-stack')).toBeNull()
  })

  it('dismisses an ask with its ask id', async () => {
    vi.mocked(api.dismissWaitingOnYou).mockResolvedValue({ ok: true })
    mount([ask()])
    fireEvent.click(screen.getByTestId('waiting-on-you-dismiss'))
    await waitFor(() => expect(screen.queryByTestId('waiting-on-you-card')).toBeNull())
    expect(api.dismissWaitingOnYou).toHaveBeenCalledWith(SLOT, 'scope-override', 'wy-0000000000000001')
  })

  it('a dismiss of an ask already gone hides it without an error', async () => {
    vi.mocked(api.dismissWaitingOnYou).mockRejectedValue(refusal(409, 'superseded'))
    mount([ask()])
    fireEvent.click(screen.getByTestId('waiting-on-you-dismiss'))
    await waitFor(() => expect(screen.queryByTestId('waiting-on-you-card')).toBeNull())
    expect(screen.queryByTestId('waiting-on-you-error')).toBeNull()
  })

  it('a failed dismiss keeps the card and says so', async () => {
    vi.mocked(api.dismissWaitingOnYou).mockRejectedValue(new TypeError('Failed to fetch'))
    mount([ask()])
    fireEvent.click(screen.getByTestId('waiting-on-you-dismiss'))
    expect(await screen.findByTestId('waiting-on-you-error')).toHaveTextContent('Could not dismiss this ask. Try again.')
    expect(screen.getByTestId('waiting-on-you-card')).toBeInTheDocument()
  })

  it('a note survives the stack remounting', () => {
    vi.mocked(api.viewerOwnerOk).mockReturnValue(new Promise(() => {}) as never)
    const first = mount([actionAsk()])
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: 'still here' } })
    first.unmount()
    const second = mount([actionAsk()])
    expect(screen.getByTestId('waiting-on-you-note')).toHaveValue('still here')
    second.unmount()
  })

  it('shows a collaborator who is not the owner the ask with no controls', async () => {
    vi.mocked(api.viewerOwnerOk).mockResolvedValue(false as never)
    mount([ask()])
    expect(await screen.findByTestId('waiting-on-you-read-only')).toHaveTextContent('Only the session owner can answer this.')
    expect(screen.queryByTestId('waiting-on-you-option')).toBeNull()
    expect(screen.queryByTestId('waiting-on-you-dismiss')).toBeNull()
  })
})
