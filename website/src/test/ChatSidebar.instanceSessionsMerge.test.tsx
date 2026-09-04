/**
 * Test: live sessions from connected remote instances MERGE into the Sessions
 * list by recency, rather than being appended after every local row.
 *
 * WHY THIS EXISTS AS A SIDEBAR TEST and not only a hook test: the hook returning
 * correct rows is not the property that broke. `history` arrives date-desc from
 * the backend, so the sidebar SKIPS its sort for the `date-desc` key as an
 * optimisation. Concatenating the hook's rows onto that pre-sorted array is a
 * type-correct change that silently violates the premise of that fast path — the
 * result is two sorted runs, not one — so every remote row rendered BELOW every
 * local row. That is the exact "local list with a remote list stuck on the end"
 * shape this feature exists to replace, and at the bottom of a long list it reads
 * as the feature not working at all. Only a test that asserts RENDERED ORDER
 * across the merge catches it; the hook's own spec passes either way.
 *
 * Mock scaffolding mirrors ChatSidebar.federatedSearch.test.tsx (which mirrors
 * ChatSidebar.offline.test.tsx, the owner of the mock setup).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { requestSlotReveal } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import { PREVIEW_INSTANCE_SESSIONS } from '../utils/previewFlags'

// Local history rows genuinely carry `modified` in epoch SECONDS. A remote slot
// does NOT: it carries the peer's ISO ladder, which the hook collapses into
// `modified`. Timestamps are NOW-RELATIVE so they land in real date segments:
// all three rows belong to the same bucket, so a correct list prints ONE header.
//
// The remote row's `created` is deliberately 30 days old while its last activity
// is minutes ago. That is the exact shape that produced duplicate `YESTERDAY` /
// `LAST 7 DAYS` headers: ranking read the ladder, but the segment header read
// `modified ?? created`, so an absent `modified` segmented the row by creation and
// the bucket flipped mid-list.
const NOW_S = Math.floor(Date.now() / 1000)
const LOCAL_NEWER = NOW_S - 60
const LOCAL_OLDER = NOW_S - 180

const { instanceChatSlotsMock, listInstancesMock, chatFoldersMock } = vi.hoisted(() => ({
  instanceChatSlotsMock: vi.fn().mockResolvedValue([
    {
      key: 'chat-9',
      title: 'REMOTE middle row',
      // Last activity ~2 min ago: between the two local rows.
      last_turn_ts: new Date(Date.now() - 120_000).toISOString(),
      // Created a month ago — a DIFFERENT date bucket than the activity above.
      created: new Date(Date.now() - 30 * 86_400_000).toISOString(),
      agent: 'default',
    },
  ]),
  listInstancesMock: vi.fn().mockResolvedValue({
    instances: [{ id: 'inst-a', name: 'astro', status: { state: 'connected' } }],
  }),
  chatFoldersMock: vi.fn().mockResolvedValue([]),
}))

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession', 'connectInstance', 'sessionsSearch',
          'instancesSearchSessions',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: chatFoldersMock,
      listInstances: listInstancesMock,
      instanceChatSlots: instanceChatSlotsMock,
    },
  }
})

// Browser API stubs
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
import type { ChatSlot, ChatHistoryItem } from '../types'
import type { RootState } from '../store'

const slot = (key: string, title?: string): ChatSlot => ({
  key, title: title ?? key, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
} as ChatSlot)

const histItem = (key: string, title: string, modified: number): ChatHistoryItem => ({
  key, title, modified,
} as unknown as ChatHistoryItem)

function renderSidebar({
  activeSlot = 's1',
  localNewerRunning = false,
  localNewerPinned = false,
  localNewerFolderId,
  onOpenSlotInNewTab,
}: {
  activeSlot?: string
  localNewerRunning?: boolean
  localNewerPinned?: boolean
  localNewerFolderId?: string
  onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void
} = {}) {
  // LIVE slots carry the ISO ladder, same as a remote row — that is what lets the
  // two interleave. The remote row's last activity sits between these two.
  const slots = [
    {
      ...slot('s-new', 'LIVE newer slot'),
      running: localNewerRunning,
      pinned: localNewerPinned,
      folder_id: localNewerFolderId,
      last_turn_ts: new Date(Date.now() - 60_000).toISOString(),
    },
    { ...slot('s-old', 'LIVE older slot'), last_turn_ts: new Date(Date.now() - 180_000).toISOString() },
  ] as ChatSlot[]
  const history = [
    histItem('h-new', 'LOCAL history row', LOCAL_NEWER),
    histItem('h-old', 'LOCAL older history row', LOCAL_OLDER),
  ]
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
      activeSlot,
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history, historyHasMore: false, historyOffset: history.length,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={activeSlot}
              unreadSlots={[]}
              history={history}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
              onOpenSlotInNewTab={onOpenSlotInNewTab}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  // History rows live behind the Older Sessions disclosure.
  fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
  // `store` is returned so a case can dispatch a reveal request, which is the
  // only way to exercise the DOM row-targeting path.
  return { ...view, store }
}

describe('ChatSidebar – remote instance sessions merge into the list', () => {
  beforeEach(() => {
    instanceChatSlotsMock.mockClear()
    listInstancesMock.mockClear()
    chatFoldersMock.mockReset().mockResolvedValue([])
    localStorage.clear()
  })

  it('orders a remote row BETWEEN local LIVE sessions by recency', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { container } = renderSidebar()

    await waitFor(() => {
      expect(container.textContent).toContain('REMOTE middle row')
    })

    const text = container.textContent ?? ''
    const newer = text.indexOf('LIVE newer slot')
    const remote = text.indexOf('REMOTE middle row')
    const older = text.indexOf('LIVE older slot')

    expect(newer).toBeGreaterThanOrEqual(0)
    expect(older).toBeGreaterThanOrEqual(0)
    // Interleaved by recency among the LIVE sessions — these are the peer's OPEN
    // slots, so they belong with local open sessions, not in the closed-tab drawer.
    expect(newer).toBeLessThan(remote)
    expect(remote).toBeLessThan(older)
  })

  it('prints each date-segment header once, even when a remote row was created in another bucket', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { container } = renderSidebar()

    await waitFor(() => {
      expect(container.textContent).toContain('REMOTE middle row')
    })

    // Segment headers are the only uppercase-tracking labels in this list. A
    // correctly ordered list changes bucket monotonically, so no label repeats;
    // a row segmented by a value it did NOT sort by makes the bucket flip and
    // print the same header twice.
    const headers = Array.from(
      container.querySelectorAll('div.uppercase'),
    ).map(el => (el.textContent || '').trim()).filter(Boolean)

    const seen = new Map<string, number>()
    for (const h of headers) seen.set(h, (seen.get(h) ?? 0) + 1)
    const repeated = [...seen.entries()].filter(([, n]) => n > 1)
    expect(repeated).toEqual([])
  })

  it('names an instance that did not answer instead of silently dropping its rows', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    instanceChatSlotsMock.mockRejectedValueOnce(new Error('peer unreachable'))
    const { container } = renderSidebar()

    // The whole point: a connected-but-silent instance must be NAMED. Without this
    // the list shows fewer rows and claims completeness, which reads as "that
    // instance has nothing open" rather than "we could not ask".
    await waitFor(() => {
      expect(container.textContent).toMatch(/unavailable/i)
    })
    expect(container.textContent).toContain('astro')
  })

  it('gives a remote row NO local-only mutation affordances', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { container } = renderSidebar()

    await waitFor(() => {
      expect(container.textContent).toContain('REMOTE middle row')
    })

    // Every one of ⋯ / duplicate / close / rename / pin / drag targets a LOCAL slot
    // key. A remote row has no local slot, so offering them could only no-op or —
    // if a peer key ever coincided with a local one — hit the WRONG session. The
    // row must therefore carry no action group and must not be draggable.
    const remoteRow = Array.from(container.querySelectorAll('[data-slot-key]'))
      .find(el => (el.textContent || '').includes('REMOTE middle row'))
    expect(remoteRow).toBeTruthy()
    expect(remoteRow!.querySelector('[aria-label="More options"]')).toBeNull()
    expect(remoteRow!.querySelector('[draggable="true"]')).toBeNull()
  })

  it('does not open a local tab when a remote row is middle-clicked', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const openInNewTab = vi.fn()
    const { container } = renderSidebar({ onOpenSlotInNewTab: openInNewTab })

    await waitFor(() => expect(container.textContent).toContain('REMOTE middle row'))
    const remoteButton = Array.from(container.querySelectorAll('[data-session-row]'))
      .find(row => row.textContent?.includes('REMOTE middle row'))
    expect(remoteButton).toBeTruthy()

    fireEvent(remoteButton!, new MouseEvent('auxclick', { bubbles: true, button: 1 }))
    expect(openInNewTab).not.toHaveBeenCalled()
  })

  it('does not rename a colliding local row when a remote title is double-clicked', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 's-new',
        title: 'REMOTE same-key rename row',
        last_turn_ts: new Date(Date.now() - 120_000).toISOString(),
      },
    ])
    const { container } = renderSidebar()

    await waitFor(() => expect(container.textContent).toContain('REMOTE same-key rename row'))
    const rows = Array.from(container.querySelectorAll('[data-slot-key="s-new"]'))
    const remoteRow = rows.find(row => row.textContent?.includes('REMOTE same-key rename row'))
    const localRow = rows.find(row => row.textContent?.includes('LIVE newer slot'))
    const remoteTitle = remoteRow?.querySelector('[data-session-title]')
    expect(remoteTitle).toBeTruthy()

    fireEvent.doubleClick(remoteTitle!)
    expect(localRow?.querySelector('textarea')).toBeNull()
  })

  it('shows a running indicator when the peer reports an active remote turn', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 'remote-running',
        title: 'REMOTE active turn',
        last_turn_ts: new Date(Date.now() - 120_000).toISOString(),
        running: true,
      },
    ])
    const { container } = renderSidebar()

    await waitFor(() => expect(container.textContent).toContain('REMOTE active turn'))
    const remoteRow = Array.from(container.querySelectorAll('[data-slot-key="remote-running"]'))
      .find(row => row.textContent?.includes('REMOTE active turn'))
    expect(remoteRow?.querySelector('.animate-spin')).not.toBeNull()
  })

  it('keeps colliding local and remote slot keys as distinct rows without leaking local state', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 's-new',
        title: 'REMOTE same-key row',
        last_turn_ts: new Date(Date.now() - 120_000).toISOString(),
        running: false,
      },
    ])
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    try {
      const { container } = renderSidebar({ activeSlot: 's-new', localNewerRunning: true })
      await waitFor(() => expect(container.textContent).toContain('REMOTE same-key row'))

      const rows = Array.from(container.querySelectorAll('[data-slot-key="s-new"]'))
      expect(rows).toHaveLength(2)
      const localRow = rows.find(row => row.textContent?.includes('LIVE newer slot'))
      const remoteRow = rows.find(row => row.textContent?.includes('REMOTE same-key row'))
      const localButton = localRow?.querySelector('[data-session-row]')
      const remoteButton = remoteRow?.querySelector('[data-session-row]')
      expect(localButton).toHaveAttribute('aria-current', 'true')
      expect(localRow?.querySelector('.animate-spin')).not.toBeNull()
      expect(remoteButton).not.toHaveAttribute('aria-current')
      expect(remoteRow?.querySelector('.animate-spin')).toBeNull()
      expect(consoleError.mock.calls.flat().join(' ')).not.toMatch(/same key|unique.*key/i)
    } finally {
      consoleError.mockRestore()
    }
  })

  it('does not let a colliding remote row inherit local pin ordering', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 's-new',
        title: 'REMOTE old same-key row',
        last_turn_ts: new Date(Date.now() - 300_000).toISOString(),
        running: false,
      },
    ])
    const { container } = renderSidebar({ localNewerPinned: true })

    await waitFor(() => expect(container.textContent).toContain('REMOTE old same-key row'))
    const text = container.textContent ?? ''
    expect(text.indexOf('LIVE newer slot')).toBeLessThan(text.indexOf('LIVE older slot'))
    expect(text.indexOf('LIVE older slot')).toBeLessThan(text.indexOf('REMOTE old same-key row'))
  })

  it('does not place a colliding remote row in the local folder', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    chatFoldersMock.mockResolvedValueOnce([
      { id: 'local-folder', name: 'Local folder', order: 0, collapsed: false },
    ])
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 's-new',
        title: 'REMOTE same-key unfiled row',
        last_turn_ts: new Date(Date.now() - 120_000).toISOString(),
        running: false,
      },
    ])
    const { container } = renderSidebar({ localNewerFolderId: 'local-folder' })

    await waitFor(() => expect(container.textContent).toContain('REMOTE same-key unfiled row'))
    const rows = Array.from(container.querySelectorAll('[data-slot-key="s-new"]'))
    const localRow = rows.find(row => row.textContent?.includes('LIVE newer slot'))
    const remoteRow = rows.find(row => row.textContent?.includes('REMOTE same-key unfiled row'))
    expect(localRow?.closest('[data-folder-drop="local-folder"]')).not.toBeNull()
    expect(remoteRow?.closest('[data-folder-drop="local-folder"]')).toBeNull()
  })

  it('does not let a colliding remote row inherit the local running filter', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    localStorage.setItem('mc-session-running-only', '1')
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 's-new',
        title: 'REMOTE idle same-key row',
        last_turn_ts: new Date(Date.now() - 120_000).toISOString(),
        running: false,
      },
    ])
    const { container } = renderSidebar({ localNewerRunning: true })

    await waitFor(() => expect(instanceChatSlotsMock).toHaveBeenCalled())
    await waitFor(() => expect(container.textContent).toContain('LIVE newer slot'))
    expect(container.textContent).not.toContain('REMOTE idle same-key row')
  })

  it('states the click destination on a remote row before it is clicked', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const { container } = renderSidebar()

    await waitFor(() => expect(container.textContent).toContain('REMOTE middle row'))
    const remoteRow = Array.from(container.querySelectorAll('[data-session-row]'))
      .find(row => row.textContent?.includes('REMOTE middle row'))
    expect(remoteRow).toBeTruthy()
    // A remote row does NOT open the transcript it names — it switches to the
    // instance's pane. The row must say so before the click, or its label
    // promises something the click does not deliver.
    const hint = `${remoteRow!.getAttribute('title') ?? ''} ${remoteRow!.getAttribute('aria-label') ?? ''}`
    expect(hint).toMatch(/astro/)
    expect(hint).toMatch(/dashboard/i)
  })

  it('says it is checking remote instances while the first remote fetch is outstanding', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    let releaseSlots: ((rows: unknown[]) => void) | undefined
    instanceChatSlotsMock.mockReturnValueOnce(
      new Promise(resolve => { releaseSlots = resolve as (rows: unknown[]) => void }),
    )
    const { container } = renderSidebar()

    // Same honesty rule the unreachable-instance notice exists for: until the
    // peer answers, the list is incomplete and must not imply otherwise.
    await waitFor(() => expect(container.textContent).toMatch(/checking remote instances/i))
    releaseSlots?.([
      { key: 'chat-9', title: 'REMOTE arrived row', last_turn_ts: new Date(Date.now() - 120_000).toISOString() },
    ])
    await waitFor(() => expect(container.textContent).toContain('REMOTE arrived row'))
    expect(container.textContent).not.toMatch(/checking remote instances/i)
  })

  it('reveals the LOCAL session when a colliding remote row sorts above it', async () => {
    // The reveal targets a row through the DOM. `data-slot-key` carries the RAW
    // key, which stops being a unique namespace once peer rows are merged: a
    // remote row with a byte-identical deterministic key carries the same
    // attribute, and `querySelector` returns whichever sorts first. With the
    // remote row newer — so it sorts ABOVE the local one — a raw-key lookup
    // scrolls to the peer's row instead of the session the user asked for.
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 's-new',
        title: 'REMOTE same-key newer row',
        last_turn_ts: new Date(Date.now() - 5_000).toISOString(),
        running: false,
      },
    ])
    const scrolledInto: string[] = []
    const originalScroll = (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
    HTMLElement.prototype.scrollIntoView = function (this: HTMLElement) {
      scrolledInto.push(this.textContent ?? '')
    }
    try {
      const { container, store } = renderSidebar()
      await waitFor(() => expect(container.textContent).toContain('REMOTE same-key newer row'))
      // Precondition: the colliding remote row really is first in the DOM, so a
      // raw-key lookup would find it rather than the local row.
      const text = container.textContent ?? ''
      expect(text.indexOf('REMOTE same-key newer row')).toBeLessThan(text.indexOf('LIVE newer slot'))

      store.dispatch(requestSlotReveal('s-new'))

      await waitFor(() => expect(scrolledInto).toHaveLength(1))
      expect(scrolledInto[0]).toContain('LIVE newer slot')
      expect(scrolledInto[0]).not.toContain('REMOTE same-key newer row')
    } finally {
      if (originalScroll) HTMLElement.prototype.scrollIntoView = originalScroll
      else delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView
    }
  })

  it('keeps the Running filter badge equal to the rows it renders', async () => {
    // THE INVARIANT: a filter badge describes the collection the filter RENDERS.
    // `running`/`recent` predicates are origin-aware, so counting `localSlots`
    // while rendering the merged set under-reported by exactly the peer rows the
    // filter then showed. Asserted through the ACTIVE-FILTER CHIP rather than the
    // dropdown: this harness cannot open a Radix menu (see
    // ChatSidebarRenameFocus.integration.test.tsx), and the chip renders the same
    // `filterCounts` value as plain DOM. With the filter active the list is
    // narrowed to running rows, so chip count vs rendered row count is a direct
    // parity check rather than a hardcoded number.
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    localStorage.setItem('mc-session-running-only', '1')
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: 'remote-running',
        title: 'REMOTE running row',
        last_turn_ts: new Date(Date.now() - 30_000).toISOString(),
        running: true,
      },
    ])
    const { container } = renderSidebar()
    await waitFor(() => expect(container.textContent).toContain('REMOTE running row'))

    const chip = await waitFor(() => {
      // Located by the parenthesized count it renders, NOT by its label: the
      // label is localized ("In progress", not "Running") and lives in the manual
      // overlay catalog, so matching text would pin this test to a translation.
      // With one active filter the chip is the only button rendering a count.
      const hit = Array.from(container.querySelectorAll('button'))
        .find(b => /\((\d+)\)\s*$/.test(b.textContent ?? ''))
      expect(hit).toBeTruthy()
      return hit as HTMLElement
    })
    const badged = /\((\d+)\)\s*$/.exec(chip.textContent ?? '')
    expect(badged).toBeTruthy()
    const rendered = container.querySelectorAll('[data-session-row]').length
    // No LOCAL slot is running, so the only running row is the peer's: the badge
    // must say 1 and the list must show exactly that one row.
    expect(Number(badged?.[1])).toBe(rendered)
    expect(rendered).toBe(1)
  })

  it('issues NO remote request and renders no remote row when the flag is off', async () => {
    // Flag absent, i.e. every user who has not opted in.
    const { container } = renderSidebar()

    await waitFor(() => {
      expect(container.textContent).toContain('LIVE newer slot')
    })

    expect(instanceChatSlotsMock).not.toHaveBeenCalled()
    expect(container.textContent).not.toContain('REMOTE middle row')
  })
})
