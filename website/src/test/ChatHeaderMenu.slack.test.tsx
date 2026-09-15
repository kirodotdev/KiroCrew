/**
 * Tests for the channel connect/disconnect control in the shared session menu.
 * LinkedSurfacesSection is keyed on slotKey and rendered by SessionActionsMenu,
 * so this exercises it through ChatHeaderMenu with the slot seeded in the store
 * and the shared channel queries mocked.
 *
 * The contract under test: ONE row per channel whose label is the action, two
 * states only, and none of the internal routing vocabulary. Rows come from the
 * wire's `links` — the component never invents one from `slack_linked`, because
 * an invented row cannot know `paused` and so rendered a disconnected channel as
 * connected.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(message: string, status = 500, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  },
  api: {
    unlinkSlack: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
    slackLink: vi.fn().mockResolvedValue({ ok: true }),
    pauseSlack: vi.fn().mockResolvedValue({ ok: true, was_paused: false }),
    pauseMirror: vi.fn().mockResolvedValue({ ok: true, was_paused: false }),
    unlinkMirror: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
    linkMirror: vi.fn().mockResolvedValue({ ok: true, conversation_id: 'dm-42' }),
    channelTargets: vi.fn().mockResolvedValue([{
      channel_type: 'slack',
      target_id: 'dm',
      label: 'Slack · Direct Message',
      available: true,
      unavailable_reason: '',
    }]),
    slackChannels: vi.fn().mockResolvedValue([]),
    mcpActive: vi.fn().mockResolvedValue([]),
    setSlotColor: vi.fn().mockResolvedValue({}),
    chatFolders: vi.fn().mockResolvedValue([]),
  },
}))

import type { RootState } from '../store'
import type { ChatSlot, SessionLink } from '../types'
import { api } from '../api/client'
import { ChatHeaderMenu } from '../pages/chat/ChatPageMessageContent'

const dashboardState = {
  status: {}, connected: true, slots: [], approvalMode: 'normal',
  channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
  subagentRunning: {}, subagentDetails: {}, subagentText: {},
  sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
} as RootState['dashboard']

/** A wire link row, with the fields a caller does not care about defaulted. */
function link(over: Partial<SessionLink> & { channel: string }): SessionLink {
  return { label: over.channel, target: '…1234', direction: 'out', live: true, ...over }
}

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

const rowOf = (slot: { links?: SessionLink[] }, channel: string) => (
  (slot.links ?? []).find(l => l.channel === channel)
)

beforeEach(() => vi.clearAllMocks())

describe('Session menu — one row per channel, two states', () => {
  it('a connected channel offers only Disconnect, and none of the old vocabulary', async () => {
    renderMenu({
      key: 'chat-1-100',
      slack_linked: true,
      links: [link({ channel: 'slack', label: 'Slack' })],
    })

    expect(await screen.findByText('Disconnect from Slack')).toBeInTheDocument()
    expect(screen.queryByText('Connect to Slack')).not.toBeInTheDocument()
    // The whole point of the change: no badge, no header, no secondary action.
    for (const gone of [
      /^Origin$/, /^Mirror$/, /^Two-way$/, /^Offline$/, /Connected:/,
      /Post reminder/, /Unlink from Slack/, /Stop mirroring/, /^Release/,
    ]) {
      expect(screen.queryByText(gone)).not.toBeInTheDocument()
    }
  })

  it('a disconnected channel offers Connect on the same single row', async () => {
    renderMenu({
      key: 'chat-1-100',
      slack_linked: true,
      links: [link({ channel: 'slack', label: 'Slack', paused: true })],
    })

    expect(await screen.findByText('Connect to Slack')).toBeInTheDocument()
    expect(screen.queryByText('Disconnect from Slack')).not.toBeInTheDocument()
  })

  it('disconnecting sets delivery off and flips the row without closing the menu', async () => {
    const { store } = renderMenu({
      key: 'chat-1-100',
      slack_linked: true,
      links: [link({ channel: 'slack', label: 'Slack' })],
    })

    fireEvent.click(await screen.findByText('Disconnect from Slack'))

    await waitFor(() => expect(api.pauseSlack).toHaveBeenCalledWith('chat-1-100', true))
    // The row is patched in place — the binding is retained, never dropped.
    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find((s: ChatSlot) => s.key === 'chat-1-100')
      expect(rowOf(slot!, 'slack')?.paused).toBe(true)
      expect(slot?.links).toHaveLength(1)
    })
    // Menu stays open so the verb flip is visible: the row IS the state display.
    expect(await screen.findByText('Connect to Slack')).toBeInTheDocument()
  })

  it('reconnecting a disconnected channel sets delivery back on', async () => {
    const { store } = renderMenu({
      key: 'chat-1-100',
      slack_linked: true,
      links: [link({ channel: 'slack', label: 'Slack', paused: true })],
    })

    fireEvent.click(await screen.findByText('Connect to Slack'))

    await waitFor(() => expect(api.pauseSlack).toHaveBeenCalledWith('chat-1-100', false))
    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find((s: ChatSlot) => s.key === 'chat-1-100')
      expect(rowOf(slot!, 'slack')?.paused).toBe(false)
    })
  })
})

