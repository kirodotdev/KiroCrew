/**
 * Regression test for the voice-session hoist (fix/voice-session-above-router).
 *
 * BUG: while a dictation was transcribing, navigating away from /chat (to
 * Schedule / Artifacts / …) unmounted ChatPage, which tore down the voice hook
 * and orphaned the in-flight transcription — the finished words were lost.
 *
 * FIX: the ONE voice session now lives in VoiceSessionProvider, ABOVE the
 * router, so a route change no longer destroys it. ChatPage registers a live
 * "sink" while mounted; when NO sink is mounted (chat surface unmounted) a
 * finished BATCH transcript is appended to the originating slot's PERSISTED
 * draft, so the remounted ChatPage recovers it instead of losing it.
 *
 * These tests exercise the provider's dispatcher directly: the mocked
 * useVoiceInput captures the `onText` the provider passes it, so a test can
 * fire a "transcript resolved" exactly as the real hook would on completion —
 * with and without a mounted sink — and assert where the text lands.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, act } from '@testing-library/react'
import { useEffect, type ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { VoiceSessionProvider, useVoiceSession, type VoiceSink } from '../providers/VoiceSessionProvider'

// Grab the onText dispatcher the provider hands to useVoiceInput so a test can
// simulate a transcript resolving without driving a real recorder.
const captured = vi.hoisted(() => ({ onText: undefined as ((text: string, sessionId: string | null) => void) | undefined }))
// In-memory stand-in for the localStorage-backed draft store. Mocking it keeps
// the assertion deterministic (the real store debounces its localStorage flush)
// and scopes this test to the PROVIDER's fallback routing — the store's own
// persistence is covered by chatDrafts' tests.
const draftStore = vi.hoisted(() => ({} as Record<string, string>))

vi.mock('../hooks/useVoiceInput', () => ({
  useVoiceInput: (onText: (text: string, sessionId: string | null) => void) => {
    captured.onText = onText
    return {
      recording: false, transcribing: false, sessionOwner: null, streamEnabled: false,
      error: null, level: 0, deviceLabel: '', partial: '', deviceSwitchIsLive: false,
      sampleRef: { current: { level: 0, centroid: 0.5, onset: 0 } },
      toggle: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(), clearError: vi.fn(), switchDevice: vi.fn(),
    }
  },
  voiceInputSupported: true,
}))
vi.mock('../hooks/mic', () => ({ createAudioSample: () => ({ level: 0, centroid: 0.5, onset: 0 }) }))
vi.mock('../store', () => ({ useAppSelector: (sel: (s: unknown) => unknown) => sel({ chat: { activeSlot: 'slot-active' } }) }))
vi.mock('../api/client', () => ({ api: { sttConfig: vi.fn().mockResolvedValue({ streaming: false }) } }))
vi.mock('../utils/chatDrafts', () => ({
  loadDrafts: () => ({ ...draftStore }),
  setDraft: (d: Record<string, string>, id: string, text: string) => { d[id] = text },
  saveDrafts: (d: Record<string, string>) => {
    for (const k of Object.keys(draftStore)) delete draftStore[k]
    Object.assign(draftStore, d)
  },
}))

function renderProvider(children: ReactNode = <div />) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <VoiceSessionProvider>{children}</VoiceSessionProvider>
    </QueryClientProvider>,
  )
}

describe('VoiceSessionProvider — transcript survives nav-away (no live composer)', () => {
  beforeEach(() => {
    for (const k of Object.keys(draftStore)) delete draftStore[k]
    captured.onText = undefined
  })

  it('appends a finished batch transcript to the originating slot draft when no sink is mounted', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    act(() => captured.onText!('hello world', 'slot-7'))
    expect(draftStore['slot-7']).toBe('hello world')
  })

  it('appends (space-joined) to an existing draft rather than overwriting it', async () => {
    renderProvider()
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    act(() => captured.onText!('first', 'slot-7'))
    act(() => captured.onText!('second', 'slot-7'))
    expect(draftStore['slot-7']).toBe('first second')
  })

  it('routes to the live sink and does NOT touch the draft when a composer is mounted', async () => {
    const received: Array<[string, string | null]> = []
    function Sink() {
      const { registerVoiceSink } = useVoiceSession()
      useEffect(() => registerVoiceSink({ onText: (t, s) => { received.push([t, s]) } } as VoiceSink), [registerVoiceSink])
      return null
    }
    renderProvider(<Sink />)
    await waitFor(() => expect(captured.onText).toBeTypeOf('function'))
    act(() => captured.onText!('to composer', 'slot-7'))
    expect(received).toEqual([['to composer', 'slot-7']])
    expect(draftStore['slot-7']).toBeUndefined()
  })
})
