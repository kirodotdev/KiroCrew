/**
 * The session summary panel's rendering contracts.
 *
 * Four of these guard states a reader will actually hit before the feature is
 * fully rolled out — off, not-yet-generated, stale, failed — because each one
 * has to explain itself rather than look broken. The rest guard the design
 * decisions that a refactor could quietly undo: recency ordering, one derived
 * state word, the asked-versus-inferred distinction, and persisted disclosure.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'

import SessionSummaryTab from '../pages/chat/SessionSummaryTab'
import { createTestStore } from './helpers'
import { setActiveSlot, setSlotState } from '../store/chatSlice'
import { ApiError } from '../api/client'
import type { SessionSummary, SessionIntent } from '../types/sessionSummary'

const sessionSummary = vi.fn()
const generateSessionSummary = vi.fn()
vi.mock('../api/client', () => ({
  // A real-shaped class, not a stub: the generate failure path narrows with
  // `e instanceof ApiError` before it may read `.body`, so a bare object would
  // make every rejection fall to the generic message and the code mapping would
  // never be exercised.
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
  api: {
    sessionSummary: (slot: string) => sessionSummary(slot),
    generateSessionSummary: (slot: string) => generateSessionSummary(slot),
  },
}))

function intent(over: Partial<SessionIntent> = {}): SessionIntent {
  return {
    title: 'a goal',
    initial_intent: '',
    progress: [],
    next_steps: [],
    ranges: [[1, 1]],
    status: 'active',
    verified: null,
    state: 'in-progress',
    last_touched_turn: 1,
    origin_turn: null,
    ...over,
  }
}

function payload(over: Partial<SessionSummary> = {}): SessionSummary {
  return {
    enabled: true,
    stale: false,
    intents: [],
    constraints: [],
    generated_at: null,
    user_turns: null,
    last_activity: null,
    ...over,
  }
}

/** SlotState is not exported from chatSlice, so the union is restated here.
 *  Keep it in step with `SlotState` in `website/src/store/chatSlice.ts`. */
type StreamState = 'idle' | 'streaming' | 'tool_running' | 'stopping' | 'compacting'

/** The panel subscribes to the store for the live-turn signal
 *  (`selectSlotStreamState`), so a Provider is part of the harness rather than an
 *  extra for one test — without it every case in this file unmounts on
 *  "could not find react-redux context value". `streamState` is dispatched
 *  through the real reducer (setActiveSlot, then setSlotState — the former resets
 *  the latter to idle) so the selector is exercised as it runs in the app. */
function mount(slot = 'chat-1', streamState: StreamState = 'idle') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = createTestStore()
  store.dispatch(setActiveSlot(slot))
  if (streamState !== 'idle') store.dispatch(setSlotState(streamState))
  return render(
    <Provider store={store}>
      <QueryClientProvider client={client}>
        <SessionSummaryTab slot={slot} />
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  sessionSummary.mockReset()
  generateSessionSummary.mockReset()
  localStorage.clear()
})

afterEach(() => {
  localStorage.clear()
})

