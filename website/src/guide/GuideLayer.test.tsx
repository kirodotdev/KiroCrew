/**
 * Registered-action guide: consent before anything moves, the arrow's target
 * and pointer contract, owner-tab / revision handling, which slot "is being
 * viewed", per-request guide headers on the ONE save a committed step names,
 * that the MCP guide only hands off to the native add form, and that the
 * browser never claims a save succeeded.
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor as rtlWaitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { useEffect, useState, type ReactNode } from 'react'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import { TAB_ID } from '../api/tabId'
import { NavigationLeaveGuardProvider, useRegisterNavigationLeaveGuard } from '../components/NavigationLeaveGuard'
import { applyGuideUpdate, GUIDE_PENDING_QUERY_KEY, type Guide, type GuideAction } from '../api/guide'
import { _resetViewedThreadForTests, clearViewedThreadSlot, setViewedThreadSlot } from '../lib/viewedThread'
import { i18nT } from '../i18n/t'
import { endedBySlot, GuideProvider, reconcilePending, useGuide } from './GuideContext'
import { useGuideOpenedSurface } from './useGuideOpenedSurface'
import { dropSelection, reportSelection, useGuideGate, useGuidePredicate, useGuideSelection } from './guidePredicates'
import GuideLayer, { arrowPlacement, GUIDE_FINISHED_DISMISS_MS, GUIDE_RELAYOUT_MS, guideReturnFor, isFinalGuideStep, memberSlugOfSlot, nearbyControlBoxes, pageChromeBoxes, popoverPlacement, clampToViewport, keepClearBoxes, KEEP_CLEAR_REPLY_PX, NEXT_FIELD_PX, openModalDialog, stepCaution, pickAlternativeRects, coveredByModal, onlyPickName, namedPickShown } from './GuideLayer'
import type { GuideStepPlan } from './guideActions'
import GuideOfferCard from './GuideOfferCard'
import { alreadyAt, findOpenerLocation, GUIDE_ANCHORS, isSensitiveSetting, resolveGuideAction, resolveGuideActions } from './guideActions'
import { factVerdict, GUIDE_EARLIER_STEP_WAIT_MS, GUIDE_FOUND_MAX_ATTEMPTS, GUIDE_FOUND_RETRY_MS, GUIDE_TARGET_WAIT_MS, GUIDE_TRACK_TICK_MS, pageStillLoading, pickMade, resolveGuideTarget, resolveStepTarget, stepBoundHolds, useGuideStepTracker, visibleBox } from './useGuideStepTracker'
import { liveTarget } from './liveRegistry'
import { GUIDE_BUILD_DIGEST, GUIDE_PLANS } from '../uiLocations/guidePlans.gen'
import { UI_LOCATIONS } from '../uiLocations/descriptors'
import McpCustomServerModal from '../components/McpCustomServerModal'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { resolveSettingElementStrict } from '../hooks/useSettingHighlight'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { resetFindStates } from './findByName'
import { registerProbeTarget } from './probeRegistry'
import { marks } from '../test/guideTargets'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.guideLayer.${k}`, v)

async function tickTracker(ticks = 1) {
  await act(async () => { await vi.advanceTimersByTimeAsync(GUIDE_TRACK_TICK_MS * ticks) })
}
const waitFor: typeof rtlWaitFor = (callback, options) => rtlWaitFor(async () => {
  await act(async () => { await vi.advanceTimersByTimeAsync(GUIDE_TRACK_TICK_MS) })
  return callback()
}, options)


type Call = { path: string; body: Record<string, unknown>; headers: Headers }
let calls: Call[]
let pending: Guide[]
/** What each write returns; defaults to echoing the guide the test set. */
let onWrite: (path: string, body: Record<string, unknown>) => Guide | null

const guide = (over: Partial<Guide> = {}): Guide => ({
  guide_id: 'g1',
  slot_key: 'slot-A',
  status: 'offered',
  revision: 1,
  owner_tab: null,
  action_index: 0,
  step_index: 0,
  actions: [{ id: 'crewmate.create', params: { name: 'radar', goal: 'watch the build' } }],
  reason: 'You asked for a crewmate.',
  expires_at: null,
  lease_expires_at: null,
  ...over,
})

const claimed = (g: Guide): Guide => ({ ...g, status: 'active', owner_tab: TAB_ID, revision: g.revision + 1 })

/** A UI-only action of several steps (the MCP add form): the fixture for a
 *  step that completes when the page moves on to a later registered control. */
const MCP_OPEN = { id: 'mcp.open_add', params: {} }

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc" data-granted={(loc.state as { leaveGranted?: boolean } | null)?.leaveGranted ? '1' : '0'}>{loc.pathname + loc.search}</div>
}

/**
 * The native MCP surfaces, reduced to what the guide touches: the Connections
 * tab (starting on Services), McpTab's Add Custom button, and the REAL add
 * dialog. The anchors are the same literals the pages carry (pinned below).
 */
function NativeMcpHost({ startOn = 'services' }: { startOn?: 'services' | 'mcp-servers' }) {
  const [tab, setTab] = useState(startOn)
  const [open, setOpen] = useState(false)
  return (
    <>
      <Target anchor="mcp.servers-tab" testId="native-mcp-tab" onClick={() => setTab('mcp-servers')} />
      {tab === 'mcp-servers' && <Target anchor="mcp.add-custom" testId="native-add-custom" onClick={() => setOpen(true)} />}
      <McpCustomServerModal open={open} onClose={() => setOpen(false)} />
    </>
  )
}

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

/** A registered control with a real-looking box (happy-dom lays nothing out). */
function Target({ anchor, rect = { top: 300, left: 100, width: 80, height: 30 }, testId, onClick }: {
  anchor?: string
  rect?: { top: number; left: number; width: number; height: number }
  testId?: string
  onClick?: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId ?? `target-${anchor ?? 'none'}`} {...marks({ anchor: anchor }, (el) => {
        if (!el) return
        el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top, toJSON: () => ({}) }) as DOMRect
      })}
    >
      target
    </button>
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

/** The wall clock a test that moves time past a bound starts at. */
const T0 = Date.UTC(2026, 0, 15, 12)
/** Freeze Date at T0 (timers stay real): the tracker stamps T0, and the test moves the clock past its bound. */
function freezeClock() {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(T0)
  vi.spyOn(Date, 'now').mockReturnValue(T0)
}

const writes = (path: string) => calls.filter(c => c.path === path)

/** A page surface whose draft appears and disappears on cue. */
const draftState = { atStake: false }
function DraftSurface() {
  useRegisterNavigationLeaveGuard(() => !draftState.atStake)
  return null
}

describe('offer and consent', () => {
  it('a draft typed while the claim was out stops the walk to the step page; the guide stays claimed', async () => {
    draftState.atStake = false
    pending = [guide()]
    let release: () => void = () => {}
    server.use(http.post('/api/guide/claim', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      calls.push({ path: '/api/guide/claim', body, headers: request.headers })
      await new Promise<void>(r => { release = r })
      return HttpResponse.json({ guide: claimed(guide()) })
    }))
    renderGuide('/chat/slot-A', <DraftSurface />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    // The user starts typing something while the claim is in flight.
    draftState.atStake = true
    release()
    await waitFor(() => expect(qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY)?.[0]?.status).toBe('active'))
    expect(screen.getByTestId('loc').textContent).toBe('/chat/slot-A')
    draftState.atStake = false
  })

  it('offers the guide only in the chat it came from, and nothing moves before Start', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A')
    expect(await screen.findByTestId('guide-start')).toBeTruthy()
    // The gateway's internal reason token is never painted as copy.
    expect(screen.queryByText('You asked for a crewmate.')).toBeNull()
    // Offered is not accepted: no claim, no navigation, no pre-fill.
    await tickTracker(1)
    expect(calls).toHaveLength(0)
    expect(screen.getByTestId('loc').textContent).toBe('/chat/slot-A')
  })

  it('says so when the pending-guides read fails, and can be closed', async () => {
    server.use(http.get('/api/guide/pending', () => HttpResponse.json({ error: 'gateway unavailable' }, { status: 500 })))
    renderGuide('/chat/slot-A')
    expect(await screen.findByTestId('guide-pending-error')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: L('close') }))
    await waitFor(() => expect(screen.queryByTestId('guide-pending-error')).toBeNull())
  })

  it('stays silent when the owner-only pending route refuses this session', async () => {
    server.use(http.get('/api/guide/pending', () => HttpResponse.json({ error: 'owner only' }, { status: 403 })))
    renderGuide('/chat/slot-A')
    await waitFor(() => expect(qc.getQueryState(GUIDE_PENDING_QUERY_KEY)?.status).toBe('error'))
    expect(screen.queryByTestId('guide-pending-error')).toBeNull()
  })

  it('Start claims first, then navigates to the Crewmates hand-off with the draft', async () => {
    pending = [guide()]
    onWrite = (name, b) => (name === 'claim' ? claimed({ ...guide(), revision: b.revision as number }) : null)
    renderGuide('/chat/slot-A')
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/members?create=1&name=radar&goal=watch+the+build'))
    const [claim] = writes('/api/guide/claim')
    expect(claim.body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 1 })
    expect(claim.body).not.toHaveProperty('take_over')
  })

  it('reads the Crewmates page thread, not a stale chat activeSlot', async () => {
    pending = [guide({ status: 'active', owner_tab: 'other-tab' })]
    // activeSlot is still slot-A from an earlier chat visit, but on /members
    // the thread on screen is whatever the page registered.
    renderGuide('/members?member=radar', undefined, null)
    await act(async () => { await qc.refetchQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }) })
    expect(screen.queryByTestId('guide-pill')).toBeNull()
    act(() => setViewedThreadSlot('slot-B'))
    expect(screen.queryByTestId('guide-pill')).toBeNull()
    act(() => setViewedThreadSlot('slot-A'))
    expect(await screen.findByText(L('other_tab'))).toBeTruthy()
  })

  it('keeps the members address when entering from the Crewmates page', () => {
    const r = resolveGuideAction({ id: 'crewmate.create', params: { goal: 'g' } })
    if (!r.ok || r.action.enter.kind !== 'navigate') throw new Error('expected navigate')
    expect(r.action.enter.to({ pathname: '/members', search: '?member=default' })).toBe('/members?member=default&create=1&goal=g')
  })
})

async function startCrewmate(extra?: ReactNode, over: Partial<Guide> = {}, at = '/members') {
  pending = [guide(over)]
  let current = claimed(guide(over))
  onWrite = (name, b) => {
    if (name === 'claim') return current
    if (name === 'progress') {
      current = { ...current, revision: current.revision + 1, step_index: (b.step_index as number) + (b.outcome === 'observed' ? 1 : 0), status: b.outcome === 'observed' ? 'active' : 'target_missing' }
      return current
    }
    return current
  }
  renderGuide('/chat/slot-A', extra)
  fireEvent.click(await screen.findByTestId('guide-start'))
  await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain(at))
  return () => current
}

