/**
 * Test: a sidebar session row wears one brand mark per channel it is CONNECTED
 * to, and nothing else.
 *
 * Two things this pins, both against the session address model
 * (docs/request-for-change/rfc-session-address-model.md §5.3, which names
 * capability, attachment and ingress as the only properties a conversation has
 * with a surface):
 *
 *  - The slot KEY draws nothing. A `slack_<ts>` key used to earn a permanent
 *    Slack mark for "where this conversation started", so a Slack-born row kept
 *    its mark after the user chose "Disconnect from Slack" while the identical
 *    mark on a dashboard-born row vanished. Origin is not a property the model
 *    has; the key is an identity, not a fact to render.
 *  - `paused` is the only thing that decides. It is the state the menu's single
 *    Connect/Disconnect row toggles, so the mark on the row and the verb in the
 *    menu cannot disagree. `direction` — origin, out, both — plays no part.
 *
 * Mock setup mirrors ChatSidebar.sourceLinkChip.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { hasChannelBrandIcon } from '../components/ChannelBrandIcon'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: Object.fromEntries(
      [
        'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
        'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
        'renameSlot', 'forkSession', 'chatTags', 'chatFolders',
      ].map(k => [k, vi.fn().mockResolvedValue({})]),
    ),
  }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatSidebar, { connectedChannelLinks } from '../pages/ChatSidebar'
import type { ChatSlot, SessionLink } from '../types'
import type { RootState } from '../store'

const link = (channel: string, direction: SessionLink['direction'], paused: boolean): SessionLink => ({
  channel, label: channel[0].toUpperCase() + channel.slice(1), target: 'redacted', direction, live: true, paused,
})

const base = { messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' }

/** Every shape the wire produces, connected and disconnected, side by side.
 *  A Slack-born session carries its thread as an `origin` link (state.py
 *  `_slot_links`); a dashboard session connected via slack-link carries an
 *  `out` link; an in-channel `!sessions` pick carries `both`. */
const slots = [
  { key: 'slack_1785370133.085469', title: 'Slack-born, connected', ...base, links: [link('slack', 'origin', false)] },
  { key: 'slack_1785370133.999999', title: 'Slack-born, disconnected', ...base, links: [link('slack', 'origin', true)] },
  { key: 'dashboard_chat-1-1', title: 'Dashboard, connected to Slack', ...base, links: [link('slack', 'out', false)] },
  { key: 'dashboard_chat-1-2', title: 'Dashboard, disconnected from Slack', ...base, links: [link('slack', 'out', true)] },
  { key: 'dashboard_chat-1-3', title: 'Dashboard, driven from Discord', ...base, links: [link('discord', 'both', false)] },
  { key: 'discord_kirocrew_direct_U1', title: 'Discord-born and mirrored to Discord', ...base, links: [link('discord', 'origin', false), link('discord', 'out', false)] },
  { key: 'discord_kirocrew_direct_U2', title: 'Discord-born, origin muted, mirror live', ...base, links: [link('discord', 'origin', true), link('discord', 'out', false)] },
  { key: 'slack_1785370133.111111', title: 'Slack key, no links', ...base },
  { key: 'unified_kirocrew', title: 'From a DM', ...base },
  { key: 'dashboard_chat-1-4', title: 'Plain dashboard', ...base },
] as unknown as ChatSlot[]

