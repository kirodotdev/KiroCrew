/**
 * `useDirectoryUpload` — the upload orchestration shared by the Files rail's
 * drop overlay and its "Upload files…" row-menu action.
 *
 * Rendered through a small harness (not `renderHook`) because the hook hands
 * back JSX (`fileInput`) that must actually mount for the hidden picker input
 * to be interactive.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({ api: { uploadToDirectory: vi.fn() } }))

import { api } from '../api/client'
import { useDirectoryUpload } from '../pages/chat/useDirectoryUpload'

const PROJECT_DIR = '/repo'
const REAL_PROJECT_DIR = '/private/repo'
const TARGET_DIR = '/repo/dir'

function Harness({ projectDir = PROJECT_DIR, rootDir }: { projectDir?: string; rootDir?: string }) {
  const { uploadFiles, pickAndUpload, progress, error, dismissError, success, dismissSuccess, fileInput } =
    useDirectoryUpload(projectDir, rootDir)
  return (
    <div>
      <button
        data-testid="upload"
        onClick={() => void uploadFiles(TARGET_DIR, [new File(['x'], 'a.txt', { type: 'text/plain' })])}
      >
        upload
      </button>
      <button
        data-testid="upload-two"
        onClick={() => void uploadFiles(TARGET_DIR, [
          new File(['x'], 'a.txt', { type: 'text/plain' }),
          new File(['y'], 'b.exe', { type: 'application/octet-stream' }),
        ])}
      >
        upload two
      </button>
      <button
        data-testid="upload-root"
        onClick={() => void uploadFiles(rootDir ?? projectDir, [new File(['x'], 'a.txt', { type: 'text/plain' })])}
      >
        upload root
      </button>
      <button
        data-testid="upload-real-sub"
        onClick={() => void uploadFiles(`${REAL_PROJECT_DIR}/sub/`, [new File(['x'], 'a.txt', { type: 'text/plain' })])}
      >
        upload real sub
      </button>
      <button data-testid="pick" onClick={() => pickAndUpload(TARGET_DIR)}>pick</button>
      {progress && <div data-testid="progress">{progress}</div>}
      {success && <div data-testid="success">{success}</div>}
      {success && <button data-testid="dismiss-success" onClick={dismissSuccess}>dismiss success</button>}
      {error && <div data-testid="error">{error}</div>}
      {error && <button data-testid="dismiss" onClick={dismissError}>dismiss</button>}
      {fileInput}
    </div>
  )
}

function mount(projectDir = PROJECT_DIR, rootDir?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
  const view = render(
    <QueryClientProvider client={qc}>
      <Harness projectDir={projectDir} rootDir={rootDir} />
    </QueryClientProvider>,
  )
  const rerenderWith = (nextDir: string) => view.rerender(
    <QueryClientProvider client={qc}>
      <Harness projectDir={nextDir} />
    </QueryClientProvider>,
  )
  return { qc, invalidateSpy, rerenderWith }
}

beforeEach(() => {
  vi.mocked(api.uploadToDirectory).mockReset()
})

describe('useDirectoryUpload — uploadFiles', () => {
  it('invalidates every file listing and search query an upload can change after a successful batch', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({ ok: true, path: `${TARGET_DIR}/a.txt`, name: 'a.txt' })
    const { qc, invalidateSpy } = mount()
    // The keys the Files rail, its tree and the folder tab read, each built
    // with the root its reader uses: the rail's filename search with the
    // backend's realpath of the project, the folder tab with its own folder.
    const listingKeys = [
      ['project-tree', PROJECT_DIR],
      ['project-tree', PROJECT_DIR, 'dir', 'dir'],
      ['git-status', PROJECT_DIR],
      ['file-grep', PROJECT_DIR, 'needle'],
      ['rail-name-search', REAL_PROJECT_DIR, 'a.txt'],
      ['browse-files', TARGET_DIR],
      ['folder-file-search', PROJECT_DIR, 'a.txt', 50],
    ]
    for (const key of listingKeys) qc.setQueryData(key, { seeded: true })

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['project-tree', PROJECT_DIR] }),
    )
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['git-status', PROJECT_DIR] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['file-grep', PROJECT_DIR] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['rail-name-search'] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['browse-files'] })
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['folder-file-search'] })
    for (const key of listingKeys) {
      expect(qc.getQueryState(key)?.isInvalidated, JSON.stringify(key)).toBe(true)
    }
    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('surfaces a non-collision failure', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({
      ok: false, status: 400, code: 'unsupported_file_type', error: 'Unsupported file type: .txt',
    })
    mount()

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() =>
      expect(screen.getByTestId('error')).toHaveTextContent("Couldn't upload a.txt: Unsupported file type: .txt"),
    )
  })

  it('surfaces every refusal from one multi-file upload batch', async () => {
    vi.mocked(api.uploadToDirectory)
      .mockResolvedValueOnce({
        ok: false, status: 400, code: 'unsupported_file_type', error: 'Unsupported file type: .txt',
      })
      .mockResolvedValueOnce({
        ok: false, status: 413, error: 'File is too large',
      })
    mount()

    fireEvent.click(screen.getByTestId('upload-two'))

    await waitFor(() => {
      const notice = screen.getByTestId('error')
      expect(notice).toHaveTextContent("Couldn't upload a.txt: Unsupported file type: .txt")
      expect(notice).toHaveTextContent("Couldn't upload b.exe: File is too large")
    })
  })

  it('dismisses a shown error', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({ ok: false, status: 500, error: 'boom' })
    mount()
    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(screen.getByTestId('error')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('dismiss'))

    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('clears a failure notice when projectDir changes in place', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({ ok: false, status: 500, error: 'boom' })
    const { rerenderWith } = mount()
    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(screen.getByTestId('error')).toBeInTheDocument())

    rerenderWith('/other-repo')

    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('drops the failures of an upload that finishes after projectDir changes', async () => {
    let resolveUpload: (value: Awaited<ReturnType<typeof api.uploadToDirectory>>) => void = () => {}
    vi.mocked(api.uploadToDirectory).mockReturnValue(
      new Promise((resolve) => { resolveUpload = resolve }),
    )
    const { invalidateSpy, rerenderWith } = mount()
    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(api.uploadToDirectory).toHaveBeenCalledTimes(1))

    rerenderWith('/other-repo')
    resolveUpload({ ok: false, status: 500, error: 'boom' })

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['project-tree', PROJECT_DIR] }),
    )
    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('clears an earlier failure notice when a later upload succeeds', async () => {
    vi.mocked(api.uploadToDirectory)
      .mockResolvedValueOnce({ ok: false, status: 500, error: 'boom' })
      .mockResolvedValueOnce({ ok: true, path: `${TARGET_DIR}/a.txt`, name: 'a.txt' })
    const { invalidateSpy } = mount()
    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(screen.getByTestId('error')).toBeInTheDocument())
    invalidateSpy.mockClear()

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['project-tree', PROJECT_DIR] }),
    )
    expect(api.uploadToDirectory).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('reports a name collision with the already-exists message and never retries', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({
      ok: false, status: 409, code: 'name_collision', error: 'already exists',
    })
    const { invalidateSpy } = mount()

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() =>
      expect(screen.getByTestId('error')).toHaveTextContent(
        '“a.txt” already exists in this folder. Rename your file, or remove the existing one, then upload again.',
      ),
    )
    expect(api.uploadToDirectory).toHaveBeenCalledTimes(1)
    expect(vi.mocked(api.uploadToDirectory).mock.calls[0]).toEqual([TARGET_DIR, expect.any(File)])
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['project-tree', PROJECT_DIR] })
  })

  it('reports a collision alongside another file\'s failure in one batch', async () => {
    vi.mocked(api.uploadToDirectory)
      .mockResolvedValueOnce({ ok: false, status: 409, code: 'name_collision', error: 'already exists' })
      .mockResolvedValueOnce({ ok: false, status: 400, code: 'unsupported_file_type', error: 'Unsupported file type: .exe' })
    mount()

    fireEvent.click(screen.getByTestId('upload-two'))

    await waitFor(() => {
      const notice = screen.getByTestId('error')
      expect(notice).toHaveTextContent('“a.txt” already exists in this folder.')
      expect(notice).toHaveTextContent("Couldn't upload b.exe: Unsupported file type: .exe")
    })
    expect(api.uploadToDirectory).toHaveBeenCalledTimes(2)
  })

  it('surfaces a rejected upload call (e.g. a transport failure) as an error instead of an unhandled rejection', async () => {
    vi.mocked(api.uploadToDirectory).mockRejectedValue(new Error('Failed to fetch'))
    mount()

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() =>
      expect(screen.getByTestId('error')).toHaveTextContent("Couldn't upload a.txt: Failed to fetch"),
    )
  })
})

type UploadResult = Awaited<ReturnType<typeof api.uploadToDirectory>>

/** Makes the next upload call hang until the returned resolver is called. */
function deferUpload(): (value: UploadResult) => void {
  let resolveUpload: (value: UploadResult) => void = () => {}
  vi.mocked(api.uploadToDirectory).mockReturnValueOnce(
    new Promise((resolve) => { resolveUpload = resolve }),
  )
  return (value) => resolveUpload(value)
}

