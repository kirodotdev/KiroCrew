import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { __resetForTests, loadPendingGoalDraft, savePendingGoalDraft } from '../utils/goalDrafts'
import { SLOT, goalBox, renderPopover, stubGoalGateway, wireDraft } from './autoNudgePopoverFixtures'

/**
 * A save the store committed whose answer never reached the browser. The edit
 * stays pending on its old version; re-sent, the store's content compare
 * answers the record that already holds it, so no conflict shows.
 */
describe('AutoNudgePopover goal save whose answer was lost', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const startButton = () => screen.getByRole('button', { name: /Start loop/i })

  it('Retry after a committed save whose answer was lost confirms the stored goal and enables Start', async () => {
    const gateway = stubGoalGateway({ draft: wireDraft('original goal'), updated_at: 5 })
    let dropAnswer = true
    gateway.intercept = (url, init) => {
      if (!dropAnswer || !url.startsWith('/api/autonudge/draft/slot/') || init?.method !== 'PUT') return undefined
      dropAnswer = false
      const body = JSON.parse(String(init.body))
      gateway.puts.push(body)
      gateway.apply(body)
      return Promise.reject(new TypeError('Failed to fetch'))
    }
    renderPopover(null)
    await waitFor(() => expect(startButton()).toBeEnabled())
    fireEvent.change(goalBox(), { target: { value: 'my saved goal' } })
    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(/Retry to sync/))
    const committed = gateway.row

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(startButton()).toBeEnabled())
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(goalBox().value).toBe('my saved goal')
    // The re-sent save names the old version and changes nothing on the server.
    expect(gateway.puts.at(-1)).toEqual({ base_updated_at: 5, draft: wireDraft('my saved goal') })
    expect(gateway.row).toBe(committed)
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
  })

  it('reopening over a committed save whose answer was lost shows no conflict and enables Start', async () => {
    savePendingGoalDraft(SLOT, { message: 'my saved goal', idleSecs: 60, maxCycles: 0 }, 5)
    const gateway = stubGoalGateway({ draft: wireDraft('my saved goal'), updated_at: 6 })

    renderPopover(null)

    await waitFor(() => expect(startButton()).toBeEnabled())
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
    expect(goalBox().value).toBe('my saved goal')
    expect(gateway.row.updated_at).toBe(6)
    expect(loadPendingGoalDraft(SLOT)).toBeNull()
  })
})