describe('arrow and targets', () => {
  it('takes the outline and arrow off a target an open dialog covers, and puts them back when it closes', async () => {
    const rect = { top: 300, left: 100, width: 80, height: 30 }
    function Host() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <Target anchor={GUIDE_ANCHORS.crewmateCreate} rect={rect} />
          <button type="button" data-testid="open-dialog" onClick={() => setOpen(true)}>open</button>
          {open && <div role="dialog" aria-modal="true"><button type="button" data-testid="close-dialog" onClick={() => setOpen(false)}>close</button></div>}
        </>
      )
    }
    await startCrewmate(<Host />)
    await screen.findByTestId('guide-target-outline')
    fireEvent.click(screen.getByTestId('open-dialog'))
    await waitFor(() => expect(screen.queryByTestId('guide-target-outline')).toBeNull())
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    // The panel stays, as the bottom chip, off the dialog's content.
    expect(screen.getByTestId('guide-pill').getAttribute('data-placement')).toBe('chip')
    fireEvent.click(screen.getByTestId('close-dialog'))
    await screen.findByTestId('guide-target-outline')
  })

  it('points at the registered control without a scrim or pointer capture, and follows it', async () => {
    const rect = { top: 300, left: 100, width: 80, height: 30 }
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.crewmateCreate} rect={rect} />)
    const outline = await screen.findByTestId('guide-target-outline')
    const arrow = screen.getByTestId('guide-arrow')
    for (const el of [outline, arrow]) {
      expect(el.className).toContain('pointer-events-none')
      expect(el.getAttribute('aria-hidden')).toBe('true')
    }
    // No element of the layer covers the viewport.
    expect(document.body.querySelector('.inset-0')).toBeNull()
    // The step panel floats next to the control, opposite the arrow (the arrow
    // is above this target, so the panel is below it), and takes no layout space.
    const panel = screen.getByTestId('guide-pill')
    expect(panel.className).toContain('fixed')
    expect(panel.parentElement).toBe(document.body)
    expect(panel.className).toContain('pointer-events-auto')
    expect(panel.getAttribute('data-placement')).toBe('opposite')
    expect(arrowPlacement(rect, { width: window.innerWidth, height: window.innerHeight }).up).toBe(false)
    expect(panel.style.top).toBe('340px')
    expect(panel).toContainElement(screen.getByTestId('guide-step-text'))
    expect(outline.style.top).toBe('296px')
    // The page underneath still gets its click.
    const onClick = vi.fn()
    screen.getByTestId(`target-${GUIDE_ANCHORS.crewmateCreate}`).addEventListener('click', onClick)
    fireEvent.click(screen.getByTestId(`target-${GUIDE_ANCHORS.crewmateCreate}`))
    expect(onClick).toHaveBeenCalled()
    // Scrolling re-measures the same control, and the panel follows it.
    rect.top = 420
    act(() => { window.dispatchEvent(new Event('scroll')) })
    await waitFor(() => expect(screen.getByTestId('guide-target-outline').style.top).toBe('416px'))
    expect(screen.getByTestId('guide-pill').style.top).toBe('460px')
    expect(screen.getByTestId('guide-pill')).toBe(panel)
  })

  describe('the step panel on a real page', () => {
    /** An element with a real-looking box (happy-dom lays nothing out). */
    const boxed = (r: { top: number; left: number; width: number; height: number }) => (el: HTMLElement | null) => {
      if (el) el.getBoundingClientRect = () => ({ ...r, right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top, toJSON: () => ({}) }) as DOMRect
    }
    let restoreHeight: () => void
    beforeEach(() => {
      const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')
      Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
        configurable: true,
        get(this: HTMLElement) { return this.getAttribute('data-testid') === 'guide-pill' ? 120 : 0 },
      })
      restoreHeight = () => { if (desc) Object.defineProperty(HTMLElement.prototype, 'offsetHeight', desc) }
    })
    afterEach(() => restoreHeight())
    const pillTop = () => Number.parseFloat(screen.getByTestId('guide-pill').style.top)

    it('stays next to the target even when that covers another control', async () => {
      // The default target sits at 300..330; a full-width row lies where the panel first goes.
      await startCrewmate(<>
        <Target anchor={GUIDE_ANCHORS.crewmateCreate} />
        <button type="button" ref={boxed({ top: 400, left: 0, width: 1024, height: 40 })}>row</button>
      </>)
      await screen.findByTestId('guide-target-outline')
      // Not pushed away to clear the row: it keeps the first spot, beside the step.
      await waitFor(() => expect(screen.getByTestId('guide-pill').getAttribute('data-placement')).toBe('opposite'))
      expect(pillTop()).toBeGreaterThan(330)
    })

    it('never covers the page chrome: the top bar and a sticky back bar', async () => {
      await startCrewmate(
        <main id="main-content" ref={boxed({ top: 42, left: 0, width: 1024, height: 726 })}>
          <div className="sticky top-0" ref={boxed({ top: 42, left: 0, width: 1024, height: 108 })} />
          <Target anchor={GUIDE_ANCHORS.crewmateCreate} rect={{ top: 200, left: 100, width: 80, height: 30 }} />
          <button type="button" ref={boxed({ top: 250, left: 0, width: 1024, height: 40 })}>row</button>
        </main>,
      )
      await screen.findByTestId('guide-target-outline')
      // Above the arrow would be the bar, so it goes below the target, over the row.
      await waitFor(() => expect(screen.getByTestId('guide-pill').getAttribute('data-placement')).toBe('opposite'))
      expect(pillTop()).toBeGreaterThanOrEqual(150)
    })
  })

  it('floats a bottom chip, not a top bar, while no target is tracked', async () => {
    await startCrewmate(<Target testId="look-alike-create" />)
    const panel = await screen.findByTestId('guide-pill')
    expect(await screen.findByText(L('looking_for_control'))).toBeTruthy()
    expect(panel.getAttribute('data-placement')).toBe('chip')
    expect(panel.parentElement).toBe(document.body)
    expect(panel.className).toContain('fixed')
    expect(panel.className).toContain('bottom-safe-offset-4')
    expect(panel.style.top).toBe('')
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
  })

  it('reports observed once the form reaches a later registered control', async () => {
    await startCrewmate(<><Target anchor={GUIDE_ANCHORS.mcpServersTab} /><Target anchor={GUIDE_ANCHORS.mcpAddCustom} /></>, { actions: [MCP_OPEN] }, '/capabilities')
    await waitFor(() => expect(writes('/api/guide/progress').length).toBeGreaterThan(0))
    const [first] = writes('/api/guide/progress')
    expect(first.body).toMatchObject({ guide_id: 'g1', tab_id: TAB_ID, action_index: 0, step_index: 0, outcome: 'observed' })
  })

  it('reports target_missing after the bounded wait and never points at a look-alike', async () => {
    freezeClock()
    // An unregistered button that looks like the step's Next is not a target.
    await startCrewmate(<Target testId="look-alike-create" />)
    // Let the tracker start its wait, then move the clock past the bound.
    await tickTracker(2)
    expect(writes('/api/guide/progress')).toHaveLength(0)
    vi.spyOn(Date, 'now').mockImplementation(() => T0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(writes('/api/guide/progress').map(c => c.body.outcome)).toEqual(['target_missing']))
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    expect(await screen.findByText(L('target_missing'))).toBeTruthy()
  })

  it('places the arrow below a control at the top edge and keeps it on screen', () => {
    expect(arrowPlacement({ top: 4, left: 0, width: 10, height: 10 }, { width: 320, height: 600 })).toMatchObject({ up: true, left: 4 })
    const p = arrowPlacement({ top: 300, left: 310, width: 20, height: 20 }, { width: 320, height: 600 })
    expect(p.up).toBe(false)
    expect(p.left).toBeLessThanOrEqual(320 - 24 - 4)
  })

  describe('the arrow keeps clear of the outline and the neighbouring controls', () => {
    type B = { top: number; left: number; bottom: number; right: number }
    const arrowBox = (a: { top: number; left: number }): B => ({ top: a.top, left: a.left, bottom: a.top + 24, right: a.left + 24 })
    /** Smallest distance between two boxes (0 when they touch or overlap). */
    const dist = (a: B, b: B) => Math.max(0, Math.max(a.left - b.right, b.left - a.right), Math.max(a.top - b.bottom, b.top - a.bottom))
    const outlineOf = (r: { top: number; left: number; width: number; height: number }): B =>
      ({ top: r.top - 4, left: r.left - 4, bottom: r.top + r.height + 4, right: r.left + r.width + 4 })

    // Settings > Display > Theme, measured on a pod (the Mode row is the target).
    it.each([
      ['desktop', { width: 1440, height: 900 }, { top: 256.7, left: 532, width: 884, height: 102.9 },
        [{ top: 202.7, left: 532, bottom: 240.7, right: 1416 }, { top: 373.6, left: 532, bottom: 403.8, right: 638 }, { top: 270.6, left: 288, bottom: 306.7, right: 508 }]],
      ['390px phone', { width: 390, height: 844 }, { top: 230.4, left: 16, width: 358, height: 102.9 },
        [{ top: 176.4, left: 16, bottom: 214.4, right: 374 }, { top: 347.3, left: 16, bottom: 377.4, right: 122 }]],
    ] as const)('%s Mode row under the Theme dropdown', (_name, vp, rect, controls) => {
      const a = arrowPlacement(rect, vp, controls)
      const box = arrowBox(a)
      expect(dist(box, outlineOf(rect))).toBeGreaterThanOrEqual(6)
      for (const c of controls) expect(dist(box, c)).toBeGreaterThanOrEqual(6)
      // The nudge moves the arrow 4px away from the target: the box it sweeps
      // keeps the same distance from every neighbour, not only its rest spot.
      const swept: B = {
        top: box.top - (a.dir === 'down' ? 4 : 0), left: box.left - (a.dir === 'right' ? 4 : 0),
        bottom: box.bottom + (a.dir === 'up' ? 4 : 0), right: box.right + (a.dir === 'left' ? 4 : 0),
      }
      for (const c of controls) expect(dist(swept, c)).toBeGreaterThanOrEqual(6)
      expect(a.dir).toBe('up')
      // The panel still never covers the target or the arrow.
      const p = popoverPlacement(rect, vp, 120, controls)
      const pb = { top: p.top, left: p.left, bottom: p.top + 120, right: p.left + p.width }
      expect(dist(pb, box)).toBeGreaterThan(0)
      expect(dist(pb, outlineOf(rect))).toBeGreaterThan(0)
    })

    it('keeps the nudge animation clear of a neighbour that sits exactly at the gap', () => {
      // No room above, so the arrow goes below the target; a full-width control
      // 6px under the arrow's REST spot is only 2px away at the nudge's peak.
      const vp = { width: 400, height: 600 }
      const rect = { top: 10, left: 150, width: 100, height: 40 }
      const below = arrowPlacement(rect, vp)
      const neighbour = { top: below.top + 24 + 6, left: 0, bottom: below.top + 24 + 40, right: 400 }
      const a = arrowPlacement(rect, vp, [neighbour])
      const box = arrowBox(a)
      const swept: B = {
        top: box.top - (a.dir === 'down' ? 4 : 0), left: box.left - (a.dir === 'right' ? 4 : 0),
        bottom: box.bottom + (a.dir === 'up' ? 4 : 0), right: box.right + (a.dir === 'left' ? 4 : 0),
      }
      expect(dist(swept, neighbour)).toBeGreaterThanOrEqual(6)
    })

    // Every control on screen at Settings > Display > Theme, measured on a pod,
    // minus the Mode row's own segmented buttons (parts of the target).
    const DESK_CONTROLS = {
      themeDropdown: { top: 202.7, left: 532, bottom: 240.7, right: 1416 },
      newTheme: { top: 373.6, left: 532, bottom: 403.8, right: 638 },
      installSource: { top: 446.3, left: 532, bottom: 484.3, right: 672 },
      installUrl: { top: 446.3, left: 680, bottom: 484.3, right: 1343 },
      install: { top: 446.3, left: 1351, bottom: 484.3, right: 1416 },
      subnavTheme: { top: 232.4, left: 288, bottom: 268.6, right: 508 },
      subnavSidebar: { top: 270.6, left: 288, bottom: 306.7, right: 508 },
      commandBar: { top: 7, left: 559.6, bottom: 35, right: 842.4 },
      navNotifications: { top: 351.9, left: 86, bottom: 388, right: 261 },
      navShortcuts: { top: 390, left: 86, bottom: 426.2, right: 261 },
    }
    const PHONE_CONTROLS = {
      menu: { top: 1, left: 8, bottom: 41, right: 48 },
      bell: { top: 7, left: 350, bottom: 35, right: 378 },
      back: { top: 42, left: 10, bottom: 79.7, right: 93 },
      themeDropdown: { top: 176.4, left: 16, bottom: 214.4, right: 374 },
      newTheme: { top: 347.3, left: 16, bottom: 377.4, right: 122 },
      installSource: { top: 420, left: 16, bottom: 458, right: 156 },
      installUrl: { top: 420, left: 164, bottom: 458, right: 301 },
      install: { top: 420, left: 309, bottom: 458, right: 374 },
    }
    it.each([
      ['desktop', { width: 1440, height: 900 }, { top: 256.7, left: 532, width: 884, height: 102.9 }, DESK_CONTROLS, { top: 0, left: 0, bottom: 42, right: 1440 }],
      ['390px phone', { width: 390, height: 844 }, { top: 230.4, left: 16, width: 358, height: 102.9 }, PHONE_CONTROLS, { top: 0, left: 0, bottom: 79.7, right: 390 }],
    ] as const)('%s: the panel sits right next to the target and never covers the header, the target or the arrow', (_n, vp, rect, controls, chrome) => {
      const list = Object.values(controls)
      const H = 127
      const p = popoverPlacement(rect, vp, H, list, [chrome])
      const pb = { top: p.top, left: p.left, bottom: p.top + H, right: p.left + p.width }
      const over = (a: B, b: B) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom
      expect(over(pb, chrome), 'header').toBe(false)
      expect(over(pb, outlineOf(rect)), 'target').toBe(false)
      expect(over(pb, arrowBox(arrowPlacement(rect, vp, list))), 'arrow').toBe(false)
      expect(pb.top).toBeGreaterThanOrEqual(8)
      expect(pb.bottom).toBeLessThanOrEqual(vp.height - 8)
      // Next to the step, not walked away past the neighbouring controls.
      expect(p.side === 'opposite' || p.side === 'beyond-arrow').toBe(true)
      expect(dist(pb, outlineOf(rect))).toBeLessThanOrEqual(40)
    })

    it('with no clear spot, covers the least control area and never the target, arrow or header', () => {
      const vp = { width: 390, height: 500 }
      const rect = { top: 200, left: 16, width: 358, height: 100 }
      // Controls filling every free band of the viewport.
      const controls = [
        { top: 90, left: 16, bottom: 180, right: 374 },
        { top: 340, left: 16, bottom: 400, right: 374 },
        { top: 410, left: 16, bottom: 420, right: 374 },
      ]
      const chrome = { top: 0, left: 0, bottom: 80, right: 390 }
      const H = 100
      const p = popoverPlacement(rect, vp, H, controls, [chrome])
      const pb = { top: p.top, left: p.left, bottom: p.top + H, right: p.left + p.width }
      const area = (a: B, b: B) => Math.max(0, Math.min(a.right, b.right) - Math.max(a.left, b.left)) * Math.max(0, Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top))
      expect(area(pb, chrome)).toBe(0)
      expect(area(pb, outlineOf(rect))).toBe(0)
      expect(area(pb, arrowBox(arrowPlacement(rect, vp, controls)))).toBe(0)
      // Below the arrow (334..) every spot covers part of the two lower controls;
      // the one chosen covers less than the plain spot under the arrow.
      const covered = controls.reduce((n, c) => n + area(pb, c), 0)
      expect(covered).toBeGreaterThan(0)
      const plain = { top: 340, left: 8, bottom: 440, right: 382 }
      expect(covered).toBeLessThan(controls.reduce((n, c) => n + area(plain, c), 0))
    })

    it('covers a control before the header, and either before the target', () => {
      const vp = { width: 390, height: 844 }
      // A tall target with a row under it: only the header and the row are left.
      const rect = { top: 150, left: 16, width: 358, height: 450 }
      const row = { top: 610, left: 0, bottom: 836, right: 390 }
      const chrome = { top: 0, left: 0, bottom: 80, right: 390 }
      const p = popoverPlacement(rect, vp, 100, [row], [chrome])
      const pb = { top: p.top, left: p.left, bottom: p.top + 100, right: p.left + p.width }
      const over = (a: B, b: B) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom
      expect(over(pb, chrome)).toBe(false)
      expect(over(pb, outlineOf(rect))).toBe(false)
      expect(over(pb, row)).toBe(true)
    })

    it('keeps clear of the composer, and on a phone of the end of the reply above it', () => {
      const doc = document.implementation.createHTMLDocument('x')
      const dock = doc.createElement('div')
      dock.setAttribute('data-guide-keep-clear', '')
      dock.getBoundingClientRect = () => ({ top: 720, left: 16, width: 358, height: 88, bottom: 808, right: 374, x: 16, y: 720, toJSON: () => ({}) }) as DOMRect
      doc.body.appendChild(dock)
      expect(keepClearBoxes({ width: 390, height: 844 }, doc)).toEqual([{ top: 720 - KEEP_CLEAR_REPLY_PX, left: 16, bottom: 808, right: 374 }])
      expect(keepClearBoxes({ width: 1280, height: 844 }, doc)).toEqual([{ top: 720, left: 16, bottom: 808, right: 374 }])
      // The page chrome the panel avoids includes it.
      const main = doc.createElement('main')
      main.id = 'main-content'
      main.getBoundingClientRect = () => ({ top: 0, left: 0, width: 1280, height: 844, bottom: 844, right: 1280, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect
      doc.body.appendChild(main)
      expect(pageChromeBoxes({ width: 1280, height: 844 }, doc)).toContainEqual({ top: 720, left: 16, bottom: 808, right: 374 })
      // A panel for a target just above the composer floats above both.
      const vp = { width: 390, height: 844 }
      const rect = { top: 560, left: 40, width: 120, height: 28 }
      const p = popoverPlacement(rect, vp, 120, [], keepClearBoxes(vp, doc))
      expect(p.top + 120).toBeLessThanOrEqual(720 - KEEP_CLEAR_REPLY_PX)
    })

    it('outlines only the part of a target its scrolling panel shows', () => {
      const panel = document.createElement('div')
      panel.style.overflowX = 'auto'
      const table = document.createElement('table')
      panel.appendChild(table)
      document.body.appendChild(panel)
      const box = (top: number, left: number, w: number, h: number) => ({ top, left, bottom: top + h, right: left + w, width: w, height: h, x: left, y: top, toJSON: () => ({}) })
      panel.getBoundingClientRect = () => box(100, 200, 600, 400) as DOMRect
      table.getBoundingClientRect = () => box(120, 200, 1100, 300) as DOMRect
      expect(visibleBox(table)).toEqual({ top: 120, left: 200, width: 600, height: 300 })
      panel.remove()
    })

    it('keeps the field under a phone target clear, so the panel goes above it', () => {
      const vp = { width: 390, height: 844 }
      const rect = { top: 430, left: 16, width: 100, height: 30 }
      const p = popoverPlacement(rect, vp, 120)
      const under = { top: rect.top + rect.height + 4, bottom: rect.top + rect.height + 4 + NEXT_FIELD_PX }
      expect(p.top + 120 <= under.top || p.top >= under.bottom).toBe(true)
    })

    it('sees a page modal, never the guide layer itself', () => {
      const dlg = document.createElement('div')
      dlg.setAttribute('role', 'dialog')
      dlg.setAttribute('aria-modal', 'true')
      document.body.appendChild(dlg)
      expect(openModalDialog()).toBe(dlg)
      dlg.remove()
      expect(openModalDialog()).toBeNull()
    })

    it('outlines only the part of a target taller than the screen', () => {
      const vp = { width: 1280, height: 800 }
      expect(clampToViewport({ top: 100, left: 300, width: 900, height: 2400 }, vp)).toEqual({ top: 100, left: 300, width: 900, height: 692 })
      // The outline is drawn 4px outside the box: an 8px margin keeps it on screen.
      expect(clampToViewport({ top: -50, left: -10, width: 200, height: 100 }, vp)).toEqual({ top: 8, left: 8, width: 182, height: 42 })
      expect(clampToViewport({ top: 100, left: 1000, width: 600, height: 40 }, vp)).toEqual({ top: 100, left: 1000, width: 272, height: 40 })
      const inside = { top: 10, left: 10, width: 40, height: 20 }
      expect(clampToViewport(inside, vp)).toEqual(inside)
    })

    it('reads the page chrome: the top bar and a sticky bar pinned to the main region', () => {
      const doc = document.implementation.createHTMLDocument('x')
      const rectOf = (r: { top: number; left: number; width: number; height: number }) => () =>
        ({ ...r, bottom: r.top + r.height, right: r.left + r.width, x: r.left, y: r.top, toJSON: () => r }) as DOMRect
      const main = doc.createElement('main')
      main.id = 'main-content'
      main.getBoundingClientRect = rectOf({ top: 42, left: 0, width: 390, height: 802 })
      const bar = doc.createElement('div')
      bar.className = 'sticky top-0'
      bar.getBoundingClientRect = rectOf({ top: 42, left: 0, width: 390, height: 37.7 })
      const lower = doc.createElement('div')
      lower.className = 'sticky'
      lower.getBoundingClientRect = rectOf({ top: 400, left: 0, width: 390, height: 40 })
      main.append(bar, lower)
      doc.body.append(main)
      expect(pageChromeBoxes({ width: 390, height: 844 }, doc)).toEqual([{ top: 0, left: 0, bottom: 79.7, right: 390 }])
      bar.remove()
      expect(pageChromeBoxes({ width: 390, height: 844 }, doc)).toEqual([{ top: 0, left: 0, bottom: 42, right: 390 }])
    })

    it('on a phone, keeps the page\'s own header (title, subtitle, its main button) clear too', () => {
      const doc = document.implementation.createHTMLDocument('x')
      const rectOf = (r: { top: number; left: number; width: number; height: number }) => () =>
        ({ ...r, bottom: r.top + r.height, right: r.left + r.width, x: r.left, y: r.top, toJSON: () => r }) as DOMRect
      const main = doc.createElement('main')
      main.id = 'main-content'
      main.getBoundingClientRect = rectOf({ top: 42, left: 0, width: 390, height: 802 })
      const header = doc.createElement('div')
      header.setAttribute('data-testid', 'page-header')
      header.getBoundingClientRect = rectOf({ top: 50, left: 0, width: 390, height: 90 })
      main.append(header)
      doc.body.append(main)
      expect(pageChromeBoxes({ width: 390, height: 844 }, doc)).toEqual([
        { top: 0, left: 0, bottom: 42, right: 390 },
        { top: 50, left: 0, bottom: 140, right: 390 },
      ])
      // A wide screen keeps the header's own box clear, not the whole width.
      expect(pageChromeBoxes({ width: 1280, height: 844 }, doc)).toEqual([
        { top: 0, left: 0, bottom: 42, right: 1280 },
        { top: 50, left: 0, bottom: 140, right: 390 },
      ])
      // The page's sub-navigation and the guide's own card in the chat stay clear too.
      const tabs = doc.createElement('div')
      tabs.setAttribute('role', 'tablist')
      tabs.getBoundingClientRect = rectOf({ top: 150, left: 0, width: 200, height: 300 })
      main.append(tabs)
      const card = doc.createElement('div')
      card.setAttribute('data-testid', 'guide-offer-card')
      card.getBoundingClientRect = rectOf({ top: 600, left: 500, width: 700, height: 56 })
      doc.body.append(card)
      expect(pageChromeBoxes({ width: 1280, height: 844 }, doc)).toEqual([
        { top: 0, left: 0, bottom: 42, right: 1280 },
        { top: 50, left: 0, bottom: 140, right: 390 },
        { top: 150, left: 0, bottom: 450, right: 200 },
        { top: 600, left: 500, bottom: 656, right: 1200 },
      ])
    })

    it('with neighbours above and below a rail entry, points from its free side, never over the next label', () => {
      const vp = { width: 1280, height: 800 }
      // The Artifacts rail entry with Schedule above and Sessions-panel controls far right.
      const rect = { top: 208, left: 14, width: 208, height: 42 }
      const rows = [{ top: 172, left: 14, bottom: 208, right: 222 }, { top: 250, left: 14, bottom: 290, right: 222 }]
      // A control beside the rail too, so no spot is fully clear.
      const panel = { top: 200, left: 238, bottom: 230, right: 400 }
      const a = arrowPlacement(rect, vp, [...rows, panel])
      expect(a.dir).toBe('left')
      const box = { top: a.top, left: a.left, bottom: a.top + 24, right: a.left + 24 }
      for (const r of rows) expect(box.right <= r.left || box.left >= r.right || box.bottom <= r.top || box.top >= r.bottom).toBe(true)
    })

    it('keeps GAP from the outline itself with no neighbours', () => {
      const rect = { top: 300, left: 600, width: 100, height: 30 }
      const a = arrowPlacement(rect, { width: 1440, height: 900 })
      expect(a).toMatchObject({ dir: 'down', top: 300 - 4 - 6 - 24 })
    })

    it('slides along the target, then goes beside it, before giving up', () => {
      const vp = { width: 1440, height: 900 }
      const rect = { top: 300, left: 400, width: 400, height: 40 }
      // Both vertical sides blocked at the centre only: slide to a clear column.
      const mid = [{ top: 250, left: 560, bottom: 290, right: 640 }, { top: 350, left: 560, bottom: 400, right: 640 }]
      const slid = arrowPlacement(rect, vp, mid)
      expect(slid.dir).toBe('down')
      expect(slid.left + 24 + 6 <= 560 || slid.left >= 640 + 6).toBe(true)
      // Both vertical sides blocked along the whole span: point from the side.
      const full = [{ top: 250, left: 380, bottom: 290, right: 820 }, { top: 350, left: 380, bottom: 400, right: 820 }]
      expect(arrowPlacement(rect, vp, full)).toMatchObject({ dir: 'left', left: 800 + 10 })
    })

    it('reads the nearby controls from the page, not the target, its parts or the guide panel', () => {
      const host = document.createElement('div')
      const mk = (tag: string, r: { top: number; left: number; width: number; height: number }, attrs: Record<string, string> = {}) => {
        const el = document.createElement(tag)
        for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v)
        el.getBoundingClientRect = () => ({ ...r, bottom: r.top + r.height, right: r.left + r.width, x: r.left, y: r.top, toJSON: () => r }) as DOMRect
        return el
      }
      const target = { top: 257, left: 531, width: 886, height: 104 }
      host.append(
        mk('button', { top: 203, left: 532, width: 884, height: 38 }),
        mk('button', { top: 300, left: 540, width: 70, height: 30 }),
        mk('button', { top: 600, left: 540, width: 70, height: 30 }),
      )
      const pill = mk('div', { top: 100, left: 600, width: 300, height: 200 }, { 'data-testid': 'guide-pill' })
      pill.append(mk('button', { top: 230, left: 610, width: 60, height: 30 }))
      host.append(pill)
      expect(nearbyControlBoxes(target, host)).toEqual([{ top: 203, left: 532, bottom: 241, right: 1416 }])
    })
  })

  describe('popoverPlacement', () => {
    const desk = { width: 1440, height: 900 }
    const phone = { width: 390, height: 844 }
    const H = 120
    type R = { top: number; left: number; width: number; height: number }
    const box = (r: R) => ({ top: r.top, left: r.left, bottom: r.top + r.height, right: r.left + r.width })
    const hits = (a: ReturnType<typeof box>, b: ReturnType<typeof box>) =>
      a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom
    /** The target outline plus the arrow: what the panel must never cover. */
    const guarded = (rect: R, vp: { width: number; height: number }) => {
      const a = arrowPlacement(rect, vp)
      return [
        box({ top: rect.top - 4, left: rect.left - 4, width: rect.width + 8, height: rect.height + 8 }),
        box({ top: a.top, left: a.left, width: 24, height: 24 }),
      ]
    }
    const panelBox = (p: { top: number; left: number; width: number }) => box({ top: p.top, left: p.left, width: p.width, height: H })
    const inside = (p: { top: number; left: number; width: number }, vp: { width: number; height: number }) =>
      p.left >= 8 && p.top >= 8 && p.left + p.width <= vp.width - 8 && p.top + H <= vp.height - 8

    it('sits opposite the arrow: below a target whose arrow is above it', () => {
      const rect = { top: 400, left: 600, width: 100, height: 30 }
      expect(arrowPlacement(rect, desk).up).toBe(false)
      const p = popoverPlacement(rect, desk, H)
      expect(p.side).toBe('opposite')
      expect(p.top).toBeGreaterThanOrEqual(rect.top + rect.height)
    })

    it('sits past the arrow, never on the control, when a top-edge control has its arrow below it', () => {
      // A control at the viewport top: the arrow goes below it and there is no
      // room above, so the panel goes past the arrow instead of onto the control.
      const top = { top: 10, left: 600, width: 100, height: 30 }
      const a = arrowPlacement(top, desk)
      expect(a.up).toBe(true)
      const p = popoverPlacement(top, desk, H)
      expect(p.side).toBe('beyond-arrow')
      expect(p.top).toBeGreaterThanOrEqual(a.top + 24)
    })

    it('goes above the target when the arrow is above but there is no room below', () => {
      const rect = { top: 820, left: 600, width: 100, height: 30 }
      const p = popoverPlacement(rect, desk, H)
      expect(p.side).toBe('beyond-arrow')
      expect(p.top + H).toBeLessThanOrEqual(arrowPlacement(rect, desk).top)
    })

    it('does not move away from the target to clear a control or a label', () => {
      const rect = { top: 400, left: 600, width: 100, height: 30 }
      const first = popoverPlacement(rect, desk, H)
      const firstBox = panelBox(first)
      // A control row right where the panel goes: the panel stays put over it.
      const row = { top: firstBox.top, left: 0, bottom: firstBox.top + 14, right: 1440 }
      expect(popoverPlacement(rect, desk, H, [row])).toMatchObject({ top: first.top, left: first.left, side: first.side })
    })

    it('centres on the target and clamps to the viewport by 8px', () => {
      expect(popoverPlacement({ top: 400, left: 600, width: 100, height: 30 }, desk, H)).toMatchObject({ left: 650 - 160, width: 320 })
      expect(popoverPlacement({ top: 400, left: 0, width: 20, height: 20 }, desk, H).left).toBe(8)
      expect(popoverPlacement({ top: 400, left: 1420, width: 20, height: 20 }, desk, H).left).toBe(1440 - 320 - 8)
    })

    it('spans a phone viewport minus its margins', () => {
      expect(popoverPlacement({ top: 400, left: 300, width: 60, height: 30 }, phone, H)).toMatchObject({ left: 8, width: 390 - 16 })
    })

    it('goes beside the target when neither vertical side fits, even over a control', () => {
      const vp = { width: 1440, height: 300 }
      expect(popoverPlacement({ top: 120, left: 200, width: 100, height: 40 }, vp, 200)).toMatchObject({ side: 'beside', left: 310 })
      // A control where the beside spot is no longer sends it to a far corner.
      const rect = { top: 120, left: 200, width: 100, height: 40 }
      const p = popoverPlacement(rect, vp, 200, [{ top: 60, left: 320, bottom: 100, right: 400 }])
      expect(p).toMatchObject({ side: 'beside', left: 310 })
      const pb = box({ top: p.top, left: p.left, width: p.width, height: 200 })
      for (const g of guarded(rect, vp)) expect(hits(pb, g)).toBe(false)
    })

    it('sits just left of a right-edge target whose arrow points in from the left', () => {
      // The profile side panel's Permissions row (QA S3, 1280x800): the Notes
      // row above and the Model row below push the arrow to the target's left,
      // and the panel must sit beside that arrow, not in a far corner.
      const vp = { width: 1280, height: 800 }
      const rect = { top: 578, left: 930, width: 327, height: 59 }
      const neighbours = [
        { top: 522, left: 930, bottom: 574, right: 1254 },
        { top: 641, left: 930, bottom: 692, right: 1254 },
      ]
      const a = arrowPlacement(rect, vp, neighbours)
      expect(a.dir).toBe('right')
      const h = 185
      const p = popoverPlacement(rect, vp, h, neighbours)
      expect(p.side).toBe('beside')
      expect(a.left - (p.left + p.width)).toBeGreaterThanOrEqual(0)
      expect(rect.left - (p.left + p.width)).toBeLessThanOrEqual(40)
      const pb = box({ top: p.top, left: p.left, width: p.width, height: h })
      expect(pb.bottom).toBeGreaterThan(rect.top)
      expect(pb.top).toBeLessThan(rect.top + rect.height)
      expect(hits(pb, box({ top: a.top, left: a.left, width: 24, height: 24 }))).toBe(false)
    })

    it('never covers the target or arrow, and stays on screen, anywhere on a desktop or phone', () => {
      for (const vp of [desk, phone]) {
        for (let top = 0; top <= vp.height - 30; top += 37) {
          for (const left of [0, vp.width / 2 - 40, vp.width - 80]) {
            const rect = { top, left, width: 80, height: 30 }
            const p = popoverPlacement(rect, vp, H)
            for (const g of guarded(rect, vp)) expect(hits(panelBox(p), g), `${vp.width} ${top} ${left} ${p.side}`).toBe(false)
            expect(inside(p, vp), `${vp.width} ${top} ${left}`).toBe(true)
          }
        }
      }
    })
  })

  it('resolves a Settings row strictly: no first-match stand-in for a missing occurrence', () => {
    const entry = { ...SETTINGS_REGISTRY[0], settingId: undefined, configKey: undefined, labelKey: undefined, label: 'Dup', occurrence: 2 }
    const one = document.createElement('div')
    one.setAttribute('data-setting-label', 'Dup')
    document.body.appendChild(one)
    expect(resolveSettingElementStrict(entry)).toBeNull()
    const two = document.createElement('div')
    two.setAttribute('data-setting-label', 'Dup')
    document.body.appendChild(two)
    expect(resolveSettingElementStrict(entry)).toBe(two)
    one.remove(); two.remove()
  })

  it('never guides to credential or security-ceiling settings', () => {
    const sensitive = SETTINGS_REGISTRY.filter(isSensitiveSetting).map(e => e.id)
    for (const id of ['security.denied-commands', 'secrets.jira-api-token', 'browser.attach-token', 'channels.slack-bot-token-slack']) {
      expect(sensitive).toContain(id)
      expect(resolveGuideAction({ id: 'settings.show', params: { setting_id: id } })).toEqual({ ok: false, reason: 'sensitive_setting' })
    }
    expect(resolveGuideAction({ id: 'settings.show', params: { setting_id: 'chat.default-model' } }).ok).toBe(true)
    expect(resolveGuideAction({ id: 'run.js', params: {} })).toEqual({ ok: false, reason: 'unknown_action' })
  })
})
/** Stands in for any nav click that takes the user off the step's page. */
function NavTo({ to }: { to: string }) {
  const navigate = useNavigate()
  return <button type="button" data-testid="nav-away" onClick={() => navigate(to)}>away</button>
}

/** A registered target the test can show and hide, like a panel that unmounts. */
function ToggleTarget({ anchor, initial = false }: { anchor: string; initial?: boolean }) {
  const [shown, setShown] = useState(initial)
  return (
    <>
      <button type="button" data-testid="toggle-target" onClick={() => setShown(s => !s)}>toggle</button>
      {shown && <Target anchor={anchor} />}
    </>
  )
}

/** The gateway's progress transitions, including recovery in place. */
const gatewayProgress = (current: Guide, b: Record<string, unknown>): Guide => {
  const next = { ...current, revision: current.revision + 1 }
  if (b.outcome === 'observed') {
    const resolved = resolveGuideActions(current.actions)
    const steps = resolved.ok ? resolved.actions[current.action_index]?.steps.length ?? 0 : 0
    const step = (b.step_index as number) + 1
    return step < steps
      ? { ...next, status: 'active', step_index: step }
      : { ...next, status: 'active', action_index: current.action_index + 1, step_index: 0 }
  }
  if (b.outcome === 'target_missing') return { ...next, status: 'target_missing' }
  if (current.status !== 'target_missing') return current
  const resume = typeof b.resume_step_index === 'number' ? { step_index: b.resume_step_index } : {}
  return { ...next, status: 'active', ...resume }
}

