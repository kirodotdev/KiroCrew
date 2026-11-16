/**
 * One-click edit shortcuts ("Quick edits" in the toolbar's More menu).
 *
 * Each item hands ONE preset instruction to the artifact's companion session as
 * background context and sends a short visible line ("Shorter · whole document")
 * as the turn; the agent then changes the artifact through its own
 * `artifact_update`. Pinned here:
 * - nothing is sent until an item is picked, and nothing is sent while offline;
 * - the instruction is written before the visible send; a refused write or a busy
 *   session sends nothing and says why;
 * - one status line follows the edit: working, saved as vN, or finished unchanged;
 * - a selection inside the rendered body scopes the instruction to that passage,
 *   no selection scopes it to the whole document;
 * - the scope label says the agent rewrites it and the result is saved as a new version;
 * - a passage picked with the mouse survives the selection box taking focus;
 * - a half-written sidebar comment is asked about before the chat panel replaces it;
 * - a failed session lookup shows its error and never creates a second session;
 * - with no bound session the shortcut opens one and sends there;
 * - widgets, apps and the other non-document kinds show no such menu, as on main.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders, createTestStore } from './helpers'
import { api } from '../api/client'
import { sseSlots, sseConnected, sseDisconnected } from '../store/dashboardSlice'
import { startLocalTurn, endLocalTurn } from '../store/chatSlice'
import { editQuotePreview, editShortcutPrompt, usableEditQuote } from '../components/artifactEditShortcuts.prompt'
import type { Artifact, ChatSlot } from '../types'
import { chooseMore, commentsShown, openMore } from './artifactMoreMenu'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '# Intro\n\nThe quick brown fox jumps over the lazy dog.',
  ...overrides,
})

const BOUND = { key: 'chat-bound', title: 'Artifact: CR Queue', messages: 3, running: false, artifact: 'cr-queue' } as ChatSlot
const accepted = () => ({ ok: true, json: () => Promise.resolve({ ok: true }) })
const QUICK_SOURCE = 'artifact-quick-edit'
/** The instructions the shortcuts handed over as background context, oldest first. */
const quickContexts = () => vi.mocked(api).chatSlotContext.mock.calls
  .filter((c) => (c[2] as { source?: string } | undefined)?.source === QUICK_SOURCE)
/** The text of the one instruction a shortcut wrote. */
async function sentInstruction(): Promise<string> {
  await waitFor(() => expect(quickContexts()).toHaveLength(1))
  return quickContexts()[0][1] as string
}
const DOC_LABEL = 'Agent rewrites the whole document · saved as a new version you can revert'

/** `cold`: the slots list has not loaded yet, so the page must ask the server before it decides. */
function renderPage(artifact: Artifact, opts: { bound?: boolean; connected?: boolean; cold?: boolean; popout?: boolean } = {}) {
  vi.mocked(api).artifact = vi.fn().mockResolvedValue(artifact)
  const store = createTestStore()
  act(() => {
    store.dispatch(opts.connected === false ? sseDisconnected() : sseConnected())
    if (!opts.cold) store.dispatch(sseSlots(opts.bound === false ? [] : [BOUND]))
  })
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage popout={opts.popout} />} />
    </Routes>,
    { route: '/artifacts/cr-queue', store },
  )
}

/** Open More, then the Quick edits submenu; resolves the submenu's items by name. */
async function openQuickEdits() {
  openMore()
  const trigger = await screen.findByRole('menuitem', { name: 'Quick edits' })
  fireEvent.click(trigger)
  await screen.findByRole('menuitem', { name: 'Shorter' })
}

/** The DOM selection a mouse drag over `word` leaves, then the mouseup the selection toolbar listens for. */
function selectWithMouse(word: string) {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node: Node | null = null
  while (walker.nextNode()) {
    if ((walker.currentNode.textContent ?? '').includes(word)) { node = walker.currentNode; break }
  }
  if (!node) throw new Error('word not in the rendered body')
  const start = (node.textContent ?? '').indexOf(word)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  // The test DOM has no layout; the toolbar reads the rect only to place its box.
  range.getBoundingClientRect = () => ({
    left: 10, top: 10, bottom: 30, right: 60, width: 50, height: 20, x: 10, y: 10, toJSON: () => ({}),
  }) as DOMRect
  const live = {
    isCollapsed: false, anchorNode: node, rangeCount: 1, getRangeAt: () => range,
    removeAllRanges: () => undefined, addRange: () => undefined, toString: () => word,
  } as unknown as Selection
  const spy = vi.spyOn(window, 'getSelection').mockReturnValue(live)
  const host = (node as Node).parentElement as HTMLElement
  fireEvent.mouseDown(host)
  fireEvent.mouseUp(host)
  /** What focus moving into the selection box's input does to the real selection. */
  const collapse = () => spy.mockReturnValue({
    isCollapsed: true, anchorNode: null, rangeCount: 0, getRangeAt: () => { throw new Error('no range') },
    removeAllRanges: () => undefined, addRange: () => undefined, toString: () => '',
  } as unknown as Selection)
  return { collapse }
}

