import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillTree: vi.fn(),
  skillFile: vi.fn(),
  createSkill: vi.fn(),
  updateSkill: vi.fn(),
  deleteSkill: vi.fn(),
  skillsPending: vi.fn(),
  skillPendingDetail: vi.fn(),
  approvePendingSkill: vi.fn(),
  dismissPendingSkill: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: () => <div data-testid="dir-browser">browser</div>,
}))

vi.mock('../components/DiffBlock', () => ({
  default: ({ code }: { code: string }) => <pre data-testid="diff">{code}</pre>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

/** Pins production's `staleTime: Infinity` (api/queryClient.ts) — without it a reopen refetches even unfixed. */
function renderWithProductionCachePolicy() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  const view = render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
  return { qc, view }
}

const NEW_ROW = {
  slug: 'fresh-skill',
  name: 'auto/fresh-skill',
  description: 'brand new procedure',
  has_scripts: false,
  kind: 'new',
  target: null,
  base_version: null,
}

const DETAIL_KEY = ['skills-pending-detail', 'fresh-skill']

function approveButton(): HTMLButtonElement {
  return screen.getByText('Approve').closest('button')!
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillsPending.mockResolvedValue({ pending: [] })
})

describe('SkillsTab pending-review detail freshness', () => {
  it('shows the candidate as it is on disk NOW when the row is reopened', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    // The candidate is edited on disk between the two opens.
    mockApi.skillPendingDetail
      .mockResolvedValueOnce({
        name: 'auto/fresh-skill',
        content: 'step one: the body as first loaded',
        scripts: [],
      })
      .mockResolvedValue({
        name: 'auto/fresh-skill',
        content: 'step one: the body after the edit on disk',
        scripts: [],
      })

    renderWithProductionCachePolicy()

    fireEvent.click(await screen.findByText('Review'))
    expect(await screen.findByText(/the body as first loaded/)).toBeTruthy()

    fireEvent.click(screen.getByText('Hide'))
    fireEvent.click(await screen.findByText('Review'))

    expect(await screen.findByText(/the body after the edit on disk/)).toBeTruthy()
    await waitFor(() =>
      expect(screen.queryByText(/the body as first loaded/)).toBeNull(),
    )
    expect(mockApi.skillPendingDetail).toHaveBeenCalledTimes(2)
  })

  it('holds Approve while the reopen is still re-reading the candidate', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    // The second read never settles, so the row stays in the window where the first body is still painted.
    let releaseSecondRead: ((d: unknown) => void) | undefined
    mockApi.skillPendingDetail
      .mockResolvedValueOnce({
        name: 'auto/fresh-skill',
        content: 'step one: the body as first loaded',
        scripts: [],
      })
      .mockImplementation(() => new Promise(resolve => { releaseSecondRead = resolve }))

    const { qc } = renderWithProductionCachePolicy()

    fireEvent.click(await screen.findByText('Review'))
    expect(await screen.findByText(/the body as first loaded/)).toBeTruthy()
    expect(approveButton().disabled).toBe(false)

    fireEvent.click(screen.getByText('Hide'))
    fireEvent.click(await screen.findByText('Review'))

    // Settle on the state under test; asserting straight out of waitFor would pass on one unrelated frame.
    await waitFor(() =>
      expect(qc.getQueryState(DETAIL_KEY)?.fetchStatus).toBe('fetching'),
    )
    expect(approveButton().disabled).toBe(true)
    expect(screen.getByText(/the body as first loaded/)).toBeTruthy()

    releaseSecondRead!({
      name: 'auto/fresh-skill',
      content: 'step one: the body after the edit on disk',
      scripts: [],
    })

    expect(await screen.findByText(/the body after the edit on disk/)).toBeTruthy()
    await waitFor(() => expect(approveButton().disabled).toBe(false))
    expect(mockApi.approvePendingSkill).not.toHaveBeenCalled()
  })

  it('reports a failed re-read and keeps Approve held over the old body', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    // A failed refetch keeps the previous data, so `!detail` cannot see it.
    mockApi.skillPendingDetail
      .mockResolvedValueOnce({
        name: 'auto/fresh-skill',
        content: 'step one: the body as first loaded',
        scripts: [],
      })
      .mockRejectedValue(new Error('could not read the candidate'))

    const { qc } = renderWithProductionCachePolicy()

    fireEvent.click(await screen.findByText('Review'))
    expect(await screen.findByText(/the body as first loaded/)).toBeTruthy()

    fireEvent.click(screen.getByText('Hide'))
    fireEvent.click(await screen.findByText('Review'))

    // Settle on the failure itself, not on the in-flight frame before it.
    await waitFor(() => {
      const state = qc.getQueryState(DETAIL_KEY)
      expect(state?.status).toBe('error')
      expect(state?.fetchStatus).toBe('idle')
    })
    // The failure is rendered, not swallowed, and it names the read that failed.
    expect(await screen.findByText(/could not read the candidate/)).toBeTruthy()
    // The stale body is still up, which is why Approve must stay held.
    expect(screen.getByText(/the body as first loaded/)).toBeTruthy()
    expect(approveButton().disabled).toBe(true)
    expect(mockApi.approvePendingSkill).not.toHaveBeenCalled()
  })
})
