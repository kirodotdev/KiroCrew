import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { TAB_ID } from '../api/tabId'
import { __resetForTests, loadPendingGoalDraft } from '../utils/goalDrafts'
import { SLOT, renderPopover, stubGoalGateway, wireDraft } from './autoNudgePopoverFixtures'

// Each pending record names the tab that wrote it; a fixed id keeps its checks exact.
vi.mock('../api/tabId', () => ({ TAB_ID: 'tab-under-test' }))

/** The conflict notice: another device saved a newer goal. */
const CONFLICT = 'A newer goal was saved on another device. Use it, or replace it with your text.'

/**
 * The gateway's create route applies the draft fence: a create naming a version
 * older than the stored draft, with other text, answers the draft PUT's 409.
 * The form reads version 5 on open; the store may hold a newer record by Start.
 */
describe('AutoNudgePopover Start fenced with the draft version the form showed', () => {
  beforeEach(() => {
    localStorage.clear()
    __resetForTests()
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  const startButton = () => screen.getByRole('button', { name: /Start loop/i })

  async function openThenStoreHolds(row: { message: string, updatedAt: number } | null) {
    const gateway = stubGoalGateway({ draft: wireDraft('goal A'), updated_at: 5 })
    const fires: string[] = []
    gateway.intercept = url => {
      if (!url.endsWith('/fire')) return undefined
      fires.push(url)
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) })
    }
    renderPopover(null)
    await waitFor(() => expect(startButton()).toBeEnabled())
    if (row) gateway.row = { draft: wireDraft(row.message), updated_at: row.updatedAt }
    fireEvent.click(screen.getByRole('checkbox', { name: 'Run first cycle now' }))
    await act(async () => { fireEvent.click(startButton()) })
    return { gateway, fires }
  }

  it('an unedited Start over a different goal saved on another device shows the conflict and arms nothing', async () => {
    const { gateway, fires } = await openThenStoreHolds({ message: 'goal B', updatedAt: 9 })

    await waitFor(() => expect(screen.getByTestId('goal-draft-sync-error')).toHaveTextContent(CONFLICT))
    expect(gateway.creates).toHaveLength(1)
    expect(gateway.creates[0]).toMatchObject({ draft_updated_at: 5 })
    expect(fires).toHaveLength(0)
    expect(startButton()).toBeDisabled()
    // The form's text stays pending on the version it showed, for the notice's actions.
    expect(loadPendingGoalDraft(SLOT)).toEqual({ draft: { message: 'goal A', idleSecs: 60, maxCycles: 0 }, base: 5, tab: TAB_ID })
  })

  it('an unedited Start with no newer goal names the version it showed and starts', async () => {
    const { gateway, fires } = await openThenStoreHolds(null)

    await waitFor(() => expect(fires).toHaveLength(1))
    expect(gateway.creates).toEqual([{ slot_key: SLOT, message: 'goal A', idle_secs: 60, max_cycles: 0, draft_updated_at: 5 }])
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
  })

  it('a newer stored goal with the same text starts', async () => {
    const { gateway, fires } = await openThenStoreHolds({ message: 'goal A', updatedAt: 9 })

    await waitFor(() => expect(fires).toHaveLength(1))
    expect(gateway.creates).toHaveLength(1)
    expect(gateway.creates[0]).toMatchObject({ draft_updated_at: 5 })
    expect(screen.queryByTestId('goal-draft-sync-error')).toBeNull()
  })
})
