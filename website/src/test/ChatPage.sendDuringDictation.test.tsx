// Security regression tests for the widget postMessage trust-boundary
// bypass. A widget action must NEVER auto-submit a user-role turn: it may only
// pre-fill the composer, requiring an explicit human gesture (Enter) to send.
// When the user does send pre-filled text, the turn is tagged meta.origin=widget.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { TranscriptOrigin } from '../hooks/useVoiceInput'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot } from '../store/chatSlice'
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
// spy that proves send() ended the dictation. `start`/`stop` exist because
// ChatPage's startVoice/stopVoice call them directly (the push-to-talk driver
// needs explicit start and stop, not just a toggle).
const voice = vi.hoisted(() => {
  const v = {
    recording: false,
    // Live partial, mirroring useVoiceInput's own `partial`. cancelVoice reads it
    // to reconstruct (and roll back) the dictated region, so a test exercising
    // the discard path has to set it alongside firing onPartial.
    partial: '',
    onPartial: null as ((t: string, sessionId?: string | null) => void) | null,
    onEndpoint: null as (() => void) | null,
    onText: null as ((t: string, sessionId: string | null, origin: TranscriptOrigin) => void) | null,
    /** Mode the page configured, so a delivered transcript can carry the origin
     *  the real hook would attach to it. */
    streaming: false,
    toggle: (() => {}) as () => void,
    start: (() => {}) as () => void,
    stop: (() => {}) as () => void,
    cancel: (() => {}) as () => void,
  }
  return v
})
voice.toggle = vi.fn(() => { voice.recording = !voice.recording })
voice.start = vi.fn(() => { voice.recording = true })
voice.stop = vi.fn(() => { voice.recording = false })
voice.cancel = vi.fn(() => { voice.recording = false })
vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: (onText: (t: string, sessionId: string | null, origin: TranscriptOrigin) => void, opts?: { onPartial?: (t: string, sessionId?: string | null) => void; onEndpoint?: () => void; streaming?: boolean }) => {
    voice.onPartial = opts?.onPartial ?? null
    voice.onEndpoint = opts?.onEndpoint ?? null
    voice.onText = onText
    voice.streaming = !!opts?.streaming
    return ({
    recording: voice.recording,
    transcribing: false,
    sessionOwner: null,
    streamEnabled: !!opts?.streaming,
    toggle: voice.toggle,
    start: voice.start,
    stop: voice.stop,
    cancel: voice.cancel,
    prewarm: vi.fn(),
    error: null,
    level: 0,
    deviceLabel: '',
    clearError: vi.fn(),
    partial: voice.partial,
    sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
    })
  },
  voiceInputSupported: true,
}))

/** Hand ChatPage a transcript exactly as `useVoiceInput` would: the origin is the
 *  capture path that produced it, which is the mode the page is configured for. */