describe('leaving a step and coming back', () => {
  const outcomes = () => writes('/api/guide/progress').map(c => c.body.outcome)

  async function startTracked(extra: ReactNode, over: Partial<Guide> = {}, at = '/members') {
    pending = [guide(over)]
    let current = claimed(guide(over))
    onWrite = (name, b) => {
      if (name === 'progress') current = gatewayProgress(current, b)
      if (name === 'cancel') current = { ...current, status: 'cancelled', owner_tab: null, revision: current.revision + 1 }
      return current
    }
    renderGuide('/chat/slot-A', extra)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain(at))
    return () => current
  }

  it('only a navigation asked about in the same step tells the page its leave was granted', async () => {
    await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><NavTo to="/settings/chat" /></>)
    // Start awaited its claim after asking: a draft typed meanwhile was never asked about.
    expect(screen.getByTestId('loc').dataset.granted).toBe('0')
    await screen.findByTestId('guide-arrow')
    fireEvent.click(screen.getByTestId('nav-away'))
    fireEvent.click(await screen.findByTestId('guide-go-back'))
    // Go back asks and navigates in one step: the page need not ask again.
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/members?create=1'))
    expect(screen.getByTestId('loc').dataset.granted).toBe('1')
  })

  it('says the user left the step at once, and Go back returns to the step page and resumes it', async () => {
    await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><NavTo to="/settings/chat" /></>)
    await screen.findByTestId('guide-arrow')
    fireEvent.click(screen.getByTestId('nav-away'))
    expect(await screen.findByText(L('left_step'))).toBeTruthy()
    // Not waiting out the missing bound, and not pointing at anything meanwhile.
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    await tickTracker(2)
    expect(outcomes()).toEqual([])
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    fireEvent.click(screen.getByTestId('guide-go-back'))
    // The action's own enter plan: the Crewmates hand-off with the draft.
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/members?create=1&name=radar&goal=watch+the+build'))
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
    expect(screen.queryByText(L('left_step'))).toBeNull()
    expect(outcomes()).toEqual([])
  })

  it('a missing target that shows again resumes the same step by itself, once each time', async () => {
    freezeClock()
    const now = await startTracked(<><ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} /><NavTo to="/settings/chat" /></>)
    await tickTracker(2)
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => T0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(outcomes()).toEqual(['target_missing']))
    expect(await screen.findByText(L('target_missing'))).toBeTruthy()
    expect(screen.getByTestId('guide-go-back')).toBeTruthy()
    // Off the page, Go back still leads back (the target was never found
    // there, so this is still "missing", not "left").
    fireEvent.click(screen.getByTestId('nav-away'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/settings/chat'))
    fireEvent.click(screen.getByTestId('guide-go-back'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/members?create=1'))
    clock.mockReturnValue(T0)
    fireEvent.click(screen.getByTestId('toggle-target'))
    await waitFor(() => expect(outcomes()).toEqual(['target_missing', 'target_found']))
    const found = writes('/api/guide/progress')[1].body
    expect(found).toMatchObject({ step_index: 0, revision: now().revision - 1 })
    expect(now()).toMatchObject({ status: 'active', step_index: 0 })
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
    expect(screen.getByTestId('guide-step-text').textContent).toBe(i18nT('components.guideLayer.step_crewmate_create'))
    await tickTracker(2)
    expect(outcomes()).toEqual(['target_missing', 'target_found'])
    // Gone again: reported again, at its new revision, not swallowed as sent.
    fireEvent.click(screen.getByTestId('toggle-target'))
    await tickTracker(2)
    vi.spyOn(Date, 'now').mockImplementation(() => T0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(outcomes()).toEqual(['target_missing', 'target_found', 'target_missing']))
    // This time the step had been on screen, so going elsewhere reads as leaving it.
    fireEvent.click(screen.getByTestId('nav-away'))
    expect(await screen.findByText(L('left_step'))).toBeTruthy()
    expect(screen.getByTestId('guide-go-back')).toBeTruthy()
  })

  it('a guide that ended never resumes when its target shows again', async () => {
    freezeClock()
    await startTracked(<ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} />)
    await tickTracker(2)
    vi.spyOn(Date, 'now').mockImplementation(() => T0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(outcomes()).toEqual(['target_missing']))
    fireEvent.click(await screen.findByRole('button', { name: L('cancel_guide') }))
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    await waitFor(() => expect(screen.queryByTestId('guide-go-back')).toBeNull())
    fireEvent.click(screen.getByTestId('toggle-target'))
    await tickTracker(2)
    expect(outcomes()).toEqual(['target_missing'])
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
  })

  it('after a reload, taking over a missing guide walks back to its step and resumes it there', async () => {
    // A reload is a new tab id: the guide is still the old tab's, still missing.
    pending = [guide({ status: 'target_missing', owner_tab: 'old-tab', revision: 6 })]
    let current: Guide = pending[0]
    onWrite = (name, b) => {
      if (name === 'claim') current = { ...current, owner_tab: TAB_ID, revision: current.revision + 1 }
      if (name === 'progress') current = gatewayProgress(current, b)
      return current
    }
    renderGuide('/chat/slot-A', <ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} />)
    fireEvent.click(await screen.findByTestId('guide-take-over'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/members?create=1&name=radar&goal=watch+the+build'))
    expect(writes('/api/guide/progress')).toHaveLength(0)
    fireEvent.click(screen.getByTestId('toggle-target'))
    await waitFor(() => expect(outcomes()).toEqual(['target_found']))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ revision: 7, tab_id: TAB_ID })
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
  })

  it('a recovery report that fails is offered again and still resumes the step', async () => {
    freezeClock()
    pending = [guide({ status: 'target_missing', owner_tab: 'old-tab', revision: 6 })]
    let current: Guide = pending[0]
    onWrite = (name, b) => {
      if (name === 'claim') current = { ...current, owner_tab: TAB_ID, revision: current.revision + 1 }
      if (name === 'progress') current = gatewayProgress(current, b)
      return current
    }
    let failNext = true
    server.use(
      http.post('/api/guide/progress', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/progress', body, headers: request.headers })
        if (failNext) {
          failNext = false
          return HttpResponse.json({ error: 'flaky' }, { status: 503 })
        }
        return HttpResponse.json({ guide: onWrite('progress', body) })
      }),
    )
    renderGuide('/chat/slot-A', <ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} />)
    fireEvent.click(await screen.findByTestId('guide-take-over'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/members?create=1'))
    fireEvent.click(screen.getByTestId('toggle-target'))
    await waitFor(() => expect(outcomes()).toEqual(['target_found']))
    expect(current.status).toBe('target_missing')
    vi.spyOn(Date, 'now').mockImplementation(() => T0 + GUIDE_FOUND_RETRY_MS + 500)
    await waitFor(() => expect(outcomes()).toEqual(['target_found', 'target_found']))
    expect(current).toMatchObject({ status: 'active', step_index: 0 })
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
    // The failed attempt's error does not outlive the report that landed.
    await waitFor(() => expect(screen.queryByText('flaky')).toBeNull())
  })

  it('a report slower than the retry spacing still ends in a stopped panel, never a silent tracker', async () => {
    freezeClock()
    await startTracked(<><ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} /></>)
    const release: Array<() => void> = []
    server.use(
      http.post('/api/guide/progress', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/progress', body, headers: request.headers })
        await new Promise<void>(r => release.push(r))
        return HttpResponse.json({ error: 'flaky' }, { status: 503 })
      }),
    )
    await tickTracker(2)
    let t = T0 + GUIDE_TARGET_WAIT_MS + 1000
    vi.spyOn(Date, 'now').mockImplementation(() => t)
    await waitFor(() => expect(outcomes()).toEqual(['target_missing']))
    // Many retry windows pass while the first report is still out: none of
    // those offers was sent, so none may use up an attempt.
    for (let i = 0; i < GUIDE_FOUND_MAX_ATTEMPTS + 2; i++) {
      t += GUIDE_FOUND_RETRY_MS + 100
      await tickTracker(2)
    }
    expect(outcomes()).toHaveLength(1)
    for (let i = 1; i < GUIDE_FOUND_MAX_ATTEMPTS; i++) {
      release.splice(0).forEach(r => r())
      t += GUIDE_FOUND_RETRY_MS + 100
      await waitFor(() => expect(outcomes()).toHaveLength(i + 1))
    }
    release.splice(0).forEach(r => r())
    // Every real attempt failed: the panel says it stopped and offers Go back.
    expect(await screen.findByText(L('target_missing'))).toBeTruthy()
    expect(screen.getByTestId('guide-go-back')).toBeTruthy()
    expect(screen.queryByText(L('looking_for_control'))).toBeNull()
    expect(screen.getByText('flaky')).toBeTruthy()
    // Going back restarts the attempt: the stopped reports' error goes with it.
    fireEvent.click(screen.getByTestId('guide-go-back'))
    await waitFor(() => expect(screen.queryByText('flaky')).toBeNull())
  }, 15_000)

  it('a failed target_missing report is offered again, and Go back re-arms a tracker that gave up', async () => {
    freezeClock()
    const now = await startTracked(<><ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} /></>)
    let failing = true
    server.use(
      http.post('/api/guide/progress', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/progress', body, headers: request.headers })
        if (failing) return HttpResponse.json({ error: 'flaky' }, { status: 503 })
        return HttpResponse.json({ guide: onWrite('progress', body) })
      }),
    )
    await tickTracker(2)
    let t = T0 + GUIDE_TARGET_WAIT_MS + 1000
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => t)
    await waitFor(() => expect(outcomes()).toEqual(['target_missing']))
    // One failure is not "stopped": the tracker is still retrying.
    await tickTracker(1)
    expect(screen.queryByText(L('target_missing'))).toBeNull()
    expect(screen.queryByTestId('guide-go-back')).toBeNull()
    // Every retry fails until the tracker gives up.
    for (let i = 1; i < GUIDE_FOUND_MAX_ATTEMPTS; i++) {
      t += GUIDE_FOUND_RETRY_MS + 100
      await waitFor(() => expect(outcomes()).toHaveLength(i + 1))
    }
    t += GUIDE_FOUND_RETRY_MS * 3
    await tickTracker(3)
    expect(outcomes()).toHaveLength(GUIDE_FOUND_MAX_ATTEMPTS)
    // It has stopped: the panel says so instead of spinning on "Looking for".
    expect(screen.queryByText(L('looking_for_control'))).toBeNull()
    expect(screen.getByText(L('target_missing'))).toBeTruthy()
    // The network is back; pressing Go back on the same page reports again.
    failing = false
    fireEvent.click(await screen.findByTestId('guide-go-back'))
    await tickTracker(2)
    t += GUIDE_TARGET_WAIT_MS + 1000
    await waitFor(() => expect(outcomes()).toHaveLength(GUIDE_FOUND_MAX_ATTEMPTS + 1))
    await waitFor(() => expect(now().status).toBe('target_missing'))
    clock.mockRestore()
  })

  it('a form that restarts on return takes the guide back to the step it shows', async () => {
    freezeClock()
    // A form that keeps its step in local state: leaving its page unmounts
    // it, and coming back starts it over at the first step.
    function Wizard() {
      const [step, setStep] = useState(1)
      return step === 1
        ? <Target anchor={GUIDE_ANCHORS.mcpServersTab} onClick={() => setStep(2)} />
        : <Target anchor={GUIDE_ANCHORS.mcpAddCustom} />
    }
    function RoutedWizard() {
      const loc = useLocation()
      return loc.pathname === '/capabilities' ? <Wizard /> : null
    }
    const now = await startTracked(<><RoutedWizard /><NavTo to="/settings/chat" /></>, { actions: [MCP_OPEN] }, '/capabilities')
    fireEvent.click(await screen.findByTestId(`target-${GUIDE_ANCHORS.mcpServersTab}`))
    await waitFor(() => expect(now()).toMatchObject({ status: 'active', step_index: 1 }))
    fireEvent.click(screen.getByTestId('nav-away'))
    expect(await screen.findByText(L('left_step'))).toBeTruthy()
    fireEvent.click(screen.getByTestId('guide-go-back'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/capabilities'))
    expect(await screen.findByTestId(`target-${GUIDE_ANCHORS.mcpServersTab}`)).toBeTruthy()
    vi.spyOn(Date, 'now').mockImplementation(() => T0 + GUIDE_EARLIER_STEP_WAIT_MS + 500)
    await waitFor(() => expect(outcomes()).toEqual(['observed', 'target_missing', 'target_found']))
    expect(writes('/api/guide/progress')[2].body).toMatchObject({ step_index: 1, resume_step_index: 0 })
    expect(now()).toMatchObject({ status: 'active', step_index: 0 })
    await waitFor(() => expect(screen.getByTestId('guide-step-text').textContent).toBe(i18nT('components.guideLayer.step_mcp_open_tab')))
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
  })

  it('a save the server refused stops the waiting, and the guide stays on the Create step', async () => {
    freezeClock()
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    const now = await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><Probe /></>)
    // On the Create step, the human pressed Create: the step is "submitted".
    act(() => { expect(api?.requestHeadersFor('crewmate.create')).toBeTruthy() })
    expect(api?.submitted).toBe(true)
    // The create came back 409: the card is still there, Create pressable again.
    act(() => api?.noteSaveRefused('crewmate.create'))
    expect(api?.submitted).toBe(false)
    await tickTracker(2)
    expect(outcomes()).toEqual([])
    expect(now()).toMatchObject({ status: 'active', step_index: 0 })
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
  })

  it('a save waits for a report still in flight, and then reads the step the gateway moved to', async () => {
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    let release!: () => void
    const gate = new Promise<void>(r => { release = r })
    await startTracked(<><ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} initial /><Probe /></>)
    // The Create button went missing, and the guide said so. The arrow goes
    // on the same tracker tick that starts the missing clock.
    await screen.findByTestId('guide-arrow')
    fireEvent.click(screen.getByTestId('toggle-target'))
    await waitFor(() => expect(screen.queryByTestId('guide-arrow')).toBeNull())
    const t0 = Date.now()
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => t0 + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(outcomes()).toEqual(['target_missing']))
    clock.mockRestore()
    server.use(
      http.post('/api/guide/progress', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/progress', body, headers: request.headers })
        await gate
        return HttpResponse.json({ guide: onWrite('progress', body) })
      }),
    )
    // The button is back and pressed before the gateway answered the recovery.
    fireEvent.click(screen.getByTestId('toggle-target'))
    await waitFor(() => expect(outcomes()).toEqual(['target_missing', 'target_found']))
    let synced = false
    const done = api!.awaitSync('crewmate.create').then((ok) => { synced = true; expect(ok).toBe(true) })
    await tickTracker(1)
    expect(synced).toBe(false)
    release()
    await done
    // Read after the wait, the headers name the commit step's revision.
    let headers: Record<string, string> | undefined
    act(() => { headers = api?.requestHeadersFor('crewmate.create') })
    expect(headers).toBeTruthy()
  })

  it('an observed report that fails is offered again, so the guide does not stay a step behind', async () => {
    freezeClock()
    let failNext = true
    const now = await startTracked(<><Target anchor={GUIDE_ANCHORS.mcpServersTab} /></>, { actions: [MCP_OPEN] }, '/capabilities')
    server.use(
      http.post('/api/guide/progress', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/progress', body, headers: request.headers })
        if (failNext) { failNext = false; return HttpResponse.json({ error: 'flaky' }, { status: 503 }) }
        return HttpResponse.json({ guide: onWrite('progress', body) })
      }),
    )
    // The page moved on: the next step's control is on screen.
    render(<Target anchor={GUIDE_ANCHORS.mcpAddCustom} rect={{ top: 500, left: 100, width: 80, height: 30 }} />)
    await waitFor(() => expect(outcomes()).toEqual(['observed']))
    vi.spyOn(Date, 'now').mockImplementation(() => T0 + GUIDE_FOUND_RETRY_MS + 500)
    await waitFor(() => expect(outcomes()).toEqual(['observed', 'observed']))
    expect(now()).toMatchObject({ status: 'active', step_index: 1 })
  })

  it('re-measures the page while the target stays put, so a control that slides in is avoided', async () => {
    // jsdom lays nothing out, so the panel measures 0 tall; the arrow has a
    // fixed size, which makes it the observable for a re-measured neighbour.
    const where = { rect: { top: 2000, left: 0, width: 10, height: 10 } }
    function Sliding() {
      return (
        <button
          type="button"
          ref={(el) => {
            if (!el) return
            el.getBoundingClientRect = () => {
              const r = where.rect
              return { ...r, right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top, toJSON: () => ({}) } as DOMRect
            }
          }}
        >
          neighbour
        </button>
      )
    }
    await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><Sliding /></>)
    const arrow = await screen.findByTestId('guide-arrow')
    const at = (el: HTMLElement) => ({ top: parseFloat(el.style.top), left: parseFloat(el.style.left) })
    const before = at(arrow)
    // A control now sits exactly where the arrow rests.
    where.rect = { top: before.top, left: before.left, width: 24, height: 24 }
    await waitFor(() => expect(at(screen.getByTestId('guide-arrow'))).not.toEqual(before), { timeout: GUIDE_RELAYOUT_MS * 4 })
  })

  it('a save the guide never caught up with ends the guide saying the change was saved', async () => {
    freezeClock()
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    let current: Guide | null = null
    const now = await startTracked(<><ToggleTarget anchor={GUIDE_ANCHORS.crewmateCreate} initial /><Probe /></>)
    // The Create button is missing, so the guide is not on a step a save could
    // credit. The arrow goes on the same tracker tick that starts the missing clock.
    await screen.findByTestId('guide-arrow')
    fireEvent.click(screen.getByTestId('toggle-target'))
    await waitFor(() => expect(screen.queryByTestId('guide-arrow')).toBeNull())
    const tMissing = Date.now()
    const missingClock = vi.spyOn(Date, 'now').mockImplementation(() => tMissing + GUIDE_TARGET_WAIT_MS + 1000)
    await waitFor(() => expect(outcomes()).toEqual(['target_missing']))
    missingClock.mockRestore()
    const prev = onWrite
    onWrite = (name, b) => {
      if (name === 'cancel') { current = { ...now(), status: 'cancelled', reason: b.reason as string, owner_tab: null, finished_at: 10, revision: now().revision + 1 }; return current }
      return prev(name, b)
    }
    // Still missing: the wait for the Create step runs out.
    let ok: boolean | undefined
    const wait = api!.awaitSync('crewmate.create').then(v => { ok = v })
    vi.spyOn(Date, 'now').mockImplementation(() => T0)
    await tickTracker(1)
    expect(ok).toBeUndefined()
    await wait
    expect(ok).toBe(false)
    act(() => api?.noteSavedWithoutGuide('crewmate.create'))
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    expect(writes('/api/guide/cancel')[0].body).toMatchObject({ reason: 'saved_without_guide' })
    expect((await screen.findAllByText(L('finished_saved_without_guide'))).length).toBeGreaterThan(0)
    expect(screen.queryByText(L('finished_cancelled'))).toBeNull()
  }, 10_000)

  it('Cancel locks the guide at once: no progress meanwhile, and a 409 retries the cancel instead of resuming', async () => {
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    const now = await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><Probe /></>)
    await screen.findByTestId('guide-arrow')
    // Another tab (or the agent) moved the guide on before this press landed.
    const server_ = { revision: now().revision + 1 }
    pending = [{ ...now(), revision: server_.revision }]
    let release: () => void = () => {}
    const held = new Promise<void>(r => { release = r })
    server.use(
      http.post('/api/guide/cancel', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/cancel', body, headers: request.headers })
        await held
        if (body.revision !== server_.revision) return HttpResponse.json({ error: 'stale', code: 'stale_revision' }, { status: 409 })
        return HttpResponse.json({ guide: { ...pending[0], status: 'cancelled', owner_tab: null, finished_at: 10, revision: server_.revision + 1 } })
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: L('cancel_guide') }))
    // Synchronously locked: an auto-progress (a press, a reach) sends nothing.
    let sent = true
    act(() => { sent = api!.report('observed') })
    expect(sent).toBe(false)
    fireEvent.click(screen.getByTestId('target-' + GUIDE_ANCHORS.crewmateCreate))
    release()
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(2), { timeout: 3000 })
    expect(writes('/api/guide/cancel').map(c => c.body.revision)).toEqual([server_.revision - 1, server_.revision])
    expect(writes('/api/guide/progress')).toHaveLength(0)
    expect((await screen.findAllByText(L('finished_cancelled'))).length).toBeGreaterThan(0)
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
  })

  it('Cancel waits for a report already in flight, then cancels at the revision it left', async () => {
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    const now = await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><Probe /></>)
    await screen.findByTestId('guide-arrow')
    let release: () => void = () => {}
    const held = new Promise<void>(r => { release = r })
    let current = now()
    server.use(
      http.post('/api/guide/progress', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/progress', body, headers: request.headers })
        await held
        current = gatewayProgress(current, body)
        return HttpResponse.json({ guide: current })
      }),
      http.post('/api/guide/cancel', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/cancel', body, headers: request.headers })
        if (body.revision !== current.revision) return HttpResponse.json({ error: 'stale', code: 'stale_revision' }, { status: 409 })
        current = { ...current, status: 'cancelled', owner_tab: null, finished_at: 10, revision: current.revision + 1 }
        return HttpResponse.json({ guide: current })
      }),
    )
    act(() => { api!.report('observed') })
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    fireEvent.click(screen.getByRole('button', { name: L('cancel_guide') }))
    await tickTracker(1)
    // Not sent while the report is out: it would race it.
    expect(writes('/api/guide/cancel')).toHaveLength(0)
    release()
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    expect(writes('/api/guide/cancel')[0].body.revision).toBe(now().revision + 1)
    expect((await screen.findAllByText(L('finished_cancelled'))).length).toBeGreaterThan(0)
  })

  it('closing a guide its save went past retries at the re-read revision after a 409', async () => {
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    const now = await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><Probe /></>)
    // The gateway moved on (a late progress report) before the browser heard.
    const server_ = { revision: now().revision + 1 }
    pending = [{ ...now(), revision: server_.revision }]
    server.use(
      http.post('/api/guide/cancel', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/cancel', body, headers: request.headers })
        if (body.revision !== server_.revision) return HttpResponse.json({ error: 'stale', code: 'stale_revision' }, { status: 409 })
        return HttpResponse.json({ guide: { ...pending[0], status: 'cancelled', reason: body.reason, owner_tab: null, finished_at: 10, revision: server_.revision + 1 } })
      }),
    )
    act(() => api?.noteSavedWithoutGuide('crewmate.create'))
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(2), { timeout: 3000 })
    expect(writes('/api/guide/cancel').map(c => c.body.revision)).toEqual([server_.revision - 1, server_.revision])
    expect((await screen.findAllByText(L('finished_saved_without_guide'))).length).toBeGreaterThan(0)
  })

  it('closing after a 409 waits for a slow re-read, then closes the same guide at its new revision', async () => {
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    const now = await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><Probe /></>)
    const server_ = { revision: now().revision + 1 }
    pending = [{ ...now(), revision: server_.revision }]
    let releaseRead: (() => void) | undefined
    server.use(
      // The re-read stays parked until the test releases it.
      http.get('/api/guide/pending', async () => {
        await new Promise<void>(resolve => { releaseRead = resolve })
        return HttpResponse.json({ guides: pending })
      }),
      http.post('/api/guide/cancel', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/cancel', body, headers: request.headers })
        if (body.revision !== server_.revision) return HttpResponse.json({ error: 'stale', code: 'stale_revision' }, { status: 409 })
        return HttpResponse.json({ guide: { ...pending[0], status: 'cancelled', reason: body.reason, owner_tab: null, finished_at: 10, revision: server_.revision + 1 } })
      }),
    )
    act(() => api?.noteSavedWithoutGuide('crewmate.create'))
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    await waitFor(() => expect(releaseRead).toBeTypeOf('function'))
    await tickTracker(6)
    // No second attempt spent on the stale revision while the read is out.
    expect(writes('/api/guide/cancel')).toHaveLength(1)
    releaseRead!()
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(2), { timeout: 4000 })
    expect(writes('/api/guide/cancel').map(c => c.body.revision)).toEqual([server_.revision - 1, server_.revision])
    expect((await screen.findAllByText(L('finished_saved_without_guide'), {}, { timeout: 4000 })).length).toBeGreaterThan(0)
  }, 10_000)

  it('a recorded row keeps the saved-without-guide wording once the live guide is gone', async () => {
    pending = []
    render(
      <Provider store={store}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter initialEntries={['/chat/slot-A']}>
            <GuideProvider>
              <GuideOfferCard guideId="gone" slotKey="slot-A" recordedStatus="cancelled" recordedReason="saved_without_guide" />
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    expect(await screen.findByText(L('finished_saved_without_guide'))).toBeTruthy()
  })

  it('awaitSync answers false at once when this tab has no guide on that action', async () => {
    let api: ReturnType<typeof useGuide> = null
    function Probe() { api = useGuide(); return null }
    renderGuide('/chat/slot-A', <Probe />)
    await waitFor(() => expect(api).toBeTruthy())
    await expect(api!.awaitSync('crewmate.create')).resolves.toBe(false)
  })

  it('a step page that redirects on arrival is never read as the user leaving it', async () => {
    // The plan leads to /members; this stand-in page canonicalises it away at
    // once, so the tab never stood on the plan's path and cannot have left it.
    function Redirect() {
      const loc = useLocation()
      const navigate = useNavigate()
      useEffect(() => { if (loc.pathname === '/members') navigate('/crew', { replace: true }) }, [loc.pathname, navigate])
      return null
    }
    pending = [guide()]
    onWrite = () => claimed(guide())
    renderGuide('/chat/slot-A', <Redirect />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/crew'))
    expect(await screen.findByText(L('looking_for_control'))).toBeTruthy()
    expect(screen.queryByText(L('left_step'))).toBeNull()
  })

  it('never sends a recovery for a guide that is not missing', async () => {
    let report: ((o: 'target_found') => void) | undefined
    function Probe() {
      report = useGuide()?.report
      return null
    }
    await startTracked(<><Target anchor={GUIDE_ANCHORS.crewmateCreate} /><Probe /></>)
    await screen.findByTestId('guide-arrow')
    act(() => report?.('target_found'))
    await tickTracker(1)
    expect(outcomes()).toEqual([])
  })
})


