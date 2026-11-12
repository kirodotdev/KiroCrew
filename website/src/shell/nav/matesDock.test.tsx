// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { ComponentProps } from 'react'
import type { DndContext, DragEndEvent } from '@dnd-kit/core'

import { api, ApiError, type MemberRosterRow } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import dashboardReducer, { markSlotRead, markSlotUnread, sseSlots } from '../../store/dashboardSlice'
import chatReducer, { sseSubagentQueued } from '../../store/chatSlice'
import notificationsReducer from '../../store/notificationsSlice'
import instancesReducer from '../../store/instancesSlice'
import type { ChatSlot } from '../../types'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'
import { attachReport, type ErrorReport } from '../../utils/errorReport'
import { DockMatesSection, DockMatesTiles } from './matesDock'

// Keep real sortable context; deliver drops at its callback boundary instead
// of depending on layout measurements that the test DOM does not implement.
const dnd = vi.hoisted(() => ({ props: null as ComponentProps<typeof DndContext> | null }))
vi.mock('@dnd-kit/core', async importOriginal => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: ComponentProps<typeof DndContext>) => {
      dnd.props = props
      return <actual.DndContext {...props} />
    },
  }
})
// Capture what "Ask the agent" stages instead of navigating to a chat.
const handoff = vi.hoisted(() => ({ prompts: [] as string[] }))
vi.mock('../../utils/errorReport', async importOriginal => {
  const actual = await importOriginal<typeof import('../../utils/errorReport')>()
  return {
    ...actual,
    sendErrorToChat: (prompt: string) => { handoff.prompts.push(prompt); return true },
  }
})
vi.mock('../../components/CrewAvatar', () => ({
  default: ({ seed, working }: { seed: string; working?: string }) =>
    <span data-testid={`face-${seed}`} data-working={working ?? 'idle'} />,
}))

const clients: QueryClient[] = []
beforeEach(() => { dnd.props = null; handoff.prompts.length = 0; localStorage.removeItem('mc-dock-mates-expanded') })
afterEach(() => {
  cleanup()
  for (const client of clients) client.clear()
  clients.length = 0
  vi.restoreAllMocks()
})

const row = (name: string, extra: Partial<MemberRosterRow> = {}): MemberRosterRow =>
  ({ name, slug: name, slot_key: '', running: false, starred: false, ...extra }) as MemberRosterRow

const slot = (agent: string, running: boolean, extra: Partial<ChatSlot> = {}): ChatSlot =>
  ({ key: `member-${agent}`, mode: 'member', agent, running, ...extra }) as ChatSlot

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

/** Applies a cache change and lets it reach the screen: react-query hands
 *  observers the change on a zero timeout, so a check right after a bare
 *  `act` would still read the previous render. */
const settleCache = (change: () => void) =>
  act(async () => { change(); await new Promise(done => setTimeout(done, 0)) })

/** Prints the router's current address, so a test can read where a click went. */
function Where() {
  const loc = useLocation()
  return <output data-testid="where">{loc.pathname + loc.search}</output>
}

/** Seeds the roster cache unless the test needs to exercise its first read. */
function renderDock(roster: MemberRosterRow[] | undefined, { url = '/schedule', collapsed = false, surface = 'section' }: {
  url?: string; collapsed?: boolean; surface?: 'section' | 'tiles' | 'both'
} = {}) {
  // The roster read retries on its own (`retryThroughRestart`); no backoff, so
  // a read that keeps failing shows its notice at once.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0, staleTime: Infinity } } })
  clients.push(qc)
  if (roster !== undefined) qc.setQueryData(MEMBERS_ROSTER_QUERY_KEY, roster)
  const liveStore = configureStore({ reducer: {
    dashboard: dashboardReducer,
    chat: chatReducer,
    notifications: notificationsReducer,
    instances: instancesReducer,
  } })
  const tree = (visibleSurface: 'section' | 'tiles' | 'both') => (
    <Provider store={liveStore}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[url]}>
          {visibleSurface !== 'tiles' && <DockMatesSection collapsed={collapsed} isMobile={false} />}
          {visibleSurface !== 'section' && <DockMatesTiles onActivate={() => {}} />}
          {visibleSurface !== 'tiles' && <Where />}
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(tree(surface))
  return { ...view, qc, liveStore, showSurface: (next: 'section' | 'tiles' | 'both') => view.rerender(tree(next)) }
}

