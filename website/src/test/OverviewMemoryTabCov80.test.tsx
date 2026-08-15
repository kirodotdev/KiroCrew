import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

/**
 * Companion to integration/MemoryTab.integration.test.tsx (which drives the tab
 * against the MSW fixtures). This file stubs the api client directly so the
 * write paths — every Save, the lesson add/delete, and the manual consolidation
 * including its partial-failure branch — are assertable by their calls.
 */
const api = {
  lessons: vi.fn(),
  memoryPreferences: vi.fn(),
  memoryProjects: vi.fn(),
  memoryHistory: vi.fn(),
  memorySettings: vi.fn(),
  saveMemorySettings: vi.fn(),
  saveMemoryPreferences: vi.fn(),
  saveMemoryProjects: vi.fn(),
  saveMemoryHistory: vi.fn(),
  createLesson: vi.fn(),
  deleteLesson: vi.fn(),
  sessions: vi.fn(),
  consolidateMemory: vi.fn(),
}
vi.mock('../api/client', () => ({ api }))
// Both cards own their own queries and their own tests; here they are seams that
// report the vector/migration state this tab branches on.
vi.mock('../pages/overview/VectorMemoryCard', () => ({ default: () => <div data-testid="vector-card" /> }))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => <div data-testid="embed-card" /> }))

const MemoryTab = (await import('../pages/overview/MemoryTab')).default

const LESSONS = [
  { rule: 'zzq-rule-beta', category: 'tool', ts: '2026-01-02T00:00:00Z' },
  { rule: 'zzq-rule-alpha', category: 'knowledge', ts: '2026-01-01T00:00:00Z' },
]

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  api.lessons.mockResolvedValue({ lessons: LESSONS })
  api.memoryPreferences.mockResolvedValue({ content: 'zzq-prefs-body' })
  api.memoryProjects.mockResolvedValue({ content: 'zzq-projects-body' })
  api.memoryHistory.mockResolvedValue({ content: 'zzq-history-body' })
  api.memorySettings.mockResolvedValue({
    history_idle_hours: 4, history_max_days: 30, migrated: false,
  })
  api.saveMemorySettings.mockResolvedValue({ ok: true })
  api.saveMemoryPreferences.mockResolvedValue({ ok: true })
  api.saveMemoryProjects.mockResolvedValue({ ok: true })
  api.saveMemoryHistory.mockResolvedValue({ ok: true })
  api.createLesson.mockResolvedValue({ ok: true })
  api.deleteLesson.mockResolvedValue({ ok: true })
  api.sessions.mockResolvedValue({ sessions: [{ key: 'zzq-s1' }, { key: 'zzq-s2' }] })
  api.consolidateMemory.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.useRealTimers()
})

/** The Save button inside the card whose heading contains `heading`. */
function saveIn(heading: RegExp): HTMLButtonElement {
  const title = screen.getByText(heading)
  const card = title.closest('.card-glow') ?? title.closest('div')?.parentElement
  const button = Array.from((card as HTMLElement).querySelectorAll('button'))
    .find((b) => /save/i.test(b.textContent ?? ''))
  return button as HTMLButtonElement
}

describe('MemoryTab — settings', () => {
  it('loads the saved retention settings and writes both fields back', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    const inputs = await waitFor(() => {
      const found = screen.getAllByRole('spinbutton') as HTMLInputElement[]
      expect(found[0].value).toBe('4')
      return found
    })
    expect(inputs[1].value).toBe('30')

    fireEvent.change(inputs[0], { target: { value: '6' } })
    fireEvent.change(inputs[1], { target: { value: '45' } })
    await userEvent.click(saveIn(/Memory Settings/i))

    await waitFor(() => expect(api.saveMemorySettings).toHaveBeenCalledWith({
      history_idle_hours: 6, history_max_days: 45,
    }))
    expect(await screen.findByText(/Saved/)).toBeInTheDocument()
  })

  it('hides the retention field, and the text-file editors, once memory is migrated', async () => {
    api.memorySettings.mockResolvedValue({
      history_idle_hours: 3, history_max_days: 90, migrated: true,
    })
    render(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(screen.getAllByRole('spinbutton')).toHaveLength(1))
    expect(screen.getByText(/read-only/i)).toBeInTheDocument()
  })

  it('clears the transient Saved marker on its own timer', async () => {
    vi.useFakeTimers()
    render(<MemoryTab refreshTrigger={0} />)
    await act(async () => {})
    const save = saveIn(/Memory Settings/i)
    fireEvent.click(save)
    await act(async () => {})
    expect(save.textContent).toContain('Saved')

    await act(async () => { vi.advanceTimersByTime(2000) })
    expect(save.textContent).not.toContain('Saved')
  })
})

