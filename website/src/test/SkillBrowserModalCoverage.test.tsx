/**
 * SkillBrowserModalCoverage — behaviour coverage for the multi-provider skill
 * discovery modal: the debounced search gate, the two-pane list/detail layout,
 * the per-skill install lifecycle (installing / done / conflict / error),
 * keyboard navigation, and the detail pane's lazy SKILL.md preview.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { DiscoveredSkill, DiscoverInstallResult, DiscoverSkillPreview } from '../types'

/* ── Mocks: must be registered before the component is imported ── */
const { mockApi, MockApiError } = vi.hoisted(() => {
  class MockApiError extends Error {
    readonly status: number
    /** Raw wire body, the shape the real ApiError keeps so a caller can read
     *  the structured `code` the message unwrapping drops. */
    readonly body: string
    /** The real ApiError's auth-expiry flag (403 + `X-Auth-Required`). */
    readonly authRequired: boolean
    constructor(status: number, message: string, body = '', authRequired = false) {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
      this.authRequired = authRequired
    }
  }
  return {
    mockApi: {
      discoverSkills: vi.fn(),
      previewDiscoveredSkill: vi.fn(),
      installDiscoveredSkill: vi.fn(),
    },
    MockApiError,
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: MockApiError }))

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

// The meta strip belongs to SkillDirectoryBrowser, whose module graph pulls in
// the syntax-highlight worker client. Stub it and expose the three values the
// MODAL derives (frontmatter > preview > search result) as data attributes —
// that derivation is the behaviour under test, not the strip's own markup.
vi.mock('../components/SkillDirectoryBrowser', () => ({
  SkillMetaStrip: ({ description, triggers, tags }: {
    description?: string
    triggers?: string
    tags?: string
  }) => (
    <div
      data-testid="meta-strip"
      data-description={description ?? ''}
      data-triggers={triggers ?? ''}
      data-tags={tags ?? ''}
    />
  ),
}))

import SkillBrowserModal, { formatInstalls, classifyPreviewFailure } from '../components/SkillBrowserModal'

/* ── Fixtures ── */
const aSkill = (over: Partial<DiscoveredSkill> = {}): DiscoveredSkill => ({
  id: 'acme/widget-wrangler',
  name: 'widget-wrangler',
  description: 'Wrangles widgets from end to end',
  provider: 'skillsh',
  display_provider: 'skills.sh',
  repo_url: 'https://github.com/acme/widget-wrangler',
  author: 'acme',
  installed: false,
  tags: ['widgets', 'ops'],
  installs: 557834,
  ...over,
})

const anInstall = (over: Partial<DiscoverInstallResult> = {}): DiscoverInstallResult => ({
  ok: true,
  key: 'skillsh:acme/widget-wrangler',
  slug: 'widget-wrangler',
  provider: 'skillsh',
  kind: 'created',
  file_count: 1,
  ...over,
})

/** A gateway refusal exactly as the wire carries it: status + `{error, code}`. */
const aRefusal = (status: number, error: string, code: string) =>
  new MockApiError(status, error, JSON.stringify({ error, code }))

const CONNECTION_HINT = "Couldn't load the preview. Check the connection and try again."
const TOO_LARGE = 'Skill bundle is 12.3 MiB, above the 10.0 MiB limit'
const BAD_FORMAT = 'skills.sh returned an unexpected response format'
const RATE_LIMITED = 'skills.sh is rate-limiting requests; try again shortly'

const SKILL_MD = [
  '---',
  'name: widget-wrangler',
  'description: Frontmatter description wins',
  'triggers: wrangle widgets, tidy widgets',
  'tags: widgets, ops',
  '---',
  '# Wrangling widgets',
].join('\n')

function deferred<T>() {
  let settle!: (value: T) => void
  const promise = new Promise<T>(res => { settle = res })
  return { promise, settle }
}

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

function renderModal(opts: { open?: boolean; qc?: QueryClient; onClose?: () => void } = {}) {
  const qc = opts.qc ?? makeClient()
  const utils = render(
    <QueryClientProvider client={qc}>
      <SkillBrowserModal open={opts.open ?? true} onClose={opts.onClose ?? (() => {})} />
    </QueryClientProvider>,
  )
  return { qc, ...utils }
}

