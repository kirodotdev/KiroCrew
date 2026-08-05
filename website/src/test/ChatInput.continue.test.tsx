import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * Sixth state of the composer's primary button. The first five are send / stop /
 * queue / steer / disabled; this one claims the one state that was previously
 * dead weight — an empty composer on a slot whose last turn was cut short.
 *
 * The invariant these tests defend: the control never carries two meanings at
 * once. Empty + resumable = Continue; anything typed = Send.
 */
const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('ChatInput continue affordance', () => {
  it('shows the normal send button when the turn is not resumable', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeInTheDocument()
  })

  it('replaces send with Continue when the composer is empty and the turn is resumable', () => {
    renderWithProviders(<ChatInput {...defaultProps} continuable onContinue={vi.fn()} />)
    expect(screen.getByTestId('composer-continue')).toBeInTheDocument()
    // Exactly one meaning at a time — the send affordance is gone, not stacked.
    expect(screen.queryByLabelText('Send')).toBeNull()
  })

  it('reverts to send as soon as the user types', () => {
    renderWithProviders(<ChatInput {...defaultProps} value="a new message" continuable onContinue={vi.fn()} />)
    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeInTheDocument()
  })

  it('reverts to send when files are attached even with an empty text box', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} pendingFiles={['/tmp/uploads/a.png']} continuable onContinue={vi.fn()} />,
    )
    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeInTheDocument()
  })

  it('invokes onContinue on press', () => {
    const onContinue = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} continuable onContinue={onContinue} />)
    fireEvent.click(screen.getByTestId('composer-continue'))
    expect(onContinue).toHaveBeenCalledTimes(1)
  })

  it('disables the button while a continue is in flight', () => {
    const onContinue = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} continuable onContinue={onContinue} continuing />)
    const btn = screen.getByTestId('composer-continue') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('explains the state in the placeholder, since an icon swap announces nothing', () => {
    renderWithProviders(<ChatInput {...defaultProps} continuable onContinue={vi.fn()} />)
    expect(screen.getByPlaceholderText(/interrupted/i)).toBeInTheDocument()
  })

  it('keeps the ordinary placeholder when the turn is not resumable', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(screen.queryByPlaceholderText(/interrupted/i)).toBeNull()
  })

  it('does not offer Continue without a handler, even when flagged resumable', () => {
    // Guards against a caller wiring the flag but not the action.
    renderWithProviders(<ChatInput {...defaultProps} continuable />)
    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeInTheDocument()
  })
})
