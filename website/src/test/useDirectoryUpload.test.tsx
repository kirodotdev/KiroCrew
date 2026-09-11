/**
 * `useDirectoryUpload` — the upload orchestration shared by the Files rail's
 * drag-and-drop, its full-pane drop overlay, and its "Upload files…" row-menu
 * action.
 *
 * Rendered through a small harness (not `renderHook`) because the hook hands
 * back JSX (`fileInput`, `confirmDialog`) that must actually mount for the
 * collision-confirm dialog and the hidden picker input to be interactive.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../api/client', () => ({ api: { uploadToDirectory: vi.fn() } }))

import { api } from '../api/client'
import { useDirectoryUpload } from '../pages/chat/useDirectoryUpload'

const PROJECT_DIR = '/repo'
const TARGET_DIR = '/repo/dir'

function Harness({ projectDir = PROJECT_DIR }: { projectDir?: string }) {
  const { uploadFiles, pickAndUpload, error, dismissError, fileInput, confirmDialog } =
    useDirectoryUpload(projectDir)
  return (
    <div>
      <button
        data-testid="upload"
        onClick={() => void uploadFiles(TARGET_DIR, [new File(['x'], 'a.txt', { type: 'text/plain' })])}
      >
        upload
      </button>
      <button data-testid="pick" onClick={() => pickAndUpload(TARGET_DIR)}>pick</button>
      {error && <div data-testid="error">{error}</div>}
      {error && <button data-testid="dismiss" onClick={dismissError}>dismiss</button>}
      {fileInput}
      {confirmDialog}
    </div>
  )
}

function mount(projectDir = PROJECT_DIR) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
  render(
    <QueryClientProvider client={qc}>
      <Harness projectDir={projectDir} />
    </QueryClientProvider>,
  )
  return { qc, invalidateSpy }
}

beforeEach(() => {
  vi.mocked(api.uploadToDirectory).mockReset()
})

describe('useDirectoryUpload — uploadFiles', () => {
  it('uploads and invalidates the tree and git-status queries on success', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({ ok: true, path: `${TARGET_DIR}/a.txt`, name: 'a.txt' })
    const { invalidateSpy } = mount()

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['project-tree', PROJECT_DIR] }),
    )
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ['git-status', PROJECT_DIR] })
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

  it('dismisses a shown error', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({ ok: false, status: 500, error: 'boom' })
    mount()
    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(screen.getByTestId('error')).toBeInTheDocument())

    fireEvent.click(screen.getByTestId('dismiss'))

    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('prompts on a name collision and retries with overwrite when replaced', async () => {
    vi.mocked(api.uploadToDirectory)
      .mockResolvedValueOnce({ ok: false, status: 409, code: 'name_collision', error: 'already exists' })
      .mockResolvedValueOnce({ ok: true, path: `${TARGET_DIR}/a.txt`, name: 'a.txt' })
    mount()

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() => expect(screen.getByText('Replace existing file?')).toBeInTheDocument())
    expect(screen.getByText(/already exists in this folder/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Replace' }))

    await waitFor(() => expect(api.uploadToDirectory).toHaveBeenCalledTimes(2))
    expect(vi.mocked(api.uploadToDirectory).mock.calls[1]).toEqual([
      TARGET_DIR, expect.any(File), { overwrite: true },
    ])
    expect(screen.queryByTestId('error')).toBeNull()
  })

  it('surfaces a rejected upload call (e.g. a transport failure) as an error instead of an unhandled rejection', async () => {
    vi.mocked(api.uploadToDirectory).mockRejectedValue(new Error('Failed to fetch'))
    mount()

    fireEvent.click(screen.getByTestId('upload'))

    await waitFor(() =>
      expect(screen.getByTestId('error')).toHaveTextContent("Couldn't upload a.txt: Failed to fetch"),
    )
  })

  it('surfaces a rejected overwrite retry as an error too', async () => {
    vi.mocked(api.uploadToDirectory)
      .mockResolvedValueOnce({ ok: false, status: 409, code: 'name_collision', error: 'already exists' })
      .mockRejectedValueOnce(new Error('network down'))
    mount()

    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(screen.getByText('Replace existing file?')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Replace' }))

    await waitFor(() =>
      expect(screen.getByTestId('error')).toHaveTextContent("Couldn't upload a.txt: network down"),
    )
  })

  it('does not re-upload when the collision prompt is cancelled', async () => {
    vi.mocked(api.uploadToDirectory).mockResolvedValue({
      ok: false, status: 409, code: 'name_collision', error: 'already exists',
    })
    mount()

    fireEvent.click(screen.getByTestId('upload'))
    await waitFor(() => expect(screen.getByText('Replace existing file?')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByText('Replace existing file?')).not.toBeInTheDocument())
    expect(api.uploadToDirectory).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('error')).toBeNull()
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
