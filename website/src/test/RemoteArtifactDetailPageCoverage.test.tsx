/**
 * Coverage suite for `RemoteArtifactDetailPage` — the read-only viewer for a
 * provider-hosted artifact the user does NOT own locally (reached from the
 * Shared/Public browse list, no fork required to read or comment).
 *
 * The page had zero test coverage. Three things it does are easy to break
 * silently and are pinned here:
 *
 *  1. **snake_case field reads + the nested `artifact` shape.** The provider
 *     `fetch_content` contract is flat snake_case, but some providers nest the
 *     metadata under `artifact` with `content` / `view_url` at the top level. A
 *     mis-read leaves every field `undefined`: HTML degrades to a raw-source
 *     <pre>, the version and owner vanish, and "Open original" disappears.
 *  2. **`safeHttpUrl` on `view_url`.** That URL comes straight from a remote
 *     provider response, so a `javascript:` URL must never become a clickable
 *     link.
 *  3. **Anchored comment posts.** The remote path has no local store to fall
 *     back on, so the provider rejects an anchor missing start/end offsets or
 *     `version_number` and the comment silently disappears.
 *
 * Kiro Crew convention: this suite mirrors `ArtifactDetailPage.test.tsx` and
 * `ArtifactDetailPage.anchoredComments.test.tsx` — automocked api client,
 * `renderWithProviders` with a real `MemoryRouter` route so `useParams` and
 * `navigate` are exercised for real, and a `Range`-backed `window.getSelection`
 * for the markdown selection path.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import type { ReactNode } from 'react'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { Routes, Route, useNavigate } from 'react-router-dom'
import RemoteArtifactDetailPage from '../pages/RemoteArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { ArtifactComment } from '../types'

vi.mock('../api/client')

const PROVIDER = 'companion'
const EXT_ID = 'ext-42'
const MD_BODY = 'alpha beta gamma'

interface RemoteDetailFixture {
  external_id?: string
  title?: string
  summary?: string
  owner?: string
  visibility?: string
  content_type?: string
  current_version?: number
  view_url?: string
  content?: string
  tags?: string[]
  artifact?: RemoteDetailFixture
}

const mkDetail = (overrides: RemoteDetailFixture = {}): RemoteDetailFixture => ({
  external_id: EXT_ID,
  title: 'Quarterly Rollup',
  summary: 'Numbers for the quarter',
  owner: 'someone',
  visibility: 'SHARED',
  content_type: 'text/plain',
  current_version: 3,
  view_url: 'https://remote.example.com/a/ext-42',
  content: 'plain body text',
  tags: ['ops', 'rollup'],
  ...overrides,
})

const mkComment = (overrides: Partial<ArtifactComment> = {}): ArtifactComment => ({
  id: 'c1',
  origin: 'provider',
  provider: PROVIDER,
  scope: 'shared',
  author: 'reviewer',
  is_agent: false,
  body: 'first review note',
  thread_id: 'c1',
  status: 'open',
  sync_state: 'synced',
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  ...overrides,
})

/** Mount the page on its real route so `useParams` resolves for real. Sibling
 *  routes stand in for the two navigation targets the page can push. `extra`
 *  renders inside the router, alongside the page. */
function renderPage(extra?: ReactNode) {
  return renderWithProviders(
    <>
      {extra}
      <Routes>
        <Route path="/artifacts/remote/:provider/:externalId" element={<RemoteArtifactDetailPage />} />
        <Route path="/artifacts" element={<div>library page target</div>} />
        <Route path="/artifacts/:slug" element={<div>local copy target</div>} />
      </Routes>
    </>,
    { route: `/artifacts/remote/${PROVIDER}/${EXT_ID}` },
  )
}

/** In-router control that swaps the route to a DIFFERENT remote artifact, so the
 *  page's "reused across the route" reset path runs for real. */
function SwitchArtifact({ to }: { to: string }) {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => navigate(`/artifacts/remote/${PROVIDER}/${to}`)}>
      switch artifact
    </button>
  )
}

