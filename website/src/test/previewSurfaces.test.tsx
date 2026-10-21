/**
 * Preview-flag gating for unreleased surfaces.
 *
 * The contract under test is "an unpolished surface is not advertised anywhere",
 * which is a claim about EVERY consumer of the surface registry, not about one
 * component. So this file pins the gate at each door a user could walk through:
 * the storage primitive, the registry predicate, the Search Everywhere Pages
 * provider, and the Settings > Developer > Feature Previews toggle that opens
 * them all. A test that only covered the nav rail would have passed while the
 * palette still shipped a one-keystroke path to the same page.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ReactElement } from 'react'
import { render, screen, renderHook, act, cleanup, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation, useNavigationType } from 'react-router-dom'

// The Developer panel's pages are heavy and irrelevant here — the last
// describe only needs the old route's redirect.
vi.mock('../pages/LogsPage', () => ({ LogViewer: () => <div /> }))
vi.mock('../pages/SystemPage', () => ({ default: () => <div /> }))
vi.mock('../pages/TelemetryPanel', () => ({ default: () => <div /> }))
vi.mock('../pages/SessionArchive', () => ({ default: () => <div /> }))
vi.mock('../pages/LocalStorageDebug', () => ({ default: () => <div /> }))
vi.mock('../pages/settings/McpManagement', () => ({ McpManagement: () => <div /> }))
vi.mock('../pages/overview', () => ({
  KiroCrewCfgTab: () => <div data-testid="kirocrew-cfg" />,
  AgentCfgTab: () => <div />,
}))
vi.mock('../pages/overview/MemoryGraphTab', () => ({ default: () => <div /> }))

import {
  registerBuiltinSurface,
  getBuiltinSurfaces,
  getBuiltinSurface,
  getAdvertisedSurfaces,
  selectAllSurfacesAttention,
  surfacePreviewEnabled,
  _resetBuiltinsForTest,
} from '../surfaces/registry'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
// Side-effect import: registers the real surfaces, which the crew describe
// asserts against. Every describe that needs a clean registry already calls
// `_resetBuiltinsForTest()` in its own `beforeEach`.
import '../surfaces/builtins'
import {
  PREVIEW_FLAG_EVENT,
  PREVIEW_FLAG_PREFIX,
  PREVIEW_INSTANCE_SESSIONS,
  PREVIEW_REMOTE_CREW_CHAT,
  PREVIEW_WEBHOOKS,
  readPreviewFlag,
  setPreviewFlag,
} from '../utils/previewFlags'
import { usePreviewFlag, usePreviewFlagRevision } from '../hooks/usePreviewFlag'
import { createPagesProvider } from '../components/commandPalette/providers/pagesProvider'
import { api } from '../api/client'
import { FeaturePreviewsSection, FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR } from '../pages/settings/FeaturePreviewsSection'
import { __resetPreviewFlagServerSyncForTests, usePreviewFlagServerSync } from '../hooks/usePreviewFlagServerSync'
import DeveloperPage from '../pages/DeveloperPage'

const TEST_ICON: ReactElement = <span />
const GATED_FLAG = `${PREVIEW_FLAG_PREFIX}test-surface`

afterEach(() => {
  cleanup()
  localStorage.clear()
})

describe('preview flag storage', () => {
  it('fails CLOSED: absent, "0", and junk all read as off', () => {
    // The whole point of the gate is that a surface stays hidden unless someone
    // deliberately opted in, so anything other than the exact opt-in is off.
    expect(readPreviewFlag(GATED_FLAG)).toBe(false)
    localStorage.setItem(GATED_FLAG, '0')
    expect(readPreviewFlag(GATED_FLAG)).toBe(false)
    localStorage.setItem(GATED_FLAG, 'true')
    expect(readPreviewFlag(GATED_FLAG)).toBe(false)
    localStorage.setItem(GATED_FLAG, '1')
    expect(readPreviewFlag(GATED_FLAG)).toBe(true)
  })

  it('persists and announces a change in one call', () => {
    const seen: Array<{ key: string; on: boolean }> = []
    const listener = (e: Event) => seen.push((e as CustomEvent<{ key: string; on: boolean }>).detail)
    window.addEventListener(PREVIEW_FLAG_EVENT, listener)
    try {
      expect(setPreviewFlag(GATED_FLAG, true)).toBe(true)
      expect(localStorage.getItem(GATED_FLAG)).toBe('1')
      expect(setPreviewFlag(GATED_FLAG, false)).toBe(true)
      expect(localStorage.getItem(GATED_FLAG)).toBe('0')
    } finally {
      window.removeEventListener(PREVIEW_FLAG_EVENT, listener)
    }
    expect(seen).toEqual([
      { key: GATED_FLAG, on: true },
      { key: GATED_FLAG, on: false },
    ])
  })

  it('stays SILENT when the write is dropped', () => {
    // Storage can refuse a write (denied in a locked-down embedding context,
    // or an exhausted quota that survives reclaim). Announcing one anyway is
    // what would make the toggle render ON while the rail and Search
    // Everywhere — which read storage directly — stayed empty, and the
    // "preference" would be gone on the next reload.
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage denied')
    })
    const seen: Event[] = []
    const listener = (e: Event) => seen.push(e)
    window.addEventListener(PREVIEW_FLAG_EVENT, listener)
    try {
      expect(setPreviewFlag(GATED_FLAG, true)).toBe(false)
      expect(seen).toEqual([])
    } finally {
      window.removeEventListener(PREVIEW_FLAG_EVENT, listener)
      spy.mockRestore()
    }
    // The reader is the source of truth, and it never saw the value.
    expect(readPreviewFlag(GATED_FLAG)).toBe(false)
    expect(surfacePreviewEnabled({ previewFlag: GATED_FLAG })).toBe(false)
  })

  it('keeps every flag under the shared prefix', () => {
    // Cross-tab listeners match on the prefix rather than a list of known flags,
    // so a flag named outside it would silently stop updating other tabs.
    for (const flag of [PREVIEW_WEBHOOKS, PREVIEW_REMOTE_CREW_CHAT, PREVIEW_INSTANCE_SESSIONS]) {
      expect(flag.startsWith(PREVIEW_FLAG_PREFIX)).toBe(true)
    }
  })
})

describe('surfacePreviewEnabled', () => {
  it('always advertises a surface with no preview flag', () => {
    expect(surfacePreviewEnabled({})).toBe(true)
  })

  it('advertises a gated surface only while its flag is on', () => {
    expect(surfacePreviewEnabled({ previewFlag: GATED_FLAG })).toBe(false)
    localStorage.setItem(GATED_FLAG, '1')
    expect(surfacePreviewEnabled({ previewFlag: GATED_FLAG })).toBe(true)
  })
})

/**
 * Crew, asserted against the REAL registry rather than a fixture.
 *
 * Declared before the `registry membership` block below, which resets the
 * registry in its `beforeEach` and would take the imported builtins with it.
 * Vitest runs describes in declaration order, so this position is the fixture.
 */
