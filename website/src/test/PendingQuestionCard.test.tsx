import { describe, it, expect, vi, beforeEach } from 'vitest'

import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { Provider } from 'react-redux'
/* Render framer-motion elements as plain DOM. jsdom cannot run the height
   animation, and a real AnimatePresence keeps the exiting body mounted for the
   duration of its exit transition — which would make "folded hides the options"
   pass or fail on timing rather than on behaviour. */
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  /* One component type per tag, cached. A proxy that minted a fresh type on
     every property read would give React a new element type each render, so it
     would unmount and remount the subtree — detaching any DOM node a test is
     holding and losing focus/caret for real users of this mock. */
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

import PendingQuestionCard from '../components/PendingQuestionCard'
import { createTestStore } from './helpers'
import { ApiError } from '../api/client'
import { api } from '../api/client'
import { setQuestionCard } from '../store/chatSlice'
import { reconcileQuestions, resolvedSince, staleAskIds } from '../hooks/useWebSocket'

/**
 * The submit round-trip for `ask_question` cards.
 *
 * Locks in the branch the backend tests cannot see: which of the two exits the
 * card takes. Reverting the `ask_id` branch to plain `send()` leaves the
 * reducer and backend suites green while stranding the blocked tool call, so
 * this is the only place that failure is visible.
 */

const QUESTIONS = [
  { question: 'Pick a trust model', options: [{ label: 'Carve-out' }, { label: 'Public only' }] },
]

const withCard = (askId?: string) =>
  createTestStore({
    chat: {
      activeSlot: 'chat-1',
      pendingQuestions: {
        'chat-1': { slot: 'chat-1', ...(askId ? { ask_id: askId } : {}), questions: QUESTIONS },
      },
    },
  } as never)

const renderCard = (store: ReturnType<typeof createTestStore>, onFallbackSend = vi.fn()) => {
  render(
    <Provider store={store}>
      <PendingQuestionCard slotKey="chat-1" onFallbackSend={onFallbackSend} />
    </Provider>,
  )
  return onFallbackSend
}

const pick = (label: string) => fireEvent.click(screen.getByText(label))
const submit = () => fireEvent.click(screen.getByText('Submit'))

const pendingOf = (store: ReturnType<typeof createTestStore>) =>
  store.getState().chat.pendingQuestions?.['chat-1']

