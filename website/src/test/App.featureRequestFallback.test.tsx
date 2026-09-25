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
import { FEATURE_REQUEST_ROW_META_KEY } from '../prompts/featureRequest'
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
    localStorage.clear()
    sendChatMock.mockReset()
    createChatSlotMock.mockReset()
    createChatSlotMock.mockResolvedValue({ key: 'fr-slot', name: 'New chat' })
  })

  it('stamps the row it sends -- the same sendId and marker on the bubble and on the wire -- and keeps nothing on the client', async () => {
    // Capacity available: the accepted-receipt path from the #4198 tests, with
    // the one addition that makes the fallback possible later -- the send's
    // `meta` says this user row IS the feature request. The gateway persists a
    // send's meta verbatim on the row and echoes it, so that is the whole
    // record: no store field, no localStorage key.
    sendChatMock.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    expect(chat.activeSlot).toBe('fr-slot')
    const bubble = chat.messages.find(m => m.role === 'user' && m.content === i18nT('app.i_d_like_to_request_a_feature'))
    const sendId = bubble?.meta?.sendId
    expect(sendId).toMatch(/^s-/)
    expect(bubble?.meta?.[FEATURE_REQUEST_ROW_META_KEY]).toBe(true)
    // The wire carries the identical meta, so the echo reconciles the bubble by
    // id and the persisted row carries the stamp the transcript reads.
    expect(sendChatMock).toHaveBeenCalledTimes(1)
    expect(sendChatMock.mock.calls[0][4]).toEqual({ sendId, [FEATURE_REQUEST_ROW_META_KEY]: true })
    // Nothing per-slot is remembered here: the row is the record (First
    // Principles review on this change).
    expect(Object.keys(localStorage).filter(k => k.startsWith('mc-feature-request-seed:'))).toEqual([])
    expect('featureRequestSeeds' in chat).toBe(false)
    // Unchanged agent flow: running on, no error row.
    expect(chat.messages.some(m => m.role === 'error')).toBe(false)
    expect(chat.slotRunning).toBe(true)
  })

  it('stamps the bubble BEFORE the send settles, so a refusal that lands first still finds a marked row', async () => {
    // The usage-limit row arrives over the WebSocket after the turn starts; a
    // stamp written only on a happy receipt would miss the one case it exists
    // for. Same for the #4198 shapes: the stamp is a fact about the sent row,
    // and their error rows keep rendering exactly as before (no structural
    // kind, so the transcript never offers the form on them -- see
    // transcriptRenderers.test).
    sendChatMock.mockResolvedValue({ ok: false, json: vi.fn().mockResolvedValue({ ok: false, error: 'slot agent mismatch' }) })
    const { store } = await clickRequestFeature()

    const chat = store.getState().chat
    const bubble = chat.messages.find(m => m.role === 'user' && m.content === i18nT('app.i_d_like_to_request_a_feature'))
    expect(bubble?.meta?.[FEATURE_REQUEST_ROW_META_KEY]).toBe(true)
    expect(chat.messages.some(m => m.role === 'error' && m.content === i18nT('pages.chatPage.send_failed_with_error', { error: 'slot agent mismatch' }))).toBe(true)
    expect(chat.slotRunning).toBe(false)
  })

  it('says on the button itself that the action starts a chat and spends the plan\'s monthly usage', async () => {
    sendChatMock.mockResolvedValue({ ok: true, json: vi.fn().mockResolvedValue({ ok: true }) })
    const { button } = await clickRequestFeature()
    // Real copy, not a hover-only `title`: the explanation is a described-by
    // tooltip that shows on keyboard focus too (see FeedbackPill.test).
    expect(button).toHaveAttribute('aria-describedby')
    expect(button).not.toHaveAttribute('title')
    // The visible label is still the action, so the accessible name is unchanged.
    expect(button).toHaveAccessibleName(i18nT('app.request_a_feature_2'))
  })
})
