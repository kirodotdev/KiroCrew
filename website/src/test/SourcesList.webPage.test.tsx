import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import SourcesList from '../pages/knowledge/SourcesList'
import * as api from '../pages/knowledge/api'
import { api as clientApi } from '../api/client'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))
vi.mock('../api/client', () => ({ api: { dashboardConfig: vi.fn() } }))

// The Web Page form adds one https page as a source; the gateway fetches it
// only from a site in knowledge.doc_ingest_hosts, which the form names.

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
        supportedFormatsDisplay=".md"
      />
    </QueryClientProvider>,
  )
}

type Answer = (path: string, init?: RequestInit) => Promise<unknown>

function mockGateway(hosts: string[] | null | Error, onAdd?: Answer) {
  vi.mocked(api.knowledgeApi).mockImplementation(((path: string, init?: RequestInit) => {
    if (path === '/config') {
      return hosts instanceof Error ? Promise.reject(hosts) : Promise.resolve({ enabled: true, supported_formats: ['.md'], url_allowed_hosts: hosts })
    }
    if (path === '/sources' && init?.method === 'POST') return onAdd ? onAdd(path, init) : Promise.resolve({ id: 'src-1', status: 'created' })
    if (path === '/sources/src-1/sync') return Promise.resolve({ status: 'syncing' })
    return Promise.resolve([])
  }) as unknown as typeof api.knowledgeApi)
}

async function openWebPage() {
  fireEvent.click(await screen.findByText('+ Add Source'))
  fireEvent.click(screen.getByRole('button', { name: /Web Page/ }))
}

function typeUrl(value: string) {
  fireEvent.change(screen.getByLabelText('Page URL'), { target: { value } })
}

beforeEach(() => {
  vi.mocked(api.knowledgeApi).mockReset()
  vi.mocked(clientApi.dashboardConfig).mockReset()
  vi.mocked(clientApi.dashboardConfig).mockResolvedValue({})
})

describe('SourcesList web page source', () => {
  it('shows the command to allow a site and holds the button while the list is empty', async () => {
    mockGateway([])
    renderList()
    await openWebPage()
    expect(await screen.findByText(/No sites are allowed yet/)).toBeInTheDocument()
    expect(screen.getByText(/kirocrew config set knowledge\.doc_ingest_hosts/)).toBeInTheDocument()
    typeUrl('https://example.com/a')
    expect(screen.getByRole('button', { name: 'Add Web Page' })).toBeDisabled()
  })

  it('names the allowed sites', async () => {
    mockGateway(['example.com'])
    renderList()
    await openWebPage()
    expect(await screen.findByText(/Allowed sites: example\.com\./)).toBeInTheDocument()
  })

  it('reports a failed config read as an error, not as an empty list', async () => {
    mockGateway(new Error('HTTP 502'))
    renderList()
    await openWebPage()
    expect(await screen.findByTestId('sources-web-page-hosts-error')).toHaveTextContent("Couldn't load the allowed sites.")
    expect(screen.queryByText(/No sites are allowed yet/)).not.toBeInTheDocument()
    typeUrl('https://example.com/a')
    expect(screen.getByRole('button', { name: 'Add Web Page' })).toBeDisabled()
  })

  it('reports a list the gateway could not read as an error', async () => {
    mockGateway(null)
    renderList()
    await openWebPage()
    expect(await screen.findByTestId('sources-web-page-hosts-error')).toHaveTextContent("Couldn't load the allowed sites.")
    expect(screen.queryByText(/No sites are allowed yet/)).not.toBeInTheDocument()
  })

  it('reads the site list again each time the form opens', async () => {
    mockGateway(new Error('HTTP 502'))
    renderList()
    await openWebPage()
    await screen.findByTestId('sources-web-page-hosts-error')
    mockGateway(['example.com'])
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getByText('+ Add Source'))
    expect(await screen.findByText(/Allowed sites: example\.com\./)).toBeInTheDocument()
    expect(screen.queryByTestId('sources-web-page-hosts-error')).not.toBeInTheDocument()
  })

  it('adds the page as a url source and starts its fetch', async () => {
    mockGateway(['example.com'])
    renderList()
    await openWebPage()
    await screen.findByText(/Allowed sites/)
    // A pasted link often ends in a newline; the form sends it trimmed.
    typeUrl('https://example.com/a\n')
    fireEvent.click(screen.getByRole('button', { name: 'Add Web Page' }))
    await waitFor(() => {
      expect(api.knowledgeApi).toHaveBeenCalledWith('/sources/src-1/sync', { method: 'POST' })
    })
    const post = vi.mocked(api.knowledgeApi).mock.calls.find(([p, init]) => p === '/sources' && init?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ name: 'https://example.com/a', source_type: 'url', uri: 'https://example.com/a' })
  })

  it('names an unlisted site, offers the command for it and holds the button', async () => {
    mockGateway(['example.com'])
    renderList()
    await openWebPage()
    await screen.findByText(/Allowed sites/)
    typeUrl('https://News.Example.org/story')
    expect(await screen.findByText(/news\.example\.org isn't an allowed site yet/)).toBeInTheDocument()
    expect(screen.getByText(/kirocrew config set knowledge\.doc_ingest_hosts/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add Web Page' })).toBeDisabled()
  })

  it('shows a refusal from the gateway through the error notice', async () => {
    const refused = "The page's host did not resolve to a public address"
    mockGateway(['example.com'], () => Promise.reject(new Error(refused)))
    renderList()
    await openWebPage()
    await screen.findByText(/Allowed sites/)
    typeUrl('https://example.com/a')
    fireEvent.click(screen.getByRole('button', { name: 'Add Web Page' }))
    expect(await screen.findByTestId('sources-web-page-error')).toHaveTextContent(refused)
  })
})