describe('crew is advertised with no opt-in', () => {
  it('carries no preview flag on the Crew Members surface', () => {
    expect(getBuiltinSurface('members')?.previewFlag).toBeUndefined()
  })

  it('lists Crew Members in the advertised surfaces on a default install', () => {
    expect(getAdvertisedSurfaces().some(s => s.navId === 'members')).toBe(true)
    expect(getBuiltinSurfaces().find(s => s.navId === 'members')?.route).toBe('/members')
  })
})

describe('browser-tab attention count', () => {
  // The tab title is an ADVERTISEMENT: a gated surface has no rail row to trace
  // a count to, so contributing one shows the user a `(1)` they cannot clear.
  // Pinned here rather than in `surfaces.test.tsx` because it is part of the
  // "not advertised ANYWHERE" contract, not of the sum's arithmetic.
  beforeEach(() => _resetBuiltinsForTest())

  const buildState = (slots: unknown[], unread: string[]) => {
    const initialDashboard = dashboardReducer(undefined, { type: '@@INIT' })
    return {
      dashboard: { ...initialDashboard, slots, unreadSlots: unread },
      notifications: notificationsReducer(undefined, { type: '@@INIT' }),
    } as unknown as Parameters<typeof selectAllSurfacesAttention>[0]
  }

  const registerPair = () => {
    registerBuiltinSurface({
      navId: 'open', route: '/open', label: 'Open', labelKey: 'nav.sessions',
      icon: TEST_ICON, group: 'Main', unreadSelector: () => 2,
    })
    registerBuiltinSurface({
      navId: 'gated', route: '/gated', label: 'Gated', labelKey: 'nav.webhooks',
      icon: TEST_ICON, group: 'Main', unreadSelector: () => 5, previewFlag: GATED_FLAG,
    })
  }

  it('omits a gated surface while its flag is off, and counts it once on', () => {
    registerPair()
    const state = buildState([], [])
    // 2, not 7: the ungated neighbour is what proves the sum ran at all.
    expect(selectAllSurfacesAttention(state)).toBe(2)
    localStorage.setItem(GATED_FLAG, '1')
    expect(selectAllSurfacesAttention(state)).toBe(7)
  })

  it('still counts a hiddenFromNav surface, which IS advertised elsewhere', () => {
    // The deliberate opposite of the gate above: `hiddenFromNav` means "rendered
    // somewhere other than the rail" (the topbar bell), so its count has an
    // owner the user can reach and must keep reaching the tab title.
    registerBuiltinSurface({
      navId: 'bell', route: '/bell', label: 'Bell', labelKey: 'nav.notifications',
      icon: TEST_ICON, group: 'Main', unreadSelector: () => 4, hiddenFromNav: true,
    })
    expect(selectAllSurfacesAttention(buildState([], []))).toBe(4)
  })
})

