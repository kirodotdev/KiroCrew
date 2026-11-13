import { describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* The real useVoiceInput returns a fresh object on every render while its
 * `clearError` stays one useCallback. The mock below reproduces exactly that
 * shape, so a `clearVoiceError` keyed on the whole engine object changes
 * identity on every render and re-renders ChatInput on every stream tick. */

const fx = vi.hoisted(() => {
  const clearError = vi.fn()
  const engine = () => ({
    recording: false, transcribing: false, sessionOwner: null, streamEnabled: false,
    toggle: () => {}, start: async () => {}, stop: () => {}, cancel: () => {}, prewarm: () => {},
    error: null, keptRecording: null, level: 0, deviceLabel: '', deviceId: '', clearError, partial: '',
    download: null, sampleRef: { current: {} }, switchDevice: () => {}, deviceSwitchIsLive: false,
  })
  return { clearError, engine }
})
vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: () => fx.engine(),
  voiceInputSupported: true,
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => ({ config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }) }))
vi.mock('../../api/client', () => ({
  api: { sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }) },
}))

import { useComposerVoice } from './useComposerVoice'

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  const inputRef = { current: '' }
  return renderHook(() => useComposerVoice({ sessionId: 'slot-a', inputRef, setInput: (v: string) => { inputRef.current = v } }), { wrapper })
}

describe('useComposerVoice clearVoiceError identity', () => {
  it('keeps one clearVoiceError across renders when the engine object is rebuilt', () => {
    const { result, rerender } = mount()
    const first = result.current.clearVoiceError
    rerender()
    rerender()
    expect(result.current.clearVoiceError).toBe(first)
  })

  it('still clears the engine error through the stable callback', () => {
    const { result, rerender } = mount()
    rerender()
    fx.clearError.mockClear()
    act(() => { result.current.clearVoiceError() })
    expect(fx.clearError).toHaveBeenCalledTimes(1)
    expect(result.current.polishError).toBeNull()
  })
})
