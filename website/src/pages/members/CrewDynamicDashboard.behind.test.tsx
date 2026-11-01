import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { focusManager } from '@tanstack/react-query'
import { createConnectedTestStore, createTestStore, renderWithProviders } from '../../test/helpers'
import { sseConnected, sseDisconnected } from '../../store/dashboardSlice'

/**
 * THE PAGE-WIDE "VALUES MAY BE BEHIND" BAND, drawn by the host above the frame.
 *
 * An open Dashboard tab that stopped moving looked exactly like one that was current:
 * a socket drop loses every push sent while it was down, a gateway restart leaves no
 * live page to push at all, and a frozen tab's fallback read never runs. The host is
 * the one place that knows the socket state and the read's age, so it says so -- a
 * page's own script cannot hide the band, and a page that never draws one cannot
 * leave a stopped page looking current.
 */

const memberDashboard = vi.fn()
const memberDashboardRefetch = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberDashboard: (...a: unknown[]) => memberDashboard(...a),
      memberDashboardRefetch: (...a: unknown[]) => memberDashboardRefetch(...a),
    },
  }
})

type Listener = (read: unknown, fromPackage: string) => void
const listeners: Listener[] = []
vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: (_slug: string, fn: Listener) => {
    listeners.push(fn)
    return () => { listeners.splice(listeners.indexOf(fn), 1) }
  },
}))

vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? 'https://localhost/sandbox/doc' : null,
    failed: false,
    pending: false,
    stalled: false,
    retry: () => {},
  }),
}))

import CrewDynamicDashboard, { DASHBOARD_BEHIND_AFTER_MS, DASHBOARD_FALLBACK_REFETCH_MS } from './CrewDynamicDashboard'
import CrewDashboardBehindBand, { BEHIND_CLOCK_MS } from './CrewDashboardBehindBand'

const answer = (boards: number) => ({
  slug: 'atlas',
  member: 'Atlas',
  dashboard: 'page-one',
  state: 'ok',
  html: '<p>stored</p>',
  rendered_html: '<p>page</p>',
  manifest: { fields: {} },
  instance_version: 1,
  template: { id: 'project-report', version: 1 },
  read: { fields: { boards }, version: 1, generation: 'gen-a' },
})

const TARGET = { kind: 'member' as const, slug: 'atlas', member: 'Atlas' }

async function mounted(store = createConnectedTestStore()) {
  const view = renderWithProviders(<CrewDynamicDashboard target={TARGET} displayName="Atlas" />, { store })
  await waitFor(() => {
    if (!view.container.querySelector('iframe')) throw new Error('the frame never mounted')
  })
  return view
}

describe('the behind band', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    listeners.length = 0
  })
  afterEach(() => {
    vi.useRealTimers()
    focusManager.setFocused(undefined)
  })

  it('is two fallback intervals, so one missed tick does not raise it', () => {
    expect(DASHBOARD_BEHIND_AFTER_MS).toBe(2 * DASHBOARD_FALLBACK_REFETCH_MS)
  })

  it('is not drawn on a fresh page with the socket up', async () => {
    memberDashboard.mockResolvedValue(answer(1))
    const { queryByTestId } = await mounted()
    expect(queryByTestId('crew-dashboard-behind-band')).toBeNull()
  })

  it('is not drawn just because the tab mounted before the socket opened', async () => {
    // The read it just made is as fresh as anything; the real dashboard boots here.
    memberDashboard.mockResolvedValue(answer(1))
    const { queryByTestId } = await mounted(createTestStore())
    expect(queryByTestId('crew-dashboard-behind-band')).toBeNull()
  })

  it('goes up when the socket drops and stays up after reconnect until a fresh read lands', async () => {
    memberDashboard.mockResolvedValue(answer(1))
    const { store, queryByTestId, getByTestId, queryClient } = await mounted()

    act(() => { store.dispatch(sseDisconnected()) })
    const band = getByTestId('crew-dashboard-behind-band')
    expect(band.dataset.reason).toBe('disconnected')
    // Announced politely and in plain words.
    expect(getByTestId('crew-dashboard-behind').getAttribute('role')).toBe('status')
    expect(getByTestId('crew-dashboard-behind').textContent).toMatch(/may be behind/)

    // Socket back, re-read not landed yet: still behind. Nothing moved the page.
    let release: (v: unknown) => void = () => {}
    memberDashboard.mockReturnValue(new Promise((resolve) => { release = resolve }))
    act(() => { store.dispatch(sseConnected()) })
    void queryClient.invalidateQueries({ queryKey: ['member-dashboard'] })
    expect(queryByTestId('crew-dashboard-behind-band')).not.toBeNull()

    // The re-read lands: current again.
    await act(async () => { release(answer(2)) })
    await waitFor(() => expect(queryByTestId('crew-dashboard-behind-band')).toBeNull())
  })

  it('stays up while the socket is down even if a GET gets through, and after it until a post-reconnect read', async () => {
    // Half the link works: the read answers, but nothing can push into the page.
    // The clock moves between steps, so each one has its own millisecond.
    vi.useFakeTimers({ toFake: ['Date'] })
    const tick = () => vi.setSystemTime(Date.now() + 1_000)
    memberDashboard.mockResolvedValue(answer(1))
    const { store, getByTestId, queryByTestId, queryClient } = await mounted()
    tick()
    act(() => { store.dispatch(sseDisconnected()) })
    tick()
    memberDashboard.mockResolvedValue(answer(2))
    await act(async () => { await queryClient.refetchQueries({ queryKey: ['member-dashboard'] }) })
    expect(getByTestId('crew-dashboard-behind-band').dataset.reason).toBe('disconnected')

    // The socket comes back. That read is older than whatever moved between it and
    // the reconnect, so the band holds until the reconnect re-read lands.
    let release: (v: unknown) => void = () => {}
    memberDashboard.mockReturnValue(new Promise((resolve) => { release = resolve }))
    tick()
    act(() => { store.dispatch(sseConnected()) })
    void queryClient.invalidateQueries({ queryKey: ['member-dashboard'] })
    expect(queryByTestId('crew-dashboard-behind-band')).not.toBeNull()
    tick()
    await act(async () => { release(answer(3)) })
    await waitFor(() => expect(queryByTestId('crew-dashboard-behind-band')).toBeNull())
  })

  it('goes up when the last read is older than the limit; a push does not clear it, a read does', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
    memberDashboard.mockResolvedValue(answer(1))
    const { queryByTestId, getByTestId, queryClient } = await mounted()
    // Every later read hangs, as a stopped gateway would.
    let release: (v: unknown) => void = () => {}
    memberDashboard.mockImplementation(() => new Promise((resolve) => { release = resolve }))

    act(() => { vi.advanceTimersByTime(DASHBOARD_BEHIND_AFTER_MS - BEHIND_CLOCK_MS) })
    expect(queryByTestId('crew-dashboard-behind-band')).toBeNull()
    act(() => { vi.advanceTimersByTime(2 * BEHIND_CLOCK_MS) })
    expect(getByTestId('crew-dashboard-behind-band').dataset.reason).toBe('old')

    // A patch is not proof: the page may refuse it on a version gap and keep the
    // older values, and the host cannot see that.
    act(() => { listeners.forEach((fn) => fn({ fields: { boards: 9 }, patch: true }, 'page-one')) })
    expect(queryByTestId('crew-dashboard-behind-band')).not.toBeNull()

    // A whole read is.
    void queryClient.invalidateQueries({ queryKey: ['member-dashboard'] })
    await act(async () => { release(answer(2)) })
    await waitFor(() => expect(queryByTestId('crew-dashboard-behind-band')).toBeNull())
  })
})

