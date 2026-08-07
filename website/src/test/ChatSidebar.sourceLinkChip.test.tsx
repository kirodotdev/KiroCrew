/**
 * Test: the sidebar PR / issue chip opens the pull request IN THE APP.
 *
 * The chip used to leave for the provider's website in a new tab. It now
 * switches to the session it belongs to and asks the consumer to reveal the link
 * in that session's side panel (`onOpenSource`), so a PR is read without leaving
 * the dashboard.
 *
 * The chip is still a real anchor with a real href, and four cases deliberately
 * fall through to plain link navigation instead — each pinned below:
 *   - no `onOpenSource` (a surface with no side panel: the sessions embed)
 *   - a modifier click (the user explicitly asked for a new tab)
 *   - offline (the panel loads a PR through the LOCAL provider CLI)
 * plus the row-switch it must never trigger by bubbling.
 *
 * Mock setup mirrors ChatSidebar.offline.test.tsx: the chat slice's switchSlot
 * thunk is mocked so we can assert whether a click reached the row handler.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, createEvent } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { switchSlotMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return { ...actual, switchSlot: (...args: unknown[]) => switchSlotMock(...args) }
})

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

const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/634'
const ISSUE_URL = 'https://github.com/kirodotdev/KiroCrew/issues/701'
/** The chip on the session that is ALREADY active. */
const ACTIVE_PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/12'

const slots = [
  {
    key: 's1', title: 'Active', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
    source_links: [{ provider: 'github', number: 12, url: ACTIVE_PR_URL, state: 'open', kind: 'change' }],
    source_links_total: 1,
  },
  {
    key: 's2', title: 'PR session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
    source_links: [
      { provider: 'github', number: 634, url: PR_URL, state: 'open', ci: 'passed' },
      { provider: 'github', number: 701, url: ISSUE_URL, kind: 'issue' },
    ],
    source_links_total: 2,
  },
] as unknown as ChatSlot[]

