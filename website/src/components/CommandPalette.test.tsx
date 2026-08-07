import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

/**
 * Tests for the Search Everywhere palette:
 *  - {@link useCommandPalette} global trigger (⌘K / Ctrl+K, with opt-in double-Shift), and
 *  - the {@link CommandPalette} modal's open render, Tab tab-scoping, Enter
 *    activation, and close paths (Escape wiring + close button + row click).
 *
 * The four provider hooks and the shared {@link useListKeyboardNav} hook are
 * mocked so the modal renders without Redux / React-Query / Router / the
 * (separately-tested) keyboard-nav machinery. The mocks return STABLE objects
 * so the palette's search/register effects don't loop on changing identities.
 */

const H = vi.hoisted(() => {
  const onActivateAll = vi.fn()
  const onActivateSess = vi.fn()
  // Stable §2 Enter-matrix sinks (paletteActions). Hoisted so the insert-token
  // branch's composer-insert (`enterInsertOrNewSession`) and new-session
  // (`newSessionWithToken`) calls are assertable across re-renders.
  const enterInsertOrNewSession = vi.fn()
  const newSessionWithToken = vi.fn()
  const navigate = vi.fn()
  const storeState = {
    dashboard: { slots: [] as Array<Record<string, unknown>>, unreadSlots: [] as string[] },
    chat: {
      slotStatusDetail: {} as Record<
        string,
        { kind: string; text: string; ts: number; toolName?: string }
      >,
    },
  }
  const onActivateRecent = vi.fn()
  const allResult = {
    id: 'all:1',
    providerId: 'all',
    title: 'All Result',
    subtitle: 'all sub',
    icon: null,
    score: 10,
    indices: [] as number[],
    onActivate: onActivateAll,
  }
  const sessResult = {
    id: 'sessions:1',
    providerId: 'sessions',
    title: 'Session Result',
    subtitle: 'sess sub',
    icon: null,
    score: 10,
    indices: [] as number[],
    onActivate: onActivateSess,
  }
  const recentResult = {
    id: 'recents:cur:chat-1',
    providerId: 'recents',
    title: 'Recent Session',
    subtitle: 'last message preview',
    icon: null,
    score: 0,
    indices: [] as number[],
    groupLabel: 'Current',
    onActivate: onActivateRecent,
  }
  const allProvider = { id: 'all', label: 'All', icon: null, search: vi.fn(async () => [allResult]) }
  const sessionsProvider = {
    id: 'sessions',
    label: 'Sessions',
    icon: null,
    search: vi.fn(async () => [sessResult]),
  }
  const pagesProvider = { id: 'pages', label: 'Pages', icon: null, search: vi.fn(() => []) }
  const actionsProvider = { id: 'actions', label: 'Actions', icon: null, search: vi.fn(() => []) }
  // P1 providers (Knowledge · Skills · Prompts) — wired into CommandPalette as
  // direct hooks. Mock them with stable, empty-search providers
  // so the modal renders without React-Query / Router (matching the P0 mocks).
  const knowledgeProvider = { id: 'knowledge', label: 'Knowledge', icon: null, search: vi.fn(() => []) }
  const skillsProvider = { id: 'skills', label: 'Skills', icon: null, search: vi.fn(() => []) }
  const promptsProvider = { id: 'prompts', label: 'Prompts', icon: null, search: vi.fn(() => []) }
  const artifactsProvider = { id: 'artifacts', label: 'Artifacts', icon: null, search: vi.fn(() => []) }
  const recentsProvider = { id: 'recents', label: 'Recent', icon: null, search: vi.fn(async () => [recentResult]) }
  const settingsProvider = { id: 'settings', label: 'Settings', icon: null, search: vi.fn(() => []) }
  const appsProvider = { id: 'apps', label: 'Apps', icon: null, search: vi.fn(async () => []) }
  // Stable return for the mocked keyboard-nav hook (constant identities avoid
  // re-render loops in the palette's effects).
  const navReturn = {
    selected: 0,
    setSelected: vi.fn(),
    selectedRef: { current: 0 },
    itemRefs: { current: [] as (HTMLElement | null)[] },
  }
  const nav = {
    current: null as null | {
      onChoose?: (i: number, withModifier?: boolean) => void
      onClose?: () => void
      onAltEnter?: (i: number) => boolean
    },
  }
  return {
    onActivateAll,
    onActivateSess,
    onActivateRecent,
    navigate,
    storeState,
    recentResult,
    enterInsertOrNewSession,
    newSessionWithToken,
    allResult,
    sessResult,
    allProvider,
    sessionsProvider,
    pagesProvider,
    actionsProvider,
    knowledgeProvider,
    skillsProvider,
    promptsProvider,
    artifactsProvider,
    recentsProvider,
    settingsProvider,
    appsProvider,
    navReturn,
    nav,
  }
})

