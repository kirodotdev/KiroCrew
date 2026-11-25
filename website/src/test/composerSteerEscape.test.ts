// The composer's take-back gate: a bare Escape takes back the newest steer the
// running turn has not read yet, behind the optimize gate and the claimants
// both yield to. Escape never stops a running turn; the Stop button does.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import type { RefObject } from 'react'
import type { ComposerEscapeEvent } from '../components/chat-input/optimizeEscape'
import { steerTakeBackHintShown, useComposerEscape, type SteerEscape, type SteerEscapeClaimants } from '../components/chat-input/steerEscape'
import { noteEscapeStoppedSpeech } from '../utils/speechStopKey'

type Opts = Parameters<typeof useComposerEscape>[0]

const ref = (on: boolean): RefObject<boolean> => ({ current: on })

const steers = (offered: number, reachable = offered): SteerEscape => ({ offered, reachable, takeBack: vi.fn(() => reachable > 0) })

function base(over: Partial<Opts> = {}): Opts {
  return {
    steerEscape: steers(1),
    composerReadOnlyRef: ref(false),
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
  return renderHook((p: Opts) => useComposerEscape(p), { initialProps: opts })
}

type Mounted = ReturnType<typeof mount>

function fire(hook: Mounted, { composing = false, repeat = false, native = new KeyboardEvent('keydown', { key: 'Escape' }) } = {}) {
  const e = { repeat, preventDefault: vi.fn() } satisfies ComposerEscapeEvent
  return { claimed: hook.result.current(e, { native, composing }), e }
}

function press(opts: Opts, keydown: Parameters<typeof fire>[1] = {}) {
  return fire(mount(opts), keydown)
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

  it('claims nothing when the page finds no steer to take back after all, and neither do the repeats of that hold', () => {
    const steerEscape: SteerEscape = { offered: 1, reachable: 1, takeBack: vi.fn(() => false) }
    const hook = mount(base({ steerEscape }))
    const first = fire(hook)
    expect(first.claimed).toBe(false)
    expect(first.e.preventDefault).not.toHaveBeenCalled()
    expect(fire(hook, { repeat: true }).claimed).toBe(false)
  })

  it('a held Escape takes back one steer: its repeats are claimed and do nothing, even once no steer is left', () => {
    const steerEscape = steers(1)
    const hook = mount(base({ steerEscape }))
    expect(fire(hook).claimed).toBe(true)
    expect(steerEscape.takeBack).toHaveBeenCalledTimes(1)
    const repeat = fire(hook, { repeat: true })
    expect(repeat.claimed).toBe(true)
    expect(repeat.e.preventDefault).toHaveBeenCalledTimes(1)
    hook.rerender(base({ steerEscape: steers(0) }))
    const later = fire(hook, { repeat: true })
    expect(later.claimed).toBe(true)
    expect(later.e.preventDefault).toHaveBeenCalledTimes(1)
    expect(steerEscape.takeBack).toHaveBeenCalledTimes(1)
  })

  it('the repeats of a hold that took nothing back pass on, and a new press starts a new hold', () => {
    const hook = mount(base({ steerEscape: steers(0) }))
    expect(fire(hook).claimed).toBe(false)
    const repeat = fire(hook, { repeat: true })
    expect(repeat.claimed).toBe(false)
    expect(repeat.e.preventDefault).not.toHaveBeenCalled()
    const steerEscape = steers(1)
    hook.rerender(base({ steerEscape }))
    expect(fire(hook).claimed).toBe(true)
    expect(steerEscape.takeBack).toHaveBeenCalledTimes(1)
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

  it('yields to a picker, dictation, an approval, IME, a voice read-back stop, a dialog, and a box that takes no text', () => {
    const steerEscape = steers(1)
    const opts = (over: Partial<Opts> = {}) => base({ steerEscape, ...over })
    expect(press(opts({ anyPickerOpenRef: ref(true) })).claimed).toBe(false)
    expect(press(opts({ voiceRecording: true })).claimed).toBe(false)
    expect(press(opts({ voiceDrainCancellable: true })).claimed).toBe(false)
    expect(press(opts({ hasApproval: true })).claimed).toBe(false)
    expect(press(opts({ approvalNudgeActive: true })).claimed).toBe(false)
    expect(press(opts({ composerReadOnlyRef: ref(true) })).claimed).toBe(false)
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
})

describe('steerTakeBackHintShown', () => {
  const free: SteerEscapeClaimants = { anyPickerOpen: false, voiceRecording: false, voiceDrainCancellable: false, hasApproval: false, approvalNudgeActive: false, composerReadOnly: false }

  it('shows while a row offers a take-back', () => {
    expect(steerTakeBackHintShown(steers(1), free)).toBe(true)
    expect(steerTakeBackHintShown(steers(3), free)).toBe(true)
  })

  it('shows nothing for a steer no press can take back yet, with none pending, or on a surface without steers', () => {
    expect(steerTakeBackHintShown(steers(0, 1), free)).toBe(false)
    expect(steerTakeBackHintShown(steers(0), free)).toBe(false)
    expect(steerTakeBackHintShown(undefined, free)).toBe(false)
  })

  it.each([
    ['a picker', { anyPickerOpen: true }],
    ['a dictation', { voiceRecording: true }],
    ['a draining dictation', { voiceDrainCancellable: true }],
    ['an approval', { hasApproval: true }],
    ['the approval-mode nudge', { approvalNudgeActive: true }],
    ['a box that takes no text', { composerReadOnly: true }],
  ] as const)('shows nothing while %s holds Escape', (_case, over) => {
    expect(steerTakeBackHintShown(steers(1), { ...free, ...over })).toBe(false)
  })

  it('shows nothing while a dialog or another Escape owner is open', () => {
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    expect(steerTakeBackHintShown(steers(1), free)).toBe(false)
    document.body.replaceChildren()
    const panel = document.createElement('div')
    panel.setAttribute('data-escape-owner', '')
    document.body.appendChild(panel)
    expect(steerTakeBackHintShown(steers(1), free)).toBe(false)
  })
})
