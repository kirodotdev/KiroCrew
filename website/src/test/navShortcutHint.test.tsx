/**
 * Per-control shortcut hints on the left-rail nav rows (#4370).
 *
 * Three properties are pinned here, because each one fails silently on its own:
 *
 * 1. THE DERIVATION. The hint is looked up from `DEFAULT_SHORTCUTS` by route, so
 *    a row with a bound panel chord advertises it and a row without one stays
 *    untouched. A regression to a hardcoded string per row would keep passing
 *    every visual check while being wrong on the other platform.
 * 2. THE ARIA SPELLING IS NOT THE DISPLAY STRING. `aria-keyshortcuts` has its own
 *    value grammar; handing it `formatShortcut()`'s glyphs announces nothing
 *    useful, and nothing in the rendered output would look wrong.
 * 3. THE CHORD IS NOT IN THE ACCESSIBLE NAME. The visible badge is decoration and
 *    the attribute is the declaration. If the badge lost `aria-hidden` the row
 *    would be announced as "Chat Alt + C", which is why the row is fetched here
 *    BY ITS EXACT NAME — that query failing IS the regression.
 *
 * Same isolation shape as App.terminalNavActive.test.tsx: routed pages and the
 * api client are stubbed so App mounts without real network, and the assertions
 * are on the NAV ROWS rather than on page content.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, act, renderHook } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import App from '../App'
import {
  DEFAULT_SHORTCUTS,
  SHORTCUTS_ENABLED_EVENT,
  SHORTCUTS_ENABLED_KEY,
  formatShortcut,
  type ShortcutDef,
} from '../hooks/useKeyboardShortcuts'
import {
  ariaKeyshortcutsFor,
  navShortcutDef,
  navShortcutId,
  useNavShortcutHint,
} from '../hooks/useNavShortcutHint'
import { getBuiltinSurfaces } from '../surfaces/registry'
import '../surfaces/builtins'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

/**
 * The four `panel-navigation` chords and the two spellings each must produce.
 * jsdom reports a non-Mac platform, so `formatShortcut()` renders the "Alt + X"
 * form here — the ARIA value is deliberately NOT that string.
 */
const PANEL_CHORDS = [
  { route: '/chat', aria: 'Alt+C', display: 'Alt + C' },
  { route: '/notifications', aria: 'Alt+N', display: 'Alt + N' },
  { route: '/projects', aria: 'Alt+P', display: 'Alt + P' },
  { route: '/schedule', aria: 'Alt+S', display: 'Alt + S' },
] as const

/**
 * The rail rows that carry a chord, with the labels the rail actually renders.
 *
 * Only two of the four panel chords have a rail row, and that is a property of
 * the surface registry rather than of this change: `getBuiltinSurfaces()`
 * excludes `hiddenFromNav` and `appOnly` surfaces, so `/notifications` (rendered
 * as the topbar bell instead) and `/projects` (present only once the Task Runner
 * app is installed) are not in the rail set. The rail's `/chat` row is labelled
 * "Sessions". `railRoutesWithChord` below derives this set rather than trusting
 * the literal, so un-hiding either surface reddens that assertion and puts the
 * hint question in front of whoever does it.
 */
const RAIL_ROWS = [
  { route: '/chat', navId: 'chat', name: 'Sessions', aria: 'Alt+C', display: 'Alt + C' },
  { route: '/schedule', navId: 'schedule', name: 'Schedule', aria: 'Alt+S', display: 'Alt + S' },
] as const

/** Rail routes that resolve a chord, derived from the registry the rail iterates. */
const railRoutesWithChord = () =>
  getBuiltinSurfaces().map(s => s.route).filter(r => navShortcutDef(r) !== undefined)

