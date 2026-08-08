/**
 * Regression test for the foreground transcript write-through
 * (fix/voice-session-above-router, round-7 GPT finding).
 *
 * BUG: `applyVoiceText`'s FOREGROUND path called `setInput(spliced)` and nothing
 * else. The persisted draft therefore still held the PRE-transcript text until
 * the `[input]` persist effect ran on a later render. React defers passive
 * effects until after paint, so a transcript resolving in the post-paint window
 * (an STT fetch/WS landing just after the sink's `useLayoutEffect` registered it
 * on remount) could be read back as the stale draft by the slot's draft-restore
 * effect (dep `[activeSlot]`) and clobbered by its `setInput(draftFallback)` —
 * the dictated words vanished.
 *
 * FIX: the foreground path now writes THROUGH to `inputRef.current` AND the
 * persisted draft store at the same time as `setInput`, so the two writers
 * CONVERGE instead of racing. Whichever effect runs second reads a draft that
 * already contains the transcript.
 *
 * The assertion deliberately targets the PERSISTED draft rather than trying to
 * interleave React's scheduler: ordering-independence is the actual contract, and
 * a test that depended on winning a scheduler race would be the flakiest possible
 * way to express it. If the draft is correct the instant the transcript is
 * applied, no later hydration read can lose it — regardless of effect order.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import { VoiceSessionProvider } from '../providers/VoiceSessionProvider'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { DRAFTS_KEY, loadDrafts, __resetForTests } from '../utils/chatDrafts'

// Capture the onText the provider hands to useVoiceInput so a test can fire a
// "transcript resolved" exactly as the real hook would, without a recorder.
const voice = vi.hoisted(() => ({
  onText: undefined as ((t: string, s: string | null) => void) | undefined,
}))

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
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
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    sttConfig: vi.fn(),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: (onText: (t: string, s: string | null) => void) => {
    voice.onText = onText
    return {
      recording: false, transcribing: false, sessionOwner: null, streamEnabled: false,
      error: null, level: 0, deviceLabel: '', partial: '', deviceSwitchIsLive: false,
      sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
      toggle: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(), clearError: vi.fn(), switchDevice: vi.fn(),
    }
  },
  voiceInputSupported: true,
}))
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
import { api } from '../api/client'

const SLOT = 'chat-main'

function makeStore() {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: 1, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot: SLOT, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
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

async function renderChat() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={makeStore()}>
          <ThemeProvider>
            <MemoryRouter><VoiceSessionProvider><ChatPage /></VoiceSessionProvider></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
  __resetForTests?.()
  voice.onText = undefined
  vi.mocked(api.sttConfig).mockResolvedValue({
    enabled: true, streaming: false, dictation_panel: true,
    provider: 'whisper', available: true,
  } as unknown as Awaited<ReturnType<typeof api.sttConfig>>)
})

describe('ChatPage — foreground transcript is persisted, not just set in state', () => {
  it('writes the transcript to the persisted draft synchronously (survives a later hydration read)', async () => {
    await renderChat()
    expect(voice.onText).toBeTypeOf('function')

    await act(async () => { voice.onText!('hello world', SLOT) })

    // The composer shows it...
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(ta.value).toContain('hello world')
    // ...AND the persisted draft already agrees. Before the fix this read the
    // pre-transcript value (''), which is exactly what the draft-restore effect
    // would have pushed back into the composer.
    expect(loadDrafts()[SLOT]).toContain('hello world')
    expect(localStorage.getItem(DRAFTS_KEY)).toContain('hello world')
  })

  it('a draft re-read after delivery returns the transcript, so a re-hydration cannot lose it', async () => {
    await renderChat()
    await waitFor(() => expect(voice.onText).toBeTypeOf('function'))

    await act(async () => { voice.onText!('dictated text', SLOT) })

    // Simulate exactly what the slot draft-restore effect does: read the store
    // and compute the value it would push into the composer. It must already be
    // the post-transcript text, so re-running hydration is a no-op rather than a
    // silent revert.
    const draftFallback = loadDrafts()[SLOT] ?? ''
    expect(draftFallback).toContain('dictated text')
  })
})
