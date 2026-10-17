/**
 * Left-nav rail filtering of PROMOTED SUB-ITEMS (`mc-nav-pinned`), pinned at
 * the App level — real nav rail, real `readNavPinned` derivation, mocked pages.
 *
 * A `pinnable` surface is registered like any other (so it keeps `labelKey`
 * coverage and ordinary rail handling) but must occupy a rail row ONLY while
 * the user has promoted it. The contract this file pins:
 *
 * - with NOTHING pinned, NO pinnable surface has a rail row. This is the
 *   regression guard for the whole feature: installing it must not change the
 *   rail a single user sees until they opt in. Asserted across EVERY pinnable
 *   surface rather than one sample, so a filter that misses a group is caught.
 * - a pinned id gets a row while an unpinned SIBLING does not — membership
 *   filters per row, not per group.
 * - the filter is LIVE: a localStorage write followed by the
 *   `mc:nav-pinned-changed` window event (the same-tab path `lib/navPinned.ts`
 *   dispatches on every persisted write) shows the row in place, no remount.
 *
 * Rows are located by `data-onboarding-nav` (the navId `NavItem` stamps), NOT
 * by their label: locating a row by the text this test is about would let it
 * pass by finding a healthy sibling that happens to render the same string,
 * and would additionally couple the assertion to catalog wording.
 *
 * Harness copied from App.appNavHiddenFilter.test.tsx — routed pages are mocked
 * so App mounts without real page trees.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import App from '../App'
import { ThemeProvider } from '../hooks/useTheme'
import { NAV_PINNED_KEY, NAV_PINNED_CHANGED_EVENT } from '../lib/navPinned'
import { getPinnableSurfaces } from '../surfaces/registry'
import '../surfaces/builtins'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page" /> }))
vi.mock('../pages/apps/DiscoverPage', () => ({ default: () => null }))
vi.mock('../pages/apps/LibraryPage', () => ({ default: () => null }))
vi.mock('../pages/AppPage', () => ({ default: () => null }))
vi.mock('../pages/AppDetailPage', () => ({ default: () => null }))
vi.mock('../pages/MigrationPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/SettingsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../pages/HooksPage', () => ({ default: () => null }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => null }))
vi.mock('../pages/KnowledgePage', () => ({ default: () => null }))
vi.mock('../pages/DeveloperPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactsPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactDetailPage', () => ({ default: () => null }))
vi.mock('../pages/ArtifactDeployPage', () => ({ default: () => null }))
vi.mock('../pages/EmbedSettingsPage', () => ({ default: () => null }))
vi.mock('../pages/PopoutFrame', () => ({ default: () => null }))
vi.mock('../pages/ArtifactPopoutFrame', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../hooks/useDashboardHealthProbe', () => ({ useDashboardHealthProbe: () => {} }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
    listApps: vi.fn().mockResolvedValue([]),
    listRegistry: vi.fn().mockResolvedValue({ apps: [], categoryOrder: [], editorialSections: [] }),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    approvals: vi.fn().mockResolvedValue([]),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class extends Error { status: number; constructor(s: number, m: string) { super(m); this.status = s } },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation(query => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
})
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

function renderApp(url = '/chat') {
  const store = configureStore({
    reducer: {
      dashboard: dashboardReducer,
      chat: chatReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter initialEntries={[url]}>
            <App />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  return qc
}

const row = (navId: string) => document.querySelector(`[data-onboarding-nav="${navId}"]`)

/** Persist a pinned set and fire the same-tab change event, as one action. */
function writePinnedAndNotify(ids: string[]) {
  act(() => {
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(ids))
    window.dispatchEvent(new Event(NAV_PINNED_CHANGED_EVENT))
  })
}

beforeEach(() => {
  localStorage.clear()
})

