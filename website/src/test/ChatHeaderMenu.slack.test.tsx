/**
 * Tests for connected-surface actions in the shared session menu.
 * LinkedSurfacesSection is keyed on slotKey and rendered by
 * SessionActionsMenu, so this exercises it through ChatHeaderMenu with the
 * slot seeded in the store and the shared channel queries mocked.
 *
 * Verifies the Slack control is exactly ONE toggle row in every state, and that
 * a Discord origin is labelled Discord without inheriting any Slack action.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', async (importOriginal) => ({
  // Keep the REAL ApiError class: the occupied-conversation branch narrows with
  // `instanceof ApiError`, so a stubbed stand-in would make that check fail and
  // the confirm dialog silently unreachable in the test as well as in the app.
  ApiError: (await importOriginal<typeof import('../api/client')>()).ApiError,
  api: {
    unlinkSlack: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
    pauseSlack: vi.fn().mockResolvedValue({ ok: true, was_paused: false }),
    slackLink: vi.fn().mockResolvedValue({ ok: true }),
    unlinkMirror: vi.fn().mockResolvedValue({ ok: true, was_linked: true }),
    linkMirror: vi.fn().mockResolvedValue({ ok: true, conversation_id: 'dm-42' }),
    reconnectMirror: vi.fn().mockResolvedValue({ ok: true, reconnected: true }),
    pauseMirror: vi.fn().mockResolvedValue({ ok: true, was_paused: false }),
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
import type { ChatSlot } from '../types'
import { ApiError, api } from '../api/client'
import { ChatHeaderMenu } from '../pages/ChatPage'
import { patchSlotLink } from '../store/dashboardSlice'

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
function renderMenu(
  slot: Partial<ChatSlot> & { key: string },
  { mirrorWire = true }: { mirrorWire?: boolean } = {},
) {
  // Mirror the wire: `_slot_links` emits a Slack ROW on exactly the condition it
  // reports `slack_linked`, and that row carries `paused`. The component
  // deliberately no longer synthesizes one (a synthesized row cannot know the
  // mute, so a muted thread rendered as connected), so a fixture that sets
  // `slack_linked` without a row is not a shape the backend can produce. Tests
  // that need a MUTED or otherwise specific Slack row pass it explicitly and this
  // leaves it alone. `mirrorWire: false` opts out, to assert the component does
  // not invent a row from an impossible payload.
  const given = slot.links ?? []
  const links = mirrorWire && slot.slack_linked && !given.some(link => link.channel === 'slack')
    ? [...given, {
      channel: 'slack', label: 'Slack', target: '', direction: 'out',
      live: true, paused: false,
    }]
    : given
  const store = createTestStore({
    dashboard: { ...dashboardState, slots: [{ ...slot, links }] },
  })
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

describe('Session menu — the single Slack row', () => {
  /** The row is found by its verb, because the verb IS the state display. */
  const slackRow = (label: string) => screen.getByText(label).closest('[role="menuitem"]')!
  /** Every Slack-ish row in the menu — the guard against rendering two. */
  const slackRowCount = () => screen.queryAllByText(/Slack/).length

  it('is ONE row when connected, with no separate status, reminder or unlink item', async () => {
    renderMenu({ key: 'chat-1-100', slack_linked: true })

    expect(await screen.findByText('Disconnect from Slack')).toBeInTheDocument()
    // Exactly one Slack row — never the row plus a picker offer for the same channel.
    expect(slackRowCount()).toBe(1)
    expect(screen.queryByText('Connect to Slack')).not.toBeInTheDocument()
    // Everything the four-row menu used to carry is gone, including the badge.
    expect(screen.queryByText('Unlink from Slack')).not.toBeInTheDocument()
    expect(screen.queryByText('Post reminder in Slack')).not.toBeInTheDocument()
    expect(screen.queryByText('Pause Slack sync')).not.toBeInTheDocument()
    expect(screen.queryByText('Reconnect to Slack')).not.toBeInTheDocument()
    expect(screen.queryByText('Connected: Slack')).not.toBeInTheDocument()
    expect(screen.queryByText(/Origin/)).not.toBeInTheDocument()
  })

  it('is the same ONE row when not connected, showing the opposite verb', async () => {
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    expect(await screen.findByText('Connect to Slack')).toBeInTheDocument()
    expect(slackRowCount()).toBe(1)
    expect(screen.queryByText('Disconnect from Slack')).not.toBeInTheDocument()
  })

  it('never advertises the machinery — no pause, reply-to-resume or thread coaching', async () => {
    renderMenu({ key: 'chat-1-100', slack_linked: true })
    await screen.findByText('Disconnect from Slack')

    // The user asked for two states and nothing else: no tooltip explaining that
    // the thread is kept, no hint to reply there, no mention of pausing.
    expect(slackRow('Disconnect from Slack')).not.toHaveAttribute('title')
    expect(screen.queryByText(/repl/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/paus/i)).not.toBeInTheDocument()
    expect(screen.queryByText(/thread/i)).not.toBeInTheDocument()
  })

  it('disconnects in place — same row, opposite verb, menu still open, binding intact', async () => {
    const { store } = renderMenu({
      key: 'chat-1-100',
      slack_linked: true,
      slack_channel: 'C-1',
      slack_thread_ts: 'ts-1',
    })

    fireEvent.click(await screen.findByText('Disconnect from Slack'))

    await waitFor(() => expect(api.pauseSlack).toHaveBeenCalledWith('chat-1-100'))
    // Disconnect is NOT a release: severing the binding would fork a new session
    // on the next reply in that thread, which is the defect this row replaced.
    expect(api.unlinkSlack).not.toHaveBeenCalled()
    // No reopen — the click kept the menu open on purpose, so the flip is visible
    // on the very row that was clicked.
    await waitFor(() => {
      expect(screen.getByText('Connect to Slack')).toBeInTheDocument()
      expect(screen.queryByText('Disconnect from Slack')).not.toBeInTheDocument()
    })
    const slot = store.getState().dashboard.slots.find((s: ChatSlot) => s.key === 'chat-1-100')
    expect(slot?.slack_channel).toBe('C-1')
    expect(slot?.slack_thread_ts).toBe('ts-1')
  })

  it('connects in place from the same row', async () => {
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    fireEvent.click(await screen.findByText('Connect to Slack'))

    // The configured target is passed only because this session has no thread yet.
    await waitFor(() => expect(api.slackLink).toHaveBeenCalledWith('chat-1-100', 'dm'))
    await waitFor(() => expect(screen.getByText('Disconnect from Slack')).toBeInTheDocument())
  })

  it('reconnects a retained thread without minting a second one', async () => {
    // A disconnected-but-retained link looks IDENTICAL to never-connected: same
    // row, same verb. The difference is only in what the click does.
    renderMenu({
      key: 'chat-1-100',
      slack_linked: true,
      slack_channel: 'C-1',
      slack_thread_ts: 'ts-1',
      links: [{
        channel: 'slack',
        label: 'Slack',
        target: 'C-1',
        direction: 'origin',
        live: false,
        paused: true,
      }],
    })

    fireEvent.click(await screen.findByText('Connect to Slack'))

    // No channel argument: the endpoint lifts the mute and re-seeds the EXISTING
    // thread, catching it up. Passing the picker's target here would strand it and
    // open another.
    await waitFor(() => expect(api.slackLink).toHaveBeenCalledWith('chat-1-100', undefined))
  })

  it('still offers another channel while Slack is connected', async () => {
    // The picker used to render ONLY when nothing was linked at all, so once a
    // session carried a Slack row every other destination became unreachable.
    // A Slack link occupies the Slack slot, not the mirror slot.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([
      {
        channel_type: 'slack',
        target_id: 'dm',
        label: 'Slack · Direct Message',
        available: true,
        unavailable_reason: '',
      },
      {
        channel_type: 'discord',
        target_id: 'D1',
        label: 'Discord DM',
        available: true,
        unavailable_reason: '',
      },
    ])
    renderMenu({ key: 'chat-1-100', slack_linked: true })

    expect(await screen.findByText('Connect to Discord DM')).toBeInTheDocument()
    // Slack never appears twice: it has its own row, and a second entry would
    // offer to mint a thread this session already has.
    expect(screen.queryByText('Connect to Slack · Direct Message')).not.toBeInTheDocument()
    expect(screen.getByText('Disconnect from Slack')).toBeInTheDocument()
    expect(slackRowCount()).toBe(1)
  })

  it('explains a Slack target that cannot link instead of failing silently', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'slack',
      target_id: 'dm',
      label: 'Slack · Direct Message',
      available: false,
      unavailable_reason: 'Slack is not configured.',
    }])
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    await screen.findByText('Connect to Slack')
    const row = slackRow('Connect to Slack')
    expect(row).toHaveAttribute('aria-disabled', 'true')
    // One row still: the reason lives in the tooltip, not on a second line. This is
    // the ONLY tooltip the row ever carries — a broken config is a fact the user
    // cannot otherwise see, unlike the resume behaviour.
    expect(row).toHaveAttribute('title', 'Slack is not configured.')

    fireEvent.click(row)
    expect(api.slackLink).not.toHaveBeenCalled()
  })

  it('renders no Slack row at all when Slack is neither linked nor configured', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([])
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    await waitFor(() => expect(api.channelTargets).toHaveBeenCalled())
    expect(screen.queryByText('Connect to Slack')).not.toBeInTheDocument()
    expect(screen.queryByText('Disconnect from Slack')).not.toBeInTheDocument()
  })

  it('never invents a Slack row from slack_linked — the row must come from the wire', async () => {
    // The row is what carries `paused`, so a row synthesized here could not know
    // the mute and would render a MUTED thread as "Disconnect from Slack" — the
    // label then snapping back on every slots push. The backend now emits a Slack
    // row on exactly the condition it reports `slack_linked`
    // (TestASlackRowAlwaysAccompaniesSlackLinked pins that), so trusting the wire
    // is what keeps the two from disagreeing.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([])
    renderMenu({ key: 'chat-1-100', slack_linked: true, links: [] }, { mirrorWire: false })

    await waitFor(() => expect(api.channelTargets).toHaveBeenCalled())
    expect(screen.queryByText('Disconnect from Slack')).not.toBeInTheDocument()
    expect(screen.queryByText('Connect to Slack')).not.toBeInTheDocument()
  })
})