describe('nav shortcut hint — derivation from the registry', () => {
  it('joins a route to its registry entry through the id registerPanelShortcut mints', () => {
    // Not a convention this module invents: registerPanelShortcut() builds the
    // same id for a downstream panel, which is why a panel registered through
    // that seam gets a hint without touching this code.
    expect(navShortcutId('/chat')).toBe('nav-chat')
    expect(navShortcutId('/notifications')).toBe('nav-notifications')
    // Tolerant of a redundant leading slash rather than producing 'nav-/chat'.
    expect(navShortcutId('//projects')).toBe('nav-projects')
  })

  it('resolves every route that has a bound panel chord', () => {
    for (const { route } of PANEL_CHORDS) {
      const def = navShortcutDef(route)
      expect(def, `${route} must resolve a registry entry`).toBeDefined()
      expect(def!.group).toBe('panel-navigation')
    }
    // The four are the whole panel-navigation group — so this covers it exactly,
    // and a fifth entry added later shows up here rather than silently.
    expect(DEFAULT_SHORTCUTS.filter(s => s.group === 'panel-navigation')).toHaveLength(PANEL_CHORDS.length)
  })

  it('resolves nothing for a rail route that has no chord, and that route is real', () => {
    // POSITIVE CONTROL on the same axis: /settings must be a REGISTERED SURFACE,
    // otherwise `undefined` would only mean "no such row" and this assertion
    // would pass for the wrong reason.
    const routes = getBuiltinSurfaces().map(s => s.route)
    expect(routes).toContain('/settings')
    expect(routes).toContain('/artifacts')
    expect(navShortcutDef('/settings')).toBeUndefined()
    expect(navShortcutDef('/artifacts')).toBeUndefined()
  })

  it('pins which rail routes carry a chord, so un-hiding a surface surfaces the question', () => {
    // Two of the four panel chords have no rail row: /notifications is
    // hiddenFromNav (topbar bell) and /projects is appOnly. Both are filtered
    // out of the set the rail iterates, so the hint they would get has nowhere
    // to render. Asserted as an equality rather than a containment: if either is
    // un-hidden later, this reddens and the new row's hint gets considered.
    expect(railRoutesWithChord().sort()).toEqual(RAIL_ROWS.map(r => r.route).sort())
  })
})

describe('nav shortcut hint — the ARIA value is its own grammar', () => {
  it('spells a modifier chord in ARIA key names, not display glyphs', () => {
    const chat = navShortcutDef('/chat')!
    expect(ariaKeyshortcutsFor(chat, false)).toBe('Alt+C')
    expect(ariaKeyshortcutsFor(chat, true)).toBe('Alt+C')
    // The load-bearing distinction: on macOS the two spellings of ONE chord are
    // different strings, so the display string cannot stand in for the attribute.
    const settings = DEFAULT_SHORTCUTS.find(s => s.id === 'open-settings')!
    expect(ariaKeyshortcutsFor({ ...settings, alt: false, meta: true }, true)).toBe('Meta+,')
    expect(ariaKeyshortcutsFor({ ...settings, alt: false, meta: true }, true))
      .not.toBe(formatShortcut({ ...settings, alt: false, meta: true }))
  })

  it('maps meta to Meta on macOS and Control elsewhere, matching the shipped MoveUndoBar declaration', () => {
    const undoLike: ShortcutDef = { id: 'probe', key: 'z', meta: true, group: 'actions' }
    expect(ariaKeyshortcutsFor(undoLike, true)).toBe('Meta+Z')
    expect(ariaKeyshortcutsFor(undoLike, false)).toBe('Control+Z')
  })

  it('keeps literal ctrl as Control on both platforms', () => {
    const monitor = DEFAULT_SHORTCUTS.find(s => s.id === 'agent-monitor')!
    expect(ariaKeyshortcutsFor(monitor, true)).toBe('Control+G')
    expect(ariaKeyshortcutsFor(monitor, false)).toBe('Control+G')
  })

  it('pins the precondition that makes NOT deduping meta and ctrl safe', () => {
    // `ariaKeyshortcutsFor` deliberately carries no meta/ctrl dedupe, because off
    // macOS both spell "Control" and a both-flags entry would announce
    // "Control+Control+X". What makes that safe is that no such entry can exist:
    // the registry has none, and `registerPanelShortcut` -- the only other writer
    // into the array -- hardcodes `alt` and sets neither.
    //
    // Pinned as the PRECONDITION rather than as the unreachable output: an
    // assertion about a def nobody can construct proves nothing, whereas this
    // reddens the day someone adds such an entry and has to choose deliberately.
    // `formatShortcut()` has the identical property, so the two stay symmetric.
    const both = DEFAULT_SHORTCUTS.filter(s => s.meta && s.ctrl)
    expect(both, `entries setting both meta and ctrl: ${both.map(s => s.id).join(', ')}`).toHaveLength(0)
    // Control on the same axis: the halves are individually non-empty, so the
    // zero above is a real zero and not a filter that matches nothing.
    expect(DEFAULT_SHORTCUTS.filter(s => s.meta).length).toBeGreaterThan(0)
    expect(DEFAULT_SHORTCUTS.filter(s => s.ctrl).length).toBeGreaterThan(0)
  })

  it('orders modifiers the way formatShortcut does, and passes named keys through unchanged', () => {
    const shifted: ShortcutDef = { id: 'probe', key: 'n', alt: true, shift: true, group: 'actions' }
    expect(ariaKeyshortcutsFor(shifted, false)).toBe('Alt+Shift+N')
    const named: ShortcutDef = { id: 'probe', key: 'ArrowLeft', alt: true, group: 'chat-navigation' }
    expect(ariaKeyshortcutsFor(named, false)).toBe('Alt+ArrowLeft')
  })
})

