/**
 * The panel-toggle recorder's Control branch on macOS.
 *
 * On a Mac the recorder maps `metaKey` to the platform-neutral `mod`, so before
 * #10303 a Control chord reached `isValidChord` as a bare key and was silently
 * dropped — no Ctrl+… shortcut was bindable at all. These cases pin the fixed
 * behaviour: `ctrlKey` lands as the chord's `ctrl` flag (Control held
 * independently of Cmd) and the binding persists.
 *
 * `isMac` is resolved once at module load from the UA, so mocking the module is
 * the only way to reach the Mac branch — same reason `useMessageSearchMacFind`
 * mocks it. The Windows/Linux recorder half (Ctrl → `mod`) is pinned by
 * `usePanelToggleShortcuts.test.tsx`, which runs with the default
 * `isMac === false`.
 */
import { act } from 'react'

import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../utils/platform', () => ({
  isMac: true,
  platformShortcut: (s: string) => s,
}))

import { usePanelToggleShortcuts } from '../hooks/usePanelToggleShortcuts'
import { PANEL_TOGGLE_SHORTCUTS_KEY } from '../lib/panelToggleShortcuts'

beforeEach(() => localStorage.clear())

/** Dispatch a window keydown the way a real keypress arrives at the recorder. */
function pressKey(init: KeyboardEventInit): KeyboardEvent {
  const event = new KeyboardEvent('keydown', { cancelable: true, bubbles: true, ...init })
  act(() => {
    window.dispatchEvent(event)
  })
  return event
}

describe('usePanelToggleShortcuts recorder on macOS', () => {
  it('records Ctrl+` as a ctrl chord and claims the keystroke', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('terminal'))
    const event = pressKey({ code: 'Backquote', key: '`', ctrlKey: true })
    expect(result.current.recordingId).toBeNull()
    expect(result.current.bindings['terminal']).toEqual({ key: '`', ctrl: true })
    expect(JSON.parse(localStorage.getItem(PANEL_TOGGLE_SHORTCUTS_KEY)!)['terminal']).toEqual({ key: '`', ctrl: true })
    expect(event.defaultPrevented).toBe(true)
  })

  it('records Cmd+Ctrl+key with both flags, kept distinct on a Mac', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('left-sidebar'))
    pressKey({ code: 'KeyJ', key: 'j', metaKey: true, ctrlKey: true })
    expect(result.current.bindings['left-sidebar']).toEqual({ key: 'j', mod: true, ctrl: true })
  })

  it('still maps a plain Cmd chord to mod without a stray ctrl flag', () => {
    const { result } = renderHook(() => usePanelToggleShortcuts())
    act(() => result.current.startRecording('session-panel'))
    pressKey({ code: 'KeyB', key: 'b', metaKey: true })
    expect(result.current.bindings['session-panel']).toEqual({ key: 'b', mod: true })
  })
})
