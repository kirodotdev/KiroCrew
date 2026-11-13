/**
 * ErrorNotice `handoffHint`.
 *
 * A notice whose "Ask the agent" is withheld only for now shows one visible line
 * saying what gets it back, in place of the button. The line never shows beside
 * a live hand-off, and a notice without the prop renders exactly as before.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'

const ASK = /ask the agent/i
const HINT = 'Apply or discard your edits to ask the agent.'

describe('ErrorNotice handoffHint', () => {
  it.each(['inline', 'block'] as const)('%s: shows the hint inside the alert while the hand-off is off', (variant) => {
    render(<ErrorNotice variant={variant} message="Read failed" testId="n" askAgent={false} handoffHint={HINT} />)
    const notice = screen.getByTestId('n')
    expect(notice).toHaveAttribute('role', 'alert')
    expect(notice).toHaveTextContent(HINT)
    expect(screen.queryByRole('button', { name: ASK })).toBeNull()
  })

  it.each(['inline', 'block'] as const)('%s: drops the hint when the hand-off is on', (variant) => {
    render(<ErrorNotice variant={variant} message="Read failed" testId="n" askAgent handoffHint={HINT} />)
    expect(screen.getByRole('button', { name: ASK })).toBeTruthy()
    expect(screen.getByTestId('n')).not.toHaveTextContent(HINT)
  })

  it.each(['inline', 'block'] as const)('%s: renders no hint line without the prop', (variant) => {
    render(<ErrorNotice variant={variant} message="Read failed" testId="n" askAgent={false} />)
    expect(screen.getByTestId('n').textContent).toBe('Read failed')
  })

  it('renders nothing at all without a message, hint or not', () => {
    const { container } = render(<ErrorNotice message={null} handoffHint={HINT} />)
    expect(container.firstChild).toBeNull()
  })
})
