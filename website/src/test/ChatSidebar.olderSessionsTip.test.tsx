/**
 * Test: the Older Sessions header explains what the pane lists, and the
 * explanation's glyph does not operate the disclosure it sits in.
 *
 * WHY THIS EXISTS
 *
 * The header reads "Older Sessions", and the pane under it lists every saved
 * session that is not open as a tab — a session closed a minute ago sits
 * under TODAY. Nothing in the label said so. The InfoTip beside the label
 * carries that copy; this test pins that it is there and that it states the
 * list's actual rule rather than the "older" the label implies.
 *
 * The glyph is a <button> INSIDE a role=button row whose click and Enter/Space
 * toggle the pane. A click on the glyph must pin the tip, not collapse the
 * pane; Enter on the focused glyph must reach the glyph's own activation
 * rather than be swallowed by the row's preventDefault. The row's key handler
 * therefore ignores keys whose target is a control inside it — the same guard
 * the history rows already use — and this test exercises both paths, plus the
 * Clear button that shares the row and relies on the same guard.
 *
 * Mock scaffolding mirrors ChatSidebar.historyDeepLink.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { i18nT } from '../i18n/t'
import en from '../i18n/locales/en.json'

const { sessionsMock } = vi.hoisted(() => ({
  sessionsMock: vi.fn().mockResolvedValue({ sessions: [], has_more: false }),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList',
          'renameSlot', 'forkSession', 'sessionsSearch',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
      sessions: sessionsMock,
    },
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot, ChatHistoryItem } from '../types'
import type { RootState } from '../store'

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string): ChatHistoryItem => ({
  key, title, last_ts: '2026-01-01T00:00:00Z',
} as unknown as ChatHistoryItem)

function renderSidebar() {
  const slots = [slot('s1', 'Session 1')]
  const history = [histItem('h1', 'A closed conversation')]
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history, historyHasMore: false, historyOffset: history.length,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'s1'}
              unreadSlots={[]}
              history={history}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The disclosure row: `aria-label` names it, so the glyph inside does not
 *  change its accessible name. */
const disclosure = () => screen.getByRole('button', { name: /^older sessions$/i })
/** The InfoTip's trigger, scoped to the header so a tip elsewhere in the
 *  sidebar cannot satisfy the query. */
const glyph = () => {
  const found = disclosure().querySelector('button[aria-label="More information"]')
  if (!(found instanceof HTMLButtonElement)) throw new Error('info glyph not rendered in the Older Sessions header')
  return found
}

const TIP = en.pages.chatSidebar.older_sessions_tip

describe('ChatSidebar — the Older Sessions header explains the list', () => {
  beforeEach(() => {
    sessionsMock.mockClear()
    localStorage.clear()
    window.history.replaceState({}, '', '/chat')
  })
  afterEach(() => { window.history.replaceState({}, '', '/') })

  it('carries an info glyph whose tip states the real rule, not the label', () => {
    renderSidebar()
    // Closed, the tip text rides the glyph's native `title` (InfoTip's
    // fallback), so the copy is reachable without opening the bubble.
    expect(glyph().getAttribute('title')).toBe(TIP)
    // The rule the pane actually applies (GET /api/sessions?exclude_open=1
    // &user_only=1): every saved session, where "saved" means "not open as a
    // tab" with no age bound (a session closed a second ago qualifies);
    // channel conversations (Slack, Discord, ...) stay in; and opening a row
    // restores it as a tab. Each clause is asserted so a rewrite that drops
    // one fails here, not in a user's hover.
    expect(TIP).toMatch(/every saved session/i)
    expect(TIP).toMatch(/not open as a tab/i)
    expect(TIP).toMatch(/just closed/i)
    expect(TIP).toMatch(/Slack or Discord/i)
    expect(TIP).toMatch(/bring it back as a tab/i)
  })

  it('a click on the glyph pins the tip and leaves the pane as it was', () => {
    renderSidebar()
    expect(disclosure().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(glyph())
    // Pinned: the bubble is open and the glyph's `title` fallback is dropped
    // so the browser's own tooltip never doubles it.
    expect(glyph().getAttribute('aria-expanded')).toBe('true')
    expect(glyph().getAttribute('title')).toBeNull()
    expect(screen.getByRole('tooltip').textContent).toBe(TIP)
    // The row's own click would have opened the pane; the glyph's did not
    // reach it.
    expect(disclosure().getAttribute('aria-expanded')).toBe('false')
    expect(sessionsMock).not.toHaveBeenCalled()
  })

  it('Enter and Space on the focused glyph do not toggle the pane', () => {
    renderSidebar()
    const g = glyph()
    // focus() runs InfoTip's onFocus, a state update, so it needs the barrier
    // fireEvent supplies on its own.
    act(() => { g.focus() })
    for (const key of ['Enter', ' ']) {
      const event = fireEvent.keyDown(g, { key })
      // The row ignores a key whose target is a control inside it, so the
      // event keeps its default (the button's own activation) instead of
      // being cancelled on the way up.
      expect(event, `keydown ${JSON.stringify(key)} was cancelled by the row`).toBe(true)
      expect(disclosure().getAttribute('aria-expanded')).toBe('false')
    }
    // The row itself still toggles on the same keys.
    fireEvent.keyDown(disclosure(), { key: 'Enter' })
    expect(disclosure().getAttribute('aria-expanded')).toBe('true')
  })

  it('Enter on the Clear button reaches Clear instead of collapsing the pane', async () => {
    // Clear renders only while the pane is open with rows in it, so the open
    // fetch must come back non-empty.
    sessionsMock.mockResolvedValueOnce({
      sessions: [histItem('h1', 'A closed conversation')], has_more: false,
    })
    // Clear's own activation runs clearClosedSessionsWithConfirm (ChatSidebar),
    // which asks confirm() before deleting anything. Answer no, so even a
    // runtime that synthesises a click from the keydown deletes nothing.
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderSidebar()
    fireEvent.keyDown(disclosure(), { key: 'Enter' })
    expect(disclosure().getAttribute('aria-expanded')).toBe('true')
    const clear = await waitFor(() => screen.getByRole('button', { name: i18nT('pages.chatSidebar.clear') }))
    expect(disclosure().contains(clear)).toBe(true)
    const event = fireEvent.keyDown(clear, { key: 'Enter' })
    // Not cancelled on the way up: the row saw a key whose target was Clear,
    // not itself, and left its default (the button's activation) alone.
    expect(event, 'keydown Enter on Clear was cancelled by the row').toBe(true)
    // And the row did not treat the key as its own toggle.
    expect(disclosure().getAttribute('aria-expanded')).toBe('true')
    expect(confirm).not.toHaveReturnedWith(true)
    confirm.mockRestore()
  })
})