describe('Session menu — every channel is the same one row', () => {
  it('offers an unconnected channel as a verb, and connects it', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'discord',
      target_id: 'user:42',
      label: 'Discord DM · 42',
      available: true,
      unavailable_reason: '',
    }])
    renderMenu({ key: 'chat-1-100', slack_linked: false })

    fireEvent.click(await screen.findByText('Connect to Discord DM · 42'))

    // No confirm on a free conversation: the fourth argument is only set after the
    // user agrees to take a conversation another session holds.
    await waitFor(() => expect(api.linkMirror).toHaveBeenCalledWith(
      'chat-1-100',
      'discord',
      'user:42',
      undefined,
    ))
  })

  it('mints the fresh row with the direction the SERVER reports', async () => {
    // Only a transport whose inbound path resolves the mirror binding routes replies
    // back — Discord today. The server reports the resulting direction so the client
    // does not keep its own copy of that list; the mock therefore carries the field
    // the real endpoint sends. `paused` is asserted too: omitting it is the same
    // class of gap that let a muted row render as connected.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'discord',
      target_id: 'user:42',
      label: 'Discord DM · 42',
      available: true,
      unavailable_reason: '',
    }])
    vi.mocked(api.linkMirror).mockResolvedValueOnce({
      ok: true, conversation_id: 'user:42', direction: 'both',
    })
    const { store } = renderMenu({ key: 'chat-1-100', slack_linked: false })

    fireEvent.click(await screen.findByText('Connect to Discord DM · 42'))

    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find(
        (s: ChatSlot) => s.key === 'chat-1-100',
      )
      const row = slot?.links?.find(link => link.channel === 'discord')
      expect(row).toBeDefined()
      expect(row?.direction).toBe('both')
      expect(row?.paused).toBe(false)
    })
  })

  it('does not claim two-way for a channel that cannot resume a session', async () => {
    // Telegram builds its session key from the route and never looks the mirror
    // binding up, so a reply there starts a SEPARATE session with none of this
    // one's context. Showing it as two-way promised something the code does not do.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'telegram',
      target_id: '4242',
      label: 'Telegram · 4242',
      available: true,
      unavailable_reason: '',
    }])
    vi.mocked(api.linkMirror).mockResolvedValueOnce({
      ok: true, conversation_id: '4242', direction: 'out',
    })
    const { store } = renderMenu({ key: 'chat-1-100', slack_linked: false })

    fireEvent.click(await screen.findByText('Connect to Telegram · 4242'))

    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find(
        (s: ChatSlot) => s.key === 'chat-1-100',
      )
      const row = slot?.links?.find(link => link.channel === 'telegram')
      expect(row).toBeDefined()
      expect(row?.direction).toBe('out')
    })
  })

  it('falls back to outbound-only when the server reports no direction', async () => {
    // An older gateway (or a trimmed response) must not be read as two-way: the
    // conservative reading is the one that cannot over-promise.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'telegram',
      target_id: '4242',
      label: 'Telegram · 4242',
      available: true,
      unavailable_reason: '',
    }])
    vi.mocked(api.linkMirror).mockResolvedValueOnce({ ok: true, conversation_id: '4242' })
    const { store } = renderMenu({ key: 'chat-1-100', slack_linked: false })

    fireEvent.click(await screen.findByText('Connect to Telegram · 4242'))

    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find(
        (s: ChatSlot) => s.key === 'chat-1-100',
      )
      expect(slot?.links?.find(link => link.channel === 'telegram')?.direction).toBe('out')
    })
  })

  it('keeps an unavailable channel focusable and explains why it cannot link', async () => {
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
    // One row still: the reason is the row's only tooltip, never a second line.
    expect(item).toHaveAttribute('title', 'WeCom can only reply to an inbound message.')

    fireEvent.click(row)
    expect(api.linkMirror).not.toHaveBeenCalled()
  })

  it('gives an origin channel no row at all — there is nothing to connect', async () => {
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

    // A session's BIRTH conversation is not a binding the dashboard can toggle,
    // and the sidebar already marks it from the slot key. So: no Discord row, and
    // none of the vocabulary the badge used to carry.
    expect(await screen.findByText('Connect to Slack')).toBeInTheDocument()
    expect(screen.queryByText(/Discord/)).not.toBeInTheDocument()
    expect(screen.queryByText('Connected: Discord DM')).not.toBeInTheDocument()
    expect(screen.queryByText(/Origin|Mirror|Two-way|Offline/)).not.toBeInTheDocument()
  })
})


