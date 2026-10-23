/**
 * A Slack code channel's crew in the real sidebar: the conductor row is all that
 * shows by default, its workers fold under it, and its status line speaks for
 * them worst-first -- a hidden worker waiting on an approval must surface on it.
 * The pure rules are pinned in pages/chat-sidebar/codeChannelCrew.test.ts.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { i18nT } from '../i18n/t'
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

function renderSidebar(slots: ChatSlot[], unread: string[] = [], active: string | null = null) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: unread, updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: active, messages: [], slotStatusDetail: {}, history: [], historyHasMore: false,
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
            <ChatSidebar slots={slots} activeSlot={active} unreadSlots={unread} history={[]} historyHasMore={false}
              defaultAgent="default" installedAgents={[]} />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}


const CHANNEL = {
  channel_name: 'fix-kirocrew-issue-22452', status: 'active' as const,
  web_url: 'https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1',
}
const conductorRow = (extra: Partial<ChatSlot> = {}) => slot('cond', { code_channel: CHANNEL, ...extra } as Partial<ChatSlot>)
const workerRow = (key: string, extra: Partial<ChatSlot> = {}) =>
  slot(key, { agent: 'kirocrew-worker', parent: { slot: 'cond', key: 'cond' }, ...extra } as Partial<ChatSlot>)

const rowOf = (title: string): HTMLElement | null => (screen.queryByText(title)?.closest('.session-row') as HTMLElement | null) ?? null

describe('code channel crew: workers fold under the conductor row', () => {
  beforeEach(() => localStorage.clear())

  it('collapsed by default: only the conductor row shows, with a fold naming its workers', () => {
    renderSidebar([conductorRow(), workerRow('w1'), workerRow('w2')])
    expect(rowOf('cond title')).not.toBeNull()
    expect(rowOf('w1 title')).toBeNull()
    expect(rowOf('w2 title')).toBeNull()
    const toggle = within(rowOf('cond title')!).getByTestId('code-channel-crew-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAccessibleName('Show 2 worker sessions')
  })

  it('expanding shows every worker, and the open state persists across a remount', () => {
    renderSidebar([conductorRow(), workerRow('w1'), workerRow('w2')])
    fireEvent.click(within(rowOf('cond title')!).getByTestId('code-channel-crew-toggle'))
    expect(rowOf('w1 title')).not.toBeNull()
    expect(rowOf('w2 title')).not.toBeNull()
    expect(within(rowOf('cond title')!).getByTestId('code-channel-crew-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(JSON.parse(localStorage.getItem('mc-sidebar-conductor-expanded') ?? '[]')).toContain('cond')
    cleanup()
    renderSidebar([conductorRow(), workerRow('w1'), workerRow('w2')])
    expect(rowOf('w1 title')).not.toBeNull()
  })

  it('a hidden worker waiting on an approval surfaces on the conductor row', () => {
    renderSidebar([
      conductorRow({ running: true }),
      workerRow('w1', { running: true }),
      workerRow('w2', { pending_approval: true, running: true, last_message: 'approve git push' }),
    ])
    expect(rowOf('w2 title')).toBeNull()
    const line = within(rowOf('cond title')!).getByTestId('code-channel-crew-state')
    expect(line).toHaveAttribute('data-crew-state', 'needs_you')
    expect(line.textContent).toBe('Waiting on you · approve git push')
  })

  it('a hidden worker\'s unread result lights the conductor row\'s attention dot', () => {
    renderSidebar([conductorRow(), workerRow('w1')], ['w1'])
    expect(within(rowOf('cond title')!).getByTestId('code-channel-crew-attention'))
      .toHaveAccessibleName(i18nT('pages.chatSidebar.agent_finished_your_turn'))
  })

  it('the attention dot shows even while the conductor has a state line of its own', () => {
    renderSidebar([conductorRow({ running: true }), workerRow('w1')], ['w1'])
    expect(within(rowOf('cond title')!).getByTestId('code-channel-crew-attention')).toBeInTheDocument()
  })

  it('the open session is never folded away', () => {
    renderSidebar([conductorRow(), workerRow('w1'), workerRow('w2')], [], 'w2')
    expect(rowOf('w2 title')).not.toBeNull()
    expect(rowOf('w1 title')).toBeNull()
    expect(within(rowOf('cond title')!).getByTestId('code-channel-crew-toggle')).toHaveAccessibleName('Show 1 worker session')
  })

  it.each([
    [{ interrupted: true }, 'interrupted', '1 interrupted'],
    [{ running: true }, 'working', '1 working'],
  ] as const)('a hidden worker %o reads as %s on the conductor row', (fields, key, text) => {
    renderSidebar([conductorRow(), workerRow('w1', fields as Partial<ChatSlot>)])
    const line = within(rowOf('cond title')!).getByTestId('code-channel-crew-state')
    expect(line).toHaveAttribute('data-crew-state', key)
    expect(line.textContent).toBe(text)
  })

  it('an idle crew adds no state line', () => {
    renderSidebar([conductorRow(), workerRow('w1')])
    expect(within(rowOf('cond title')!).queryByTestId('code-channel-crew-state')).toBeNull()
  })

  it('an ordinary parent keeps its children in the list, with no fold', () => {
    renderSidebar([slot('cond'), workerRow('w1')])
    expect(rowOf('w1 title')).not.toBeNull()
    expect(screen.queryByTestId('code-channel-crew-toggle')).toBeNull()
  })
})

describe('code channel crew: the optional work_aggregate on the conductor slot', () => {
  beforeEach(() => localStorage.clear())
  const agg = (state: string, label: string, counts: Record<string, number>) =>
    ({ work_aggregate: { state, label, counts } }) as Partial<ChatSlot>

  it.each([
    ['done', '', { done: 3, total: 3 }, 'done', '3 of 3 done'],
    ['stuck', 'tests worker went quiet', { stuck: 1 }, 'stuck', '1 stuck · tests worker went quiet'],
    ['working', '2 PRs in review', { working: 4 }, 'working', '4 working · 2 PRs in review'],
    ['waiting', 'approve git push', { waiting: 1 }, 'needs_you', 'Waiting on you · approve git push'],
  ] as const)('present, %s reads on the conductor row', (state, label, counts, key, text) => {
    renderSidebar([conductorRow(agg(state, label, counts)), workerRow('w1')])
    const line = within(rowOf('cond title')!).getByTestId('code-channel-crew-state')
    expect(line).toHaveAttribute('data-crew-state', key)
    expect(line.textContent).toBe(text)
  })

  it('present and idle: no crew line', () => {
    renderSidebar([conductorRow(agg('idle', '', { total: 2 })), workerRow('w1', { running: true })])
    expect(within(rowOf('cond title')!).queryByTestId('code-channel-crew-state')).toBeNull()
  })

  it('null (no workers on the board) falls back to the slot list, and an approval still surfaces', () => {
    renderSidebar([
      conductorRow({ work_aggregate: null } as Partial<ChatSlot>),
      workerRow('w1', { pending_approval: true, running: true, last_message: 'approve git push' }),
    ])
    expect(within(rowOf('cond title')!).getByTestId('code-channel-crew-state').textContent).toBe('Waiting on you · approve git push')
  })

  it('present, a hidden worker waiting on approval still wins over "done"', () => {
    renderSidebar([
      conductorRow(agg('done', '', { done: 2, total: 3 })),
      workerRow('w1', { pending_approval: true, running: true, last_message: 'approve git push' }),
    ])
    expect(within(rowOf('cond title')!).getByTestId('code-channel-crew-state').textContent).toBe('Waiting on you · approve git push')
  })

  it('present with no worker rows on screen: the line shows, with no fold', () => {
    renderSidebar([conductorRow(agg('done', '', { done: 3, total: 3 }))])
    const row = rowOf('cond title')!
    expect(within(row).getByTestId('code-channel-crew-state').textContent).toBe('3 of 3 done')
    expect(within(row).queryByTestId('code-channel-crew-toggle')).toBeNull()
  })

  it('a malformed aggregate falls back to the slot list without breaking the row', () => {
    renderSidebar([
      conductorRow({ work_aggregate: { state: 'exploded', label: 7, counts: null } } as unknown as Partial<ChatSlot>),
      workerRow('w1', { interrupted: true }),
    ])
    expect(within(rowOf('cond title')!).getByTestId('code-channel-crew-state').textContent).toBe('1 interrupted')
  })

  it('INVARIANT: a hidden worker waiting on approval lights the attention dot under any aggregate', () => {
    for (const state of ['waiting', 'stuck', 'working', 'done', 'idle']) {
      renderSidebar([
        conductorRow(agg(state, 'from the board', { done: 1, total: 2 })),
        workerRow('w1', { pending_approval: true, running: true, last_message: 'approve git push' }),
      ])
      const row = rowOf('cond title')!
      expect(within(row).getByTestId('code-channel-crew-attention')).toHaveAttribute('data-crew-attention', 'needs_you')
      expect(within(row).getByTestId('code-channel-crew-state').textContent).toBe('Waiting on you · approve git push')
      cleanup()
    }
  })

  it('no second unread dot when the conductor row is itself unread', () => {
    renderSidebar([conductorRow(), workerRow('w1')], ['cond', 'w1'])
    expect(within(rowOf('cond title')!).queryByTestId('code-channel-crew-attention')).toBeNull()
  })

  it('an ordinary session carrying an aggregate is unchanged', () => {
    renderSidebar([slot('plain', agg('done', '', { done: 1, total: 1 }))])
    expect(within(rowOf('plain title')!).queryByTestId('code-channel-crew-state')).toBeNull()
  })
})
