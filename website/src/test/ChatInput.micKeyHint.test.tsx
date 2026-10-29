import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { ComposerVoiceSliceOverride } from '../chat-core/composer/Composer'
import { createAudioSample } from '../hooks/mic'
import { bindingAriaKeyshortcuts, type PttBinding } from '../lib/pushToTalk'

/**
 * The composer mic names the push-to-talk key it is given, in its label and
 * title and as `aria-keyshortcuts`, because the key does the same job and
 * nothing else in the composer says so. With no key (`voicePttBinding` null:
 * not this composer's key, the trigger off, or STT not ready; decided in
 * `useComposerVoice`), it says nothing about one.
 */

vi.mock('../components/Strands', () => ({
  __esModule: true,
  default: () => <div data-testid="strands-stub" />,
  strandsSupported: () => true,
}))

const voiceProps = {
  onVoiceToggle: vi.fn(),
  onVoiceCancel: vi.fn(),
  onVoicePrewarm: vi.fn(),
  onVoiceStart: vi.fn(),
  onVoiceStop: vi.fn(),
  voiceSampleRef: { current: createAudioSample() },
}

const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), connected: true }

/** A fine pointer, so the mic is the record button rather than a mode switch. */
function stubFinePointer() {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: false,
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

function renderComposer(voicePttBinding: PttBinding | null) {
  renderWithProviders(
    <ComposerVoiceSliceOverride inputProps={{ ...voiceProps, voicePttBinding }}>
      <ChatInput {...base} />
    </ComposerVoiceSliceOverride>,
  )
}

beforeEach(() => {
  stubFinePointer()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ChatInput — the mic names the push-to-talk key', () => {
  it('names a given key in the label, the title and aria-keyshortcuts', () => {
    // jsdom reports no Mac platform, so a chord is written "Alt + Shift + Space".
    renderComposer({ code: 'Space', alt: true, shift: true })
    const mic = screen.getByRole('button', { name: 'Voice input (Alt + Shift + Space)' })
    expect(mic).toHaveAttribute('title', 'Voice input (Alt + Shift + Space)')
    expect(mic).toHaveAttribute('aria-keyshortcuts', 'Alt+Shift+Space')
  })

  it('says nothing about a key when given none', () => {
    renderComposer(null)
    const mic = screen.getByRole('button', { name: 'Voice input' })
    expect(mic).toHaveAttribute('title', 'Voice input')
    expect(mic).not.toHaveAttribute('aria-keyshortcuts')
  })
})

describe('bindingAriaKeyshortcuts', () => {
  it('writes a bare modifier as its family and a chord modifiers-first', () => {
    expect(bindingAriaKeyshortcuts({ code: 'AltRight' })).toBe('Alt')
    expect(bindingAriaKeyshortcuts({ code: 'MetaLeft' })).toBe('Meta')
    expect(bindingAriaKeyshortcuts({ code: 'Space', alt: true, shift: true })).toBe('Alt+Shift+Space')
    expect(bindingAriaKeyshortcuts({ code: 'Digit1', ctrl: true })).toBe('Control+1')
  })
})
