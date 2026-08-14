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

import SessionSummaryTab from '../pages/chat/SessionSummaryTab'
import type { SessionSummary, SessionIntent } from '../types/sessionSummary'

const sessionSummary = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    sessionSummary: (slot: string) => sessionSummary(slot),
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

function mount(slot = 'chat-1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <SessionSummaryTab slot={slot} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  sessionSummary.mockReset()
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
