/**
 * Coverage for the App shell surfaces that no other App.*.test.tsx reaches:
 *
 * - the version-change changelog gate (`mc-last-version` diffing + section
 *   filtering) and the changelog modal's own controls
 * - `handleUpdate` (success + both error-shapes) and the `UpdateOverlay`
 *   progress/failed/stuck states it mounts
 * - the header capsule's resource-posture segment and the system-metrics
 *   segment's error / no-totals branches
 * - the Kiro credits modal with a bonus pool and a signed-in identity
 * - the notification popover's pointer-outside and route-change dismissals
 * - developer mode and the Electron native-menu navigation bridge
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { setUpdateProgress, sseConnected, sseDisconnected } from '../store/dashboardSlice'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => <div data-testid="logs-page">LogsPage</div> }))
vi.mock('../pages/DeveloperPage', () => ({ default: () => <div data-testid="developer-page">DeveloperPage</div> }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/CapabilitiesPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
  Lightbox: () => null,
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({}),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({}),
    themes: vi.fn().mockResolvedValue({ themes: [] }),
    themeBoot: vi.fn().mockResolvedValue({ mode: '', color: '', onboarded: true, import_onboarded: true }),
    updateThemeConfig: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    patchConfig: vi.fn().mockResolvedValue({}),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    changelog: vi.fn().mockResolvedValue({ content: '' }),
    applyUpdate: vi.fn().mockResolvedValue({}),
    cancelUpdate: vi.fn().mockResolvedValue({}),
    setAutoUpdate: vi.fn().mockResolvedValue({}),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {},
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
} as typeof ResizeObserver

import App from '../App'
import { api } from '../api/client'

/** Status payload the shell diffs `mc-last-version` against. */
// `update_can_apply` is what licenses the in-app "Update Now" path: availability
// says a newer build EXISTS, capability says this install can replace its own
// bytes from here. Only a git checkout can (`POST /api/update` is fetch + reset),
// and these suites drive exactly that path — a wheel or desktop install is offered
// the installer command instead, covered by App.changelogModalApply.test.tsx.
const STATUS = {
  uptime: '1h', sessions: 0, messages: 0, version: '0.4.0',
  update_available: true, update_can_apply: true,
}

/** A two-release changelog: only the 0.4.0 section is newer than 0.3.0. */
const CHANGELOG = '## [0.4.0]\n- adds the resource capsule\n\n## [0.3.0]\n- older entry line\n'

type ElectronBridge = {
  setDevMode?: (v: boolean) => void
  onNavigate?: (cb: (path: string) => void) => () => void
  onFullScreenChanged?: (cb: (fs: boolean) => void) => () => void
  setBadgeCount?: (n: number) => void
}
const setElectronBridge = (bridge: ElectronBridge | undefined) => {
  ;(window as Window & { electronAPI?: ElectronBridge }).electronAPI = bridge
}

/** Mark first run complete so no onboarding chapter covers the shell. */
function completeFirstRun() {
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
}

beforeEach(() => {
  localStorage.clear()
  completeFirstRun()
  setElectronBridge(undefined)
  vi.mocked(api.status).mockResolvedValue(STATUS as never)
  vi.mocked(api.changelog).mockResolvedValue({ content: CHANGELOG } as never)
  vi.mocked(api.applyUpdate).mockResolvedValue({} as never)
  vi.mocked(api.cancelUpdate).mockResolvedValue({} as never)
  vi.mocked(api.setAutoUpdate).mockResolvedValue({} as never)
  vi.mocked(api.system).mockResolvedValue({ mem_used_gb: 4, mem_total_gb: 16, cpu_pct: 25, disk_total_gb: 100, disk_free_gb: 60 } as never)
  vi.mocked(api.sessionsUsage).mockResolvedValue({ usage: { available: false } } as never)
})

afterEach(() => {
  vi.useRealTimers()
  setElectronBridge(undefined)
})

/** Render at /chat with a version the shell will treat as newly installed. */
function renderShell(route = '/chat') {
  localStorage.setItem('mc-last-version', '0.3.0')
  return renderWithProviders(<App />, { route })
}

const changelogDialog = () => screen.findByRole('dialog', { name: 'Changelog' })

describe('baseline probe', () => {
  it('renders the shell', () => {
    localStorage.setItem('mc-last-version', '0.4.0')
    renderWithProviders(<App />, { route: '/chat' })
    expect(screen.getByTestId('dashboard-shell')).toBeInTheDocument()
  })
})

