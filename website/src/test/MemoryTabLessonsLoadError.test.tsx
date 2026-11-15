/**
 * A lessons list that cannot be read says so, instead of showing "No lessons
 * yet" over lessons that exist; an Add that was stored but whose re-read fails
 * says the lesson was added.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const { api } = vi.hoisted(() => ({
  api: {
    lessons: vi.fn(),
    createLesson: vi.fn(),
    memoryPreferences: vi.fn(),
    memoryProjects: vi.fn(),
    memoryHistory: vi.fn(),
    memorySettings: vi.fn(),
    sessions: vi.fn(),
    memoryStores: vi.fn(),
    memoryRetired: vi.fn(),
    memoryBackups: vi.fn(),
    memoryCarve: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api }))
vi.mock('../pages/overview/VectorMemoryCard', () => ({ SEMANTIC_CARD_ID: 'memory-semantic-card', default: () => <div /> }))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div /> }))
vi.mock('../pages/overview/MemoryRecordsEditor', () => ({ default: () => <div /> }))
const { default: MemoryTab } = await import('../pages/overview/MemoryTab')

beforeEach(() => {
  vi.clearAllMocks()
  api.lessons.mockResolvedValue({ lessons: [] })
  api.memoryPreferences.mockResolvedValue({ content: '' })
  api.memoryProjects.mockResolvedValue({ content: '' })
  api.memoryHistory.mockResolvedValue({ content: '' })
  api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
  api.sessions.mockResolvedValue({ sessions: [] })
  api.memoryStores.mockResolvedValue({ stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }] })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
})

describe('MemoryTab lessons load error', () => {
  it('says the lessons could not be loaded, not that there are none', async () => {
    api.lessons.mockRejectedValue(new Error('HTTP 500'))
    renderWithProviders(<MemoryTab refreshTrigger={0} />)

    expect(await screen.findByText('Could not load the lessons')).toBeInTheDocument()
    expect(screen.getByText('HTTP 500')).toBeInTheDocument()
    expect(screen.queryByText('No lessons yet')).toBeNull()
    // Not dismissible: dismissing would let "No lessons yet" claim a list never read.
    expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull()
  })

  it('says a stored lesson was added when the list then fails to refresh', async () => {
    api.createLesson.mockResolvedValue({ outcome: 'inserted' })
    renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const rule = await screen.findByRole('textbox', { name: 'Rule' })
    await screen.findByText('No lessons yet')

    api.lessons.mockRejectedValue(new Error('HTTP 503'))
    fireEvent.change(rule, { target: { value: 'Always use tabs' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    expect(await screen.findByText('Lesson added, but the list could not be refreshed')).toBeInTheDocument()
    expect(rule).toHaveValue('')
  })

  it('clears the notice once a later read works', async () => {
    api.lessons.mockRejectedValueOnce(new Error('HTTP 500'))
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('Could not load the lessons')

    api.lessons.mockResolvedValue({ lessons: [] })
    rerender(<MemoryTab refreshTrigger={1} />)
    await waitFor(() => expect(screen.queryByText('Could not load the lessons')).toBeNull())
    expect(screen.getByText('No lessons yet')).toBeInTheDocument()
  })

  it('does not let a slow older failure land over a newer read that worked', async () => {
    let rejectFirst: (e: Error) => void = () => {}
    api.lessons.mockImplementationOnce(() => new Promise((_r, reject) => { rejectFirst = reject }))
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(api.lessons).toHaveBeenCalledTimes(1))

    api.lessons.mockResolvedValue({ lessons: [] })
    rerender(<MemoryTab refreshTrigger={1} />)
    await screen.findByText('No lessons yet')
    rejectFirst(new Error('HTTP 500'))
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByText('Could not load the lessons')).toBeNull()
    expect(screen.getByText('No lessons yet')).toBeInTheDocument()
  })

  it('keeps an open lesson editor shown when a refresh then fails', async () => {
    const row = { rule: 'zzq-old-rule', category: 'tool', ts: '2026-01-03T00:00:00Z', scope: 'workspace', workspace: 'ws-1' }
    api.lessons.mockResolvedValue({ lessons: [row] })
    const { rerender } = renderWithProviders(<MemoryTab refreshTrigger={0} />)
    const cell = await screen.findByText('zzq-old-rule')
    fireEvent.click(within(cell.closest('tr')!).getByRole('button', { name: /^Edit$/ }))
    fireEvent.change(screen.getByRole('textbox', { name: /^Edit rule$/ }), { target: { value: 'zzq-draft' } })

    // The row is deleted elsewhere, then the next read fails.
    api.lessons.mockResolvedValue({ lessons: [] })
    rerender(<MemoryTab refreshTrigger={1} />)
    await waitFor(() => expect(api.lessons).toHaveBeenCalledTimes(2))
    api.lessons.mockRejectedValue(new Error('HTTP 500'))
    rerender(<MemoryTab refreshTrigger={2} />)
    await screen.findByText('Could not load the lessons')
    expect(screen.getByRole('textbox', { name: /^Edit rule$/ })).toHaveValue('zzq-draft')
  })
})
