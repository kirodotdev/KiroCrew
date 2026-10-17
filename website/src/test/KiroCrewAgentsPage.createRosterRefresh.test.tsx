/**
 * A create that FAILED, opened from the crew manager's door, must leave that
 * page's own roster refreshed.
 *
 * `NewCrewmateDialog` has two front doors reading two different queries: the
 * Crewmates page renders `MEMBERS_ROSTER_QUERY_KEY`
 * (`['kirocrew-agents', 'members-roster']`), and `KiroCrewAgentsPage` renders
 * `['kirocrew-agents']`. React Query invalidates a query when the FILTER key is a
 * PREFIX of the query's key, and the roster leaf is one segment LONGER than the
 * registry key — so the dialog's three failure-reconcile sites, which invalidated
 * the leaf alone, never reached this page. Under the app's `staleTime: Infinity`
 * (api/queryClient.ts) the list behind the dialog then stayed at its pre-request
 * snapshot, and `existingNames` — the roster the dialog refuses a duplicate
 * against before any request, and the premise its reconcile is built on — went
 * stale: a name the create actually committed would still look free, so a
 * resubmit escapes the up-front refusal and reaches the server as a second POST.
 *
 * This pins the fix from the HOST's side: the reconcile invalidates the registry
 * key this page renders as well as the roster leaf. Reverting it fails the first
 * assertion — the page re-reads its roster exactly once (the mount) and never
 * again.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { MEMBERS_ROSTER_QUERY_KEY } from '../api/membersQuery'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const mockApi = vi.hoisted(() => ({
  kirocrewAgents: vi.fn(),
  agentsInstalled: vi.fn(),
  agentCatalog: vi.fn(),
  workspaces: vi.fn(),
  kirocrewConfig: vi.fn(),
  createWorkspace: vi.fn(),
  createKirocrewAgent: vi.fn(),
  updateKirocrewAgent: vi.fn(),
  deleteKirocrewAgent: vi.fn(),
  agentResolvedModel: vi.fn(),
  setDefaultAgent: vi.fn(),
  uploadCrewAvatar: vi.fn(),
  models: vi.fn(),
  members: vi.fn(),
  crons: vi.fn(),
  webhooks: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import KiroCrewAgentsPage from '../pages/KiroCrewAgentsPage'

const ONCALL = {
  name: 'oncall',
  kiro_agent: 'kirocrew',
  workspace: 'default',
  memory_store: 'default',
  triggers: 'incidents',
  session_color: '',
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [ONCALL], default_agent: 'kirocrew' })
  mockApi.agentsInstalled.mockResolvedValue([{ name: 'kirocrew' }])
  mockApi.agentCatalog.mockResolvedValue({ agents: [] })
  mockApi.workspaces.mockResolvedValue({ workspaces: [{ name: 'default', dir: 'workspace' }] })
  mockApi.kirocrewConfig.mockResolvedValue({ memory_stores: { default: {} } })
  mockApi.agentResolvedModel.mockResolvedValue({ model: '' })
  mockApi.models.mockResolvedValue([])
  mockApi.members.mockResolvedValue({ members: [] })
  mockApi.crons.mockResolvedValue({ jobs: [] })
  mockApi.webhooks.mockResolvedValue({ tokens: [] })
})
afterEach(() => { vi.restoreAllMocks(); cleanup() })

/** The page with the shipped cache posture. `staleTime: Infinity` matches
 *  api/queryClient.ts: under the test helper's default of 0 a missed
 *  invalidation is invisible, because nothing had to be invalidated to be
 *  re-read. */
async function openCreateDialog() {
  const rendered = renderWithProviders(<KiroCrewAgentsPage />, {
    queryDefaults: { staleTime: Infinity },
    route: '/capabilities',
  })
  fireEvent.click(await screen.findByTestId('new-crew'))
  await screen.findByTestId('crewmate-create-form')
  return rendered
}

describe('NewCrewmateDialog opened from KiroCrewAgentsPage', () => {
  it('refreshes this page\'s roster when a dropped create is reconciled as taken', async () => {
    const { queryClient } = await openCreateDialog()
    // The Crewmates page's own roster leaf, seeded so this test can see whether
    // that door is still served too: the fix adds a reader, it does not swap one.
    queryClient.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [])
    expect(mockApi.kirocrewAgents).toHaveBeenCalledTimes(1)

    // No answer at all (a dropped connection), and the reconcile read then finds
    // the row — the create may well have committed.
    const configReadsBefore = mockApi.kirocrewConfig.mock.calls.length
    mockApi.createKirocrewAgent.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    mockApi.members.mockResolvedValue({ members: [{ name: 'radar', slug: 'radar' }] })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    expect(await screen.findByTestId('crewmate-create-error')).toHaveTextContent('A crewmate named radar already exists.')

    // The host's list was re-read, so `existingNames` now holds radar and a
    // resubmit is refused here instead of posting a second create.
    expect(mockApi.kirocrewAgents).toHaveBeenCalledTimes(2)
    // And the Crewmates-page door is still invalidated by the same reconcile.
    expect(queryClient.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.isInvalidated).toBe(true)
    // And the crew manager's config-derived list, which the SAME write lands in,
    // is refreshed too: the reconcile must refresh the identical pair the success
    // path does, or a committed-but-lost create stays absent from the config view
    // until a manual reload. (The config query is active on the page, so its
    // invalidation triggers an immediate refetch — observed as a fresh read, not
    // a lingering isInvalidated flag, which an active query clears on refetch.)
    await waitFor(() =>
      expect(mockApi.kirocrewConfig.mock.calls.length).toBeGreaterThan(configReadsBefore),
    )
  })

  it('refreshes this page\'s roster when a dropped create cannot be reconciled at all', async () => {
    const { queryClient } = await openCreateDialog()
    queryClient.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [])
    expect(mockApi.kirocrewAgents).toHaveBeenCalledTimes(1)

    // Neither the request nor the roster read answers: whether the create landed
    // is unknown, which is the case that most needs a fresh roster — the dialog
    // tells the user to check the list, and the list is this page's.
    mockApi.createKirocrewAgent.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    mockApi.members.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'radar' } })
    fireEvent.click(screen.getByTestId('crewmate-create-submit'))
    await screen.findByTestId('crewmate-create-unconfirmed')

    expect(mockApi.kirocrewAgents).toHaveBeenCalledTimes(2)
    expect(queryClient.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.isInvalidated).toBe(true)
  })
})
