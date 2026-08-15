import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'

// jsdom polyfill: SegmentedControl uses ResizeObserver
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import { openActivityToTab, sseSubagentQueued } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import { sseSlots } from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    browseFiles: vi.fn().mockResolvedValue({ path: '/projects/foo', parent: '/', dirs: [], files: [] }),
    pullRequestSource: vi.fn().mockImplementation(() => new Promise(() => {})),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
    // Artifacts tab: real session-scoped artifacts + the virtual file-backed docs.
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifactSessionDocs: vi.fn().mockResolvedValue({ docs: [] }),
    materializeArtifact: vi.fn().mockResolvedValue({}),
    setArtifactPinned: vi.fn().mockResolvedValue({}),
    // Files tab: add-to-library on a file row.
    createArtifact: vi.fn().mockResolvedValue({ slug: 'new-slug', version: 1 }),
  },
}))

// MarkdownPanel pulls in Pierre + a large renderer tree; stub it so the
// Files-tab inline-preview test stays focused on the list↔file swap behavior.
// forwardRef + requestClose mirror the real imperative handle so the preview's
// "Back to files" button (which routes through the panel's guarded close) works
// against the stub without a "function components cannot be given refs" warning.
vi.mock('../components/MarkdownPanel', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<{ requestClose: () => void }, { filePath: string; content: string; onClose: () => void; onSave: (p: string, c: string) => Promise<void>; onContentChange: (c: string) => void }>(
      ({ filePath, content, onClose, onSave, onContentChange }, ref) => {
        useImperativeHandle(ref, () => ({ requestClose: onClose }), [onClose])
        return (
          <div data-testid="md-panel">
            <span>{filePath}::{content}</span>
            <button data-testid="md-save" onClick={() => onSave(filePath, 'SAVED')}>save</button>
            <button data-testid="md-edit" onClick={() => onContentChange('EDITED')}>edit</button>
          </div>
        )
      },
    ),
  }
})

import ActivityViewer from '../pages/chat/ActivityViewer'
import { api } from '../api/client'
import { __resetPanelTabs } from '../hooks/usePanelTabs'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

/* ── Section-header helpers ───────────────────────────────────────────────────
 * Both list tabs render their group headers through PanelSectionHeader, which
 * keeps the count as a SIBLING node of the label rather than punctuation inside
 * it. So `findByText('Artifact library (1)')` matches nothing — read the pair
 * through these helpers instead. */

/** Every section header in the tree, as `[label, count]`. `count` is null when
 *  the header was rendered without one (children[1] is then the hairline rule). */
function sectionHeaders(): (string | null)[][] {
  return screen.getAllByTestId('panel-section-header').map(h => [
    h.children[0].textContent,
    h.children[1]?.classList.contains('h-px') ? null : h.children[1]?.textContent ?? null,
  ])
}

/** Waits for a section header carrying this label and count to appear. */
async function findSection(label: string, count: number) {
  await waitFor(() => {
    expect(sectionHeaders()).toContainEqual([label, String(count)])
  })
}

