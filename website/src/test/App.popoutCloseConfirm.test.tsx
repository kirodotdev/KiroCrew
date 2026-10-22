/**
 * The pop-out and embed frames render outside the dashboard's local pane, so
 * App must host their own copies of the ⌘W close question and of the app
 * error notice, or each waits on a surface nobody sees.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, cleanup, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { setActiveSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'
import App from '../App'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { appErrorNotice } from '../lib/appNotice'

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

describe.each([
  ['pop-out', '/popout/chat/x?sid=slot-1'],
  ['embed', '/embed/chat?sid=slot-1'],
])('%s frame surfaces', (_frame, route) => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    window.history.pushState({}, '', route)
  })
  afterEach(() => { cleanup(); window.history.pushState({}, '', '/'); localStorage.clear() })

  it('renders the close question', async () => {
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'slot-1', title: 'Work' } as ChatSlot]))
    store.dispatch(setActiveSlot('slot-1'))
    renderWithProviders(<NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>, { route, store })
    await act(async () => { fireEvent.keyDown(document.body, { key: 'w', code: 'KeyW', ctrlKey: true }) })
    expect(await screen.findByRole('button', { name: 'Close session' })).toBeInTheDocument()
  })

  it('shows an app error notice, which the frame has no banner for', async () => {
    renderWithProviders(<NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>, { route, store: createTestStore() })
    act(() => { appErrorNotice('Reload failed in this frame') })
    expect(await screen.findByText('Reload failed in this frame')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText('Reload failed in this frame')).not.toBeInTheDocument()
  })

  it('announces a repeated identical failure as a fresh alert', async () => {
    renderWithProviders(<NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>, { route, store: createTestStore() })
    act(() => { appErrorNotice('Reload failed again') })
    const first = await screen.findByRole('alert')
    act(() => { appErrorNotice('Reload failed again') })
    const second = screen.getByRole('alert')
    expect(second).toHaveTextContent('Reload failed again')
    expect(second).not.toBe(first)
  })
})
