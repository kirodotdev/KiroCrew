import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { i18nT } from '../i18n/t'
import { confirmAction } from './confirmAction'

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

// The pending list loads only after the skills list settles, so the first control waits on two chained queries.
const CHAINED_QUERY_TIMEOUT_MS = 5000

const row = (slug: string) => ({ slug, name: `auto/${slug}`, description: slug, has_scripts: false, kind: 'new', target: null, base_version: null })

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.skills.mockResolvedValue([])
  mockApi.dismissPendingSkill.mockResolvedValue({ ok: true })
  mockApi.dismissAllPendingSkills.mockResolvedValue({ ok: true })
})

describe('SkillsTab dismissal confirmations', () => {
  it('keeps every candidate when Dismiss All is cancelled', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first')] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><SkillsTab /></MemoryRouter></QueryClientProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss All' }, { timeout: CHAINED_QUERY_TIMEOUT_MS }))
    await confirmAction('Cancel')
    expect(mockApi.dismissAllPendingSkills).not.toHaveBeenCalled()
  })

  it('keeps the Dismiss All question mounted when the pending queue empties', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first')] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><SkillsTab /></MemoryRouter></QueryClientProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss All' }, { timeout: CHAINED_QUERY_TIMEOUT_MS }))
    await screen.findByRole('dialog')
    act(() => { qc.setQueryData(['skills-pending'], { pending: [] }) })
    await waitFor(() => expect(screen.queryByText('Pending review (1)')).not.toBeInTheDocument())
    await confirmAction('Dismiss All')
    await waitFor(() => expect(mockApi.dismissAllPendingSkills).toHaveBeenCalledWith(['first']))
  })

  it('renders the full dismiss question for a long candidate name in a wrapping body', async () => {
    const candidate = { ...row('first'), name: `auto/${'long-candidate-'.repeat(30)}suffix` }
    mockApi.skillsPending.mockResolvedValue({ pending: [candidate] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><SkillsTab /></MemoryRouter></QueryClientProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss', exact: true }, { timeout: CHAINED_QUERY_TIMEOUT_MS }))
    const dialog = await screen.findByRole('dialog', { name: 'Dismiss' })
    const body = within(dialog).getByText(i18nT('pages.overview.skillsTab.dismiss_confirm', { name: candidate.name }), { selector: 'p' })
    expect(body).toHaveStyle({ overflowWrap: 'anywhere' })
    await confirmAction('Cancel')
    expect(mockApi.dismissPendingSkill).not.toHaveBeenCalled()
  })

  it('dismisses with the digest captured before confirmation despite a detail refresh', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first')] })
    mockApi.skillPendingDetail.mockResolvedValue({ content: 'candidate body', content_sha256: 'a'.repeat(64) })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><SkillsTab /></MemoryRouter></QueryClientProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Review', exact: true }, { timeout: CHAINED_QUERY_TIMEOUT_MS }))
    await screen.findByText('candidate body')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss', exact: true }))
    await screen.findByRole('dialog')
    act(() => { qc.setQueryData(['skills-pending-detail', 'first'], { content: 'rewritten body', content_sha256: 'b'.repeat(64) }) })
    await screen.findByText('rewritten body')
    await confirmAction('Dismiss')
    await waitFor(() => expect(mockApi.dismissPendingSkill).toHaveBeenCalledWith('first', 'a'.repeat(64)))
  })

  it('cancels a candidate question if the owning row disappears', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first')] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><SkillsTab /></MemoryRouter></QueryClientProvider>)
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss', exact: true }, { timeout: CHAINED_QUERY_TIMEOUT_MS }))
    await screen.findByRole('dialog')
    act(() => { qc.setQueryData(['skills-pending'], { pending: [] }) })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(mockApi.dismissPendingSkill).not.toHaveBeenCalled()
  })

  it('Dismiss All dismisses the captured candidates and excludes later arrivals', async () => {
    mockApi.skillsPending.mockResolvedValue({ pending: [row('first')] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><SkillsTab /></MemoryRouter></QueryClientProvider>)

    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss All' }, { timeout: CHAINED_QUERY_TIMEOUT_MS }))
    const dialog = await screen.findByRole('dialog', { name: 'Dismiss All' })
    const body = within(dialog).getByText(i18nT('pages.overview.skillsTab.dismiss_all_confirm', { count: 1 }), { selector: 'p' })
    expect(body).toHaveStyle({ overflowWrap: 'anywhere' })

    mockApi.skillsPending.mockResolvedValue({ pending: [row('first'), row('second')] })
    await act(async () => { await qc.invalidateQueries({ queryKey: ['skills-pending'] }) })
    await waitFor(() => expect(screen.getAllByText('auto/second').length).toBeGreaterThan(0))

    await confirmAction('Dismiss All')
    await waitFor(() => expect(mockApi.dismissAllPendingSkills).toHaveBeenCalledTimes(1))
    expect(mockApi.dismissAllPendingSkills).toHaveBeenCalledWith(['first'])
  })
})
