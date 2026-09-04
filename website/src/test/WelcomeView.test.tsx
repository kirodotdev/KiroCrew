import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import WelcomeView from '../components/WelcomeView'

const defaultProps = {
  setInput: vi.fn(),
}

describe('WelcomeView', () => {
  it('renders welcome heading by default', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.getByText('What can I do for you?')).toBeInTheDocument()
  })

  it('renders Autopilot heading in orchestrator mode', () => {
    renderWithProviders(<WelcomeView {...defaultProps} mode="orchestrator" />)
    expect(screen.getByText('Autopilot')).toBeInTheDocument()
  })

  it('shows the orchestrator try button only in orchestrator mode', () => {
    const { rerender } = renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.queryByText(/Try:/)).not.toBeInTheDocument()
    rerender(<WelcomeView {...defaultProps} mode="orchestrator" />)
    expect(screen.getByText(/Try:/)).toBeInTheDocument()
  })

  it('shows ephemeral mode toggle when onSwitchMode is provided', () => {
    renderWithProviders(<WelcomeView {...defaultProps} onSwitchMode={vi.fn()} />)
    expect(screen.getByText('Switch to ephemeral mode')).toBeInTheDocument()
  })

  it('shows revert toggle in incognito mode', () => {
    renderWithProviders(<WelcomeView {...defaultProps} onSwitchMode={vi.fn()} memoryMode="incognito" />)
    expect(screen.getByText('Switch back to default mode')).toBeInTheDocument()
  })

  describe('suggestion pills', () => {
    // Falls back to FALLBACK_SUGGESTIONS when api.suggestions is unmocked.
    const FALLBACK_PILL = 'Check my pipeline status'

    it('pill is type=button and prevents mousedown default so the textarea keeps focus', () => {
      renderWithProviders(<WelcomeView {...defaultProps} />)
      const pill = screen.getByRole('button', { name: FALLBACK_PILL })
      expect(pill).toHaveAttribute('type', 'button')
      // fireEvent returns false when the (cancelable) event had preventDefault
      // called — i.e. focus stays in the textarea instead of moving to the pill,
      // so a follow-up Enter sends instead of re-activating this button.
      const notCancelled = fireEvent.mouseDown(pill)
      expect(notCancelled).toBe(false)
    })

    it('clicking a pill sets the input to the suggestion text', () => {
      const setInput = vi.fn()
      renderWithProviders(<WelcomeView setInput={setInput} />)
      fireEvent.click(screen.getByRole('button', { name: FALLBACK_PILL }))
      expect(setInput).toHaveBeenCalledWith(FALLBACK_PILL)
    })
  })
})
