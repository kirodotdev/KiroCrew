import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { CrewModeChip } from './CrewModeChip'
import CrewModeWelcome from './CrewModeWelcome'

describe('CrewModeChip', () => {
  it('reads off by default and reports its state through aria-pressed', () => {
    renderWithProviders(<CrewModeChip on={false} onToggle={vi.fn()} />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toHaveAttribute('aria-pressed', 'false')
    expect(chip).not.toHaveTextContent('Crew Mode on')
    // Only one off label is displayed: the short name below `sm`, the
    // explained one from `sm` up.
    expect(within(chip).getByText('Crew Mode')).toHaveClass('sm:hidden')
    expect(within(chip).getByText('Crew Mode: split into tasks')).toHaveClass('hidden', 'sm:inline')
  })

  it('says what turning it off does in its hint', () => {
    renderWithProviders(<CrewModeChip on onToggle={vi.fn()} />)
    expect(screen.getByTestId('crew-mode-chip')).toHaveAttribute(
      'title',
      'Turn off Crew Mode. This chat becomes a normal chat again; chats it already opened keep running.',
    )
  })

  it('says it is on when on', () => {
    renderWithProviders(<CrewModeChip on onToggle={vi.fn()} />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    expect(chip).toHaveTextContent('Crew Mode on')
    // A visible "off" mark beside the label.
    expect(chip.querySelector('svg.lucide-x')).not.toBeNull()
  })

  it('calls onToggle on click', () => {
    const onToggle = vi.fn()
    renderWithProviders(<CrewModeChip on={false} onToggle={onToggle} />)
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it('cannot be flipped while a reply is running', () => {
    const onToggle = vi.fn()
    renderWithProviders(<CrewModeChip on onToggle={onToggle} disabled />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toBeDisabled()
    fireEvent.click(chip)
    expect(onToggle).not.toHaveBeenCalled()
  })

  it('keeps its on look while a reply runs, with only the cursor and hint changed', () => {
    const onToggle = vi.fn()
    renderWithProviders(<CrewModeChip on onToggle={onToggle} busy />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toBeDisabled()
    expect(chip).toHaveAttribute('aria-pressed', 'true')
    expect(chip.className).not.toContain('opacity-')
    expect(chip).toHaveClass('disabled:cursor-not-allowed')
    expect(chip).toHaveAttribute('title', 'Available after the reply.')
    fireEvent.click(chip)
    expect(onToggle).not.toHaveBeenCalled()
  })

  it('fades when disabled for any other reason', () => {
    renderWithProviders(<CrewModeChip on onToggle={vi.fn()} disabled />)
    expect(screen.getByTestId('crew-mode-chip')).toHaveClass('disabled:opacity-60')
  })
})

describe('CrewModeWelcome', () => {
  it('says what it is in big type and what it does in four short labels', () => {
    renderWithProviders(<CrewModeWelcome />)
    const page = screen.getByTestId('crew-mode-welcome')
    expect(within(page).getByRole('heading', { name: 'Crew Mode' })).toBeInTheDocument()
    expect(page).toHaveTextContent('You set the goal.')
    expect(page).toHaveTextContent('Your crew does the work.')
    for (const label of ['Opens chats', 'Shares updates', 'Takes requests', 'Shows progress']) {
      expect(within(page).getByText(label)).toBeInTheDocument()
    }
  })

  it('marks its task board as a preview, with the question that needs the person first', () => {
    renderWithProviders(<CrewModeWelcome />)
    const board = screen.getByRole('region', { name: 'Your crew' })
    expect(within(board).getByText('Example')).toBeInTheDocument()
    // Sample content only: nothing a reader would try to click, and no answers line.
    expect(within(board).queryAllByRole('button')).toHaveLength(0)
    expect(board).not.toHaveTextContent('Today')
    expect(board).not.toHaveTextContent('Tomorrow')
    // Only the ghost circles are muted; the text renders at full contrast.
    expect(board.className).not.toContain('opacity-')
    // The question's icon tile is muted like the ghost circles; its text is not.
    const questionIcon = within(board).getByTestId('crew-mode-sample-question-icon')
    expect(questionIcon).toHaveClass('opacity-70')
    // The sample question is neutral: no warn tone on its row or its tile.
    expect(within(board).getByTestId('crew-mode-sample-question').className).not.toContain('warn')
    expect(questionIcon.className).not.toContain('warn')
    expect(within(board).getByText('Which day works for you?').className).not.toContain('opacity-')
    const text = board.textContent ?? ''
    expect(text.indexOf('Which day works for you?')).toBeLessThan(text.indexOf('Email'))
    for (const state of ['Done', 'Working', 'Needs you', 'Waiting']) {
      expect(within(board).getByText(state)).toBeInTheDocument()
    }
    // State words are muted text with no tone class; the tone sits on the ghost circle.
    for (const state of ['Done', 'Working', 'Needs you', 'Waiting']) {
      const word = within(board).getByText(state)
      expect(word).toHaveClass('text-muted')
      expect(word.className).not.toMatch(/text-(ok|info|warn|aim|accent)/)
    }
  })
})
