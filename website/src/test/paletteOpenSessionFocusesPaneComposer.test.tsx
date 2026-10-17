/**
 * Split view: a session opened from the Cmd/Ctrl+K surface must put the caret in
 * THAT session's pane (#15937).
 *
 * The single-view contract (#15732, #15785) is pinned in
 * `paletteOpenSessionFocusesComposer.test.tsx`. This file is its split-view
 * counterpart: the real session grid (`SessionGridView`) restoring a persisted
 * two-pane layout, each pane a REAL `ChatPane` with its own composer bound to its
 * own slot, beside the real quick-search surfaces -- the Command Bar app,
 * default-on, and the legacy palette it falls back to -- driven the way the
 * reporter drives them: open, arrow to a row, Enter.
 *
 * What makes split view its own case: the grid keeps a focus model of its own
 * (pane focus never follows `activeSlot`), and every pane's composer answers to a
 * fixed slot. So "the composer" is N composers, only one of which is the opened
 * session's. Before this fix the quick-search focus step could not tell which,
 * and focused nothing while any pane was mounted. Now each pane names its slot in
 * the DOM and the step resolves the pane bound to the opened key.
 *
 * Only the backend is mocked (plus the per-pane hooks the sibling ChatPane tests
 * stub for the same reason: no socket, no voice, no branding fetch).
 * `isTouchDevice` is pinned false because every composer-focus path skips touch
 * devices by design.
 */
import { useState, type ComponentType, type ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'

import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import SessionGridView from '../components/SessionGridView'
import CommandPalette from '../components/CommandPalette'
import CommandBarOverlay from '../apps/command-bar/CommandBarOverlay'
import type { GridNode } from '../hooks/useSessionGrid'
import type { ChatSlot } from '../types'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
// The per-pane stubs the sibling ChatPane tests use: a live pane would otherwise
// open a socket, probe the microphone and fetch branding.
vi.mock('../hooks/useWebSocket', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../hooks/useWebSocket')>()
  return { ...actual, useWebSocket: () => ({ subscribeLogs: () => {} }), emitSlotFocused: vi.fn() }
})
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }], choices: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const apiMock = vi.hoisted(() => ({
  sessions: vi.fn(),
  crons: vi.fn(),
  chatFolders: vi.fn(),
  chatSlots: vi.fn(),
  chatSlotDetail: vi.fn(),
  resumeChatSlot: vi.fn(),
  sessionsSearch: vi.fn(),
  instancesSearchSessions: vi.fn(),
  listInstances: vi.fn(),
  chatHistory: vi.fn(),
  models: vi.fn(),
  agents: vi.fn(),
  agentDetail: vi.fn(),
  workspaces: vi.fn(),
  spawnList: vi.fn(),
  fileSearch: vi.fn(),
  chatSlotSelectionCapabilities: vi.fn(),
}))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, ...apiMock } }
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

/** The two sessions on screen, left and right, and one that is not. The left
 *  pane is the grid-focused one on entry (the grid focuses its first leaf when
 *  it restores a layout), so a lookup that answered "the grid-focused pane"
 *  would land in LEFT for every open. */
const LEFT = 'zzq-left'
const RIGHT = 'zzq-right'
const OFFSCREEN = 'zzq-offscreen'
const LAYOUT_STORE_KEY = 'mc-split-layouts'

function slot(patch: Partial<ChatSlot> & { key: string }): ChatSlot {
  return { messages: 1, running: false, ...patch } as ChatSlot
}

const emptyTranscript = (key: string) => ({ key, messages: [], running: false, has_more: false, total: 0, next_before: 0 })

/** The live slots both the grid's slot list and the surfaces' recents read. */
const LIVE: ChatSlot[] = [
  slot({ key: LEFT, title: 'Left session', last_activity_ts: '2024-05-15T12:00:00Z' }),
  slot({ key: RIGHT, title: 'Right session', last_activity_ts: '2024-05-15T11:00:00Z' }),
  slot({ key: OFFSCREEN, title: 'Offscreen session', last_activity_ts: '2024-05-15T10:00:00Z' }),
]

const leaf = (id: string, slotKey: string): GridNode => ({ type: 'leaf', id, kind: 'session', slot: slotKey })
const twoPanes = (): GridNode => ({ type: 'split', id: 'split-root', dir: 'col', children: [leaf('l-left', LEFT), leaf('l-right', RIGHT)], sizes: [0.5, 0.5] })

type Surface = ComponentType<{ open: boolean; onClose: () => void }>
type Field = { role: 'combobox' | 'textbox'; name: string }

/** ChatPage's split-mode wiring in miniature: the grid where the composer would
 *  be, and the shell-owned open state of the quick-search surface beside it. */