vi.mock('../hooks/useListKeyboardNav', () => ({
  useListKeyboardNav: (args: {
    onChoose?: (i: number, withModifier?: boolean) => void
    onClose?: () => void
    onAltEnter?: (i: number) => boolean
  }) => {
    H.nav.current = args
    return H.navReturn
  },
}))
vi.mock('./commandPalette/providers', () => ({
  registerProvider: vi.fn(),
  getProviders: () => [],
  getProvider: () => undefined,
  _resetProvidersForTest: () => {},
}))
vi.mock('./commandPalette/providers/allAggregator', () => ({ useAllAggregator: () => H.allProvider }))
vi.mock('./commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => H.sessionsProvider,
}))
vi.mock('./commandPalette/providers/pagesProvider', () => ({ usePagesProvider: () => H.pagesProvider }))
vi.mock('./commandPalette/providers/actionsProvider', () => ({
  useActionsProvider: () => H.actionsProvider,
}))
vi.mock('./commandPalette/providers/knowledgeProvider', () => ({
  useKnowledgeProvider: () => H.knowledgeProvider,
}))
vi.mock('./commandPalette/providers/skillsProvider', () => ({
  useSkillsProvider: () => H.skillsProvider,
}))
vi.mock('./commandPalette/providers/promptsProvider', () => ({
  usePromptsProvider: () => H.promptsProvider,
}))
vi.mock('./commandPalette/providers/artifactsProvider', () => ({
  useArtifactsProvider: () => H.artifactsProvider,
}))
vi.mock('./commandPalette/providers/recentsProvider', () => ({
  useRecentsProvider: () => H.recentsProvider,
}))
vi.mock('./commandPalette/providers/settingsProvider', () => ({
  useSettingsProvider: () => H.settingsProvider,
}))
vi.mock('./commandPalette/providers/appsProvider', () => ({
  useAppsProvider: () => H.appsProvider,
}))
// usePaletteActions backs the §2 Enter matrix (composer-insert + new-session).
// Return the STABLE hoisted spies CommandPalette consumes so the insert-token
// branch of dispatchEnter is assertable (which sink, with which token); the
// modal still renders without the chat store / router.
vi.mock('./commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({
    enterInsertOrNewSession: H.enterInsertOrNewSession,
    newSessionWithToken: H.newSessionWithToken,
    navigate: H.navigate,
  }),
}))

// The palette reads live slot state (running/approval/pin/unread) to key the
// recents search query. Mock the typed selector hook with a minimal dashboard
// slice so the component renders without the real Redux store.
vi.mock('../store', () => ({
  useAppSelector: (sel: (s: unknown) => unknown) => sel(H.storeState),
}))

import CommandPalette from './CommandPalette'
import { useCommandPalette, DOUBLE_SHIFT_WINDOW_MS } from '../hooks/useCommandPalette'
import { SHORTCUTS_ENABLED_KEY } from '../hooks/useKeyboardShortcuts'
import { QUICK_SEARCH_SHORTCUT_KEY } from '../lib/quickSearchShortcut'
import type { Result } from './commandPalette/types'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  H.nav.current = null
  H.storeState.dashboard.slots = []
  H.storeState.dashboard.unreadSlots = []
  H.storeState.chat.slotStatusDetail = {}
  // localStorage is file-scoped, not per-test: a chat-config write (e.g. the
  // simplifiedToolNames toggle test) would otherwise persist into siblings.
  localStorage.removeItem('mc-chat-config')
  localStorage.removeItem(QUICK_SEARCH_SHORTCUT_KEY)
  queryClient.clear()
})