describe('Session menu — a connected channel', () => {
  const discordLink = {
    channel: 'discord',
    label: 'Discord DM',
    target: '…767244',
    direction: 'out' as const,
    live: true,
  }

  it('reads as the disconnect verb, with no reminder or release item', async () => {
    renderMenu({ key: 'mirrored-session', slack_linked: false, links: [discordLink] })

    expect(await screen.findByText('Disconnect from Discord DM')).toBeInTheDocument()
    // Everything the three-row shape used to carry is gone.
    expect(screen.queryByText('Post reminder in Discord DM')).not.toBeInTheDocument()
    expect(screen.queryByText('Stop mirroring to Discord DM')).not.toBeInTheDocument()
    expect(screen.queryByText('Release Discord DM')).not.toBeInTheDocument()
    expect(screen.queryByText('Connected: Discord DM')).not.toBeInTheDocument()
    expect(screen.queryByText(/Mirror|Two-way|Offline/)).not.toBeInTheDocument()
  })

  it('disconnects by muting, never by releasing, and flips in place', async () => {
    renderMenu({ key: 'mirrored-session', slack_linked: false, links: [discordLink] })

    fireEvent.click(await screen.findByText('Disconnect from Discord DM'))

    // Named channel: a session can hold several bindings, so an unnamed mute
    // would silence an arbitrary sibling.
    await waitFor(() => expect(api.pauseMirror).toHaveBeenCalledWith('mirrored-session', 'discord'))
    // Releasing would evict the binding, so a reply in that conversation would no
    // longer resolve to this session. There is no confirm either: this is
    // reversible from the same row.
    expect(api.unlinkMirror).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(screen.getByText('Connect to Discord DM')).toBeInTheDocument()
      expect(screen.queryByText('Disconnect from Discord DM')).not.toBeInTheDocument()
    })
  })

  it('reconnects a muted channel through the same row, with no target argument', async () => {
    renderMenu({
      key: 'mirrored-session',
      slack_linked: false,
      links: [{ ...discordLink, paused: true }],
    })

    fireEvent.click(await screen.findByText('Connect to Discord DM'))

    // No target: the endpoint reuses the existing binding and catches the
    // conversation up. Passing one would mint a second binding.
    // The channel, but no target: the endpoint reuses the existing binding and
    // catches the conversation up. Passing a target would mint a second one.
    await waitFor(() => expect(api.reconnectMirror).toHaveBeenCalledWith(
      'mirrored-session', 'discord',
    ))
    expect(api.linkMirror).not.toHaveBeenCalled()
  })

  it('does not offer a second channel while one is connected', async () => {
    // A session holds ONE non-Slack binding. Offering another would silently
    // replace it, which is the opposite of what the row says.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'telegram',
      target_id: 'user:9',
      label: 'Telegram DM',
      available: true,
      unavailable_reason: '',
    }])
    renderMenu({ key: 'mirrored-session', slack_linked: false, links: [discordLink] })

    expect(await screen.findByText('Disconnect from Discord DM')).toBeInTheDocument()
    expect(screen.queryByText('Connect to Telegram DM')).not.toBeInTheDocument()
  })

  it('reads the same whether or not the transport can send right now', async () => {
    // `live: false` used to surface as an "Offline" badge and hide the reminder.
    // Two states only now, so a channel that cannot send this second still reads
    // as connected — which it is.
    renderMenu({
      key: 'dead-mirror-session',
      slack_linked: false,
      links: [{ ...discordLink, live: false }],
    })

    expect(await screen.findByText('Disconnect from Discord DM')).toBeInTheDocument()
    expect(screen.queryByText(/Offline/)).not.toBeInTheDocument()
  })
})


