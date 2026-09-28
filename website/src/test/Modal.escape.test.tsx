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