const OK: UploadResult = { ok: true, path: `${TARGET_DIR}/a.txt`, name: 'a.txt' }

describe('useDirectoryUpload — progress and success', () => {
  it('shows the uploading file while its upload runs and drops the line when it ends', async () => {
    const resolve = deferUpload()
    mount()

    fireEvent.click(screen.getByTestId('upload'))

    expect(await screen.findByTestId('progress')).toHaveTextContent('Uploading a.txt…')
    resolve(OK)
    await waitFor(() => expect(screen.queryByTestId('progress')).toBeNull())
  })

  it('counts the files of a running multi-file batch', async () => {
    const resolve = deferUpload()
    vi.mocked(api.uploadToDirectory).mockResolvedValue(OK)
    mount()

    fireEvent.click(screen.getByTestId('upload-two'))

    expect(await screen.findByTestId('progress')).toHaveTextContent('Uploading 2 files…')
    resolve(OK)
    await waitFor(() => expect(screen.queryByTestId('progress')).toBeNull())
  })

  it('names the file and its folder, relative to the project, after a successful upload', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue(OK)
    mount()

    fireEvent.click(screen.getByTestId('upload'))

    expect(await screen.findByTestId('success')).toHaveTextContent('Uploaded a.txt to dir.')
    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('names the project folder for an upload into the tree root, and a subfolder under a realpath root', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue(OK)
    mount(PROJECT_DIR, REAL_PROJECT_DIR)

    fireEvent.click(screen.getByTestId('upload-root'))
    expect(await screen.findByTestId('success')).toHaveTextContent('Uploaded a.txt to repo.')

    fireEvent.click(screen.getByTestId('upload-real-sub'))
    await waitFor(() => expect(screen.getByTestId('success')).toHaveTextContent('Uploaded a.txt to sub.'))
  })

  it('counts the files of a successful multi-file batch', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue(OK)
    mount()

    fireEvent.click(screen.getByTestId('upload-two'))

    expect(await screen.findByTestId('success')).toHaveTextContent('Uploaded 2 files to dir.')
  })

  it('shows no success line for a batch with a failure', async () => {
    vi.mocked(api.uploadToDirectory)
      .mockResolvedValueOnce(OK)
      .mockResolvedValueOnce({ ok: false, status: 413, error: 'File is too large' })
    mount()

    fireEvent.click(screen.getByTestId('upload-two'))

    await waitFor(() => expect(screen.getByTestId('error')).toHaveTextContent("Couldn't upload b.exe"))
    expect(screen.queryByTestId('success')).toBeNull()
  })

  it('does not send a name that is still uploading into the same folder', async () => {
    const resolve = deferUpload()
    vi.mocked(api.uploadToDirectory).mockResolvedValue(OK)
    mount()

    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(api.uploadToDirectory).toHaveBeenCalledTimes(1))
    fireEvent.click(screen.getByTestId('upload'))

    expect(api.uploadToDirectory).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('progress')).toHaveTextContent('Uploading a.txt…')
    resolve(OK)
    expect(await screen.findByTestId('success')).toHaveTextContent('Uploaded a.txt to dir.')
    expect(api.uploadToDirectory).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('error')).toBeNull()

    // Once the first upload has ended the same name is sent again (and the
    // server answers for it).
    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(api.uploadToDirectory).toHaveBeenCalledTimes(2))
  })

  it('clears the success line on dismiss and when the next batch starts', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue(OK)
    mount()
    fireEvent.click(screen.getByTestId('upload'))
    await screen.findByTestId('success')

    fireEvent.click(screen.getByTestId('dismiss-success'))
    expect(screen.queryByTestId('success')).toBeNull()

    fireEvent.click(screen.getByTestId('upload'))
    await screen.findByTestId('success')
    const resolve = deferUpload()
    fireEvent.click(screen.getByTestId('upload'))

    await screen.findByTestId('progress')
    expect(screen.queryByTestId('success')).toBeNull()
    resolve(OK)
    await screen.findByTestId('success')
  })

  it('clears the success line and the running batch line when projectDir changes in place', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValueOnce(OK)
    const { invalidateSpy, rerenderWith } = mount()
    fireEvent.click(screen.getByTestId('upload'))
    await screen.findByTestId('success')

    rerenderWith('/other-repo')
    expect(screen.queryByTestId('success')).toBeNull()

    const resolve = deferUpload()
    fireEvent.click(screen.getByTestId('upload-two'))
    await screen.findByTestId('progress')
    rerenderWith(PROJECT_DIR)
    expect(screen.queryByTestId('progress')).toBeNull()

    // A batch that finishes after the switch reports nothing in the new panel.
    vi.mocked(api.uploadToDirectory).mockResolvedValue(OK)
    resolve(OK)
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['project-tree', '/other-repo'] }),
    )
    expect(screen.queryByTestId('success')).toBeNull()
    expect(screen.queryByTestId('progress')).toBeNull()
  })
})