function Harness({ surface: QuickSearch, mountWhileOpen }: { surface: Surface; mountWhileOpen: boolean }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>open palette</button>
      <SessionGridView onClose={() => {}} onCollapse={() => {}} seedSlot={LEFT} />
      {mountWhileOpen ? (open && <QuickSearch open onClose={() => setOpen(false)} />) : <QuickSearch open={open} onClose={() => setOpen(false)} />}
    </>
  )
}

/** Let the frames the helpers schedule run out, without asserting anything. */
const settleFrames = async (count = 3) => {
  for (let i = 0; i < count; i++) {
    await new Promise<void>((r) => requestAnimationFrame(() => r()))
  }
  await Promise.resolve()
}

beforeEach(() => {
  localStorage.clear()
  // The grid restores this anchor's persisted layout instead of seeding
  // [current | picker]: two real session panes, LEFT then RIGHT.
  localStorage.setItem(LAYOUT_STORE_KEY, JSON.stringify({ [LEFT]: twoPanes() }))
  apiMock.sessions.mockReset().mockResolvedValue({ sessions: [] })
  apiMock.crons.mockReset().mockResolvedValue([])
  apiMock.chatFolders.mockReset().mockResolvedValue([])
  apiMock.chatSlots.mockReset().mockResolvedValue(LIVE)
  apiMock.chatSlotDetail.mockReset().mockImplementation((key: string) => Promise.resolve(emptyTranscript(key)))
  apiMock.resumeChatSlot.mockReset().mockImplementation((key: string) =>
    Promise.resolve({ ok: true, key, mode: '', surface: '', messages: [], has_more: false, total: 0, next_before: 0 }))
  apiMock.sessionsSearch.mockReset().mockResolvedValue({ sessions: [] })
  apiMock.instancesSearchSessions.mockReset().mockRejectedValue(new Error('instances off'))
  apiMock.listInstances.mockReset().mockResolvedValue({ instances: [] })
  apiMock.chatHistory.mockReset().mockResolvedValue({ sessions: [] })
  apiMock.models.mockReset().mockResolvedValue([])
  apiMock.agents.mockReset().mockResolvedValue([])
  apiMock.agentDetail.mockReset().mockResolvedValue({})
  apiMock.workspaces.mockReset().mockResolvedValue({ workspaces: [] })
  apiMock.spawnList.mockReset().mockResolvedValue({ agents: [] })
  apiMock.fileSearch.mockReset().mockResolvedValue({ root: '/repo', results: [] })
  apiMock.chatSlotSelectionCapabilities.mockReset().mockResolvedValue({})
})
afterEach(() => { localStorage.clear() })

/** The pane roots and their composers, in document order: LEFT then RIGHT. Found
 *  by the grid's own pane marker and the composer hook, NOT by the slot name
 *  this fix adds -- so the lookup here works on base too, where that name does
 *  not exist, and the red run fails on focus and not on a missing attribute. */
async function panes() {
  await waitFor(() => expect(document.querySelectorAll('[data-chat-pane]')).toHaveLength(2))
  const roots = Array.from(document.querySelectorAll<HTMLElement>('[data-chat-pane]'))
  const composers = roots.map((root) => {
    const ta = root.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')
    if (!ta) throw new Error('pane without a composer')
    return ta
  })
  return { roots, composers }
}

async function openSurface(surface: Surface, field: Field, mountWhileOpen: boolean) {
  const store = createTestStore({ chat: { ...chatReducer(undefined, { type: '@@init' }), activeSlot: LEFT } })
  store.dispatch(sseSlots(LIVE))
  renderWithProviders(<Harness surface={surface} mountWhileOpen={mountWhileOpen} />, { store, route: '/chat' })
  const { roots, composers } = await panes()
  const [left, right] = composers
  // The grid-focused pane on entry is the FIRST leaf of the restored layout.
  expect(roots[0]).toHaveAttribute('data-chat-pane', 'focused')
  expect(roots[1]).toHaveAttribute('data-chat-pane', '')
  // Start from "focus somewhere else", as a user who clicked into a transcript
  // would: the panes' own mount-time autofocus is not the thing under test.
  ;(document.activeElement as HTMLElement | null)?.blur?.()
  expect(left).not.toHaveFocus()
  expect(right).not.toHaveFocus()

  fireEvent.click(screen.getByText('open palette'))
  const input = await screen.findByRole(field.role, { name: field.name })
  await waitFor(() => expect(input).toHaveFocus())
  // The pane title rows carry the same titles, so wait on the surface's own
  // rows (role=option), not on text anywhere on the page.
  await rowReady('Right session')
  await rowReady('Offscreen session')
  return { store, left, right, input }
}

const rowReady = (title: string) =>
  waitFor(() => expect(screen.getAllByRole('option').some((row) => row.textContent?.includes(title))).toBe(true))

