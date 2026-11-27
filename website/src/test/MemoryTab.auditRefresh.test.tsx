import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from './helpers'

const { api } = vi.hoisted(() => ({ api: {
  lessons: vi.fn(), createLesson: vi.fn(), deleteLesson: vi.fn(),
  memoryPreferences: vi.fn(), memoryProjects: vi.fn(), memoryHistory: vi.fn(),
  memorySettings: vi.fn(), sessions: vi.fn(), consolidateMemory: vi.fn(),
  memoryStores: vi.fn(), memoryRetired: vi.fn(), memoryBackups: vi.fn(), memoryCarve: vi.fn(),
  recurringFailures: vi.fn(), kirocrewConfig: vi.fn(),
  themes: vi.fn(), themeDetail: vi.fn(), themeBoot: vi.fn(),
} }))
vi.mock('../api/client', () => ({ api }))
vi.mock('../pages/overview/EmbeddingModelCard', () => ({ default: () => null }))
vi.mock('../pages/overview/memoryPage/MemoryEmbeddedLine', () => ({ MemoryEmbeddedLine: () => null }))
vi.mock('../pages/overview/memoryPage/MemoryGraphPanel', () => ({ MemoryGraphPanel: () => null }))
// Keep the Audit tab unobserved, as when the user has left it to perform a write.
vi.mock('../pages/overview/memoryPage/MemoryAudit', () => ({ default: () => null }))
vi.mock('../pages/overview/useVectorMemoryState', () => ({ useVectorMemoryState: () => ({ active: false, settled: true, migrated: false }) }))
const { default: MemoryTab } = await import('../pages/overview/MemoryTab')

beforeEach(() => {
  for (const mock of Object.values(api)) mock.mockReset()
  api.lessons.mockResolvedValue({ lessons: [] })
  api.memoryPreferences.mockResolvedValue({ content: '' })
  api.memoryProjects.mockResolvedValue({ content: '' })
  api.memoryHistory.mockResolvedValue({ content: '' })
  api.memorySettings.mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false })
  api.sessions.mockResolvedValue({ sessions: [{ key: 'session-1' }] })
  api.consolidateMemory.mockResolvedValue({ ok: true })
  api.memoryStores.mockResolvedValue({ active: 'default', stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }] })
  api.memoryRetired.mockResolvedValue({ retired: [] })
  api.memoryBackups.mockResolvedValue({ backups: [] })
  api.memoryCarve.mockResolvedValue({ counts: {} })
  api.kirocrewConfig.mockResolvedValue({})
  api.themes.mockResolvedValue({ themes: [] })
  api.themeDetail.mockResolvedValue({})
  api.themeBoot.mockResolvedValue({})
  api.recurringFailures.mockResolvedValue({ window_days: 7, failures: [{
    signature: 'failure-1', tool_kind: 'mcp', tool: 'read_tool', rule_id: '',
    error: 'execution_failed', hits: 3, sessions: 2, days: 2, previous_hits: 0, trend: 'new',
  }] })
})

async function ready(focus?: string) {
  const result = renderWithProviders(<MemoryTab refreshTrigger={0} focus={focus} />, { queryDefaults: { staleTime: Infinity } })
  await waitFor(() => expect(api.lessons).toHaveBeenCalled())
  await waitFor(() => expect(result.queryClient.isFetching()).toBe(0))
  const auditKey = ['memory-audit', 'default', { q: 'lesson' }]
  const countsKey = [...auditKey, 'counts']
  result.queryClient.setQueryData(auditKey, { events: [] })
  result.queryClient.setQueryData(countsKey, { total: 0 })
  expect(result.queryClient.getQueryState(auditKey)?.isInvalidated).toBe(false)
  return async () => {
    await waitFor(() => {
      expect(result.queryClient.getQueryState(auditKey)?.isInvalidated).toBe(true)
      expect(result.queryClient.getQueryState(countsKey)?.isInvalidated).toBe(true)
    })
    await waitFor(() => expect(result.queryClient.isFetching()).toBe(0))
  }
}

describe('memory writes refresh the cached audit', () => {
  it.each(['inserted', 'enriched'])('refreshes after a repeated-failure lesson is %s', async outcome => {
    api.createLesson.mockResolvedValue({ outcome, reason: '' })
    const assertRefreshed = await ready('failures')
    await userEvent.click(await screen.findByRole('button', { name: /Draft lesson/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Save lesson' }))
    await screen.findByText('Lesson saved')
    await assertRefreshed()
    expect(api.createLesson).toHaveBeenCalledWith(expect.any(String), 'tool')
  })

  it('refreshes even when the saved lesson list cannot be read', async () => {
    api.createLesson.mockResolvedValue({ outcome: 'inserted', reason: '' })
    const assertRefreshed = await ready('failures')
    api.lessons.mockRejectedValue(new Error('lesson read failed'))
    await userEvent.click(await screen.findByRole('button', { name: /Draft lesson/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Save lesson' }))
    await screen.findByText('Lesson saved, but the lesson list could not be refreshed')
    await assertRefreshed()
  })

  it('refreshes after adding a lesson', async () => {
    api.createLesson.mockResolvedValue({ outcome: 'inserted' })
    const assertRefreshed = await ready()
    fireEvent.change(screen.getByRole('textbox', { name: 'Rule' }), { target: { value: 'Use tabs' } })
    await userEvent.click(screen.getByRole('button', { name: 'Add' }))
    await assertRefreshed()
  })

  it.each(['edit', 'delete'])('refreshes after a lesson %s', async action => {
    api.lessons.mockResolvedValue({ lessons: [{ rule: 'Old lesson', category: 'tool', repo_scope: '', scope: 'global', ts: '2026-01-03T00:00:00Z' }] })
    api.createLesson.mockResolvedValue({ outcome: 'replaced' })
    api.deleteLesson.mockResolvedValue({ ok: true })
    const assertRefreshed = await ready()
    const row = (await screen.findByText('Old lesson')).closest('tr')!
    if (action === 'edit') {
      await userEvent.click(within(row).getByRole('button', { name: 'Edit' }))
      fireEvent.change(screen.getByRole('textbox', { name: 'Edit rule' }), { target: { value: 'New lesson' } })
      await userEvent.click(within(row).getByRole('button', { name: 'Save' }))
    } else {
      await userEvent.click(within(row).getByRole('button', { name: 'Delete' }))
    }
    await assertRefreshed()
  })

  it('refreshes after consolidation writes memory', async () => {
    const assertRefreshed = await ready()
    await userEvent.click(screen.getByRole('tab', { name: 'Settings' }))
    await userEvent.click(screen.getByRole('button', { name: /Summarize now/i }))
    await assertRefreshed()
    await act(async () => {})
    expect(api.consolidateMemory).toHaveBeenCalledWith('session-1', true)
  })
})