describe('Session menu — several channels at once', () => {
  const discordLink = {
    channel: 'discord', label: 'Discord DM', target: '…767244', direction: 'out' as const, live: true,
  }
  const telegramLink = {
    channel: 'telegram', label: 'Telegram DM', target: '…99', direction: 'out' as const, live: true,
  }

  it('gives every binding its own row, and Slack keeps its own', async () => {
    renderMenu({
      key: 'multi-session',
      slack_linked: true,
      links: [discordLink, telegramLink],
    })

    expect(await screen.findByText('Disconnect from Discord DM')).toBeInTheDocument()
    expect(screen.getByText('Disconnect from Telegram DM')).toBeInTheDocument()
    expect(screen.getByText('Disconnect from Slack')).toBeInTheDocument()
  })

  it('shows each binding its own state, so one muted channel is not read as all', async () => {
    renderMenu({
      key: 'multi-session',
      slack_linked: false,
      links: [{ ...discordLink, paused: true }, telegramLink],
    })

    expect(await screen.findByText('Connect to Discord DM')).toBeInTheDocument()
    expect(screen.getByText('Disconnect from Telegram DM')).toBeInTheDocument()
  })

  it('disconnects only the channel whose row was clicked', async () => {
    renderMenu({ key: 'multi-session', slack_linked: false, links: [discordLink, telegramLink] })

    fireEvent.click(await screen.findByText('Disconnect from Telegram DM'))

    await waitFor(() => expect(api.pauseMirror).toHaveBeenCalledWith('multi-session', 'telegram'))
    expect(api.pauseMirror).not.toHaveBeenCalledWith('multi-session', 'discord')
    // The sibling is untouched and still reads connected.
    expect(screen.getByText('Disconnect from Discord DM')).toBeInTheDocument()
  })

  it('does not offer a channel it already holds, but still offers the others', async () => {
    // One binding per channel type: a second Discord conversation would be a
    // second binding on the same channel, which cannot be addressed.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([
      { channel_type: 'discord', target_id: 'D2', label: 'Discord DM', available: true, unavailable_reason: '' },
      { channel_type: 'telegram', target_id: 'T1', label: 'Telegram DM', available: true, unavailable_reason: '' },
    ])
    renderMenu({ key: 'multi-session', slack_linked: false, links: [discordLink] })

    expect(await screen.findByText('Connect to Telegram DM')).toBeInTheDocument()
    expect(screen.getByText('Disconnect from Discord DM')).toBeInTheDocument()
    expect(screen.queryByText('Connect to Discord DM')).not.toBeInTheDocument()
  })
})


