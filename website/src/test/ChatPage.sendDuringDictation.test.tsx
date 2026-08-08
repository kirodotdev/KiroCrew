// Security regression tests for the widget postMessage trust-boundary
// bypass. A widget action must NEVER auto-submit a user-role turn: it may only
// pre-fill the composer, requiring an explicit human gesture (Enter) to send.
// When the user does send pre-filled text, the turn is tagged meta.origin=widget.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
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
// Controllable voice mock: `recording` is flipped per test and `toggle` is the
// spy that proves send() ended the dictation.
const voice = vi.hoisted(() => {
  const v = {
    recording: false,
    onPartial: null as ((t: string) => void) | null,
    onText: null as ((t: string) => void) | null,
    toggle: (() => {}) as () => void,
  }
  return v
})
voice.toggle = vi.fn(() => { voice.recording = !voice.recording })
vi.mock('../hooks/useVoiceInput', async () => {
  const React = await vi.importActual<typeof import('react')>('react')
  return {
    useVoiceInput: (onText: (t: string) => void, opts?: { onPartial?: (t: string) => void; streaming?: boolean }) => {
      voice.onPartial = opts?.onPartial ?? null
      voice.onText = onText
      const [, rerender] = React.useState(0)
      // Wrap toggle to also trigger a re-render of the host (VoiceSessionProvider)
      // so the new recording value propagates through context to ChatPage.
      const wrappedToggle = React.useCallback(() => {
        voice.toggle()
        rerender((c: number) => c + 1)
      }, [rerender])
      return ({
        recording: voice.recording,
        transcribing: false,
        sessionOwner: null,
        streamEnabled: !!opts?.streaming,
        toggle: wrappedToggle,
        cancel: vi.fn(),
        switchDevice: vi.fn(),
        deviceSwitchIsLive: false,
        prewarm: vi.fn(),
        error: null,
        level: 0,
        deviceLabel: '',
        clearError: vi.fn(),
        partial: '',
        sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
      })
    },
    voiceInputSupported: true,
  }
})
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

function makeStore(activeSlot: string, slots: { key: string; mode?: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        // connected: true seed required for tests that exercise ChatPage.send().
        // send() begins with `if (!connected) return` defense-in-depth (covers
        // all 5 call sites — keyboard, follow-up option, reconnect auto-send,
        // widget event, question card), so without this seed send() bails before
        // api.sendChat is invoked. dashboardSlice initial state defaults connected
        // to false (= fresh page load before WS handshake).
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
  vi.mocked(api.sendChat).mockClear()
})

describe('ChatPage — sending while dictating', () => {
  const setStt = (streaming: boolean) => vi.mocked(api.sttConfig).mockResolvedValue({
    enabled: true, streaming, dictation_panel: true,
    provider: streaming ? 'transcribe' : 'whisper', available: true,
  } as unknown as Awaited<ReturnType<typeof api.sttConfig>>)

  beforeEach(() => {
    setStt(true)
    voice.recording = false
    vi.mocked(voice.toggle).mockClear()
    vi.mocked(api.sendChat).mockClear()
  })

  it('drops a partial that lands after the send', async () => {
    // Reproduction of the real sequence. `frozenInputRef` holds the text that was
    // in the composer BEFORE dictation started, so that partials append to it
    // rather than replacing it. Send clears the composer — but if capture keeps
    // running, the next partial re-derives the value from that stale prefix and
    // the already-sent text reappears in the composer.
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // The mock must actually have handed us ChatPage's onPartial, or every
    // assertion below passes vacuously.
    expect(typeof voice.onPartial).toBe('function')

    // Arm via the REAL path: toggleVoice() is what clears sttDisarmedRef, and a
    // mount effect leaves it disarmed. Pre-setting `recording` would skip that
    // and every partial below would be dropped for the wrong reason.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    expect(voice.recording).toBe(true)

    // 1. user types a prefix, then dictates a word: frozen prefix = 'note: '
    await act(async () => { fireEvent.change(ta, { target: { value: 'note: ' } }) })
    await act(async () => { voice.onPartial?.('first') })
    expect(ta.value).toBe('note: first')

    // 2. Enter sends (the affordance the panel advertises)
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter', code: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    expect(ta.value).toBe('')

    // 3. a partial still in flight arrives. It must be DROPPED. Without the fix
    //    it rebuilds 'note: ' + text and the sent prefix reappears.
    await act(async () => { voice.onPartial?.('late') })
    expect(ta.value).toBe('')
  })

  it('does NOT disarm batch capture — the transcript arrives after stop', async () => {
    // The mirror-image bug of the test above. In batch mode (whisper) there are
    // no partials: MediaRecorder.onstop posts the blob and the whole transcript
    // comes back later through `onText`, which honours `sttDisarmedRef`.
    // Disarming on send would therefore throw the entire recording away — so the
    // send-time teardown is gated on streaming, and batch keeps its pre-existing
    // behaviour.
    setStt(false)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(typeof voice.onText).toBe('function')

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    expect(voice.recording).toBe(true)

    await act(async () => { fireEvent.change(ta, { target: { value: 'typed' } }) })
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter', code: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    expect(ta.value).toBe('')

    // Capture was NOT disarmed, so the transcript still lands.
    await act(async () => { voice.onText?.('dictated words') })
    expect(ta.value).toBe('dictated words')
  })

  it('does not touch voice capture when not recording', async () => {
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(ta, { target: { value: 'typed only' } }) })
    await act(async () => { fireEvent.keyDown(ta, { key: 'Enter', code: 'Enter' }) })
    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    expect(voice.toggle).not.toHaveBeenCalled()
  })

  it('inserts a batch transcript at the caret, not appended to the end', async () => {
    // Cursor-position dictation: with the caret in the MIDDLE of existing text,
    // the transcript splices in at the caret (with a joining space) rather than
    // appending to the end or overwriting. Guards the append→splice change.
    setStt(false)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(typeof voice.onText).toBe('function')

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    // Type a sentence, then place the caret right after "Hello" (offset 5).
    await act(async () => { fireEvent.change(ta, { target: { value: 'Hello world' } }) })
    await act(async () => { ta.setSelectionRange(5, 5); fireEvent.select(ta) })

    await act(async () => { voice.onText?.('there') })
    expect(ta.value).toBe('Hello there world')
  })

  it('adds a joining space when dictating right before existing text', async () => {
    // Guards the gluing bug: caret at the very start of "world" + dictate
    // "hello" must yield "hello world", not "helloworld".
    setStt(false)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    await act(async () => { fireEvent.change(ta, { target: { value: 'world' } }) })
    await act(async () => { ta.setSelectionRange(0, 0); fireEvent.select(ta) })
    await act(async () => { voice.onText?.('hello') })
    expect(ta.value).toBe('hello world')
  })

  it('leaves the draft untouched on an empty transcript (no selection deletion)', async () => {
    // An empty transcript with an active selection must NOT delete the selected
    // text (guards the empty-partial splice bug).
    setStt(false)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    await act(async () => { fireEvent.change(ta, { target: { value: 'keep me' } }) })
    await act(async () => { ta.setSelectionRange(0, 7); fireEvent.select(ta) })
    await act(async () => { voice.onText?.('') })
    expect(ta.value).toBe('keep me')
  })
})