describe('nav shortcut hint — the global shortcuts toggle', () => {
  afterEach(() => localStorage.removeItem(SHORTCUTS_ENABLED_KEY))

  it('produces both spellings for a bound route by default', () => {
    const { result } = renderHook(() => useNavShortcutHint('/chat'))
    expect(result.current).toEqual({ chord: 'Alt + C', ariaKeyshortcuts: 'Alt+C' })
  })

  it('produces nothing for a route with no chord', () => {
    const { result } = renderHook(() => useNavShortcutHint('/settings'))
    expect(result.current).toBeNull()
  })

  it('produces nothing while shortcuts are switched off', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const { result } = renderHook(() => useNavShortcutHint('/chat'))
    expect(result.current).toBeNull()
  })

  it('follows the toggle LIVE, on the same event the key handler listens to', () => {
    // Both directions, because a one-way test passes against a hint that is
    // simply computed once and never updated.
    const { result } = renderHook(() => useNavShortcutHint('/chat'))
    expect(result.current).not.toBeNull()

    act(() => {
      localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
      window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
    })
    expect(result.current).toBeNull()

    act(() => {
      localStorage.setItem(SHORTCUTS_ENABLED_KEY, '1')
      window.dispatchEvent(new Event(SHORTCUTS_ENABLED_EVENT))
    })
    expect(result.current).toEqual({ chord: 'Alt + C', ariaKeyshortcuts: 'Alt+C' })
  })
})

