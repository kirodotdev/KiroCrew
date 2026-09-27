/**
 * Phone chat page: ONE top bar.
 *
 * Below the breakpoint the chat route used to stack two bars — the shell's
 * (logo, search square, readout capsule, bell) over the chat page's own title
 * row (sessions toggle, title, pop-out, activity panel). This pins the merged
 * shape at the SHELL end of the hand-off:
 *
 *  - on /chat the header is the `topbar-single` variant with the chat page's
 *    two portal targets, and without the nav-drawer logo, the search square or
 *    the readout capsule;
 *  - the shell hands the chat page its main-navigation RAIL through
 *    `MobileNavRailContext`, built from the same registry the desktop rail
 *    uses (the Chat row is the active one, Settings is there, Search is pinned);
 *  - every other phone page keeps the logo -> nav drawer, loses the search
 *    square and the capsule, and still has THREE in-flow header children, so
 *    the actions group cannot be auto-placed into the `auto` centre track.
 *
 * The ChatPage stub stands in for the real page's consumer side (covered in
 * ChatPage.mobileSingleTopbar.test.tsx) and renders whatever rail it is handed.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, fireEvent, screen, within } from '@testing-library/react'
import { sseConnected, sseDisconnected } from '../store/dashboardSlice'
import { renderWithProviders } from './helpers'
import App from '../App'
import { useMobileNavRail } from '../components/MobileNavRailContext'

function ChatPageStub() {
  const rail = useMobileNavRail()
  return (
    <div data-testid="chat-page">
      {rail ? rail({ onActivate: () => {} }) : <span data-testid="no-rail" />}
    </div>
  )
}
vi.mock('../pages/ChatPage', () => ({
  default: ChatPageStub,
}))
vi.mock('../pages/SettingsPage', () => ({ default: () => <div data-testid="settings-page">SettingsPage</div> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: null }),
    listApps: vi.fn().mockResolvedValue([]),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: 'claude' } }),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    themeDetail: vi.fn().mockResolvedValue({}),
    themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: true, import_onboarded: true }),
    updateThemeConfig: vi.fn().mockResolvedValue({}),
    onboardingImportScan: vi.fn().mockResolvedValue({ sources: [], skipped: [], merge_only: true }),
    onboardingImportState: vi.fn().mockResolvedValue({}),
    beaconStatus: vi.fn().mockResolvedValue({ enabled: true, would_send: true, reason: 'ready', endpoint_configured: true, env_override: false, env_var: 'KIROCREW_TELEMETRY_DISABLED' }),
    patchConfig: vi.fn().mockResolvedValue({}),
    createChatSlot: vi.fn().mockResolvedValue({ key: 's', title: 's', messages: 0, running: false }),
    chatSlotContext: vi.fn().mockResolvedValue({ ok: true }),
    sendChat: vi.fn().mockResolvedValue({ ok: true }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

// Phone: the mobile media query matches. useIsMobile subscribes to exactly this
// query, so every consumer in the shell sees the narrow layout.
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(max-width: 767px)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as typeof ResizeObserver

const header = () => document.querySelector('header.topbar') as HTMLElement
const flowChildren = (el: HTMLElement) => [...el.children].filter(c => !c.className.includes('absolute'))

describe('phone chat page: one top bar', () => {
  it('renders the single-bar variant with the chat page portal targets and none of the old shell controls', async () => {
    localStorage.setItem('mc-onboarded', '1')
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    const h = header()
    expect(h).toHaveClass('topbar-single')
    // The two hand-off points the chat page fills: title slot in the centre
    // cell, overflow-menu slot after the bell.
    expect(within(h).getByTestId('mobile-topbar-slot')).toBeInTheDocument()
    const trail = within(h).getByTestId('mobile-topbar-trail-slot')
    const bell = within(h).getByRole('button', { name: 'Notifications' })
    expect(bell.compareDocumentPosition(trail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The trailing cell holds exactly two things: the bell and the page's
    // menu slot. The update pill and extension widgets live in the leading
    // cell, so a pending update cannot make this a three-control group.
    expect([...trail.parentElement!.children]).toHaveLength(2)
    expect(trail.parentElement).toHaveClass('tb-trail')
    // Gone from the bar: the nav-drawer logo (the rail is the page's nav now),
    // the search square (Search is pinned in the rail) and the readout capsule.
    expect(within(h).queryByRole('button', { name: 'Open menu' })).toBeNull()
    expect(within(h).queryByRole('button', { name: 'Search sessions, files, and commands' })).toBeNull()
    expect(h.querySelector('.tb-capsule')).toBeNull()
    // Three in-flow cells — and NOT the inline-size-contained groups: a size
    // container in the variant's `auto` side tracks would collapse to its
    // padding (topbarMenuButtonNarrow.test.ts records the measurement).
    const flow = flowChildren(h)
    expect(flow).toHaveLength(3)
    for (const cell of flow) {
      expect(cell).not.toHaveClass('tb-left')
      expect(cell).not.toHaveClass('tb-right')
    }
    expect(flow[1]).toHaveAttribute('id', 'mobile-topbar-slot')
  })

  it('hands the chat page a rail built from the nav registry, with Search pinned', async () => {
    localStorage.setItem('mc-onboarded', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const rail = await screen.findByTestId('mobile-nav-rail')
    expect(screen.queryByTestId('no-rail')).toBeNull()
    // Rows come from the same registry the desktop rail renders: the surface
    // labels are the accessible names, and the current page's row is active.
    const chat = within(rail).getByRole('button', { name: 'Sessions' })
    expect(chat).toHaveClass('nav-active')
    expect(chat).toHaveClass('rounded-xl')
    // Each tile carries a visible caption: a finger cannot summon the desktop
    // rail's hover tip, and an icon-only Artifacts glyph was unreadable cold.
    expect(chat).toHaveTextContent('Sessions')
    expect(within(rail).getByRole('button', { name: 'Settings' })).not.toHaveClass('nav-active')
    // Search moved here from the bar, same label, same command palette.
    expect(within(rail).getByTestId('mobile-nav-rail-search')).toHaveAccessibleName('Search sessions, files, and commands')
    // The brand mark is a control (home = chat root), not an inert picture in
    // the position every other app puts a tappable logo.
    expect(within(rail).getByTestId('mobile-nav-rail-home')).toHaveAccessibleName()
    // Every tile carries a visible caption (a finger cannot summon the desktop
    // rail's hover tip); the long label gets its short form, the name stays full.
    const caps = within(rail).getByRole('button', { name: 'Agent Capabilities' })
    expect(caps).toHaveTextContent('Capabilities')
    expect(caps).not.toHaveTextContent('Agent Capabilities')
    expect(within(rail).getByRole('button', { name: 'Settings' })).toHaveTextContent('Settings')
    // The rail is the last row-set before Search; nothing in it is a text label
    // (icon-only, 56px wide), so every row must be named.
    for (const row of within(rail).getAllByRole('button')) expect(row).toHaveAccessibleName()
    // Only the Apps list scrolls (its own frame, like the desktop rail); the
    // brand mark above and Capabilities / Settings / Search below stay pinned,
    // so 14 installed apps cannot push Settings off the bottom of the screen.
    expect(rail).toHaveClass('overflow-hidden')
    expect(rail).not.toHaveClass('overflow-y-auto')
    const apps = within(rail).getByTestId('mobile-nav-rail-apps')
    expect(apps).toHaveClass('overflow-y-auto', 'flex-1', 'min-h-0')
    expect(apps).not.toContainElement(within(rail).getByTestId('mobile-nav-rail-home'))
    expect(apps).not.toContainElement(within(rail).getByTestId('mobile-nav-rail-search'))
    expect(apps).not.toContainElement(within(rail).getByRole('button', { name: 'Settings' }))
    expect(apps).not.toContainElement(caps)
  })

  it('keeps the logo -> nav drawer on other phone pages and still has three in-flow header cells', async () => {
    localStorage.setItem('mc-onboarded', '1')
    renderWithProviders(<App />, { route: '/settings' })
    await screen.findByTestId('settings-page')
    const h = header()
    expect(h).not.toHaveClass('topbar-single')
    expect(within(h).getByRole('button', { name: 'Open menu' })).toBeInTheDocument()
    expect(within(h).queryByTestId('mobile-topbar-slot')).toBeNull()
    expect(within(h).queryByRole('button', { name: 'Search sessions, files, and commands' })).toBeNull()
    expect(h.querySelector('.tb-capsule')).toBeNull()
    expect(within(h).getByRole('button', { name: 'Notifications' })).toBeInTheDocument()
    // The empty centre spacer keeps the third in-flow child, so the actions
    // group is not auto-placed into the `auto` centre track.
    const flow = flowChildren(h)
    expect(flow).toHaveLength(3)
    expect(flow[0]).toHaveClass('tb-left')
    expect(flow[1]).toHaveAttribute('data-testid', 'topbar-centre-spacer')
    expect(flow[2]).toHaveClass('tb-right')
    // No rail off the chat route.
    expect(screen.queryByTestId('mobile-nav-rail')).toBeNull()
  })

  it('shows a reconnecting strip under the bar while the socket is down, since the phone has no capsule dot', async () => {
    localStorage.setItem('mc-onboarded', '1')
    const { store } = renderWithProviders(<App />, { route: '/settings' })
    await screen.findByTestId('settings-page')
    // Drive the transport state explicitly: the shell's first `/api/status`
    // reply flips `connected` on (`sseStatus`), and whether it lands before or
    // after the page mounts is a race, so "the store starts disconnected" is
    // not a premise this test may rest on. The strip is the only generic
    // offline signal below the breakpoint, and it hangs off the bar out of
    // its grid flow.
    act(() => { store.dispatch(sseDisconnected()) })
    const strip = screen.getByTestId('mobile-offline-strip')
    expect(strip).toHaveTextContent('Gateway offline')
    expect(strip.parentElement).toBe(header())
    expect(strip).toHaveClass('absolute')
    // It hangs over the top of <main>: a readout must not swallow the taps
    // and scroll starts that land there.
    expect(strip).toHaveClass('pointer-events-none')
    act(() => { store.dispatch(sseConnected()) })
    expect(screen.queryByTestId('mobile-offline-strip')).toBeNull()
  })

  it('stays quiet while the socket is down for an expired session, which has its own banner', async () => {
    localStorage.setItem('mc-onboarded', '1')
    const { isAuthBannerShown } = await import('../api/client')
    vi.mocked(isAuthBannerShown).mockReturnValue(true)
    try {
      const { store } = renderWithProviders(<App />, { route: '/settings' })
      await screen.findByTestId('settings-page')
      // Same explicit transport state as above, or `connected` may already be
      // on and the absence below would prove nothing about the auth gate.
      act(() => { store.dispatch(sseDisconnected()) })
      // "Reconnecting" would point the user at the transport when pasting a
      // token is the fix; the auth banner carries that message.
      expect(screen.queryByTestId('mobile-offline-strip')).toBeNull()
      // ...visibly. The banner has no live region, so the phone keeps the
      // capsule's sr-only announcement of the auth-specific cause.
      expect(screen.getByTestId('mobile-offline-sr')).toHaveAttribute('role', 'status')
      expect(screen.getByTestId('mobile-offline-sr')).toHaveTextContent(/session expired/i)
    } finally {
      vi.mocked(isAuthBannerShown).mockReturnValue(false)
    }
    // Search left the bar, so the nav drawer -- the one surface every non-chat
    // phone page opens -- carries it as a row, same label, same palette.
    const h = header()
    fireEvent.click(within(h).getByRole('button', { name: 'Open menu' }))
    const nav = await screen.findByRole('navigation', { name: 'Main navigation' })
    fireEvent.click(within(nav).getByRole('button', { name: 'Search sessions, files, and commands' }))
    expect(await screen.findByRole('dialog', { name: 'Search everywhere' })).toBeInTheDocument()
  })

  // The desktop opens the account modal (balance, sign-in state) from the
  // readout capsule's credits segment; the phone renders no capsule, so the two
  // phone drawers carry an Account entry -- on exactly the readings the desktop
  // segment would show. The default mocks are a non-Kiro harness with no
  // reading: the one state with no segment on the desktop, and no entry here.
  it('offers no Account entry on a harness whose desktop bar would show no credits segment', async () => {
    localStorage.setItem('mc-onboarded', '1')
    renderWithProviders(<App />, { route: '/chat' })
    const rail = await screen.findByTestId('mobile-nav-rail')
    // Settle the config + usage reads before asserting absence.
    await screen.findByRole('button', { name: 'Settings' })
    expect(within(rail).queryByRole('button', { name: 'Kiro Account' })).toBeNull()
  })

  it('puts a Kiro Account tile in the rail and a row in the nav drawer on the Kiro backend, opening the account modal', async () => {
    localStorage.setItem('mc-onboarded', '1')
    const { api } = await import('../api/client')
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: '' } })
    try {
      const { unmount } = renderWithProviders(<App />, { route: '/chat' })
      const rail = await screen.findByTestId('mobile-nav-rail')
      const tile = await within(rail).findByRole('button', { name: 'Kiro Account' })
      // Between Capabilities and Settings, pinned with them (not in the Apps scroller).
      expect(within(rail).getByTestId('mobile-nav-rail-apps')).not.toContainElement(tile)
      expect(tile).toHaveAttribute('aria-pressed', 'false')
      fireEvent.click(tile)
      expect(await screen.findByRole('dialog', { name: 'Kiro Account' })).toBeInTheDocument()
      expect(tile).toHaveAttribute('aria-pressed', 'true')
      unmount()

      // Every other phone page: the nav drawer carries the same entry.
      renderWithProviders(<App />, { route: '/settings' })
      await screen.findByTestId('settings-page')
      fireEvent.click(within(header()).getByRole('button', { name: 'Open menu' }))
      const nav = await screen.findByRole('navigation', { name: 'Main navigation' })
      fireEvent.click(await within(nav).findByRole('button', { name: 'Kiro Account' }))
      expect(await screen.findByRole('dialog', { name: 'Kiro Account' })).toBeInTheDocument()
    } finally {
      vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'claude' } })
    }
  })
})
