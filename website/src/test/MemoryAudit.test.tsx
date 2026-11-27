import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'

const api = vi.hoisted(() => ({ memoryEvents: vi.fn() }))
vi.mock('../api/client', () => ({ api }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
const { default: MemoryAudit } = await import('../pages/overview/memoryPage/MemoryAudit')
const { eventLabel } = await import('../pages/overview/memoryPage/memoryAuditLabels')

const FACETS = {
  event: { create: 40, allowlist_reject: 5, low_confidence: 3, injection_blocked: 2 },
  kind: { fact: 30, directive: 12, episode: 8 },
  source: { consolidation: 45, user_explicit: 5 },
}
const REFUSAL = { id: 9, event_type: 'allowlist_reject', memory_type: 'semantic', memory_key: 'pref.deploy.target', old_value: null, new_value: '"prod-us-east-1"', source: 'consolidation:dashboard:chat-1', created_at: '2026-11-25T14:31:07+00:00', kind: 'fact' }
const LESSON = { id: 8, event_type: 'create', memory_type: 'semantic', memory_key: 'lesson.r3nobare', old_value: null, new_value: JSON.stringify({ rule: 'Flag every bare except', negative: 'Let a bare except pass', category: 'tool' }), source: 'user_explicit', created_at: '2026-11-25T18:28:47+00:00', kind: 'directive' }
const saved = (n: number) => ({ id: 1000 + n, event_type: 'create', memory_type: 'semantic', memory_key: `user.k${n}`, old_value: null, new_value: `"v${n}"`, source: 'user_explicit', created_at: '2026-11-20T10:00:00+00:00', kind: 'fact' })
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
    : { events: offset ? [saved(48), ...Array.from({ length: 20 }, (_, i) => saved(100 + i))] : [REFUSAL, ...Array.from({ length: 49 }, (_, i) => saved(i))], total: 70, has_more: offset === 0, facets: FACETS })
})
afterEach(() => { globalThis.IntersectionObserver = original; vi.useRealTimers(); vi.unstubAllGlobals() })

