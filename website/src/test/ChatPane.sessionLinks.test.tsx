import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, waitFor, fireEvent, screen } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A session link in a ChatPane transcript must resolve the way it does on the
 * single-chat page. A crewmate DM is the surface that reports it: a crewmate's
 * prose names sessions constantly, and until the pane was handed the session
 * triple its transcript rendered with `sessionRouting=false`, so a `/chat?sid=…`
 * link, a slot-key chip and a short name were all inert.
 *
 * Both conversational row kinds of a DM are pinned, because they come from
 * different entries in pages/chat/transcriptRenderers: the crewmate assistant
 * bubble and the steer-only user row. The real MarkdownRenderer is used — the
 * claim is about its own resolver, so mocking it would pin nothing. The
 * withheld-props control is the other half: a host that wires none of this must
 * keep the plain link it has today. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

const DM_SLOT = 'member-radar'
/** The session the DM's prose points at. Open, so it may chip. */
const THERE = 'chat-24-1784661951'
/** Written after THERE was minted — the SHORT-name form refuses a slot minted
 *  after the text naming it, so the row needs a write time. */
const WRITTEN = '2026-09-22T06:00:09Z'
const radar = { name: 'Radar' }

const MESSAGES = [
  { role: 'user', content: `pick up [my other session](/chat?sid=${THERE}) please`, cls: '', ts: '2026-09-22T06:00:00Z' },
  { role: 'assistant', content: `On it — dispatched [the worker](/chat?sid=${THERE}), tracked as \`${THERE}\`.`, cls: '', ts: WRITTEN },
]

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: DM_SLOT, messages: MESSAGES.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

/** The pane as the Members page mounts a DM: a crewmate identity and the
 *  steer-only composer, plus whatever session wiring the case is about. */
async function renderDm(wiring: Record<string, unknown>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={makeStore()}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane
              slotKey={DM_SLOT}
              crewmate={radar}
              busyMode="steer-only"
              frameless
              agentLocked
              activeSession={DM_SLOT}
              {...wiring}
            />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  )
  await waitFor(() => expect(screen.getByRole('link', { name: 'the worker' })).toBeTruthy())
}

const roster = () => new Map([[THERE, 'Fix the pagination bug']])

beforeEach(() => {
  vi.clearAllMocks()
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: MESSAGES, running: false, has_more: false, total: MESSAGES.length,
  })
})

describe('a session link in a crewmate DM', () => {
  it("the reply's link switches in place instead of navigating", async () => {
    const onSessionOpen = vi.fn()
    await renderDm({ onSessionOpen, sessions: roster() })
    const link = screen.getByRole('link', { name: 'the worker' })
    // In-app navigation, so no new tab and no muted (unresolvable) styling.
    expect(link).not.toHaveAttribute('target')
    expect(link.className).not.toContain('text-muted')
    fireEvent.click(link)
    expect(onSessionOpen).toHaveBeenCalledWith(THERE)
  })

  it("the reply's slot-key chip opens the session", async () => {
    const onSessionOpen = vi.fn()
    await renderDm({ onSessionOpen, sessions: roster() })
    const chip = screen.getByText(THERE)
    expect(chip.tagName).toBe('CODE')
    expect(chip).toHaveAttribute('data-session-key', THERE)
    fireEvent.click(chip)
    expect(onSessionOpen).toHaveBeenCalledWith(THERE)
  })

  it("the user's own pasted link resolves too", async () => {
    const onSessionOpen = vi.fn()
    await renderDm({ onSessionOpen, sessions: roster() })
    // The user row is the steer-only entry's, a different renderer from the
    // reply above, so it is wired separately and pinned separately.
    const link = screen.getByRole('link', { name: 'my other session' })
    expect(link).not.toHaveAttribute('target')
    fireEvent.click(link)
    expect(onSessionOpen).toHaveBeenCalledWith(THERE)
  })

  it('a host that wires nothing keeps the plain link: no handler, no interception', async () => {
    await renderDm({})
    const link = screen.getByRole('link', { name: 'the worker' })
    // Unrouted, so it stays an ordinary external-style link rather than a
    // swallowed no-op — the pre-change behaviour of every other pane host.
    expect(link).toHaveAttribute('target', '_blank')
    const evt = createClick()
    link.dispatchEvent(evt)
    expect(evt.defaultPrevented).toBe(false)
    // And the key renders as text, not an affordance that cannot act.
    expect(screen.getByText(THERE)).not.toHaveAttribute('data-session-key')
  })

  it('withheld roster (offline) leaves the link alone even with a handler', async () => {
    const onSessionOpen = vi.fn()
    await renderDm({ onSessionOpen })
    fireEvent.click(screen.getByRole('link', { name: 'the worker' }))
    expect(onSessionOpen).not.toHaveBeenCalled()
  })
})

function createClick(): MouseEvent {
  return new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
}
