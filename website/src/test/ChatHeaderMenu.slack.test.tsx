/**
 * Tests for connected-surface actions in the shared session menu.
 * LinkedSurfacesSection is keyed on slotKey and rendered by
 * SessionActionsMenu, so this exercises it through ChatHeaderMenu with the
 * slot seeded in the store and the shared channel queries mocked.
 *
 * Verifies the symmetric Slack contract remains unchanged and that a Discord
 * origin is labelled Discord without inheriting any Slack action.
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
    unlinkSlack: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
    slackLink: vi.fn().mockResolvedValue({ ok: true }),
    unlinkMirror: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
    remindMirror: vi.fn().mockResolvedValue({ ok: true, already_linked: true }),
    // LinkedSurfacesSection fetches the workspace channel list internally now.
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

import type { RootState } from '../store'
import type { ChatSlot } from '../types'
import { api } from '../api/client'
import { ChatHeaderMenu } from '../pages/ChatPage'

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as RootState['dashboard']

/**
 * Seed the slot into the store's slots[] (LinkedSurfacesSection reads live link
 * state there, and updateSlot only mutates an existing slot), then render the
 * render the header menu and open it. No slack props — the section is connected.
 */
function renderMenu(slot: Partial<ChatSlot> & { key: string }) {
  const store = createTestStore({ dashboard: { ...dashboardState, slots: [{ ...slot }] } })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatHeaderMenu activeSlot={slot.key} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // Open the ⋯ menu. The trigger is a Radix DropdownMenuTrigger, which opens on
  // keyboard activation (Enter) — a path jsdom handles, unlike the
  // PointerEvent-driven click Radix uses for mouse opens.
  fireEvent.keyDown(utils.container.querySelector('button')!, { key: 'Enter' })
  return { store, ...utils }
}

beforeEach(() => vi.clearAllMocks())

describe('Session menu — Slack link/unlink (connected)', () => {
  it('linked menu shows Unlink + Post reminder and hides Send to Slack', async () => {
    renderMenu({ key: 'chat-1-100', slack_linked: true })
    expect(await screen.findByText('Unlink from Slack')).toBeInTheDocument()
    expect(screen.getByText('Post reminder in Slack')).toBeInTheDocument()
    expect(screen.queryByText('Send to Slack')).not.toBeInTheDocument()
  })

  it('unlinked menu shows Send to Slack and hides Unlink', async () => {
    renderMenu({ key: 'chat-1-100', slack_linked: false })
    // "Send to Slack" appears only once the channel list resolves.
    expect(await screen.findByText('Send to Slack')).toBeInTheDocument()
    expect(screen.queryByText('Unlink from Slack')).not.toBeInTheDocument()
    expect(screen.queryByText('Post reminder in Slack')).not.toBeInTheDocument()
  })

  it('clicking Unlink calls api.unlinkSlack and clears the link in the store', async () => {
    const { store } = renderMenu({ key: 'chat-1-100', slack_linked: true, slack_channel: 'C-1', slack_thread_ts: 'ts-1' })

    fireEvent.click(await screen.findByText('Unlink from Slack'))

    await waitFor(() => expect(api.unlinkSlack).toHaveBeenCalledWith('chat-1-100'))
    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find((s: ChatSlot) => s.key === 'chat-1-100')
      expect(slot?.slack_linked).toBe(false)
      // All three link fields cleared, not just the flag.
      expect(slot?.slack_channel).toBeUndefined()
      expect(slot?.slack_thread_ts).toBeUndefined()
    })
  })

  it('clicking Unlink live-swaps the menu to Send to Slack on the same tree', async () => {
    // The section is store-connected, so the optimistic updateSlot re-renders it.
    const { container } = renderMenu({ key: 'chat-1-100', slack_linked: true })
    fireEvent.click(await screen.findByText('Unlink from Slack'))
    await waitFor(() => expect(api.unlinkSlack).toHaveBeenCalled())

    // The select also closed the menu; reopen via the ⋯ toggle (the only button
    // left in the tree) and assert the symmetric swap.
    fireEvent.keyDown(container.querySelector('button')!, { key: 'Enter' })
    await waitFor(() => {
      expect(screen.getByText('Send to Slack')).toBeInTheDocument()
      expect(screen.queryByText('Unlink from Slack')).not.toBeInTheDocument()
    })
  })
})

describe('Session menu — channel-neutral connected surfaces', () => {
  it('shows a Discord origin badge without Slack or disconnect actions', async () => {
    renderMenu({
      key: 'discord-session',
      slack_linked: false,
      links: [{
        channel: 'discord',
        label: 'Discord DM',
        target: '…767244',
        direction: 'origin',
        live: true,
      }],
    })

    expect(await screen.findByText('Connected: Discord DM')).toBeInTheDocument()
    expect(screen.getByText('Origin')).toBeInTheDocument()
    expect(screen.queryByText(/Slack/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Stop mirroring/)).not.toBeInTheDocument()
    expect(api.unlinkMirror).not.toHaveBeenCalled()
  })
})


describe('Session menu — outbound mirror actions', () => {
  it('offers channel-scoped reminder and stop actions for a live Discord mirror', async () => {
    renderMenu({
      key: 'mirrored-session',
      slack_linked: false,
      links: [{
        channel: 'discord',
        label: 'Discord DM',
        target: '…767244',
        direction: 'out',
        live: true,
      }],
    })

    expect(await screen.findByText('Post reminder in Discord DM')).toBeInTheDocument()
    const stop = screen.getByText('Stop mirroring to Discord DM')
    expect(screen.queryByText(/Slack/)).not.toBeInTheDocument()

    fireEvent.click(stop)
    await waitFor(() => expect(api.unlinkMirror).toHaveBeenCalledWith('mirrored-session'))
  })
})
