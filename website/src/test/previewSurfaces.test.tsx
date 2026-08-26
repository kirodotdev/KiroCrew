/**
 * Preview-flag gating for unreleased surfaces.
 *
 * The contract under test is "an unpolished surface is not advertised anywhere",
 * which is a claim about EVERY consumer of the surface registry, not about one
 * component. So this file pins the gate at each door a user could walk through:
 * the storage primitive, the registry predicate, the Search Everywhere Pages
 * provider, and the Developer > Feature Previews toggle that opens them all. A
 * test that only covered the nav rail would have passed while the palette still
 * shipped a one-keystroke path to the same page.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { ReactElement } from 'react'
import { render, screen, renderHook, act, cleanup } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// DeveloperPage's sibling tabs are heavy and irrelevant here — the last describe
// only needs the page's tab rail and the Feature Previews pane behind it.
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
  getAdvertisedSurfaces,
  surfacePreviewEnabled,
  _resetBuiltinsForTest,
} from '../surfaces/registry'
import {
  PREVIEW_FLAG_EVENT,
  PREVIEW_FLAG_PREFIX,
  PREVIEW_WEBHOOKS,
  readPreviewFlag,
  setPreviewFlag,
} from '../utils/previewFlags'
import { usePreviewFlag, usePreviewFlagRevision } from '../hooks/usePreviewFlag'
import { createPagesProvider } from '../components/commandPalette/providers/pagesProvider'
import { FeaturePreviewsTab } from '../pages/developer/FeaturePreviewsTab'
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
    expect(PREVIEW_WEBHOOKS.startsWith(PREVIEW_FLAG_PREFIX)).toBe(true)
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

describe('Developer > Feature Previews', () => {
  const renderTab = () =>
    render(<MemoryRouter><FeaturePreviewsTab /></MemoryRouter>)

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

  it('is its own tab on the Developer page, not part of Config', async () => {
    // Pin both halves of the move: Config must not carry the switch, and the
    // rail must offer the tab that does — otherwise the opt-ins become
    // unreachable while every unit test above still passes.
    render(<MemoryRouter initialEntries={['/developer?tab=config']}><DeveloperPage /></MemoryRouter>)
    expect(screen.getByTestId('kirocrew-cfg')).toBeTruthy()
    expect(screen.queryByRole('switch', { name: /webhooks/i })).toBeNull()

    const tab = screen.getByRole('button', { name: /feature previews/i })
    await act(async () => { tab.click() })
    expect(screen.getByRole('switch', { name: /webhooks/i })).toBeTruthy()
  })
})