const dockNames = () =>
  screen.queryAllByTestId(/^dock-mate-/).map(el => el.getAttribute('data-testid')!.replace('dock-mate-', ''))

function drop(active: string, over: string) {
  expect(dnd.props?.onDragEnd).toBeTypeOf('function')
  dnd.props!.onDragEnd!({ active: { id: active }, over: { id: over } } as DragEndEvent)
}

/** A transport failure with its structured report pinned, as the API client throws it. */
function failedRequest(message: string, endpoint: string, status: number) {
  const report: ErrorReport = { id: `err-${endpoint}`, at: 0, source: 'api', message, endpoint, status, code: 'unavailable', route: '/schedule' }
  return attachReport(new Error(message), report)
}

/** Stub a scroller's geometry (jsdom does no layout), then let its listener read it. */
function setOverflow(el: HTMLElement, hidden: number) {
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 100 })
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 100 + hidden })
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => 0 })
  act(() => { el.dispatchEvent(new Event('scroll')) })
}

const saved = {} as Awaited<ReturnType<typeof api.updateKirocrewAgent>>
const membersReply = (members: MemberRosterRow[]) => ({ members }) as Awaited<ReturnType<typeof api.members>>

describe('DockMatesSection', () => {
  it('renders nothing when no mate is starred', () => {
    renderDock([row('a'), row('b')])
    expect(screen.queryByTestId('dock-mates')).toBeNull()
  })

  it('lists only starred mates, placed ones first by star_order', () => {
    renderDock([
      row('plain'),
      row('later', { starred: true }),
      row('second', { starred: true, star_order: 2 }),
      row('first', { starred: true, star_order: 1 }),
    ])
    expect(dockNames()).toEqual(['first', 'second', 'later'])
  })

  it('folds behind an "N more" toggle that unfolds every mate and folds back, in place', () => {
    renderDock(['a', 'b', 'c', 'd', 'e'].map((n, i) => row(n, { starred: true, star_order: i + 1 })))
    expect(dockNames()).toEqual(['a', 'b', 'c'])
    const toggle = within(screen.getByTestId('dock-mates-more')).getByRole('button')
    expect(toggle).toHaveAccessibleName('2 more')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(toggle)
    expect(dockNames()).toEqual(['a', 'b', 'c', 'd', 'e'])
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveAccessibleName('Show less')
    expect(screen.getByTestId('where')).toHaveTextContent(/^\/schedule$/)
    expect(localStorage.getItem('mc-dock-mates-expanded')).toBe('1')
    fireEvent.click(toggle)
    expect(dockNames()).toEqual(['a', 'b', 'c'])
    expect(localStorage.getItem('mc-dock-mates-expanded')).toBe('0')
  })

  it('has no toggle when every starred mate fits', () => {
    renderDock(['a', 'b', 'c'].map(n => row(n, { starred: true })))
    expect(screen.queryByTestId('dock-mates-more')).toBeNull()
  })

  it('lets a mate starred late be dragged from below the fold into the top three', async () => {
    const roster = [
      ...['a', 'b', 'c'].map((n, i) => row(n, { starred: true, star_order: i + 1 })),
      row('late', { starred: true }),
    ]
    const update = vi.spyOn(api, 'updateKirocrewAgent').mockResolvedValue(saved)
    vi.spyOn(api, 'members').mockResolvedValue(membersReply(roster))
    renderDock(roster)
    fireEvent.click(within(screen.getByTestId('dock-mates-more')).getByRole('button'))
    expect(dockNames()).toEqual(['a', 'b', 'c', 'late'])
    act(() => drop('late', 'a'))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(4))
    expect(update.mock.calls).toEqual([
      ['late', { star_order: 1 }], ['a', { star_order: 2 }], ['b', { star_order: 3 }], ['c', { star_order: 4 }],
    ])
  })

  it('keeps the open mate on screen while folded, and lights it', () => {
    renderDock(['a', 'b', 'c', 'd', 'e'].map((n, i) => row(n, { starred: true, star_order: i + 1 })), { url: '/members?member=e' })
    expect(dockNames()).toEqual(['a', 'b', 'c', 'e'])
    expect(within(screen.getByTestId('dock-mate-e')).getByRole('button')).toHaveAttribute('aria-current', 'page')
    expect(within(screen.getByTestId('dock-mates-more')).getByRole('button')).toHaveAccessibleName('1 more')
  })

  it('links each row to its mate chat', () => {
    renderDock([row('Issue Triage', { slug: 'triage', starred: true })])
    fireEvent.click(within(screen.getByTestId('dock-mate-triage')).getByRole('button'))
    expect(screen.getByTestId('where')).toHaveTextContent('/members?member=Issue%20Triage')
  })

  it('lights the row of the mate whose chat is open', () => {
    renderDock([row('a', { starred: true }), row('b', { starred: true })], { url: '/members?member=b' })
    expect(within(screen.getByTestId('dock-mate-b')).getByRole('button')).toHaveAttribute('aria-current', 'page')
    expect(within(screen.getByTestId('dock-mate-a')).getByRole('button')).not.toHaveAttribute('aria-current')
  })

  it('marks a working mate on its face, not with a dot, before live slot data arrives', () => {
    renderDock([row('busy', { starred: true, running: true }), row('idle', { starred: true })])
    expect(within(screen.getByTestId('dock-mate-busy')).getByTestId('face-busy')).toHaveAttribute('data-working', 'subtle')
    expect(within(screen.getByTestId('dock-mate-busy')).queryByRole('status')).toBeNull()
    expect(within(screen.getByTestId('dock-mate-idle')).getByTestId('face-idle')).toHaveAttribute('data-working', 'idle')
  })

  // jsdom computes no layout, so this pins the structure that makes overlap
  // impossible: an in-flow dot after the truncating name, in one flex line.
  it('gives the wide row\'s dot its own space after the name, so a long name truncates before it', () => {
    const { liveStore } = renderDock([row('a', { starred: true, slot_key: 'member-a', display_name: 'Kiro Web Development' })])
    act(() => { liveStore.dispatch(sseSlots([slot('a', false)])); liveStore.dispatch(markSlotUnread('member-a')) })
    const dot = within(screen.getByTestId('dock-mate-a')).getByRole('status', { name: 'Unread message' })
    const name = within(screen.getByTestId('dock-mate-a')).getByText('Kiro Web Development')
    expect(dot).not.toHaveClass('absolute')
    expect(dot).toHaveClass('shrink-0')
    expect(name).toHaveClass('truncate')
    expect(dot.parentElement).toBe(name.parentElement)
    expect(name.compareDocumentPosition(dot) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps the narrow row\'s dot in the face\'s corner, where no name is drawn', () => {
    const { liveStore } = renderDock([row('a', { starred: true, slot_key: 'member-a' })], { collapsed: true })
    act(() => { liveStore.dispatch(sseSlots([slot('a', false)])); liveStore.dispatch(markSlotUnread('member-a')) })
    expect(within(screen.getByTestId('dock-mate-a')).getByRole('status', { name: 'Unread message' })).toHaveClass('absolute')
  })

  it('ignores overlapping drops until writes and the confirming roster read settle', async () => {
    const initial = ['a', 'b', 'c'].map((n, i) => row(n, { starred: true, star_order: i + 1 }))
    const serverOrder = ['c', 'a', 'b'].map((n, i) => row(n, { starred: true, star_order: i + 1 }))
    const write = deferred<Awaited<ReturnType<typeof api.updateKirocrewAgent>>>()
    const read = deferred<Awaited<ReturnType<typeof api.members>>>()
    const update = vi.spyOn(api, 'updateKirocrewAgent').mockResolvedValue(saved).mockImplementationOnce(() => write.promise)
    const members = vi.spyOn(api, 'members').mockResolvedValue(membersReply(serverOrder)).mockImplementationOnce(() => read.promise)
    const { qc } = renderDock(initial)
    try {
      act(() => { drop('c', 'a'); drop('b', 'a') })
      await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(screen.getByTestId('dock-mates')).toHaveAttribute('aria-busy', 'true'))
      act(() => drop('b', 'a'))
      expect(qc.getMutationCache().getAll()).toHaveLength(1)
      expect(update).toHaveBeenCalledTimes(1)

      await act(async () => { write.resolve(saved) })
      await waitFor(() => expect(members).toHaveBeenCalledTimes(1))
      expect(update).toHaveBeenCalledTimes(3)
      act(() => drop('b', 'a'))
      expect(qc.getMutationCache().getAll()).toHaveLength(1)
      expect(update).toHaveBeenCalledTimes(3)

      await act(async () => { read.resolve(membersReply(serverOrder)) })
      await waitFor(() => expect(screen.getByTestId('dock-mates')).toHaveAttribute('aria-busy', 'false'))
      expect(qc.getMutationCache().getAll()[0].state.status).toBe('success')
      expect(dockNames()).toEqual(['c', 'a', 'b'])
      act(() => drop('b', 'a'))
      await waitFor(() => expect(qc.getMutationCache().getAll()[1]?.state.status).toBe('success'))
      expect(update.mock.calls.slice(3)).toEqual([
        ['c', { star_order: 1 }], ['b', { star_order: 2 }], ['a', { star_order: 3 }],
      ])
    } finally {
      write.resolve(saved)
      read.resolve(membersReply(serverOrder))
    }
  })

  it('refetches partial server order and shows the reorder failure', async () => {
    const initial = ['a', 'b', 'c'].map((n, i) => row(n, { starred: true, star_order: i + 1 }))
    const partial = [initial[0], initial[1], { ...initial[2], star_order: 1 }]
    vi.spyOn(api, 'updateKirocrewAgent').mockResolvedValueOnce(saved).mockRejectedValueOnce(new Error('write failed'))
    const members = vi.spyOn(api, 'members').mockResolvedValue(membersReply(partial))
    const { qc } = renderDock(initial)
    act(() => drop('c', 'a'))
    const notice = await screen.findByTestId('dock-mates-reorder-error')
    expect(within(notice).getByRole('alert')).toHaveTextContent("Couldn't save the new order")
    expect(members).toHaveBeenCalledTimes(1)
    expect(qc.getQueryData(MEMBERS_ROSTER_QUERY_KEY)).toEqual(partial)
    expect(dockNames()).toEqual(['a', 'c', 'b'])
  })

  it('shows one dismissible load failure with agent hand-off when no roster is cached', async () => {
    vi.spyOn(api, 'members').mockRejectedValue(new Error('roster unavailable'))
    renderDock(undefined, { collapsed: true, surface: 'both' })
    const notice = await screen.findByTestId('dock-mates-load-error')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(within(notice).getByRole('alert')).toHaveTextContent("Couldn't load your starred bots.")
    expect(within(notice).getByRole('button', { name: /ask.*agent/i })).toBeInTheDocument()
    fireEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
    expect(screen.queryByTestId('dock-mates')).toBeNull()
  })

  it('hands the failed roster request to the agent, not just the sentence on screen', async () => {
    vi.spyOn(api, 'members').mockRejectedValue(failedRequest('roster unavailable', '/api/members', 503))
    renderDock(undefined)
    const notice = await screen.findByTestId('dock-mates-load-error')
    fireEvent.click(within(notice).getByRole('button', { name: /ask.*agent/i }))
    expect(handoff.prompts).toHaveLength(1)
    expect(handoff.prompts[0]).toContain('/api/members -> HTTP 503')
  })

  it.each(['section', 'tiles'] as const)('shows no rows and no notice on the %s when a guest is refused the roster', async surface => {
    const members = vi.spyOn(api, 'members').mockRejectedValue(new ApiError(403, 'owner only', '{"code":"owner_only"}'))
    renderDock(undefined, { surface })
    await waitFor(() => expect(members).toHaveBeenCalled())
    await act(async () => {})
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByTestId('dock-mates')).toBeNull()
    expect(screen.queryAllByTestId(/^rail-mate-/)).toHaveLength(0)
  })

  it('never asks again for the roster a guest is refused', async () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    const members = vi.spyOn(api, 'members').mockRejectedValue(new ApiError(403, 'owner only', '{"code":"owner_only"}'))
    renderDock(undefined, { surface: 'both' })
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(members).toHaveBeenCalledTimes(1)
    // Two 30s re-read periods pass with no further request.
    await act(async () => { await vi.advanceTimersByTimeAsync(61_000) })
    expect(members).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
  })

  it('still shows the notice for an expired session, which is not a guest refusal', async () => {
    vi.spyOn(api, 'members').mockRejectedValue(new ApiError(403, 'Session expired', '{"code":"owner_only"}', true))
    renderDock(undefined)
    expect(await screen.findByTestId('dock-mates-load-error')).toBeInTheDocument()
  })

  it('hands the failed reorder write to the agent', async () => {
    const initial = ['a', 'b'].map((n, i) => row(n, { starred: true, star_order: i + 1 }))
    vi.spyOn(api, 'updateKirocrewAgent').mockRejectedValue(failedRequest('write failed', '/api/kirocrew/agents/b', 409))
    vi.spyOn(api, 'members').mockResolvedValue(membersReply(initial))
    renderDock(initial)
    act(() => drop('b', 'a'))
    const notice = await screen.findByTestId('dock-mates-reorder-error')
    fireEvent.click(within(notice).getByRole('button', { name: /ask.*agent/i }))
    expect(handoff.prompts).toHaveLength(1)
    expect(handoff.prompts[0]).toContain('/api/kirocrew/agents/b -> HTTP 409')
  })

  it('fades the bottom edge only while rows are scrolled out of sight', () => {
    renderDock(['a', 'b', 'c', 'd'].map((n, i) => row(n, { starred: true, star_order: i + 1 })))
    const group = screen.getByTestId('dock-mates')
    const mask = MORE_BELOW_MASK.split(' ')[0]
    setOverflow(group, 0)
    expect(group.className).not.toContain(mask)
    setOverflow(group, 60)
    expect(group.className).toContain(mask)
    setOverflow(group, 0)
    expect(group.className).not.toContain(mask)
  })

  it.each(['section', 'tiles', 'both'] as const)('keeps cached shortcuts alongside one background failure notice on %s', async surface => {
    vi.spyOn(api, 'members').mockRejectedValue(failedRequest('roster unavailable', '/api/members', 503))
    const roster = [row('a', { starred: true })]
    const { qc } = renderDock(roster, { surface })
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
    await act(async () => { await qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }) })
    const notice = await screen.findByTestId('dock-mates-load-error')
    expect(qc.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.status).toBe('error')
    expect(qc.getQueryData(MEMBERS_ROSTER_QUERY_KEY)).toEqual(roster)
    if (surface !== 'tiles') expect(dockNames()).toEqual(['a'])
    if (surface !== 'section') expect(screen.getByTestId('rail-mate-a')).toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(within(notice).getByRole('alert')).toHaveTextContent("Couldn't load your starred bots.")
    fireEvent.click(within(notice).getByRole('button', { name: /ask.*agent/i }))
    expect(handoff.prompts).toHaveLength(1)
    expect(handoff.prompts[0]).toContain('/api/members -> HTTP 503')
  })

  it('rides out a short network drop: a re-read that fails twice and then lands shows no notice', async () => {
    const roster = [row('a', { starred: true })]
    const linkDown = () => new TypeError('Failed to fetch')
    const members = vi.spyOn(api, 'members')
      .mockRejectedValueOnce(linkDown())
      .mockRejectedValueOnce(linkDown())
      .mockResolvedValue(membersReply([...roster, row('b', { starred: true })]))
    const { qc } = renderDock(roster)
    await act(async () => { await qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }) })
    // Wait for the read to settle on screen, whichever way it went.
    await waitFor(() => expect(screen.queryByTestId('dock-mates-load-error') ?? screen.queryByTestId('dock-mate-b')).not.toBeNull())
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
    expect(members).toHaveBeenCalledTimes(3)
    expect(qc.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.status).toBe('success')
    expect(dockNames()).toEqual(['a', 'b'])
  })

  it('re-reads a roster that stays failed every 30s, so its notice clears once a read lands', async () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    const roster = [row('a', { starred: true })]
    const members = vi.spyOn(api, 'members').mockRejectedValue(new TypeError('Failed to fetch'))
    const { qc } = renderDock(roster)
    await act(async () => {
      void qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      await vi.advanceTimersByTimeAsync(1_000)
    })
    // The ladder is spent: the first read and its three retries all failed.
    expect(members).toHaveBeenCalledTimes(4)
    expect(screen.getByTestId('dock-mates-load-error')).toBeInTheDocument()
    // Nothing else re-reads an idle page while the socket stays up.
    members.mockResolvedValue(membersReply([...roster, row('b', { starred: true })]))
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(members).toHaveBeenCalledTimes(5)
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
    expect(dockNames()).toEqual(['a', 'b'])
  })

  it('keeps a dismissed notice away through a failed 30s re-read', async () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    const roster = [row('a', { starred: true })]
    const members = vi.spyOn(api, 'members').mockRejectedValue(new TypeError('Failed to fetch'))
    const { qc } = renderDock(roster)
    await act(async () => {
      void qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
      await vi.advanceTimersByTimeAsync(1_000)
    })
    expect(members).toHaveBeenCalledTimes(4)
    const notice = screen.getByTestId('dock-mates-load-error')
    fireEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
    // The re-read and its retries fail too: a new failure in the same outage.
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000) })
    expect(members).toHaveBeenCalledTimes(8)
    expect(qc.getQueryState(MEMBERS_ROSTER_QUERY_KEY)?.status).toBe('error')
    expect(screen.queryByTestId('dock-mates-load-error')).toBeNull()
    expect(dockNames()).toEqual(['a'])
  })
})