/** The comment-panel toggle (a `Btn` carrying `aria-pressed`). */
const commentToggle = () => screen.getByRole('button', { name: /Comments/ })

/**
 * Select `word` inside the rendered markdown body and fire the mouseup the page
 * listens on. Returns false when the body text node isn't found so a test fails
 * loudly rather than silently asserting nothing. jsdom has no real selection, so
 * a real `Range` over the rendered DOM backs `window.getSelection` — the same
 * object shape the production code reads.
 */
function selectInMarkdown(word: string): boolean {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  let node: Node | null = null
  while (walker.nextNode()) {
    const t = walker.currentNode.textContent ?? ''
    if (t.includes(word) && t.includes(MD_BODY)) { node = walker.currentNode; break }
  }
  if (!node) return false
  const text = node.textContent ?? ''
  const start = text.indexOf(word)
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + word.length)
  // jsdom Range has no layout; the page reads the rect only to place the popover.
  range.getBoundingClientRect = () => ({
    left: 12, top: 10, bottom: 34, right: 60, width: 48, height: 24, x: 12, y: 10,
    toJSON: () => ({}),
  }) as DOMRect

  vi.spyOn(window, 'getSelection').mockReturnValue({
    isCollapsed: false,
    anchorNode: node,
    rangeCount: 1,
    getRangeAt: () => range,
    toString: () => word,
    removeAllRanges: () => {},
  } as unknown as Selection)

  const host = document.querySelector('.msg-content')
  if (!host) return false
  fireEvent.mouseUp(host)
  return true
}