const COMPOSER_INPUT = 'Comment on the selected text'
const SIDEBAR_BOX = 'Add a comment on the whole artifact…'

/** Open the sidebar and leave an unsent comment in its add box. */
async function leaveSidebarDraft() {
  if (!(await commentsShown())) await chooseMore(/Show comments/)
  fireEvent.click(await screen.findByRole('button', { name: /^Add comment$/ }))
  fireEvent.change(screen.getByPlaceholderText(SIDEBAR_BOX), { target: { value: 'unsent note' } })
}

describe('ArtifactDetailPage edit shortcuts', () => {
  afterEach(() => { vi.restoreAllMocks() })
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    window.getSelection()?.removeAllRanges()
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'cr-queue', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).sandboxDocUrl = vi.fn().mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    vi.mocked(api).createChatSlot = vi.fn().mockResolvedValue({ key: 'slot-new', title: 'Artifact: CR Queue' })
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([BOUND])
    vi.mocked(api).chatSlotDetail = vi.fn().mockResolvedValue({ messages: [], has_more: false, total: 0 })
    vi.mocked(api).sendChat = vi.fn().mockResolvedValue(accepted())
  })

  it('sends nothing until an item is picked', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
  })

  it('applies a shortcut to the whole document when nothing is selected', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    expect(screen.getByText(DOC_LABEL)).toBeInTheDocument()
    expect(screen.queryByText(/Applies to/)).toBeNull()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Proofread' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const [message, slot] = vi.mocked(api).sendChat.mock.calls[0]
    expect(slot).toBe('chat-bound')
    expect(message).toBe('Proofread · whole document')
    const instruction = await sentInstruction()
    expect(instruction).toContain('Proofread the whole document.')
    expect(instruction).toContain('slug `cr-queue`')
    expect(instruction).toContain('artifact_update')
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })

  it('scopes a shortcut to the selected passage', async () => {
    renderPage(mkArtifact())
    const body = await screen.findByText(/The quick brown fox/)
    const range = document.createRange()
    range.selectNodeContents(body)
    window.getSelection()?.addRange(range)
    await openQuickEdits()
    expect(screen.getByText('Agent rewrites “The quick brown fox jumps over the lazy dog.” · saved as a new version')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).sendChat.mock.calls[0][0]).toBe('Shorter · selected passage')
    const instruction = await sentInstruction()
    expect(instruction).toContain('Make the selected passage shorter.')
    expect(instruction).toContain(JSON.stringify('The quick brown fox jumps over the lazy dog.'))
  })

  it('sends the tone presets from the Change tone submenu', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Change tone' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'More formal' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).sendChat.mock.calls[0][0]).toBe('More formal · whole document')
    expect(await sentInstruction()).toContain('more formal tone')
  })

  it('opens a companion session when none is bound and sends there', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    renderPage(mkArtifact(), { bound: false })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Longer' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api).sendChat.mock.calls[0][1]).toBe('slot-new')
  })

  it('marks the Quick edits row with a pen, not the chat toggle sparkle, and shows chevrons on both submenu rows', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    const quick = screen.getByRole('menuitem', { name: 'Quick edits' })
    expect(quick.querySelector('svg.lucide-pen-line')).not.toBeNull()
    expect(quick.querySelector('svg.lucide-sparkles')).toBeNull()
    expect(quick.querySelector('svg.lucide-chevron-right')).not.toBeNull()
    expect(screen.getByRole('menuitem', { name: 'Change tone' }).querySelector('svg.lucide-chevron-right')).not.toBeNull()
  })

  it('keeps the passage picked with the mouse after the selection box takes focus', async () => {
    renderPage(mkArtifact())
    await screen.findByText(/The quick brown fox/)
    const { collapse } = selectWithMouse('brown fox')
    await screen.findByLabelText(COMPOSER_INPUT)
    collapse()
    await openQuickEdits()
    expect(screen.getByText('Agent rewrites “brown fox” · saved as a new version')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const instruction = await sentInstruction()
    expect(instruction).toContain('Make the selected passage shorter.')
    expect(instruction).toContain(JSON.stringify('brown fox'))
  })

  it.each([false, true])('asks before a half-written sidebar comment is replaced by the chat (popout %s)', async (popout) => {
    renderPage(mkArtifact(), { popout })
    await screen.findByText('CR Queue')
    await leaveSidebarDraft()
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    const dialog = await screen.findByRole('dialog')
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByPlaceholderText(SIDEBAR_BOX)).toHaveValue('unsent note')
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    expect(screen.queryByTestId('chat-page')).toBeNull()
  })

  it('sends once the half-written sidebar comment is discarded', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await leaveSidebarDraft()
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard comment' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(await screen.findByTestId('chat-page')).toBeInTheDocument()
  })

  it('asks nothing when the sidebar holds no draft', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    if (!(await commentsShown())) await chooseMore(/Show comments/)
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('shows a long passage cut at a word with an ellipsis, on one line', async () => {
    const long = 'We ship on Tuesday, after the team signs off on the release notes and the rollback plan is written down.'
    renderPage(mkArtifact({ content: `# Intro\n\n${long}` }))
    const body = await screen.findByText(/We ship on Tuesday/)
    const range = document.createRange()
    range.selectNodeContents(body)
    window.getSelection()?.addRange(range)
    await openQuickEdits()
    expect(screen.getByText('Agent rewrites “We ship on Tuesday, after the team signs off on the release…” · saved as a new version'))
      .toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(await sentInstruction()).toContain(JSON.stringify(long))
  })

  it('asks again after the session lookup when a comment was typed during the wait, and sends nothing if it is kept', async () => {
    let release: (slots: ChatSlot[]) => void = () => undefined
    const pending = new Promise<ChatSlot[]>((resolve) => { release = resolve })
    vi.mocked(api).chatSlots = vi.fn().mockReturnValue(pending)
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await leaveSidebarDraft()
    await act(async () => { release([BOUND]); await pending })
    const dialog = await screen.findByRole('dialog')
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByPlaceholderText(SIDEBAR_BOX)).toHaveValue('unsent note')
    expect(screen.queryByTestId('chat-page')).toBeNull()
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })

  it('sends after the second ask when the comment typed during the lookup is discarded', async () => {
    let release: (slots: ChatSlot[]) => void = () => undefined
    const pending = new Promise<ChatSlot[]>((resolve) => { release = resolve })
    vi.mocked(api).chatSlots = vi.fn().mockReturnValue(pending)
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await leaveSidebarDraft()
    await act(async () => { release([BOUND]); await pending })
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard comment' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).sendChat.mock.calls[0][1]).toBe('chat-bound')
  })

  it('shows the error and creates no second session when the session lookup fails', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockRejectedValue(new Error('slots unavailable'))
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    expect(await screen.findByText('slots unavailable')).toBeInTheDocument()
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
  })

  it('sends to the session the lookup finds on a cold page, and creates none', async () => {
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).sendChat.mock.calls[0][1]).toBe('chat-bound')
    expect(vi.mocked(api).createChatSlot).not.toHaveBeenCalled()
  })

  it('creates a session only after a successful lookup that found none', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
    renderPage(mkArtifact(), { cold: true })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).createChatSlot).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api).sendChat.mock.calls[0][1]).toBe('slot-new')
  })

  it('sends nothing while the gateway is disconnected', async () => {
    renderPage(mkArtifact(), { connected: false })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await act(async () => { await Promise.resolve() })
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
  })

  it('shows a short line as the turn and hands the instruction over as background context first', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(1))
    const send = vi.mocked(api).sendChat.mock
    expect(send.calls[0][0]).toBe('Shorter · whole document')
    expect(send.calls[0][0]).not.toContain('artifact_')
    // No steer: a busy session is refused before the send, so this is always a fresh turn.
    expect(send.calls[0][5]).toBeUndefined()
    const [slot, instruction, opts] = quickContexts()[0]
    expect(slot).toBe('chat-bound')
    expect(opts).toEqual({ source: QUICK_SOURCE, maxAge: 60 })
    expect(instruction).toMatch(/^The user's next message is a Quick edit picked on the artifact page\./)
    const ctx = vi.mocked(api).chatSlotContext.mock
    const ctxOrder = ctx.invocationCallOrder[ctx.calls.indexOf(quickContexts()[0])]
    expect(ctxOrder).toBeLessThan(send.invocationCallOrder[0])
  })

  it('sends nothing and shows the reason when the background context is refused', async () => {
    vi.mocked(api).chatSlotContext = vi.fn().mockImplementation((_s: string, _t: string, o?: { source?: string }) =>
      o?.source === QUICK_SOURCE ? Promise.reject(new Error('context queue full')) : Promise.resolve({ ok: true }))
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    expect(await screen.findByText('context queue full')).toBeInTheDocument()
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    expect(screen.queryByRole('status', { name: /Quick edit/ })).toBeNull()
  })

  it('sends nothing and writes no context while the bound session is busy', async () => {
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([{ ...BOUND, running: true }])
    const { store } = renderPage(mkArtifact(), { bound: false })
    act(() => { store.dispatch(sseSlots([{ ...BOUND, running: true }])); store.dispatch(startLocalTurn('chat-bound')) })
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    expect(await screen.findByText('The agent is still working in this chat. Try the quick edit again when it finishes.'))
      .toBeInTheDocument()
    expect(vi.mocked(api).sendChat).not.toHaveBeenCalled()
    expect(quickContexts()).toHaveLength(0)
  })

  it('follows the edit on one status line: working, then saved as the new version', async () => {
    const { queryClient } = renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    const status = await screen.findByRole('status', { name: /Quick edit/ })
    await waitFor(() => expect(status).toHaveTextContent('Shorter: the agent is rewriting the whole document…'))
    expect(within(status).queryByRole('button')).toBeNull()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ version: 3 }))
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['artifact', 'cr-queue'] }) })
    await waitFor(() => expect(status).toHaveTextContent(
      'Shorter: saved as v3. To undo, pick the earlier version under Version and press Revert.'))
    fireEvent.click(within(status).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status', { name: /Quick edit/ })).toBeNull()
  })

  it('says the agent finished without a new version when its turn ends and the version did not move', async () => {
    const { store } = renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Proofread' }))
    const status = await screen.findByRole('status', { name: /Quick edit/ })
    await waitFor(() => expect(status).toHaveTextContent('Proofread: the agent is rewriting the whole document…'))
    act(() => { store.dispatch(startLocalTurn('chat-bound')) })
    expect(status).toHaveTextContent('Proofread: the agent is rewriting the whole document…')
    act(() => { store.dispatch(endLocalTurn('chat-bound')) })
    await waitFor(() => expect(status).toHaveTextContent(
      'Proofread: the agent finished without saving a new version. Its reply is in the chat.'))
    fireEvent.click(within(status).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('status', { name: /Quick edit/ })).toBeNull()
  })

  it('replaces the status line with the next quick edit', async () => {
    renderPage(mkArtifact())
    await screen.findByText('CR Queue')
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Shorter' }))
    const first = await screen.findByRole('status', { name: /Quick edit/ })
    await waitFor(() => expect(first).toHaveTextContent('Shorter:'))
    await openQuickEdits()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Longer' }))
    await waitFor(() => expect(vi.mocked(api).sendChat).toHaveBeenCalledTimes(2))
    const lines = await screen.findAllByRole('status', { name: /Quick edit/ })
    expect(lines).toHaveLength(1)
    await waitFor(() => expect(lines[0]).toHaveTextContent('Longer: the agent is rewriting the whole document…'))
  })

  it.each(['widget', 'webapp', 'json', 'svg', 'image'] as const)('offers no Quick edits on a %s artifact', async (kind) => {
    renderPage(mkArtifact({ kind, content: kind === 'json' ? '{}' : '<div>x</div>' }))
    await screen.findByText('CR Queue')
    openMore()
    await screen.findByRole('menu')
    expect(screen.queryByRole('menuitem', { name: 'Quick edits' })).toBeNull()
  })
})

