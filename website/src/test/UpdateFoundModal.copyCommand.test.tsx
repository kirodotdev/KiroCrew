/**
 * Test: UpdateFoundModal's "Copy command" button must gate its confirmation on
 * whether the text actually reached the clipboard, not on the write merely
 * having been attempted.
 *
 * The modal is mounted through the real App shell, matching the pattern in
 * App.changelogModalApply.test.tsx: the command affordance is only wired up
 * from redux status there.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { RootState } from '../store'
import App from '../App'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

const { COMMAND, statusOverride } = vi.hoisted(() => ({
  COMMAND: 'python3 -m pip install --upgrade kiro-crew',
  statusOverride: { value: {} as Record<string, unknown> },
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockImplementation(async () => ({
      uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
      version: '0.2.0rc9', update_available: true, update_can_apply: false,
      update_latest_version: '0.3.0',
      update_check_status: 'succeeded', update_command: COMMAND,
      ...statusOverride.value,
    })),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 0, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    changelog: vi.fn().mockResolvedValue({ content: '## [0.2.0rc9]\n- a new entry\n' }),
    setAutoUpdate: vi.fn().mockResolvedValue({}),
    // Empty record: no prior snooze/skip for this version, so the popup opens.
    kirocrewConfig: vi.fn().mockResolvedValue({ dashboard: {} }),
    patchConfig: vi.fn().mockResolvedValue({}),
    checkUpdate: vi.fn().mockResolvedValue({ changes: '', check_status: 'succeeded' }),
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

vi.mock('../utils/clipboard', () => ({
  // Resolves TRUE by default: the real signature is `Promise<boolean>`, and a
  // bare `vi.fn()` returning undefined is falsy, so it could not tell a
  // successful clipboard write from a failed one.
  copyToClipboard: vi.fn().mockResolvedValue(true),
}))

import { copyToClipboard } from '../utils/clipboard'

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)',
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const wheelState = (over: Record<string, unknown> = {}) => {
  statusOverride.value = over
  return {
    dashboard: {
      connected: true,
      slots: [],
      approvalMode: 'normal',
      status: {
        platform: 'linux',
        version: '0.2.0rc9',
        update_available: true,
        update_latest_version: '0.3.0',
        update_can_apply: false,
        update_check_status: 'succeeded',
        update_command: COMMAND,
        ...over,
      },
    } as unknown as RootState['dashboard'],
  }
}

describe('UpdateFoundModal copy-command confirmation', () => {
  beforeEach(() => {
    // Matches the mocked status version, so the (unrelated) changelog modal
    // does not also open and compete for the same screen.
    localStorage.setItem('mc-last-version', '0.2.0rc9')
    statusOverride.value = {}
    vi.mocked(copyToClipboard).mockReset().mockResolvedValue(true)
  })

  it('confirms only once the text actually reached the clipboard', async () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: wheelState() })

    const button = await screen.findByText('Copy command')
    fireEvent.click(button)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith(COMMAND))
    expect(await screen.findByText('Copied')).toBeTruthy()
  })

  it('withholds the confirmation when the clipboard write fails', async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false)
    renderWithProviders(<App />, { route: '/chat', preloadedState: wheelState() })

    const button = await screen.findByText('Copy command')
    fireEvent.click(button)

    await waitFor(() => expect(copyToClipboard).toHaveBeenCalledWith(COMMAND))
    // No new user-facing failure string exists for this surface, so a failed
    // write must leave the button exactly as it was rather than lie.
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByText('Copied')).toBeNull()
    expect(screen.getByText('Copy command')).toBeTruthy()
  })
})
