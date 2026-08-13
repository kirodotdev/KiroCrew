/**
 * Test: the sidebar's "+N" overflow chip expands the full link list and collapses again.
 *
 * The slots payload caps chips at three PER KIND (state.py's
 * `_SERIALIZED_SOURCE_LINKS_PER_SLOT`), so the links behind "+N" are not on the
 * client at all — expanding has to fetch them. These tests pin the four
 * behaviours that makes load-bearing: the fetch happens, the revealed chips
 * render, collapsing returns to the budgeted set, and a failed fetch does NOT
 * expand into the three links we already had (which would read as "this session
 * only has three").
 *
 * Harness mirrors ChatSidebar.issueChip.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// `vi.mock` factories are hoisted above every top-level statement, so the spy
// has to be hoisted with them or the factory closes over an uninitialised const.
const { chatSlotSourceLinks } = vi.hoisted(() => ({ chatSlotSourceLinks: vi.fn() }))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession', 'chatTags', 'chatFolders', 'tagColumns',
          // These tests await async work, so react-query's mount refetch lands
          // MID-test and its result is rendered — unlike the sibling chip tests,
          // which finish first. The sidebar spreads these payloads as arrays, so
          // `[]` is the only resolution that survives being rendered.
        ].map(k => [k, vi.fn().mockResolvedValue([])]),
      ),
      chatSlotSourceLinks,
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

const PR = (n: number) => `https://github.com/kirodotdev/KiroCrew/pull/${n}`
const ISSUE = (n: number) => `https://github.com/kirodotdev/KiroCrew/issues/${n}`

/** What the slots payload carries: the per-kind budget, three PRs + one issue. */
const budgeted = [
  { provider: 'github', number: 1, url: PR(1), state: 'open', kind: 'change' },
  { provider: 'github', number: 2, url: PR(2), state: 'open', kind: 'change' },
  { provider: 'github', number: 3, url: PR(3), state: 'open', kind: 'change' },
  { provider: 'github', number: 90, url: ISSUE(90), kind: 'issue' },
]
/** What the unbudgeted read returns: the same four plus the two behind "+2". */
const full = [
  ...budgeted.slice(0, 3),
  { provider: 'github', number: 4, url: PR(4), state: 'merged', kind: 'change' },
  budgeted[3],
  { provider: 'github', number: 91, url: ISSUE(91), kind: 'issue' },
]

const slots = [
  {
    key: 's1', title: 'Busy session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
    source_links: budgeted,
    source_links_total: 6,
  },
] as unknown as ChatSlot[]

/** The same session after the agent swapped one pull request for another: the
 *  TOTAL is unchanged, which is precisely what a count-based cache tag misses. */
const swapped = [
  {
    ...slots[0],
    source_links: [
      { provider: 'github', number: 9, url: PR(9), state: 'open', kind: 'change' },
      ...budgeted.slice(1),
    ],
    source_links_total: 6,
  },
] as unknown as ChatSlot[]

function renderSidebar(initial: ChatSlot[] = slots) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: true,
      slots: initial,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: 's1',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Seeded because these tests await async work: an unseeded query would resolve
  // the `{}` the api mock returns mid-test, and the sidebar spreads these as
  // arrays.
  qc.setQueryData(['chat-folders'], [])
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  const tree = (value: ChatSlot[]) => (
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={value} activeSlot={'s1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>
  )
  const { rerender } = render(tree(initial))
  /** Simulate a slots push: the same mounted row, a newer payload. */
  return { push: (next: ChatSlot[]) => rerender(tree(next)) }
}