/** Arrow down to the row carrying `title` and press Enter on it -- the
 *  reporter's keyboard path, through the surface's own key handling. */
function chooseRow(input: HTMLElement, title: string) {
  const rows = screen.getAllByRole('option')
  const index = rows.findIndex((row) => row.textContent?.includes(title))
  expect(index).toBeGreaterThanOrEqual(0)
  for (let i = 0; i < index; i++) fireEvent.keyDown(input, { key: 'ArrowDown' })
  expect(rows[index]).toHaveAttribute('aria-selected', 'true')
  fireEvent.keyDown(input, { key: 'Enter' })
}

const surfaces: Array<{ name: string; surface: Surface; field: Field; mountWhileOpen: boolean }> = [
  { name: 'Command Bar', surface: CommandBarOverlay, field: { role: 'combobox', name: 'Command Bar' }, mountWhileOpen: true },
  { name: 'legacy palette', surface: CommandPalette, field: { role: 'textbox', name: 'Search everywhere' }, mountWhileOpen: false },
]

describe.each(surfaces)('split view: opening a session from the $name (#15937)', ({ surface, field, mountWhileOpen }) => {
  const closed = () => waitFor(() => expect(screen.queryByRole(field.role, { name: field.name })).toBeNull())

  it('puts the caret in the pane bound to the opened session, not the grid-focused pane', async () => {
    const { store, left, right, input } = await openSurface(surface, field, mountWhileOpen)
    chooseRow(input, 'Right session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(RIGHT))
    await closed()
    // The focus lands on the frame after the switch has landed, so "holds
    // focus" is a wait, not a property of the first frame.
    await waitFor(() => expect(right).toHaveFocus())
    expect(left).not.toHaveFocus()
  })

  it('puts the caret in the opened pane even when it is the one already active', async () => {
    // The active slot is LEFT and LEFT is on screen: the key never changes, so no
    // autofocus effect runs anywhere -- the surface's own focus step is the
    // only thing that can place the caret.
    const { store, left, right, input } = await openSurface(surface, field, mountWhileOpen)
    chooseRow(input, 'Left session')
    expect(store.getState().chat.activeSlot).toBe(LEFT)
    await closed()
    await waitFor(() => expect(left).toHaveFocus())
    expect(right).not.toHaveFocus()
  })

  it('focuses no pane when the opened session is not on screen -- and the split stays as it was', async () => {
    // The open product question on the issue (leave the split, or swap the
    // session into the focused pane) is NOT decided here. What happens today is
    // pinned: the store switches, the grid shows the same two panes, and no
    // caret is placed -- the grid-focused pane is a session the gesture did not
    // open, where the next Enter would send.
    const { store, left, right, input } = await openSurface(surface, field, mountWhileOpen)
    chooseRow(input, 'Offscreen session')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(OFFSCREEN))
    await closed()
    await settleFrames()
    expect(left).not.toHaveFocus()
    expect(right).not.toHaveFocus()
    const roots = Array.from(document.querySelectorAll<HTMLElement>('[data-chat-pane]'))
    expect(roots).toHaveLength(2)
    expect(roots.map((r) => r.getAttribute('data-pane-slot'))).toEqual([LEFT, RIGHT])
  })
})

describe('split view: the legacy palette\'s search path resumes the session (#15937)', () => {
  // Typing routes the palette to its search providers; the sessions provider's
  // row opens through the async `resumeFromHistory` even for a live session, so
  // the key the focus step reads is the resume payload's, not a switch's.
  const field: Field = { role: 'textbox', name: 'Search everywhere' }

  it('puts the caret in the pane bound to the resumed session', async () => {
    apiMock.sessionsSearch.mockResolvedValue({ sessions: [{ key: RIGHT, title: 'Right session' }] })
    const { store, left, right, input } = await openSurface(CommandPalette, field, false)
    fireEvent.change(input, { target: { value: 'Right' } })
    // Wait for the row the assertion is about: the search is debounced and the
    // recents listing shows the same title until the search has been issued for
    // the typed query and answered (the recents rows then drop out).
    await waitFor(() => expect(apiMock.sessionsSearch.mock.calls.map((c) => c[0])).toContain('Right'), { timeout: 5000 })
    await waitFor(() => expect(screen.getAllByRole('option').some((row) => row.textContent?.includes('Offscreen session'))).toBe(false), { timeout: 5000 })
    chooseRow(input, 'Right session')
    await waitFor(() => expect(apiMock.resumeChatSlot).toHaveBeenCalledWith(RIGHT, 'Right session'))
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe(RIGHT))
    await waitFor(() => expect(screen.queryByRole(field.role, { name: field.name })).toBeNull())
    await waitFor(() => expect(right).toHaveFocus())
    expect(left).not.toHaveFocus()
  })
})
