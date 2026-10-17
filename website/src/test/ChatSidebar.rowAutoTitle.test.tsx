/**
 * The sessions-pane row menu offers "Regenerate title", so a stale name can be
 * re-derived from the conversation without making the session active. It calls
 * the same endpoint as the chat header's hover-revealed button and writes the
 * answer into the store; a refusal is surfaced in the sidebar's error cluster.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { useAppSelector, type RootState } from '../store'
import { initI18n } from '../i18n/all'
import i18next from 'i18next'
import type { ChatSlot } from '../types'

const { generateTitleMock } = vi.hoisted(() => ({ generateTitleMock: vi.fn() }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => false }))
vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: {
    ...actual.api,
    ...Object.fromEntries(['sessions', 'chatSlots', 'chatSlotDetail', 'fetchHistory', 'chatFolders', 'chatTags', 'tagColumns', 'channelTargets'].map(k => [k, vi.fn().mockResolvedValue([])])),
    generateTitle: (...args: unknown[]) => generateTitleMock(...args),
  } }
})
import ChatSidebar from '../pages/ChatSidebar'

const OLD_TITLE = 'What the session started as'
const SLOT_KEY = 's-row-auto-title'

function rows(): ChatSlot[] {
  return [
    { key: 'active', title: 'The active session', messages: 1, running: false, last_ts: '2026-01-02T00:00:00Z' },
    { key: SLOT_KEY, title: OLD_TITLE, messages: 12, running: false, last_ts: '2026-01-01T00:00:00Z' },
  ] as ChatSlot[]
}

// A different session is active: the point of the row item is that the session
// being renamed need not be.
function ConnectedSidebar() {
  const slots = useAppSelector(s => s.dashboard.slots)
  return <ChatSidebar slots={slots} activeSlot="active" unreadSlots={[]} history={[]} historyHasMore={false} defaultAgent="default" installedAgents={[]} />
}

function renderSidebar() {
  const store = createTestStore({
    dashboard: { status: { platform: 'darwin' }, connected: true, slots: rows(), approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {}, sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear', slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: 'active', messages: [], slotRunning: false, slotStopping: false, slotState: 'idle', slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0, pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null, subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [], slotMessages: {}, slotLoading: false,
    } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  for (const key of ['chat-folders', 'chat-tags', 'tag-columns', 'channel-targets']) qc.setQueryData([key], [])
  render(<QueryClientProvider client={qc}><Provider store={store}><ThemeProvider><MemoryRouter><ConnectedSidebar /></MemoryRouter></ThemeProvider></Provider></QueryClientProvider>)
  return { store }
}

function sessionRow() {
  return document.querySelector(`[data-session-row="${SLOT_KEY}"]`) as HTMLElement
}

async function pickRegenerate() {
  fireEvent.pointerDown(within(sessionRow()).getByRole('button', { name: 'More options' }), { button: 0, ctrlKey: false })
  fireEvent.click(await screen.findByRole('menuitem', { name: 'Regenerate title' }))
}

const titleInStore = (store: ReturnType<typeof createTestStore>) =>
  store.getState().dashboard.slots.find(s => s.key === SLOT_KEY)?.title

describe('session row menu: Regenerate title', () => {
  beforeEach(async () => {
    generateTitleMock.mockReset()
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
    await initI18n(); await i18next.changeLanguage('en')
  })

  it('regenerates an inactive session title and writes it into the store', async () => {
    generateTitleMock.mockResolvedValue({ title: 'What it is about now' })
    const { store } = renderSidebar()

    await pickRegenerate()

    expect(generateTitleMock).toHaveBeenCalledTimes(1)
    expect(generateTitleMock).toHaveBeenCalledWith(SLOT_KEY)
    await waitFor(() => expect(titleInStore(store)).toBe('What it is about now'))
    expect(screen.queryByTestId('auto-title-error')).toBeNull()
  })

  it('surfaces a refused regeneration and leaves the title alone', async () => {
    generateTitleMock.mockRejectedValue(new Error('model unavailable'))
    const { store } = renderSidebar()

    await pickRegenerate()

    const notice = await screen.findByTestId('auto-title-error')
    expect(notice.textContent).toContain("Couldn't generate a title")
    // The notice sits away from the row, so it names the session that failed.
    expect(notice.textContent).toContain(`${OLD_TITLE}: model unavailable`)
    expect(titleInStore(store)).toBe(OLD_TITLE)
  })

  it('does not start a second generation while one is in flight', async () => {
    let resolve: (v: { title: string }) => void = () => {}
    generateTitleMock.mockReturnValue(new Promise(r => { resolve = r }))
    const { store } = renderSidebar()

    await pickRegenerate()
    // The menu has closed; the row itself shows the call is running.
    expect(within(sessionRow()).getByTestId('row-title-generating').getAttribute('aria-label')).toBe('Generating title')
    await pickRegenerate()
    expect(generateTitleMock).toHaveBeenCalledTimes(1)

    resolve({ title: 'Settled' })
    await waitFor(() => expect(titleInStore(store)).toBe('Settled'))
    expect(screen.queryByTestId('row-title-generating')).toBeNull()
  })
})