describe('the acknowledge button on the last step', () => {
  /** The view GuideContext derives for *g* at its current indices. */
  const viewOf = (g: Guide) => {
    const resolved = resolveGuideActions(g.actions)
    const action = resolved.ok ? resolved.actions[g.action_index] ?? null : null
    const step = action ? action.steps[g.step_index] ?? null : null
    return { guide: g, resolved, ownedHere: true, needsEnter: false, action, step }
  }
  const show = { id: 'settings.show', params: { setting_id: 'chat.default-model' } }
  const mcp = { id: 'mcp.open_add', params: {} }

  it('a single-setting guide ends on its only step', () => {
    expect(isFinalGuideStep(viewOf(guide({ actions: [show] })))).toBe(true)
  })

  it('a step that another action follows is not the last', () => {
    expect(isFinalGuideStep(viewOf(guide({ actions: [show, mcp], action_index: 0 })))).toBe(false)
    expect(isFinalGuideStep(viewOf(guide({ actions: [mcp, show], action_index: 1 })))).toBe(true)
  })

  it('an earlier step of a multi-step action is not the last; its final step is', () => {
    expect(isFinalGuideStep(viewOf(guide({ actions: [mcp], step_index: 0 })))).toBe(false)
    expect(isFinalGuideStep(viewOf(guide({ actions: [mcp], step_index: 1 })))).toBe(false)
    expect(isFinalGuideStep(viewOf(guide({ actions: [mcp], step_index: 2 })))).toBe(true)
  })

  it('a refused guide has no last step', () => {
    expect(isFinalGuideStep(viewOf(guide({ actions: [{ id: 'run.js', params: {} }] })))).toBe(false)
  })

  it('the settings step asks for the change and never claims the guide makes it', () => {
    const text = L('step_settings_show')
    expect(text).not.toMatch(/\bNext\b|found it/)
    expect(text).toMatch(/doesn.t change it for you/)
  })
})

describe('owner tab and revision', () => {
  it('a second tab cannot advance; explicit takeover claims the current revision', async () => {
    pending = [guide({ status: 'active', owner_tab: 'other-tab', revision: 5 })]
    onWrite = () => claimed(guide({ revision: 5 }))
    renderGuide('/chat/slot-A', <Target anchor={GUIDE_ANCHORS.crewmateCreate} />)
    expect(await screen.findByText(L('other_tab'))).toBeTruthy()
    await tickTracker(2)
    expect(writes('/api/guide/progress')).toHaveLength(0)
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    fireEvent.click(screen.getByTestId('guide-take-over'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(writes('/api/guide/claim')[0].body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 5, take_over: true })
  })

  it('the old tab stops once another tab takes the guide over', async () => {
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.crewmateCreate} />)
    await screen.findByTestId('guide-arrow')
    act(() => applyGuideUpdate(qc, { ...claimed(guide()), owner_tab: 'other-tab', revision: 9 }))
    await waitFor(() => expect(screen.queryByTestId('guide-arrow')).toBeNull())
    const before = calls.length
    await tickTracker(2)
    expect(calls.slice(before).filter(c => c.path === '/api/guide/progress')).toHaveLength(0)
  })

  it('ignores an update older than the revision it holds', async () => {
    pending = [guide({ revision: 4 })]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-start')
    act(() => applyGuideUpdate(qc, guide({ revision: 3, status: 'cancelled' })))
    expect(screen.getByTestId('guide-start')).toBeTruthy()
  })
})

/** The native add form carries no box in happy-dom; give it one. */
function layOutCustomForm() {
  const form = document.querySelector<HTMLElement>('[data-guide-anchor="mcp.custom-form"]')
  if (!form) return null
  form.getBoundingClientRect = () => ({ top: 100, left: 100, width: 400, height: 300, right: 500, bottom: 400, x: 100, y: 100, toJSON: () => ({}) }) as DOMRect
  return form
}

describe('MCP: navigate to the native add form, never install', () => {
  const mcpGuide = (over: Partial<Guide> = {}) => guide({ actions: [{ id: 'mcp.open_add', params: {} }], ...over })

  it('resolves only with empty params and navigates to the existing MCP page', () => {
    const r = resolveGuideAction({ id: 'mcp.open_add', params: {} })
    if (!r.ok) throw new Error('expected ok')
    expect(r.action.enter.to({ pathname: '/chat/slot-A', search: '' })).toBe('/capabilities?tab=mcp')
    // Into the form the button opens: the guide ends on its JSON box (where
    // the env variables go), and only the person's Done ends it there.
    expect(r.action.steps.map(st => st.complete.kind)).toEqual(['reach', 'reach', 'ack'])
    expect(r.action.steps[2].target).toEqual({ kind: 'anchor', anchor: 'mcp.custom-json' })
    expect(r.action.steps.some(st => st.complete.kind === 'committed')).toBe(false)
    for (const params of [{ name: 'files' }, { spec: { command: 'x' } }, { name: 'files', spec: { command: 'x' } }]) {
      expect(resolveGuideAction({ id: 'mcp.open_add', params })).toEqual({ ok: false, reason: 'invalid_params' })
    }
    expect(resolveGuideAction({ id: 'mcp.add_review', params: { name: 'files', spec: { command: 'x' } } })).toEqual({ ok: false, reason: 'unknown_action' })
  })

  it('walks Services -> MCP Servers tab -> Add Custom -> empty native form, with zero MCP writes', async () => {
    pending = [mcpGuide()]
    let current = claimed(mcpGuide())
    onWrite = (name, b) => {
      if (name === 'progress') {
        const last = (b.step_index as number) >= 2
        current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
      }
      return current
    }
    renderGuide('/chat/slot-A', <NativeMcpHost />)
    await screen.findByTestId('guide-start')
    expect(document.querySelector('[data-guide-anchor="mcp.custom-form"]')).toBeNull()
    fireEvent.click(screen.getByTestId('guide-start'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/capabilities?tab=mcp'))

    // Step 1: the arrow is on the existing MCP Servers tab; nothing auto-opens.
    expect(await screen.findByText(L('step_mcp_open_tab'))).toBeTruthy()
    await tickTracker(2)
    expect(writes('/api/guide/progress')).toHaveLength(0)
    expect(document.querySelector('[data-guide-anchor="mcp.custom-form"]')).toBeNull()
    fireEvent.click(screen.getByTestId('native-mcp-tab'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })

    // Step 2: the arrow is on the existing Add Custom button; the human opens it.
    expect(await screen.findByText(L('step_mcp_add_custom'))).toBeTruthy()
    expect(document.querySelector('[data-guide-anchor="mcp.custom-form"]')).toBeNull()
    fireEvent.click(screen.getByTestId('native-add-custom'))
    await waitFor(() => expect(layOutCustomForm()).not.toBeNull())
    // The native form is exactly as an unguided open leaves it: nothing pre-filled.
    const editor = screen.getByRole('textbox', { name: i18nT('components.mcpCustomServerModal.servers_json') }) as HTMLTextAreaElement
    expect(editor.value).toBe('')
    editor.getBoundingClientRect = () => ({ top: 160, left: 110, width: 380, height: 200, right: 490, bottom: 360, x: 110, y: 160, toJSON: () => ({}) }) as DOMRect
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })

    // Step 3: the arrow is on the JSON box inside the form; Done ends the guide.
    expect(await screen.findByText(L('step_mcp_custom_json'))).toBeTruthy()
    await tickTracker(2)
    expect(writes('/api/guide/progress')).toHaveLength(2)
    fireEvent.click(screen.getByTestId('guide-next'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(3))
    expect(writes('/api/guide/progress')[2].body).toMatchObject({ step_index: 2, outcome: 'observed' })
    expect(screen.queryByLabelText(i18nT('components.mcpCustomServerModal.server_name_2'))).toBeNull()
    // Completion says the guide ended, never that a server was added.
    expect((await screen.findByTestId('guide-result')).textContent).toContain(L('finished_completed'))
    expect(screen.queryByTestId('guide-saved-server')).toBeNull()
    expect(writes('/api/mcp/custom')).toHaveLength(0)
    for (const c of calls) expect(c.headers.get('X-Guide-Id')).toBeNull()

    // The gateway keeps serving the ended guide (its chat's result line), so a
    // focus refetch must not take the line away while the user is reading it.
    pending = (qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY) ?? []).filter(g => g.status === 'completed')
    expect(pending).toHaveLength(1)
    await act(async () => { await qc.invalidateQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }) })
    // Past any exit animation: the line must still be there, not fading out.
    await tickTracker(3)
    expect(screen.getByTestId('guide-result').textContent).toContain(L('finished_completed'))
  })

  it('skips the tab step when the MCP Servers view is already showing', async () => {
    pending = [mcpGuide()]
    onWrite = () => claimed(mcpGuide())
    renderGuide('/chat/slot-A', <NativeMcpHost startOn="mcp-servers" />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })
    expect(writes('/api/mcp/custom')).toHaveLength(0)
  })

  it('the native pages carry the registered anchors and no guide prefill', () => {
    const src = (rel: string) => readFileSync(path.join(__dirname, rel), 'utf-8')
    const connections = src('../pages/connections/ConnectionsPage.tsx')
    const mcpTab = src('../pages/overview/McpTab.tsx')
    const modal = src('../components/McpCustomServerModal.tsx')
    expect(connections).toContain(`guideAnchor('${GUIDE_ANCHORS.mcpServersTab}')`)
    expect(mcpTab).toContain(`guideAnchor('${GUIDE_ANCHORS.mcpAddCustom}')`)
    expect(modal).toContain(`'${GUIDE_ANCHORS.mcpCustomForm}'`)
    for (const text of [connections, mcpTab, modal]) {
      expect(text).not.toMatch(/guide\/GuideContext|useGuide|X-Guide|draft=/)
    }
  })
})

describe('in-chat offer card', () => {
  it('shows no top banner and no card in another slot\'s chat', async () => {
    pending = [guide()]
    // Split view: slot-A's pane and slot-B's pane are both open, slot-A viewed.
    renderGuide('/chat/slot-A', <div data-testid="pane-A"><GuideOfferCard guideId="g1" slotKey="slot-A" /></div>, 'slot-B')
    const start = await screen.findByTestId('guide-start')
    expect(screen.getByTestId('pane-A').contains(start)).toBe(true)
    expect(screen.getAllByTestId('guide-offer-card')).toHaveLength(1)
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })

  it('waits in slot A\'s chat while the user views slot B', async () => {
    store.dispatch(setActiveSlot('slot-B'))
    pending = [guide()]
    renderGuide('/chat/slot-B')
    const card = await screen.findByTestId('guide-offer-card')
    expect(card.getAttribute('data-guide-status')).toBe('offered')
    expect(card.textContent).toContain(i18nT('components.guideLayer.title_crewmate_create'))
    expect(screen.queryByTestId('guide-offer-hint')).toBeNull()
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })

  it('Start claims this slot\'s guide even when another slot is viewed', async () => {
    store.dispatch(setActiveSlot('slot-B'))
    pending = [guide()]
    onWrite = (name) => (name === 'claim' ? claimed(guide()) : null)
    renderGuide('/chat/slot-B')
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(writes('/api/guide/claim')[0].body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 1 })
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/members'))
  })

  it('Dismiss cancels and the card is REPLACED by one result line', async () => {
    pending = [guide()]
    onWrite = (name) => (name === 'cancel' ? guide({ status: 'cancelled', revision: 2, finished_at: 10 }) : null)
    renderGuide('/chat/slot-A')
    fireEvent.click(await screen.findByTestId('guide-dismiss'))
    await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    expect(writes('/api/guide/claim')).toHaveLength(0)
    const line = await screen.findByTestId('guide-result')
    expect(line.textContent).toContain(L('finished_cancelled'))
    await waitFor(() => expect(document.activeElement).toBe(line))
    // One element in the chat: the offer's shell is gone, not wrapping the line.
    expect(screen.queryByTestId('guide-offer-card')).toBeNull()
    expect(screen.queryByTestId('guide-start')).toBeNull()
    expect(screen.getByTestId('guide-offer-cards').children).toHaveLength(1)
    expect(screen.getByTestId('guide-result-row').getAttribute('data-guide-status')).toBe('cancelled')
  })

  it('a cancelled guide is one muted line, and one a newer offer replaced leaves nothing', () => {
    pending = []
    const done = (reason: string | null) => renderGuide('/chat/slot-A', <GuideOfferCard guideId="gx" slotKey="slot-A" recordedStatus="cancelled" recordedReason={reason} />, null)
    const { unmount } = done('cancelled_by_user')
    const row = screen.getByTestId('guide-result-row')
    expect(row.tagName).toBe('P')
    expect(row.className).not.toContain('border')
    unmount()
    done('superseded')
    expect(screen.queryByTestId('guide-result-row')).toBeNull()
  })

  it('a completed show-me guide offers Show again, which replays and walks it again', async () => {
    const show = [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }]
    pending = [guide({ actions: show, status: 'completed', revision: 4, finished_at: 10 })]
    server.use(http.post('/api/guide/replay', async ({ request }) => {
      calls.push({ path: '/api/guide/replay', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      return HttpResponse.json({ guide: guide({ actions: show, status: 'offered', revision: 5 }) })
    }))
    onWrite = (name) => (name === 'claim' ? guide({ actions: show, status: 'active', revision: 6, owner_tab: TAB_ID }) : null)
    renderGuide('/chat/slot-A')
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(writes('/api/guide/replay')[0].body).toEqual({ guide_id: 'g1', revision: 4 })
    expect(writes('/api/guide/claim')[0].body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 5 })
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toContain('/settings'))
  })

  it('a Show again that fails says so on the result line it was pressed on', async () => {
    const show = [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }]
    pending = [guide({ actions: show, status: 'completed', revision: 4, finished_at: 10 })]
    server.use(http.post('/api/guide/replay', () => HttpResponse.json({ error: 'gateway busy' }, { status: 503 })))
    renderGuide('/chat/slot-A')
    expect(screen.queryByTestId('guide-line-error')?.textContent ?? '').toBe('')
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(screen.getByTestId('guide-line-error').textContent).not.toBe(''))
    expect(screen.getByTestId('guide-replay')).toBeTruthy()
    // No hand-off: navigating to a chat would discard the composer draft beside it.
    expect(within(screen.getByTestId('guide-line-error')).queryByRole('button')).toBeNull()
  })

  it('a guide that saved a change is never offered again', async () => {
    pending = [guide({ status: 'completed', revision: 4, finished_at: 10 })]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-result')
    expect(screen.queryByTestId('guide-replay')).toBeNull()
  })

  it('the result line survives a page reload, and has no close control', async () => {
    // What the gateway serves after the reload: the slot's newest ended guide.
    pending = [guide({ status: 'cancelled', revision: 2, finished_at: 10 })]
    renderGuide('/chat/slot-A')
    const line = await screen.findByTestId('guide-result')
    expect(line.textContent).toContain(L('finished_cancelled'))
    expect(screen.queryByTestId('guide-offer-card')).toBeNull()
    // It is the conversation's record of the offer, so nothing hides it.
    expect(screen.queryByRole('button', { name: L('close') })).toBeNull()
  })

  it('once the gateway forgets the guide, the row draws the status it recorded', async () => {
    pending = []
    render(
      <Provider store={store}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <MemoryRouter initialEntries={['/chat/slot-A']}>
            <GuideProvider>
              <div data-testid="done"><GuideOfferCard guideId="g1" slotKey="slot-A" recordedStatus="completed" /></div>
              <div data-testid="lapsed"><GuideOfferCard guideId="g2" slotKey="slot-A" recordedStatus="offered" /></div>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    const done = await screen.findByText(L('finished_completed'))
    expect(screen.getByTestId('done').contains(done)).toBe(true)
    // An offer the store forgot never finished: it reads as expired, with no Start.
    await waitFor(() => expect(screen.getByTestId('lapsed').textContent).toContain(L('finished_expired')))
    expect(screen.queryByTestId('guide-start')).toBeNull()
  })

  it('a row in another slot\'s chat never draws this slot\'s live offer', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A', <div data-testid="pane-B"><GuideOfferCard guideId="g1" slotKey="slot-B" recordedStatus="offered" /></div>)
    await screen.findByTestId('guide-start')
    expect(screen.getAllByTestId('guide-start')).toHaveLength(1)
    expect(screen.getByTestId('pane-B').querySelector('[data-testid="guide-start"]')).toBeNull()
  })

  it('a read that left before the cancel answered cannot bring the offer back', () => {
    const cancelled = guide({ status: 'cancelled', revision: 2 })
    expect(reconcilePending([cancelled], [guide()])).toEqual([cancelled])
    // A newer server row wins, and a row the server dropped is dropped.
    const newer = guide({ revision: 3 })
    expect(reconcilePending([cancelled], [newer])).toEqual([newer])
    expect(reconcilePending([cancelled], [])).toEqual([])
  })

  it('a slot shows only its newest ended guide, and none while a guide is live', () => {
    const old = guide({ guide_id: 'g0', status: 'cancelled', finished_at: 5 })
    const recent = guide({ guide_id: 'g1', status: 'expired', finished_at: 9 })
    expect(endedBySlot([recent, old], new Set())['slot-A'].guide_id).toBe('g1')
    expect(endedBySlot([recent, old], new Set(['g1']))['slot-A'].guide_id).toBe('g0')
    expect(endedBySlot([old, guide({ guide_id: 'g2' })], new Set())['slot-A']).toBeUndefined()
    expect(endedBySlot([{ ...recent, dismissed: true }], new Set())['slot-A']).toBeUndefined()
  })

  it('a completed crewmate guide leaves the created line in the chat', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-start')
    const done = guide({ status: 'completed', revision: 5, actions: [{ id: 'crewmate.create', params: {}, result: { name: 'radar' } }] })
    act(() => applyGuideUpdate(qc, done))
    expect(await screen.findByText(L('finished_completed'))).toBeTruthy()
    expect(screen.getByTestId('guide-result-crewmate').textContent).toBe(i18nT('components.meetCrewmatesFlow.step4_title', { name: 'radar' }))
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })
})

