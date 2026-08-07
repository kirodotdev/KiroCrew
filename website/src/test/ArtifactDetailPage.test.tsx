import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route, useNavigate } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')
// Stub the embedded chat page (companion chat) — its rendering is covered by its
// own suites; here it just needs to mount without ChatPage's full hook/provider
// graph.
vi.mock('../pages/ChatPage', () => ({
  default: () => <div data-testid="chat-page" />,
  PREFILL_STORAGE_KEY: 'kirocrew_prefill',
}))

const mkArtifact = (overrides: Partial<Artifact> = {}): Artifact => ({
  slug: 'cr-queue',
  name: 'CR Queue',
  kind: 'widget',
  source: 'chat',
  description: 'Hourly CR snapshot',
  tags: ['ops', 'cr'],
  version: 2,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:30:00.000000+00:00',
  content: '<div>CR Queue widget body</div>',
  ...overrides,
})

function renderRoute() {
  return renderWithProviders(
    <Routes>
      <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
      <Route path="/artifacts" element={<div>library page</div>} />
    </Routes>,
    { route: '/artifacts/cr-queue' },
  )
}

/**
 * The version picker's trigger. `SimpleSelect` wraps a Radix Select, so this is
 * a <button role="combobox"> — its current value is read with toHaveTextContent,
 * not `.value`, and it carries `disabled` like any button.
 */
const versionTrigger = () => screen.getByRole('combobox', { name: /Version/i })

/**
 * Pick a row from the version picker. A `change` event on the trigger does
 * nothing — Radix needs open-then-click (see `SimpleSelect.test.tsx`).
 */
async function pickVersion(label: string) {
  fireEvent.click(versionTrigger())
  fireEvent.click(await screen.findByRole('option', { name: label }))
}

/** The version rows' labels. They exist in the DOM only while the popup is open. */
async function versionRowLabels() {
  fireEvent.click(versionTrigger())
  return (await screen.findAllByRole('option')).map((o) => o.textContent?.trim())
}