describe('ActivityViewer', () => {
  const baseProps = {
    subagents: {},
    toolLog: [],
    open: true,
    onToggle: vi.fn(),
    slot: 'test-slot',
  }

  // useSortableTable persists the chosen sort to localStorage keyed by tableId,
  // so clear it between tests to keep the file-browser sort tests independent.
  // Also reset the module-level panel-tab store (which holds inline-preview
  // drafts) so a draft from one test can't leak into the next.
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('renders each detected PR as a source selector in the Changes view', () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="changes"
        sources={[
          { provider: 'github', owner: 'octo', repo: 'alpha', number: 42, url: 'https://github.com/octo/alpha/pull/42' },
          { provider: 'gitlab', owner: 'team', repo: 'beta', number: 7, url: 'https://gitlab.com/team/beta/-/merge_requests/7' },
        ]}
        selectedSourceUrl="https://github.com/octo/alpha/pull/42"
        onSelectSource={vi.fn()}
      />,
      { wrapper },
    )

    expect(screen.getByRole('tab', { name: 'PR #42' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'MR !7' })).toBeInTheDocument()
    expect(screen.getByText('Loading source provider…')).toBeInTheDocument()
  })

  it('shows an empty state, not the Files view, when Changes is opened with no PR', () => {
    // Changes is a PINNED view (always present under `view` mode), so with no
    // sources it must NOT fall back to the touched-files list under a "Changes"
    // header — it owns its own PR empty state instead. Even with touched files
    // present, the Changes view stays empty.
    render(
      <ActivityViewer
        {...baseProps}
        view="changes"
        sources={[]}
        files={[{ path: '/proj/foo.ts', ts: 1, source: 'tool' }]}
      />,
      { wrapper },
    )
    expect(screen.queryByText('No files changed yet')).toBeNull()
    expect(screen.queryByText('/proj/foo.ts')).toBeNull()
    expect(screen.getByText(/No pull requests in this session yet/)).toBeInTheDocument()
  })

  it('Resources hides links present in the Changes tab (sources) and keeps the rest', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    store.dispatch(openActivityToTab('links'))
    const prUrl = 'https://github.com/kirodotdev/KiroCrew/pull/42'
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ActivityViewer
            {...baseProps}
            // The Changes tab surfaces this PR, so it should NOT also appear in Resources.
            sources={[{ url: prUrl, provider: 'github', number: 42, repo: 'KiroCrew' }]}
            navLinks={[
              { url: prUrl, type: 'cr', label: 'PR #42', msgIdx: 0 },
              // Not in `sources` (a code-review host Changes can't render) — must stay reachable.
              { url: 'https://git.example.com/reviews/CR-1', type: 'cr', label: 'CR-1', msgIdx: 0 },
              { url: 'https://git.example.com/packages/KiroCrew', type: 'other', label: 'KiroCrew repo', msgIdx: 0 },
            ]}
          />
        </QueryClientProvider>
      </Provider>,
    )
    expect(screen.getByText('Resources')).toBeInTheDocument()
    // Non-Changes links stay in Resources.
    expect(screen.getByText('KiroCrew repo')).toBeInTheDocument()
    expect(screen.getByText('CR-1')).toBeInTheDocument()
    // The link already shown in the Changes tab is hidden from Resources.
    expect(screen.queryByText('PR #42')).not.toBeInTheDocument()
  })

  it('search filters links, with a no-matches state', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="links"
        // >5 entries, so the search box clears its display threshold.
        navLinks={[
          { url: 'https://example.com/alpha', type: 'other', label: 'Alpha link', msgIdx: 0 },
          { url: 'https://example.com/beta', type: 'other', label: 'Beta link', msgIdx: 0 },
          { url: 'https://example.com/gamma', type: 'other', label: 'Gamma link', msgIdx: 0 },
          { url: 'https://example.com/delta', type: 'other', label: 'Delta link', msgIdx: 0 },
          { url: 'https://example.com/epsilon', type: 'other', label: 'Epsilon link', msgIdx: 0 },
          { url: 'https://example.com/zeta', type: 'other', label: 'Zeta link', msgIdx: 0 },
        ]}
      />,
      { wrapper },
    )
    expect(screen.getByText('Resources')).toBeInTheDocument()

    const box = screen.getByLabelText('Search by label or URL…')
    fireEvent.change(box, { target: { value: 'alpha' } })
    expect(screen.getByText('Alpha link')).toBeInTheDocument()
    expect(screen.queryByText('Beta link')).not.toBeInTheDocument()

    // A query matching nothing shows the no-matches state, not the empty state.
    fireEvent.change(box, { target: { value: 'zzz-no-such-thing' } })
    expect(screen.getByText('No matches')).toBeInTheDocument()
    expect(screen.queryByText('No links yet')).not.toBeInTheDocument()

    // Clearing restores everything.
    fireEvent.change(box, { target: { value: '' } })
    expect(screen.getByText('Beta link')).toBeInTheDocument()
  })

  it('keeps the search box mounted while a query is active, even below the threshold', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="links"
        navLinks={[
          { url: 'https://example.com/alpha', type: 'other', label: 'Alpha link', msgIdx: 0 },
          { url: 'https://example.com/beta', type: 'other', label: 'Beta link', msgIdx: 0 },
          { url: 'https://example.com/gamma', type: 'other', label: 'Gamma link', msgIdx: 0 },
          { url: 'https://example.com/delta', type: 'other', label: 'Delta link', msgIdx: 0 },
          { url: 'https://example.com/epsilon', type: 'other', label: 'Epsilon link', msgIdx: 0 },
          { url: 'https://example.com/zeta', type: 'other', label: 'Zeta link', msgIdx: 0 },
        ]}
      />,
      { wrapper },
    )
    const label = 'Search by label or URL…'
    const box = screen.getByLabelText(label)
    // Filtering down to ONE match takes the visible count below the 5-entry
    // threshold. The box must NOT unmount — otherwise the stale query keeps
    // filtering with no input left to clear it, and the hidden rows read as lost.
    fireEvent.change(box, { target: { value: 'alpha' } })
    expect(screen.getByLabelText(label)).toBeInTheDocument()
    expect(screen.getByText('Alpha link')).toBeInTheDocument()
    expect(screen.queryByText('Beta link')).not.toBeInTheDocument()
    // Clearing brings everything back.
    fireEvent.change(screen.getByLabelText(label), { target: { value: '' } })
    expect(screen.getByText('Beta link')).toBeInTheDocument()
  })

  it('hides the search box for a short list (nothing to filter yet)', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="links"
        navLinks={[{ url: 'https://example.com/alpha', type: 'other', label: 'Alpha link', msgIdx: 0 }]}
      />,
      { wrapper },
    )
    // The list renders, but a 1-item list is faster to scan than to filter.
    expect(screen.getByText('Alpha link')).toBeInTheDocument()
    expect(screen.queryByLabelText('Search by label or URL…')).not.toBeInTheDocument()
  })










  /* ── Files tab: add a file to the artifact library ─────────────────────────
   * The Artifacts tab lists artifact records only, so a plain file can no
   * longer reach the library by extension alone. The explicit way in lives on
   * the file row itself. */
})

