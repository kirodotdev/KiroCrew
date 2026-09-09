/**
 * The artifacts folder menu carries the same standing offline reason as its
 * siblings, so its own gateway writes have to be gated too — a reason row above a
 * full-weight Delete states something false and fires a doomed write.
 *
 * Renders `FolderMenu` DIRECTLY: a green assertion driven through the sidebar's
 * folder menu would prove nothing about this component.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from '../../test/helpers'
import { ThemeProvider } from '../../hooks/useTheme'
import type { RootState } from '../../store'
import type { ArtifactFolder } from '../../types'

vi.mock('../../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import { FolderMenu } from './LibraryTable'

const folder = { id: 'af1', name: 'Reports', parent_id: null, color: undefined } as unknown as ArtifactFolder

/** `connected` is explicit: createTestStore() models a DISCONNECTED dashboard, so
 *  an inherited default would run the offline branch while looking deliberate. */
function renderMenu(connected: boolean) {
  const actions = {
    onRename: vi.fn(), onDelete: vi.fn(), onMove: vi.fn(), onSetColor: vi.fn(),
  }
  const store = createTestStore({
    dashboard: {
      status: {}, connected, slots: [], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <FolderMenu folder={folder} folders={[folder]} actions={actions as never} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return { actions, store }
}

const itemFor = (label: RegExp) => screen.getByText(label).closest('[aria-disabled]') as HTMLElement | null

beforeEach(() => vi.clearAllMocks())

describe('artifacts FolderMenu – the reason row must be true of the whole menu', () => {
  it('offline Rename and Delete are marked disabled, not just the Move submenu', () => {
    const { store } = renderMenu(false)
    expect(store.getState().dashboard.connected).toBe(false)
    expect(itemFor(/^Rename$/)?.getAttribute('aria-disabled')).toBe('true')
    expect(itemFor(/^Delete…$/)?.getAttribute('aria-disabled')).toBe('true')
  })

  it('offline Rename and Delete fire no write and leave the menu open', () => {
    const { actions } = renderMenu(false)
    fireEvent.click(screen.getByText(/^Delete…$/))
    expect(actions.onDelete).not.toHaveBeenCalled()
    // Suppressed through onSelect, so the menu — and its reason row — survives.
    expect(screen.getByText(/^Delete…$/)).toBeInTheDocument()
    fireEvent.click(screen.getByText(/^Rename$/))
    expect(actions.onRename).not.toHaveBeenCalled()
  })

  it('the reason row sits LAST, so a mid-aim disconnect cannot shift Delete under the pointer', () => {
    renderMenu(false)
    const reason = screen.getByTestId('artifact-folder-offline-reason')
    const del = screen.getByText(/^Delete…$/).closest('[aria-disabled]') as HTMLElement
    expect(del.compareDocumentPosition(reason) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('offline colour swatches READ as inert, not merely refuse', () => {
    renderMenu(false)
    // The per-button `disabled:` variants did not render in a real frame, so the
    // row itself carries the dim — the same vocabulary the gated rows use.
    const row = screen.getAllByRole('radio')[0].closest('[role="radiogroup"]') as HTMLElement
    expect(row.className).toContain('opacity-40')
  })

  it('connected the swatch row is NOT dimmed — the control for the case above', () => {
    renderMenu(true)
    const row = screen.getAllByRole('radio')[0].closest('[role="radiogroup"]') as HTMLElement
    expect(row.className).not.toContain('opacity-40')
  })

  it('offline colour swatches fire no write', () => {
    const { actions } = renderMenu(false)
    const swatch = screen.getAllByRole('radio')[0]
    fireEvent.click(swatch)
    expect(actions.onSetColor).not.toHaveBeenCalled()
  })

  it('connected the same three still work, so the gate is not blanket', () => {
    const { actions } = renderMenu(true)
    expect(itemFor(/^Rename$/)?.getAttribute('aria-disabled')).toBe('false')
    fireEvent.click(screen.getAllByRole('radio')[0])
    expect(actions.onSetColor).toHaveBeenCalled()
    fireEvent.click(screen.getByText(/^Delete…$/))
    expect(actions.onDelete).toHaveBeenCalled()
  })
})
