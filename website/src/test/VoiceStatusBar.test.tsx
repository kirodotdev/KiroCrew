import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import VoiceStatusBar from '../components/VoiceStatusBar'

describe('VoiceStatusBar', () => {
  it('renders nothing when idle and error-free', () => {
    const { container } = render(<VoiceStatusBar recording={false} level={0} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows the recording indicator with the active mic name while recording', () => {
    render(<VoiceStatusBar recording level={0.5} deviceLabel="MacBook Pro Microphone" />)
    expect(screen.getByText('Recording')).toBeTruthy()
    expect(screen.getByText('MacBook Pro Microphone')).toBeTruthy()
  })

  it('falls back to "Default microphone" when no device label is known', () => {
    render(<VoiceStatusBar recording level={0.2} />)
    expect(screen.getByText('Default microphone')).toBeTruthy()
  })

  it('shows a dismissible error and prefers it over the recording indicator', () => {
    const onDismissError = vi.fn()
    render(
      <VoiceStatusBar recording level={0.5} error="Microphone permission denied." onDismissError={onDismissError} />,
    )
    expect(screen.getByText('Microphone permission denied.')).toBeTruthy()
    expect(screen.queryByText('Recording')).toBeNull()
    fireEvent.click(screen.getByLabelText('Dismiss microphone error'))
    expect(onDismissError).toHaveBeenCalledTimes(1)
  })
})
