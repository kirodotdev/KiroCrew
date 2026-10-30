/**
 * The goal card's Heartbeat checkbox: one press sets the goal and keeps it
 * moving. Checked by default, it names how often it runs and that each run
 * uses credits; flipping it re-previews the card with `heartbeat`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { store as reduxStore } from '../store'
import { cardsApi, type Card } from '../api/cards'
import { GoalHeartbeatOption as GoalBody } from './kindBodies'
import { cardChanges } from './cardRegistry'
import ChangeCard from './ChangeCard'

const offerRow = (checked: boolean, interval = 60) => ({
  label: 'Heartbeat (each run uses credits)',
  field: 'heartbeat_offer',
  after: checked,
  interval,
  per_day: (24 * 60) / interval,
})

const goalCard = (over: Partial<Card> = {}): Card => ({
  id: 'g1',
  slot_key: 'slot-A',
  kind: 'crewmate.goal',
  revision: 1,
  status: 'pending',
  risk: 'normal',
  title: 'Add a goal for “Scout”',
  changes: [
    { label: 'Goal 1', field: 'goal', number: 1, before: null, after: 'Ship the docs' },
    offerRow(true),
  ],
  scope: { members: ['Scout'], count: 1 },
  reason: 'You asked me to own the release notes.',
  editable: ['long_term', 'heartbeat'],
  params: { member: 'scout', long_term: 'Ship the docs' },
  plan: { apply: [], undo: null },
  created_at: 1_700_000_000,
  // A fixed instant far ahead keeps the card pending without reading the clock.
  expires_at: 4_000_000_000_000,
  ...over,
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('GoalBody', () => {
  it('renders the Heartbeat box checked by default with its cadence and credit note', () => {
    render(<GoalBody card={goalCard()} disabled={false} preview={vi.fn()} />)
    const box = screen.getByTestId('change-card-goal-heartbeat-box') as HTMLInputElement
    expect(box.checked).toBe(true)
    expect(screen.getByLabelText('Keep this moving on its own (Heartbeat)')).toBe(box)
    const note = screen.getByTestId('change-card-goal-heartbeat-note')
    expect(note.textContent?.replace(/\s/g, ' ')).toBe('Runs about every 1 hr. Each run uses credits, and you can turn it off any time.')
    expect(box.getAttribute('aria-describedby')).toBe(note.id)
  })

  it('names the mate\u2019s own interval', () => {
    render(<GoalBody card={goalCard({ changes: [offerRow(true, 240)] })} disabled={false} preview={vi.fn()} />)
    expect(screen.getByTestId('change-card-goal-heartbeat-note').textContent?.replace(/\s/g, ' ')).toContain('every 4 hr')
  })

  it('re-previews with heartbeat flipped when the box is toggled', () => {
    const preview = vi.fn()
    const { rerender } = render(<GoalBody card={goalCard()} disabled={false} preview={preview} />)
    fireEvent.click(screen.getByTestId('change-card-goal-heartbeat-box'))
    expect(preview).toHaveBeenLastCalledWith({ member: 'scout', long_term: 'Ship the docs', heartbeat: false })
    const unchecked = goalCard({ changes: [offerRow(false)], params: { member: 'scout', long_term: 'Ship the docs', heartbeat: false } })
    rerender(<GoalBody card={unchecked} disabled={false} preview={preview} />)
    const box = screen.getByTestId('change-card-goal-heartbeat-box') as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    expect(preview).toHaveBeenLastCalledWith({ member: 'scout', long_term: 'Ship the docs', heartbeat: true })
  })

  it('shows no box when the gateway offered none (the Heartbeat is on, or was declined)', () => {
    render(<GoalBody card={goalCard({ changes: [goalCard().changes[0]] })} disabled={false} preview={vi.fn()} />)
    expect(screen.queryByTestId('change-card-goal-heartbeat')).toBeNull()
  })

  it('locks the box on a finished card', () => {
    render(<GoalBody card={goalCard({ status: 'applied' })} disabled={false} preview={vi.fn()} />)
    expect((screen.getByTestId('change-card-goal-heartbeat-box') as HTMLInputElement).disabled).toBe(true)
  })

  it('keeps the Heartbeat row out of the change lines', () => {
    expect(cardChanges(goalCard()).map(c => c.field)).toEqual(['goal'])
  })
})

describe('ChangeCard with a goal card', () => {
  it('sends the toggled heartbeat to the card preview', async () => {
    const spy = vi.spyOn(cardsApi, 'preview').mockResolvedValue(null)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={reduxStore}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <ChangeCard card={goalCard()} store={() => {}} refresh={() => {}} remove={() => {}} />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    // The option of the Set goal press sits after the reason, just above the buttons.
    const option = screen.getByTestId('change-card-goal-heartbeat')
    const reason = screen.getByTestId('change-card-reason')
    expect(reason.compareDocumentPosition(option) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByTestId('change-card-rows').contains(option)).toBe(false)
    fireEvent.click(screen.getByTestId('change-card-goal-heartbeat-box'))
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy.mock.calls[0][1]).toEqual({ member: 'scout', long_term: 'Ship the docs', heartbeat: false })
    // The preview settles before the test ends, so no state update leaks past it.
    await spy.mock.results[0].value
  })
})