describe('App — version-change changelog gate', () => {
  it('shows only the sections newer than the last seen version', async () => {
    renderShell()
    const dialog = await changelogDialog()
    expect(within(dialog).getByText("What's new")).toBeInTheDocument()
    expect(within(dialog).getByText(/adds the resource capsule/)).toBeInTheDocument()
    expect(within(dialog).queryByText(/older entry line/)).toBeNull()
    // The seen-version baseline advances even though the modal is still open.
    await waitFor(() => expect(localStorage.getItem('mc-last-version')).toBe('0.4.0'))
  })

  it('closes the changelog from its close button', async () => {
    renderShell()
    const dialog = await changelogDialog()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Changelog' })).toBeNull())
  })

  it('shows nothing when the running build is ahead of every section', async () => {
    // The reported bug. `main` is bumped a minor ahead of the released line and
    // a release's notes are written when it ships, so between releases the
    // newest section in the file is OLDER than the running build: a 0.6.0 build
    // opened a modal titled `v0.6.0` whose body was headed `[0.4.0]` and offered
    // to update to it. There is nothing to say here, so nothing is said.
    vi.mocked(api.status).mockResolvedValue({ ...STATUS, version: '0.6.0' } as never)
    localStorage.setItem('mc-last-version', '0.5.0')
    renderWithProviders(<App />, { route: '/chat' })

    await screen.findByTestId('dashboard-shell')
    // The baseline still advances, so the check does not re-run every load.
    await waitFor(() => expect(localStorage.getItem('mc-last-version')).toBe('0.6.0'))
    expect(screen.queryByRole('dialog', { name: 'Changelog' })).toBeNull()
  })

  it('keeps an unversioned heading out of the section above it', async () => {
    // Any level-2 heading ends the preceding section, matching the renderer.
    // Keying only on `## [` left this body inside 0.4.0's notes.
    vi.mocked(api.changelog).mockResolvedValue({
      content: '## [0.4.0]\n- adds the resource capsule\n\n## Notes\n- housekeeping prose\n',
    } as never)
    renderShell()

    const dialog = await changelogDialog()
    expect(within(dialog).getByText(/adds the resource capsule/)).toBeInTheDocument()
    expect(within(dialog).queryByText(/housekeeping prose/)).toBeNull()
  })

  it('delivers the notes when no further update is pending', async () => {
    // The state right after a successful update is "current", and the body used
    // to be gated on an update being available -- so the one moment the modal
    // exists for replaced its notes with "You're on the latest version".
    vi.mocked(api.status).mockResolvedValue({
      ...STATUS, update_available: false, update_can_apply: false,
    } as never)
    renderShell()

    const dialog = await changelogDialog()
    expect(within(dialog).getByText(/adds the resource capsule/)).toBeInTheDocument()
    expect(within(dialog).getByText("You're on the latest version")).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Update Now' })).toBeNull()
  })
})

describe('App — changelog modal controls', () => {
  it('persists the auto-update preference from the changelog switch', async () => {
    renderShell()
    const dialog = await changelogDialog()
    const toggle = within(dialog).getByRole('switch', { name: 'Auto-update on restart' })
    expect(toggle.getAttribute('aria-checked')).toBe('true')

    fireEvent.click(toggle)
    await waitFor(() => expect(api.setAutoUpdate).toHaveBeenCalledWith(false))
    expect(toggle.getAttribute('aria-checked')).toBe('false')
  })
})

/** Open the changelog and take the "Update Now" path into the progress overlay. */
async function startUpdate(route = '/chat') {
  const rendered = renderShell(route)
  const dialog = await changelogDialog()
  fireEvent.click(within(dialog).getByRole('button', { name: 'Update Now' }))
  return rendered
}

