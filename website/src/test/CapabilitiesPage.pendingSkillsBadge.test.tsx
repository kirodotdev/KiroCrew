/**
 * Generated skill candidates wait for approval in the Skills tab's review
 * queue, and that queue renders only while the Skills tab is open. The
 * Capabilities rail's Skills row therefore carries the waiting count, so a user
 * on any other tab (the page opens on Crewmates) can see that something needs
 * them. Pins:
 *  1. a non-empty queue puts a count pill on the Skills row, named for what it
 *     counts, while another tab is showing;
 *  2. an empty queue shows no pill;
 *  3. a queue that failed to load shows no pill and says, through the shared
 *     error notice, that the check failed: a silent 0 would read as "nothing
 *     is waiting". The notice carries no agent hand-off, which would navigate
 *     away from whatever unsaved editor the open tab holds.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import React from 'react'

const mockApi = vi.hoisted(() => ({
  skillsPending: vi.fn(),
  kirocrewConfig: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

// Tab bodies are irrelevant; the rail is what is under test.
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => <div data-testid="crews-pane" /> }))
vi.mock('../pages/HooksPage', () => ({ default: () => <div /> }))
vi.mock('../pages/connections/ConnectionsPage', () => ({ default: () => <div /> }))
vi.mock('../pages/KnowledgePage', () => ({ default: () => <div /> }))
vi.mock('../pages/overview', () => ({
  SkillsTab: () => <div />,
  PromptsTab: () => <div />,
  SteeringTab: () => <div />,
}))

import CapabilitiesPage from '../pages/CapabilitiesPage'

function wrap(initialEntry = '/capabilities') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={qc}>
        <CapabilitiesPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const candidate = (slug: string) => ({ slug, name: `auto/${slug}`, description: '', has_scripts: false, kind: 'new' })

beforeEach(() => {
  mockApi.skillsPending.mockReset()
  mockApi.kirocrewConfig.mockReset()
  mockApi.kirocrewConfig.mockResolvedValue({})
})

describe('CapabilitiesPage: pending skill count on the Skills row', () => {
  it('shows how many generated skills await review while another tab is open', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [candidate('a'), candidate('b')] })
    wrap('/capabilities?tab=crews')
    await waitFor(() => expect(screen.getByTestId('crews-pane')).toBeTruthy())
    const pill = await screen.findByRole('status', { name: 'Skills awaiting review (2)' })
    expect(pill.textContent).toBe('2')
    // The pill sits on the Skills row itself, not elsewhere on the page.
    expect(pill.closest('button')?.textContent).toContain('Skills')
  })

  it('shows no count when nothing is waiting', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    wrap()
    await waitFor(() => expect(mockApi.skillsPending).toHaveBeenCalled())
    await waitFor(() => expect(screen.getAllByRole('button', { name: /Skills/ }).length).toBeGreaterThan(0))
    expect(screen.queryByRole('status', { name: /Skills awaiting review/ })).toBeNull()
  })

  it('says the check failed, without a count, when the queue cannot be read', async () => {
    mockApi.skillsPending.mockRejectedValue(new Error('offline'))
    wrap('/capabilities?tab=crews')
    const notice = await screen.findByTestId('capabilities-pending-skills-failure')
    expect(notice.textContent).toContain("Couldn't check for skills awaiting review")
    expect(notice.textContent).toContain('offline')
    // No hand-off: it would navigate away from an open tab's unsaved editor.
    expect(notice.textContent).not.toContain('Ask the agent')
    expect(screen.queryByRole('status', { name: /Skills awaiting review/ })).toBeNull()
  })

  it('shows no failure notice when the queue reads', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    wrap()
    await waitFor(() => expect(mockApi.skillsPending).toHaveBeenCalled())
    await waitFor(() => expect(screen.getAllByRole('button', { name: /Skills/ }).length).toBeGreaterThan(0))
    expect(screen.queryByTestId('capabilities-pending-skills-failure')).toBeNull()
  })
})
