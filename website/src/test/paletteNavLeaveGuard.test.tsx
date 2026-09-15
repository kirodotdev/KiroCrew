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
import { cleanup, renderHook } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { usePaletteActions } from '../components/commandPalette/paletteActions'
import {
  NavigationLeaveGuardProvider,
  useRegisterNavigationLeaveGuard,
} from '../components/NavigationLeaveGuard'

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => navigateSpy }
})

/** A page that is holding work: it answers "no" unless the user confirms. */
function DirtyPage({ children }: { children: React.ReactNode }) {
  useRegisterNavigationLeaveGuard(() => confirm('Discard unsaved changes?'))
  return <>{children}</>
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
  beforeEach(() => { navigateSpy.mockClear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('does not navigate when the page declines', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = renderActions(DirtyPage)
    result.current.navigate('/chat')
    expect(confirmSpy).toHaveBeenCalled()
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('navigates once the page accepts', () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { result } = renderActions(DirtyPage)
    result.current.navigate('/chat')
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })

  it('does not ask when the page has nothing at stake', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = renderActions(CleanPage)
    result.current.navigate('/chat')
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(navigateSpy).toHaveBeenCalledWith('/chat')
  })

  it('asks when the jump would drop the query the pane is mounted on', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = renderActions(DirtyPage)
    // Same pathname, no query: this is the palette's own Skills row jumping to
    // `/capabilities`. The pane is mounted on `?tab=prompts`, so it unmounts.
    result.current.navigate('/capabilities')
    expect(confirmSpy).toHaveBeenCalled()
    expect(navigateSpy).not.toHaveBeenCalled()
  })

  it('never asks for a jump to the address we are already at', () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { result } = renderActions(DirtyPage)
    result.current.navigate('/capabilities?tab=prompts')
    // Nothing changes, so nothing unmounts. A confirm the user did not earn is
    // what teaches them to click through the one that matters.
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(navigateSpy).toHaveBeenCalledWith('/capabilities?tab=prompts')
  })
})
