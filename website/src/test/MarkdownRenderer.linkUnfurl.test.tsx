// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import MarkdownRenderer, { LinkOverrideCtx, unfurlableHref } from '../components/MarkdownRenderer'
import { __resetLinkMetaForTests } from '../lib/linkMeta'

// Link unfurl wiring: a link the model emitted renders as favicon + page title
// instead of a raw URL — a CHIP when it sits inside a sentence, a block CARD
// when it is the whole paragraph. The feature is opt-in (`linkPreviews`), never
// fetches while the block is still streaming, and always yields to a
// LinkOverrideCtx provider (Issue Radar's in-app issue/PR references).

const HREF = 'https://example.com/post'
const TITLE = 'Example Title'
const DESCRIPTION = 'A description of the page.'

const wire = (over: Record<string, unknown> = {}) => ({
  url: HREF,
  title: TITLE,
  description: DESCRIPTION,
  site_name: 'Example',
  domain: 'example.com',
  icon: 'data:image/png;base64,AAAA',
  fetched_at: 1770000000,
  ...over,
})

const ok = (body: unknown) =>
  ({ ok: true, status: 200, json: async () => body }) as unknown as Response

const err = (status: number, code: string) =>
  ({ ok: false, status, json: async () => ({ code }) }) as unknown as Response

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

/** The description only exists on the CARD, so its presence/absence is what
 *  distinguishes the two forms without asserting on Tailwind classes. */
const cardShown = () => screen.queryByText(DESCRIPTION) !== null

describe('unfurlableHref', () => {
  it('accepts an absolute http(s) URL', () => {
    expect(unfurlableHref(HREF)).toBe(HREF)
    expect(unfurlableHref('http://example.com/')).toBe('http://example.com/')
  })

  it('rejects non-http(s) schemes and relative hrefs', () => {
    expect(unfurlableHref('mailto:a@b.co')).toBeNull()
    expect(unfurlableHref('vscode://file/tmp/x')).toBeNull()
    expect(unfurlableHref('/docs/page')).toBeNull()
    expect(unfurlableHref(undefined)).toBeNull()
    expect(unfurlableHref('')).toBeNull()
  })

  it('rejects credential-bearing URLs', () => {
    expect(unfurlableHref('https://user:pass@example.com/x')).toBeNull()
  })

  it('rejects in-app routes: artifacts and anything same-origin', () => {
    expect(unfurlableHref('/artifacts/my-report')).toBeNull()
    expect(unfurlableHref(`${window.location.origin}/artifacts/my-report`)).toBeNull()
    expect(unfurlableHref(`${window.location.origin}/chat`)).toBeNull()
  })
})

