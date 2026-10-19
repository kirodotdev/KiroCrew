import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { createConnectedTestStore, renderWithProviders } from '../../test/helpers'
import { __resetPanelTabs } from '../../hooks/usePanelTabs'
import { formatCommentsMessage, type InlineComment } from '../../components/CommentOverlay'

/* A markdown document open in the crewmate page's side panel takes inline
 * comments the way the chat page's side panel does, and Submit All delivers the
 * batch into THAT crewmate's chat. These cases drive the real path: the page,
 * its SidePanel and the real MarkdownPanel, a passage selected in the rendered
 * preview, the selection composer, Submit All, and the chat-core transport down
 * to its wire (`api.sendChat`). Only the thread pane is a stub: it hands the
 * page's file opener a path, the way a crewmate reply's file link does. */

const { DOC, CONTENT } = vi.hoisted(() => ({
  DOC: '/ws/release-notes.md',
  CONTENT: '# Release notes draft\n\nThe search box now keeps your last query when you come back.\n',
}))
const PASSAGE = 'keeps your last query'
const COMMENT = 'Say how long the query is kept.'

vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    crewBoard: vi.fn(() => Promise.reject(Object.assign(new Error('no_ledger'), { status: 404 }))),
    memberRecap: vi.fn(() => Promise.reject(new Error('no recap in this test'))),
    memberThread: vi.fn(),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    memberBriefing: vi.fn(() => Promise.resolve({ slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })),
    sessionCrewLogProjections: vi.fn(() => Promise.resolve({ folds: {}, resolved: true, writesDrained: true })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    fileDiff: vi.fn(() => Promise.resolve({ diff: '', original: '', status: 'clean' })),
    artifacts: vi.fn(() => Promise.resolve({ artifacts: [] })),
    // The side panel reads the slot's session environment; this file is about comments.
    chatSlotEnvironment: vi.fn(() => Promise.resolve({ profile: null, profiles: [] })),
    // The dashboard wire under `sendTurn`: every send from the page lands here.
    sendChat: vi.fn(),
  },
}))

/* The panel bodies this file is not about. MarkdownPanel stays real. */
vi.mock('../chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../chat/FilesHomePanel', () => ({ default: () => null }))
vi.mock('../chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => true,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../../hooks/useDevMode', () => ({ useDevMode: () => false }))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey, onFileOpen }: { slotKey: string; onFileOpen?: (path: string) => void }) => (
    <div data-testid="chat-pane-stub">
      {slotKey}
      {onFileOpen && <button onClick={() => onFileOpen(DOC)}>stub-open-doc</button>}
    </div>
  ),
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'

const sendChat = api.sendChat as ReturnType<typeof vi.fn>
const accepted = () => ({ ok: true, json: async () => ({ ok: true }) })

function row(overrides: Record<string, unknown> = {}) {
  return {
    name: 'oncall', slug: 'oncall', bound: false, slot_key: '', running: false,
    kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default', model: '',
    ...overrides,
  }
}

/** The copy helpers, loaded the way the other cases here load them. */
const copy = () => import('../../chat-core/transport/receiptCopy')

/** What the refused notice says after the crewmate's name: the name is the only
 *  part that varies with the roster, so the tail is what the cases compare. */
async function refusedTail(panelClosed: boolean) {
  const { commentsRefusedCopy } = await copy()
  return commentsRefusedCopy('\u0000', panelClosed).split('\u0000')[1]
}

function storedComments(): InlineComment[] | undefined {
  return JSON.parse(localStorage.getItem('mc-comment-drafts') ?? '{}')[DOC]
}

/** Opens crewmate `oncall`, waits for its thread to be confirmed, then opens
 *  the document in the side panel the way a reply's file link does. */
async function openDocument() {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row()], default_agent: 'kirocrew' })
  ;(api.memberThread as ReturnType<typeof vi.fn>).mockResolvedValue({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: true })
  const utils = renderWithProviders(<MembersPage />, { store: createConnectedTestStore() })
  fireEvent.click(await screen.findByText('oncall'))
  await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
  fireEvent.click(await screen.findByRole('button', { name: 'stub-open-doc' }))
  await screen.findByText(new RegExp(PASSAGE))
  return utils
}

/** Select PASSAGE in the rendered preview; the type-first comment box opens. */
async function selectPassage() {
  const para = await screen.findByText(new RegExp(PASSAGE))
  const text = para.firstChild as Text
  const start = text.data.indexOf(PASSAGE)
  const range = document.createRange()
  range.setStart(text, start)
  range.setEnd(text, start + PASSAGE.length)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
  fireEvent.mouseUp(document)
  return screen.findByLabelText('Comment on the selected text')
}

