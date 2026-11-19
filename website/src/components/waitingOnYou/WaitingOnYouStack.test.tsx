/** The composer's stack of pinned asks: what it shows, what each click sends,
 *  and what each refusal of the answer and dismiss routes leaves on screen. */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import { api } from '../../api/client'
import { ApiError } from '../../api/apiError'
import type { WaitingOnYouAsk } from '../../types'
import WaitingOnYouStack from './WaitingOnYouStack'
import { __resetAnswerIdsForTests } from './answerIds'
import { __resetWaitingDraftsForTests } from './drafts'

vi.mock('../../api/client', () => ({
  api: { answerWaitingOnYou: vi.fn(), dismissWaitingOnYou: vi.fn(), viewerOwnerOk: vi.fn() },
}))

const SLOT = 'chat-1'
const ANSWER_ID = /^[A-Za-z0-9_-]{8,64}$/

function ask(over: Partial<WaitingOnYouAsk> = {}): WaitingOnYouAsk {
  return {
    key: 'scope-override',
    ask_id: 'wy-0000000000000001',
    title: 'Post the Security Scope override on #38289',
    detail: '',
    options: [{ label: 'I posted it', description: 'I re-check the lane' }, { label: 'Hold the override', description: '' }],
    ts: 1795550000,
    state: 'pinned',
    ...over,
  }
}

const actionAsk = (over: Partial<WaitingOnYouAsk> = {}) => ask({ key: 'run-migration', ask_id: 'wy-00000000000000a1', title: 'Run the migration', options: [], ...over })

function refusal(status: number, code: string, error = ''): ApiError {
  return new ApiError(status, code, JSON.stringify({ ok: false, code, ...(error ? { error } : {}) }))
}

function mount(asks: WaitingOnYouAsk[], onReturn = vi.fn()) {
  const ui = (next: readonly WaitingOnYouAsk[]) => <WaitingOnYouStack slotKey={SLOT} asks={next} agent="kirocrew" onReturnToComposer={onReturn} />
  const view = renderWithProviders(ui(asks))
  // The slot push: the caller re-renders the stack with the row's new asks.
  const push = (next: WaitingOnYouAsk[]) => act(() => { view.rerender(ui(next)) })
  return { ...view, onReturn, push }
}

const answerCalls = () => vi.mocked(api.answerWaitingOnYou).mock.calls
const chooseAndSend = (index = 0) => {
  fireEvent.click(screen.getAllByTestId('waiting-on-you-option')[index])
  fireEvent.click(screen.getByTestId('waiting-on-you-send'))
}

beforeEach(() => {
  vi.mocked(api.viewerOwnerOk).mockResolvedValue(true as never)
  vi.setSystemTime(new Date(1795550000 * 1000 + 3 * 3600 * 1000))
})

afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
  __resetAnswerIdsForTests()
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
    vi.mocked(api.answerWaitingOnYou).mockResolvedValue({ ok: true, outcome: 'sent', answer_id: 'x' })
    const options = Array.from({ length: 6 }, (_, i) => ({ label: `Choice ${i + 1}`, description: '' }))
    mount([ask({ options })])

    const list = screen.getByTestId('waiting-on-you-options')
    expect(list).toHaveClass('flex-col')
    expect(within(list).getAllByTestId('waiting-on-you-option')).toHaveLength(6)
    expect(screen.getByTestId('waiting-on-you-send')).toBeDisabled()

    fireEvent.click(within(list).getByText('Choice 5'))
    expect(answerCalls()).toHaveLength(0)
    expect(within(list).getByText('Choice 5').closest('button')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByTestId('waiting-on-you-send'))

    await waitFor(() => expect(answerCalls()).toHaveLength(1))
    expect(answerCalls()[0][2]).toEqual(expect.objectContaining({ label: 'Choice 5' }))
  })

  it('renders nothing for a slot with no asks', () => {
    mount([])
    expect(screen.queryByTestId('waiting-on-you-stack')).toBeNull()
  })

  it('shows the newest ask in full and expands the older ones in place', () => {
    mount([ask({ key: 'a', ask_id: 'wy-a', title: 'First' }), ask({ key: 'b', ask_id: 'wy-b', title: 'Second' }), ask({ key: 'c', ask_id: 'wy-c', title: 'Third' })])
    expect(screen.getAllByTestId('waiting-on-you-title').map(e => e.textContent)).toEqual(['Third'])
    const more = screen.getByTestId('waiting-on-you-more')
    expect(more).toHaveTextContent('+2 more')
    fireEvent.click(more)
    expect(screen.getAllByTestId('waiting-on-you-title').map(e => e.textContent)).toEqual(['First', 'Second', 'Third'])
    expect(screen.getByTestId('waiting-on-you-more')).toHaveTextContent('Show fewer')
  })

  it('answers a decision card with the exact label and a fresh answer id', async () => {
    vi.mocked(api.answerWaitingOnYou).mockResolvedValue({ ok: true, outcome: 'sent', answer_id: 'x' })
    mount([ask()])
    chooseAndSend(1)
    await waitFor(() => expect(screen.queryByTestId('waiting-on-you-card')).toBeNull())
    const [slot, key, body] = answerCalls()[0]
    expect([slot, key]).toEqual([SLOT, 'scope-override'])
    expect(body).toEqual({ ask_id: 'wy-0000000000000001', answer_id: expect.stringMatching(ANSWER_ID), label: 'Hold the override' })
  })

  it('answers an action card with Done and the typed note, or an empty note', async () => {
    vi.mocked(api.answerWaitingOnYou).mockResolvedValue({ ok: true, outcome: 'queued', answer_id: 'x' })
    mount([actionAsk(), actionAsk({ key: 'second', ask_id: 'wy-00000000000000a2' })])
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: '  ran it on prod  ' } })
    fireEvent.click(screen.getByTestId('waiting-on-you-done'))
    await waitFor(() => expect(answerCalls()).toHaveLength(1))
    expect(answerCalls()[0][2]).toEqual({ ask_id: 'wy-00000000000000a2', answer_id: expect.stringMatching(ANSWER_ID), text: 'ran it on prod' })
    await waitFor(() => expect(screen.getByTestId('waiting-on-you-title')).toHaveTextContent('Run the migration'))
    fireEvent.click(screen.getByTestId('waiting-on-you-done'))
    await waitFor(() => expect(answerCalls()).toHaveLength(2))
    expect(answerCalls()[1][2]).toEqual({ ask_id: 'wy-00000000000000a1', answer_id: expect.stringMatching(ANSWER_ID), text: '' })
  })

  it('reuses the answer id when the same click is retried after a failure', async () => {
    vi.mocked(api.answerWaitingOnYou).mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce({ ok: true, outcome: 'sent', answer_id: 'x' })
    mount([ask()])
    chooseAndSend()
    expect(await screen.findByTestId('waiting-on-you-error')).toHaveTextContent('Your answer did not reach the session. Try again.')
    fireEvent.click(screen.getByTestId('waiting-on-you-send'))
    await waitFor(() => expect(answerCalls()).toHaveLength(2))
    expect(answerCalls()[1][2].answer_id).toBe(answerCalls()[0][2].answer_id)
  })

  it('sends one request for a double click', async () => {
    let settle: (v: unknown) => void = () => {}
    vi.mocked(api.answerWaitingOnYou).mockReturnValue(new Promise(r => { settle = r }) as never)
    mount([ask()])
    const option = screen.getAllByTestId('waiting-on-you-option')[0]
    fireEvent.click(option)
    const send = screen.getByTestId('waiting-on-you-send')
    fireEvent.click(send)
    fireEvent.click(send)
    expect(answerCalls()).toHaveLength(1)
    await act(async () => { settle({ ok: true, outcome: 'sent', answer_id: 'x' }) })
  })

  it('409 answered: shows "Answered elsewhere" and drops the label', async () => {
    vi.mocked(api.answerWaitingOnYou).mockRejectedValue(refusal(409, 'answered'))
    const { onReturn } = mount([ask()])
    chooseAndSend()
    expect(await screen.findByTestId('waiting-on-you-stale')).toHaveTextContent('Answered elsewhere.')
    await waitFor(() => expect(screen.queryByTestId('waiting-on-you-card')).toBeNull())
    expect(onReturn).not.toHaveBeenCalled()
  })

  it.each([
    ['superseded', 'The agent replaced this ask.'],
    ['cleared', 'The agent withdrew this ask.'],
  ])('409 %s: says so and puts the label in the composer, unsent', async (code, sentence) => {
    vi.mocked(api.answerWaitingOnYou).mockRejectedValue(refusal(409, code))
    const { onReturn } = mount([ask()])
    chooseAndSend()
    const notice = await screen.findByTestId('waiting-on-you-stale')
    expect(notice).toHaveTextContent(sentence)
    expect(notice).toHaveTextContent('Your answer is in the message box. Nothing was sent.')
    expect(onReturn).toHaveBeenCalledWith('I posted it')
    expect(answerCalls()).toHaveLength(1)
  })

  it.each(['answered', 'superseded', 'cleared'])('409 %s: the card being read becomes its notice in place, before and after the push', async code => {
    vi.mocked(api.answerWaitingOnYou).mockRejectedValue(refusal(409, code))
    const { push } = mount([ask()])
    const card = screen.getByTestId('waiting-on-you-card')
    chooseAndSend()
    expect(await screen.findByTestId('waiting-on-you-stale')).toBe(card)
    push([])
    expect(screen.getByTestId('waiting-on-you-stale')).toBe(card)
  })

  it('a retired ask with a typed note becomes its closed card in place', () => {
    const { push } = mount([actionAsk()])
    const card = screen.getByTestId('waiting-on-you-card')
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: 'half typed' } })
    push([])
    expect(screen.getByTestId('waiting-on-you-closed')).toBe(card)
    expect(screen.queryByTestId('waiting-on-you-card')).toBeNull()
  })

  it('409 uncertain: keeps the card, warns, and the next click takes a new answer id', async () => {
    vi.mocked(api.answerWaitingOnYou).mockRejectedValueOnce(refusal(409, 'uncertain'))
      .mockResolvedValueOnce({ ok: true, outcome: 'sent', answer_id: 'x' })
    mount([ask()])
    chooseAndSend()
    expect(await screen.findByTestId('waiting-on-you-uncertain')).toHaveTextContent('Your answer may already have been sent. Check the chat above.')
    fireEvent.click(screen.getByTestId('waiting-on-you-send'))
    await waitFor(() => expect(answerCalls()).toHaveLength(2))
    expect(answerCalls()[1][2].answer_id).not.toBe(answerCalls()[0][2].answer_id)
  })

  it('shows the uncertain warning for an ask the gateway restored as uncertain', () => {
    mount([ask({ state: 'uncertain' })])
    expect(screen.getByTestId('waiting-on-you-uncertain')).toBeInTheDocument()
  })

  it('a refused send keeps the ask pinned and shows the gateway reason', async () => {
    vi.mocked(api.answerWaitingOnYou).mockRejectedValue(refusal(409, 'crew_paused', 'The crew is paused.'))
    const { onReturn } = mount([ask()])
    chooseAndSend()
    expect(await screen.findByTestId('waiting-on-you-error')).toHaveTextContent('The session did not take the answer, so the ask stays pinned. The crew is paused.')
    expect(screen.getByTestId('waiting-on-you-card')).toBeInTheDocument()
    expect(onReturn).not.toHaveBeenCalled()
  })

  it('400 invalid_answer keeps the card and says the option is gone', async () => {
    vi.mocked(api.answerWaitingOnYou).mockRejectedValue(refusal(400, 'invalid_answer', 'label not among options'))
    mount([ask()])
    chooseAndSend()
    expect(await screen.findByTestId('waiting-on-you-error')).toHaveTextContent('That answer is no longer one of the options.')
    expect(screen.getByTestId('waiting-on-you-card')).toBeInTheDocument()
  })

  it('dismisses an ask with its ask id', async () => {
    vi.mocked(api.dismissWaitingOnYou).mockResolvedValue({ ok: true })
    mount([ask()])
    fireEvent.click(screen.getByTestId('waiting-on-you-dismiss'))
    await waitFor(() => expect(screen.queryByTestId('waiting-on-you-card')).toBeNull())
    expect(api.dismissWaitingOnYou).toHaveBeenCalledWith(SLOT, 'scope-override', 'wy-0000000000000001')
  })

  it('a failed dismiss keeps the card and says so', async () => {
    vi.mocked(api.dismissWaitingOnYou).mockRejectedValue(new TypeError('Failed to fetch'))
    mount([ask()])
    fireEvent.click(screen.getByTestId('waiting-on-you-dismiss'))
    expect(await screen.findByTestId('waiting-on-you-error')).toHaveTextContent('Could not dismiss this ask. Try again.')
    expect(screen.getByTestId('waiting-on-you-card')).toBeInTheDocument()
  })

  it('keeps a typed note when the ask is retired, and hands it to the composer', () => {
    const { push, onReturn } = mount([actionAsk()])
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: 'half typed' } })
    push([])
    const closed = screen.getByTestId('waiting-on-you-closed')
    expect(closed).toHaveTextContent('This ask was closed. Your note was not sent.')
    expect(closed).toHaveTextContent('Run the migration')
    fireEvent.click(screen.getByTestId('waiting-on-you-copy-draft'))
    expect(onReturn).toHaveBeenCalledWith('half typed')
    expect(screen.queryByTestId('waiting-on-you-stack')).toBeNull()
    expect(answerCalls()).toHaveLength(0)
  })

  it('a kept note can be discarded instead', () => {
    const { push, onReturn } = mount([actionAsk()])
    fireEvent.change(screen.getByTestId('waiting-on-you-note'), { target: { value: 'half typed' } })
    push([])
    fireEvent.click(screen.getByTestId('waiting-on-you-discard-draft'))
    expect(screen.queryByTestId('waiting-on-you-stack')).toBeNull()
    expect(onReturn).not.toHaveBeenCalled()
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