describe('MarkdownRenderer link unfurl — card vs chip', () => {
  it('renders a standalone-link paragraph as a card, replacing the <p>', async () => {
    const { container } = render(<MarkdownRenderer content={HREF} linkPreviews />)
    await waitFor(() => expect(cardShown()).toBe(true))
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', HREF)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    // The card replaces the paragraph rather than nesting inside it.
    expect(container.querySelector('p')).toBeNull()
    expect(screen.getByText('example.com')).toBeTruthy()
  })

  it('renders a markdown link that is the whole paragraph as a card too', async () => {
    render(<MarkdownRenderer content={`[Docs](${HREF})`} linkPreviews />)
    await waitFor(() => expect(cardShown()).toBe(true))
  })

  it('renders a link inside a sentence as an inline chip, keeping the paragraph', async () => {
    const { container } = render(
      <MarkdownRenderer content={`Read the [docs](${HREF}) carefully.`} linkPreviews />
    )
    await waitFor(() => expect(screen.getByRole('link', { name: TITLE })).toBeTruthy())
    expect(container.querySelector('p')).not.toBeNull()
    // Chip form: page title only, no description/domain block.
    expect(cardShown()).toBe(false)
    expect(container.textContent).toContain('Read the ')
    expect(container.textContent).toContain(' carefully.')
  })

  it('treats a paragraph with two links as prose (chips, not a card)', async () => {
    render(<MarkdownRenderer content={`[a](${HREF}) [b](${HREF})`} linkPreviews />)
    await waitFor(() => expect(screen.getAllByRole('link')).toHaveLength(2))
    expect(cardShown()).toBe(false)
  })

  it('fetches one URL once even when the same link appears twice', async () => {
    render(<MarkdownRenderer content={`See [a](${HREF}) and [b](${HREF}).`} linkPreviews />)
    await waitFor(() => expect(screen.getAllByRole('link', { name: TITLE })).toHaveLength(2))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('MarkdownRenderer link unfurl — gates', () => {
  it('fetches nothing while the block is still streaming', async () => {
    // A half-typed URL in the streaming tail must never reach the backend.
    const { container } = render(
      <MarkdownRenderer content={'Here is https://exa'} linkPreviews streaming />
    )
    await waitFor(() => expect(container.textContent).toContain('https://exa'))
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('fetches nothing while streaming even for an already-complete URL', async () => {
    render(<MarkdownRenderer content={HREF} linkPreviews streaming />)
    await waitFor(() => expect(screen.getByRole('link')).toHaveAttribute('href', HREF))
    expect(fetchMock).not.toHaveBeenCalled()
    expect(cardShown()).toBe(false)
  })

  it('unfurls the same content once streaming ends', async () => {
    const { rerender } = render(<MarkdownRenderer content={HREF} linkPreviews streaming />)
    expect(fetchMock).not.toHaveBeenCalled()
    rerender(<MarkdownRenderer content={HREF} linkPreviews streaming={false} />)
    await waitFor(() => expect(cardShown()).toBe(true))
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('fetches nothing when the feature is disabled (the default)', async () => {
    const { container } = render(<MarkdownRenderer content={`Read the [docs](${HREF}).`} />)
    await waitFor(() => expect(screen.getByRole('link', { name: 'docs' })).toBeTruthy())
    expect(fetchMock).not.toHaveBeenCalled()
    expect(cardShown()).toBe(false)
    expect(container.querySelector('p')).not.toBeNull()
  })

  it('fetches nothing in sourcePos mode, so data-sourcepos anchors survive', async () => {
    // A card replaces the <p> that carries data-sourcepos, which the inline
    // commenting flow needs to map a selection back to source coordinates.
    const { container } = render(<MarkdownRenderer content={HREF} linkPreviews sourcePos />)
    await waitFor(() => expect(screen.getByRole('link')).toHaveAttribute('href', HREF))
    expect(fetchMock).not.toHaveBeenCalled()
    expect(cardShown()).toBe(false)
    expect(container.querySelector('p[data-sourcepos]')).not.toBeNull()
  })

  it('never fetches a non-http(s) or in-app link even when enabled', async () => {
    const md = `[mail](mailto:a@b.co) [art](/artifacts/my-report) [rel](/docs/page)`
    const { container } = render(<MarkdownRenderer content={md} linkPreviews />)
    await waitFor(() => expect(screen.getAllByRole('link')).toHaveLength(3))
    expect(fetchMock).not.toHaveBeenCalled()
    // All three keep today's anchor rendering.
    for (const a of container.querySelectorAll('a')) {
      expect(a.className).toContain('text-accent')
    }
  })

  it('keeps a standalone artifact link as a plain paragraph anchor', async () => {
    const { container } = render(
      <MarkdownRenderer content={'[My Report](/artifacts/my-report)'} linkPreviews />
    )
    await waitFor(() => expect(screen.getByRole('link', { name: 'My Report' })).toBeTruthy())
    expect(container.querySelector('p')).not.toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('MarkdownRenderer link unfurl — LinkOverrideCtx priority', () => {
  const override = (link: { href: string }) => <span data-testid="ref">{`ref:${link.href}`}</span>

  it('lets the override own an inline link, with no unfurl fetch', async () => {
    render(
      <LinkOverrideCtx.Provider value={override}>
        <MarkdownRenderer content={`See [x](${HREF}) here.`} linkPreviews />
      </LinkOverrideCtx.Provider>
    )
    await waitFor(() => expect(screen.getByTestId('ref')).toBeTruthy())
    expect(screen.queryByRole('link')).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('lets the override own a standalone link instead of rendering a card', async () => {
    const { container } = render(
      <LinkOverrideCtx.Provider value={override}>
        <MarkdownRenderer content={HREF} linkPreviews />
      </LinkOverrideCtx.Provider>
    )
    await waitFor(() => expect(screen.getByTestId('ref')).toBeTruthy())
    expect(cardShown()).toBe(false)
    expect(container.querySelector('p')).not.toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('still unfurls an href the override declines to claim', async () => {
    const declining = () => null
    render(
      <LinkOverrideCtx.Provider value={declining}>
        <MarkdownRenderer content={HREF} linkPreviews />
      </LinkOverrideCtx.Provider>
    )
    await waitFor(() => expect(cardShown()).toBe(true))
  })
})

describe('MarkdownRenderer link unfurl — metadata unavailable', () => {
  it('falls back to today\u2019s anchor when the backend has nothing to show', async () => {
    fetchMock.mockImplementation(async () => err(502, 'fetch_failed'))
    const { container } = render(<MarkdownRenderer content={`Read the [docs](${HREF}).`} linkPreviews />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const link = screen.getByRole('link', { name: 'docs' })
    expect(link).toHaveAttribute('href', HREF)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.className).toContain('text-accent')
    expect(cardShown()).toBe(false)
    expect(container.querySelector('p')).not.toBeNull()
  })

  it('keeps a standalone link as a paragraph anchor when the preview is refused', async () => {
    fetchMock.mockImplementation(async () => err(403, 'link_previews_disabled'))
    const { container } = render(<MarkdownRenderer content={HREF} linkPreviews />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(container.querySelector('p')).not.toBeNull()
    expect(screen.getByRole('link')).toHaveAttribute('href', HREF)
    expect(cardShown()).toBe(false)
  })
})