describe('registry membership', () => {
  beforeEach(() => _resetBuiltinsForTest())

  const registerBoth = () => {
    registerBuiltinSurface({
      navId: 'open', route: '/open', label: 'Open', labelKey: 'nav.sessions',
      icon: TEST_ICON, group: 'Main',
    })
    registerBuiltinSurface({
      navId: 'gated', route: '/gated', label: 'Gated', labelKey: 'nav.webhooks',
      icon: TEST_ICON, group: 'Main', previewFlag: GATED_FLAG,
    })
  }

  it('keeps a gated surface IN getBuiltinSurfaces', () => {
    // Filtering it out of that list would retire the registry-wide invariants
    // (e.g. "every surface carries a translatable labelKey") for exactly the
    // surfaces still being built. Visibility is a separate question.
    registerBoth()
    expect(getBuiltinSurfaces().map(s => s.navId)).toEqual(['open', 'gated'])
  })

  it('drops it from getAdvertisedSurfaces until the flag is on', () => {
    // The two lists answering different questions is the point: this is the one
    // a consumer may SHOW, so a call site that reaches for it cannot forget the
    // filter and leak an unreleased surface.
    registerBoth()
    expect(getAdvertisedSurfaces().map(s => s.navId)).toEqual(['open'])
    localStorage.setItem(GATED_FLAG, '1')
    expect(getAdvertisedSurfaces().map(s => s.navId)).toEqual(['open', 'gated'])
  })
})

describe('Search Everywhere Pages provider', () => {
  beforeEach(() => {
    _resetBuiltinsForTest()
    // No `labelKey` on either fixture: `surfaceLabel()` then falls through to the
    // literal label, which is what the provider fuzzy-matches against. Pointing
    // them at real catalog keys would make both searches miss for a reason that
    // has nothing to do with the gate. The provider reads
    // `getAdvertisedSurfaces()`, so this exercises the real registry filter.
    registerBuiltinSurface({
      navId: 'open', route: '/open', label: 'Openly Visible',
      icon: TEST_ICON, group: 'Main',
    })
    registerBuiltinSurface({
      navId: 'gated', route: '/gated', label: 'Gated Surface',
      icon: TEST_ICON, group: 'Main', previewFlag: GATED_FLAG,
    })
  })

  const routesFor = (query: string) =>
    createPagesProvider(vi.fn()).search(query).map(r => r.id)

  it('omits a gated surface while its flag is off', () => {
    expect(routesFor('gated')).toEqual([])
    // The ungated neighbour proves the provider itself still works, so an empty
    // result above cannot be a broken fixture.
    expect(routesFor('openly')).toEqual(['pages:open'])
  })

  it('includes it once the flag is on', () => {
    localStorage.setItem(GATED_FLAG, '1')
    expect(routesFor('gated')).toEqual(['pages:gated'])
  })
})