describe('empty and error states', () => {
  it('explains itself when the feature is switched off', async () => {
    // Not an error: the Settings toggle ships in a later change, so a reader
    // who opens the tab before enabling it needs a reason, not a failure.
    sessionSummary.mockResolvedValue(payload({ enabled: false }))
    mount()
    expect(await screen.findByText(/summaries are off/i)).toBeTruthy()
  })

  it('says no summary yet rather than showing an empty shell', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [] }))
    mount()
    expect(await screen.findByText(/no summary yet/i)).toBeTruthy()
  })

  it('tells the reader what actually produces a summary', async () => {
    // "Wait and it appears" is false for an idle or historical session: the
    // trigger is a turn finishing, so the copy has to name that or the reader is
    // left pressing a refresh that cannot succeed.
    sessionSummary.mockResolvedValue(payload({ intents: [] }))
    mount()
    await screen.findByText(/no summary yet/i)
    expect(screen.getByText(/when a turn finishes/i)).toBeTruthy()
    expect(screen.getByText(/send a message/i)).toBeTruthy()
  })

  it('offers a working refresh while waiting for the first summary', async () => {
    // This state resolves on its own when a turn ends, so it is the one a reader
    // sits in. Without a control here the only way to check is reloading the
    // whole dashboard.
    sessionSummary.mockResolvedValue(payload({ intents: [] }))
    mount()
    await screen.findByText(/no summary yet/i)

    const refresh = screen.getByRole('button', { name: /check again/i })
    sessionSummary.mockResolvedValue(payload({ intents: [intent({ title: 'now here' })] }))
    fireEvent.click(refresh)

    // Refetches, not just present.
    expect(await screen.findByText('now here')).toBeTruthy()
  })

  it('reports a load failure with a way to recover from it', async () => {
    sessionSummary.mockRejectedValue(new Error('boom'))
    mount()
    expect(await screen.findByText('Could not load the summary')).toBeTruthy()
    // The body explains rather than repeating the title.
    expect(screen.getByText(/try again, or reload the page/i)).toBeTruthy()
    // The failure branch returns before the header renders, so its own Retry is
    // the ONLY control that can recover the panel. Asserting the message alone
    // passed happily while the state was a dead end.
    const retry = screen.getByRole('button', { name: /try again/i })
    expect(retry).toBeTruthy()

    // And it must actually refetch, not merely exist.
    sessionSummary.mockResolvedValue(payload({ intents: [intent({ title: 'recovered' })] }))
    fireEvent.click(retry)
    expect(await screen.findByText('recovered')).toBeTruthy()
  })

  it('serves a stale summary AND flags it, rather than withholding it', async () => {
    // An empty panel reads as "broken"; a stale one reads as "not regenerated
    // yet", which is the truth.
    sessionSummary.mockResolvedValue(
      // `generated_at` is set because `stale` MEANS a stored summary whose
      // transcript moved on — null is reserved for "never generated", where
      // staleness has no meaning.
      payload({
        stale: true,
        generated_at: 1786561786,
        intents: [intent({ title: 'still useful' })],
      }),
    )
    mount()
    expect(await screen.findByText('still useful')).toBeTruthy()
    // ONE freshness line, in the footer: the timestamp and the behind-ness are
    // the same sentence, so they cannot disagree across two corners.
    expect(screen.getByText(/behind the conversation/i)).toBeTruthy()
  })
})

