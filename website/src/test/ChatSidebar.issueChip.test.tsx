/**
 * Test: the sidebar chip row splits by the `kind` discriminator.
 *
 * An issue chip renders as `#123` with NO ci/state decoration (the chip-status
 * cache is pull-request-only, so an issue has nothing truthful to colour). A PR
 * chip keeps its full ci/state decoration — including when `kind` is ABSENT,
 * which is what a payload without the discriminator looks like.
 *
 * Harness mirrors ChatSidebar.sourceLinkChip.test.tsx.
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

/** The chip's title now names the panel and the modifier escape hatch. Built
 *  here rather than matched loosely, so the tooltip's promise is asserted too.
 *  `platformShortcut` is deterministic under jsdom: navigator.platform is '',
 *  so the non-mac branch yields 'Ctrl+click'. */
const chipTitle = (url: string) => `Open ${url} in the side panel (Ctrl+click to open it in the browser)`
const ISSUE_URL = 'https://github.com/kirodotdev/KiroCrew/issues/701'
const MR_ISSUE_URL = 'https://gitlab.com/acme/service/-/issues/8'
const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/634'
const LEGACY_PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/500'

const slots = [
  {
    key: 's1', title: 'Mixed session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
    source_links: [
      { provider: 'github', number: 634, url: PR_URL, state: 'open', ci: 'failed', kind: 'change' },
      // No `kind`: the wire default. Must render as a PR chip, not an issue chip.
      { provider: 'github', number: 500, url: LEGACY_PR_URL, state: 'merged' },
      { provider: 'github', number: 701, url: ISSUE_URL, kind: 'issue' },
      { provider: 'gitlab', number: 8, url: MR_ISSUE_URL, kind: 'issue' },
    ],
    source_links_total: 6,
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
  qc.setQueryData(['chat-folders'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={'s1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatSidebar – issue chips', () => {
  it('renders an issue chip as #number with no ci/state decoration', () => {
    renderSidebar()
    const chip = screen.getByTestId('session-issue-chip-701')
    expect(chip.tagName).toBe('A')
    expect(chip).toHaveAttribute('href', ISSUE_URL)
    expect(chip).toHaveAttribute('target', '_blank')
    expect(chip.getAttribute('title')).toContain(`Open ${ISSUE_URL} in the side panel`)
    expect(chip.getAttribute('rel')).toContain('noopener')
    expect(chip).toHaveTextContent('#701')
    // The PR chip's CI / merge markers carry aria-labels; an issue chip has none.
    expect(chip.querySelector('[aria-label="Checks failed"]')).toBeNull()
    expect(chip.querySelector('[aria-label="Checks passed"]')).toBeNull()
    expect(chip.querySelector('[aria-label="Merged"]')).toBeNull()
    // Negative control: a PR chip is NOT rendered through the issue branch.
    expect(screen.queryByTestId('session-issue-chip-634')).toBeNull()
    expect(screen.queryByTestId('session-issue-chip-500')).toBeNull()
  })

  it('uses # for a GitLab issue too (only merge requests use !)', () => {
    renderSidebar()
    const chip = screen.getByTestId('session-issue-chip-8')
    expect(chip).toHaveAttribute('href', MR_ISSUE_URL)
    expect(chip).toHaveTextContent('#8')
    expect(chip).not.toHaveTextContent('!8')
  })

  it('keeps the PR chip decorated, including when kind is absent', () => {
    renderSidebar()
    const pr = screen.getByTitle(chipTitle(PR_URL))
    expect(pr).toHaveTextContent('#634')
    expect(pr.querySelector('[aria-label="Checks failed"]')).not.toBeNull()

    // `kind` absent === 'change': the merged marker still renders.
    const legacy = screen.getByTitle(chipTitle(LEGACY_PR_URL))
    expect(legacy).toHaveTextContent('#500')
    expect(legacy.querySelector('[aria-label="Merged"]')).not.toBeNull()
  })

  it('keeps the +N overflow chip and words it for a mixed list', () => {
    renderSidebar()
    // 6 total, 4 rendered.
    expect(screen.getByTitle('2 more pull requests or issues in this session')).toHaveTextContent('+2')
  })
})
