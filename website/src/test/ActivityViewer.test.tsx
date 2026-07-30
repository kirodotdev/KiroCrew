import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

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
  },
}))

// MarkdownPanel pulls in Monaco + a large renderer tree; stub it so the
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
    // Files tab is the default
    store.dispatch(openActivityToTab('files'))
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

  it('search filters both files and links, with a no-matches state', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="files"
        // >5 total entries, so the search box clears its display threshold.
        files={[
          { path: '/proj/alpha.md', source: 'tool' },
          { path: '/proj/beta.ts', source: 'tool' },
          { path: '/proj/gamma.ts', source: 'tool' },
          { path: '/proj/delta.ts', source: 'tool' },
          { path: '/proj/epsilon.ts', source: 'tool' },
        ]}
        navLinks={[{ url: 'https://example.com/alpha-notes', type: 'other', label: 'Alpha notes', msgIdx: 0 }]}
        onFileOpen={vi.fn()}
      />,
      { wrapper },
    )
    expect(screen.getByText('Changed files')).toBeInTheDocument()
    expect(screen.getByText('Resources')).toBeInTheDocument()

    // Typing filters ACROSS both sections (path match + link label match).
    const box = screen.getByLabelText('Search by file name, folder, or link…')
    fireEvent.change(box, { target: { value: 'alpha' } })
    expect(screen.getByText('alpha.md')).toBeInTheDocument()
    expect(screen.getByText('Alpha notes')).toBeInTheDocument()
    expect(screen.queryByText('beta.ts')).not.toBeInTheDocument()

    // A query matching nothing shows the no-matches state, not the empty state.
    fireEvent.change(box, { target: { value: 'zzz-no-such-thing' } })
    expect(screen.getByText('No matches')).toBeInTheDocument()
    expect(screen.queryByText('No files changed yet')).not.toBeInTheDocument()

    // Clearing restores everything.
    fireEvent.change(box, { target: { value: '' } })
    expect(screen.getByText('beta.ts')).toBeInTheDocument()
  })

  it('keeps the search box mounted while a query is active, even below the threshold', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="files"
        files={[
          { path: '/proj/alpha.md', source: 'tool' },
          { path: '/proj/beta.ts', source: 'tool' },
          { path: '/proj/gamma.ts', source: 'tool' },
          { path: '/proj/delta.ts', source: 'tool' },
          { path: '/proj/epsilon.ts', source: 'tool' },
          { path: '/proj/zeta.ts', source: 'tool' },
        ]}
        onFileOpen={vi.fn()}
      />,
      { wrapper },
    )
    const label = 'Search by file name, folder, or link…'
    const box = screen.getByLabelText(label)
    // Filtering down to ONE match takes the visible count below the 5-entry
    // threshold. The box must NOT unmount — otherwise the stale query keeps
    // filtering with no input left to clear it, and the hidden rows read as lost.
    fireEvent.change(box, { target: { value: 'alpha' } })
    expect(screen.getByLabelText(label)).toBeInTheDocument()
    expect(screen.getByText('alpha.md')).toBeInTheDocument()
    expect(screen.queryByText('beta.ts')).not.toBeInTheDocument()
    // Clearing brings everything back.
    fireEvent.change(screen.getByLabelText(label), { target: { value: '' } })
    expect(screen.getByText('beta.ts')).toBeInTheDocument()
  })

  it('hides the search box for a short list (nothing to filter yet)', async () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="files"
        files={[{ path: '/proj/alpha.md', source: 'tool' }]}
        onFileOpen={vi.fn()}
      />,
      { wrapper },
    )
    // The list renders, but a 1-item list is faster to scan than to filter.
    expect(screen.getByText('alpha.md')).toBeInTheDocument()
    expect(screen.queryByLabelText('Search by file name, folder, or link…')).not.toBeInTheDocument()
  })

  it('Files tab opens a file inline with a back button, not a new tab', async () => {
    const onFileOpen = vi.fn()
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => 'hello world', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    try {
      render(
        <ActivityViewer
          {...baseProps}
          view="files"
          files={[{ path: '/proj/a.md', source: 'tool' }]}
          onFileOpen={onFileOpen}
          onFileSave={vi.fn().mockResolvedValue(undefined)}
        />,
        { wrapper },
      )
      // Starts on the list.
      expect(screen.getByText('Changed files')).toBeInTheDocument()

      // Clicking the file swaps the list for the inline preview — no tab opened.
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      expect(await screen.findByTestId('md-panel', {}, { timeout: 3000 })).toHaveTextContent('/proj/a.md::hello world')
      expect(onFileOpen).not.toHaveBeenCalled()
      expect(screen.queryByText('Changed files')).not.toBeInTheDocument()

      // Back returns to the list.
      fireEvent.click(screen.getByRole('button', { name: 'Back to files' }))
      expect(screen.getByText('Changed files')).toBeInTheDocument()
      expect(screen.queryByTestId('md-panel')).not.toBeInTheDocument()
    } finally {
      global.fetch = prevFetch
    }
  })

  it('preserves the open inline file across chat-slot switches (like a document tab)', async () => {
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => 'contents', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    try {
      const props = (slot: string) => ({
        ...baseProps,
        slot,
        view: 'files' as const,
        files: [{ path: '/proj/a.md', source: 'tool' as const }],
        onFileOpen: vi.fn(),
        onFileSave: vi.fn().mockResolvedValue(undefined),
      })
      const { rerender } = render(<ActivityViewer {...props('slot-A')} />, { wrapper })
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      expect(await screen.findByTestId('md-panel', {}, { timeout: 3000 })).toHaveTextContent('/proj/a.md')
      // Switching chat slots keeps the open file mounted (not slot-scoped) — no
      // discard, no remount — exactly like a persistent document-tab editor.
      rerender(<ActivityViewer {...props('slot-B')} />)
      expect(screen.getByTestId('md-panel')).toHaveTextContent('/proj/a.md')
    } finally {
      global.fetch = prevFetch
    }
  })

  it('shows a retryable error instead of an editable panel when the read fails', async () => {
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: false, status: 404, text: async () => 'ignored', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    try {
      render(
        <ActivityViewer
          {...baseProps}
          view="files"
          files={[{ path: '/proj/gone.md', source: 'tool' }]}
          onFileOpen={vi.fn()}
          onFileSave={vi.fn().mockResolvedValue(undefined)}
        />,
        { wrapper },
      )
      fireEvent.click(screen.getByTitle('/proj/gone.md'))
      // A failed read must NOT mount the editor (saving would overwrite the file
      // with the placeholder) — it offers a retry instead.
      expect(await screen.findByRole('button', { name: 'Retry' }, { timeout: 3000 })).toBeInTheDocument()
      expect(screen.queryByTestId('md-panel')).not.toBeInTheDocument()
    } finally {
      global.fetch = prevFetch
    }
  })

  it('shows a retryable error (not an editor) when the read REJECTS at the network level', async () => {
    const prevFetch = global.fetch
    // fetch rejects (offline / DNS / abort) — the query would otherwise leave
    // `data` undefined and fall through to an editor over an empty buffer.
    global.fetch = vi.fn().mockRejectedValue(new Error('network down')) as unknown as typeof fetch
    try {
      render(
        <ActivityViewer
          {...baseProps}
          view="files"
          files={[{ path: '/proj/a.md', source: 'tool' }]}
          onFileOpen={vi.fn()}
          onFileSave={vi.fn().mockResolvedValue(undefined)}
        />,
        { wrapper },
      )
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      expect(await screen.findByRole('button', { name: 'Retry' }, { timeout: 3000 })).toBeInTheDocument()
      expect(screen.queryByTestId('md-panel')).not.toBeInTheDocument()
    } finally {
      global.fetch = prevFetch
    }
  })

  it('Escape collapses the panel on the list, but defers to the editor when a file is open', async () => {
    const onToggle = vi.fn()
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => 'x', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    try {
      render(
        <ActivityViewer
          {...baseProps}
          onToggle={onToggle}
          view="files"
          files={[{ path: '/proj/a.md', source: 'tool' }]}
          onFileOpen={vi.fn()}
          onFileSave={vi.fn().mockResolvedValue(undefined)}
        />,
        { wrapper },
      )
      const region = screen.getByRole('region', { name: 'Activity' })
      // On the list, Escape collapses the panel.
      fireEvent.keyDown(region, { key: 'Escape' })
      expect(onToggle).toHaveBeenCalledTimes(1)
      // Open a file inline — now Escape must NOT collapse; it defers to the
      // editor's own guarded close (which returns to the list / prompts).
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      await screen.findByTestId('md-panel', {}, { timeout: 3000 })
      fireEvent.keyDown(region, { key: 'Escape' })
      expect(onToggle).toHaveBeenCalledTimes(1) // unchanged — no collapse
    } finally {
      global.fetch = prevFetch
    }
  })

  it('refreshes the shared file-read cache on save so reopening shows saved content', async () => {
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => 'orig', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    try {
      render(
        <ActivityViewer
          {...baseProps}
          view="files"
          files={[{ path: '/proj/a.md', source: 'tool' }]}
          onFileOpen={vi.fn()}
          onFileSave={vi.fn().mockResolvedValue(undefined)}
        />,
        { wrapper },
      )
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      expect(await screen.findByTestId('md-panel', {}, { timeout: 3000 })).toHaveTextContent('/proj/a.md::orig')
      // Save via the editor, then wait for the cache write.
      fireEvent.click(screen.getByTestId('md-save'))
      await waitFor(() => expect(screen.getByTestId('md-panel')).toBeInTheDocument())
      await new Promise(r => setTimeout(r, 20))
      // Back to the list, reopen — seeds from the REFRESHED cache (saved
      // content), not the stale pre-save disk read.
      fireEvent.click(screen.getByRole('button', { name: 'Back to files' }))
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      expect(await screen.findByTestId('md-panel', {}, { timeout: 3000 })).toHaveTextContent('/proj/a.md::SAVED')
    } finally {
      global.fetch = prevFetch
    }
  })

  it('preserves an in-progress inline edit across an unmount (store-backed draft)', async () => {
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => 'orig', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    const props = {
      ...baseProps,
      view: 'files' as const,
      files: [{ path: '/proj/a.md', source: 'tool' as const }],
      onFileOpen: vi.fn(),
      onFileSave: vi.fn().mockResolvedValue(undefined),
    }
    try {
      const { unmount } = render(<ActivityViewer {...props} />, { wrapper })
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      await screen.findByTestId('md-panel', {}, { timeout: 3000 })
      fireEvent.click(screen.getByTestId('md-edit')) // edit -> draft in module store
      // Unmount the whole panel (models close / auto-collapse) WITHOUT a guard.
      unmount()
      // Remount fresh and reopen the same file: the edit is recovered from the
      // store-backed draft (survived the unmount), not re-seeded from disk.
      render(<ActivityViewer {...props} />, { wrapper })
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      expect(await screen.findByTestId('md-panel', {}, { timeout: 3000 })).toHaveTextContent('/proj/a.md::EDITED')
    } finally {
      global.fetch = prevFetch
    }
  })

  it('routes to the existing document tab instead of a second inline editor (one per path)', async () => {
    const onFileOpen = vi.fn()
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => 'x', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    try {
      render(
        <ActivityViewer
          {...baseProps}
          view="files"
          files={[{ path: '/proj/a.md', source: 'tool' }]}
          onFileOpen={onFileOpen}
          onFileSave={vi.fn().mockResolvedValue(undefined)}
          openDocPaths={new Set(['/proj/a.md'])}
        />,
        { wrapper },
      )
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      // Already open as a document tab → focus that tab; no inline editor opens.
      expect(onFileOpen).toHaveBeenCalledWith('/proj/a.md')
      expect(screen.queryByTestId('md-panel')).not.toBeInTheDocument()
      expect(screen.getByText('Changed files')).toBeInTheDocument()
    } finally {
      global.fetch = prevFetch
    }
  })

  it('drives the lifted preview state when controlled (onPreviewPathChange provided)', async () => {
    const onPreviewPathChange = vi.fn()
    const prevFetch = global.fetch
    global.fetch = vi.fn().mockResolvedValue({
      ok: true, status: 200, text: async () => 'x', json: async () => ({ runs: [] }),
    }) as unknown as typeof fetch
    try {
      render(
        <ActivityViewer
          {...baseProps}
          view="files"
          files={[{ path: '/proj/a.md', source: 'tool' }]}
          onFileOpen={vi.fn()}
          onFileSave={vi.fn().mockResolvedValue(undefined)}
          previewPath={null}
          onPreviewPathChange={onPreviewPathChange}
        />,
        { wrapper },
      )
      fireEvent.click(screen.getByTitle('/proj/a.md'))
      // Controlled: opening routes through the lifted setter (ChatPage owns the
      // state and coordinates one-editor-per-path); it does NOT mount inline off
      // internal state while the parent-controlled previewPath stays null.
      expect(onPreviewPathChange).toHaveBeenCalledWith('/proj/a.md')
      expect(screen.queryByTestId('md-panel')).not.toBeInTheDocument()
    } finally {
      global.fetch = prevFetch
    }
  })
})

