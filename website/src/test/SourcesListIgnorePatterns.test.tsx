/**
 * Editing a folder source's ignore patterns after it was added.
 *
 * The add-source form is the only other place patterns are set; these tests pin
 * the expanded-view editor: it shows the stored list, sends the cleaned list to
 * the existing PATCH route, and keeps the editor open with the gateway's message
 * when the save is refused.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

type Handler = (path: string, opts?: RequestInit) => unknown
let handler: Handler = () => ({ ok: true })

const folder = (over: Partial<Source> = {}): Source => ({
  id: 'f1', name: 'Notes folder', source_type: 'local_folder', uri: '/tmp/notes',
  sync_status: 'active', item_count: 5, ...over,
})

function renderList() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <SourcesList onIngest={vi.fn()} uploadNamespace="" setUploadNamespace={vi.fn()}
        namespaces={[]} ingestionJobs={[]} />
    </QueryClientProvider>,
  )
}

const patchCalls = () =>
  vi.mocked(api.knowledgeApi).mock.calls.filter(([p, opts]) =>
    p === '/sources/f1' && (opts as RequestInit | undefined)?.method === 'PATCH')

async function expandFolder() {
  fireEvent.click(await screen.findByLabelText('Expand folder details'))
}

beforeEach(() => {
  handler = () => ({ ok: true })
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(
    (async (path: string, opts?: RequestInit) => handler(path, opts)) as unknown as never,
  )
})

describe('folder ignore-pattern editor', () => {
  it('lists the stored patterns in the expanded view', async () => {
    handler = (path) => path === '/sources'
      ? [folder({ properties: JSON.stringify({ ignore_patterns: ['.trash/*', 'generated/**'] }) })]
      : { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    renderList()
    await expandFolder()
    expect(screen.getByText('.trash/*')).toBeTruthy()
    expect(screen.getByText('generated/**')).toBeTruthy()
  })

  it('says so when a folder has no patterns', async () => {
    handler = (path) => path === '/sources' ? [folder()] : { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    renderList()
    await expandFolder()
    expect(screen.getByText('No ignore patterns')).toBeTruthy()
  })

  it('saves the cleaned list through PATCH without purging by default', async () => {
    handler = (path) => path === '/sources'
      ? [folder({ properties: { ignore_patterns: ['.trash/*'] } })]
      : { ok: true, files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))

    const box = screen.getByLabelText('Ignore patterns') as HTMLTextAreaElement
    expect(box.value).toBe('.trash/*')
    expect(screen.getByText(/Files already in the library stay, but stop updating/)).toBeTruthy()
    expect(box.placeholder).toBe('e.g. .trash/*')
    expect((screen.getByLabelText(/Also remove files already in the library/) as HTMLInputElement).checked).toBe(false)

    fireEvent.change(box, { target: { value: '.trash/*\n\n  generated/**  \n' } })
    fireEvent.click(screen.getByText('Save patterns'))

    await waitFor(() => expect(patchCalls()).toHaveLength(1))
    const body = JSON.parse((patchCalls()[0][1] as RequestInit).body as string)
    expect(body).toEqual({ ignore_patterns: ['.trash/*', 'generated/**'] })
    await waitFor(() => expect(screen.queryByText('Save patterns')).toBeNull())
  })

  it('asks the gateway to purge matches only when the box is ticked', async () => {
    handler = (path) => path === '/sources'
      ? [folder()]
      : { ok: true, purge: "started", files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'generated/**' } })
    fireEvent.click(screen.getByLabelText(/Also remove files already in the library/))
    fireEvent.click(screen.getByText('Save and remove matching files'))

    await waitFor(() => expect(patchCalls()).toHaveLength(1))
    const body = JSON.parse((patchCalls()[0][1] as RequestInit).body as string)
    expect(body).toEqual({ ignore_patterns: ['generated/**'], purge_ignored: true })
  })

  it('refreshes the file list after a purge so removed files disappear', async () => {
    let purged = false
    const file = (p: string) => ({ file_path: p, status: 'done', error_message: null, mtime: 1, item_count: 1 })
    handler = (path, opts) => {
      if (path === '/sources') return [folder()]
      if (path === '/sources/f1' && opts?.method === 'PATCH') { purged = true; return { ok: true, purge: 'started' } }
      if (path === '/sources/f1/files') {
        const files = purged ? [file('/tmp/notes/keep.md')] : [file('/tmp/notes/keep.md'), file('/tmp/notes/generated/old.md')]
        return { files, total: files.length, done: files.length, failed: 0, skipped: 0 }
      }
      return { ok: true }
    }
    renderList()
    await expandFolder()
    expect(await screen.findByText('old.md')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'generated/**' } })
    fireEvent.click(screen.getByLabelText(/Also remove files already in the library/))
    fireEvent.click(screen.getByText('Save and remove matching files'))

    await waitFor(() => expect(screen.queryByText('old.md')).toBeNull())
    expect(screen.getByText('keep.md')).toBeTruthy()
  })

  it('cannot purge with an empty pattern list', async () => {
    handler = (path) => path === '/sources'
      ? [folder({ properties: { ignore_patterns: ['a/**'] } })]
      : { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: '' } })
    expect((screen.getByLabelText(/Also remove files already in the library/) as HTMLInputElement).disabled).toBe(true)
  })

  it('keeps the editor open and shows the gateway error when the save fails', async () => {
    handler = (path, opts) => {
      if (path === '/sources') return [folder()]
      if (path === '/sources/f1' && opts?.method === 'PATCH') throw new Error('ignore_patterns must be a list of strings')
      return { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'a/**' } })
    fireEvent.click(screen.getByText('Save patterns'))

    expect((await screen.findByRole('alert')).textContent).toContain('ignore_patterns must be a list of strings')
    expect(screen.getByText('Save patterns')).toBeTruthy()
  })

  it('explains a refused purge in plain words when folder watching is off', async () => {
    handler = (path, opts) => {
      if (path === '/sources') return [folder()]
      if (path === '/sources/f1' && opts?.method === 'PATCH') {
        throw Object.assign(new Error('folder watcher is not running; nothing was changed'),
          { code: 'folder_watcher_unavailable' })
      }
      return { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'a/**' } })
    fireEvent.click(screen.getByLabelText(/Also remove files already in the library/))
    fireEvent.click(screen.getByText('Save and remove matching files'))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain("Can't remove files while folder watching is off")
    expect(alert.textContent).not.toContain('folder watcher is not running')
  })

  it('reports how many files a purge removed once it finishes', async () => {
    let done = false
    handler = (path, opts) => {
      if (path === '/sources') {
        return [folder({ properties: done
          ? { ignore_patterns: ['generated/**'], last_purge: { id: 'p1', removed: 2 } }
          : {} })]
      }
      if (path === '/sources/f1' && opts?.method === 'PATCH') {
        return { ok: true, purge: 'started', purge_id: 'p1' }
      }
      return { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'generated/**' } })
    fireEvent.click(screen.getByLabelText(/Also remove files already in the library/))
    fireEvent.click(screen.getByText('Save and remove matching files'))

    expect(await screen.findByText('Removing matching files…')).toBeTruthy()
    done = true
    expect(await screen.findByText('Files removed: 2', {}, { timeout: 4000 })).toBeTruthy()
  })

  it('says so when a purge failed', async () => {
    let done = false
    handler = (path, opts) => {
      if (path === '/sources') {
        return [folder({ properties: done ? { last_purge: { id: 'p2', failed: true } } : {} })]
      }
      if (path === '/sources/f1' && opts?.method === 'PATCH') {
        return { ok: true, purge: 'started', purge_id: 'p2' }
      }
      return { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'a/**' } })
    fireEvent.click(screen.getByLabelText(/Also remove files already in the library/))
    fireEvent.click(screen.getByText('Save and remove matching files'))
    await screen.findByText('Removing matching files…')
    done = true
    const alert = await screen.findByRole('alert', {}, { timeout: 4000 })
    expect(alert.textContent).toContain("Couldn't remove the matching files")
  })

  it('names the destructive save on the button while the box is ticked', async () => {
    handler = (path) => path === '/sources' ? [folder()] : { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'a/**' } })
    expect(screen.getByText('Save patterns')).toBeTruthy()
    fireEvent.click(screen.getByLabelText(/Also remove files already in the library/))
    expect(screen.getByText('Save and remove matching files')).toBeTruthy()
    expect(screen.queryByText('Save patterns')).toBeNull()
  })

  it('says no files matched when a purge removed nothing', async () => {
    let done = false
    handler = (path, opts) => {
      if (path === '/sources') {
        return [folder({ properties: done ? { last_purge: { id: 'p3', removed: 0 } } : {} })]
      }
      if (path === '/sources/f1' && opts?.method === 'PATCH') {
        return { ok: true, purge: 'started', purge_id: 'p3' }
      }
      return { files: [], total: 0, done: 0, failed: 0, skipped: 0 }
    }
    renderList()
    await expandFolder()
    fireEvent.click(screen.getByLabelText('Edit ignore patterns'))
    fireEvent.change(screen.getByLabelText('Ignore patterns'), { target: { value: 'a/**' } })
    fireEvent.click(screen.getByLabelText(/Also remove files already in the library/))
    fireEvent.click(screen.getByText('Save and remove matching files'))
    await screen.findByText('Removing matching files…')
    done = true
    expect(await screen.findByText('No files matched these patterns', {}, { timeout: 4000 })).toBeTruthy()
  })

  it('offers no pattern editor on a single-file source', async () => {
    handler = (path) => path === '/sources'
      ? [{ id: 's1', name: 'doc.md', source_type: 'local_file', uri: '/tmp/doc.md', sync_status: 'synced', item_count: 1 }]
      : { ok: true }
    renderList()
    await screen.findByText('doc.md')
    expect(screen.queryByLabelText('Edit ignore patterns')).toBeNull()
  })
})