describe('MemoryTab — the three text stores', () => {
  it('loads each store and saves the edited text back to its own endpoint', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    const prefs = await screen.findByRole('textbox', { name: /preferences/i }) as HTMLTextAreaElement
    await waitFor(() => expect(prefs.value).toBe('zzq-prefs-body'))

    fireEvent.change(prefs, { target: { value: 'zzq-prefs-edited' } })
    await userEvent.click(saveIn(/^Preferences$/))
    await waitFor(() => expect(api.saveMemoryPreferences).toHaveBeenCalledWith('zzq-prefs-edited'))

    const projects = screen.getByRole('textbox', { name: /projects/i }) as HTMLTextAreaElement
    fireEvent.change(projects, { target: { value: 'zzq-projects-edited' } })
    await userEvent.click(saveIn(/^Projects$/))
    await waitFor(() => expect(api.saveMemoryProjects).toHaveBeenCalledWith('zzq-projects-edited'))

    const history = screen.getByRole('textbox', { name: /daily history/i }) as HTMLTextAreaElement
    fireEvent.change(history, { target: { value: 'zzq-history-edited' } })
    await userEvent.click(saveIn(/Daily History/i))
    await waitFor(() => expect(api.saveMemoryHistory).toHaveBeenCalledWith('zzq-history-edited'))
  })

  it('re-reads every store when the parent bumps the refresh trigger', async () => {
    const { rerender } = render(<MemoryTab refreshTrigger={0} />)
    await waitFor(() => expect(api.memoryPreferences).toHaveBeenCalled())
    const before = api.memoryPreferences.mock.calls.length
    const lessonsBefore = api.lessons.mock.calls.length
    rerender(<MemoryTab refreshTrigger={1} />)
    await waitFor(() =>
      expect(api.memoryPreferences.mock.calls.length).toBeGreaterThan(before))
    expect(api.lessons.mock.calls.length).toBeGreaterThan(lessonsBefore)
  })

  it('tolerates an empty payload rather than rendering undefined', async () => {
    api.memoryPreferences.mockResolvedValue({})
    render(<MemoryTab refreshTrigger={0} />)
    const prefs = await screen.findByRole('textbox', { name: /preferences/i }) as HTMLTextAreaElement
    await waitFor(() => expect(prefs.value).toBe(''))
  })
})

