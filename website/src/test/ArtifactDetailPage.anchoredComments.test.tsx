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
 * jsdom has no real text selection, so `window.getSelection` is backed by a real
 * `Range` over the rendered body — the same object the production code reads.
 * The suite uses a MARKDOWN artifact because that is the kind that renders to a
 * DOM tree behind `previewRef`; text/json/svg render as a highlighted <pre> with
 * no preview ref, so a DOM selection there has nothing to map back to source.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
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
  // jsdom Range has no layout; the page only reads the rect for popover placement.
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
    toString: () => word,
  } as unknown as Selection)

  // The page attaches mouseup to the body wrapper; the rendered text node's
  // element ancestor is inside it, so the event bubbles up to the handler.
  const host = node.parentElement as HTMLElement
  fireEvent.mouseDown(host)
  fireEvent.mouseUp(host)
  return true
}

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
    const input = await screen.findByLabelText('Add a comment')
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
    fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'note' } })
    fireEvent.click(screen.getByLabelText('Add comment'))
    await waitFor(() =>
      expect(screen.getByLabelText('Toggle comments')).toHaveAttribute('aria-pressed', 'true'))
  })

  it('does not open the popover for a collapsed (empty) selection', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    vi.spyOn(window, 'getSelection').mockReturnValue({
      isCollapsed: true, rangeCount: 0, toString: () => '', removeAllRanges: () => undefined,
    } as unknown as Selection)
    fireEvent.mouseUp(document.body)
    expect(screen.queryByLabelText('Add a comment')).toBeNull()
  })

  it('anchors a comment on a text artifact', async () => {
    // text bodies render as a highlighted <pre> that carries previewRef, so a
    // selection there has a root to map back to source. Without that ref the
    // tip shows but the popover cannot open — a dead affordance.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'text' }))
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    expect(screen.getByText(/select text to anchor a comment/i)).toBeInTheDocument()

    expect(selectInBody('beta')).toBe(true)
    fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'tighten this' } })
    fireEvent.click(screen.getByLabelText('Add comment'))

    await waitFor(() => expect(vi.mocked(api).postArtifactComment).toHaveBeenCalledTimes(1))
    const anchor = vi.mocked(api).postArtifactComment.mock.calls[0][1].anchor as { quote: string }
    expect(anchor.quote).toBe('beta')
  })

  it('does not offer anchored add while editing', async () => {
    // Selecting inside a textarea is an edit gesture, not an annotation gesture.
    renderPage()
    await waitFor(() => expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle(/edit content/i))
    await waitFor(() => expect(screen.getByText(/unsaved changes|save/i)).toBeInTheDocument())
    selectInBody('beta')
    expect(screen.queryByLabelText('Add a comment')).toBeNull()
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
    expect(screen.queryByLabelText('Add a comment')).toBeNull()
  })
})