describe('the Audit tab', () => {
  it('counts every refusal under one Refused item, and ticking it asks for every refusal code', async () => {
    renderWithProviders(<MemoryAudit store="reviewer" />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    const refused = await within(rail).findByRole('checkbox', { name: 'Refused' })
    expect(refused.closest('[data-facet-row]')).toHaveTextContent('8')
    expect(within(rail).getByRole('checkbox', { name: 'Blocked: unsafe text' }).closest('[data-facet-row]')).toHaveTextContent('2')
    expect(within(rail).getByRole('checkbox', { name: 'Lessons' }).closest('[data-facet-row]')).toHaveTextContent('12')
    fireEvent.click(refused)
    await waitFor(() => expect(lastQuery().filters.event).toEqual(['allowlist_reject', 'low_confidence', 'reserved_prefix', 'value_empty', 'work_state', 'consolidation_value_size']))
    expect(api.memoryEvents.mock.calls.every(([store]) => store === 'reviewer')).toBe(true)
    // The pick is a removable chip; Clear all brings every event back.
    expect(screen.getByRole('button', { name: 'Remove filter: Refused' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    await waitFor(() => expect(lastQuery().filters).toEqual({}))
  })

  it('gives each known type a (?) that opens on a press or a key and never ticks the box', async () => {
    renderWithProviders(<MemoryAudit store="reviewer" botName="reviewer" />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    const row = (await within(rail).findByRole('checkbox', { name: 'Refused' })).closest('[data-facet-row]') as HTMLElement
    const tip = within(row).getByRole('button')
    const before = api.memoryEvents.mock.calls.length
    fireEvent.click(tip)
    expect(within(row).getByRole('checkbox')).not.toBeChecked()
    expect(screen.getByRole('tooltip')).toBeInTheDocument()
    expect(api.memoryEvents.mock.calls.length).toBe(before)
    // Every event, kind and source the store knows has one; a focusable button reaches it by keyboard.
    for (const name of ['Saved', 'Blocked: unsafe text', 'Facts', 'Lessons', 'Experiences', 'Learned from chat', 'Saved on purpose']) {
      const each = within(rail).getByRole('checkbox', { name }).closest('[data-facet-row]') as HTMLElement
      expect(within(each).getByRole('button')).not.toHaveAttribute('tabindex', '-1')
    }
    // On a bot's page a tip that speaks of the bot names it.
    const lessons = within(rail).getByRole('checkbox', { name: 'Lessons' }).closest('[data-facet-row]') as HTMLElement
    fireEvent.click(within(lessons).getByRole('button'))
    await waitFor(() => expect(screen.getAllByRole('tooltip').some(node => node.textContent!.includes('reviewer'))).toBe(true))
  })

  it('loads the next page as the end comes near and says how many are shown', async () => {
    renderWithProviders(<MemoryAudit store="default" />)
    const list = await screen.findByRole('list', { name: 'Events' })
    expect(within(list).getAllByRole('listitem')).toHaveLength(50)
    expect(screen.getByText('Events: 50 of 70')).toBeVisible()
    act(() => watched.at(-1)!([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver))
    // The next page repeats one event a write pushed down; it shows once.
    await waitFor(() => expect(within(list).getAllByRole('listitem')).toHaveLength(70))
    expect(pageCalls().map(call => call[2])).toEqual([0, 50])
    expect(screen.getByText('Events: 70 of 70')).toBeVisible()
  })

  it('narrows to a date range sent to the server, and its chip brings the whole log back', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    try {
      vi.setSystemTime(new Date(2026, 10, 25, 12, 0, 0))
      renderWithProviders(<MemoryAudit store="default" />)
      await screen.findByRole('list', { name: 'Events' })
      fireEvent.pointerDown(screen.getByRole('button', { name: 'Date range' }), { button: 0, ctrlKey: false })
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Last 7 days' }))
      await waitFor(() => expect(lastQuery().since).toBeTruthy())
      expect(new Date(lastQuery().since).getTime()).toBe(new Date(2026, 10, 19, 0, 0, 0).getTime())
      fireEvent.click(screen.getByRole('button', { name: 'Remove filter: Last 7 days' }))
      await waitFor(() => expect(lastQuery().since).toBeUndefined())
    } finally {
      vi.useRealTimers()
    }
  })

  it.each([
    ['Today', 25, 26], ['Last 7 days', 19, 20], ['Last 30 days', -4, -3],
  ])('moves %s forward when the local day changes', async (preset, beforeDay, afterDay) => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date(2026, 10, 25, 23, 59, 30))
    renderWithProviders(<MemoryAudit store="default" />, { queryDefaults: { staleTime: Infinity } })
    await screen.findByRole('list', { name: 'Events' })
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Date range' }), { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: preset }))
    const before = new Date(2026, 10, beforeDay).toISOString()
    await waitFor(() => expect(lastQuery().since).toBe(before))
    const calls = api.memoryEvents.mock.calls.length

    vi.setSystemTime(new Date(2026, 10, 26, 0, 0, 1))
    act(() => { document.dispatchEvent(new Event('visibilitychange')) })
    const after = new Date(2026, 10, afterDay).toISOString()
    await waitFor(() => {
      const next = api.memoryEvents.mock.calls.slice(calls)
      expect(next.some(call => call[3] === 50 && call[1].since === after)).toBe(true)
      expect(next.some(call => call[3] === 1 && call[1].since === after)).toBe(true)
    })
  })

  it.each([
    { width: 600, collapsed: false, stacked: true },
    { width: 900, collapsed: false, stacked: true },
    { width: 900, collapsed: true, stacked: false },
    { width: 1100, collapsed: false, stacked: false },
  ])('fits the detail at $width px with collapsed filters=$collapsed', async ({ width, collapsed, stacked }) => {
    if (collapsed) localStorage.setItem('kirocrew:memory-filters-collapsed', '1')
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) {
        if (target.getAttribute('data-testid') === 'memory-audit') {
          this.callback([{ target, contentRect: { width } } as ResizeObserverEntry], this as unknown as ResizeObserver)
        }
      }
      disconnect() {}
      unobserve() {}
    })
    renderWithProviders(<MemoryAudit store="default" />)
    const list = await screen.findByRole('list', { name: 'Events' })
    fireEvent.click(within(list).getByText('pref.deploy.target = prod-us-east-1'))
    const detail = await screen.findByTestId('memory-audit-detail')
    expect(detail.closest('[data-testid="memory-detail-slide"]') === null).toBe(stacked)
    expect(within(screen.getByTestId('memory-audit-toolbar')).getByRole('textbox')).toBeVisible()
    if (stacked) {
      expect(screen.queryByRole('complementary', { name: 'Filters' })).toBeNull()
      fireEvent.click(screen.getByRole('button', { name: 'Filters' }))
      const filters = await screen.findByRole('dialog', { name: 'Filters' })
      expect(await within(filters).findByRole('checkbox', { name: 'Refused' })).toBeVisible()
    }
  })

  it('shows a lesson by its rule and its don’t text, with the date and the full time in its tooltip', async () => {
    api.memoryEvents.mockImplementation(async (_store: string, _query: unknown, _offset: number, limit: number) => limit === 1
      ? { events: [], total: 1, has_more: false, facets: FACETS }
      : { events: [LESSON], total: 1, has_more: false, facets: FACETS })
    renderWithProviders(<MemoryAudit store="member-reviewer" />)
    expect(await screen.findByText('Flag every bare except')).toBeVisible()
    expect(screen.getByText('Don’t: Let a bare except pass')).toBeVisible()
    expect(screen.queryByText(/lesson\.r3nobare =/)).toBeNull()
    const time = document.querySelector('time[datetime="2026-11-25T18:28:47+00:00"]')!
    expect(time.textContent).not.toMatch(/:\d\d/)
    expect(time.getAttribute('title')).toMatch(/:\d\d/)
  })

  it('names the memory page’s own edits and gives each a (?)', async () => {
    // `memory_edit.apply_edit` writes `correct` and `resolve`; neither may show as its raw code.
    api.memoryEvents.mockImplementation(async (_store: string, _query: unknown, _offset: number, limit: number) => limit === 1
      ? { events: [], total: 2, has_more: false, facets: { ...FACETS, event: { correct: 3, resolve: 1 } } }
      : { events: [{ ...saved(1), event_type: 'correct' }, { ...saved(2), event_type: 'resolve' }], total: 2, has_more: false, facets: FACETS })
    renderWithProviders(<MemoryAudit store="default" />)
    const rail = await screen.findByRole('complementary', { name: 'Filters' })
    for (const code of ['correct', 'resolve']) {
      expect(eventLabel(code)).not.toBe(code)
      const row = (await within(rail).findByRole('checkbox', { name: eventLabel(code) })).closest('[data-facet-row]') as HTMLElement
      expect(within(row).getByRole('button')).toBeInTheDocument()
    }
    const list = await screen.findByRole('list', { name: 'Events' })
    expect(within(list).queryByText('correct')).toBeNull()
    expect(within(list).queryByText('resolve')).toBeNull()
  })

  it.each([
    ['the events read', 50],
    ['the counts read', 1],
  ])('offers the agent hand-off when %s fails', async (_name, failing) => {
    api.memoryEvents.mockImplementation(async (_store: string, _query: unknown, _offset: number, limit: number) => {
      if (limit === failing) throw Object.assign(new Error('memory store locked'), { status: 409 })
      return { events: [saved(1)], total: 1, has_more: false, facets: FACETS }
    })
    renderWithProviders(<MemoryAudit store="default" />)
    const alert = (await screen.findByText('memory store locked')).closest('[role="alert"], [role="status"]') as HTMLElement
    expect(within(alert).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('opens a refusal with its value once, marked not saved, and the rule it broke', async () => {
    renderWithProviders(<MemoryAudit store="default" />)
    const list = await screen.findByRole('list', { name: 'Events' })
    fireEvent.click(within(list).getByText('pref.deploy.target = prod-us-east-1'))
    const detail = await screen.findByTestId('memory-audit-detail')
    expect(within(detail).getByRole('heading', { name: 'Refused: key not allowed' })).toBeInTheDocument()
    // The heading names the event; its raw code is not repeated.
    expect(within(detail).queryByText('allowlist_reject')).toBeNull()
    expect(within(detail).getByRole('region', { name: 'Not saved' })).toHaveTextContent('prod-us-east-1')
    expect(within(detail).queryByRole('region', { name: 'Change' })).toBeNull()
    expect(within(detail).queryByText('After')).toBeNull()
    expect(within(detail).getByRole('region', { name: 'Why' }).textContent!.length).toBeGreaterThan(3)
  })

  it('opens a write that landed with its value before and after, and no rule', async () => {
    renderWithProviders(<MemoryAudit store="default" />)
    const list = await screen.findByRole('list', { name: 'Events' })
    fireEvent.click(within(list).getByText('user.k0 = v0'))
    const detail = await screen.findByTestId('memory-audit-detail')
    expect(within(detail).getByRole('heading', { name: 'Saved' })).toBeInTheDocument()
    const change = within(detail).getByRole('region', { name: 'Change' })
    expect(change).toHaveTextContent('Before—')
    expect(change).toHaveTextContent('Afterv0')
    expect(within(detail).queryByRole('region', { name: 'Not saved' })).toBeNull()
    expect(within(detail).queryByRole('region', { name: 'Why' })).toBeNull()
  })
})