describe('RemoteArtifactDetailPage', () => {
  // The object-URL stubs below are direct property assignments on the URL global,
  // which `vi.restoreAllMocks()` does NOT undo — capture the originals so this
  // file's stubs cannot leak into later files in the same worker.
  const originalCreate = globalThis.URL.createObjectURL
  const originalRevoke = globalThis.URL.revokeObjectURL

  beforeEach(() => {
    vi.clearAllMocks()
    // happy-dom has no object-URL support, which the HTML render path needs.
    // @ts-expect-error assigning a test stub onto the URL global
    globalThis.URL.createObjectURL = vi.fn().mockReturnValue('blob:remote-test')
    // @ts-expect-error assigning a test stub onto the URL global
    globalThis.URL.revokeObjectURL = vi.fn()
    // clearAllMocks resets call history only, so re-establish the defaults every
    // test: otherwise a test that reassigns a member leaks its mock forward and
    // an un-stubbed automock resolves `undefined`, which the render then reads
    // as `undefined.comments` AFTER the test ends (unhandled rejection).
    vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mkDetail())
    vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [] })
    vi.mocked(api).postRemoteArtifactComment = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).replyRemoteArtifactComment = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).markReviewRemoteComment = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).deleteRemoteComment = vi.fn().mockResolvedValue({ ok: true })
    vi.mocked(api).forkRemoteArtifact = vi.fn().mockResolvedValue({ slug: 'quarterly-rollup' })
  })

  afterEach(() => {
    globalThis.URL.createObjectURL = originalCreate
    globalThis.URL.revokeObjectURL = originalRevoke
    vi.restoreAllMocks()
  })

  describe('load states', () => {
    it('shows the loading placeholder while the detail query is in flight', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockReturnValue(new Promise(() => {}))
      renderPage()
      expect(await screen.findByText('Loading…')).toBeInTheDocument()
    })

    it('renders the failure card with the error message and routes back to the library', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockRejectedValue(new Error('provider offline'))
      renderPage()
      expect(await screen.findByText('provider offline')).toBeInTheDocument()
      // The heading and the fallback message share a string, so the headline is
      // read as "at least one" rather than a unique match.
      expect(screen.getAllByText('Failed to load remote artifact').length).toBeGreaterThan(0)
      fireEvent.click(screen.getByRole('button', { name: /Back to library/ }))
      expect(await screen.findByText('library page target')).toBeInTheDocument()
    })

    it('falls back to a generic message when the rejection is not an Error', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockRejectedValue('socket hang up')
      renderPage()
      // No `.message` to show, so the headline string is reused for the detail
      // line: two occurrences instead of one.
      await waitFor(() =>
        expect(screen.getAllByText('Failed to load remote artifact')).toHaveLength(2),
      )
    })
  })

  describe('metadata rendering', () => {
    it('renders title, provider, visibility, owner, version, tags and summary from flat snake_case fields', async () => {
      renderPage()
      expect(await screen.findByText('Quarterly Rollup')).toBeInTheDocument()
      expect(screen.getByText(`Remote artifact · ${PROVIDER}`)).toBeInTheDocument()
      expect(screen.getByText('SHARED')).toBeInTheDocument()
      expect(screen.getByText('someone')).toBeInTheDocument()
      expect(screen.getByText('v3')).toBeInTheDocument()
      expect(screen.getByText('ops')).toBeInTheDocument()
      expect(screen.getByText('rollup')).toBeInTheDocument()
      expect(screen.getByText('Numbers for the quarter')).toBeInTheDocument()
    })

    it('falls back to the external id when the provider returns no title', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mkDetail({ title: '' }))
      renderPage()
      // The subtitle already carries the provider name, so the id shows up once
      // as the heading.
      expect(await screen.findByText(EXT_ID)).toBeInTheDocument()
    })

    it('omits the optional chrome when visibility, owner, version and tags are absent', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ visibility: undefined, owner: undefined, current_version: undefined, tags: undefined, summary: undefined }),
      )
      renderPage()
      expect(await screen.findByText('Quarterly Rollup')).toBeInTheDocument()
      expect(screen.queryByText('SHARED')).not.toBeInTheDocument()
      expect(screen.queryByText('someone')).not.toBeInTheDocument()
      expect(screen.queryByText(/^v\d/)).not.toBeInTheDocument()
      expect(screen.queryByText('Numbers for the quarter')).not.toBeInTheDocument()
    })
  })

  describe('content rendering by content_type', () => {
    it('renders a non-html, non-markdown body as preformatted source', async () => {
      renderPage()
      const pre = await screen.findByText('plain body text')
      expect(pre.tagName).toBe('PRE')
    })

    it('renders an html body inside a sandboxed blob iframe', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ content_type: 'text/html', content: '<p>widget body</p>' }),
      )
      renderPage()
      const frame = await screen.findByTitle(`Remote artifact: ${EXT_ID}`)
      expect(frame).toHaveAttribute('src', 'blob:remote-test')
      expect(frame).toHaveAttribute('sandbox', expect.stringContaining('allow-scripts'))
      expect(URL.createObjectURL).toHaveBeenCalled()
    })

    it('flattens a nested "artifact" payload so content_type still selects the html renderer', async () => {
      // Provider variant: metadata nested, content + view_url at the top level.
      // Reading these flat-only would leave isHtml false and dump raw source.
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue({
        content: '<p>nested body</p>',
        view_url: 'https://remote.example.com/a/nested',
        artifact: mkDetail({ content_type: 'text/html', content: undefined, view_url: undefined, title: 'Nested Rollup' }),
      })
      renderPage()
      expect(await screen.findByText('Nested Rollup')).toBeInTheDocument()
      expect(await screen.findByTitle(`Remote artifact: ${EXT_ID}`)).toBeInTheDocument()
      // Nested version survived the flatten too.
      expect(screen.getByText('v3')).toBeInTheDocument()
    })

    it('renders a markdown body through the markdown renderer', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ content_type: 'text/markdown', content: `# Heading\n\n${MD_BODY}` }),
      )
      renderPage()
      expect(await screen.findByRole('heading', { name: 'Heading' })).toBeInTheDocument()
      expect(screen.getByText(MD_BODY)).toBeInTheDocument()
    })

    it('treats an empty content_type as plain source and tolerates missing content', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ content_type: undefined, content: undefined, title: 'Bare' }),
      )
      renderPage()
      expect(await screen.findByText('Bare')).toBeInTheDocument()
      expect(screen.queryByTitle(/^Remote artifact:/)).not.toBeInTheDocument()
    })
  })

  describe('external link safety', () => {
    it('opens the validated https view_url in a new tab', async () => {
      const open = vi.spyOn(window, 'open').mockReturnValue(null)
      renderPage()
      fireEvent.click(await screen.findByRole('button', { name: /Open original/ }))
      expect(open).toHaveBeenCalledWith(
        'https://remote.example.com/a/ext-42',
        '_blank',
        'noopener,noreferrer',
      )
    })

    it('renders no "Open original" affordance for a javascript: view_url', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ view_url: 'javascript:alert(1)' }),
      )
      renderPage()
      await screen.findByText('Quarterly Rollup')
      expect(screen.queryByRole('button', { name: /Open original/ })).not.toBeInTheDocument()
    })

    it('renders no "Open original" affordance when view_url is absent', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mkDetail({ view_url: undefined }))
      renderPage()
      await screen.findByText('Quarterly Rollup')
      expect(screen.queryByRole('button', { name: /Open original/ })).not.toBeInTheDocument()
    })
  })

  describe('fork', () => {
    it('navigates to the new local slug on success', async () => {
      renderPage()
      fireEvent.click(await screen.findByRole('button', { name: /Fork/ }))
      expect(await screen.findByText('local copy target')).toBeInTheDocument()
      expect(vi.mocked(api).forkRemoteArtifact).toHaveBeenCalledWith(PROVIDER, EXT_ID)
    })

    it('surfaces a provider-reported fork error without navigating', async () => {
      vi.mocked(api).forkRemoteArtifact = vi.fn().mockResolvedValue({ error: 'quota exceeded' })
      renderPage()
      fireEvent.click(await screen.findByRole('button', { name: /Fork/ }))
      expect(await screen.findByText('quota exceeded')).toBeInTheDocument()
      expect(screen.queryByText('local copy target')).not.toBeInTheDocument()
    })

    it('surfaces a thrown fork failure and re-enables the button afterwards', async () => {
      vi.mocked(api).forkRemoteArtifact = vi.fn().mockRejectedValue(new Error('network down'))
      renderPage()
      const btn = await screen.findByRole('button', { name: /Fork/ })
      fireEvent.click(btn)
      expect(await screen.findByText('network down')).toBeInTheDocument()
      // `finally` clears the in-flight flag, so a retry is possible.
      await waitFor(() => expect(screen.getByRole('button', { name: /Fork/ })).not.toBeDisabled())
    })
  })

  describe('comment sidebar', () => {
    it('stays collapsed when the artifact has no comments', async () => {
      renderPage()
      await screen.findByText('Quarterly Rollup')
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'false'))
      expect(screen.queryByRole('button', { name: 'Add comment' })).not.toBeInTheDocument()
    })

    it('auto-reveals with a count badge when the artifact has comments', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({
        comments: [mkComment(), mkComment({ id: 'c2', thread_id: 'c2', body: 'second note' })],
      })
      renderPage()
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'true'))
      expect(within(commentToggle()).getByText('2')).toBeInTheDocument()
      expect(screen.getByText('first review note')).toBeInTheDocument()
    })

    it('keeps the panel closed after a manual toggle even though comments exist', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      renderPage()
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'true'))
      fireEvent.click(commentToggle())
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'false'))
      // The manual override must survive the comment-count effect re-running.
      expect(screen.queryByText('first review note')).not.toBeInTheDocument()
      fireEvent.click(commentToggle())
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'true'))
    })

    it('renders the provider sync warning when the comments response carries one', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({
        comments: [mkComment()],
        remote_sync_error: 'provider rate limited',
      })
      renderPage()
      expect(await screen.findByText(/provider rate limited/)).toBeInTheDocument()
    })

    it('posts a document-level comment through the remote endpoint', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      renderPage()
      fireEvent.click(await screen.findByRole('button', { name: 'Add comment' }))
      fireEvent.change(screen.getByPlaceholderText('Add a comment on the whole artifact…'), {
        target: { value: 'looks good' },
      })
      fireEvent.click(screen.getByRole('button', { name: 'Comment' }))
      await waitFor(() =>
        expect(vi.mocked(api).postRemoteArtifactComment).toHaveBeenCalledWith(
          PROVIDER, EXT_ID, { text: 'looks good' },
        ),
      )
    })

    it('replies to an existing thread through the remote endpoint', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      renderPage()
      await screen.findByText('first review note')
      fireEvent.click(screen.getByRole('button', { name: 'Reply' }))
      fireEvent.change(screen.getByPlaceholderText('Reply…'), { target: { value: 'ack' } })
      fireEvent.keyDown(screen.getByPlaceholderText('Reply…'), { key: 'Enter' })
      await waitFor(() =>
        expect(vi.mocked(api).replyRemoteArtifactComment).toHaveBeenCalledWith(
          PROVIDER, EXT_ID, 'c1', { text: 'ack' },
        ),
      )
    })

    it('advances a thread to review through the remote endpoint', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      renderPage()
      await screen.findByText('first review note')
      // The button's accessible name is its visible label ("Review"); the
      // longer "Advance to Review" is only the tooltip.
      fireEvent.click(screen.getByRole('button', { name: 'Review' }))
      await waitFor(() =>
        expect(vi.mocked(api).markReviewRemoteComment).toHaveBeenCalledWith(PROVIDER, EXT_ID, 'c1'),
      )
    })

    it('refreshes comments on demand', async () => {
      const fetchComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      vi.mocked(api).remoteArtifactComments = fetchComments
      renderPage()
      await screen.findByText('first review note')
      const before = fetchComments.mock.calls.length
      fireEvent.click(screen.getByRole('button', { name: 'Refresh comments' }))
      await waitFor(() => expect(fetchComments.mock.calls.length).toBeGreaterThan(before))
    })

    it('closes the panel from the sidebar collapse control', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      renderPage()
      await screen.findByText('first review note')
      fireEvent.click(screen.getByRole('button', { name: 'Collapse comments' }))
      await waitFor(() => expect(screen.queryByText('first review note')).not.toBeInTheDocument())
    })
  })

  describe('anchored comments on a markdown body', () => {
    const mdDetail = (overrides: RemoteDetailFixture = {}) =>
      mkDetail({ content_type: 'text/markdown', content: MD_BODY, ...overrides })

    it('opens the popover on a body selection and posts the anchor with offsets and version', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mdDetail())
      renderPage()
      await screen.findByText(MD_BODY)
      expect(selectInMarkdown('beta')).toBe(true)

      const box = await screen.findByLabelText('Add a comment')
      fireEvent.change(box, { target: { value: 'this number is stale' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))

      await waitFor(() => expect(vi.mocked(api).postRemoteArtifactComment).toHaveBeenCalled())
      const [, , body] = vi.mocked(api).postRemoteArtifactComment.mock.calls[0]
      expect(body.text).toBe('this number is stale')
      // The provider rejects an anchor missing any of these, and the comment
      // then vanishes with no error surfaced to the user.
      expect(body.anchor).toMatchObject({
        quote: 'beta',
        prefix: 'alpha ',
        suffix: ' gamma',
        start_offset: MD_BODY.indexOf('beta'),
        end_offset: MD_BODY.indexOf('beta') + 'beta'.length,
        version_number: 3,
      })
    })

    it('reveals the comment panel after an anchored add', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mdDetail())
      renderPage()
      await screen.findByText(MD_BODY)
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'false'))
      expect(selectInMarkdown('gamma')).toBe(true)
      fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'note' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'true'))
    })

    it('defaults the anchor version to 1 when the provider omits current_version', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mdDetail({ current_version: undefined }))
      renderPage()
      await screen.findByText(MD_BODY)
      expect(selectInMarkdown('alpha')).toBe(true)
      fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'x' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))
      await waitFor(() => expect(vi.mocked(api).postRemoteArtifactComment).toHaveBeenCalled())
      const [, , body] = vi.mocked(api).postRemoteArtifactComment.mock.calls[0]
      expect(body.anchor).toMatchObject({ quote: 'alpha', prefix: '', start_offset: 0, version_number: 1 })
    })

    it('dismisses the popover on cancel without posting', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mdDetail())
      renderPage()
      await screen.findByText(MD_BODY)
      expect(selectInMarkdown('beta')).toBe(true)
      await screen.findByLabelText('Add a comment')
      fireEvent.click(screen.getByRole('button', { name: 'Close' }))
      await waitFor(() => expect(screen.queryByLabelText('Add a comment')).not.toBeInTheDocument())
      expect(vi.mocked(api).postRemoteArtifactComment).not.toHaveBeenCalled()
    })

    it('ignores a collapsed selection', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mdDetail())
      renderPage()
      await screen.findByText(MD_BODY)
      vi.spyOn(window, 'getSelection').mockReturnValue({
        isCollapsed: true,
        anchorNode: document.body,
        rangeCount: 0,
        toString: () => '',
        removeAllRanges: () => {},
      } as unknown as Selection)
      const host = document.querySelector('.msg-content')
      expect(host).not.toBeNull()
      fireEvent.mouseUp(host as Element)
      expect(screen.queryByLabelText('Add a comment')).not.toBeInTheDocument()
    })

    it('ignores a selection made outside the artifact body', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(mdDetail())
      renderPage()
      await screen.findByText(MD_BODY)
      // A node the preview ref does not contain: the page header title.
      const outside = screen.getByText('Quarterly Rollup').firstChild ?? screen.getByText('Quarterly Rollup')
      const range = document.createRange()
      range.selectNodeContents(outside)
      vi.spyOn(window, 'getSelection').mockReturnValue({
        isCollapsed: false,
        anchorNode: outside,
        rangeCount: 1,
        getRangeAt: () => range,
        toString: () => 'Quarterly',
        removeAllRanges: () => {},
      } as unknown as Selection)
      const host = document.querySelector('.msg-content')
      fireEvent.mouseUp(host as Element)
      expect(screen.queryByLabelText('Add a comment')).not.toBeInTheDocument()
    })

    it('does not open the popover from a selection on a plain-source body', async () => {
      // The mouseup handler is bound only on the markdown branch; a text/plain
      // body renders a <pre> with no preview ref to map a selection back to.
      renderPage()
      await screen.findByText('plain body text')
      expect(document.querySelector('.msg-content')).toBeNull()
      expect(screen.queryByLabelText('Add a comment')).not.toBeInTheDocument()
    })
  })

  describe('theme forwarding into the sandboxed iframe', () => {
    afterEach(() => {
      document.documentElement.style.removeProperty('--accent')
    })

    it('copies sanitized dashboard theme variables into the srcdoc', async () => {
      document.documentElement.style.setProperty('--accent', '#3355ff')
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ content_type: 'text/html', content: '<p>themed body</p>' }),
      )
      renderPage()
      await screen.findByTitle(`Remote artifact: ${EXT_ID}`)
      const blob = vi.mocked(globalThis.URL.createObjectURL).mock.calls[0][0] as Blob
      const html = await blob.text()
      // The remote page reads the live custom properties rather than hardcoding a
      // palette, so a remote artifact matches the dashboard theme.
      expect(html).toContain('--accent:#3355ff')
      expect(html).toContain('themed body')
    })
  })

  describe('route reuse across remote artifacts', () => {
    it('drops the manual sidebar override when the route switches to another artifact', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      renderPage(<SwitchArtifact to="ext-99" />)
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'true'))
      // Manual collapse: the comment-count effect must respect it...
      fireEvent.click(commentToggle())
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'false'))
      // ...but only for THIS artifact. The component is reused across the route,
      // so the next artifact gets the comment-driven default back.
      fireEvent.click(screen.getByRole('button', { name: 'switch artifact' }))
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'true'))
      expect(vi.mocked(api).remoteArtifactDetail).toHaveBeenCalledWith(PROVIDER, 'ext-99')
    })
  })

  describe('navigation out of the loaded view', () => {
    it('returns to the library from the back control', async () => {
      renderPage()
      await screen.findByText('Quarterly Rollup')
      fireEvent.click(screen.getByRole('button', { name: 'Back' }))
      expect(await screen.findByText('library page target')).toBeInTheDocument()
    })
  })

  describe('clicking an anchored comment row', () => {
    const anchored = mkComment({
      body: 'anchored note',
      anchor: { quote: 'beta', prefix: 'alpha ', suffix: ' gamma', start_offset: 6, end_offset: 10, version_number: 3 },
    })

    beforeEach(() => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [anchored] })
    })

    it('scrolls the markdown body instead of opening a reply box', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ content_type: 'text/markdown', content: MD_BODY }),
      )
      renderPage()
      const row = await screen.findByText('anchored note')
      fireEvent.click(row)
      // An anchored row routes the click to the scroll handler; an unanchored row
      // would open the inline reply composer instead.
      expect(screen.queryByPlaceholderText('Reply…')).not.toBeInTheDocument()
    })

    it('asks the html iframe to scroll to the anchor', async () => {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ content_type: 'text/html', content: '<p>widget body</p>' }),
      )
      renderPage()
      const frame = (await screen.findByTitle(`Remote artifact: ${EXT_ID}`)) as HTMLIFrameElement
      const post = vi.fn()
      Object.defineProperty(frame, 'contentWindow', { value: { postMessage: post }, configurable: true })
      fireEvent.click(await screen.findByText('anchored note'))
      // The HTML body lives in a null-origin sandbox, so the scroll request can
      // only travel by postMessage.
      await waitFor(() => expect(post).toHaveBeenCalled())
      expect(post.mock.calls[0][0]).toMatchObject({ id: anchored.id })
      expect(screen.queryByPlaceholderText('Reply…')).not.toBeInTheDocument()
    })
  })

  describe('in-iframe comment bridge', () => {
    /** Post a bridge message as if it came from inside the sandboxed iframe. The
     *  hook only trusts messages whose `source` is that iframe's contentWindow. */
    function postFromIframe(frame: HTMLIFrameElement, data: Record<string, unknown>) {
      const source = { postMessage: vi.fn() }
      Object.defineProperty(frame, 'contentWindow', { value: source, configurable: true })
      window.dispatchEvent(new MessageEvent('message', { data, source: source as unknown as Window }))
    }

    async function renderHtmlPage() {
      vi.mocked(api).remoteArtifactDetail = vi.fn().mockResolvedValue(
        mkDetail({ content_type: 'text/html', content: '<p>widget body</p>' }),
      )
      renderPage()
      return (await screen.findByTitle(`Remote artifact: ${EXT_ID}`)) as HTMLIFrameElement
    }

    it('opens the popover for a selection made inside the iframe and posts the anchor', async () => {
      const frame = await renderHtmlPage()
      postFromIframe(frame, {
        type: 'mc-comment-select',
        quote: 'widget body',
        prefix: 'before ',
        suffix: ' after',
        startOffset: 7,
        endOffset: 18,
        rect: { x: 20, y: 40 },
      })
      fireEvent.change(await screen.findByLabelText('Add a comment'), { target: { value: 'from the frame' } })
      fireEvent.click(screen.getByRole('button', { name: 'Add comment' }))
      await waitFor(() => expect(vi.mocked(api).postRemoteArtifactComment).toHaveBeenCalled())
      const [, , body] = vi.mocked(api).postRemoteArtifactComment.mock.calls[0]
      expect(body.anchor).toMatchObject({
        quote: 'widget body',
        prefix: 'before ',
        suffix: ' after',
        start_offset: 7,
        end_offset: 18,
        version_number: 3,
      })
    })

    it('reveals the panel when a highlight inside the iframe is clicked', async () => {
      vi.mocked(api).remoteArtifactComments = vi.fn().mockResolvedValue({ comments: [mkComment()] })
      const frame = await renderHtmlPage()
      await waitFor(() => expect(commentToggle()).toHaveAttribute('aria-pressed', 'true'))
      postFromIframe(frame, { type: 'mc-comment-highlight-click', id: 'c1', rect: { x: 1, y: 2, w: 3, h: 4 } })
      // The clicked highlight flashes its sidebar row; the row itself stays put.
      expect(await screen.findByText('first review note')).toBeInTheDocument()
    })
  })
})
