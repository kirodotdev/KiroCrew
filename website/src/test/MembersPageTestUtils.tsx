/**
 * Fixtures the MembersPage test files share: roster rows, the page render with
 * its router and leave-guard probes, the "+" menu drivers, the bounded-create
 * clock and the api mock reset. Every test file that imports these declares the
 * same vi.mock factories itself, and the helpers reach the mocked modules through
 * it.
 */
import { expect, vi } from 'vitest'
import { useState } from 'react'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { namedCeiling } from './namedCeiling'
import { defaultScheduler, notifyManager } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { renderWithProviders } from './helpers'
import { NavigationLeaveGuardProvider, useMayLeaveForNavigation } from '../components/NavigationLeaveGuard'
import { api } from '../api/client'
import { MobileRosterBarContext } from '../components/MobileNavRailContext'
import MembersPage from '../pages/members/MembersPage'

/** The page's own memory key (mirrors `LAST_MEMBER_KEY` in useMembersPageNavigation.ts). */
export const LAST_MEMBER_KEY = 'mc-members-last-member'
// Spelled out rather than imported: the value IS the contract with a returning
// browser, so a rename of the page's constant must fail here.
export const PANEL_OPEN_KEY = 'mc-members-panel-open'

/** A window wide enough to dock the side panel BESIDE the thread (see
 *  panelSitsBeside): roster 264 + gaps 24 + shell reserve 560 + panel min 320
 *  = 1168. happy-dom's default is narrower, which would put every case in
 *  overlay mode with the panel closed. Narrow-window cases set their own. */
export const WIDE_WINDOW = 1440
export function setWindowWidth(px: number) {
  Object.defineProperty(window, 'innerWidth', { value: px, configurable: true, writable: true })
}

export function row(overrides: Record<string, unknown> = {}) {
  const base = {
    name: 'oncall',
    slug: 'oncall',
    bound: false,
    slot_key: '',
    running: false,
    kiro_agent: 'kirocrew',
    workspace: 'default',
    memory_store: 'default',
    model: '',
    ...overrides,
  }
  // Every roster row now carries a baseline projections block (the backend
  // contract). The `roster` face mirrors the row's own config fields so the
  // page reads identical values whether from the row or the seeded store; a
  // case that wants a divergence overrides `projections` explicitly.
  const projections = {
    asOfSeq: 1,
    values: {
      roster: {
        name: base.name,
        slug: base.slug,
        kiro_agent: base.kiro_agent,
        workspace: base.workspace,
        memory_store: base.memory_store,
        model: base.model,
        slot_key: base.slot_key,
        last_active_ts: (base as { last_active_ts?: number }).last_active_ts,
        last_message: (base as { last_message?: string }).last_message,
        starred: (base as { starred?: boolean }).starred,
      },
      // `activity` is deliberately omitted from the default fixture: the
      // activity-focused cases feed entries through api.memberActivity, and a
      // member with no activity projection must fall back to that query. Cases
      // that want a pushed activity projection seed it via memberProjectionStore.
      wake: { patrol: 'none' as const },
      driving: { open: [] },
    },
  }
  return { ...base, projections, ...overrides }
}

/** The built-in Assistant crewmate: its own member (`mate`, template
 *  `mate`), separate from the reserved `default` member. The gateway reports
 *  its welcome as owed and its marker as unclaimed unless a case says otherwise. */
export function assistantRow(overrides: Record<string, unknown> = {}) {
  return row({ name: 'mate', slug: 'mate', kiro_agent: 'kirocrew', source: 'builtin', memory_store: 'default', welcome_owed: true, welcome_claimed: false, ...overrides })
}
/** The reserved `default` member, as the backend lists it. */
export function defaultRow(overrides: Record<string, unknown> = {}) {
  return row({ name: 'default', slug: 'default', ...overrides })
}

/** Echoes the requested slug back as the thread's member — the happy path for
 *  any roster, so an open (a click, or the restore of a remembered member)
 *  resolves cleanly for whichever member it names. Cases that need a collision
 *  or a failure pass `thread`. */
export function echoThread(slug: string) {
  return Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true })
}

/**
 * Ceiling for a wait on the chat pane, or on anything an open or a create's
 * follow-up produces (the pane, its viewed-thread registration, an open-failure
 * notice, the roster re-read, the first-greeting request, a team view's inbox cards
 * behind its thread POST and slot-detail read, the Dashboard tab's own
 * member-panel read). `renderPage`
 * returns once the roster fetch has been ISSUED; the pane sits behind a real
 * chain after that -- members resolve, the roster commits, the open (a
 * remembered restore, a ?member= URL, a click, or the crewmate a create hands
 * over) POSTs `memberThread`, that resolves, and the pane mounts. Under load (a
 * shared host, coverage instrumentation) that ran past the 1000ms default in one
 * of four full runs; a named ceiling, not a longer guess --
 * website/docs/testing.md. It goes in a findBy's THIRD argument: the second is
 * matcher options, and a timeout there is ignored.
 */
