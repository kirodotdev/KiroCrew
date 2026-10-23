import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* The composer streams only when the selected provider can stream.
 *
 * `stt.streaming` is the user's preference and defaults to on. A provider an
 * edition registered without streaming support would otherwise be sent to the
 * live socket and refused, so the composer also reads the served
 * `streaming_providers` list. A gateway that serves no list keeps the old
 * behaviour. */

const fx = vi.hoisted(() => ({ opts: null as null | { streaming?: boolean } }))

vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: (_onText: unknown, opts: { streaming?: boolean }) => {
    fx.opts = opts
    return {
      recording: false, transcribing: false, transport: null, streamEnabled: !!opts.streaming,
      error: null, sessionOwner: null, toggle: () => {}, start: async () => {}, stop: () => {},
      cancel: () => {}, prewarm: () => {}, level: 0, deviceLabel: '', deviceId: '',
      clearError: () => {}, partial: '', download: null, sampleRef: { current: {} },
      switchDevice: () => {}, deviceSwitchIsLive: false, keptRecording: null, drainCancellable: false,
    }
  },
  voiceInputSupported: true,
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
vi.mock('../../hooks/voiceTranscriptInbox', () => ({ redeliverPending: () => {} }))
vi.mock('../../lib/haptic', () => ({ haptic: () => {} }))
vi.mock('../../api/client', () => ({ api: { sttConfig: vi.fn().mockResolvedValue({}) } }))

import { useComposerVoice } from './useComposerVoice'

function streamingFor(cfg: Record<string, unknown>): boolean | undefined {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, ...cfg })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  const inputRef = { current: '' }
  renderHook(() => useComposerVoice({ sessionId: 'slot-a', inputRef, setInput: () => {}, onAutoSubmit: () => {} }), { wrapper })
  return fx.opts?.streaming
}

describe('composer streaming follows the provider capability', () => {
  it('streams a provider the gateway says can stream', () => {
    expect(streamingFor({ provider: 'local', streaming: true, streaming_providers: ['local', 'transcribe'] })).toBe(true)
  })

  it('records a batch clip for a provider that cannot stream, even with streaming on', () => {
    expect(streamingFor({ provider: 'acme', streaming: true, streaming_providers: ['local', 'transcribe'] })).toBe(false)
  })

  it('keeps the saved preference when the gateway serves no capability list', () => {
    expect(streamingFor({ provider: 'local', streaming: true })).toBe(true)
  })

  it('never streams when the preference is off', () => {
    expect(streamingFor({ provider: 'local', streaming: false, streaming_providers: ['local'] })).toBe(false)
  })
})
