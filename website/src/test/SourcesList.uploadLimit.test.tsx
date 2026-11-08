import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import { api as clientApi } from '../api/client'
import type { IngestionJob } from '../pages/knowledge/types'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))
vi.mock('../api/client', () => ({ api: { dashboardConfig: vi.fn() } }))

function listElement(queryClient: QueryClient, onIngest: (files: File[]) => void, ingestionJobs: IngestionJob[]) {
  return (
    <QueryClientProvider client={queryClient}>
      <SourcesList
        onIngest={onIngest}
        uploadNamespace="default"
        setUploadNamespace={() => {}}
        namespaces={['default']}
        ingestionJobs={ingestionJobs}
        supportedFormatsDisplay=".md, .zip"
      />
    </QueryClientProvider>
  )
}

function renderList(onIngest: (files: File[]) => void = () => {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(listElement(queryClient, onIngest, []))
  return { ...view, showJobs: (jobs: IngestionJob[]) => view.rerender(listElement(queryClient, onIngest, jobs)) }
}

async function openLocalFile() {
  fireEvent.click(await screen.findByText('+ Add Source'))
}

beforeEach(() => {
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(api.knowledgeApi).mockResolvedValue([] as unknown as never)
  vi.mocked(clientApi.dashboardConfig).mockReset()
})

describe('SourcesList upload limit copy', () => {
  it('advertises the gateway-served Knowledge ceiling, not the composer one', async () => {
    // The Knowledge ceiling is the smaller of the composer cap and the
    // ingestion cap, so it can differ from `upload_max_mb`.
    vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ upload_max_mb: 250, knowledge_upload_max_mb: 100 })
    renderList()
    await openLocalFile()
    expect(await screen.findByText(/Max 100 MB per file\./)).toBeInTheDocument()
    expect(screen.queryByTestId('sources-upload-limit-notice')).not.toBeInTheDocument()
  })

  it('shows a fractional Knowledge ceiling as served', async () => {
    vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ upload_max_mb: 100, knowledge_upload_max_mb: 0.5 })
    renderList()
    await openLocalFile()
    expect(await screen.findByText(/Max 0\.5 MB per file\./)).toBeInTheDocument()
  })

  it('refuses an over-limit file before sending it, naming the file and the limit', async () => {
    vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ upload_max_mb: 1, knowledge_upload_max_mb: 1, knowledge_upload_max_bytes: 1024 * 1024 })
    const onIngest = vi.fn()
    renderList(onIngest)
    await openLocalFile()
    await screen.findByText(/Max 1 MB per file\./)
    const big = new File(['x'], 'big.md')
    Object.defineProperty(big, 'size', { value: 1024 * 1024 + 1 })
    fireEvent.change(screen.getByLabelText('Upload files'), { target: { files: [big] } })
    // Nothing is sent, and the panel stays open with the refusal in view.
    expect(onIngest).not.toHaveBeenCalled()
    expect(await screen.findByTestId('sources-upload-refusal')).toHaveTextContent('File too large: big.md (max 1 MB)')
  })

  it('still sends a file at the limit and closes the panel', async () => {
    vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ upload_max_mb: 1, knowledge_upload_max_mb: 1, knowledge_upload_max_bytes: 1024 * 1024 })
    const onIngest = vi.fn()
    renderList(onIngest)
    await openLocalFile()
    await screen.findByText(/Max 1 MB per file\./)
    const ok = new File(['x'], 'ok.md')
    Object.defineProperty(ok, 'size', { value: 1024 * 1024 })
    fireEvent.change(screen.getByLabelText('Upload files'), { target: { files: [ok] } })
    expect(onIngest).toHaveBeenCalledWith([ok])
    expect(screen.queryByTestId('sources-upload-refusal')).not.toBeInTheDocument()
  })

  it('still sends the in-limit files of a mixed drop and names every refused one', async () => {
    vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ upload_max_mb: 1, knowledge_upload_max_mb: 1, knowledge_upload_max_bytes: 1024 * 1024 })
    const onIngest = vi.fn()
    renderList(onIngest)
    await openLocalFile()
    await screen.findByText(/Max 1 MB per file\./)
    const sized = (name: string, size: number) => { const f = new File(['x'], name); Object.defineProperty(f, 'size', { value: size }); return f }
    const small = sized('small.md', 10)
    const big1 = sized('big1.md', 2 * 1024 * 1024)
    const big2 = sized('big2.md', 3 * 1024 * 1024)
    fireEvent.change(screen.getByLabelText('Upload files'), { target: { files: [big1, small, big2] } })
    expect(onIngest).toHaveBeenCalledWith([small])
    const hint = await screen.findByTestId('sources-upload-refusal')
    expect(hint).toHaveTextContent('big1.md')
    expect(hint).toHaveTextContent('big2.md')
    expect(hint).not.toHaveTextContent('small.md')
  })

  it('checks against the exact byte ceiling, not the rounded MB figure', async () => {
    // A 0.54 MB ceiling is 566,231 bytes, shown as 0.5 MB.
    vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ upload_max_mb: 1, knowledge_upload_max_mb: 0.5, knowledge_upload_max_bytes: 566231 })
    const onIngest = vi.fn()
    renderList(onIngest)
    await openLocalFile()
    await screen.findByText(/Max 0\.5 MB per file\./)
    const under = new File(['x'], 'under.md')
    Object.defineProperty(under, 'size', { value: 550000 })
    fireEvent.change(screen.getByLabelText('Upload files'), { target: { files: [under] } })
    expect(onIngest).toHaveBeenCalledWith([under])
  })

  it('reports a failed settings read through the error notice and shows no figure rather than a default', async () => {
    vi.mocked(clientApi.dashboardConfig).mockRejectedValue(new Error('HTTP 502'))
    renderList()
    await openLocalFile()
    // A failed request is an error (ErrorNotice); the copy says uploads are
    // still checked by the server, so it does not read as a blocked upload.
    const notice = await screen.findByTestId('sources-upload-limit-notice')
    expect(notice).toHaveTextContent("Couldn't load the size limit. You can still upload; the server checks each file.")
    expect(screen.queryByText(/Max \d+ MB per file/)).not.toBeInTheDocument()
  })
})

describe('SourcesList server refusal', () => {
  it('opens the panel again when the server refuses an uploaded file, so its reason is in view', async () => {
    vi.mocked(clientApi.dashboardConfig).mockResolvedValue({ upload_max_mb: 1, knowledge_upload_max_mb: 1, knowledge_upload_max_bytes: 1024 * 1024 })
    const onIngest = vi.fn()
    const { showJobs } = renderList(onIngest)
    await openLocalFile()
    await screen.findByText(/Max 1 MB per file\./)
    const deck = new File(['x'], 'deck.pptx')
    fireEvent.change(screen.getByLabelText('Upload files'), { target: { files: [deck] } })
    expect(onIngest).toHaveBeenCalledWith([deck])
    // The drop closes the panel, as it does for every file it sends...
    expect(screen.queryByLabelText('Upload files')).not.toBeInTheDocument()
    // ...and the server's refusal brings it back, with the reason on that file's row.
    showJobs([{ name: 'deck.pptx', status: 'error', error: 'Unsupported file type: .pptx' }])
    expect(await screen.findByText('Unsupported file type: .pptx')).toBeInTheDocument()
  })
})
