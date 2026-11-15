import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'
import { QUICK_SEARCH_SHORTCUT_KEY } from '../../lib/quickSearchShortcut'

/**
 * The command bar is modal, so a key pressed inside it must not reach the page's
 * global shortcuts behind it. Those listen in the bubble phase on `document`
 * (useKeyboardShortcuts, useInstanceShortcuts); the quick-search gesture that
 * toggles the bar listens on `window` (useCommandPalette) and is the one key that
 * still travels. The bar's own keys are handled on its input and keep working.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const newSessionWithToken = vi.fn()
const enterInsertOrNewSession = vi.fn()

const storeState = {
  dashboard: { slots: [] as Record<string, unknown>[], unreadSlots: [] as string[] },
  chat: { slotStatusDetail: {} as Record<string, unknown>, activeSlot: null as string | null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
  useAppStore: () => ({ getState: () => storeState }),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  switchSlot: (key: string) => ({ type: 'switchSlot', key }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({ navigate, enterInsertOrNewSession, newSessionWithToken }),
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<typeof import('../../components/commandPalette/providers/recentsProvider')>()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useDialogFocusTrap', () => ({ useDialogFocusTrap: () => {} }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))

function mount(onClose = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <CommandBarOverlay open onClose={onClose} />
    </QueryClientProvider>,
  )
  return onClose
}

describe('CommandBarOverlay keyboard isolation', () => {
  const documentListener = vi.fn()
  const windowListener = vi.fn()

  beforeEach(() => {
    dispatch.mockReset()
    navigate.mockReset()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    localStorage.clear()
    documentListener.mockReset()
    windowListener.mockReset()
    document.addEventListener('keydown', documentListener)
    window.addEventListener('keydown', windowListener)
  })

  afterEach(() => {
    document.removeEventListener('keydown', documentListener)
    window.removeEventListener('keydown', windowListener)
  })

  it.each([
    ['the Settings chord', { key: ',', code: 'Comma', altKey: true }],
    ['a session jump', { key: '1', code: 'Digit1', ctrlKey: true }],
    ['the agent monitor', { key: 'g', code: 'KeyG', ctrlKey: true }],
    ['a plain letter', { key: 'a', code: 'KeyA' }],
  ])('keeps %s from reaching the page behind the bar', (_name, init) => {
    mount()
    fireEvent.keyDown(screen.getByRole('combobox'), init)
    expect(documentListener).not.toHaveBeenCalled()
    expect(windowListener).not.toHaveBeenCalled()
  })

  it('lets the default quick-search gesture through, so it can close the bar', () => {
    mount()
    fireEvent.keyDown(screen.getByRole('combobox'), { key: 'k', code: 'KeyK', ctrlKey: true })
    expect(windowListener).toHaveBeenCalledTimes(1)
  })

  it('lets a recorded custom quick-search chord through, and Ctrl+K then stops', () => {
    localStorage.setItem(
      QUICK_SEARCH_SHORTCUT_KEY,
      JSON.stringify({ mode: 'custom', custom: { key: 'j', ctrl: true } }),
    )
    mount()
    const input = screen.getByRole('combobox')
    fireEvent.keyDown(input, { key: 'j', code: 'KeyJ', ctrlKey: true })
    expect(windowListener).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(input, { key: 'k', code: 'KeyK', ctrlKey: true })
    expect(windowListener).toHaveBeenCalledTimes(1)
  })

  it('keeps the bar’s own keys working: arrows move the selection, Escape closes', () => {
    const onClose = mount()
    const input = screen.getByRole('combobox')
    const before = input.getAttribute('aria-activedescendant')
    expect(before).toBe('command-bar-row-0')
    fireEvent.keyDown(input, { key: 'ArrowDown', code: 'ArrowDown' })
    expect(input.getAttribute('aria-activedescendant')).toBe('command-bar-row-1')
    expect(documentListener).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Escape', code: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
