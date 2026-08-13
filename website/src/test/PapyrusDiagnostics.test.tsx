import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import DiagnosticsList from '../apps/papyrus/DiagnosticsList'
import type { Diagnostic } from '../apps/papyrus/api'

// The whole point of parsing the compiler log server-side is that a row is
// CLICKABLE and moves the editor's cursor to the offending line. That is the
// behaviour pinned here, along with the two cases where it must NOT be offered:
// a message with no line number, and the typesetting hints that are collapsed by
// default because a paper near its page limit produces dozens of them.

const error = (line: number | null, message = 'Undefined control sequence.'): Diagnostic =>
  ({ level: 'error', message, line, file: null })

const warning = (line: number | null): Diagnostic =>
  ({ level: 'warning', message: 'Reference undefined.', line, file: null })

const hint = (line: number): Diagnostic =>
  ({ level: 'typesetting', message: 'Overfull \\hbox', line, file: null })

describe('DiagnosticsList', () => {
  it('renders the raw log when nothing parsed', () => {
    render(<DiagnosticsList diagnostics={[]} log="some compiler noise" onJumpToLine={vi.fn()} />)
    expect(screen.getByText('some compiler noise')).toBeInTheDocument()
  })

  it('says so when there is neither a diagnostic nor a log', () => {
    render(<DiagnosticsList diagnostics={[]} log="" onJumpToLine={vi.fn()} />)
    expect(screen.getByText('The compiler reported nothing.')).toBeInTheDocument()
  })

  it('jumps to the line when a row is clicked', async () => {
    const onJumpToLine = vi.fn()
    render(<DiagnosticsList diagnostics={[error(42)]} log="" onJumpToLine={onJumpToLine} />)
    await userEvent.click(screen.getByRole('button', { name: 'Go to line 42' }))
    expect(onJumpToLine).toHaveBeenCalledWith(42)
  })

  it('jumps on Enter, so the list is usable from the keyboard', async () => {
    const onJumpToLine = vi.fn()
    render(<DiagnosticsList diagnostics={[error(7)]} log="" onJumpToLine={onJumpToLine} />)
    screen.getByRole('button', { name: 'Go to line 7' }).focus()
    await userEvent.keyboard('{Enter}')
    expect(onJumpToLine).toHaveBeenCalledWith(7)
  })

  it('shows a message with no line but does NOT make it interactive', () => {
    // Offering a control that cannot do anything is worse than showing plain
    // text — especially to a keyboard user, who has to tab through it.
    render(<DiagnosticsList diagnostics={[error(null, 'Emergency stop.')]} log="" onJumpToLine={vi.fn()} />)
    expect(screen.getByText('Emergency stop.')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('shows errors and warnings together', () => {
    render(<DiagnosticsList diagnostics={[error(1), warning(2)]} log="" onJumpToLine={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Go to line 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Go to line 2' })).toBeInTheDocument()
  })

  it('collapses typesetting hints behind a pluralized toggle', async () => {
    render(
      <DiagnosticsList diagnostics={[hint(10), hint(20)]} log="" onJumpToLine={vi.fn()} />,
    )
    // Plural form comes from the catalog via i18next, never a concatenated 's'.
    const toggle = screen.getByRole('button', { name: /2 typesetting hints/ })
    expect(screen.queryByRole('button', { name: 'Go to line 10' })).not.toBeInTheDocument()
    await userEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Go to line 10' })).toBeInTheDocument()
  })

  it('uses the singular hint form for exactly one', () => {
    render(<DiagnosticsList diagnostics={[hint(10)]} log="" onJumpToLine={vi.fn()} />)
    expect(screen.getByRole('button', { name: /1 typesetting hint$/ })).toBeInTheDocument()
  })

  it('marks the hints toggle with its expanded state', async () => {
    render(<DiagnosticsList diagnostics={[hint(10)]} log="" onJumpToLine={vi.fn()} />)
    const toggle = screen.getByRole('button', { name: /typesetting hint/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
  })

  it('shows the file when the compiler named one', () => {
    // An error inside \input{sections/intro} reports sections/intro.tex, not the
    // main document — so the filename has to be visible or the line is misleading.
    const diagnostic: Diagnostic = {
      level: 'error', message: 'Missing $ inserted.', line: 3, file: 'sections/intro.tex',
    }
    render(<DiagnosticsList diagnostics={[diagnostic]} log="" onJumpToLine={vi.fn()} />)
    expect(screen.getByText('sections/intro.tex')).toBeInTheDocument()
  })

  it('contains no emoji', () => {
    const { container } = render(
      <DiagnosticsList diagnostics={[error(1), warning(2), hint(3)]} log="" onJumpToLine={vi.fn()} />,
    )
    // AUTOSDE `no-emoji-as-icons`: status is carried by Lucide glyphs + design
    // tokens, which emoji cannot participate in across the 11 themes.
    expect(container.textContent ?? '').not.toMatch(/\p{Extended_Pictographic}/u)
  })
})