describe('ChatSidebar – source chip overflow expand', () => {
  beforeEach(() => {
    chatSlotSourceLinks.mockReset()
    chatSlotSourceLinks.mockResolvedValue({ links: full, total: 6 })
  })

  it('exposes the overflow chip as a collapsed expander button', () => {
    renderSidebar()
    const overflow = screen.getByTestId('session-source-overflow')
    expect(overflow.tagName).toBe('BUTTON')
    expect(overflow).toHaveTextContent('+2')
    // Collapsed state must be announced, not just drawn.
    expect(overflow).toHaveAttribute('aria-expanded', 'false')
    expect(overflow).toHaveAttribute('aria-label', '2 more pull requests or issues in this session')
    expect(screen.queryByTestId('session-source-collapse')).toBeNull()
  })

  it('fetches the unbudgeted list on click and reveals the hidden chips', async () => {
    renderSidebar()
    // Only the budgeted links are on screen to start with.
    expect(screen.queryByTestId('session-issue-chip-91')).toBeNull()

    await userEvent.click(screen.getByTestId('session-source-overflow'))

    await waitFor(() => expect(screen.getByTestId('session-issue-chip-91')).toBeInTheDocument())
    expect(chatSlotSourceLinks).toHaveBeenCalledWith('s1')
    // The link that was behind "+2" renders with its full decoration.
    const revealed = screen.getByTitle(`Open ${PR(4)} in the side panel (Ctrl+click to open it in the browser)`)
    expect(revealed).toHaveTextContent('#4')
    expect(revealed.querySelector('[aria-label="Merged"]')).not.toBeNull()
    // Nothing is hidden any more, so the "+N" chip is gone and a collapse
    // control takes its place at the end of the strip.
    expect(screen.queryByTestId('session-source-overflow')).toBeNull()
    const collapse = screen.getByTestId('session-source-collapse')
    expect(collapse).toHaveAttribute('aria-expanded', 'true')
    expect(collapse).toHaveAttribute('aria-label', 'Show fewer')
  })

  it('collapses back to the budgeted strip and re-expands without refetching', async () => {
    renderSidebar()
    await userEvent.click(screen.getByTestId('session-source-overflow'))
    await waitFor(() => expect(screen.getByTestId('session-source-collapse')).toBeInTheDocument())

    await userEvent.click(screen.getByTestId('session-source-collapse'))

    expect(screen.queryByTestId('session-issue-chip-91')).toBeNull()
    expect(screen.getByTestId('session-source-overflow')).toHaveTextContent('+2')
    expect(screen.queryByTestId('session-source-collapse')).toBeNull()

    // Second expand is served from the cache the first one filled — the total it
    // was fetched at still matches the payload's.
    await userEvent.click(screen.getByTestId('session-source-overflow'))
    await waitFor(() => expect(screen.getByTestId('session-issue-chip-91')).toBeInTheDocument())
    expect(chatSlotSourceLinks).toHaveBeenCalledTimes(1)
  })

  it('carries keyboard focus onto the control that replaces the one clicked', async () => {
    renderSidebar()
    // Expanding UNMOUNTS the button that was activated, so leaving focus where
    // it fell drops a keyboard user to the top of the document mid-row.
    await userEvent.click(screen.getByTestId('session-source-overflow'))
    await waitFor(() => expect(screen.getByTestId('session-source-collapse')).toHaveFocus())

    await userEvent.click(screen.getByTestId('session-source-collapse'))
    await waitFor(() => expect(screen.getByTestId('session-source-overflow')).toHaveFocus())
  })

  it('stays collapsed when the fetch fails and offers a retry', async () => {
    chatSlotSourceLinks.mockRejectedValueOnce(new Error('offline'))
    renderSidebar()

    await userEvent.click(screen.getByTestId('session-source-overflow'))

    const overflow = await screen.findByTitle('Could not load the rest. Click to retry.')
    // Critically NOT expanded: showing the three links we already had would read
    // as "this session only has three".
    expect(overflow).toHaveTextContent('+2')
    expect(screen.queryByTestId('session-source-collapse')).toBeNull()
    expect(screen.queryByTestId('session-issue-chip-91')).toBeNull()

    // The same button retries, and the retry succeeds.
    await userEvent.click(overflow)
    await waitFor(() => expect(screen.getByTestId('session-issue-chip-91')).toBeInTheDocument())
    expect(chatSlotSourceLinks).toHaveBeenCalledTimes(2)
  })

  it('names the failure in the accessible name, not just the tooltip', async () => {
    chatSlotSourceLinks.mockRejectedValueOnce(new Error('offline'))
    renderSidebar()

    await userEvent.click(screen.getByTestId('session-source-overflow'))

    // An aria-label OUTRANKS the title in the accessible-name computation, so a
    // label left on the success wording would have a screen reader announce "2
    // more pull requests…" on a button that just failed.
    await waitFor(() => expect(screen.getByTestId('session-source-overflow'))
      .toHaveAttribute('aria-label', 'Could not load the rest. Click to retry.'))
  })

  it('treats a malformed response as a failure instead of crashing the sidebar', async () => {
    // A 200 whose body lost `links` would put `undefined` where the render
    // filters an array, and an exception in render unmounts the whole sidebar —
    // the session list would vanish, not just this row's chips.
    chatSlotSourceLinks.mockResolvedValueOnce({ total: 6 })
    renderSidebar()

    await userEvent.click(screen.getByTestId('session-source-overflow'))

    const overflow = await screen.findByTitle('Could not load the rest. Click to retry.')
    expect(overflow).toHaveTextContent('+2')
    // The row — and the rest of the list — is still rendered.
    expect(screen.getByTestId('session-issue-chip-90')).toBeInTheDocument()
  })

  it('drops an expanded strip whose payload moved, even at an unchanged total', async () => {
    const { push } = renderSidebar()
    await userEvent.click(screen.getByTestId('session-source-overflow'))
    await waitFor(() => expect(screen.getByTestId('session-issue-chip-91')).toBeInTheDocument())

    // One pull request dropped out of the session as another appeared, so the
    // TOTAL is unchanged — the case a count-based cache tag cannot detect. The
    // fetched list is now superseded and must not stay on screen.
    push(swapped)

    await waitFor(() => expect(screen.queryByTestId('session-issue-chip-91')).toBeNull())
    // Fell back to the live budgeted strip, which re-offers the overflow.
    expect(screen.getByTitle(`Open ${PR(9)} in the side panel (Ctrl+click to open it in the browser)`))
      .toBeInTheDocument()
    expect(screen.getByTestId('session-source-overflow')).toHaveTextContent('+2')
    expect(screen.queryByTestId('session-source-collapse')).toBeNull()

    // And re-expanding refetches rather than serving the superseded set.
    chatSlotSourceLinks.mockResolvedValue({ links: [...swapped[0].source_links!, full[3], full[5]], total: 6 })
    await userEvent.click(screen.getByTestId('session-source-overflow'))
    await waitFor(() => expect(chatSlotSourceLinks).toHaveBeenCalledTimes(2))
  })
})