describe('App — applying an update', () => {
  it('replaces the changelog with the progress overlay', async () => {
    await startUpdate()
    await waitFor(() => expect(api.applyUpdate).toHaveBeenCalled())
    expect(await screen.findByText('Updating Kiro Crew…')).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Changelog' })).toBeNull()
    // No step reported yet: the overlay shows its neutral waiting copy.
    expect(screen.getByText('Starting update…')).toBeInTheDocument()
    expect(screen.getByText('Page will reconnect when ready…')).toBeInTheDocument()
  })

  it('surfaces the server-supplied reason when applying fails', async () => {
    vi.mocked(api.applyUpdate).mockRejectedValueOnce(new Error(JSON.stringify({ error: 'gateway is mid-restart' })))
    await startUpdate()

    const dialog = await screen.findByRole('dialog', { name: 'Update error' })
    expect(within(dialog).getByText('Update Failed')).toBeInTheDocument()
    expect(within(dialog).getByText('gateway is mid-restart')).toBeInTheDocument()
    // The overlay is not mounted: the apply never started.
    expect(screen.queryByText('Updating Kiro Crew…')).toBeNull()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update error' })).toBeNull())
  })

  it('falls back to the raw failure text when it is not a JSON body', async () => {
    vi.mocked(api.applyUpdate).mockRejectedValueOnce(new Error('no space left on device'))
    await startUpdate()
    const dialog = await screen.findByRole('dialog', { name: 'Update error' })
    expect(within(dialog).getByText('no space left on device')).toBeInTheDocument()
  })
})

describe('App — update progress overlay', () => {
  it('marks earlier steps done and times the active one', async () => {
    const { store } = await startUpdate()
    await screen.findByText('Updating Kiro Crew…')

    act(() => { store.dispatch(setUpdateProgress({ step: 'building', detail: 'compiling the wheel' })) })

    expect(screen.getByText('compiling the wheel')).toBeInTheDocument()
    // Every step label is listed; the two before "building" have completed.
    expect(screen.getByText('Pulling latest changes')).toBeInTheDocument()
    expect(screen.getByText('Rebuilding package')).toBeInTheDocument()
    expect(screen.getByText('Restarting server')).toBeInTheDocument()
    expect(screen.getByText('0s')).toBeInTheDocument()
  })

  it('names the restart when the socket drops mid-update', async () => {
    // The restart step kills the socket BY DESIGN (the gateway execs itself)
    // and progress events stop with it. Before this state existed the overlay
    // froze on the last step with its idle waiting copy — indistinguishable
    // from a hang. Disconnected-while-updating must say the gateway is
    // restarting and that reconnection is in progress.
    const { store } = await startUpdate()
    await screen.findByText('Updating Kiro Crew…')

    act(() => { store.dispatch(setUpdateProgress({ step: 'restarting', detail: 'Restarting server…' })) })
    expect(screen.getByText('Page will reconnect when ready…')).toBeInTheDocument()

    act(() => { store.dispatch(sseDisconnected()) })
    const note = screen.getByTestId('update-reconnecting')
    expect(note.textContent).toContain('Gateway is restarting — reconnecting…')
    // The idle copy stands down: both lines together would say "waiting" and
    // "dialing" about the same moment.
    expect(screen.queryByText('Page will reconnect when ready…')).toBeNull()

    // Reconnected (the gateway is back, the reload latch takes it from here):
    // the transient line yields back to the idle copy.
    act(() => { store.dispatch(sseConnected()) })
    expect(screen.queryByTestId('update-reconnecting')).toBeNull()
    expect(screen.getByText('Page will reconnect when ready…')).toBeInTheDocument()
  })

})

describe('App — header capsule resource posture', () => {
  it('flags a critical host posture with the free-memory reason and subagent cap', async () => {
    localStorage.setItem('mc-last-version', '0.4.0')
    vi.mocked(api.system).mockResolvedValue({
      mem_used_gb: 15, mem_total_gb: 16, cpu_pct: 95, disk_total_gb: 100, disk_free_gb: 5,
      resource_posture: 'critical', resource_available_gb: 1.25, subagent_cap: 2,
    } as never)
    renderWithProviders(<App />, { route: '/chat' })

    expect(await screen.findByText('Critical')).toBeInTheDocument()
    expect(screen.getByText('· cap: 2')).toBeInTheDocument()
    expect(screen.getByTitle(/Host memory is critically low \(1.3 GB free\)/)).toBeInTheDocument()
  })

  it('flags a tight host posture', async () => {
    localStorage.setItem('mc-last-version', '0.4.0')
    vi.mocked(api.system).mockResolvedValue({
      mem_used_gb: 12, mem_total_gb: 16, cpu_pct: 60, disk_total_gb: 100, disk_free_gb: 30,
      resource_posture: 'tight', resource_available_gb: 3,
    } as never)
    renderWithProviders(<App />, { route: '/chat' })

    expect(await screen.findByText('Tight')).toBeInTheDocument()
    expect(screen.getByTitle(/Host memory is tight \(3.0 GB free\)/)).toBeInTheDocument()
    // No cap reported, so no cap clause is appended.
    expect(screen.queryByText(/cap:/)).toBeNull()
  })

  it('leaves the capsule clean when the host has ample headroom', async () => {
    localStorage.setItem('mc-last-version', '0.4.0')
    vi.mocked(api.system).mockResolvedValue({
      mem_used_gb: 2, mem_total_gb: 16, cpu_pct: 5, disk_total_gb: 100, disk_free_gb: 90,
      resource_posture: 'ample', resource_available_gb: 12,
    } as never)
    renderWithProviders(<App />, { route: '/chat' })

    await screen.findByTestId('chat-page')
    expect(screen.queryByText('Tight')).toBeNull()
    expect(screen.queryByText('Critical')).toBeNull()
  })
})

