import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { Skill } from '../types'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillsPending: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('../providers', () => ({
  useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }),
}))

// The detail pane's browser names the open skill, which is all these tests read.
vi.mock('../components/SkillDirectoryBrowser', () => ({
  default: ({ skillKey }: { skillKey: string }) => <div data-testid="dir-browser" data-skill={skillKey}>browser</div>,
}))

import SkillsTab from '../pages/overview/SkillsTab'

const skill = (key: string, description: string): Skill => ({
  key, name: key, description, source: 'kirocrew', loaded_by_agents: [],
} as unknown as Skill)

// The shape of the report: a skill listed first whose description mentions
// the query, then the skill the user actually typed.
const SKILLS = [
  skill('artifacts', 'Save artifacts, widgets and reports'),
  skill('widgets', 'Inline widgets in chat'),
  skill('zebra', 'unrelated'),
]

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><SkillsTab /></MemoryRouter>
    </QueryClientProvider>,
  )
}

const openSkill = () => screen.getByTestId('dir-browser').getAttribute('data-skill')
const row = (name: string) => screen.getByRole('button', { name: `Select ${name}` })
const filterBox = () => screen.getByPlaceholderText(/filter skills/i)

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.skills.mockResolvedValue(SKILLS)
  mockApi.skillsPending.mockResolvedValue([])
  mockApi.skill.mockResolvedValue({ name: 'x', content: '---\nname: x\n---\nbody', hash: 'h' })
})

describe('SkillsTab filter selection (#20262)', () => {
  it('opens the skill whose name matches the filter, not the first row', async () => {
    renderTab()
    await waitFor(() => expect(openSkill()).toBe('artifacts'))

    fireEvent.change(filterBox(), { target: { value: 'widgets' } })

    // Both rows stay listed (the description match is kept) ...
    expect(row('Artifacts')).toBeInTheDocument()
    expect(row('Widgets')).toBeInTheDocument()
    // ... but the open skill is the one that was typed.
    await waitFor(() => expect(openSkill()).toBe('widgets'))
  })

  it('opens a name that contains a partial query', async () => {
    renderTab()
    await waitFor(() => expect(openSkill()).toBe('artifacts'))

    fireEvent.change(filterBox(), { target: { value: 'WIDG' } })
    await waitFor(() => expect(openSkill()).toBe('widgets'))
  })

  it('keeps a skill the user clicked while the filter still shows it', async () => {
    renderTab()
    await waitFor(() => expect(openSkill()).toBe('artifacts'))
    fireEvent.click(row('Artifacts'))

    fireEvent.change(filterBox(), { target: { value: 'widgets' } })
    // Give the selection effect a chance to run before asserting it did nothing.
    await waitFor(() => expect(row('Widgets')).toBeInTheDocument())
    expect(openSkill()).toBe('artifacts')
  })

  it('moves a clicked skill to the best match once the filter hides it', async () => {
    renderTab()
    await waitFor(() => expect(openSkill()).toBe('artifacts'))
    fireEvent.click(row('Zebra'))
    await waitFor(() => expect(openSkill()).toBe('zebra'))

    fireEvent.change(filterBox(), { target: { value: 'widgets' } })
    await waitFor(() => expect(openSkill()).toBe('widgets'))
  })

  it('falls back to the first row when only descriptions match', async () => {
    renderTab()
    await waitFor(() => expect(openSkill()).toBe('artifacts'))
    fireEvent.click(row('Zebra'))

    fireEvent.change(filterBox(), { target: { value: 'reports' } })
    await waitFor(() => expect(openSkill()).toBe('artifacts'))
  })
})
