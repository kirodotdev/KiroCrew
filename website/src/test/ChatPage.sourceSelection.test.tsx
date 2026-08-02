/**
 * TRUE regression test for the focused Change / Issue tab surviving a session
 * round-trip.
 *
 * The Changes panel renders one tab per pull request the transcript mentions.
 * The focused one used to live in a single component-local `useState('')` in
 * ChatPage, so it was neither per-slot nor persisted: switching sessions (or
 * reloading) reconciled it back to the FIRST link in the transcript, discarding
 * whichever PR the user had actually opened.
 *
 * SidePanel is mocked to echo the selected urls into the DOM, so these tests
 * assert the real selection ChatPage passes down — not just what it persisted.
 * Reverting the per-slot/persisted selection in ChatPage.tsx fails them.
 */
import { useEffect } from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { appendMessage } from '../store/chatSlice'
import { ThemeProvider } from '../hooks/useTheme'
import { __resetPanelTabs } from '../hooks/usePanelTabs'
import type { ChatMessage, ChatSlot } from '../types'
import type { RootState } from '../store'

vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../components/PendingQuestionCard', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ contentWidth: 'compact' }),
  CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } },
}))

// Echo the selection props, and expose the select callbacks as buttons so a
// test can drive a real user selection without the whole panel tree.
//
// This mock MIRRORS the real PullRequestPanel's self-normalizing effect: when the
// selected url is not among the tabs it renders, it reports the first one back to
// the parent. That behaviour is the reason onReconcileSource exists, so a mock
// without it cannot catch the parent wiring that path to a persisting callback —
// which is exactly how a durable-overwrite regression went unnoticed here.
vi.mock('../pages/chat/SidePanel', () => ({
  // Named (uppercase) rather than an inline arrow: react-hooks/rules-of-hooks
  // rejects a hook called inside a function named `default`.
  default: function MockSidePanel({ selectedSourceUrl, selectedIssueUrl, sources, onSelectSource, onReconcileSource }: {
    selectedSourceUrl?: string
    selectedIssueUrl?: string
    sources?: Array<{ url: string }>
    onSelectSource?: (url: string) => void
    onReconcileSource?: (url: string) => void
  }) {
    const rendered = (sources ?? []).slice(0, 64)
    const selected = rendered.find(source => source.url === selectedSourceUrl) || rendered[0]
    useEffect(() => {
      if (selected && selected.url !== selectedSourceUrl) {
        (onReconcileSource || onSelectSource)?.(selected.url)
      }
    }, [selected, selectedSourceUrl, onSelectSource, onReconcileSource])
    return (
      <div>
        <span data-testid="selected-source">{selectedSourceUrl}</span>
        <span data-testid="selected-issue">{selectedIssueUrl}</span>
        {(sources ?? []).map(source => (
          <button key={source.url} aria-label={`pick ${source.url}`} onClick={() => onSelectSource?.(source.url)} />
        ))}
      </div>
    )
  },
  CHAT_PANE_MIN_W: 320,
  sidePanelFillWidth: () => false,
}))

vi.mock('../hooks/usePanelState', () => ({
  usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }),
  useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }),
}))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'sendChat', 'dashboardConfig'].map(k => [
      k, vi.fn().mockResolvedValue(k === 'chatSlotDetail' ? { messages: [], has_more: false } : {}),
    ]),
  ),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import ChatPage from '../pages/ChatPage'

// One key per (slot, kind) — see the store's header comment on why there is no
// shared blob. Helpers keep the tests reading in terms of a session's selection.
const selKey = (slot: string, kind: 'change' | 'issue' = 'change') => `mc-pr-source-sel:${kind}:${slot}`
const seedSelection = (slot: string, url: string, kind: 'change' | 'issue' = 'change') =>
  localStorage.setItem(selKey(slot, kind), JSON.stringify({ u: url, t: Date.now() }))
const readSelection = (slot: string, kind: 'change' | 'issue' = 'change'): string => {
  try {
    return JSON.parse(localStorage.getItem(selKey(slot, kind)) || 'null')?.u ?? ''
  } catch {
    return ''
  }
}
const PR_ONE = 'https://github.com/acme/widgets/pull/11'
const PR_TWO = 'https://github.com/acme/widgets/pull/12'

