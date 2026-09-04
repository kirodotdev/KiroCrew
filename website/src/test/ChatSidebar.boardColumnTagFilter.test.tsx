/**
 * Board column tag filter (issue #1897): a column whose filter names a tag
 * must render ONLY the sessions carrying that tag — the reported failure was
 * every column showing the full session list (the match-all fallback that an
 * empty tag_ids produces).
 *
 * Also covers the write-path contract that closes the bug for good: when the
 * column PATCH is rejected (the server now 400s on an unknown tag id instead
 * of silently dropping it), the mutation's onError re-syncs both the
 * ['chat-tags'] and ['tag-columns'] caches so a stale popover redraws from
 * server truth instead of showing a selection that was never applied.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import { PREVIEW_INSTANCE_SESSIONS } from '../utils/previewFlags'
import type { RootState } from '../store'

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef<HTMLElement, Record<string, unknown> & { children?: React.ReactNode }>(
      (props, ref) => {
        const clean: Record<string, unknown> = {}
        for (const k of Object.keys(props)) {
          if (k === 'children') continue
          if (FRAMER_PROPS.has(k)) continue
          clean[k] = props[k]
        }
        return React.createElement(tag, { ...clean, ref }, props.children)
      })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: { children?: React.ReactNode }) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

const {
  TAG_JIRA, COL_FILTERED, COL_ALL, tags, columns,
  updateTagColumn, listInstancesMock, instanceChatSlotsMock, chatFoldersMock,
} = vi.hoisted(() => {
  const TAG_JIRA = '11111111-1111-1111-1111-111111111111'
  const COL_FILTERED = 'col-filtered'
  const COL_ALL = 'col-all'
  const REMOTE_KEY = 'remote-untagged-1'
  return {
    TAG_JIRA,
    COL_FILTERED,
    COL_ALL,
    tags: [{ id: TAG_JIRA, name: 'Jira', color: '#e11', order: 0, status: false }],
    columns: [
      { id: COL_FILTERED, name: 'Jira lane', tag_ids: [TAG_JIRA], mode: 'any' as const, order: 0 },
      { id: COL_ALL, name: 'Everything', tag_ids: [] as string[], mode: 'any' as const, order: 1 },
    ],
    updateTagColumn: vi.fn(),
    listInstancesMock: vi.fn().mockResolvedValue({
      instances: [{ id: 'inst-a', name: 'astro', status: { state: 'connected' } }],
    }),
    instanceChatSlotsMock: vi.fn().mockResolvedValue([
      { key: REMOTE_KEY, title: 'Remote untagged', running: false, last_turn_ts: '2026-09-01T12:00:00Z' },
    ]),
    chatFoldersMock: vi.fn().mockResolvedValue([]),
  }
})
// chatTags/tagColumns must serve the SAME data seeded into the query cache:
// React Query refetches on mount, and an empty-array default would replace the
// seeded caches and unmount the popover's tag rows mid-test.
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({}, {
    get: (_t, prop: string) => {
      if (prop === 'updateTagColumn') return updateTagColumn
      if (prop === 'chatTags') return () => Promise.resolve(tags)
      if (prop === 'tagColumns') return () => Promise.resolve(columns)
      if (prop === 'listInstances') return listInstancesMock
      if (prop === 'instanceChatSlots') return instanceChatSlotsMock
      if (prop === 'chatFolders') return chatFoldersMock
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

const TAGGED_KEY = 'chat-tagged-1'
const UNTAGGED_KEY = 'chat-untagged-1'

const taggedSlot = { key: TAGGED_KEY, title: 'Tagged', running: false, tags: [TAG_JIRA], created: '', last_ts: '' }
const untaggedSlot = { key: UNTAGGED_KEY, title: 'Untagged', running: false, tags: [], created: '', last_ts: '' }

function renderSidebar(
  slots = [taggedSlot, untaggedSlot],
  folders: Array<{ id: string; name: string; collapsed: boolean; order: number }> = [],
) {
  chatFoldersMock.mockResolvedValue(folders)
  const store = createTestStore({
    dashboard: {
      status: {}, connected: false, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as RootState['dashboard'],
    chat: { activeSlot: null } as RootState['chat'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  const utils = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { ...utils, qc }
}

beforeEach(() => {
  localStorage.clear()
  updateTagColumn.mockReset()
  chatFoldersMock.mockReset().mockResolvedValue([])
})
afterEach(() => vi.clearAllMocks())

function slotKeysIn(container: HTMLElement, columnId: string): string[] {
  const col = container.querySelector(`[data-testid="column-${columnId}"]`)
  expect(col).toBeTruthy()
  return Array.from((col as HTMLElement).querySelectorAll('[data-slot-key]'))
    .map(el => el.getAttribute('data-slot-key') as string)
}

describe('board column tag filter', () => {
  it('keeps remote rows out of board columns and names the hidden count', async () => {
    localStorage.setItem(PREVIEW_INSTANCE_SESSIONS, '1')
    const folderId = 'collision-folder'
    instanceChatSlotsMock.mockResolvedValueOnce([
      {
        key: TAGGED_KEY,
        title: 'Remote colliding with foldered local',
        running: false,
        last_turn_ts: '2026-09-01T12:00:00Z',
      },
    ])
    const { container } = renderSidebar(
      [{ ...taggedSlot, folder_id: folderId }, untaggedSlot],
      [{ id: folderId, name: 'Collision folder', collapsed: false, order: 0 }],
    )

    await waitFor(() => expect(instanceChatSlotsMock).toHaveBeenCalled())
    await waitFor(() => expect(container.textContent).toContain('1 remote session is not shown in board view'))
    const allColumn = container.querySelector(`[data-testid="column-${COL_ALL}"]`)
    expect(allColumn?.textContent).toContain('Tagged')
    expect(allColumn?.textContent).not.toContain('Remote colliding with foldered local')
  })

  it('renders only sessions carrying the selected tag in a filtered column', () => {
    const { container } = renderSidebar()
    // The filtered lane holds exactly the tagged session — not the full list.
    expect(slotKeysIn(container, COL_FILTERED)).toEqual([TAGGED_KEY])
    // The unfiltered (match-all) lane still shows every session.
    expect(new Set(slotKeysIn(container, COL_ALL))).toEqual(new Set([TAGGED_KEY, UNTAGGED_KEY]))
  })

  it('re-syncs tag + column caches when the column PATCH is rejected', async () => {
    updateTagColumn.mockRejectedValue(new Error('invalid_column_payload'))
    const { container, qc } = renderSidebar()
    const invalidate = vi.spyOn(qc, 'invalidateQueries')
    // Open the filtered column's popover and toggle the tag swatch.
    fireEvent.click(container.querySelector(`[data-testid="column-edit-${COL_FILTERED}"]`)!)
    const swatch = await waitFor(() => {
      const el = document.querySelector(`[data-column-popover="${COL_FILTERED}"] [role="checkbox"]`)
      expect(el).toBeTruthy()
      return el as HTMLElement
    })
    fireEvent.click(swatch)
    await waitFor(() => expect(updateTagColumn).toHaveBeenCalledWith(COL_FILTERED, { tag_ids: [] }))
    await waitFor(() => {
      const keys = invalidate.mock.calls.map(c => JSON.stringify(c[0]?.queryKey))
      expect(keys).toContain(JSON.stringify(['chat-tags']))
      expect(keys).toContain(JSON.stringify(['tag-columns']))
    })
  })

  it('popover Tab trap wraps a boundary Tab but declines one an IME composition owns', async () => {
    // On WebKit the keydown that commits a candidate arrives AFTER
    // compositionend with `isComposing` already false — unguarded, the trap
    // would yank focus and abort the composition (issue class of the shared
    // hook's own guard).
    const { container } = renderSidebar()
    fireEvent.click(container.querySelector(`[data-testid="column-edit-${COL_FILTERED}"]`)!)
    const popover = await waitFor(() => {
      const el = document.querySelector(`[data-column-popover="${COL_FILTERED}"]`)
      expect(el).toBeTruthy()
      return el as HTMLElement
    })
    const focusable = Array.from(popover.querySelectorAll<HTMLElement>(
      'a[href],button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])',
    ))
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    expect(focusable.length).toBeGreaterThan(1)

    // Positive control: a plain boundary Tab still wraps.
    last.focus()
    fireEvent.keyDown(popover, { key: 'Tab' })
    expect(document.activeElement).toBe(first)

    // A boundary Tab inside the post-composition window is declined.
    last.focus()
    fireEvent.compositionStart(last)
    fireEvent.compositionEnd(last)
    fireEvent.keyDown(popover, { key: 'Tab' })
    expect(document.activeElement).toBe(last)
    expect(document.activeElement).not.toBe(first)
  })
})
