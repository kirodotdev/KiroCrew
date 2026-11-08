import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
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
import { Btn } from '../components/ui'

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
        scripts: [], content_sha256: 'c'.repeat(64),
      })
      .mockResolvedValue({
        name: 'auto/fresh-skill',
        content: 'step one: the body after the edit on disk',
        scripts: [], content_sha256: 'c'.repeat(64),
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
        scripts: [], content_sha256: 'c'.repeat(64),
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
      scripts: [], content_sha256: 'c'.repeat(64),
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
        scripts: [], content_sha256: 'c'.repeat(64),
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

describe('SkillsTab collapsed-row Approve', () => {
  it('looks like a disabled primary button, so a mixed queue shows one disabled look', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    renderWithProductionCachePolicy()
    await screen.findByText('Review', undefined, { timeout: 5000 })
    expect(approveButton()).toHaveAttribute('aria-disabled', 'true')
    render(<Btn primary disabled>Probe</Btn>)
    const disabledLook = screen.getByRole('button', { name: 'Probe' }).className.split(/\s+/)
      .filter(c => c.startsWith('disabled:')).map(c => `aria-${c}`)
    expect(approveButton().className.split(/\s+/)).toEqual(expect.arrayContaining(disabledLook))
  })

  it('says Review comes first on hover and keyboard focus, and its click does nothing', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [NEW_ROW] })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'auto/fresh-skill', content: 'step one', scripts: [], content_sha256: 'c'.repeat(64),
    })
    const { qc } = renderWithProductionCachePolicy()

    // Read once and collapse, so the cached detail holds a sha a click could send.
    // The row waits on the skills -> pending-list chain, Approve on the detail read.
    fireEvent.click(await screen.findByText('Review', undefined, { timeout: 5000 }))
    await waitFor(() => expect(approveButton().disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(screen.getByText('Hide'))

    const hint = 'Review the candidate to enable Approve.'
    const approve = screen.getByRole('button', { name: 'Approve', description: hint })
    expect(approve).toHaveAttribute('aria-disabled', 'true')

    // Real focus: jsdom refuses it on a natively disabled button.
    act(() => approve.focus())
    expect(approve).toHaveFocus()
    expect(screen.getByRole('tooltip')).toHaveTextContent(hint)
    act(() => approve.blur())
    expect(screen.queryByRole('tooltip')).toBeNull()

    fireEvent.mouseEnter(approve)
    expect(await screen.findByRole('tooltip')).toHaveTextContent(hint)

    // `mutate` registers its mutation synchronously; the API call lands a few awaits later.
    fireEvent.click(approve)
    expect(qc.getMutationCache().getAll()).toHaveLength(0)
    expect(mockApi.approvePendingSkill).not.toHaveBeenCalled()
  })
})