describe('a surface the guide opened', () => {
  /** Stands in for the Crewmates page's creation flow. */
  function Surface({ pristine, onClose }: { pristine: boolean; onClose: () => void }) {
    const [open, setOpen] = useState(false)
    const mark = useGuideOpenedSurface('crewmate.create', {
      open,
      isPristine: () => pristine,
      close: () => { setOpen(false); onClose() },
    })
    return (
      <>
        <button type="button" data-testid="open-surface" onClick={() => { mark(); setOpen(true) }}>open</button>
        {open && <div data-testid="surface" />}
      </>
    )
  }

  const owned = () => guide({ status: 'active', owner_tab: TAB_ID, revision: 2 })

  it.each([
    ['cancelled', true, true],
    ['expired', true, true],
    ['cancelled', false, false],
    ['completed', true, false],
    // Ended because its save went through: the ready step stays on screen.
    ['saved_without_guide', true, false],
  ] as const)('a %s guide, untouched=%s, closes it: %s', async (status, pristine, closes) => {
    pending = [owned()]
    const onClose = vi.fn()
    renderGuide('/members', <Surface pristine={pristine} onClose={onClose} />, null)
    await waitFor(() => expect(qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY)).toHaveLength(1))
    fireEvent.click(screen.getByTestId('open-surface'))
    expect(screen.getByTestId('surface')).toBeTruthy()
    act(() => applyGuideUpdate(qc, guide(status === 'saved_without_guide'
      ? { status: 'cancelled', reason: 'saved_without_guide', revision: 3 }
      : { status, revision: 3 })))
    if (closes) {
      await waitFor(() => expect(screen.queryByTestId('surface')).toBeNull())
      expect(onClose).toHaveBeenCalledTimes(1)
    } else {
      await tickTracker(1)
      expect(screen.getByTestId('surface')).toBeTruthy()
      expect(onClose).not.toHaveBeenCalled()
    }
  })

  it('never closes a surface the user opened themselves', async () => {
    pending = []
    const onClose = vi.fn()
    renderGuide('/members', <Surface pristine onClose={onClose} />, null)
    fireEvent.click(screen.getByTestId('open-surface'))
    act(() => applyGuideUpdate(qc, guide({ status: 'cancelled', revision: 3 })))
    await tickTracker(1)
    expect(screen.getByTestId('surface')).toBeTruthy()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('the Crewmates page wires it to the New crewmate card and its edited flag', () => {
    const src = readFileSync(path.join(__dirname, '../pages/members/MembersPage.tsx'), 'utf-8')
    expect(src).toMatch(/useGuideOpenedSurface\('crewmate\.create'/)
    expect(src).toMatch(/isPristine: \(\) => !createEditedRef\.current && !createBusyRef\.current/)
    expect(src).toMatch(/markGuideOpenedRef\.current\(\)\n\s+openCreate\(/)
  })
})

describe('the way back once a guide ends', () => {
  const pilotRow = { name: 'pilot', kiro_agent: 'kirocrew', slug: 'pilot', slot_key: 'slot-pilot', running: false }
  const radarRow = { name: 'radar', kiro_agent: 'kirocrew', slug: 'radar', slot_key: 'slot-radar', running: false }

  beforeEach(() => {
    server.use(http.get('/api/members', () => HttpResponse.json({ members: [pilotRow, radarRow] })))
  })

  /** Owns *slot*'s guide in this tab, away from every chat, and lets it end. */
  async function endOwnedGuide(slot: string, how: 'complete' | 'cancel', at = '/settings/display/theme', extra?: ReactNode) {
    const owned = claimed(guide({ slot_key: slot }))
    pending = [owned]
    onWrite = (name) => (name === 'cancel' ? { ...owned, status: 'cancelled', revision: owned.revision + 1, finished_at: 10 } : null)
    renderGuide(at, extra, null)
    await screen.findByTestId('guide-continue')
    if (how === 'cancel') {
      fireEvent.click(screen.getByRole('button', { name: L('cancel_guide') }))
      await waitFor(() => expect(writes('/api/guide/cancel')).toHaveLength(1))
    } else {
      act(() => applyGuideUpdate(qc, { ...owned, status: 'completed', revision: owned.revision + 1, finished_at: 10 }))
    }
  }

  describe('the finish chip does not linger', () => {
    function TwoWays() {
      const navigate = useNavigate()
      return (
        <>
          <button type="button" data-testid="go-a" onClick={() => navigate('/schedule')}>a</button>
          <button type="button" data-testid="go-b" onClick={() => navigate('/artifacts')}>b</button>
        </>
      )
    }
    afterEach(() => vi.useRealTimers())

    it('leaves by itself after a while, without a dismissal write', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      await screen.findByTestId('guide-back')
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS - 1_000) })
      expect(screen.queryByTestId('guide-back')).not.toBeNull()
      act(() => { vi.advanceTimersByTime(1_500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
      expect(writes('/api/guide/dismiss')).toHaveLength(0)
    })

    it('stays while focus is in it, so it never vanishes from under the keyboard', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      const back = await screen.findByTestId('guide-back')
      act(() => back.focus())
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS + 500) })
      expect(screen.queryByTestId('guide-back')).not.toBeNull()
    })

    it('a click that left focus in it does not hold it: Done, then focus on the way back', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      const back = await screen.findByTestId('guide-back')
      fireEvent.pointerDown(back)
      act(() => back.focus())
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS + 500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
      // Focus goes somewhere real, never the body.
      expect(document.activeElement).not.toBe(back)
    })

    it('counts from the end, not from the last time it was shown: hidden and shown again it still leaves on time', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete')
      await screen.findByTestId('guide-back')
      act(() => { vi.advanceTimersByTime(6_000) })
      // The chat it came from comes on screen and goes again (a route change
      // settling): the chip hides and returns.
      act(() => setViewedThreadSlot('slot-pilot'))
      expect(screen.queryByTestId('guide-back')).toBeNull()
      act(() => clearViewedThreadSlot('slot-pilot'))
      await screen.findByTestId('guide-back')
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS - 6_000 + 500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
    })

    it('a route change right after the end does not restart the countdown', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      await endOwnedGuide('slot-pilot', 'complete', '/settings/display/theme', <TwoWays />)
      await screen.findByTestId('guide-back')
      fireEvent.click(screen.getByTestId('go-a'))
      await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/schedule'))
      act(() => { vi.advanceTimersByTime(GUIDE_FINISHED_DISMISS_MS + 500) })
      expect(screen.queryByTestId('guide-pill')).toBeNull()
    })

    it('survives the first move to another page and leaves on the second', async () => {
      await endOwnedGuide('slot-pilot', 'complete', '/settings/display/theme', <TwoWays />)
      await screen.findByTestId('guide-back')
      fireEvent.click(screen.getByTestId('go-a'))
      await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/schedule'))
      expect(screen.queryByTestId('guide-back')).not.toBeNull()
      fireEvent.click(screen.getByTestId('go-b'))
      await waitFor(() => expect(screen.queryByTestId('guide-pill')).toBeNull())
    })
  })

  it.each([['complete', 'finished_completed'], ['cancel', 'finished_cancelled']] as const)(
    '%s from a crewmate\'s chat: the way back lands on that crewmate\'s thread and keeps its result line',
    async (how, finishedKey) => {
      await endOwnedGuide('slot-radar', how)
      const back = await screen.findByTestId('guide-back')
      expect(screen.getByTestId('guide-pill').textContent).toContain(L(finishedKey))
      expect(back.getAttribute('data-guide-return')).toBe('chat')
      expect(back.textContent).toBe(i18nT('components.meetCrewmatesFlow.back_to_chat'))
      fireEvent.click(back)
      await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/members?member=radar'))
      await waitFor(() => expect(screen.queryByTestId('guide-pill')).toBeNull())
      // Going back is not a dismissal: the crewmate's chat still shows the result.
      expect(writes('/api/guide/dismiss')).toHaveLength(0)
      expect(endedBySlot(qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY) ?? [], new Set())['slot-radar']?.status)
        .toBe(how === 'cancel' ? 'cancelled' : 'completed')
    },
  )

  it('a guide from an ordinary chat goes back to that chat', async () => {
    await endOwnedGuide('slot-A', 'cancel')
    fireEvent.click(await screen.findByTestId('guide-back'))
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe('/chat?slot=slot-A'))
  })

  it('shows no way back while the user is already on that crewmate\'s chat', async () => {
    setViewedThreadSlot('slot-pilot')
    await endOwnedGuide('slot-pilot', 'complete', '/members?member=pilot')
    await act(async () => { await tickTracker(1) })
    expect(screen.queryByTestId('guide-back')).toBeNull()
    expect(screen.queryByTestId('guide-pill')).toBeNull()
  })

  it('the step panel BECOMES the finish chip: one element, never unmounted between', async () => {
    const owned = claimed(guide({ slot_key: 'slot-pilot' }))
    pending = [owned]
    renderGuide('/settings/display/theme', undefined, null)
    await screen.findByTestId('guide-continue')
    const panel = screen.getByTestId('guide-pill')
    const seen: Array<Element | null> = []
    const mo = new MutationObserver(() => seen.push(document.querySelector('[data-testid="guide-pill"]')))
    mo.observe(document.body, { childList: true, subtree: true })
    act(() => applyGuideUpdate(qc, { ...owned, status: 'completed', revision: owned.revision + 1, finished_at: 10 }))
    await screen.findByTestId('guide-back')
    mo.disconnect()
    expect(screen.getByTestId('guide-pill')).toBe(panel)
    expect(seen.every(el => el === panel)).toBe(true)
  })

  it('names no destination until the roster says whose chat it was', () => {
    expect(guideReturnFor('slot-cap', undefined, false)).toBeNull()
    expect(guideReturnFor('slot-cap', undefined, true)).toEqual({ kind: 'chat', to: '/chat?slot=slot-cap' })
    expect(guideReturnFor('slot-cap', [{ ...pilotRow, slot_key: 'slot-cap' }], false)).toEqual({ kind: 'chat', to: '/members?member=pilot' })
  })

  it('reads a crewmate thread from its slot key, never the chat page, which does not show it', () => {
    expect(memberSlugOfSlot('member-radar')).toBe('radar')
    expect(memberSlugOfSlot('member-pilot.memory-pilot-v2')).toBe('pilot')
    for (const k of ['member-', 'member-Radar', 'member--x', 'xmember-radar', 'chat-1-2', 'slot-A']) expect(memberSlugOfSlot(k)).toBeNull()
    // Another crewmate's: by exact name when one row carries the slug.
    const oncall = { name: 'Oncall', kiro_agent: 'kirocrew', slug: 'oncall', slot_key: 'member-oncall.memory-oncall-v2', running: false }
    expect(guideReturnFor('member-oncall', [oncall, radarRow], false)).toEqual({ kind: 'chat', to: '/members?member=Oncall' })
    // ...by slug when the roster failed, or two rows share the lossy slug.
    expect(guideReturnFor('member-radar', undefined, true)).toEqual({ kind: 'chat', to: '/members?member=radar' })
    expect(guideReturnFor('member-oncall', [oncall, { ...oncall, name: 'oncall', slot_key: '' }], false)).toEqual({ kind: 'chat', to: '/members?member=oncall' })
    // ...and the roster's answer is awaited.
    expect(guideReturnFor('member-radar', undefined, false)).toBeNull()
    // An ordinary chat slot still goes to the chat page.
    expect(guideReturnFor('chat-1-2', undefined, true)).toEqual({ kind: 'chat', to: '/chat?slot=chat-1-2' })
  })

  it.each([
    ['the roster failed', () => HttpResponse.json({ error: 'boom' }, { status: 500 }), '/members?member=radar'],
    ['the binding moved on', () => HttpResponse.json({ members: [pilotRow, { ...radarRow, name: 'Radar', slot_key: 'member-radar.memory-radar-v2' }] }), '/members?member=Radar'],
  ])('a crewmate\'s guide goes back to that crewmate when %s', async (_why, members, to) => {
    server.use(http.get('/api/members', members))
    await endOwnedGuide('member-radar', 'complete')
    const back = await screen.findByTestId('guide-back')
    expect(back.textContent).toBe(i18nT('components.meetCrewmatesFlow.back_to_chat'))
    fireEvent.click(back)
    await waitFor(() => expect(screen.getByTestId('loc').textContent).toBe(to))
  })

  it('announces the end in a status region that was there before it', async () => {
    const owned = claimed(guide({ slot_key: 'member-pilot' }))
    pending = [owned]
    renderGuide('/settings/display/theme', undefined, null)
    await screen.findByTestId('guide-continue')
    const status = screen.getByTestId('guide-finished-status')
    expect(status.getAttribute('role')).toBe('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toBe('')
    act(() => applyGuideUpdate(qc, { ...owned, status: 'cancelled', revision: owned.revision + 1, finished_at: 10 }))
    await screen.findByTestId('guide-back')
    expect(screen.getByTestId('guide-finished-status')).toBe(status)
    expect(status.textContent).toBe(L('finished_cancelled'))
    fireEvent.click(screen.getByTestId('guide-finished-close'))
    await waitFor(() => expect(status.textContent).toBe(''))
  })

  it('the finish chip\'s close and way back are 44px touch targets', async () => {
    await endOwnedGuide('member-pilot', 'complete')
    for (const el of [await screen.findByTestId('guide-back'), screen.getByTestId('guide-finished-close')]) {
      expect(el.className).toMatch(/\bmin-h-11\b/)
    }
    expect(screen.getByTestId('guide-finished-close').className).toMatch(/\bmin-w-11\b/)
  })

  /** A Settings row the settings.show guide points at, with a laid-out box. */
  function SettingRow() {
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'chat.default-model')!
    return (
      <div
        data-setting-label={i18nT(entry.labelKey!)}
        ref={(el) => {
          if (el) el.getBoundingClientRect = () => ({ top: 300, left: 100, width: 80, height: 30, right: 180, bottom: 330, x: 100, y: 300, toJSON: () => ({}) }) as DOMRect
        }}
      >
        row
      </div>
    )
  }

  it('Done that finishes the guide moves focus to the way back; closing returns it to the page', async () => {
    const g = guide({ slot_key: 'member-pilot', actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }] })
    let current = claimed(g)
    pending = [current]
    onWrite = (name) => {
      if (name === 'progress') current = { ...current, status: 'completed', revision: current.revision + 1, finished_at: 10 }
      return current
    }
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}><GuideLayer /><SettingRow /><LocationProbe /></main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('guide-continue'))
    const next = await screen.findByTestId('guide-next')
    await waitFor(() => expect((next as HTMLButtonElement).disabled).toBe(false))
    // The guide's only step: the button ends it, so it reads Done, not Next.
    expect(next.textContent).toBe(i18nT('components.meetCrewmatesFlow.done'))
    expect(next.textContent).not.toBe(L('next'))
    act(() => next.focus())
    fireEvent.click(next)
    const back = await screen.findByTestId('guide-back')
    await waitFor(() => expect(document.activeElement).toBe(back))
    act(() => screen.getByTestId('guide-finished-close').focus())
    fireEvent.click(screen.getByTestId('guide-finished-close'))
    await waitFor(() => expect(screen.queryByTestId('guide-pill')).toBeNull())
    expect(document.activeElement).toBe(document.getElementById('main-content'))
  })

  it('Done works again on a replayed guide', async () => {
    const g = guide({ slot_key: 'member-pilot', actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }] })
    let current = claimed(g)
    pending = [current]
    onWrite = (name) => {
      if (name === 'progress') current = { ...current, status: 'completed', revision: current.revision + 1, finished_at: 10 }
      if (name === 'claim') current = { ...current, status: 'active', owner_tab: TAB_ID, revision: current.revision + 1 }
      return current
    }
    server.use(http.post('/api/guide/replay', async ({ request }) => {
      calls.push({ path: '/api/guide/replay', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      current = { ...current, status: 'offered', owner_tab: null, revision: current.revision + 1, finished_at: null, step_index: 0, action_index: 0 }
      return HttpResponse.json({ guide: current })
    }))
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}>
                <GuideLayer /><SettingRow /><LocationProbe />
                <GuideOfferCard guideId={g.guide_id} slotKey="member-pilot" />
              </main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('guide-continue'))
    const first = await screen.findByTestId('guide-next')
    await waitFor(() => expect((first as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(first)
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    const again = await screen.findByTestId('guide-next')
    await waitFor(() => expect((again as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(again)
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
  })

  it('a replayed multi-action guide enters each action\'s page again', async () => {
    const actions = [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }, { id: 'mcp.open_add', params: {} }]
    const g = guide({ slot_key: 'member-pilot', actions })
    let current: Guide = { ...claimed(g), action_index: 1, step_index: 0 }
    pending = [current]
    onWrite = (name) => {
      if (name === 'claim') current = { ...current, status: 'active', owner_tab: TAB_ID, revision: current.revision + 1, action_index: 0, step_index: 0 }
      return current
    }
    server.use(http.post('/api/guide/replay', async ({ request }) => {
      calls.push({ path: '/api/guide/replay', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      current = { ...current, status: 'offered', owner_tab: null, revision: current.revision + 1, finished_at: null, action_index: 0, step_index: 0 }
      return HttpResponse.json({ guide: current })
    }))
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}>
                <GuideLayer /><SettingRow /><LocationProbe />
                <GuideOfferCard guideId={g.guide_id} slotKey="member-pilot" />
              </main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    // The first walk entered the second action in this tab.
    fireEvent.click(await screen.findByTestId('guide-continue'))
    await waitFor(() => expect(screen.queryByTestId('guide-continue')).toBeNull())
    current = { ...current, status: 'completed', owner_tab: null, revision: current.revision + 1, finished_at: 10 }
    act(() => { qc.setQueryData(GUIDE_PENDING_QUERY_KEY, [current]) })
    fireEvent.click(await screen.findByTestId('guide-replay'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    // The gateway advances the replay to its second action.
    current = { ...current, action_index: 1, step_index: 0, revision: current.revision + 1 }
    act(() => { qc.setQueryData(GUIDE_PENDING_QUERY_KEY, [current]) })
    expect(await screen.findByTestId('guide-continue')).toBeTruthy()
  })

  it('a settings step another action follows keeps Next', async () => {
    const g = guide({ slot_key: 'member-pilot', actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' } }, { id: 'mcp.open_add', params: {} }] })
    pending = [claimed(g)]
    onWrite = () => null
    render(
      <Provider store={store}>
        <QueryClientProvider client={(qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }))}>
          <MemoryRouter initialEntries={['/overview']}>
            <GuideProvider>
              <main id="main-content" tabIndex={-1}><GuideLayer /><SettingRow /><LocationProbe /></main>
            </GuideProvider>
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('guide-continue'))
    const next = await screen.findByTestId('guide-next')
    expect(next.textContent).toBe(L('next'))
  })

  it('Cancel moves focus into the chip; a guide that ends while the user works elsewhere does not steal focus', async () => {
    await endOwnedGuide('member-pilot', 'cancel')
    // Cancel was clicked without focusing it (a mouse on Safari): focus stays put.
    expect(document.activeElement).toBe(document.body)
    cleanup()

    const owned = claimed(guide({ slot_key: 'member-pilot' }))
    pending = [owned]
    onWrite = (name) => (name === 'cancel' ? { ...owned, status: 'cancelled', revision: owned.revision + 1, finished_at: 10 } : null)
    renderGuide('/settings/display/theme', undefined, null)
    await screen.findByTestId('guide-continue')
    const cancel = screen.getByRole('button', { name: L('cancel_guide') })
    act(() => cancel.focus())
    fireEvent.click(cancel)
    await screen.findByTestId('guide-back')
    // Focus lands in the chip: on the way back, or on the finish line when the
    // way back was not known yet as the guide ended (the roster loads async).
    const chip = document.querySelector('[data-placement="chip"]')
    await waitFor(() => expect(chip?.contains(document.activeElement)).toBe(true))
    expect(document.activeElement).not.toBe(document.body)
    cleanup()

    // Focus was in the panel, then the user pressed on the page (a spot that
    // takes no focus, so the blur names no new target).
    pending = [owned]
    renderGuide('/settings/display/theme', <p data-testid="page-text">text</p>, null)
    const cont = await screen.findByTestId('guide-continue')
    act(() => cont.focus())
    fireEvent.pointerDown(screen.getByTestId('page-text'))
    act(() => cont.blur())
    act(() => applyGuideUpdate(qc, { ...owned, status: 'completed', revision: owned.revision + 1, finished_at: 10 }))
    await screen.findByTestId('guide-back')
    await act(async () => { await tickTracker(1) })
    expect(document.activeElement).toBe(document.body)
  })
})

/** A UI-location control (`data-ui-location`) with a real-looking box. */
function LocTarget({ id, testId, onClick, rect = { top: 200, left: 40, width: 120, height: 28 } }: {
  id: string
  testId?: string
  onClick?: () => void
  rect?: { top: number; left: number; width: number; height: number }
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={testId ?? `loc-${id}`} {...marks({ location: id }, (el) => {
        if (!el) return
        el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top, toJSON: () => ({}) }) as DOMRect
      })}
    >
      {id}
    </button>
  )
}

/** The Sessions sidebar, reduced to the two controls the older-sessions plan names. */
function SessionsHost({ open: startOpen = false, onOlder }: { open?: boolean; onOlder?: () => void }) {
  const [open, setOpen] = useState(startOpen)
  return (
    <>
      <LocTarget id="chat.sessions-sidebar-toggle" testId="sidebar-toggle" rect={{ top: 60, left: 10, width: 28, height: 28 }} onClick={() => setOpen(o => !o)} />
      {open && <LocTarget id="chat.older-sessions" testId="older-sessions" onClick={onOlder} />}
    </>
  )
}

const phone = () => vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({
  matches: q === '(max-width: 767px)', media: q, onchange: null,
  addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
}) as unknown as MediaQueryList)

describe('ui.show: point at an indexed location, never click it', () => {
  // The gateway stores the digest of the index it accepted the guide against.
  const show = (location_id: string): GuideAction => ({ id: 'ui.show', params: { location_id }, build_digest: GUIDE_BUILD_DIGEST })
  const uiGuide = (over: Partial<Guide> = {}) => guide({ actions: [show('chat.older-sessions')], ...over })

  it('resolves only from the generated plan, for the current viewport', () => {
    const desk = resolveGuideAction(show('chat.older-sessions'))
    if (!desk.ok) throw new Error('expected ok')
    expect(desk.action.steps.map(st => st.target)).toEqual([
      { kind: 'location', id: 'chat.sessions-sidebar-toggle' },
      { kind: 'location', id: 'chat.older-sessions' },
    ])
    // The reveal step is done once its scope reports open (or the target
    // shows); the last step is acknowledged.
    expect(desk.action.steps.map(st => st.complete)).toEqual([
      { kind: 'reach', targets: [{ kind: 'location', id: 'chat.older-sessions' }], scope: 'chat.sessions-sidebar' },
      { kind: 'ack' },
    ])
    expect(desk.action.steps.map(st => st.textKey)).toEqual(['components.guideLayer.step_ui_show_open', 'components.guideLayer.step_ui_show_here'])
    expect(desk.action.steps[1].textVars).toEqual({ label: 'Older Sessions' })
    expect(desk.action.steps.some(st => st.complete.kind === 'committed')).toBe(false)

    const spy = phone()
    const mob = resolveGuideAction(show('chat.older-sessions'))
    if (!mob.ok) throw new Error('expected ok')
    expect(mob.action.steps[0].target).toEqual({ kind: 'location', id: 'chat.mobile-sessions-toggle' })
    // A desktop-only control has no placement on a phone.
    expect(resolveGuideAction(show('shell.focus-mode'))).toEqual({ ok: false, reason: 'location_not_on_this_screen' })
    spy.mockRestore()
  })

  /** Gives every element under *root* a box, so the visibility checks see it drawn. */
  const drawAll = (root: ParentNode = document.body) => {
    for (const el of Array.from(root.querySelectorAll<HTMLElement>('*'))) {
      el.getBoundingClientRect = () => ({ top: 10, left: 10, width: 100, height: 20, right: 110, bottom: 30, x: 10, y: 10, toJSON: () => ({}) }) as DOMRect
    }
  }

  it('a tick-the-job step outlines the row checkbox, never the row that opens the job', () => {
    const r = resolveGuideAction({ ...show('schedule.move-to-folder'), params: { location_id: 'schedule.move-to-folder', pick: 'Morning summary' } })
    if (!r.ok) throw new Error(r.reason)
    const tick = r.action.steps.find(st => st.complete.kind === 'select')!
    expect(tick.complete).toMatchObject({ selection: 'one_job_checked' })
    render(
      <table {...marks({ location: "schedule.job-list" })}><tbody>
        <tr data-testid="row" {...marks({ pick: "Morning summary" })}>
          <td><input type="checkbox" aria-label="Select Morning summary" data-testid="tick" {...marks({ pickControl: "one_job_checked" })} /></td>
          <td>Morning summary</td>
        </tr>
        <tr {...marks({ pick: "Evening digest" })}><td><input type="checkbox" aria-label="Select Evening digest" {...marks({ pickControl: "one_job_checked" })} /></td><td>Evening digest</td></tr>
      </tbody></table>,
    )
    drawAll()
    expect(resolveGuideTarget(tick.target)).toBe(screen.getByTestId('tick'))
    // Another selection on the same row has no such control: the row itself.
    const open = resolveGuideAction({ ...show('schedule.delete'), params: { location_id: 'schedule.delete', pick: 'Morning summary' } })
    if (!open.ok) throw new Error(open.reason)
    const choose = open.action.steps.find(st => st.complete.kind === 'select')!
    expect(resolveGuideTarget(choose.target)).toBe(screen.getByTestId('row'))
  })

  it('the outline is never drawn over an open dialog its target sits behind', () => {
    render(
      <>
        <button type="button" data-testid="behind">Row</button>
        <div role="dialog" aria-modal="true"><button type="button" data-testid="inside">Name</button></div>
      </>,
    )
    expect(coveredByModal(screen.getByTestId('behind'))).toBe(true)
    expect(coveredByModal(screen.getByTestId('inside'))).toBe(false)
    cleanup()
    render(<button type="button" data-testid="alone">Row</button>)
    expect(coveredByModal(screen.getByTestId('alone'))).toBe(false)
  })

  it('a confirm opened from inside a dialog covers that dialog\'s own controls', () => {
    render(
      <>
        <div role="dialog" aria-modal="true" data-testid="job"><button type="button" data-testid="delete">Delete</button></div>
        <div role="alertdialog" aria-modal="true" data-testid="confirm"><button type="button" data-testid="cancel">Cancel</button></div>
      </>,
    )
    expect(openModalDialog()).toBe(screen.getByTestId('confirm'))
    expect(coveredByModal(screen.getByTestId('delete'))).toBe(true)
    expect(coveredByModal(screen.getByTestId('cancel'))).toBe(false)
  })

  it('a choose step over a list of one names that item instead of "choose the one you want"', () => {
    const r = resolveGuideAction(show('schedule.delete'))
    if (!r.ok) throw new Error(r.reason)
    const choose = r.action.steps.find(st => st.complete.kind === 'select')!
    render(<table {...marks({ location: "schedule.job-list" })}><tbody><tr {...marks({ pick: "Morning summary" })}><td>Morning summary</td></tr></tbody></table>)
    drawAll()
    expect(onlyPickName(choose)).toBe('Morning summary')
    cleanup()
    render(<table {...marks({ location: "schedule.job-list" })}><tbody><tr {...marks({ pick: "A" })}><td>A</td></tr><tr {...marks({ pick: "B" })}><td>B</td></tr></tbody></table>)
    drawAll()
    expect(onlyPickName(choose)).toBeNull()
  })

  it('a page still drawing placeholders is loading; the guide layer itself never counts', () => {
    render(<><div aria-busy="true" data-testid="sk" /><div data-guide-layer><div aria-busy="true" /></div></>)
    expect(pageStillLoading()).toBe(true)
    screen.getByTestId('sk').setAttribute('aria-busy', 'false')
    expect(pageStillLoading()).toBe(false)
  })

  it('a choose step outlines the item the person named, or the only one, never the add card', () => {
    const named = resolveGuideAction({ ...show('agents.delete'), params: { location_id: 'agents.delete', pick: 'Helper' } })
    if (!named.ok) throw new Error(named.reason)
    const choose = named.action.steps[0]
    expect(choose.target).toMatchObject({ kind: 'location', id: 'agents.crew-list', pickFrom: true, pickName: 'Helper' })
    render(
      <div role="group" {...marks({ location: "agents.crew-list" })}>
        <div role="button" aria-label="Edit Pilot" {...marks({ pick: "Pilot" })}>Pilot</div>
        <div role="button" aria-label="Edit Helper" data-testid="helper" {...marks({ pick: "Helper" })}>Helper</div>
        <div role="button" aria-label="Add crew member">Add crew member</div>
      </div>,
    )
    drawAll()
    expect(resolveGuideTarget(choose.target)).toBe(screen.getByTestId('helper'))
    // No name: two items. The first card is outlined and the other beside it,
    // never the whole grid (its add card and the gaps between cards).
    const plain = resolveGuideAction(show('agents.delete'))
    if (!plain.ok) throw new Error(plain.reason)
    expect(resolveGuideTarget(plain.action.steps[0].target)?.getAttribute('data-guide-pick')).toBe('Pilot')
    expect(pickAlternativeRects(plain.action.steps[0], { width: 1280, height: 800 })).toHaveLength(1)
    expect(pickAlternativeRects(choose, { width: 1280, height: 800 })).toEqual([])
    cleanup()
    // One item: it is outlined directly; the add card is never a choice.
    render(
      <div role="group" {...marks({ location: "agents.crew-list" })}>
        <div role="button" data-testid="only" {...marks({ pick: "Helper" })}>Helper</div>
        <div role="button" aria-label="Add crew member">Add crew member</div>
      </div>,
    )
    drawAll()
    expect(resolveGuideTarget(plain.action.steps[0].target)).toBe(screen.getByTestId('only'))
    // pick is a name, not a free parameter.
    expect(resolveGuideAction({ ...show('agents.delete'), params: { location_id: 'agents.delete', pick: 7 } })).toEqual({ ok: false, reason: 'invalid_params' })
  })

  it('a named pick is matched by the row\'s own entity name, never its text, and never falls back to another row', () => {
    const r = resolveGuideAction({ ...show('agents.delete'), params: { location_id: 'agents.delete', pick: 'Helper' } })
    if (!r.ok) throw new Error(r.reason)
    const choose = r.action.steps[0]
    // Only B is listed: the pick misses, so the whole list is outlined for the person, not B.
    render(
      <div role="group" data-testid="list" {...marks({ location: "agents.crew-list" })}>
        <div role="button" data-testid="ops" {...marks({ pick: "Ops" })}>Ops: asks Helper to file the weekly report</div>
      </div>,
    )
    drawAll()
    expect(resolveGuideTarget(choose.target)).toBe(screen.getByTestId('list'))
    cleanup()
    // Two rows carrying the name: ambiguous, so again the person picks in the whole list.
    render(
      <div role="group" data-testid="list" {...marks({ location: "agents.crew-list" })}>
        <div role="button" {...marks({ pick: "Helper" })}>Helper</div>
        <div role="button" {...marks({ pick: "helper" })}>helper</div>
      </div>,
    )
    drawAll()
    expect(resolveGuideTarget(choose.target)).toBe(screen.getByTestId('list'))
  })

  it('a named pick completes its choose step only once that entity is the one open', () => {
    const r = resolveGuideAction({ ...show('agents.delete'), params: { location_id: 'agents.delete', pick: 'Helper' } })
    if (!r.ok) throw new Error(r.reason)
    const choose = r.action.steps[0]
    if (choose.complete.kind !== 'select') throw new Error('expected select')
    const owner = Symbol('t')
    try {
      // Another crewmate already open: not done, the step keeps pointing so the person picks.
      reportSelection('crewmate_editor_open', owner, true, true, 'Ops')
      expect(factVerdict(choose)).toBe('point')
      expect(pickMade(choose)).toBe(false)
      // Open but unnamed (an owner that cannot say which): still not the pick.
      reportSelection('crewmate_editor_open', owner, true, true)
      expect(factVerdict(choose)).toBe('point')
      reportSelection('crewmate_editor_open', owner, true, true, ' helper ')
      expect(factVerdict(choose)).toBe('done')
      // Without a pick, any open crewmate completes it as before.
      const plain = resolveGuideAction(show('agents.delete'))
      if (!plain.ok) throw new Error(plain.reason)
      reportSelection('crewmate_editor_open', owner, true, true, 'Ops')
      expect(factVerdict(plain.action.steps[0])).toBe('done')
    } finally {
      dropSelection('crewmate_editor_open', owner)
    }
  })

  it('refuses a pick for a list whose rows cannot name their entity', () => {
    const sessionPlan = Object.entries(GUIDE_PLANS).find(([, plan]) => plan.placements.some(p => p.steps.some(st => st.kind === 'select' && st.location === 'sessions.list')))
    if (!sessionPlan) throw new Error('expected a plan choosing a session')
    expect(resolveGuideAction({ ...show(sessionPlan[0]), params: { location_id: sessionPlan[0], pick: 'Morning' } })).toEqual({ ok: false, reason: 'invalid_params' })
  })

  it('moving a job to a folder has the person tick that one job, never Select all', () => {
    const r = resolveGuideAction({ ...show('schedule.move-to-folder'), params: { location_id: 'schedule.move-to-folder', pick: 'Morning summary' } })
    if (!r.ok) throw new Error(r.reason)
    const [tick, move] = r.action.steps
    expect(tick.complete).toMatchObject({ kind: 'select', selection: 'one_job_checked' })
    expect(tick.target).toMatchObject({ kind: 'location', id: 'schedule.job-list', pickName: 'Morning summary' })
    expect(tick.textKey).toBe('components.guideLayer.select_tickjob')
    expect(move.target).toMatchObject({ kind: 'location', id: 'schedule.move-to-folder' })
    expect(r.action.steps.some(st => st.target.kind === 'location' && st.target.id === 'schedule.select-all')).toBe(false)
  })

  it('a choose step is passed when its only item\'s control already shows', () => {
    const r = resolveGuideAction(show('schedule.delete'))
    if (!r.ok) throw new Error(r.reason)
    const choose = r.action.steps.find(st => st.complete.kind === 'select')!
    if (choose.complete.kind !== 'select') throw new Error('expected select')
    expect(choose.complete.then).toEqual({ kind: 'location', id: 'schedule.delete' })
    render(
      <>
        <table {...marks({ location: "schedule.job-list" })}><tbody><tr {...marks({ pick: "" })}><td>Morning summary</td></tr></tbody></table>
        <button {...marks({ location: "schedule.delete" })}>Delete</button>
      </>,
    )
    drawAll()
    expect(factVerdict(choose)).toBe('done')
    cleanup()
    render(
      <>
        <table {...marks({ location: "schedule.job-list" })}><tbody><tr {...marks({ pick: "" })}><td>A</td></tr><tr {...marks({ pick: "" })}><td>B</td></tr></tbody></table>
        <button {...marks({ location: "schedule.delete" })}>Delete</button>
      </>,
    )
    drawAll()
    expect(factVerdict(choose)).toBe('point')
  })

  it('uninstalls an app from its Library card menu, the card picked by name, the removal never finishing by a press', () => {
    const r = resolveGuideAction({ ...show('apps.library.tile-uninstall'), params: { location_id: 'apps.library.tile-uninstall', pick: 'Command Bar' } })
    if (!r.ok) throw new Error(r.reason)
    const [choose, uninstall] = r.action.steps
    expect(choose.target).toEqual({ kind: 'location', id: 'apps.library.app-list', pickFrom: true, pickControl: 'app_tile_menu_open', pickName: 'Command Bar' })
    expect(choose.complete).toMatchObject({ kind: 'select', selection: 'app_tile_menu_open', entity: 'app' })
    expect(uninstall).toMatchObject({ caution: true, complete: { kind: 'ack' }, target: { kind: 'location', id: 'apps.library.tile-uninstall' } })
    if (r.action.enter.kind !== 'navigate') throw new Error('expected navigate')
    expect(r.action.enter.to({ pathname: '/chat', search: '' })).toBe('/apps/library')
    // Details is reached the same way.
    const d = resolveGuideAction({ ...show('apps.library.tile-details'), params: { location_id: 'apps.library.tile-details', pick: 'Command Bar' } })
    if (!d.ok) throw new Error(d.reason)
    expect(d.action.steps.map(st => st.complete.kind)).toEqual(['select', 'ack'])
  })

  it('renaming a chat opens that session, then its own row\'s ⋯, with no pointer on the row', () => {
    const r = resolveGuideAction(show('sessions.row-menu.rename'))
    if (!r.ok) throw new Error(r.reason)
    const kinds = r.action.steps.map(st => (st.target.kind === 'location' ? st.target.id : st.target.kind))
    expect(kinds.slice(-3)).toEqual(['sessions.list', 'sessions.row-menu', 'sessions.row-menu.rename'])
    const menu = r.action.steps.find(st => st.target.kind === 'location' && st.target.id === 'sessions.row-menu')!
    expect(menu.bound?.selection).toBe('session_open')
    function Rows({ open }: { open: 'a' | 'b' | null }) {
      useGuideSelection('session_open', { selected: open !== null, available: true })
      return (
        <ul>
          {(['a', 'b'] as const).map(k => (
            <li key={k} aria-current={open === k ? 'true' : undefined}>
              <button type="button" data-testid={`more-${k}`} {...marks({ location: 'sessions.row-menu' })}>⋯</button>
            </li>
          ))}
        </ul>
      )
    }
    const { rerender } = render(<Rows open={null} />)
    drawAll()
    expect(resolveStepTarget(menu)).toBeNull()
    rerender(<Rows open="b" />)
    expect(resolveStepTarget(menu)).toBe(screen.getByTestId('more-b'))
  })

  it('stopping a goal loop opens its panel only while a loop runs; otherwise the opener is a blocker', () => {
    const r = resolveGuideAction(show('composer.automation.pause'))
    if (!r.ok) throw new Error(r.reason)
    const opener = r.action.steps.find(st => st.target.kind === 'location' && st.target.id === 'composer.automation')!
    expect(opener.requires).toContain('goal_loop_running')
    const last = r.action.steps[r.action.steps.length - 1]
    expect(last.target).toMatchObject({ kind: 'location', id: 'composer.automation.pause' })
    function Composer({ running }: { running: boolean }) {
      useGuidePredicate('goal_loop_running', running)
      useGuideSelection('session_open', { selected: true, available: true, name: 'Release notes' })
      return <button type="button" data-testid="goal" {...marks({ location: 'composer.automation' })}>Set a goal</button>
    }
    const { rerender } = render(<Composer running={false} />)
    drawAll()
    expect(resolveStepTarget(opener)).toBeNull()
    rerender(<Composer running />)
    expect(resolveStepTarget(opener)).toBe(screen.getByTestId('goal'))
    // The monitor's Stop asks to confirm: the press alone never ends the guide.
    const m = resolveGuideAction(show('composer.automation.stop-monitor'))
    if (!m.ok) throw new Error(m.reason)
    expect(m.action.steps[m.action.steps.length - 1].caution).toBe(true)
  })

  it('a card shown under a translated name is found by its manifest name, outlined at its ⋯, and named as shown', () => {
    const r = resolveGuideAction({ ...show('apps.library.tile-uninstall'), params: { location_id: 'apps.library.tile-uninstall', pick: 'Command Bar' } })
    if (!r.ok) throw new Error(r.reason)
    const [choose, uninstall] = r.action.steps
    function Cards({ open }: { open: string | null }) {
      useGuideSelection('app_tile_menu_open', { selected: open !== null, available: true, name: open ?? undefined, alias: open === '命令栏' ? 'Command Bar' : undefined })
      return (
        <div {...marks({ location: 'apps.library.app-list' })}>
          <div {...marks({ pick: '命令栏', pickAlias: 'Command Bar' })}><span>命令栏</span><button type="button" data-testid="more-cb" {...marks({ pickControl: 'app_tile_menu_open' })}>⋯</button></div>
          <div {...marks({ pick: '秘书' })}><span>秘书</span><button type="button" {...marks({ pickControl: 'app_tile_menu_open' })}>⋯</button></div>
        </div>
      )
    }
    const { rerender } = render(<Cards open={null} />)
    drawAll()
    expect(resolveGuideTarget(choose.target)).toBe(screen.getByTestId('more-cb'))
    expect(namedPickShown(choose)).toBe('命令栏')
    expect(pickMade(choose)).toBe(false)
    rerender(<Cards open="命令栏" />)
    expect(pickMade(choose)).toBe(true)
    expect(stepBoundHolds(uninstall)).toBe(true)
    rerender(<Cards open="秘书" />)
    expect(stepBoundHolds(uninstall)).toBe(false)
  })

  it('walks to Delete crewmate through the editor and its Danger zone row, which it points at', () => {
    const r = resolveGuideAction(show('agents.delete'))
    if (!r.ok) throw new Error(r.reason)
    expect(r.action.steps.map(st => st.complete.kind)).toEqual(['select', 'reach', 'ack'])
    expect(r.action.steps[0].complete).toMatchObject({ kind: 'select', selection: 'crewmate_editor_open' })
    // The section row is a step of its own, pointed at while its pane is closed.
    expect(r.action.steps[1].target).toEqual({ kind: 'location', id: 'agents.section-danger' })
    expect(r.action.steps[1].requires).toBeUndefined()
    expect(r.action.steps[2]).toMatchObject({ caution: true, target: { kind: 'location', id: 'agents.delete' } })
    expect(r.action.steps[2].requires).toBeUndefined()
    // Same for the model: its section row, then the field.
    const m = resolveGuideAction(show('agents.model'))
    if (!m.ok) throw new Error(m.reason)
    expect(m.action.steps.map(st => st.target.kind === 'location' ? st.target.id : st.target.kind)).toEqual(['agents.crew-list', 'agents.section-model', 'agents.model'])
  })

  it('a page find points at the link that opens the page, never navigating or ending silently', () => {
    const r = resolveGuideAction({ id: 'ui.find', params: { label: 'Logs', route: '/logs', page: true } })
    if (!r.ok) throw new Error(r.reason)
    const [open, here] = r.action.steps
    expect(open.complete).toEqual({ kind: 'arrive', route: '/logs' })
    expect(open.target).toMatchObject({ kind: 'find', query: { label: 'Logs', role: 'link' } })
    expect(open.textKey).toBe('components.guideLayer.step_page_open')
    // On the page, the last step says so and waits for Done.
    expect(here).toMatchObject({ target: { kind: 'none' }, complete: { kind: 'ack' }, textKey: 'components.guideLayer.step_page_here' })
    // Start keeps the address: the person opens the page themselves.
    if (r.action.enter?.kind !== 'navigate') throw new Error('expected navigate')
    expect(r.action.enter.to({ pathname: '/chat', search: '?s=1' })).toBe('/chat?s=1')
    window.history.pushState({}, '', '/logs')
    expect(factVerdict(open)).toBe('done')
    window.history.pushState({}, '', '/chat')
    // Not there yet: no verdict, so the step points at the entry like any target.
    expect(factVerdict(open)).toBeNull()
    expect(here.complete).toEqual({ kind: 'ack', at: '/logs' })
    // Only the gateway marks a page find, and only with a route and no control id.
    expect(resolveGuideAction({ id: 'ui.find', params: { label: 'Logs', page: true } })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(resolveGuideAction({ id: 'ui.find', params: { label: 'Logs', route: '/logs', page: 'yes' } })).toEqual({ ok: false, reason: 'invalid_params' })
  })

  it('a page find completes only on that exact page, never a sub-route or another tab', () => {
    const page = (label: string, route: string) => {
      const r = resolveGuideAction({ id: 'ui.find', params: { label, route, page: true } })
      if (!r.ok) throw new Error(r.reason)
      return r.action.steps[0]
    }
    const discover = page('Discover', '/apps')
    window.history.pushState({}, '', '/apps/library')
    expect(factVerdict(discover)).toBeNull()
    window.history.pushState({}, '', '/apps/')
    expect(factVerdict(discover)).toBe('done')
    const mcp = page('MCP Servers', '/capabilities?tab=mcp')
    window.history.pushState({}, '', '/capabilities?tab=crews')
    expect(factVerdict(mcp)).toBeNull()
    window.history.pushState({}, '', '/capabilities?tab=mcp')
    expect(factVerdict(mcp)).toBe('done')
    window.history.pushState({}, '', '/chat')
  })

  it('a page find highlights the main-menu link to the page, not a link in a chat reply', () => {
    window.history.pushState({}, '', '/chat')
    render(
      <>
        <nav aria-label="Main navigation"><a href="/artifacts">Artifacts</a></nav>
        <div className="message-bubble"><a href="/artifacts">Artifacts</a></div>
      </>,
    )
    const r = resolveGuideAction({ id: 'ui.find', params: { label: 'Artifacts', route: '/artifacts', page: true } }, 'g-page', 0)
    if (!r.ok) throw new Error(r.reason)
    const el = resolveGuideTarget(r.action.steps[0].target)
    expect(el?.closest('nav')).not.toBeNull()
  })

  it('refuses an id with no plan and any parameter it does not take', () => {
    // apps.detail.uninstall is guidable with a caution, but its prerequisites leave it without a plan.
    for (const id of ['apps.detail.uninstall', 'mcp.add-custom', 'setting:chat.link-previews', 'nope.nope']) {
      expect(resolveGuideAction(show(id))).toEqual({ ok: false, reason: 'unknown_location' })
    }
    expect(resolveGuideAction({ id: 'ui.show', params: {} })).toEqual({ ok: false, reason: 'invalid_params' })
    expect(resolveGuideAction({ id: 'ui.show', params: { location_id: 'chat.older-sessions', route: '/x' } })).toEqual({ ok: false, reason: 'invalid_params' })
  })

  it('keeps the address when the person is already on the page, and opens it otherwise', () => {
    const r = resolveGuideAction(show('chat.older-sessions'))
    if (!r.ok) throw new Error('expected ok')
    expect(r.action.enter.to({ pathname: '/chat/slot-A', search: '' })).toBe('/chat/slot-A')
    expect(r.action.enter.to({ pathname: '/schedule', search: '' })).toBe('/chat')
    const mcp = resolveGuideAction(show('mcp.add-server'))
    if (!mcp.ok) throw new Error('expected ok')
    expect(mcp.action.enter.to({ pathname: '/capabilities', search: '?tab=skills' })).toBe('/capabilities?tab=mcp')
    expect(mcp.action.enter.to({ pathname: '/capabilities', search: '?tab=mcp' })).toBe('/capabilities?tab=mcp')
    expect(alreadyAt('/chat', { pathname: '/chatter', search: '' })).toBe(false)
  })

  it('every generated plan resolves, and names only registered locations', () => {
    for (const [id, plan] of Object.entries(GUIDE_PLANS)) {
      const r = resolveGuideAction(show(id))
      const needsPhone = plan.placements.every(p => p.viewport === 'mobile')
      expect(r.ok || (needsPhone && r.reason === 'location_not_on_this_screen')).toBe(true)
      // A gate step points at nothing; every other step names a registered location.
      for (const p of plan.placements) for (const st of p.steps) if (st.kind !== 'gate') expect(Object.hasOwn(UI_LOCATIONS, st.location!)).toBe(true)
    }
  })

  it('a location target is exactly ONE visible element: two visible copies are missing, a hidden copy is no rival', () => {
    const { unmount } = render(<><LocTarget id="chat.older-sessions" testId="a" /><LocTarget id="chat.older-sessions" testId="b" /></>)
    expect(resolveGuideTarget({ kind: 'location', id: 'chat.older-sessions' })).toBeNull()
    unmount()
    render(<><div hidden><LocTarget id="chat.older-sessions" testId="hid" /></div><LocTarget id="chat.older-sessions" testId="vis" /></>)
    expect(resolveGuideTarget({ kind: 'location', id: 'chat.older-sessions' })).toBe(screen.getByTestId('vis'))
    // Exact id only: a prefix is not a match.
    expect(resolveGuideTarget({ kind: 'location', id: 'chat.older' })).toBeNull()
  })

  it('walks Show sessions sidebar -> Older Sessions in place, and never clicks either', async () => {
    pending = [uiGuide()]
    let current = claimed(uiGuide())
    onWrite = (name, b) => {
      if (name === 'progress') {
        const last = (b.step_index as number) >= 1
        current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
      }
      return current
    }
    const onOlder = vi.fn()
    renderGuide('/chat/slot-A', <SessionsHost onOlder={onOlder} />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    // Already on the Sessions page: the open session stays open.
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(screen.getByTestId('loc').textContent).toBe('/chat/slot-A')

    // The reveal step names the destination, not the icon-only toggle's label.
    expect(await screen.findByText(L('step_ui_show_open', { target: 'Older Sessions' }))).toBeTruthy()
    expect(screen.queryByText(/Show sessions sidebar/)).toBeNull()
    await tickTracker(2)
    expect(writes('/api/guide/progress')).toHaveLength(0)
    expect(screen.queryByTestId('older-sessions')).toBeNull()
    fireEvent.click(screen.getByTestId('sidebar-toggle'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })

    expect(await screen.findByText(L('step_ui_show_here', { label: 'Older Sessions' }))).toBeTruthy()
    fireEvent.click(await screen.findByTestId('guide-next'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
    expect((await screen.findByTestId('guide-result')).textContent).toContain(L('finished_completed'))
    expect(onOlder).not.toHaveBeenCalled()
  })

  it('pressing the highlighted control on the last step finishes the guide, as Done does', async () => {
    pending = [uiGuide()]
    let current = claimed(uiGuide())
    onWrite = (name, b) => {
      if (name === 'progress') {
        const last = (b.step_index as number) >= 1
        current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
      }
      return current
    }
    const onOlder = vi.fn()
    renderGuide('/chat/slot-A', <SessionsHost open onOlder={onOlder} />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('step_ui_show_here', { label: 'Older Sessions' }))).toBeTruthy()
    expect(screen.queryByTestId('guide-step-caution')).toBeNull()
    expect(writes('/api/guide/progress')).toHaveLength(1)
    fireEvent.click(screen.getByTestId('older-sessions'))
    // The control still does its own thing, and the guide ends on the press.
    expect(onOlder).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2), { timeout: 4000 })
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
    expect((await screen.findByTestId('guide-result')).textContent).toContain(L('finished_completed'))
  })

  it('a destructive last step warns, and pressing the control alone never finishes the guide', async () => {
    // Older Sessions stands in for a destructive control: its last step carries the caution.
    const plan = GUIDE_PLANS['chat.older-sessions'] as { placements: { steps: { caution?: true }[] }[] }
    const lasts = plan.placements.map(p => p.steps[p.steps.length - 1])
    for (const st of lasts) st.caution = true
    try {
      pending = [uiGuide()]
      let current = claimed(uiGuide())
      onWrite = (name, b) => {
        if (name === 'progress') {
          const last = (b.step_index as number) >= 1
          current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
        }
        return current
      }
      const onOlder = vi.fn()
      renderGuide('/chat/slot-A', <SessionsHost open onOlder={onOlder} />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByText(L('step_ui_show_here', { label: 'Older Sessions' }))).toBeTruthy()
      expect(screen.getByTestId('guide-step-caution').textContent).toBe(L('step_caution'))
      expect(writes('/api/guide/progress')).toHaveLength(1)
      fireEvent.click(screen.getByTestId('older-sessions'))
      // The control does its own thing; the guide does not end on the press.
      expect(onOlder).toHaveBeenCalledTimes(1)
      await act(async () => { await Promise.resolve() })
      expect(writes('/api/guide/progress')).toHaveLength(1)
      // Done still ends it.
      fireEvent.click(screen.getByTestId('guide-next'))
      await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
      expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
    } finally {
      for (const st of lasts) delete st.caution
    }
  })

  it("a destructive step with the page's own words shows them, never the generic 'gone for good' line", async () => {
    const key = 'pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its'
    const plan = GUIDE_PLANS['chat.older-sessions'] as { placements: { steps: { caution?: true; caution_key?: string }[] }[] }
    const lasts = plan.placements.map(p => p.steps[p.steps.length - 1])
    for (const st of lasts) { st.caution = true; st.caution_key = key }
    try {
      pending = [uiGuide()]
      let current = claimed(uiGuide())
      onWrite = (name, b) => {
        if (name === 'progress') current = { ...current, revision: current.revision + 1, step_index: (b.step_index as number) + 1 }
        return current
      }
      renderGuide('/chat/slot-A', <SessionsHost open onOlder={vi.fn()} />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByText(L('step_ui_show_here', { label: 'Older Sessions' }))).toBeTruthy()
      const line = await screen.findByTestId('guide-step-caution')
      expect(line.textContent).toBe(i18nT(key))
      expect(line.textContent).not.toBe(L('step_caution'))
    } finally {
      for (const st of lasts) { delete st.caution; delete st.caution_key }
    }
  })

  it('the Delete crewmate guide carries the Danger zone notice as its caution text', () => {
    const r = resolveGuideAction(show('agents.delete'))
    if (!r.ok) throw new Error(r.reason)
    const last = r.action.steps[r.action.steps.length - 1]
    expect(last.cautionKey).toBe('pages.kiroCrewAgentsPage.deleting_a_crew_unbinds_it_from_new_sessions_its')
    expect(i18nT(last.cautionKey!)).toMatch(/only unbinds/)
    expect(r.action.steps.slice(0, -1).every(st => st.cautionKey === undefined)).toBe(true)
  })

  it('a destructive last step ends once the confirm its press opened is answered', async () => {
    const plan = GUIDE_PLANS['chat.older-sessions'] as { placements: { steps: { caution?: true }[] }[] }
    const lasts = plan.placements.map(p => p.steps[p.steps.length - 1])
    for (const st of lasts) st.caution = true
    try {
      pending = [uiGuide()]
      let current = claimed(uiGuide())
      onWrite = (name, b) => {
        if (name === 'progress') {
          const last = (b.step_index as number) >= 1
          current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
        }
        return current
      }
      function ConfirmHost() {
        const [asking, setAsking] = useState(false)
        return (
          <>
            <LocTarget id="chat.sessions-sidebar-toggle" testId="sidebar-toggle" rect={{ top: 60, left: 10, width: 28, height: 28 }} />
            <LocTarget id="chat.older-sessions" testId="older-sessions" onClick={() => setAsking(true)} />
            {asking && (
              <div role="alertdialog">
                <button type="button" data-testid="confirm-no" onClick={() => setAsking(false)}>Cancel</button>
                <button type="button" data-testid="confirm-yes" {...marks({ confirm: '' })} onClick={() => setAsking(false)}>Yes</button>
              </div>
            )}
          </>
        )
      }
      renderGuide('/chat/slot-A', <ConfirmHost />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByTestId('guide-step-caution')).toBeTruthy()
      // A cancel is not a confirm: the step stays.
      fireEvent.click(screen.getByTestId('older-sessions'))
      expect(await screen.findByRole('alertdialog')).toBeTruthy()
      fireEvent.click(screen.getByTestId('confirm-no'))
      await act(async () => { await Promise.resolve() })
      expect(screen.queryByRole('alertdialog')).toBeNull()
      expect(writes('/api/guide/progress')).toHaveLength(1)
      fireEvent.click(screen.getByTestId('older-sessions'))
      expect(await screen.findByRole('alertdialog')).toBeTruthy()
      await act(async () => { await Promise.resolve() })
      expect(writes('/api/guide/progress')).toHaveLength(1)
      fireEvent.click(screen.getByTestId('confirm-yes'))
      await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
      expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
    } finally {
      for (const st of lasts) delete st.caution
    }
  })

  it.each([
    ['ends the step when it is the registered final control', true],
    ['never ends the step on the dialog closing alone when it is not', false],
  ] as const)('a destructive submit inside an open dialog %s', async (_what, marked) => {
    const plan = GUIDE_PLANS['chat.older-sessions'] as { placements: { steps: { caution?: true }[] }[] }
    const lasts = plan.placements.map(p => p.steps[p.steps.length - 1])
    for (const st of lasts) st.caution = true
    try {
      pending = [uiGuide()]
      let current = claimed(uiGuide())
      onWrite = (name, b) => {
        if (name === 'progress') {
          const last = (b.step_index as number) >= 1
          current = { ...current, revision: current.revision + 1, step_index: last ? current.step_index : (b.step_index as number) + 1, status: last ? 'completed' : 'active' }
        }
        return current
      }
      function FormDialog() {
        const [open, setOpen] = useState(true)
        return (
          <>
            <LocTarget id="chat.sessions-sidebar-toggle" testId="sidebar-toggle" rect={{ top: 60, left: 10, width: 28, height: 28 }} />
            {open && <div role="dialog"><span {...marks({ confirm: marked ? '' : null })}><LocTarget id="chat.older-sessions" testId="older-sessions" onClick={() => setOpen(false)} /></span></div>}
          </>
        )
      }
      renderGuide('/chat/slot-A', <FormDialog />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByTestId('guide-step-caution')).toBeTruthy()
      await screen.findByTestId('guide-target-outline')
      expect(writes('/api/guide/progress')).toHaveLength(1)
      fireEvent.click(screen.getByTestId('older-sessions'))
      if (marked) {
        await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
        expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
      } else {
        // The close's mutation records and any report it could trigger are microtasks.
        for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve() })
        expect(screen.queryByTestId('older-sessions')).toBeNull()
        expect(writes('/api/guide/progress').filter(w => w.body.outcome === 'observed' && w.body.step_index === 1)).toEqual([])
      }
    } finally {
      for (const st of lasts) delete st.caution
    }
  })

  it('carries a destructive control\'s caution from its generated plan onto the last step only', () => {
    const r = resolveGuideAction(show('sessions.list-menu.clean-up'))
    if (!r.ok) throw new Error('expected ok')
    expect(r.action.steps.map(st => !!st.caution)).toEqual([...r.action.steps.slice(1).map(() => false), true])
    const plain = resolveGuideAction(show('chat.older-sessions'))
    if (!plain.ok) throw new Error('expected ok')
    expect(plain.action.steps.some(st => st.caution)).toBe(false)
  })

  it('a stalled acknowledge step offers Cancel and Go back only, never a dead Next beside them', async () => {
    freezeClock()
    pending = [uiGuide()]
    let current = claimed(uiGuide())
    onWrite = (name, b) => {
      if (name === 'progress') current = { ...current, revision: current.revision + 1, step_index: (b.step_index as number) + 1 }
      return current
    }
    renderGuide('/chat/slot-A', <SessionsHost open />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('step_ui_show_here', { label: 'Older Sessions' }))).toBeTruthy()
    expect(screen.getByTestId('guide-next')).toBeTruthy()
    server.use(
      http.post('/api/guide/progress', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        calls.push({ path: '/api/guide/progress', body, headers: request.headers })
        return HttpResponse.json({ error: 'flaky' }, { status: 503 })
      }),
    )
    // The control goes away and every target_missing report fails.
    fireEvent.click(screen.getByTestId('sidebar-toggle'))
    await tickTracker(2)
    let t = T0 + GUIDE_TARGET_WAIT_MS + 1000
    const clock = vi.spyOn(Date, 'now').mockImplementation(() => t)
    for (let i = 0; i < GUIDE_FOUND_MAX_ATTEMPTS + 1; i++) {
      t += GUIDE_FOUND_RETRY_MS + 100
      await tickTracker(2)
    }
    expect(await screen.findByTestId('guide-go-back')).toBeTruthy()
    expect(screen.queryByTestId('guide-next')).toBeNull()
    const row = screen.getByTestId('guide-go-back').parentElement!
    expect(within(row).getAllByRole('button')).toHaveLength(2)
    clock.mockRestore()
  }, 15_000)

  it('skips the reveal step at once when the sidebar is already open', async () => {
    pending = [uiGuide()]
    onWrite = () => claimed(uiGuide())
    renderGuide('/chat/slot-A', <SessionsHost open />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })
  })

  it('claims with the viewport placement, and names the recorded step id in every report', async () => {
    const plan = GUIDE_PLANS['chat.older-sessions']
    const ids = plan.placements.find(p => p.id === 'desktop')!.steps.map(s => s.id)
    const placements = Object.fromEntries(plan.placements.map(p => [p.id, p.steps.map(s => s.id)]))
    const offered = guide({ actions: [{ ...show('chat.older-sessions'), plan_version: 2, placements }] })
    pending = [offered]
    // The gateway records the claimed placement's step ids on the record.
    onWrite = (name, b) => (name === 'claim'
      ? claimed({ ...offered, actions: [{ ...offered.actions[0], placement: 'desktop', step_ids: ids }], revision: b.revision as number })
      : null)
    renderGuide('/chat/slot-A', <SessionsHost open />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/claim')).toHaveLength(1))
    expect(writes('/api/guide/claim')[0].body).toEqual({ guide_id: 'g1', tab_id: TAB_ID, revision: 1, placements: ['desktop'] })
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, step_id: ids[0], outcome: 'observed' })
  })

  it('refuses a claimed record whose step ids are not this bundle’s', () => {
    const r = resolveGuideAction({ ...show('chat.older-sessions'), placement: 'desktop', step_ids: ['desktop:x', 'desktop:y'] })
    expect(r).toEqual({ ok: false, reason: 'build_mismatch' })
    // A recorded placement is walked whatever the viewport is now.
    const spy = phone()
    const ids = GUIDE_PLANS['chat.older-sessions'].placements.find(p => p.id === 'desktop')!.steps.map(s => s.id)
    const kept = resolveGuideAction({ ...show('chat.older-sessions'), placement: 'desktop', step_ids: ids })
    spy.mockRestore()
    if (!kept.ok) throw new Error(kept.reason)
    expect(kept.action.steps[0].target).toEqual({ kind: 'location', id: 'chat.sessions-sidebar-toggle' })
  })

  it('shows a blocker instead of pointing when the reveal control needs an unmet predicate', async () => {
    function NoSessions() {
      useGuidePredicate('has_open_sessions', false)
      useGuidePredicate('full_dashboard', true)
      return null
    }
    pending = [uiGuide()]
    onWrite = (name) => (name === 'claim' ? claimed(uiGuide()) : null)
    renderGuide('/chat/slot-A', <NoSessions />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    const blocker = await screen.findByTestId('guide-blocker')
    expect(blocker.textContent).toContain(L('predicate_has_open_sessions'))
    expect(blocker.textContent).not.toContain(L('predicate_full_dashboard'))
    // Reported missing with its reason after the short settle, not the full wait.
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 4000 })
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'target_missing', detail: 'predicate_unmet' })
  })
})