describe('Session menu — the conversation a session was born in', () => {
  it('renders ONE row when the wire reports the same channel twice', async () => {
    // A session BORN in Discord that is then mirrored to Discord carries two
    // links for the one channel: an `origin` fact and a `mirror` fact. Rendering
    // both produced two Discord controls sharing one piece of state — exactly the
    // confusion this menu replaced. The row is labelled from the explicit binding,
    // because that is the real target while an origin's coordinates are
    // provenance; the click still covers both, which the next test pins.
    renderMenu({
      key: 'discord-session',
      slack_linked: false,
      links: [
        link({ channel: 'discord', label: 'Discord DM', direction: 'origin' }),
        link({ channel: 'discord', label: 'Discord DM', direction: 'out' }),
      ],
    })

    expect(await screen.findByText('Disconnect from Discord')).toBeInTheDocument()
    expect(screen.getAllByText('Disconnect from Discord')).toHaveLength(1)
  })

  it('acts on BOTH deliveries when one channel carries two', async () => {
    // The two deliveries hold SEPARATE flags. Showing one row and then acting on
    // only one of them left the other with no control anywhere on screen — it
    // could be muted with nothing able to unmute it. The channel is the unit the
    // user is choosing about, so one click changes all of it.
    renderMenu({
      key: 'discord-session',
      slack_linked: false,
      links: [
        link({ channel: 'discord', label: 'Discord DM', direction: 'origin' }),
        link({ channel: 'discord', label: 'Discord DM', direction: 'out' }),
      ],
    })

    fireEvent.click(await screen.findByText('Disconnect from Discord'))

    await waitFor(() => expect(api.pauseMirror).toHaveBeenCalledTimes(2))
    const calls = vi.mocked(api.pauseMirror).mock.calls
    // One call per delivery, addressed by role, never the same one twice.
    expect(calls.map(call => call[2]).sort()).toEqual([false, true])
    // Every call is a DISCONNECT: the row read connected, so the whole channel stops.
    expect(calls.every(call => call[1] === true)).toBe(true)
  })

  it('reads as connected while ANY delivery on the channel is still live', async () => {
    // A mixed group can only arise from a partial failure or pre-existing data.
    // Reporting `Connect` there would be a lie while messages were still arriving,
    // and clicking it would have RESUMED the live one instead of stopping it.
    renderMenu({
      key: 'discord-session',
      slack_linked: false,
      links: [
        link({ channel: 'discord', label: 'Discord DM', direction: 'origin', paused: true }),
        link({ channel: 'discord', label: 'Discord DM', direction: 'out' }),
      ],
    })

    expect(await screen.findByText('Disconnect from Discord')).toBeInTheDocument()
    expect(screen.queryByText('Connect to Discord')).not.toBeInTheDocument()
  })

  it('offers Disconnect for an origin channel, so it can stop syndicating there', async () => {
    // Previously an origin rendered a read-only badge with no control at all —
    // the last carve-out where a channel could not be turned off.
    renderMenu({
      key: 'discord-session',
      slack_linked: false,
      links: [link({ channel: 'discord', label: 'Discord DM', direction: 'origin' })],
    })

    expect(await screen.findByText('Disconnect from Discord')).toBeInTheDocument()
    expect(screen.queryByText('Origin')).not.toBeInTheDocument()
    expect(screen.queryByText('Connected: Discord DM')).not.toBeInTheDocument()
  })

  it('disconnects an origin channel through the channel-neutral endpoint', async () => {
    renderMenu({
      key: 'discord-session',
      slack_linked: false,
      links: [link({ channel: 'discord', label: 'Discord DM', direction: 'origin' })],
    })

    fireEvent.click(await screen.findByText('Disconnect from Discord'))
    // The third argument marks this as the BORN-IN conversation rather than an
    // explicit mirror. They are separate flags on the backend; see the next test.
    await waitFor(() => expect(api.pauseMirror).toHaveBeenCalledWith('discord-session', true, true))
    // Disconnect is never an unlink: nothing severs the binding.
    expect(api.unlinkMirror).not.toHaveBeenCalled()
  })

  it('disconnects two non-Slack channels independently', async () => {
    // A session BORN in Discord that ALSO mirrors to Telegram draws two rows.
    // Both once read and wrote ONE shared flag, so disconnecting either silently
    // disconnected the other — the row the user never touched went quiet with it.
    // Each row now names its own delivery, which is what keeps them independent.
    renderMenu({
      key: 'discord-session',
      slack_linked: false,
      links: [
        link({ channel: 'discord', label: 'Discord DM', direction: 'origin' }),
        link({ channel: 'telegram', label: 'Telegram', direction: 'out' }),
      ],
    })

    fireEvent.click(await screen.findByText('Disconnect from Telegram'))
    await waitFor(() => expect(api.pauseMirror).toHaveBeenCalledWith('discord-session', true, false))

    fireEvent.click(await screen.findByText('Disconnect from Discord'))
    await waitFor(() => expect(api.pauseMirror).toHaveBeenCalledWith('discord-session', true, true))

    // Two distinct deliveries addressed, never the same one twice.
    const origins = vi.mocked(api.pauseMirror).mock.calls.map(call => call[2])
    expect(origins).toEqual([false, true])
  })

  it('labels a two-way binding identically — direction is not user-facing', async () => {
    renderMenu({
      key: 'resumed-session',
      slack_linked: false,
      links: [link({ channel: 'discord', label: 'Discord DM', direction: 'both' })],
    })

    expect(await screen.findByText('Disconnect from Discord')).toBeInTheDocument()
    expect(screen.queryByText('Two-way')).not.toBeInTheDocument()
  })
})

