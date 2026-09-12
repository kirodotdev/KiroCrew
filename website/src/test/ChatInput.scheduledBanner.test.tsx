import { describe, it, expect, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { stubStripHeights } from './stripHeights'
import ChatInput from '../components/ChatInput'

/**
 * The banner is what makes a scheduled message findable again: the plus-menu row
 * that creates one is not a persistent affordance, so without a strip above the
 * composer a pending message is invisible until it fires.
 */
const base = {
  value: '',
  onChange: () => {},
  onSend: () => {},
  connected: true,
}

const future = Math.floor(Date.now() / 1000) + 86400

beforeEach(() => {
  // jsdom does no layout and the composer MEASURES its strips, so an unstubbed box
  // measures 0 and a rendered strip reads as no strip at all (same reason
  // ChatInput.test.tsx stubs these).
  stubStripHeights()
})

describe('ChatInput scheduled-message banner', () => {
  it('shows a pending scheduled message with a link to the Schedule page', () => {
    renderWithProviders(<ChatInput {...base} scheduledMessages={[{ id: 'j1', at_ts: future, name: 'remind me' }]} />)
    expect(screen.getByTestId('scheduled-message-banner')).toBeTruthy()
    expect(screen.getByTestId('scheduled-message-banner-link').getAttribute('href')).toBe('/schedule')
  })

  it('renders nothing when the host supplies none', () => {
    renderWithProviders(<ChatInput {...base} />)
    expect(screen.queryByTestId('scheduled-message-banner')).toBeNull()
  })

  it('drops a job whose fire time has already passed', () => {
    // A fired one-shot is deleted server-side, but the host's read can be up to its
    // staleTime old — so the banner must not keep advertising a run that is gone.
    renderWithProviders(<ChatInput {...base} scheduledMessages={[{ id: 'j1', at_ts: future - 172800, name: 'old' }]} />)
    expect(screen.queryByTestId('scheduled-message-banner')).toBeNull()
  })

  it('names the soonest fire time when several are pending', () => {
    renderWithProviders(
      <ChatInput
        {...base}
        scheduledMessages={[
          { id: 'late', at_ts: future + 3600, name: 'later' },
          { id: 'soon', at_ts: future, name: 'sooner' },
        ]}
      />,
    )
    expect(screen.getByTestId('scheduled-message-banner').textContent).toContain(
      new Date(future * 1000).toLocaleString(),
    )
  })
})