/** The shell's gate fact, as App.tsx reports it. */
function GateFact({ id, on }: { id: 'developer_mode' | 'terminal_enabled'; on: boolean }) {
  useGuideGate(id, on)
  return null
}

/** A picker page's selection fact, as the owning page reports it: two booleans, never which. */
function SelectionFact({ selected, available }: { selected: boolean; available: boolean }) {
  useGuideSelection('crewmate_selected', { selected, available })
  return null
}

/** The Schedule page's facts, as SchedulePage reports them: the list view and whether a job is open. */
function JobFacts({ open }: { open: boolean }) {
  useGuidePredicate('schedule_list_view', true)
  useGuideSelection('job_open', { selected: open, available: true })
  return null
}

/** A matchMedia whose phone query can be flipped, announcing the change. */
function switchableViewport() {
  let mobile = false
  const listeners = new Set<() => void>()
  const spy = vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({
    matches: q === '(max-width: 767px)' && mobile, media: q, onchange: null,
    addEventListener: (_e: string, l: () => void) => { listeners.add(l) },
    removeEventListener: (_e: string, l: () => void) => { listeners.delete(l) },
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }) as unknown as MediaQueryList)
  return { spy, toPhone: () => { mobile = true; listeners.forEach(l => l()) } }
}

describe('ui.show phase 3: selection steps, gates and the mid-guide re-plan', () => {
  const show = (location_id: string): GuideAction => ({ id: 'ui.show', params: { location_id }, build_digest: GUIDE_BUILD_DIGEST })
  const planned = (location_id: string, placement: string): Guide => {
    const plan = GUIDE_PLANS[location_id]
    const placements = Object.fromEntries(plan.placements.map(p => [p.id, p.steps.map(s => s.id)]))
    return guide({ actions: [{ ...show(location_id), plan_version: 2, placements, placement, step_ids: placements[placement] }] })
  }
  /** Echo the claim; each `observed` moves the guide on one step. */
  const walk = (offered: Guide) => {
    let current = claimed(offered)
    onWrite = (name, b) => {
      if (name === 'progress' && b.outcome === 'observed') current = { ...current, revision: current.revision + 1, step_index: (b.step_index as number) + 1 }
      else if (name === 'progress') current = { ...current, revision: current.revision + 1, status: 'target_missing', reason: String(b.detail ?? '') }
      else if (name === 'replan') current = { ...current, revision: current.revision + 1, actions: [{ ...current.actions[0], placement: b.placement as string, step_ids: (current.actions[0].placements as Record<string, string[]>)[b.placement as string] }] }
      return current
    }
  }

  it('the pairing guide points at Connect your phone only where pairing is available, and says so otherwise', async () => {
    const r = resolveGuideAction(show('shell.connect-phone'))
    if (!r.ok) throw new Error(r.reason)
    expect(r.action.steps.at(-1)).toMatchObject({ target: { kind: 'location', id: 'shell.connect-phone' } })
    function Phone({ available }: { available: boolean }) {
      useGuidePredicate('phone_connect_available', available)
      return available ? <LocTarget id="shell.connect-phone" testId="pair" /> : null
    }
    const offered = planned('shell.connect-phone', 'desktop')
    pending = [offered]
    walk(offered)
    renderGuide('/chat/slot-A', <Phone available={false} />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect((await screen.findByTestId('guide-blocker')).textContent).toContain(L('predicate_phone_connect_available'))
    cleanup()
    pending = [offered]
    walk(offered)
    renderGuide('/chat/slot-A', <Phone available />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(screen.getByTestId('guide-target-outline')).toBeTruthy())
    expect(screen.queryByTestId('guide-blocker')).toBeNull()
  })

  it('the plans resolve a select step at its picker and a gate step that points at nothing', () => {
    const edit = resolveGuideAction(show('members.edit'))
    if (!edit.ok) throw new Error(edit.reason)
    // The Crewmates preview's gate opens the plan; the pick is the next step.
    expect(edit.action.steps[0]).toMatchObject({ target: { kind: 'none' }, complete: { kind: 'gate', gate: 'preview_flag:mc-preview-crew' } })
    expect(edit.action.steps[1]).toMatchObject({
      target: { kind: 'location', id: 'members.roster-list' },
      complete: { kind: 'select', selection: 'crewmate_selected', entity: 'crewmate' },
      textKey: 'components.guideLayer.select_crewmate',
    })
    const dev = resolveGuideAction(show('shell.developer'))
    if (!dev.ok) throw new Error(dev.reason)
    expect(dev.action.steps[0]).toMatchObject({
      target: { kind: 'none' },
      complete: { kind: 'gate', gate: 'developer_mode', settingId: 'developer.developer-mode' },
      textKey: 'components.guideLayer.gate_setting',
    })
    expect(dev.action.steps[0].textVars?.label).toBe(i18nT('pages.settings.developerPanel.developer_mode'))
  })

  it('a crewmate\'s permission picker is its own ceiling: ui.show has no plan for it', () => {
    expect(resolveGuideAction(show('members.permissions')).ok).toBe(false)
  })

  it('a gate that is off pauses on a blocker naming its setting, reports gate_off, and points at nothing', async () => {
    pending = [planned('shell.developer', 'desktop')]
    walk(pending[0])
    renderGuide('/chat/slot-A', <><GateFact id="developer_mode" on={false} /><LocTarget id="shell.developer" /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    const blocker = await screen.findByTestId('guide-gate-blocker')
    expect(blocker.textContent).toBe(L('gate_setting', { label: i18nT('pages.settings.developerPanel.developer_mode') }))
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 4000 })
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'target_missing', detail: 'gate_off' })
  })

  it('a paused gate resumes by itself once the gate is on, and the guide never flips it', async () => {
    const offered = planned('shell.developer', 'desktop')
    pending = [offered]
    walk(offered)
    function Host() {
      const [on, setOn] = useState(false)
      return <><GateFact id="developer_mode" on={on} /><button type="button" data-testid="turn-on" onClick={() => setOn(true)}>on</button><LocTarget id="shell.developer" /></>
    }
    renderGuide('/chat/slot-A', <Host />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1), { timeout: 4000 })
    // Now missing: only the gate turning on brings it back.
    await tickTracker(3)
    expect(writes('/api/guide/progress')).toHaveLength(1)
    fireEvent.click(screen.getByTestId('turn-on'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 0, outcome: 'target_found' })
    // Nothing but progress reports went out: no settings write, no click.
    expect(calls.every(c => c.path.startsWith('/api/guide/'))).toBe(true)
  })

  it('a select step points at the picker and completes only on the selection fact, never on a visible later control', async () => {
    localStorage.setItem('mc-preview-crew', '1')
    pending = [planned('members.edit', 'any')]
    walk(pending[0])
    function Host() {
      const [picked, setPicked] = useState(false)
      return (
        <>
          <SelectionFact selected={picked} available />
          <LocTarget id="members.roster-list" testId="roster" onClick={() => setPicked(true)} />
          {/* The target is already drawn: that alone must not finish the pick. */}
          <LocTarget id="members.edit" rect={{ top: 400, left: 300, width: 80, height: 28 }} />
        </>
      )
    }
    renderGuide('/members', <Host />)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('select_crewmate'))).toBeTruthy()
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
    await tickTracker(4)
    // Only the preview gate (on) has passed; the pick is still waiting.
    expect(writes('/api/guide/progress')).toHaveLength(1)
    fireEvent.click(screen.getByTestId('roster'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
  })

  it('a pick already made when the step starts passes the step at once: no "already open, press Next" stop', async () => {
    localStorage.setItem('mc-preview-crew', '1')
    pending = [planned('members.edit', 'any')]
    walk(pending[0])
    renderGuide('/members', <><SelectionFact selected available /><LocTarget id="members.roster-list" /><LocTarget id="members.edit" rect={{ top: 400, left: 300, width: 80, height: 28 }} /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    // The preview gate passes, then the pick already made passes its step.
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
  })

  it('a job already open before a delete step is shown once to confirm, and goes on only on Next', async () => {
    pending = [planned('schedule.delete', 'any')]
    walk(pending[0])
    renderGuide('/schedule', <><JobFacts open /><LocTarget id="schedule.job-list" /><LocTarget id="schedule.delete" rect={{ top: 400, left: 300, width: 80, height: 28 }} /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('select_confirm_job'))).toBeTruthy()
    expect(await screen.findByTestId('guide-arrow')).toBeTruthy()
    // Not passed over silently: nothing is reported until the person confirms.
    await tickTracker(4)
    expect(writes('/api/guide/progress')).toHaveLength(0)
    fireEvent.click(screen.getByTestId('guide-next'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })
  })

  it('the confirm before a delete step names the job actually open', async () => {
    pending = [planned('schedule.delete', 'any')]
    walk(pending[0])
    function NamedJob() {
      useGuidePredicate('schedule_list_view', true)
      useGuideSelection('job_open', { selected: true, available: true, name: 'Nightly backup' })
      return null
    }
    renderGuide('/schedule', <><NamedJob /><LocTarget id="schedule.job-list" /><LocTarget id="schedule.delete" rect={{ top: 400, left: 300, width: 80, height: 28 }} /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('select_confirm_job'))).toBeTruthy()
    expect((await screen.findByTestId('guide-confirm-name')).textContent).toBe(L('select_open_named', { name: 'Nightly backup' }))
  })

  it('a named job pick with another job open is not confirmed or completed: the person is asked to pick', async () => {
    const g = planned('schedule.delete', 'any')
    const named: Guide = { ...g, actions: [{ ...g.actions[0], params: { location_id: 'schedule.delete', pick: 'Morning summary' } }] }
    pending = [named]
    walk(named)
    function OtherJob() {
      useGuidePredicate('schedule_list_view', true)
      useGuideSelection('job_open', { selected: true, available: true, name: 'Nightly backup' })
      return null
    }
    renderGuide('/schedule', <><OtherJob /><LocTarget id="schedule.job-list" /><LocTarget id="schedule.delete" rect={{ top: 400, left: 300, width: 80, height: 28 }} /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('select_job'))).toBeTruthy()
    await tickTracker(4)
    expect(screen.queryByText(L('select_confirm_job'))).toBeNull()
    expect(screen.queryByTestId('guide-confirm-name')).toBeNull()
    expect(writes('/api/guide/progress')).toHaveLength(0)
  })

  it('a job already open before a delete step, with its list folded away, floats the confirm and never sends the person to the list', async () => {
    pending = [planned('schedule.delete', 'any')]
    walk(pending[0])
    renderGuide('/schedule', <><JobFacts open /><LocTarget id="schedule.delete" rect={{ top: 400, left: 300, width: 80, height: 28 }} /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    const text = await screen.findByTestId('guide-step-text')
    await waitFor(() => expect(text.textContent).toBe(L('select_confirm_unseen_job')))
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    await tickTracker(2)
    expect(writes('/api/guide/progress')).toHaveLength(0)
    fireEvent.click(screen.getByTestId('guide-next'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/progress')[0].body).toMatchObject({ step_index: 0, outcome: 'observed' })
  })

  it('a job picked during the step before a delete completes it on the pick, with no confirm', async () => {
    pending = [planned('schedule.delete', 'any')]
    walk(pending[0])
    function Picker() {
      const [open, setOpen] = useState(false)
      return <><JobFacts open={open} /><button data-testid="open-job" onClick={() => setOpen(true)}>open</button></>
    }
    renderGuide('/schedule', <><Picker /><LocTarget id="schedule.job-list" /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByText(L('select_job'))).toBeTruthy()
    fireEvent.click(screen.getByTestId('open-job'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(screen.queryByText(L('select_confirm_job'))).toBeNull()
  })

  it('a pick already made while its list is folded away passes the step too, never sending the person to the list', async () => {
    localStorage.setItem('mc-preview-crew', '1')
    pending = [planned('members.edit', 'any')]
    walk(pending[0])
    renderGuide('/members', <><SelectionFact selected available /><LocTarget id="members.edit" rect={{ top: 400, left: 300, width: 80, height: 28 }} /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    // The preview gate passes, then the pick already made passes its step.
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2))
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'observed' })
  })

  it('an empty picker shows a blocker and reports selection_empty; it never points at a create control', async () => {
    localStorage.setItem('mc-preview-crew', '1')
    pending = [planned('members.edit', 'any')]
    walk(pending[0])
    renderGuide('/members', <><SelectionFact selected={false} available={false} /><LocTarget id="members.roster-list" /><LocTarget id="members.new" /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect((await screen.findByTestId('guide-selection-empty')).textContent).toBe(L('select_empty_crewmate'))
    expect(screen.queryByTestId('guide-arrow')).toBeNull()
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(2), { timeout: 4000 })
    expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1, outcome: 'target_missing', detail: 'selection_empty' })
  })

  it('asks for a re-plan when the viewport class changes mid-guide, and not before', async () => {
    const vp = switchableViewport()
    pending = [planned('shell.developer', 'desktop')]
    walk(pending[0])
    renderGuide('/chat/slot-A', <><GateFact id="developer_mode" on /><LocTarget id="shell.developer" /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    expect(writes('/api/guide/replan')).toHaveLength(0)
    act(() => vp.toPhone())
    await waitFor(() => expect(writes('/api/guide/replan')).toHaveLength(1))
    expect(writes('/api/guide/replan')[0].body).toMatchObject({ guide_id: 'g1', tab_id: TAB_ID, action_index: 0, placement: 'mobile' })
    // Asked once for that revision: no second request for the same change.
    await tickTracker(2)
    expect(writes('/api/guide/replan')).toHaveLength(1)
  })

  it('a refused re-plan is not asked again for the same revision, even as the guide is re-read', async () => {
    const vp = switchableViewport()
    const offered = planned('shell.developer', 'desktop')
    pending = [offered]
    walk(offered)
    server.use(http.post('/api/guide/replan', async ({ request }) => {
      calls.push({ path: '/api/guide/replan', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      return HttpResponse.json({ error: 'the steps walked so far differ in that placement', code: 'replan_not_at_boundary' }, { status: 409 })
    }))
    renderGuide('/chat/slot-A', <><GateFact id="developer_mode" on /><LocTarget id="shell.developer" /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    act(() => vp.toPhone())
    await waitFor(() => expect(writes('/api/guide/replan')).toHaveLength(1))
    // The same guide at the same revision, re-read with a renewed lease (what a
    // heartbeat answer does every 15 s): a new object, but nothing to re-plan.
    const held = qc.getQueryData<Guide[]>(GUIDE_PENDING_QUERY_KEY)!
    pending = held.map(g => ({ ...g, lease_expires_at: 99_999 }))
    await act(async () => { await qc.invalidateQueries({ queryKey: GUIDE_PENDING_QUERY_KEY }) })
    await tickTracker(2)
    expect(writes('/api/guide/replan')).toHaveLength(1)
    // A declined re-plan is designed to change nothing: no error line.
    expect(screen.queryByText('the steps walked so far differ in that placement')).toBeNull()
  })

  it('a re-plan that fails for any other reason says so in the guide', async () => {
    const vp = switchableViewport()
    const offered = planned('shell.developer', 'desktop')
    pending = [offered]
    walk(offered)
    server.use(http.post('/api/guide/replan', async ({ request }) => {
      calls.push({ path: '/api/guide/replan', body: (await request.json()) as Record<string, unknown>, headers: request.headers })
      return HttpResponse.json({ error: 'replan store unavailable' }, { status: 503 })
    }))
    renderGuide('/chat/slot-A', <><GateFact id="developer_mode" on /><LocTarget id="shell.developer" /></>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    await waitFor(() => expect(writes('/api/guide/progress')).toHaveLength(1))
    act(() => vp.toPhone())
    await waitFor(() => expect(writes('/api/guide/replan')).toHaveLength(1))
    expect(await screen.findByText('replan store unavailable')).toBeTruthy()
  })

  describe('a pick binds every step after it, through the confirm its removal opens', () => {
    const APPS = ['Command Bar', 'Secretary']
    const box = (rect: { top: number; left: number; width: number; height: number }) => (el: HTMLElement | null) => {
      if (!el) return
      el.getBoundingClientRect = () => ({ ...rect, right: rect.left + rect.width, bottom: rect.top + rect.height, x: rect.left, y: rect.top, toJSON: () => ({}) }) as DOMRect
    }
    /** Library, reduced to its cards, one ⋯ menu at a time (drawn outside the card, naming its app) and the uninstall confirm. */
    function LibraryHost({ initialOpen = null }: { initialOpen?: string | null }) {
      const [open, setOpen] = useState<string | null>(initialOpen)
      const [asking, setAsking] = useState(false)
      useGuideSelection('app_tile_menu_open', { selected: open !== null, available: true, name: open ?? undefined })
      return (
        <>
          <div {...marks({ location: "apps.library.app-list" }, box({ top: 100, left: 20, width: 400, height: 120 }))}>
            {APPS.map((a, i) => (
              <div key={a} {...marks({ pick: a }, box({ top: 110, left: 30 + i * 200, width: 180, height: 100 }))}>
                <button type="button" data-testid={`menu-${a}`} onClick={() => setOpen(o => (o === a ? null : a))}>{a}</button>
              </div>
            ))}
          </div>
          {open && (
            <div {...marks({ pickOf: open })}>
              <LocTarget
                id="apps.library.tile-uninstall"
                testId={`uninstall-${open}`}
                rect={{ top: open === 'Command Bar' ? 400 : 600, left: 40, width: 120, height: 28 }}
                onClick={() => { setOpen(null); setAsking(true) }}
              />
            </div>
          )}
          {asking && (
            <div role="dialog">
              <button type="button" {...marks({ confirm: '' })} data-testid="confirm-uninstall" onClick={() => setAsking(false)}>Uninstall</button>
              <button type="button" data-testid="cancel-uninstall" onClick={() => setAsking(false)}>Cancel</button>
            </div>
          )}
        </>
      )
    }
    const uninstall = (pick?: string): Guide => {
      const g = planned('apps.library.tile-uninstall', 'any')
      return pick ? { ...g, actions: [{ ...g.actions[0], params: { location_id: 'apps.library.tile-uninstall', pick } }] } : g
    }
    /** The gateway: observed moves on (the last one completes), missing and found recover in place or at a resume step. */
    const gateway = (offered: Guide) => {
      let current = claimed(offered)
      onWrite = (name, b) => {
        if (name === 'progress') {
          current = b.outcome === 'observed' && (b.step_index as number) >= 1
            ? { ...current, revision: current.revision + 1, status: 'completed' }
            : gatewayProgress(current, b)
        }
        return current
      }
      return () => current
    }
    const outcomes = () => writes('/api/guide/progress').map(c => c.body.outcome)
    const outlineTop = () => screen.queryByTestId('guide-target-outline')?.style.top
    // The tracker's own clock: its ticks and bounds are advanced, never slept through.
    beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }) })
    afterEach(() => { vi.useRealTimers() })
    const pass = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

    it('the Uninstall step is held to the app picked: another app\'s menu is never its target', () => {
      const r = resolveGuideAction({ ...show('apps.library.tile-uninstall'), params: { location_id: 'apps.library.tile-uninstall', pick: 'Command Bar' } })
      if (!r.ok) throw new Error(r.reason)
      const [, step] = r.action.steps
      expect(step.bound).toEqual({ selection: 'app_tile_menu_open', pickName: 'Command Bar' })
      const owner = { current: 'Secretary' }
      function Menu({ open }: { open: string }) {
        useGuideSelection('app_tile_menu_open', { selected: true, available: true, name: open })
        return <div {...marks({ pickOf: owner.current })}><LocTarget id="apps.library.tile-uninstall" /></div>
      }
      // Secretary's menu open: not the pick, nothing to point at.
      const { rerender } = render(<Menu open="Secretary" />)
      expect(resolveStepTarget(step)).toBeNull()
      // The pick's menu open, but the item drawn for another app: not it either.
      rerender(<Menu open="Command Bar" />)
      expect(resolveStepTarget(step)).toBeNull()
      owner.current = 'Command Bar'
      rerender(<Menu open="Command Bar" />)
      expect(resolveStepTarget(step)?.getAttribute('data-ui-location')).toBe('apps.library.tile-uninstall')
    })

    it('an SVG carrying the Uninstall marker and the picked app\'s name is never the target, counted copy or outline', async () => {
      const r = resolveGuideAction({ ...show('apps.library.tile-uninstall'), params: { location_id: 'apps.library.tile-uninstall', pick: 'Command Bar' } })
      if (!r.ok) throw new Error(r.reason)
      const [, step] = r.action.steps
      function Forged({ real }: { real: boolean }) {
        useGuideSelection('app_tile_menu_open', { selected: true, available: true, name: 'Command Bar' })
        return (
          <>
            {/* Content copying the strings: an artifact drawn in the page, no ref behind it. */}
            <svg data-ui-location="apps.library.tile-uninstall" data-guide-pick-of="Command Bar" ref={box({ top: 900, left: 40, width: 120, height: 28 }) as never}><rect width="10" height="10" /></svg>
            {real && <div {...marks({ pickOf: 'Command Bar' })}><LocTarget id="apps.library.tile-uninstall" testId="real" /></div>}
          </>
        )
      }
      const { rerender } = render(<Forged real={false} />)
      expect(liveTarget('apps.library.tile-uninstall').status).toBe('unmounted')
      expect(resolveStepTarget(step)).toBeNull()
      // The real item beside it: one copy, not two, and it is the one resolved.
      rerender(<Forged real />)
      expect(liveTarget('apps.library.tile-uninstall').status).toBe('pointable')
      expect(resolveStepTarget(step)).toBe(screen.getByTestId('real'))
      cleanup()
      // Another app's real menu, with the picked app's name copied in between: still another app's.
      function Wrapped() {
        useGuideSelection('app_tile_menu_open', { selected: true, available: true, name: 'Command Bar' })
        return <div {...marks({ pickOf: 'Secretary' })}><div data-guide-pick-of="Command Bar"><LocTarget id="apps.library.tile-uninstall" testId="secretary" /></div></div>
      }
      render(<Wrapped />)
      expect(liveTarget('apps.library.tile-uninstall').status).toBe('pointable')
      expect(resolveStepTarget(step)).toBeNull()
      cleanup()
      // Through the whole guide: the select step outlines a card, never the SVG.
      const offered = uninstall('Command Bar')
      pending = [offered]
      gateway(offered)
      renderGuide('/apps/library', <><LibraryHost /><svg data-ui-location="apps.library.tile-uninstall" data-guide-pick-of="Command Bar" data-guide-pick="Command Bar" ref={box({ top: 900, left: 40, width: 120, height: 28 }) as never}><rect width="10" height="10" /></svg></>)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByText(L('select_app'))).toBeTruthy()
      pass(GUIDE_TRACK_TICK_MS * 4)
      expect(outlineTop()).toBe('106px')
    })

    it('a closed menu sends the guide back to the choice, and another app\'s menu never gets its Uninstall outlined', async () => {
      const offered = uninstall('Command Bar')
      pending = [offered]
      const now = gateway(offered)
      renderGuide('/apps/library', <LibraryHost />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByText(L('select_app'))).toBeTruthy()
      fireEvent.click(screen.getByTestId('menu-Command Bar'))
      await waitFor(() => expect(outcomes()).toEqual(['observed']))
      await waitFor(() => expect(outlineTop()).toBe('396px'))
      // Closed: the step it depends on no longer holds, so the guide walks back to it.
      fireEvent.click(screen.getByTestId('menu-Command Bar'))
      for (let i = 0; i < 4 && now().step_index !== 0; i++) { pass(GUIDE_EARLIER_STEP_WAIT_MS); await act(async () => { await Promise.resolve() }) }
      await waitFor(() => expect(now().step_index).toBe(0))
      expect(outcomes()).toEqual(['observed', 'target_missing', 'target_found'])
      expect(writes('/api/guide/progress')[2].body).toMatchObject({ resume_step_index: 0 })
      expect(await screen.findByText(L('select_app'))).toBeTruthy()
      // Another app's menu: the choice stays open, pointing at the app named.
      fireEvent.click(screen.getByTestId('menu-Secretary'))
      expect(screen.getByTestId('uninstall-Secretary')).toBeTruthy()
      for (let i = 0; i < 8; i++) {
        pass(GUIDE_TARGET_WAIT_MS / 4)
        expect(outlineTop()).not.toBe('596px')
      }
      expect(outlineTop()).toBe('106px')
      expect(now().step_index).toBe(0)
      expect(outcomes()).toEqual(['observed', 'target_missing', 'target_found'])
    })

    it('the confirm of an app already open goes on only while that pick holds at the press', async () => {
      const offered = uninstall('Command Bar')
      pending = [offered]
      gateway(offered)
      renderGuide('/apps/library', <LibraryHost initialOpen="Command Bar" />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      expect(await screen.findByText(L('select_confirm_app'))).toBeTruthy()
      fireEvent.click(screen.getByTestId('menu-Secretary'))
      fireEvent.click(screen.getByTestId('guide-next'))
      await act(async () => { await Promise.resolve() })
      expect(outcomes()).toEqual([])
      // Back to the pick: Next goes on.
      fireEvent.click(screen.getByTestId('menu-Command Bar'))
      fireEvent.click(screen.getByTestId('guide-next'))
      await waitFor(() => expect(outcomes()).toEqual(['observed']))
    })

    it('without a named pick, Next confirms only the app the panel names', async () => {
      const offered = uninstall()
      pending = [offered]
      gateway(offered)
      renderGuide('/apps/library', <LibraryHost initialOpen="Command Bar" />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      await waitFor(() => expect(screen.getByTestId('guide-confirm-name').textContent).toBe(L('select_open_named', { name: 'Command Bar' })))
      // Switched just before the press: the panel still names Command Bar.
      fireEvent.click(screen.getByTestId('menu-Secretary'))
      fireEvent.click(screen.getByTestId('guide-next'))
      await act(async () => { await Promise.resolve() })
      expect(outcomes()).toEqual([])
      await waitFor(() => expect(screen.getByTestId('guide-confirm-name').textContent).toBe(L('select_open_named', { name: 'Secretary' })))
      fireEvent.click(screen.getByTestId('guide-next'))
      await waitFor(() => expect(outcomes()).toEqual(['observed']))
    })

    async function pressUninstall() {
      const offered = uninstall('Command Bar')
      pending = [offered]
      const now = gateway(offered)
      renderGuide('/apps/library', <LibraryHost />)
      fireEvent.click(await screen.findByTestId('guide-start'))
      fireEvent.click(await screen.findByTestId('menu-Command Bar'))
      await waitFor(() => expect(outlineTop()).toBe('396px'))
      expect(screen.getByTestId('guide-step-caution')).toBeTruthy()
      // The press closes the menu: the item is gone, the confirm stays open.
      fireEvent.click(screen.getByTestId('uninstall-Command Bar'))
      expect(screen.queryByTestId('uninstall-Command Bar')).toBeNull()
      expect(screen.getByRole('dialog')).toBeTruthy()
      for (let i = 0; i < 6; i++) pass(GUIDE_TARGET_WAIT_MS / 4)
      // Waited out well past every missing bound: never missing, never back.
      expect(outcomes()).toEqual(['observed'])
      expect(now().step_index).toBe(1)
      return now
    }

    it('the confirm outlives the menu item that opened it: confirming ends the guide', async () => {
      const now = await pressUninstall()
      fireEvent.click(screen.getByTestId('confirm-uninstall'))
      await waitFor(() => expect(outcomes()).toEqual(['observed', 'observed']))
      expect(writes('/api/guide/progress')[1].body).toMatchObject({ step_index: 1 })
      expect(now().status).toBe('completed')
    })

    it('backing out of the confirm returns to the step, which walks back to the choice: never stuck', async () => {
      const now = await pressUninstall()
      fireEvent.click(screen.getByTestId('cancel-uninstall'))
      for (let i = 0; i < 4 && now().step_index !== 0; i++) { pass(GUIDE_EARLIER_STEP_WAIT_MS); await act(async () => { await Promise.resolve() }) }
      await waitFor(() => expect(now().step_index).toBe(0))
      expect(outcomes()).toEqual(['observed', 'target_missing', 'target_found'])
      expect(await screen.findByText(L('select_app'))).toBeTruthy()
    })
  })
})