describe('on-demand generation', () => {
  /** The empty panel has THREE causes and only one is actionable, so each state
   *  is pinned on its own copy AND on the affordance it does or does not offer.
   *  Asserting the copy alone would let two states collapse into one button. */
  const GENERATE = /^summarize$/i
  const CHECK_AGAIN = /check again/i

  it('offers the generate button when the server says ready', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    mount()
    await screen.findByText(/no summary yet/i)

    expect(screen.getByText(/has not been summarized yet/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: GENERATE })).toBeTruthy()
    // And it must NOT read as the free refresh this panel shipped with.
    expect(screen.queryByRole('button', { name: CHECK_AGAIN })).toBeNull()
  })

  it('says there is not enough to summarize, and offers no button at all', async () => {
    // A disabled button invites hunting for the thing that would enable it, and
    // an enabled one could only fail. The only honest affordance is a sentence.
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'too_few_turns' }))
    mount()
    await screen.findByText(/no summary yet/i)

    expect(screen.getByText(/not enough here to summarize/i)).toBeTruthy()
    // Absence, asserted over the whole branch: this state renders before the
    // header, so zero buttons is the real contract, not "no generate button".
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryByText(/has not been summarized yet/i)).toBeNull()
  })

  it('degrades to the read-only Check again state when generate_state is absent', async () => {
    // The backwards-compatible path: a gateway that predates the POST route
    // sends no verdict, and a button the backend cannot serve is worse than the
    // read-only panel this replaced.
    sessionSummary.mockResolvedValue(payload({ intents: [] }))
    mount()
    await screen.findByText(/no summary yet/i)

    expect(screen.getByText(/when a turn finishes/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: GENERATE })).toBeNull()
    expect(screen.queryByText(/uses tokens for one pass/i)).toBeNull()

    // And the fallback control still refetches rather than merely existing.
    sessionSummary.mockResolvedValue(payload({ intents: [intent({ title: 'older gateway' })] }))
    fireEvent.click(screen.getByRole('button', { name: CHECK_AGAIN }))
    expect(await screen.findByText('older gateway')).toBeTruthy()
    expect(generateSessionSummary).not.toHaveBeenCalled()
  })

  it('degrades the same way when the server says unavailable', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'unavailable' }))
    mount()
    await screen.findByText(/no summary yet/i)

    expect(screen.getByText(/when a turn finishes/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: CHECK_AGAIN })).toBeTruthy()
    expect(screen.queryByRole('button', { name: GENERATE })).toBeNull()
  })

  it('summarizes once on click, then renders what the refetch brings back', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    generateSessionSummary.mockResolvedValue(payload({ intents: [] }))
    mount()
    await screen.findByText(/no summary yet/i)
    expect(sessionSummary).toHaveBeenCalledTimes(1)

    // The POST's own body is deliberately NOT written into the cache — the GET
    // is the one shape the panel reads — so the summary must arrive via refetch.
    sessionSummary.mockResolvedValue(payload({ intents: [intent({ title: 'freshly summarized' })] }))
    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    expect(await screen.findByText('freshly summarized')).toBeTruthy()
    // Exactly once: a double-POST spends the person's tokens twice.
    expect(generateSessionSummary).toHaveBeenCalledTimes(1)
    expect(generateSessionSummary).toHaveBeenCalledWith('chat-1')
    expect(sessionSummary).toHaveBeenCalledTimes(2)
  })

  it('names the work and disables the button while the pass is running', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    let finish: (v: SessionSummary) => void = () => {}
    generateSessionSummary.mockImplementation(
      () => new Promise<SessionSummary>(res => { finish = res }),
    )
    mount()
    await screen.findByText(/no summary yet/i)

    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    // In flight the label switches to the progressive form and the control
    // locks, which is what stops a second paid pass being queued behind this one.
    const busy = await screen.findByRole('button', { name: /summarizing…/i })
    expect(busy).toBeDisabled()
    expect(screen.queryByRole('button', { name: GENERATE })).toBeNull()

    finish(payload({ intents: [] }))
    // Settles back to an offered, enabled button rather than staying locked.
    await waitFor(() => expect(screen.getByRole('button', { name: GENERATE })).toBeEnabled())
    expect(generateSessionSummary).toHaveBeenCalledTimes(1)
  })

  it('says a pass is already running when the backend rejects with summary_in_flight', async () => {
    // The distinction is the point: "already running" means wait, while the
    // generic failure means try again. One message for both teaches the person
    // to retry a pass that is already spending their tokens.
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    generateSessionSummary.mockRejectedValue(
      new ApiError(409, 'conflict', JSON.stringify({ code: 'summary_in_flight' })),
    )
    mount()
    await screen.findByText(/no summary yet/i)
    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    expect(await screen.findByText(/already being written/i)).toBeTruthy()
    expect(screen.queryByText(/could not summarize this session/i)).toBeNull()
    // The failure is recoverable, so the button comes back.
    expect(screen.getByRole('button', { name: GENERATE })).toBeEnabled()
  })

  it('keeps a generated summary when the reconciling refetch fails', async () => {
    // The person has already paid for this pass, so the summary must not depend
    // on a second network call succeeding. Without seeding the cache from the
    // POST's own body, a failed refetch leaves the query in its error state and
    // the panel says "could not load the summary" about a summary that exists.
    sessionSummary.mockResolvedValueOnce(payload({ intents: [], generate_state: 'ready' }))
    generateSessionSummary.mockResolvedValue(
      payload({ intents: [intent({ title: 'paid for and kept' })] }),
    )
    mount()
    await screen.findByText(/no summary yet/i)

    // The reconciling read fails right after the POST succeeds.
    sessionSummary.mockRejectedValue(new Error('network gone'))
    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    expect(await screen.findByText(/paid for and kept/i)).toBeTruthy()
    expect(screen.queryByText(/could not load the summary/i)).toBeNull()
  })

  it('re-reads server state after a refusal, so the button stops offering a rejected action', async () => {
    // A refusal is news about the server, not just about this click. If the
    // feature was switched off while the panel sat here, refetching only on
    // success would leave an enabled button whose action the backend now
    // rejects -- the person would click it again and get the same failure.
    sessionSummary.mockResolvedValueOnce(payload({ intents: [], generate_state: 'ready' }))
    generateSessionSummary.mockRejectedValue(
      new ApiError(409, 'conflict', JSON.stringify({ code: 'summary_disabled' })),
    )
    mount()
    await screen.findByText(/no summary yet/i)
    expect(sessionSummary).toHaveBeenCalledTimes(1)

    // The re-read reports the feature as off, which is the state that arrived
    // while this panel was open.
    sessionSummary.mockResolvedValue(payload({ enabled: false }))
    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    await waitFor(() => expect(sessionSummary).toHaveBeenCalledTimes(2))
    // The panel now shows the off state rather than a stale Summarize button.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: GENERATE })).toBeNull(),
    )
  })

  it('shows the generic failure for any other backend code', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    generateSessionSummary.mockRejectedValue(
      new ApiError(503, 'nope', JSON.stringify({ code: 'summary_unavailable' })),
    )
    mount()
    await screen.findByText(/no summary yet/i)
    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    expect(await screen.findByText(/could not summarize this session/i)).toBeTruthy()
    expect(screen.queryByText(/already being written/i)).toBeNull()
  })

  it('shows the generic failure when the rejection carries no code at all', async () => {
    // A network-level throw is not an ApiError and has no body to read, so the
    // mapping has to fall through rather than crash on `.body`.
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    generateSessionSummary.mockRejectedValue(new Error('offline'))
    mount()
    await screen.findByText(/no summary yet/i)
    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    expect(await screen.findByText(/could not summarize this session/i)).toBeTruthy()
  })
})

