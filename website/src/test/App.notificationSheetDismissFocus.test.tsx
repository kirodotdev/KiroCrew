/**
 * Notification Center sheet: a Dismiss in the sheet's detail panel hands
 * keyboard focus to the sheet's own feed list. The sheet's portal root carries
 * data-notif-scope; without it the pressed Dismiss unmounts and focus falls
 * to <body>.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import type { RootState } from '../store'
import type { Notification } from '../types'

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

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    ackNotification: vi.fn().mockResolvedValue({}),
    deleteNotification: vi.fn().mockResolvedValue({}),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
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

const approval: Notification = {
  kind: 'approval', ts: '2026-05-29T10:02:00Z', title: 'Tool approval: shell',
  body: 'ls', approval_id: 'apr-gone', acked: true, _local: true,
}

describe('Notification Center sheet: Dismiss focus', () => {
  afterEach(() => { localStorage.clear() })

  it('Dismiss on a retired approval hands focus to the sheet\'s own feed list', async () => {
    const store = createTestStore({
      notifications: { items: [approval], retiredApprovals: { [approval.ts]: true } } as RootState['notifications'],
    } as Partial<RootState>)
    // Past first run, so no onboarding chapter takes focus for its heading.
    localStorage.setItem('mc-onboarded', '1')
    renderWithProviders(<App />, { store, route: '/chat' })
    fireEvent.click(await screen.findByLabelText('Notifications'))
    // The sheet's feed column (the scope marker sits on the portal root above it).
    const sheet = () => document.querySelector('[data-nc-phase]') as HTMLElement
    fireEvent.click(within(sheet()).getByText(approval.title))
    const dismiss = await screen.findByTestId('notif-detail-dismiss')
    dismiss.focus()
    act(() => { dismiss.click() })
    await waitFor(() => {
      expect(document.activeElement).toBe(within(sheet()).getByTestId('notification-feed-list'))
    })
  })
})