export const PANE_READY = namedCeiling('PANE_READY', 5000)

/**
 * Put setTimeout/clearTimeout on a FAKE clock for a create whose read never
 * answers. NewCrewmateDialog races that read against a setTimeout of
 * CACHE_WARM_BOUND_MS (the warm-up) or RECONCILE_BOUND_MS (the reconcile), so a
 * case can stand one millisecond short of the bound and then on it. Install it
 * once the page has settled and before the submit that arms the bound. React
 * Query hands results to React on a setTimeout(0), which this clock would hold,
 * so those run as microtasks meanwhile. Until `realCreateClock`, step with
 * `stepCreateClock`, never waitFor/findBy: Testing Library's waits do not
 * advance vitest's fake clock.
 */
export function fakeCreateClock() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }) // flake-ok: every test file that calls this runs realCreateClock after each case
  notifyManager.setScheduler(queueMicrotask)
}
export function realCreateClock() {
  vi.useRealTimers()
  notifyManager.setScheduler(defaultScheduler)
}
/** Run `action`, then move the fake clock on by `ms`, letting all it settles render. */
export const stepCreateClock = (ms: number, action: () => void = () => {}) =>
  act(async () => { action(); await vi.advanceTimersByTimeAsync(ms) })

/** Renders the page at the URL and lets the roster load. `thread` replaces
 *  the thread-endpoint mock BEFORE mount: a remembered member (or a
 *  ?member= URL) opens a thread as soon as the roster is in, so a mock
 *  installed after render would miss that first POST. A fresh visit with
 *  nothing remembered opens no one (#11763). */
export async function renderPage(
  members = [row()],
  defaultAgent = 'kirocrew',
  {
    route = '/members',
    thread,
    queryDefaults,
    reportRosterBar,
  }: { route?: string; thread?: Record<string, unknown> | Error; queryDefaults?: Record<string, unknown>; reportRosterBar?: (showsRoster: boolean) => void } = {},
) {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
    members,
    default_agent: defaultAgent,
  })
  const threadMock = api.memberThread as ReturnType<typeof vi.fn>
  if (thread instanceof Error) threadMock.mockRejectedValue(thread)
  else if (thread) threadMock.mockResolvedValue(thread)
  else threadMock.mockImplementation(echoThread)
  const utils = renderWithProviders(
    <MobileRosterBarContext.Provider value={reportRosterBar ?? null}>
    <NavigationLeaveGuardProvider>
      <MembersPage />
      <LocationProbe />
      <LeaveProbe />
      <RailProbe />
      <GuideNavProbe />
    </NavigationLeaveGuardProvider>
    </MobileRosterBarContext.Provider>,
    { route, queryDefaults },
  )
  await waitFor(() => expect(api.members).toHaveBeenCalled())
  return utils
}

/** Exposes the router's current search string, so tests can assert the URL
 *  the page writes without reaching into MemoryRouter. */
export function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="location-probe">{loc.pathname + loc.search}</div>
}
export const currentUrl = () => screen.getByTestId('location-probe').textContent

/** Stands in for a guide's `enter`: a navigation to the create link whose
 *  leave the guide's own `guardedLeave` already asked (`leaveGranted`), or,
 *  without it, one nothing asked. */
function GuideNavProbe() {
  const nav = useNavigate()
  const to = '/members?create=1&name=scout&goal=Watch%20the%20train'
  return (
    <>
      <button type="button" data-testid="guide-nav-granted" onClick={() => nav(to, { state: { leaveGranted: true } })}>guide nav (asked)</button>
      <button type="button" data-testid="guide-nav-plain" onClick={() => nav(to)}>guide nav (unasked)</button>
      <button type="button" data-testid="guide-nav-blank" onClick={() => nav('/members?create=1')}>guide nav (blank draft)</button>
    </>
  )
}

/** Stands in for the rail's Crewmates row, exactly `NavItem`'s sequence: when
 *  the address changes it asks the page and, allowed, navigates to `/members`
 *  carrying `leaveGranted`; when the address is already `/members` it asks
 *  nothing and navigates bare. `rail-members-raw` is a push nobody guarded (a
 *  command-bar row's plain `navigate('/members')`). */