describe('generation while a turn is running', () => {
  const GENERATE = /^summarize$/i
  /** The English copy for pages.chat.sessionSummary.generate_turn_running,
   *  asserted verbatim: the tooltip and the server-refusal line are meant to be
   *  the SAME sentence, and a regex loose enough to match both would not prove
   *  it. */
  const BLOCKED = 'Cannot summarize while a turn is running. Wait for this turn to finish.'

  it('leaves the button live, and silent, while the slot is idle', async () => {
    // The counterpart to the blocked case: without it, a button hardcoded to
    // disabled would satisfy every other assertion in this block.
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    mount('chat-1', 'idle')
    await screen.findByText(/no summary yet/i)

    expect(screen.getByRole('button', { name: GENERATE })).toBeEnabled()
    // No always-on tooltip: on a button that works, the explanation is noise.
    expect(screen.queryByTitle(BLOCKED)).toBeNull()
    expect(screen.queryByText(BLOCKED)).toBeNull()
  })

  it('disables the button while a turn is in flight and says why on the wrapper', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    mount('chat-1', 'streaming')
    await screen.findByText(/no summary yet/i)

    // Offered, not withdrawn: the state is temporary, so removing the control
    // would read as "this session cannot be summarized".
    const btn = screen.getByRole('button', { name: GENERATE })
    expect(btn).toBeDisabled()

    // The hover target is the WRAPPER, because a disabled button gets no
    // pointer events and browsers never surface a `title` set on it. Asserting
    // only "some element has the title" would pass with it back on the button,
    // where the person can never see it.
    const hoverTarget = screen.getByTitle(BLOCKED)
    expect(hoverTarget.tagName).toBe('SPAN')
    expect(hoverTarget).toContainElement(btn)
    expect(btn.getAttribute('title')).toBeNull()
  })

  it('spends nothing when the blocked button is clicked', async () => {
    // `disabled` is the whole guard — there is no second check in onGenerate —
    // so this is what proves a click cannot start a paid pass.
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    mount('chat-1', 'streaming')
    await screen.findByText(/no summary yet/i)

    fireEvent.click(screen.getByRole('button', { name: GENERATE }))
    await waitFor(() => expect(generateSessionSummary).not.toHaveBeenCalled())
    // And the click produces no failure line either: nothing happened at all.
    expect(screen.queryByText(/could not summarize this session/i)).toBeNull()
  })

  it('gives the same reason when the backend refuses with summary_turn_running', async () => {
    // Reachable despite the disabled button: the store's per-slot state falls
    // back to idle for a turn this client never saw start, so the server stays
    // the authority — and must say the same sentence the tooltip does, not the
    // generic "try again".
    sessionSummary.mockResolvedValue(payload({ intents: [], generate_state: 'ready' }))
    generateSessionSummary.mockRejectedValue(
      new ApiError(409, 'conflict', JSON.stringify({ code: 'summary_turn_running' })),
    )
    mount('chat-1', 'idle')
    await screen.findByText(/no summary yet/i)
    fireEvent.click(screen.getByRole('button', { name: GENERATE }))

    expect(await screen.findByText(BLOCKED)).toBeTruthy()
    expect(screen.queryByText(/could not summarize this session/i)).toBeNull()
    expect(screen.queryByText(/already being written/i)).toBeNull()
  })
})

