import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockKnowledgeApi = vi.fn()
vi.mock('../pages/knowledge/api', () => ({
  knowledgeApi: (...args: unknown[]) => mockKnowledgeApi(...args),
}))

const { SourceGroup, NO_SOURCE, GROUP_PAGE_SIZE } = await import('../pages/knowledge/SourceGroup')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

const FOLDER_SOURCE = {
  id: 's1', name: 'PersonalKnowledgeBase', source_type: 'local_folder',
  uri: '/home/me/pkb', sync_status: 'synced', item_count: 378,
}

function item(id: string, filePath?: string) {
  return {
    id, title: `Item ${id}`, item_type: 'document', status: 'active',
    source_id: 's1', created_at: '2026-01-01', updated_at: '2026-01-01',
    ...(filePath ? { _file_path: filePath } : {}),
  }
}

let calls: string[] = []
let respond: (path: string) => unknown

function renderGroup(props: Record<string, unknown> = {}) {
  return render(
    <SourceGroup
      sourceId="s1"
      source={FOLDER_SOURCE}
      count={378}
      filters={{ status: 'active' }}
      onItemClick={() => {}}
      selectedItems={new Set()}
      onSelect={() => {}}
      {...props}
    />,
    { wrapper: Wrapper }
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  calls = []
  respond = () => ({ items: [item('a'), item('b')], total: 378 })
  mockKnowledgeApi.mockImplementation((path: string) => {
    calls.push(String(path))
    return Promise.resolve(respond(String(path)))
  })
})

describe('SourceGroup', () => {
  it('renders the source name and its filter-aware count while collapsed', () => {
    renderGroup()
    expect(screen.getByText('PersonalKnowledgeBase')).toBeInTheDocument()
    expect(screen.getByText('378')).toBeInTheDocument()
  })

  it('fetches nothing while collapsed', () => {
    renderGroup()
    expect(mockKnowledgeApi).not.toHaveBeenCalled()
  })

  it('fetches items scoped to its own source on expand', async () => {
    renderGroup()
    await userEvent.click(screen.getByText('PersonalKnowledgeBase'))
    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0]).toContain('source_id=s1')
    expect(calls[0]).toContain(`limit=${GROUP_PAGE_SIZE}`)
  })

  it('forwards active filters to the scoped fetch', async () => {
    renderGroup({ filters: { type: 'document', status: 'archived', namespace: 'team' } })
    await userEvent.click(screen.getByText('PersonalKnowledgeBase'))
    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0]).toContain('type=document')
    expect(calls[0]).toContain('status=archived')
    expect(calls[0]).toContain('namespace=team')
  })

  it('pages within its own source only', async () => {
    renderGroup()
    await userEvent.click(screen.getByText('PersonalKnowledgeBase'))
    // 378 / 100 = 4 pages for this source alone
    expect(await screen.findByText('Page 1 of 4')).toBeInTheDocument()
    await userEvent.click(screen.getByText(/Next/))
    await waitFor(() => expect(calls.some(c => c.includes('page=2'))).toBe(true))
    expect(await screen.findByText('Page 2 of 4')).toBeInTheDocument()
    // Every request stayed scoped to this source.
    expect(calls.every(c => c.includes('source_id=s1'))).toBe(true)
  })

  it('hides the pager when the source fits on one page', async () => {
    respond = () => ({ items: [item('a')], total: 3 })
    renderGroup({ count: 3 })
    await userEvent.click(screen.getByText('PersonalKnowledgeBase'))
    await waitFor(() => expect(calls.length).toBe(1))
    expect(screen.queryByText(/^Page /)).not.toBeInTheDocument()
  })

  it('uses the badge count for pager math until the fetch resolves', async () => {
    // Never resolves: the pager must still derive 4 pages from `count`.
    mockKnowledgeApi.mockImplementation(() => new Promise(() => {}))
    renderGroup()
    await userEvent.click(screen.getByText('PersonalKnowledgeBase'))
    expect(await screen.findByText('Page 1 of 4')).toBeInTheDocument()
  })

  it('sub-groups folder-source items by file path', async () => {
    respond = () => ({
      items: [item('a', '/home/me/pkb/notes.md'), item('b', '/home/me/pkb/notes.md'), item('c', '/home/me/pkb/todo.md')],
      total: 3,
    })
    renderGroup({ count: 3 })
    await userEvent.click(screen.getByText('PersonalKnowledgeBase'))
    expect(await screen.findByText('notes.md')).toBeInTheDocument()
    expect(screen.getByText('todo.md')).toBeInTheDocument()
    expect(screen.getByText('(2)')).toBeInTheDocument()
  })

  it('renders flat item cards for non-folder sources', async () => {
    respond = () => ({ items: [item('a')], total: 1 })
    renderGroup({
      count: 1,
      source: { ...FOLDER_SOURCE, source_type: 'local_file' },
    })
    await userEvent.click(screen.getByText('PersonalKnowledgeBase'))
    expect(await screen.findByText('Item a')).toBeInTheDocument()
    expect(screen.queryByText('notes.md')).not.toBeInTheDocument()
  })

  it('labels the sourceless bucket and queries it with the __none__ sentinel', async () => {
    respond = () => ({ items: [item('a')], total: 1 })
    renderGroup({ sourceId: NO_SOURCE, source: undefined, count: 1 })
    expect(screen.getByText('No source')).toBeInTheDocument()
    await userEvent.click(screen.getByText('No source'))
    await waitFor(() => expect(calls.length).toBe(1))
    expect(calls[0]).toContain(`source_id=${encodeURIComponent(NO_SOURCE)}`)
  })

  it('resets to page 1 when the filters change', async () => {
    const { rerender } = renderGroup({ defaultOpen: true })
    await screen.findByText('Page 1 of 4')
    await userEvent.click(screen.getByText(/Next/))
    expect(await screen.findByText('Page 2 of 4')).toBeInTheDocument()
    // A filter change can shrink the source below the current page; without a
    // reset the still-mounted group would sit on an out-of-range page.
    rerender(
      <SourceGroup
        sourceId="s1"
        source={FOLDER_SOURCE}
        count={378}
        filters={{ status: 'archived' }}
        onItemClick={() => {}}
        selectedItems={new Set()}
        onSelect={() => {}}
        defaultOpen
      />
    )
    expect(await screen.findByText('Page 1 of 4')).toBeInTheDocument()
  })

  it('opens pre-expanded when defaultOpen is set', async () => {
    renderGroup({ defaultOpen: true })
    await waitFor(() => expect(calls.length).toBe(1))
    expect(screen.getByRole('button', { expanded: true })).toBeInTheDocument()
  })
})
