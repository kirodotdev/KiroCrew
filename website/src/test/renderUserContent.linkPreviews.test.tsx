// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { renderUserContent } from '../pages/ChatPage'
import { __resetLinkMetaForTests } from '../lib/linkMeta'

// Link previews in USER messages (issue #2580): the `linkPreviews` value must
// thread through renderUserContent's markdown path so a URL the user pasted
// gets the same unfurl treatment as one the model wrote. The setting stays
// strictly opt-in — no `link-meta` fetch may ever fire when it is off — and
// the paste-chip split path renders whitespace-preserving spans, not markdown,
// so previews deliberately do not apply there.

const noop = () => {}
const HREF = 'https://example.com/post'
const TITLE = 'Example Title'
const DESCRIPTION = 'A description of the page.'

const wire = () => ({
  url: HREF,
  title: TITLE,
  description: DESCRIPTION,
  site_name: 'Example',
  domain: 'example.com',
  icon: 'data:image/png;base64,AAAA',
  fetched_at: 1770000000,
})

const ok = (body: unknown) =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  __resetLinkMetaForTests()
  fetchMock = vi.fn(async () => ok(wire()))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('renderUserContent — link previews (issue #2580)', () => {
  it('unfurls a URL in a user message when linkPreviews is on', async () => {
    render(<>{renderUserContent({ content: HREF, meta: undefined, onFileOpen: noop, linkPreviews: true })}</>)
    // The card carries the fetched description — its arrival proves the
    // unfurl path ran end to end (gate open → link-meta fetch → card).
    await waitFor(() => expect(screen.queryByText(DESCRIPTION)).not.toBeNull())
    expect(fetchMock).toHaveBeenCalled()
  })

  it('unfurls a markdown link the user pasted when linkPreviews is on', async () => {
    render(<>{renderUserContent({ content: `[Docs](${HREF})`, meta: undefined, onFileOpen: noop, linkPreviews: true })}</>)
    await waitFor(() => expect(screen.queryByText(DESCRIPTION)).not.toBeNull())
    expect(fetchMock).toHaveBeenCalled()
  })

  it('issues NO fetch when linkPreviews is off (explicit false)', async () => {
    const { container } = render(<>{renderUserContent({ content: HREF, meta: undefined, onFileOpen: noop, linkPreviews: false })}</>)
    // The link renders as a plain anchor and nothing is requested.
    await waitFor(() => expect(container.querySelector('a')).not.toBeNull())
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByText(DESCRIPTION)).toBeNull()
  })

  it('issues NO fetch when linkPreviews is omitted (default off)', async () => {
    const { container } = render(<>{renderUserContent({ content: HREF, meta: undefined, onFileOpen: noop })}</>)
    await waitFor(() => expect(container.querySelector('a')).not.toBeNull())
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('leaves the paste-split path unaffected even with linkPreviews on', async () => {
    // A message that mixes a paste chip with a URL routes its text segments
    // through renderInlineSegment (whitespace-preserving spans, no markdown),
    // so the URL stays literal text: no anchor, no card, no fetch.
    const block = { id: 'b1', seq: 1, lines: 4, content: 'a\nb\nc\nd' }
    const content = `see [ Paste #1 · 4 lines ]\n${HREF}`
    const { container } = render(
      <>{renderUserContent({ content: content, meta: { pastes: [block] }, onFileOpen: noop, linkPreviews: true })}</>,
    )
    // The chip rendered (paste path taken)…
    expect(container.textContent).toContain('Paste #1')
    // …and the URL was NOT unfurled or linkified.
    expect(fetchMock).not.toHaveBeenCalled()
    expect(screen.queryByText(DESCRIPTION)).toBeNull()
    expect(container.querySelector(`a[href="${HREF}"]`)).toBeNull()
  })
})