describe('Session menu — taking a conversation from another session', () => {
  /** The backend refuses an occupied conversation with 409 until confirmed.
   *
   * Built the way the real client builds it: `friendlyErrText` unwraps the body's
   * `error` field and DISCARDS `code`, so the message carries only the prose. A
   * hand-rolled `new Error('conversation_occupied')` would let a message-matching
   * implementation pass while the real 409 never triggered the confirm — this
   * error must stay shaped like the one `apiFailure` actually throws.
   */
  const OCCUPIED_BODY = JSON.stringify({
    error: 'another session is connected to this conversation',
    code: 'conversation_occupied',
  })
  const occupied = () => new ApiError(
    409, 'another session is connected to this conversation', OCCUPIED_BODY,
  )

  it('asks before disconnecting the session that holds it, then retries confirmed', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'discord', target_id: 'D1', label: 'Discord DM', available: true, unavailable_reason: '',
    }])
    vi.mocked(api.linkMirror)
      .mockRejectedValueOnce(occupied())
      .mockResolvedValueOnce({ ok: true, conversation_id: 'D1' })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderMenu({ key: 'chat-1-100', slack_linked: false })

      fireEvent.click(await screen.findByText('Connect to Discord DM'))

      await waitFor(() => expect(confirmSpy).toHaveBeenCalledWith(
        expect.stringContaining('already connected to another session'),
      ))
      // Only the confirmed retry carries the flag that authorises the eviction.
      await waitFor(() => expect(api.linkMirror).toHaveBeenCalledWith(
        'chat-1-100', 'discord', 'D1', true,
      ))
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('leaves the other session connected when the confirm is dismissed', async () => {
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'discord', target_id: 'D1', label: 'Discord DM', available: true, unavailable_reason: '',
    }])
    vi.mocked(api.linkMirror).mockRejectedValueOnce(occupied())
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    try {
      renderMenu({ key: 'chat-1-100', slack_linked: false })

      fireEvent.click(await screen.findByText('Connect to Discord DM'))

      await waitFor(() => expect(confirmSpy).toHaveBeenCalled())
      // No confirmed retry: the other session keeps the conversation.
      expect(api.linkMirror).not.toHaveBeenCalledWith('chat-1-100', 'discord', 'D1', true)
    } finally {
      confirmSpy.mockRestore()
    }
  })

  it('does not offer a takeover for a 409 that is not about occupancy', async () => {
    // The endpoint answers 409 for `configured_target_unavailable` too. Matching
    // the STATUS alone offered to disconnect a session from a channel that was
    // merely unavailable — a prompt for a takeover that was never possible, and
    // clicking OK just retried the same failure with `confirm: true`.
    vi.mocked(api.channelTargets).mockResolvedValueOnce([{
      channel_type: 'discord', target_id: 'D1', label: 'Discord DM', available: true, unavailable_reason: '',
    }])
    vi.mocked(api.linkMirror).mockRejectedValueOnce(new ApiError(
      409,
      'that channel is not available right now',
      JSON.stringify({
        error: 'that channel is not available right now',
        code: 'configured_target_unavailable',
      }),
    ))
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    try {
      renderMenu({ key: 'chat-1-100', slack_linked: false })

      fireEvent.click(await screen.findByText('Connect to Discord DM'))

      // The failure is reported, and no takeover is proposed.
      await waitFor(() => expect(api.linkMirror).toHaveBeenCalledTimes(1))
      expect(confirmSpy).not.toHaveBeenCalled()
      expect(api.linkMirror).not.toHaveBeenCalledWith('chat-1-100', 'discord', 'D1', true)
    } finally {
      confirmSpy.mockRestore()
    }
  })
})




