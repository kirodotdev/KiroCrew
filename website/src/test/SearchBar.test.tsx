import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import SearchBar from '../components/SearchBar'
import type { SearchMatch } from '../hooks/useMessageSearch'

const defaults = {
  term: '',
  setTerm: vi.fn(),
  matches: [] as SearchMatch[],
  currentIdx: 0,
  next: vi.fn(),
  prev: vi.fn(),
  close: vi.fn(),
  caseSensitive: false,
  toggleCaseSensitive: vi.fn(),
}

function renderBar(overrides: Partial<typeof defaults> = {}) {
  const props = { ...defaults, ...overrides }
  // Reset mocks from defaults that weren't overridden
  for (const k of Object.keys(defaults) as (keyof typeof defaults)[]) {
    if (typeof props[k] === 'function' && !(k in overrides)) {
      (props[k] as ReturnType<typeof vi.fn>).mockClear()
    }
  }
  return render(<SearchBar {...props} />)
}

describe('SearchBar', () => {
  it('renders input with placeholder', () => {
    renderBar()
    expect(screen.getByPlaceholderText('Find in chat…')).toBeInTheDocument()
  })

  it('auto-focuses input on mount', () => {
    renderBar()
    expect(screen.getByPlaceholderText('Find in chat…')).toHaveFocus()
  })

  it('calls setTerm on input change', () => {
    const setTerm = vi.fn()
    renderBar({ setTerm })
    fireEvent.change(screen.getByPlaceholderText('Find in chat…'), { target: { value: 'test' } })
    expect(setTerm).toHaveBeenCalledWith('test')
  })

  it('shows "No results" when term is set but matches is empty', () => {
    renderBar({ term: 'xyz', matches: [] })
    expect(screen.getByText('No results')).toBeInTheDocument()
  })

  it('shows correct count format', () => {
    const matches: SearchMatch[] = [
      { msgIdx: 0, occ: 0 }, { msgIdx: 2, occ: 0 }, { msgIdx: 5, occ: 0 },
      { msgIdx: 8, occ: 0 }, { msgIdx: 10, occ: 0 },
    ]
    renderBar({ term: 'test', matches, currentIdx: 2 })
    expect(screen.getByText('3 of 5 results')).toBeInTheDocument()
  })

  it('shows "1 of 1 results" for single match', () => {
    renderBar({ term: 'unique', matches: [{ msgIdx: 0, occ: 0 }], currentIdx: 0 })
    expect(screen.getByText('1 of 1 results')).toBeInTheDocument()
  })

  it('hides count when term is empty', () => {
    const { container } = renderBar({ term: '' })
    expect(container.querySelector('.tabular-nums')).not.toBeInTheDocument()
  })

  it('Enter calls next()', () => {
    const next = vi.fn()
    renderBar({ next, term: 'x', matches: [{ msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }] })
    fireEvent.keyDown(screen.getByPlaceholderText('Find in chat…'), { key: 'Enter' })
    expect(next).toHaveBeenCalled()
  })

  it('Shift+Enter calls prev()', () => {
    const prev = vi.fn()
    renderBar({ prev, term: 'x', matches: [{ msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }] })
    fireEvent.keyDown(screen.getByPlaceholderText('Find in chat…'), { key: 'Enter', shiftKey: true })
    expect(prev).toHaveBeenCalled()
  })

  it('Escape calls close()', () => {
    const close = vi.fn()
    renderBar({ close })
    fireEvent.keyDown(screen.getByPlaceholderText('Find in chat…'), { key: 'Escape' })
    expect(close).toHaveBeenCalled()
  })

  it('ArrowDown calls next() (navigates results from the input)', () => {
    const next = vi.fn()
    renderBar({ next, term: 'x', matches: [{ msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }] })
    fireEvent.keyDown(screen.getByPlaceholderText('Find in chat…'), { key: 'ArrowDown' })
    expect(next).toHaveBeenCalled()
  })

  it('ArrowUp calls prev() (navigates results from the input)', () => {
    const prev = vi.fn()
    renderBar({ prev, term: 'x', matches: [{ msgIdx: 0, occ: 0 }, { msgIdx: 1, occ: 0 }] })
    fireEvent.keyDown(screen.getByPlaceholderText('Find in chat…'), { key: 'ArrowUp' })
    expect(prev).toHaveBeenCalled()
  })

  it('up arrow button calls prev()', () => {
    const prev = vi.fn()
    renderBar({ prev })
    fireEvent.click(screen.getByTitle('Previous (Shift+Enter)'))
    expect(prev).toHaveBeenCalled()
  })

  it('down arrow button calls next()', () => {
    const next = vi.fn()
    renderBar({ next })
    fireEvent.click(screen.getByTitle('Next (Enter)'))
    expect(next).toHaveBeenCalled()
  })

  it('close button calls close()', () => {
    const close = vi.fn()
    renderBar({ close })
    fireEvent.click(screen.getByTitle('Close (Esc)'))
    expect(close).toHaveBeenCalled()
  })

  it('case-sensitive button calls toggleCaseSensitive', () => {
    const toggleCaseSensitive = vi.fn()
    renderBar({ toggleCaseSensitive })
    fireEvent.click(screen.getByTitle('Case sensitive'))
    expect(toggleCaseSensitive).toHaveBeenCalled()
  })

  const someMatches: SearchMatch[] = [
    { msgIdx: 0, occ: 0 }, { msgIdx: 2, occ: 0 }, { msgIdx: 5, occ: 0 },
  ]

  it('Home at the cursor start jumps to the first result via goTo(0)', () => {
    const goTo = vi.fn()
    renderBar({ term: 'hello', matches: someMatches, currentIdx: 2, goTo })
    const input = screen.getByPlaceholderText('Find in chat…') as HTMLInputElement
    input.setSelectionRange(0, 0)
    fireEvent.keyDown(input, { key: 'Home' })
    expect(goTo).toHaveBeenCalledWith(0)
  })

  it('End at the cursor end jumps to the last result via goTo(length-1)', () => {
    const goTo = vi.fn()
    renderBar({ term: 'hello', matches: someMatches, currentIdx: 0, goTo })
    const input = screen.getByPlaceholderText('Find in chat…') as HTMLInputElement
    input.setSelectionRange(input.value.length, input.value.length)
    fireEvent.keyDown(input, { key: 'End' })
    expect(goTo).toHaveBeenCalledWith(2)
  })

  it('Home mid-query moves the text cursor, not the results (regression)', () => {
    const goTo = vi.fn()
    renderBar({ term: 'hello', matches: someMatches, currentIdx: 2, goTo })
    const input = screen.getByPlaceholderText('Find in chat…') as HTMLInputElement
    input.setSelectionRange(3, 3) // cursor in the middle of the query
    fireEvent.keyDown(input, { key: 'Home' })
    expect(goTo).not.toHaveBeenCalled()
  })

  it('Home/End do nothing when there are no matches', () => {
    const goTo = vi.fn()
    renderBar({ term: 'hello', matches: [], currentIdx: 0, goTo })
    const input = screen.getByPlaceholderText('Find in chat…') as HTMLInputElement
    input.setSelectionRange(0, 0)
    fireEvent.keyDown(input, { key: 'Home' })
    input.setSelectionRange(input.value.length, input.value.length)
    fireEvent.keyDown(input, { key: 'End' })
    expect(goTo).not.toHaveBeenCalled()
  })

  it('exposes combobox a11y wiring so the active result is announced', () => {
    renderBar({ term: 'x', matches: someMatches, currentIdx: 1 })
    const input = screen.getByPlaceholderText('Find in chat…')
    expect(input).toHaveAttribute('role', 'combobox')
    expect(input).toHaveAttribute('aria-controls', 'mc-search-results-listbox')
    expect(input).toHaveAttribute('aria-expanded', 'true')
    // aria-activedescendant points at the current option's id.
    expect(input).toHaveAttribute('aria-activedescendant', 'mc-search-opt-1')
  })

  it('omits aria-activedescendant and collapses when there are no matches', () => {
    renderBar({ term: 'zzz', matches: [], currentIdx: 0 })
    const input = screen.getByPlaceholderText('Find in chat…')
    expect(input).toHaveAttribute('aria-expanded', 'false')
    expect(input).not.toHaveAttribute('aria-activedescendant')
  })
})