/** Type into the combobox and step past the 300ms query debounce. */
async function search(text: string) {
  fireEvent.change(screen.getByRole('combobox', { name: 'Search skills' }), {
    target: { value: text },
  })
  await act(async () => { await vi.advanceTimersByTimeAsync(350) })
}

/** Search, select the one result, and wait for its preview notice. */
async function openFailedPreview(rejection: unknown) {
  mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
  mockApi.previewDiscoveredSkill.mockRejectedValue(rejection)
  renderModal()
  await search('widget')
  fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))
  return screen.findByTestId('skill-preview-error')
}

// The modal debounces the query behind a 300ms setTimeout. Fake timers keep
// that callback on a clock this file controls, and clearAllTimers drops any
// pending one at teardown so it cannot fire into a torn-down document.
// `shouldAdvanceTime` keeps react-query's own timers behaving as they do on
// the real clock.
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.discoverSkills.mockResolvedValue({ results: [], providers: ['skillsh'] })
  mockApi.previewDiscoveredSkill.mockResolvedValue({
    name: 'widget-wrangler',
    description: 'Preview description',
  } satisfies DiscoverSkillPreview)
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('classifyPreviewFailure', () => {
  it('is null without an error, and connectivity for a rejection that is not an ApiError', () => {
    expect(classifyPreviewFailure(null)).toBeNull()
    expect(classifyPreviewFailure(undefined)).toBeNull()
    expect(classifyPreviewFailure(new TypeError('Failed to fetch')))
      .toEqual({ message: CONNECTION_HINT, retry: true })
  })

  it('lets a recognized code win over a conflicting status', () => {
    // A proxy that rewrites the status (a 504 wrapped around the gateway's
    // too_large body) must not turn a definitive refusal into connection
    // advice with a dead retry.
    expect(classifyPreviewFailure(aRefusal(504, TOO_LARGE, 'too_large')))
      .toEqual({ message: TOO_LARGE, retry: false })
    expect(classifyPreviewFailure(aRefusal(504, BAD_FORMAT, 'bad_format')))
      .toEqual({ message: BAD_FORMAT, retry: false })
    // And the other way round: a connectivity or transient code under a
    // definitive-looking status keeps its own class.
    expect(classifyPreviewFailure(aRefusal(413, 'Could not reach skills.sh', 'unreachable')))
      .toEqual({ message: CONNECTION_HINT, retry: true })
    expect(classifyPreviewFailure(aRefusal(404, RATE_LIMITED, 'rate_limited')))
      .toEqual({ message: RATE_LIMITED, retry: true })
  })

  it('lets an expired session win over both status and code', () => {
    const expired = new MockApiError(
      504, 'Session expired', JSON.stringify({ error: 'Session expired', code: 'timeout' }), true,
    )
    expect(classifyPreviewFailure(expired)).toEqual({ message: 'Session expired', retry: false })
    expect(classifyPreviewFailure(new MockApiError(403, 'Session expired', '', true)))
      .toEqual({ message: 'Session expired', retry: false })
  })

  it('falls back to the status only when the body carries no recognized code', () => {
    expect(classifyPreviewFailure(new MockApiError(504, 'HTTP 504')))
      .toEqual({ message: CONNECTION_HINT, retry: true })
    expect(classifyPreviewFailure(new MockApiError(413, 'HTTP 413')))
      .toEqual({ message: 'HTTP 413', retry: false })
    expect(classifyPreviewFailure(new MockApiError(404, 'HTTP 404')))
      .toEqual({ message: 'HTTP 404', retry: false })
    // An unrecognized code is no code: the status decides.
    expect(classifyPreviewFailure(aRefusal(413, 'refused', 'something_new')))
      .toEqual({ message: 'refused', retry: false })
    // Any other bare status keeps the gateway message and stays retryable.
    expect(classifyPreviewFailure(new MockApiError(502, 'HTTP 502')))
      .toEqual({ message: 'HTTP 502', retry: true })
  })
})

