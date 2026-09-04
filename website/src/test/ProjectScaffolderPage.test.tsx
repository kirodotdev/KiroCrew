/**
 * Integration test for src/apps/project-scaffolder/ProjectScaffolderPage.tsx.
 *
 * `fetch` is mocked rather than the app's `api.ts`, so the error-shape parsing
 * (`code`, `unknown`, verbatim `error` prose) is exercised as the real thing:
 * the stale-selection branch is chosen by a `code` this test only ever supplies
 * inside a 400 body, which is where the server puts it.
 *
 * The shared `ProjectPicker` reaches the network through `api/client` rather than
 * a bare `fetch`, so its two GETs are spied there instead. That keeps the queue
 * below strictly the scan/scaffold POSTs, which is what lets a test assert on
 * `calls[n]` by index.
 *
 * Covers: the picker-driven root, preview rendering with mixed tiers, existing
 * rows disabled, select-all/none scoped to its own list, confidence-first row
 * ordering, the one collapsed section deeper candidates are deferred into, the
 * empty status, an inline root refusal, the stale-selection rescan prompt, and
 * failed-row rendering.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import ProjectScaffolderPage from '../apps/project-scaffolder/ProjectScaffolderPage'
import { api } from '../api/client'

const ROOT = '/work/monorepo'

/** Two groups, mixed tiers, and one already-scaffolded row. */
const SCAN = {
  root: ROOT,
  root_existing: false,
  status: 'ok',
  candidates: [
    {
      path: `${ROOT}/services`, name: 'services', parent_path: null,
      tier: 'auto', signals: ['package.json'], existing: false, selected: true,
    },
    {
      path: `${ROOT}/services/api`, name: 'api', parent_path: `${ROOT}/services`,
      tier: 'auto', signals: ['pyproject.toml'], existing: false, selected: true,
    },
    {
      path: `${ROOT}/services/legacy`, name: 'legacy', parent_path: `${ROOT}/services`,
      tier: 'offered', signals: ['Makefile'], existing: false, selected: false,
    },
    {
      path: `${ROOT}/services/done`, name: 'done', parent_path: `${ROOT}/services`,
      tier: 'auto', signals: ['package.json'], existing: true, selected: false,
    },
  ],
  warnings: ['depth cap reached under /work/monorepo/vendor'],
}

const EMPTY_SCAN = {
  root: ROOT, root_existing: false, status: 'empty',
  candidates: [], warnings: [],
}

/**
 * A tree whose server order is deliberately the WRONG presentation order.
 *
 * The offered-only `tools` bucket is delivered first, and the `services` bucket
 * interleaves its tiers, so any assertion that confident rows and
 * confident-bearing groups come first is measuring the page's own ordering
 * rather than the order it was handed.
 */
const MIXED_SCAN = {
  root: ROOT,
  root_existing: false,
  status: 'ok',
  candidates: [
    {
      path: `${ROOT}/services`, name: 'services', parent_path: null,
      tier: 'auto', signals: ['git'], existing: false, selected: true,
    },
    {
      path: `${ROOT}/services/api`, name: 'api', parent_path: `${ROOT}/services`,
      tier: 'auto', signals: ['git'], existing: false, selected: true,
    },
    {
      path: `${ROOT}/services/legacy`, name: 'legacy', parent_path: `${ROOT}/services`,
      tier: 'offered', signals: ['manifest:package.json'], existing: false, selected: false,
    },
    {
      path: `${ROOT}/services/zzz`, name: 'zzz', parent_path: `${ROOT}/services`,
      tier: 'auto', signals: ['.kiro'], existing: false, selected: true,
    },
    {
      path: `${ROOT}/tools`, name: 'tools', parent_path: null,
      tier: 'offered', signals: ['manifest:package.json'], existing: false, selected: false,
    },
    {
      path: `${ROOT}/tools/gen`, name: 'gen', parent_path: `${ROOT}/tools`,
      tier: 'offered', signals: ['manifest:package.json'], existing: false, selected: false,
    },
    {
      path: `${ROOT}/tools/lint`, name: 'lint', parent_path: `${ROOT}/tools`,
      tier: 'offered', signals: ['manifest:package.json'], existing: false, selected: false,
    },
  ],
  warnings: [],
}