describe('edit shortcut prompt', () => {
  it('drops a blank or over-long selection so the shortcut falls back to the whole document', () => {
    expect(usableEditQuote(undefined)).toBe('')
    expect(usableEditQuote('   \n ')).toBe('')
    expect(usableEditQuote('x'.repeat(4001))).toBe('')
    expect(usableEditQuote('  keep me  ')).toBe('keep me')
  })

  it('previews a passage on one line, cut between words and ended with an ellipsis', () => {
    expect(editQuotePreview('  We ship\n on   Tuesday  ')).toBe('We ship on Tuesday')
    expect(editQuotePreview('one two three four', 12)).toBe('one two…')
    expect(editQuotePreview('one two three', 13)).toBe('one two three')
    expect(editQuotePreview('x'.repeat(100))).toBe(`${'x'.repeat(60)}…`)
    expect(editQuotePreview('We ship on Tuesday, after the team signs off on the release notes'))
      .toBe('We ship on Tuesday, after the team signs off on the release…')
  })

  it('quotes the passage as a JSON string so it cannot break out of the instruction', () => {
    const text = editShortcutPrompt('shorter', 'Doc', 'doc', 'say "hi"\nand stop')
    expect(text).toContain(JSON.stringify('say "hi"\nand stop'))
    expect(text).not.toContain('\n')
  })
})
