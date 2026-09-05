/**
 * Test: the sidebar source-link chip carries an Unlink affordance.
 *
 * The chips are derived by scanning the transcript, so unlinking cannot delete
 * anything -- it records the link's serialized identity in a per-slot dismissed
 * set the backend derivation filters against. The chip is hidden OPTIMISTICALLY
 * on click (the authoritative removal arrives via the next slots push), the X
 * calls `api.unlinkSourceLink` with the opaque identity, and a failure re-shows
 * the chip with an error. A chip missing the identity (an older gateway) shows
 * no X because there is nothing to name to the DELETE endpoint.
 *
 * Harness mirrors ChatSidebar.sourceLinkChip.test.tsx.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

const { switchSlotMock, unlinkMock } = vi.hoisted(() => ({
  switchSlotMock: vi.fn(() => ({ type: 'chat/switchSlot/pending', meta: {} })),
  unlinkMock: vi.fn(() => Promise.resolve({ ok: true, dismissed: true })),
}))

vi.mock('../store/chatSlice', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../store/chatSlice')>()
  return { ...actual, switchSlot: (...args: unknown[]) => switchSlotMock(...args) }
})

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...Object.fromEntries(
        ['sessions', 'chatSlots', 'chatSlotDetail', 'fetchHistory', 'chatFolders', 'chatTags', 'tagColumns'].map(
          k => [k, vi.fn().mockResolvedValue([])],
        ),
      ),
      unlinkSourceLink: (...args: unknown[]) => unlinkMock(...(args as [string, string])),
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

const PR_URL = 'https://github.com/acme/widgets/pull/634'
const ISSUE_URL = 'https://github.com/acme/widgets/issues/701'
const PR_IDENTITY = '["github","github.com","acme","widgets",634,"","change",""]'
const ISSUE_IDENTITY = '["github","github.com","acme","widgets",701,"","issue",""]'

function rows(): ChatSlot[] {
  return [
    {
      key: 's1', title: 'PR session', messages: 1, running: false, mode: '', created: '', last_ts: '2026-01-01T00:00:00Z',
      source_links: [
        { provider: 'github', number: 634, label: '#634', url: PR_URL, state: 'open', kind: 'change', identity: PR_IDENTITY },
        { provider: 'github', number: 701, label: '#701', url: ISSUE_URL, kind: 'issue', identity: ISSUE_IDENTITY },
      ],
      source_links_total: 2,
    },
  ] as unknown as ChatSlot[]
}

function renderSidebar(list: ChatSlot[], opts: { connected?: boolean } = {}) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' },
      connected: opts.connected ?? true,
      slots: list,
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
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={list} activeSlot={'s1'} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent={'default'} installedAgents={[]}
              onOpenSource={vi.fn(() => true)}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('ChatSidebar – source-link unlink affordance', () => {
  beforeEach(() => {
    switchSlotMock.mockClear()
    unlinkMock.mockClear()
    unlinkMock.mockResolvedValue({ ok: true, dismissed: true })
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
  })

  it('renders an unlink X on both a change and an issue chip', () => {
    renderSidebar(rows())
    expect(screen.getByTestId('session-source-unlink-634')).toBeInTheDocument()
    expect(screen.getByTestId('session-source-unlink-701')).toBeInTheDocument()
  })

  it('calls the DELETE endpoint with the chip\'s opaque identity', async () => {
    renderSidebar(rows())
    fireEvent.click(screen.getByTestId('session-source-unlink-634'))
    await waitFor(() => expect(unlinkMock).toHaveBeenCalledWith('s1', PR_IDENTITY))
  })

  it('hides the chip optimistically on click, without switching sessions', async () => {
    renderSidebar(rows())
    expect(screen.getByText('#634')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('session-source-unlink-634'))
    // Gone immediately, before any server round-trip echoes back.
    await waitFor(() => expect(screen.queryByTestId('session-source-unlink-634')).toBeNull())
    // The sibling chip is untouched, and the row never switched.
    expect(screen.getByTestId('session-source-unlink-701')).toBeInTheDocument()
    expect(switchSlotMock).not.toHaveBeenCalled()
  })

  it('re-shows the chip and surfaces an error when the unlink fails', async () => {
    unlinkMock.mockRejectedValueOnce(new Error('network'))
    renderSidebar(rows())
    fireEvent.click(screen.getByTestId('session-source-unlink-634'))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    // Reverted: the object is still linked, so its chip is back.
    expect(screen.getByTestId('session-source-unlink-634')).toBeInTheDocument()
  })

  it('shows no unlink X when the gateway did not send an identity', () => {
    const list = rows()
    // Older gateway: chip carries no identity, so it cannot be named to DELETE.
    delete (list[0].source_links as Array<Record<string, unknown>>)[0].identity
    renderSidebar(list)
    expect(screen.queryByTestId('session-source-unlink-634')).toBeNull()
    // The other chip, which still has an identity, keeps its X.
    expect(screen.getByTestId('session-source-unlink-701')).toBeInTheDocument()
  })

  it('disables the unlink X while offline', () => {
    renderSidebar(rows(), { connected: false })
    expect(screen.getByTestId('session-source-unlink-634')).toBeDisabled()
  })
})