/** Queue of responses `fetch` hands out, in call order. */
let queued: { status: number; body: unknown }[] = []
let calls: { url: string; body: Record<string, unknown> }[] = []

function mockFetch() {
  return vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: JSON.parse(String(init?.body ?? '{}')) })
    const next = queued.shift() ?? { status: 500, body: { error: 'no response queued' } }
    return {
      ok: next.status < 400,
      status: next.status,
      json: async () => next.body,
      text: async () => JSON.stringify(next.body),
    } as Response
  })
}

beforeEach(() => {
  queued = []
  calls = []
  vi.stubGlobal('fetch', mockFetch())
  // The picker's own directory listings. Spied on the shared client so they never
  // consume from the POST queue above.
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [ROOT, '/work/other'] })
  vi.spyOn(api, 'browseDirs').mockResolvedValue({
    path: '/work', parent: '/', dirs: [{ name: 'monorepo', path: ROOT }],
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** Type a root and press Scan, then wait for the preview (or empty state) to land. */
async function scan(user: ReturnType<typeof userEvent.setup>, root = ROOT) {
  await user.type(screen.getByLabelText('Project directory'), root)
  await user.click(screen.getByRole('button', { name: 'Scan' }))
}

/** Choose the root from the picker instead of typing it, then press Scan. */
async function scanViaPicker(user: ReturnType<typeof userEvent.setup>, root = ROOT) {
  await user.click(screen.getByTestId('scaffolder-browse'))
  await user.click(await screen.findByRole('option', { name: new RegExp(root) }))
  await waitFor(() => expect(screen.getByLabelText('Project directory')).toHaveValue(root))
  await user.click(screen.getByRole('button', { name: 'Scan' }))
}

/** Open the deferred section, which starts collapsed, and hand back its list. */
async function expandDeferred(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByTestId('nested-toggle'))
  return screen.getByTestId('nested-list')
}


/** The page under a bare QueryClientProvider — and nothing else. The fetch mock
 *  is an ORDERED queue, so the full provider stack (whose contexts fetch on
 *  mount) would consume responses meant for the page. The provider itself
 *  performs no requests; it only hosts the page's mutations. */
function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <ProjectScaffolderPage />
    </QueryClientProvider>,
  )
}