describe('App — system metrics segment', () => {
  beforeEach(() => {
    localStorage.setItem('mc-last-version', '0.4.0')
    localStorage.setItem('mc-topbar-metrics', '1')
  })

  it('shows an unavailable pill when the metrics fetch fails, and hides on click', async () => {
    vi.mocked(api.system).mockRejectedValue(new Error('probe unreachable'))
    renderWithProviders(<App />, { route: '/chat' })

    const pill = await screen.findByText('metrics unavailable')
    fireEvent.click(pill)
    await waitFor(() => expect(localStorage.getItem('mc-topbar-metrics')).toBe('0'))
    expect(screen.queryByText('metrics unavailable')).toBeNull()
  })

  it('renders placeholders for readouts the host cannot report', async () => {
    vi.mocked(api.system).mockResolvedValue({
      mem_used_gb: 0, mem_total_gb: 0, cpu_pct: null, disk_total_gb: 0, disk_free_gb: 0,
    } as never)
    renderWithProviders(<App />, { route: '/chat' })

    const cpu = await screen.findByTitle('CPU: unavailable')
    expect(cpu.textContent).toContain('—')
    expect(screen.getByTitle('Memory: unavailable').textContent).toContain('—')
    expect(screen.getByTitle('Disk: unavailable').textContent).toContain('—')
  })
})

describe('App — Kiro credits modal', () => {
  const usageWithBonus = (startUrl: string, accountType = 'Social') => ({
    usage: {
      credits_used: 8000, credits_plan: 10000, credits_overage: 0,
      bonus_credits: [{ name: 'Welcome credits', used: 500, total: 2000, days_left: 19 }],
      plan: 'KIRO POWER', resets: '2026-09-01', overage_rate: '0.04', cost_usd: 1.5,
      account_type: accountType, email: 'builder@example.com', start_url: startUrl,
    },
  })

  beforeEach(() => { localStorage.setItem('mc-last-version', '0.4.0') })

  it('breaks the total into a bonus pool and a plan pool', async () => {
    vi.mocked(api.sessionsUsage).mockResolvedValue(usageWithBonus('https://example.awsapps.com/start') as never)
    renderWithProviders(<App />, { route: '/chat' })

    fireEvent.click(await screen.findByTitle(/Kiro credit usage/))
    const dialog = await screen.findByRole('dialog')
    // Plan pool: the progressbar tracks the plan only, not bonus grants.
    expect(within(dialog).getByText('KIRO POWER')).toBeInTheDocument()
    expect(within(dialog).getByText('8,000')).toBeInTheDocument()
    expect(within(dialog).getByText(/\/\s*10,000\s*credits/)).toBeInTheDocument()
    expect(within(dialog).getByText(/Resets\s*Sep 1/)).toBeInTheDocument()
    // Bonus pool: its own section with per-grant balance and expiry.
    expect(within(dialog).getByText('Bonus credits')).toBeInTheDocument()
    expect(within(dialog).getByText('Welcome credits')).toBeInTheDocument()
    expect(within(dialog).getByText('Remaining credit balance: 1,500')).toBeInTheDocument()
    expect(within(dialog).getByText('Days until expiration: 19')).toBeInTheDocument()
    expect(within(dialog).getByText('$1.50')).toBeInTheDocument()
    expect(within(dialog).getByText('Signed in with Social login')).toBeInTheDocument()
  })

  it('drops the issuer host when the start URL will not parse', async () => {
    vi.mocked(api.sessionsUsage).mockResolvedValue(usageWithBonus('not-a-url', 'IamIdentityCenter') as never)
    renderWithProviders(<App />, { route: '/chat' })

    fireEvent.click(await screen.findByTitle(/Kiro credit usage/))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Signed in with IAM Identity Center')).toBeInTheDocument()
  })
})

