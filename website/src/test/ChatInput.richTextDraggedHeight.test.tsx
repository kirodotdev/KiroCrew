import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'
import { LAZY_COMPOSER_MOUNT } from './lazyComposerReady'

afterEach(() => {
  localStorage.removeItem('mc-input-height')
})

/* Under a height the user dragged, the rich-text editor (Style Markdown While
   Typing) fills the box and scrolls inside it, as the textarea does. jsdom has
   no layout, so this pins the two rules that make it so: the host gives the
   editor's box a height, and the editor takes that height. */
describe('ChatInput rich-text editor under a dragged height', () => {
  it('fills the box instead of being as tall as its draft', async () => {
    localStorage.setItem('mc-input-height', '200')
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} lexicalComposer />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    expect(screen.getByTestId('input-wrapper').style.height).toBe('200px')
    const box = input.parentElement!
    expect(box).toHaveClass('flex-1', 'min-h-0')
    expect(box).toHaveClass('[&>[data-lexical-composer]]:h-full')
    expect(input).toHaveClass('overflow-y-auto')
  })
})
