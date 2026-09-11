import { act, fireEvent, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import {
  __resetBottomTerminal,
  addTab,
  openBottomTerminal,
  useBottomTerminal,
} from '../hooks/useBottomTerminal'
import { renderWithProviders } from './helpers'

const deleteTerminal = vi.fn()
const deleteTerminalAsync = vi.fn<() => Promise<void>>()
const disposeTerminal = vi.fn()

vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
  disposeTerminalSession: (id: string) => disposeTerminal(id),
  useDeleteTerminalSession: () => ({
    mutate: deleteTerminal,
    mutateAsync: deleteTerminalAsync,
  }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: (id: string) => id,
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  openPopout: vi.fn(),
  isPopoutOpen: vi.fn(() => true),
  focusPopout: vi.fn(),
  bringBack: vi.fn(),
  returnSelfToMain: vi.fn(),
}))

function seedTabs(count: number): string[] {
  openBottomTerminal()
  for (let i = 1; i < count; i++) addTab()
  const { result, unmount } = renderHook(() => useBottomTerminal())
  const ids = result.current.tabs.map(tab => tab.id)
  unmount()
  return ids
}

function openMenu(tabId: string) {
  fireEvent.contextMenu(screen.getByRole('tab', { name: tabId }), {
    clientX: 80,
    clientY: 40,
  })
}

beforeEach(() => {
  __resetBottomTerminal()
  deleteTerminal.mockReset()
  deleteTerminalAsync.mockReset()
  deleteTerminalAsync.mockResolvedValue(undefined)
  disposeTerminal.mockReset()
})

afterEach(() => {
  __resetBottomTerminal()
  vi.useRealTimers()
})

describe('terminal tab close menu', () => {
  it('opens on right-click and closes every tab to the right', async () => {
    const [first, second, third] = seedTabs(3)
    renderWithProviders(<TerminalTabsView variant="dock" />)

    openMenu(second)
    expect(screen.getByRole('menuitem', { name: 'Close' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Close other tabs' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Close tabs to the right' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Close all tabs' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('menuitem', { name: 'Close tabs to the right' }))

    await waitFor(() => {
      expect(screen.queryByRole('tab', { name: third })).not.toBeInTheDocument()
    })
    expect(screen.getByRole('tab', { name: first })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: second })).toHaveAttribute('aria-selected', 'true')
    expect(deleteTerminal).toHaveBeenCalledTimes(1)
    expect(deleteTerminal).toHaveBeenCalledWith(third)
    expect(disposeTerminal).toHaveBeenCalledWith(third)
  })

  it('closes other tabs and disables actions with no eligible target', async () => {
    const [first, second, third] = seedTabs(3)
    renderWithProviders(<TerminalTabsView variant="dock" />)

    openMenu(third)
    expect(screen.getByRole('menuitem', { name: 'Close tabs to the right' })).toHaveAttribute('data-disabled')

    fireEvent.click(screen.getByRole('menuitem', { name: 'Close other tabs' }))
    await waitFor(() => {
      expect(screen.getAllByRole('tab')).toHaveLength(1)
    })
    expect(screen.getByRole('tab', { name: third })).toBeInTheDocument()
    expect(deleteTerminal.mock.calls.map(call => call[0])).toEqual([first, second])
    expect(disposeTerminal.mock.calls.map(call => call[0])).toEqual([first, second])

    openMenu(third)
    expect(screen.getByRole('menuitem', { name: 'Close other tabs' })).toHaveAttribute('data-disabled')
  })

  it('opens on a stationary touch hold', () => {
    const [, second] = seedTabs(2)
    renderWithProviders(<TerminalTabsView variant="dock" />)
    vi.useFakeTimers()

    fireEvent.pointerDown(screen.getByRole('tab', { name: second }), {
      pointerType: 'touch',
      button: 0,
      clientX: 80,
      clientY: 40,
    })
    act(() => { vi.advanceTimersByTime(699) })
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()

    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('waits for every popout DELETE before closing all tabs', async () => {
    let resolveDelete!: () => void
    const pending = new Promise<void>(resolve => { resolveDelete = resolve })
    deleteTerminalAsync.mockReturnValue(pending)
    const [first, second] = seedTabs(2)
    renderWithProviders(<TerminalTabsView variant="popout" />)

    openMenu(first)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))

    expect(screen.getByRole('tab', { name: first })).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByRole('tab', { name: second })).toHaveAttribute('aria-busy', 'true')
    expect(deleteTerminalAsync.mock.calls.map(call => call[0])).toEqual([first, second])
    expect(disposeTerminal).not.toHaveBeenCalled()

    await act(async () => { resolveDelete(); await pending })
    await waitFor(() => {
      expect(screen.queryAllByRole('tab')).toHaveLength(0)
    })
    expect(disposeTerminal.mock.calls.map(call => call[0])).toEqual([first, second])
  })

  it('clears pending popout tabs if the window unloads before DELETEs settle', async () => {
    const pending = new Promise<void>(() => {})
    deleteTerminalAsync.mockReturnValue(pending)
    const [first, second] = seedTabs(2)
    const view = renderWithProviders(<TerminalTabsView variant="popout" />)

    openMenu(first)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Close all tabs' }))
    expect(screen.getByRole('tab', { name: first })).toHaveAttribute('aria-busy', 'true')

    view.unmount()

    const { result, unmount } = renderHook(() => useBottomTerminal())
    expect(result.current.tabs).toHaveLength(0)
    unmount()
    expect(deleteTerminalAsync.mock.calls.map(call => call[0])).toEqual([first, second])
    expect(disposeTerminal.mock.calls.map(call => call[0])).toEqual([first, second])
  })
})