describe('MemoryTab — lessons', () => {
  it('lists the stored lessons, newest first by default', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const rules = Array.from(document.querySelectorAll('tbody tr td:first-child'))
      .map((td) => td.textContent)
    expect(rules).toEqual(['zzq-rule-beta', 'zzq-rule-alpha'])
  })

  it('re-sorts on a header click', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    await userEvent.click(screen.getByText(/^Rule$/))
    const rules = Array.from(document.querySelectorAll('tbody tr td:first-child'))
      .map((td) => td.textContent)
    expect(rules).toEqual(['zzq-rule-alpha', 'zzq-rule-beta'])

    await userEvent.click(screen.getByText(/^Category$/))
    const cats = Array.from(document.querySelectorAll('tbody tr td:nth-child(2)'))
      .map((td) => td.textContent)
    expect(cats).toEqual(['knowledge', 'tool'])
  })

  it('adds a lesson with the chosen category, then re-reads the list', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const reads = api.lessons.mock.calls.length
    const input = screen.getByPlaceholderText(/Rule/) as HTMLInputElement
    fireEvent.change(input, { target: { value: 'zzq-new-rule' } })
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))

    await waitFor(() => expect(api.createLesson).toHaveBeenCalledWith('zzq-new-rule', 'knowledge'))
    await waitFor(() => expect(api.lessons.mock.calls.length).toBeGreaterThan(reads))
    expect(input.value).toBe('')
  })

  it('refuses to add an empty rule', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    await userEvent.click(screen.getByRole('button', { name: /^Add$/ }))
    expect(api.createLesson).not.toHaveBeenCalled()
  })

  it('deletes the lesson its row names, then re-reads the list', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    await screen.findByText('zzq-rule-beta')
    const reads = api.lessons.mock.calls.length
    const row = screen.getByText('zzq-rule-beta').closest('tr') as HTMLElement
    const del = Array.from(row.querySelectorAll('button'))
      .find((b) => /delete/i.test(b.textContent ?? '')) as HTMLButtonElement
    await userEvent.click(del)

    await waitFor(() => expect(api.deleteLesson).toHaveBeenCalledWith('zzq-rule-beta'))
    await waitFor(() => expect(api.lessons.mock.calls.length).toBeGreaterThan(reads))
  })

  it('shows an empty state rather than a bare table', async () => {
    api.lessons.mockResolvedValue({ lessons: [] })
    render(<MemoryTab refreshTrigger={0} />)
    expect(await screen.findByText(/No lessons yet/i)).toBeInTheDocument()
  })

  it('tolerates a response with no lessons key', async () => {
    api.lessons.mockResolvedValue({})
    render(<MemoryTab refreshTrigger={0} />)
    expect(await screen.findByText(/No lessons yet/i)).toBeInTheDocument()
  })
})

describe('MemoryTab — manual consolidation', () => {
  it('consolidates every known session and reports the count', async () => {
    render(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    await waitFor(() => expect(api.consolidateMemory).toHaveBeenCalledTimes(2))
    expect(api.consolidateMemory).toHaveBeenCalledWith('zzq-s1', true)
    expect(await screen.findByText(/Consolidated/)).toBeInTheDocument()
  })

  it('reports a partial failure instead of claiming success', async () => {
    api.consolidateMemory
      .mockResolvedValueOnce({ ok: true })
      .mockRejectedValueOnce(new Error('zzq-consolidate-failed'))
    render(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))

    const msg = await screen.findByText(/failed/i)
    expect(msg).toBeInTheDocument()
    // The warning tone, not the success one.
    expect((msg.closest('span') as HTMLElement).className).toContain('text-danger')
  })

  it('says there is nothing to consolidate when no session exists', async () => {
    api.sessions.mockResolvedValue({ sessions: [] })
    render(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))
    expect(await screen.findByText(/No sessions to consolidate/i)).toBeInTheDocument()
    expect(api.consolidateMemory).not.toHaveBeenCalled()
  })

  it('treats an unreadable session list as nothing to do rather than crashing', async () => {
    api.sessions.mockRejectedValue(new Error('zzq-sessions-unreachable'))
    render(<MemoryTab refreshTrigger={0} />)
    await userEvent.click(await screen.findByRole('button', { name: /Summarize now/i }))
    expect(await screen.findByText(/No sessions to consolidate/i)).toBeInTheDocument()
  })

  it('clears the outcome message on its own timer', async () => {
    vi.useFakeTimers()
    render(<MemoryTab refreshTrigger={0} />)
    await act(async () => {})
    fireEvent.click(screen.getByRole('button', { name: /Summarize now/i }))
    await act(async () => {})
    expect(screen.getByText(/Consolidated/)).toBeInTheDocument()

    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(screen.queryByText(/Consolidated/)).toBeNull()
  })
})
