/**
 * Coverage for the collapsed-disclosure redesign of SessionPulseSurveyCard:
 * the card shows as a single slim trigger row by default, expands to the
 * full rating/feedback/email form only on click, and collapses again to a
 * one-line "thanks" row after submit -- rather than opening the whole form
 * uninvited the instant it becomes eligible.
 *
 * Also pins the reopen-loop fix this redesign sits on top of (dismiss and
 * the post-submit auto-close must not immediately reopen the card) and the
 * onLayoutChange contract ChatPage's scroll re-anchor depends on.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'

import { server } from '../../integration/mocks/server'
import SessionPulseSurveyCard from '../components/SessionPulseSurveyCard'

// Real animation timing/keyframes aren't relevant here and only slow the
// suite down -- same substitution TipCard.test.tsx uses.
vi.mock('framer-motion', () => ({
  motion: {
    div: ({ children, ...props }: { children?: React.ReactNode } & Record<string, unknown>) => <div {...props}>{children}</div>,
  },
  AnimatePresence: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}))

const RATING_QUESTION = 'How would you rate your experience with Kiro Crew today?'
const THANKS = 'Thanks for your feedback!'
const DISMISS_LABEL = 'Dismiss survey'

function renderCard(onLayoutChange = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <SessionPulseSurveyCard
        sessionId="chat-1-1786589233"
        kiroCrewVersion="1.0.0"
        turnCount={0}
        slotOrigin="user"
        onLayoutChange={onLayoutChange}
      />
    </QueryClientProvider>,
  )
  const rerenderWithTurnCount = (turnCount: number) =>
    utils.rerender(
      <QueryClientProvider client={qc}>
        <SessionPulseSurveyCard
          sessionId="chat-1-1786589233"
          kiroCrewVersion="1.0.0"
          turnCount={turnCount}
          slotOrigin="user"
          onLayoutChange={onLayoutChange}
        />
      </QueryClientProvider>,
    )
  return { ...utils, rerenderWithTurnCount, onLayoutChange }
}

/** Crosses the 10-turn eligibility threshold and waits for the card to show. */
async function showCard(rerenderWithTurnCount: (n: number) => void) {
  rerenderWithTurnCount(10)
  await waitFor(() => {
    expect(screen.getByText(RATING_QUESTION)).toBeInTheDocument()
  })
}