describe('useCommandPalette — global trigger', () => {
  it('starts closed and ⌘K toggles open ⇄ closed', () => {
    const { result } = renderHook(() => useCommandPalette())
    expect(result.current.open).toBe(false)
    act(() => {
      fireEvent.keyDown(window, { key: 'k', metaKey: true })
    })
    expect(result.current.open).toBe(true)
    act(() => {
      fireEvent.keyDown(window, { key: 'k', metaKey: true })
    })
    expect(result.current.open).toBe(false)
  })

  it('Ctrl+K is an equivalent alias', () => {
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    })
    expect(result.current.open).toBe(true)
  })

  it('double-Shift (two bare Shift keydowns) opens when opted in', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(result.current.open).toBe(true)
  })

  it('two Shifts spaced BEYOND the double-tap window do not open (accidental tap)', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    const nowSpy = vi.spyOn(Date, 'now')
    try {
      const { result } = renderHook(() => useCommandPalette())
      // Base off a NON-ZERO timestamp: t=0 collides with the hook's
      // "no pending first tap" sentinel (lastShiftRef.current === 0), which
      // would make the first tap silently fail to arm and pass this test for
      // the wrong reason. With T0 > 0 the first tap genuinely arms.
      const T0 = 1_000
      nowSpy.mockReturnValue(T0)
      act(() => fireEvent.keyDown(window, { key: 'Shift' }))
      // Second tap one window+1ms later — too slow to count as a double-tap.
      nowSpy.mockReturnValue(T0 + DOUBLE_SHIFT_WINDOW_MS + 1)
      act(() => fireEvent.keyDown(window, { key: 'Shift' }))
      expect(result.current.open).toBe(false)
      // …but that slow second tap re-arms as a new first tap: a follow-up tap
      // a real in-window gap later (half the window) still opens, so the
      // gesture stays usable. A 0ms delta here would pass for any window ≥ 0
      // and would not pin the 250ms boundary.
      nowSpy.mockReturnValue(T0 + DOUBLE_SHIFT_WINDOW_MS + 1 + Math.floor(DOUBLE_SHIFT_WINDOW_MS / 2))
      act(() => fireEvent.keyDown(window, { key: 'Shift' }))
      expect(result.current.open).toBe(true)
    } finally {
      nowSpy.mockRestore()
    }
  })

  it('a key pressed between the two Shifts cancels the gesture', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'a' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(result.current.open).toBe(false)
  })

  it('openPalette() and close() drive the open state imperatively', () => {
    const { result } = renderHook(() => useCommandPalette())
    act(() => result.current.openPalette())
    expect(result.current.open).toBe(true)
    act(() => result.current.close())
    expect(result.current.open).toBe(false)
  })
})

describe('useCommandPalette — Enable shortcuts toggle', () => {
  // jsdom localStorage is shared across the file/suite, so a lingering '0'
  // would disable shortcuts for every later test. Always clear the key.
  afterEach(() => localStorage.removeItem(SHORTCUTS_ENABLED_KEY))

  it('double-Shift does NOT open the palette when shortcuts are disabled', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(result.current.open).toBe(false)
  })

  it('⌘K does NOT open the palette when shortcuts are disabled', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'k', metaKey: true })
    })
    expect(result.current.open).toBe(false)
  })

  it('the nav button (openPalette) still works while shortcuts are disabled', () => {
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const { result } = renderHook(() => useCommandPalette())
    act(() => result.current.openPalette())
    expect(result.current.open).toBe(true)
  })

  it('re-enabling restores double-Shift (no stale first tap carries over)', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '0')
    const { result } = renderHook(() => useCommandPalette())
    // A Shift arrives while disabled (must not seed a pending first tap)…
    act(() => { fireEvent.keyDown(window, { key: 'Shift' }) })
    // …then shortcuts are re-enabled and a single Shift lands.
    localStorage.setItem(SHORTCUTS_ENABLED_KEY, '1')
    act(() => { fireEvent.keyDown(window, { key: 'Shift' }) })
    expect(result.current.open).toBe(false)
    // A genuine double-tap after re-enabling opens as normal.
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(result.current.open).toBe(true)
  })
})