describe('Session menu — two channels toggled at once', () => {
  /**
   * The callbacks used to rebuild the whole `links` array from the `links` captured
   * when `mutate` was called. With two toggles in flight, both derived from the same
   * pre-mutation snapshot and the second dispatch overwrote the first — the sibling
   * row silently reverted or vanished until the next slots push corrected it.
   *
   * Each row is now patched independently against current store state, so this is
   * about the STORE operation, not about disabling the buttons: the user may toggle
   * two channels as fast as they like.
   */
  it('keeps both rows when a Slack and a Discord connect resolve out of order', async () => {
    // Asserted on the STORE operation, not by driving two menu clicks. Radix closes
    // the dropdown on select and would not reopen under jsdom (the trigger stays
    // `data-state="closed"` after a synthetic Enter), so a two-click harness could
    // not reach the second row at all — and the invariant is not about the buttons,
    // it is that one channel's patch never rewrites another's row. That is exactly
    // what `patchSlotLink` does, so it is tested directly.
    //
    // The sequence below is the defect: Discord's callback runs LAST and, under the
    // old code, dispatched a whole `links` array it had computed from a snapshot
    // taken before Slack's row existed.
    const store = createTestStore({
      dashboard: { ...dashboardState, slots: [{ key: 'chat-1-100', links: [] }] },
    })
    const slackRow = {
      channel: 'slack', label: 'Slack', target: 'C999',
      direction: 'out' as const, live: true, paused: false,
    }
    const discordRow = {
      channel: 'discord', label: 'Discord DM · 42', target: 'user:42',
      direction: 'both' as const, live: true, paused: false,
    }

    store.dispatch(patchSlotLink({ key: 'chat-1-100', channel: 'slack', link: slackRow }))
    store.dispatch(patchSlotLink({ key: 'chat-1-100', channel: 'discord', link: discordRow }))

    const slot = store.getState().dashboard.slots.find((s: ChatSlot) => s.key === 'chat-1-100')
    const channels = (slot?.links ?? []).map(link => link.channel)
    expect(channels).toContain('slack')
    expect(channels).toContain('discord')
    expect(channels).toHaveLength(2)
  })

  it('patching one channel leaves every sibling field untouched', () => {
    const store = createTestStore({
      dashboard: {
        ...dashboardState,
        slots: [{
          key: 'chat-1-100',
          links: [
            { channel: 'slack', label: 'Slack', target: 'C1', direction: 'out', live: true, paused: false },
            { channel: 'discord', label: 'Discord DM', target: 'D1', direction: 'both', live: true, paused: false },
          ],
        }],
      },
    })

    store.dispatch(patchSlotLink({
      key: 'chat-1-100', channel: 'slack', link: null, patch: { paused: true },
    }))

    const rows = store.getState().dashboard.slots[0].links ?? []
    expect(rows.find(link => link.channel === 'slack')?.paused).toBe(true)
    expect(rows.find(link => link.channel === 'discord')).toEqual({
      channel: 'discord', label: 'Discord DM', target: 'D1',
      direction: 'both', live: true, paused: false,
    })
  })

  it('a patch for a channel with no row does not invent one', () => {
    // An invented row cannot know `paused`, which is how a muted thread came to
    // render as connected.
    const store = createTestStore({
      dashboard: { ...dashboardState, slots: [{ key: 'chat-1-100', links: [] }] },
    })

    store.dispatch(patchSlotLink({
      key: 'chat-1-100', channel: 'telegram', link: null, patch: { paused: true },
    }))

    expect(store.getState().dashboard.slots[0].links).toEqual([])
  })

  it('a disconnect patches only its own row', async () => {
    const { store } = renderMenu({
      key: 'chat-1-100',
      slack_linked: true,
      links: [
        { channel: 'slack', label: 'Slack', target: 'C1', direction: 'out', live: true, paused: false },
        { channel: 'discord', label: 'Discord DM', target: 'D1', direction: 'both', live: true, paused: false },
      ],
    })
    vi.mocked(api.pauseSlack).mockResolvedValueOnce({ ok: true, was_paused: false })

    fireEvent.click(await screen.findByText('Disconnect from Slack'))

    await waitFor(() => {
      const slot = store.getState().dashboard.slots.find((s: ChatSlot) => s.key === 'chat-1-100')
      const rows = slot?.links ?? []
      expect(rows.find(link => link.channel === 'slack')?.paused).toBe(true)
      // The sibling is untouched — same object shape, still connected.
      expect(rows.find(link => link.channel === 'discord')?.paused).toBe(false)
      expect(rows.find(link => link.channel === 'discord')?.direction).toBe('both')
    })
  })
})

