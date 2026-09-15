/**
 * Tests for the connected SlotTagPopover (session-menu unification). The per-slot
 * tag picker is the single app-wide popover; which slot is open comes from the
 * ChatPage-scoped TagPopover context (useTagPopover().open / .close), seeded here
 * via TagPopoverProvider initialSlotKey. It fetches the workspace tags itself
 * (['chat-tags']) and persists via api.setSlotTags.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem } from '../components/ui/context-menu'

vi.mock('../api/client', () => ({
  api: {
    chatTags: vi.fn().mockResolvedValue([
      { id: 't1', name: 'Alpha', color: '#ff0000', order: 0 },
      { id: 't2', name: 'Beta', color: '#00ff00', order: 1 },
    ]),
    setSlotTags: vi.fn().mockResolvedValue({ ok: true }),
    createChatTag: vi.fn().mockResolvedValue({ ok: true }),
  },
}))

import { api } from '../api/client'
import SlotTagPopover, { elementBeneath } from '../components/SlotTagPopover'
import { TagPopoverProvider } from '../hooks/useTagPopover'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], slotsLoaded: true, updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
  enabledAppIds: [],
} as unknown as RootState['dashboard']

/**
 * Seed the open slot via TagPopoverProvider (the context owns open-state);
 * the slot's tags still live in the Redux store, so `slots` seeds those.
 */
function renderPopover({ slotKey, slots = [] }: { slotKey: string | null; slots?: Partial<ChatSlot>[] }) {
  const store = createTestStore({ dashboard: { ...dashboardState, slots } as unknown as RootState['dashboard'] })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <TagPopoverProvider initialSlotKey={slotKey}>
              <SlotTagPopover />
            </TagPopoverProvider>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...utils }
}

beforeEach(() => vi.clearAllMocks())

describe('SlotTagPopover (connected)', () => {
  it('renders nothing when no slot is targeted', () => {
    renderPopover({ slotKey: null })
    expect(screen.queryByTestId('slot-tag-picker')).not.toBeInTheDocument()
  })

  it('renders the picker with the workspace tags when a slot is targeted', async () => {
    renderPopover({ slotKey: 'chat-1-100', slots: [{ key: 'chat-1-100', tags: [] }] })
    expect(await screen.findByTestId('slot-tag-picker')).toBeInTheDocument()
    expect(await screen.findByText('Alpha')).toBeInTheDocument()
    expect(screen.getByText('Beta')).toBeInTheDocument()
  })

  it('toggling an unchecked tag persists via api.setSlotTags composed onto the current list', async () => {
    renderPopover({ slotKey: 'chat-1-100', slots: [{ key: 'chat-1-100', tags: ['t2'] }] })
    fireEvent.click(await screen.findByText('Alpha'))
    await waitFor(() => expect(api.setSlotTags).toHaveBeenCalledWith('chat-1-100', ['t2', 't1']))
  })

  it('clicking a checked tag removes it', async () => {
    renderPopover({ slotKey: 'chat-1-100', slots: [{ key: 'chat-1-100', tags: ['t1', 't2'] }] })
    fireEvent.click(await screen.findByText('Alpha'))
    await waitFor(() => expect(api.setSlotTags).toHaveBeenCalledWith('chat-1-100', ['t2']))
  })

  it('closing via the X button unmounts the picker (context clears the open slot)', async () => {
    renderPopover({ slotKey: 'chat-1-100', slots: [{ key: 'chat-1-100', tags: [] }] })
    await screen.findByTestId('slot-tag-picker')
    fireEvent.click(screen.getByLabelText('Close'))
    await waitFor(() => expect(screen.queryByTestId('slot-tag-picker')).not.toBeInTheDocument())
  })
})

/**
 * Right-click while the picker is open. The picker's full-viewport backdrop
 * sits over every session row, so without forwarding the row's Radix
 * ContextMenuTrigger never sees the `contextmenu` and the OS/Electron menu pops
 * instead. The contract: suppress the native menu, close the picker, and re-issue
 * the gesture to the element under the pointer so its own menu opens fresh.
 */
