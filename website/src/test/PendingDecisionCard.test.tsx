import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

import PendingDecisionCard from '../components/PendingDecisionCard'
import { api, ApiError } from '../api/client'

/**
 * The "Waiting on you" card for a buried [OPTIONS:] decision.
 *
 * The backend suite (test_slot_pending_decision.py) pins WHEN the payload is
 * raised; these tests pin what the card DOES with it. Interaction mirrors the
 * sibling QuestionCard, not the FollowUpBar chips: options are aria-pressed
 * TOGGLES, and exactly two action buttons dispatch (Send, Add to composer) —
 * the `max-two-buttons-per-row` budget is a design rule, so the button count
 * is pinned here on purpose. The dismiss round-trip has three exits
 * (confirmed, stale-404, retryable failure). The mount gating (question card
 * owns the band) lives at the call sites; this component is store-free.
 */

const DECISION = {
  options: ['Retry again', 'Check mainline', 'Stop'],
  excerpt: 'CTR failed twice on flaky suites — how should I proceed?',
  ts: '2026-09-11T10:00:00',
}

const renderCard = (over: Partial<Parameters<typeof PendingDecisionCard>[0]> = {}) => {
  const onPick = vi.fn()
  const onSendDirect = vi.fn()
  render(
    <PendingDecisionCard
      slotKey="chat-1"
      decision={DECISION}
      onPick={onPick}
      onSendDirect={onSendDirect}
      {...over}
    />,
  )
  return { onPick, onSendDirect }
}

beforeEach(() => vi.restoreAllMocks())

describe('rendering', () => {
  it('shows the excerpt and every option as a toggle', () => {
    renderCard()
    expect(screen.getByTestId('pending-decision-card')).toBeInTheDocument()
    expect(screen.getByText(DECISION.excerpt)).toBeInTheDocument()
    const toggles = screen.getAllByTestId('pending-decision-option')
    expect(toggles.map((c) => c.textContent)).toEqual(DECISION.options)
    for (const t of toggles) expect(t).toHaveAttribute('aria-pressed', 'false')
  })

  it('keeps the action row at exactly two buttons however many options exist', () => {
    // max-two-buttons-per-row is a blocking design rule: the options are
    // selections, and only Send + Add to composer act.
    renderCard()
    expect(screen.getByTestId('pending-decision-send')).toBeInTheDocument()
    expect(screen.getByTestId('pending-decision-fill')).toBeInTheDocument()
    // Both disabled until something is picked — nothing to dispatch yet.
    expect(screen.getByTestId('pending-decision-send')).toBeDisabled()
    expect(screen.getByTestId('pending-decision-fill')).toBeDisabled()
  })

  it('renders nothing for an empty option list', () => {
    render(
      <PendingDecisionCard slotKey="chat-1" decision={{ options: [], ts: 't' }} onPick={vi.fn()} />,
    )
    expect(screen.queryByTestId('pending-decision-card')).toBeNull()
  })
})

describe('pick-then-act semantics', () => {
  it('toggles selection with aria-pressed and re-click deselects', () => {
    renderCard()
    const opt = screen.getByText('Retry again')
    fireEvent.click(opt)
    expect(opt.closest('button')).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(opt)
    expect(opt.closest('button')).toHaveAttribute('aria-pressed', 'false')
  })

  it('Send dispatches the picked options joined in option order', () => {
    const { onPick, onSendDirect } = renderCard()
    // Click in REVERSE order — the joined text must still follow option order,
    // the same ordered suffix the composer chips append.
    fireEvent.click(screen.getByText('Stop'))
    fireEvent.click(screen.getByText('Retry again'))
    fireEvent.click(screen.getByTestId('pending-decision-send'))
    expect(onSendDirect).toHaveBeenCalledWith('Retry again, Stop')
    expect(onPick).not.toHaveBeenCalled()
  })

  it('Add to composer fills without sending', () => {
    const { onPick, onSendDirect } = renderCard()
    fireEvent.click(screen.getByText('Check mainline'))
    fireEvent.click(screen.getByTestId('pending-decision-fill'))
    expect(onPick).toHaveBeenCalledWith('Check mainline')
    expect(onSendDirect).not.toHaveBeenCalled()
  })

  it('a multi-pick names its count on the primary button', () => {
    // Multi-select is SIGNALLED: two toggles flip the label from "Submit" to
    // "Submit 2 answers", so the cold reader knows both picks travel in one
    // answer (joined in option order — pinned above).
    renderCard()
    const send = screen.getByTestId('pending-decision-send')
    expect(send.textContent).toContain('Submit')
    expect(send.textContent).not.toContain('2')
    fireEvent.click(screen.getByText('Retry again'))
    fireEvent.click(screen.getByText('Stop'))
    expect(send.textContent).toContain('2')
  })
})

