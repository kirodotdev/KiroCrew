/**
 * Tests for the connected SlotTagPopover (session-menu unification). The per-slot
 * tag picker is the single app-wide popover; which slot is open comes from the
 * ChatPage-scoped TagPopover context (useTagPopover().open / .close), seeded here
 * via TagPopoverProvider initialSlotKey. It fetches the workspace tags itself
 * (['chat-tags']) and persists via api.setSlotTags.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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
import SlotTagPopover from '../components/SlotTagPopover'
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