describe('Session menu — an in-flight toggle shows on its OWN row', () => {
  const row = (label: string) => screen.getByText(label).closest('[role="menuitem"]')!

  /** A slot holding two independently mutable bindings. */
  const twoChannels = {
    key: 'chat-1',
    links: [
      {
        channel: 'discord', label: 'Discord DM', target: 'd1',
        direction: 'both', live: true, paused: false,
      },
      {
        channel: 'telegram', label: 'Telegram DM', target: 't1',
        direction: 'out', live: true, paused: false,
      },
    ],
  }

  it('marks the clicked row busy while the mutation is in flight', async () => {
    // Connect runs server-side catch-up before responding, so this gap is the
    // normal case rather than an edge one: a never-resolving request is exactly
    // what the user experiences as a dead click.
    let release: (() => void) | undefined
    vi.mocked(api.pauseMirror).mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({ ok: true }) }) as never,
    )

    renderMenu(twoChannels as never)
    fireEvent.click(await screen.findByText('Disconnect from Discord DM'))

    await waitFor(() =>
      expect(row('Disconnect from Discord DM')).toHaveAttribute('aria-busy', 'true'),
    )
    release?.()
  })

  it('leaves the OTHER row alone, because the rows are independently mutable', async () => {
    // The alternative fix — disable every toggle while any mutation is pending —
    // is indistinguishable from this one on a single row, and contradicts the
    // whole point of per-channel rows. So the assertion is about the sibling.
    let release: (() => void) | undefined
    vi.mocked(api.pauseMirror).mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({ ok: true }) }) as never,
    )

    renderMenu(twoChannels as never)
    fireEvent.click(await screen.findByText('Disconnect from Discord DM'))

    await waitFor(() =>
      expect(row('Disconnect from Discord DM')).toHaveAttribute('aria-busy', 'true'),
    )
    expect(row('Disconnect from Telegram DM')).not.toHaveAttribute('aria-busy')
    release?.()
  })
})

