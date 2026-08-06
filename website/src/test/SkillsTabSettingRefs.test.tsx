/**
 * SkillsTab SettingRef call sites — the auto_create_hint chip (always-visible header hint)
 * and approval_required_hint chip (pending panel) must both render as real
 * navigable Links (ui mode) because their config keys are registered in the
 * generated SETTINGS_REGISTRY.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mocks ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillTree: vi.fn(),
  skillFile: vi.fn(),
  createSkill: vi.fn(),
  updateSkill: vi.fn(),
  deleteSkill: vi.fn(),
  skillsPending: vi.fn(),
  approveSkill: vi.fn(),
  dismissSkill: vi.fn(),
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

import SkillsTab from '../pages/overview/SkillsTab'

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => 'mockReset' in m && m.mockReset())
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody' })
  mockApi.skillsPending.mockResolvedValue({ pending: [] })
})

describe('SkillsTab SettingRef call sites', () => {
  it('header hint renders skills.auto_create_from_sessions as a Link to the Skills settings tab', async () => {
    // Return empty skills so the EmptyState renders
    mockApi.skills.mockResolvedValue([])
    const { container } = renderTab()

    await waitFor(() => {
      const links = container.querySelectorAll('a')
      const settingLink = Array.from(links).find(a => {
        const href = a.getAttribute('href') ?? ''
        return href.includes('/settings') && /highlight=key(%3A|:)skills\.auto_create_from_sessions/.test(href)
      })
      expect(settingLink, 'expected SettingRef chip for skills.auto_create_from_sessions to render as an anchor (ui mode)').not.toBeNull()
      expect(settingLink!.textContent).toContain('skills.auto_create_from_sessions')
    })
  })

  it('pending panel renders skills.approval_required as a Link when pending candidates exist', async () => {
    // Need at least one skill so the main list renders (not empty state)
    // and at least one pending candidate so the panel renders
    mockApi.skills.mockResolvedValue([
      { key: 'existing', name: 'existing', description: 'a skill', source: 'kirocrew', loaded_by_agents: [] },
    ])

    // Mock the pending skills API to return a candidate
    mockApi.skillsPending.mockResolvedValue({
      pending: [{ slug: 'test-candidate', name: 'Test Candidate', description: 'a candidate', created: Date.now() }],
    })

    const { container } = renderTab()

    await waitFor(() => {
      const links = container.querySelectorAll('a')
      const settingLink = Array.from(links).find(a => {
        const href = a.getAttribute('href') ?? ''
        return href.includes('/settings') && /highlight=key(%3A|:)skills\.approval_required/.test(href)
      })
      expect(settingLink, 'expected SettingRef chip for skills.approval_required to render as an anchor (ui mode)').not.toBeNull()
      expect(settingLink!.textContent).toContain('skills.approval_required')
    })
  })
})