const slot = (key: string): ChatSlot => ({
  key, title: key, messages: 0, running: false, mode: '', created: '', last_ts: '',
  pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})
const allSlots = [slot('chat-1'), slot('chat-2')]

// Agent-authored: under first-mention attribution only agent-surfaced PRs
// become Change sources. All urls go in ONE message so the extracted source
// order is fixed by the message text, independent of transcript hydration.
const transcript = (...urls: string[]): ChatMessage[] =>
  [{ role: 'assistant', content: `opened ${urls.join(' and ')}`, cls: '' }]

function renderChatPage(activeSlot: string, messages: ChatMessage[]) {
  const store = createTestStore({
    dashboard: {
      status: { platform: 'darwin' }, connected: false, slots: allSlots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot, messages, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: true, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={['/chat']}>
            <ChatPage />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...view, store }
}

beforeEach(() => {
  localStorage.clear()
  __resetPanelTabs()
})

describe('focused source tab persistence', () => {
  it('falls back to the first source when the slot has no remembered tab', async () => {
    renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })
  })

  it('restores the tab the user had open instead of the first source', async () => {
    seedSelection('chat-2', PR_TWO)
    renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_TWO)
    })
  })

  it('persists a user selection so the next mount comes back to it', async () => {
    const { unmount } = renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })

    await act(async () => { screen.getByLabelText(`pick ${PR_TWO}`).click() })
    await waitFor(() => {
      expect(readSelection('chat-2')).toBe(PR_TWO)
    })

    unmount()
    renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_TWO)
    })
  })

  it('keeps each slot on its own tab when switching sessions', async () => {
    seedSelection('chat-1', PR_ONE)
    seedSelection('chat-2', PR_TWO)
    const { unmount } = renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_TWO)
    })
    unmount()

    // Same two links in the other session: a single shared selection would
    // still satisfy the "is it in the list" check and leak chat-2's tab here.
    renderChatPage('chat-1', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })
  })

  it('shows the first source but keeps the stored one when the transcript lacks it', async () => {
    // A transcript that does not carry the remembered url is not proof the url
    // is gone — switchSlot.pending renders a CACHED copy with slotLoading
    // already false while the real fetch is in flight. So the panel falls back
    // to a valid tab, but the stored selection is left alone; once the full
    // transcript arrives carrying PR 12 it is restored rather than lost.
    seedSelection('chat-2', PR_TWO)
    renderChatPage('chat-2', transcript(PR_ONE))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })
    await act(async () => {})

    expect(readSelection('chat-2')).toBe(PR_TWO)
  })

  it('keeps the remembered tab when a history fetch fails', async () => {
    // switchSlot.rejected (dropped/failed history fetch) empties `messages` AND
    // clears slotLoading in one reducer pass, so the hydration guard does not
    // hold. Because the selection is durable now, clearing here would outlive
    // the failure and lose the tab on the user's retry.
    seedSelection('chat-2', PR_TWO)
    renderChatPage('chat-2', [])
    await act(async () => {})

    expect(readSelection('chat-2')).toBe(PR_TWO)
  })

  it('picks up a sibling window changing the tab for the session on screen', async () => {
    renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })

    // A second window (a popped-out session shares this origin's localStorage)
    // moves chat-2 to PR 12. Only the OTHER document gets the storage event, so
    // write the value first and then dispatch it by hand, exactly as the browser
    // would deliver it here.
    seedSelection('chat-2', PR_TWO)
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: selKey('chat-2'),
        newValue: localStorage.getItem(selKey('chat-2')),
        storageArea: localStorage,
      }))
    })

    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_TWO)
    })
  })

  it('ignores a storage event for an unrelated key', async () => {
    renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })

    seedSelection('chat-2', PR_TWO)
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: 'mc-something-else',
        newValue: 'x',
        storageArea: localStorage,
      }))
    })

    // Still on PR 11: the listener must not re-read on every unrelated write.
    expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
  })

  it('does not adopt or re-commit a url this window has no tab for', async () => {
    // A sibling window on the same session has a newer transcript and selects a
    // PR this window has never seen. Adopting it would make this window's own
    // reconciliation overwrite the sibling's choice, which the sibling would
    // overwrite back — an unbounded cross-window write loop. This window must
    // keep its own selection AND leave storage exactly as the sibling wrote it.
    renderChatPage('chat-2', transcript(PR_ONE))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })

    seedSelection('chat-2', PR_TWO)
    await act(async () => {
      window.dispatchEvent(new StorageEvent('storage', {
        key: selKey('chat-2'),
        newValue: localStorage.getItem(selKey('chat-2')),
        storageArea: localStorage,
      }))
    })
    await act(async () => {})

    expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    // The sibling's value is untouched — no answering write, so no ping-pong.
    expect(readSelection('chat-2')).toBe(PR_TWO)
  })
  it('never persists a reconciled pick, so a fresh slot stores nothing', async () => {
    // Reconciliation is in-memory only. Persisting sourceLinks[0] bought nothing
    // — the fallback is deterministic, so a return visit recomputes the same tab
    // — while every write from that path risked destroying a real choice.
    renderChatPage('chat-2', transcript(PR_ONE, PR_TWO))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })
    await act(async () => {})

    expect(readSelection('chat-2')).toBe('')
  })

  it('restores the stored tab once the transcript carries it, without a remount', async () => {
    // The provisional-render path, in ONE document: switchSlot.pending serves a
    // cached transcript with slotLoading already false, so the panel falls back in
    // memory while storage still holds PR 12. Nothing else re-reads storage in the
    // window that wrote it — loadSourceSelections runs only in the useState
    // initializer and the `storage` event never fires in the writing document — so
    // without the one re-read the fallback would stick until a reload. The
    // transcript is grown IN PLACE here; a remount would prove nothing, because a
    // fresh mount reads storage anyway.
    seedSelection('chat-2', PR_TWO)
    const { store } = renderChatPage('chat-2', transcript(PR_ONE))
    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_ONE)
    })
    expect(readSelection('chat-2')).toBe(PR_TWO)

    // The fetch lands and the transcript now carries PR 12.
    await act(async () => {
      store.dispatch(appendMessage({ role: 'assistant', content: `opened ${PR_TWO}`, cls: '' }))
    })

    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(PR_TWO)
    })
  })

  it('does not delete the stored tab when the transcript has no sources', async () => {
    // Same reasoning applied to the clear branch: a stale cached transcript can
    // be nonempty and still carry no links, which is not proof they are gone.
    seedSelection('chat-2', PR_TWO)
    renderChatPage('chat-2', [{ role: 'assistant', content: 'no links here', cls: '' }])
    await act(async () => {})

    expect(readSelection('chat-2')).toBe(PR_TWO)
  })

  it('leaves a stored tab alone when it sits beyond the rendered cap', async () => {
    // The panels render only the first MAX_PULL_REQUEST_SOURCES (64) tabs, so a
    // remembered url past that point is not reachable as a tab and the panel
    // normalizes to tab 0. That normalize must not be persisted: this fires even
    // for a fully-settled transcript, so it is the one trigger that does not need
    // a provisional render to destroy a real choice.
    const many = Array.from({ length: 70 }, (_, i) => `https://github.com/o/r/pull/${i + 1}`)
    const beyondCap = many[68]
    seedSelection('chat-2', beyondCap)
    renderChatPage('chat-2', [{ role: 'assistant', content: many.join(' '), cls: '' }])

    await waitFor(() => {
      expect(screen.getByTestId('selected-source')).toHaveTextContent(many[0])
    })
    await act(async () => {})

    expect(readSelection('chat-2')).toBe(beyondCap)
  })

  it('does not touch storage for a session with no pull requests', async () => {
    // The reconciliation effects re-run on every streaming chunk (the link index
    // hands back a fresh array each time), and commitSourceSelection enumerates
    // all of localStorage to decide whether the value already matches. An
    // unconditional clear therefore cost a full enumeration per chunk for every
    // session that never mentions a pull request — the common case. The clear is
    // gated on there being a selection to clear.
    const keySpy = vi.spyOn(Storage.prototype, 'key')
    try {
      renderChatPage('chat-2', [{ role: 'assistant', content: 'no links here', cls: '' }])
      await act(async () => {})
      expect(keySpy).not.toHaveBeenCalled()
    } finally {
      keySpy.mockRestore()
    }
  })
})
