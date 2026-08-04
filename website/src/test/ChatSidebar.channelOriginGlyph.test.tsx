/**
 * Test: the channel-origin glyph on a sidebar session row is the CHANNEL's brand
 * mark, not a generic chat bubble.
 *
 * The row already says a chat happened, so a bubble carried no information — a
 * session started from Discord looked identical to one started from Slack. The
 * brand mark the app already ships for live-mirror links is reused for the
 * origin glyph.
 *
 * Namespaces with no brand asset keep the bubble on purpose: ChannelBrandIcon
 * falls through to `Link2`, which in this row means live mirroring, so an
 * origin-only session would be badged as if it were mirroring.
 *
 * Mock setup mirrors ChatSidebar.sourceLinkChip.test.tsx.
 */
import { describe, it, expect, vi } from 'vitest'
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

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot } from '../types'
import type { RootState } from '../store'

const slots = [
  { key: 'discord_kirocrew_direct_U1', title: 'From Discord', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
  { key: 'slack_1785370133.085469', title: 'From Slack', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
  { key: 'unified_kirocrew', title: 'From a DM', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
  { key: 'dashboard_chat-1-1', title: 'Plain dashboard', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
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
      activeSlot: 'discord_kirocrew_direct_U1',
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
              slots={slots} activeSlot={'discord_kirocrew_direct_U1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The row that owns a given title. No source_links in the fixtures, so the only
 *  <img> a row can hold is the origin glyph. */
const row = (title: string) => screen.getByText(title).closest('.session-row') as HTMLElement

describe('ChatSidebar – channel-origin glyph', () => {
  it('badges a channel-origin session with that channel brand mark', () => {
    renderSidebar()
    expect(row('From Discord').querySelector('img')?.getAttribute('src')).toMatch(/discord/)
    expect(row('From Slack').querySelector('img')?.getAttribute('src')).toMatch(/slack/)
  })

  it('leaves a brandless namespace and a plain dashboard session unbadged by a logo', () => {
    renderSidebar()
    // `unified` is the aggregated DM inbox — not a product, so no brand mark.
    expect(row('From a DM').querySelector('img')).toBeNull()
    // Negative control: a dashboard session gets no origin glyph at all.
    expect(row('Plain dashboard').querySelector('img')).toBeNull()
  })

  // The glyph's tooltip is the only place the dashboard states the relationship
  // between this tab and the channel, so it is pinned here. It used to read
  // "Copied from Slack — replies stay here", which the one-session refactor made
  // false: the tab IS the conversation, so a reply is delivered to the channel
  // and a message sent there arrives here. Same wording as the inbound-link chip.
  it('describes the channel relationship as two-way, not as a copy', () => {
    renderSidebar()
    // The glyph is the row's only span carrying both `title` and `aria-label`
    // (the merged badge is the other, and no fixture is merged) — asserted
    // rather than assumed, so this cannot silently start reading a different
    // element's tooltip.
    const glyphTitle = (title: string) => {
      const found = row(title).querySelectorAll('span[title][aria-label]')
      expect(found.length).toBe(1)
      return found[0].getAttribute('title') ?? ''
    }

    expect(glyphTitle('From Slack')).toBe(
      'This session is two-way with Slack: replies are delivered there, and messages sent there arrive here.',
    )
    // The DM variant is a whole sentence of its own, not the channel sentence
    // with an English article fragment interpolated into it.
    expect(glyphTitle('From a DM')).toBe(
      'This session is two-way with a direct message: replies are delivered there, and messages sent there arrive here.',
    )
    // The retired copy asserted the opposite of what the code now does.
    expect(glyphTitle('From Discord')).not.toMatch(/copied from|replies stay here/i)
    // A missing catalog key renders as the raw key rather than throwing.
    expect(glyphTitle('From Discord')).not.toMatch(/pages\.chatSidebar/)
  })
})

describe('hasChannelBrandIcon', () => {
  it('is true only for namespaces with a real brand asset', () => {
    for (const ch of ['slack', 'discord', 'telegram', 'teams', 'webex', 'wecom', 'weixin']) {
      expect(hasChannelBrandIcon(ch)).toBe(true)
    }
    // Both fall through to the Link2 default, which callers must not mistake for
    // a brand mark: `whatsapp` has no asset yet, `unified` never will.
    expect(hasChannelBrandIcon('whatsapp')).toBe(false)
    expect(hasChannelBrandIcon('unified')).toBe(false)
    expect(hasChannelBrandIcon('')).toBe(false)
  })

  it('is case-insensitive, matching ChannelBrandIcon lookup', () => {
    expect(hasChannelBrandIcon('Discord')).toBe(true)
  })
})
