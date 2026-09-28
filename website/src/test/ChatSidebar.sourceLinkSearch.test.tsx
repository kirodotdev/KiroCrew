/**
 * Test: the sidebar's session search matches a card's PR/CR/issue BADGE ids.
 *
 * A chip on the card reads its id, so typing that id must find the row. The
 * predicate used to match TITLE only, so an id the title never spelled out found
 * nothing locally and the row appeared only if the backend's content ranking
 * happened to return it.
 *
 * One case per branch/edge: bare number with the ranking live but empty; chip
 * label, case-insensitively; the no-ranking FALLBACK branch, so a typed id
 * matches without waiting on the backend; a negative control proving the
 * widened predicate does not match everything; and `source_links` undefined
 * (the shape with the `session_card_source_links` config flag off), which must
 * not throw.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

// The backend returns NO hits: the ranking is live (a Map, so `searchRanked` is
// non-null) but empty, which isolates the LOCAL half of the predicate.
const { sessionsSearchMock } = vi.hoisted(() => ({
  sessionsSearchMock: vi.fn().mockResolvedValue({ sessions: [] }),
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
          'renameSlot', 'forkSession',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      // Both resolve to ARRAYS: the sidebar iterates them directly, so the
      // generic `{}` above would throw before any predicate ran.
      chatTags: vi.fn().mockResolvedValue([]),
      chatFolders: vi.fn().mockResolvedValue([]),
      sessionsSearch: sessionsSearchMock,
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

const CR_NUMBER = 4287
const CR_TITLE = 'Alpha session'
const ISSUE_TITLE = 'Beta session'
const DECOY_TITLE = 'Gamma session'
const NO_LINKS_TITLE = 'Delta session'
const MR_TITLE = 'Zeta session'
const BARE_TITLE = 'Eta session'
const BARE_NUMBER = 123

const base = (key: string, title: string): ChatSlot => ({
  key, title, messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z', pinned: false,
} as ChatSlot)

/** Titles carry NO digits and NO id text, so every match below is attributable
 *  to `source_links` alone rather than to the pre-existing title match. */
const CR_SLOT = {
  ...base('chat-alpha', CR_TITLE),
  source_links: [{
    provider: 'github', number: CR_NUMBER, label: `CR-${CR_NUMBER}`,
    url: `https://github.com/kirodotdev/KiroCrew/pull/${CR_NUMBER}`, kind: 'change',
  }],
  source_links_total: 1,
} as ChatSlot

const ISSUE_SLOT = {
  ...base('chat-beta', ISSUE_TITLE),
  source_links: [{
    provider: 'jira', number: 4411, label: 'PROJ-4411',
    url: 'https://jira.example.com/browse/PROJ-4411', kind: 'issue',
  }],
  source_links_total: 1,
} as ChatSlot

const DECOY_SLOT = {
  ...base('chat-gamma', DECOY_TITLE),
  source_links: [{
    provider: 'github', number: 55, label: '#55',
    url: 'https://github.com/kirodotdev/KiroCrew/pull/55', kind: 'change',
  }],
  source_links_total: 1,
} as ChatSlot

/** A GitLab MR, so the `/-/merge_requests/` route words are PRESENT in the
 *  fixture: without one, "merge retains nothing" passes whatever the code does. */
const MR_SLOT = {
  ...base('chat-zeta', MR_TITLE),
  source_links: [{
    provider: 'gitlab', number: 912, label: '!912',
    url: 'https://gitlab.com/example-org/example-svc/-/merge_requests/912', kind: 'change',
  }],
  source_links_total: 1,
} as ChatSlot

/** A payload with NO `label` — the older-gateway shape the type marks optional.
 *  Its chip renders `#123` via `chipLabel`, so that is what a user will type. */
const BARE_SLOT = {
  ...base('chat-eta', BARE_TITLE),
  source_links: [{
    provider: 'github', number: BARE_NUMBER,
    url: `https://github.com/kirodotdev/KiroCrew/pull/${BARE_NUMBER}`, kind: 'change',
  }],
  source_links_total: 1,
} as ChatSlot

/** What every slot looks like with the `session_card_source_links` config flag
 *  off — the serializer skips extraction entirely, so the field is absent. */
const NO_LINKS_SLOT = base('chat-delta', NO_LINKS_TITLE)

const ALL = [CR_SLOT, ISSUE_SLOT, DECOY_SLOT, MR_SLOT, BARE_SLOT, NO_LINKS_SLOT]

function renderSidebar(slots: ChatSlot[]) {
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
      activeSlot: 'chat-gamma',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'chat-gamma'}
              unreadSlots={[]}
              history={[]}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

const search = (q: string) =>
  fireEvent.change(screen.getByPlaceholderText(/search sessions/i), { target: { value: q } })

