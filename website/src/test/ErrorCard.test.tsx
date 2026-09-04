import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { ErrorCard } from '../pages/chat/ErrorCard'

/**
 * The error row used to be an actionless div whose own copy told the reader to
 * retry. These tests pin the two shapes: settled (no action) and resumable
 * (Continue), plus the guard that a press cannot double-fire.
 */
describe('ErrorCard', () => {
  it('renders the prose verbatim with no action when the turn is not resumable', () => {
    render(<ErrorCard content="⟳ Connection lost — please retry." />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('⟳ Connection lost — please retry.')
    // Deliberately ABSENT rather than disabled: a permanently greyed button on a
    // red card reads as a broken feature.
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
  })

  it('renders a Continue action when the turn is resumable', () => {
    render(<ErrorCard content="boom" onContinue={() => {}} />)
    expect(screen.getByTestId('error-card-continue')).toBeTruthy()
    expect(screen.getByTestId('error-card')).toHaveAttribute('data-continuable', 'true')
  })

  it('invokes onContinue on press', () => {
    const onContinue = vi.fn()
    render(<ErrorCard content="boom" onContinue={onContinue} />)
    fireEvent.click(screen.getByTestId('error-card-continue'))
    expect(onContinue).toHaveBeenCalledTimes(1)
  })

  it('disables the action while a continue is in flight', () => {
    const onContinue = vi.fn()
    render(<ErrorCard content="boom" onContinue={onContinue} continuing />)
    const btn = screen.getByTestId('error-card-continue') as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    fireEvent.click(btn)
    expect(onContinue).not.toHaveBeenCalled()
  })

  it('keeps the error prose visible in the resumable shape', () => {
    render(<ErrorCard content="⟳ Session busy — please retry." onContinue={() => {}} />)
    expect(screen.getByTestId('error-card')).toHaveTextContent('⟳ Session busy — please retry.')
  })
})
