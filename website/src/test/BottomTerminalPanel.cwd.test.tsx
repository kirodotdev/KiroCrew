/**
 * Bottom terminal panel — new terminals open in the SELECTED session's
 * workspace/project directory (issue #2832).
 *
 * Pins the two halves of the behavior:
 *
 * 1. `selectActiveSlotProject` resolves the active chat slot's project
 *    directory (and yields undefined when no session is selected, or the
 *    selected session has no project — the "fall back to the server default"
 *    contract).
 * 2. The panel's "+" (new terminal) button threads that directory into the
 *    minted tab's `cwd`, which is what the WS layer sends as `?cwd=` — and
 *    omits it when no session is selected, so the backend default applies.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders, createTestStore } from './helpers'
import { TerminalTabsView } from '../components/BottomTerminalPanel'
import { __resetBottomTerminal, openBottomTerminal, useBottomTerminal } from '../hooks/useBottomTerminal'
import { selectActiveSlotProject } from '../store/chatSlice'
import { renderHook } from '@testing-library/react'
import type { RootState } from '../store'

vi.mock('../components/CliPanel', () => ({
  default: ({ sessionId }: { sessionId: string }) => <div data-testid={`cli-${sessionId}`} />,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalTitle: () => '',
  disposeTerminalConnection: vi.fn(),
}))
vi.mock('../utils/terminalPopout', () => ({
  openPopout: vi.fn(),
  isPopoutOpen: vi.fn(() => true),
  focusPopout: vi.fn(),
  bringBack: vi.fn(),
  returnSelfToMain: vi.fn(),
}))
vi.mock('../hooks/usePanelTabs', () => ({
  usePanelTabs: () => ({}),
}))

beforeEach(() => {
  __resetBottomTerminal()
  vi.clearAllMocks()
})
afterEach(() => {
  __resetBottomTerminal()
})

/** Store whose active slot is `key` and whose dashboard slot list carries the
 *  given project (undefined = session without a project directory). */
function storeWith(key: string | null, project?: string) {
  const store = createTestStore()
  const base = store.getState() as RootState
  return createTestStore({
    chat: { ...base.chat, activeSlot: key },
    dashboard: {
      ...base.dashboard,
      slots: key ? [{ key, messages: 0, running: false, project }] : [],
    },
  } as Partial<RootState>)
}

describe('selectActiveSlotProject', () => {
  it('returns the active slot project directory', () => {
    const store = storeWith('s1', '/repos/my-app')
    expect(selectActiveSlotProject(store.getState() as RootState)).toBe('/repos/my-app')
  })

  it('returns undefined when no session is selected', () => {
    const store = storeWith(null)
    expect(selectActiveSlotProject(store.getState() as RootState)).toBeUndefined()
  })

  it('returns undefined when the selected session has no project (empty string too)', () => {
    const store = storeWith('s1', '')
    expect(selectActiveSlotProject(store.getState() as RootState)).toBeUndefined()
  })

  it('returns undefined when the active slot is not in the slot list', () => {
    const store = storeWith('missing')
    const state = store.getState() as RootState
    expect(selectActiveSlotProject({
      ...state,
      dashboard: { ...state.dashboard, slots: [] },
    } as RootState)).toBeUndefined()
  })
})

describe('TerminalTabsView "+" button — new tab cwd', () => {
  it('mints the new tab with the selected session project as cwd', async () => {
    openBottomTerminal() // first tab, no cwd (panel opened before selection)
    const store = storeWith('s1', '/repos/my-app')
    renderWithProviders(<TerminalTabsView variant="dock" />, { store })

    await userEvent.click(screen.getByRole('button', { name: 'New terminal' }))

    const { result } = renderHook(() => useBottomTerminal())
    const tabs = result.current.tabs
    expect(tabs).toHaveLength(2)
    expect(tabs[1].cwd).toBe('/repos/my-app')
  })

  it('mints the new tab without a cwd when no session is selected (server default)', async () => {
    openBottomTerminal()
    const store = storeWith(null)
    renderWithProviders(<TerminalTabsView variant="dock" />, { store })

    await userEvent.click(screen.getByRole('button', { name: 'New terminal' }))

    const { result } = renderHook(() => useBottomTerminal())
    const tabs = result.current.tabs
    expect(tabs).toHaveLength(2)
    expect(tabs[1].cwd).toBeUndefined()
  })
})
