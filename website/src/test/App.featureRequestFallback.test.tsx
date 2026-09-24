/**
 * Test: "Request a Feature" keeps a non-inference exit when the plan is spent
 * (#13342).
 *
 * The action is an agent turn by design: the `feature-request` skill drafts and
 * files the issue conversationally, so it consumes metered inference. At the
 * monthly usage limit the backend refuses that turn and the transcript gets a
 * terminal error row -- which used to be the end of the road, precisely when a
 * user with no credits left wanted to report something. The fix has three
 * halves, and this file pins the App-level one: the flow RECORDS which slot it
 * created, so the transcript can later recognise a `usage_limit` error row in
 * that slot and offer the repo's feature-request form on it (the card and the
 * renderer are pinned in ErrorCard.test.tsx and transcriptRenderers.test.tsx;
 * the wiring through ChatPage in ChatPage.featureRequestFallback.test.tsx).
 *
 * Same mocks as App.featureRequestFailure.test.tsx, so the two files drive the
 * same real flow: `createSlot` thunk, seed, `sendTurn`, receipt.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { i18nT } from '../i18n/t'
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
    await new Promise(res => setTimeout(res, 0))
    for (let i = 0; i < 10; i++) await Promise.resolve()
  })
  return { ...rendered, button }
}

describe('Request a Feature — the non-inference exit is wired (#13342)', () => {
  beforeEach(() => {
    sendChatMock.mockReset()
    createChatSlotMock.mockReset()
    createChatSlotMock.mockResolvedValue({ key: 'fr-slot', name: 'New chat' })
  })

  it('records the slot it created as the feature-request slot, and leaves the agent flow untouched', async () => {
    // Capacity available: the accepted-receipt path from the #4198 tests, with
    // the one addition that makes the fallback possible later -- the slot is
    // remembered as belonging to this flow.
    sendChatMock.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.activeSlot).toBe('fr-slot')
    expect(chat.featureRequestSlots).toContain('fr-slot')
    // Unchanged agent flow: the optimistic bubble, running on, no error row, one send.
    expect(chat.messages.some(m => m.role === 'user' && m.content === i18nT('app.i_d_like_to_request_a_feature'))).toBe(true)
    expect(chat.messages.some(m => m.role === 'error')).toBe(false)
    expect(chat.slotRunning).toBe(true)
    expect(sendChatMock).toHaveBeenCalledTimes(1)
  })

  it('marks the slot BEFORE the send settles, so a refusal that lands first still finds it', async () => {
    // The usage-limit row arrives over the WebSocket after the turn starts; a
    // marker written only on a happy receipt would miss the one case it exists
    // for. Same for the #4198 shapes: the marker is a fact about the slot, and
    // their rows keep rendering exactly as before (no structural kind, so the
    // transcript never offers the form on them -- see transcriptRenderers.test).
    sendChatMock.mockResolvedValue({ ok: false, json: vi.fn().mockResolvedValue({ ok: false, error: 'slot agent mismatch' }) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.featureRequestSlots).toContain('fr-slot')
    expect(chat.messages.some(m => m.role === 'error' && m.content === i18nT('pages.chatPage.send_failed_with_error', { error: 'slot agent mismatch' }))).toBe(true)
    expect(chat.slotRunning).toBe(false)
  })

  it('says on the button itself that the action starts an agent conversation', async () => {
    sendChatMock.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) })
    const { button } = await clickRequestFeature()
    expect(button).toHaveAttribute('title', i18nT('components.feedbackPill.request_feature_starts_agent'))
    // The visible label is still the action, so the accessible name is unchanged.
    expect(button).toHaveAccessibleName(i18nT('app.request_a_feature_2'))
  })
})
