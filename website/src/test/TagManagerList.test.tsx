/**
 * Tests for TagManagerList — the tag-management list shared by the board
 * column-filter popover and the header "Manage tags…" panel in list view.
 * Covers both modes
 * (manage / column-filter) for rename · status · delete-with-confirm · create,
 * plus the include/exclude swatch behaviour that keeps the board mutating its
 * column's tag_ids identically.
 *
 * Also carries the SessionActionsMenu regression: the per-session "Tags…" item
 * must render regardless of the (board-only) tagColumnsEnabled config, so
 * the tag picker is reachable from the list-view row menu too.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: {
    chatTags: vi.fn().mockResolvedValue([
      { id: 't1', name: 'Alpha', color: '#ff0000', order: 0 },
      { id: 't2', name: 'Beta', color: '#00ff00', order: 1, status: true },
    ]),
    createChatTag: vi.fn().mockResolvedValue({ ok: true }),
    updateChatTag: vi.fn().mockResolvedValue({ ok: true }),
    deleteChatTag: vi.fn().mockResolvedValue({ ok: true }),
    chatFolders: vi.fn().mockResolvedValue([]),
    slackChannels: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    mcpActive: vi.fn().mockResolvedValue([]),
  },
}))

// The board gates its column strip behind this flag; the list-view "Tags…" item
// and pills must NOT depend on it. Force it off so the regression is explicit.
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

import { api } from '../api/client'
import TagManagerList from '../components/TagManagerList'
import SessionActionsMenu from '../components/SessionActionsMenu'
import { TagPopoverProvider } from '../hooks/useTagPopover'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent } from '../components/ui/dropdown-menu'

function renderList(props: React.ComponentProps<typeof TagManagerList>) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <TagManagerList {...props} />
    </QueryClientProvider>,
  )
}

beforeEach(() => vi.clearAllMocks())

describe('TagManagerList — shared CRUD (both modes)', () => {
  it('renames a tag on blur (persists only a changed, non-empty value)', async () => {
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-name-t1')
    fireEvent.change(input, { target: { value: 'Renamed' } })
    fireEvent.blur(input)
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { name: 'Renamed' }))
  })

  it('reverts the rename on Escape and restores on empty blur (no persist)', async () => {
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-name-t1') as HTMLInputElement
    // Escape reverts the in-progress edit to the canonical name without persisting.
    fireEvent.change(input, { target: { value: 'Scratch' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input.value).toBe('Alpha')
    // A blank blur restores the name rather than leaving an empty row, and never persists.
    fireEvent.change(input, { target: { value: '   ' } })
    fireEvent.blur(input)
    expect(input.value).toBe('Alpha')
    expect(api.updateChatTag).not.toHaveBeenCalled()
  })

  it('toggles a tag\'s status flag', async () => {
    renderList({ mode: 'manage' })
    fireEvent.click(await screen.findByTestId('tag-status-t1')) // t1 has no status → turn on
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { status: true }))
    fireEvent.click(screen.getByTestId('tag-status-t2')) // t2 is a status tag → turn off
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t2', { status: false }))
  })

  it('deletes a tag only after confirm', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    renderList({ mode: 'manage' })
    const del = await screen.findByTestId('tag-delete-t1')

    confirmSpy.mockReturnValueOnce(false)
    fireEvent.click(del)
    expect(api.deleteChatTag).not.toHaveBeenCalled()

    confirmSpy.mockReturnValueOnce(true)
    fireEvent.click(del)
    await waitFor(() => expect(api.deleteChatTag).toHaveBeenCalledWith('t1'))
    confirmSpy.mockRestore()
  })

  it('creates a tag on Enter and clears the input', async () => {
    renderList({ mode: 'manage' })
    const input = await screen.findByTestId('tag-create') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'Gamma' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.createChatTag).toHaveBeenCalledWith('Gamma', undefined, undefined))
    expect(input.value).toBe('')
  })
})

describe('TagManagerList — manage mode', () => {
  it('renders swatches as colour buttons, not filter checkboxes', async () => {
    renderList({ mode: 'manage' })
    await screen.findByTestId('tag-row-t1')
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.getByTestId('tag-color-t1')).toHaveAttribute('aria-expanded', 'false')
  })

  it('opens the palette from the swatch and PATCHes the picked colour', async () => {
    renderList({ mode: 'manage' })
    const swatch = await screen.findByTestId('tag-color-t1')
    fireEvent.click(swatch)
    expect(swatch).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('tag-palette-t1')).toBeInTheDocument()
    // Pick green from the shared folder palette.
    fireEvent.click(screen.getByTestId('tag-color-t1-22c55e'))
    await waitFor(() => expect(api.updateChatTag).toHaveBeenCalledWith('t1', { color: '#22c55e' }))
    // Palette closes and focus returns to the swatch (not <body>).
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(document.activeElement).toBe(swatch)
  })

  it('marks the tag\'s current colour as pressed in the palette', async () => {
    renderList({ mode: 'manage' })
    // t1 is #ff0000 (not in the palette) — nothing pressed.
    fireEvent.click(await screen.findByTestId('tag-color-t1'))
    const pressed = screen.getByTestId('tag-palette-t1').querySelectorAll('[aria-pressed="true"]')
    expect(pressed.length).toBe(0)
  })

  it('Escape closes the palette without persisting and refocuses the swatch', async () => {
    renderList({ mode: 'manage' })
    const swatch = await screen.findByTestId('tag-color-t1')
    fireEvent.click(swatch)
    fireEvent.keyDown(screen.getByTestId('tag-palette-t1'), { key: 'Escape' })
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(api.updateChatTag).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(swatch)
  })

  it('only one palette is open at a time (opening another closes the first)', async () => {
    renderList({ mode: 'manage' })
    fireEvent.click(await screen.findByTestId('tag-color-t1'))
    fireEvent.click(screen.getByTestId('tag-color-t2'))
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(screen.getByTestId('tag-palette-t2')).toBeInTheDocument()
  })
})

describe('TagManagerList — colour palette isolation', () => {
  it('column-filter mode never renders the colour trigger or palette', async () => {
    renderList({ mode: 'column-filter', selectedIds: [], onToggleTag: vi.fn() })
    await screen.findByTestId('tag-row-t1')
    expect(screen.queryByTestId('tag-color-t1')).toBeNull()
    // Clicking the filter checkbox must not open a palette either.
    fireEvent.click(screen.getByLabelText('Include Alpha in filter'))
    expect(screen.queryByTestId('tag-palette-t1')).toBeNull()
    expect(api.updateChatTag).not.toHaveBeenCalled()
  })
})

describe('TagManagerList — column-filter mode', () => {
  it('swatches are include/exclude checkboxes reflecting selectedIds', async () => {
    renderList({ mode: 'column-filter', selectedIds: ['t2'], onToggleTag: vi.fn() })
    await screen.findByTestId('tag-row-t1')
    // t2 is selected → aria-checked; t1 is not.
    expect(screen.getByLabelText('Include Beta in filter')).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('Include Alpha in filter')).toHaveAttribute('aria-checked', 'false')
  })

  it('toggling a swatch calls onToggleTag with the composed next id list', async () => {
    const onToggleTag = vi.fn()
    renderList({ mode: 'column-filter', selectedIds: ['t2'], onToggleTag })
    fireEvent.click(await screen.findByLabelText('Include Alpha in filter')) // add t1
    expect(onToggleTag).toHaveBeenCalledWith('t1', ['t2', 't1'])
    fireEvent.click(screen.getByLabelText('Include Beta in filter')) // remove t2
    expect(onToggleTag).toHaveBeenCalledWith('t2', [])
  })

  it('uses the provided createTestId for the new-tag input', async () => {
    renderList({ mode: 'column-filter', selectedIds: [], onToggleTag: vi.fn(), createTestId: 'tag-create-col-9' })
    expect(await screen.findByTestId('tag-create-col-9')).toBeInTheDocument()
  })
})

describe('SessionActionsMenu — Tags… item (list-view regression)', () => {
  it('renders the "Tags…" item even when tagColumnsEnabled is false', async () => {
    const store = createTestStore({
      dashboard: {
        status: {}, connected: true, approvalMode: 'normal', channelTrusted: false,
        refreshTrigger: 0, unreadSlots: [], slotsLoaded: true, updateProgress: null,
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
        sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
        slots: [{ key: 'chat-1', title: 'S1' }],
      } as any,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <TagPopoverProvider>
                <DropdownMenu>
                  <DropdownMenuTrigger asChild><button aria-label="open">open</button></DropdownMenuTrigger>
                  <DropdownMenuContent><SessionActionsMenu variant="dropdown" slotKey="chat-1" /></DropdownMenuContent>
                </DropdownMenu>
              </TagPopoverProvider>
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
    fireEvent.keyDown(screen.getByLabelText('open'), { key: 'Enter' })
    expect(await screen.findByText('Tags…')).toBeInTheDocument()
  })
})
