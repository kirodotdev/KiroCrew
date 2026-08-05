import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * Sixth state of the composer's primary button. The first five are send / stop /
 * queue / steer / disabled; this one claims the one state that was previously
 * dead weight — an empty composer on an idle slot that already holds a
 * conversation.
 *
 * Two invariants these tests defend: the control never carries two meanings at
 * once (empty = Continue, anything typed = Send), and its copy never asserts an
 * interruption the transcript did not show (`continueIsRecovery`).
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
    expect(screen.getByPlaceholderText(/carry on/i)).toBeInTheDocument()
  })

  it('names the interruption only when the transcript actually showed one', () => {
    // The copy split: Continue is offered on any idle slot, so the default
    // wording must not assert a breakage. A force-quit leaves no evidence, and
    // claiming "interrupted" there would be a guess dressed as a fact.
    renderWithProviders(
      <ChatInput {...defaultProps} continuable continueIsRecovery onContinue={vi.fn()} />,
    )
    expect(screen.getByPlaceholderText(/interrupted/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Continue the interrupted turn')).toBeInTheDocument()
  })

  it('uses neutral wording when nothing proves an interruption', () => {
    renderWithProviders(<ChatInput {...defaultProps} continuable onContinue={vi.fn()} />)
    expect(screen.queryByPlaceholderText(/interrupted/i)).toBeNull()
    expect(screen.getByLabelText('Carry on from here')).toBeInTheDocument()
  })

  it('keeps the ordinary placeholder when the turn is not resumable', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(screen.queryByPlaceholderText(/interrupted/i)).toBeNull()
    expect(screen.queryByPlaceholderText(/carry on/i)).toBeNull()
  })

  it('does not offer Continue without a handler, even when flagged resumable', () => {
    // Guards against a caller wiring the flag but not the action.
    renderWithProviders(<ChatInput {...defaultProps} continuable />)
    expect(screen.queryByTestId('composer-continue')).toBeNull()
    expect(screen.getByLabelText('Send')).toBeInTheDocument()
  })
})