describe('SlotTagPopover — right-click on the backdrop', () => {
  const realElementsFromPoint = document.elementsFromPoint
  afterEach(() => {
    // happy-dom has no elementsFromPoint; restore whatever was there (undefined).
    if (realElementsFromPoint) document.elementsFromPoint = realElementsFromPoint
    else delete (document as unknown as { elementsFromPoint?: unknown }).elementsFromPoint
  })

  /** Render the picker over a sibling session row that owns a Radix context menu. */
  function renderOverRow(onRowContextMenu?: (e: React.MouseEvent) => void) {
    const store = createTestStore({ dashboard: { ...dashboardState, slots: [{ key: 'chat-1-100', tags: [] }] } as unknown as RootState['dashboard'] })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter>
              <ContextMenu>
                <ContextMenuTrigger asChild>
                  {/* Same shape as a real session row: a focusable button-role div. */}
                  <div role="button" tabIndex={0} data-testid="other-row" onContextMenu={onRowContextMenu}>Other session</div>
                </ContextMenuTrigger>
                <ContextMenuContent data-testid="other-row-menu">
                  <ContextMenuItem>Close session</ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
              <TagPopoverProvider initialSlotKey="chat-1-100">
                <SlotTagPopover />
              </TagPopoverProvider>
            </MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  }

  /** Make the (layout-less) test DOM report `row` as sitting under `backdrop`. */
  function stubPointHit(backdrop: Element, row: Element) {
    document.elementsFromPoint = vi.fn(() => [backdrop, row, document.body, document.documentElement])
  }

  it('suppresses the native menu, closes the picker, and opens the context menu of the row beneath', async () => {
    const rowSpy = vi.fn()
    renderOverRow(rowSpy)
    await screen.findByTestId('slot-tag-picker')
    const backdrop = screen.getByLabelText('Close tag picker')
    const row = screen.getByTestId('other-row')
    stubPointHit(backdrop, row)

    const native = createEvent.contextMenu(backdrop, { clientX: 40, clientY: 120, button: 2 })
    fireEvent(backdrop, native)

    // 1. the native browser/Electron menu is suppressed on the original gesture
    expect(native.defaultPrevented).toBe(true)
    // 2. the picker is dismissed
    await waitFor(() => expect(screen.queryByTestId('slot-tag-picker')).not.toBeInTheDocument())
    // 3. the row under the pointer received the same gesture at the same point…
    expect(rowSpy).toHaveBeenCalledTimes(1)
    const forwarded = rowSpy.mock.calls[0][0] as React.MouseEvent
    expect(forwarded.clientX).toBe(40)
    expect(forwarded.clientY).toBe(120)
    expect(forwarded.button).toBe(2)
    // …and its Radix context menu is open
    expect(await screen.findByTestId('other-row-menu')).toBeInTheDocument()
    expect(screen.getByText('Close session')).toBeInTheDocument()
  })

  it('still closes (and suppresses the native menu) when nothing selectable is beneath', async () => {
    renderOverRow()
    await screen.findByTestId('slot-tag-picker')
    const backdrop = screen.getByLabelText('Close tag picker')
    document.elementsFromPoint = vi.fn(() => [backdrop, document.body, document.documentElement])

    const native = createEvent.contextMenu(backdrop, { clientX: 10, clientY: 10, button: 2 })
    fireEvent(backdrop, native)

    expect(native.defaultPrevented).toBe(true)
    await waitFor(() => expect(screen.queryByTestId('slot-tag-picker')).not.toBeInTheDocument())
    expect(screen.queryByTestId('other-row-menu')).not.toBeInTheDocument()
  })

  it('degrades to a plain dismiss when the platform lacks elementsFromPoint', async () => {
    renderOverRow()
    await screen.findByTestId('slot-tag-picker')
    const backdrop = screen.getByLabelText('Close tag picker')
    delete (document as unknown as { elementsFromPoint?: unknown }).elementsFromPoint

    const native = createEvent.contextMenu(backdrop, { clientX: 10, clientY: 10, button: 2 })
    expect(() => fireEvent(backdrop, native)).not.toThrow()
    expect(native.defaultPrevented).toBe(true)
    await waitFor(() => expect(screen.queryByTestId('slot-tag-picker')).not.toBeInTheDocument())
  })

  it('a right-click inside the dialog keeps the picker open and its native menu', async () => {
    const rowSpy = vi.fn()
    renderOverRow(rowSpy)
    await screen.findByTestId('slot-tag-picker')
    const backdrop = screen.getByLabelText('Close tag picker')
    stubPointHit(backdrop, screen.getByTestId('other-row'))

    const input = screen.getByPlaceholderText('New tag…')
    const native = createEvent.contextMenu(input, { clientX: 300, clientY: 200, button: 2 })
    fireEvent(input, native)

    expect(native.defaultPrevented).toBe(false)
    expect(screen.getByTestId('slot-tag-picker')).toBeInTheDocument()
    expect(rowSpy).not.toHaveBeenCalled()
  })
})

describe('elementBeneath', () => {
  const realElementsFromPoint = document.elementsFromPoint
  afterEach(() => {
    if (realElementsFromPoint) document.elementsFromPoint = realElementsFromPoint
    else delete (document as unknown as { elementsFromPoint?: unknown }).elementsFromPoint
  })

  it('skips the backdrop and its descendants and returns the first other hit', () => {
    const backdrop = document.createElement('div')
    const inner = document.createElement('div')
    backdrop.appendChild(inner)
    const row = document.createElement('div')
    document.elementsFromPoint = vi.fn(() => [inner, backdrop, row, document.body])
    expect(elementBeneath(backdrop, 1, 1)).toBe(row)
    expect(document.elementsFromPoint).toHaveBeenCalledWith(1, 1)
  })

  it('returns null when only the backdrop is hit or the API is missing', () => {
    const backdrop = document.createElement('div')
    document.elementsFromPoint = vi.fn(() => [backdrop])
    expect(elementBeneath(backdrop, 1, 1)).toBeNull()
    delete (document as unknown as { elementsFromPoint?: unknown }).elementsFromPoint
    expect(elementBeneath(backdrop, 1, 1)).toBeNull()
  })
})
