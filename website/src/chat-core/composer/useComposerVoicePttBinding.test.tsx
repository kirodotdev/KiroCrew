import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/**
 * Which push-to-talk key a composer's mic may name (`pttBinding`).
 *
 * The mic names the key only where pressing it would record into this
 * composer: the composer must own the key, the key trigger must be on, and STT
 * must be ready, since a keyboard start is silent and does nothing otherwise.
 * A recording this composer owns keeps the name, because the key still stops it.
 * The config comes from `usePushToTalk`, the one live reader of the binding.
 */

const fx = vi.hoisted(() => ({
  engine: {
    recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
    toggle: () => {}, start: async () => {}, stop: () => {}, cancel: () => {}, prewarm: () => {},
    error: null, level: 0, deviceLabel: '', deviceId: '', clearError: () => {}, partial: '',
    download: null, sampleRef: { current: {} }, switchDevice: () => {}, deviceSwitchIsLive: false,
  },
  ptt: {
    config: { enabled: true, mode: 'hybrid', binding: { code: 'Space', alt: true, shift: true } as Record<string, unknown>, holdMs: 500 },
    phase: 'idle',
    holding: false,
  },
}))
vi.mock('../../hooks/useVoiceInput', () => ({ useVoiceInput: () => fx.engine, voiceInputSupported: true }))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => fx.ptt }))
vi.mock('../../api/client', () => ({ api: { sttConfig: vi.fn().mockResolvedValue(null) } }))

import { useComposerVoice, _resetMicOwner } from './useComposerVoice'

const STT_ON = { enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local' }
const CHORD = { code: 'Space', alt: true, shift: true }

function mount({ pushToTalk = true, stt = STT_ON as typeof STT_ON | null } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (stt) qc.setQueryData(['sttConfig'], stt)
  const inputRef = { current: '' }
  return renderHook(
    () => useComposerVoice({ sessionId: 'slot-a', inputRef, setInput: () => {}, pushToTalk }),
    { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> },
  )
}

beforeEach(() => {
  _resetMicOwner()
  fx.engine.recording = false
  fx.engine.sessionOwner = null
  fx.ptt.config = { enabled: true, mode: 'hybrid', binding: CHORD, holdMs: 500 }
})

describe('useComposerVoice — the push-to-talk key the mic may name', () => {
  it('names the key on the composer that owns it, with STT ready', () => {
    expect(mount().result.current.pttBinding).toEqual(CHORD)
  })

  it('names no key on a composer that does not own it', () => {
    expect(mount({ pushToTalk: false }).result.current.pttBinding).toBeNull()
  })

  it('names no key when the key trigger is off', () => {
    fx.ptt.config = { ...fx.ptt.config, enabled: false }
    expect(mount().result.current.pttBinding).toBeNull()
  })

  it('names no key when STT is off, unavailable or not loaded yet', () => {
    expect(mount({ stt: { ...STT_ON, enabled: false } }).result.current.pttBinding).toBeNull()
    expect(mount({ stt: { ...STT_ON, available: false } }).result.current.pttBinding).toBeNull()
    expect(mount({ stt: null }).result.current.pttBinding).toBeNull()
  })

  it('keeps the name through a recording this composer owns, since the key stops it', () => {
    fx.engine.recording = true
    fx.engine.sessionOwner = 'slot-a'
    expect(mount({ stt: { ...STT_ON, available: false } }).result.current.pttBinding).toEqual(CHORD)
  })

  it('follows the binding usePushToTalk reports', () => {
    const hook = mount()
    const rebound = { code: 'KeyV', ctrl: true, shift: true }
    fx.ptt.config = { ...fx.ptt.config, binding: rebound }
    hook.rerender()
    expect(hook.result.current.pttBinding).toEqual(rebound)
  })
})