describe('ordering and triage', () => {
  it('renders intents in the order given, most recently touched first', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({ title: 'newest', last_touched_turn: 30, ranges: [[20, 30]] }),
          intent({ title: 'oldest', last_touched_turn: 4, ranges: [[1, 4]] }),
        ],
      }),
    )
    const { container } = mount()
    await screen.findByText('newest')
    const text = container.textContent || ''
    expect(text.indexOf('newest')).toBeLessThan(text.indexOf('oldest'))
  })

  it('hoists an unverified intent into the needs-you block with its source', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({
            title: 'Per-app isolation',
            state: 'needs-you',
            next_steps: [{ what: 'run a real app through it', why: 'never verified', expect: '' }],
          }),
        ],
      }),
    )
    mount()
    // Deliberately appears twice: hoisted into the triage block AND left in the
    // intent's own card, so the block is a summary rather than a relocation.
    await waitFor(() =>
      expect(screen.getAllByText(/run a real app through it/)).toHaveLength(2),
    )
    // Collapsed, the hoisted item is one headline: the block answers "does this
    // need me?" at a glance, and the reasoning is one click away rather than
    // pushing the intent list off screen.
    expect(screen.queryByText(/Part of Per-app isolation/)).toBeNull()
  })

  it('reveals a triage item\'s reasoning and source intent when expanded', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({
            title: 'Per-app isolation',
            state: 'needs-you',
            next_steps: [{ what: 'run a real app through it', why: 'never verified', expect: '' }],
          }),
        ],
      }),
    )
    mount()
    // The triage headline is the FIRST of the two occurrences — the block is
    // rendered above the intent cards.
    const headline = (await screen.findAllByText(/run a real app through it/))[0]
    fireEvent.click(headline)
    // Naming the source intent is what keeps a hoisted item from losing context.
    await waitFor(() => expect(screen.getByText(/Part of Per-app isolation/)).toBeTruthy())
    expect(screen.getAllByText('never verified').length).toBeGreaterThan(0)
  })

  it('keeps a triage item expanded across a remount', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({
            title: 'Per-app isolation',
            state: 'needs-you',
            next_steps: [{ what: 'run a real app through it', why: 'never verified', expect: '' }],
          }),
        ],
      }),
    )
    const first = mount('chat-11')
    fireEvent.click((await screen.findAllByText(/run a real app through it/))[0])
    await waitFor(() => expect(screen.getByText(/Part of Per-app isolation/)).toBeTruthy())
    first.unmount()

    mount('chat-11')
    await waitFor(() => expect(screen.getByText(/Part of Per-app isolation/)).toBeTruthy())
  })

  it('gives two intents the same step text independent disclosure', async () => {
    // Disclosure is keyed by source intent AND step text. Keyed on the text
    // alone, one chevron would expand both headlines — and persist both.
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({
            title: 'First goal',
            state: 'needs-you',
            last_touched_turn: 9,
            next_steps: [{ what: 'run the tests', why: 'from the first', expect: '' }],
          }),
          intent({
            title: 'Second goal',
            state: 'needs-you',
            last_touched_turn: 8,
            next_steps: [{ what: 'run the tests', why: 'from the second', expect: '' }],
          }),
        ],
      }),
    )
    mount()
    // Both intents contribute an identically-worded headline to the block.
    const headlines = await screen.findAllByText('run the tests')
    fireEvent.click(headlines[0])

    // Only the clicked one reveals its body.
    await waitFor(() => expect(screen.getByText(/Part of First goal/)).toBeTruthy())
    expect(screen.queryByText(/Part of Second goal/)).toBeNull()
  })

  it('gives two intents the same title independent disclosure', async () => {
    // Titles are LLM-generated and not unique. Keyed on the title alone, both
    // cards shared one disclosure entry (toggling either moved both) and
    // collided as React keys. The key also includes the first turn, which — un-
    // like the list index — survives the list re-sorting by recency.
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({
            title: 'Fix the tests',
            initial_intent: 'the first ask',
            progress: ['first gist'],
            ranges: [[9, 12]],
            last_touched_turn: 12,
          }),
          intent({
            title: 'Fix the tests',
            initial_intent: 'the second ask',
            progress: ['second gist'],
            ranges: [[3, 5]],
            last_touched_turn: 5,
          }),
        ],
      }),
    )
    mount()
    await screen.findAllByText('Fix the tests')
    // Only the most recent card starts open, so exactly one body is present.
    expect(screen.getAllByText(/you asked for/i)).toHaveLength(1)
    expect(screen.getByText('the first ask')).toBeTruthy()

    // Collapse the open one, then expand the OTHER. This direction is what
    // exposes aliasing: on a shared key, collapsing hid both (so a collapse-only
    // assertion passes either way), while expanding one opens BOTH.
    fireEvent.click(screen.getByRole('button', { expanded: true }))
    await waitFor(() => expect(screen.queryByText(/you asked for/i)).toBeNull())

    const secondHeader = screen.getByText('second gist').closest('button')
    expect(secondHeader).toBeTruthy()
    fireEvent.click(secondHeader as HTMLElement)

    // Exactly one body, and it is the second intent's — not both.
    await waitFor(() => expect(screen.getAllByText(/you asked for/i)).toHaveLength(1))
    expect(screen.getByText('the second ask')).toBeTruthy()
    expect(screen.queryByText('the first ask')).toBeNull()
  })

  it('counts every open item but renders only the first few, saying how many are hidden', async () => {
    // The cap exists so the block stays glanceable; the COUNT must not be
    // capped with it. A chip reading "3 open items" on a session with seven
    // understates exactly the busiest sessions, and the block is read as the
    // whole answer to "does this need me?".
    sessionSummary.mockResolvedValue(
      payload({
        intents: [1, 2, 3, 4, 5, 6, 7].map(n =>
          intent({
            title: `goal ${n}`,
            state: 'needs-you',
            progress: [`gist ${n}`],
            ranges: [[n, n]],
            last_touched_turn: 100 - n,
            next_steps: [{ what: `step ${n}`, why: '', expect: '' }],
          }),
        ),
      }),
    )
    mount()

    // The count is the truth: seven.
    expect(await screen.findByText('7 open items')).toBeTruthy()
    // The list is capped at three, and the overflow is stated rather than silent.
    expect(screen.getByText('+4 more')).toBeTruthy()
    // Three hoisted headlines; the first also appears in its own open card.
    expect(screen.getAllByText('step 1')).toHaveLength(2)
    // The fourth is neither hoisted nor visible — its card is collapsed to a
    // gist, which is exactly why the overflow row has to state the remainder.
    expect(screen.queryByText('step 4')).toBeNull()
  })

  it('renders when storage is denied instead of taking the panel down', async () => {
    // Safari private mode and blocked third-party storage make getItem throw
    // SecurityError. Inside a useState initializer an unguarded throw unmounts
    // the whole panel rather than falling back to the default state.
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    try {
      sessionSummary.mockResolvedValue(
        payload({ intents: [intent({ title: 'still renders' })], constraints: ['a note'] }),
      )
      mount()
      expect(await screen.findByText('still renders')).toBeTruthy()
    } finally {
      spy.mockRestore()
    }
  })

  it('shows no needs-you block when nothing is open', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [intent({ state: 'done' })] }))
    mount()
    await screen.findByText('a goal')
    expect(screen.queryByText(/open item/i)).toBeNull()
  })
})