describe('SessionPulseSurveyCard (collapsed disclosure)', () => {
  beforeEach(() => {
    localStorage.clear()
    server.use(
      http.get('/api/feedback/eligible', () => HttpResponse.json({ eligible: true })),
      http.post('/api/feedback/submit', () => HttpResponse.json({ ok: true })),
    )
  })

  it('shows collapsed by default: the question is visible but no form fields render', async () => {
    const { rerenderWithTurnCount } = renderCard()
    await showCard(rerenderWithTurnCount)

    expect(screen.queryByText('Very Poor')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Submit' })).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText('you@example.com (optional)')).not.toBeInTheDocument()
  })

  it('clicking the trigger row expands the full rating/feedback/email form', async () => {
    const { rerenderWithTurnCount } = renderCard()
    await showCard(rerenderWithTurnCount)

    fireEvent.click(screen.getByRole('button', { name: RATING_QUESTION }))

    expect(screen.getByRole('radio', { name: 'Very Poor' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Excellent' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument()
    expect(screen.getByPlaceholderText('you@example.com (optional)')).toBeInTheDocument()
  })

  it('clicking the header again collapses the form back without submitting (no separate Cancel needed)', async () => {
    const { rerenderWithTurnCount } = renderCard()
    await showCard(rerenderWithTurnCount)

    const header = screen.getByRole('button', { name: RATING_QUESTION })
    fireEvent.click(header)
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument()

    fireEvent.click(header)
    expect(screen.queryByRole('button', { name: 'Submit' })).not.toBeInTheDocument()
    // Collapsing back is not a dismissal -- the trigger row is still there.
    expect(screen.getByText(RATING_QUESTION)).toBeInTheDocument()
  })

  it('dismiss (X) on the collapsed row hides the card and it does not reopen', async () => {
    const { rerenderWithTurnCount } = renderCard()
    await showCard(rerenderWithTurnCount)

    fireEvent.click(screen.getByRole('button', { name: DISMISS_LABEL }))
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()

    // A later turn arriving must not re-trigger the reopen-loop bug this
    // redesign sits on top of.
    rerenderWithTurnCount(11)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()
  })

  it('does NOT re-show within 30 days of a prior show, even past the turn threshold', async () => {
    // Simulate a browser that already saw the survey in a prior build: a fresh
    // cooldown stamp is present. Even with eligibility true (beforeEach) and
    // the turn gate crossed, the 30-day cooldown must keep the card hidden --
    // this is what stops anyone who already answered from being re-surveyed.
    const { rerenderWithTurnCount } = renderCard()
    localStorage.setItem('kirocrew_survey_last_shown', new Date().toISOString())
    rerenderWithTurnCount(10)
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()
  })

  it('shows again once the 30-day cooldown has elapsed', async () => {
    // The flip side: a stamp older than 30 days no longer blocks, so the pulse
    // remains periodic rather than one-and-done.
    const { rerenderWithTurnCount } = renderCard()
    const thirtyOneDaysAgoMs = Date.now() - 31 * 24 * 60 * 60 * 1000
    localStorage.setItem('kirocrew_survey_last_shown', new Date(thirtyOneDaysAgoMs).toISOString())
    await showCard(rerenderWithTurnCount)
  })

  it('submitting collapses to a one-line thank-you row (not the full form) and auto-hides', async () => {
    const { rerenderWithTurnCount } = renderCard()
    await showCard(rerenderWithTurnCount)

    fireEvent.click(screen.getByRole('button', { name: RATING_QUESTION }))
    fireEvent.click(screen.getByRole('radio', { name: 'Good' }))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() => {
      expect(screen.getByText(THANKS)).toBeInTheDocument()
    })
    // The confirmation is the same slim row shape as the trigger -- the
    // rating form must not still be showing underneath it.
    expect(screen.queryByRole('button', { name: 'Good' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Submit' })).not.toBeInTheDocument()

    // Auto-closes on its own after the confirmation display window.
    await waitFor(
      () => {
        expect(screen.queryByText(THANKS)).not.toBeInTheDocument()
      },
      { timeout: 4000 },
    )
  })

  it('calls onLayoutChange on show, on expand, and on collapse -- so the parent can re-anchor scroll for every height change', async () => {
    const onLayoutChange = vi.fn()
    const { rerenderWithTurnCount } = renderCard(onLayoutChange)
    // The baseline must be captured while the card is still hidden — if the
    // show already happened, `callsBeforeShow` silently includes the show's
    // own call and the delta assertion below can never pass.
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()
    const callsBeforeShow = onLayoutChange.mock.calls.length

    await showCard(rerenderWithTurnCount)
    // The callback fires from a layout effect, one commit AFTER the text the
    // show waits on is queryable — poll the condition instead of asserting
    // the instant count (raced on slow runners: the count was read before
    // the effect flushed).
    await waitFor(() => expect(onLayoutChange.mock.calls.length).toBeGreaterThan(callsBeforeShow))
    const callsAfterShow = onLayoutChange.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: RATING_QUESTION }))
    await waitFor(() => expect(onLayoutChange.mock.calls.length).toBeGreaterThan(callsAfterShow))
    const callsAfterExpand = onLayoutChange.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: RATING_QUESTION }))
    await waitFor(() => expect(onLayoutChange.mock.calls.length).toBeGreaterThan(callsAfterExpand))
  })
})

describe('SessionPulseSurveyCard (origin-based surface gate)', () => {
  beforeEach(() => {
    localStorage.clear()
    server.use(
      http.get('/api/feedback/eligible', () => HttpResponse.json({ eligible: true })),
      http.post('/api/feedback/submit', () => HttpResponse.json({ ok: true })),
    )
  })

  function renderCardWithSession(sessionId: string, slotOrigin: string | undefined) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const utils = render(
      <QueryClientProvider client={qc}>
        <SessionPulseSurveyCard
          sessionId={sessionId}
          kiroCrewVersion="1.0.0"
          turnCount={0}
          slotOrigin={slotOrigin}
        />
      </QueryClientProvider>,
    )
    const rerenderWithTurnCount = (turnCount: number) =>
      utils.rerender(
        <QueryClientProvider client={qc}>
          <SessionPulseSurveyCard
            sessionId={sessionId}
            kiroCrewVersion="1.0.0"
            turnCount={turnCount}
            slotOrigin={slotOrigin}
          />
        </QueryClientProvider>,
      )
    return { ...utils, rerenderWithTurnCount }
  }

  it.each([
    ['app', 'an app-minted session (Slack import, task-runner, app SDK)'],
    ['cron', 'a cron-triggered session'],
    ['system', 'a gateway-internal session'],
    [undefined, 'an untagged / still-loading session'],
  ])(
    'never shows for origin=%s (%s), even on a chat-<n>-<ts> key past turn 10',
    async (origin) => {
      // Deliberately uses the ORDINARY chat-key shape: the gate is the slot
      // ORIGIN, not the key. A non-user origin must be excluded even when the
      // key looks like a normal dashboard chat — the exact bypass (imported
      // Slack thread / task-runner slot minting a chat-<n>-<ts> key) that a
      // key-shape check let through and that caused the revert.
      const { rerenderWithTurnCount } = renderCardWithSession(
        'chat-1-1786589233',
        origin as string | undefined,
      )
      rerenderWithTurnCount(10)
      // Give any (incorrectly) enabled query a chance to resolve and the show
      // effect a chance to fire, so this isn't just "we didn't wait long enough".
      await new Promise((r) => setTimeout(r, 100))
      expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()
    },
  )

  it('still shows for a user-origin chat session (the dashboard Chat page)', async () => {
    const { rerenderWithTurnCount } = renderCardWithSession('chat-7-1786950000', 'user')
    await showCard(rerenderWithTurnCount)
    expect(screen.getByText(RATING_QUESTION)).toBeInTheDocument()
  })
})