// ── Artifacts tab: artifact records only ────────────────────────────────────
//
// SessionArtifactsTab lists real artifact RECORDS from two queries over the same
// store: the session's involvement scope (`?touched_by=`) and the whole library.
// Auto-registered widgets surface through the first — their HTML is inline in the
// message and never hits disk. It used to run a THIRD query for "session
// documents" (plain files admitted by extension), which put scratch notes in the
// library; these tests pin that those are gone, plus the de-dup and the
// save-permanently routing. SessionArtifactsTab uses useNavigate, so it needs a
// Router.
describe('ActivityViewer — Artifacts tab', () => {
  const artifactProps = {
    subagents: {},
    toolLog: [],
    open: true,
    onToggle: vi.fn(),
    slot: 'test-slot',
    view: 'artifacts' as const,
  }

  function routerWrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    return (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
      </Provider>
    )
  }

  beforeEach(() => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({ docs: [] })
  })

  it('scopes one artifact query to this session and fetches the library too', async () => {
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    await waitFor(() => {
      // Session section uses the INVOLVEMENT scope (created + read + iterated),
      // not the narrower origin-only `session` filter — that is what lets an
      // artifact the agent merely consumed appear under "This session".
      expect(api.artifacts).toHaveBeenCalledWith({ touchedBy: 'test-slot' })
    })
    // The library section is a second, unscoped query.
    await waitFor(() => {
      expect(api.artifacts).toHaveBeenCalledWith({})
    })
  })

  it('lists an auto-registered widget artifact (no filesystem path)', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'abc123', name: 'Sales Chart', kind: 'widget', pinned: false, auto_registered: true }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Sales Chart')).toBeInTheDocument()
    // Auto-registered + unpinned is the sweepable state, so this row is the one
    // that offers to save it permanently.
    expect(screen.getByTestId('artifact-save-abc123')).toBeInTheDocument()
  })

  it('lists an auto-registered image artifact for this session', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{
        slug: 'generated-image',
        name: 'Generated image',
        kind: 'image',
        pinned: false,
        auto_registered: true,
        image: { mime: 'image/png', ext: 'png', alt: 'generated preview' },
      }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Generated image')).toBeInTheDocument()
    expect(screen.getByText('image')).toBeInTheDocument()
    expect(screen.getByTestId('artifact-save-generated-image')).toBeInTheDocument()
  })

  it('offers no save action once an auto-registered widget is pinned', async () => {
    // Pinning is one-way in this panel: un-pinning only buys eligibility for
    // the sweep, so a pinned row must NOT render a toggle back.
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'abc123', name: 'Kept', kind: 'widget', pinned: true, auto_registered: true }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Kept')).toBeInTheDocument()
    expect(screen.queryByTestId('artifact-save-abc123')).not.toBeInTheDocument()
  })

  it('offers no save action on an explicitly created artifact', async () => {
    // Nothing sweeps a non-auto-registered record, so the control would promise
    // a safety it is not providing.
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'plan-md', name: 'Launch plan', kind: 'markdown', pinned: false, auto_registered: false }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Launch plan')).toBeInTheDocument()
    expect(screen.queryByTestId('artifact-save-plan-md')).not.toBeInTheDocument()
  })

  it('lists only artifact records — a scratch file this session wrote is not one', async () => {
    // The tab used to run a second query (GET /api/artifacts/session-docs) that
    // admitted any path by extension (.md/.txt/.rst/…), so a scratch note the
    // agent wrote appeared here as if it were an artifact. Files belong to the
    // Files tab; getting into the library is an explicit act.
    mockArtifactQueries([], [])
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText(/No artifacts in this chat yet/)).toBeInTheDocument()
    expect(screen.queryByText('notes.md')).not.toBeInTheDocument()
    expect(api.artifactSessionDocs).not.toHaveBeenCalled()
  })

  it('saving a sweepable widget pins it', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'w1', name: 'Widget One', kind: 'widget', pinned: false, auto_registered: true }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    fireEvent.click(await screen.findByTestId('artifact-save-w1'))
    await waitFor(() => {
      // The REAL session key travels so a restricted slot is gated on the pin too.
      expect(api.setArtifactPinned).toHaveBeenCalledWith('w1', true, 'dashboard:test-slot')
    })
    expect(api.materializeArtifact).not.toHaveBeenCalled()
  })

  it('shows the empty state when the session produced nothing and the library is empty', async () => {
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText(/No artifacts in this chat yet/)).toBeInTheDocument()
  })

  it('keeps a file-backed artifact this session only READ in the session section', async () => {
    // A file-backed artifact the agent merely read is still a real artifact
    // record and belongs under "This session" — the consumed-artifact case the
    // touched_by scan exists to surface.
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'spec-md', name: 'spec.md', kind: 'markdown', pinned: false, source_path: '/p/spec.md' }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    // Renders once, under the session header — not duplicated into the library.
    await waitFor(() => { expect(screen.getAllByText('spec.md')).toHaveLength(1) })
    expect(screen.queryByText(/Artifact library/)).not.toBeInTheDocument()
  })

  /* ── Library section (section B) ──────────────────────────────────────────
   * The tab is both a session view and a library browser. These lock in that
   * the library renders, and that an artifact is never listed in both places.
   */

  /** Route the two queries independently: the session section asks with
   *  `touchedBy`, the library section with `{}`. The default mock answers both
   *  with the same value, which is what would mask a de-dup regression. */
  function mockArtifactQueries(session: unknown[], library: unknown[]) {
    vi.mocked(api.artifacts).mockImplementation((filters?: { touchedBy?: string }) =>
      Promise.resolve({ artifacts: filters?.touchedBy ? session : library }) as never,
    )
  }

  it('surfaces a library artifact by search, de-duped against the session', async () => {
    mockArtifactQueries(
      [{ slug: 'mine', name: 'Made Here', kind: 'widget', pinned: false }],
      [
        { slug: 'mine', name: 'Made Here', kind: 'widget', pinned: false },
        { slug: 'older', name: 'From Last Week', kind: 'markdown', pinned: true },
      ],
    )
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    await findSection('This session', 1)
    // The library is no longer mirrored inline — the "From your library" header
    // carries no count and no rows until the user searches.
    expect(screen.getByText('From your library')).toBeInTheDocument()
    expect(screen.queryByText('From Last Week')).not.toBeInTheDocument()
    // Typing a query surfaces the matching library artifact.
    fireEvent.change(screen.getByPlaceholderText('Search your library…'), { target: { value: 'last week' } })
    expect(await screen.findByText('From Last Week')).toBeInTheDocument()
    // 'Made Here' stays once (session only) — the session item is excluded from
    // library search results.
    expect(screen.getAllByText('Made Here')).toHaveLength(1)
  })

  it('shows the empty hero and the library bridge when the session touched nothing', async () => {
    mockArtifactQueries([], [{ slug: 'older', name: 'From Last Week', kind: 'markdown', pinned: true }])
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    // A fresh session shows the empty hero + "From your library", not an empty
    // "This session" heading.
    expect(await screen.findByText(/No artifacts in this chat yet/)).toBeInTheDocument()
    expect(screen.getByText('From your library')).toBeInTheDocument()
    expect(screen.queryByText(/^This session/)).not.toBeInTheDocument()
  })

  it('caps library search results and shows a no-matches state', async () => {
    const many = Array.from({ length: 55 }, (_, i) => ({
      slug: `a${i}`, name: `Artifact ${i}`, kind: 'widget', pinned: false,
    }))
    mockArtifactQueries([], many)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    // Nothing is listed until the user searches.
    expect(await screen.findByText('From your library')).toBeInTheDocument()
    expect(screen.queryByText('Artifact 0')).not.toBeInTheDocument()
    const box = screen.getByPlaceholderText('Search your library…')
    // A broad query matches more than the cap; at most 20 rows render.
    fireEvent.change(box, { target: { value: 'artifact' } })
    await waitFor(() => {
      expect(screen.getAllByText(/^Artifact \d+$/).length).toBe(20)
    })
    // A query matching nothing shows the no-matches state.
    fireEvent.change(box, { target: { value: 'zzz-nope' } })
    expect(await screen.findByText('No matches')).toBeInTheDocument()
  })

  it('links to the full artifacts library', async () => {
    mockArtifactQueries([], [{ slug: 'older', name: 'From Last Week', kind: 'markdown', pinned: true }])
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    // The browse control carries the total library count and routes to /artifacts.
    expect(await screen.findByText(/Browse all \(1\)/)).toBeInTheDocument()
  })

  /* ── Companion binding (requirement: the association must persist) ─────── */

  /** Provider tree with a store the test keeps, so slots can be seeded. The
   *  shared routerWrapper builds its store inline and hands back no handle. */
  function renderWithSlots(slots: unknown[]) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    store.dispatch(sseSlots(slots as never))
    return render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter><ActivityViewer {...artifactProps} /></MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
  }

  it('lists the bound companion artifact under This session, untouched', async () => {
    // A session started from an artifact's detail page carries slot.artifact
    // (persisted in the history meta line). The binding alone is the
    // association, so the artifact belongs in the session section even though
    // the agent never read or edited it — touched_by returns nothing here.
    mockArtifactQueries([], [{ slug: 'cr-queue', name: 'CR Queue', kind: 'widget', pinned: true }])
    renderWithSlots([{ key: 'test-slot', title: 'Artifact: CR Queue', messages: 2, running: false, artifact: 'cr-queue' }])
    await findSection('This session', 1)
    expect(screen.getAllByText('CR Queue')).toHaveLength(1)
    // Pulled up into the session section, so the library section is now empty.
    expect(screen.queryByText(/^Artifact library/)).not.toBeInTheDocument()
  })

  it('does not double-list a bound artifact the session also touched', async () => {
    mockArtifactQueries(
      [{ slug: 'cr-queue', name: 'CR Queue', kind: 'widget', pinned: true }],
      [{ slug: 'cr-queue', name: 'CR Queue', kind: 'widget', pinned: true }],
    )
    renderWithSlots([{ key: 'test-slot', title: 'Artifact: CR Queue', messages: 2, running: false, artifact: 'cr-queue' }])
    await findSection('This session', 1)
    expect(screen.getAllByText('CR Queue')).toHaveLength(1)
  })

  it('ignores a binding whose artifact no longer exists', async () => {
    // The slot keeps its binding after the artifact is deleted; there is no
    // metadata to render, so the row is skipped rather than faked.
    mockArtifactQueries([], [])
    renderWithSlots([{ key: 'test-slot', title: 'Artifact: Gone', messages: 1, running: false, artifact: 'deleted-slug' }])
    expect(await screen.findByText(/No artifacts in this chat yet/)).toBeInTheDocument()
  })

  // ── Row click routes into the side panel, not a full-page navigation ───────
  //
  // The whole point of the panel tab: clicking an artifact opens it inline in
  // the side panel instead of hard-navigating to /artifacts/<slug>, which would
  // tear down the chat to show a document the panel can render inline. Files
  // never did that, so without this artifacts would be the only panel-capable
  // content you could not flip between like a file.
  it('opens an artifact row through onArtifactOpen instead of navigating', async () => {
    const onArtifactOpen = vi.fn()
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'cr-queue', name: 'CR Queue', kind: 'widget', pinned: false }],
    } as never)
    render(<ActivityViewer {...artifactProps} onArtifactOpen={onArtifactOpen} />, { wrapper: routerWrapper })
    fireEvent.click(await screen.findByText('CR Queue'))
    expect(onArtifactOpen).toHaveBeenCalledWith('cr-queue')
  })

  it('routes a library search-result row through onArtifactOpen too', async () => {
    // A library artifact surfaced by search is the same artifact row, so it must
    // route through onArtifactOpen (open in panel), not navigate().
    const onArtifactOpen = vi.fn()
    mockArtifactQueries([], [{ slug: 'old-doc', name: 'Old Doc', kind: 'markdown', pinned: false }])
    render(<ActivityViewer {...artifactProps} onArtifactOpen={onArtifactOpen} />, { wrapper: routerWrapper })
    fireEvent.change(await screen.findByPlaceholderText('Search your library…'), { target: { value: 'old doc' } })
    fireEvent.click(await screen.findByText('Old Doc'))
    expect(onArtifactOpen).toHaveBeenCalledWith('old-doc')
  })

  it('never routes an Artifacts-tab row to the file opener', async () => {
    // The tab no longer holds anything path-addressable, so nothing here can
    // reach onFileOpen — that is the Files tab's job.
    const onArtifactOpen = vi.fn()
    const onFileOpen = vi.fn()
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'notes-md', name: 'notes.md', kind: 'markdown', pinned: true, source_path: '/p/notes.md' }],
    } as never)
    render(
      <ActivityViewer {...artifactProps} onFileOpen={onFileOpen} onArtifactOpen={onArtifactOpen} />,
      { wrapper: routerWrapper },
    )
    fireEvent.click(await screen.findByText('notes.md'))
    expect(onArtifactOpen).toHaveBeenCalledWith('notes-md')
    expect(onFileOpen).not.toHaveBeenCalled()
  })

  it('falls back to the detail page when no panel host is supplied', async () => {
    // The tab can render outside a chat, where there is no panel to open into.
    // Without a fallback the row would be a dead click. MemoryRouter never
    // touches window.location, so the routed path is read from the router.
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'cr-queue', name: 'CR Queue', kind: 'widget', pinned: false }],
    } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    function LocationProbe() {
      return <span data-testid="path">{useLocation().pathname}</span>
    }
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>
            <ActivityViewer {...artifactProps} />
            <LocationProbe />
          </MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByText('CR Queue'))
    await waitFor(() => {
      expect(screen.getByTestId('path')).toHaveTextContent('/artifacts/cr-queue')
    })
  })

  // Layout: in the narrow activity rail, if every header item were shrinkable
  // "Subagent Running Tool", the elapsed clock and the Cancel button would all
  // wrap onto two lines and blow the card's header height up. The status label
  // is the last thing to give way (the agent chip yields first); the clock and
  // Cancel button must hold their single line.
  it('keeps the subagent card header on one line in a narrow rail', () => {
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    store.dispatch(openActivityToTab('subagents'))
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ActivityViewer
            toolLog={[]}
            open
            onToggle={vi.fn()}
            slot="test-slot"
            subagents={{
              s1: {
                id: 's1', task: 'READ-ONLY RESEARCH', agent: 'kirocrew', status: 'tool',
                streaming: '', lastTool: 'read', startedAt: Date.now() - 239_000, elapsed: 0,
              },
            }}
          />
        </QueryClientProvider>
      </Provider>,
    )

    // The status is the informative half, so it is what the header shows; the
    // full phrase stays reachable as the tooltip.
    const title = screen.getByText('Running Tool')
    expect(title).toHaveAttribute('title', 'Subagent Running Tool')
    expect(title.className).toContain('truncate')
    expect(title.className).toContain('min-w-0')

    // Agent chip: yields BEFORE the status label (weighted shrink) and capped,
    // so a long agent name can neither wrap nor starve the label, the clock and
    // the Cancel button.
    const chip = screen.getByText('kirocrew')
    expect(chip.className).toContain('shrink-[3]')
    expect(chip.className).toContain('truncate')
    expect(chip.className).toContain('min-w-0')

    const clock = screen.getByText('3m 59s')
    expect(clock.className).toContain('shrink-0')
    expect(clock.className).toContain('whitespace-nowrap')

    const cancel = screen.getByTestId('subagent-cancel-btn')
    expect(cancel.className).toContain('shrink-0')
    expect(cancel.className).toContain('whitespace-nowrap')
  })
})