describe('one answer per decision', () => {
  it('Send dispatches once however fast the second click lands', () => {
    // The lock is set synchronously in the dispatching click: the card stays
    // mounted until the slot payload retires it, so without the lock a
    // double-click queues the same user turn twice.
    const { onSendDirect } = renderCard()
    fireEvent.click(screen.getByText('Stop'))
    const send = screen.getByTestId('pending-decision-send')
    fireEvent.click(send)
    fireEvent.click(send)
    expect(onSendDirect).toHaveBeenCalledTimes(1)
    expect(send).toBeDisabled()
    expect(screen.getByTestId('pending-decision-fill')).toBeDisabled()
  })

  it('a superseding decision drops the old selection and re-arms the actions', () => {
    // The component stays mounted across a slot refresh; a NEWER options turn
    // replaces `decision` in place. Selections and the spent send lock belong
    // to the decision they were made on, so both reset on the ts change even
    // when the new options carry identical strings.
    const onPick = vi.fn()
    const onSendDirect = vi.fn()
    const { rerender } = render(
      <PendingDecisionCard slotKey="chat-1" decision={DECISION} onPick={onPick} onSendDirect={onSendDirect} />,
    )
    fireEvent.click(screen.getByText('Stop'))
    fireEvent.click(screen.getByTestId('pending-decision-send'))
    expect(onSendDirect).toHaveBeenCalledTimes(1)
    rerender(
      <PendingDecisionCard
        slotKey="chat-1"
        decision={{ ...DECISION, ts: '2026-09-11T11:30:00' }}
        onPick={onPick}
        onSendDirect={onSendDirect}
      />,
    )
    for (const t of screen.getAllByTestId('pending-decision-option')) {
      expect(t).toHaveAttribute('aria-pressed', 'false')
    }
    // Nothing picked on the new decision yet — disabled by emptiness, not by a
    // stale lock; picking again re-enables Send for the new question.
    expect(screen.getByTestId('pending-decision-send')).toBeDisabled()
    fireEvent.click(screen.getByText('Stop'))
    expect(screen.getByTestId('pending-decision-send')).toBeEnabled()
    fireEvent.click(screen.getByTestId('pending-decision-send'))
    expect(onSendDirect).toHaveBeenCalledTimes(2)
  })
})

describe('dismiss round-trip', () => {
  it('names the decision by ts and hides only on confirmation', async () => {
    const dismiss = vi.spyOn(api, 'dismissPendingDecision').mockResolvedValue({ ok: true } as never)
    renderCard()
    fireEvent.click(screen.getByTestId('pending-decision-dismiss'))
    expect(dismiss).toHaveBeenCalledWith('chat-1', DECISION.ts)
    await waitFor(() => expect(screen.queryByTestId('pending-decision-card')).toBeNull())
  })

  it('treats a 404 as stale and takes the card away', async () => {
    vi.spyOn(api, 'dismissPendingDecision').mockRejectedValue(new ApiError(404, 'gone'))
    renderCard()
    fireEvent.click(screen.getByTestId('pending-decision-dismiss'))
    await waitFor(() => expect(screen.queryByTestId('pending-decision-card')).toBeNull())
  })

  it('keeps the card AND says why on a retryable failure', async () => {
    vi.spyOn(api, 'dismissPendingDecision').mockRejectedValue(new ApiError(503, 'offline'))
    renderCard()
    fireEvent.click(screen.getByTestId('pending-decision-dismiss'))
    await waitFor(() => expect(screen.getByTestId('pending-decision-dismiss-error')).toBeInTheDocument())
    expect(screen.getByTestId('pending-decision-card')).toBeInTheDocument()
  })
})
