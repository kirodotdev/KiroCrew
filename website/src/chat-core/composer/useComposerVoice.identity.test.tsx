import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* The Voice atom publishes `composerVoiceInputProps(cv)` into the composer's voice slot and skips the
 * publish when a shallow compare says nothing changed (Composer.tsx, VoiceAtomInner). That is only sound
 * if every member of the bundle is stable across a render that changed nothing.
 *
 * The real `useVoiceInput` returns a NEW object literal on every call while each member (`clearError`,
 * `toggle`, ...) is one stable function. The fake below reproduces exactly that; it must NOT mint new
 * `vi.fn()`s per call, or the bundle test would fail for the fake's sake instead of the code's. */

const fx = vi.hoisted(() => ({
  clearError: vi.fn(), toggle: vi.fn(), start: vi.fn(async () => {}), stop: vi.fn(),
  cancel: vi.fn(), prewarm: vi.fn(), switchDevice: vi.fn(), sampleRef: { current: {} },
}))

vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    recording: false, transcribing: false, sessionOwner: null, streamEnabled: false,
    toggle: fx.toggle, start: fx.start, stop: fx.stop, cancel: fx.cancel, prewarm: fx.prewarm,
    error: null, level: 0, deviceLabel: '', deviceId: '', clearError: fx.clearError, partial: '',
    download: null, sampleRef: fx.sampleRef, switchDevice: fx.switchDevice, deviceSwitchIsLive: false,
  }),
  voiceInputSupported: true,
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => undefined }))
vi.mock('../../api/client', () => ({
  api: { sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, provider: 'local' }) },
}))

import { useComposerVoice, composerVoiceInputProps, _resetMicOwner } from './useComposerVoice'

function mount() {
  // Stable across renders, as ChatPage's are: a new `setInput` per render would churn `onVoiceCancel`.
  const inputRef = { current: '' }
  const setInput = (v: string) => { inputRef.current = v }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['sttConfig'], { enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  return renderHook(() => useComposerVoice({ sessionId: 'slot-a', inputRef, setInput }), { wrapper })
}

beforeEach(() => {
  _resetMicOwner()
  fx.clearError.mockClear()
})

describe('useComposerVoice — identity of the callbacks it publishes', () => {
  it('clearVoiceError keeps its identity when useVoiceInput hands back a new object', () => {
    const { result, rerender } = mount()
    const first = result.current
    rerender(); rerender()
    // Precondition: the fake really does churn `voice`, so a pass below is not vacuous.
    expect(result.current.voice).not.toBe(first.voice)
    expect(result.current.clearVoiceError).toBe(first.clearVoiceError)
  })

  it('the published voice bundle is shallow-stable across a render that changed nothing', () => {
    const { result, rerender } = mount()
    const prev = composerVoiceInputProps(result.current)
    rerender()
    const next = composerVoiceInputProps(result.current)
    const churned = (Object.keys(next) as (keyof typeof next)[]).filter(k => !Object.is(prev[k], next[k]))
    expect(churned).toEqual([])
  })

  it('clearVoiceError still clears the engine error', () => {
    const { result } = mount()
    act(() => { result.current.clearVoiceError() })
    expect(fx.clearError).toHaveBeenCalledTimes(1)
  })
})
