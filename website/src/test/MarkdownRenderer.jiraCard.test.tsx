// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import MarkdownRenderer, { LinkOverrideCtx } from '../components/MarkdownRenderer'
import { JiraHostsCtx } from '../lib/jiraHosts'
import { __resetLinkMetaForTests } from '../lib/linkMeta'

// Sole-link Jira cards: a Jira issue URL that is a paragraph's ONLY content
// renders as a block LinkCard (provider mark, issue key, instance host) built
// synchronously from the URL alone — no fetch, because Jira instances sit
// behind auth so the generic unfurl path can never be relied on for them.
// Position stays the whole selection rule: the same URL inside a sentence
// keeps today's inline chip. Recognition is the same allowlist-gated parse as
// the chip (Cloud *.atlassian.net automatic, self-hosted via JiraHostsCtx),
// and the card obeys the same linkPreviews/streaming gate as the fetched card.

const CLOUD = 'https://acme.atlassian.net/browse/PROJ-123'

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  __resetLinkMetaForTests()
  fetchMock = vi.fn(async () => ({ ok: false, status: 502, json: async () => ({}) }) as unknown as Response)
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** The card is the only Jira form that shows the instance host, so its
 *  presence/absence distinguishes card from chip without asserting classes. */
const cardShown = () => screen.queryByText('acme.atlassian.net') !== null

describe('MarkdownRenderer Jira link card — sole-link paragraphs', () => {
  it('renders a standalone Jira issue URL as a card, not a chip', async () => {
    const { container } = render(<MarkdownRenderer content={CLOUD} linkPreviews />)
    await waitFor(() => expect(cardShown()).toBe(true))
    // Card replaces the paragraph rather than nesting inside it.
    expect(container.querySelector('p')).toBeNull()
    // Issue key is the title; the provider mark renders; copy button present.
    expect(screen.getByText('PROJ-123')).toBeTruthy()
    expect(screen.getByTestId('jira-provider-mark')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Copy URL of PROJ-123' })).toBeTruthy()
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', CLOUD)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('issues no network request on the sole-link Jira path', async () => {
    render(<MarkdownRenderer content={CLOUD} linkPreviews />)
    await waitFor(() => expect(cardShown()).toBe(true))
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('keeps the same URL inside a sentence as an inline chip', async () => {
    const { container } = render(
      <MarkdownRenderer content={`Tracked in ${CLOUD} since May.`} linkPreviews />
    )
    await waitFor(() => expect(screen.getByText('PROJ-123')).toBeTruthy())
    // Chip form: paragraph survives, no host block, no copy button. (MdAnchor's
    // pre-existing discarded unfurl probe for inline links is out of scope here
    // — the no-fetch guarantee this change makes is for the sole-link card path,
    // asserted above.)
    expect(container.querySelector('p')).not.toBeNull()
    expect(cardShown()).toBe(false)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('renders neither chip nor card for a non-allowlisted host with a Jira-looking path', async () => {
    const { container } = render(
      <MarkdownRenderer content="https://jira.evil.example/browse/CORE-5" linkPreviews />
    )
    await waitFor(() => expect(screen.getByRole('link')).toBeTruthy())
    // Fails closed: no key label, no provider mark, no host block — the sole
    // link goes down the generic unfurl path like any other URL.
    expect(screen.queryByText('CORE-5')).toBeNull()
    expect(screen.queryByTestId('jira-provider-mark')).toBeNull()
    expect(container.textContent).toContain('jira.evil.example')
  })

  it('cards an allowlisted self-hosted Jira URL', async () => {
    render(
      <JiraHostsCtx.Provider value={['jira.acme.internal']}>
        <MarkdownRenderer content="https://jira.acme.internal/browse/CORE-5" linkPreviews />
      </JiraHostsCtx.Provider>
    )
    await waitFor(() => expect(screen.getByText('CORE-5')).toBeTruthy())
    expect(screen.getByText('jira.acme.internal')).toBeTruthy()
    expect(screen.getByTestId('jira-provider-mark')).toBeTruthy()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('stays a chip when link previews are disabled (the default)', async () => {
    const { container } = render(<MarkdownRenderer content={CLOUD} />)
    await waitFor(() => expect(screen.getByText('PROJ-123')).toBeTruthy())
    expect(container.querySelector('p')).not.toBeNull()
    expect(cardShown()).toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('stays a chip while the block is still streaming, then cards once settled', async () => {
    const { container, rerender } = render(
      <MarkdownRenderer content={CLOUD} linkPreviews streaming />
    )
    await waitFor(() => expect(screen.getByText('PROJ-123')).toBeTruthy())
    expect(cardShown()).toBe(false)
    expect(container.querySelector('p')).not.toBeNull()
    rerender(<MarkdownRenderer content={CLOUD} linkPreviews streaming={false} />)
    await waitFor(() => expect(cardShown()).toBe(true))
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('strips Basic-auth userinfo from the card href (canonical URL, not the raw href)', async () => {
    // This branch renders before the unfurl path's safeHttpUrl() rejection, so
    // it must not hand a credential-bearing raw href to the anchor or the copy
    // button. The parser's canonical URL is rebuilt from hostname+port alone.
    render(
      <MarkdownRenderer content="https://user:secret@acme.atlassian.net/browse/PROJ-123" linkPreviews />
    )
    await waitFor(() => expect(cardShown()).toBe(true))
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', CLOUD)
    expect(link.getAttribute('href')).not.toContain('secret')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('yields to a LinkOverrideCtx provider that claims the href', async () => {
    const override = (link: { href: string }) => <span data-testid="ref">{`ref:${link.href}`}</span>
    const { container } = render(
      <LinkOverrideCtx.Provider value={override}>
        <MarkdownRenderer content={CLOUD} linkPreviews />
      </LinkOverrideCtx.Provider>
    )
    await waitFor(() => expect(screen.getAllByTestId('ref').length).toBeGreaterThan(0))
    expect(cardShown()).toBe(false)
    expect(container.querySelector('p')).not.toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