describe('intent card', () => {
  it('opens the most recent intent and collapses the rest', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({ title: 'first', initial_intent: 'the visible ask', progress: ['p1'] }),
          intent({ title: 'second', initial_intent: 'the hidden ask', progress: ['p2'] }),
        ],
      }),
    )
    mount()
    await screen.findByText('first')
    // Assert on a body-only marker: a collapsed card still shows a one-line
    // gist, so gist text is present either way and cannot prove open/closed.
    expect(screen.getAllByText(/you asked for/i)).toHaveLength(1)
    expect(screen.getByText('the visible ask')).toBeTruthy()
    expect(screen.queryByText('the hidden ask')).toBeNull()
  })

  it('is a real button with aria-expanded, not a clickable div', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [intent({ title: 'only' })] }))
    mount()
    await screen.findByText('only')
    const buttons = screen.getAllByRole('button', { expanded: true })
    expect(buttons.length).toBeGreaterThan(0)
  })

  it('remembers a collapse across remount, per slot', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [intent({ title: 'sticky', initial_intent: 'the ask', progress: ['gist'] })],
      }),
    )
    const first = mount('chat-7')
    await screen.findByText('sticky')
    expect(screen.getAllByText(/you asked for/i)).toHaveLength(1)
    fireEvent.click(screen.getAllByRole('button', { expanded: true })[0])
    await waitFor(() => expect(screen.queryByText(/you asked for/i)).toBeNull())
    first.unmount()

    mount('chat-7')
    await screen.findByText('sticky')
    // The reader's own choice outlives the open-the-first-card default.
    expect(screen.queryByText(/you asked for/i)).toBeNull()
  })

  it('keeps a one-line gist on a collapsed card so the closed state still informs', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({ title: 'open one' }),
          intent({ title: 'closed one', progress: ['the gist line'] }),
        ],
      }),
    )
    mount()
    await screen.findByText('closed one')
    expect(screen.getByText('the gist line')).toBeTruthy()
  })

  it('renders a next step with why and expect, not just the action', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [
          intent({
            title: 'has steps',
            next_steps: [{ what: 'do the thing', why: 'because reasons', expect: 'this happens' }],
          }),
        ],
      }),
    )
    mount()
    // Two occurrences each: the triage block and the card both carry the step.
    await waitFor(() => expect(screen.getAllByText('do the thing')).toHaveLength(2))
    expect(screen.getAllByText('because reasons').length).toBeGreaterThan(0)
    expect(screen.getAllByText('this happens').length).toBeGreaterThan(0)
  })

  it('says nothing outstanding when a finished intent needs no action', async () => {
    sessionSummary.mockResolvedValue(
      payload({ intents: [intent({ title: 'finished', state: 'done', next_steps: [] })] }),
    )
    mount()
    expect(await screen.findByText(/nothing outstanding/i)).toBeTruthy()
  })

  it('shows the origin turn as provenance on the intent it caused', async () => {
    sessionSummary.mockResolvedValue(
      payload({
        intents: [intent({ title: 'pivoted', initial_intent: 'the ask', origin_turn: 20 })],
      }),
    )
    mount()
    await screen.findByText('pivoted')
    expect(screen.getByText(/turn 20/)).toBeTruthy()
  })

  it('shows every range of a resumed intent, not just the first', async () => {
    sessionSummary.mockResolvedValue(
      payload({ intents: [intent({ title: 'resumed', ranges: [[1, 14], [77, 100]] })] }),
    )
    mount()
    await screen.findByText('resumed')
    expect(screen.getByText(/1–14, 77–100/)).toBeTruthy()
  })
})

