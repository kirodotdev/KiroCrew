/**
 * Anchored comment creation on ArtifactDetailPage.
 *
 * An anchored comment is how a human pins an instruction to an EXACT span of an
 * artifact for the agent to act on: select text, type a note, and the comment is
 * stored with the quoted span plus enough surrounding context to relocate it
 * later. The backend persists `quote` / `prefix` / `suffix` / `start_offset` /
 * `end_offset` / `version_number` and re-validates every anchor on each content
 * write (flipping `anchor_orphaned` when the span disappears); the agent reads
 * the anchors back through `artifact_get_comments`.
 *
 * That whole chain is worthless if the client posts a comment WITHOUT its
 * anchor — the note silently degrades to a free-floating document comment and
 * the agent loses the "which part of this?" signal. These tests pin the payload.
 *
 * The test DOM has no real text selection, so `window.getSelection` is backed by a
 * real `Range` over the rendered body — the same object the production code reads.
 * Selecting opens the toolbar's type-first composer (the same box the file
 * viewer uses); its `onOpen` is where the page resolves the anchor.
 * The suite uses a MARKDOWN artifact because that is the kind that renders to a
 * DOM tree behind `previewRef`; text/json/svg render as a highlighted <pre> with
 * no preview ref, so a DOM selection there has nothing to map back to source.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const BODY = 'alpha beta gamma'

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'notes',
  name: 'Notes',
  kind: 'markdown',
  source: 'chat',
  description: '',
  tags: [],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: BODY,
  ...overrides,
})

function renderPage() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      <Route path="/artifacts" element={<div>library page target</div>} />
    </Routes>,
    { route: '/artifacts/notes' },
  )
}

/**
 * Select `word` inside the rendered artifact body and fire the mouseup the page
 * listens on. Returns false when the body text node isn't found, so a test fails
 * loudly rather than silently asserting nothing.
 */
function selectInBody(word: string): boolean {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node: Node | null = null
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent ?? ''
    if (t.includes(word) && t.includes(BODY)) { node = walker.currentNode; break }
  }
  if (!node) return false
  const text = node.textContent ?? ''
  const start = text.indexOf(word)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  // The test DOM's Range has no layout; the toolbar only reads the rect to place the box.
  range.getBoundingClientRect = () => ({
    left: 10, top: 10, bottom: 30, right: 60, width: 50, height: 20, x: 10, y: 10,
    toJSON: () => ({}),
  }) as DOMRect

  vi.spyOn(window, 'getSelection').mockReturnValue({
    isCollapsed: false,
    anchorNode: node,
    rangeCount: 1,
    getRangeAt: () => range,
    removeAllRanges: () => undefined,
    addRange: () => undefined,
    toString: () => word,
  } as unknown as Selection)

  // The selection toolbar listens for mouseup on the document; the rendered
  // text node's element ancestor is inside its container, so the bubbled event
  // reaches it and the (debounced) selection check opens the composer.
  const host = node.parentElement as HTMLElement
  fireEvent.mouseDown(host)
  fireEvent.mouseUp(host)
  return true
}

/** The composer's textarea, as the toolbar labels it. */
const COMPOSER_INPUT = 'Comment on the selected text'

/** Let the toolbar's debounced selection check run, so a negative assertion
 *  ("no composer") is made after the point at which one would have opened. */
const settle = () => act(() => new Promise<void>(resolve => { setTimeout(resolve, 80) }))

