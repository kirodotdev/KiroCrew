/**
 * SkillsTab SettingRef call sites — the auto_create_hint chip (always-visible header hint)
 * and approval_required_hint chip (pending panel) must both render as real
 * navigable Links (ui mode) because their config keys are registered in the
 * generated SETTINGS_REGISTRY.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/react'
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
  skillPendingDetail: vi.fn(),
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

  it('pending panel hint surfaces the auto-approve opt-out and the script caveat', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'existing', name: 'existing', description: 'a skill', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsPending.mockResolvedValue({
      pending: [{ slug: 'test-candidate', name: 'Test Candidate', description: 'a candidate', has_scripts: false, created: Date.now() }],
    })

    const { container } = renderTab()

    await waitFor(() => {
      // The hint must SAY prose-only skills can auto-publish via the setting —
      // the discoverability gap in #3927 was a link labelled only "required
      // by", which reads as immutable fact rather than an opt-out. Wording is
      // state-neutral ("when ... is off") so it stays true whether the user
      // already disabled approval or not.
      expect(container.textContent).toContain('can go live automatically when')
      expect(container.textContent).toContain('skills that bundle scripts always require review')
    })
  })

  it('script-bearing candidates carry the always-requires-review explanation', async () => {
    mockApi.skills.mockResolvedValue([
      { key: 'existing', name: 'existing', description: 'a skill', source: 'kirocrew', loaded_by_agents: [] },
    ])
    mockApi.skillsPending.mockResolvedValue({
      pending: [
        { slug: 'prose-only', name: 'Prose Only', description: 'no scripts', has_scripts: false, created: Date.now() },
        { slug: 'with-scripts', name: 'With Scripts', description: 'bundles a script', has_scripts: true, created: Date.now() },
      ],
    })
    mockApi.skillPendingDetail.mockResolvedValue({
      name: 'With Scripts',
      content: '---\nname: with-scripts\n---\nbody',
      scripts: [{ filename: 'run.sh', content: 'echo hi' }],
    })

    const { container, getAllByText } = renderTab()

    // Collapsed: the badge is a plain marker; the explanation is NOT hidden
    // behind hover (no title) — it renders as visible text once expanded.
    await waitFor(() => {
      expect(getAllByText('script')).toHaveLength(1)
      expect(container.textContent).not.toContain('Bundled scripts always require manual review')
    })

    // Expanded row: the explanation renders as visible text.
    const reviewBtns = getAllByText('Review')
    // The script-bearing row is second in the fixture; open it.
    fireEvent.click(reviewBtns[1])
    await waitFor(() => {
      expect(container.textContent).toContain('Bundled scripts always require manual review')
    })
  })
})