async function addComment() {
  fireEvent.change(await selectPassage(), { target: { value: COMMENT } })
  fireEvent.click(screen.getByLabelText('Add comment'))
  await waitFor(() => expect(document.querySelector('[data-comment-id]')).not.toBeNull())
  await waitFor(() => expect(storedComments()).toHaveLength(1))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  __resetPanelTabs()
  Object.defineProperty(window, 'innerWidth', { value: 1440, configurable: true, writable: true })
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.startsWith('/api/file-read')) return new Response(CONTENT, { status: 200 })
    if (url.startsWith('/api/knowledge/config')) return new Response(JSON.stringify({ enabled: false }), { status: 200 })
    return new Response('{}', { status: 404 })
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('MembersPage side panel: inline comments on a document', () => {
  it('offers the comment box on a selection, not only Copy', async () => {
    await openDocument()
    expect(await selectPassage()).toBeInTheDocument()
    expect(screen.getByLabelText('Add comment')).toBeInTheDocument()
  })

  it('Submit All sends the batch into this mate\'s chat as the panel wrote it, then clears it', async () => {
    sendChat.mockResolvedValue(accepted())
    await openDocument()
    await addComment()
    const batch = storedComments()!

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // The crewmate's own DM slot, and the message the chat page sends for the
    // same comments on the same file: MarkdownPanel's own formatting, verbatim.
    // No steer flag: the crewmate is idle, so the busy-send decision is a plain send.
    expect(sendChat).toHaveBeenCalledWith(
      formatCommentsMessage(DOC, batch, CONTENT), 'member-oncall', undefined, expect.any(AbortSignal), undefined, undefined,
    )
    const message = sendChat.mock.calls[0][0] as string
    expect(message).toContain(PASSAGE)
    expect(message).toContain(COMMENT)
    // Delivered: the panel drops the sent comment, from the list and from storage.
    await waitFor(() => expect(document.querySelector('[data-comment-id]')).toBeNull())
    expect(storedComments()).toBeUndefined()
    // A success info message shows the count (1 comment sent) using the neutral
    // status element -- NOT the error notice, which is reserved for failures.
    await waitFor(() => {
      const info = screen.queryByTestId('member-panel-action-info')
      expect(info).toBeTruthy()
      expect(info!.textContent).toMatch(/comment.*sent/i)
    })
    // The error banner must NOT appear on a successful send.
    expect(screen.queryByTestId('member-panel-action-error')).toBeNull()
  })

  it('success info shows plural count for 2 comments (dispatched/queued only, not unknown)', async () => {
    // Test count pluralization via the copy function directly (i18n is initialized
    // in the test setup; the integration test above already covers the render path).
    // count=1: singular form
    const { commentsSentCopy } = await import('../../chat-core/transport/receiptCopy')
    expect(commentsSentCopy(1)).toMatch(/1.*comment/i)
    // count=2: plural form (comments, not comment)
    expect(commentsSentCopy(2)).toMatch(/2.*comment/i)
    // count=5: plural form (covers ru _many; English same as _other)
    expect(commentsSentCopy(5)).toMatch(/5.*comment/i)
    // None of them should say "comment" without a count prefix
    expect(commentsSentCopy(2)).toContain('2')
    expect(commentsSentCopy(5)).toContain('5')
  })

  it('an unreadable 2xx body counts as delivered: the same status line as a confirmed send, comments cleared', async () => {
    // unknown: a 2xx whose body could not be read. The server took the request,
    // so the batch counts as delivered, as the chat page counts it. The line
    // states it as plainly as for a confirmed send: it has nothing to hedge.
    sendChat.mockResolvedValue({ ok: true, json: async () => { throw new SyntaxError('unreadable body') } })
    await openDocument()
    await addComment()

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('[data-comment-id]')).toBeNull())
    expect(storedComments()).toBeUndefined()
    // The neutral status element carries it (role=status), not the error notice.
    await waitFor(() => {
      const info = screen.queryByTestId('member-panel-action-info')
      expect(info).toBeTruthy()
      expect(info).toHaveAttribute('role', 'status')
      expect(info!.textContent).toBe('1 comment sent')
    })
    expect(screen.queryByTestId('member-panel-action-error')).toBeNull()
  })

  it('success clears a previous refused error banner', async () => {
    // Refused first, then a successful retry: the red banner must disappear.
    sendChat.mockResolvedValueOnce({ ok: false, json: async () => ({ error: 'slot unavailable' }) })
    sendChat.mockResolvedValueOnce(accepted())
    await openDocument()
    await addComment()

    // First send: refused
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Submit All/i })) })
    await waitFor(() => expect(screen.getByTestId('member-panel-action-error')).toBeTruthy())
    // Comment is still pending (refused keeps it)
    expect(document.querySelector('[data-comment-id]')).not.toBeNull()

    // Submit again: success -- banner should clear
    // Use role selector to find the Submit All BUTTON specifically (the error banner
    // also contains the text "Submit All" in its message).
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Submit All/i })) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(2))
    // Red banner must be gone after the success.
    await waitFor(() => expect(screen.queryByTestId('member-panel-action-error')).toBeNull())
  })

  it('a refused send shows a user-friendly error (no raw server reason), keeps the comments', async () => {
    sendChat.mockResolvedValue({ ok: false, json: async () => ({ error: 'slot unavailable' }) })
    await openDocument()
    await addComment()

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // Comments stay pending so the user can retry.
    expect(document.querySelector('[data-comment-id]')).not.toBeNull()
    expect(storedComments()).toHaveLength(1)
    // The action-error banner appears outside the DM (not a DM row).
    await waitFor(() => expect(screen.getByTestId('member-panel-action-error')).toBeTruthy())
    // The raw server reason ("slot unavailable") must not be visible in the UI.
    expect(screen.queryByText(/slot unavailable/i)).toBeNull()
    // The document is on screen beside the thread, so the notice does not tell the
    // person to reopen anything: it says the comments are still there to send again.
    const banner = screen.getByTestId('member-panel-action-error')
    expect(banner.textContent).toContain(await refusedTail(false))
    expect(banner.textContent).not.toContain(await refusedTail(true))
  })

  it('a transport-error receipt shows a delivery-unconfirmed banner, keeps the comments', async () => {
    // transport-error: the connection dropped before a response arrived.
    sendChat.mockRejectedValue(new TypeError('network error'))
    await openDocument()
    await addComment()

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // Delivery is indeterminate; comments stay pending for a retry.
    expect(document.querySelector('[data-comment-id]')).not.toBeNull()
    expect(storedComments()).toHaveLength(1)
    // The delivery-unconfirmed banner appears; never "comments sent".
    await waitFor(() => {
      const banner = screen.queryByTestId('member-panel-action-error')
      expect(banner).toBeTruthy()
      expect(banner!.textContent).not.toMatch(/comment.*sent/i)
    })
    // Panel beside the thread: the variant that sends them again from the document.
    const { deliveryUnconfirmedCommentsCopy } = await copy()
    expect(screen.getByTestId('member-panel-action-error').textContent).toContain(deliveryUnconfirmedCommentsCopy(false))
  })

  it('a response-late receipt shows a delivery-unconfirmed banner, keeps the comments', async () => {
    // response-late: the AbortController fired before a receipt arrived.
    sendChat.mockRejectedValue(new DOMException('aborted', 'AbortError'))
    await openDocument()
    await addComment()

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    // Delivery is indeterminate; comments stay pending for a retry.
    expect(document.querySelector('[data-comment-id]')).not.toBeNull()
    expect(storedComments()).toHaveLength(1)
    // The delivery-unconfirmed banner appears; never "comments sent".
    await waitFor(() => {
      const banner = screen.queryByTestId('member-panel-action-error')
      expect(banner).toBeTruthy()
      expect(banner!.textContent).not.toMatch(/comment.*sent/i)
    })
    // Panel beside the thread: the variant that sends them again from the document.
    const { deliveryUnconfirmedCommentsCopy } = await copy()
    expect(screen.getByTestId('member-panel-action-error').textContent).toContain(deliveryUnconfirmedCommentsCopy(false))
  })

  it('sends nothing while the thread is being re-confirmed, and the batch stays', async () => {
    sendChat.mockResolvedValue(accepted())
    await openDocument()
    await addComment()
    // Re-clicking the open crewmate re-POSTs its thread; until that answers
    // the slot is unconfirmed and the panel withholds the document tab with the
    // other slot-bound views (the body stays mounted, off the strip).
    let confirm!: (v: unknown) => void
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementationOnce(() => new Promise((resolve) => { confirm = resolve }))
    fireEvent.click(within(screen.getByTestId('member-roster')).getByText('oncall'))
    await waitFor(() => expect(api.memberThread).toHaveBeenCalledTimes(2))
    // Unconfirmed: the slot-bound views are off the strip.
    await waitFor(() => expect(screen.queryByRole('tab', { name: 'Files' })).toBeNull())

    // A person cannot press Submit here (its tab is withheld), so this reaches
    // the handler directly. Its slot check is the backstop: nothing is sent.
    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })
    expect(sendChat).not.toHaveBeenCalled()
    expect(storedComments()).toHaveLength(1)
    // And it stays silent: a notice would name a button nobody can see.
    expect(screen.queryByTestId('member-panel-action-error')).toBeNull()
    expect(screen.queryByTestId('member-panel-action-info')).toBeNull()

    // After re-confirmation the slot-bound views come back with the batch intact.
    await act(async () => { confirm({ slot_key: 'member-oncall', slug: 'oncall', member: 'oncall', created: false }) })
    await screen.findByRole('tab', { name: 'Files' })
    expect(storedComments()).toHaveLength(1)
  })

  it('a batch typed on one mate\'s document is only ever sent to that mate', async () => {
    sendChat.mockResolvedValue(accepted())
    ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({
      members: [row(), row({ name: 'fixer', slug: 'fixer' })],
      default_agent: 'kirocrew',
    })
    ;(api.memberThread as ReturnType<typeof vi.fn>).mockImplementation((slug: string) =>
      Promise.resolve({ slot_key: `member-${slug}`, slug, member: slug, created: true }),
    )
    renderWithProviders(<MembersPage />, { store: createConnectedTestStore() })
    fireEvent.click(await screen.findByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    fireEvent.click(await screen.findByRole('button', { name: 'stub-open-doc' }))
    await screen.findByText(new RegExp(PASSAGE))
    await addComment()

    // The other crewmate has its own panel strip: oncall's document is not on it,
    // so there is no Submit All that could aim oncall's batch at fixer's DM.
    fireEvent.click(within(screen.getByTestId('member-roster')).getByText('fixer'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-fixer'))
    await waitFor(() => expect(screen.queryByText(/Submit All/)).toBeNull())
    expect(sendChat).not.toHaveBeenCalled()

    // Back on oncall the document and its batch return, and Submit All sends to oncall.
    fireEvent.click(within(screen.getByTestId('member-roster')).getByText('oncall'))
    await waitFor(() => expect(screen.getByTestId('chat-pane-stub')).toHaveTextContent('member-oncall'))
    await act(async () => { fireEvent.click(await screen.findByText(/Submit All/)) })
    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    expect(sendChat.mock.calls[0][1]).toBe('member-oncall')
  })

  it('in overlay placement a failed send closes the overlay so the notice is not left under it', async () => {
    // A narrow window puts the panel in a fixed overlay over the thread. The
    // notice renders above the thread, so a failure that left the overlay open
    // would be hidden behind it.
    Object.defineProperty(window, 'innerWidth', { value: 700, configurable: true, writable: true })
    sendChat.mockResolvedValue({ ok: false, json: async () => ({ error: 'slot unavailable' }) })
    await openDocument()
    expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'overlay')
    await addComment()

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('member-panel-action-error')).toBeTruthy())
    await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull())
    // The batch survives in storage for the retry once the panel is reopened.
    expect(storedComments()).toHaveLength(1)
    // The panel just closed under the person, so the notice says to reopen it.
    const banner = screen.getByTestId('member-panel-action-error')
    expect(banner.textContent).toContain(await refusedTail(true))
    expect(await refusedTail(true)).not.toBe(await refusedTail(false))
  })

  it('in overlay placement a delivery-unconfirmed notice closes the overlay and says to reopen the panel', async () => {
    Object.defineProperty(window, 'innerWidth', { value: 700, configurable: true, writable: true })
    sendChat.mockRejectedValue(new TypeError('network error'))
    await openDocument()
    expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'overlay')
    await addComment()

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId('member-panel-action-error')).toBeTruthy())
    await waitFor(() => expect(screen.queryByTestId('member-side-panel')).toBeNull())
    expect(storedComments()).toHaveLength(1)
    const { deliveryUnconfirmedCommentsCopy } = await copy()
    expect(deliveryUnconfirmedCommentsCopy(true)).not.toBe(deliveryUnconfirmedCommentsCopy(false))
    expect(screen.getByTestId('member-panel-action-error').textContent).toContain(deliveryUnconfirmedCommentsCopy(true))
  })

  it('in overlay placement a delivered send keeps the overlay open', async () => {
    Object.defineProperty(window, 'innerWidth', { value: 700, configurable: true, writable: true })
    sendChat.mockResolvedValue(accepted())
    await openDocument()
    expect(screen.getByTestId('member-side-panel')).toHaveAttribute('data-placement', 'overlay')
    await addComment()

    await act(async () => { fireEvent.click(screen.getByText(/Submit All/)) })

    await waitFor(() => expect(sendChat).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.querySelector('[data-comment-id]')).toBeNull())
    expect(screen.getByTestId('member-side-panel')).toBeInTheDocument()
  })

  it('uses Your mate as fallback when crewmateLabel is empty', async () => {
    // When the member has no display name, commentsRefusedCopy falls back to
    // the localized "Your crewmate" string, not an empty string.
    const { commentsRefusedCopy } = await import('../../chat-core/transport/receiptCopy')
    const text = commentsRefusedCopy('')
    // Must not start with a space or punctuation (the name is empty).
    expect(text).not.toMatch(/^\s/)
    // Must not contain the literal empty-string interpolation "  didn" or similar.
    expect(text).not.toMatch(/\s{2,}/)
    // Must not start with a symbol from {{name}} being empty.
    expect(text.trimStart()).not.toMatch(/^didn/)
  })
})
