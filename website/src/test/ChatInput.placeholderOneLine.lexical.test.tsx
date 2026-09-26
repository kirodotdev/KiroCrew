import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { awaitComposer, composerPlaceholder, renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * The Lexical composer's counterpart of `ChatInput.placeholderOneLine.test.tsx`
 * (#13812). Its placeholder is an overlay `<div>`, not a `::placeholder`, so the
 * one-line rule for the sigil hint lands on the overlay's own classes: nowrap plus
 * the faded-tail mask. Status sentences (gateway offline, stopping, recording)
 * and a caller's own placeholder keep wrapping, exactly as on the textarea.
 *
 * jsdom lays out no text, so the rendered proof is acceptance step 32 of
 * `website/scripts/capture-composer-pills.mjs` (a 420px viewport: one line rect,
 * `white-space: nowrap`, a `linear-gradient` mask). These tests defend the class
 * contract that step measures, and its scope.
 */
const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  lexicalComposer: true,
}

const ONE_LINE = ['whitespace-nowrap', '[mask-image:linear-gradient(', '[-webkit-mask-image:linear-gradient(']

function overlay(): HTMLElement {
  const el = document.querySelector<HTMLElement>('[data-composer-placeholder]')
  if (!el) throw new Error('no placeholder overlay')
  return el
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('Lexical composer placeholder stays on one line', () => {
  it('keeps the hint unwrapped, clipped and faded', async () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    await awaitComposer()
    await waitFor(() => expect(composerPlaceholder()).toMatch(/\/command/))
    for (const token of ONE_LINE) expect(overlay().className).toContain(token)
    expect(overlay().className).toContain('overflow-hidden')
  })

  it('lets a status placeholder wrap instead', async () => {
    renderWithProviders(<ChatInput {...defaultProps} connected={false} />)
    await awaitComposer()
    await waitFor(() => expect(composerPlaceholder()).toMatch(/will not send/i))
    for (const token of ONE_LINE) expect(overlay().className).not.toContain(token)
  })

  it('leaves a caller-supplied placeholder wrapping too', async () => {
    renderWithProviders(<ChatInput {...defaultProps} placeholder="Ask a side question about this selection" />)
    await awaitComposer()
    await waitFor(() => expect(composerPlaceholder()).toMatch(/Ask a side question/))
    for (const token of ONE_LINE) expect(overlay().className).not.toContain(token)
  })

  it('scopes the rule to the overlay, never to the editor', async () => {
    // A `nowrap` on the editable root would hide a draft the user is still typing.
    renderWithProviders(<ChatInput {...defaultProps} />)
    const root = await awaitComposer()
    await waitFor(() => expect(composerPlaceholder()).toMatch(/\/command/))
    const classes = root.className.split(/\s+/)
    expect(classes).not.toContain('whitespace-nowrap')
    expect(classes).toContain('whitespace-pre-wrap')
    expect(screen.getByRole('textbox')).toBe(root)
  })
})
