/**
 * Registered-action guide: consent before anything moves, the arrow's target
 * and pointer contract, owner-tab / revision handling, which slot "is being
 * viewed", per-request guide headers on the ONE save a committed step names,
 * and that the browser never claims a save succeeded.
 *
 * Sibling suites: GuideLayer.offerAndFinish (the MCP hand-off to the native
 * add form, the in-chat offer card, a surface the guide opened, the way back
 * once it ends, the crewmate's own words), GuideLayer.uiShow and
 * GuideLayer.uiFind. Shared fixtures: src/test/GuideLayerTestUtils.tsx. Each
 * file keeps its own word-for-word copy of the guide state its tests
 * reassign (pending, onWrite, qc, calls) and of every helper that reads or
 * assigns it (renderGuide, writes, startCrewmate, the top-level hooks), plus
 * L and, where used, freezeClock: an import cannot be reassigned,
 * src/i18n/dynamicKeys.test.ts allows L's assembled key only in a .test.
 * file, and the flake ratchet allows freezeClock's fake timers only in a
 * file whose afterEach restores real timers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
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
import { applyGuideUpdate, GUIDE_PENDING_QUERY_KEY, type Guide } from '../api/guide'
import { _resetViewedThreadForTests, setViewedThreadSlot } from '../lib/viewedThread'
import { i18nT } from '../i18n/t'
import { GuideProvider, useGuide } from './GuideContext'
import GuideLayer, { arrowPlacement, GUIDE_RELAYOUT_MS, isFinalGuideStep, nearbyControlBoxes, pageChromeBoxes, popoverPlacement, clampToViewport, keepClearBoxes, KEEP_CLEAR_REPLY_PX, NEXT_FIELD_PX, openModalDialog } from './GuideLayer'
import GuideOfferCard from './GuideOfferCard'
import { GUIDE_ANCHORS, isSensitiveSetting, resolveGuideAction, resolveGuideActions } from './guideActions'
import { GUIDE_EARLIER_STEP_WAIT_MS, GUIDE_FOUND_MAX_ATTEMPTS, GUIDE_FOUND_RETRY_MS, GUIDE_TARGET_WAIT_MS, visibleBox } from './useGuideStepTracker'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { resolveSettingElementStrict } from '../hooks/useSettingHighlight'
import {
  tickTracker,
  waitFor,
  guide,
  claimed,
  MCP_OPEN,
  LocationProbe,
  Target,
  T0,
  gatewayProgress,
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