describe('ProjectScaffolderPage', () => {
  it('fills the root from the shared project picker without scanning on selection', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    renderPage()

    await user.click(screen.getByTestId('scaffolder-browse'))
    // The same picker the sidebar's folder settings launches — recent + browse.
    // Two "Browse" texts while it is open: the launcher and the picker's own tab,
    // so the tab is counted rather than fetched by text alone.
    expect(await screen.findByText('Recent')).toBeInTheDocument()
    expect(screen.getAllByText('Browse')).toHaveLength(2)

    await user.click(await screen.findByRole('option', { name: new RegExp(ROOT) }))

    // A pick stages the path and stops: the field holds it, the picker is gone,
    // and no scan has been requested yet.
    const field = screen.getByLabelText('Project directory')
    await waitFor(() => expect(field).toHaveValue(ROOT))
    expect(screen.queryByText('Recent')).not.toBeInTheDocument()
    expect(calls).toHaveLength(0)
    // Focus lands back on the field, so the path is editable and Enter scans it.
    expect(field).toHaveFocus()

    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByTestId('preview-group')).toBeInTheDocument())
    expect(calls[0]).toEqual({ url: '/api/project-scaffold/scan', body: { root: ROOT } })
  })

  it('opens the picker from the keyboard and leaves the root untouched on Escape', async () => {
    const user = userEvent.setup()
    renderPage()

    const browse = screen.getByTestId('scaffolder-browse')
    await user.tab()
    await user.tab()
    expect(browse).toHaveFocus()

    await user.keyboard('{Enter}')
    expect(await screen.findByText('Recent')).toBeInTheDocument()

    // Escape abandons the pick: nothing is chosen and focus is not stranded in a
    // dropdown that no longer exists.
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByText('Recent')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Project directory')).toHaveValue('')
    expect(calls).toHaveLength(0)

    // Re-opening and choosing with the keyboard alone commits the highlighted row.
    await user.click(browse)
    expect(await screen.findByText('Recent')).toBeInTheDocument()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByLabelText('Project directory')).toHaveValue(ROOT))
  })

  it('scans a picked root end to end, same as a typed one', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    renderPage()
    await scanViaPicker(user)

    await waitFor(() => expect(screen.getByTestId('preview-group')).toBeInTheDocument())
    expect(calls[0]).toEqual({ url: '/api/project-scaffold/scan', body: { root: ROOT } })
    expect(screen.getByTestId('selected-count')).toHaveTextContent('2 selected')
  })

  it('renders a grouped preview with tiers, signals, and the server default selection', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    renderPage()
    await scan(user)

    await waitFor(() => expect(screen.getByTestId('preview-group')).toBeInTheDocument())
    expect(calls[0].url).toBe('/api/project-scaffold/scan')
    expect(calls[0].body).toEqual({ root: ROOT })

    // Only the root list is on screen at first, so the deferred rows' tiers and
    // signals arrive with the disclosure rather than ahead of it.
    expect(screen.getAllByText('Confident')).toHaveLength(1)
    expect(screen.queryByText('Offered')).not.toBeInTheDocument()
    expect(screen.queryByText('pyproject.toml')).not.toBeInTheDocument()

    // The server's own default selection is honored: the two non-existing AUTO
    // rows, one of them deferred and therefore counted on the summary too.
    expect(screen.getByTestId('selected-count')).toHaveTextContent('2 selected')
    expect(screen.getByTestId('nested-selected')).toHaveTextContent('1 selected inside')
    // Warnings are surfaced rather than swallowed.
    expect(screen.getByTestId('scan-warnings')).toHaveTextContent('depth cap reached')

    // Both tiers are distinguishable, and each row shows its signals, once open.
    await expandDeferred(user)
    expect(screen.getAllByText('Confident')).toHaveLength(3)
    expect(screen.getByText('Offered')).toBeInTheDocument()
    expect(screen.getByText('pyproject.toml')).toBeInTheDocument()
  })

  it('shows an already-scaffolded row with a disabled checkbox it cannot tick', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    renderPage()
    await scan(user)

    await expandDeferred(user)
    await waitFor(() => expect(screen.getAllByTestId('candidate-row')).toHaveLength(4))
    const existing = screen.getByLabelText(`${ROOT}/services/done (Already set up)`)
    expect(existing).toBeDisabled()
    expect(existing).not.toBeChecked()
    expect(screen.getByTestId('already-set-up')).toBeInTheDocument()
  })

  it('select-all adds only the tickable rows of its own list, select-none clears them', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    renderPage()
    await scan(user)

    const nested = await expandDeferred(user)

    // The deferred list holds api + legacy + done; select-all must reach the first
    // two and leave the already-scaffolded one alone.
    await user.click(within(screen.getByTestId('nested-suggestions')).getByRole('button', { name: 'Select all' }))
    expect(screen.getByTestId('selected-count')).toHaveTextContent('3 selected')
    expect(within(nested).getByLabelText(`${ROOT}/services/done (Already set up)`)).not.toBeChecked()

    await user.click(within(screen.getByTestId('nested-suggestions')).getByRole('button', { name: 'Select none' }))
    // The root list keeps its own selection, proving the bulk action is scoped.
    expect(screen.getByTestId('selected-count')).toHaveTextContent('1 selected')
    expect(screen.getByLabelText(`${ROOT}/services`)).toBeChecked()
  })

  it('posts exactly the ticked paths and reports created, skipped, and failed rows', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    queued.push({
      status: 200,
      body: {
        root: ROOT,
        created: [{ path: `${ROOT}/services`, folder_id: 'f1', name: 'services' }],
        skipped_existing: [`${ROOT}/services/done`],
        failed: [{
          path: `${ROOT}/services/api`,
          error: 'color must be one of the folder palette values',
          code: 'color_invalid',
        }],
        warnings: [],
      },
    })
    renderPage()
    await scan(user)
    await waitFor(() => expect(screen.getByTestId('preview-group')).toBeInTheDocument())

    // Untick one of the two defaults, so the posted set is provably the live one.
    // It is a deferred row, so the disclosure has to be opened to reach it.
    const nested = await expandDeferred(user)
    await user.click(within(nested).getByLabelText(`${ROOT}/services/api`))
    await user.click(screen.getByRole('button', { name: 'Create folders' }))

    await waitFor(() => expect(screen.getByTestId('scaffold-results')).toBeInTheDocument())
    expect(calls[1].url).toBe('/api/project-scaffold/create')
    expect(calls[1].body).toEqual({ root: ROOT, selected: [`${ROOT}/services`] })

    expect(screen.getByTestId('result-created')).toHaveTextContent('1 created')
    expect(screen.getByTestId('result-skipped')).toHaveTextContent('1 already existed')
    expect(screen.getByTestId('result-failed')).toHaveTextContent('1 failed')
    // A failed row carries the server's prose and its machine-readable code.
    const failed = screen.getByTestId('failed-rows')
    expect(failed).toHaveTextContent('color must be one of the folder palette values')
    expect(failed).toHaveTextContent('color_invalid')
  })

  it('distinguishes an empty scan from a populated one and still offers the root folder', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: EMPTY_SCAN })
    renderPage()
    await scan(user)

    await waitFor(() => expect(screen.getByTestId('scan-empty')).toBeInTheDocument())
    expect(screen.getByTestId('scan-empty-title')).toHaveTextContent('No sub-projects found')
    // Not a preview: no checklist is rendered at all.
    expect(screen.queryAllByTestId('preview-group')).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Create the root folder only' })).toBeEnabled()
  })

  it('renders a refused root-only create beside the empty state action', async () => {
    // The empty state's button shares createMut with the preview's, but the
    // preview card that carries the error element does not exist on this
    // branch — a refusal must not leave the button silently returning to idle
    // with the user unable to tell whether a folder now exists.
    const user = userEvent.setup()
    queued.push({ status: 200, body: EMPTY_SCAN })
    queued.push({ status: 500, body: { error: 'folders.json is not writable' } })
    renderPage()
    await scan(user)
    await waitFor(() => expect(screen.getByTestId('scan-empty')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Create the root folder only' }))

    await waitFor(() => expect(screen.getByTestId('create-error')).toBeInTheDocument())
    expect(screen.getByTestId('create-error')).toHaveTextContent('folders.json is not writable')
  })

  it('renders a refused root verbatim against the field', async () => {
    const user = userEvent.setup()
    queued.push({
      status: 400,
      body: { error: 'project_dir must be an existing directory', code: 'folder_scan_root_invalid' },
    })
    renderPage()
    await scan(user, '/no/such/place')

    await waitFor(() => expect(screen.getByTestId('root-error')).toBeInTheDocument())
    // The server's own sentence, not a re-worded local one.
    expect(screen.getByTestId('root-error')).toHaveTextContent('project_dir must be an existing directory')
    expect(screen.getByLabelText('Project directory')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.queryAllByTestId('preview-group')).toHaveLength(0)
  })

  it('renders a create failure at the create button, not in the root field slot', async () => {
    // The click that fails is at the BOTTOM of a preview that can run for
    // screens; a message rendered up in the project-directory card is off-screen,
    // so the button reads as dead. The root slot is also only cleared by a scan,
    // which would leave a create failure sitting there after it stopped being true.
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    queued.push({ status: 500, body: { error: 'folders.json is not writable' } })
    renderPage()
    await scan(user)
    await waitFor(() => expect(screen.getAllByTestId('preview-group').length).toBeGreaterThan(0))
    await user.click(screen.getByRole('button', { name: 'Create folders' }))

    await waitFor(() => expect(screen.getByTestId('create-error')).toBeInTheDocument())
    expect(screen.getByTestId('create-error')).toHaveTextContent('folders.json is not writable')
    expect(screen.queryByTestId('root-error')).not.toBeInTheDocument()
  })

  it('clears a previous create failure when the user retries', async () => {
    // Pins the retry path specifically: the message must not still be sitting
    // beside a button that is working again. Clearing happens when the retry is
    // dispatched, so this does not isolate the success handler's own reset — it
    // pins the property the user actually sees.
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    queued.push({ status: 500, body: { error: 'folders.json is not writable' } })
    queued.push({
      status: 200,
      body: {
        root: ROOT, created: [], skipped_existing: [], failed: [], warnings: [],
      },
    })
    renderPage()
    await scan(user)
    await waitFor(() => expect(screen.getAllByTestId('preview-group').length).toBeGreaterThan(0))
    await user.click(screen.getByRole('button', { name: 'Create folders' }))
    await waitFor(() => expect(screen.getByTestId('create-error')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Create folders' }))

    await waitFor(() => expect(screen.queryByTestId('create-error')).not.toBeInTheDocument())
  })

  it('turns a stale-selection refusal into a rescan prompt naming the dropped paths', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: SCAN })
    queued.push({
      status: 400,
      body: {
        error: 'selection is out of date — re-scan before creating folders',
        code: 'folder_scaffold_selection_stale',
        unknown: [`${ROOT}/services/api`],
      },
    })
    // The rescan returns a tree that no longer holds the vanished directory.
    queued.push({
      status: 200,
      body: { ...SCAN, candidates: [SCAN.candidates[0]], warnings: [] },
    })
    renderPage()
    await scan(user)
    await waitFor(() => expect(screen.getByTestId('preview-group')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Create folders' }))
    await waitFor(() => expect(screen.getByTestId('stale-selection')).toBeInTheDocument())
    const stale = screen.getByTestId('stale-selection')
    expect(stale).toHaveTextContent('Scan again before creating folders')
    expect(stale).toHaveTextContent(`${ROOT}/services/api`)
    // A stale selection is not an outcome, so no results card is shown.
    expect(screen.queryByTestId('scaffold-results')).not.toBeInTheDocument()

    // The prompt's own action re-scans the same root and replaces the preview.
    await user.click(within(stale).getByRole('button', { name: 'Re-scan' }))
    await waitFor(() => expect(screen.getAllByTestId('candidate-row')).toHaveLength(1))
    expect(calls[2]).toEqual({ url: '/api/project-scaffold/scan', body: { root: ROOT } })
    expect(screen.queryByTestId('stale-selection')).not.toBeInTheDocument()
  })

  it('renders confident rows before offered ones in a single root list', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: MIXED_SCAN })
    renderPage()
    await scan(user)

    // Exactly one titled list, however many packages contain a nested manifest.
    await waitFor(() => expect(screen.getAllByTestId('preview-group')).toHaveLength(1))
    const root = screen.getByTestId('preview-group')
    expect(root).toHaveTextContent('Directly under the root')

    // Root-level row order: the confident row leads the offered one, and the
    // server's delivered order survives inside each tier.
    const names = within(root)
      .getAllByTestId('candidate-row')
      .map((row) => row.querySelector('span')?.textContent)
    expect(names).toEqual(['services', 'tools'])

    // Presentation only: the server's own default selection is untouched.
    expect(screen.getByTestId('selected-count')).toHaveTextContent('3 selected')
  })

  it('defers deeper candidates to one collapsed section instead of a titled group each', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: MIXED_SCAN })
    renderPage()
    await scan(user)

    await waitFor(() => expect(screen.getByTestId('nested-suggestions')).toBeInTheDocument())

    // The two containing packages produce no sections of their own: naming one in
    // a heading reads as if that package had been demoted out of the root list it
    // is in fact still ticked in.
    expect(screen.getAllByTestId('preview-group')).toHaveLength(1)
    expect(screen.queryByText('Nested inside services')).not.toBeInTheDocument()
    expect(screen.queryByText('Nested inside tools')).not.toBeInTheDocument()

    // One summary for the whole scan, counting what it hides — including the rows
    // the server pre-ticked, which the create step would otherwise act on unseen.
    const toggle = screen.getByTestId('nested-toggle')
    expect(toggle).toHaveTextContent('5 possible sub-folders inside packages above')
    expect(screen.getByTestId('nested-selected')).toHaveTextContent('2 selected inside')

    // Collapsed by default: no deferred row, and no control that could tick one.
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('nested-list')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(`${ROOT}/services/api`)).not.toBeInTheDocument()
    expect(within(screen.getByTestId('nested-suggestions'))
      .queryByRole('button', { name: 'Select all' })).not.toBeInTheDocument()

    // Reachable and operable from the keyboard alone, as a native button.
    toggle.focus()
    await user.keyboard('{Enter}')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')

    // Expanded, each row says which package it was found inside and keeps the
    // full path as the disambiguating detail. Confident rows lead here too, so
    // the pre-ticked ones are what the reader meets first.
    const list = screen.getByTestId('nested-list')
    expect(within(list).getAllByTestId('candidate-row')).toHaveLength(5)
    expect(within(list).getAllByTestId('nested-inside')[0]).toHaveTextContent('Inside services')
    expect(within(list).getByLabelText(`${ROOT}/services/api`)).toBeInTheDocument()
  })

  it('keeps the root list select-all out of the deferred section', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: MIXED_SCAN })
    renderPage()
    await scan(user)

    await waitFor(() => expect(screen.getByTestId('preview-group')).toBeInTheDocument())
    const root = screen.getByTestId('preview-group')

    // Root select-all reaches services + tools only; the two deferred rows the
    // server pre-ticked stay exactly as many as they were.
    await user.click(within(root).getByRole('button', { name: 'Select all' }))
    expect(screen.getByTestId('selected-count')).toHaveTextContent('4 selected')
    expect(screen.getByTestId('nested-selected')).toHaveTextContent('2 selected inside')

    // Root select-none likewise leaves the deferred selection alone: the total
    // drops by the two root rows and by nothing else.
    await user.click(within(root).getByRole('button', { name: 'Select none' }))
    expect(screen.getByTestId('selected-count')).toHaveTextContent('2 selected')
    expect(screen.getByTestId('nested-selected')).toHaveTextContent('2 selected inside')
  })

  it('posts a ticked deferred path exactly as it posts a root one', async () => {
    const user = userEvent.setup()
    queued.push({ status: 200, body: MIXED_SCAN })
    queued.push({
      status: 200,
      body: {
        root: ROOT,
        created: [{ path: `${ROOT}/tools/lint`, folder_id: 'f1', name: 'lint' }],
        skipped_existing: [], failed: [], warnings: [],
      },
    })
    renderPage()
    await scan(user)

    const list = await expandDeferred(user)
    const section = screen.getByTestId('nested-suggestions')
    // Clear both lists, then tick one offered row inside the disclosure, so the
    // posted set is provably the deferred one.
    await user.click(within(section).getByRole('button', { name: 'Select none' }))
    await user.click(within(screen.getByTestId('preview-group')).getByRole('button', { name: 'Select none' }))
    await user.click(within(list).getByLabelText(`${ROOT}/tools/lint`))

    expect(screen.getByTestId('selected-count')).toHaveTextContent('1 selected')
    await user.click(screen.getByRole('button', { name: 'Create folders' }))

    await waitFor(() => expect(screen.getByTestId('scaffold-results')).toBeInTheDocument())
    expect(calls[1].url).toBe('/api/project-scaffold/create')
    expect(calls[1].body).toEqual({ root: ROOT, selected: [`${ROOT}/tools/lint`] })
  })

  it('omits the deferred section entirely when nothing is nested deeper', async () => {
    const user = userEvent.setup()
    queued.push({
      status: 200,
      body: { ...SCAN, candidates: [SCAN.candidates[0]], warnings: [] },
    })
    renderPage()
    await scan(user)

    await waitFor(() => expect(screen.getByTestId('preview-group')).toBeInTheDocument())
    expect(screen.queryByTestId('nested-suggestions')).not.toBeInTheDocument()
    expect(screen.queryByTestId('nested-toggle')).not.toBeInTheDocument()
  })
})
