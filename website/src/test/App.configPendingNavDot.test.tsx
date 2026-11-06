/**
 * The Settings nav dot lights while a settings file edited outside the dashboard
 * waits for review (GET /api/security/config-pending `count > 0`), named for that
 * cause, and stays dark at 0. Same dot the update nudge uses.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { i18nT } from '../i18n/t'
import { renderWithProviders } from './helpers'
import type { RootState } from '../store'
import App from '../App'

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))

const { pending } = vi.hoisted(() => ({ pending: { count: 0 } }))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ version: '0.12.0', update_available: false, update_check_status: 'succeeded' }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4, mem_total_gb: 16, cpu_pct: 25, disk_total_gb: 100, disk_free_gb: 60 }),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    changelog: vi.fn().mockResolvedValue({ content: '' }),
    configPending: vi.fn().mockImplementation(async () => ({
      enforcing: true,
      count: pending.count,
      pending: pending.count ? [{ leaf: 'config.json', signature: 's', disk_valid: true, changes: [], truncated: false }] : [],
    })),
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
  value: vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
})
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

const state = {
  dashboard: {
    connected: true,
    slots: [],
    approvalMode: 'normal',
    status: { platform: 'linux', version: '0.12.0', update_available: false, update_check_status: 'succeeded' },
  } as unknown as RootState['dashboard'],
}

const dotName = () => i18nT('pages.settings.securityPanel.configPending.nav_dot')

describe('Settings nav dot for settings files changed outside the dashboard', () => {
  beforeEach(() => {
    localStorage.setItem('mc-last-version', '0.12.0')
  })

  it('lights, named for the review, while a file is pending', async () => {
    pending.count = 1
    renderWithProviders(<App />, { route: '/chat', preloadedState: state })
    expect(await screen.findByRole('status', { name: dotName() })).toBeInTheDocument()
  })

  it('lights while the bell note is muted: it reads the pending count only', async () => {
    pending.count = 1
    const { api } = await import('../api/client')
    const silenced = {
      ts: '2026-11-06T00:00:00Z', kind: 'agent', source: 'system', channel: 'system.approval',
      priority: 'passive', silenced: true, title: 't', body: 'b', config_pending_leaf: 'config.json',
    }
    ;(api.notifications as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ notifications: [silenced] })
    renderWithProviders(<App />, {
      route: '/chat',
      preloadedState: {
        ...state,
        notifications: {
          items: [silenced], clearSeq: 0, ackSeq: 0, ackSeqByTs: {}, retiredApprovals: {}, approvalDecisions: {}, dismissFailed: {},
        } as unknown as RootState['notifications'],
      },
    })
    expect(await screen.findByRole('status', { name: dotName() })).toBeInTheDocument()
  })

  it('stays dark when nothing is pending', async () => {
    pending.count = 0
    const { api } = await import('../api/client')
    renderWithProviders(<App />, { route: '/chat', preloadedState: state })
    await waitFor(() => expect(api.configPending).toHaveBeenCalled())
    // The read the dot is derived from has answered (count 0) before we look.
    await (api.configPending as ReturnType<typeof vi.fn>).mock.results.at(-1)!.value
    await screen.findByTestId('chat-page')
    expect(screen.queryByRole('status', { name: dotName() })).toBeNull()
  })
})