describe.each(['section', 'tiles'] as const)('live presence in %s', surface => {
  it('updates the face from live activity rather than the HTTP snapshot, with no dot for working', () => {
    const { liveStore } = renderDock([row('a', { starred: true, slot_key: 'member-a', running: true })], { surface })
    const mate = screen.getByTestId(surface === 'section' ? 'dock-mate-a' : 'rail-mate-a')
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'subtle')
    act(() => { liveStore.dispatch(sseSlots([slot('a', false)])) })
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'idle')
    act(() => { liveStore.dispatch(sseSlots([slot('a', true)])) })
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'subtle')
    expect(within(mate).queryByRole('status')).toBeNull()
  })

  it('resolves a new member slot by name and includes live child work on the face', () => {
    const { liveStore } = renderDock([row('a', { starred: true })], { surface })
    const mate = screen.getByTestId(surface === 'section' ? 'dock-mate-a' : 'rail-mate-a')
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'idle')
    act(() => { liveStore.dispatch(sseSlots([slot('a', false, { subagents_running: 1 })])) })
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'subtle')
    act(() => {
      liveStore.dispatch(sseSlots([slot('a', false)]))
      liveStore.dispatch(sseSubagentQueued({ slot: 'member-a', queued: 1 }))
    })
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'subtle')
    act(() => { liveStore.dispatch(sseSubagentQueued({ slot: 'member-a', queued: 0 })) })
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'idle')
    expect(within(mate).queryByRole('status')).toBeNull()
  })

  it('shows the dot only for an unread reply, and drops it once read even while still working', () => {
    const { liveStore } = renderDock([row('a', { starred: true, slot_key: 'member-a' })], { surface })
    const mate = screen.getByTestId(surface === 'section' ? 'dock-mate-a' : 'rail-mate-a')
    act(() => { liveStore.dispatch(sseSlots([slot('a', false)])) })
    expect(within(mate).queryByRole('status')).toBeNull()
    act(() => { liveStore.dispatch(markSlotUnread('member-a')) })
    expect(within(mate).getByRole('status', { name: 'Unread message' })).toBeInTheDocument()
    act(() => { liveStore.dispatch(sseSlots([slot('a', false, { subagents_running: 1 })])) })
    act(() => { liveStore.dispatch(markSlotRead('member-a')) })
    expect(within(mate).queryByRole('status')).toBeNull()
    expect(within(mate).getByTestId('face-a')).toHaveAttribute('data-working', 'subtle')
  })

  it('puts no dot on a mate whose turn or dispatched session waits on the person', () => {
    const { liveStore } = renderDock([row('a', { starred: true, slot_key: 'member-a' })], { surface })
    const mate = screen.getByTestId(surface === 'section' ? 'dock-mate-a' : 'rail-mate-a')
    act(() => {
      liveStore.dispatch(sseSlots([
        slot('a', false, { needs_input: true }),
        { key: 'chat-worker', agent: 'kirocrew', running: false, mate_root: 'member-a', pending_approval: true } as ChatSlot,
      ]))
    })
    expect(within(mate).queryByRole('status')).toBeNull()
  })
})

