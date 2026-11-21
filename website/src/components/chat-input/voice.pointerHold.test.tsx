import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'

type Controls = { start: () => unknown }
const captured: { touch?: Controls; pointer?: Controls } = {}

vi.mock('../../hooks/useTouchPushToTalk', () => ({
  useTouchPushToTalk: (voice: Controls) => {
    captured.touch = voice
    return { owns: false, phase: 'idle', zone: null }
  },
}))
vi.mock('../../hooks/usePointerHoldToTalk', () => ({
  usePointerHoldToTalk: (voice: Controls) => {
    captured.pointer = voice
    return { owns: false, phase: 'idle' }
  },
}))
vi.mock('../../store', () => ({ useAppSelector: () => null }))

import { useHoldToTalk } from './voice'

afterEach(() => {
  captured.touch = undefined
  captured.pointer = undefined
})

function renderWith(onVoiceStart: ((opts?: { silent?: boolean }) => void) | undefined) {
  return renderHook(() => useHoldToTalk({
    onVoiceStart,
    onVoiceStop: vi.fn(),
    onVoiceCancel: vi.fn(),
    voiceCaptureActive: false,
    voiceRecording: false,
    voiceTranscribeActive: false,
    voiceTranscribing: false,
    voiceDownload: undefined,
    voiceBusyElsewhere: false,
    voiceBusyElsewhereSession: undefined,
    composerHasDraft: false,
    disabled: false,
    optimizing: false,
    placeholder: '',
    showDictation: null,
  }))
}

describe('useHoldToTalk gesture starts', () => {
  it('starts the pointer hold silently, so a slow click never opens voice setup', () => {
    const onVoiceStart = vi.fn()
    renderWith(onVoiceStart)
    captured.pointer!.start()
    expect(onVoiceStart).toHaveBeenCalledTimes(1)
    expect(onVoiceStart).toHaveBeenCalledWith({ silent: true })
  })

  it('leaves the touch hold bar start unchanged', () => {
    const onVoiceStart = vi.fn()
    renderWith(onVoiceStart)
    expect(captured.touch!.start).toBe(onVoiceStart)
  })

  it('gives the pointer hold a no-op start when voice input is unsupported', () => {
    renderWith(undefined)
    expect(() => captured.pointer!.start()).not.toThrow()
  })
})
