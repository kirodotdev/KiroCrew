import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ExclusionsDisclosure from '../pages/knowledge/ExclusionsDisclosure'
import * as api from '../pages/knowledge/api'

vi.mock('../pages/knowledge/api', () => ({ knowledgeApi: vi.fn() }))

// One client outside the wrapper: a per-render client re-runs the query and
// leaves the error-path rejection unhandled.
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
)

/** Shaped like the real GET /api/knowledge/exclusions payload. */
const RULES = [
  { id: 'hidden_dirs', kind: 'rule', entries: [] },
  { id: 'hard_skip_dirs', kind: 'directories', entries: ['node_modules', 'dist', '.git'] },
  {
    id: 'source_type_dirs', kind: 'directories_by_source_type', entries: [],
    entries_by_source_type: { obsidian_vault: ['.obsidian', '.trash'] },
  },
  { id: 'junk_files', kind: 'globs', entries: ['._*', 'thumbs.db'] },
  {
    id: 'extension_allowlist', kind: 'allowlist',
    entries: ['.md', '.py'], accepts_no_extension: true,
  },
  { id: 'file_cap', kind: 'limit', entries: [], value: 5000 },
]

function mockExclusions(payload: unknown) {
  vi.mocked(api.knowledgeApi).mockImplementation(async () => payload as never)
}

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.knowledgeApi).mockReset()
})

async function expand() {
  fireEvent.click(await screen.findByText('What gets skipped automatically'))
}

describe('ExclusionsDisclosure', () => {
  it('renders each rule group with its entries once expanded', async () => {
    mockExclusions({ rules: RULES })
    render(<ExclusionsDisclosure />, { wrapper })
    // Collapsed on mount: entries hidden until the user opens it.
    expect(screen.queryByText('node_modules')).toBeNull()
    await expand()
    expect(screen.getByText('node_modules')).toBeInTheDocument()
    expect(screen.getByText('thumbs.db')).toBeInTheDocument()
    // entries_by_source_type renders under its source-type heading.
    expect(screen.getByText('obsidian_vault')).toBeInTheDocument()
    expect(screen.getByText('.obsidian')).toBeInTheDocument()
  })

  it('renders the file-cap group with the localized value and unit', async () => {
    mockExclusions({ rules: [RULES[5]] })
    render(<ExclusionsDisclosure />, { wrapper })
    await expand()
    // The limit branch composes fmtNumber(value) + the unit label; assert the
    // unit shows (the number itself is the i18n seam's concern, not ours).
    expect(screen.getByText(/files$/)).toBeInTheDocument()
  })

  it('shows the extensionless note only when the backend allows it', async () => {
    mockExclusions({ rules: [{ ...RULES[4], accepts_no_extension: true }] })
    const { unmount } = render(<ExclusionsDisclosure />, { wrapper })
    await expand()
    expect(screen.getByText(/no extension at all/)).toBeInTheDocument()
    unmount()

    queryClient.clear()
    mockExclusions({ rules: [{ ...RULES[4], accepts_no_extension: false }] })
    render(<ExclusionsDisclosure />, { wrapper })
    await expand()
    expect(screen.queryByText(/no extension at all/)).toBeNull()
  })

  it('renders an unrecognised rule id instead of dropping it', async () => {
    // A newer gateway growing a rule group must stay visible, not vanish.
    mockExclusions({ rules: [{ id: 'future_rule', kind: 'directories', entries: ['secrets'] }] })
    render(<ExclusionsDisclosure />, { wrapper })
    await expand()
    expect(screen.getByText('future_rule')).toBeInTheDocument()
    expect(screen.getByText('secrets')).toBeInTheDocument()
  })

  it('renders nothing when the endpoint is absent or empty', async () => {
    // Older gateway (404) and a no-rules payload both collapse to no UI, so the
    // panel never errors the Add Source form.
    vi.mocked(api.knowledgeApi).mockRejectedValue(new Error('404'))
    const missing = render(<ExclusionsDisclosure />, { wrapper })
    await waitFor(() => expect(missing.container).toBeEmptyDOMElement())
    missing.unmount()

    queryClient.clear()
    mockExclusions({ rules: [] })
    const empty = render(<ExclusionsDisclosure />, { wrapper })
    await waitFor(() => expect(empty.container).toBeEmptyDOMElement())
  })
})
