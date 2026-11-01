/**
 * #13598 — ChatSidebar session-slot rename: closing the textarea never strands
 * focus on document.body.
 *
 * Enter and Escape return focus to the session row (data-session-row).
 * A pointer blur moves focus nowhere. A confirmed rename is announced through
 * the polite status region.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  // Cache per tag so the same component TYPE is returned on every render —
  // a new forwardRef() each time is a new type, which causes React to unmount
  // and remount the subtree and detaches any element that received focus.
  const cache = new Map<string, ReturnType<typeof React.forwardRef>>()
  const make = (tag: string) => {
    if (!cache.has(tag)) {
      cache.set(tag, React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
        const clean: Record<string, unknown> = {}
        for (const k of Object.keys(props)) {
          if (k === 'children') continue
          if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
          if (FRAMER_PROPS.has(k)) continue
          clean[k] = props[k]
        }
        return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
      }))
    }
    return cache.get(tag)!
  }
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))

const { renameSlotMock, chatSettingsCfg } = vi.hoisted(() => ({
  renameSlotMock: vi.fn(),
  chatSettingsCfg: { tagColumnsEnabled: false },
}))

vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: chatSettingsCfg.tagColumnsEnabled, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'renameSlot') return renameSlotMock
      return vi.fn().mockResolvedValue([])
    },
  }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'
import type { ChatSlot, ChatFolder, ChatTag, TagColumn } from '../types'

const SLOT_KEY = 'chat-slot-focus-test'
const SLOT_TITLE = 'Focus Test Session'

const makeSlot = (title = SLOT_TITLE): ChatSlot =>
  ({ key: SLOT_KEY, title, running: false, tags: [], created: '', last_ts: '' } as unknown as ChatSlot)

function renderSidebar(title = SLOT_TITLE) {
  const slot = makeSlot(title)
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon',
      sessionColorsIntensity: 'clear', slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], [])
  qc.setQueryData(['tag-columns'], [])
  qc.setQueryData(['chat-folders'], [])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
            <button type="button" data-testid="elsewhere">Elsewhere</button>
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, container: view.container }
}

const nextFrame = () => act(async () => { await new Promise(requestAnimationFrame) })

/** Open the rename textarea by double-clicking the session title.
 * Also advances one frame so the focus rAF fires, ensuring document.activeElement
 * is the textarea before Enter/Escape tests run. */
async function openRenameAsync(container: HTMLElement) {
  const wrap = container.querySelector(`[data-session-row]`) as HTMLElement
  expect(wrap).toBeTruthy()
  const titleDiv = wrap.querySelector('[data-session-title]') as HTMLElement
  expect(titleDiv).toBeTruthy()
  act(() => { fireEvent.click(titleDiv, { detail: 1 }) })
  act(() => { fireEvent.click(titleDiv, { detail: 2 }) })
  act(() => { fireEvent.doubleClick(titleDiv, { detail: 2 }) })
  // Advance one frame so rename.ts's focus rAF fires and textarea becomes activeElement
  await act(async () => { await new Promise(requestAnimationFrame) })
  const ta = container.querySelector('textarea') as HTMLTextAreaElement
  expect(ta).toBeTruthy()
  return ta
}

const statusRegion = () => document.querySelector('[data-testid="slot-rename-status"]')

beforeEach(() => {
  localStorage.clear()
  renameSlotMock.mockReset()
  renameSlotMock.mockResolvedValue({})
  chatSettingsCfg.tagColumnsEnabled = false
})
afterEach(() => vi.clearAllMocks())