describe('useDirectoryUpload — pickAndUpload', () => {
  it('opens the hidden file picker scoped to the requested directory', () => {
    mount()
    const input = screen.getByLabelText('Upload files…') as HTMLInputElement
    const clickSpy = vi.spyOn(input, 'click')

    fireEvent.click(screen.getByTestId('pick'))

    expect(clickSpy).toHaveBeenCalledTimes(1)
  })

  it('uploads the picker selection into the pending directory', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({ ok: true, path: `${TARGET_DIR}/b.txt`, name: 'b.txt' })
    mount()
    fireEvent.click(screen.getByTestId('pick'))
    const input = screen.getByLabelText('Upload files…') as HTMLInputElement
    const file = new File(['y'], 'b.txt', { type: 'text/plain' })

    fireEvent.change(input, { target: { files: [file] } })

    await waitFor(() => expect(api.uploadToDirectory).toHaveBeenCalledWith(TARGET_DIR, file))
  })

  it('does nothing when the picker is dismissed with no selection', () => {
    mount()
    fireEvent.click(screen.getByTestId('pick'))
    const input = screen.getByLabelText('Upload files…') as HTMLInputElement

    fireEvent.change(input, { target: { files: [] } })

    expect(api.uploadToDirectory).not.toHaveBeenCalled()
  })
})
