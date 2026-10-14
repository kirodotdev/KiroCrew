import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { TAB_ID } from '../api/tabId'
import { __resetForTests, loadPendingGoalDraft, pendingGoalDraftClear } from '../utils/goalDrafts'
import {
  deferred, goalBox, makeLoop, renderBridgedPopover, renderStatefulPopover, stubGoalGateway, wireDraft,
  type FakeGateway,
} from './autoNudgePopoverFixtures'

// Each pending record names the tab that wrote it; a fixed id keeps its checks exact.
vi.mock('../api/tabId', () => ({ TAB_ID: 'tab-under-test' }))

const CLEAR_FAILED = "Couldn't clear this goal on every device yet. Retry to finish clearing it."

const popoverOpen = () => screen.getByTestId('popover-open').textContent
const startButton = () => screen.getByRole('button', { name: /Start loop/i })

function pressClear() {
  fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' }))
  fireEvent.click(screen.getByRole('button', { name: 'Clear' }))
}

/** The stored goal of the paused loop, as version 5. */
const storedGoal = () => stubGoalGateway({ draft: wireDraft('active loop goal', 90, 3), updated_at: 5 })

/** Hold the loop DELETE until the test answers it, ok or failed. */
function holdDelete(gateway: FakeGateway) {
  const removal = deferred<boolean>()
  let started = false
  gateway.intercept = (_url, init) => {
    if (init?.method !== 'DELETE') return undefined
    started = true
    return removal.promise.then(ok => (ok
      ? { ok: true, status: 204, json: () => Promise.resolve({}) }
      : { ok: false, status: 500, json: () => Promise.resolve({ error: 'loop removal failed' }) }))
  }
  return { answer: removal.resolve, started: () => started }
}