function RailProbe() {
  const mayLeave = useMayLeaveForNavigation()
  const navigate = useNavigate()
  const loc = useLocation()
  const current = loc.pathname + loc.search === '/members'
  return (
    <>
      <button type="button" data-testid="rail-members" onClick={() => {
        if (current) { navigate('/members'); return }
        if (mayLeave()) navigate('/members', { state: { leaveGranted: true } })
      }}>Crewmates</button>
      <button type="button" data-testid="rail-members-raw" onClick={() => navigate('/members')}>Crewmates (unguarded)</button>
    </>
  )
}

/** Stands in for an app-shell navigation surface (sidebar, palette, Back): asks
 *  the page's registered leave guard and records the answer. */
function LeaveProbe() {
  const mayLeave = useMayLeaveForNavigation()
  const [answer, setAnswer] = useState('')
  return (
    <button type="button" data-testid="leave-probe" onClick={() => setAnswer(String(mayLeave()))}>
      {answer}
    </button>
  )
}

/* The open member's name also renders in the thread header (and the panel's identity row),
 * so a bare screen query by name is ambiguous once a member is open (a click,
 * a remembered restore, or a ?member= URL). Scope name lookups to the roster
 * column. */
export const roster = () => within(screen.getByTestId('member-roster'))
/* The roster rows are the members-resolve -> roster-commit leg of the chain
 * PANE_READY names, so they share its ceiling. */
export const rosterRow = async (name: string) =>
  within(await screen.findByTestId('member-roster')).findByText(name, undefined, PANE_READY)

/* The roster header's "+" is a menu whose only door is New crewmate while
 * team creation is hidden for the phase. Radix opens the dropdown on
 * pointerdown (mouse), not click, so every case that wants the crewmate item
 * goes through here. Returns the item, which carries the create hold
 * (`aria-disabled` + the reason written under its label). */
export const openAddMenu = async () => {
  fireEvent.pointerDown(screen.getByTestId('member-add'), { button: 0, ctrlKey: false, pointerType: 'mouse' })
  return await screen.findByTestId('member-add-crewmate')
}
/* Closes an open "+" menu (the trigger's pointerdown TOGGLES, so a second
 * open on an open menu would close it instead), and waits for it to go. */
export const closeAddMenu = async () => {
  fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
  await waitFor(() => expect(screen.queryByTestId('member-add-menu')).toBeNull())
}
/* Selects New crewmate from the "+" menu: what one click on the old button
 * did. Radix closes the menu on select. */
export const clickAddCrewmate = async () => {
  fireEvent.click(await openAddMenu())
}
/* The "+" menu's New crewmate, then the card's own Advanced settings toggle:
 * the "+" menu holds no Advanced row of its own. */
export const clickAddAdvanced = async () => {
  await clickAddCrewmate()
  const toggle = screen.queryByTestId('crewmate-create-advanced-toggle')
  if (toggle && toggle.getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle)
}
/* Unfolds the card's Advanced settings unless the door already did. */
export const unfoldAdvanced = () => {
  const toggle = screen.getByTestId('crewmate-create-advanced-toggle')
  if (toggle.getAttribute('aria-expanded') !== 'true') fireEvent.click(toggle)
}
/* Reads the New crewmate item's hold and closes the menu again: `held` is the
 * item's `aria-disabled` (a Radix item has no DOM `disabled` for
 * `toBeDisabled` to read), `reason` the text written under its label, `null`
 * while the item is live. */
export const probeAddCrewmate = async () => {
  const item = await openAddMenu()
  const held = item.getAttribute('aria-disabled') === 'true'
  const reason = screen.queryByTestId('member-add-crewmate-held')?.textContent ?? null
  await closeAddMenu()
  return { held, reason }
}
/* Waits for the New crewmate item to reach `held` (the hold is released at
 * the follow-up's terminal points, so this is the cases' synchronisation
 * point), watching the one open menu rather than toggling it per attempt. */
export const waitForAddCrewmate = async (held: boolean) => {
  const item = await openAddMenu()
  await waitFor(() => expect(item.getAttribute('aria-disabled') === 'true').toBe(held), PANE_READY)
  await closeAddMenu()
}

/** Put every api mock back to its factory default. A clear keeps each mock's
 *  persistent and queued-once implementations, so a rejection one case installs
 *  -- on the drawer's reads, the patrol registry, the editor roster -- would
 *  reach every later case, and a once-answer it never consumes would be served
 *  to the next case's first call. `members` and `memberThread` have no factory
 *  default, so after the reset they answer undefined until `renderPage`, or the
 *  case itself, seeds them. */
export function resetApiMocks() {
  const owned = [api, api.teams] as unknown as Array<Record<string, unknown>>
  for (const group of owned) {
    for (const fn of Object.values(group)) {
      if (vi.isMockFunction(fn)) fn.mockReset()
    }
  }
}
