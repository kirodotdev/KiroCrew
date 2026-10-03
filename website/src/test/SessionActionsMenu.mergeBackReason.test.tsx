/**
 * The session menu greys two rows for one state, a turn in the middle of
 * running: "Reload session" and, on a fork, "Merge into parent…". Someone
 * scanning the menu reads one explanation for that state, so the two rows carry
 * the same reason string, in every language.
 */
import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '../components/ui/dropdown-menu'
import { MergeBackDialogProvider } from '../hooks/useMergeBackDialog'
import { i18nT } from '../i18n/t'
import type { RootState } from '../store'

vi.mock('../api/client', () => ({
  api: {
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

import SessionActionsMenu from '../components/SessionActionsMenu'

const FORK_MID_TURN = { key: 'fork', title: '↳ Fork of A', messages: 4, running: true, forked_from: 'dashboard:a' }

const dashboardState = {
  status: {}, connected: true, slots: [FORK_MID_TURN], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as unknown as RootState['dashboard']

function renderMenu() {
  const store = createTestStore({ dashboard: dashboardState })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <MergeBackDialogProvider>
              <DropdownMenu>
                <DropdownMenuTrigger>menu</DropdownMenuTrigger>
                <DropdownMenuContent>
                  <SessionActionsMenu variant="dropdown" slotKey="fork" />
                </DropdownMenuContent>
              </DropdownMenu>
            </MergeBackDialogProvider>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.getByText('menu'), { key: 'Enter' })
  return utils
}

/** The small reason text at the right of a greyed row: everything after its label. */
function reasonOf(row: HTMLElement, label: string): string {
  const text = row.textContent ?? ''
  expect(text).toContain(label)
  return text.slice(text.indexOf(label) + label.length).trim()
}

describe('SessionActionsMenu on a fork in the middle of a turn', () => {
  it('greys Reload session and Merge into parent… with the same reason', () => {
    renderMenu()
    const reloadLabel = i18nT('components.sessionActionsMenu.reload_session')
    const reloadRow = screen.getByText(reloadLabel).closest('[role="menuitem"]') as HTMLElement
    const mergeRow = screen.getByTestId('merge-back')

    expect(reloadRow).toHaveAttribute('aria-disabled', 'true')
    expect(mergeRow).toHaveAttribute('aria-disabled', 'true')
    const reloadReason = reasonOf(reloadRow, reloadLabel)
    const mergeReason = reasonOf(mergeRow, i18nT('components.mergeBackMenuItem.merge_into_parent'))
    expect(reloadReason).toBe('A turn is still running')
    expect(reloadReason).toBe(mergeReason)
  })
})
