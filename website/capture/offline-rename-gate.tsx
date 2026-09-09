/**
 * Capture harness: a sidebar rename is REFUSED while the gateway is offline.
 *
 * Scenes, selected by ?scene=:
 *   affordance — connected; double-clicking a session title opens the editor
 *   refused    — disconnected; the same gesture opens nothing
 *   recovered  — starts disconnected, then the in-frame button reconnects
 *   artifacts  — disconnected; the Artifacts/Library folder menu, whose gated
 *                rows and standing reason row had no frame of their own
 *
 * initI18n is load-bearing, not boilerplate: without the catalog every i18nT
 * lookup returns undefined, so the row meta line renders "undefined 12:00 PM"
 * and the interpolator throws on `.replace()` of an undefined template. That is
 * harness noise, and committing it as evidence would misrepresent the product.
 *
 * Fixture values are invented — no real session key, title, path or person.
 */
import React from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseConnected } from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import { initI18n } from '../src/i18n/all'
import ChatSidebar from '../src/pages/ChatSidebar'
import { FolderMenu } from '../src/components/library/LibraryTable'
import SlotTagPopover from '../src/components/SlotTagPopover'
import { TagPopoverProvider } from '../src/hooks/useTagPopover'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'affordance'
const startConnected = scene === 'affordance'
initI18n(params.get('lang') || 'en')

// Collapse-by-staleness would hide the fixture rows.
localStorage.setItem('mc-session-stale-collapse-ms', '0')

const TS = '2026-09-08T12:00:00Z'
const slot = (key: string, title: string, messages: number) => ({
  key,
  title,
  messages,
  running: false,
  stopping: false,
  pending_approval: false,
  created: TS,
  last_ts: TS,
  last_turn_ts: TS,
  last_message: '…',
  agent: 'default',
  effective_agent: '',
  model: 'sonnet',
  reasoning_effort: '',
  mode: 'normal',
  surface: 'dashboard',
  workspace: '',
  executor: 'local',
  origin: 'user',
  memory_mode: 'persistent',
  clean_mode: false,
  pinned: false,
  trust: false,
  trust_reads: false,
  tags: [],
  links: [],
  source_links: [],
  source_links_total: 0,
  color_index: null,
  color_hex: null,
  forked_from: null,
})

const slots = [
  slot('chat-101-1700000001', 'Pipeline debug', 4),
  slot('chat-102-1700000002', 'Release notes draft', 2),
  slot('chat-103-1700000003', 'Flaky test triage', 7),
]

const store = configureStore({
  reducer: {
    dashboard: dashboardReducer,
    chat: chatReducer,
    notifications: notificationsReducer,
    instances: instancesReducer,
  },
  preloadedState: {
    dashboard: {
      status: { platform: 'linux' },
      connected: startConnected,
      slots,
      approvalMode: 'normal', channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
      slotsLoaded: true,
    },
    chat: {
      activeSlot: 'chat-101-1700000001',
      messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    },
  } as never,
})

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

function Harness() {
  if (scene === 'artifacts') {
    const folder = { id: 'af1', name: 'Design notes', parent_id: null, color: null } as never
    const noop = () => {}
    return (
      // A short box at the top: Radix opens the menu BELOW the trigger, so a tall
      // container pushed the reason row past the shutter's bottom edge.
      <div data-testid={`scene-${scene}`} style={{ display: 'flex', justifyContent: 'flex-start', width: 320, height: 32, padding: 4 }}>
        <FolderMenu
          folder={folder}
          folders={[folder]}
          actions={{
            onRename: noop, onDelete: noop, onSetColor: noop, onMove: noop,
            onNewSubfolder: noop, onToggleHideWhenEmpty: noop, onSettings: noop,
          } as never}
        />
      </div>
    )
  }
  return (
    // position: relative so the recovered scene's reconnect control sits INSIDE
    // the screenshot box instead of bleeding past its right edge.
    <div data-testid={`scene-${scene}`} style={{ position: 'relative', display: 'flex', width: 320, height: 520, overflow: 'hidden' }}>
      {scene === 'recovered' && (
        <button
          data-testid="harness-reconnect"
          style={{ position: 'absolute', top: 4, left: 4, zIndex: 50, fontSize: 11 }}
          onClick={() => store.dispatch(sseConnected())}
        >
          reconnect
        </button>
      )}
      <ChatSidebar
        slots={slots as never}
        activeSlot={'chat-101-1700000001'}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent={'default'}
        installedAgents={[] as never}
      />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          {/* The tag picker is an app-level singleton hosted by ChatPage, so the
              sidebar alone cannot open it — host it here to photograph it. */}
          <TagPopoverProvider>
            <Harness />
            <SlotTagPopover />
          </TagPopoverProvider>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
