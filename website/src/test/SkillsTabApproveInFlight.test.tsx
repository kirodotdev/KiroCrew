import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { namedCeiling } from './namedCeiling'

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

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

/** The queue mounts only after the `skills` read settles and then fetches
 *  `skills-pending` itself: the first Review row waits on two chained queries. */
const pendingRowReady = () => namedCeiling('PENDING_ROW_READY', 5000)

const row = (slug: string) => ({
  slug,
  name: `auto/${slug}`,
  description: `procedure ${slug}`,
  has_scripts: false,
  kind: 'new',
  target: null,
  base_version: null,
})

const detail = (slug: string) => ({
  name: `auto/${slug}`,
  content: `step one of ${slug}`,
  scripts: [],
  content_sha256: 'c'.repeat(64),
})

function approveButtons(): HTMLButtonElement[] {
  return screen.getAllByText('Approve').map(el => el.closest('button')!)
}

/** An approve request that stays in flight until the test settles it. */
function holdApprovals() {
  const settle: Array<(v: unknown) => void> = []
  mockApi.approvePendingSkill.mockImplementation((slug: string) => new Promise(resolve => {
    settle.push(() => resolve({ approved: `auto/${slug}` }))
  }))
  return settle
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillPendingDetail.mockImplementation((slug: string) => Promise.resolve(detail(slug)))
})

describe('SkillsTab Approve while its approval is in flight', () => {
  it('sends one approve request however often Approve is clicked before it settles', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('fresh-skill')] })
    const settle = holdApprovals()
    renderTab()

    fireEvent.click(await screen.findByText('Review', undefined, pendingRowReady()))
    expect(await screen.findByText(/step one of fresh-skill/)).toBeTruthy()
    const [button] = approveButtons()
    expect(button.getAttribute('aria-busy')).toBeNull()

    fireEvent.click(button)
    await waitFor(() => expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(button.getAttribute('aria-busy')).toBe('true'))
    expect(button.getAttribute('aria-disabled')).toBe('true')
    // Busy, not disabled: the button a keyboard user just pressed keeps focus.
    expect(button.disabled).toBe(false)

    fireEvent.click(button)
    fireEvent.click(button)

    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    settle[0](undefined)
    await waitFor(() => expect(screen.queryByText('Approve')).toBeNull())
    expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(1)
  })

  it('sends one approve request when the second click lands before the busy state renders', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('fresh-skill')] })
    const settle = holdApprovals()
    renderTab()

    fireEvent.click(await screen.findByText('Review', undefined, pendingRowReady()))
    expect(await screen.findByText(/step one of fresh-skill/)).toBeTruthy()
    await waitFor(() => expect(approveButtons()[0].disabled).toBe(false))
    const [button] = approveButtons()

    // Both clicks run before React Query's deferred notify re-renders the row.
    fireEvent.click(button)
    fireEvent.click(button)
    // The busy state renders after every click's request has started.
    await waitFor(() => expect(button.getAttribute('aria-busy')).toBe('true'))
    expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(1)

    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    settle.forEach(s => s(undefined))
    await waitFor(() => expect(screen.queryByText('Approve')).toBeNull())
    expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(1)
  })

  it('keeps the first row busy when a second row is approved before the first settles', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first-skill'), row('second-skill')] })
    const settle = holdApprovals()
    renderTab()

    await waitFor(() => expect(screen.getAllByText('Review')).toHaveLength(2), pendingRowReady())
    for (const review of screen.getAllByText('Review')) fireEvent.click(review)
    expect(await screen.findByText(/step one of first-skill/)).toBeTruthy()
    expect(await screen.findByText(/step one of second-skill/)).toBeTruthy()
    await waitFor(() => expect(approveButtons().every(b => !b.disabled)).toBe(true))
    const [first, second] = approveButtons()

    fireEvent.click(first)
    await waitFor(() => expect(first.getAttribute('aria-busy')).toBe('true'))
    expect(second.getAttribute('aria-busy')).toBeNull()

    fireEvent.click(second)
    await waitFor(() => expect(second.getAttribute('aria-busy')).toBe('true'))
    expect(first.getAttribute('aria-busy')).toBe('true')

    fireEvent.click(first)

    mockApi.skillsPending.mockResolvedValue({ pending: [] })
    settle.forEach(s => s(undefined))
    await waitFor(() => expect(screen.queryByText('Approve')).toBeNull())
    expect(mockApi.approvePendingSkill).toHaveBeenCalledTimes(2)
  })
})
