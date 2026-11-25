// The composer's take-back gate: a bare Escape takes back the newest steer the
// running turn has not read yet, behind the optimize gate and the claimants
// both yield to. Escape never stops a running turn; the Stop button does.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { RefObject } from 'react'
import { useComposerOptimizeEscape, type ComposerEscapeEvent } from '../components/chat-input/optimizeEscape'
import { useComposerSteerEscape, type SteerEscape } from '../components/chat-input/steerEscape'
import { noteEscapeStoppedSpeech } from '../utils/speechStopKey'

type Opts = Omit<Parameters<typeof useComposerSteerEscape>[0], 'optimizeEscape'> & { onCancelOptimize?: () => void }

const ref = (on: boolean): RefObject<boolean> => ({ current: on })

const steers = (offered: number, reachable = offered): SteerEscape => ({ offered, reachable, takeBack: vi.fn(() => reachable > 0) })

function base(over: Partial<Opts> = {}): Opts {
  return {
    steerEscape: steers(1),
    composerReadOnly: false,
    anyPickerOpenRef: ref(false),
    voiceRecording: false,
    voiceDrainCancellable: false,
    hasApproval: false,
    approvalNudgeActive: false,
    optimizingRef: ref(false),
    onCancelOptimize: vi.fn(),
    ...over,
  }
}

function mount(opts: Opts) {
  const { onCancelOptimize, ...rest } = opts
  return renderHook(() => {
    const optimizeEscape = useComposerOptimizeEscape({
      anyPickerOpenRef: rest.anyPickerOpenRef, voiceRecording: rest.voiceRecording, voiceDrainCancellable: rest.voiceDrainCancellable,
      hasApproval: rest.hasApproval, approvalNudgeActive: rest.approvalNudgeActive, optimizingRef: rest.optimizingRef, onCancelOptimize,
    })
    return useComposerSteerEscape({ optimizeEscape, ...rest })
  })
}

function press(opts: Opts, { composing = false, repeat = false, native = new KeyboardEvent('keydown', { key: 'Escape' }) } = {}) {
  const e = { repeat, preventDefault: vi.fn() } satisfies ComposerEscapeEvent
  const { result } = mount(opts)
  return { claimed: result.current.composerEscape(e, { native, composing }), e, hint: result.current.takeBackHint }
}

afterEach(() => { document.body.replaceChildren() })

describe('useComposerSteerEscape', () => {
  it('takes back the newest pending steer and claims the key', () => {
    const steerEscape = steers(2)
    const { claimed, e } = press(base({ steerEscape }))
    expect(claimed).toBe(true)
    expect(steerEscape.takeBack).toHaveBeenCalledTimes(1)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
  })

  it('reaches a steer whose row has no id yet, so the press is kept for its echo', () => {
    const steerEscape = steers(0, 1)
    const { claimed } = press(base({ steerEscape }))
    expect(claimed).toBe(true)
    expect(steerEscape.takeBack).toHaveBeenCalledTimes(1)
  })

  it('claims nothing with no steer to take back, or on a surface that cannot take one back', () => {
    const none = steers(0)
    expect(press(base({ steerEscape: none })).claimed).toBe(false)
    expect(none.takeBack).not.toHaveBeenCalled()
    const { claimed, e } = press(base({ steerEscape: undefined }))
    expect(claimed).toBe(false)
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('takes nothing back on a held Escape, and still claims the key', () => {
    const steerEscape = steers(2)
    const { claimed, e } = press(base({ steerEscape }), { repeat: true })
    expect(claimed).toBe(true)
    expect(e.preventDefault).toHaveBeenCalledTimes(1)
    expect(steerEscape.takeBack).not.toHaveBeenCalled()
  })

  it('leaves the key to an in-flight optimize, which the press cancels instead', () => {
    const steerEscape = steers(1)
    const onCancelOptimize = vi.fn()
    const { claimed } = press(base({ steerEscape, optimizingRef: ref(true), onCancelOptimize }))
    expect(claimed).toBe(true)
    expect(onCancelOptimize).toHaveBeenCalledTimes(1)
    expect(steerEscape.takeBack).not.toHaveBeenCalled()
    const held = steers(1)
    expect(press(base({ steerEscape: held, optimizingRef: ref(true), onCancelOptimize: undefined })).claimed).toBe(false)
    expect(held.takeBack).not.toHaveBeenCalled()
  })

  it('yields to a picker, dictation, an approval, IME, a voice read-back stop and a dialog', () => {
    const steerEscape = steers(1)
    const opts = (over: Partial<Opts> = {}) => base({ steerEscape, ...over })
    expect(press(opts({ anyPickerOpenRef: ref(true) })).claimed).toBe(false)
    expect(press(opts({ voiceRecording: true })).claimed).toBe(false)
    expect(press(opts({ voiceDrainCancellable: true })).claimed).toBe(false)
    expect(press(opts({ hasApproval: true })).claimed).toBe(false)
    expect(press(opts({ approvalNudgeActive: true })).claimed).toBe(false)
    expect(press(opts(), { composing: true }).claimed).toBe(false)
    const native = new KeyboardEvent('keydown', { key: 'Escape' })
    noteEscapeStoppedSpeech(native)
    expect(press(opts(), { native }).claimed).toBe(false)
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    expect(press(opts()).claimed).toBe(false)
    expect(steerEscape.takeBack).not.toHaveBeenCalled()
  })

  describe('the hint', () => {
    it('shows while a row offers a take-back', () => {
      expect(press(base({ steerEscape: steers(1) })).hint).toBe(true)
      expect(press(base({ steerEscape: steers(3) })).hint).toBe(true)
    })

    it('shows nothing for a steer no press can take back yet, with none pending, or on a surface without steers', () => {
      expect(press(base({ steerEscape: steers(0, 1) })).hint).toBe(false)
      expect(press(base({ steerEscape: steers(0) })).hint).toBe(false)
      expect(press(base({ steerEscape: undefined })).hint).toBe(false)
    })

    it('shows nothing while a dictation or an approval owns Escape, or the message box takes no text', () => {
      const opts = (over: Partial<Opts> = {}) => base({ steerEscape: steers(1), ...over })
      expect(press(opts({ voiceRecording: true })).hint).toBe(false)
      expect(press(opts({ voiceDrainCancellable: true })).hint).toBe(false)
      expect(press(opts({ hasApproval: true })).hint).toBe(false)
      expect(press(opts({ approvalNudgeActive: true })).hint).toBe(false)
      expect(press(opts({ composerReadOnly: true })).hint).toBe(false)
    })
  })
})
