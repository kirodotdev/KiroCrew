/**
 * Test: Older Sessions search federates across connected remote instances.
 *
 * When at least one remote instance holds a warm (live) connection, the
 * sidebar's history search must call the federated endpoint
 * (`/api/instances/search-sessions`) instead of the plain local search, and:
 *  - remote rows (tagged `instance_id`/`instance_name`) render an instance
 *    badge next to the agent label, tinted `info` + carrying a server glyph so
 *    "this transcript is on another machine" does not read as one more neutral
 *    meta chip;
 *  - remote rows hide the local delete hover button — `deleteHistorySession`
 *    targets the LOCAL session file, which for a remote row is at best a
 *    same-keyed unrelated conversation;
 *  - a federated-endpoint failure (including the 403 when the instances
 *    feature is off) falls back to the plain local search, which is always
 *    the floor.
 * Without any warm instance, the plain local endpoint is used and the
 * federated one is never called.
 *
 * Mock scaffolding mirrors ChatSidebar.historySearchOrder.test.tsx (which in
 * turn mirrors ChatSidebar.offline.test.tsx, the owner of the mock setup).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { setWarm } from '../store/instancesSlice'

const { LOCAL_ROW, REMOTE_ROW, sessionsSearchMock, federatedSearchMock, listInstancesMock } =
  vi.hoisted(() => {
    // Deliberately SAME key on both rows: the remote row must not inherit the
    // local row's delete/resume semantics just because the keys collide.
    const LOCAL_ROW = { key: 'chat-7', title: 'deploy checklist (local)', modified: 2_000_000 }
    const REMOTE_ROW = {
      key: 'chat-7',
      title: 'deploy checklist (remote)',
      modified: 1_000_000,
      instance_id: 'inst-a',
      instance_name: 'clouddeskARM',
    }
    return {
      LOCAL_ROW,
      REMOTE_ROW,
      sessionsSearchMock: vi.fn().mockResolvedValue({ sessions: [LOCAL_ROW] }),
      federatedSearchMock: vi.fn().mockResolvedValue({ sessions: [LOCAL_ROW, REMOTE_ROW] }),
      listInstancesMock: vi.fn().mockResolvedValue({
        instances: [{ id: 'inst-a', name: 'clouddeskARM', status: { state: 'connected' } }],
      }),
    }
  })

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        [
          'sessions', 'chatSlots', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot',
          'resumeChatSlot', 'deleteSession', 'agentDetail', 'spawnList', 'fetchHistory',
          'renameSlot', 'forkSession', 'connectInstance',
        ].map(k => [k, vi.fn().mockResolvedValue({})]),
      ),
      chatFolders: vi.fn().mockResolvedValue([]),
      sessionsSearch: sessionsSearchMock,
      instancesSearchSessions: federatedSearchMock,
      listInstances: listInstancesMock,
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

const histItem = (key: string, title: string): ChatHistoryItem => ({
  key, title, last_ts: '2026-01-01T00:00:00Z',
} as unknown as ChatHistoryItem)

function renderSidebar({ warm }: { warm: boolean }) {
  const slots = [slot('s1', 'Session 1')]
  const history = [histItem('h1', 'Placeholder history row')]
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
      lastChunkSeq: undefined,
      history, historyHasMore: false, historyOffset: history.length,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  // Warm connection drives the federated-vs-local endpoint choice; set it the
  // way the real connect flow does (setWarm on a live tunnel).
  if (warm) store.dispatch(setWarm({ id: 'inst-a', conn: { port: 45123, token: 't' } }))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots}
              activeSlot={'s1'}
              unreadSlots={[]}
              history={history}
              historyHasMore={false}
              defaultAgent={'default'}
              installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

async function searchOlderSessions(query: string) {
  fireEvent.click(screen.getByRole('button', { name: /^older sessions$/i }))
  fireEvent.change(screen.getByPlaceholderText(/search older sessions/i), {
    target: { value: query },
  })
}

describe('ChatSidebar – federated history search across connected instances', () => {
  beforeEach(() => {
    sessionsSearchMock.mockClear()
    federatedSearchMock.mockClear()
    federatedSearchMock.mockResolvedValue({ sessions: [LOCAL_ROW, REMOTE_ROW] })
    listInstancesMock.mockClear()
    localStorage.clear()
  })

  it('routes the search through the federated endpoint when a warm instance exists', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      renderSidebar({ warm: true })
      await searchOlderSessions('deploy checklist')

      await waitFor(() => expect(federatedSearchMock).toHaveBeenCalledWith('deploy checklist'))
      // The federated endpoint REPLACES the local call (backend already merges).
      expect(sessionsSearchMock).not.toHaveBeenCalled()

      // Both rows render; the remote one carries the instance badge. Their raw
      // slot keys intentionally collide, so origin-qualified UI identity is what
      // keeps React from dropping or mis-reconciling either row.
      await screen.findByText('deploy checklist (remote)')
      expect(screen.getByText('deploy checklist (local)')).toBeTruthy()
      expect(screen.getByText('clouddeskARM')).toBeTruthy()
      expect(consoleError.mock.calls.flat().join(' ')).not.toMatch(/same key|unique.*key/i)
    } finally {
      consoleError.mockRestore()
    }
  })

  it('marks the remote badge as remote, not as one more neutral meta chip', async () => {
    renderSidebar({ warm: true })
    await searchOlderSessions('deploy checklist')

    const badge = await screen.findByText('clouddeskARM')
    const cls = badge.className
    // The claim "this transcript is on another machine" must not look like the
    // neutral chip styling shared by every other meta chip, or it reads as a
    // tag. Tint classes are the mechanism, so they are what gets pinned.
    expect(cls).toContain('text-info')
    expect(cls).toContain('bg-info-subtle')
    expect(cls).toContain('border-info/40')
    expect(cls).not.toContain('text-muted')
    expect(cls).not.toContain('bg-bg-elevated')
    // Non-colour half of the cue: an icon, so the distinction survives a
    // colour-vision deficiency rather than resting on hue alone.
    expect(badge.querySelector('svg')).not.toBeNull()
  })

  it('hides the local delete button on remote rows but keeps it on local rows', async () => {
    renderSidebar({ warm: true })
    await searchOlderSessions('deploy checklist')

    const remoteTitle = await screen.findByText('deploy checklist (remote)')
    const localTitle = screen.getByText('deploy checklist (local)')
    const rowOf = (el: HTMLElement) => el.closest('[role="button"]') as HTMLElement
    const deleteIn = (row: HTMLElement) =>
      row.querySelector('button[aria-label*="elete"]')

    // deleteHistorySession targets the LOCAL file; exposing it on a remote row
    // would delete a same-keyed unrelated local conversation.
    expect(deleteIn(rowOf(remoteTitle))).toBeNull()
    expect(deleteIn(rowOf(localTitle))).not.toBeNull()
  })

  it('falls back to the plain local search when the federated endpoint fails (e.g. 403 feature-off)', async () => {
    federatedSearchMock.mockRejectedValue(new Error('403'))
    renderSidebar({ warm: true })
    await searchOlderSessions('deploy checklist')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('deploy checklist'))
    // Local results still render — the local search is always the floor.
    await screen.findByText('deploy checklist (local)')
    expect(screen.queryByText('deploy checklist (remote)')).toBeNull()
  })

  it('uses the plain local endpoint when no instance is warm', async () => {
    renderSidebar({ warm: false })
    await searchOlderSessions('deploy checklist')

    await waitFor(() => expect(sessionsSearchMock).toHaveBeenCalledWith('deploy checklist'))
    expect(federatedSearchMock).not.toHaveBeenCalled()
  })
})
