/**
 * ui.show guides: point at an indexed location and never click it, then
 * phase 3's selection steps, gates and the mid-guide re-plan.
 *
 * Shared fixtures: src/test/GuideLayerTestUtils.tsx. This file keeps its own
 * word-for-word copy of the guide state its tests reassign (pending,
 * onWrite, qc, calls) and of every helper that reads or assigns it
 * (renderGuide, writes, the top-level hooks), plus L and freezeClock: an
 * import cannot be reassigned, src/i18n/dynamicKeys.test.ts allows L's
 * assembled key only in a .test. file, and the flake ratchet allows
 * freezeClock's fake timers only in a file whose afterEach restores real
 * timers.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { useState, type ReactNode } from 'react'
import { server } from '../../integration/mocks/server'
import '../api/client'
import { store } from '../store'
import { setActiveSlot } from '../store/chatSlice'
import { TAB_ID } from '../api/tabId'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { GUIDE_PENDING_QUERY_KEY, type Guide, type GuideAction } from '../api/guide'
import { _resetViewedThreadForTests } from '../lib/viewedThread'
import { i18nT } from '../i18n/t'
import { GuideProvider } from './GuideContext'
import { dropSelection, reportSelection, useGuideGate, useGuidePredicate, useGuideSelection } from './guidePredicates'
import GuideLayer, { openModalDialog, pickAlternativeRects, coveredByModal, onlyPickName, namedPickShown } from './GuideLayer'
import GuideOfferCard from './GuideOfferCard'
import { alreadyAt, resolveGuideAction } from './guideActions'
import { factVerdict, GUIDE_EARLIER_STEP_WAIT_MS, GUIDE_FOUND_MAX_ATTEMPTS, GUIDE_FOUND_RETRY_MS, GUIDE_TARGET_WAIT_MS, GUIDE_TRACK_TICK_MS, pageStillLoading, pickMade, resolveGuideTarget, resolveStepTarget, stepBoundHolds } from './useGuideStepTracker'
import { liveTarget } from './liveRegistry'
import { GUIDE_BUILD_DIGEST, GUIDE_PLANS } from '../uiLocations/guidePlans.gen'
import { UI_LOCATIONS } from '../uiLocations/descriptors'
import { marks } from '../test/guideTargets'
import {
  tickTracker,
  waitFor,
  guide,
  claimed,
  LocationProbe,
  T0,
  gatewayProgress,
} from '../test/GuideLayerTestUtils'
import type { Call } from '../test/GuideLayerTestUtils'

const L = (k: string, v?: Record<string, unknown>) => i18nT(`components.guideLayer.${k}`, v)
/**
 * Resolve once the last step watches its control's press. The watch is a
 * passive effect of the render that outlines the target, and the tracker's
 * tick renders outside act: the outline can be in the DOM while that effect
 * is still queued, and a press in that gap is never seen. The empty act runs
 * after the queued effect, so the press that follows is watched.
 */
async function untilPressWatched() {
  await screen.findByTestId('guide-target-outline')
  await act(async () => {})
  // Flush the useEffect that attaches the click listener on the target element;
  // the listener registration runs after the render that shows the outline.
  await act(async () => {})
}
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

  it('walks to Delete mate through the editor and its Danger zone row, which it points at', () => {
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
    await untilPressWatched()
    expect(writes('/api/guide/progress')).toHaveLength(1)
    fireEvent.click(screen.getByTestId('older-sessions'))
    // The control still does its own thing, and the guide ends on the press.
    await waitFor(() => expect(onOlder).toHaveBeenCalledTimes(1))
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
      await untilPressWatched()
      expect(writes('/api/guide/progress')).toHaveLength(1)
      fireEvent.click(screen.getByTestId('older-sessions'))
      // The control does its own thing; the guide does not end on the press.
      await waitFor(() => expect(onOlder).toHaveBeenCalledTimes(1))
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

  it('the Delete mate guide carries the Danger zone notice as its caution text', () => {
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
      await untilPressWatched()
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
      await untilPressWatched()
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

  it('a mate\'s permission picker is its own ceiling: ui.show has no plan for it', () => {
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
      expect(await screen.findByTestId('uninstall-Secretary')).toBeTruthy()
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
      await waitFor(() => expect(screen.queryByTestId('uninstall-Command Bar')).toBeNull())
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