describe("the offering crewmate's own words in a guide", () => {
  // The guide's slot is a crewmate's pinned thread, so its words are that
  // crewmate's: the "From" line names it from the roster.
  beforeEach(() => {
    server.use(http.get('/api/members', () => HttpResponse.json({
      members: [{ name: 'radar-keeper', display_name: 'Skipper', kiro_agent: 'kirocrew', slug: 'radar-keeper', slot_key: 'slot-A', running: false }],
    })))
  })

  const noted = (over: Partial<Guide> = {}): Partial<Guide> => ({
    intro: 'Radar keeps an eye on your build so you do not have to.',
    actions: [{ ...MCP_OPEN, note: 'Adding it is what puts radar on duty tonight.' }],
    ...over,
  })
  const follows = (a: HTMLElement, b: HTMLElement) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

  it('shows the intro on the offer card under its title, attributed to the offering crewmate', async () => {
    pending = [guide(noted())]
    renderGuide('/chat/slot-A')
    const intro = await screen.findByTestId('guide-offer-intro')
    expect(intro.textContent).toContain('Radar keeps an eye on your build so you do not have to.')
    expect((await screen.findByTestId('guide-offer-intro-from')).textContent).toBe(L('note_from', { name: 'Skipper' }))
    const card = screen.getByTestId('guide-offer-card')
    expect(follows(card.querySelector('h3')!, intro)).toBe(true)
  })

  it('renders the text as text, never as markup', async () => {
    pending = [guide(noted({ intro: '<b>bold</b> <img src=x onerror=alert(1)>' }))]
    renderGuide('/chat/slot-A')
    const intro = await screen.findByTestId('guide-offer-intro')
    expect(intro.querySelector('b, img')).toBeNull()
    expect(intro.textContent).toContain('<b>bold</b> <img src=x onerror=alert(1)>')
  })

  it("puts the intro on the first step, under the dashboard's own line, and no note there", async () => {
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.mcpServersTab} />, noted(), '/capabilities')
    const template = await screen.findByTestId('guide-step-text')
    expect(template.textContent).toBe(L('step_mcp_open_tab'))
    const intro = screen.getByTestId('guide-step-intro')
    expect(follows(template, intro)).toBe(true)
    expect((await screen.findByTestId('guide-step-intro-from')).textContent).toBe(L('note_from', { name: 'Skipper' }))
    expect(screen.queryByTestId('guide-step-note')).toBeNull()
  })

  it("shows an action's note under its final step and keeps the template line", async () => {
    await startCrewmate(<Target anchor={GUIDE_ANCHORS.mcpCustomJson} />, noted({ step_index: 2 }), '/capabilities')
    const template = await screen.findByTestId('guide-step-text')
    expect(template.textContent).toBe(L('step_mcp_custom_json'))
    const note = screen.getByTestId('guide-step-note')
    expect(note.textContent).toContain('Adding it is what puts radar on duty tonight.')
    expect(screen.getByTestId('guide-step-note-from').textContent).toBe(L('note_from', { name: 'Skipper' }))
    expect(follows(template, note)).toBe(true)
    expect(screen.queryByTestId('guide-step-intro')).toBeNull()
  })

  describe('a step that is both the first and an action\'s last', () => {
    const one = (note?: string): Guide => claimed(guide({
      intro: 'Find the Developer Mode setting',
      actions: [{ id: 'settings.show', params: { setting_id: 'chat.default-model' }, ...(note === undefined ? {} : { note }) }],
    }))
    const at = () => {
      const r = resolveGuideAction({ id: 'settings.show', params: { setting_id: 'chat.default-model' } })
      if (!r.ok) throw new Error(r.reason)
      return r.action.enter.to({ pathname: '/', search: '' })
    }

    it('shows one block, the note, never the intro beside it', async () => {
      pending = [one('This takes you right to the toggle.')]
      renderGuide(at(), undefined, null)
      fireEvent.click(await screen.findByTestId('guide-continue'))
      const note = await screen.findByTestId('guide-step-note')
      expect(note.textContent).toContain('This takes you right to the toggle.')
      expect(screen.queryByTestId('guide-step-intro')).toBeNull()
      expect(await screen.findAllByText(L('note_from', { name: 'Skipper' }))).toHaveLength(1)
    })

    it('falls back to the intro when the action has no note', async () => {
      pending = [one('  ')]
      renderGuide(at(), undefined, null)
      fireEvent.click(await screen.findByTestId('guide-continue'))
      const intro = await screen.findByTestId('guide-step-intro')
      expect(intro.textContent).toContain('Find the Developer Mode setting')
      expect(screen.queryByTestId('guide-step-note')).toBeNull()
    })
  })

  it('draws nothing extra when the agent gave no words', async () => {
    pending = [guide()]
    renderGuide('/chat/slot-A')
    await screen.findByTestId('guide-start')
    expect(screen.queryByTestId('guide-offer-intro')).toBeNull()
  })
})

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
    const g = findGuide({ label: 'Clear', location_id: 'notifications.page-clear-all' })
    pending = [g]
    walk(g)
    renderGuide('/chat/slot-A', <p>No notifications</p>)
    fireEvent.click(await screen.findByTestId('guide-start'))
    expect(await screen.findByTestId('guide-find-nothing-yet', {}, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText(L('find_nothing_yet', { label: 'Clear' }))).toBeTruthy()
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