describe('usePreviewFlag', () => {
  it('reads the persisted flag on mount', () => {
    localStorage.setItem(GATED_FLAG, '1')
    const { result } = renderHook(() => usePreviewFlag(GATED_FLAG))
    expect(result.current).toBe(true)
  })

  it('updates on a same-tab change and ignores other flags', () => {
    const { result } = renderHook(() => usePreviewFlag(GATED_FLAG))
    expect(result.current).toBe(false)
    act(() => setPreviewFlag(`${PREVIEW_FLAG_PREFIX}something-else`, true))
    expect(result.current).toBe(false)
    act(() => setPreviewFlag(GATED_FLAG, true))
    expect(result.current).toBe(true)
  })

  it('reacts to a cross-tab storage event on its own key', () => {
    const { result } = renderHook(() => usePreviewFlag(GATED_FLAG))
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: GATED_FLAG, newValue: '1' }))
    })
    expect(result.current).toBe(true)
  })
})

describe('usePreviewFlagRevision', () => {
  it('changes on ANY preview flag change without naming one', () => {
    // This is what keeps the nav rail live: it renders whole lists and decides
    // visibility per item, so it must not have to know which flags exist. The
    // number is also a memo dep — a bare re-render would not recompute the
    // memoized Apps-group list.
    const seen: number[] = []
    renderHook(() => { seen.push(usePreviewFlagRevision()) })
    const before = seen[seen.length - 1]
    act(() => setPreviewFlag(GATED_FLAG, true))
    expect(seen[seen.length - 1]).not.toBe(before)
    const afterEvent = seen[seen.length - 1]
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: `${PREVIEW_FLAG_PREFIX}other`, newValue: '1' }))
    })
    expect(seen[seen.length - 1]).not.toBe(afterEvent)
  })

  it('ignores unrelated storage keys', () => {
    const seen: number[] = []
    renderHook(() => { seen.push(usePreviewFlagRevision()) })
    const before = seen[seen.length - 1]
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: 'mc-apps-expanded', newValue: '1' }))
    })
    expect(seen[seen.length - 1]).toBe(before)
  })
})

