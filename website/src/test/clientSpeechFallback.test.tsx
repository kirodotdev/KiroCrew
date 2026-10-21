/**
 * Client-speech fallback for Read aloud on a headless remote gateway (#22710):
 * the window.speechSynthesis helper, the voiceSynthesize start/fail contract,
 * and the playback owner's decision — speak on the client vs show the failure
 * notice — driven through the real useWebSocket frame router.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createVoiceEndpoints } from '../api/client/voice'
import { ApiError } from '../api/apiError'
import { clientSpeechAvailable, speakOnClient, stopClientSpeech } from '../lib/clientSpeech'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'
import { store as globalStore } from '../store'
import chatReducer, { setActiveSlot, clearMessages } from '../store/chatSlice'
import { recentErrors } from '../utils/errorReport'
import { ACTIVE, WS_INSTANCES, MockWebSocket } from './UseWebSocketCoverageTestUtils'

// ── Web Speech fake ───────────────────────────────────────────────────

class FakeUtterance {
  onend: (() => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  constructor(public text: string) {}
}

function installSpeechSynthesis() {
  const spoken: FakeUtterance[] = []
  let cancelCount = 0
  ;(window as unknown as { speechSynthesis: unknown }).speechSynthesis = {
    speak: (u: FakeUtterance) => { spoken.push(u) },
    cancel: () => { cancelCount++ },
  }
  ;(window as unknown as { SpeechSynthesisUtterance: unknown }).SpeechSynthesisUtterance = FakeUtterance
  return { spoken, getCancelCount: () => cancelCount }
}

function removeSpeechSynthesis() {
  delete (window as unknown as { speechSynthesis?: unknown }).speechSynthesis
  delete (window as unknown as { SpeechSynthesisUtterance?: unknown }).SpeechSynthesisUtterance
}

// ── clientSpeech helper ───────────────────────────────────────────────

describe('clientSpeech helper', () => {
  afterEach(() => { removeSpeechSynthesis() })

  it('reports unavailable when the webview has no speech API', () => {
    removeSpeechSynthesis()
    expect(clientSpeechAvailable()).toBe(false)
    const onPlaying = vi.fn()
    expect(speakOnClient('hello', { onPlaying, onError: vi.fn() })).toBe(false)
    expect(onPlaying).not.toHaveBeenCalled()
  })

  it('speaks on the client and brackets playback with onPlaying', () => {
    const { spoken } = installSpeechSynthesis()
    expect(clientSpeechAvailable()).toBe(true)
    const onPlaying = vi.fn()
    const onError = vi.fn()
    expect(speakOnClient('read this aloud', { onPlaying, onError })).toBe(true)
    expect(spoken).toHaveLength(1)
    expect(spoken[0].text).toBe('read this aloud')
    expect(onPlaying).toHaveBeenLastCalledWith(true)
    spoken[0].onend?.()
    expect(onPlaying).toHaveBeenLastCalledWith(false)
    expect(onError).not.toHaveBeenCalled()
  })

  it('does not report a user cancel as an error', () => {
    const { spoken } = installSpeechSynthesis()
    const onError = vi.fn()
    speakOnClient('x', { onPlaying: vi.fn(), onError })
    spoken[0].onerror?.({ error: 'canceled' })
    expect(onError).not.toHaveBeenCalled()
  })

  it('reports a genuine synthesis error', () => {
    const { spoken } = installSpeechSynthesis()
    const onError = vi.fn()
    speakOnClient('x', { onPlaying: vi.fn(), onError })
    spoken[0].onerror?.({ error: 'synthesis-failed' })
    expect(onError).toHaveBeenCalledWith('voice_playback_failed')
  })

  it('cancels the engine on stop', () => {
    const { getCancelCount } = installSpeechSynthesis()
    speakOnClient('x', { onPlaying: vi.fn(), onError: vi.fn() })
    stopClientSpeech()
    expect(getCancelCount()).toBeGreaterThan(0)
  })

  it('refuses to speak empty text', () => {
    installSpeechSynthesis()
    expect(speakOnClient('   ', { onPlaying: vi.fn(), onError: vi.fn() })).toBe(false)
  })
})

// ── voiceSynthesize start/fail contract ───────────────────────────────

function makeEndpoints(rejectCode: string | null) {
  const post = vi.fn().mockResolvedValue({} as Response)
  const j = vi.fn().mockImplementation(() =>
    rejectCode === null
      ? Promise.resolve({ ok: true })
      : Promise.reject(new ApiError(502, 'no audio', JSON.stringify({ code: rejectCode }))))
  const transport = { post, put: vi.fn(), j, jfetch: vi.fn() } as unknown as Parameters<typeof createVoiceEndpoints>[0]
  return createVoiceEndpoints(transport).synthesis
}

function captureEvents(types: string[]) {
  const seen: { type: string; detail: unknown }[] = []
  const handlers = types.map(type => {
    const h = (e: Event) => seen.push({ type, detail: (e as CustomEvent).detail })
    window.addEventListener(type, h)
    return { type, h }
  })
  return { seen, dispose: () => handlers.forEach(({ type, h }) => window.removeEventListener(type, h)) }
}

describe('voiceSynthesize start/fail contract', () => {
  it('carries clientFallback and text on the start event so the owner can decide', () => {
    const synthesis = makeEndpoints(null)
    const cap = captureEvents(['voice-synthesis-start'])
    try {
      void synthesis.voiceSynthesize('chat-a', 'speak me', { clientFallback: true })
    } finally {
      cap.dispose()
    }
    const start = cap.seen.find(e => e.type === 'voice-synthesis-start')
    expect((start!.detail as { clientFallback: boolean; text: string }).clientFallback).toBe(true)
    expect((start!.detail as { text: string }).text).toBe('speak me')
  })

  it('emits voice-synthesis-failed with the gateway code on failure (owner decides the fallback)', async () => {
    const synthesis = makeEndpoints('voice_unavailable')
    const cap = captureEvents(['voice-synthesis-failed', 'voice-client-speak'])
    try {
      await expect(synthesis.voiceSynthesize('chat-a', 'x', { clientFallback: true })).rejects.toBeInstanceOf(ApiError)
    } finally {
      cap.dispose()
    }
    // The HTTP catch no longer routes to client speech itself — the playback
    // owner does, off the start event — so no bespoke event is emitted here.
    expect(cap.seen.some(e => e.type === 'voice-client-speak')).toBe(false)
    const failed = cap.seen.find(e => e.type === 'voice-synthesis-failed')
    expect((failed!.detail as { code: string }).code).toBe('voice_unavailable')
  })
})

// ── playback owner fallback decision (real frame router) ───────────────

describe('playback owner client fallback (#22710)', () => {
  let testStore: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    testStore = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@INIT' }), activeSlot: ACTIVE } })
    vi.stubGlobal('WebSocket', MockWebSocket)
    installSpeechSynthesis()
    globalStore.dispatch(setActiveSlot(ACTIVE))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    removeSpeechSynthesis()
    globalStore.dispatch(clearMessages())
    globalStore.dispatch(setActiveSlot(null))
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return createElement(Provider, { store: testStore },
      createElement(QueryClientProvider, { client: qc }, children))
  }

  function mount(detail: Record<string, unknown>) {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => {
      ws.simulateOpen()
      window.dispatchEvent(new CustomEvent('voice-synthesis-start', { detail }))
    })
    return ws
  }

  const speechSpoken = () => (window as unknown as { speechSynthesis: { speak: unknown } }).speechSynthesis

  it('speaks on the client and shows NO failure notice when a fallback request hits voice_unavailable', () => {
    const { spoken } = installSpeechSynthesis()
    const ws = mount({ slot: ACTIVE, request_id: 'rq-fb', clientFallback: true, text: 'hello world' })
    const before = recentErrors().map(r => r.id)
    act(() => { ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'rq-fb', code: 'voice_unavailable' } }) })
    expect(spoken.map(u => u.text)).toContain('hello world')
    // The WS voice_error frame did NOT raise a failure notice — the fallback owned it.
    expect(recentErrors().filter(r => !before.includes(r.id))).toHaveLength(0)
  })

  it('shows the failure notice (no client speech) for a request that did NOT allow fallback', () => {
    const before = recentErrors().map(r => r.id)
    const ws = mount({ slot: ACTIVE, request_id: 'rq-nofb' })
    act(() => { ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'rq-nofb', code: 'voice_unavailable' } }) })
    expect(speechSpoken()).toBeDefined()
    expect(recentErrors().filter(r => !before.includes(r.id)).length).toBeGreaterThan(0)
  })

  it('does not start client speech after Stop (late voice_error is ignored)', () => {
    const { spoken } = installSpeechSynthesis()
    const ws = mount({ slot: ACTIVE, request_id: 'rq-stop', clientFallback: true, text: 'cancelled reply' })
    act(() => { window.dispatchEvent(new Event('voice-stop')) })
    act(() => { ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'rq-stop', code: 'voice_unavailable' } }) })
    expect(spoken.map(u => u.text)).not.toContain('cancelled reply')
  })

  it('speaks the gateway-cleaned text when the error carries speakable', () => {
    const { spoken } = installSpeechSynthesis()
    const ws = mount({ slot: ACTIVE, request_id: 'rq-clean', clientFallback: true, text: 'Here is code:\n```js\nconst x=1\n```' })
    act(() => { ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'rq-clean', code: 'voice_unavailable', speakable: 'Here is code: (code block)' } }) })
    // The cleaned text is spoken, not the raw fenced markdown.
    expect(spoken.map(u => u.text)).toContain('Here is code: (code block)')
    expect(spoken.map(u => u.text)).not.toContain('```js')
  })

  it('keeps client speech alive and raises no notice when the socket drops', () => {
    const { spoken, getCancelCount } = installSpeechSynthesis()
    const ws = mount({ slot: ACTIVE, request_id: 'rq-sock', clientFallback: true, text: 'still speaking' })
    act(() => { ws.simulateMessage({ type: 'voice_error', data: { slot: ACTIVE, request_id: 'rq-sock', code: 'voice_unavailable' } }) })
    expect(spoken.map(u => u.text)).toContain('still speaking')
    const cancelsBefore = getCancelCount()
    const before = recentErrors().map(r => r.id)
    // Socket closes while client speech (which does not need the socket) plays.
    act(() => { ws.onclose?.(new CloseEvent('close')) })
    // Speech was not cancelled and no "Read aloud failed" notice was raised.
    expect(getCancelCount()).toBe(cancelsBefore)
    expect(recentErrors().filter(r => !before.includes(r.id))).toHaveLength(0)
  })
})