describe('SessionPulseSurveyCard (cooldown re-checked at show time)', () => {
  beforeEach(() => {
    localStorage.clear()
    server.use(
      http.get('/api/feedback/eligible', () => HttpResponse.json({ eligible: true })),
      http.post('/api/feedback/submit', () => HttpResponse.json({ ok: true })),
    )
  })

  it('does not reopen from a cached eligible result while the 30-day cooldown is active (remount within cache lifetime)', async () => {
    // One shared QueryClient so the staleTime:Infinity eligible cache survives
    // the remount -- reproducing "submit -> switch session -> return" within
    // the cache lifetime, which is when the stale cached `true` is exposed.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (turnCount: number) => (
      <QueryClientProvider client={qc}>
        <SessionPulseSurveyCard
          sessionId="chat-1-1786589233"
          kiroCrewVersion="1.0.0"
          turnCount={turnCount}
          slotOrigin="user"
        />
      </QueryClientProvider>
    )

    // First mount: cross turn 10 so the card shows and writes the cooldown stamp.
    const first = render(tree(0))
    first.rerender(tree(10))
    await waitFor(() => {
      expect(screen.getByText(RATING_QUESTION)).toBeInTheDocument()
    })
    expect(localStorage.getItem('kirocrew_survey_last_shown')).not.toBeNull()

    // Remount the same session on the same QueryClient: the eligible query is
    // cached `true`, `handled` has reset, but the 30-day cooldown is now active.
    first.unmount()
    const second = render(tree(0))
    second.rerender(tree(10))

    // The card must stay closed: the show effect re-checks the cooldown via
    // eligibilityGate, so a stale cached `true` can no longer reopen it and
    // solicit a second response inside the window. Wait long enough that a
    // wrongly-fired show effect would have shown the card.
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()
  })
})