describe('Session menu — independence and offers', () => {
  it('a disconnect in flight on one channel does not freeze another row', async () => {
    // The mutations are shared across rows, so keying the guard on `isPending`
    // froze every sibling while one row was mid-flight — which contradicts rows
    // the design makes independently mutable.
    let releaseSlack: ((v: unknown) => void) | undefined
    vi.mocked(api.pauseSlack).mockImplementationOnce(
      () => new Promise(resolve => { releaseSlack = resolve }),
    )
    renderMenu({
      key: 'both-session',
      slack_linked: true,
      links: [
        link({ channel: 'slack', label: 'Slack' }),
        link({ channel: 'discord', label: 'Discord DM' }),
      ],
    })

    fireEvent.click(await screen.findByText('Disconnect from Slack'))
    await waitFor(() => expect(api.pauseSlack).toHaveBeenCalled())

    // Slack is still in flight; the Discord row must still accept a click.
    fireEvent.click(screen.getByText('Disconnect from Discord'))
    await waitFor(() => expect(api.pauseMirror).toHaveBeenCalledWith('both-session', true, false))

    releaseSlack?.({ ok: true, was_paused: false })
  })

  it('offers a channel the session does not hold, and links it on click', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'discord',
      target_id: 'user:42',
      label: 'Discord DM · 42',
      available: true,
      unavailable_reason: '',
    }])
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    // Named by DESTINATION, not by brand — see the next test for why.
    fireEvent.click(await screen.findByText('Connect to Discord DM · 42'))

    await waitFor(() => expect(api.linkMirror).toHaveBeenCalledWith(
      'chat-1-100', 'discord', 'user:42',
    ))
  })

  it('names each destination so two offers on one channel are distinguishable', async () => {
    // Every offer once carried the BRAND label, so two Slack destinations both
    // read "Connect to Slack" and clicking one backfilled this session's
    // transcript to a conversation the user was never shown.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([
      {
        channel_type: 'slack',
        target_id: 'C-eng',
        label: 'Slack · #eng',
        available: true,
        unavailable_reason: '',
      },
      {
        channel_type: 'discord',
        target_id: 'user:7',
        label: 'Discord · Direct Message',
        available: true,
        unavailable_reason: '',
      },
    ])
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    expect(await screen.findByText('Connect to Slack · #eng')).toBeInTheDocument()
    expect(screen.getByText('Connect to Discord · Direct Message')).toBeInTheDocument()
    // The bare brand label would hide which conversation is being offered.
    expect(screen.queryByText('Connect to Slack')).not.toBeInTheDocument()
  })

  it('does not offer a second conversation on a channel it already holds', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'discord',
      target_id: 'user:99',
      label: 'Discord DM · 99',
      available: true,
      unavailable_reason: '',
    }])
    renderMenu({
      key: 'chat-1-100',
      slack_linked: false,
      links: [link({ channel: 'discord', label: 'Discord DM' })],
    })

    // One Discord row, and it is the binding's — not an offer for another.
    await waitFor(() => expect(screen.getAllByText(/Discord/)).toHaveLength(1))
    expect(screen.getByText('Disconnect from Discord')).toBeInTheDocument()
  })

  it('keeps an unconnectable channel focusable and explains why, without a badge', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'wecom',
      target_id: 'configured',
      label: 'WeCom · Configured account',
      available: false,
      unavailable_reason: 'WeCom can only reply to an inbound message.',
    }])
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    const row = await screen.findByText('Connect to WeCom · Configured account')
    const item = row.closest('[role="menuitem"]')
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(item).toHaveAttribute('title', 'WeCom can only reply to an inbound message.')
    // VISIBLE, not only in `title`. A keyboard or touch user never sees a tooltip,
    // so a dimmed row whose reason lived only there was a control that refused to
    // work with no discoverable why — and the reason is what gates the task.
    expect(
      screen.getByText('WeCom can only reply to an inbound message.'),
    ).toBeInTheDocument()

    fireEvent.click(row)
    expect(api.linkMirror).not.toHaveBeenCalled()
  })
})
