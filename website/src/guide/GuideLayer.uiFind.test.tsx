/**
 * ui.find guides: point at a control by its name.
 *
 * Shared fixtures: src/test/GuideLayerTestUtils.tsx. This file keeps its own
 * word-for-word copy of the guide state its tests reassign (pending,
 * onWrite, qc, calls) and of every helper that reads or assigns it
 * (renderGuide, writes, the top-level hooks), plus L: an import cannot be
 * reassigned, src/i18n/dynamicKeys.test.ts allows L's assembled key only in
 * a .test. file.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, renderHook, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { useEffect, useState, type ReactNode } from 'react'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { type Guide, type GuideAction } from '../api/guide'
import { _resetViewedThreadForTests } from '../lib/viewedThread'
import { i18nT } from '../i18n/t'
import { GuideProvider } from './GuideContext'
import GuideLayer, { stepCaution } from './GuideLayer'
import type { GuideStepPlan } from './guideActions'
import GuideOfferCard from './GuideOfferCard'
import { findOpenerLocation, resolveGuideAction, resolveGuideActions } from './guideActions'
import { GUIDE_EARLIER_STEP_WAIT_MS, GUIDE_TARGET_WAIT_MS, GUIDE_TRACK_TICK_MS, useGuideStepTracker } from './useGuideStepTracker'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { resetFindStates } from './findByName'
import { registerProbeTarget } from './probeRegistry'
import { marks } from '../test/guideTargets'
import {
  tickTracker,
  waitFor,
  guide,
  claimed,
  LocationProbe,
  T0,
} from '../test/GuideLayerTestUtils'
import type { Call } from '../test/GuideLayerTestUtils'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.guideLayer.${k}`, v)
let calls: Call[]
let pending: Guide[]
/** What each write returns; defaults to echoing the guide the test set. */
let onWrite: (path: string, body: Record<string, unknown>) => Guide | null

let qc: QueryClient
/** *chatSlot* stands in for the open chat pane, whose conversation holds the
 *  offer's row (guide `g1`, the id every fixture here uses). */
