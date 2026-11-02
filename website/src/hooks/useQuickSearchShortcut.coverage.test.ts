/**
 * `useQuickSearchShortcut`: the live preference, the recording state machine and the window-capture
 * recorder behind Settings → Shortcuts. The preference round-trips through the real
 * `lib/quickSearchShortcut` and happy-dom's localStorage, so what is asserted is what a reload would read.
 */
import { act, createEvent, fireEvent, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  loadQuickSearchConfig,
  QUICK_SEARCH_SHORTCUT_EVENT,
  QUICK_SEARCH_SHORTCUT_KEY,
} from '../lib/quickSearchShortcut'
import { isMac } from '../utils/platform'
import { useQuickSearchShortcut } from './useQuickSearchShortcut'

/** The platform's command modifier, read from the same `isMac` the hook reads. */
const MOD: KeyboardEventInit = isMac ? { metaKey: true } : { ctrlKey: true }

afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

/** Dispatch a keydown on window (the hook listens there, in the capture phase) and return the event. */
const press = (init: KeyboardEventInit) => {
  const event = createEvent.keyDown(window, init)
  fireEvent(window, event)
  return event
}

const startRecording = () => {
  const hook = renderHook(() => useQuickSearchShortcut())
  act(() => hook.result.current.selectMode('custom'))
  return hook
}

describe('useQuickSearchShortcut preference', () => {
  it('starts on the default and on a stored preset', () => {
    expect(renderHook(() => useQuickSearchShortcut()).result.current.config).toEqual({ mode: 'mod-k' })
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    expect(renderHook(() => useQuickSearchShortcut()).result.current.config).toEqual({ mode: 'double-shift' })
  })

  it('persists a preset at once and updates the live config', () => {
    const { result } = renderHook(() => useQuickSearchShortcut())
    act(() => result.current.selectMode('double-shift'))
    expect(loadQuickSearchConfig()).toEqual({ mode: 'double-shift' })
    expect(result.current.config).toEqual({ mode: 'double-shift' })
    expect(result.current.recording).toBe(false)
  })

  it('re-reads on a storage event for its own key only', () => {
    const { result } = renderHook(() => useQuickSearchShortcut())
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: 'some-other-key' })) })
    expect(result.current.config.mode).toBe('mod-k')
    act(() => { window.dispatchEvent(new StorageEvent('storage', { key: QUICK_SEARCH_SHORTCUT_KEY })) })
    expect(result.current.config.mode).toBe('double-shift')
  })

  it('removes its listeners on unmount', () => {
    const remove = vi.spyOn(window, 'removeEventListener')
    renderHook(() => useQuickSearchShortcut()).unmount()
    const removed = remove.mock.calls.map(([type]) => type)
    expect(removed).toEqual(expect.arrayContaining([QUICK_SEARCH_SHORTCUT_EVENT, 'storage']))
  })
})

describe('useQuickSearchShortcut recording', () => {
  it('enters recording on "custom" without persisting anything', () => {
    const { result } = startRecording()
    expect(result.current.recording).toBe(true)
    expect(localStorage.getItem(QUICK_SEARCH_SHORTCUT_KEY)).toBeNull()
  })

  it('captures a chord with a modifier, saves it and stops recording', () => {
    const { result } = startRecording()
    const event = press({ code: 'KeyJ', key: 'j', altKey: true, shiftKey: true, ...MOD })
    expect(event.defaultPrevented).toBe(true)
    const custom = { key: 'j', mod: true, alt: true, shift: true }
    expect(loadQuickSearchConfig()).toEqual({ mode: 'custom', custom })
    expect(result.current.config).toEqual({ mode: 'custom', custom })
    expect(result.current.recording).toBe(false)
  })

  it('keeps waiting on a bare key or a bare modifier press', () => {
    const { result } = startRecording()
    expect(press({ code: 'KeyJ', key: 'j' }).defaultPrevented).toBe(false)
    expect(press({ key: 'Shift', shiftKey: true }).defaultPrevented).toBe(false)
    expect(result.current.recording).toBe(true)
    expect(localStorage.getItem(QUICK_SEARCH_SHORTCUT_KEY)).toBeNull()
  })

  it('cancels on Escape and leaves the stored preference as it was', () => {
    localStorage.setItem(QUICK_SEARCH_SHORTCUT_KEY, JSON.stringify({ mode: 'double-shift' }))
    const { result } = startRecording()
    expect(press({ key: 'Escape' }).defaultPrevented).toBe(true)
    expect(result.current.recording).toBe(false)
    expect(loadQuickSearchConfig()).toEqual({ mode: 'double-shift' })
  })

  it('startRecording and cancelRecording toggle the recorder', () => {
    const { result } = renderHook(() => useQuickSearchShortcut())
    act(() => result.current.startRecording())
    expect(result.current.recording).toBe(true)
    act(() => result.current.cancelRecording())
    expect(result.current.recording).toBe(false)
  })

  it('stops listening to keys once recording ends', () => {
    const { result } = startRecording()
    act(() => result.current.cancelRecording())
    expect(press({ code: 'KeyJ', key: 'j', ...MOD }).defaultPrevented).toBe(false)
    expect(localStorage.getItem(QUICK_SEARCH_SHORTCUT_KEY)).toBeNull()
  })
})
