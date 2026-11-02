/**
 * A Knowledge folder source's scan rows (currently-scanning, recently-done,
 * failed) each show the file's name. The value is the file's gateway path,
 * which on Windows is a native `C:\…` path — a `/`-only split would show it
 * whole — so each row takes the name with `pathBasename` (via
 * `utils/pathBasename`). The retry/skip calls still carry the full path.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { SourceFilesResponse } from './types'

const api = vi.hoisted(() => ({ files: null as SourceFilesResponse | null }))

vi.mock('./api', () => ({
  knowledgeApi: vi.fn(async (path: string) => {
    if (path.endsWith('/files')) return api.files
    return {}
  }),
}))

import { FolderProgress } from './SourcesList'

function filesResponse(file_path: string): SourceFilesResponse {
  return {
    files: [
      { file_path, status: 'scanning', mtime: 0, item_count: 0 },
      { file_path, status: 'done', mtime: 0, item_count: 2 },
      { file_path, status: 'failed', mtime: 0, item_count: 0, error_message: 'bad' },
    ],
    total: 3, done: 1, failed: 1, skipped: 0,
  }
}

function renderProgress() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <FolderProgress sourceId="s1" />
    </QueryClientProvider>,
  )
}

describe('FolderProgress scan rows', () => {
  beforeEach(() => { api.files = null })
  afterEach(() => cleanup())

  it('shows the file name, not the whole C:\\ path, on the scanning / done / failed rows', async () => {
    api.files = filesResponse('C:\\notes\\x.md')
    renderProgress()
    // The scanning, done and failed rows all render the same path; the name must
    // appear three times and the full path never.
    await waitFor(() => expect(screen.getAllByText('x.md')).toHaveLength(3))
    expect(screen.queryByText('C:\\notes\\x.md')).toBeNull()
  })

  it('still shows the file name on a POSIX path (existing behaviour unchanged)', async () => {
    api.files = filesResponse('/home/u/notes/x.md')
    renderProgress()
    await waitFor(() => expect(screen.getAllByText('x.md')).toHaveLength(3))
  })

  it('keeps a POSIX name that contains a backslash whole', async () => {
    api.files = filesResponse('/tmp/we\\ird.md')
    renderProgress()
    await waitFor(() => expect(screen.getAllByText('we\\ird.md')).toHaveLength(3))
  })
})
