/**
 * Linux desktop shell — header inset contract (FRAMELESS window).
 *
 * On CSD-preferring Wayland desktops the Electron shell goes frameless
 * (electron/linux-frame.js) and the main process injects a caption-control
 * cluster at the window's top-right (#electron-linux-controls). The SPA must
 * reserve that corner via `.linux-electron` (right inset), and must NOT apply
 * the mac (84px left) or win (142px right) insets.
 *
 * Deliberately NO vi.mock of ../lib/electron here: `window.kirocrew` is set
 * in a hoisted block BEFORE the module loads, so the real derivation in
 * src/lib/electron.ts is what is under test — a refactor that mis-classifies
 * a Linux shell as mac/win, or stops reading the preload's linuxFrameless
 * flag, fails this test. (electronPlatform.test.ts covers the framed
 * window, which needs no inset at all.)
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'

// Must run before src/lib/electron.ts is imported (module-level consts).
vi.hoisted(() => {
  ;(window as unknown as { kirocrew: { isElectron: boolean; platform: string; linuxFrameless: boolean } }).kirocrew = { isElectron: true, platform: 'linux', linuxFrameless: true }
})

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import App from '../App'
import { isLinuxFramelessElectron, isMacElectron, isWinElectron } from '../lib/electron'

describe('App shell — frameless Linux Electron header inset', () => {
  it('derives the platform consts from the preload bridge (real module)', () => {
    expect(isLinuxFramelessElectron).toBe(true)
    expect(isMacElectron).toBe(false)
    expect(isWinElectron).toBe(false)
  })

  it('applies linux-electron (caption-control inset) and neither mac nor win inset', async () => {
    const { container } = renderWithProviders(<App />, { route: '/chat' })
    await screen.findByTestId('chat-page')

    // The caption-control cluster injected by the main process occupies the
    // header's top-right; .linux-electron reserves that corner (index.css).
    expect(container.querySelector('.linux-electron')).toBeTruthy()
    // The mac 84px left inset and win 142px right inset must not apply.
    expect(container.querySelector('.mac-electron')).toBeNull()
    expect(container.querySelector('.win-electron')).toBeNull()
  })
})
