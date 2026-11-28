import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from './helpers'

/**
 * On a phone the Recovery and Failures tabs read as one list: the row's subject,
 * its one action, then one line of what is known, with no placeholder for a
 * value that was never recorded.
 */
const { api } = vi.hoisted(() => ({
  api: {
    memoryRetired: vi.fn(),
    memoryRestoreRetired: vi.fn(),
    recurringFailures: vi.fn(),
    createLesson: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    themes: vi.fn(),
    themeDetail: vi.fn(),
    themeBoot: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api }))
let phone = true
let cardWidth: number | null = null
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => phone }))
vi.mock('../hooks/useContainerWidth', () => ({ useContainerWidth: () => [() => {}, cardWidth] }))
const { default: MemoryRetiredCard } = await import('../pages/overview/MemoryRetiredCard')
const { default: MemoryFailuresCard } = await import('../pages/overview/MemoryFailuresCard')

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset()
  phone = true
  cardWidth = null
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
  api.kirocrewConfig.mockResolvedValue({})
})

describe('phone memory lists', () => {
  it('lists replaced experiences with Restore and only the facts that were recorded', async () => {
    api.memoryRetired.mockResolvedValue({ retired: [
      { id: 'r1', text: 'Ray prefers tabs', superseded_by: 'Ray prefers spaces', retired_times: 2, ts: '2026-11-23T10:00:00Z' },
      { id: 'r2', text: 'Uses zsh', superseded_by: null, retired_times: 1, ts: null },
    ] })
    api.memoryRestoreRetired.mockResolvedValue({ ok: true })
    renderWithProviders(<MemoryRetiredCard store="" />)
    const rows = await screen.findAllByRole('listitem')
    expect(screen.queryByRole('table')).toBeNull()
    expect(rows[0]).toHaveTextContent('Ray prefers tabs')
    expect(rows[0]).toHaveTextContent('Replaced by: Ray prefers spaces')
    expect(rows[0]).toHaveTextContent('Times replaced: 2')
    expect(rows[1]).toHaveTextContent('Times replaced: 1')
    expect(rows[1]).not.toHaveTextContent(/Replaced by|not recorded|unknown/)
    await userEvent.click(within(rows[1]).getByRole('button', { name: 'Restore experience' }))
    expect(api.memoryRestoreRetired).toHaveBeenCalledWith('r2', undefined)
  })

  it.each(['', 'member-test'])('invalidates cached audit rows and counts after restoring in store %s', async store => {
    api.memoryRetired.mockResolvedValue({ retired: [{ id: 'r1', text: 'Restorable experience' }] })
    api.memoryRestoreRetired.mockResolvedValue({ ok: true })
    const { queryClient } = renderWithProviders(<MemoryRetiredCard store={store} />, { queryDefaults: { staleTime: Infinity } })
    const restore = await screen.findByRole('button', { name: 'Restore experience' })
    const auditKey = ['memory-audit', store || 'default', { q: 'experience' }]
    const countsKey = [...auditKey, 'counts']
    queryClient.setQueryData(auditKey, { events: [] })
    queryClient.setQueryData(countsKey, { total: 0 })
    expect(queryClient.getQueryState(auditKey)?.isInvalidated).toBe(false)

    await userEvent.click(restore)

    await waitFor(() => {
      expect(queryClient.getQueryState(auditKey)?.isInvalidated).toBe(true)
      expect(queryClient.getQueryState(countsKey)?.isInvalidated).toBe(true)
    })
    expect(api.memoryRestoreRetired).toHaveBeenCalledWith('r1', store || undefined)
  })

  it.each([true, false])('renders the recorded error through a compact notice and preserves the draft (phone=%s)', async narrow => {
    phone = narrow
    cardWidth = narrow ? 375 : 1200
    api.recurringFailures.mockResolvedValue({ window_days: 7, failures: [
      { signature: 's1', tool_kind: 'mcp', tool: 'work_ledger_read', rule_id: '', error: 'execution_failed', hits: 6, sessions: 4, days: 3, previous_hits: 0, trend: 'new' },
    ] })
    renderWithProviders(<MemoryFailuresCard />)
    const row = await screen.findByTestId('recurring-failure-row')
    expect(row.closest('table') !== null).toBe(!narrow)
    expect(within(row).getByRole('alert')).toHaveTextContent('execution_failed')
    expect(within(row).getByRole('alert')).toHaveClass('inline-flex')
    await userEvent.click(within(row).getByRole('button', { name: /Draft lesson/ }))
    const draft = screen.getByRole('textbox', { name: 'Lesson text' })
    await userEvent.clear(draft)
    await userEvent.type(draft, 'Keep this unsaved lesson')
    expect(within(row).queryByRole('button', { name: /Ask the agent/i })).toBeNull()
    expect(draft).toHaveValue('Keep this unsaved lesson')
  })

  it('shows an empty recovery list as one empty state', async () => {
    api.memoryRetired.mockResolvedValue({ retired: [] })
    renderWithProviders(<MemoryRetiredCard store="" />)
    expect(await screen.findByText('No replaced experiences')).toBeInTheDocument()
  })

  it('lists repeated failures with the reason, the trend, one action and the counts', async () => {
    api.recurringFailures.mockResolvedValue({ window_days: 7, failures: [
      { signature: 's1', tool_kind: 'mcp', tool: 'work_ledger_read', rule_id: '', error: 'execution_failed', hits: 6, sessions: 4, days: 3, previous_hits: 0, trend: 'new' },
    ] })
    renderWithProviders(<MemoryFailuresCard />)
    const row = await screen.findByTestId('recurring-failure-row')
    expect(row).toHaveTextContent('work_ledger_read')
    expect(row).toHaveTextContent('execution_failed')
    expect(row).toHaveTextContent('Times: 6 · Chats: 4 · Days: 3')
    await userEvent.click(within(row).getByRole('button', { name: /Draft lesson/ }))
    expect(within(row).queryByRole('button', { name: /Draft lesson/ })).toBeNull()
    expect(screen.getByTestId('recurring-failure-draft')).toBeInTheDocument()
  })

  it('shows an empty failures list as one empty state', async () => {
    api.recurringFailures.mockResolvedValue({ window_days: 7, failures: [] })
    renderWithProviders(<MemoryFailuresCard />)
    expect(await screen.findByText(/No repeated failures/i)).toBeInTheDocument()
  })

  it('uses the list on a wide screen when the card is too narrow for the table, and the table when it fits', async () => {
    phone = false
    cardWidth = 686
    api.memoryRetired.mockResolvedValue({ retired: [{ id: 'r1', text: 'Ray prefers tabs', superseded_by: null, retired_times: 1, ts: null }] })
    const { unmount } = renderWithProviders(<MemoryRetiredCard store="" />)
    expect(await screen.findByRole('listitem')).toHaveTextContent('Ray prefers tabs')
    expect(screen.queryByRole('table')).toBeNull()
    unmount()
    cardWidth = 1200
    renderWithProviders(<MemoryRetiredCard store="" />)
    expect(await screen.findByRole('table')).toBeInTheDocument()
  })
})
