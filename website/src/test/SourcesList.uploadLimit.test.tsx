import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import { api as clientApi } from '../api/client'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))
vi.mock('../api/client', () => ({ api: { dashboardConfig: vi.fn() } }))

function renderList() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <SourcesList
        onIngest={() => {}}
        uploadNamespace="default"
        setUploadNamespace={() => {}}
        namespaces={['default']}
        ingestionJobs={[]}
        supportedFormatsDisplay=".md, .zip"
      />
    </QueryClientProvider>,
  )
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

  it('reports a failed settings read through the error notice and shows no figure rather than a default', async () => {
    vi.mocked(clientApi.dashboardConfig).mockRejectedValue(new Error('HTTP 502'))
    renderList()
    await openLocalFile()
    // A failed request is an error (ErrorNotice); the copy says uploads are
    // still checked by the server, so it does not read as a blocked upload.
    const notice = await screen.findByTestId('sources-upload-limit-notice')
    expect(notice).toHaveTextContent("Couldn't load the size limit; the server still checks each file on upload.")
    expect(screen.queryByText(/Max \d+ MB per file/)).not.toBeInTheDocument()
  })
})
