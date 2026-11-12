import { useState } from 'react'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '../../test/helpers'
import MemoryFailuresCard from './MemoryFailuresCard'
import { ApiError } from '../../api/apiError'

const { api } = vi.hoisted(() => ({
  api: {
    recurringFailures: vi.fn(),
    createLesson: vi.fn(),
    // ThemeProvider in the shared wrapper reads these on mount.
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
  },
}))
vi.mock('../../api/client', () => ({ api }))

const failure = {
  signature: 'mcp|work_ledger_read||execution_failed',
  tool_kind: 'mcp',
  tool: 'work_ledger_read',
  rule_id: '',
  error: 'execution_failed',
  hits: 6,
  sessions: 4,
  days: 3,
  previous_hits: 0,
  trend: 'new' as const,
}

beforeEach(() => {
  vi.clearAllMocks()
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
  api.recurringFailures.mockResolvedValue({ failures: [failure], window_days: 7 })
})

describe('MemoryFailuresCard', () => {
  it('writes nothing until the owner saves the draft, then saves the edited text as a tool lesson', async () => {
    const onLessonSaved = vi.fn()
    api.createLesson.mockResolvedValue({ ok: true, outcome: 'inserted', reason: '' })
    renderWithProviders(<MemoryFailuresCard onLessonSaved={onLessonSaved} />)

    await userEvent.click(await screen.findByRole('button', { name: /Draft lesson/ }))
    const box = screen.getByRole('textbox', { name: 'Lesson text' })
    expect(box).toHaveValue('work_ledger_read keeps failing. Check why before you try it again.')
    expect(api.createLesson).not.toHaveBeenCalled()

    await userEvent.clear(box)
    await userEvent.type(box, 'Read the work ledger once per cycle')
    await userEvent.click(screen.getByRole('button', { name: 'Save lesson' }))

    expect(api.createLesson).toHaveBeenCalledWith('Read the work ledger once per cycle', 'tool')
    expect(await screen.findByText('Lesson saved')).toBeInTheDocument()
    expect(onLessonSaved).toHaveBeenCalledTimes(1)
  })

  it('keeps the draft and says why when the lesson is refused', async () => {
    api.createLesson.mockResolvedValue({ ok: false, outcome: 'refused', reason: 'injection_detected' })
    renderWithProviders(<MemoryFailuresCard />)

    await userEvent.click(await screen.findByRole('button', { name: /Draft lesson/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Save lesson' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('injection_detected')
    expect(screen.getByRole('textbox', { name: 'Lesson text' })).toBeInTheDocument()
  })

  it('opens one draft at a time and reports it as unsaved work', async () => {
    const onDirtyChange = vi.fn()
    api.recurringFailures.mockResolvedValue({ failures: [failure, { ...failure, signature: 'second', tool: 'learn_add' }], window_days: 7 })
    renderWithProviders(<MemoryFailuresCard onDirtyChange={onDirtyChange} />)

    const [first] = await screen.findAllByRole('button', { name: /Draft lesson/ })
    await userEvent.click(first)

    const rest = screen.getAllByRole('button', { name: /Draft lesson/ })
    expect(rest).toHaveLength(1)
    expect(rest[0]).toBeDisabled()
    expect(onDirtyChange).toHaveBeenLastCalledWith(true)
  })

  it('reports an open draft once to a page that passes a fresh callback each render', async () => {
    const seen: boolean[] = []
    function Page() {
      const [, setDirty] = useState<Record<string, boolean>>({})
      // Bounded: a looping card stops re-rendering the page after 20 reports.
      return <MemoryFailuresCard onDirtyChange={d => { seen.push(d); if (seen.length <= 20) setDirty(old => ({ ...old, failures: d })) }} />
    }
    renderWithProviders(<Page />)

    await userEvent.click(await screen.findByRole('button', { name: /Draft lesson/ }))

    expect(screen.getByRole('textbox', { name: 'Lesson text' })).toBeInTheDocument()
    expect(seen).toEqual([false, true])
  })

  it('says so when the lesson saved but the list could not be re-read', async () => {
    api.createLesson.mockResolvedValue({ ok: true, outcome: 'inserted', reason: '' })
    renderWithProviders(<MemoryFailuresCard onLessonSaved={() => Promise.reject(new Error('gateway down'))} />)

    await userEvent.click(await screen.findByRole('button', { name: /Draft lesson/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Save lesson' }))

    expect(await screen.findByText('Lesson saved, but the lesson list could not be refreshed')).toBeInTheDocument()
    expect(screen.getByText('Lesson saved')).toBeInTheDocument()
  })

  it('keeps an open draft, with Save and Cancel, outside the sideways-scrolling table', async () => {
    renderWithProviders(<MemoryFailuresCard />)

    await userEvent.click(await screen.findByRole('button', { name: /Draft lesson/ }))

    const box = screen.getByRole('textbox', { name: 'Lesson text' })
    expect(box.closest('table')).toBeNull()
    expect(screen.getByRole('button', { name: 'Save lesson' }).closest('table')).toBeNull()
    expect(screen.getByTestId('recurring-failure-draft')).toHaveTextContent('work_ledger_read')
  })

  it('shows the empty state when nothing recurred', async () => {
    api.recurringFailures.mockResolvedValue({ failures: [], window_days: 7 })
    renderWithProviders(<MemoryFailuresCard />)

    expect(await screen.findByText('No repeated failures')).toBeInTheDocument()
  })

  it('re-reads the findings when the page refresh signal changes, not on mount', async () => {
    const { rerender } = renderWithProviders(<MemoryFailuresCard refreshTrigger={0} />)
    await screen.findAllByRole('button', { name: /Draft lesson/ })
    expect(api.recurringFailures).toHaveBeenCalledTimes(1)

    rerender(<MemoryFailuresCard refreshTrigger={1} />)

    await waitFor(() => expect(api.recurringFailures).toHaveBeenCalledTimes(2))
  })

  it('reads the findings again when the card mounts again on a warm cache', async () => {
    // The app's shared client keeps reads fresh forever.
    const { rerender } = renderWithProviders(<MemoryFailuresCard key="a" />, { queryDefaults: { staleTime: Infinity } })
    await screen.findAllByRole('button', { name: /Draft lesson/ })

    rerender(<MemoryFailuresCard key="b" />)

    await waitFor(() => expect(api.recurringFailures).toHaveBeenCalledTimes(2))
  })
  it.each([
    ['a teammate (403 owner_only)', 403, 'owner_only'],
    ['a pre-owner session (401 stale_session_reauth)', 401, 'stale_session_reauth'],
  ])('hides the card from %s instead of showing a load failure', async (_who, status, code) => {
    api.recurringFailures.mockRejectedValue(new ApiError(status, 'owner only', JSON.stringify({ error: 'owner only', code })))
    const { container } = renderWithProviders(<MemoryFailuresCard />)

    await waitFor(() => expect(api.recurringFailures).toHaveBeenCalled())
    await waitFor(() => expect(container.textContent).toBe(''))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows the card again once the re-auth banner signs a pre-owner session in', async () => {
    api.recurringFailures.mockRejectedValueOnce(new ApiError(401, 'stale', JSON.stringify({ error: 'stale', code: 'stale_session_reauth' })))
    const { container, queryClient } = renderWithProviders(<MemoryFailuresCard />)
    await waitFor(() => expect(container.textContent).toBe(''))

    // The banner's recovery re-reads only the queries a lapse left in error.
    await queryClient.invalidateQueries({ predicate: q => q.state.status === 'error' && q.state.data === undefined })

    expect(await screen.findByText('work_ledger_read')).toBeInTheDocument()
  })

  it('still reports a load failure that is not the owner gate', async () => {
    api.recurringFailures.mockRejectedValue(new ApiError(500, 'boom', JSON.stringify({ error: 'boom' })))
    renderWithProviders(<MemoryFailuresCard />)

    expect(await screen.findByRole('alert')).toHaveTextContent('boom')
  })
})
