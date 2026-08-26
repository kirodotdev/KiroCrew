/**
 * Test: the "Request a Feature" flow reports a send the server never accepted
 * (#4198).
 *
 * The flow dispatches an optimistic user bubble and `setSlotRunning(true)`,
 * then awaits `api.sendChat` — and an HTTP 4xx/5xx RESOLVES rather than
 * rejecting, so the old `catch { /* WS will handle response *\/ }` never saw
 * the errors that matter: a refused send left the bubble on screen next to a
 * slot stuck `running`, with nothing said. These tests pin the fix on both
 * failure shapes (a resolved not-ok receipt and a transport reject): an error
 * row lands in the transcript that owns the bubble, reusing the existing
 * `pages.chatPage.send_failed` catalog entry, and the optimistic running state
 * is undone. The accepted-receipt case pins that success stays untouched.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'
import type { RootState } from '../store'
import App from '../App'

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
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

const { sendChatMock, createChatSlotMock } = vi.hoisted(() => ({
  sendChatMock: vi.fn(),
  createChatSlotMock: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: 'none' }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
    skills: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    createChatSlot: createChatSlotMock,
    sendChat: sendChatMock,
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

const connectedState = {
  dashboard: { connected: true, status: { platform: 'darwin' }, slots: [], approvalMode: 'normal' } as unknown as RootState['dashboard'],
}

/** Mount, click the feedback pill's Request-a-Feature action, and settle. */
async function clickRequestFeature() {
  const rendered = renderWithProviders(<App />, { route: '/chat', preloadedState: connectedState })
  const button = await screen.findByRole('button', { name: i18nT('app.request_a_feature_2') })
  await act(async () => {
    fireEvent.click(button)
    // Drain the flow's await chain: skills → createSlot thunk → sendChat →
    // receipt. Macrotask first so thunk middleware settles, then microtasks.
    await new Promise(res => setTimeout(res, 0))
    for (let i = 0; i < 10; i++) await Promise.resolve()
  })
  return rendered
}

describe('Request a Feature — failed send is reported (#4198)', () => {
  beforeEach(() => {
    sendChatMock.mockReset()
    createChatSlotMock.mockReset()
    createChatSlotMock.mockResolvedValue({ key: 'fr-slot', name: 'New chat' })
  })

  it('appends an error row and undoes the running state on a resolved not-ok receipt', async () => {
    // The real-world shape: the server answers the POST (e.g. 409/403), so the
    // promise RESOLVES — the failure must come from reading the receipt.
    sendChatMock.mockResolvedValue({ ok: false, json: vi.fn().mockResolvedValue({ ok: false, error: 'slot agent mismatch' }) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.activeSlot).toBe('fr-slot')
    // The optimistic bubble stays (the request WAS made), the error row lands
    // under it carrying the server's own explanation, and running is undone.
    const roles = chat.messages.map(m => m.role)
    expect(roles).toContain('user')
    expect(chat.messages.some(m => m.role === 'error' && m.content === i18nT('pages.chatPage.send_failed_with_error', { error: 'slot agent mismatch' }))).toBe(true)
    expect(chat.slotRunning).toBe(false)
  })

  it('falls back to the shared send-failed string when the receipt has no reason', async () => {
    // A 2xx whose body parsed but carries neither flag: still a refusal, and
    // the one with no reason to quote.
    sendChatMock.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({}) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.messages.some(m => m.role === 'error' && m.content === i18nT('pages.chatPage.send_failed'))).toBe(true)
    expect(chat.slotRunning).toBe(false)
  })

  it('reports a transport reject the same way', async () => {
    sendChatMock.mockRejectedValue(new Error('network down'))
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.messages.some(m => m.role === 'error' && m.content === i18nT('pages.chatPage.send_failed'))).toBe(true)
    expect(chat.slotRunning).toBe(false)
  })

  it('says NOTHING when a 2xx receipt will not parse — the request was accepted (#4217)', async () => {
    // A truncated or proxy-mangled body on an ACCEPTED post proves only that the
    // request got through, so the turn may be running right now. Reporting a
    // failure here tells the user to resend a request that already went out.
    sendChatMock.mockResolvedValue({ ok: true, json: vi.fn().mockRejectedValue(new Error('unexpected end of JSON input')) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.messages.some(m => m.role === 'error')).toBe(false)
    // Running is NOT undone either: the pill's own optimistic state is the only
    // thing standing in for a turn whose stream is still to come.
    expect(chat.slotRunning).toBe(true)
  })

  it('leaves an accepted send untouched — running stays on, no error row', async () => {
    sendChatMock.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.messages.some(m => m.role === 'error')).toBe(false)
    expect(chat.slotRunning).toBe(true)
  })
})