describe('ArtifactDetailPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // jsdom needs URL.createObjectURL for blob iframes
    if (!URL.createObjectURL) {
      // @ts-expect-error stub
      URL.createObjectURL = vi.fn().mockReturnValue('blob:test')
      // @ts-expect-error stub
      URL.revokeObjectURL = vi.fn()
    }
    // Default events response so the events query never throws "undefined".
    // Individual tests can override this with .mockResolvedValueOnce when
    // they need a specific event log.
    vi.mocked(api).artifactEvents = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', events: [] })
    // Default to no comments. beforeEach uses clearAllMocks (call history only,
    // not implementations), so a test that reassigns artifactComments would
    // otherwise leak its mock into later tests. Resetting the default here keeps
    // every test's comment count at 0 unless it explicitly overrides.
    vi.mocked(api).artifactComments = vi
      .fn()
      .mockResolvedValue({ comments: [] })
    // The companion toggle creates a bound session, which dispatches fetchSlots()
    // in the background. Without this mock the automock resolves undefined and
    // the fetchSlots.fulfilled reducer throws on `payload.map` AFTER the test
    // ends — an unhandled rejection that fails the run (`Errors: N errors`)
    // while every test still reports as passing.
    vi.mocked(api).chatSlots = vi.fn().mockResolvedValue([])
  })

  it('renders artifact metadata and iframe', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.getByText(/Artifact: cr-queue/i)).toBeInTheDocument()
    expect(screen.getByText('Hourly CR snapshot')).toBeInTheDocument()
    expect(screen.getByText('widget')).toBeInTheDocument()
    // The iframe title appears only after ArtifactBodyIframe's effect resolves
    // the blob URL (async); findByTitle waits for it. A synchronous getByTitle
    // races the effect under coverage instrumentation (CI-only flake).
    expect(await screen.findByTitle(/Artifact: cr-queue/)).toBeInTheDocument()
  })

  it('keeps the comment sidebar collapsed when the artifact has no comments', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({ comments: [] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Empty comment panel = wasted space on a dashboard/infographic, so the
    // sidebar stays collapsed by default.
    const toggle = screen.getByLabelText('Toggle comments')
    await waitFor(() => expect(toggle).toHaveAttribute('aria-pressed', 'false'))
  })

  it('auto-opens the comment sidebar when the artifact has comments', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactComments = vi.fn().mockResolvedValue({
      comments: [
        {
          id: 'c1', author: 'joe', is_agent: false, body: 'first review note',
          thread_id: 'c1', status: 'open', scope: 'private',
          origin: 'local', sync_state: 'local_only',
          created_at: '', updated_at: '',
        },
      ],
    })
    renderRoute()
    // The comment body appears because the sidebar auto-reveals on comments.
    await waitFor(() => expect(screen.getByText('first review note')).toBeInTheDocument())
    expect(screen.getByLabelText('Toggle comments')).toHaveAttribute('aria-pressed', 'true')
  })

  it('clears the manual sidebar override when navigating to another artifact', async () => {
    // Both artifacts have a comment. The component instance is reused across the
    // parameterized route, so without a per-navigation reset a manual close on
    // one artifact would permanently suppress auto-reveal on the next.
    const mkComment = (s: string) => ({
      comments: [
        {
          id: `${s}-c1`, author: 'joe', is_agent: false, body: `${s} note`,
          thread_id: `${s}-c1`, status: 'open', scope: 'private',
          origin: 'local', sync_state: 'local_only',
          created_at: '', updated_at: '',
        },
      ],
    })
    vi.mocked(api).artifact = vi.fn((s: string) =>
      Promise.resolve(mkArtifact({ slug: s, name: s === 'art-a' ? 'Artifact A' : 'Artifact B' })),
    ) as any
    vi.mocked(api).artifactVersions = vi.fn((s: string) =>
      Promise.resolve({ slug: s, versions: [1] }),
    ) as any
    vi.mocked(api).artifactComments = vi.fn((s: string) => Promise.resolve(mkComment(s))) as any

    function Nav() {
      const navigate = useNavigate()
      return <button onClick={() => navigate('/artifacts/art-b')}>go-b</button>
    }
    renderWithProviders(
      <>
        <Nav />
        <Routes>
          <Route path="/artifacts/:slug" element={<ArtifactDetailPage />} />
        </Routes>
      </>,
      { route: '/artifacts/art-a' },
    )
    // Artifact A auto-opens on its comment; the user then closes it.
    await waitFor(() => expect(screen.getByText('art-a note')).toBeInTheDocument())
    const toggleA = screen.getByLabelText('Toggle comments')
    expect(toggleA).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(toggleA)
    expect(screen.getByLabelText('Toggle comments')).toHaveAttribute('aria-pressed', 'false')
    // Navigate to B (same route, different param). The override must reset so B
    // — which also has a comment — auto-reveals its sidebar again.
    fireEvent.click(screen.getByText('go-b'))
    await waitFor(() => expect(screen.getByText('art-b note')).toBeInTheDocument())
    expect(screen.getByLabelText('Toggle comments')).toHaveAttribute('aria-pressed', 'true')
  })

  it('shows version dropdown with Live default and changes selected version', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ version: 2 }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    const versionFetch = vi
      .fn()
      .mockResolvedValue(mkArtifact({ version: 1, content: '<div>v1 body</div>' }))
    vi.mocked(api).artifactVersion = versionFetch

    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const trigger = versionTrigger()
    // Dropdown defaults to "Live" — historical snapshots are numbered and
    // ordered newest-first below it.
    expect(trigger).toHaveTextContent('Live')
    expect(screen.getByText(/Showing Live \(v2\)/i)).toBeInTheDocument()
    // Numbered rows exist for each historical version. Assert the labels rather
    // than the values: the Radix rows carry no value attribute, and the label is
    // what a person actually picks from.
    expect(await versionRowLabels()).toEqual(['Live', 'v2', 'v1'])
  })

  it('displays loading state', () => {
    vi.mocked(api).artifact = vi.fn().mockImplementation(() => new Promise(() => {}))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockImplementation(() => new Promise(() => {}))
    renderRoute()
    expect(screen.getByText(/Loading/i)).toBeInTheDocument()
  })

  it('shows error fallback when artifact fetch fails', async () => {
    vi.mocked(api).artifact = vi.fn().mockRejectedValue(new Error('not found'))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [] })
    renderRoute()
    await waitFor(() =>
      expect(screen.getByText(/Failed to load artifact/i)).toBeInTheDocument(),
    )
    expect(screen.getByText(/not found/i)).toBeInTheDocument()
  })

  it('back button is rendered', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.getByText(/Back/i)).toBeInTheDocument()
  })

  it('renders without description gracefully', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ description: '' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByText('Hourly CR snapshot')).not.toBeInTheDocument()
  })

  // ── native rendering for non-iframe kinds ──────────
  it('markdown artifacts render natively (no iframe)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({
        kind: 'markdown',
        content: '# Hello world\n\nThis is the BRD.',
      }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Markdown body renders inline; no iframe should be present.
    expect(document.querySelector('iframe')).toBeNull()
    // Heading text renders directly into the page (MarkdownRenderer dispatches
    // to a real <h1>).
    expect(screen.getByText('Hello world')).toBeInTheDocument()
  })

  it('json artifacts render natively (no iframe) and show parsed structure', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({
        kind: 'json',
        content: '{"foo": "bar", "n": 42}',
      }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(document.querySelector('iframe')).toBeNull()
    // JsonViewer expands depth<2 by default; key labels appear inline.
    expect(screen.getByText('"foo"')).toBeInTheDocument()
  })

  it('widget artifacts still render via iframe (existing path preserved)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Widget kind keeps iframe-based rendering. The blob URL is set via
    // useEffect (async), so wait for the iframe to appear.
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
  })

  // ── inline edit + revert ───────────────────────────
  it('edit toggle is hidden for non-editable kinds (widget)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByTitle('Edit content')).toBeNull()
  })

  it('edit toggle shown for markdown artifacts and reveals Save/Cancel', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# Doc' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const editBtn = screen.getByTitle('Edit content')
    expect(editBtn).toBeInTheDocument()
    editBtn.click()
    await waitFor(() => expect(screen.getByTitle(/Save/)).toBeInTheDocument())
    expect(screen.getByTitle(/Cancel/)).toBeInTheDocument()
  })

  it('cron-source warning banner shows when editing a cron-generated artifact', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', source: 'cron', content: '# auto-generated' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Banner is hidden in read-only mode.
    expect(screen.queryByText(/regenerated by a cron job/i)).toBeNull()
    screen.getByTitle('Edit content').click()
    await waitFor(() =>
      expect(screen.getByText(/regenerated by a cron job/i)).toBeInTheDocument(),
    )
  })

  it('revert button appears only when viewing a historical version', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 2, content: '# current' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 1, content: '# old' }),
    )
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Current view: no Revert button.
    expect(screen.queryByTitle(/Revert to v/)).toBeNull()
    // Switch to v1.
    await pickVersion('v1')
    await waitFor(() => expect(screen.getByTitle(/Revert to v1/)).toBeInTheDocument())
  })

  // ── comments → companion chat ───────────────────────────────
  // Anchored (selection-driven) commenting is the mechanism for pinning an
  // instruction to an exact span of the artifact for the agent to act on, so the
  // "select text to anchor a comment" tip must appear on the kinds that support
  // it (markdown/text render natively, so a DOM selection maps back to source
  // coordinates) and stay absent on the kinds that do not.
  it('shows the "select text to anchor a comment" tip on markdown', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# Doc' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.getByText(/select text to anchor a comment/i)).toBeInTheDocument()
  })

  it('does not show the anchored tip on non-commentable kinds (widget)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByText(/select text to anchor a comment/i)).toBeNull()
  })

  // ── lifecycle event log + activity timeline ────────
  it('Activity section is always rendered', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.getByText('Activity')).toBeInTheDocument()
  })

  it('renders the lifecycle event log when events are present', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({
      slug: 'cr-queue',
      events: [
        { ts: '2026-05-25T22:00:00.000Z', type: 'created', by: 'agent', version: 1 },
        { ts: '2026-05-25T22:30:00.000Z', type: 'iterated', by: 'agent', session_id: 'slot-abc', version: 2 },
      ],
    })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('Created')).toBeInTheDocument())
    expect(screen.getByText('Iterated')).toBeInTheDocument()
    // Newest first: the iterated row should appear before the created row.
    const list = screen.getByText('Activity').nextSibling as HTMLElement
    const items = list.querySelectorAll('li')
    expect(items[0].textContent).toContain('Iterated')
    expect(items[1].textContent).toContain('Created')
  })

  it('shows the empty-state message when events log is empty', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact())
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({
      slug: 'cr-queue', events: [],
    })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.getByText(/no lifecycle events yet/i)).toBeInTheDocument()
  })

 // ── companion chat toggle ──────────────────────────────────
  // The header sparkle is a PANEL TOGGLE (not a navigate-away action), so it is
  // available for every kind — including widgets, where asking the agent is the
  // only way to change the artifact at all.
  it('renders the companion chat toggle, unpressed, for editable kinds (markdown)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'markdown' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const toggle = screen.getByLabelText('Toggle agent chat')
    expect(toggle).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
  })

  it('renders the companion chat toggle for widget artifacts too', async () => {
    // Widgets cannot be edited inline, so asking the agent is the ONLY way to
    // change them — the toggle must never be kind-gated.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.getByLabelText('Toggle agent chat')).toBeInTheDocument()
  })

  it('reverted events render with from_version and no broken session link', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'markdown' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2, 3] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({
      slug: 'cr-queue',
      events: [
        { ts: '2026-05-25T22:00:00.000Z', type: 'created', by: 'agent', version: 1 },
        { ts: '2026-05-25T22:30:00.000Z', type: 'edited', by: 'user', session_id: 'dashboard:ui', version: 2 },
        { ts: '2026-05-25T22:45:00.000Z', type: 'reverted', by: 'user', session_id: 'dashboard:ui', version: 3, from_version: 1 },
      ],
    })
    renderRoute()
    await waitFor(() => expect(screen.getByText('Reverted')).toBeInTheDocument())
    // Revert info shows source version
    expect(screen.getByText(/v1 → v3/)).toBeInTheDocument()
    expect(screen.getByText(/content copied from v1/i)).toBeInTheDocument()
    // dashboard:ui session id should NOT render as a clickable link
    expect(screen.queryByText(/from session dashboard:ui/i)).toBeNull()
    // It should render the 'via dashboard' qualifier instead
    expect(screen.getAllByText(/via dashboard/i).length).toBeGreaterThan(0)
  })

  // ── comment lifecycle events in the activity timeline ──────
  it('renders comment lifecycle events with snippet and reason', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'markdown' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({
      slug: 'cr-queue',
      events: [
        {
          ts: '2026-07-13T05:00:00.000Z', type: 'comment', by: 'agent', version: 2,
          metadata: { action: 'deleted', comment_snippet: 'delete this paragraph', reason: 'applied in v2: paragraph removed' },
        },
        {
          ts: '2026-07-13T05:01:00.000Z', type: 'comment', by: 'agent', version: 2,
          metadata: { action: 'reviewed', comment_snippet: 'reframe the intro' },
        },
      ],
    })
    renderRoute()
    await waitFor(() => expect(screen.getByText('Comment removed')).toBeInTheDocument())
    expect(screen.getByText('Comment marked for review')).toBeInTheDocument()
    // Snippet + reason line survives the comment's deletion.
    expect(screen.getByText(/delete this paragraph/)).toBeInTheDocument()
    expect(screen.getByText(/applied in v2: paragraph removed/)).toBeInTheDocument()
    // Comment events carry no version arrow (they don't bump versions).
    expect(screen.queryByText(/→ v2/)).toBeNull()
  })

  it('Save and Snapshot buttons both render in edit mode with distinct titles', async () => {
    // Save = silent live update, Snapshot = bumps version. Both buttons
    // appear together in edit mode under the explicit-snapshot model. We can't
    // drive the Monaco editor in jsdom so
    // we rely on the unit tests for the actual snapshot=true/false wiring
    // on the store side (test_artifacts.py::TestExplicitSnapshotModel).
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# v1' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })

    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle('Edit content'))
    await waitFor(() =>
      expect(
        screen.getByTitle(/Save to Live \(Cmd\+S\) — updates the live state/i),
      ).toBeInTheDocument(),
    )
    expect(
      screen.getByTitle(/Snapshot \(Cmd\+Shift\+S\) — save and create a new version/i),
    ).toBeInTheDocument()
  })

  it('version dropdown shows Live + numbered snapshots newest-first', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ version: 3 }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2, 3] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const labels = await versionRowLabels()
    expect(labels).toEqual(['Live', 'v3', 'v2', 'v1'])
  })

  it('selecting the latest version number reads that snapshot — NOT Live (round 11 regression)', async () => {
    // Numbered versions ALWAYS read versions/v{N}.html, even N=latest:
    // selecting the highest version (e.g. v3 when art.version === 3) must
    // render the frozen snapshot, not Live. Otherwise isCurrent would collapse
    // the two cases and "v3" would appear to mutate alongside Live after a
    // silent save until the next snapshot froze it again.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 3, content: 'Live (now diverged)' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2, 3] })
    const versionFetch = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 3, content: 'v3 (frozen)' }),
    )
    vi.mocked(api).artifactVersion = versionFetch

    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Select v3 (the latest numbered snapshot).
    await pickVersion('v3')
    // versionQuery must fire for v3 — the buggy code skipped it.
    await waitFor(() => expect(versionFetch).toHaveBeenCalledWith('cr-queue', 3))
    // Page renders v3 content (frozen), not Live.
    await waitFor(() => expect(screen.getByText(/v3 \(frozen\)/)).toBeInTheDocument())
    expect(screen.queryByText(/Live \(now diverged\)/)).toBeNull()
    // Badge says "historical" since v3 is no longer Live.
    expect(screen.getByText(/Showing v3 \(historical\)/i)).toBeInTheDocument()
  })

  it('Snapshot button appears in view mode when artifact.live_dirty', async () => {
    // Snapshot-anytime affordance — when live has drifted from
    // the latest version (silent saves or external file edits), the
    // detail page exposes a "Snapshot" button outside edit mode.
    vi.mocked(api).artifact = vi
      .fn()
      .mockResolvedValue(mkArtifact({ kind: 'markdown', live_dirty: true }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.getByText('Snapshot')).toBeInTheDocument()
  })

  it('Snapshot hidden when artifact is in sync with latest version', async () => {
    vi.mocked(api).artifact = vi
      .fn()
      .mockResolvedValue(mkArtifact({ kind: 'markdown', live_dirty: false }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByText('Snapshot')).toBeNull()
  })

  it('Snapshot click calls updateArtifact with snapshot:true (no content)', async () => {
    vi.mocked(api).artifact = vi
      .fn()
      .mockResolvedValue(mkArtifact({ kind: 'markdown', live_dirty: true }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    const updateSpy = vi.fn().mockResolvedValue(mkArtifact({ kind: 'markdown' }))
    vi.mocked(api).updateArtifact = updateSpy
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Snapshot'))
    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith('cr-queue', { snapshot: true }),
    )
  })

  it('Back button confirms before discarding unsaved edits (round 12)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# v1' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Enter edit mode (no Monaco interaction needed — dirty stays false).
    fireEvent.click(screen.getByTitle('Edit content'))
    // Back without dirty: no confirm.
    fireEvent.click(screen.getByRole('button', { name: /Back/ }))
    expect(confirmSpy).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('version dropdown is disabled while saving (round 12)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', live_dirty: true }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    // Make updateArtifact hang so we can observe the in-flight saving state.
    let resolveUpdate: ((v: Artifact) => void) | null = null
    vi.mocked(api).updateArtifact = vi.fn().mockImplementation(() =>
      new Promise((resolve) => { resolveUpdate = resolve }),
    )
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    const trigger = versionTrigger()
    expect(trigger).not.toBeDisabled()
    fireEvent.click(screen.getByText('Snapshot'))
    // Wait for the saving state to render (in-flight update).
    await waitFor(() => expect(trigger).toBeDisabled())
    // Resolve to clean up.
    resolveUpdate?.(mkArtifact({ kind: 'markdown' }))
  })

  it('Cmd+S triggers handleSave when dirty in edit mode', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# v1' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle('Edit content'))
    // Dispatch Cmd+S — without dirty state the handler is a no-op,
    // but the keydown listener path executes for coverage.
    fireEvent.keyDown(document, { key: 's', metaKey: true })
    fireEvent.keyDown(document, { key: 'Escape' })
  })

  it('creates no chat slot until the companion toggle is clicked', async () => {
    // Merely viewing an artifact must never spawn a session — the bound session
    // is created lazily on the first explicit toggle click.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# v1' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    const createSlotSpy = vi.fn().mockResolvedValue({ key: 'slot-new' })
    vi.mocked(api).createChatSlot = createSlotSpy
    vi.mocked(api).chatSlotContext = vi.fn().mockResolvedValue({ ok: true })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(createSlotSpy).not.toHaveBeenCalled()
    fireEvent.click(screen.getByLabelText('Toggle agent chat'))
    await waitFor(() => expect(createSlotSpy).toHaveBeenCalledTimes(1))
    // The 8th positional argument is the artifact binding the backend persists.
    expect(createSlotSpy.mock.calls[0][7]).toBe('cr-queue')
  })

  it('description renders when artifact has one', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ description: 'Tracking ~/notes/test.md' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('Tracking ~/notes/test.md')).toBeInTheDocument())
  })

  it('renders Activity timeline with reverted event qualifier', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 4 }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2, 3, 4] })
    vi.mocked(api).artifactEvents = vi.fn().mockResolvedValue({
      slug: 'cr-queue',
      events: [
        { ts: '2026-05-25T20:00:00.000Z', type: 'created', by: 'agent', version: 1 },
        { ts: '2026-05-25T22:00:00.000Z', type: 'reverted', by: 'user', version: 4, from_version: 2 },
      ],
    })
    renderRoute()
    await waitFor(() => expect(screen.getByText('Reverted')).toBeInTheDocument())
    expect(screen.getByText(/v2 → v4/)).toBeInTheDocument()
    expect(screen.getByText(/content copied from v2/i)).toBeInTheDocument()
  })

  // ── More coverage for the explicit-snapshot paths ──────────────────────
  it('renders historical version via versionQuery when non-Live selected', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 3, content: 'live state' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2, 3] })
    const versionFetch = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 2, content: 'historical v2' }),
    )
    vi.mocked(api).artifactVersion = versionFetch
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    await pickVersion('v2')
    await waitFor(() => expect(versionFetch).toHaveBeenCalledWith('cr-queue', 2))
    await waitFor(() => expect(screen.getByText(/historical v2/)).toBeInTheDocument())
    // Edit/Snapshot buttons hidden on historical view.
    expect(screen.queryByTitle('Edit content')).toBeNull()
    // Revert button visible.
    expect(screen.getByTitle(/Revert to v2/)).toBeInTheDocument()
  })

  it('revert click calls updateArtifact with reverted event_type and from_version', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 3, content: 'live' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2, 3] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 2, content: 'v2 content' }),
    )
    const updateSpy = vi.fn().mockResolvedValue(mkArtifact({ kind: 'markdown' }))
    vi.mocked(api).updateArtifact = updateSpy
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    await pickVersion('v2')
    await waitFor(() => expect(screen.getByTitle(/Revert to v2/)).toBeInTheDocument())
    fireEvent.click(screen.getByTitle(/Revert to v2/))
    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith('cr-queue', expect.objectContaining({
        content: 'v2 content',
        event_type: 'reverted',
        from_version: 2,
      })),
    )
    confirmSpy.mockRestore()
  })

  it('Cancel button confirms before discarding while dirty', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# v1' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    fireEvent.click(screen.getByTitle('Edit content'))
    // Click Cancel — without dirty there's no confirm.
    const confirmSpy = vi.spyOn(window, 'confirm')
    fireEvent.click(screen.getByTitle(/Cancel/))
    expect(confirmSpy).not.toHaveBeenCalled()
    // Edit toggle should reappear.
    await waitFor(() => expect(screen.getByTitle('Edit content')).toBeInTheDocument())
    confirmSpy.mockRestore()
  })

  it('iterate button (with its comment-count badge) is absent while hidden', async () => {
    // The comment-count badge belongs to the Iterate button. While that button
    // is hidden pending redesign, neither the button nor the badge renders;
    // restore the badge assertion when the redesign re-enables it.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByTitle(/Discuss this artifact/i)).toBeNull()
  })

  it('SVG artifacts render without iframe', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'svg', content: '<svg viewBox="0 0 10 10"><rect width="10" height="10"/></svg>' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('text artifacts render natively', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'text', content: 'plain text content' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(document.querySelector('iframe')).toBeNull()
  })

  it('html artifacts render via iframe (uses iframe path)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'html', content: '<p>hi</p>' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
  })

  it('Live dropdown change with dirty buffer prompts before discarding', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 2 }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    vi.mocked(api).artifactVersion = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', version: 1 }),
    )
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Enter edit mode but stay clean — no dirty, no confirm needed.
    fireEvent.click(screen.getByTitle('Edit content'))
    const confirmSpy = vi.spyOn(window, 'confirm')
    await pickVersion('v1')
    expect(confirmSpy).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('beforeunload listener registers when dirty', async () => {
    // The beforeunload handler is registered/unregistered by the dirty
    // effect. We can verify the addEventListener / removeEventListener
    // calls by spying on window.
    const addSpy = vi.spyOn(window, 'addEventListener')
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    // Effect runs but dirty=false initially, so no beforeunload register.
    const beforeUnloadCalls = addSpy.mock.calls.filter(c => c[0] === 'beforeunload')
    expect(beforeUnloadCalls.length).toBe(0)
    addSpy.mockRestore()
  })

  // ── UpstreamSyncBanner (fork/publish sync) ──────────────────────────────
  describe('UpstreamSyncBanner Pull latest', () => {
    const mkFork = () => mkArtifact({
      kind: 'markdown',
      content: '# local',
      fork_metadata: {
        upstream_artifact_id: 'up-1',
        upstream_url: 'https://remote.example.com/a/up-1',
        upstream_owner: 'alice',
        upstream_version: 3,
        forked_at: '2026-06-01T00:00:00Z',
      },
    })

    beforeEach(() => {
      vi.mocked(api).getArtifactPublishProviders = vi.fn().mockResolvedValue({
        providers: [{
          name: 'companion', display_name: 'Companion', capabilities: ['content_versions'],
          kind_support: 'native', capable: true,
          sharing_model: {
            supports_private: true, supports_shared: true, supports_public: true,
            principal_kind: 'user', supports_roles: false, supports_expiration: false,
            programmable: true, out_of_band_url: '',
          },
          sync_model: { authority: 'mirror', concurrency: 'token', collab_mode: 'mirror' },
          discovery_model: {
            list_mine: true, list_shared_with_me: true, list_public: true,
            full_text_search: false, pull_by_id: true,
          },
        }],
        kind: 'markdown',
      })
      vi.mocked(api).artifactVersions = vi.fn().mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    })

    it('surfaces a benign "up to date" pull no-op as a neutral notice, not a danger error', async () => {
      vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkFork())
      // Upstream NOT ahead → the info-tone "Forked from" banner with a Pull button.
      vi.mocked(api).upstreamStatus = vi.fn().mockResolvedValue({ upstream_ahead: false })
      vi.mocked(api).pullLatest = vi.fn().mockResolvedValue({
        pull_result: { pulled: false, reason: 'up to date' },
      })
      renderRoute()
      await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
      const pullBtn = await screen.findByTitle(/Pull the latest remote content/i)
      fireEvent.click(pullBtn)
      const notice = await screen.findByText('up to date')
      // Neutral tone — must NOT be the danger-styled error span.
      expect(notice.className).toContain('text-muted')
      expect(notice.className).not.toContain('text-danger')
    })
  })

  // `pinned` is record-level and is the retention control: prune_auto_widgets
  // only sweeps unpinned records. The library exposed it on rows and cards but
  // NOT here — the page where you read an artifact and decide to keep it.
  describe('star (pinned) chip', () => {
    beforeEach(() => {
      vi.mocked(api).artifactVersions = vi
        .fn()
        .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    })

    it('renders an unpressed star for an unpinned artifact', async () => {
      vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ pinned: false }))
      renderRoute()
      await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
      const star = screen.getByLabelText('Star artifact')
      expect(star).toHaveAttribute('aria-pressed', 'false')
    })

    it('labels the chip "Starred" and presses it for a pinned artifact', async () => {
      vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ pinned: true }))
      renderRoute()
      await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
      const star = screen.getByLabelText('Remove star from artifact')
      expect(star).toHaveAttribute('aria-pressed', 'true')
      expect(star).toHaveTextContent('Starred')
    })

    it('stars via setArtifactPinned without refetching the artifact content', async () => {
      vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ pinned: false }))
      vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
      renderRoute()
      await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
      const callsBefore = vi.mocked(api).artifact.mock.calls.length

      fireEvent.click(screen.getByLabelText('Star artifact'))

      await waitFor(() =>
        expect(vi.mocked(api).setArtifactPinned).toHaveBeenCalledWith('cr-queue', true),
      )
      // The chip flips from the patched cache, not from a re-read.
      await waitFor(() =>
        expect(screen.getByLabelText('Remove star from artifact')).toBeInTheDocument(),
      )
      // Refetching ['artifact', slug] would move an open editor's baseline under a
      // stale buffer and let the next Save overwrite an agent's update — the exact
      // hazard useWebSocket's isArtifactEditing branch withholds that invalidation
      // to avoid. `pinned` is record-level, so nothing needs re-reading.
      expect(vi.mocked(api).artifact.mock.calls.length).toBe(callsBefore)
    })

    it('does not refetch content while the editor is open', async () => {
      // jsdom cannot drive the Monaco editor, so this asserts the mechanism the
      // hazard runs through rather than the buffer itself: while editing, a star
      // toggle must not re-read ['artifact', slug]. A refetch there moves the
      // editor's baseline under a stale buffer and the next Save silently
      // overwrites whatever landed server-side — which is why useWebSocket's
      // isArtifactEditing branch withholds that same invalidation.
      vi.mocked(api).artifact = vi
        .fn()
        .mockResolvedValue(mkArtifact({ kind: 'markdown', content: '# v1', pinned: false }))
      vi.mocked(api).setArtifactPinned = vi.fn().mockResolvedValue({})
      renderRoute()
      await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

      fireEvent.click(screen.getByTitle('Edit content'))
      const callsWhileEditing = vi.mocked(api).artifact.mock.calls.length

      fireEvent.click(screen.getByLabelText('Star artifact'))
      await waitFor(() =>
        expect(vi.mocked(api).setArtifactPinned).toHaveBeenCalledWith('cr-queue', true),
      )

      expect(vi.mocked(api).artifact.mock.calls.length).toBe(callsWhileEditing)
    })

    it('surfaces a failed toggle instead of silently reverting', async () => {
      vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ pinned: false }))
      vi.mocked(api).setArtifactPinned = vi.fn().mockRejectedValue(new Error('pin refused'))
      renderRoute()
      await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())

      fireEvent.click(screen.getByLabelText('Star artifact'))

      await waitFor(() => expect(screen.getByText(/pin refused/i)).toBeInTheDocument())
      // Still unpinned and still clickable — not stuck in the pending state.
      expect(screen.getByLabelText('Star artifact')).not.toBeDisabled()
    })

    it('hides the chip while viewing a historical version', async () => {
      // A version snapshot does not carry the live record-level `pinned`, so
      // rendering the chip there could show a stale star for state the user
      // cannot meaningfully toggle from that view.
      vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ pinned: true }))
      vi.mocked(api).artifactVersion = vi
        .fn()
        .mockResolvedValue(mkArtifact({ version: 1, content: '<div>v1 body</div>' }))
      renderRoute()
      await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
      expect(screen.getByLabelText('Remove star from artifact')).toBeInTheDocument()

      await pickVersion('v1')

      await waitFor(() =>
        expect(screen.queryByLabelText('Remove star from artifact')).not.toBeInTheDocument(),
      )
      expect(screen.queryByLabelText('Star artifact')).not.toBeInTheDocument()
    })
  })
})