describe('Settings > Developer > Feature Previews', () => {
  // One card in this section — Decisions — is backed by gateway state rather
  // than a `previewFlags.ts` key: whether it is drawn at all reads
  // `decisions_enabled` off `['dashboardConfig']` (the `capabilities.decisions`
  // ceiling), its switch reads the consent keystone and its share reads
  // `['kirocrewConfig']`. All three stubbed here rather than left to reach
  // the network: every case below is about the four localStorage previews, and a
  // real read cannot succeed under vitest (a failed one would render an
  // ErrorNotice with its own "Ask the agent" link and pollute the link census).
  // `decisionsCard.test.tsx` owns that card's own states.
  beforeEach(() => {
    vi.spyOn(api, 'dashboardConfig').mockResolvedValue({ decisions_enabled: true } as never)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({} as never)
    vi.spyOn(api, 'getDecisionsConsent').mockResolvedValue({
      enabled: false, endpoint: '', configured_endpoint: 'https://api.typesafe.ai/v1/systemone', permits: false,
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  const renderTab = () =>
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter><FeaturePreviewsSection /></MemoryRouter>
      </QueryClientProvider>,
    )

  /** `aria-checked` via the ATTRIBUTE: the Toggle is a `div role="switch"`, and
   *  the reflected `ariaChecked` DOM property is not populated for one. */
  const toggleState = () =>
    screen.getByRole('switch', { name: /webhooks/i }).getAttribute('aria-checked')

  it('starts off and offers no way into the hidden page', () => {
    renderTab()
    expect(toggleState()).toBe('false')
    expect(screen.queryByRole('button', { name: /open webhooks/i })).toBeNull()
  })

  it('persists the opt-in and then links to the page', async () => {
    renderTab()
    await act(async () => {
      screen.getByRole('switch', { name: /webhooks/i }).click()
    })
    expect(localStorage.getItem(PREVIEW_WEBHOOKS)).toBe('1')
    expect(screen.getByRole('button', { name: /open webhooks/i })).toBeTruthy()
  })

  it('reflects an opt-in made in another tab', () => {
    localStorage.setItem(PREVIEW_WEBHOOKS, '1')
    renderTab()
    expect(toggleState()).toBe('true')
  })

  it('says why a switch went back when the gateway refuses the write', async () => {
    __resetPreviewFlagServerSyncForTests()
    vi.spyOn(api, 'patchConfig').mockRejectedValue(Object.assign(new Error('owner only'), { status: 403 }))
    function WithSync() {
      usePreviewFlagServerSync()
      return <FeaturePreviewsSection />
    }
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MemoryRouter><WithSync /></MemoryRouter>
      </QueryClientProvider>,
    )
    expect(screen.queryByTestId('feature-previews-save-error')).toBeNull()
    await act(async () => {
      screen.getByRole('switch', { name: /webhooks/i }).click()
    })
    await waitFor(() => expect(screen.getByTestId('feature-previews-save-error').textContent)
      .toMatch(/only the owner of this Kiro Crew can change feature previews, so “Webhooks” was switched back/i))
    act(() => { __resetPreviewFlagServerSyncForTests() })
  })

  it('offers no ingress button besides the webhooks link', async () => {
    // `webhooks` is `hiddenFromNav`, so its card is that page's ONLY door and
    // carries a link once on. No other card here does.
    //
    // Counted as `<button>` ELEMENTS rather than by accessible name: the name of
    // a link that no longer exists is not in any catalog, so a name query could
    // never fail. `SettingsToggle`'s own row is a `div role="button"`, so it is
    // correctly not counted here. The "See what it looks like" button each card
    // may carry (`FeaturePreviewIntroButton`) is excluded by its test id: it is
    // not an ingress — it opens an explainer dialog, never the page — and it is
    // present on either side of the toggle by design.
    const { container } = renderTab()
    /**
     * The Decisions card is INCLUDED, subtree and all.
     *
     * In the state this test renders — governance permitting, consent off, no stored
     * credential — that card draws no buttons at all: its point rows are
     * `role="tab"`, and the credential's reveal and replace controls exist only once a
     * key is stored. So excluding its frame subtracted an empty set and left the census
     * blind to the largest card on the tab for no benefit.
     *
     * Counting it is what makes this a ratchet rather than decoration: a button
     * appearing anywhere in that subtree in this state fails here, which is exactly the
     * ingress drift the census exists to catch. A future state that legitimately draws
     * one belongs in `decisionsCard.test.tsx`, which pins the card's own states.
     */
    const realButtons = () =>
      Array.from(
        container.querySelectorAll('button:not([data-testid="feature-preview-intro-button"])'),
      )
    /**
     * The Decisions card is WALKED, not allowed for.
     *
     * Its subtree is asserted as an exact set, so a button appearing there fails here --
     * and so does a second copy of one already expected. A name filter over the whole
     * tab would have done neither: it went unscoped, so the same name on another card
     * disappeared from the census too, and it bounded no count.
     *
     * The two it draws once settled: the hand-off on its read-failure notice (this tab
     * stubs no API, so the card's reads fail here) and the credential field's reveal.
     * Neither navigates. A third belongs in the expected set with a reason, never behind
     * a blanket allowance -- and because the set is exact, a duplicate of one of these
     * fails too, which a name filter could not catch.
     */
    const nameOf = (b: Element) =>
      (b.textContent?.trim() || b.getAttribute('aria-label') || '?').trim()
    // SettingsCard is borderless now (no `.card-glow`); its root carries
    // `animate-rise`, which is the enclosing card frame the switch sits in.
    const decisionsFrame = () =>
      screen.queryByRole('switch', { name: 'Decisions (Jev)' })?.closest('.animate-rise') ?? null
    const decisionsButtons = () =>
      Array.from(
        decisionsFrame()?.querySelectorAll(
          'button:not([data-testid="feature-preview-intro-button"])',
        ) ?? [],
      )
        .map(nameOf)
        .sort()
    /**
     * The built-in KAS engine card is WALKED the same way. This tab stubs no API, so
     * its status read fails and it draws the read-failure notice's Retry and the
     * hand-off; neither navigates. Its Install button exists only once a status
     * says nothing is installed, which `KasBundleCard.test.tsx` pins.
     */
    const kasFrame = () =>
      container.querySelector('[data-testid="kas-bundle-card"]')?.closest('.animate-rise') ?? null
    const kasButtons = () =>
      Array.from(kasFrame()?.querySelectorAll('button') ?? [])
        .map(nameOf)
        .sort()
    // Outside those cards, unfiltered and unchanged: this is the ingress property.
    const ingressButtons = () =>
      realButtons()
        .filter(el => !decisionsFrame()?.contains(el) && !kasFrame()?.contains(el))
        .map(nameOf)
        .sort()
    // Every button outside the Decisions card is observed and partitioned:
    // the rows' InfoTip help toggles (named 'More information'; they open a
    // tooltip, never a page) are counted exactly, and whatever is left is the
    // ingress census proper. The partition keeps the ratchet exhaustive --
    // an unexpected button lands in `ingress`, an extra or missing tip
    // changes `tips` -- while saying what each half protects.
    const INFO_TIP = 'More information'
    const INFO_TIP_COUNT = 1 // one per Feature Previews row carrying a `hint`
    const partition = () => {
      const all = ingressButtons()
      return { tips: all.filter(n => n === INFO_TIP).length, ingress: all.filter(n => n !== INFO_TIP) }
    }
    expect(partition()).toEqual({ tips: INFO_TIP_COUNT, ingress: [] })
    // The Decisions subtree, exactly. A new button here -- or a duplicate of one of
    // these -- fails, which a name allowance could not do.
    // The Decisions subtree, exactly, once its reads have settled. Awaited rather than
    // sampled: the card draws only the credential reveal until its reads land and adds
    // the hand-off on the read-failure notice after, so a bare expect reads whichever
    // moment it hit. A button this set does not name never converges, so it fails here.
    await waitFor(() => {
      expect(decisionsButtons()).toEqual(['Ask the agent', 'Show'])
    })
    await waitFor(() => {
      expect(kasButtons()).toEqual(['Ask the agent', 'Retry'])
    })
    // The webhooks card HAS its link, so the empty census above is not the
    // ingress mechanism having been broken.
    await act(async () => {
      screen.getByRole('switch', { name: /webhooks/i }).click()
    })
    expect(partition()).toEqual({ tips: INFO_TIP_COUNT, ingress: ['Open Webhooks'] })
    // And the card beside it still draws only its own two.
    expect(decisionsButtons()).toEqual(['Ask the agent', 'Show'])
  })

  it('renders the section header and the section-wide caveat once, above the cards', () => {
    // The caveat used to be the Developer-page tab's description, rendered by
    // SidePanelLayout as the page header. Inside Settings the section has to
    // carry it itself — once, not per card — or the toggles read as released
    // features that merely happen to be off.
    renderTab()
    expect(screen.getByRole('heading', { name: /feature previews/i })).toBeTruthy()
    expect(screen.getAllByText(/unpolished on purpose/i)).toHaveLength(1)
  })

  it('carries the redirect anchor on ONE element that wraps the whole section', async () => {
    // `?highlight=key:<anchor>` rings the element carrying data-setting-key.
    // The old-bookmark reader asked a section-sized question, so the ring must
    // enclose the header and every card — an anchor on a single card would
    // answer "is this row selected?" instead.
    const { container } = renderTab()
    // Awaited: the Decisions card is not drawn until the governance read
    // (`decisions_enabled`) lands, so the last switch arrives a tick late.
    await waitFor(() => {
      expect(screen.getAllByRole('switch')).toHaveLength(9)
    })
    const anchors = container.querySelectorAll(`[data-setting-key="${FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR}"]`)
    expect(anchors).toHaveLength(1)
    const anchor = anchors[0]
    expect(anchor.contains(screen.getByRole('heading', { name: /feature previews/i }))).toBe(true)
    // The count is here so a card added outside the anchor fails rather than
    // silently escaping the ring. The last is Decisions, whose switch is
    // backend config — a different write path, the same ring.
    for (const s of screen.getAllByRole('switch')) expect(anchor.contains(s)).toBe(true)
  })

  it('carries no Dynamic Dashboard card and no automatic-cards switch', async () => {
    // The Dynamic Dashboard is on for every reader, so it has no preview card.
    // Automatic cards is a server setting with its own card on the Developer tab.
    renderTab()
    await waitFor(() => { expect(screen.getAllByRole('switch')).toHaveLength(9) })
    expect(screen.queryByRole('switch', { name: /^dynamic dashboard$/i })).toBeNull()
    expect(screen.queryByRole('switch', { name: /automatic cards for all sessions/i })).toBeNull()
  })

  it('has one Chat on a crew switch for both crew halves, and no second card', async () => {
    // The crew groups in the sidebar and "New chat on crew" were two switches;
    // one on and one off let a user open a chat on a crew with no group to
    // return to. A shared write would release other surfaces too, so the
    // sibling assertions are the point rather than padding.
    renderTab()
    expect(screen.queryByRole('switch', { name: /^remote crew sessions$/i })).toBeNull()
    const toggle = () => screen.getByRole('switch', { name: /^chat on a crew$/i })
    expect(toggle().getAttribute('aria-checked')).toBe('false')
    await act(async () => { toggle().click() })
    expect(localStorage.getItem(PREVIEW_REMOTE_CREW_CHAT)).toBe('1')
    expect(toggle().getAttribute('aria-checked')).toBe('true')
    for (const other of [PREVIEW_WEBHOOKS, PREVIEW_INSTANCE_SESSIONS]) {
      expect(localStorage.getItem(other)).not.toBe('1')
    }
  })

  it('reads the retired instance-sessions key as on, and clears it when turned off', async () => {
    // A device that turned the old "Remote crew sessions" switch on keeps the
    // feature on; turning the one switch off must not leave that key holding
    // it on behind a switch that reads off.
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    renderTab()
    const toggle = () => screen.getByRole('switch', { name: /^chat on a crew$/i })
    expect(toggle().getAttribute('aria-checked')).toBe('true')
    await act(async () => { toggle().click() })
    expect(toggle().getAttribute('aria-checked')).toBe('false')
    expect(localStorage.getItem(PREVIEW_INSTANCE_SESSIONS)).not.toBe('1')
    expect(localStorage.getItem(PREVIEW_REMOTE_CREW_CHAT)).not.toBe('1')
  })

  // `/developer?tab=…` survives in bookmarks, docs, curated tips and palette
  // history. The probe reads the FINAL location AND the navigation type: a push
  // would leave the pre-move URL one Back press away. Literal URLs on purpose:
  // this is the contract old links depend on.
  function redirectOf(from: string): string {
    function LocationProbe() {
      const loc = useLocation()
      const navType = useNavigationType()
      return <div data-testid="loc">{`${navType} ${loc.pathname}${loc.search}`}</div>
    }
    render(
      <MemoryRouter initialEntries={[from]}>
        <DeveloperPage />
        <LocationProbe />
      </MemoryRouter>,
    )
    return screen.getByTestId('loc').textContent ?? ''
  }

  it('redirects the old Feature Previews link to its page, ringing the section', () => {
    expect(redirectOf('/developer?tab=feature-previews')).toBe(
      `REPLACE /settings/developer/feature-previews?highlight=key%3A${FEATURE_PREVIEWS_HIGHLIGHT_ANCHOR}`,
    )
  })

  it('redirects every other old Developer tab to the same page in Settings', () => {
    expect(redirectOf('/developer?tab=config')).toBe('REPLACE /settings/developer/config')
  })

  it('keeps the other query params a page reads', () => {
    expect(redirectOf('/developer?tab=system&plane=services')).toBe('REPLACE /settings/developer/system?plane=services')
  })

  it('redirects a trailing-slash path the route also matches', () => {
    expect(redirectOf('/developer/?tab=system')).toBe('REPLACE /settings/developer/system')
  })

  it('redirects a mixed-case path the route also matches', () => {
    expect(redirectOf('/Developer?tab=config')).toBe('REPLACE /settings/developer/config')
  })

  it('sends an unknown or absent tab to the Developer tab itself', () => {
    expect(redirectOf('/developer?tab=nope')).toBe('REPLACE /settings/developer')
  })
})
