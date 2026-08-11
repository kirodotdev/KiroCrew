/**
 * Behaviour tests for the knowledge sources list, aimed at the paths the four
 * existing SourcesList suites leave cold.
 *
 * Those suites cover rename, spend figures, the namespace body field and the
 * responsive row. What is untested is everything that MUTATES a source: the
 * inline folder-scan panel (progress, retry/skip of a failed file, the
 * invalidation that fires when a scan finishes), the row actions (sync, pause,
 * resume, confirm, delete with its optimistic rollback), the add-source dialog's
 * folder branch (folder picker, ignore patterns, recursive toggle) and the
 * pending-confirmation dialog that stands between a folder and a large scan.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ComponentProps } from 'react'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import type { Source, SourceFileInfo, SourceFilesResponse } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

type ListProps = ComponentProps<typeof SourcesList>
type Handler = (path: string, opts?: RequestInit) => unknown

/** Every test installs its own routing table; anything unrouted answers OK. */
let handler: Handler = () => ({ ok: true })

const src = (over: Partial<Source> = {}): Source => ({
  id: 's1', name: 'doc.md', source_type: 'local_file', uri: '/tmp/doc.md',
  sync_status: 'synced', item_count: 3, ...over,
})

const folder = (over: Partial<Source> = {}): Source =>
  src({ id: 'f1', name: 'Notes folder', source_type: 'local_folder', uri: '/tmp/notes', sync_status: 'active', item_count: 5, ...over })

const filesResp = (over: Partial<SourceFilesResponse> = {}): SourceFilesResponse => ({
  files: [], total: 0, done: 0, failed: 0, skipped: 0, ...over,
})

const fileInfo = (file_path: string, status: SourceFileInfo['status'], over: Partial<SourceFileInfo> = {}): SourceFileInfo => ({
  file_path, status, mtime: 0, item_count: 0, ...over,
})

function renderList(props: Partial<ListProps> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const onIngest = vi.fn()
  const setUploadNamespace = vi.fn()
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <SourcesList
        onIngest={onIngest}
        uploadNamespace=""
        setUploadNamespace={setUploadNamespace}
        namespaces={[]}
        ingestionJobs={[]}
        {...props}
      />
    </QueryClientProvider>,
  )
  return { ...utils, queryClient, onIngest, setUploadNamespace }
}

/** Calls made to a path, optionally narrowed to one HTTP method. */
const callsTo = (path: string, method?: string) =>
  vi.mocked(api.knowledgeApi).mock.calls.filter(([p, opts]) =>
    p === path && (!method || (opts as RequestInit | undefined)?.method === method))

let confirmSpy: ReturnType<typeof vi.spyOn> | undefined

beforeEach(() => {
  handler = () => ({ ok: true })
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockImplementation(
    (async (path: string, opts?: RequestInit) => handler(path, opts)) as unknown as never,
  )
})

afterEach(() => {
  confirmSpy?.mockRestore()
  confirmSpy = undefined
})

