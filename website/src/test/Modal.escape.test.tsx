import { useState } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import Modal from '../components/Modal'

describe('Modal — Escape handling', () => {
  it('closes on a plain Escape', () => {
    const onClose = vi.fn()
    render(<Modal open={true} onClose={onClose} title="T"><div /></Modal>)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('ignores an Escape a nested layer already consumed', () => {
    // Regression: overlays that portal ABOVE the modal (ProjectPicker at
    // z-[9999]) handle Escape themselves and call preventDefault. The modal
    // listens on `window`, so without a defaultPrevented check the SAME keydown
    // bubbled through and tore down the modal underneath — destroying whatever
    // draft the user was part-way through entering.
    const onClose = vi.fn()
    render(<Modal open={true} onClose={onClose} title="T"><div /></Modal>)
    const consumed = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true })
    consumed.preventDefault()
    window.dispatchEvent(consumed)
    expect(onClose).not.toHaveBeenCalled()
  })

  it('an Escape pressed the moment the dialog appears still closes it', async () => {
    // Regression: the Escape listener was attached in a passive effect. An
    // open raised outside an event handler (a fetch answering, as the
    // unscanned-download confirm does) commits on a default-priority render,
    // and when that render runs past the scheduler's 5 ms slice React yields
    // to the host before flushing passive effects. The dialog was then in the
    // DOM with no Escape listener, so the user's first Escape was dropped.
    // A render that takes longer than the slice forces that yield here.
    function SlowBody() {
      const end = performance.now() + 20
      while (performance.now() < end) { /* outlast the scheduler slice */ }
      return null
    }
    const onClose = vi.fn()
    let show: () => void = () => {}
    function Host() {
      const [open, setOpen] = useState(false)
      show = () => setOpen(true)
      return <Modal open={open} onClose={onClose} title="T"><SlowBody /></Modal>
    }
    render(<Host />)

    const g = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
    const prior = g.IS_REACT_ACT_ENVIRONMENT
    // Let React schedule the open as it does in the app, not flush it in act().
    g.IS_REACT_ACT_ENVIRONMENT = false
    try {
      // Press Escape at the first moment anything can see the dialog: the
      // DOM mutation that inserts it, which is what findByRole waits on.
      const pressed = new Promise<void>(resolve => {
        const watch = new MutationObserver(() => {
          const dialog = document.querySelector('[role="dialog"]')
          if (!dialog) return
          watch.disconnect()
          fireEvent.keyDown(dialog, { key: 'Escape' })
          resolve()
        })
        watch.observe(document.body, { childList: true, subtree: true })
      })
      show()
      await pressed
    } finally {
      g.IS_REACT_ACT_ENVIRONMENT = prior
    }
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('still closes via the X button', () => {
    const onClose = vi.fn()
    render(<Modal open={true} onClose={onClose} title="T"><div /></Modal>)
    fireEvent.click(screen.getByLabelText('Close'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('guardAccidentalDismiss suppresses Escape but not the X button', () => {
    const onClose = vi.fn()
    render(<Modal open={true} onClose={onClose} title="T" guardAccidentalDismiss><div /></Modal>)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
    // The explicit path must still work, or the modal becomes a trap.
    fireEvent.click(screen.getByLabelText('Close'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('dismissDisabled refuses Escape and the X button, and renders the X disabled', () => {
    // A write the modal owns is in flight: dismissing would unmount the
    // surface a late failure has to render in, so every path is refused and
    // the X shows it.
    const onClose = vi.fn()
    const { rerender } = render(<Modal open={true} onClose={onClose} title="T" dismissDisabled><div /></Modal>)
    expect(screen.getByLabelText('Close')).toBeDisabled()
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(screen.getByLabelText('Close'))
    expect(onClose).not.toHaveBeenCalled()
    // Once the write settles the modal is dismissable again.
    rerender(<Modal open={true} onClose={onClose} title="T"><div /></Modal>)
    expect(screen.getByLabelText('Close')).toBeEnabled()
    fireEvent.click(screen.getByLabelText('Close'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