describe('project notes', () => {
  it('are absent entirely when the session has none', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [intent()], constraints: [] }))
    mount()
    await screen.findByText('a goal')
    expect(screen.queryByText(/how this project works/i)).toBeNull()
  })

  it('render collapsed by default, advertised by a count', async () => {
    sessionSummary.mockResolvedValue(
      payload({ intents: [intent()], constraints: ['use the flag', 'restart after'] }),
    )
    mount()
    // The header and its count are always present; the notes themselves are
    // durable background, so they do not push the intent list down by default.
    expect(await screen.findByText(/how this project works/i)).toBeTruthy()
    expect(screen.getByText('2')).toBeTruthy()
    expect(screen.queryByText('use the flag')).toBeNull()
  })

  it('expand on click, and the expansion persists per slot', async () => {
    sessionSummary.mockResolvedValue(
      payload({ intents: [intent()], constraints: ['a durable fact'] }),
    )
    const first = mount('chat-9')
    const header = await screen.findByText(/how this project works/i)
    fireEvent.click(header)
    await waitFor(() => expect(screen.getByText('a durable fact')).toBeTruthy())
    first.unmount()

    mount('chat-9')
    await screen.findByText(/how this project works/i)
    expect(screen.getByText('a durable fact')).toBeTruthy()
  })
})