describe('ChatSidebar slot rename — focus contract (#13598)', () => {
  it('slot-rename-status region is present and polite', () => {
    renderSidebar()
    const region = statusRegion()
    expect(region).toBeTruthy()
    expect(region?.getAttribute('role')).toBe('status')
    expect(region?.getAttribute('aria-live')).toBe('polite')
    expect(region?.textContent).toBe('')
  })

  it('Enter commits and returns focus to the session row', async () => {
    const { container } = renderSidebar()
    const textarea = await openRenameAsync(container)
    act(() => { fireEvent.change(textarea, { target: { value: 'Renamed' } }) })
    act(() => { fireEvent.keyDown(textarea, { key: 'Enter' }) })
    act(() => { fireEvent.blur(textarea) })
    await nextFrame()
    await nextFrame()
    const row = container.querySelector('[data-session-row]') as HTMLElement
    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('Escape cancels and returns focus to the session row', async () => {
    const { container } = renderSidebar()
    const textarea = await openRenameAsync(container)
    act(() => { fireEvent.change(textarea, { target: { value: 'Abandoned' } }) })
    act(() => { fireEvent.keyDown(textarea, { key: 'Escape' }) })
    await nextFrame()
    await nextFrame()
    expect(renameSlotMock).not.toHaveBeenCalled()
    const row = container.querySelector('[data-session-row]') as HTMLElement
    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('pointer blur commits but does not redirect focus', async () => {
    const { container } = renderSidebar()
    const textarea = await openRenameAsync(container)
    act(() => { fireEvent.change(textarea, { target: { value: 'Clicked Away' } }) })
    const elsewhere = screen.getByTestId('elsewhere')
    act(() => { elsewhere.focus() })
    act(() => { fireEvent.blur(textarea) })
    await nextFrame()
    await nextFrame()
    expect(renameSlotMock).toHaveBeenCalled()
    expect(document.activeElement).toBe(elsewhere)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('confirmed rename is announced through the polite status region', async () => {
    const { container } = renderSidebar()
    const textarea = await openRenameAsync(container)
    act(() => { fireEvent.change(textarea, { target: { value: 'Announced Session' } }) })
    act(() => { fireEvent.keyDown(textarea, { key: 'Enter' }) })
    act(() => { fireEvent.blur(textarea) })
    await waitFor(() => expect(statusRegion()?.textContent).toBe('Session renamed to Announced Session'))
  })
})

// ── Board-folder session rename: focus contract (#13598) ──────────────────────
// Regression: in board view, a session inside a folder is started with scope
// `${folderLaneScope}:${folder.id}` (boardLane.tsx:renderColumnFolder) but the
// row carries only folderLaneScope in data-session-scope. Both restore queries
// in rename.ts used the renameScope to build a selector that matched nothing,
// so Enter and Escape left focus on document.body.

const BF_TAG_ID = 'tag-board-focus'
const BF_COL_ID = 'col-board-focus'
const BF_FOLDER_ID = 'folder-board-focus'
const BF_SLOT_KEY = 'slot-board-focus'

const bfTag: ChatTag = { id: BF_TAG_ID, name: 'BF', color: '#aaa', order: 0, status: true }
const bfColumn: TagColumn = { id: BF_COL_ID, name: 'BF Col', tag_ids: [BF_TAG_ID], mode: 'any', order: 0 }
const bfFolder: ChatFolder = { id: BF_FOLDER_ID, name: 'BF Folder', order: 0 }
const bfSlot = (): ChatSlot => ({
  key: BF_SLOT_KEY, title: 'Board Folder Session', running: false,
  tags: [BF_TAG_ID], folder_id: BF_FOLDER_ID, created: '', last_ts: '',
} as unknown as ChatSlot)

function renderBoardFolderSidebar() {
  chatSettingsCfg.tagColumnsEnabled = true
  const slot = bfSlot()
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots: [slot], approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon',
      sessionColorsIntensity: 'clear', slotsLoaded: true,
    } as unknown as RootState['dashboard'],
    chat: { activeSlot: null } as unknown as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['chat-tags'], [bfTag])
  qc.setQueryData(['tag-columns'], [bfColumn])
  qc.setQueryData(['chat-folders'], [bfFolder])
  const view = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={[slot]} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, container: view.container }
}

describe('ChatSidebar board-folder slot rename — focus contract (#13598)', () => {
  // The board-folder path was broken: renameScope was `${folderLaneScope}:${folder.id}`
  // but data-session-scope carries only folderLaneScope, so the scope-keyed
  // querySelector found nothing and focus fell to document.body.

  it('board-folder Enter commits and returns focus to the session row', async () => {
    const { container } = renderBoardFolderSidebar()
    const textarea = await openRenameAsync(container)
    act(() => { fireEvent.change(textarea, { target: { value: 'Renamed BF' } }) })
    act(() => { fireEvent.keyDown(textarea, { key: 'Enter' }) })
    act(() => { fireEvent.blur(textarea) })
    await nextFrame()
    await nextFrame()
    const row = container.querySelector('[data-session-row]') as HTMLElement
    expect(row).toBeTruthy()
    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })

  it('board-folder Escape cancels and returns focus to the session row', async () => {
    const { container } = renderBoardFolderSidebar()
    const textarea = await openRenameAsync(container)
    act(() => { fireEvent.change(textarea, { target: { value: 'Abandoned BF' } }) })
    act(() => { fireEvent.keyDown(textarea, { key: 'Escape' }) })
    await nextFrame()
    await nextFrame()
    expect(renameSlotMock).not.toHaveBeenCalled()
    const row = container.querySelector('[data-session-row]') as HTMLElement
    expect(row).toBeTruthy()
    expect(document.activeElement).toBe(row)
    expect(document.activeElement).not.toBe(document.body)
  })
})
