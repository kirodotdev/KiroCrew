/**
 * Integration tests for the "Not in progress" filter option in the sidebar
 * Filter menu (#15535). Verifies that the option hides running sessions,
 * shows a "Not in progress (N)" chip, and is mutually exclusive with the
 * "In progress" show-only filter.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { screen, waitFor, fireEvent, cleanup } from '@testing-library/react'

vi.mock('@radix-ui/react-dropdown-menu', () => import('./__mocks__/@radix-ui/react-dropdown-menu'))
vi.mock('@radix-ui/react-context-menu', () => import('./__mocks__/@radix-ui/react-context-menu'))

import ChatSidebar from '../src/pages/ChatSidebar'
import { renderWithProviders } from './helpers'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'
import { __resetAuthRecoveryStateForTests } from '../src/api/client'

const mockConfirm = vi.fn(() => true)
Object.defineProperty(window, 'confirm', { writable: true, value: mockConfirm })

const slotsWithRunning = [
  { key: 'slot-idle-1', title: 'Pipeline debug', running: false, agent: 'kirocrew', created: '2026-04-08T01:00:00Z', last_ts: '2026-04-08T02:00:00Z', folder_id: '' },
  { key: 'slot-running-1', title: 'Code review', running: true, agent: 'kirocrew', created: '2026-04-08T00:00:00Z', last_ts: '2026-04-08T01:30:00Z', folder_id: '' },
  { key: 'slot-idle-2', title: 'Oncall triage', running: false, agent: 'kirocrew', created: '2026-04-07T10:00:00Z', last_ts: '2026-04-07T12:00:00Z', folder_id: '' },
]

const defaultProps = {
  slots: slotsWithRunning as any,
  activeSlot: 'slot-idle-1',
  unreadSlots: [] as string[],
  history: [],
  historyHasMore: false,
  defaultAgent: 'kirocrew',
  installedAgents: [{ name: 'kirocrew', source: 'builtin' }],
}

/** Open the filter menu by clicking the Sort & filter button. */
function openFilterMenu() {
  fireEvent.click(screen.getByRole('button', { name: 'Sort and filter sessions' }))
}

describe('ChatSidebar Not-in-progress filter', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    // Use the flat list lane so filter effects are directly observable.
    localStorage.setItem('mc-sidebar-lane', 'flat')
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    server.use(
      http.post('/api/auth/refresh', () => HttpResponse.json({ ok: true })),
      http.get('/api/auth/me', () => HttpResponse.json({ ok: true })),
      http.get('/api/chat/folders', () => HttpResponse.json([])),
    )
    __resetAuthRecoveryStateForTests()
    ;(document.activeElement as HTMLElement | null)?.blur?.()
  })

  afterEach(() => {
    cleanup()
    ;(document.activeElement as HTMLElement | null)?.blur?.()
  })

  it('shows "Not in progress" row in the Filter menu when "In progress" is inactive', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())

    openFilterMenu()

    // The "Not in progress" row is present while "In progress" show-only is off.
    expect(screen.getByTestId('filter-not-running')).toBeInTheDocument()
  })

  it('hides running sessions when "Not in progress" is activated', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)

    // All three sessions visible initially.
    await waitFor(() => {
      expect(screen.getByText('Pipeline debug')).toBeInTheDocument()
      expect(screen.getByText('Code review')).toBeInTheDocument()
      expect(screen.getByText('Oncall triage')).toBeInTheDocument()
    })

    openFilterMenu()
    // onSelect calls e.preventDefault(), so the menu stays open after this click.
    fireEvent.click(screen.getByTestId('filter-not-running'))

    // Running session is gone; idle sessions remain.
    await waitFor(() => {
      expect(screen.queryByText('Code review')).not.toBeInTheDocument()
    })
    expect(screen.getByText('Pipeline debug')).toBeInTheDocument()
    expect(screen.getByText('Oncall triage')).toBeInTheDocument()
  })

  it('shows a "Not in progress (1)" chip when the filter is active', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())

    openFilterMenu()
    fireEvent.click(screen.getByTestId('filter-not-running'))

    await waitFor(() => {
      const chip = screen.getByTestId('hidden-filter-chip')
      // Chip uses hiding_named_filter: "Hiding In progress (1)"
      expect(chip).toHaveTextContent(/In progress/)
      expect(chip).toHaveTextContent(/\(1\)/)
    })
  })

  it('restores running sessions when the "Not in progress" chip is cleared', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())

    openFilterMenu()
    fireEvent.click(screen.getByTestId('filter-not-running'))
    await waitFor(() => expect(screen.queryByText('Code review')).not.toBeInTheDocument())

    // The chip is itself the clear button — clicking it calls clearHiddenFilters.
    fireEvent.click(screen.getByTestId('hidden-filter-chip'))

    await waitFor(() => expect(screen.getByText('Code review')).toBeInTheDocument())
    expect(screen.queryByTestId('hidden-filter-chip')).not.toBeInTheDocument()
  })

  it('"Not in progress" and "In progress" are mutually exclusive', async () => {
    renderWithProviders(<ChatSidebar {...defaultProps} />)
    await waitFor(() => expect(screen.getByText('Pipeline debug')).toBeInTheDocument())

    // Step 1: Activate "Not in progress" — running sessions are hidden.
    openFilterMenu()
    fireEvent.click(screen.getByTestId('filter-not-running'))
    await waitFor(() => expect(screen.queryByText('Code review')).not.toBeInTheDocument())
    expect(screen.getByTestId('hidden-filter-chip')).toHaveTextContent(/In progress/)

    // Step 2: While the menu is still open (filter-not-running called
    // e.preventDefault()), click "In progress" to activate show-only mode.
    // This calls clearHideFor('running'), dropping the Not-in-progress hide.
    fireEvent.click(screen.getByRole('menuitem', { name: /^In progress/ }))

    // After: only the running session remains (show-only), and the
    // "Not in progress" chip is gone (hide was cleared).
    await waitFor(() => {
      expect(screen.getByText('Code review')).toBeInTheDocument()
      expect(screen.queryByText('Pipeline debug')).not.toBeInTheDocument()
      expect(screen.queryByText('Oncall triage')).not.toBeInTheDocument()
    })
    expect(screen.queryByTestId('hidden-filter-chip')).not.toBeInTheDocument()
  })
})