/**
 * Queued-wave visibility. A spawn_run wave accepted but still behind the
 * concurrency cap / stagger gate emits `subagent_queued` and NOTHING else — no
 * per-agent entry exists yet. Rendering "No subagents running" for that entire
 * window is flatly false — the single most misleading state the panel can show.
 */
describe('ActivityViewer — queued subagents', () => {
  const SLOT = 'test-slot'
  const baseProps = { subagents: {}, toolLog: [], open: true, onToggle: vi.fn(), slot: SLOT }

  function queuedWrapper(queued: number) {
    return function Wrapper({ children }: { children: React.ReactNode }) {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const store = configureStore({
        reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
      })
      if (queued > 0) store.dispatch(sseSubagentQueued({ slot: SLOT, queued }))
      return (
        <Provider store={store}>
          <QueryClientProvider client={qc}>{children}</QueryClientProvider>
        </Provider>
      )
    }
  }

  it('announces agents waiting to start instead of claiming none are running', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, { wrapper: queuedWrapper(3) })
    expect(screen.getByTestId('subagent-queued-banner').textContent).toContain('3 waiting to start')
    expect(screen.queryByText('No subagents running')).not.toBeInTheDocument()
  })

  it('keeps the honest empty state when nothing is queued or running', () => {
    render(<ActivityViewer {...baseProps} view="subagents" />, { wrapper: queuedWrapper(0) })
    expect(screen.getByText('No subagents running')).toBeInTheDocument()
    expect(screen.queryByTestId('subagent-queued-banner')).not.toBeInTheDocument()
  })
})

