/**
 * A Slack code channel row's meta line (agent name, the channel chip, the time)
 * on a short sidebar: who gives up width first.
 *
 * The order is: the agent name yields first, then the channel name inside the
 * chip, down to "#" plus a few characters; the Slack mark keeps a fixed size and
 * never shrinks; the time never shrinks and is never ellipsized. jsdom lays
 * nothing out, so this pins the flex weights and floors that make that order;
 * the pixels (the mark not cut in half, the time fully visible) are measured on
 * the real build by scripts/capture-slack-code-channel-row-fit.mjs.
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

describe('code channel row: shrink order on a short meta line', () => {
  it('the agent name yields before the channel chip does', () => {
    renderSidebar([CODE])
    const { agent, chip } = metaLine('code title')
    expect(chip).not.toBeNull()
    expect(agent).toHaveClass('truncate', 'min-w-0')
    expect(shrinkWeight(agent)).toBeGreaterThan(shrinkWeight(chip!) * 100)
    expect(shrinkWeight(chip!)).toBeGreaterThan(0)
    // No cap on the chip's share of the line: a cap would cut the channel name
    // while the agent name still had width to give.
    expect(chip!.getAttribute('class')).not.toMatch(/(^|\s)max-w-/)
  })

  it('the channel name truncates inside the chip but keeps "#" and a few characters', () => {
    renderSidebar([CODE])
    const chip = metaLine('code title').chip as HTMLElement
    const name = within(chip).getByTestId('code-channel-name')
    expect(name.textContent).toBe('#bump-node-runtime-and-lockfile')
    expect(name).toHaveClass('truncate', 'min-w-0')
    // The slot holding it has a readable floor; below it the slot leaves whole
    // (the chip wraps it onto a clipped line) rather than showing a bare "#…".
    const slot = within(chip).getByTestId('code-channel-text')
    expect(slot).toHaveClass('basis-0', 'grow', 'min-w-[5ch]', 'overflow-hidden')
    expect(chip).toHaveClass('flex-wrap', 'overflow-hidden', 'h-4')
    // The chip itself has a floor too, so it cannot shrink below its Slack mark.
    expect(chip.getAttribute('class')).toMatch(/(^|\s)min-w-\[\d+px\](\s|$)/)
  })

  it('the link glyph leaves before the channel name loses a character', () => {
    renderSidebar([CODE])
    const slot = within(metaLine('code title').chip as HTMLElement).getByTestId('code-channel-text')
    const name = within(slot).getByTestId('code-channel-name')
    const glyph = within(slot).getByTestId('code-channel-link-glyph')
    // Same wrapping line, glyph after the name, and the name at its FULL width
    // (no zero basis): a name and glyph that do not fit wrap the glyph away
    // first, and only then does the name, alone on its line, truncate.
    expect(slot).toHaveClass('flex-wrap', 'h-[14px]')
    expect(glyph.parentElement).toBe(slot)
    expect(name.compareDocumentPosition(glyph) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(name).not.toHaveClass('basis-0')
    expect(shrinkWeight(glyph)).toBe(0)
  })

  it('the Slack mark has a fixed size and never shrinks', () => {
    renderSidebar([CODE])
    const mark = within(metaLine('code title').chip as HTMLElement).getByTestId('code-channel-slack-mark')
    expect(shrinkWeight(mark)).toBe(0)
    expect(mark.style.width).toBe('10px')
    expect(mark.style.height).toBe('10px')
    // First in the chip, so the chip's wrap (which drops text) never takes it.
    expect(mark.parentElement?.firstElementChild).toBe(mark)
  })

  it('the time never shrinks and is never ellipsized', () => {
    renderSidebar([CODE])
    const { time } = metaLine('code title')
    expect(shrinkWeight(time)).toBe(0)
    expect(time).not.toHaveClass('truncate')
  })

  it('an ordinary row keeps its agent name exactly as before', () => {
    renderSidebar([PLAIN])
    const row = screen.getByText('plain title').closest('.session-row') as HTMLElement
    const agent = within(row).getByTestId('session-row-agent')
    expect(shrinkWeight(agent)).toBe(0)
    expect(row.querySelector('[data-testid="code-channel-badge-row"]')).toBeNull()
  })
})