describe('ChatSidebar – session search matches source-link badge ids', () => {
  beforeEach(() => {
    sessionsSearchMock.mockReset()
    sessionsSearchMock.mockResolvedValue({ sessions: [] })
    localStorage.clear()
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
  })

  it('matches a bare badge number while the backend ranking is live but empty', async () => {
    renderSidebar(ALL)
    search(String(CR_NUMBER))

    // Assert in the state that used to hide the row: ranking on, zero hits.
    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(String(CR_NUMBER)))
    await waitFor(() => expect(screen.getByText(CR_TITLE)).toBeTruthy())
    expect(screen.queryByText(ISSUE_TITLE)).toBeNull()
    expect(screen.queryByText(DECOY_TITLE)).toBeNull()
    expect(screen.queryByText(NO_LINKS_TITLE)).toBeNull()
  })

  it('matches a chip label case-insensitively', async () => {
    renderSidebar(ALL)
    search('proj-44')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('proj-44'))
    await waitFor(() => expect(screen.getByText(ISSUE_TITLE)).toBeTruthy())
    expect(screen.queryByText(CR_TITLE)).toBeNull()
    expect(screen.queryByText(DECOY_TITLE)).toBeNull()
  })

  it('matches on the fallback branch, with no backend ranking at all', async () => {
    // Never resolves: `searchRanked` stays null for the whole test, which is the
    // "instant local match, without waiting for the backend" requirement.
    sessionsSearchMock.mockImplementation(() => new Promise(() => {}))
    renderSidebar(ALL)
    search(String(CR_NUMBER))

    await waitFor(() => expect(screen.getByText(CR_TITLE)).toBeTruthy())
    expect(screen.queryByText(ISSUE_TITLE)).toBeNull()
    expect(screen.queryByText(DECOY_TITLE)).toBeNull()
    expect(screen.queryByText(NO_LINKS_TITLE)).toBeNull()
  })

  it('does NOT match a slot whose source links lack the query', async () => {
    // A well-formed id that no fixture carries: had the widening leaked into
    // key/agent or matched links unconditionally, a row would appear here.
    renderSidebar(ALL)
    search('987654321')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('987654321'))
    expect(screen.queryByText(CR_TITLE)).toBeNull()
    expect(screen.queryByText(ISSUE_TITLE)).toBeNull()
    expect(screen.queryByText(DECOY_TITLE)).toBeNull()
    expect(screen.queryByText(NO_LINKS_TITLE)).toBeNull()
  })

  it.each(['pull', 'merge', 'github', 'browse', 'kirocrew', 'example-svc'])('does NOT retain badged rows for URL boilerplate %s', async (q) => {
    // The URL is an invisible href, and the local predicate fires from the first
    // character, so matching any part of it retained every badged row.
    renderSidebar(ALL)
    search(q)

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(q))
    expect(screen.queryByText(CR_TITLE)).toBeNull()
    expect(screen.queryByText(ISSUE_TITLE)).toBeNull()
    expect(screen.queryByText(DECOY_TITLE)).toBeNull()
    expect(screen.queryByText(MR_TITLE)).toBeNull()
    expect(screen.queryByText(BARE_TITLE)).toBeNull()
    expect(screen.queryByText(NO_LINKS_TITLE)).toBeNull()
  })

  it('matches the rendered chip text of a link that carries NO label', async () => {
    // `chipLabel` renders `#123` for a label-less payload, so `#123` is what the
    // user sees and types; matching the raw `label` field finds nothing.
    renderSidebar(ALL)
    search('#123')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('#123'))
    await waitFor(() => expect(screen.getByText(BARE_TITLE)).toBeTruthy())
    expect(screen.queryByText(CR_TITLE)).toBeNull()
  })

  it('does NOT match a badge on an ordinary slash-bearing title query', async () => {
    // The query is matched only against the number and the rendered chip text,
    // so a branch-like term is compared literally and prefixes neither.
    renderSidebar(ALL)
    search('feat/sidebar')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('feat/sidebar'))
    expect(screen.queryByText(CR_TITLE)).toBeNull()
    expect(screen.queryByText(MR_TITLE)).toBeNull()
    expect(screen.queryByText(BARE_TITLE)).toBeNull()
  })

  it.each(['release/44', 'v2/12', 'sprint/4'])('does NOT retain a badged row for the branch-like query %s', async (q) => {
    // A slash-and-digit query is NOT a badge reference: matching is by PREFIX of
    // the number or chip text, and a leading `release/` prefixes neither.
    renderSidebar(ALL)
    search(q)

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(q))
    expect(screen.queryByText(CR_TITLE)).toBeNull()
    expect(screen.queryByText(ISSUE_TITLE)).toBeNull()
    expect(screen.queryByText(DECOY_TITLE)).toBeNull()
    expect(screen.queryByText(MR_TITLE)).toBeNull()
    expect(screen.queryByText(BARE_TITLE)).toBeNull()
  })

  it('prefix-matches a badge number rather than matching mid-number', async () => {
    // Progressive typing must keep working, but a query that is only an interior
    // run of the digits is an accident, not an id.
    renderSidebar(ALL)
    search('287')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('287'))
    expect(screen.queryByText(CR_TITLE)).toBeNull()
  })

  it('matches a badge number typed one digit at a time', async () => {
    renderSidebar(ALL)
    search('42')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('42'))
    await waitFor(() => expect(screen.getByText(CR_TITLE)).toBeTruthy())
  })

  it('tolerates a slot with source_links undefined (config flag off)', async () => {
    // Every row is the flag-off shape, so the predicate runs against an absent
    // field on all of them. Titles still match, proving it ran rather than bailed.
    renderSidebar([NO_LINKS_SLOT, base('chat-eps', 'Epsilon session')])
    search(NO_LINKS_TITLE)

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith(NO_LINKS_TITLE))
    await waitFor(() => expect(screen.getByText(NO_LINKS_TITLE)).toBeTruthy())
    expect(screen.queryByText('Epsilon session')).toBeNull()
  })
})
