import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { INPUT_DEFAULT_MAX_H, INPUT_PREFILL_MAX_H } from '../components/chat-input/sizing'
import { renderWithProviders } from './helpers'
import { LAZY_COMPOSER_MOUNT } from './lazyComposerReady'

// #35680: each Quote adds a block to the draft, and the Lexical editor grew to
// half the window because it was capped at 50vh while the textarea path stops
// at its autosize cap. It must stop at the same cap and scroll inside itself.
describe('ChatInput Lexical height cap', () => {
  it('caps the editor at the textarea autosize height when no height is pinned', async () => {
    localStorage.removeItem('mc-input-height')
    renderWithProviders(<ChatInput value={'> quoted\n\nnote\n'.repeat(4)} onChange={vi.fn()} onSend={vi.fn()} lexicalComposer />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    expect(input.style.maxHeight).toBe(`min(${INPUT_DEFAULT_MAX_H}px, 50vh)`)
    expect(input.className).toContain('overflow-y-auto')
  })

  it('uses the taller prefill cap while a seeded prompt is being read', async () => {
    localStorage.removeItem('mc-input-height')
    renderWithProviders(<ChatInput value={'line\n'.repeat(20)} onChange={vi.fn()} onSend={vi.fn()} lexicalComposer prefillHint />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input.style.maxHeight).toBe(`min(${INPUT_PREFILL_MAX_H}px, 50vh)`)
  })

  it('keeps the drag ceiling when the user pinned a height', async () => {
    localStorage.setItem('mc-input-height', '300')
    try {
      renderWithProviders(<ChatInput value="hi" onChange={vi.fn()} onSend={vi.fn()} lexicalComposer />)
      const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
      expect(input.style.maxHeight).toBe('')
      expect(input.className).toContain('max-h-[50vh]')
    } finally {
      localStorage.removeItem('mc-input-height')
    }
  })
})