function renderSidebar() {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 'dashboard_chat-1-4',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-folders'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={'dashboard_chat-1-4'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The row that owns a given title. No source_links in the fixtures, so the only
 *  <img> a row can hold is a channel brand mark. */
const row = (title: string) => screen.getByText(title).closest('.session-row') as HTMLElement
const marks = (title: string) => Array.from(row(title).querySelectorAll('img')).map(img => img.getAttribute('src') ?? '')

// Fixtures here carry fixed old timestamps; keep the stale-session collapse
// off so every row stays queryable (its own behavior is pinned in
// ChatSidebar.staleCollapse.test.tsx).
beforeEach(() => localStorage.setItem('mc-session-stale-collapse-ms', '0'))

describe('ChatSidebar – connected-channel mark', () => {
  it('marks a connected channel whatever the direction of the link', () => {
    renderSidebar()
    expect(marks('Slack-born, connected')).toHaveLength(1)
    expect(marks('Slack-born, connected')[0]).toMatch(/slack/)
    expect(marks('Dashboard, connected to Slack')).toHaveLength(1)
    expect(marks('Dashboard, connected to Slack')[0]).toMatch(/slack/)
    expect(marks('Dashboard, driven from Discord')).toHaveLength(1)
    expect(marks('Dashboard, driven from Discord')[0]).toMatch(/discord/)
  })

  // The defect this file exists for. Both rows are disconnected; before this
  // change only the second one lost its mark, because the first drew a second
  // mark from its slot key that never read `paused`.
  it('drops the mark on disconnect for a Slack-born row exactly as for a dashboard row', () => {
    renderSidebar()
    expect(marks('Slack-born, disconnected')).toEqual([])
    expect(marks('Dashboard, disconnected from Slack')).toEqual([])
  })

  it('draws nothing from the slot key alone', () => {
    renderSidebar()
    // A channel-namespaced key with no link row is not connected to anything.
    expect(marks('Slack key, no links')).toEqual([])
    expect(marks('From a DM')).toEqual([])
    expect(marks('Plain dashboard')).toEqual([])
    // And no channel-derived tooltip survives on any of them: the row's only
    // `title`+`aria-label` spans are channel marks, and there are none.
    for (const title of ['Slack key, no links', 'From a DM', 'Plain dashboard']) {
      expect(row(title).querySelectorAll('span[title][aria-label][role="img"]')).toHaveLength(0)
    }
  })

  it('shows one mark per channel, connected while any delivery on it is live', () => {
    renderSidebar()
    // Born in Discord AND mirrored to Discord: two links, one channel, one mark.
    expect(marks('Discord-born and mirrored to Discord')).toHaveLength(1)
    // Origin muted but the mirror still delivers: still connected, same rule
    // the menu row uses to pick "Disconnect from Discord".
    expect(marks('Discord-born, origin muted, mirror live')).toHaveLength(1)
  })

  it('labels the mark as a connection, not as provenance', () => {
    renderSidebar()
    const mark = row('Slack-born, connected').querySelector('span[role="img"][title]')
    expect(mark?.getAttribute('title')).toBe('Connected to Slack')
    expect(mark?.getAttribute('aria-label')).toBe('Connected to Slack')
    // A missing catalog key renders as the raw key rather than throwing.
    expect(mark?.getAttribute('title')).not.toMatch(/pages\.chatSidebar/)
  })
})

describe('connectedChannelLinks', () => {
  it('keeps the first live link per channel, in first-seen order, and drops paused ones', () => {
    const out = connectedChannelLinks([
      link('discord', 'origin', true),
      link('slack', 'out', false),
      link('discord', 'out', false),
      link('slack', 'origin', false),
    ])
    expect(out.map(l => `${l.channel}:${l.direction}`)).toEqual(['slack:out', 'discord:out'])
  })

  it('returns nothing for a session with no links or only paused ones', () => {
    expect(connectedChannelLinks(undefined)).toEqual([])
    expect(connectedChannelLinks([])).toEqual([])
    expect(connectedChannelLinks([link('slack', 'origin', true), link('slack', 'out', true)])).toEqual([])
  })
})

describe('hasChannelBrandIcon', () => {
  it('is true only for namespaces with a real brand asset', () => {
    for (const ch of ['slack', 'discord', 'telegram', 'teams', 'webex', 'wecom', 'weixin']) {
      expect(hasChannelBrandIcon(ch)).toBe(true)
    }
    expect(hasChannelBrandIcon('whatsapp')).toBe(true)
    expect(hasChannelBrandIcon('unified')).toBe(false)
    expect(hasChannelBrandIcon('')).toBe(false)
  })

  it('is case-insensitive, matching ChannelBrandIcon lookup', () => {
    expect(hasChannelBrandIcon('Discord')).toBe(true)
  })
})
