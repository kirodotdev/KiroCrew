import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import {
  GOAL_DRAFT_SYNC_TIMEOUT_MS,
  __resetForTests,
  loadPendingGoalDraft,
  pendingGoalDraftClear,
} from '../utils/goalDrafts'
import { deferred, goalBox, makeLoop, renderBridgedPopover, stubGoalGateway, wireDraft } from './autoNudgePopoverFixtures'

const CLEAR_FAILED = "Couldn't clear this goal on every device yet. Retry to finish clearing it."

/**
 * A Clear whose loop removal gets no answer: past the editor's sync deadline,
 * or lost when the connection drops. The dashboard remounts the editor on the
 * `removed` frame, which the server sends before it answers the DELETE. Either
 * way the removal may have landed, so the failed Clear stands with Retry: Retry
 * re-sends the removal and only its answer finishes the Clear. Until then the
 * goal it cleared never comes back and Start stays disabled.
 */
describe('AutoNudgePopover goal clear with no answer', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  /** The stored goal as version 5, and loop removals that each answer only when
   *  the test says so, if ever, or fail as a dropped connection does. The double
   *  ignores the abort signal, as a stalled network can. */
  function stubGateway() {
    const gateway = stubGoalGateway({ draft: wireDraft('active loop goal', 90, 3), updated_at: 5 })
    const removals: ((outcome: 'answered' | 'lost') => void)[] = []
    const control = {
      deleteStarted: false,
      /** The loop removals sent so far. */
      deletes: () => removals.length,
      /** Answer the latest removal. */
      resolveDelete: () => removals[removals.length - 1]('answered'),
      /** Drop the latest removal's connection before its answer arrives. */
      loseDeleteAnswer: () => removals[removals.length - 1]('lost'),
    }
    gateway.intercept = (_url, init) => {
      if (init?.method !== 'DELETE') return undefined
      control.deleteStarted = true
      const removal = deferred<'answered' | 'lost'>()
      removals.push(removal.resolve)
      return removal.promise.then(outcome => {
        if (outcome === 'lost') throw new TypeError('Failed to fetch')
        return { ok: true, status: 204, json: () => Promise.resolve({}) }
      })
    }
    return { gateway, control }
  }

  const startButton = () => screen.getByRole('button', { name: /Start loop/i })
  const retryButton = () => screen.getByRole('button', { name: /^Retry$/i })
  const syncError = () => screen.getByTestId('goal-draft-sync-error')

  function pressClear() {
    fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
  }

  /** Clear, then the `removed` frame re-keys the editor while the DELETE is unanswered. */
  async function clearWithRemovedFrame(control: { deleteStarted: boolean }) {
    pressClear()
    await waitFor(() => expect(control.deleteStarted).toBe(true))
    fireEvent.click(screen.getByTestId('remove-automation'))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull())
    expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent('Checking saved goal')
  }

  async function passTheDeadline() {
    await act(async () => { await vi.advanceTimersByTimeAsync(GOAL_DRAFT_SYNC_TIMEOUT_MS + 1) })
  }

  /** Reopened after the Clear finished, the editor shows no goal. */
  async function reopenShowsNoGoal() {
    fireEvent.click(screen.getByTestId('reopen-popover'))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    expect(goalBox().value).not.toContain('active loop goal')
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
  }

  it('shows the failed clear with Retry once a stalled DELETE passes the deadline', async () => {
    const slot = 'chat-clear-delete-stalls'
    const { gateway, control } = stubGateway()

    renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    await clearWithRemovedFrame(control)
    await passTheDeadline()

    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    expect(screen.getByTestId('goal-draft-sync-status')).not.toHaveTextContent('Checking saved goal')
    expect(retryButton()).toBeInTheDocument()
    expect(startButton()).toBeDisabled()
    expect(pendingGoalDraftClear(slot)).toBeUndefined()
    expect(gateway.puts).toHaveLength(0)
  })

  it('finishes a Clear whose DELETE succeeded after the deadline once Retry re-sends it and it is answered', async () => {
    const slot = 'chat-clear-delete-late-success'
    const { gateway, control } = stubGateway()

    renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    await clearWithRemovedFrame(control)
    await passTheDeadline()
    await waitFor(() => expect(retryButton()).toBeInTheDocument())
    expect(startButton()).toBeDisabled()
    // The removal went through on the server; its answer comes too late to count.
    control.resolveDelete()

    fireEvent.click(retryButton())
    await waitFor(() => expect(control.deletes()).toBe(2))
    expect(startButton()).toBeDisabled()
    control.resolveDelete()
    await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
    expect(gateway.row.draft).toBeNull()
    expect(loadPendingGoalDraft(slot)).toBeNull()
    await reopenShowsNoGoal()
  })

  it('a connection that drops after the server deleted: Retry re-sends the removal and its answer finishes the Clear', async () => {
    const slot = 'chat-clear-answer-lost-after-delete'
    const { gateway, control } = stubGateway()

    renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    await clearWithRemovedFrame(control)
    // The server removed the loop and sent `removed`; the connection dropped
    // before the DELETE's answer arrived, well inside the deadline.
    control.loseDeleteAnswer()
    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    expect(goalBox().value).not.toContain('active loop goal')
    expect(startButton()).toBeDisabled()
    expect(gateway.puts).toHaveLength(0)

    fireEvent.click(retryButton())
    await waitFor(() => expect(control.deletes()).toBe(2))
    // Waiting on the re-sent removal, the cleared goal is still not offered.
    expect(goalBox().value).not.toContain('active loop goal')
    expect(startButton()).toBeDisabled()
    control.resolveDelete()
    await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
    expect(gateway.row.draft).toBeNull()
    expect(loadPendingGoalDraft(slot)).toBeNull()
    await reopenShowsNoGoal()
  })

  it('a connection that drops before the server processed the DELETE: the loop shows the failed Clear, and Retry finishes it', async () => {
    const slot = 'chat-clear-lost-before-delete'
    const { gateway, control } = stubGateway()

    renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    pressClear()
    await waitFor(() => expect(control.deletes()).toBe(1))
    // No `removed` frame: the server never processed the removal.
    control.loseDeleteAnswer()

    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    const resume = screen.getByRole('button', { name: 'Resume loop and nudge now' })
    expect(resume).toBeDisabled()
    expect(resume).toHaveAttribute('aria-describedby', syncError().id)
    expect(gateway.puts).toHaveLength(0)
    expect(gateway.row.draft).toEqual(wireDraft('active loop goal', 90, 3))

    fireEvent.click(retryButton())
    await waitFor(() => expect(control.deletes()).toBe(2))
    control.resolveDelete()
    await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
    expect(gateway.row.draft).toBeNull()
    expect(loadPendingGoalDraft(slot)).toBeNull()
    await reopenShowsNoGoal()
  })

  it('Retry on a loop whose Clear got no answer re-sends it on the captured base, so a goal saved elsewhere is not erased', async () => {
    const slot = 'chat-clear-resend-keeps-base'
    const { gateway, control } = stubGateway()

    renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    pressClear()
    await waitFor(() => expect(control.deletes()).toBe(1))
    // The server removed the loop, but its answer and its `removed` frame were
    // both lost, so the loop is still shown.
    control.loseDeleteAnswer()
    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    // Meanwhile another device saves a new goal on the stored version.
    const other = wireDraft('goal B from another device')
    expect(gateway.apply({ base_updated_at: 5, draft: other }).status).toBe(200)

    fireEvent.click(retryButton())
    await waitFor(() => expect(control.deletes()).toBe(2))
    control.resolveDelete()
    // The tombstone goes out on the version the Clear captured, so it meets the
    // other device's goal as a conflict instead of erasing it.
    await waitFor(() => expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }]))
    expect(gateway.row.draft).toEqual(other)
  })

  it('reopening a paused loop whose Clear got no answer keeps the failed Clear and Resume disabled until Retry finishes it', async () => {
    const slot = 'chat-clear-reopen-keeps-hold'
    const { gateway, control } = stubGateway()

    renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    pressClear()
    await waitFor(() => expect(control.deletes()).toBe(1))
    control.loseDeleteAnswer()
    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.getByTestId('popover-open')).toHaveTextContent('false')

    fireEvent.click(screen.getByTestId('reopen-popover'))
    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    const resume = screen.getByRole('button', { name: 'Resume loop and nudge now' })
    expect(resume).toBeDisabled()
    expect(resume).toHaveAttribute('aria-describedby', syncError().id)

    fireEvent.click(retryButton())
    await waitFor(() => expect(control.deletes()).toBe(2))
    control.resolveDelete()
    await waitFor(() => expect(screen.getByTestId('popover-open')).toHaveTextContent('false'))
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
  })

  it('a loop record changed since its unanswered Clear opens without that Clear, so nothing removes the new record', async () => {
    const slot = 'chat-clear-record-changed'
    const { control } = stubGateway()

    const first = renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    pressClear()
    await waitFor(() => expect(control.deletes()).toBe(1))
    control.loseDeleteAnswer()
    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    first.unmount()

    // Another device edited the paused loop's goal before this tab opened it again.
    renderBridgedPopover(makeLoop({ slot_key: slot, active: false, message: 'goal edited on another device' }), slot)
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(screen.getByRole('button', { name: 'Resume loop and nudge now' })).toBeEnabled()
    expect(control.deletes()).toBe(1)
  })

  it('a re-sent removal that also gets no answer keeps the failed Clear: no goal comes back and Start stays disabled', async () => {
    const slot = 'chat-clear-resend-also-lost'
    const { gateway, control } = stubGateway()

    renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
    await clearWithRemovedFrame(control)
    control.loseDeleteAnswer()
    await waitFor(() => expect(retryButton()).toBeInTheDocument())

    fireEvent.click(retryButton())
    await waitFor(() => expect(control.deletes()).toBe(2))
    control.loseDeleteAnswer()

    await waitFor(() => expect(syncError()).toHaveTextContent(CLEAR_FAILED))
    expect(screen.getByTestId('goal-draft-sync-status')).not.toHaveTextContent('Checking saved goal')
    expect(retryButton()).toBeInTheDocument()
    expect(goalBox().value).not.toContain('active loop goal')
    expect(startButton()).toBeDisabled()
    expect(screen.getByTestId('popover-open')).toHaveTextContent('true')
    expect(gateway.puts).toHaveLength(0)
    expect(gateway.row.draft).toEqual(wireDraft('active loop goal', 90, 3))
  })
})
