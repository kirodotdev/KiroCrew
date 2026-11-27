/**
 * The in-app application menu on a frameless Linux window.
 *
 * A CSD Wayland compositor draws no window frame, so it draws no menu bar
 * either. Electron still installs the native menu, so every accelerator works,
 * but nothing renders it — the shell's own menu is the only visible path. This
 * pins which surfaces get that menu: Windows and frameless Linux, never a
 * framed Linux window (whose compositor draws the bar itself, so a second one
 * would be a duplicate) and never the phone form.
 *
 * The platform flags are module-level consts frozen at import, so they are
 * mocked here through one mutable record, which lets a single file exercise all
 * three surfaces. `App.linuxElectron.test.tsx` and `electronPlatform.test.ts`
 * own the derivation itself on both Linux branches; this file owns only which
 * render condition those flags select.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'

const electronEnv = vi.hoisted(() => ({
  isElectron: true,
  isWinElectron: false,
  isMacElectron: false,
  isLinuxFramelessElectron: false,
  shellServesLinuxAppMenu: true,
}))
const viewportForm = vi.hoisted(() => ({ mobile: false }))

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))

// Accessors, not values: the record is reassigned per test and the shell reads
// each flag at render time, so a frozen spread would pin one surface per file.
vi.mock('../lib/electron', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/electron')>()),
  get isElectron() { return electronEnv.isElectron },
  get isWinElectron() { return electronEnv.isWinElectron },
  get isMacElectron() { return electronEnv.isMacElectron },
  get isLinuxFramelessElectron() { return electronEnv.isLinuxFramelessElectron },
  get shellServesLinuxAppMenu() { return electronEnv.shellServesLinuxAppMenu },
}))

// Every export, not just the hook: `ThemeExperienceLayer` reads
// `useIsNarrowViewport` and `useKeyboardShortcuts` reads `MOBILE_BREAKPOINT`
// off this module, and a partial mock hands both of them undefined.
vi.mock('../hooks/useIsMobile', () => ({
  MOBILE_BREAKPOINT: 768,
  useIsNarrowViewport: () => viewportForm.mobile,
  useIsMobile: () => viewportForm.mobile,
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { available: false } }),
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
    constructor(status: number, message: string) { super(message); this.status = status }
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import App from '../App'

/** The menu's own landmark. The phone nav toggle carries the same accessible
 *  name, so the role is what tells the two apart. */
function titlebarMenu(): HTMLElement | null {
  return screen.queryByRole('navigation', { name: i18nT('app.open_menu') })
}

describe('App shell — the application menu on frameless Linux', () => {
  beforeEach(() => {
    localStorage.clear()
    electronEnv.isElectron = true
    electronEnv.isWinElectron = false
    electronEnv.isMacElectron = false
    electronEnv.isLinuxFramelessElectron = false
    electronEnv.shellServesLinuxAppMenu = true
    viewportForm.mobile = false
  })

  it('renders the menu on a frameless Linux window', async () => {
    electronEnv.isLinuxFramelessElectron = true
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    expect(titlebarMenu()).toBeTruthy()
  })

  // The shell and this bundle install separately, so an older shell can serve
  // a newer dashboard. Its preload still reports `linuxFrameless`, because a
  // frameless window is what it always was, but its main process answers
  // `app-menu:items` with `[]` -- its own guard is still Windows-only. Mounting
  // on the frameless flag alone therefore paints a trigger whose every popup
  // opens empty and whose every click refuses, with nothing on screen saying
  // why. The capability flag is the shell stating it serves this menu, so its
  // absence means an older shell and the trigger stays away.
  it('renders no menu when the installed shell does not serve it', async () => {
    electronEnv.isLinuxFramelessElectron = true
    electronEnv.shellServesLinuxAppMenu = false
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    expect(titlebarMenu()).toBeNull()
  })

  // The other true branch of the same condition. Without it, dropping
  // `isWinElectron` would still pass every other test in this file. The Linux
  // capability is held false on purpose: Windows has never needed it, so
  // gating the whole condition on it rather than only the Linux term would
  // take the menu away from every Windows window.
  it('still renders the menu on Windows', async () => {
    electronEnv.isWinElectron = true
    electronEnv.shellServesLinuxAppMenu = false
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    expect(titlebarMenu()).toBeTruthy()
  })

  // The most visible regression available: a framed window already has the
  // compositor's own menu bar, so a second one is a duplicate menu.
  it('renders no menu on a framed Linux window, which keeps its native bar', async () => {
    renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    expect(titlebarMenu()).toBeNull()
  })

  // Off a chat route on purpose: `mobileSingle` is
  // `isMobile && (isChat || membersSingleBar)` (shell/appShell.tsx), and it
  // drops the whole left cluster, so on /chat this would report absence without
  // the `!isMobile` guard having anything to do with it. Neither term is true on
  // /schedule. The phone nav toggle is asserted present as the control that the
  // narrow form really is active.
  it('renders no menu in the phone form', async () => {
    electronEnv.isLinuxFramelessElectron = true
    viewportForm.mobile = true
    renderWithProviders(<App />, { route: '/schedule' })
    await screen.findByRole('button', { name: i18nT('app.open_menu') })

    expect(titlebarMenu()).toBeNull()
  })
})