function renderGuide(path: string, extra?: ReactNode, chatSlot: string | null = 'slot-A') {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={[path]}>
          <NavigationLeaveGuardProvider>
            <GuideProvider>
              <GuideLayer />
              {chatSlot && <GuideOfferCard guideId="g1" slotKey={chatSlot} />}
              <LocationProbe />
              {extra}
            </GuideProvider>
          </NavigationLeaveGuardProvider>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(T0)
  calls = []
  pending = []
  onWrite = () => null
  _resetViewedThreadForTests()
  store.dispatch(setActiveSlot('slot-A'))
  server.use(
    http.get('/api/guide/pending', () => HttpResponse.json({ guides: pending })),
    ...['claim', 'progress', 'heartbeat', 'cancel', 'dismiss', 'replan'].map(name =>
      http.post(`/api/guide/${name}`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: `/api/guide/${name}`, body, headers: request.headers })
        return HttpResponse.json({ guide: onWrite(name, body) })
      }),
    ),
    http.post('/api/mcp/custom', async ({ request }) => {
      calls.push({ path: '/api/mcp/custom', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      return HttpResponse.json({ ok: true, added: ['files'], enabled: false })
    }),
  )
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

const writes = (path: string) => calls.filter(c => c.path === path)

describe('ui.find: point at a control by its name', () => {
  const realRect = HTMLElement.prototype.getBoundingClientRect
  beforeEach(() => {
    HTMLElement.prototype.getBoundingClientRect = function () {
      return { top: 200, left: 40, width: 120, height: 28, right: 160, bottom: 228, x: 40, y: 200, toJSON: () => ({}) } as DOMRect
    }
  })
  afterEach(() => {
    HTMLElement.prototype.getBoundingClientRect = realRect
    resetFindStates()
  })

  const find = (params: Record<string, unknown>): GuideAction => ({ id: 'ui.find', params })
  const findGuide = (params: Record<string, unknown>) => guide({ actions: [find(params)] })
  /** The gateway's answer to each report: advance, resume, or hold the step missing with its reason. */
  const walk = (g: Guide) => {
    let current = claimed(g)
    onWrite = (name, b) => {
      if (name === 'progress') {
        if (b.outcome === 'target_missing') {
          current = { ...current, revision: current.revision + 1, status: 'target_missing', reason: b.detail === 'not_found' ? 'not_found' : b.detail === 'ambiguous' ? 'ambiguous_target' : 'target_missing' }
        } else if (b.outcome === 'target_found') {
          const at = typeof b.resume_step_index === 'number' ? b.resume_step_index : current.step_index
          current = { ...current, revision: current.revision + 1, step_index: at, status: 'active', reason: null as unknown as string }
        } else {
          const last = (b.step_index as number) >= 1
          current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
        }
      }
      return current
    }
  }

  /** The dashboard's shared menu, uncontrolled: a guide's probe may open it. */
  function MoreMenu({ items = ['Export…'] }: { items?: string[] }) {
    return (
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild><button data-testid="more">More actions</button></DropdownMenuTrigger>
        <DropdownMenuContent>
          {items.map((label, i) => <DropdownMenuItem key={i} data-testid={`item-${i}`}>{label}</DropdownMenuItem>)}
        </DropdownMenuContent>
      </DropdownMenu>
    )
  }

  it('finds a control inside a registered menu, points at the menu, then at the control by its real name', async () => {
    const g = findGuide({ label: 'export', role: 'menuitem' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <MoreMenu />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    // The probe opens the menu through its own state, finds Export… and closes it again.
    expect(await screen.findByText(L('step_ui_find_open', { target: 'export' }), {}, { timeout: 3000 })).toBeTruthy()
    // Pointed at once the probe found the menu holding it.
    expect(await screen.findByTestId('guide-target-outline', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByTestId('item-0')).toBeNull()
    expect(writes('/api/guide/progress')).toHaveLength(0)
    fireEvent.pointerDown(screen.getByTestId('more'), { button: 0, ctrlKey: false })
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed', find: { result: 'found', count: 1, role: 'menuitem' } })
    // The panel names the control as the page does, not as it was asked for.
    expect(await screen.findByText(L('step_ui_show_here', { label: 'Export…' }))).toBeTruthy()
    fireEvent.click(screen.getByTestId('item-0'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(JSON.stringify(writes('/api/guide/progress').map(c => c.body))).not.toContain('Export…')
  })

  it('says there is nothing to act on, not "open a menu", for a control drawn only with something to clear', async () => {
    const g = findGuide({ label: 'Clear all', location_id: 'notifications.page-clear-all' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <p>No notifications</p>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByTestId('guide-find-nothing-yet', {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(L('find_nothing_yet', { label: 'Clear all' }))).toBeTruthy()
    expect(screen.queryByTestId('guide-find-not-found')).toBeNull()
  })

  it('says a highlighted control that is greyed out has nothing to do yet, never "press it"', async () => {
    const g = findGuide({ label: 'Forward' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <button type="button" aria-label="Forward" disabled data-testid="fwd">→</button>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('step_target_disabled', { label: 'Forward' }), {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByText(L('step_ui_show_here', { label: 'Forward' }))).toBeNull()
  })

  it('asks the person to open the container when nothing the guide may open holds the name', async () => {
    const pressed = vi.fn()
    const g = findGuide({ label: 'Rebuild index' })
    pending = [g]
    walk(g)
    // A hand-rolled menu: no primitive registered it, so the guide never opens it.
    renderGuide('/chat/slot-A', <button aria-haspopup="menu" aria-expanded="false" onClick={pressed}>Advanced</button>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 5000 })
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ outcome: 'target_missing', detail: 'not_found', find: { result: 'none', count: 0 } })
    expect(await screen.findByTestId('guide-find-not-found')).toBeTruthy()
    expect(screen.getByText(L('find_not_found', { label: 'Rebuild index' }))).toBeTruthy()
    expect(pressed).not.toHaveBeenCalled()
    // On its own page there is nowhere to go back to: Cancel only.
    expect(screen.queryByTestId('guide-go-back')).toBeNull()
    expect(screen.getByText(L('cancel_guide'))).toBeTruthy()
  })

  /** A menu no primitive registered: only the person opens it. Its trigger is a registered location. */
  function BellMenu({ onOpen }: { onOpen?: () => void }) {
    const [open, setOpen] = useState(false)
    return (
      <>
        <button aria-expanded={open} onClick={() => { onOpen?.(); setOpen(true) }} {...marks({ location: "shell.notifications" })}>Notifications</button>
        {open && <div role="menu"><button data-testid="mark" {...marks({ location: "notifications.mark-all-read" })}>Mark all as read</button></div>}
      </>
    )
  }

  it('points at the opener the index names first, never opens it, and goes on once the person opened it', async () => {
    const opened = vi.fn()
    const g = findGuide({ label: 'Mark all as read', location_id: 'notifications.mark-all-read', opener: 'shell.notifications' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <BellMenu onOpen={opened} />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('step_ui_find_open', { target: 'Mark all as read' }), {}, { timeout: 3000 })).toBeTruthy()
    expect(await screen.findByTestId('guide-target-outline', {}, { timeout: 3000 })).toBeTruthy()
    await tickTracker(3)
    expect(opened).not.toHaveBeenCalled()
    expect(writes('/api/guide/progress')).toHaveLength(0)
    fireEvent.click(screen.getByText('Notifications'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed', find: { result: 'found', count: 1, location_id: 'notifications.mark-all-read' } })
  })

  it('a row menu every list row draws is opened on the row the person has open', () => {
    render(
      <ul>
        <li className="session-row"><button data-testid="a" {...marks({ location: "sessions.row-menu" })}>More options</button></li>
        <li className="session-row" aria-current="true"><button data-testid="b" {...marks({ location: "sessions.row-menu" })}>More options</button></li>
      </ul>,
    )
    expect(findOpenerLocation('sessions.row-menu', () => true)).toBe(screen.getByTestId('b'))
    // No row is open: the guide cannot tell which row is meant.
    screen.getByTestId('b').closest('li')!.removeAttribute('aria-current')
    expect(findOpenerLocation('sessions.row-menu', () => true)).toBeNull()
  })

  it('never points at an opener whose real element removes something: not found, path in words', async () => {
    // The index id is a harmless trigger, but the element the page draws
    // under it removes something: judged on the element, it is never shown.
    const opened = vi.fn()
    const g = findGuide({ label: 'Mark all as read', location_id: 'notifications.mark-all-read', opener: 'shell.notifications' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <button data-guide-caution="" onClick={opened} {...marks({ location: "shell.notifications" })}>Clear all notifications</button>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 5000 })
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ outcome: 'target_missing', detail: 'not_found', find: { result: 'none', count: 0 } })
    // Mark all as read is drawn only with something unread: the miss says so.
    expect(await screen.findByTestId('guide-find-nothing-yet')).toBeTruthy()
    expect(screen.queryByTestId('guide-target-outline')).toBeNull()
    expect(opened).not.toHaveBeenCalled()
  })

  it('the open step of a ui.find shows the caution line when what it points at removes something', () => {
    const step = { target: { kind: 'find-container', key: 'k' }, complete: { kind: 'ack' }, textKey: 'x' } as unknown as GuideStepPlan
    const del = document.createElement('button')
    del.textContent = 'Clear all'
    del.setAttribute('data-guide-caution', '')
    const safe = document.createElement('button')
    safe.textContent = 'Notifications'
    expect(stepCaution(step, del)).toBe(true)
    expect(stepCaution(step, safe)).toBe(false)
  })

  it('a page guide on its page says so and waits for Done, never looking for a control or going missing', async () => {
    const g = findGuide({ label: 'Logs', route: '/logs', page: true })
    pending = [g]
    walk(g)
    window.history.pushState({}, '', '/logs')
    renderGuide('/logs', <p>Logs page</p>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('step_page_here', { label: 'Logs' }), {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText(L('looking_for_control'))).toBeNull()
    expect(screen.getByTestId('guide-next')).not.toBeDisabled()
    expect(writes('/api/guide/progress').some(w => w.body.outcome === 'target_missing')).toBe(false)
    window.history.pushState({}, '', '/')
  })

  it('a step that points at nothing is never reported missing, however long it waits', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'setTimeout', 'Date', 'requestAnimationFrame'] })
    try {
      const onMissing = vi.fn(() => true)
      const step: GuideStepPlan = { target: { kind: 'none' }, complete: { kind: 'ack' }, textKey: 'components.guideLayer.step_page_here' }
      renderHook(() => useGuideStepTracker({ stepId: 's', step, enabled: true, suppressMissing: false, reduceMotion: true, onObserved: () => true, onMissing }))
      act(() => { vi.advanceTimersByTime(GUIDE_TARGET_WAIT_MS * 2) })
      expect(onMissing).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('a page guide started on another page outlines the menu entry and never reports it missing while shown', async () => {
    const g = findGuide({ label: 'Logs', route: '/logs', page: true })
    pending = [g]
    walk(g)
    window.history.pushState({}, '', '/chat/slot-A')
    try {
      renderGuide('/chat/slot-A', <nav aria-label="Main navigation"><a href="/logs" data-testid="logs-link">Logs</a></nav>)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByText(L('step_page_open', { target: 'Logs' }), {}, { timeout: 3000 })).toBeTruthy()
      expect(await screen.findByTestId('guide-target-outline', {}, { timeout: 3000 })).toBeTruthy()
      expect(screen.queryByText(L('looking_for_control'))).toBeNull()
      expect(writes('/api/guide/progress')).toHaveLength(0)
    } finally { window.history.pushState({}, '', '/') }
  })

  it('tracks a page guide from another page: the entry is outlined, arriving completes it, nothing goes missing', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'setTimeout', 'Date', 'requestAnimationFrame'] })
    window.history.pushState({}, '', '/chat')
    try {
      render(<nav aria-label="Main navigation"><a href="/logs">Logs</a></nav>)
      const r = resolveGuideAction({ id: 'ui.find', params: { label: 'Logs', route: '/logs', page: true } }, 'g-arrive', 0)
      if (!r.ok) throw new Error(r.reason)
      const onMissing = vi.fn(() => true)
      const onObserved = vi.fn(() => true)
      const { result } = renderHook(() => useGuideStepTracker({ stepId: 's', step: r.action.steps[0], enabled: true, suppressMissing: false, reduceMotion: true, onObserved, onMissing }))
      act(() => { vi.advanceTimersByTime(GUIDE_TARGET_WAIT_MS * 2) })
      expect(result.current).not.toBeNull()
      expect(onMissing).not.toHaveBeenCalled()
      expect(onObserved).not.toHaveBeenCalled()
      window.history.pushState({}, '', '/logs')
      act(() => { vi.advanceTimersByTime(GUIDE_TRACK_TICK_MS * 2) })
      expect(onObserved).toHaveBeenCalled()
      expect(onMissing).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
      window.history.pushState({}, '', '/')
    }
  })

  it('a page guide\'s "you\'re here" is bound to its page: leaving makes it missing, coming back recovers it', () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'setTimeout', 'Date', 'requestAnimationFrame'] })
    window.history.pushState({}, '', '/logs')
    try {
      const r = resolveGuideAction({ id: 'ui.find', params: { label: 'Logs', route: '/logs', page: true } }, 'g-here', 0)
      if (!r.ok) throw new Error(r.reason)
      const here = r.action.steps[1]
      const onMissing = vi.fn(() => true)
      const onFound = vi.fn(() => true)
      const active = renderHook(() => useGuideStepTracker({ stepId: 'h', step: here, enabled: true, suppressMissing: false, reduceMotion: true, onObserved: () => true, onMissing }))
      act(() => { vi.advanceTimersByTime(GUIDE_TARGET_WAIT_MS) })
      expect(onMissing).not.toHaveBeenCalled()
      window.history.pushState({}, '', '/chat')
      act(() => { vi.advanceTimersByTime(GUIDE_EARLIER_STEP_WAIT_MS + GUIDE_TRACK_TICK_MS * 2) })
      expect(onMissing).toHaveBeenCalledTimes(1)
      active.unmount()
      renderHook(() => useGuideStepTracker({ stepId: 'h2', step: here, enabled: true, recover: true, suppressMissing: false, reduceMotion: true, onObserved: () => true, onMissing: () => true, onFound }))
      act(() => { vi.advanceTimersByTime(GUIDE_TRACK_TICK_MS * 2) })
      expect(onFound).not.toHaveBeenCalled()
      window.history.pushState({}, '', '/logs')
      act(() => { vi.advanceTimersByTime(GUIDE_TRACK_TICK_MS * 2) })
      expect(onFound).toHaveBeenCalledWith(undefined)
    } finally {
      vi.useRealTimers()
      window.history.pushState({}, '', '/')
    }
  })

  it('without an opener on screen, the same hidden control is not found rather than guessed at', async () => {
    const g = findGuide({ label: 'Mark all as read', location_id: 'notifications.mark-all-read', opener: 'shell.notifications' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <button>Elsewhere</button>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 5000 })
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ outcome: 'target_missing', detail: 'not_found' })
  })

  it('numbers several visible controls with the name, lets the person pick one, and sends only the count', async () => {
    const g = findGuide({ label: 'Run Now' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', (
      <>
        <section aria-label="Daily digest"><button>Run Now</button></section>
        <section aria-label="Weekly report"><button data-testid="weekly">Run Now</button></section>
      </>
    ))
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 5000 })
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ outcome: 'target_missing', detail: 'ambiguous', find: { result: 'ambiguous', count: 2 } })
    const list = await screen.findByTestId('guide-find-ambiguous')
    expect(within(list).getByText('1. Daily digest')).toBeTruthy()
    expect(within(list).getByText('2. Weekly report')).toBeTruthy()
    expect(JSON.stringify(writes('/api/guide/progress')[0].body)).not.toMatch(/Daily|Weekly/)
    fireEvent.click(within(list).getByTestId('guide-find-pick-2'))
    // Picked: the guide comes back and walks on to the one the person chose.
    await waitFor(() => expect(writes('/api/guide/progress').some(c => c.body.outcome === 'target_found')).toBe(true), { timeout: 5000 })
    expect(await screen.findByText(L('step_ui_show_here', { label: 'Run Now' }), {}, { timeout: 5000 })).toBeTruthy()
    expect(JSON.stringify(writes('/api/guide/progress').map(c => c.body))).not.toMatch(/Daily|Weekly/)
  })

  it('points at the container that hides several matches, then lets the person pick once it is open', async () => {
    const g = findGuide({ label: 'Copy' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <MoreMenu items={['Copy', 'Copy']} />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('step_ui_find_open', { target: 'Copy' }), {}, { timeout: 3000 })).toBeTruthy()
    expect(await screen.findByTestId('guide-target-outline', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByTestId('item-0')).toBeNull()
    fireEvent.pointerDown(screen.getByTestId('more'), { button: 0, ctrlKey: false })
    await waitFor(() => expect(writes('/api/guide/progress')[0]?.body).toMatchObject({ step_index: 0, outcome: 'observed' }), { timeout: 3000 })
    const list = await screen.findByTestId('guide-find-ambiguous', {}, { timeout: 5000 })
    expect(within(list).getAllByRole('button')).toHaveLength(2)
    fireEvent.click(within(list).getByTestId('guide-find-pick-2'))
    await waitFor(() => expect(writes('/api/guide/progress').some(c => c.body.outcome === 'target_found')).toBe(true), { timeout: 5000 })
  })

  it('refuses a control on a trust-root page even when the guide named no page', async () => {
    const g = findGuide({ label: 'Profiles' })
    pending = [g]
    walk(g)
    // The dashboard's router keeps the window's address; the policy reads that.
    window.history.replaceState(null, '', '/settings/security')
    try {
      renderGuide('/settings/security', <button>Profiles</button>)
      fireEvent.click(await screen.findByTestId('guide-start'))
      await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 5000 })
      expect(writes('/api/guide/progress')[0].body).toMatchObject({ outcome: 'target_missing', detail: 'not_found', find: { result: 'none', count: 0 } })
      expect(await screen.findByTestId('guide-find-sensitive')).toBeTruthy()
      expect(screen.queryByTestId('guide-target-outline')).toBeNull()
    } finally { window.history.replaceState(null, '', '/') }
  })

  it('shows the caution for a destructive control found by its Chinese name, though the agent sent none', async () => {
    const g = findGuide({ label: '删除' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <button {...marks({ auto: "auto:schedule:JobRow:pages.schedulePage.delete" })}>删除</button>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 3000 })
    expect(await screen.findByTestId('guide-step-caution', {}, { timeout: 3000 })).toBeTruthy()
  })

  it('stops the probe the moment Cancel is pressed, before the gateway answers', async () => {
    const g = findGuide({ label: 'Rebuild index' })
    pending = [g]
    walk(g)
    let answer: () => void = () => {}
    server.use(http.post('/api/guide/cancel', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      calls.push({ path: '/api/guide/cancel', body, headers: request.headers })
      await new Promise<void>(r => { answer = r })
      return HttpResponse.json({ guide: { ...claimed(g), status: 'cancelled', owner_tab: null, revision: claimed(g).revision + 1 } })
    }))
    // Many registered containers, none holding the name: a probe left running
    // would keep opening them one after another.
    const opened: number[] = []
    const restored: number[] = []
    function Containers() {
      useEffect(() => {
        const offs = Array.from({ length: 12 }, (_, i) => {
          let open = false
          return registerProbeTarget({
            kind: 'popup',
            trigger: () => document.querySelector<HTMLElement>(`[data-testid="box-${i}"]`),
            isOpen: () => open,
            open: () => { open = true; opened.push(i); return () => { open = false; restored.push(i) } },
          })
        })
        return () => { for (const off of offs) off() }
      }, [])
      return <>{Array.from({ length: 12 }, (_, i) => <button key={i} data-testid={`box-${i}`}>{`Box ${i}`}</button>)}</>
    }
    renderGuide('/chat/slot-A', <Containers />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(opened.length).toBeGreaterThan(0), { timeout: 3000 })
    fireEvent.click(screen.getByRole('button', { name: L('cancel_guide') }))
    const atCancel = opened.length
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    await tickTracker(2)
    // The gateway has still not answered, and nothing more was opened.
    expect(opened.length).toBe(atCancel)
    expect(restored).toEqual(opened)
    answer()
  })

  it('refuses a trust-root page on the offer itself', () => {
    expect(resolveGuideActions([find({ route: '/settings/computer-use', label: 'Enable' })])).toEqual({ ok: false, reason: 'sensitive_page' })
  })
})
