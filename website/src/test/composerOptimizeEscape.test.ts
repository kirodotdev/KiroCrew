// The composer's Escape gate: a bare Escape cancels an in-flight optimize and
// nothing else. Escape never stops a running turn; the Stop button does.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { RefObject } from 'react'
import { useComposerOptimizeEscape, type ComposerEscapeEvent } from '../components/chat-input/optimizeEscape'
import { noteEscapeStoppedSpeech } from '../utils/speechStopKey'

type Opts = Parameters<typeof useComposerOptimizeEscape>[0]

const ref = (on: boolean): RefObject<boolean> => ({ current: on })

function base(over: Partial<Opts> = {}): Opts {
  return {
    anyPickerOpenRef: ref(false),
    voiceRecording: false,
    voiceDrainCancellable: false,
    hasApproval: false,
    approvalNudgeActive: false,
    optimizingRef: ref(true),
    onCancelOptimize: vi.fn(),
    ...over,
  }
}

function run(opts: Opts, composing = false, native: Event = new KeyboardEvent('keydown', { key: 'Escape' })) {
  const e = { preventDefault: vi.fn() } satisfies ComposerEscapeEvent
  const { result } = renderHook(() => useComposerOptimizeEscape(opts))
  return { claimed: result.current(e, { native, composing }), e }
}

afterEach(() => { document.body.replaceChildren() })

describe('useComposerOptimizeEscape', () => {
  it('cancels an in-flight optimize and claims the key', () => {
    const onCancelOptimize = vi.fn()
    const { claimed, e } = run(base({ onCancelOptimize }))
    expect(claimed).toBe(true)
    expect(onCancelOptimize).toHaveBeenCalledTimes(1)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
  })

  it('claims nothing when no optimize runs', () => {
    const { claimed, e } = run(base({ optimizingRef: ref(false) }))
    expect(claimed).toBe(false)
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('yields to a picker, dictation, an approval, IME, a voice read-back stop and a dialog', () => {
    const onCancelOptimize = vi.fn()
    const opts = (over: Partial<Opts> = {}) => base({ onCancelOptimize, ...over })
    expect(run(opts({ anyPickerOpenRef: ref(true) })).claimed).toBe(false)
    expect(run(opts({ voiceRecording: true })).claimed).toBe(false)
    expect(run(opts({ voiceDrainCancellable: true })).claimed).toBe(false)
    expect(run(opts({ hasApproval: true })).claimed).toBe(false)
    expect(run(opts({ approvalNudgeActive: true })).claimed).toBe(false)
    expect(run(opts(), true).claimed).toBe(false)
    const native = new KeyboardEvent('keydown', { key: 'Escape' })
    noteEscapeStoppedSpeech(native)
    expect(run(opts(), false, native).claimed).toBe(false)
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    expect(run(opts()).claimed).toBe(false)
    expect(onCancelOptimize).not.toHaveBeenCalled()
  })
})