describe('DockMatesTiles', () => {
  it('shows a dismissible roster failure with the request hand-off when no data is cached', async () => {
    vi.spyOn(api, 'members').mockRejectedValue(failedRequest('roster unavailable', '/api/members', 503))
    renderDock(undefined, { url: '/chat', surface: 'tiles' })
    const notice = await screen.findByTestId('dock-mates-load-error')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(within(notice).getByRole('alert')).toHaveTextContent("Couldn't load your starred bots.")
    expect(notice).toHaveStyle({ left: '16px' })
    fireEvent.click(within(notice).getByRole('button', { name: /ask.*agent/i }))
    expect(handoff.prompts).toHaveLength(1)
    expect(handoff.prompts[0]).toContain('/api/members -> HTTP 503')
    fireEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it.each([
    { first: 'section', cached: false },
    { first: 'tiles', cached: false },
    { first: 'section', cached: true },
    { first: 'tiles', cached: true },
  ] as const)('keeps one notice as drawers mount and the $first owner leaves (cached: $cached)', async ({ first, cached }) => {
    vi.spyOn(api, 'members').mockRejectedValue(new Error('roster unavailable'))
    const { qc, showSurface } = renderDock(cached ? [row('a', { starred: true })] : undefined, { surface: first })
    if (cached) await act(async () => { await qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY }) })
    await screen.findByTestId('dock-mates-load-error')
    // Prevent mounting the other observer from starting a new failed request.
    qc.setQueryDefaults(MEMBERS_ROSTER_QUERY_KEY, { retryOnMount: false, refetchOnMount: false })
    showSurface('both')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    showSurface(first === 'tiles' ? 'section' : 'tiles')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    const notice = screen.getByTestId('dock-mates-load-error')
    fireEvent.click(within(notice).getByRole('button', { name: /dismiss/i }))
    showSurface('both')
    expect(screen.queryByRole('alert')).toBeNull()
    showSurface(first)
    expect(screen.queryByRole('alert')).toBeNull()
    // Distinct timestamps, without relying on wall-clock timing. Another
    // failure in the same outage keeps the dismiss.
    const query = qc.getQueryCache().find({ queryKey: MEMBERS_ROSTER_QUERY_KEY })!
    const failedAt = query.state.errorUpdatedAt
    await settleCache(() => query.setState({ errorUpdatedAt: failedAt + 1 }))
    expect(screen.queryByRole('alert')).toBeNull()
    // A good read ends the outage; a failure after it shows the notice again.
    await settleCache(() => query.setState({ status: 'success', error: null, data: query.state.data ?? [], dataUpdatedAt: failedAt + 2 }))
    expect(screen.queryByRole('alert')).toBeNull()
    await settleCache(() => query.setState({ status: 'error', error: new Error('roster unavailable'), errorUpdatedAt: failedAt + 3 }))
    await screen.findByTestId('dock-mates-load-error')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  function renderTiles(roster: MemberRosterRow[]) {
    return renderDock(roster, { url: '/chat', surface: 'tiles' })
  }

  it('renders nothing when no mate is starred', () => {
    const { container } = renderTiles([row('a')])
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the same three mates in dock order and no "N more" tile', () => {
    renderTiles(['a', 'b', 'c', 'd'].map((n, i) => row(n, { starred: true, star_order: 4 - i })))
    expect(screen.queryAllByTestId(/^rail-mate-/).map(el => el.getAttribute('data-testid'))).toEqual([
      'rail-mate-d', 'rail-mate-c', 'rail-mate-b',
    ])
    expect(screen.queryByTestId('rail-mates-more')).toBeNull()
  })

  it('adds the open mate\'s tile when it sits past the three', () => {
    renderDock(['a', 'b', 'c', 'd'].map((n, i) => row(n, { starred: true, star_order: i + 1 })), { url: '/members?member=d', surface: 'tiles' })
    expect(screen.queryAllByTestId(/^rail-mate-/).map(el => el.getAttribute('data-testid'))).toEqual([
      'rail-mate-a', 'rail-mate-b', 'rail-mate-c', 'rail-mate-d',
    ])
  })
})
