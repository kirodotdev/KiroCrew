import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  skill: vi.fn(),
  skillTree: vi.fn(),
  skillFile: vi.fn(),
  skillsPending: vi.fn(),
  skillPendingDetail: vi.fn(),
  approvePendingSkill: vi.fn(),
  dismissPendingSkill: vi.fn(),
  dismissAllPendingSkills: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../providers', () => ({ useProvider: () => ({ labels: { pluginRegistryName: 'Packages' } }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <div>{content}</div> }))
vi.mock('../components/SkillDirectoryBrowser', () => ({ default: () => null }))

import SkillsTab from '../pages/overview/SkillsTab'

const row = (slug: string) => ({ slug, name: `auto/${slug}`, description: slug, has_scripts: false, kind: 'new', target: null, base_version: null })

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.dismissAllPendingSkills.mockResolvedValue({ ok: true })
})

describe('SkillsTab Dismiss all', () => {
  it('dismisses only the candidates its question named, not one staged while it was open', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first')] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><SkillsTab /></MemoryRouter></QueryClientProvider>)

    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss All' }))
    const dialog = await screen.findByRole('dialog')

    // A second candidate arrives while the question is open.
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first'), row('second')] })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['skills-pending'] }) })
    await waitFor(() => expect(screen.getAllByText('auto/second').length).toBeGreaterThan(0))

    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Dismiss All' })) })
    await waitFor(() => expect(mockApi.dismissAllPendingSkills).toHaveBeenCalledTimes(1))
    expect(mockApi.dismissAllPendingSkills).toHaveBeenCalledWith(['first'])
  })
})