describe('App — notification popover dismissal', () => {
  beforeEach(() => { localStorage.setItem('mc-last-version', '0.4.0') })

  it('closes on a pointer press outside the bell and its popover', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const bell = await screen.findByLabelText('Notifications')

    fireEvent.click(bell)
    expect(bell.getAttribute('aria-expanded')).toBe('true')

    fireEvent.pointerDown(document.body)
    expect(bell.getAttribute('aria-expanded')).toBe('false')
  })

  it('stays open when the press lands on the bell itself', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    const bell = await screen.findByLabelText('Notifications')

    fireEvent.click(bell)
    fireEvent.pointerDown(bell)
    expect(bell.getAttribute('aria-expanded')).toBe('true')
  })

  it('closes when the route changes underneath it', async () => {
    let navigateFromMenu: ((path: string) => void) | undefined
    setElectronBridge({ onNavigate: cb => { navigateFromMenu = cb; return () => { navigateFromMenu = undefined } } })
    renderWithProviders(<App />, { route: '/chat' })
    const bell = await screen.findByLabelText('Notifications')

    fireEvent.click(bell)
    expect(bell.getAttribute('aria-expanded')).toBe('true')

    act(() => navigateFromMenu?.('/logs'))
    await screen.findByTestId('logs-page')
    expect(bell.getAttribute('aria-expanded')).toBe('false')
  })
})

describe('App — Electron bridges', () => {
  beforeEach(() => { localStorage.setItem('mc-last-version', '0.4.0') })

  it('relays the boot dev-mode state to the native shell', async () => {
    const setDevMode = vi.fn()
    setElectronBridge({ setDevMode })
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    expect(setDevMode).toHaveBeenCalledWith(false)
  })

  it('routes a plain in-app path from the native menu and ignores a host-relative one', async () => {
    let navigateFromMenu: ((path: string) => void) | undefined
    setElectronBridge({ onNavigate: cb => { navigateFromMenu = cb; return () => { navigateFromMenu = undefined } } })
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    act(() => navigateFromMenu?.('/logs'))
    expect(await screen.findByTestId('logs-page')).toBeInTheDocument()

    // Protocol-relative targets are refused by construction, so the route holds.
    act(() => navigateFromMenu?.('//example.com/steal'))
    expect(screen.getByTestId('logs-page')).toBeInTheDocument()
  })

  it('a session deep link SELECTS the session, not just the route', async () => {
    // The Crew Companion's "Open session" CTA arrives on this same channel. A
    // bare navigate would be enough only from another page: ChatPage reads `?sid`
    // while it MOUNTS, so from an already-open /chat the window would come
    // forward with the previous session still on screen — the notification would
    // appear to have opened nothing, which is the bug the CTA is meant to fix.
    let navigateFromMain: ((path: string) => void) | undefined
    setElectronBridge({ onNavigate: cb => { navigateFromMain = cb; return () => { navigateFromMain = undefined } } })
    const { store } = renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    expect(store.getState().chat.activeSlot).not.toBe('chat-2-200')

    act(() => navigateFromMain?.('/chat?sid=chat-2-200'))

    expect(store.getState().chat.activeSlot).toBe('chat-2-200')
    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })
})

describe('App — developer mode', () => {
  beforeEach(() => { localStorage.setItem('mc-last-version', '0.4.0') })

  it('adds the Developer rail entry with an unseen dot, then clears it on visit', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')
    expect(screen.queryByRole('button', { name: 'Developer' })).toBeNull()

    act(() => { window.dispatchEvent(new CustomEvent('mc-dev-mode-changed', { detail: true })) })

    const devRow = await screen.findByRole('button', { name: 'Developer' })
    expect(devRow.querySelector('.animate-pulse')).toBeTruthy()

    fireEvent.click(devRow)
    await screen.findByTestId('developer-page')
    expect(devRow.querySelector('.animate-pulse')).toBeNull()
  })

  it('drops the Developer rail entry when developer mode is switched back off', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    act(() => { window.dispatchEvent(new CustomEvent('mc-dev-mode-changed', { detail: true })) })
    expect(await screen.findByRole('button', { name: 'Developer' })).toBeInTheDocument()

    act(() => { window.dispatchEvent(new CustomEvent('mc-dev-mode-changed', { detail: false })) })
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Developer' })).toBeNull())
  })
})