describe('App nav rail — rows declare and display their chord', () => {
  beforeEach(() => {
    localStorage.removeItem('mc-nav')
    localStorage.removeItem(SHORTCUTS_ENABLED_KEY)
  })

  it('declares the chord on the row via aria-keyshortcuts', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.getByRole('button', { name: RAIL_ROWS[0].name })).toBeInTheDocument())
    for (const { name, aria } of RAIL_ROWS) {
      expect(screen.getByRole('button', { name }), `${name} row`).toHaveAttribute('aria-keyshortcuts', aria)
    }
  })

  it('leaves a row with no chord undeclared, and that row is present', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    // Presence first: an absent row would satisfy "no attribute" vacuously.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Settings' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Settings' })).not.toHaveAttribute('aria-keyshortcuts')
    expect(screen.getByRole('button', { name: 'Artifacts' })).not.toHaveAttribute('aria-keyshortcuts')
  })

  it('shows the display chord as decoration, located by row identity rather than by the value asserted', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.getByTestId('nav-shortcut-chat')).toBeInTheDocument())
    for (const { navId, display } of RAIL_ROWS) {
      const badge = screen.getByTestId(`nav-shortcut-${navId}`)
      expect(badge, `${navId} badge`).toHaveTextContent(display)
      // Decoration, so it must not reach assistive tech — the row's
      // aria-keyshortcuts is the single declaration.
      expect(badge).toHaveAttribute('aria-hidden', 'true')
      // Keycap DATA: the render-time i18n gate's own opaque marker, so the chord is
      // never charged to the surrounding prose as an untranslated Latin run.
      expect(badge).toHaveAttribute('data-i18n-opaque')
    }
    // COMPLEMENT: no chordless row grew a badge, so the hint is per-bound-route
    // and not sprayed across the rail.
    expect(screen.queryByTestId('nav-shortcut-settings')).toBeNull()
    expect(screen.queryByTestId('nav-shortcut-artifacts')).toBeNull()
    expect(screen.queryByTestId('nav-shortcut-capabilities')).toBeNull()
  })

  it('keeps the chord out of the row accessible name', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.getByTestId('nav-shortcut-chat')).toBeInTheDocument())
    // The badge is rendered (asserted above), so fetching each row by its EXACT
    // name is the assertion: were the badge announced, the name would be
    // "Sessions Alt + C" and these queries would fail.
    for (const { name } of RAIL_ROWS) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument()
    }
  })

  it('reveals the badge on hover AND on keyboard focus, never on hover alone', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.getByTestId('nav-shortcut-chat')).toBeInTheDocument())
    const cls = screen.getByTestId('nav-shortcut-chat').className
    // The row already owns the `group/nav` seam and is tabIndex=0, so both
    // variants resolve against the same ancestor. A hover-only hint would be
    // unreachable without a pointer, which is the defect class #4120 was fixed
    // for — so the focus-visible variant is the load-bearing half here.
    expect(cls).toContain('group-hover/nav:opacity-100')
    expect(cls).toContain('group-focus-visible/nav:opacity-100')
  })

  it('drops every hint when shortcuts are switched off', async () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Settings' })).toBeInTheDocument())
    for (const { name, navId } of RAIL_ROWS) {
      expect(screen.getByRole('button', { name })).not.toHaveAttribute('aria-keyshortcuts')
      expect(screen.queryByTestId(`nav-shortcut-${navId}`)).toBeNull()
    }
    // The bell is on the same helper, so the toggle must reach it too.
    expect(screen.getByRole('button', { name: 'Notifications' })).not.toHaveAttribute('aria-keyshortcuts')
  })
})

describe('App topbar bell — the control Alt+N actually operates', () => {
  // /notifications is `hiddenFromNav`, so its chord has no rail row. The bell is
  // the control, and it resolves through the same route-keyed helper.
  beforeEach(() => {
    localStorage.removeItem('mc-nav')
    localStorage.removeItem(SHORTCUTS_ENABLED_KEY)
  })

  const bell = () => screen.getByRole('button', { name: 'Notifications' })

  it('has no rail row to carry the hint, which is why the bell must', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(bell()).toBeInTheDocument())
    // Both halves: the chord EXISTS in the registry, and the rail set excludes it.
    expect(navShortcutDef('/notifications')).toBeDefined()
    expect(getBuiltinSurfaces().map(s => s.route)).not.toContain('/notifications')
  })

  it('declares the chord via aria-keyshortcuts', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(bell()).toBeInTheDocument())
    expect(bell()).toHaveAttribute('aria-keyshortcuts', 'Alt+N')
  })

  it('keeps the chord OUT of the tooltip, because an attribute cannot be marked opaque', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(bell()).toBeInTheDocument())
    // Not a stylistic choice. The render-time i18n gate scans `title` for Latin
    // runs under the en-XA pseudolocale and its attribute branch honours no
    // `[data-i18n-opaque]` escape, so a chord here is 220 untranslated-attribute
    // findings. Pinned so nobody "improves" it back: the tooltip is the base copy
    // and nothing else, while aria-keyshortcuts above carries the chord.
    expect(bell().getAttribute('title')).toBe('Notifications')
    expect(bell().getAttribute('title')).not.toMatch(/Alt/)
  })

  it('keeps the chord out of the bell accessible name', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Notifications' })).toBeInTheDocument())
    expect(bell().getAttribute('aria-label')).toBe('Notifications')
  })
})
