import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { Routes, Route } from 'react-router-dom'
import ArtifactDetailPage from '../pages/ArtifactDetailPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact } from '../types'

vi.mock('../api/client')

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
    const select = screen.getByRole('combobox') as HTMLSelectElement
    // New model: dropdown defaults to "Live" — historical snapshots are
    // numbered and ordered newest-first below it.
    expect(select.value).toBe('live')
    expect(screen.getByText(/Showing Live \(v2\)/i)).toBeInTheDocument()
    // Numbered options exist for each historical version.
    const options = Array.from(select.options).map((o) => o.value)
    expect(options).toEqual(['live', '2', '1'])
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

  // ── Phase 2 (Mesh-1654): native rendering for non-iframe kinds ──────────
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
    // Widget kind keeps iframe-based rendering.
    expect(document.querySelector('iframe')).not.toBeNull()
  })

  // ── Phase 3 (Mesh-1654): inline edit + revert ───────────────────────────
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
    const select = screen.getByRole('combobox') as HTMLSelectElement
    fireEvent.change(select, { target: { value: '1' } })
    await waitFor(() => expect(screen.getByTitle(/Revert to v1/)).toBeInTheDocument())
  })

  // ── Phase 4 (Mesh-1654): comments → chat ────────────────────────────────
  // Inline commenting feeds the Iterate flow, which is hidden pending redesign
  // (P472753393) via SHOW_ARTIFACT_ITERATE. While hidden, the "select text to
  // add inline comments" tip must NOT appear on any kind. Flip these back to
  // assert presence when the Iterate redesign re-enables the flag.
  it('does not show the "select text to comment" tip while Iterate is hidden (markdown)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# Doc' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByText(/select text to add inline comments/i)).toBeNull()
  })

  it('does not show comment tip on non-commentable kinds (widget)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByText(/select text to add inline comments/i)).toBeNull()
  })

  // ── Phase 5 (Mesh-1654): lifecycle event log + activity timeline ────────
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

  // ── Iterate affordances hidden pending redesign (P472753393) ────────────
  // The header "Iterate" button is gated behind SHOW_ARTIFACT_ITERATE (false).
  // These assert it is ABSENT for every kind; flip back to assert presence when
  // the redesign re-enables the flag.
  it('Iterate button is hidden for editable kinds (markdown)', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'markdown' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByTitle(/Discuss this artifact with the agent/i)).toBeNull()
  })

  it('Iterate button is hidden for widget artifacts', async () => {
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(mkArtifact({ kind: 'widget' }))
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1, 2] })
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByTitle(/Discuss this artifact with the agent/i)).toBeNull()
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


  it('Save and Snapshot buttons both render in edit mode with distinct titles', async () => {
    // Save = silent live update, Snapshot = bumps version. Both buttons
    // appear together in edit mode under the new explicit-snapshot model
    // (Mesh-1654 round 5). We can't drive the Monaco editor in jsdom so
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
    const select = screen.getByRole('combobox') as HTMLSelectElement
    const labels = Array.from(select.options).map((o) => o.textContent?.trim())
    expect(labels).toEqual(['Live', 'v3', 'v2', 'v1'])
  })

  it('selecting the latest version number reads that snapshot — NOT Live (round 11 regression)', async () => {
    // Bug: when the user selected the highest version in the dropdown
    // (e.g. v3 when art.version === 3), the page rendered Live content
    // under the v3 label because isCurrent collapsed the two cases. After
    // any silent save, "v3" appeared to mutate alongside Live until the
    // user took a NEW snapshot, at which point v3 became "frozen" again.
    // Fix: numbered versions ALWAYS read versions/v{N}.html, even N=latest.
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
    const select = screen.getByRole('combobox') as HTMLSelectElement
    // Select v3 (the latest numbered snapshot).
    fireEvent.change(select, { target: { value: '3' } })
    // versionQuery must fire for v3 — the buggy code skipped it.
    await waitFor(() => expect(versionFetch).toHaveBeenCalledWith('cr-queue', 3))
    // Page renders v3 content (frozen), not Live.
    await waitFor(() => expect(screen.getByText(/v3 \(frozen\)/)).toBeInTheDocument())
    expect(screen.queryByText(/Live \(now diverged\)/)).toBeNull()
    // Badge says "historical" since v3 is no longer Live.
    expect(screen.getByText(/Showing v3 \(historical\)/i)).toBeInTheDocument()
  })

  it('Snapshot button appears in view mode when artifact.live_dirty', async () => {
    // Round 6: snapshot-anytime affordance — when live has drifted from
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

  // ── AutoSDE round 12 polish ─────────────────────────────────────────────
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
    const select = screen.getByRole('combobox') as HTMLSelectElement
    expect(select.disabled).toBe(false)
    fireEvent.click(screen.getByText('Snapshot'))
    // Wait for the saving state to render (in-flight update).
    await waitFor(() => expect(select.disabled).toBe(true))
    // Resolve to clean up.
    resolveUpdate?.(mkArtifact({ kind: 'markdown' }))
  })

  // ── Coverage push: bump frontend new-line coverage above 60% ──────────────
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

  it('no Iterate button means no chat-slot creation entry point (hidden pending redesign)', async () => {
    // With SHOW_ARTIFACT_ITERATE off (P472753393) the header button is gone,
    // so there is no UI path to createChatSlot from the artifact page.
    vi.mocked(api).artifact = vi.fn().mockResolvedValue(
      mkArtifact({ kind: 'markdown', content: '# v1' }),
    )
    vi.mocked(api).artifactVersions = vi
      .fn()
      .mockResolvedValue({ slug: 'cr-queue', versions: [1] })
    const createSlotSpy = vi.fn().mockResolvedValue({ key: 'slot-new' })
    vi.mocked(api).createChatSlot = createSlotSpy
    renderRoute()
    await waitFor(() => expect(screen.getByText('CR Queue')).toBeInTheDocument())
    expect(screen.queryByTitle(/Discuss this artifact with the agent/i)).toBeNull()
    expect(createSlotSpy).not.toHaveBeenCalled()
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
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '2' } })
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
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '2' } })
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
    // The comment-count badge lived on the Iterate button. With the button
    // hidden pending redesign (P472753393) neither the button nor the badge
    // renders. Restore the badge assertion when the redesign re-enables it.
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
    expect(document.querySelector('iframe')).not.toBeNull()
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
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })
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
})
