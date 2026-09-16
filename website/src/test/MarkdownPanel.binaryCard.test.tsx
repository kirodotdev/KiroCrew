/**
 * The panel-level gate: a file `/api/file-read` refused to decode renders the
 * binary card INSTEAD of the Pierre editor, and offers no way to edit or diff a
 * buffer that was never decoded.
 *
 * A `.bin` file is the interesting case precisely because it is not a rich type:
 * `detectFileType` maps it to `'code'`, whose default view IS the editor, so
 * nothing about the file's name keeps the editor away — only the verdict does.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { createTestStore } from './helpers'
import MarkdownPanel from '../components/MarkdownPanel'

// Pierre owns a virtualizer and a worker pool — neither is the subject here, and
// a stub makes "did the editor render at all" a direct assertion.
vi.mock('../pierre', () => ({
  PierreCode: () => <div data-testid="pierre-code" />,
  PierreEditor: () => <div data-testid="pierre-editor" />,
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))
vi.mock('../components/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="pierre-editor" />,
}))

let lastQueryClient: QueryClient | undefined

function renderPanel(props: {
  filePath: string
  content: string
  binary?: boolean
  onDiskContent?: (c: string, binary?: boolean) => void
}) {
  const store = createTestStore()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  lastQueryClient = queryClient
  const panel = (panelProps: typeof props) => (
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <MarkdownPanel
            embedded
            filePath={panelProps.filePath}
            content={panelProps.content}
            binary={panelProps.binary}
            onDiskContent={panelProps.onDiskContent}
            onContentChange={vi.fn()}
            onSave={vi.fn()}
            onClose={vi.fn()}
          />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(panel(props))
  return { ...view, rerenderPanel: (nextProps: typeof props) => view.rerender(panel(nextProps)) }
}

describe('MarkdownPanel binary gate', () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => null },
        text: () => Promise.resolve(''),
        json: () => Promise.resolve({}),
      } as unknown as Response),
    ) as unknown as typeof fetch
  })

  it('renders the card and never mounts the editor for an undecodable file', () => {
    renderPanel({ filePath: '/home/user/data/index.sqlite', content: '', binary: true })
    expect(screen.getByTestId('binary-file-card')).toBeInTheDocument()
    expect(screen.queryByTestId('pierre-editor')).toBeNull()
    expect(screen.queryByTestId('pierre-code')).toBeNull()
  })

  it('offers neither Edit nor a diff toggle for it', () => {
    renderPanel({ filePath: '/home/user/data/index.sqlite', content: '', binary: true })
    // Saving would write replacement characters over the real bytes, and a
    // source diff of an undecoded file compares nothing to nothing.
    expect(screen.queryByRole('button', { name: 'Edit' })).toBeNull()
    expect(screen.queryByRole('button', { name: /toggle diff view/i })).toBeNull()
  })

  it('shows the card for a binary .json, whose viewer reads the buffer', () => {
    // `.json` is a rich type, but `JsonViewer` renders FROM the content string —
    // and for an undecodable file that string is empty, so exempting every rich
    // type would hand it a blank tree. Only the byte-backed viewers (image, pdf,
    // sheet, office, video, audio) can skip the card, because they fetch the
    // real bytes from the path.
    renderPanel({ filePath: '/home/user/data/cache.json', content: '', binary: true })
    expect(screen.getByTestId('binary-file-card')).toBeInTheDocument()
  })

  it('leaves a byte-backed viewer alone — an image is binary and renders fine', () => {
    // The control for the rule above: `ImageViewer` reads /api/file-raw from the
    // path, so replacing it with a download card would be a regression.
    renderPanel({ filePath: '/home/user/pics/shot.png', content: '', binary: true })
    expect(screen.queryByTestId('binary-file-card')).toBeNull()
    expect(screen.getByAltText('shot.png')).toBeInTheDocument()
  })

  it('withdraws the add-to-artifacts affordance for an undecodable file', () => {
    // Promotion re-reads the file and stores `res.text()`; on a binary read that
    // text is the JSON envelope, and an artifact is COPIED, so the wrong content
    // would be permanent.
    renderPanel({ filePath: '/home/user/data/index.sqlite', content: '', binary: true })
    expect(screen.queryByRole('button', { name: /artifact/i })).toBeNull()
  })

  it('withdraws Snapshot for a linked artifact when the file is binary', async () => {
    const filePath = '/home/user/notes.md'
    renderPanel({ filePath, content: '', binary: true })
    const queryKey = ['artifact-by-source-path', filePath]
    await lastQueryClient!.cancelQueries({ queryKey })
    lastQueryClient!.setQueryData(queryKey, {
      slug: 'linked-notes',
      name: 'notes.md',
      live_dirty: false,
      pinned: false,
    })

    await userEvent.click(screen.getByTestId('markdown-panel-more-options'))
    expect(await screen.findByRole('menuitem', { name: /in artifacts/i })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /snapshot/i })).toBeNull()
  })

  it('reports the verdict with the content when Refresh finds binary bytes', async () => {
    // The stale-verdict case. A text file open in the panel is replaced on disk
    // by binary bytes (a build output, a checkout, an agent write). Refresh
    // patches the buffer, so it must also be able to change the card decision —
    // otherwise the editor stays live over bytes it cannot represent and a save
    // overwrites them.
    const onDiskContent = vi.fn()
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/file-read')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          headers: { get: (h: string) => (h === 'X-File-Binary' ? 'true' : null) },
          text: () => Promise.resolve('{"binary": true}'),
        } as unknown as Response)
      }
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        text: () => Promise.resolve(''), json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch

    renderPanel({ filePath: '/home/user/notes.md', content: '# notes', onDiskContent })
    await userEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await userEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(onDiskContent).toHaveBeenCalled())
    // Empty buffer, not the envelope JSON, AND the verdict alongside it.
    expect(onDiskContent).toHaveBeenCalledWith('', true)
  })

  it('reports a text refresh as not-binary, so a stale true cannot stick', async () => {
    // The other direction: a file that WAS binary and is now text must clear the
    // verdict, or the card outlives the bytes that justified it.
    const onDiskContent = vi.fn()
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/file-read')) {
        return Promise.resolve({
          ok: true, status: 200,
          headers: { get: () => null },
          text: () => Promise.resolve('# now text'),
        } as unknown as Response)
      }
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        text: () => Promise.resolve(''), json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch

    renderPanel({ filePath: '/home/user/notes.md', content: '# old', onDiskContent })
    await userEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await userEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(onDiskContent).toHaveBeenCalled())
    expect(onDiskContent).toHaveBeenCalledWith('# now text', false)
  })

  it('reports a failed Refresh instead of doing nothing visible', async () => {
    // A click that produces no change reads as a broken button. The watch stays
    // quiet on the same failure by design -- nobody asked it for anything.
    const onDiskContent = vi.fn()
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/file-read')) {
        return Promise.resolve({ ok: false, status: 500, headers: { get: () => null } } as unknown as Response)
      }
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        text: () => Promise.resolve(''), json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch

    renderPanel({ filePath: '/home/user/notes.md', content: '# notes', onDiskContent })
    await userEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await userEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    expect(await screen.findByText('Cannot read file')).toBeInTheDocument()
    // And the buffer is untouched: a partial application is the corruption the
    // whole read path exists to prevent.
    expect(onDiskContent).not.toHaveBeenCalled()
  })

  it('a disk read still in flight when the panel unmounts applies nothing', async () => {
    const filePath = '/home/user/notes.md'
    const onDiskContent = vi.fn()
    let resolveRead!: (response: Response) => void
    let signal: AbortSignal | undefined
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/api/file-read')) {
        signal = init?.signal
        return new Promise<Response>((resolve, reject) => {
          resolveRead = resolve
          signal?.addEventListener(
            'abort',
            () => reject(new DOMException('aborted', 'AbortError')),
            { once: true },
          )
        })
      }
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        text: () => Promise.resolve(''), json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch
    globalThis.fetch = fetchMock

    const view = renderPanel({ filePath, content: '# notes', onDiskContent })
    await userEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await userEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    view.unmount()
    resolveRead({
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: () => Promise.resolve('# stale text'),
    } as unknown as Response)
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(onDiskContent).not.toHaveBeenCalled()
    expect(signal?.aborted).toBe(true)
  })

  it('a disk read started for one path applies nothing after the panel moves to another', async () => {
    const oldPath = '/home/user/a.md'
    const onDiskContent = vi.fn()
    let resolveRead!: (response: Response) => void
    let signal: AbortSignal | undefined
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/api/file-read')) {
        signal = init?.signal
        return new Promise<Response>((resolve, reject) => {
          resolveRead = resolve
          signal?.addEventListener(
            'abort',
            () => reject(new DOMException('aborted', 'AbortError')),
            { once: true },
          )
        })
      }
      return Promise.resolve({
        ok: true, status: 200, headers: { get: () => null },
        text: () => Promise.resolve(''), json: () => Promise.resolve({}),
      } as unknown as Response)
    }) as unknown as typeof fetch
    globalThis.fetch = fetchMock

    const view = renderPanel({ filePath: oldPath, content: '# a', onDiskContent })
    await userEvent.click(screen.getByTestId('markdown-panel-more-options'))
    await userEvent.click(screen.getByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    view.rerenderPanel({ filePath: '/home/user/b.md', content: '# b', onDiskContent })
    resolveRead({
      ok: true,
      status: 200,
      headers: { get: () => null },
      text: () => Promise.resolve('# stale text'),
    } as unknown as Response)
    await new Promise(resolve => setTimeout(resolve, 0))

    expect(onDiskContent).not.toHaveBeenCalled()
    expect(signal?.aborted).toBe(true)
  })

  it('still opens the editor for a same-extension-family TEXT file', () => {
    // The control: nothing about `.bin` decides this, so a file the sniff let
    // through must keep the editor it had before.
    renderPanel({ filePath: '/home/user/notes.bin', content: 'plain text' })
    expect(screen.queryByTestId('binary-file-card')).toBeNull()
    expect(screen.getByTestId('pierre-editor')).toBeInTheDocument()
  })
})
