import { act } from 'react'

import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import { usePanelToggleShortcuts } from '../hooks/usePanelToggleShortcuts'
import {
  PANEL_TOGGLE_SHORTCUTS_EVENT,
  PANEL_TOGGLE_SHORTCUTS_KEY,
} from '../lib/panelToggleShortcuts'

beforeEach(() => localStorage.clear())

/** Dispatch a window keydown the way a real keypress arrives at the recorder. */
function pressKey(init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { cancelable: true, bubbles: true, ...init })
  act(() => {
    window.dispatchEvent(event)
  })
  return event
}

describe('usePanelToggleShortcuts', () => {
  it('exposes the resolved default bindings with no recording in progress', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    expect(result.current.recordingId).toBeNull()
    expect(result.current.bindings['left-sidebar']).toBeNull()
    expect(result.current.bindings['session-panel']).toEqual({ key: 'b', mod: true })
    expect(result.current.bindings['side-panel']).toEqual({ key: '\\', mod: true })
  })

  it('startRecording marks the panel as recording; cancelRecording aborts without a write', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('session-panel'))
    expect(result.current.recordingId).toBe('session-panel')
    act(() => result.current.cancelRecording())
    expect(result.current.recordingId).toBeNull()
    expect(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)).toBeNull()
    expect(result.current.bindings['session-panel']).toEqual({ key: 'b', mod: true })
  })

  it('recording a valid mod chord persists it, stops recording, and claims the keystroke', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('left-sidebar'))
    // isMac is false under the test UA, so Ctrl is the platform `mod`.
    const event = pressKey({ code: 'KeyJ', key: 'j', ctrlKey: true })
    expect(result.current.recordingId).toBeNull()
    expect(result.current.bindings['left-sidebar']).toEqual({ key: 'j', mod: true })
    expect(JSON.parse(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)!)['left-sidebar']).toEqual({ key: 'j', mod: true })
    // The recorded chord must not also fire an app shortcut on the same keystroke.
    expect(event.defaultPrevented).toBe(true)
  })

  it('records alt and shift into the stored chord', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('side-panel'))
    pressKey({ code: 'KeyP', key: 'p', altKey: true, shiftKey: true })
    expect(result.current.bindings['side-panel']).toEqual({ key: 'p', alt: true, shift: true })
  })

  it('Escape cancels recording and leaves the previous binding live', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('session-panel'))
    const event = pressKey({ key: 'Escape' })
    expect(result.current.recordingId).toBeNull()
    expect(result.current.bindings['session-panel']).toEqual({ key: 'b', mod: true })
    expect(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)).toBeNull()
    expect(event.defaultPrevented).toBe(true)
  })

  it('keeps waiting on a bare modifier press', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('session-panel'))
    pressKey({ key: 'Control', ctrlKey: true })
    expect(result.current.recordingId).toBe('session-panel')
    expect(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)).toBeNull()
  })

  it('refuses a bare-key chord (no mod/alt) and keeps recording', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('session-panel'))
    const event = pressKey({ code: 'KeyX', key: 'x' })
    expect(result.current.recordingId).toBe('session-panel')
    expect(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)).toBeNull()
    // Not claimed: the keystroke stays with whatever else wanted it.
    expect(event.defaultPrevented).toBe(false)
  })

  it('ignores keydowns entirely while not recording', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    pressKey({ code: 'KeyJ', key: 'j', ctrlKey: true })
    expect(result.current.recordingId).toBeNull()
    expect(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)).toBeNull()
  })

  it('clear() unbinds the panel, persists the explicit null, and stops any recording', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('session-panel'))
    act(() => result.current.clear('session-panel'))
    expect(result.current.recordingId).toBeNull()
    expect(result.current.bindings['session-panel']).toBeNull()
    expect(JSON.parse(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)!)['session-panel']).toBeNull()
  })

  it('re-reads bindings when another surface broadcasts a change (same-tab event)', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => {
      localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({ 'side-panel': { key: 'm', mod: true } }))
      window.dispatchEvent(new Event(PANEL_TOGGLE_SHORTCUTS_EVENT))
    })
    expect(result.current.bindings['side-panel']).toEqual({ key: 'm', mod: true })
  })

  it('re-reads bindings on a cross-tab storage event for its own key only', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => {
      localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({ 'session-panel': null }))
      window.dispatchEvent(new StorageEvent('storage', { key: PANEL_TOGGLE_SHORTCUTS_KEY }))
    })
    expect(result.current.bindings['session-panel']).toBeNull()

    act(() => {
      localStorage.setItem(PANEL_TOGGLE_SHORTCUTS_KEY, JSON.stringify({}))
      window.dispatchEvent(new StorageEvent('storage', { key: 'some-other-key' }))
    })
    // Unrelated storage keys do not trigger a refresh.
    expect(result.current.bindings['session-panel']).toBeNull()
  })

  it('detaches its recording listener on unmount', () => {
    const { result, unmount } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('session-panel'))
    unmount()
    pressKey({ code: 'KeyJ', key: 'j', ctrlKey: true })
    expect(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)).toBeNull()
  })
})