describe('Session menu — a CONNECT is busy on its own row too', () => {
  const row = (label: string) => screen.getByText(label).closest('[role="menuitem"]')!

  it('marks a reconnecting row busy, reading the channel from the mutation', async () => {
    // The connect and disconnect paths read the in-flight channel from DIFFERENT
    // mutations, so a test that only clicks Disconnect leaves the connect branch
    // unpinned — breaking it was invisible until this case existed.
    let release: (() => void) | undefined
    vi.mocked(api.reconnectMirror).mockImplementationOnce(
      () => new Promise(resolve => {
        release = () => resolve({ ok: true, reconnected: true })
      }) as never,
    )

    renderMenu({
      key: 'chat-1',
      links: [
        {
          channel: 'discord', label: 'Discord DM', target: 'd1',
          direction: 'both', live: true, paused: true,
        },
        {
          channel: 'telegram', label: 'Telegram DM', target: 't1',
          direction: 'out', live: true, paused: false,
        },
      ],
    } as never)
    fireEvent.click(await screen.findByText('Connect to Discord DM'))

    await waitFor(() =>
      expect(row('Connect to Discord DM')).toHaveAttribute('aria-busy', 'true'),
    )
    expect(row('Disconnect from Telegram DM')).not.toHaveAttribute('aria-busy')
    release?.()
  })
})

describe('Session menu — a pending row does not freeze its siblings', () => {
  it('accepts a click on another channel while one disconnect is in flight', async () => {
    // The four mutations are SHARED across rows, so guarding on `isPending` froze
    // every sibling while one was mid-flight. The rows are independently mutable by
    // design, so the guard has to be per channel.
    let release: (() => void) | undefined
    vi.mocked(api.pauseMirror).mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({ ok: true }) }) as never,
    )

    renderMenu({
      key: 'chat-1',
      links: [
        {
          channel: 'discord', label: 'Discord DM', target: 'd1',
          direction: 'both', live: true, paused: false,
        },
        {
          channel: 'telegram', label: 'Telegram DM', target: 't1',
          direction: 'out', live: true, paused: false,
        },
      ],
    } as never)

    fireEvent.click(await screen.findByText('Disconnect from Discord DM'))
    await waitFor(() => expect(vi.mocked(api.pauseMirror)).toHaveBeenCalledTimes(1))

    // Discord is still mid-flight; Telegram must still be clickable.
    fireEvent.click(screen.getByText('Disconnect from Telegram DM'))

    await waitFor(() => expect(vi.mocked(api.pauseMirror)).toHaveBeenCalledTimes(2))
    const channels = vi.mocked(api.pauseMirror).mock.calls.map(call => call[1])
    expect(channels).toContain('telegram')
    release?.()
  })
})
