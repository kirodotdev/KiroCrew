import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { __resetForTests, loadPendingGoalDraft, savePendingGoalDraft } from '../utils/goalDrafts'
import { SLOT, deferred, goalBox, renderPopover, stubGoalGateway, wireDraft, type WireDraft } from './autoNudgePopoverFixtures'

const CONFLICT = 'A newer goal was saved on another device. Use it, or replace it with your text.'
const CLEARED = 'This goal was cleared on another device. Keep it cleared, or replace it with your text.'

const startButton = () => screen.getByRole('button', { name: /Start loop/i })
const syncError = () => screen.queryByTestId('goal-draft-sync-error')

/** This browser holds an edit it made on version 3; another device has since
 *  saved `confirmed` as version 7. */
function stubNewerRecord(confirmed: WireDraft | null) {
  savePendingGoalDraft(SLOT, { message: 'my browser edit', idleSecs: 60, maxCycles: 0 }, 3)
  return stubGoalGateway({ draft: confirmed, updated_at: 7 })
}

describe('AutoNudgePopover goal-draft conflict actions', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const confirmedGoal = wireDraft('goal confirmed on another device', 90, 4)

  it('an edit made on an older version is a conflict with two actions, never a silent overwrite or Retry', async () => {
    const gateway = stubNewerRecord(confirmedGoal)
    renderPopover(null)

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(gateway.puts).toEqual([{ base_updated_at: 3, draft: wireDraft('my browser edit') }])
    expect(gateway.row.draft).toEqual(confirmedGoal)
    expect(goalBox().value).toBe('my browser edit')
    expect(screen.getByRole('button', { name: 'Use newer goal' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Replace with my text' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: /^Retry$/i })).toBeNull()
    expect(startButton()).toBeDisabled()
    expect(startButton()).toHaveAttribute('aria-describedby', syncError()!.id)
  })

  it('a reopen keeps the refused edit as the conflict and leaves the newer record in place', async () => {
    const gateway = stubNewerRecord(confirmedGoal)
    const first = renderPopover(null)
    await screen.findByTestId('goal-draft-sync-error')
    first.unmount()

    renderPopover(null)

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(goalBox().value).toBe('my browser edit')
    expect(gateway.row).toEqual({ draft: confirmedGoal, updated_at: 7 })
    expect(startButton()).toBeDisabled()
  })

  it('Use newer goal shows the confirmed goal, drops the pending edit without a write, and Start runs it', async () => {
    const gateway = stubNewerRecord(confirmedGoal)
    renderPopover(null)
    await screen.findByTestId('goal-draft-sync-error')

    fireEvent.click(screen.getByRole('button', { name: 'Use newer goal' }))

    await waitFor(() => expect(goalBox().value).toBe(confirmedGoal.message))
    expect(syncError()).toBeNull()
    expect(gateway.puts).toHaveLength(1)
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
    await waitFor(() => expect(startButton()).toBeEnabled())
    await act(async () => { fireEvent.click(startButton()) })
    await waitFor(() => expect(gateway.creates).toHaveLength(1))
    expect(gateway.creates[0]).toMatchObject({ slot_key: SLOT, message: confirmedGoal.message, draft_updated_at: 7 })
  })

  it('Replace with my text is the explicit compare-and-set on the confirmed goal', async () => {
    const gateway = stubNewerRecord(confirmedGoal)
    renderPopover(null)
    await screen.findByTestId('goal-draft-sync-error')

    fireEvent.click(screen.getByRole('button', { name: 'Replace with my text' }))

    await waitFor(() => expect(syncError()).toBeNull())
    expect(gateway.puts.at(-1)).toEqual({ base_updated_at: 7, draft: wireDraft('my browser edit') })
    expect(gateway.row.draft).toEqual(wireDraft('my browser edit'))
    expect(goalBox().value).toBe('my browser edit')
    await waitFor(() => expect(startButton()).toBeEnabled())
  })

  it('a Clear confirmed on another device offers the Keep it cleared its notice names, and it keeps the goal cleared', async () => {
    const gateway = stubNewerRecord(null)
    renderPopover(null)

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CLEARED)
    expect(screen.getByRole('button', { name: 'Keep it cleared' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Replace with my text' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Use newer goal' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Keep it cleared' }))

    await waitFor(() => expect(syncError()).toBeNull())
    expect(goalBox().value).not.toBe('my browser edit')
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
    expect(gateway.puts).toHaveLength(1)
    expect(gateway.row.draft).toBeNull()
  })

  it('a conflict answered after the user typed again changes nothing in the editor and still blocks Start', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('original shared goal'), updated_at: 5 })
    const put = deferred<void>()
    gateway.intercept = (url, init) => {
      if (!url.startsWith('/api/autonudge/draft/slot/') || init?.method !== 'PUT') return undefined
      gateway.puts.push(JSON.parse(String(init.body)))
      return put.promise.then(() => gateway.apply(JSON.parse(String(init.body))))
    }
    renderPopover(null)
    await waitFor(() => expect(startButton()).toBeEnabled())
    // Another device saves after this editor read the store.
    gateway.row = { draft: wireDraft('goal saved on another device'), updated_at: 6 }
    fireEvent.change(goalBox(), { target: { value: 'my edit' } })
    await waitFor(() => expect(gateway.puts).toHaveLength(1))
    fireEvent.change(goalBox(), { target: { value: 'my edit, then more' } })

    await act(async () => { put.resolve() })

    expect(await screen.findByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT)
    expect(goalBox().value).toBe('my edit, then more')
    expect(startButton()).toBeDisabled()
  })
})
