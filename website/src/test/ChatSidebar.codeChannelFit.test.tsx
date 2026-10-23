/**
 * A Slack code channel row's meta line (agent name, the channel's Slack mark,
 * the time) in the real sidebar.
 *
 * The row carries the Slack mark ALONE: a link named "Open #channel in Slack",
 * with no channel text anywhere in the row (the session header shows the full
 * link). The mark keeps a fixed size and never shrinks, the agent name yields
 * first, and the time never shrinks. jsdom lays nothing out, so this pins the
 * DOM and the flex weights; the pixels (the mark drawn whole at its fixed size,
 * nested rows at the narrowest width) are measured on the real build by
 * scripts/capture-slack-code-channel-row-fit.mjs.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession', 'patchConfig', 'setSlotFolder', 'setPinnedOrder', 'setManualOrder',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatSlots: vi.fn().mockResolvedValue([]),
      chatFolders: vi.fn().mockResolvedValue([]),
      kirocrewConfig: vi.fn().mockResolvedValue({ dashboard: {} }),
      sessionsSearch: vi.fn().mockResolvedValue({ sessions: [] }),
    },
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

const TS = new Date(Date.now() - 60_000).toISOString()
const slot = (key: string, extra: Partial<ChatSlot> = {}): ChatSlot => ({
  key, title: `${key} title`, messages: 1, running: false, mode: '', created: TS, last_ts: TS,
  agent: 'kirocrew-conductor', ...extra,
} as ChatSlot)

const CODE = slot('code', {
  code_channel: {
    channel_name: 'bump-node-runtime-and-lockfile',
    status: 'active',
    web_url: 'https://slack.com/app_redirect?channel=C0CODE2&team=T0TEAM1',
  },
} as Partial<ChatSlot>)
const PLAIN = slot('plain')

function renderSidebar(slots: ChatSlot[]) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: null, messages: [], slotStatusDetail: {}, history: [], historyHasMore: false,
      subagents: {}, toolLog: [], slotActivity: {}, slotHistory: [], slotMessages: {},
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  qc.setQueryData(['kirocrewConfig'], { dashboard: {} })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar slots={slots} activeSlot={null} unreadSlots={[]} history={[]} historyHasMore={false}
              defaultAgent="default" installedAgents={[]} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The flex-shrink weight a Tailwind class list gives an item (1 when unset). */
function shrinkWeight(el: Element): number {
  const cls = (el.getAttribute('class') ?? '').split(/\s+/).filter(c => !c.includes(':'))
  for (const c of cls) {
    if (c === 'shrink-0' || c === 'flex-none') return 0
    const m = /^shrink-\[(\d+)\]$/.exec(c)
    if (m) return Number(m[1])
    if (c === 'shrink') return 1
  }
  return 1
}

function metaLine(title: string) {
  const row = screen.getByText(title).closest('.session-row') as HTMLElement
  const line = row.querySelector('.session-agent-label') as HTMLElement
  return {
    agent: within(line).getByTestId('session-row-agent'),
    chip: line.querySelector('[data-testid="code-channel-badge-row"]'),
    time: within(line).getByTestId('session-row-time'),
  }
}

describe('code channel row: the Slack mark alone', () => {
  it('the row carries one link to the channel, named exactly for it', () => {
    renderSidebar([CODE])
    const { chip } = metaLine('code title')
    expect(chip).not.toBeNull()
    expect(chip!.tagName).toBe('A')
    expect(chip).toHaveAttribute('href', 'https://slack.com/app_redirect?channel=C0CODE2&team=T0TEAM1')
    expect(chip).toHaveAttribute('aria-label', 'Open #bump-node-runtime-and-lockfile in Slack')
    expect(chip).toHaveAttribute('title', 'Open #bump-node-runtime-and-lockfile in Slack')
  })

  it('no channel-name text renders anywhere in the row', () => {
    renderSidebar([CODE])
    const row = screen.getByText('code title').closest('.session-row') as HTMLElement
    expect(row.textContent).not.toContain('bump-node-runtime')
    expect(within(row).queryByTestId('code-channel-name')).toBeNull()
    expect(within(row).queryByTestId('code-channel-link-glyph')).toBeNull()
    expect(metaLine('code title').chip!.textContent).toBe('')
  })

  it('the Slack mark has a fixed size and never shrinks', () => {
    renderSidebar([CODE])
    const chip = metaLine('code title').chip as HTMLElement
    const mark = within(chip).getByTestId('code-channel-slack-mark')
    expect(shrinkWeight(chip)).toBe(0)
    expect(shrinkWeight(mark)).toBe(0)
    expect(mark.style.width).toBe('10px')
    expect(mark.style.height).toBe('10px')
  })

  it('the agent name yields first; the time never shrinks and is never ellipsized', () => {
    renderSidebar([CODE])
    const { agent, time } = metaLine('code title')
    expect(agent).toHaveClass('truncate', 'min-w-0')
    expect(shrinkWeight(agent)).toBeGreaterThan(1)
    expect(shrinkWeight(time)).toBe(0)
    expect(time).not.toHaveClass('truncate')
  })

  it('an ordinary row keeps its agent name exactly as before and has no mark', () => {
    renderSidebar([PLAIN])
    const row = screen.getByText('plain title').closest('.session-row') as HTMLElement
    const agent = within(row).getByTestId('session-row-agent')
    expect(shrinkWeight(agent)).toBe(0)
    expect(row.querySelector('[data-testid="code-channel-badge-row"]')).toBeNull()
  })
})
