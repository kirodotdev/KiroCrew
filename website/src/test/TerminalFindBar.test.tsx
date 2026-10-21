import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, act, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import TerminalFindBar from '../components/TerminalFindBar'
import type { TerminalSearch } from '../hooks/useTerminalSearch'

// isMac is a module-load const in utils/platform; default to Ctrl (non-mac).
const platformMock = vi.hoisted(() => ({ isMac: false }))
vi.mock('../utils/platform', () => ({
  get isMac() { return platformMock.isMac },
  isMacOSPlatform: () => platformMock.isMac,
  platformShortcut: (s: string) => s,
}))

/* The terminal scrollback find bar (#10176): a controlled component driven by
 * the useTerminalSearch handle. These tests pass a fake handle and assert the
 * UI wires to it — the search mechanics themselves are the hook's own test.
 *
 * Each test flushes the shared ThemeProvider's async theme settle inside act
 * (`await act(async () => {})`) after rendering, so a render-only assertion
 * does not trip the act-warning ratchet. */

function fakeSearch(overrides: Partial<TerminalSearch> = {}): TerminalSearch {
  return {
    open: true,
    query: '',
    resultIndex: -1,
    resultCount: 0,
    openSearch: vi.fn(),
    closeSearch: vi.fn(),
    setQuery: vi.fn(),
    findNext: vi.fn(),
    findPrevious: vi.fn(),
    inputRef: { current: null },
    ...overrides,
  }
}

async function render(search: TerminalSearch, bannerShowing = false, onToggleFullscreen?: () => void) {
  const result = renderWithProviders(<TerminalFindBar search={search} bannerShowing={bannerShowing} onToggleFullscreen={onToggleFullscreen} />)
  await act(async () => {})
  return result
}

beforeEach(() => vi.clearAllMocks())

describe('TerminalFindBar', () => {
  it('renders nothing when closed', async () => {
    const { container } = await render(fakeSearch({ open: false }))
    expect(container.querySelector('[data-testid="terminal-find-bar"]')).toBeNull()
  })

  it('handles the find chord while the input has focus on Linux/Windows (Ctrl+Shift+F), and leaves bare Ctrl+F alone', async () => {
    const onToggleFullscreen = vi.fn()
    const search = fakeSearch()
    await render(search, false, onToggleFullscreen)
    const input = screen.getByLabelText('Search terminal scrollback')
    const findEvt = fireEvent.keyDown(input, { key: 'F', code: 'KeyF', ctrlKey: true, shiftKey: true })
    expect(search.openSearch).toHaveBeenCalledTimes(1)
    expect(onToggleFullscreen).not.toHaveBeenCalled()
    expect(findEvt).toBe(false)
    // Bare Ctrl+F is not a terminal chord off macOS: not claimed here.
    const ctrlF = fireEvent.keyDown(input, { key: 'f', code: 'KeyF', ctrlKey: true })
    expect(search.openSearch).toHaveBeenCalledTimes(1)
    expect(ctrlF).toBe(true)
  })

  it('handles Cmd+F and Cmd+Shift+F while the input has focus on macOS', async () => {
    platformMock.isMac = true
    try {
      const onToggleFullscreen = vi.fn()
      const search = fakeSearch()
      await render(search, false, onToggleFullscreen)
      const input = screen.getByLabelText('Search terminal scrollback')
      expect(fireEvent.keyDown(input, { key: 'f', code: 'KeyF', metaKey: true })).toBe(false)
      expect(search.openSearch).toHaveBeenCalledTimes(1)
      expect(fireEvent.keyDown(input, { key: 'F', code: 'KeyF', metaKey: true, shiftKey: true })).toBe(false)
      expect(onToggleFullscreen).toHaveBeenCalledTimes(1)
    } finally {
      platformMock.isMac = false
    }
  })

  it('sits at the top strip by default and drops below the connection banner when it is showing', async () => {
    const { rerender } = await render(fakeSearch(), false)
    const bar = () => screen.getByTestId('terminal-find-bar')
    // No banner: pinned to the top strip.
    expect(bar().className).toContain('top-2')
    expect(bar().className).not.toContain('top-10')
    // Banner up (also z-30, top of the pane): drop below it so it is never covered.
    rerender(<TerminalFindBar search={fakeSearch()} bannerShowing />)
    await act(async () => {})
    expect(bar().className).toContain('top-10')
    expect(bar().className).not.toContain('top-2')
  })

  it('shows the find input, option toggles, and navigation when open', async () => {
    await render(fakeSearch())
    expect(screen.getByRole('search')).toBeInTheDocument()
    expect(screen.getByLabelText('Search terminal scrollback')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Previous match' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next match' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close search' })).toBeInTheDocument()
    // No option toggles: search uses the addon defaults.
    expect(screen.queryByRole('button', { name: 'Match case' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Use regular expression' })).toBeNull()
  })

  it('types into the query and reports it through setQuery', async () => {
    const s = fakeSearch()
    await render(s)
    await userEvent.type(screen.getByLabelText('Search terminal scrollback'), 'e')
    expect(s.setQuery).toHaveBeenCalledWith('e')
  })

  it('Enter runs findNext, Shift+Enter runs findPrevious, Escape closes', async () => {
    const s = fakeSearch({ query: 'err' })
    await render(s)
    const input = screen.getByLabelText('Search terminal scrollback')
    input.focus()
    await userEvent.keyboard('{Enter}')
    expect(s.findNext).toHaveBeenCalledTimes(1)
    await userEvent.keyboard('{Shift>}{Enter}{/Shift}')
    expect(s.findPrevious).toHaveBeenCalledTimes(1)
    await userEvent.keyboard('{Escape}')
    expect(s.closeSearch).toHaveBeenCalledTimes(1)
  })

  it('the nav buttons and close call their handlers', async () => {
    const s = fakeSearch({ query: 'x' })
    await render(s)
    await userEvent.click(screen.getByRole('button', { name: 'Next match' }))
    await userEvent.click(screen.getByRole('button', { name: 'Previous match' }))
    await userEvent.click(screen.getByRole('button', { name: 'Close search' }))
    expect(s.findNext).toHaveBeenCalledTimes(1)
    expect(s.findPrevious).toHaveBeenCalledTimes(1)
    expect(s.closeSearch).toHaveBeenCalledTimes(1)
  })

  it('shows no count for an empty query', async () => {
    await render(fakeSearch({ query: '' }))
    expect(screen.getByTestId('terminal-find-count').textContent).toBe('')
  })

  it('shows "n of m" for an active match', async () => {
    await render(fakeSearch({ query: 'e', resultIndex: 2, resultCount: 9 }))
    expect(screen.getByTestId('terminal-find-count').textContent).toBe('3 of 9')
  })

  it('shows "No results" for a non-empty query that matches nothing', async () => {
    await render(fakeSearch({ query: 'zzz', resultCount: 0, resultIndex: -1 }))
    expect(screen.getByTestId('terminal-find-count').textContent).toBe('No results')
  })

  it('falls back to the bare total when the count overflows the highlight limit', async () => {
    await render(fakeSearch({ query: 'e', resultCount: 1000, resultIndex: -1 }))
    expect(screen.getByTestId('terminal-find-count').textContent).toBe('1000 matches')
  })
})
