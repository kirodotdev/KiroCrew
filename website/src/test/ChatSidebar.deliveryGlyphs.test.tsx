/**
 * Test: which links earn a delivery glyph on a sidebar session row.
 *
 * The row's glyph is the ONLY place outside the ⋯ menu that states where this
 * session is being delivered, so a glyph that disagrees with the menu is a lie
 * the user sees first. Two ways the old `direction === 'out'` filter did that:
 *
 * - A DISCONNECTED link keeps its direction (disconnect sets `paused` and retains
 *   the binding on purpose, so a reply in the thread can resume the same session).
 *   Filtering on direction alone kept promising "Mirroring to Slack" for a session
 *   whose own menu, one row away, reads "Send to Slack".
 * - A TWO-WAY binding (`both`, set by picking the session with `!sessions` from
 *   inside the channel) failed the filter, so the session with messages flowing
 *   BOTH ways was the one that looked unlinked, while a strictly weaker one-way
 *   mirror got a glyph.
 *
 * `origin` stays excluded: channelOrigin.ts already badges it from the slot key,
 * and a second mark would double-badge the same fact.
 *
 * Mock setup mirrors ChatSidebar.channelOriginGlyph.test.tsx.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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

const base = { messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' }
const slots = [
  {
    ...base,
    key: 'dashboard_chat-1-1',
    title: 'One-way mirror',
    links: [{ channel: 'discord', label: 'Discord DM', target: 'D1', direction: 'out', live: true }],
  },
  {
    ...base,
    key: 'dashboard_chat-1-2',
    title: 'Two-way binding',
    links: [{ channel: 'discord', label: 'Discord DM', target: 'D2', direction: 'both', live: true }],
  },
  {
    ...base,
    key: 'dashboard_chat-1-3',
    title: 'Connected Slack',
    links: [{ channel: 'slack', label: 'Slack', target: 'C1', direction: 'out', live: true, paused: false }],
  },
  {
    ...base,
    key: 'dashboard_chat-1-4',
    title: 'Disconnected Slack',
    links: [{ channel: 'slack', label: 'Slack', target: 'C1', direction: 'out', live: true, paused: true }],
  },
  {
    ...base,
    key: 'dashboard_chat-1-5',
    title: 'Origin only',
    links: [{ channel: 'discord', label: 'Discord DM', target: 'D3', direction: 'origin', live: true }],
  },
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
      activeSlot: 'dashboard_chat-1-1',
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
              slots={slots} activeSlot={'dashboard_chat-1-1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const row = (title: string) => screen.getByText(title).closest('.session-row') as HTMLElement
/** The delivery glyph carries `aria-label` AND a matching `title`, so it shows
 *  no hover tooltip; the origin glyph carries both, which is what keeps these two
 *  apart without either test reading the other's element. */
const deliveryLabels = (title: string) => Array.from(
  row(title).querySelectorAll('span[aria-label][title]'),
).map(node => node.getAttribute('aria-label'))

describe('ChatSidebar – delivery glyphs', () => {
  it('badges a one-way mirror', () => {
    renderSidebar()
    expect(deliveryLabels('One-way mirror')).toContain('Connected to Discord DM')
  })

  it('badges a two-way binding, which is MORE connected than a one-way mirror', () => {
    renderSidebar()
    expect(deliveryLabels('Two-way binding')).toContain('Connected to Discord DM')
  })

  it('badges a connected Slack link', () => {
    renderSidebar()
    expect(deliveryLabels('Connected Slack')).toContain('Connected to Slack')
  })

  it('names the glyph for hover AND for screen readers, with the same string', () => {
    renderSidebar()
    // The ORIGIN glyph rendered directly above carries a `title`, and for a session
    // that started in Slack and also delivers to Slack both are the same brand mark.
    // Naming only one of them left a sighted user hovering two identical marks and
    // getting text from just one, so the delivery glyph carries both attributes.
    const glyphs = row('Connected Slack').querySelectorAll('span[aria-label][title]')
    expect(glyphs.length).toBeGreaterThan(0)
    glyphs.forEach(node =>
      expect(node.getAttribute('title')).toBe(node.getAttribute('aria-label')),
    )
  })

  it('gives the glyph an img role, without which its label is not announced', () => {
    renderSidebar()
    // `aria-label` on a generic (roleless) span is not reliably announced, so the
    // accessible name needs the role even though the `title` is also present.
    const glyphs = row('Connected Slack').querySelectorAll('span[aria-label][title]')
    expect(glyphs.length).toBeGreaterThan(0)
    glyphs.forEach(node => expect(node).toHaveAttribute('role', 'img'))
  })

  it('does NOT badge a disconnected link, whose binding is retained', () => {
    renderSidebar()
    // The exact contradiction this closes: the menu says "Connect to Slack" while
    // the sidebar said "Mirroring to Slack" for the same session.
    expect(deliveryLabels('Disconnected Slack')).not.toContain('Connected to Slack')
  })

  it('does NOT badge an origin link, which channelOrigin already marks', () => {
    renderSidebar()
    expect(deliveryLabels('Origin only')).not.toContain('Connected to Discord DM')
  })
})