describe('ArtifactDetailPage anchored comments', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'notes', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({ slug: 'notes', events: [] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(
      mkArtifact({ version: 1, content: 'alpha beta gamma (v1)' }),
    )
    vi.mocked(api).postArtifactComment = vi.fn().mockResolvedValue({ ok: true })
  })

  afterEach(() => { vi.restoreAllMocks() })

  it('stores the selected span as the comment anchor', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())

    expect(selectInBody('beta')).toBe(true)
    const input = await screen.findByLabelText(COMPOSER_INPUT)
    fireEvent.change(input, { target: { value: 'tighten this wording' } })
    fireEvent.click(screen.getByLabelText('Add comment'))

    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    const [slug, body] = vi.mocked(api).postArtifactComment.mock.calls[0]
    expect(slug).toBe('notes')
    expect(body.text).toBe('tighten this wording')
    const anchor = body.anchor as { quote: string; start_offset?: number; end_offset?: number }
    expect(anchor).toBeDefined()
    expect(anchor.quote).toBe('beta')
    // Offsets pin the comment to THIS occurrence when the quote repeats. They are
    // rendered-text offsets (what the highlighter walks), so assert the span
    // width rather than a source index.
    expect(typeof anchor.start_offset).toBe('number')
    expect((anchor.end_offset ?? 0) - (anchor.start_offset ?? 0)).toBe('beta'.length)
  })

  it('reveals the comments panel after an anchored add', async () => {
    // The new comment has to be visible somewhere, or the user cannot tell it
    // landed — an anchored add hands control back to the comment-driven default.
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    expect(selectInBody('gamma')).toBe(true)
    fireEvent.change(await screen.findByLabelText(COMPOSER_INPUT), { target: { value: 'note' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await waitFor(() =>
      expect(screen.getByLabelText('Toggle comments')).toHaveAttribute('aria-pressed', 'true'))
  })

  it('a refused post keeps the typed comment in the box with a notice, and the retry lands', async () => {
    // A single rejected POST (offline, gateway restart, 5xx) must never be the
    // moment the only copy of the comment disappears.
    vi.mocked(api).postArtifactComment.mockRejectedValueOnce(new Error('gateway restarting'))
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    expect(selectInBody('beta')).toBe(true)
    const input = await screen.findByLabelText(COMPOSER_INPUT)
    fireEvent.change(input, { target: { value: 'still mine' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await screen.findByText(/Couldn’t save your comment/)
    expect(screen.getByLabelText(COMPOSER_INPUT)).toHaveValue('still mine')
    // The panel did not reveal for a comment that was not stored.
    expect(screen.getByRole('button', { name: /comments/i })).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(screen.getByLabelText('Add comment'))
    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByLabelText(COMPOSER_INPUT)).toBeNull())
    const [, body] = vi.mocked(api).postArtifactComment.mock.calls[1]
    expect(body.text).toBe('still mine')
    expect((body.anchor as { quote: string }).quote).toBe('beta')
  })

  it('does not open the composer for a collapsed (empty) selection', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true, rangeCount: 0, toString: () => '', removeAllRanges: () => undefined,
    } as unknown as Selection)
    fireEvent.mouseUp(document.body)
    await settle()
    expect(screen.queryByLabelText(COMPOSER_INPUT)).toBeNull()
  })

  it('anchors a comment on a text artifact', async () => {
    // text bodies render as a highlighted <pre> that carries previewRef, so a
    // selection there has a root to map back to source. Without that ref the
    // tip shows but the composer opens with a quote-only anchor — a dead affordance.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'text' }))
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    expect(screen.getByText(/select text to anchor a comment/i)).toBeInTheDocument()

    expect(selectInBody('beta')).toBe(true)
    fireEvent.change(await screen.findByLabelText(COMPOSER_INPUT), { target: { value: 'tighten this' } })
    fireEvent.click(screen.getByLabelText('Add comment'))

    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    const anchor = vi.mocked(api).postArtifactComment.mock.calls[0][1].anchor as { quote: string }
    expect(anchor.quote).toBe('beta')
  })

  it('anchors a comment on a selection made inside a widget iframe, by quote and context only', async () => {
    // The widget body is a sandboxed iframe the DOM toolbar cannot see into: its
    // bridge relays the selection, and the page hands it to the toolbar as an
    // external selection so the SAME composer opens. The frame's offsets are in
    // its own text space, so the anchor carries quote + prefix/suffix only.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget', content: '<p>widget body</p>' }))
    // The sandboxed frame is minted through the gateway; the automock resolves
    // it to `undefined`, which never yields an <iframe>.
    vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    const { container } = renderPage()
    const frame = await waitFor(() => {
      const node = container.querySelector('iframe')
      expect(node).not.toBeNull()
      return node as HTMLIFrameElement
    })
    // The bridge only trusts messages whose `source` is the frame's contentWindow.
    const source = { postMessage: vi.fn() }
    Object.defineProperty(frame, 'contentWindow', { value: source, configurable: true })
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-comment-select', quote: 'widget body', prefix: 'before ', suffix: ' after', startOffset: 7, endOffset: 18, rect: { x: 20, y: 40 } },
      source: source as unknown as Window,
    }))

    fireEvent.change(await screen.findByLabelText(COMPOSER_INPUT), { target: { value: 'from the frame' } })
    // A second drag-select inside the frame while the draft is open: the box
    // keeps its passage, and so must the anchor the comment is posted with.
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-comment-select', quote: 'other passage', prefix: 'x ', suffix: ' y', startOffset: 1, endOffset: 14, rect: { x: 20, y: 90 } },
      source: source as unknown as Window,
    }))
    fireEvent.click(screen.getByLabelText('Add comment'))
    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api).postArtifactComment.mock.calls[0][1].anchor).toEqual({ quote: 'widget body', prefix: 'before ', suffix: ' after' })
  })

  it('ignores a widget-iframe selection on a historical version', async () => {
    // A comment is stored against the CURRENT artifact; a snapshot's passage is
    // not annotatable, so the bridge's selection must not open the composer.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget', content: '<p>widget body</p>' }))
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget', version: 1, content: '<p>old widget body</p>' }))
    vi.mocked(api.sandboxDocUrl).mockResolvedValue({ url: '/sandbox-doc/test/tok' })
    const { container } = renderPage()
    await waitFor(() => expect(container.querySelector('iframe')).not.toBeNull())
    fireEvent.click(screen.getByRole('combobox', { name: /Version/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'v1' }))
    await waitFor(() => expect(screen.getByTitle(/revert to v1/i)).toBeInTheDocument())
    const frame = container.querySelector('iframe') as HTMLIFrameElement
    const source = { postMessage: vi.fn() }
    Object.defineProperty(frame, 'contentWindow', { value: source, configurable: true })
    window.dispatchEvent(new MessageEvent('message', {
      data: { type: 'mc-comment-select', quote: 'old widget body', prefix: '', suffix: '', startOffset: 0, endOffset: 15, rect: { x: 20, y: 40 } },
      source: source as unknown as Window,
    }))
    await settle()
    expect(screen.queryByLabelText(COMPOSER_INPUT)).toBeNull()
  })

  it('switching to Edit over a typed draft asks first; cancelling keeps the draft, confirming discards it', async () => {
    // Edit unmounts the toolbar and the box with it; the typed comment must not
    // vanish without a word.
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    expect(selectInBody('beta')).toBe(true)
    const input = await screen.findByLabelText(COMPOSER_INPUT)
    fireEvent.change(input, { target: { value: 'not yet added' } })

    fireEvent.click(screen.getByTitle(/edit content/i))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard your unsaved comment?')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(screen.getByLabelText(COMPOSER_INPUT)).toHaveValue('not yet added')

    fireEvent.click(screen.getByTitle(/edit content/i))
    const again = await screen.findByRole('dialog')
    fireEvent.click(within(again).getByRole('button', { name: 'Discard comment' }))
    await waitFor(() => expect(screen.queryByLabelText(COMPOSER_INPUT)).toBeNull())
    expect(vi.mocked(api).postArtifactComment).not.toHaveBeenCalled()
  })

  it('does not offer anchored add while editing', async () => {
    // Selecting inside a textarea is an edit gesture, not an annotation gesture.
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle(/edit content/i))
    await waitFor(() => expect(screen.getByText(/unsaved changes|save/i)).toBeInTheDocument())
    selectInBody('beta')
    await settle()
    expect(screen.queryByLabelText(COMPOSER_INPUT)).toBeNull()
  })

  it('does not offer anchored add on a historical version', async () => {
    // Anchors are version-scoped; pinning a note to a snapshot you cannot change
    // would produce an instruction the agent can never satisfy.
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    // The version picker is a Radix Select (SimpleSelect), so a `change` event on
    // the trigger does nothing — open it, then click the row.
    fireEvent.click(screen.getByRole('combobox', { name: /Version/i }))
    fireEvent.click(await screen.findByRole('option', { name: 'v1' }))
    await waitFor(() => expect(screen.getByTitle(/revert to v1/i)).toBeInTheDocument())
    selectInBody('beta')
    await settle()
    expect(screen.queryByLabelText(COMPOSER_INPUT)).toBeNull()
  })
})