describe('SessionPulseSurveyCard (form interactions)', () => {
  beforeEach(() => {
    localStorage.clear()
    server.use(
      http.get('/api/feedback/eligible', () => HttpResponse.json({ eligible: true })),
      http.post('/api/feedback/submit', () => HttpResponse.json({ ok: true })),
    )
  })

  async function expandForm() {
    const { rerenderWithTurnCount } = renderCard()
    await showCard(rerenderWithTurnCount)
    fireEvent.click(screen.getByRole('button', { name: RATING_QUESTION }))
    expect(screen.getByRole('radio', { name: 'Very Poor' })).toBeInTheDocument()
  }

  const isChecked = (name: string) =>
    screen.getByRole('radio', { name }).getAttribute('aria-checked') === 'true'

  it('arrow keys rove the rating radiogroup (select-first, step forward/back, and wrap-around)', async () => {
    await expandForm()
    const group = screen.getByRole('radiogroup')

    // A non-arrow key is ignored -- nothing gets selected.
    fireEvent.keyDown(group, { key: 'Enter' })
    expect(isChecked('Very Poor')).toBe(false)

    // First ArrowRight with nothing selected lands on the first option.
    fireEvent.keyDown(group, { key: 'ArrowRight' })
    expect(isChecked('Very Poor')).toBe(true)

    // ArrowDown steps forward to the next option.
    fireEvent.keyDown(group, { key: 'ArrowDown' })
    expect(isChecked('Poor')).toBe(true)

    // ArrowLeft steps back.
    fireEvent.keyDown(group, { key: 'ArrowLeft' })
    expect(isChecked('Very Poor')).toBe(true)

    // ArrowLeft off the first option wraps around to the last.
    fireEvent.keyDown(group, { key: 'ArrowLeft' })
    expect(isChecked('Excellent')).toBe(true)
  })

  it('captures typed feedback and email into the form fields', async () => {
    await expandForm()

    const feedbackBox = screen.getByPlaceholderText('Optional') as HTMLTextAreaElement
    fireEvent.change(feedbackBox, { target: { value: 'the diff view is great' } })
    expect(feedbackBox.value).toBe('the diff view is great')

    const emailBox = screen.getByPlaceholderText(
      'you@example.com (optional)',
    ) as HTMLInputElement
    fireEvent.change(emailBox, { target: { value: 'dev@example.com' } })
    expect(emailBox.value).toBe('dev@example.com')
  })

  it('keeps the form open and shows an error when submit returns non-2xx (no false thank-you)', async () => {
    server.use(
      http.post('/api/feedback/submit', () =>
        HttpResponse.json({ error: 'nope' }, { status: 500 }),
      ),
    )
    await expandForm()
    fireEvent.click(screen.getByRole('radio', { name: 'Good' }))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() => {
      expect(screen.getByText("Couldn't submit — please try again.")).toBeInTheDocument()
    })
    // No false confirmation, and the form is still there to retry.
    expect(screen.queryByText(THANKS)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Submit' })).toBeInTheDocument()
  })

  it('surfaces the submit error when the network throws (does not show the thank-you row)', async () => {
    server.use(http.post('/api/feedback/submit', () => HttpResponse.error()))
    await expandForm()
    fireEvent.click(screen.getByRole('radio', { name: 'Good' }))
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() => {
      expect(screen.getByText("Couldn't submit — please try again.")).toBeInTheDocument()
    })
    expect(screen.queryByText(THANKS)).not.toBeInTheDocument()
  })
})

describe('SessionPulseSurveyCard (eligibility fails closed)', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('does not show when the eligibility endpoint returns non-2xx', async () => {
    server.use(
      http.get('/api/feedback/eligible', () =>
        HttpResponse.json({ eligible: true }, { status: 500 }),
      ),
      http.post('/api/feedback/submit', () => HttpResponse.json({ ok: true })),
    )
    const { rerenderWithTurnCount } = renderCard()
    rerenderWithTurnCount(10)
    // Give the (now failing) eligibility query time to resolve so this isn't
    // just "we didn't wait long enough".
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()
  })

  it('does not show when the eligibility check errors at the network level', async () => {
    server.use(
      http.get('/api/feedback/eligible', () => HttpResponse.error()),
      http.post('/api/feedback/submit', () => HttpResponse.json({ ok: true })),
    )
    const { rerenderWithTurnCount } = renderCard()
    rerenderWithTurnCount(10)
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()
  })
})

describe('SessionPulseSurveyCard (turn baseline / reopened chat)', () => {
  beforeEach(() => {
    localStorage.clear()
    server.use(
      http.get('/api/feedback/eligible', () => HttpResponse.json({ eligible: true })),
      http.post('/api/feedback/submit', () => HttpResponse.json({ ok: true })),
    )
  })

  it('ignores an old chat\u2019s prior history: needs 10 NEW back-and-forths past the mount baseline', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (turnCount: number) => (
      <QueryClientProvider client={qc}>
        <SessionPulseSurveyCard
          sessionId="chat-9-1786950000"
          kiroCrewVersion="1.0.0"
          turnCount={turnCount}
          slotOrigin="user"
        />
      </QueryClientProvider>
    )

    // Reopen a chat that already has 40 back-and-forths of history: the card
    // captures 40 as its baseline at mount, so those prior turns must NOT count.
    const { rerender } = render(tree(40))

    // 9 NEW back-and-forths (turnCount 49) is still one short of the live
    // threshold -- the survey stays hidden even though the absolute count (49)
    // is far past 10.
    rerender(tree(49))
    await new Promise((r) => setTimeout(r, 100))
    expect(screen.queryByText(RATING_QUESTION)).not.toBeInTheDocument()

    // The 10th NEW back-and-forth (turnCount 50) crosses the live threshold.
    rerender(tree(50))
    await waitFor(() => {
      expect(screen.getByText(RATING_QUESTION)).toBeInTheDocument()
    })
  })
})
