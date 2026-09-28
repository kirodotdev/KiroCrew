/**
 * The composer does not claim transcription while the speech model is still working.
 *
 * Capture ends at the key release, but a cold model may still be fetching or
 * loading, and `VoiceStatusBar` sits directly above the input saying which stage
 * it is in. The composer's own placeholder is driven by the transcribe flag, which
 * is true for that whole wait, so without a guard the two lines disagree about the
 * same wait: the strip reads "Downloading the speech model: 40%" while the input
 * underneath reads "Transcribing, please wait…". A reader cannot tell which is
 * true, and the honest answer is the strip's -- it names the stage, and nothing is
 * being transcribed yet.
 *
 * So the strip owns the explanation for that wait and the placeholder falls
 * through to its default. Once the model is ready, `download` clears and the
 * placeholder speaks again.
 *
 * Dictation state reaches `ChatInput` from the Composer root's voice atom, not
 * from props, so the slice is what this drives.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'

const voiceInput: { current: Record<string, unknown> } = { current: {} }

vi.mock('../chat-core/composer/Composer', async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    '../chat-core/composer/Composer',
  )
  // The component destructures `composerVoice?.inputProps`, so the slice's shape
  // is what this has to answer with -- a flat object silently reads as no mic.
  return { ...actual, useComposerVoiceSlice: () => ({ inputProps: voiceInput.current }) }
})

const ChatInput = (await import('../components/ChatInput')).default
const { renderWithProviders } = await import('./helpers')

const TRANSCRIBING = 'Transcribing, please wait\u2026'

function placeholderOf(): string | null {
  return screen.getByLabelText('Message input').getAttribute('placeholder')
}

describe('composer placeholder during a speech-model wait', () => {
  beforeEach(() => {
    voiceInput.current = {}
  })

  it('says transcribing when a transcript really is in flight', () => {
    voiceInput.current = { voiceTranscribing: true }
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    expect(placeholderOf()).toBe(TRANSCRIBING)
  })

  it('stops saying transcribing while the model is still loading', () => {
    voiceInput.current = {
      voiceTranscribing: true,
      voiceDownload: { done: 0, total: 0, stage: 'preparing' },
    }
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const shown = placeholderOf()
    expect(shown).not.toBe(TRANSCRIBING)
    // Still a real placeholder, not blank: the input keeps its default prompt.
    expect(shown).toBeTruthy()
  })

  it('stops saying transcribing while the weights are still downloading', () => {
    voiceInput.current = {
      voiceTranscribing: true,
      voiceDownload: { done: 5_000_000, total: 10_000_000, stage: 'downloading' },
    }
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    expect(placeholderOf()).not.toBe(TRANSCRIBING)
  })
})
