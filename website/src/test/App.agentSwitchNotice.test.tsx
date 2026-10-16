import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react'
import App from '../App'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import type { RootState } from '../store'
import { createTestStore, renderWithProviders } from './helpers'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  const defaults: Record<string, unknown> = {
    approvals: [],
    chatSlots: [],
    crons: { jobs: [] },
    kirocrewConfig: { agent: { acp_backend: 'claude' } },
    listApps: [],
    listInstances: { instances: [], warm_set_cap: 5 },
    listRegistry: { apps: [], categoryOrder: [], editorialSections: [] },
    mobileConnectMethods: { methods: [] },
    notifications: { notifications: [] },
    sessionsUsage: { usage: null },
    status: { version: '1.0.0' },
    system: { mem_used_gb: 4, mem_total_gb: 16, cpu_pct: 25, disk_total_gb: 100, disk_free_gb: 60 },
    themeBoot: { mode: '', color: '', onboarded: true, import_onboarded: true, privacy_acked: true },
    themes: { themes: [] },
  }
  const api = new Proxy({} as Record<string, unknown>, {
    get(_target, name: string) {
      if (name === 'then') return undefined
      return () => Promise.resolve(structuredClone(name in defaults ? defaults[name] : {}))
    },
  })
  return { ...actual, api, isAuthBannerShown: () => false }
})
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page" /> }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../hooks/useWebSocket', () => ({
  useWebSocket: () => ({ subscribeLogs: () => {}, subscribeSubagents: () => {}, forceReconnect: () => {} }),
}))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' }) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))

type Notice = NonNullable<RootState['chat']['agentSwitchNotice']>

const initialChat = chatReducer(undefined, { type: '@@INIT' })
const initialDashboard = dashboardReducer(undefined, { type: '@@INIT' })

function renderNotice(notice: Notice) {
  const store = createTestStore({
    chat: { ...initialChat, agentSwitchNotice: notice },
    dashboard: { ...initialDashboard, connected: true },
  })
  return renderWithProviders(<App />, { route: '/chat', store })
}

describe('agent switch notice', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    localStorage.setItem('mc-onboarded', '1')
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ enabled: true, sessions: [] }),
      { status: 200, headers: { 'content-type': 'application/json' } },
    )))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    cleanup()
  })

  it('renders an info notice as a neutral status card with no link, and dismisses on the X', async () => {
    renderNotice({ message: 'No recently closed sessions.', tone: 'info' })

    const status = (await screen.findByText('No recently closed sessions.')).closest('[role="status"]')
    expect(status).not.toBeNull()
    expect(status).toHaveClass('border-border')
    expect(status!.querySelector('a')).toBeNull()
    expect(screen.queryByRole('link', { name: 'Open Older Sessions' })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

    await waitFor(() => expect(screen.queryByText('No recently closed sessions.')).not.toBeInTheDocument())
  })

  it('renders a non-info notice as the warning-bordered status card without an ask-the-agent control', async () => {
    renderNotice({ message: 'A turn is already in flight.' })

    const status = (await screen.findByText('A turn is already in flight.')).closest('[role="status"]')
    expect(status).not.toBeNull()
    // The warn border is an inline color-mix() value, which jsdom's CSS parser
    // drops, so the branch is told apart by the neutral class being absent.
    expect(status).not.toHaveClass('border-border')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('Ask the agent')).toBeNull()
    expect(status!.querySelectorAll('button')).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Dismiss' })).toBeInTheDocument()
  })
})