describe('a tab coming back to the front', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    listeners.length = 0
  })
  afterEach(() => {
    vi.useRealTimers()
    focusManager.setFocused(undefined)
  })

  it('re-reads when its read is older than one interval', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    memberDashboard.mockResolvedValue(answer(1))
    await mounted()
    const before = memberDashboard.mock.calls.length
    act(() => { focusManager.setFocused(false) })
    vi.setSystemTime(Date.now() + DASHBOARD_FALLBACK_REFETCH_MS + 1_000)
    act(() => { focusManager.setFocused(true) })
    await waitFor(() => expect(memberDashboard.mock.calls.length).toBe(before + 1))
  })

  it('does not re-read after a quick switch away and back', async () => {
    memberDashboard.mockResolvedValue(answer(1))
    await mounted()
    const before = memberDashboard.mock.calls.length
    act(() => { focusManager.setFocused(false) })
    act(() => { focusManager.setFocused(true) })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(memberDashboard.mock.calls.length).toBe(before)
  })
})

describe('a band another notice stands in for', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    memberDashboardRefetch.mockReset()
    listeners.length = 0
  })

  it('still remembers a drop seen while the refetch-failed band hid it', async () => {
    memberDashboard.mockResolvedValue(answer(1))
    memberDashboardRefetch.mockRejectedValueOnce(new Error('the fold could not be re-read'))
    const { store, container, getByTestId, queryByTestId } = await mounted()
    const frame = container.querySelector('iframe') as HTMLIFrameElement
    Object.defineProperty(frame, 'contentWindow', { configurable: true, value: { postMessage: () => {} } })

    // The page asks for a whole read and it fails: the refetch band is up.
    await act(async () => {
      const event = new MessageEvent('message', { data: { type: 'kirocrew-dashboard:refetch', held: 1 } })
      Object.defineProperty(event, 'source', { value: frame.contentWindow })
      window.dispatchEvent(event)
    })
    await waitFor(() => expect(getByTestId('crew-dashboard-refetch-error-band')).toBeTruthy())
    // The socket drops while that band stands in for this one.
    act(() => { store.dispatch(sseDisconnected()) })
    expect(queryByTestId('crew-dashboard-behind-band')).toBeNull()

    // Retry gets through over HTTP; the socket is still down.
    memberDashboardRefetch.mockResolvedValueOnce({ read: { fields: { boards: 2 }, version: 2, generation: 'gen-a' } })
    await act(async () => { getByTestId('crew-dashboard-refetch-retry').click() })
    await waitFor(() => expect(queryByTestId('crew-dashboard-refetch-error-band')).toBeNull())
    expect(getByTestId('crew-dashboard-behind-band').dataset.reason).toBe('disconnected')
  })
})

describe('a read that got through while the socket was down', () => {
  afterEach(() => { vi.useRealTimers() })

  it('does not clear the band once the socket is back; a later read does', () => {
    // Values can move between that read and the reconnect, and those frames are lost.
    vi.useFakeTimers({ toFake: ['Date'] })
    const store = createConnectedTestStore()
    const t0 = Date.now()
    const band = (readAt: number) => <CrewDashboardBehindBand readAt={readAt} staleAfterMs={DASHBOARD_BEHIND_AFTER_MS} />
    const view = renderWithProviders(band(t0), { store })
    vi.setSystemTime(t0 + 1_000)
    act(() => { store.dispatch(sseDisconnected()) })
    view.rerender(band(t0 + 2_000))
    vi.setSystemTime(t0 + 3_000)
    act(() => { store.dispatch(sseConnected()) })
    expect(view.getByTestId('crew-dashboard-behind-band').dataset.reason).toBe('disconnected')
    view.rerender(band(t0 + 4_000))
    expect(view.queryByTestId('crew-dashboard-behind-band')).toBeNull()
  })
})