describe('SourcesList — inline folder scan panel', () => {
  const scanFiles = [
    fileInfo('/tmp/notes/alpha.md', 'done', { item_count: 7 }),
    fileInfo('/tmp/notes/bravo.md', 'scanning'),
    fileInfo('/tmp/notes/charlie.md', 'failed', { error_message: 'unreadable' }),
    fileInfo('/tmp/notes/delta.md', 'pending'),
  ]

  const routeScan = (resp: SourceFilesResponse) => {
    handler = (path: string) => {
      if (path === '/sources') return [folder()]
      if (path === '/sources/f1/files') return resp
      return { ok: true }
    }
  }

  async function expandFolder() {
    const utils = renderList()
    await screen.findByText('Notes folder')
    fireEvent.click(screen.getByLabelText('Expand folder details'))
    return utils
  }

  it('shows scan progress, the file in flight, recent successes and failures', async () => {
    routeScan(filesResp({ total: 4, done: 1, failed: 1, files: scanFiles }))
    await expandFolder()

    expect(await screen.findByText('1/4 (25%)')).toBeTruthy()
    expect(screen.getByText('1 failed')).toBeTruthy()
    // Basenames only: the full path is noise once the folder is named above.
    expect(screen.getByText('bravo.md')).toBeTruthy()
    expect(screen.getByText('alpha.md')).toBeTruthy()
    expect(screen.getByText('7 items')).toBeTruthy()
    expect(screen.getByText('charlie.md')).toBeTruthy()
    // The failure reason must be visible, not only in a tooltip.
    expect(screen.getByText('unreadable')).toBeTruthy()
    // A pending file is not yet news.
    expect(screen.queryByText('delta.md')).toBeNull()
  })

  it('retries and skips an individual failed file', async () => {
    routeScan(filesResp({ total: 4, done: 1, failed: 1, files: scanFiles }))
    await expandFolder()
    await screen.findByText('charlie.md')

    fireEvent.click(screen.getByText('Retry'))
    await waitFor(() => expect(callsTo('/sources/f1/files/retry', 'POST')).toHaveLength(1))
    expect(JSON.parse((callsTo('/sources/f1/files/retry')[0][1] as RequestInit).body as string))
      .toEqual({ file_path: '/tmp/notes/charlie.md' })

    fireEvent.click(screen.getByText('Skip'))
    await waitFor(() => expect(callsTo('/sources/f1/files/skip', 'POST')).toHaveLength(1))
    expect(JSON.parse((callsTo('/sources/f1/files/skip')[0][1] as RequestInit).body as string))
      .toEqual({ file_path: '/tmp/notes/charlie.md' })
  })

  it('collapses the panel again', async () => {
    routeScan(filesResp({ total: 4, done: 1, failed: 1, files: scanFiles }))
    await expandFolder()
    await screen.findByText('1/4 (25%)')

    fireEvent.click(screen.getByLabelText('Collapse folder details'))
    expect(screen.queryByText('1/4 (25%)')).toBeNull()
    expect(screen.getByLabelText('Expand folder details')).toBeTruthy()
  })

  it('renders nothing for a folder the gateway has no files for', async () => {
    routeScan(filesResp())
    await expandFolder()
    // No progress figure at all — a bare 0/0 bar would imply work is queued.
    await waitFor(() => expect(callsTo('/sources/f1/files')).not.toHaveLength(0))
    expect(screen.queryByText(/\(\d+%\)/)).toBeNull()
  })

  it('refreshes the item and graph views once a scan completes', async () => {
    // The list and graph tabs are rendered from separate queries, so a finished
    // scan is invisible there until it invalidates them.
    routeScan(filesResp({ total: 4, done: 1, files: scanFiles }))
    const { queryClient } = await expandFolder()
    await screen.findByText('1/4 (25%)')

    const invalidated = vi.spyOn(queryClient, 'invalidateQueries')
    act(() => {
      queryClient.setQueryData(['source-files', 'f1'], filesResp({
        total: 4, done: 4, files: scanFiles.map(f => ({ ...f, status: 'done' as const })),
      }))
    })

    await screen.findByText('4/4 (100%)')
    const keys = invalidated.mock.calls.map(c => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    expect(keys).toContain('["knowledge-items"]')
    expect(keys).toContain('["knowledge-graph"]')
    expect(keys).toContain('["knowledge-stats"]')
    invalidated.mockRestore()
  })
})

describe('SourcesList — row actions', () => {
  it('syncs a manual source and disables the button while in flight', async () => {
    let releaseSync: (v: unknown) => void = () => {}
    handler = (path: string, opts?: RequestInit) => {
      if (path === '/sources') return [src()]
      if (path === '/sources/s1/sync' && opts?.method === 'POST') {
        return new Promise(res => { releaseSync = res })
      }
      return { ok: true }
    }
    renderList()
    await screen.findByText('doc.md')

    const button = screen.getByLabelText('Sync source')
    fireEvent.click(button)
    await waitFor(() => expect(screen.getByLabelText('Sync source')).toHaveProperty('disabled', true))

    releaseSync({ synced: true })
    await waitFor(() => expect(screen.getByLabelText('Sync source')).toHaveProperty('disabled', false))
    expect(callsTo('/sources/s1/sync', 'POST')).toHaveLength(1)
  })

  it('offers Pause on a watching folder and Resume on a paused one', async () => {
    handler = (path: string) => {
      if (path === '/sources') return [
        folder({ id: 'f1', name: 'Watching', sync_status: 'active' }),
        folder({ id: 'f2', name: 'Halted', sync_status: 'paused' }),
      ]
      return { ok: true }
    }
    renderList()
    await screen.findByText('Watching')

    expect(screen.getByTitle('Watching folder')).toBeTruthy()
    expect(screen.getByTitle('Paused')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Pause scan'))
    await waitFor(() => expect(callsTo('/sources/f1/pause', 'POST')).toHaveLength(1))

    fireEvent.click(screen.getByLabelText('Resume scan'))
    await waitFor(() => expect(callsTo('/sources/f2/resume', 'POST')).toHaveLength(1))
  })

  it('confirms a folder that is still awaiting confirmation', async () => {
    handler = (path: string) => {
      if (path === '/sources') return [folder({ sync_status: 'pending_confirmation' })]
      return { ok: true }
    }
    renderList()
    await screen.findByText('Notes folder')

    // A folder that has not been confirmed must not offer Pause: there is nothing
    // running to pause yet.
    expect(screen.queryByLabelText('Pause scan')).toBeNull()
    expect(screen.getByTitle('Awaiting confirmation')).toBeTruthy()

    fireEvent.click(screen.getByLabelText('Confirm scan'))
    await waitFor(() => expect(callsTo('/sources/f1/confirm', 'POST')).toHaveLength(1))
  })

  it('puts the row back when the delete fails', async () => {
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    handler = (path: string, opts?: RequestInit) => {
      if (path === '/sources') return [src()]
      if (path === '/sources/s1' && opts?.method === 'DELETE') throw new Error('gateway said no')
      return { ok: true }
    }
    renderList()
    await screen.findByText('doc.md')

    fireEvent.click(screen.getByLabelText('Remove source'))
    // The optimistic removal must be rolled back, or the source silently vanishes
    // from the UI while still being indexed.
    expect(await screen.findByText('doc.md')).toBeTruthy()
  })

  it('does not delete when the user declines the prompt', async () => {
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    handler = (path: string) => {
      if (path === '/sources') return [src()]
      return { ok: true }
    }
    renderList()
    await screen.findByText('doc.md')

    fireEvent.click(screen.getByLabelText('Remove source'))
    expect(callsTo('/sources/s1', 'DELETE')).toHaveLength(0)
    expect(screen.getByText('doc.md')).toBeTruthy()
  })

  it('abandons a rename without a PATCH when the cancel control is used', async () => {
    handler = (path: string) => {
      if (path === '/sources') return [src({ name: 'Before' })]
      return { ok: true }
    }
    renderList()
    await screen.findByText('Before')

    fireEvent.click(screen.getByLabelText('Rename source'))
    fireEvent.change(screen.getByLabelText('Source name'), { target: { value: 'After' } })
    fireEvent.click(screen.getByLabelText('Cancel rename'))

    expect(screen.queryByLabelText('Source name')).toBeNull()
    expect(screen.getByText('Before')).toBeTruthy()
    expect(callsTo('/sources/s1', 'PATCH')).toHaveLength(0)
  })

  it('refreshes the dependent views when a sync finishes', async () => {
    handler = (path: string) => {
      if (path === '/sources') return [src({ sync_status: 'syncing' })]
      return { ok: true }
    }
    const { queryClient } = renderList()
    await screen.findByText('doc.md')

    const invalidated = vi.spyOn(queryClient, 'invalidateQueries')
    act(() => {
      queryClient.setQueryData(['knowledge-sources'], [src({ sync_status: 'synced' })])
    })

    await waitFor(() => {
      const keys = invalidated.mock.calls.map(c => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
      expect(keys).toContain('["knowledge-items"]')
      expect(keys).toContain('["knowledge-graph"]')
      expect(keys).toContain('["knowledge-stats"]')
    })
    invalidated.mockRestore()
  })
})

describe('SourcesList — row metadata', () => {
  it('flags a source that has not synced in over a month and leaves a fresh one plain', async () => {
    const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString()
    handler = (path: string) => {
      if (path === '/sources') return [
        src({ id: 's1', name: 'Fresh', last_synced: daysAgo(2) }),
        src({ id: 's2', name: 'Stale', last_synced: daysAgo(200) }),
        src({ id: 's3', name: 'Untouched' }),
      ]
      return { ok: true }
    }
    renderList()
    await screen.findByText('Fresh')

    const fresh = screen.getByText('2d ago')
    expect(fresh.className).toContain('text-muted')
    expect(fresh.className).not.toContain('text-warn')

    const stale = screen.getByText(/6mo ago/)
    expect(stale.className).toContain('text-warn')

    expect(screen.getByText('never synced')).toBeTruthy()
  })

  it('renders a word count and abbreviates a large one', async () => {
    handler = (path: string) => {
      if (path === '/sources') return [
        src({ id: 's1', name: 'Short', properties: JSON.stringify({ word_count: 500 }) }),
        src({ id: 's2', name: 'Long', properties: JSON.stringify({ word_count: 2500 }) }),
        src({ id: 's3', name: 'Unknown', properties: JSON.stringify({}) }),
      ]
      return { ok: true }
    }
    renderList()
    await screen.findByText('Short')

    expect(screen.getByText('500 words')).toBeTruthy()
    expect(screen.getByText('~3k words')).toBeTruthy()
    expect(screen.getAllByText(/words/)).toHaveLength(2)
  })

  it('shows a summary topic even when the stored themes are unreadable', async () => {
    // summary_themes is opaque JSON from the gateway; a truncated write must not
    // take the whole row down with it.
    handler = (path: string) => {
      if (path === '/sources') return [src({ summary_topic: 'Release notes', summary_themes: '{not json' })]
      return { ok: true }
    }
    renderList()
    expect(await screen.findByText('Release notes')).toBeTruthy()
  })
})

describe('SourcesList — add-source dialog, file branch', () => {
  async function openAddDialog(props: Partial<ListProps> = {}) {
    handler = (path: string) => {
      if (path === '/sources') return []
      return { ok: true }
    }
    const utils = renderList(props)
    fireEvent.click(await screen.findByText('+ Add Source'))
    return utils
  }

  const dropZone = () =>
    screen.getByText('Drop files here or click to upload').closest('[role="button"]') as HTMLElement

  it('highlights the drop zone while a drag is over it and clears the highlight on leave', async () => {
    await openAddDialog()
    const zone = dropZone()
    expect(zone.className).not.toContain('border-accent')

    fireEvent.dragOver(zone)
    expect(zone.className).toContain('border-accent')

    fireEvent.dragLeave(zone)
    expect(zone.className).not.toContain('border-accent')
  })

  it('ingests dropped files and closes the dialog', async () => {
    const { onIngest } = await openAddDialog()
    const file = new File(['# notes'], 'dropped.md', { type: 'text/markdown' })

    fireEvent.drop(dropZone(), { dataTransfer: { files: [file] } })

    expect(onIngest).toHaveBeenCalledTimes(1)
    expect(onIngest.mock.calls[0][0]).toEqual([file])
    expect(screen.queryByText('Drop files here or click to upload')).toBeNull()
  })

  it('ingests files chosen through the picker', async () => {
    const { onIngest } = await openAddDialog()
    const file = new File(['x'], 'picked.txt', { type: 'text/plain' })

    fireEvent.change(screen.getByLabelText('Upload files'), { target: { files: [file] } })

    expect(onIngest).toHaveBeenCalledTimes(1)
    expect(onIngest.mock.calls[0][0]).toEqual([file])
  })

  it('honours a caller-supplied accept list and the extensionless-file note', async () => {
    await openAddDialog({ uploadAccept: '.md,.txt', acceptsNoExtension: true })

    expect(screen.getByLabelText('Upload files').getAttribute('accept')).toBe('.md,.txt')
    expect(screen.getByText(/Files with no extension/)).toBeTruthy()
  })

  it('reports per-file ingestion progress for done, failed and in-flight jobs', async () => {
    await openAddDialog({
      ingestionJobs: [
        { name: 'alpha.md', status: 'done' },
        { name: 'bravo.md', status: 'error: too large' },
        { name: 'charlie.md', status: 'chunking' },
      ],
    })

    expect(screen.getByText('alpha.md')).toBeTruthy()
    expect(screen.getByText('done')).toBeTruthy()
    expect(screen.getByText('error: too large')).toBeTruthy()
    expect(screen.getByText('chunking')).toBeTruthy()
  })

  it('reports a typed namespace up to the caller', async () => {
    const { setUploadNamespace } = await openAddDialog({
      namespaces: [{ name: 'legacy-default', count: 4 }],
    })

    fireEvent.change(screen.getByLabelText('Namespace'), { target: { value: 'research' } })

    expect(setUploadNamespace).toHaveBeenCalledWith('research')
    // The datalist is what makes an existing namespace reusable without typing it
    // exactly, so its options must carry the item counts.
    expect(screen.getByRole('option', { hidden: true }).textContent).toBe('legacy-default (4)')
  })
})

describe('SourcesList — add-source dialog, folder branch', () => {
  async function openFolderForm(opts: { folderPicker?: boolean; extra?: Handler } = {}) {
    handler = (path: string, init?: RequestInit) => {
      if (path === '/sources' && init?.method !== 'POST') return []
      if (path === '/config') return { enabled: true, supported_formats: [], folder_picker: opts.folderPicker ?? false }
      return opts.extra?.(path, init) ?? { ok: true }
    }
    const utils = renderList()
    fireEvent.click(await screen.findByText('+ Add Source'))
    fireEvent.click(screen.getByText('Local Folder'))
    return utils
  }

  it('hides the folder picker when the gateway cannot open one', async () => {
    await openFolderForm({ folderPicker: false })
    expect(screen.queryByLabelText('Browse for a folder')).toBeNull()
  })

  it('fills the path from the native folder picker and shows it opening', async () => {
    let releasePick: (v: unknown) => void = () => {}
    await openFolderForm({
      folderPicker: true,
      extra: (path, init) => {
        if (path === '/pick-folder' && init?.method === 'POST') {
          return new Promise(res => { releasePick = res })
        }
        return { ok: true }
      },
    })

    fireEvent.click(await screen.findByLabelText('Browse for a folder'))
    expect(await screen.findByText('Opening...')).toBeTruthy()

    releasePick({ path: '/home/user/notes' })
    await waitFor(() =>
      expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '/home/user/notes'))
  })

  it('leaves the path alone when the picker is dismissed', async () => {
    await openFolderForm({
      folderPicker: true,
      extra: (path) => (path === '/pick-folder' ? { path: null } : { ok: true }),
    })

    fireEvent.click(await screen.findByLabelText('Browse for a folder'))
    await waitFor(() => expect(callsTo('/pick-folder', 'POST')).toHaveLength(1))
    expect(screen.getByLabelText('Folder path')).toHaveProperty('value', '')
  })

  it('sends the name, ignore patterns and recursive flag the user set', async () => {
    await openFolderForm()

    fireEvent.change(screen.getByLabelText('Source name (optional)'), { target: { value: 'Team notes' } })
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/tmp/docs' } })
    fireEvent.change(screen.getByLabelText('Ignore patterns'), {
      target: { value: '.trash/*\n\n  Templates/*  \n' },
    })
    fireEvent.click(screen.getByLabelText('Include subdirectories (recursive)'))

    fireEvent.click(screen.getByText('Add Folder'))
    await waitFor(() => expect(callsTo('/sources', 'POST')).toHaveLength(1))

    const body = JSON.parse((callsTo('/sources', 'POST')[0][1] as RequestInit).body as string)
    expect(body.name).toBe('Team notes')
    expect(body.source_type).toBe('local_folder')
    expect(body.uri).toBe('/tmp/docs')
    // Blank lines and stray padding are the normal shape of a pasted list; they
    // must not become patterns that match nothing.
    expect(body.properties.ignore_patterns).toEqual(['.trash/*', 'Templates/*'])
    expect(body.properties.recursive).toBe(false)
  })

  it('omits ignore_patterns entirely when the box is left blank', async () => {
    await openFolderForm()
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/tmp/docs' } })
    fireEvent.click(screen.getByText('Add Folder'))

    await waitFor(() => expect(callsTo('/sources', 'POST')).toHaveLength(1))
    const body = JSON.parse((callsTo('/sources', 'POST')[0][1] as RequestInit).body as string)
    expect(body.properties).not.toHaveProperty('ignore_patterns')
    // Falls back to the path when no display name was given.
    expect(body.name).toBe('/tmp/docs')
  })

  it('cannot be submitted with an empty path', async () => {
    await openFolderForm()
    expect(screen.getByText('Add Folder')).toHaveProperty('disabled', true)
  })

  it('surfaces the gateway error message when the add fails', async () => {
    await openFolderForm({
      extra: (path, init) => {
        if (path === '/sources' && init?.method === 'POST') throw new Error('path is not a directory')
        return { ok: true }
      },
    })
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/tmp/nope' } })
    fireEvent.click(screen.getByText('Add Folder'))

    expect(await screen.findByText('path is not a directory')).toBeTruthy()
  })

  it('closes the dialog on cancel', async () => {
    await openFolderForm()
    fireEvent.click(screen.getByText('Cancel'))
    expect(screen.queryByLabelText('Folder path')).toBeNull()
  })

  it('syncs the new source straight away when no confirmation is needed', async () => {
    await openFolderForm({
      extra: (path, init) => {
        if (path === '/sources' && init?.method === 'POST') return { id: 'new-1', status: 'active' }
        return { ok: true }
      },
    })
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/tmp/docs' } })
    fireEvent.click(screen.getByText('Add Folder'))

    await waitFor(() => expect(callsTo('/sources/new-1/sync', 'POST')).toHaveLength(1))
    expect(screen.queryByLabelText('Folder path')).toBeNull()
  })
})

describe('SourcesList — pending folder confirmation', () => {
  async function addFolderExpectingConfirmation(fileCount: number, confirmImpl?: Handler) {
    handler = (path: string, init?: RequestInit) => {
      if (path === '/sources' && init?.method === 'POST') {
        return { id: 'p1', status: 'pending_confirmation', file_count: fileCount }
      }
      if (path === '/sources') return []
      if (path === '/config') return { enabled: true, supported_formats: [], folder_picker: false }
      return confirmImpl?.(path, init) ?? { ok: true }
    }
    const utils = renderList()
    fireEvent.click(await screen.findByText('+ Add Source'))
    fireEvent.click(screen.getByText('Local Folder'))
    fireEvent.change(screen.getByLabelText('Folder path'), { target: { value: '/tmp/huge' } })
    fireEvent.click(screen.getByText('Add Folder'))
    return utils
  }

  it('warns before a large scan and starts it on confirmation', async () => {
    await addFolderExpectingConfirmation(150)

    expect(await screen.findByText(/150 supported files found/)).toBeTruthy()
    expect(screen.getByText(/Scanning this many files/)).toBeTruthy()

    fireEvent.click(screen.getByText('Start Scanning'))
    await waitFor(() => expect(callsTo('/sources/p1/confirm', 'POST')).toHaveLength(1))
    await waitFor(() => expect(screen.queryByText('Start Scanning')).toBeNull())
  })

  it('does not warn for a small folder', async () => {
    await addFolderExpectingConfirmation(4)

    expect(await screen.findByText(/4 supported files found/)).toBeTruthy()
    expect(screen.queryByText(/Scanning this many files/)).toBeNull()
    expect(screen.getByText(/watched continuously/)).toBeTruthy()
  })

  it('still offers to watch a folder that has nothing in it yet', async () => {
    await addFolderExpectingConfirmation(0)

    expect(await screen.findByText(/empty \(0 supported files\)/)).toBeTruthy()
    // Watching an empty folder is a legitimate choice — files arrive later.
    expect(screen.getByText('Watch Anyway')).toBeTruthy()
    expect(screen.getByText(/Any supported files added here/)).toBeTruthy()
  })

  it('shows the in-flight state while the scan is starting', async () => {
    let releaseConfirm: (v: unknown) => void = () => {}
    await addFolderExpectingConfirmation(10, (path, init) => {
      if (path === '/sources/p1/confirm' && init?.method === 'POST') {
        return new Promise(res => { releaseConfirm = res })
      }
      return { ok: true }
    })

    fireEvent.click(await screen.findByText('Start Scanning'))
    expect(await screen.findByText('Starting...')).toBeTruthy()

    releaseConfirm({ ok: true })
    await waitFor(() => expect(screen.queryByText('Starting...')).toBeNull())
  })

  it('deletes the unconfirmed source when the user backs out', async () => {
    await addFolderExpectingConfirmation(150)
    await screen.findByText('Start Scanning')

    // Cancelling must not leave a half-registered source behind that a later
    // sweep would pick up anyway.
    fireEvent.click(screen.getByText('Cancel'))
    await waitFor(() => expect(callsTo('/sources/p1', 'DELETE')).toHaveLength(1))
    expect(screen.queryByText('Start Scanning')).toBeNull()
  })
})
