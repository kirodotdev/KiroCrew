// A provider-prefixed model id (`<provider>/<model>`) names the model only at its
// end, so the chip's truncating span puts its ellipsis at the front: the span runs
// right to left and the id inside it stays a left-to-right run.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'
import { defaultProps } from './ChatInputTestUtils'

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
})

describe('ChatInput model chip name', () => {
  it('truncates a provider-prefixed id from the front', () => {
    const id = 'amazon-bedrock/global.anthropic.claude-sonnet-5'
    renderWithProviders(<ChatInput {...defaultProps} modelName={id} onModelClick={vi.fn()} />)
    const chip = screen.getByTestId('composer-model-chip')
    const name = chip.querySelector('bdi')
    expect(name).not.toBeNull()
    expect(name).toHaveAttribute('dir', 'ltr')
    expect(name).toHaveTextContent(id)
    const span = name!.parentElement!
    expect(span).toHaveClass('truncate', '[direction:rtl]', 'text-left')
    // The full id stays one hover away and in the accessible name.
    expect(chip.getAttribute('title')).toContain(id)
    expect(chip.getAttribute('aria-label')).toContain(id)
  })
})