describe('PendingQuestionCard — round 6 findings', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('locks the controls while a submission is in flight', async () => {
    let release: (v: unknown) => void = () => {}
    const answer = vi.spyOn(api, 'answerQuestion').mockReturnValue(
      new Promise((res) => { release = res }) as never,
    )
    const store = withCard('ask-1')
    renderCard(store)

    pick('Carve-out')
    submit()
    // A second click from one user intent must not produce a second call: the
    // first resolves the wait, the second 404s, and the 404 handler would then
    // send the answer AGAIN as a chat turn.
    submit()
    submit()
    expect(answer).toHaveBeenCalledTimes(1)

    const dismiss = screen.getByLabelText('Dismiss question without answering') as HTMLButtonElement
    expect(dismiss.disabled).toBe(true)
    release({ ok: true })
    await waitFor(() => expect(pendingOf(store)).toBeUndefined())
  })

  it('clears by ask_id, so a late response cannot erase a newer ask', async () => {
    let release: (v: unknown) => void = () => {}
    vi.spyOn(api, 'answerQuestion').mockReturnValue(
      new Promise((res) => { release = res }) as never,
    )
    const store = withCard('ask-OLD')
    const dispatched: string[] = []
    const realDispatch = store.dispatch.bind(store)
    // Asserting on the dispatched ACTION, not just the resulting state: both
    // clear paths can leave the same end state in a single-card slot, so only
    // the action distinguishes clear-by-ask_id from clear-by-slot.
    vi.spyOn(store, 'dispatch').mockImplementation(((a: { type: string }) => {
      dispatched.push(a.type)
      return realDispatch(a as never)
    }) as never)
    renderCard(store)

    pick('Carve-out')
    submit()

    // While ask-OLD's response is in flight, a newer ask replaces it in the very
    // same slot. Clearing by slot would wipe ask-NEW and leave it blocked with
    // no card until its own timeout.
    realDispatch(setQuestionCard({ slot: 'chat-1', ask_id: 'ask-NEW', questions: QUESTIONS }) as never)
    release({ ok: true })

    await waitFor(() => expect(dispatched).toContain('chat/resolveQuestionCard'))
    expect(dispatched).not.toContain('chat/clearQuestionCard')
    // ask-NEW must survive the late response for ask-OLD.
    expect(store.getState().chat.pendingQuestions?.['chat-1']?.ask_id).toBe('ask-NEW')
  })

  it('offers a dismiss control that unblocks the agent with no answer', async () => {
    const answer = vi.spyOn(api, 'answerQuestion').mockResolvedValue({ ok: true } as never)
    const store = withCard('ask-1')
    const onFallbackSend = renderCard(store)

    fireEvent.click(screen.getByLabelText('Dismiss question without answering'))

    // No answers argument -> the client sends {dismissed: true}, which unblocks
    // the caller with a timeout-equivalent result.
    expect(answer).toHaveBeenCalledWith('ask-1', undefined)
    await waitFor(() => expect(pendingOf(store)).toBeUndefined())
    expect(onFallbackSend).not.toHaveBeenCalled()
  })

  it('does not carry a legacy card\u2019s picks into the next legacy card in the same slot', () => {
    // A legacy card has no ask_id, so the QuestionCard key falls back to the slot
    // and a second stateless card in the same slot does NOT remount. Without a
    // reset, index-keyed state (picks, typed answers, folds) would carry over and
    // Submit would fire an answer the user never chose for THIS question.
    const store = withCard()
    renderCard(store)
    pick('Carve-out')
    expect((screen.getByText('Submit').closest('button') as HTMLButtonElement).disabled).toBe(false)

    act(() => {
      store.dispatch(setQuestionCard({
        slot: 'chat-1',
        questions: [{ question: 'Pick an environment', options: [{ label: 'staging' }, { label: 'prod' }] }],
      }))
    })

    expect((screen.getByText('Submit').closest('button') as HTMLButtonElement).disabled).toBe(true)
    // …and the new question is open, not inheriting a fold from the old one.
    expect(screen.getByText('staging')).toBeInTheDocument()
  })

  it('offers a dismiss control on a legacy card, which just takes it off screen', () => {
    const answer = vi.spyOn(api, 'answerQuestion')
    const store = withCard()
    const onFallbackSend = renderCard(store)

    // A legacy card blocks nothing, so there is no wait to resolve — but it is
    // still parked on top of the composer, so it MUST be removable. Withholding
    // the control left a card that could only be answered.
    fireEvent.click(screen.getByLabelText('Dismiss question without answering'))

    expect(answer).not.toHaveBeenCalled()
    expect(onFallbackSend).not.toHaveBeenCalled()
    expect(pendingOf(store)).toBeUndefined()
  })
})

