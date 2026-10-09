import { describe, expect, it, vi } from 'vitest'
import { activateTerminalEntry, isTerminalShown } from './terminalEntry'

function activate(open: boolean, workspaceFullscreen: boolean, poppedOut = false) {
  const calls: string[] = []
  activateTerminalEntry({
    open,
    workspaceFullscreen,
    poppedOut,
    exitFullscreen: workspaceFullscreen ? () => calls.push('exit') : undefined,
    focusPopout: () => calls.push('focus'),
    toggle: () => calls.push('toggle'),
  })
  return calls
}

describe('terminal entries under workspace fullscreen', () => {
  it('treats an open terminal as shown only outside fullscreen', () => {
    expect(isTerminalShown(true, false)).toBe(true)
    expect(isTerminalShown(true, true)).toBe(false)
    expect(isTerminalShown(false, false)).toBe(false)
  })

  it('toggles outside fullscreen', () => {
    expect(activate(false, false)).toEqual(['toggle'])
    expect(activate(true, false)).toEqual(['toggle'])
  })

  it('exits fullscreen before opening a closed terminal', () => {
    expect(activate(false, true)).toEqual(['exit', 'toggle'])
  })

  it('reveals an open terminal covered by fullscreen without closing it', () => {
    expect(activate(true, true)).toEqual(['exit'])
  })

  it('focuses a popped-out terminal after leaving fullscreen', () => {
    expect(activate(true, true, true)).toEqual(['exit', 'focus'])
    const toggle = vi.fn()
    activateTerminalEntry({ open: false, workspaceFullscreen: false, poppedOut: true, focusPopout: vi.fn(), toggle })
    expect(toggle).not.toHaveBeenCalled()
  })
})
