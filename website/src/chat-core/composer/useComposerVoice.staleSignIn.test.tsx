import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* A mic press that finds voice not ready must ask the gateway again before it
 * explains. The `['sttConfig']` query never goes stale on its own, so an answer
 * read at page load ("needs a sign-in") otherwise lasts until a reload, even
 * after the user signed in. */

const fx = vi.hoisted(() => ({
  start: vi.fn(async () => {}),
  sttConfig: vi.fn(),
}))

vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    recording: false, transcribing: false, sessionOwner: null, streamEnabled: false,
    toggle: vi.fn(), start: fx.start, stop: vi.fn(), cancel: vi.fn(), prewarm: vi.fn(),
    error: null, level: 0, deviceLabel: '', deviceId: '', clearError: vi.fn(), partial: '',
    download: null, sampleRef: { current: {} }, switchDevice: vi.fn(), deviceSwitchIsLive: false,
  }),
  voiceInputSupported: true,
}))
const ptt = vi.hoisted(() => ({ config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => ptt }))
vi.mock('../../api/client', () => ({ api: { sttConfig: fx.sttConfig } }))

import { useComposerVoice, _resetMicOwner } from './useComposerVoice'

const SIGNED_OUT = { enabled: true, available: false, code: 'sign_in_required', provider: 'hosted' }
const READY = { enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'hosted' }

function mount() {
  const inputRef = { current: '' }
  const setInput = (v: string) => { inputRef.current = v }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  // The page-load answer, cached for good.
  qc.setQueryData(['sttConfig'], SIGNED_OUT)
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return renderHook(() => useComposerVoice({ sessionId: 'slot-a', inputRef, setInput }), { wrapper })
}

beforeEach(() => {
  _resetMicOwner()
  fx.start.mockClear()
  fx.sttConfig.mockReset()
})

describe('useComposerVoice — a stale "not signed in" answer', () => {
  it('re-reads the config on a mic press and opens no setup modal once signed in', async () => {
    fx.sttConfig.mockResolvedValue(READY)
    const h = mount()
    expect(h.result.current.sttReady).toBe(false)
    await act(async () => { await h.result.current.startVoice() })
    await waitFor(() => expect(h.result.current.sttReady).toBe(true))
    expect(fx.sttConfig).toHaveBeenCalledTimes(1)
    expect(h.result.current.setup.open).toBe(false)
    // The next press records.
    await act(async () => { await h.result.current.startVoice() })
    expect(fx.start).toHaveBeenCalledTimes(1)
  })

  it('still opens the setup modal when the fresh answer is still not ready', async () => {
    fx.sttConfig.mockResolvedValue(SIGNED_OUT)
    const h = mount()
    await act(async () => { await h.result.current.startVoice() })
    await waitFor(() => expect(h.result.current.setup.open).toBe(true))
    expect(fx.start).not.toHaveBeenCalled()
  })

  it('a silent (push-to-talk key) start neither re-reads nor opens the modal', async () => {
    const h = mount()
    await act(async () => { await h.result.current.startVoice({ silent: true }) })
    expect(fx.sttConfig).not.toHaveBeenCalled()
    expect(h.result.current.setup.open).toBe(false)
  })
})
