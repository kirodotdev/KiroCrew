import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import VoiceStatusBar from '../components/VoiceStatusBar'

describe('VoiceStatusBar', () => {
  it('renders nothing when idle and error-free', () => {
    const { container } = render(<VoiceStatusBar recording={false} level={0} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('keeps explaining the model load after capture ends', () => {
    // The mic is released the moment the key comes up, but the retained audio is
    // still waiting on the model. An empty strip here is a composer that looks
    // broken for as long as the load takes.
    render(<VoiceStatusBar recording={false} level={0} download={{ done: 0, total: 0, stage: 'preparing' }} />)
    expect(screen.getByTestId('voice-status-download').textContent).toContain('Loading the speech model…')
    expect(screen.queryByText('Recording')).toBeNull()
  })

  it('promises the released utterance is kept, only after capture ends', () => {
    // Without this the wait is indistinguishable from having lost the sentence,
    // and the sensible response to that is to give up and retype it.
    const { unmount } = render(
      <VoiceStatusBar recording={false} level={0} download={{ done: 0, total: 0, stage: 'preparing' }} />,
    )
    expect(screen.getByTestId('voice-status-download').textContent)
      .toContain('Your dictation is kept and will be transcribed.')
    unmount()

    // Not while the mic is live: nothing looks lost yet, and the recording row
    // has to keep the controls that end the recording on it.
    render(<VoiceStatusBar recording level={0.5} download={{ done: 0, total: 0, stage: 'preparing' }} />)
    expect(screen.queryByText(/Your dictation is kept/)).toBeNull()
  })

  it('keeps explaining the weight fetch after capture ends', () => {
    render(<VoiceStatusBar recording={false} level={0} download={{ done: 5000000, total: 10000000, stage: 'downloading' }} />)
    expect(screen.getByTestId('voice-status-download').textContent).toContain('Downloading the speech model')
  })

  it('prefers the model line over an idle notice while the model is still working', () => {
    // The load is why nothing has arrived yet; the idle notice is about a mic
    // the user is not waiting on.
    render(
      <VoiceStatusBar
        recording={false}
        level={0}
        download={{ done: 0, total: 0, stage: 'preparing' }}
        notice={{ text: 'Dictation added to your message', tone: 'ok' }}
      />,
    )
    expect(screen.getByTestId('voice-status-download').textContent).toContain('Loading the speech model…')
    expect(screen.queryByText('Dictation added to your message')).toBeNull()
  })

  it('prefers a mic error over the model line', () => {
    render(
      <VoiceStatusBar
        recording={false}
        level={0}
        download={{ done: 0, total: 0, stage: 'preparing' }}
        error="Microphone permission denied."
      />,
    )
    expect(screen.getByText('Microphone permission denied.')).toBeTruthy()
    expect(screen.queryByTestId('voice-status-download')).toBeNull()
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