describe('SkillBrowserModal', () => {
  it('formats install counts compactly', () => {
    expect(formatInstalls(557834)).toBe('557.8K')
    expect(formatInstalls(0)).toBe('0')
  })

  it('resets session state and refetches discovery results when it opens', () => {
    const qc = makeClient()
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    renderModal({ qc })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['discover-skills'] })
  })

  it('gates the provider query until two characters are typed', async () => {
    renderModal()
    expect(screen.getByText('Type at least 2 characters to search')).toBeInTheDocument()

    await search('w')
    expect(mockApi.discoverSkills).not.toHaveBeenCalled()
    expect(screen.getByText('Type at least 2 characters to search')).toBeInTheDocument()
  })

  it('renders debounced results with provider badge, install count and description', async () => {
    mockApi.discoverSkills.mockResolvedValue({
      results: [aSkill(), aSkill({ id: 'acme/gadget-gardener', name: 'gadget-gardener', installs: 0, description: '' })],
      providers: ['skillsh'],
    })
    renderModal()
    await search('widget')

    const row = await screen.findByRole('option', { name: 'widget-wrangler' })
    expect(mockApi.discoverSkills).toHaveBeenCalledWith('widget')
    expect(within(row).getByText('skills.sh')).toBeInTheDocument()
    expect(within(row).getByText('557.8K installs')).toBeInTheDocument()
    expect(within(row).getByText('Wrangles widgets from end to end')).toBeInTheDocument()
    expect(screen.getByText('2 results')).toBeInTheDocument()
    expect(screen.getByText('Searching: skillsh')).toBeInTheDocument()

    // installs === 0 suppresses the download line entirely.
    const bare = screen.getByRole('option', { name: 'gadget-gardener' })
    expect(within(bare).queryByText(/installs/)).not.toBeInTheDocument()
  })

  it('empty result set shows the no-results placeholder', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [], providers: [] })
    renderModal()
    await search('nothing-here')

    await waitFor(() => expect(mockApi.discoverSkills).toHaveBeenCalled())
    expect(screen.getByText(/found for/)).toBeInTheDocument()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('clearing the search drops the results and the selection', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(350) })

    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
    expect(screen.getByText('Type at least 2 characters to search')).toBeInTheDocument()
  })

  it('detail pane renders frontmatter meta, markdown body and provenance links', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.previewDiscoveredSkill.mockResolvedValue({
      name: 'widget-wrangler',
      description: 'Preview description',
      author: 'preview-author',
      license: 'MIT',
      content: SKILL_MD,
      files: ['SKILL.md'],
      file_count: 3,
    } satisfies DiscoverSkillPreview)
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    await waitFor(() =>
      expect(mockApi.previewDiscoveredSkill).toHaveBeenCalledWith('skillsh', 'acme/widget-wrangler'))

    const strip = await screen.findByTestId('meta-strip')
    // Frontmatter beats the preview description, which beats the search row's.
    expect(strip).toHaveAttribute('data-description', 'Frontmatter description wins')
    expect(strip).toHaveAttribute('data-triggers', 'wrangle widgets, tidy widgets')
    expect(strip).toHaveAttribute('data-tags', 'widgets, ops')
    // Body is rendered without the YAML block.
    const body = screen.getByTestId('md')
    expect(body).toHaveTextContent('# Wrangling widgets')
    expect(body).not.toHaveTextContent('Frontmatter description wins')

    expect(screen.getByText('by preview-author')).toBeInTheDocument()
    expect(screen.getByText('License: MIT')).toBeInTheDocument()
    expect(screen.getByText('3 files')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Source/ }))
      .toHaveAttribute('href', 'https://github.com/acme/widget-wrangler')
    // A successful preview shows no failure notice and no retry.
    expect(screen.queryByTestId('skill-preview-error')).not.toBeInTheDocument()
    expect(screen.queryByTestId('skill-preview-retry')).not.toBeInTheDocument()
  })

  it('falls back to the search-result tags and author when the preview omits them', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.previewDiscoveredSkill.mockResolvedValue({
      name: 'widget-wrangler',
      description: '',
      content: '# No frontmatter here',
    } satisfies DiscoverSkillPreview)
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    const strip = await screen.findByTestId('meta-strip')
    expect(strip).toHaveAttribute('data-description', 'Wrangles widgets from end to end')
    expect(strip).toHaveAttribute('data-tags', 'widgets, ops')
    expect(strip).toHaveAttribute('data-triggers', '')
    expect(screen.getByText('by acme')).toBeInTheDocument()
  })

  it('refuses a non-http(s) repo_url as a Source link', async () => {
    // repo_url is provider-controlled data: a javascript: scheme must never
    // become a clickable href.
    mockApi.discoverSkills.mockResolvedValue({
      results: [aSkill({ repo_url: 'javascript:alert(1)' })],
      providers: ['skillsh'],
    })
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    await waitFor(() => expect(mockApi.previewDiscoveredSkill).toHaveBeenCalled())
    expect(screen.queryByRole('link', { name: /Source/ })).not.toBeInTheDocument()
  })

  it('shows the preview spinner while SKILL.md is in flight', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    const pending = deferred<DiscoverSkillPreview>()
    mockApi.previewDiscoveredSkill.mockReturnValue(pending.promise)
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    expect(await screen.findByText('Loading preview...', undefined, { timeout: 5_000 })).toBeInTheDocument()

    await act(async () => {
      pending.settle({ name: 'widget-wrangler', description: 'Preview description' })
      await vi.advanceTimersByTimeAsync(0)
    })
    await waitFor(() => expect(screen.queryByText('Loading preview...')).not.toBeInTheDocument())
  })

  it('falls back to the description, then to a no-preview notice, without SKILL.md', async () => {
    mockApi.discoverSkills.mockResolvedValue({
      results: [aSkill({ description: '' })],
      providers: ['skillsh'],
    })
    mockApi.previewDiscoveredSkill.mockResolvedValue({
      name: 'widget-wrangler',
      description: '',
    } satisfies DiscoverSkillPreview)
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    expect(await screen.findByText('No preview available.')).toBeInTheDocument()
    expect(screen.queryByTestId('meta-strip')).not.toBeInTheDocument()
  })

  it('a too-large bundle (413 too_large) shows the size and the limit, with no retry and no connection advice', async () => {
    // The gateway measured the bundle; the same request refuses the same way
    // every time, so a retry is a dead button and "check the connection" is
    // the wrong diagnosis.
    const alert = await openFailedPreview(aRefusal(413, TOO_LARGE, 'too_large'))
    expect(alert).toHaveTextContent(TOO_LARGE)
    expect(alert).not.toHaveTextContent(CONNECTION_HINT)
    expect(screen.queryByTestId('skill-preview-retry')).not.toBeInTheDocument()
    expect(screen.queryByText('No preview available.')).not.toBeInTheDocument()
  })

  it('a browser-side fetch failure gets the connection words and a retry that refetches', async () => {
    const alert = await openFailedPreview(new TypeError('Failed to fetch'))
    expect(alert).not.toHaveTextContent('Failed to fetch')
    expect(alert).toHaveTextContent(CONNECTION_HINT)
    // The retry re-requests the preview; a later success replaces the notice.
    mockApi.previewDiscoveredSkill.mockResolvedValue({
      name: 'widget-wrangler',
      description: 'now it loads',
      content: '# Widget Wrangler\n\nbody',
    } satisfies DiscoverSkillPreview)
    fireEvent.click(screen.getByTestId('skill-preview-retry'))
    await waitFor(() => expect(mockApi.previewDiscoveredSkill).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('md')).toBeInTheDocument()
    expect(screen.queryByTestId('skill-preview-error')).not.toBeInTheDocument()
  })

  it('Try again is disabled while a refetch over a cached preview is in flight, so a second click cannot queue a duplicate', async () => {
    // With no cached preview a retry shows the loading state (the notice and
    // its button unmount, so there is nothing to double-click). With a cached
    // preview the query keeps its error while refetching, the notice stays up,
    // and the button itself has to refuse the second click.
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.previewDiscoveredSkill.mockResolvedValue({
      name: 'widget-wrangler',
      description: 'Preview description',
      content: '# first',
    } satisfies DiscoverSkillPreview)
    const { qc } = renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))
    expect(await screen.findByTestId('md')).toHaveTextContent('# first')

    // A later refetch fails: the notice appears over the cached preview.
    mockApi.previewDiscoveredSkill.mockRejectedValue(new TypeError('Failed to fetch'))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['skill-preview'] }) })
    expect(await screen.findByTestId('skill-preview-error')).toHaveTextContent(CONNECTION_HINT)
    expect(mockApi.previewDiscoveredSkill).toHaveBeenCalledTimes(2)

    const pending = deferred<DiscoverSkillPreview>()
    mockApi.previewDiscoveredSkill.mockReturnValue(pending.promise)
    expect(screen.getByTestId('skill-preview-retry')).not.toBeDisabled()
    fireEvent.click(screen.getByTestId('skill-preview-retry'))
    await waitFor(() => expect(screen.getByTestId('skill-preview-retry')).toBeDisabled())
    expect(screen.getByTestId('skill-preview-error')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('skill-preview-retry'))
    expect(mockApi.previewDiscoveredSkill).toHaveBeenCalledTimes(3)

    await act(async () => {
      pending.settle({ name: 'widget-wrangler', description: 'now it loads', content: '# second' })
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(await screen.findByTestId('md')).toHaveTextContent('# second')
    expect(screen.queryByTestId('skill-preview-error')).not.toBeInTheDocument()
  })

  it('an unreachable registry (502 unreachable) and a timeout (504 timeout) are connectivity, not a verdict on the skill', async () => {
    const alert = await openFailedPreview(aRefusal(502, 'Could not reach skills.sh', 'unreachable'))
    expect(alert).toHaveTextContent(CONNECTION_HINT)
    expect(alert).not.toHaveTextContent('Could not reach')
    expect(screen.getByTestId('skill-preview-retry')).toBeInTheDocument()

    mockApi.previewDiscoveredSkill.mockRejectedValue(aRefusal(504, 'Fetch timed out', 'timeout'))
    fireEvent.click(screen.getByTestId('skill-preview-retry'))
    await waitFor(() => expect(mockApi.previewDiscoveredSkill).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('skill-preview-error')).toHaveTextContent(CONNECTION_HINT)
    expect(screen.getByTestId('skill-preview-retry')).toBeInTheDocument()
  })

  it('a malformed registry payload (502 bad_format) keeps the gateway reason: no connection words, no retry', async () => {
    // Same status as an unreachable registry; the structured code tells them
    // apart, and a payload the registry serves wrong does not fix itself on a
    // second request.
    const alert = await openFailedPreview(aRefusal(502, BAD_FORMAT, 'bad_format'))
    expect(alert).toHaveTextContent(BAD_FORMAT)
    expect(alert).not.toHaveTextContent(CONNECTION_HINT)
    expect(screen.queryByTestId('skill-preview-retry')).not.toBeInTheDocument()
  })

  it('a missing skill (404 not_found) is final: the gateway reason, no retry', async () => {
    const alert = await openFailedPreview(
      aRefusal(404, "Skill 'acme/widget-wrangler' was not found on skills.sh", 'not_found'),
    )
    expect(alert).toHaveTextContent("Skill 'acme/widget-wrangler' was not found on skills.sh")
    expect(alert).not.toHaveTextContent(CONNECTION_HINT)
    expect(screen.queryByTestId('skill-preview-retry')).not.toBeInTheDocument()
  })

  it('shows a provider rate-limit message on the preview pane and on the install row, with a retry', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.previewDiscoveredSkill.mockRejectedValue(aRefusal(429, RATE_LIMITED, 'rate_limited'))
    mockApi.installDiscoveredSkill.mockRejectedValue(aRefusal(429, RATE_LIMITED, 'rate_limited'))
    renderModal()
    await search('widget')
    const row = await screen.findByRole('option', { name: 'widget-wrangler' })
    fireEvent.click(row)
    const alert = await screen.findByTestId('skill-preview-error')
    expect(alert).toHaveTextContent(RATE_LIMITED)
    expect(alert).not.toHaveTextContent(CONNECTION_HINT)
    // A rate limit clears with time, so the retry stays.
    expect(screen.getByTestId('skill-preview-retry')).toBeInTheDocument()
    fireEvent.click(within(row).getByRole('button', { name: 'Install' }))
    // Both the row's failure line and the detail pane's copy carry the message.
    await waitFor(() => expect(screen.getAllByText(RATE_LIMITED).length).toBeGreaterThanOrEqual(2))
  })

  it('lists the bundle manifest when the skill ships more than one file', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.previewDiscoveredSkill.mockResolvedValue({
      name: 'widget-wrangler',
      description: 'Preview description',
      content: SKILL_MD,
      files: ['SKILL.md', 'scripts/run.py'],
      file_count: 2,
    } satisfies DiscoverSkillPreview)
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    expect(await screen.findByText('Bundle contents (2 files)')).toBeInTheDocument()
    expect(screen.getByText('scripts/run.py')).toBeInTheDocument()
  })

  it('install flow: installing spinner, then Installed, and the skills list is invalidated', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    const pending = deferred<DiscoverInstallResult>()
    mockApi.installDiscoveredSkill.mockReturnValue(pending.promise)
    const qc = makeClient()
    renderModal({ qc })
    await search('widget')

    const row = await screen.findByRole('option', { name: 'widget-wrangler' })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    fireEvent.click(within(row).getByRole('button', { name: 'Install' }))

    expect(await screen.findByText('Installing...')).toBeInTheDocument()
    expect(mockApi.installDiscoveredSkill)
      .toHaveBeenCalledWith('skillsh', 'acme/widget-wrangler', { overwrite: undefined })

    await act(async () => {
      pending.settle(anInstall())
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(await screen.findByText('Installed')).toBeInTheDocument()
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['skills'] })
    // Clicking the row after a local install must not re-offer Install.
    expect(within(row).queryByRole('button', { name: 'Install' })).not.toBeInTheDocument()
  })

  it('reports the file count when a bundle install writes several files', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.installDiscoveredSkill.mockResolvedValue(anInstall({ file_count: 4, kind: 'updated' }))
    renderModal()
    await search('widget')

    const row = await screen.findByRole('option', { name: 'widget-wrangler' })
    fireEvent.click(within(row).getByRole('button', { name: 'Install' }))
    expect(await screen.findByText('Installed 4 files')).toBeInTheDocument()
  })

  it('a 409 collision offers Overwrite, which retries with overwrite: true', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.installDiscoveredSkill.mockRejectedValueOnce(new MockApiError(409, 'already exists'))
    renderModal()
    await search('widget')

    const row = await screen.findByRole('option', { name: 'widget-wrangler' })
    fireEvent.click(within(row).getByRole('button', { name: 'Install' }))

    expect(await screen.findByText('Exists', undefined, { timeout: 5_000 })).toBeInTheDocument()
    mockApi.installDiscoveredSkill.mockResolvedValue(anInstall({ kind: 'updated' }))
    fireEvent.click(within(row).getByRole('button', { name: 'Overwrite' }))

    await waitFor(() => expect(mockApi.installDiscoveredSkill)
      .toHaveBeenLastCalledWith('skillsh', 'acme/widget-wrangler', { overwrite: true }))
    expect(await screen.findByText('Installed')).toBeInTheDocument()
  })

  it('surfaces a non-409 install failure on the row', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    mockApi.installDiscoveredSkill.mockRejectedValue(new MockApiError(503, 'provider unavailable'))
    renderModal()
    await search('widget')

    const row = await screen.findByRole('option', { name: 'widget-wrangler' })
    fireEvent.click(within(row).getByRole('button', { name: 'Install' }))
    expect(await screen.findByText('provider unavailable', undefined, { timeout: 5_000 })).toBeInTheDocument()
  })

  it('an already-installed result renders as Installed with no Install action', async () => {
    mockApi.discoverSkills.mockResolvedValue({
      results: [aSkill({ installed: true })],
      providers: ['skillsh'],
    })
    renderModal()
    await search('widget')

    const row = await screen.findByRole('option', { name: 'widget-wrangler' })
    expect(within(row).getByText('Installed')).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Install' })).not.toBeInTheDocument()

    // Enter must not re-install what is already there.
    const input = screen.getByRole('combobox', { name: 'Search skills' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(mockApi.installDiscoveredSkill).not.toHaveBeenCalled()
  })

  it('keyboard: ArrowDown selects the first row and Enter installs it', async () => {
    mockApi.discoverSkills.mockResolvedValue({
      results: [aSkill(), aSkill({ id: 'acme/gadget-gardener', name: 'gadget-gardener' })],
      providers: ['skillsh'],
    })
    mockApi.installDiscoveredSkill.mockResolvedValue(anInstall())
    renderModal()
    await search('widget')
    await screen.findByRole('option', { name: 'widget-wrangler' })
    const input = screen.getByRole('combobox', { name: 'Search skills' })

    fireEvent.keyDown(input, { key: 'ArrowDown' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'widget-wrangler' }))
      .toHaveAttribute('aria-selected', 'true'))
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'gadget-gardener' }))
      .toHaveAttribute('aria-selected', 'true'))
    // Clamped at the end of the list.
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expect(screen.getByRole('option', { name: 'gadget-gardener' }))
      .toHaveAttribute('aria-selected', 'true')
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'widget-wrangler' }))
      .toHaveAttribute('aria-selected', 'true'))

    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockApi.installDiscoveredSkill)
      .toHaveBeenCalledWith('skillsh', 'acme/widget-wrangler', { overwrite: undefined }))
  })

  it('keyboard: ArrowUp with nothing selected wraps to the last row, and rows handle their own keys', async () => {
    mockApi.discoverSkills.mockResolvedValue({
      results: [aSkill(), aSkill({ id: 'acme/gadget-gardener', name: 'gadget-gardener' })],
      providers: ['skillsh'],
    })
    renderModal()
    await search('widget')
    const last = await screen.findByRole('option', { name: 'gadget-gardener' })
    const input = screen.getByRole('combobox', { name: 'Search skills' })

    fireEvent.keyDown(input, { key: 'ArrowUp' })
    await waitFor(() => expect(last).toHaveAttribute('aria-selected', 'true'))

    // The row itself is a keydown target once focusable.
    fireEvent.keyDown(last, { key: 'ArrowUp' })
    await waitFor(() => expect(screen.getByRole('option', { name: 'widget-wrangler' }))
      .toHaveAttribute('aria-selected', 'true'))
    // An unhandled key is a no-op.
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.getByRole('option', { name: 'widget-wrangler' }))
      .toHaveAttribute('aria-selected', 'true')
  })

  it('arrow keys are a no-op before any result arrives', async () => {
    renderModal()
    const input = screen.getByRole('combobox', { name: 'Search skills' })
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(mockApi.installDiscoveredSkill).not.toHaveBeenCalled()
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument()
  })

  it('a new result set drops a selection that is no longer present', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))
    await waitFor(() => expect(mockApi.previewDiscoveredSkill).toHaveBeenCalled())

    mockApi.discoverSkills.mockResolvedValue({
      results: [aSkill({ id: 'acme/gadget-gardener', name: 'gadget-gardener' })],
      providers: ['skillsh'],
    })
    await search('gadget')

    await screen.findByRole('option', { name: 'gadget-gardener' })
    expect(await screen.findByText('Select a skill to preview')).toBeInTheDocument()
  })

  it('the narrow-viewport Back button returns from the detail pane to the list', async () => {
    mockApi.discoverSkills.mockResolvedValue({ results: [aSkill()], providers: ['skillsh'] })
    renderModal()
    await search('widget')
    fireEvent.click(await screen.findByRole('option', { name: 'widget-wrangler' }))

    // Single-pane mode: the list is hidden below the md breakpoint.
    const list = screen.getByRole('listbox', { name: 'Skill search results' })
    await waitFor(() => expect(list).toHaveClass('hidden'))

    fireEvent.click(await screen.findByRole('button', { name: /Back to results/ }))
    await waitFor(() => expect(list).not.toHaveClass('hidden'))
  })

  it('unmounting with a pending debounce never fires a provider query', async () => {
    const { unmount } = renderModal()
    fireEvent.change(screen.getByRole('combobox', { name: 'Search skills' }), {
      target: { value: 'widget' },
    })
    unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000) })
    expect(mockApi.discoverSkills).not.toHaveBeenCalled()
  })
})
