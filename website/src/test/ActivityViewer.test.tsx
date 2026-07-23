import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

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
import { openActivityToTab } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    browseFiles: vi.fn().mockResolvedValue({ path: '/projects/foo', parent: '/', dirs: [], files: [] }),
    pullRequestSource: vi.fn().mockImplementation(() => new Promise(() => {})),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
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