describe('PendingQuestionCard — round 7 findings', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('re-enables the controls for the NEXT card after a successful submit', async () => {
    // ChatPane mounts this component
    // unconditionally (it returns null with no card but keeps its state), so a
    // lock left set after a successful submit disabled the pane's every later
    // card — an unanswerable card and a blocked agent, on the ordinary happy
    // path of answering one question and being asked a follow-up.
    vi.spyOn(api, 'answerQuestion').mockResolvedValue({ ok: true } as never)
    const store = withCard('ask-1')
    renderCard(store)

    pick('Carve-out')
    submit()
    await waitFor(() => expect(pendingOf(store)).toBeUndefined())

    // Same pane, next question.
    act(() => { store.dispatch(setQuestionCard({ slot: 'chat-1', ask_id: 'ask-2', questions: QUESTIONS })) })
    pick('Carve-out')
    const button = screen.getByText('Submit').closest('button') as HTMLButtonElement
    expect(button.disabled).toBe(false)
    const dismiss = screen.getByLabelText('Dismiss question without answering') as HTMLButtonElement
    expect(dismiss.disabled).toBe(false)
  })

  it('does not let an old response unlock a newer in-flight submission', async () => {
    let releaseOld: (v: unknown) => void = () => {}
    let releaseNew: (v: unknown) => void = () => {}
    const answer = vi.spyOn(api, 'answerQuestion')
      .mockReturnValueOnce(new Promise((res) => { releaseOld = res }) as never)
      .mockReturnValueOnce(new Promise((res) => { releaseNew = res }) as never)
    const store = withCard('ask-OLD')
    renderCard(store)

    pick('Carve-out')
    submit()
    act(() => {
      store.dispatch(setQuestionCard({ slot: 'chat-1', ask_id: 'ask-NEW', questions: QUESTIONS }))
    })
    pick('Carve-out')
    submit()
    expect(answer).toHaveBeenCalledTimes(2)

    // OLD settles after NEW is already in flight. Its finally handler must not
    // clear NEW's lock and allow a duplicate submission (whose eventual 404
    // would incorrectly become a fallback chat turn).
    await act(async () => { releaseOld({ ok: true }) })
    const button = screen.getByText('Submit').closest('button') as HTMLButtonElement
    expect(button.disabled).toBe(true)
    submit()
    expect(answer).toHaveBeenCalledTimes(2)

    await act(async () => { releaseNew({ ok: true }) })
    await waitFor(() => expect(pendingOf(store)).toBeUndefined())
  })

  it('does not carry the previous card\u2019s selections into the next one', async () => {
    vi.spyOn(api, 'answerQuestion').mockResolvedValue({ ok: true } as never)
    const store = withCard('ask-1')
    renderCard(store)

    pick('Carve-out')
    submit()
    await waitFor(() => expect(pendingOf(store)).toBeUndefined())

    act(() => { store.dispatch(setQuestionCard({ slot: 'chat-1', ask_id: 'ask-2', questions: QUESTIONS })) })
    // A fresh ask starts unanswered: inheriting the prior pick would let Submit
    // fire an answer the user never chose for THIS question.
    const button = screen.getByText('Submit').closest('button') as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('renders nothing for a prototype-polluting slot key', () => {
    // `map['__proto__']` returns an INHERITED object: truthy, but with no
    // `questions`, so the card would render and crash.
    const store = createTestStore({ chat: { activeSlot: '__proto__', pendingQuestions: {} } } as never)
    render(
      <Provider store={store}>
        <PendingQuestionCard slotKey="__proto__" onFallbackSend={vi.fn()} />
      </Provider>,
    )
    expect(screen.queryByText('Submit')).not.toBeInTheDocument()
  })
})

describe('PendingQuestionCard — ask_id round-trip', () => {
  beforeEach(() => { vi.restoreAllMocks() })

  it('answers through the endpoint and clears the card, without sending a message', async () => {
    const answer = vi.spyOn(api, 'answerQuestion').mockResolvedValue({ ok: true } as never)
    const store = withCard('ask-1')
    const onFallbackSend = renderCard(store)

    pick('Carve-out')
    submit()

    expect(answer).toHaveBeenCalledWith('ask-1', { 'Pick a trust model': 'Carve-out' })
    await waitFor(() => expect(pendingOf(store)).toBeUndefined())
    // Critical: a message here would start a second turn the tool cannot join.
    expect(onFallbackSend).not.toHaveBeenCalled()
  })

  it('keeps the card on a retryable failure so the answer can be retried', async () => {
    vi.spyOn(api, 'answerQuestion').mockRejectedValue(new ApiError(500, 'boom'))
    const store = withCard('ask-1')
    const onFallbackSend = renderCard(store)

    pick('Carve-out')
    submit()

    // The agent is still blocked, so the card must survive and no second turn
    // may start.
    await waitFor(() => expect(onFallbackSend).not.toHaveBeenCalled())
    expect(pendingOf(store)).toBeDefined()
  })

  it('falls back to a message only when the wait is provably gone (404)', async () => {
    vi.spyOn(api, 'answerQuestion').mockRejectedValue(new ApiError(404, 'no pending question'))
    const store = withCard('ask-1')
    const onFallbackSend = renderCard(store)

    pick('Public only')
    submit()

    await waitFor(() => expect(onFallbackSend).toHaveBeenCalledWith('Public only'))
    expect(pendingOf(store)).toBeUndefined()
  })

  it('sends a legacy card (no ask_id) as an ordinary message', () => {
    const answer = vi.spyOn(api, 'answerQuestion')
    const store = withCard()
    const onFallbackSend = renderCard(store)

    pick('Carve-out')
    submit()

    expect(answer).not.toHaveBeenCalled()
    expect(onFallbackSend).toHaveBeenCalledWith('Carve-out')
    expect(pendingOf(store)).toBeUndefined()
  })

  it('renders nothing when the slot has no pending card', () => {
    render(
      <Provider store={createTestStore()}>
        <PendingQuestionCard slotKey="chat-1" onFallbackSend={vi.fn()} />
      </Provider>,
    )
    expect(screen.queryByText('Submit')).not.toBeInTheDocument()
  })
})

describe('QuestionCard — every question must be answered', () => {
  it('keeps Submit disabled until all questions have an answer', () => {
    const store = createTestStore({
      chat: {
        activeSlot: 'chat-1',
        pendingQuestions: {
          'chat-1': {
            slot: 'chat-1',
            ask_id: 'ask-2',
            questions: [
              { question: 'Trust model', options: [{ label: 'Carve-out' }] },
              { question: 'Environments', options: [{ label: 'staging' }] },
            ],
          },
        },
      },
    } as never)
    renderCard(store)

    const button = screen.getByText('Submit').closest('button') as HTMLButtonElement
    expect(button.disabled).toBe(true)

    // One of two answered is still incomplete: submitting here would resume the
    // agent with a map missing an entry it asked for.
    pick('Carve-out')
    expect(button.disabled).toBe(true)

    pick('staging')
    expect(button.disabled).toBe(false)
  })
})

describe('staleAskIds — reconnect reconciliation', () => {
  it('reports a card the server no longer lists as pending', () => {
    // Resolved while the socket was down: the `question_card_resolved` broadcast
    // was missed, so without this the dead card stays clickable on screen.
    const current = { 'chat-1': { ask_id: 'gone' }, 'chat-2': { ask_id: 'live' } }
    expect(staleAskIds(current, [{ ask_id: 'live' }])).toEqual(['gone'])
  })

  it('reports nothing when every local card is still pending', () => {
    const current = { 'chat-1': { ask_id: 'a' }, 'chat-2': { ask_id: 'b' } }
    expect(staleAskIds(current, [{ ask_id: 'a' }, { ask_id: 'b' }])).toEqual([])
  })

  it('never reports a legacy card, which the server does not track', () => {
    // A legacy AskUserQuestion card has no ask_id and no server-side record, so
    // its absence from the response is not evidence it is stale — dropping it
    // would delete a card the user is mid-way through.
    expect(staleAskIds({ 'chat-1': { ask_id: undefined } }, [])).toEqual([])
    expect(staleAskIds({ 'chat-1': undefined }, [])).toEqual([])
  })

  it('tolerates absent state', () => {
    expect(staleAskIds(undefined, [{ ask_id: 'a' }])).toEqual([])
  })
})

describe('reconcileQuestions — reconnect race (GPT BLOCKING, round 7)', () => {
  const card = (askId: string) => ({ ask_id: askId, slot: 'chat-1', questions: QUESTIONS })

  it('never drops a card that arrived DURING the fetch', () => {
    // The response describes the server before the new
    // card existed. Reconciling against post-fetch state would delete it and
    // leave the agent blocked until its timeout.
    const before = {}
    const after = { 'chat-1': { ask_id: 'arrived-mid-fetch' } }
    const { drop, add } = reconcileQuestions(before, after, [])
    expect(drop).toEqual([])
    expect(add).toEqual([])
  })

  it('still drops a card that was already local and is gone server-side', () => {
    const map = { 'chat-1': { ask_id: 'dead' } }
    expect(reconcileQuestions(map, map, []).drop).toEqual(['dead'])
  })

  it('does not re-add a card resolved by a WS event during the fetch', () => {
    // The response still lists it, but it is already dead locally: resurrecting
    // it would show a card whose submit can only 404.
    const before = { 'chat-1': { ask_id: 'resolved-mid-fetch' } }
    const after = {}
    const { add } = reconcileQuestions(before, after, [card('resolved-mid-fetch')])
    expect(add).toEqual([])
  })

  it('adds a genuinely pending card the client missed', () => {
    const { add, drop } = reconcileQuestions({}, {}, [card('missed')])
    expect(add.map((q) => q.ask_id)).toEqual(['missed'])
    expect(drop).toEqual([])
  })

  it('skips malformed rows rather than dispatching an empty card', () => {
    const rows = [
      { ask_id: 'no-slot', slot: '', questions: QUESTIONS },
      { ask_id: 'no-questions', slot: 'chat-1', questions: [] },
    ]
    expect(reconcileQuestions({}, {}, rows).add).toEqual([])
  })
})

describe('resolvedSince + reconcileQuestions — resolution for a never-held card', () => {
  const card = (askId: string) => ({ ask_id: askId, slot: 'chat-1', questions: QUESTIONS })

  it('does not re-add a card resolved during the fetch that was never local', () => {
    // The third race variant: local state is empty, the GET snapshots X, another
    // owner session resolves X before the response lands. The WS resolution
    // dispatch is a no-op (nothing to remove), so before/after cannot see it —
    // only the observed-resolution log can.
    const log = new Map<string, number>()
    const watermark = 0
    log.set('X', 1) // arrived while the fetch was in flight
    const { add } = reconcileQuestions({}, {}, [card('X')], resolvedSince(log, watermark))
    expect(add).toEqual([])
  })

  it('ignores resolutions that predate the fetch', () => {
    // Already accounted for before this reconcile began: an older resolution must
    // not suppress a genuinely pending card that the server still lists.
    const log = new Map<string, number>([['X', 1]])
    const { add } = reconcileQuestions({}, {}, [card('X')], resolvedSince(log, 1))
    expect(add.map((q) => q.ask_id)).toEqual(['X'])
  })

  it('resolvedSince returns only ids past the watermark', () => {
    const log = new Map<string, number>([['a', 1], ['b', 2], ['c', 3]])
    expect(resolvedSince(log, 1).sort()).toEqual(['b', 'c'])
    expect(resolvedSince(log, 3)).toEqual([])
  })
})

/**
 * Reconnect re-dispatch must not churn the card. syncPendingQuestions
 * re-dispatches the SAME still-pending card with a freshly parsed
 * (structurally equal, referentially new) payload on every websocket
 * reconnect; the reducer keeps the entry and QuestionCard compares the
 * serialized payload, so the user's typed custom answer survives on screen.
 */
describe('reconnect re-dispatch', () => {
  const typeCustomAnswer = (text: string) => {
    const input = screen.getByPlaceholderText(/type a custom answer/i)
    fireEvent.change(input, { target: { value: text } })
  }

  it('a reconnect re-dispatch of the same card keeps the typed text on screen', () => {
    const store = withCard()
    renderCard(store)
    typeCustomAnswer('still typing')
    act(() => {
      store.dispatch(setQuestionCard({ slot: 'chat-1', questions: JSON.parse(JSON.stringify(QUESTIONS)) }))
    })
    expect(store.getState().chat.pendingQuestions['chat-1']).toBeDefined()
    expect(screen.getByDisplayValue('still typing')).toBeTruthy()
  })

  it('user dismiss removes the card', () => {
    const store = withCard() // stateless card — dismiss is a pure local clear
    renderCard(store)
    typeCustomAnswer('abandoned on purpose')
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(store.getState().chat.pendingQuestions['chat-1']).toBeUndefined()
  })
})