describe('AutoNudgePopover goal clear tombstones', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  it('a second device that opened on the live loop saves the tombstone on the stored version and closes once it is confirmed', async () => {
    const slot = 'chat-clear-second-device'
    const gateway = storedGoal()

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    pressClear()

    await waitFor(() => expect(popoverOpen()).toBe('false'))
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
    expect(gateway.row.draft).toBeNull()
    expect(loadPendingGoalDraft(slot)).toBeNull()

    // Reopened on the cleared slot, the form shows the template, not the cleared goal.
    fireEvent.click(screen.getByTestId('reopen-popover'))
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
    expect(goalBox().value).toContain('north star')
    expect(gateway.puts).toHaveLength(1)
  })

  it('a stale device cannot resurrect a cleared goal: its old edit meets the tombstone as a conflict', async () => {
    const slot = 'chat-clear-stale-device'
    const gateway = storedGoal()
    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    pressClear()
    await waitFor(() => expect(gateway.row.draft).toBeNull())

    expect(gateway.apply({ base_updated_at: 5, draft: wireDraft('stale copy') }).status).toBe(409)
    expect(gateway.row.draft).toBeNull()
  })

  it('keeps a failed tombstone write visible with Retry, and Retry finishes the clear', async () => {
    const slot = 'chat-clear-remote-failure'
    const gateway = storedGoal()
    let healthy = false
    gateway.intercept = (url, init) => (!healthy && url.startsWith('/api/autonudge/draft/slot/') && init?.method === 'PUT'
      ? Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({ error: 'temporarily unavailable' }) })
      : undefined)

    renderStatefulPopover(makeLoop({ slot_key: slot, active: false }), slot)
    pressClear()

    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(CLEAR_FAILED))
    expect(popoverOpen()).toBe('true')
    expect(goalBox().value).toContain('north star')
    expect(startButton()).toBeDisabled()
    expect(loadPendingGoalDraft(slot)).toEqual({ draft: null, base: 5, tab: TAB_ID })

    healthy = true
    fireEvent.click(screen.getByRole('button', { name: /^Retry$/i }))
    await waitFor(() => expect(popoverOpen()).toBe('false'))
    expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
    expect(gateway.row.draft).toBeNull()
    expect(loadPendingGoalDraft(slot)).toBeNull()
  })

  describe('through the dashboard bridge, which remounts the editor on the cleared loop', () => {
    it('keeps text typed while the loop removal is pending and saves it on the tombstone', async () => {
      const slot = 'chat-bridge-clear-delete-edit'
      const gateway = storedGoal()
      const removal = holdDelete(gateway)

      renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
      pressClear()
      await waitFor(() => expect(removal.started()).toBe(true))
      fireEvent.change(goalBox(), { target: { value: 'typed while the loop is removed' } })
      removal.answer(true)

      await waitFor(() => expect(gateway.row.draft?.message).toBe('typed while the loop is removed'))
      expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull()
      expect(gateway.puts).toHaveLength(2)
      expect(gateway.puts[0]).toEqual({ base_updated_at: 5, draft: null })
      // The typed edit is made on the cleared slot, so it saves on the tombstone's version.
      expect(gateway.puts[1]).toMatchObject({ base_updated_at: gateway.row.updated_at - 1 })
      await waitFor(() => expect(startButton()).toBeEnabled())
      expect(goalBox().value).toBe('typed while the loop is removed')
      expect(popoverOpen()).toBe('true')
      // The typed edit is this device's own, not another device's update.
      expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement()
      expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    })

    it('with nothing typed ends in the tombstone, closes, and reopens on the default template', async () => {
      const slot = 'chat-bridge-clear-untouched'
      const gateway = storedGoal()

      renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
      pressClear()

      await waitFor(() => expect(popoverOpen()).toBe('false'))
      expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
      fireEvent.click(screen.getByTestId('reopen-popover'))
      await waitFor(() => expect(screen.getByTestId('goal-draft-sync-status')).toBeEmptyDOMElement())
      expect(goalBox().value).toContain('north star')
      expect(gateway.puts).toHaveLength(1)
    })

    describe('when the removed frame remounts the editor before the DELETE answers', () => {
      async function removedFrameBeforeDeleteAnswers(started: () => boolean) {
        await waitFor(() => expect(started()).toBe(true))
        fireEvent.click(screen.getByTestId('remove-automation'))
        await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear stopped goal' })).toBeNull())
      }

      it('waits for the Clear read-only, never offers the cleared goal to Start, then closes on the template', async () => {
        const slot = 'chat-bridge-clear-removed-first'
        const gateway = storedGoal()
        const removal = holdDelete(gateway)

        renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
        pressClear()
        await removedFrameBeforeDeleteAnswers(removal.started)
        expect(screen.getByTestId('goal-draft-sync-status')).toHaveTextContent('Checking saved goal')
        expect(goalBox()).toBeDisabled()
        expect(startButton()).toBeDisabled()
        removal.answer(true)

        await waitFor(() => expect(popoverOpen()).toBe('false'))
        expect(gateway.puts).toEqual([{ base_updated_at: 5, draft: null }])
        expect(gateway.row.draft).toBeNull()
        expect(pendingGoalDraftClear(slot)).toBeUndefined()
      })

      it('shows the failed clear with Retry when the DELETE then fails, and writes no tombstone', async () => {
        const slot = 'chat-bridge-clear-removed-delete-fails'
        const gateway = storedGoal()
        const removal = holdDelete(gateway)

        renderBridgedPopover(makeLoop({ slot_key: slot, active: false }), slot)
        pressClear()
        await removedFrameBeforeDeleteAnswers(removal.started)
        removal.answer(false)

        await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(CLEAR_FAILED))
        expect(screen.getByRole('button', { name: /^Retry$/i })).toBeInTheDocument()
        expect(startButton()).toBeDisabled()
        expect(popoverOpen()).toBe('true')
        expect(pendingGoalDraftClear(slot)).toBeUndefined()
        expect(gateway.puts).toHaveLength(0)
        expect(gateway.row.draft).toEqual(wireDraft('active loop goal', 90, 3))
      })
    })
  })
})
