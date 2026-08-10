/**
 * Clipboard image paste at the PAGE level (issue #2489).
 *
 * ChatInput's paste handler funnels clipboard images into the SAME
 * onUploadFiles callback the file picker and drag-and-drop use — so every
 * constraint ChatPage.uploadFiles enforces (50 MB size cap, 20-file cap,
 * error banner) applies to a pasted image exactly as it does to a picked
 * one. These tests pin that funnel end-to-end through a mounted ChatPage:
 *  - a pasted image reaches api.uploadFiles (attach works)
 *  - an OVERSIZE pasted image is rejected with the same user-visible error
 *    a picked oversize file gets, and never reaches the server
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: ['/uploads/x.png'] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore(activeSlot: string, slots: { key: string; mode?: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true, slots: slots.map(s => ({ key: s.key, messages: 1, running: false, mode: s.mode || '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderAndWaitForInput(store: ReturnType<typeof makeStore>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter><ChatPage /></MemoryRouter>
        </ThemeProvider>
      </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

/** Fire an image-only clipboard paste (the OS-screenshot clipboard shape). */
const pasteImage = (input: HTMLElement, file: File) =>
  fireEvent.paste(input, {
    clipboardData: {
      types: ['Files'],
      items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
      getData: () => '',
    },
  })

/** A File whose reported size exceeds the 50 MB cap without allocating it. */
function oversizeImage(): File {
  const f = new File(['px'], 'huge.png', { type: 'image/png' })
  Object.defineProperty(f, 'size', { value: 51 * 1024 * 1024 })
  return f
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage clipboard image paste', () => {
  it('funnels a pasted image into the same upload path as the picker', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.uploadFiles).mockClear()

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const file = new File(['px'], 'screenshot.png', { type: 'image/png' })
    await act(async () => { pasteImage(screen.getByLabelText('Message input'), file) })

    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    const sent = vi.mocked(api.uploadFiles).mock.calls[0][0]
    expect(sent).toHaveLength(1)
    expect(sent[0].name).toBe('screenshot.png')
  })

  it('rejects an OVERSIZE pasted image with the picker path error and never uploads it', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.uploadFiles).mockClear()

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const big = oversizeImage()
    await act(async () => { pasteImage(screen.getByLabelText('Message input'), big) })

    // Exactly the file_too_large banner a picked oversize file produces —
    // resolved through the same i18n path the component uses, so the
    // surfaces cannot drift.
    const { i18nT } = await import('../i18n/t')
    const expected = i18nT('pages.chatPage.file_too_large', { name: 'huge.png' })
    expect(expected).toContain('huge.png') // guard: key resolved, not echoed
    await waitFor(() => expect(screen.getByText(expected)).toBeTruthy())
    expect(api.uploadFiles).not.toHaveBeenCalled()
  })
})
