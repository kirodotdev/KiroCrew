/**
 * Pin: the legacy query-param → path translation layer on SettingsPage.
 *
 * Settings navigation moved from query params (?tab=X with the per-host
 * `channel`/`section` aliases) to path segments (/settings/<tab>/<sub>).
 * Old URLs survive in bookmarks, command-palette history and docs, so
 * SettingsPage translates any legacy navigation param found on mount into
 * the canonical path form with a REPLACE navigation. These tests pin that
 * contract at the URL level:
 *
 *   /settings?tab=channels&channel=slack&highlight=x
 *     → /settings/channels/slack?highlight=x
 *   /settings?tab=security&section=tailnet&highlight=y
 *     → /settings/security/tailnet?highlight=y
 *
 * and that the translation REPLACES the legacy entry — back must land on
 * whatever preceded the legacy URL, never on the pre-translation URL itself.
 * Non-navigation params (`highlight`) ride along untouched.
 *
 * SettingsPage is mounted under the same `/settings/*` splat shape App.tsx
 * uses, so the page unmounts when back leaves /settings — exactly the
 * production condition that keeps SidePanelLayout's remembered-tab restore
 * from rewriting a foreign route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, act, cleanup } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom'
import type { Location, NavigateFunction } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

// These tests assert the URL, not panel internals — stub every panel the two
// scenarios can render (the pre-translation frame shows the default Overview
// tab for one tick, then Channels / Security after the segments land).
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/ChannelsPanel', () => ({
  ChannelsPanel: () => <div data-testid="channels-panel" />,
  // SettingsPage's translation reads CHANNEL_KEYS only for the oldest
  // per-channel form (?tab=slack). These tests use ?tab=channels, so the
  // array's contents never decide an assertion — it just has to exist.
  CHANNEL_KEYS: ['slack', 'discord', 'telegram', 'webex', 'wecom', 'teams'],
}))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: () => <div data-testid="security-panel" /> }))
vi.mock('../pages/settings/SettingsSearch', () => ({ default: () => null }))
// The highlight hook strips ?highlight= after applying it (immediately, for an
// id the registry does not know). The contract under test is the TRANSLATION
// layer carrying highlight across untouched, so the hook must not race the
// search assertion — its own strip behavior is pinned by its own tests.
vi.mock('../hooks/useSettingHighlight', () => ({ useSettingHighlight: () => {} }))
vi.mock('../store', () => ({ useAppSelector: () => '1.0.0' }))

// SidePanelLayout → useIsMobile reads window.matchMedia at module load; jsdom lacks it.
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

import SettingsPage from '../pages/SettingsPage'

// Router state observed from OUTSIDE the /settings route, so it keeps
// reporting after back navigates away and SettingsPage unmounts.
const probe: { location: Location | null; navigate: NavigateFunction | null } = {
  location: null,
  navigate: null,
}
function RouterProbe() {
  probe.location = useLocation()
  probe.navigate = useNavigate()
  return null
}

function renderAt(entries: string[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={entries} initialIndex={entries.length - 1}>
        <Routes>
          {/* Same splat shape App.tsx registers — the page owns the segments. */}
          <Route path="/settings/*" element={<SettingsPage />} />
          <Route path="*" element={null} />
        </Routes>
        <RouterProbe />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  probe.location = null
  probe.navigate = null
  // SidePanelLayout remembers the shown tab per rememberKey; a tab remembered
  // by one test must not decide another test's pre-translation frame.
  sessionStorage.clear()
})

