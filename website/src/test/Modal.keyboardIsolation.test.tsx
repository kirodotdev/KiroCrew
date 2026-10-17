/**
 * Modal — keyboard isolation.
 *
 * A dialog holding unsaved input must not let the page's global shortcuts fire
 * underneath it. `useKeyboardShortcuts` binds `document.addEventListener
 * ('keydown', ...)` in the BUBBLE phase (plus a `window` binding for the
 * Ctrl-held badge), and several chords deliberately fire while an input has
 * focus — so a Ctrl+digit typed into a half-filled form would otherwise reach
 * it, navigate away, and unmount the dialog with the input still in it. The
 * `ui/dialog` family carries this guard (see DialogKeyboardIsolation.test.tsx);
 * the shared Modal has to as well — in the component, not per call site, so
 * all ~24 consumers get it.
 *
 * The guard must be surgical, and the surgical part differs from ui/dialog:
 * Modal's own dismissal is a BUBBLE-phase `window` listener, so unlike Radix's
 * capture-phase dismissal it does NOT survive a blanket stop. Escape must keep
 * bubbling (asserted here), except an Escape the IME owns — that one is
 * cancelling a candidate list, not the dialog, and must NOT dismiss (also
 * asserted here).
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import Modal from '../components/Modal'
import { Lightbox } from '../components/MarkdownRenderer'

function renderModal(onClose = vi.fn()) {
  render(
    <Modal open onClose={onClose} title="T">
      <input aria-label="field" />
    </Modal>,
  )
  return onClose
}

describe('Modal — keyboard isolation', () => {
  it('does not let a chord from inside reach a bubble-phase document or window listener', () => {
    const documentShortcut = vi.fn()
    const windowShortcut = vi.fn()
    document.addEventListener('keydown', documentShortcut)
    window.addEventListener('keydown', windowShortcut)
    try {
      renderModal()
      // A real Ctrl+3 (a session-jump chord that deliberately fires from
      // inside inputs), dispatched AT the field so it bubbles the way a
      // browser would deliver it.
      fireEvent.keyDown(screen.getByLabelText('field'), { key: '3', code: 'Digit3', ctrlKey: true })
      expect(documentShortcut).not.toHaveBeenCalled()
      expect(windowShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', documentShortcut)
      window.removeEventListener('keydown', windowShortcut)
    }
  })

  it('keeps the header X button inside the boundary', () => {
    // The X button is one Shift+Tab from any form field; a chord fired while
    // it holds focus must not leak either. It lives in the dialog header,
    // OUTSIDE any caller-rendered content, so only a boundary owned by Modal
    // itself can cover it — this is the assertion a call-site wrapper cannot
    // strengthen.
    const globalShortcut = vi.fn()
    document.addEventListener('keydown', globalShortcut)
    try {
      renderModal()
      fireEvent.keyDown(screen.getByLabelText('Close'), { key: ',', code: 'Comma', metaKey: true })
      expect(globalShortcut).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', globalShortcut)
    }
  })

  it('still dismisses on a plain Escape fired from inside the dialog', () => {
    // The half that makes the guard surgical rather than a dismissal bug:
    // Modal listens for Escape on window BUBBLE phase, and stopPropagation()
    // on the synthetic event also stops the native event from travelling on
    // to window — so a blanket stop would break every consumer's Escape. This
    // pins that Escape is excepted from the boundary.
    const onClose = renderModal()
    fireEvent.keyDown(screen.getByLabelText('field'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not dismiss on an Escape the IME owns (candidate-list cancel)', async () => {
    // A CJK user cancelling the IME candidate list mid-composition is not
    // cancelling the dialog: that Escape must never reach Modal's window
    // listener, or the part-composed draft is destroyed with the dialog.
    const onClose = renderModal()
    const field = screen.getByLabelText('field')
    fireEvent.change(field, { target: { value: '频道' } })
    fireEvent.compositionStart(field)
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    // After the composition ends (and its post-composition window passes), a
    // real Escape dismisses again. 60ms clears POST_COMPOSITION_MS (50ms in
    // useImeGuard.ts — not exported; the sibling ChannelPageCoverage test
    // carries the same margin).
    fireEvent.compositionEnd(field)
    await new Promise(r => setTimeout(r, 60))
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('still reaches a CAPTURE-phase document listener with a non-Escape key', () => {
    // The property that keeps Modal's own machinery alive: useDialogFocusTrap's
    // Tab trap listens on window CAPTURE, and useKeyboardShortcuts has two
    // capture-phase document listeners. Capture runs before the event descends
    // to the panel, so the boundary cannot (and must not) starve them. A future
    // "hardening" that moved the guard to a capture-phase or document-level
    // listener would pass every other test here while breaking the trap — this
    // is the rail against that.
    const capturing = vi.fn()
    document.addEventListener('keydown', capturing, { capture: true })
    try {
      renderModal()
      fireEvent.keyDown(screen.getByLabelText('field'), { key: '3', code: 'Digit3', ctrlKey: true })
      expect(capturing).toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', capturing, { capture: true })
    }
  })

  it('keeps the image Lightbox keyboard alive above a Modal', () => {
    // The Lightbox is an App-level singleton rendered OUTSIDE any modal, but a
    // README image inside a Modal body (SkillBrowserModal, McpBrowserModal,
    // SteeringTab) opens it while focus stays inside the dialog panel — the
    // panel is tabIndex={-1}, so even clicking the image focuses the panel,
    // not body. The viewer's keydown listener is window CAPTURE
    // (MarkdownRenderer's Lightbox effect, matching DiagramLightbox), which
    // runs before the panel boundary; a bubble-phase listener there would go
    // silently dead for every key but Escape.
    render(<Lightbox />)
    renderModal()
    act(() => {
      window.dispatchEvent(new CustomEvent('lightbox', {
        detail: { images: [{ src: 'a.png', alt: 'a' }, { src: 'b.png', alt: 'b' }], index: 0 },
      }))
    })
    expect(screen.getByAltText('a')).toBeInTheDocument()
    // ArrowRight dispatched AT the field inside the panel — the exact path the
    // boundary stops for bubble-phase listeners.
    act(() => {
      fireEvent.keyDown(screen.getByLabelText('field'), { key: 'ArrowRight' })
    })
    expect(screen.getByAltText('b')).toBeInTheDocument()
  })
})
