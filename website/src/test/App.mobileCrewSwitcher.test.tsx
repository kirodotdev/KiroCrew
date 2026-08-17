/**
 * Test: the crew switcher is reachable at phone widths.
 *
 * The switcher used to be gated out below 768px entirely, which left a phone
 * with no route to another crew at all (the command palette could still switch,
 * but nothing said so). It renders at every width now; what keeps it fitting is
 * the identity group's collapse ladder in index.css, asserted by
 * topbarMenuButtonNarrow.test.ts. This test pins the part CSS cannot: that the
 * component is MOUNTED on mobile, with its dropdown trigger — the control the
 * ladder protects — present.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import type { RootState } from '../store'
import App from '../App'

vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => true }))
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

// One remembered remote crew: the switcher renders only when at least one exists
// (visibleInstanceTabs), so a single-crew user's header is unchanged. Built inside
// vi.hoisted because the mock factory below is hoisted above module scope.
const { crew } = vi.hoisted(() => ({
  crew: {
    id: 'devbox',
    name: 'devbox',
    ssh_host: 'devbox.example',
    remote_port: 5476,
    local_port: 5500,
    ttl: '8h',
    remote_bin: 'kirocrew',
    connection_method: 'ssh',
    ssm_target: '',
    aws_profile: '',
    aws_region: '',
    ssm_run_as: '',
    was_connected: true,
    status: { state: 'connected', unread: 0 },
  },
}))

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: null }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4, mem_total_gb: 16, cpu_pct: 25, disk_total_gb: 100, disk_free_gb: 60 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ active: true, instances: [crew], warm_set_cap: 5 }),
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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

describe('crew switcher at phone widths', () => {
  const state = {
    dashboard: { connected: true, status: { platform: 'linux' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
  }

  it('mounts the inline switcher and its dropdown trigger on mobile', async () => {
    renderWithProviders(<App />, { route: '/chat', preloadedState: state })
    // The trailing dropdown is the affordance that must survive: it lists every
    // crew, including the one on screen, so it alone is a complete switcher.
    expect(await screen.findByLabelText('Switch crew')).toBeTruthy()
    // The nav button shares the group and must not be crowded out of the DOM.
    expect(screen.getByLabelText('Open menu')).toBeTruthy()
  })
})