describe('useCommandPalette — configurable activation', () => {
  // jsdom localStorage is shared across the file; clear the preference so a
  // mode set here can't leak into later suites, and vice versa.
  afterEach(() => {
    localStorage.removeItem(QUICK_SEARCH_SHORTCUT_KEY)
    localStorage.removeItem(SHORTCUTS_ENABLED_KEY)
  })

  it('default (unconfigured) does NOT open on double-Shift but ⌘K works', () => {
    // Fresh install: default is mod-k, so double-Shift is inactive.
    const a = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(a.result.current.open).toBe(false)
    a.unmount()

    const b = renderHook(() => useCommandPalette())
    act(() => { fireEvent.keyDown(window, { key: 'k', metaKey: true }) })
    expect(b.result.current.open).toBe(true)
  })

  it('explicit double-shift opt-in preserves the gesture for existing users', () => {
    // An existing user who selected double-shift in Settings has it stored.
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(result.current.open).toBe(true)
  })

  it('mod-k mode: ⌘K/Ctrl+K toggles, but double-Shift no longer opens', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'mod-k' }))
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(result.current.open).toBe(false)
    act(() => { fireEvent.keyDown(window, { key: 'k', ctrlKey: true }) })
    expect(result.current.open).toBe(true)
  })

  it('custom mode: the recorded chord toggles; double-Shift and the ⌘K alias are off', () => {
    // Non-mac test env, so the `mod` chord resolves to Ctrl.
    localStorage.setItem(
      QUICK_SEARCH_SHORTCUT_KEY,
      JSON.stringify({ mode: 'custom', custom: { key: 'p', mod: true } }),
    )
    const { result } = renderHook(() => useCommandPalette())
    act(() => {
      fireEvent.keyDown(window, { key: 'Shift' })
      fireEvent.keyDown(window, { key: 'Shift' })
    })
    expect(result.current.open).toBe(false)
    // The ⌘K/Ctrl+K alias is suppressed once a custom chord is bound.
    act(() => { fireEvent.keyDown(window, { key: 'k', ctrlKey: true }) })
    expect(result.current.open).toBe(false)
    // The custom chord (Ctrl+P here) toggles the palette.
    act(() => { fireEvent.keyDown(window, { key: 'p', code: 'KeyP', ctrlKey: true }) })
    expect(result.current.open).toBe(true)
  })
})