describe('SettingsPage legacy query URL → path translation', () => {
  it('renders the LEGACY tab on the very first frame — no default-tab flash before translation', () => {
    // The translation runs as a passive effect, so the first paint happens on
    // the untranslated URL. The read-side fallback must already resolve the
    // legacy tab there: a synchronous query straight after render() observes
    // the pre-effect frame. Rendering the default tab here is the regression
    // the i18n render gate caught (default-tab text attributed to the linked
    // surface). Tab DESCRIPTIONS are the markers — labels all sit in the rail
    // regardless of which pane is active.
    const { container } = renderAt(['/settings?tab=chat'])
    expect(container.textContent).toContain('Message behavior, history, timestamps, and context')
    expect(container.textContent).not.toContain('System health, activity, and usage & memory at a glance')
  })

  it('resolves the tab from a legacy ALIAS link on the first frame too (?tab=channels&channel=slack)', () => {
    // The channels tab header is up immediately — not the default Overview.
    // (The slack PANE's content is data-gated and asserted in the panel's own
    // suite; this pins the shell-level first frame.)
    const { container } = renderAt(['/settings?tab=channels&channel=slack'])
    expect(container.textContent).toContain('Chat platforms the agent can send and receive on')
    expect(container.textContent).not.toContain('System health, activity, and usage & memory at a glance')
  })

  it('replace-navigates ?tab=channels&channel=slack&highlight=x to /settings/channels/slack?highlight=x', async () => {
    renderAt(['/chat', '/settings?tab=channels&channel=slack&highlight=x'])

    await waitFor(() => expect(probe.location?.pathname).toBe('/settings/channels/slack'))
    // The navigation params are consumed; highlight rides along untouched.
    expect(probe.location?.search).toBe('?highlight=x')

    // REPLACE, not push: back lands on the entry that preceded the legacy
    // URL — never on the pre-translation query form.
    act(() => probe.navigate!(-1))
    await waitFor(() => expect(probe.location?.pathname).toBe('/chat'))
    expect(probe.location?.search).toBe('')
  })

  it('translates the Security ?section alias: ?tab=security&section=tailnet&highlight=y → /settings/security/tailnet?highlight=y', async () => {
    renderAt(['/settings?tab=security&section=tailnet&highlight=y'])

    await waitFor(() => expect(probe.location?.pathname).toBe('/settings/security/tailnet'))
    expect(probe.location?.search).toBe('?highlight=y')
  })

  it('a present-but-empty ?sub= silences the aliases and preserves the path selection', async () => {
    // Canonical `sub` wins BY PRESENCE (the read-path precedence): with
    // ?sub=&channel=slack the empty canonical param silences the alias, its
    // empty value normalizes to absent, and the existing path segments win —
    // the alias must NOT override a valid path deep link through the gap.
    renderAt(['/settings/channels/discord?sub=&channel=slack'])
    await waitFor(() => expect(probe.location?.search).toBe(''))
    expect(probe.location?.pathname).toBe('/settings/channels/discord')
  })

  it('alias precedence is presence-based between aliases too: ?channel=&section=rules yields no selection', async () => {
    // The FIRST present alias wins even when empty (mirroring the SubNav read
    // path's `find(v => v != null)`): an empty ?channel= silences ?section=,
    // so the security pane is NOT selected through the gap. A value-based
    // `find(v => v)` would translate this to /settings/security/rules.
    renderAt(['/settings/security?channel=&section=rules'])
    await waitFor(() => expect(probe.location?.search).toBe(''))
    expect(probe.location?.pathname).toBe('/settings/security')
  })

  it('an EMPTY legacy param on a path deep link is stripped without erasing the path state', async () => {
    // ?tab= / ?channel= with no value is a degenerate link, not navigation
    // intent. Two regressions this pins: (a) treating "present but empty" as
    // present replace-navigated /settings/channels/slack?tab= to the bare
    // /settings, destroying both segments; (b) ignoring it entirely left a
    // permanent hybrid URL that every subsequent navigation carried along.
    renderAt(['/settings/channels/slack?tab='])
    await waitFor(() => expect(probe.location?.search).toBe(''))
    expect(probe.location?.pathname).toBe('/settings/channels/slack')

    cleanup()
    renderAt(['/settings/channels/slack?channel=&highlight=x'])
    await waitFor(() => expect(probe.location?.search).toBe('?highlight=x'))
    expect(probe.location?.pathname).toBe('/settings/channels/slack')
  })

  it('a dot-only legacy param cannot escape /settings and preserves the path it arrived on', async () => {
    // '..' survives encodeURIComponent and the WHATWG URL parser resolves
    // even its percent-form against the tree — so it is treated as absent:
    // the segments backfill and the junk param is consumed.
    renderAt(['/settings/channels/slack?tab=..'])
    await waitFor(() => expect(probe.location?.search).toBe(''))
    expect(probe.location?.pathname).toBe('/settings/channels/slack')
  })

  it('an empty path segment is positional — /settings/channels//slack?tab=channels does not promote slack to sub', async () => {
    renderAt(['/settings/channels//slack?tab=channels'])
    await waitFor(() => expect(probe.location?.search).toBe(''))
    // segs[1] is '' (not 'slack'), so the translation lands on the tab alone.
    expect(probe.location?.pathname).toBe('/settings/channels')
  })
})