const deliverText = (
  text: string,
  sessionId: string | null = null,
  origin: TranscriptOrigin = voice.streaming ? 'stream' : 'batch',
) => voice.onText?.(text, sessionId, origin)
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
import { savePttConfig } from '../lib/pushToTalk'

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
            <MemoryRouter><ChatPage /></MemoryRouter>
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
    await act(async () => { deliverText('dictated words') })
    expect(ta.value).toBe('dictated words')
  })

  it('routes a late batch final once to the slot that started dictation', async () => {
    setStt(false)
    const store = makeStore('chat-a', [{ key: 'chat-a' }, { key: 'chat-b' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Start the real page path in A, then leave while its batch transcription
    // is still pending. A slot switch stops the shared mic, but Whisper's one
    // final arrives later and remains attributed to the capture owner.
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /voice input/i })) })
    expect(voice.recording).toBe(true)
    await act(async () => { fireEvent.change(ta, { target: { value: 'draft for A' } }) })

    await act(async () => { store.dispatch(setActiveSlot('chat-b')) })
    await waitFor(() => expect(ta.value).toBe(''))
    await act(async () => { fireEvent.change(ta, { target: { value: 'draft for B' } }) })

    await act(async () => { deliverText('batch final', 'chat-a', 'batch') })

    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(drafts['chat-a']).toBe('draft for A batch final')
      expect(drafts['chat-b']).toBe('draft for B')
    })
    expect(ta.value).toBe('draft for B')

    // Contrast the batch route with stale streaming callbacks from A. A late
    // hypothesis is neither allowed into B's live composer nor appended again
    // to A, whose streaming partial was already persisted on the switch.
    await act(async () => { voice.onPartial?.('stale partial', 'chat-a') })
    await act(async () => { deliverText('stale final', 'chat-a', 'stream') })
    expect(ta.value).toBe('draft for B')

    await act(async () => { store.dispatch(setActiveSlot('chat-a')) })
    await waitFor(() => expect(ta.value).toBe('draft for A batch final'))
    expect(ta.value.match(/batch final/g)).toHaveLength(1)
    expect(ta.value).not.toContain('stale')
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

    await act(async () => { deliverText('there') })
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
    await act(async () => { deliverText('hello') })
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
    await act(async () => { deliverText('') })
    expect(ta.value).toBe('keep me')
  })

  it('keeps the utterance when a streaming stop beats the first partial', async () => {
    // A COLD stream: useStreamingStt connects its worklet and buffers PCM before
    // the server's `ready` frame, so a short press can end capture before ANY
    // partial has landed. The composer therefore holds no copy of the speech and
    // the draining final is the only one — disarming it deletes the utterance
    // outright, which is the ordinary outcome of the first push-to-talk press of
    // a session (the one where the handshake is slowest).
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(typeof voice.onText).toBe('function')

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    expect(voice.recording).toBe(true)
    // Type a prefix. This is also what re-renders ChatPage so the mic button
    // re-reads `recording` from the mock — mutating that property does not
    // notify React, so without a render in between the second click would read
    // a stale `false` and START again instead of stopping. Do not "simplify"
    // this away.
    await act(async () => { fireEvent.change(ta, { target: { value: 'note: ' } }) })
    // Deliberately NO onPartial: the handshake never produced one.
    await act(async () => { fireEvent.click(mic) })
    expect(voice.recording).toBe(false)

    // The final drains in after the stop and must still reach the composer.
    await act(async () => { deliverText('hello there') })
    expect(ta.value).toBe('note: hello there')
  })

  it('rolls the composer back when a push-to-talk press is discarded', async () => {
    // Capture opens on the keydown now, so a partial can reach the composer
    // BEFORE the press is revealed as a chord or a sub-threshold tap in
    // hold-only mode. The discard therefore has to run the streaming rollback
    // (`cancelVoice`) — the hook's raw `cancel` would drop the capture and leave
    // that text stranded in the composer with nothing left to clear it.
    setStt(true)
    savePttConfig({ mode: 'ptt', binding: { code: 'AltRight' }, holdMs: 500 })
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Press: the driver opens capture immediately.
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { code: 'AltRight', altKey: true, bubbles: true, cancelable: true }))
    })
    expect(voice.recording).toBe(true)

    // A fast partial lands while the press is still arming.
    voice.partial = 'hello'
    await act(async () => { voice.onPartial?.('hello') })
    expect(ta.value).toBe('hello')

    // Released under the threshold: in hold-only mode a tap means nothing, so the
    // press is discarded — and the composer must not keep the dictated text.
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keyup', { code: 'AltRight', bubbles: true }))
    })
    expect(voice.cancel).toHaveBeenCalled()
    expect(ta.value).toBe('')
    voice.partial = ''
  })

  it('rolls back a discarded press when the dictation spliced mid-draft', async () => {
    // The rollback has to reconstruct the region the SAME way onPartial wrote
    // it. onPartial splices at the snapshotted caret, so with the caret in the
    // middle of the draft the composer reads `before + partial + after` — an
    // append-only reconstruction (`frozen + separator + partial`) fails its
    // startsWith check, falls through to the leave-unchanged branch, and strands
    // the dictated word inside the user's sentence. A chord like ⌥e typed
    // mid-sentence hits exactly this path, so the stranded text is something the
    // user never asked to dictate.
    setStt(true)
    savePttConfig({ mode: 'ptt', binding: { code: 'AltRight' }, holdMs: 500 })
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Draft with the caret parked in the middle, after "Hello".
    await act(async () => { fireEvent.change(ta, { target: { value: 'Hello world' } }) })
    await act(async () => { ta.setSelectionRange(5, 5); fireEvent.select(ta) })

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { code: 'AltRight', altKey: true, bubbles: true, cancelable: true }))
    })
    expect(voice.recording).toBe(true)

    // A fast partial splices in at the caret before the press resolves.
    voice.partial = 'there'
    await act(async () => { voice.onPartial?.('there') })
    expect(ta.value).toBe('Hello there world')

    // Sub-threshold release in hold-only mode: discarded, and the draft must be
    // exactly what the user had typed.
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keyup', { code: 'AltRight', bubbles: true }))
    })
    expect(voice.cancel).toHaveBeenCalled()
    expect(ta.value).toBe('Hello world')
    voice.partial = ''
  })

  it('still drops the draining final once partials have populated the composer', async () => {
    // The guard being narrowed is real, so pin it: with the speech already in
    // the composer the draining final is redundant, and letting it land rebuilds
    // the value from the stale pre-dictation snapshot — clobbering whatever the
    // user typed while the socket drained.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('hello') })
    expect(ta.value).toBe('hello')
    await act(async () => { fireEvent.click(mic) })

    // The user edits while the socket drains, then the final arrives.
    await act(async () => { fireEvent.change(ta, { target: { value: 'edited by hand' } }) })
    await act(async () => { deliverText('hello there') })
    expect(ta.value).toBe('edited by hand')
  })

  it('lets a drain-time correction replace the hypothesis after a manual stop', async () => {
    // The point of the two flags. `stop()` leaves the socket draining on purpose,
    // and Transcribe keeps stabilising: the hook re-emits `finals.join(' ')`
    // through onPartial as each segment settles. That route REPLACES the region
    // at the frozen boundary, so it cannot duplicate anything — and it carries
    // the words the release beat. Suppressing it left the user holding the last
    // UNSTABLE hypothesis, which on a short push-to-talk hold is the common case.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('remind me to') })
    expect(ta.value).toBe('remind me to')

    await act(async () => { fireEvent.click(mic) })
    expect(voice.recording).toBe(false)

    // Drain: Transcribe finalises the segment, adding the tail the release cut off.
    await act(async () => { voice.onPartial?.('remind me to call Ana') })
    expect(ta.value).toBe('remind me to call Ana')

    // The close-time route still APPENDS, so it must stay suppressed — otherwise
    // the composer would read "remind me to call Ana remind me to call Ana".
    await act(async () => { deliverText('remind me to call Ana') })
    expect(ta.value).toBe('remind me to call Ana')
  })

  it('keeps text typed after the release when a drain correction lands', async () => {
    // Release the key and the user reasonably starts typing straight away —
    // dictation is over as far as they can tell. A drain-time replace must take
    // the correction WITHOUT deleting what they typed, so the region is verified
    // and the suffix carried across rather than rebuilt from the snapshot.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('remind me to') })
    expect(ta.value).toBe('remind me to')
    await act(async () => { fireEvent.click(mic) })

    // Typing immediately after the release.
    await act(async () => { fireEvent.change(ta, { target: { value: 'remind me to — urgent' } }) })
    // The drain then finalises the segment.
    await act(async () => { voice.onPartial?.('remind me to call Ana') })

    expect(ta.value).toBe('remind me to call Ana — urgent')
  })

  it('leaves the composer alone when the dictated region was edited', async () => {
    // If the region cannot be verified the user rewrote it, and a suffix-match
    // heuristic there would delete text they authored. Same policy cancelVoice
    // takes: change nothing rather than guess.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('remind me to') })
    await act(async () => { fireEvent.click(mic) })

    // The user replaces the dictation entirely.
    await act(async () => { fireEvent.change(ta, { target: { value: 'never mind' } }) })
    await act(async () => { voice.onPartial?.('remind me to call Ana') })

    expect(ta.value).toBe('never mind')
  })

  it('keeps a mid-draft correction and everything after it', async () => {
    // Dictation splices at the caret, so it can land in the MIDDLE of a draft
    // with an existing tail after it — and typing after the release goes to the
    // restored caret, i.e. between the dictated words and that tail. The drain
    // correction must replace only the dictated region: both the typed text and
    // the original tail have to come through untouched.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Draft with the caret parked before "world".
    await act(async () => {
      fireEvent.change(ta, { target: { value: 'hello world', selectionStart: 6, selectionEnd: 6 } })
    })

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('remind me') })
    expect(ta.value).toBe('hello remind me world')
    await act(async () => { fireEvent.click(mic) })

    // Typing lands where the caret was restored — right after the dictation.
    await act(async () => {
      fireEvent.change(ta, { target: { value: 'hello remind me NOW world' } })
    })
    await act(async () => { voice.onPartial?.('remind me to call Ana') })

    expect(ta.value).toBe('hello remind me to call Ana NOW world')
  })

  it('carries the user caret across a drain correction', async () => {
    // Not arming the caret is not the same as leaving it alone: React replaces the
    // textarea value and the browser resets the DOM caret to the END. Mid-draft
    // that end is past the tail, i.e. nowhere near where the user was. Their
    // logical position must be re-armed, shifted by how much the region ahead of
    // it grew.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    // Draft with the caret before 'later', so dictation lands mid-draft.
    await act(async () => {
      fireEvent.change(ta, { target: { value: 'call later', selectionStart: 5, selectionEnd: 5 } })
    })
    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('remind') })
    expect(ta.value).toBe('call remind later')
    await act(async () => { fireEvent.click(mic) })

    // Type ' NOW' right after the dictation; their caret sits at offset 15.
    await act(async () => {
      fireEvent.change(ta, { target: { value: 'call remind NOW later', selectionStart: 15, selectionEnd: 15 } })
    })
    // The correction grows the region from 'call remind' (11) to
    // 'call remind me to call Ana' (26) — a shift of 15.
    await act(async () => { voice.onPartial?.('remind me to call Ana') })
    expect(ta.value).toBe('call remind me to call Ana NOW later')

    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    // 15 + 15 = 30, right after ' NOW'. A caret left unarmed would have been
    // reset to the end of the value (36), past the ' later' tail.
    expect(ta.selectionStart).toBe('call remind me to call Ana NOW'.length)
  })

  it('does not reclaim the caret on a later correction in the same drain', async () => {
    // A drain emits several corrections as segments stabilise. The first one sees
    // the typed suffix and leaves the caret alone — but it also rewrites the
    // composer to include that suffix, so a per-update "was it edited?" test
    // would say no on the SECOND correction and pull the caret back to the end of
    // the dictation, in front of the user's text. The edited state has to stay
    // sticky for the whole drain.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('remind me') })
    await act(async () => { fireEvent.click(mic) })

    await act(async () => { fireEvent.change(ta, { target: { value: 'remind me NOW' } }) })
    // First drain correction: carries the suffix, must not steer the caret.
    await act(async () => { voice.onPartial?.('remind me to call') })
    expect(ta.value).toBe('remind me to call NOW')
    // Second correction, user has not touched anything since.
    await act(async () => { voice.onPartial?.('remind me to call Ana') })
    expect(ta.value).toBe('remind me to call Ana NOW')

    // Let the caret-restore frame run. Nothing should have armed it, so the
    // caret is wherever the value commit left it — NOT at the end of the
    // dictated region ('remind me to call Ana' = offset 21), which sits in
    // front of the typed ' NOW'.
    await act(async () => { await new Promise(r => requestAnimationFrame(() => r(null))) })
    expect(ta.selectionStart).not.toBe('remind me to call Ana'.length)
  })

  it('inserts a cold-stream transcript where the user was speaking', async () => {
    // Release, then type BEFORE the drain's first partial arrives. Nothing has
    // pinned the insertion point yet, so without a caret frozen at the release
    // the partial would snapshot the edited composer and land the transcript
    // AFTER the newly typed text — reordering the utterance.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    // A real value change (not a repeat of the same string) is what fires
    // onChange -> records the caret AND re-renders the button so the next click
    // reads the flipped `recording`. Caret parked before 'later'.
    await act(async () => {
      fireEvent.change(ta, { target: { value: 'call later', selectionStart: 5, selectionEnd: 5 } })
    })
    await act(async () => { fireEvent.click(mic) })

    // Typing lands at the end (a fresh change resets the caret there), so the
    // release-time caret and the live caret genuinely disagree.
    await act(async () => { fireEvent.change(ta, { target: { value: 'call later today' } }) })
    await act(async () => { voice.onPartial?.('Ana') })

    // Dictation goes where they were speaking (offset 5), not after 'today'.
    expect(ta.value).toBe('call Ana later today')
  })

  it('rebases a frozen caret when the user edits before it', async () => {
    // A frozen OFFSET only means what it meant at the release if the text before
    // it is unchanged. Prepend after releasing and offset 5 no longer points at
    // the same spot — splicing there cuts into the word they just wrote.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    // Caret before 'later'; no partial fires, so this is the cold-stream path.
    await act(async () => {
      fireEvent.change(ta, { target: { value: 'call later', selectionStart: 5, selectionEnd: 5 } })
    })
    await act(async () => { fireEvent.click(mic) })

    // Prepend 'Hi, ' — everything after it shifts right by 4.
    await act(async () => { fireEvent.change(ta, { target: { value: 'Hi, call later' } }) })
    await act(async () => { voice.onPartial?.('remind') })

    // Offset rebased 5 -> 9, so the transcript lands before 'later' as intended.
    // Trusting the stale 5 would have produced 'Hi, c remind all later'.
    expect(ta.value).toBe('Hi, call remind later')
  })

  it('does not delete a typed replacement for a selection frozen at release', async () => {
    // Dictating over a selection replaces it, so the frozen caret is a RANGE.
    // If the user then types over that selection after releasing, the range is
    // stale: splicing with it would delete exactly the replacement they wrote.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    // 'Bob' selected (offsets 5..8) at the moment of release.
    await act(async () => {
      fireEvent.change(ta, { target: { value: 'call Bob later', selectionStart: 5, selectionEnd: 8 } })
    })
    await act(async () => { fireEvent.click(mic) })

    // The user types over the selection, replacing 'Bob' with 'Ana'.
    await act(async () => { fireEvent.change(ta, { target: { value: 'call Ana later' } }) })
    await act(async () => { voice.onPartial?.('remind') })

    // 'Ana' must survive: the transcript is inserted at the selection start
    // rather than overwriting the range that no longer exists.
    expect(ta.value).toContain('Ana')
    expect(ta.value).toBe('call remind Ana later')
  })

  it('keeps post-release typing through a cold-stream drain', async () => {
    // Cold-stream stop: no partial landed, so the append route stays armed on
    // purpose. The drain still delivers the utterance through onPartial, and the
    // suffix-preservation branch must engage there too — gating it on the append
    // flag would skip it in exactly this case and delete the typed text.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    // A real value change is what re-renders the button so the second click
    // reads the flipped `recording` — an unchanged value does not, and the click
    // would re-run start (resetting every flag) instead of stop. No onPartial
    // fires, so frozenInputRef is still null at stop: the cold-stream path.
    await act(async () => { fireEvent.change(ta, { target: { value: 'draft' } }) })
    await act(async () => { fireEvent.click(mic) })

    // The drain's first partial lands after the release and seeds the anchor.
    await act(async () => { voice.onPartial?.('remind me') })
    expect(ta.value).toBe('draft remind me')
    // The user types, then a later correction arrives.
    await act(async () => { fireEvent.change(ta, { target: { value: 'draft remind me NOW' } }) })
    await act(async () => { voice.onPartial?.('remind me to call Ana') })

    expect(ta.value).toBe('draft remind me to call Ana NOW')

    // The socket then closes and delivers its final. On the streaming path
    // applyVoiceText re-splices from frozenInputRef and OVERWRITES — it does not
    // append — so letting it through here would delete the typed ' NOW'. The
    // drain already put the stabilised text in the composer, so the final has
    // nothing to add and must be suppressed.
    await act(async () => { deliverText('remind me to call Ana') })
    expect(ta.value).toBe('draft remind me to call Ana NOW')
  })

  it('does not auto-send on an endpoint verdict after a cold-stream stop', async () => {
    // Release before the server's first partial: no partial landed, so the append
    // route stays OPEN on purpose (the close-time final is the only copy of the
    // utterance). That must not leave the endpointer armed — "stop capturing" is
    // not "send". With an existing draft in the composer there IS something to
    // submit, so a trailing final's verdict would send the user's draft for them.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    // Typing while holding — this is also what re-renders the button so the
    // second click reads the mock's flipped `recording` (see the other tests).
    // No onPartial fires, so frozenInputRef stays null: the cold-stream case.
    await act(async () => { fireEvent.change(ta, { target: { value: 'draft in progress' } }) })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onEndpoint?.() })

    expect(vi.mocked(api.sendChat)).not.toHaveBeenCalled()
    expect(ta.value).toBe('draft in progress')
  })

  it('does not auto-send on a drain-time endpoint verdict after a manual stop', async () => {
    // A manual stop is the user saying "stop capturing", not "send". The backend
    // can still emit its semantic-endpoint verdict while the socket drains, and
    // splitting the flag must not quietly re-arm that auto-submit.
    setStt(true)
    const store = makeStore('chat-main', [{ key: 'chat-main' }])
    await renderAndWaitForInput(store)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(typeof voice.onEndpoint).toBe('function')

    const mic = screen.getByRole('button', { name: /voice input/i })
    await act(async () => { fireEvent.click(mic) })
    await act(async () => { voice.onPartial?.('send this later') })
    expect(ta.value).toBe('send this later')
    await act(async () => { fireEvent.click(mic) })

    await act(async () => { voice.onEndpoint?.() })
    expect(vi.mocked(api.sendChat)).not.toHaveBeenCalled()
  })
})