// ── Artifacts tab: the widget-as-artifact merge ─────────────────────────────
//
// SessionArtifactsTab merges TWO inputs: real session-scoped artifacts (which is
// how auto-registered widgets surface — their HTML is inline in the message and
// never hits disk, so the file-backed scan below cannot see them) and the
// virtual session documents. These tests pin the merge, the dedup, and the star
// routing. SessionArtifactsTab uses useNavigate, so it needs a Router.
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
      artifacts: [{ slug: 'abc123', name: 'Sales Chart', kind: 'widget', pinned: false }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Sales Chart')).toBeInTheDocument()
    // Offers to star (hollow), since auto-registration leaves it unpinned.
    expect(screen.getByLabelText('Star Sales Chart')).toBeInTheDocument()
  })

  it('shows a pinned artifact as starred', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'abc123', name: 'Kept', kind: 'widget', pinned: true }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByLabelText('Unstar Kept')).toBeInTheDocument()
  })

  it('merges artifacts and session documents into one list', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'w1', name: 'Widget One', kind: 'widget', pinned: false }],
    } as never)
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({
      docs: [{ path: '/p/notes.md', name: 'notes.md', slug: '', saved: false, session_key: 'test-slot', session_title: '', updated_at: '', message_ts: '' }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Widget One')).toBeInTheDocument()
    expect(screen.getByText('notes.md')).toBeInTheDocument()
  })

  it('does not double-list a materialized doc that is also an artifact', async () => {
    // A materialized document is BOTH inputs. The path-aware doc row wins, so
    // clicking it can still open the file.
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'notes-md', name: 'notes.md', kind: 'markdown', pinned: true }],
    } as never)
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({
      docs: [{ path: '/p/notes.md', name: 'notes.md', slug: 'notes-md', saved: true, session_key: 'test-slot', session_title: '', updated_at: '', message_ts: '' }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    await waitFor(() => { expect(screen.getAllByText('notes.md')).toHaveLength(1) })
    // The surviving row is the doc row — it shows the path as its subtitle.
    expect(screen.getByText('/p/notes.md')).toBeInTheDocument()
  })

  it('starring an artifact row pins it (no materialize call)', async () => {
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'w1', name: 'Widget One', kind: 'widget', pinned: false }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    fireEvent.click(await screen.findByLabelText('Star Widget One'))
    await waitFor(() => {
      expect(api.setArtifactPinned).toHaveBeenCalledWith('w1', true)
    })
    expect(api.materializeArtifact).not.toHaveBeenCalled()
  })

  it('starring an unsaved document materializes it instead of pinning', async () => {
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({
      docs: [{ path: '/p/notes.md', name: 'notes.md', slug: '', saved: false, session_key: 'test-slot', session_title: '', updated_at: '', message_ts: '' }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    fireEvent.click(await screen.findByLabelText('Star notes.md'))
    await waitFor(() => {
      expect(api.materializeArtifact).toHaveBeenCalledWith('/p/notes.md', 'test-slot')
    })
  })

  it('does not double-list a materialized doc that was later UNSTARRED', async () => {
    // The session-docs backend maps path->slug from PINNED artifacts only, so an
    // unstarred materialized doc reports slug:'' — matching on slug alone would
    // let its artifact twin through as a second row with its own star.
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'notes-md', name: 'notes.md', kind: 'markdown', pinned: false, source_path: '/p/notes.md' }],
    } as never)
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({
      docs: [{ path: '/p/notes.md', name: 'notes.md', slug: '', saved: false, session_key: 'test-slot', session_title: '', updated_at: '', message_ts: '' }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    await waitFor(() => { expect(screen.getAllByText('notes.md')).toHaveLength(1) })
    // The surviving row is the path-aware doc row.
    expect(screen.getByText('/p/notes.md')).toBeInTheDocument()
  })

  it('shows the empty state when the session produced nothing and the library is empty', async () => {
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText(/No artifacts yet/)).toBeInTheDocument()
  })

  it('keeps a file-backed artifact this session only READ in the session section', async () => {
    // The doc-twin exclusion must join on the session's own doc paths, not on
    // "has a source_path at all". A file-backed artifact the agent merely read
    // is not one of this session's documents, so a blanket exclusion would
    // banish it to "Other artifacts" — dropping the consumed-artifact case the
    // touched_by scan exists to surface.
    vi.mocked(api.artifacts).mockResolvedValue({
      artifacts: [{ slug: 'spec-md', name: 'spec.md', kind: 'markdown', pinned: false, source_path: '/p/spec.md' }],
    } as never)
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({
      docs: [{ path: '/p/other.md', name: 'other.md', slug: '', saved: false, session_key: 'test-slot', session_title: '', updated_at: '', message_ts: '' }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    // Renders once, under the session header — not duplicated into the library.
    await waitFor(() => { expect(screen.getAllByText('spec.md')).toHaveLength(1) })
    expect(screen.queryByText(/Other artifacts/)).not.toBeInTheDocument()
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

  it('lists the whole library in its own section below the session', async () => {
    mockArtifactQueries(
      [{ slug: 'mine', name: 'Made Here', kind: 'widget', pinned: false }],
      [
        { slug: 'mine', name: 'Made Here', kind: 'widget', pinned: false },
        { slug: 'older', name: 'From Last Week', kind: 'markdown', pinned: true },
      ],
    )
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('This session (1)')).toBeInTheDocument()
    // The library section excludes the session's own artifact, so it counts 1,
    // not 2 — the de-dup is visible in the header count, not just the rows.
    expect(await screen.findByText('Other artifacts (1)')).toBeInTheDocument()
    expect(screen.getByText('From Last Week')).toBeInTheDocument()
    // 'Made Here' appears exactly once, in the session section.
    expect(screen.getAllByText('Made Here')).toHaveLength(1)
  })

  it('hides the session header entirely when the session touched nothing', async () => {
    mockArtifactQueries([], [{ slug: 'older', name: 'From Last Week', kind: 'markdown', pinned: true }])
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Other artifacts (1)')).toBeInTheDocument()
    // A fresh session shows the library alone, not an empty "This session" heading.
    expect(screen.queryByText(/^This session/)).not.toBeInTheDocument()
  })

  it('does not list a library twin of an unstarred materialized doc', async () => {
    // Same slug:'' state the section-A test covers, but from the library side:
    // the doc row claims no slug, so only a source_path join keeps its library
    // twin from rendering as a second copy of the same document.
    mockArtifactQueries(
      [],
      [{ slug: 'notes-md', name: 'notes.md', kind: 'markdown', pinned: false, source_path: '/p/notes.md' }],
    )
    vi.mocked(api.artifactSessionDocs).mockResolvedValue({
      docs: [{ path: '/p/notes.md', name: 'notes.md', slug: '', saved: false, session_key: 'test-slot', session_title: '', updated_at: '', message_ts: '' }],
    } as never)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    await waitFor(() => { expect(screen.getAllByText('notes.md')).toHaveLength(1) })
    expect(screen.getByText('/p/notes.md')).toBeInTheDocument()
  })

  it('caps the library list and reveals the rest on Show all', async () => {
    const many = Array.from({ length: 55 }, (_, i) => ({
      slug: `a${i}`, name: `Artifact ${i}`, kind: 'widget', pinned: false,
    }))
    mockArtifactQueries([], many)
    render(<ActivityViewer {...artifactProps} />, { wrapper: routerWrapper })
    expect(await screen.findByText('Other artifacts (55)')).toBeInTheDocument()
    // 50 rendered, 5 held back — the panel is ~460px wide, so the DOM is capped.
    expect(screen.queryByText('Artifact 54')).not.toBeInTheDocument()
    fireEvent.click(screen.getByText(/Show all \(5\)/))
    expect(await screen.findByText('Artifact 54')).toBeInTheDocument()
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
    expect(await screen.findByText('This session (1)')).toBeInTheDocument()
    expect(screen.getAllByText('CR Queue')).toHaveLength(1)
    // Pulled up into the session section, so the library section is now empty.
    expect(screen.queryByText(/^Other artifacts/)).not.toBeInTheDocument()
  })

  it('does not double-list a bound artifact the session also touched', async () => {
    mockArtifactQueries(
      [{ slug: 'cr-queue', name: 'CR Queue', kind: 'widget', pinned: true }],
      [{ slug: 'cr-queue', name: 'CR Queue', kind: 'widget', pinned: true }],
    )
    renderWithSlots([{ key: 'test-slot', title: 'Artifact: CR Queue', messages: 2, running: false, artifact: 'cr-queue' }])
    expect(await screen.findByText('This session (1)')).toBeInTheDocument()
    expect(screen.getAllByText('CR Queue')).toHaveLength(1)
  })

  it('ignores a binding whose artifact no longer exists', async () => {
    // The slot keeps its binding after the artifact is deleted; there is no
    // metadata to render, so the row is skipped rather than faked.
    mockArtifactQueries([], [])
    renderWithSlots([{ key: 'test-slot', title: 'Artifact: Gone', messages: 1, running: false, artifact: 'deleted-slug' }])
    expect(await screen.findByText(/No artifacts yet/)).toBeInTheDocument()
  })

  // Layout regression: in the narrow activity rail every header item used to be
  // shrinkable, so "Subagent Running Tool", the elapsed clock and the Cancel
  // button all wrapped onto two lines and blew the card's header height up.
  // The status label is the last thing to give way (the agent chip yields
  // first); the clock and Cancel button must hold their single line.
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
 * per-agent entry exists yet. The panel used to render "No subagents running"
 * for that entire window, which is flatly false and was the single most
 * misleading state it had.
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
