import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import VoiceDisabledModal from '../components/VoiceDisabledModal'
import { parseSpeechProviders, speechProviderNotice, type SpeechProviderEntry } from '../lib/speechProviders'

/**
 * An edition-registered provider that refuses to run is installed: its own
 * check() says why. The mic modal must show that row through the same
 * `SpeechProviderNotice` Settings → Voice uses, not the generic "isn't installed
 * on this machine" fallback.
 */
const REASON = 'Not signed in to Kiro. Sign in from Settings → Agent Harness → Kiro sign-in.'
const noop = () => {}

function entry(overrides: Partial<SpeechProviderEntry> = {}): SpeechProviderEntry {
  return { name: 'acme', label: 'Acme', builtin: false, available: false, reason: REASON, action: null, error: false, ...overrides }
}

describe('VoiceDisabledModal with an edition provider notice', () => {
  it('shows the provider reason as status text instead of "not installed"', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="acme" code="stt_provider_unavailable" providerNotice={entry()} onClose={noop} onOpenSettings={noop} />,
    )
    expect(screen.getByTestId('voice-modal-provider-notice')).toHaveTextContent(REASON)
    expect(screen.getByRole('status')).toBeInTheDocument()
    expect(screen.queryByText(/isn't installed on this machine/i)).not.toBeInTheDocument()
  })

  it('renders a FAILED provider check as an error notice, not status text', () => {
    render(
      <VoiceDisabledModal
        open
        reason="unavailable"
        provider="acme"
        code="stt_provider_unavailable"
        providerNotice={entry({ reason: 'the provider check failed', error: true })}
        onClose={noop}
        onOpenSettings={noop}
      />,
    )
    expect(screen.getByText('the provider check failed')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByText(/isn't installed on this machine/i)).not.toBeInTheDocument()
  })

  it('keeps the generic sentence when there is no provider notice', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="acme" code="stt_provider_unavailable" providerNotice={null} onClose={noop} onOpenSettings={noop} />,
    )
    expect(screen.getByText(/isn't installed on this machine/i)).toBeInTheDocument()
    expect(screen.queryByTestId('voice-modal-provider-notice')).not.toBeInTheDocument()
  })
})

describe('the composer picks the notice the way Settings does', () => {
  const rows = [
    { name: 'local', label: '', builtin: true, available: false, reason: 'x', action: null },
    { name: 'acme', label: 'Acme', builtin: false, available: false, reason: REASON, action: null },
  ]

  it('returns the selected edition row that cannot run', () => {
    expect(speechProviderNotice(parseSpeechProviders(rows), 'acme')?.reason).toBe(REASON)
  })

  it('returns nothing for a built-in, an unknown name or no served rows', () => {
    expect(speechProviderNotice(parseSpeechProviders(rows), 'local')).toBeNull()
    expect(speechProviderNotice(parseSpeechProviders(rows), 'other')).toBeNull()
    expect(speechProviderNotice(parseSpeechProviders(undefined), 'acme')).toBeNull()
  })
})
