/**
 * Find-in-chat's document-level chord on macOS.
 *
 * `hasCommandModifier` is an XOR (Cmd or Ctrl, never both), which is right for
 * every other chord but wrong for `f` on a Mac: Ctrl+F is Cocoa's standard
 * `forward-char` Emacs binding, live in every text field including the chat
 * composer. A document listener that answered it would `preventDefault()` the
 * keystroke and open the find pane instead of moving the caret one character
 * right — so on Mac the pane is Cmd+F only, and Ctrl+F is left unhandled.
 *
 * Windows/Linux keep Ctrl+F as the trigger; that half is pinned by the bare
 * Ctrl+F cases in `useMessageSearch.test.ts`, which run with the default
 * `isMac === false` (happy-dom reports `navigator.platform` as '').
 *
 * `isMac` is resolved once at module load from the UA, so mocking the module is
 * the only way to reach the Mac branch — same reason `MoveUndoBar.test.tsx`
 * mocks it to pin ⌘Z over Ctrl+Z.
 */
import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

vi.mock('../utils/platform', () => ({
  isMac: true,
  platformShortcut: (s: string) => s,
}))

import { useMessageSearch } from '../hooks/useMessageSearch'
import type { ChatMessage } from '../types'

const messages: ChatMessage[] = [{ role: 'user', content: 'hello world', cls: '' }]

/** Dispatch a cancelable document keydown the way a real keypress arrives. */
const press = (init: KeyboardEventInit): KeyboardEvent => {
  const event = new KeyboardEvent('keydown', { key: 'f', cancelable: true, ...init })
  act(() => { document.dispatchEvent(event) })
  return event
}

describe('useMessageSearch find chord on macOS', () => {
  it('leaves bare Ctrl+F unhandled so the Cocoa forward-char binding still fires', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    const event = press({ ctrlKey: true })
    expect(result.current.isOpen).toBe(false)
    expect(event.defaultPrevented).toBe(false)
  })

  it('opens the pane on Cmd+F and claims the keystroke', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    const event = press({ metaKey: true })
    expect(result.current.isOpen).toBe(true)
    expect(event.defaultPrevented).toBe(true)
  })

  it('still leaves Ctrl+Cmd+F to the OS (Toggle Full Screen)', () => {
    const { result } = renderHook(() => useMessageSearch(messages, 'slot-1'))
    const event = press({ metaKey: true, ctrlKey: true })
    expect(result.current.isOpen).toBe(false)
    expect(event.defaultPrevented).toBe(false)
  })
})
