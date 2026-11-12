/**
 * The full-screen file view's icon-only refresh is disabled while its re-read
 * runs. A disabled "Refresh file" reads as unavailable with no reason, so its
 * name and tooltip say it is busy then, and go back when the read settles.
 * The file read is held open by the fetch stub; nothing else moves.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreCode: () => <div data-testid="pierre-code" />,
  PierreEditor: () => <div data-testid="pierre-editor" />,
  PierreFilePair: () => <div data-testid="pierre-diff" />,
}))
vi.mock('../components/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="pierre-editor" />,
}))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn().mockResolvedValue({ live_dirty: false, pinned: false }),
    fileDiff: vi.fn().mockResolvedValue({ diff: '', original: '', status: 'clean' }),
    revealPath: vi.fn(),
  },
}))

const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
/** Set while a test holds `/api/file-read` open; calling it lets the read land. */
let releaseRead: (() => void) | null = null
let holdRead = false

beforeEach(() => {
  qc.clear()
  localStorage.clear()
  holdRead = false
  releaseRead = null
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.startsWith('/api/knowledge/config')) return { ok: true, json: async () => ({ enabled: false, supported_formats: [] }) }
    if (url.startsWith('/api/knowledge/sources')) return { ok: true, json: async () => [] }
    if (url.startsWith('/api/file-read') && holdRead) {
      await new Promise<void>(r => { releaseRead = r })
    }
    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '# Title\n' }
  }))
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.style.overflow = ''
})

describe('MarkdownPanel full-screen refresh', () => {
  it('names the disabled overlay refresh for the read in flight, then restores it', async () => {
    render(
      <MemoryRouter>
        <QueryClientProvider client={qc}>
          <MarkdownPanel embedded filePath="/tmp/notes.md" content={'# Title\n'} onContentChange={vi.fn()} onSave={vi.fn(async () => {})} onClose={vi.fn()} initialDiffMode={false} />
        </QueryClientProvider>
      </MemoryRouter>,
    )
    fireEvent.click(screen.getAllByTestId('markdown-panel-more-options')[0])
    fireEvent.click(screen.getByText('Full screen'))
    await screen.findByRole('dialog')

    holdRead = true
    fireEvent.click(screen.getByRole('button', { name: 'Refresh file' }))

    const busy = await screen.findByRole('button', { name: 'Refreshing file…' })
    expect(busy).toBeDisabled()
    expect(busy).toHaveAttribute('title', 'Refreshing file…')
    expect(screen.queryByRole('button', { name: 'Refresh file' })).toBeNull()

    await vi.waitFor(() => expect(releaseRead).not.toBeNull())
    await act(async () => { releaseRead!() })

    const idle = await screen.findByRole('button', { name: 'Refresh file' })
    expect(idle).toBeEnabled()
    expect(idle).toHaveAttribute('title', 'Refresh file')
  })
})