// ── Section headers are shared across tabs ──────────────────────────────────
//
// The Files and Artifacts tabs sit behind adjacent buttons in the same panel, so
// a user flipping between them sees both headers within a second of each other.
// They used to be hand-rolled separately and had drifted apart on case, size,
// weight, colour, divider, and whether the count was a node or punctuation in
// the label — one panel that looked like two. Both now route through
// PanelSectionHeader; these tests fail if either tab grows its own idiom again.
describe('ActivityViewer — panel section headers', () => {
  function panelWrapper({ children }: { children: React.ReactNode }) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    return (
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
      </Provider>
    )
  }

  const headerProps = { subagents: {}, toolLog: [], open: true, onToggle: vi.fn(), slot: 'test-slot' }

  beforeEach(() => {
    vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] })
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({ docs: [] })
  })

  it('renders the Links tab group through the shared header', () => {
    render(
      <ActivityViewer
        {...headerProps}
        view="links"
        navLinks={[{ url: 'https://example.com/x', type: 'other', label: 'Notes', msgIdx: 0 }]}
      />,
      { wrapper: panelWrapper },
    )
    expect(sectionHeaders()).toEqual([['Resources', '1']])
  })

  it('renders the Artifacts tab groups through the shared header, count outside the label', async () => {
    vi.mocked(api.artifacts).mockImplementation((params?: { touchedBy?: string }) =>
      Promise.resolve({
        artifacts: params?.touchedBy
          ? [{ slug: 'w1', name: 'Widget One', kind: 'widget', pinned: false }]
          : [{ slug: 'w1', name: 'Widget One', kind: 'widget', pinned: false },
             { slug: 'lib1', name: 'Kept Doc', kind: 'markdown', pinned: true }],
      }) as never)
    render(<ActivityViewer {...headerProps} view="artifacts" />, { wrapper: panelWrapper })
    await findSection('This session', 1)
    // "This session" carries its count as a sibling node; "From your library" is
    // a countless header (the hairline rule sits where a count would).
    expect(sectionHeaders()).toEqual([['This session', '1'], ['From your library', null]])
    expect(screen.queryByText(/\(1\)/)).not.toBeInTheDocument()
  })
})