function renderSidebar(opts: {
  onOpenSource?: (slot: string, link: { url: string; kind: 'change' | 'issue' }) => boolean
  connected?: boolean
  rows?: ChatSlot[]
} = {}) {
  const rows = opts.rows ?? slots
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: opts.connected ?? true,
      slots: rows,
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
              slots={rows} activeSlot={'s1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
              onOpenSource={opts.onOpenSource}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

/** The chip's title now names the panel and the modifier escape hatch. Built
 *  here rather than matched loosely, so the tooltip's promise is asserted too.
 *  `platformShortcut` is deterministic under jsdom: navigator.platform is '',
 *  so the non-mac branch yields 'Ctrl+click'. */
const chipTitle = (url: string) => `Open ${url} in the side panel (Ctrl+click to open it in the browser)`
const chip = (url = PR_URL) => screen.getByTitle(chipTitle(url))
/** Click and report whether the anchor's own navigation was suppressed. */
const clickChip = (el: HTMLElement, init?: MouseEventInit): boolean => {
  const event = createEvent.click(el, init)
  fireEvent(el, event)
  return event.defaultPrevented
}
/** The consumer took the link (the normal case). */
const took = () => vi.fn(() => true)

describe('ChatSidebar – PR chip', () => {
  beforeEach(() => switchSlotMock.mockClear())

  it('is still an anchor carrying the provider url', () => {
    // Link semantics are load-bearing for the fall-through cases below, for
    // "Copy link address", and for assistive tech.
    renderSidebar({ onOpenSource: took() })
    const a = chip()
    expect(a.tagName).toBe('A')
    expect(a).toHaveAttribute('href', PR_URL)
    expect(a).toHaveAttribute('target', '_blank')
    expect(a.getAttribute('rel')).toContain('noopener')
    expect(a).toHaveTextContent('#634')
  })

  it('switches to the chip\'s session and reveals the pull request in the panel', () => {
    const onOpenSource = took()
    renderSidebar({ onOpenSource })
    expect(clickChip(chip())).toBe(true) // no navigation to github.com
    expect(switchSlotMock).toHaveBeenCalledWith('s2')
    expect(onOpenSource).toHaveBeenCalledWith('s2', { url: PR_URL, kind: 'change' })
  })

  it('reports an issue chip as kind "issue" so the Issues tab is opened', () => {
    const onOpenSource = took()
    renderSidebar({ onOpenSource })
    expect(clickChip(chip(ISSUE_URL))).toBe(true)
    expect(onOpenSource).toHaveBeenCalledWith('s2', { url: ISSUE_URL, kind: 'issue' })
  })

  it('does not re-switch when the chip is on the session already open', () => {
    // Re-dispatching switchSlot for the active slot refetches the transcript and
    // flashes the loading state for nothing.
    const onOpenSource = took()
    renderSidebar({ onOpenSource })
    expect(clickChip(chip(ACTIVE_PR_URL))).toBe(true)
    expect(switchSlotMock).not.toHaveBeenCalled()
    expect(onOpenSource).toHaveBeenCalledWith('s1', { url: ACTIVE_PR_URL, kind: 'change' })
  })

  it('never lets a chip click reach the row underneath', () => {
    renderSidebar({ onOpenSource: took() })
    // Positive control first: the row handler IS reachable in this harness, so
    // the negative assertion below is meaningful and not vacuous.
    const row = chip().closest('.session-row') as HTMLElement
    fireEvent.click(row)
    expect(switchSlotMock).toHaveBeenCalledWith('s2')

    // A chip click switches to s2 exactly once — via the chip, not by bubbling
    // (which would fire the row handler on top of it).
    switchSlotMock.mockClear()
    clickChip(chip())
    expect(switchSlotMock).toHaveBeenCalledTimes(1)
  })

  it('lets a modifier click through to the provider in a new tab', () => {
    const onOpenSource = took()
    renderSidebar({ onOpenSource })
    for (const modifier of ['metaKey', 'ctrlKey', 'shiftKey', 'altKey'] as const) {
      expect(clickChip(chip(), { [modifier]: true })).toBe(false)
    }
    expect(onOpenSource).not.toHaveBeenCalled()
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('falls back to the provider link on a surface with no side panel', () => {
    // `onOpenSource` omitted — the /embed/sessions list has no panel to reveal into.
    renderSidebar()
    expect(clickChip(chip())).toBe(false)
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('falls back to the provider link while the gateway is offline', () => {
    // The panel loads a PR through the local provider CLI, so with the gateway
    // down the provider's own page is the only thing that can answer.
    const onOpenSource = took()
    renderSidebar({ onOpenSource, connected: false })
    expect(clickChip(chip())).toBe(false)
    expect(onOpenSource).not.toHaveBeenCalled()
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('falls back to the provider link when the panel cannot resolve the url', () => {
    // The panel re-parses the url against ITS OWN host allowlist, which is loaded
    // from dashboard config and is empty until that query resolves — so a
    // self-hosted chip the backend scan accepted can still be unresolvable here.
    // Suppressing navigation on that path would make the click do nothing at all.
    const onOpenSource = vi.fn(() => false)
    renderSidebar({ onOpenSource })
    expect(clickChip(chip())).toBe(false)
    // Asked, declined, and handed back to the anchor — not skipped like the
    // offline/no-panel cases above.
    expect(onOpenSource).toHaveBeenCalledWith('s2', { url: PR_URL, kind: 'change' })
  })
})

/**
 * A terminal pull request can never merge, so its CI rollup is moot and only the
 * lifecycle glyph is meaningful. `closed` is the case that actually hangs: a PR
 * closed before its checks were approved to run keeps a PENDING rollup forever,
 * which the backend faithfully projects as `ci: "running"` — so a chip gated
 * only on `merged` spins its spinner indefinitely on work nobody is waiting for.
 *
 * The `merged` half is asserted here too. Both terminal states plus a live
 * control live in one table.
 */
describe('ChatSidebar – terminal PR chips suppress CI', () => {
  const url = (n: number) => `https://github.com/kirodotdev/KiroCrew/pull/${n}`

  function stateRows(): ChatSlot[] {
    return [
      { key: 's1', title: 'Other', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
      {
        key: 's2', title: 'PR states', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
        source_links: [
          // Every chip carries ci: 'running' so the ONLY variable is `state`.
          { provider: 'github', number: 993, url: url(993), state: 'closed', ci: 'running' },
          { provider: 'github', number: 994, url: url(994), state: 'merged', ci: 'running' },
          { provider: 'github', number: 995, url: url(995), state: 'open', ci: 'running' },
          // No `state` at all: the provider status has not been read yet, which
          // is NOT terminal — CI must still render.
          { provider: 'github', number: 996, url: url(996), ci: 'running' },
        ],
        source_links_total: 4,
      },
    ] as unknown as ChatSlot[]
  }

  const spinner = (n: number) =>
    chip(url(n)).querySelector('[aria-label="Checks running"]')

  it.each([
    ['closed', 993],
    ['merged', 994],
  ])('hides the running-checks spinner on a %s chip', (_state, number) => {
    renderSidebar({ rows: stateRows() })
    expect(spinner(number as number)).toBeNull()
  })

  it('still shows the spinner while the PR is live or its state is unknown', () => {
    renderSidebar({ rows: stateRows() })
    // Positive control: proves the fixture really does carry ci: 'running' and
    // the assertions above are not passing because nothing rendered.
    expect(spinner(995)).not.toBeNull()
    expect(spinner(996)).not.toBeNull()
  })

  it('keeps the closed chip\'s own lifecycle label', () => {
    renderSidebar({ rows: stateRows() })
    // The spinner goes away; the terminal signal must not.
    expect(chip(url(993))).toHaveTextContent('closed')
    expect(chip(url(994)).querySelector('[aria-label="Merged"]')).not.toBeNull()
  })

  it.each(['passed', 'failed'] as const)('hides a %s CI glyph on a closed chip too', (ci) => {
    const rows = [
      { key: 's1', title: 'Other', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z' },
      {
        key: 's2', title: 'PR states', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
        source_links: [{ provider: 'github', number: 993, url: url(993), state: 'closed', ci }],
        source_links_total: 1,
      },
    ] as unknown as ChatSlot[]
    renderSidebar({ rows })
    const chipEl = chip(url(993))
    expect(chipEl.querySelector('[aria-label="Checks passed"]')).toBeNull()
    expect(chipEl.querySelector('[aria-label="Checks failed"]')).toBeNull()
  })
})
