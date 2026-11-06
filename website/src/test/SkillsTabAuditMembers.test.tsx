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
  skillsAudit: vi.fn(),
}))
// A stub ApiError declared inside vi.hoisted so the mock factory (hoisted above
// the imports) can close over it: createSkill.onError branches on
// `instanceof ApiError` before reading the coded body, so the mock has to
// export something that branch recognizes. Same shape as PromptsTab.test.tsx.
const StubApiError = vi.hoisted(() => class ApiError extends Error {
  status: number
  body: string
  constructor(status: number, message: string, body = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: StubApiError }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

// Controllable viewport: the narrow-viewport back control only renders under
// useIsMobile, and jsdom has no real matchMedia. Default is desktop; the
// mobile-specific tests flip it and restore it themselves.
const isMobileMock = vi.hoisted(() => ({ value: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => isMobileMock.value }))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

// Skip the heavy SkillDirectoryBrowser internals in this tab-level test —
// other tests exercise that component directly.  Render the skill key +
// loaded_by_agents on the probe element so SkillsTab's wiring is testable.
vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: ({ skillKey, skill }: { skillKey: string; skill?: { loaded_by_agents?: string[] } }) => (
    <div
      data-testid="dir-browser"
      data-skill={skillKey}
      data-agents={(skill?.loaded_by_agents || []).join(',')}
    >browser</div>
  ),
}))

import SkillsTab from '../pages/overview/SkillsTab'

// The audit modal (pages/overview/SkillsAuditModal.tsx) mounts after its query
// settles. On a loaded CI shard that can outlast findBy*/waitFor's 1 s default,
// so every wait for the modal, its query or its contents names this timeout.
const AUDIT_MOUNT = { timeout: 5000 }

function renderWithQuery() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  // MemoryRouter: the pending-review panel reads (and clears) the `?review=<slug>`
  // deep link a skill notification points at, so the tab needs a router.
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <MemoryRouter><SkillsTab /></MemoryRouter>
      </QueryClientProvider>,
    ),
    qc,
  }
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody', hash: 'h-x' })
})

// Audit-modal member links and audit cache refreshes. Split from SkillsTab.test.tsx
// (file line cap); the mocks and helpers above are that file's, unchanged.
describe('SkillsTab audit member links and refreshes', () => {
  it('resolves a live member by catalog key, not by another skill display name', async () => {
    // `a-copy` keeps the frontmatter name of the skill it was copied from, and
    // is listed first, so a display-name match would win over the exact key.
    mockApi.skills.mockResolvedValue([
      { key: 'a-copy', name: 'z-original', description: 'copy', source: 'kirocrew', loaded_by_agents: [] },
      { key: 'z-original', name: 'original', description: 'source', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsAudit.mockResolvedValue({
      clusters: [{
        classification: 'duplicate',
        score: 1,
        members: [
          { id: 'live:a-copy', kind: 'live', name: 'a-copy' },
          { id: 'live:z-original', kind: 'live', name: 'z-original' },
        ],
        relations: [],
      }],
    })

    renderWithQuery()
    fireEvent.click(await screen.findByRole('button', { name: 'Find overlapping skills' }))
    fireEvent.click(await screen.findByRole('button', { name: 'z-original' }, AUDIT_MOUNT))

    await waitFor(() => expect(screen.queryByTestId('skills-audit-modal')).not.toBeInTheDocument())
    await waitFor(() =>
      expect(document.getElementById('skill-row-z-original')).toHaveAttribute('aria-current', 'true'),
    )
    expect(document.getElementById('skill-row-a-copy')).not.toHaveAttribute('aria-current')
  })

  it('selects a live member on a phone while another skill detail is open', async () => {
    // On a phone the list pane unmounts while a detail is open, so the member's
    // row is not in the DOM. The link must still select the skill.
    isMobileMock.value = true
    try {
      mockApi.skills.mockResolvedValue([
        { key: 'deploy-one', name: 'deploy-one', description: 'first', source: 'kirocrew', loaded_by_agents: [] },
        { key: 'deploy-two', name: 'deploy-two', description: 'second', source: 'kirocrew', loaded_by_agents: [] },
      ])
      mockApi.skillsAudit.mockResolvedValue({
        clusters: [{
          classification: 'overlapping',
          score: 0.5,
          members: [
            { id: 'live:deploy-one', kind: 'live', name: 'deploy-one' },
            { id: 'live:deploy-two', kind: 'live', name: 'deploy-two' },
          ],
          relations: [],
        }],
      })

      renderWithQuery()
      fireEvent.click(await screen.findByRole('button', { name: 'Select Deploy One' }))
      await waitFor(() => expect(document.getElementById('skill-row-deploy-two')).toBeNull())

      fireEvent.click(screen.getByRole('button', { name: 'Find overlapping skills' }))
      fireEvent.click(await screen.findByRole('button', { name: 'deploy-two' }, AUDIT_MOUNT))

      await waitFor(() => expect(screen.queryByTestId('skills-audit-modal')).not.toBeInTheDocument())
      expect(screen.queryByTestId('skills-audit-selection-failure')).not.toBeInTheDocument()
      // Back to the list: the member the link named is now the selection.
      fireEvent.click(screen.getByRole('button', { name: 'Skills' }))
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Select Deploy Two' })).toHaveAttribute('aria-current', 'true'),
      )
    } finally {
      isMobileMock.value = false
    }
  })

  it('refreshes the overlap audit after a live skill is deleted', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'doomed', name: 'doomed', description: 'will go', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({ name: 'doomed', content: '---\nname: doomed\n---\n', hash: 'h-doomed' })
    mockApi.deleteSkill.mockResolvedValue({ ok: true })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    const { qc } = renderWithQuery()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    fireEvent.click(await screen.findByText('Delete'))
    await waitFor(() => expect(mockApi.deleteSkill).toHaveBeenCalledWith('doomed'))
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['skills-audit'] }))
    confirmSpy.mockRestore()
  })

  it('refreshes the overlap audit after a live skill is saved', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'edited', name: 'edited', description: 'before', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skill.mockResolvedValue({ name: 'edited', content: '---\nname: edited\n---\n', hash: 'h-edited' })
    mockApi.updateSkill.mockResolvedValue({ ok: true })

    const { qc } = renderWithQuery()
    const editBtn = await screen.findByText('Edit')
    await waitFor(() => expect(editBtn).not.toBeDisabled())
    fireEvent.click(editBtn)
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    fireEvent.click(await screen.findByText('Save'))
    await waitFor(() => expect(mockApi.updateSkill).toHaveBeenCalled())
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['skills-audit'] }))
  })
})
