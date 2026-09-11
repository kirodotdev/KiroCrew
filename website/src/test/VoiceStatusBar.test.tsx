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
    fireEvent.click(screen.getByLabelText('Dismiss'))
    expect(onDismissError).toHaveBeenCalledTimes(1)
  })

  it('renders the notice action label as a button inside the sentence', () => {
    const onClick = vi.fn()
    render(
      <VoiceStatusBar recording={false} level={0} notice={{ text: 'Microphone in use in kirocrew', tone: 'muted', action: { label: 'kirocrew', onClick } }} />,
    )
    const notice = screen.getByTestId('voice-status-notice')
    expect(notice).toHaveTextContent('Microphone in use in kirocrew')
    fireEvent.click(screen.getByRole('button', { name: 'kirocrew' }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('keeps the quotation marks around the name inside the button, so a wrap cannot orphan them', () => {
    const onClick = vi.fn()
    render(
      <VoiceStatusBar recording={false} level={0} notice={{ text: 'Microphone in use in “kirocrew”', tone: 'muted', action: { label: 'kirocrew', onClick } }} />,
    )
    const button = screen.getByRole('button')
    // Word joiners (U+2060) glue the quotes to the name so a wrap cannot split them.
    expect(button.textContent).toBe('“\u2060kirocrew\u2060”')
    expect(screen.getByTestId('voice-status-notice').textContent?.replace(/\u2060/g, '')).toBe('Microphone in use in “kirocrew”')
    fireEvent.click(button)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('keeps the notice plain text when there is no action, or the label is not in the text', () => {
    const { rerender } = render(<VoiceStatusBar recording={false} level={0} notice={{ text: 'Dictation added to your message', tone: 'ok' }} />)
    expect(screen.queryByRole('button')).toBeNull()
    rerender(<VoiceStatusBar recording={false} level={0} notice={{ text: 'Microphone in use in another chat', tone: 'muted', action: { label: 'kirocrew', onClick: vi.fn() } }} />)
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByTestId('voice-status-notice')).toHaveTextContent('Microphone in use in another chat')
  })
})
