// The model chip draws the session's harness mark before the model only when the
// session runs on another harness than this install's. The host passes the
// selection-capabilities `models_backend`, which the server sets only then
// (`own_models_backend`), so a default chat looks as it always did.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { stubStripHeights } from './stripHeights'
import { defaultProps } from './ChatInputTestUtils'
import { namedCeiling } from './namedCeiling'

const LAZY_LOGO = namedCeiling('LAZY_LOGO', 5000)

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
})

function renderChip(harnessBackend: string | undefined) {
  renderWithProviders(
    <ChatInput {...defaultProps} modelName="opus" onModelClick={vi.fn()} harnessBackend={harnessBackend} />,
  )
  return screen.getByTestId('composer-model-chip')
}

describe('ChatInput model chip harness mark', () => {
  it('marks a Claude Code session, named in the tooltip', async () => {
    const chip = renderChip('claude')
    const mark = screen.getByTestId('composer-model-chip-harness')
    expect(mark).toHaveAttribute('title', 'Runs on Claude Code')
    // The logo is lazy-loaded: wait for the Claude mark itself, past the default
    // 1 s deadline a loaded coverage fork can exceed.
    await waitFor(() => expect(mark.querySelector('[data-testid="harness-logo-claude"]')).not.toBeNull(), LAZY_LOGO)
    expect(chip).toHaveAttribute('aria-label', expect.stringMatching(/^Runs on Claude Code · /))
    // No new text: the chip still reads just the model.
    expect(chip).toHaveTextContent(/^opus$/)
  })

  it('marks a Kiro session on another harness install with the Kiro name', () => {
    renderChip('kas')
    expect(screen.getByTestId('composer-model-chip-harness')).toHaveAttribute('title', 'Runs on Kiro Agent')
  })

  it('leaves a session on the install harness unchanged', () => {
    const chip = renderChip(undefined)
    expect(screen.queryByTestId('composer-model-chip-harness')).toBeNull()
    expect(chip.getAttribute('aria-label')).not.toMatch(/Runs on/)
    expect(chip).toHaveTextContent(/^opus$/)
  })
})