describe('promoted sub-items on the nav rail', () => {
  it('registers pinnable surfaces at all (control for the assertions below)', () => {
    // Without this the two "absent" assertions would pass vacuously on an
    // empty registry, proving nothing about the filter.
    const ids = getPinnableSurfaces().map(s => s.navId)
    expect(ids.length).toBeGreaterThan(0)
    expect(ids).toContain('capabilities-steering')
  })

  it('shows NO promoted row when nothing is pinned', () => {
    renderApp()
    const leaked = getPinnableSurfaces().map(s => s.navId).filter(id => row(id) !== null)
    expect(leaked, `pinnable surfaces leaked onto the rail unpinned: ${leaked.join(', ')}`).toEqual([])
  })

  it('shows a pinned sub-item and leaves an unpinned sibling off the rail', () => {
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(['capabilities-steering']))
    renderApp()
    expect(row('capabilities-steering')).not.toBeNull()
    // Complement: the pin is per row, so a sibling of the SAME host panel must
    // still be absent. Without this, a filter that showed the whole group once
    // any member was pinned would pass.
    expect(row('capabilities-skills')).toBeNull()
  })

  it('adds the row live on a same-tab write, with no remount', () => {
    renderApp()
    expect(row('capabilities-steering')).toBeNull()
    writePinnedAndNotify(['capabilities-steering'])
    expect(row('capabilities-steering')).not.toBeNull()
    // And removes it again on the reverse write, so the row tracks the set
    // rather than only ever being added.
    writePinnedAndNotify([])
    expect(row('capabilities-steering')).toBeNull()
  })

  it('leaves the ordinary rail rows exactly as they were when a pin is added', () => {
    renderApp()
    const pinnableIds = new Set(getPinnableSurfaces().map(s => s.navId))
    // The whole ordinary rail, not a sampled row: this is what says the change
    // is additive. (`settings` and `capabilities` are hand-rendered in the
    // bottom block without this attribute, so a named-row probe would have been
    // asserting about the DOM rather than about the filter.)
    const ordinaryRows = () => [...document.querySelectorAll('[data-onboarding-nav]')]
      .map(el => el.getAttribute('data-onboarding-nav') as string)
      .filter(id => !pinnableIds.has(id))
      .sort()

    const before = ordinaryRows()
    // Control: without this the comparison below would pass on two empty lists
    // if the probe selector were wrong.
    expect(before.length).toBeGreaterThan(0)
    expect(before).toContain('chat')

    writePinnedAndNotify(['capabilities-steering'])
    expect(row('capabilities-steering')).not.toBeNull()
    expect(ordinaryRows()).toEqual(before)
  })
})

describe('a promoted row paints as the current row', () => {
  // The rail's `active` state reaches the DOM only as a class on the row's icon
  // (`is-lit`); rail rows carry no aria-current, so this is the one observable.
  const isActive = (navId: string) => row(navId)?.querySelector('.is-lit') != null

  it('is active while its own tab is showing', () => {
    // The regression this guards: every other row compares a PATHNAME, and a
    // promoted row's path carries `?tab=`, so a pathname-only comparison can
    // never match and the row would never light up while you stood on it.
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(['capabilities-steering']))
    renderApp('/capabilities?tab=steering')
    expect(row('capabilities-steering')).not.toBeNull()
    expect(isActive('capabilities-steering')).toBe(true)
  })

  it('is NOT active while a SIBLING tab of the same panel is showing', () => {
    // Complement, and the case that fails if the tab param is dropped from the
    // comparison and only the pathname is matched: both rows share
    // /capabilities, so pathname alone would light the wrong one.
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(['capabilities-steering', 'capabilities-skills']))
    renderApp('/capabilities?tab=skills')
    expect(isActive('capabilities-skills')).toBe(true)
    expect(isActive('capabilities-steering')).toBe(false)
  })
  it('lights EXACTLY ONE rail row while a promoted tab is showing', () => {
    // The host row's active test is a prefix match on /capabilities and the
    // promoted row's an exact ?tab= match, so both passed and the rail painted
    // two rows as "where I am" -- caught by screenshot, not by this suite,
    // because the tests above assert only that the promoted row IS lit. Counting
    // is what turns that into a guard.
    localStorage.setItem(NAV_PINNED_KEY, JSON.stringify(['capabilities-steering']))
    renderApp('/capabilities?tab=steering')

    expect(document.querySelectorAll('[data-onboarding-nav] .is-lit')).toHaveLength(1)
    expect(isActive('capabilities-steering')).toBe(true)
  })

  it('still lights the host row when the tab it hosts is NOT promoted', () => {
    // The falsifying direction: with nothing pinned the host must keep its own
    // active paint, or the fix has merely switched the host row off for good.
    renderApp('/capabilities?tab=steering')

    expect(row('capabilities-steering')).toBeNull()
    expect(document.querySelectorAll('[data-onboarding-nav] .is-lit')).toHaveLength(1)
  })

})