describe('cost discipline', () => {
  it('fetches once per slot and does not poll', async () => {
    sessionSummary.mockResolvedValue(payload({ intents: [intent()] }))
    mount()
    await screen.findByText('a goal')
    // Freshness comes from the websocket push, not an interval — a polling
    // panel would reward the refresh habit this feature exists to remove.
    expect(sessionSummary).toHaveBeenCalledTimes(1)
  })
})

describe('theme tokens', () => {
  it('uses no phantom color class', () => {
    // A Tailwind utility only exists if its key is MAPPED in tailwind.config.js.
    // `--panel` / `--panel-strong` are defined in index.css but never mapped, so
    // `bg-panel` and `bg-panel-strong` compile to nothing and the element paints
    // the colour behind it — which is invisible in code review and, on a dark
    // theme, nearly invisible in a screenshot. The panel's two pinned bars are
    // the surfaces that depend on a real fill to separate them from the list
    // they sit outside, so they are what this pins. Real keys: `card`,
    // `bg-elevated`. opsMissionControl.test.ts guards the same mistake for the
    // ops panels; its file list cannot cover files added later, so each surface
    // carries its own assertion.
    const src = readFileSync(
      resolve(__dirname, '../pages/chat/SessionSummaryTab.tsx'),
      'utf-8',
    )
    expect(src).not.toMatch(/\bbg-panel\b/)
    expect(src).not.toMatch(/\bbg-panel-strong\b/)
    expect(src).not.toMatch(/\bborder-line\b/)
    expect(src).not.toMatch(/\bborder-subtle\b/)
    expect(src).not.toMatch(/\btext-success\b/)
    expect(src).not.toMatch(/\btext-warning\b/)
    // The replacements must be present, or this passes by the classes simply
    // having been deleted.
    expect(src).toMatch(/\bbg-card\b/)
    expect(src).toMatch(/\bbg-bg-elevated\b/)
  })
})
