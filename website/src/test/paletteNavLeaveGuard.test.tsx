/**
 * The command palette jumps between whole pages, so it destroys an unsaved draft
 * on the page it leaves exactly as the global sidebar does — and it is one
 * keystroke from anywhere, which makes it the second-most-likely way to lose a
 * draft after the sidebar. Three review lanes on PR #7946 named it as the
 * uncovered sibling of the sidebar exit.
 *
 * Every palette consumer (CommandPalette, CommandBarOverlay) navigates through
 * the single `navigate` delegate `usePaletteActions` returns rather than calling
 * react-router's `navigate` itself, so these pin the guard at that one delegate
 * and cover both.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { cleanup, renderHook, screen, fireEvent, within, waitFor, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { usePaletteActions } from '../components/commandPalette/paletteActions'
import {
  NavigationLeaveGuardProvider,
  useRegisterNavigationLeaveGuard,
} from '../components/NavigationLeaveGuard'

import { useConfirm } from '../components/ConfirmDialog'

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

/** A page that is holding work: it answers "no" unless the user confirms. */
function DirtyPage({ children }: { children: React.ReactNode }) {
  const { confirm, confirmDialog } = useConfirm()
  useRegisterNavigationLeaveGuard(() => confirm({ title: 'Discard unsaved changes?', confirmLabel: 'Discard changes' }))
  return <>{children}{confirmDialog}</>
}

/** A page with nothing at stake registers no guard at all. */
function CleanPage({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}

/** The provider stack `renderHookWithProviders` gives, plus the guard channel and
 *  a page registered in it. Composed here rather than by extending the shared
 *  helper, which every other suite depends on. */
const renderActions = (
  Page: typeof DirtyPage,
  route = '/capabilities?tab=prompts',
) => {
  const store = createTestStore()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return renderHook(() => usePaletteActions(), {
    wrapper: ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <MemoryRouter initialEntries={[route]}>
            <NavigationLeaveGuardProvider><Page>{children}</Page></NavigationLeaveGuardProvider>
          </MemoryRouter>
        </Provider>
      </QueryClientProvider>
    ),
  })
}

describe('command palette navigation leave guard', () => {
  const settleDiscard = async (confirmed: boolean) => {
    const dialog = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: confirmed ? 'Discard changes' : 'Cancel' })) })
  }

  beforeEach(() => { navigateSpy.mockClear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('does not navigate when the page declines', async () => {
    const { result } = renderActions(DirtyPage)
    act(() => { result.current.navigate('/chat') })
    await settleDiscard(false)
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('navigates once the page accepts', async () => {
    const { result } = renderActions(DirtyPage)
    act(() => { result.current.navigate('/chat') })
    await settleDiscard(true)
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/chat', { state: { leaveGranted: true } }))
  })

  it('does not ask when the page has nothing at stake', async () => {
    const { result } = renderActions(CleanPage)
    result.current.navigate('/chat')
    await waitFor(() => expect(navigateSpy).toHaveBeenCalledWith('/chat', { state: { leaveGranted: true } }))
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).toBeNull()
  })

  it('asks when the jump would drop the query the pane is mounted on', async () => {
    const { result } = renderActions(DirtyPage)
    act(() => { result.current.navigate('/capabilities') })
    await settleDiscard(false)
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('never asks for a jump to the address we are already at', () => {
    const { result } = renderActions(DirtyPage)
    result.current.navigate('/capabilities?tab=prompts')
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).toBeNull()
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=prompts', undefined)
  })
})
