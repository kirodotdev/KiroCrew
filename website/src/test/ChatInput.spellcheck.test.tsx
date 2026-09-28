/**
 * The composer honours the spellcheck preference (issue #10776).
 *
 * The "Spell Check Message Input" Settings toggle sets a persisted preference.
 * `ChatInput` reads that preference INTERNALLY (via `useComposerSpellcheck`) and
 * puts it on the composer input's `spellCheck` attribute, so every render site
 * of `ChatInput` -- main chat, the side panel, and each split-view grid pane
 * (`ChatPane`) -- honours it with no per-call-site wiring. These assertions pin
 * that invariant: with NO prop passed, the attribute follows the stored
 * preference. A call site that forgets a prop therefore cannot regress it.
 *
 * Native spellcheck squiggles are drawn by the browser's own spellcheck engine
 * and do NOT render in a headless Chromium (no dictionaries ship there), so a
 * screenshot cannot show the effect. This DOM assertion is the honest evidence
 * in its place: the attribute the browser reads to decide whether to underline
 * is present and follows the preference.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { screen } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

const CONFIG_KEY = 'mc-chat-config'

afterEach(() => {
  localStorage.removeItem(CONFIG_KEY)
})

describe('composer spellcheck preference (read internally, no prop)', () => {
  it('defaults to spellcheck on with no stored preference', () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const textarea = screen.getByLabelText('Message input')
    // React renders the true preference as the DOM attribute spellcheck="true".
    expect(textarea).toHaveAttribute('spellcheck', 'true')
  })

  it('sets spellcheck="false" when the stored preference is off -- with no prop passed', () => {
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ spellcheck: false }))
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} />)
    const textarea = screen.getByLabelText('Message input')
    expect(textarea).toHaveAttribute('spellcheck', 'false')
  })
})
