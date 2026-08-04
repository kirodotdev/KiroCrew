/**
 * ui/dialog — keyboard isolation.
 *
 * A dialog holding unsaved input must not let the page's global shortcuts fire
 * underneath it. `useKeyboardShortcuts` binds `document.addEventListener
 * ('keydown', ...)` in the BUBBLE phase, so a Cmd+, typed into a half-filled
 * form would otherwise reach it, navigate to Settings, and unmount the dialog
 * with the input still in it. `SideSheet` carried this guard; the centered
 * dialog that replaced it on the Crews page has to as well.
 *
 * The guard must be surgical: Radix's own Escape handling uses
 * `{ capture: true }`, which runs BEFORE the event reaches the dialog, so
 * stopping bubble propagation must not cost dismissal. Both halves are asserted
 * here.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '../components/ui/dialog'

function renderDialog(extra: Record<string, unknown> = {}) {
  return render(
    <Dialog open onOpenChange={() => {}}>
      <DialogContent aria-label="Test dialog" {...extra}>
        <DialogHeader><DialogTitle>T</DialogTitle></DialogHeader>
        <input aria-label="field" />
      </DialogContent>
    </Dialog>,
  )
}

describe('ui/dialog — keyboard isolation', () => {
  it('does not let a keystroke inside it reach a bubble-phase document listener', () => {
    const globalShortcut = vi.fn()
    document.addEventListener('keydown', globalShortcut)
    try {
      renderDialog()
      // A real Cmd+, typed into the form, dispatched AT the field so it bubbles
      // the way a browser would deliver it.
      fireEvent.keyDown(screen.getByLabelText('field'), { key: ',', code: 'Comma', metaKey: true })
      expect(globalShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', globalShortcut)
    }
  })

  it('still reaches a CAPTURE-phase document listener, which is how Radix dismisses', () => {
    // This is the half that makes the guard safe rather than a dismissal bug.
    const capturing = vi.fn()
    document.addEventListener('keydown', capturing, { capture: true })
    try {
      renderDialog()
      fireEvent.keyDown(screen.getByLabelText('field'), { key: 'Escape' })
      expect(capturing).toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', capturing, { capture: true })
    }
  })

  it('still runs a caller-supplied onKeyDown', () => {
    // The guard wraps the caller's handler rather than replacing it.
    const onKeyDown = vi.fn()
    renderDialog({ onKeyDown })
    fireEvent.keyDown(screen.getByLabelText('field'), { key: 'a' })
    expect(onKeyDown).toHaveBeenCalled()
  })
})
