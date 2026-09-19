import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ShortcutsModal, {
  useShortcutPrefs,
  groupShortcuts,
  ShortcutRow,
  KeyCapSequence,
  SearchEverywhereRow,
  PanelToggleRows,
  GlobalHotkeyRow,
} from './ShortcutsModal'

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

describe('ShortcutsModal & subcomponents', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('handles useShortcutPrefs correctly', () => {
    const dispatchSpy = vi.spyOn(window, 'dispatchEvent')
    const { result } = renderHook(() => useShortcutPrefs())
    expect(result.current.enabled).toBe(true)
    expect(result.current.macCtrl).toBe(true)

    act(() => {
      result.current.toggle(false)
    })
    expect(result.current.enabled).toBe(false)
    expect(dispatchSpy).toHaveBeenCalled()

    act(() => {
      result.current.toggleMacCtrl(false)
    })
    expect(result.current.macCtrl).toBe(false)
    expect(dispatchSpy).toHaveBeenCalledTimes(2)
  })

  it('handles groupShortcuts with and without macCtrl', () => {
    const navigationShortcuts = groupShortcuts('chat-navigation', true)
    expect(navigationShortcuts.length).toBeGreaterThan(0)

    const noCtrlShortcuts = groupShortcuts('chat-navigation', false)
    expect(noCtrlShortcuts.length).toBeGreaterThan(0)
  })

  it('renders ShortcutRow correctly', () => {
    renderWithQuery(<ShortcutRow label="Test Action" keys={['Alt', 'T']} />)
    expect(screen.getByText('Test Action')).toBeInTheDocument()
    expect(screen.getByText('Alt')).toBeInTheDocument()
    expect(screen.getByText('T')).toBeInTheDocument()
  })

  it('renders KeyCapSequence with and without plus separator', () => {
    const { rerender } = renderWithQuery(<KeyCapSequence caps={['Cmd', 'K']} plus />)
    expect(screen.getByText('Cmd')).toBeInTheDocument()
    expect(screen.getByText('K')).toBeInTheDocument()
    expect(screen.getByText('+')).toBeInTheDocument()

    rerender(<KeyCapSequence caps={['Shift', 'Shift']} plus={false} />)
    expect(screen.queryByText('+')).not.toBeInTheDocument()
  })

  it('renders SearchEverywhereRow', () => {
    renderWithQuery(<SearchEverywhereRow />)
    expect(screen.getByText(/search everywhere/i)).toBeInTheDocument()
  })

  it('renders PanelToggleRows with custom ids', () => {
    const { rerender } = renderWithQuery(<PanelToggleRows ids={['left-sidebar', 'session-panel', 'side-panel']} />)
    expect(screen.getAllByText(/toggle|sidebar|panel/i).length).toBeGreaterThan(0)

    rerender(<PanelToggleRows ids={['side-panel']} />)
    expect(screen.getAllByText(/side panel/i).length).toBeGreaterThan(0)
  })

  it('renders GlobalHotkeyRow when hotkey is configured', () => {
    const { container } = renderWithQuery(<GlobalHotkeyRow />)
    expect(container).toBeInTheDocument()
  })

  it('renders ShortcutsModal and filters items dynamically when searching', () => {
    const onClose = vi.fn()
    renderWithQuery(<ShortcutsModal onClose={onClose} />)

    // Verify search input is present
    const searchInput = screen.getByRole('textbox')
    expect(searchInput).toBeInTheDocument()

    // Type a specific search query ('chat')
    fireEvent.change(searchInput, { target: { value: 'chat' } })

    // Verify matching content displays
    const chatMatches = screen.queryAllByText(/chat/i)
    expect(chatMatches.length).toBeGreaterThan(0)

    // Verify non-matching items disappear
    expect(screen.queryByText(/toggle theme/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/stop speaking/i)).not.toBeInTheDocument()

    // Type non-matching search query
    fireEvent.change(searchInput, { target: { value: 'nonexistentquery12345' } })
    expect(screen.getByTestId('filtered-empty')).toBeInTheDocument()
    expect(screen.getByText(/nonexistentquery12345/i)).toBeInTheDocument()

    // Click clear button in FilteredEmpty
    fireEvent.click(screen.getByTestId('filtered-empty-clear'))
    expect(searchInput).toHaveValue('')

    // Type query and verify clear button on search input
    fireEvent.change(searchInput, { target: { value: 'search' } })
    const clearBtn = screen.getByTestId('shortcuts-search-clear')
    expect(clearBtn).toBeInTheDocument()
    fireEvent.click(clearBtn)
    expect(searchInput).toHaveValue('')

    // Search by section name
    fireEvent.change(searchInput, { target: { value: 'panel' } })
    expect(screen.queryAllByText(/panel/i).length).toBeGreaterThan(0)
  })

  it('handles Escape key: clears search input first, then closes modal', () => {
    const onClose = vi.fn()
    renderWithQuery(<ShortcutsModal onClose={onClose} />)

    const searchInput = screen.getByRole('textbox')
    fireEvent.change(searchInput, { target: { value: 'test' } })
    expect(searchInput).toHaveValue('test')

    // First Escape: clears search query
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(searchInput).toHaveValue('')
    expect(onClose).not.toHaveBeenCalled()

    // Second Escape: closes modal
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes modal on backdrop and close button click', () => {
    const onClose = vi.fn()
    const { rerender } = renderWithQuery(<ShortcutsModal onClose={onClose} />)

    // Click backdrop (dialog overlay)
    const dialog = screen.getByRole('dialog')
    fireEvent.click(dialog)
    expect(onClose).toHaveBeenCalledTimes(1)

    // Click modal close button
    onClose.mockClear()
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <ShortcutsModal onClose={onClose} />
      </QueryClientProvider>,
    )
    const closeButtons = screen.getAllByLabelText(/close/i)
    fireEvent.click(closeButtons[0])
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