describe('CommandPalette — render', () => {
  it('opens onto the recents quick-switcher (no tab strip) with grouped rows', async () => {
    render(<CommandPalette open onClose={vi.fn()} />, { wrapper })
    // Empty query -> recents provider (quick switcher), NOT the All aggregator.
    expect(await screen.findByText('Recent Session')).toBeInTheDocument()
    expect(screen.getByText('Current')).toBeInTheDocument() // group header
    expect(screen.queryByText('All Result')).toBeNull()
    // The visible tab strip is gone — scoping is prefix+Tab / sigil driven.
    expect(screen.queryByRole('tab')).toBeNull()
    expect(screen.getByPlaceholderText('Search for anything')).toBeInTheDocument()
  })

  it('refreshes recents when a running slot starts a real tool call', async () => {
    H.storeState.dashboard.slots = [
      { key: 'chat-1', title: 'Live session', running: true, messages: 2 },
    ]
    const { rerender } = render(<CommandPalette open onClose={vi.fn()} />, { wrapper })
    await screen.findByText('Recent Session')
    expect(H.recentsProvider.search).toHaveBeenCalledTimes(1)

    H.storeState.chat.slotStatusDetail = {
      'chat-1': { kind: 'tool', text: 'Running: read /workspace/src/app.ts', ts: 1 },
    }
    rerender(<CommandPalette open onClose={vi.fn()} />)

    await waitFor(() => expect(H.recentsProvider.search).toHaveBeenCalledTimes(2))
  })

  it('refreshes recents when the simplified-tool-names preference changes', async () => {
    // Rows render the tool status through toolStatusLabel, so the preference is
    // part of the live state the query key covers — otherwise a running row keeps
    // its pre-toggle label until the next status tick or staleTime expiry.
    H.storeState.dashboard.slots = [
      { key: 'chat-1', title: 'Live session', running: true, messages: 2 },
    ]
    H.storeState.chat.slotStatusDetail = {
      'chat-1': { kind: 'tool', text: 'Reading the app entrypoint', ts: 1 },
    }
    const { rerender } = render(<CommandPalette open onClose={vi.fn()} />, { wrapper })
    await screen.findByText('Recent Session')
    expect(H.recentsProvider.search).toHaveBeenCalledTimes(1)

    localStorage.setItem('mc-chat-config', JSON.stringify({ simplifiedToolNames: false }))
    window.dispatchEvent(new Event('mc-config-changed'))
    rerender(<CommandPalette open onClose={vi.fn()} />)

    await waitFor(() => expect(H.recentsProvider.search).toHaveBeenCalledTimes(2))
  })

  it('typing a query switches from recents to the All aggregator', async () => {
    render(<CommandPalette open onClose={vi.fn()} />, { wrapper })
    await screen.findByText('Recent Session')
    fireEvent.change(screen.getByRole('textbox', { name: 'Search everywhere' }), { target: { value: 'alice' } })
    // Debounced 150ms before the All search fires.
    expect(await screen.findByText('All Result', {}, { timeout: 2000 })).toBeInTheDocument()
    // The provider switches to All immediately (a transient search('') fires);
    // the typed query lands after the 150ms debounce.
    await waitFor(() => expect(H.allProvider.search).toHaveBeenCalledWith('alice'))
  })

  it('renders nothing when closed', () => {
    render(<CommandPalette open={false} onClose={vi.fn()} />, { wrapper })
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('CommandPalette — keyboard & activation', () => {
  it('prefix + Tab adopts the hinted scope and narrows to that provider', async () => {
    render(<CommandPalette open onClose={vi.fn()} />, { wrapper })
    await screen.findByText('Recent Session')

    // "sess" uniquely prefixes Sessions -> the tab hint appears.
    fireEvent.change(screen.getByRole('textbox', { name: 'Search everywhere' }), { target: { value: 'sess' } })
    expect(await screen.findByText('Sessions')).toBeInTheDocument() // the hint label

    act(() => {
      fireEvent.keyDown(window, { key: 'Tab' })
    })

    // Scope chip adopted: placeholder narrows and the sessions provider serves.
    expect(await screen.findByPlaceholderText('Search sessions…')).toBeInTheDocument()
    expect(await screen.findByText('Session Result')).toBeInTheDocument()
  })

  it('a leading sigil ($) instantly scopes to Skills and strips the sigil', async () => {
    render(<CommandPalette open onClose={vi.fn()} />, { wrapper })
    await screen.findByText('Recent Session')

    fireEvent.change(screen.getByRole('textbox', { name: 'Search everywhere' }), { target: { value: '$sla' } })

    expect(await screen.findByPlaceholderText('Search skills…')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Search everywhere' })).toHaveValue('sla')
  })

  it('Backspace on an empty scoped query pops the scope chip', async () => {
    render(<CommandPalette open onClose={vi.fn()} />, { wrapper })
    await screen.findByText('Recent Session')

    fireEvent.change(screen.getByRole('textbox', { name: 'Search everywhere' }), { target: { value: '$' } })
    await screen.findByPlaceholderText('Search skills…')

    act(() => {
      fireEvent.keyDown(window, { key: 'Backspace' })
    })

    expect(await screen.findByPlaceholderText('Search for anything')).toBeInTheDocument()
  })

  it('Enter (hook onChoose) activates the selected result and closes', async () => {
    const onClose = vi.fn()
    render(<CommandPalette open onClose={onClose} />, { wrapper })
    await screen.findByText('Recent Session')

    act(() => {
      H.nav.current?.onChoose?.(0)
    })

    expect(H.onActivateRecent).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('wires Escape (hook onClose) and the close button to the onClose prop', async () => {
    const onClose = vi.fn()
    render(<CommandPalette open onClose={onClose} />, { wrapper })
    await screen.findByText('Recent Session')

    // Escape is handled inside useListKeyboardNav — assert the same onClose was
    // threaded into the hook so Escape closes the palette.
    expect(H.nav.current?.onClose).toBe(onClose)

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('clicking a result row activates it and closes', async () => {
    const onClose = vi.fn()
    render(<CommandPalette open onClose={onClose} />, { wrapper })
    const row = await screen.findByText('Recent Session')

    fireEvent.mouseDown(row)

    expect(H.onActivateRecent).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

/**
 * Per-type Enter matrix — central {@link dispatchEnter} routing (§2).
 *
 * Drives the palette's active (All) tab to render exactly one fixture result
 * carrying a declarative `EnterAction`, then fires the shared hook's
 * `onChoose(0, withModifier)` — the same entry point real Enter (`false`) and
 * ⌘/Ctrl+Enter (`true`) keypresses call — and asserts `dispatchEnter` routes to
 * the correct per-type handler with the correct payload, and that the palette
 * closes (`onClose`) after every dispatch.
 *
 * The composer-insert (`enterInsertOrNewSession`) and new-session
 * (`newSessionWithToken`) APIs are the stable hoisted `usePaletteActions` spies;
 * navigate / open-session / open-knowledge / invoke route through the result's
 * own bound closures (`onActivate` / `onCmdActivate` / `action.run`) which we
 * assert as spies (their payload binding is covered by the provider unit tests).
 */
describe('CommandPalette — per-type Enter matrix (dispatchEnter routing)', () => {
  function fixture(over: Partial<Result>): Result {
    return {
      id: 'x:1',
      providerId: 'all',
      title: 'Fixture',
      icon: null,
      score: 10,
      indices: [],
      onActivate: vi.fn(),
      ...over,
    }
  }

  async function mountWith(result: Result): Promise<{ onClose: ReturnType<typeof vi.fn> }> {
    const onClose = vi.fn()
    // Empty query opens on recents; type a query so the All aggregator serves
    // the single fixture (unscoped typing searches everything).
    H.allProvider.search.mockResolvedValue([result])
    render(<CommandPalette open onClose={onClose} />, { wrapper })
    await screen.findByText('Recent Session')
    fireEvent.change(screen.getByRole('textbox', { name: 'Search everywhere' }), { target: { value: 'fixture' } })
    await screen.findByText(result.title, {}, { timeout: 2000 })
    return { onClose }
  }

  // Fire the shared hook's choose callback (Enter → false, ⌘/Ctrl+Enter → true).
  const choose = (withModifier: boolean) =>
    act(() => {
      H.nav.current?.onChoose?.(0, withModifier)
    })

  // Restore the default All-provider result for any later tests.
  afterEach(() => {
    H.allProvider.search.mockResolvedValue([H.allResult])
  })

  it('Sessions (open-session): Enter opens/switches to the session and closes', async () => {
    const onActivate = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Session Row',
        providerId: 'sessions',
        enter: { kind: 'open-session', sessionKey: 's-1', title: 'Session Row' },
        onActivate,
      }),
    )
    choose(false)
    expect(onActivate).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Sessions (open-session): ⌘Enter opens in split via onCmdActivate', async () => {
    const onActivate = vi.fn()
    const onCmdActivate = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Session Row',
        providerId: 'sessions',
        enter: { kind: 'open-session', sessionKey: 's-1', title: 'Session Row' },
        onActivate,
        onCmdActivate,
      }),
    )
    choose(true)
    expect(onCmdActivate).toHaveBeenCalledTimes(1)
    expect(onActivate).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Skills: Enter navigates to the skills catalog (palette-as-nav) and closes', async () => {
    // Skills rows are a NAVIGATION target — Enter opens /capabilities to
    // view/edit the skill rather than inserting a $token (there is no
    // per-skill deep link yet).
    const { onClose } = await mountWith(
      fixture({
        title: 'Skill Row',
        providerId: 'skills',
        enter: { kind: 'insert-token', token: '$brazil', tokenKind: 'skill' },
      }),
    )
    choose(false)
    expect(H.navigate).toHaveBeenCalledWith('/capabilities')
    expect(H.enterInsertOrNewSession).not.toHaveBeenCalled()
    expect(H.newSessionWithToken).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Skills: ⌘Enter also navigates (no distinct modifier action)', async () => {
    const { onClose } = await mountWith(
      fixture({
        title: 'Skill Row',
        providerId: 'skills',
        enter: { kind: 'insert-token', token: '$brazil', tokenKind: 'skill' },
      }),
    )
    choose(true)
    expect(H.navigate).toHaveBeenCalledWith('/capabilities')
    expect(H.newSessionWithToken).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Prompts (insert-token): Enter inserts the @token into the active composer', async () => {
    const { onClose } = await mountWith(
      fixture({
        title: 'Prompt Row',
        providerId: 'prompts',
        enter: { kind: 'insert-token', token: '@standup', tokenKind: 'prompt' },
      }),
    )
    choose(false)
    expect(H.enterInsertOrNewSession).toHaveBeenCalledWith('@standup')
    expect(H.newSessionWithToken).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Prompts (insert-token): ⌘Enter opens a new session seeded with the @token', async () => {
    const { onClose } = await mountWith(
      fixture({
        title: 'Prompt Row',
        providerId: 'prompts',
        enter: { kind: 'insert-token', token: '@standup', tokenKind: 'prompt' },
      }),
    )
    choose(true)
    expect(H.newSessionWithToken).toHaveBeenCalledWith('@standup')
    expect(H.enterInsertOrNewSession).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Knowledge (open-knowledge): Enter opens/navigates to the entry', async () => {
    const onActivate = vi.fn()
    const onCmdActivate = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Knowledge Row',
        providerId: 'knowledge',
        enter: { kind: 'open-knowledge', entryId: 'k1', title: 'Knowledge Row' },
        onActivate,
        onCmdActivate,
      }),
    )
    choose(false)
    expect(onActivate).toHaveBeenCalledTimes(1)
    expect(onCmdActivate).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Knowledge (open-knowledge): ⌘Enter attaches the entry as chat context', async () => {
    const onActivate = vi.fn()
    const onCmdActivate = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Knowledge Row',
        providerId: 'knowledge',
        enter: { kind: 'open-knowledge', entryId: 'k1', title: 'Knowledge Row' },
        onActivate,
        onCmdActivate,
      }),
    )
    choose(true)
    expect(onCmdActivate).toHaveBeenCalledTimes(1)
    expect(onActivate).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Knowledge (open-knowledge): ⌘Enter degrades to open when no attach handler is bound', async () => {
    const onActivate = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Knowledge Row',
        providerId: 'knowledge',
        enter: { kind: 'open-knowledge', entryId: 'k1', title: 'Knowledge Row' },
        onActivate,
        // no onCmdActivate supplied
      }),
    )
    choose(true)
    expect(onActivate).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Pages (navigate): Enter navigates to the page route', async () => {
    const onActivate = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Pages Row',
        providerId: 'pages',
        enter: { kind: 'navigate', route: '/logs' },
        onActivate,
      }),
    )
    choose(false)
    expect(onActivate).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Pages (navigate): ⌘Enter ignores the modifier and still navigates', async () => {
    const onActivate = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Pages Row',
        providerId: 'pages',
        enter: { kind: 'navigate', route: '/logs' },
        onActivate,
      }),
    )
    choose(true)
    expect(onActivate).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Actions (invoke): Enter runs the action callback', async () => {
    const run = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Action Row',
        providerId: 'actions',
        enter: { kind: 'invoke', run },
        onActivate: vi.fn(),
      }),
    )
    choose(false)
    expect(run).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('Actions (invoke): ⌘Enter ignores the modifier and still runs the callback', async () => {
    const run = vi.fn()
    const { onClose } = await mountWith(
      fixture({
        title: 'Action Row',
        providerId: 'actions',
        enter: { kind: 'invoke', run },
        onActivate: vi.fn(),
      }),
    )
    choose(true)
    expect(run).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
