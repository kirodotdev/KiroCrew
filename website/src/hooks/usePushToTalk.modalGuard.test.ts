import { createElement } from 'react'
import { act, cleanup, render, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CrewEditorChunkLoading from '../components/crew/CrewEditorChunkLoading'
import { savePttConfig, type PttConfig } from '../lib/pushToTalk'
import { usePushToTalk, type VoiceControls } from './usePushToTalk'

const CONFIG: PttConfig = { enabled: true, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }

function makeVoice() {
  const voice: VoiceControls = {
    recording: false,
    start: vi.fn(() => { voice.recording = true }),
    stop: vi.fn(() => { voice.recording = false }),
    cancel: vi.fn(() => { voice.recording = false }),
  }
  return voice
}

function key(type: 'keydown' | 'keyup', target: EventTarget = document, init: KeyboardEventInit = {}) {
  const event = new KeyboardEvent(type, { code: 'AltRight', altKey: type === 'keydown', bubbles: true, cancelable: true, ...init })
  act(() => { target.dispatchEvent(event) })
  return event
}

let fixture: HTMLDivElement
let composer: HTMLTextAreaElement
beforeEach(() => {
  vi.useFakeTimers()
  localStorage.clear()
  savePttConfig(CONFIG)
  fixture = document.createElement('div')
  composer = document.createElement('textarea')
  fixture.append(composer)
  document.body.append(fixture)
})
afterEach(() => {
  cleanup()
  fixture.remove()
  vi.useRealTimers()
  localStorage.clear()
})

function overlay(attrs: Record<string, string>) {
  const dialog = document.createElement('div')
  for (const [name, value] of Object.entries(attrs)) dialog.setAttribute(name, value)
  fixture.append(dialog)
  return dialog
}

function mountVoice() {
  const voice = makeVoice()
  return { voice, ...renderHook(() => usePushToTalk(voice)) }
}

const OPEN_OVERLAYS = [
  ['ARIA modal', { role: 'dialog', 'aria-modal': 'true' }],
  ['Radix dialog', { role: 'dialog', 'data-state': 'open' }],
] as const

describe('push-to-talk overlay start guard', () => {
  it.each(OPEN_OVERLAYS)('blocks a key start under an open %s', (_name, attrs) => {
    // The dialog isolates bubble-phase keys, but the driver listens in capture.
    const dialog = overlay(attrs)
    const field = document.createElement('input')
    dialog.append(field)
    field.focus()
    const isolate = vi.fn((event: Event) => event.stopPropagation())
    dialog.addEventListener('keydown', isolate)
    const { voice, result } = mountVoice()
    key('keydown', field)
    act(() => { vi.advanceTimersByTime(500) })
    key('keyup', field)
    expect(isolate).toHaveBeenCalledTimes(1)
    expect(voice.start).not.toHaveBeenCalled()
    expect(voice.recording).toBe(false)
    expect(result.current.phase).toBe('idle')
  })

  it.each(['hybrid', 'toggle', 'ptt'] as const)('blocks new gestures in %s mode', mode => {
    savePttConfig({ ...CONFIG, mode })
    overlay({ 'aria-modal': 'true' })
    const voice = makeVoice()
    const { result } = renderHook(() => usePushToTalk(voice))
    key('keydown')
    key('keyup')
    expect(voice.start).not.toHaveBeenCalled()
    expect(result.current.phase).toBe('idle')
  })

  it('does not claim a chord key belonging to a blocking dialog', () => {
    savePttConfig({ ...CONFIG, binding: { code: 'Space', alt: true, shift: true } })
    const dialog = overlay({ role: 'dialog', 'data-state': 'open' })
    const { voice } = mountVoice()
    const event = key('keydown', dialog, { code: 'Space', altKey: true, shiftKey: true })
    expect(event.defaultPrevented).toBe(false)
    expect(voice.start).not.toHaveBeenCalled()
  })

  it('starts with an always-mounted closed Radix dialog', () => {
    overlay({ role: 'dialog', 'data-state': 'closed' })
    const { voice, result } = mountVoice()
    key('keydown')
    expect(voice.start).toHaveBeenCalledTimes(1)
    expect(result.current.phase).toBe('arming')
  })

  it('stops a running latch after a modal opens, then blocks a restart', () => {
    const { voice } = mountVoice()
    key('keydown')
    key('keyup')
    expect(voice.recording).toBe(true)
    const dialog = overlay({ role: 'dialog', 'data-state': 'open' })
    key('keydown', dialog)
    expect(voice.stop).toHaveBeenCalledWith('finish')
    expect(voice.recording).toBe(false)
    key('keyup', dialog)
    key('keydown', dialog)
    expect(voice.start).toHaveBeenCalledTimes(1)
  })

  it('still releases a held capture after a modal opens', () => {
    const { voice } = mountVoice()
    key('keydown')
    act(() => { vi.advanceTimersByTime(500) })
    const dialog = overlay({ 'aria-modal': 'true' })
    key('keyup', dialog)
    expect(voice.stop).toHaveBeenCalledWith('finish')
    expect(voice.recording).toBe(false)
  })

  it('still reconciles a lost release after a modal opens', () => {
    const { voice } = mountVoice()
    key('keydown')
    act(() => { vi.advanceTimersByTime(500) })
    const dialog = overlay({ 'aria-modal': 'true' })
    key('keydown', dialog, { code: 'KeyA', altKey: false })
    expect(voice.stop).toHaveBeenCalledWith(undefined)
    expect(voice.recording).toBe(false)
  })

  it('the rendered editor loading box blocks start until it unmounts', () => {
    const { voice } = mountVoice()
    const loading = render(createElement(CrewEditorChunkLoading))
    expect(loading.getByRole('dialog')).toHaveAttribute('aria-modal', 'true')
    key('keydown')
    key('keyup')
    expect(voice.start).not.toHaveBeenCalled()
    loading.unmount()
    key('keydown')
    expect(voice.start).toHaveBeenCalledTimes(1)
  })

  it('starts normally in main chat with no overlay', () => {
    const voice = makeVoice()
    renderHook(() => usePushToTalk(voice))
    key('keydown')
    expect(voice.start).toHaveBeenCalledTimes(1)
  })

  it('does not treat a plain dialog-role picker menu as a modal', () => {
    // ChatPanePickerMenus portals have role="dialog" but neither modal ARIA
    // nor Radix's open state. Their filter inputs do not cover the composer.
    overlay({ role: 'dialog', 'aria-label': 'Model list' })
    const { voice } = mountVoice()
    key('keydown')
    expect(voice.start).toHaveBeenCalledTimes(1)
  })

  it('does not treat an open Radix popover (non-modal, popper-wrapped) as a modal', () => {
    // Radix Popover content carries role="dialog" + data-state="open" inside
    // its popper wrapper, e.g. a passive teaching callout beside the composer.
    const wrapper = overlay({ 'data-radix-popper-content-wrapper': '' })
    const popover = document.createElement('div')
    popover.setAttribute('role', 'dialog')
    popover.setAttribute('data-state', 'open')
    wrapper.append(popover)
    const { voice } = mountVoice()
    key('keydown')
    expect(voice.start).toHaveBeenCalledTimes(1)
  })

  it('queries overlays only for a matching non-repeated start', () => {
    const { voice } = mountVoice()
    const query = vi.spyOn(document, 'querySelector')
    key('keydown', document, { code: 'KeyA' })
    key('keydown', document, { repeat: true })
    expect(query).not.toHaveBeenCalled()
    key('keydown')
    expect(query).toHaveBeenCalledTimes(1)
    key('keyup')
    key('keydown')
    expect(voice.stop).toHaveBeenCalledTimes(1)
    expect(query).toHaveBeenCalledTimes(1)
    query.mockRestore()
  })
})
