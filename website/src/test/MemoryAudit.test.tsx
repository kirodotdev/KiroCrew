import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const api = vi.hoisted(() => ({ memoryEvents: vi.fn() }))
vi.mock('../api/client', () => ({ api }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
const { default: MemoryAudit } = await import('../pages/overview/memoryPage/MemoryAudit')

const FACETS = {
  event: { create: 40, allowlist_reject: 5, low_confidence: 3, injection_blocked: 2 },
  kind: { fact: 30, directive: 12, episode: 8 },
  source: { consolidation: 45, user_explicit: 5 },
}
const REFUSAL = { id: 9, event_type: 'allowlist_reject', memory_type: 'semantic', memory_key: 'pref.deploy.target', old_value: null, new_value: '"prod-us-east-1"', source: 'consolidation:dashboard:chat-1', created_at: '2026-11-25T14:31:07+00:00', kind: 'fact' }
const saved = (id: number) => ({ id, event_type: 'create', memory_type: 'semantic', memory_key: `user.k${id}`, old_value: null, new_value: `"v${id}"`, source: 'user_explicit', created_at: '2026-11-20T10:00:00+00:00', kind: 'fact' })
const lastQuery = () => api.memoryEvents.mock.calls.at(-1)![1]
const pageCalls = () => api.memoryEvents.mock.calls.filter(call => call[3] === 50)

let watched: IntersectionObserverCallback[]
const original = globalThis.IntersectionObserver
beforeEach(() => {
  vi.clearAllMocks()
  sessionStorage.clear(); localStorage.clear()
  watched = []
  globalThis.IntersectionObserver = class { constructor(callback: IntersectionObserverCallback) { watched.push(callback) } observe() {} disconnect() {} unobserve() {} takeRecords() { return [] } } as unknown as typeof IntersectionObserver
  api.memoryEvents.mockImplementation(async (_store: string, _query: unknown, offset: number, limit: number) => limit === 1
    ? { events: [], total: 70, has_more: true, facets: FACETS }
    : { events: offset ? Array.from({ length: 20 }, (_, i) => saved(100 + i)) : [REFUSAL, ...Array.from({ length: 49 }, (_, i) => saved(i))], total: 70, has_more: offset === 0, facets: FACETS })
})
afterEach(() => { globalThis.IntersectionObserver = original })

describe('the Audit tab', () => {
  it('counts every refusal under one Refused item, and ticking it asks for every refusal code', async () => {
    renderWithProviders(<MemoryAudit store="reviewer" />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    const refused = await within(rail).findByRole('checkbox', { name: 'Refused' })
    expect(refused.closest('label')).toHaveTextContent('8')
    expect(within(rail).getByRole('checkbox', { name: 'Blocked: unsafe text' }).closest('label')).toHaveTextContent('2')
    expect(within(rail).getByRole('checkbox', { name: 'Lessons' }).closest('label')).toHaveTextContent('12')
    fireEvent.click(refused)
    await waitFor(() => expect(lastQuery().filters.event).toEqual(['allowlist_reject', 'low_confidence', 'reserved_prefix', 'value_empty', 'work_state', 'consolidation_value_size']))
    expect(api.memoryEvents.mock.calls.every(([store]) => store === 'reviewer')).toBe(true)
    // The pick is a removable chip; Clear all brings every event back.
    expect(screen.getByRole('button', { name: 'Remove filter: Refused' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    await waitFor(() => expect(lastQuery().filters).toEqual({}))
  })

  it('loads the next page as the end comes near and says how many are shown', async () => {
    renderWithProviders(<MemoryAudit store="default" />)
    const list = await screen.findByRole('list', { name: 'Events' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(50)
    expect(screen.getByText('Events: 50 of 70')).toBeVisible()
    act(() => watched.at(-1)!([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(70))
    expect(pageCalls().map(call => call[2])).toEqual([0, 50])
    expect(screen.getByText('Events: 70 of 70')).toBeVisible()
  })

  it('narrows to a date range sent to the server, and its chip brings the whole log back', async () => {
    renderWithProviders(<MemoryAudit store="default" />)
    await screen.findByRole('list', { name: 'Events' })
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Date range' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Last 7 days' }))
    await waitFor(() => expect(lastQuery().since).toBeTruthy())
    const since = new Date(lastQuery().since)
    const start = new Date(); start.setHours(0, 0, 0, 0); start.setDate(start.getDate() - 6)
    expect(since.getTime()).toBe(start.getTime())
    fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Last 7 days' }))
    await waitFor(() => expect(lastQuery().since).toBeUndefined())
  })

  it('opens an event with its value before and after and the rule a refusal broke', async () => {
    renderWithProviders(<MemoryAudit store="default" />)
    const list = await screen.findByRole('list', { name: 'Events' })
    fireEvent.click(within(list).getByText('pref.deploy.target = prod-us-east-1'))
    const detail = await screen.findByTestId('memory-audit-detail')
    expect(within(detail).getByRole('heading', { name: 'Refused: key not allowed' })).toBeInTheDocument()
    expect(within(detail).getByText('allowlist_reject')).toBeInTheDocument()
    const change = within(detail).getByRole('region', { name: 'Change' })
    expect(change).toHaveTextContent('Before—')
    expect(change).toHaveTextContent('Afterprod-us-east-1')
    expect(within(detail).getByRole('region', { name: 'Why' }).textContent!.length).toBeGreaterThan(3)
    // A write that landed has no rule to explain.
    fireEvent.click(within(list).getByText('user.k0 = v0'))
    await waitFor(() => expect(within(screen.getByTestId('memory-audit-detail')).getByRole('heading', { name: 'Saved' })).toBeInTheDocument())
    expect(within(screen.getByTestId('memory-audit-detail')).queryByRole('region', { name: 'Why' })).toBeNull()
  })
})
