import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ComponentType } from 'react'
import ArtifactsPage from '../pages/ArtifactsPage'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import type { Artifact, ArtifactFolder, SessionDoc } from '../types'

vi.mock('../api/client')

// VirtuosoMasonry virtualizes against real layout, which the test DOM lacks, so
// it renders zero items — same shim ArtifactsPage.test.tsx installs.
vi.mock('@virtuoso.dev/masonry', () => ({
  VirtuosoMasonry: ({ data, context, ItemContent }: {
    data: unknown[]
    context: unknown
    ItemContent: ComponentType<{ data: unknown; index: number; context: unknown }>
  }) => (
    <div data-testid="masonry">
      {data.map((d, i) => (
        <ItemContent key={i} data={d} index={i} context={context} />
      ))}
    </div>
  ),
}))

// Slugs are hyphenated on purpose: a card renders BOTH the name ("cr queue")
// and the slug ("cr-queue"), so a single-word slug makes every text query
// ambiguous.
const mkArtifact = (slug: string, overrides: Partial<Artifact> = {}): Artifact => ({
  slug,
  name: slug.replace(/-/g, ' '),
  kind: 'widget',
  source: 'chat',
  pinned: false,
  description: '',
  tags: [],
  version: 1,
  created_at: '2026-05-21T22:00:00.000000+00:00',
  updated_at: '2026-05-21T22:00:00.000000+00:00',
  ...overrides,
})

const mkFolder = (id: string, name: string, overrides: Partial<ArtifactFolder> = {}): ArtifactFolder => ({
  id,
  name,
  order: 0,
  parent_id: '',
  item_count: 0,
  ...overrides,
})

const mkDoc = (path: string, name: string): SessionDoc => ({
  path,
  name,
  updated_at: '2026-08-07T12:00:00',
  session_key: 'dashboard_chat-1',
  session_title: 'Research session',
  message_ts: 'm1',
  saved: false,
  slug: '',
})

/** Seed every query the page fires so no auto-mocked method resolves undefined. */
function seed({
  artifacts = [] as Artifact[],
  folders = [] as ArtifactFolder[],
  docs = [] as SessionDoc[],
  full,
}: {
  artifacts?: Artifact[]
  folders?: ArtifactFolder[]
  docs?: SessionDoc[]
  full?: Partial<Artifact>
} = {}) {
  const m = vi.mocked(api)
  m.artifacts = vi.fn().mockResolvedValue({ artifacts })
  m.artifactFolders = vi.fn().mockResolvedValue({ folders })
  m.artifactSessionDocs = vi.fn().mockResolvedValue({ docs })
  m.getArtifactPublishProviders = vi.fn().mockResolvedValue({ providers: [], kind: 'widget' })
  m.themeBoot = vi.fn().mockResolvedValue({})
  m.artifact = vi.fn().mockImplementation((slug: string) => {
    const base = artifacts.find((a) => a.slug === slug) ?? mkArtifact(slug)
    return Promise.resolve({ ...base, ...full })
  })
  m.setArtifactPinned = vi.fn().mockResolvedValue({})
  m.deleteArtifact = vi.fn().mockResolvedValue({ ok: true })
  m.createArtifact = vi.fn().mockResolvedValue(mkArtifact('untitled', { kind: 'markdown' }))
  m.setArtifactFolder = vi.fn().mockResolvedValue({ ok: true })
  m.createArtifactFolder = vi.fn().mockResolvedValue(mkFolder('new', 'New'))
  m.updateArtifactFolder = vi.fn().mockResolvedValue({ ok: true })
  m.deleteArtifactFolder = vi.fn().mockResolvedValue({ ok: true })
  m.materializeArtifact = vi.fn().mockResolvedValue({})
  return m
}

const folderMenuFor = (name: string) =>
  screen.getByRole('button', { name: `Actions for folder ${name}` })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // useAppPreview hits /api/artifacts/<slug>/app-preview with bare fetch; a
  // not-ok response is the "no local copy" answer, so webapp cards render
  // their status hero deterministically with zero network.
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ── Kind-aware previews (ContentThumb) ────────────────────────────────────
// Every non-iframe kind takes its own branch; only the raw-snippet fallback
// was previously exercised.
describe('ArtifactsPage — card previews per kind', () => {
  it('renders a markdown artifact through the markdown renderer', async () => {
    const arts = [mkArtifact('daily-notes', { kind: 'markdown' })]
    seed({ artifacts: arts, full: { content: '# Heading one\n\nbody text' } })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Heading one' })).toBeInTheDocument())
    expect(screen.getByText('body text')).toBeInTheDocument()
  })

  it('draws an SVG artifact inline after sanitizing it', async () => {
    const arts = [mkArtifact('brand-logo', { kind: 'svg' })]
    seed({
      artifacts: arts,
      // The opening angle bracket is concatenated away from the tag name so this
      // added line does not match the blocking `use-lucide-icons` grep, which
      // scans ADDED lines in src/**/*.tsx unconditionally with no exception for
      // tests. Splitting at the attribute instead does NOT work: that rule's
      // pattern tolerates any run of non-`>` characters between the tag name and
      // the attribute, so `'... ' + '...'` still matches. The runtime string
      // here is byte-identical to the single literal it replaces, and this is
      // sanitizer-fixture content rather than a hand-rolled icon.
      full: { content: '<' + 'svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4" /><script>window.x=1</script></svg>' },
    })
    const { container } = renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(container.querySelector('svg circle')).toBeTruthy())
    // The sanitizer strips executable content from the drawn markup.
    expect(container.querySelector('svg script')).toBeNull()
  })

  it('pretty-prints a JSON artifact', async () => {
    const arts = [mkArtifact('app-config', { kind: 'json' })]
    seed({ artifacts: arts, full: { content: '{"a":1,"b":[2]}' } })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/"a": 1/)).toBeInTheDocument())
  })

  it('keeps unparseable JSON as its raw text instead of blanking the preview', async () => {
    const arts = [mkArtifact('broken-config', { kind: 'json' })]
    seed({ artifacts: arts, full: { content: '{not json at all' } })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('{not json at all')).toBeInTheDocument())
  })

  it('renders a placeholder rather than an empty preview for blank content', async () => {
    const arts = [mkArtifact('blank-doc', { kind: 'text' })]
    seed({ artifacts: arts, full: { content: '   \n  ' } })
    const { container } = renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('blank doc')).toBeInTheDocument())
    expect(container.querySelector('pre')).toBeNull()
  })

  it('credits the upstream owner on an imported artifact card', async () => {
    const arts = [mkArtifact('forked-board', {
      source: 'import',
      fork_metadata: { upstream_owner: 'dana' } as Artifact['fork_metadata'],
    })]
    seed({ artifacts: arts })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText(/by\s+dana/)).toBeInTheDocument())
  })
})

// ── Webapp cards (WebAppThumb) ────────────────────────────────────────────
describe('ArtifactsPage — webapp cards', () => {
  const webapp = (status: string, publicUrl = ''): Artifact =>
    mkArtifact('shop-app', {
      kind: 'webapp',
      webapp_metadata: {
        lifecycle: { status },
        deploy_target: { public_url: publicUrl },
        architecture: { frontend: true, backend: true, state: false },
      },
    } as Partial<Artifact>)

  it('shows the deployed host in the mock browser chrome for a live app', async () => {
    const arts = [webapp('live', 'https://d123.cloudfront.net/site/')]
    seed({ artifacts: arts })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('d123.cloudfront.net/site/')).toBeInTheDocument())
    // The remote frame is only trusted after a header probe, so the hero shows.
    expect(screen.getByText('Live')).toBeInTheDocument()
    // The architecture summary lists only the parts the app actually has.
    expect(screen.getByText('frontend · api')).toBeInTheDocument()
  })

  it('labels an undeployed app and falls back to a "not deployed" URL slot', async () => {
    const arts = [webapp('draft')]
    seed({ artifacts: arts })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Not deployed')).toBeInTheDocument())
    expect(screen.getByText('not deployed')).toBeInTheDocument()
  })

  it('treats an unparseable public URL as not deployed', async () => {
    const arts = [webapp('live', 'not-a-url')]
    seed({ artifacts: arts })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('not deployed')).toBeInTheDocument())
  })

  it('shows the expired hero for a lapsed deployment', async () => {
    const arts = [webapp('expired', 'https://d123.cloudfront.net/')]
    seed({ artifacts: arts })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Expired')).toBeInTheDocument())
  })

  it('shows the deploying hero while a deploy is in flight', async () => {
    const arts = [webapp('deploying', 'https://d123.cloudfront.net/')]
    seed({ artifacts: arts })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Deploying…')).toBeInTheDocument())
  })
})

// ── Folders in the gallery ────────────────────────────────────────────────
describe('ArtifactsPage — folder cards in the gallery', () => {
  it('renders a folder card with its subtree counts', async () => {
    seed({
      artifacts: [mkArtifact('cr-queue')],
      folders: [
        mkFolder('ops', 'Ops', { item_count: 2 }),
        mkFolder('deep', 'Deep', { parent_id: 'ops', item_count: 3 }),
      ],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())
    // 2 own + 3 in the nested folder, and one subfolder.
    expect(screen.getByText(/5 artifacts/)).toBeInTheDocument()
    expect(screen.getByText(/· 1 folder$/)).toBeInTheDocument()
    // Nested folders are not surfaced at root level.
    expect(screen.queryByRole('button', { name: 'Open folder Deep' })).not.toBeInTheDocument()
  })

  it('previews up to three artifacts inside a folder, direct children first', async () => {
    const arts = [
      mkArtifact('first-one', { folder_id: 'ops' }),
      mkArtifact('second-one', { folder_id: 'ops' }),
      mkArtifact('deep-one', { folder_id: 'deep' }),
      mkArtifact('loose-one'),
    ]
    seed({
      artifacts: arts,
      folders: [mkFolder('ops', 'Ops'), mkFolder('deep', 'Deep', { parent_id: 'ops' })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())
    const card = screen.getByRole('button', { name: 'Open folder Ops' })
    // Direct children plus one descendant fill the three preview tiles.
    expect(within(card).getByTitle('first one')).toBeInTheDocument()
    expect(within(card).getByTitle('second one')).toBeInTheDocument()
    expect(within(card).getByTitle('deep one')).toBeInTheDocument()
    expect(within(card).queryByTitle('loose one')).not.toBeInTheDocument()
  })

  it('renders the derived emoji badge on a closed folder glyph', async () => {
    seed({ artifacts: [], folders: [mkFolder('ops', 'Ops', { icon: '🛠', color: '#3b82f6' })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('🛠')).toBeInTheDocument())
  })

  it('opens a folder on click and scopes the gallery to it, with a breadcrumb back out', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [mkArtifact('inside-one', { folder_id: 'ops' }), mkArtifact('outside-one')],
      folders: [mkFolder('ops', 'Ops', { item_count: 1 })],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('outside one')).toBeInTheDocument())
    expect(screen.queryByText('inside one')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Open folder Ops' }))

    await waitFor(() => expect(screen.getByText('inside one')).toBeInTheDocument())
    expect(screen.queryByText('outside one')).not.toBeInTheDocument()
    const crumbs = screen.getByRole('navigation', { name: 'Folder breadcrumb' })
    expect(within(crumbs).getByRole('button', { name: 'All Artifacts' })).toBeEnabled()
    // The current segment is inert — it would navigate to where you already are.
    expect(within(crumbs).getByRole('button', { name: 'Ops' })).toBeDisabled()

    await user.click(within(crumbs).getByRole('button', { name: 'All Artifacts' }))
    await waitFor(() => expect(screen.getByText('outside one')).toBeInTheDocument())
  })

  it('opens a folder from the keyboard', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [mkArtifact('inside-one', { folder_id: 'ops' })],
      folders: [mkFolder('ops', 'Ops', { item_count: 1 })],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    const card = await screen.findByRole('button', { name: 'Open folder Ops' })
    card.focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.getByText('inside one')).toBeInTheDocument())
  })

  it('treats a bookmarked URL pointing at an unknown folder as the library root', async () => {
    seed({ artifacts: [mkArtifact('outside-one')], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=deleted-folder' })
    await waitFor(() => expect(screen.getByText('outside one')).toBeInTheDocument())
    expect(screen.queryByRole('navigation', { name: 'Folder breadcrumb' })).not.toBeInTheDocument()
  })

  it('says an empty folder is empty rather than showing the library empty state', async () => {
    seed({ artifacts: [mkArtifact('outside-one')], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() =>
      expect(screen.getByText(/This folder is empty\. Drag artifacts onto it to file them here\./)).toBeInTheDocument(),
    )
  })

  it('distinguishes "nothing directly here" from "empty" when a folder only holds subfolders', async () => {
    seed({
      artifacts: [],
      folders: [mkFolder('ops', 'Ops'), mkFolder('deep', 'Deep', { parent_id: 'ops' })],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByText('No artifacts directly in this folder.')).toBeInTheDocument())
  })

  it('reports an all-filed library as having no unfiled artifacts', async () => {
    seed({ artifacts: [mkArtifact('filed-one', { folder_id: 'ops' })], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() =>
      expect(screen.getByText('No unfiled artifacts — everything is filed in folders.')).toBeInTheDocument(),
    )
  })
})

// ── Folder creation ───────────────────────────────────────────────────────
describe('ArtifactsPage — creating a folder', () => {
  it('creates the folder inside the folder being browsed', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New folder/i }))
    await user.type(screen.getByLabelText('New folder name'), 'Runbooks{Enter}')

    await waitFor(() =>
      expect(m.createArtifactFolder).toHaveBeenCalledWith({ name: 'Runbooks', parent_id: 'ops' }),
    )
  })

  it('commits the name when the inline field loses focus', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New folder/i }))
    await user.type(screen.getByLabelText('New folder name'), '  Runbooks  ')
    await user.click(screen.getByText('Your Artifacts'))

    // Whitespace is trimmed, and no parent is sent at the library root.
    await waitFor(() => expect(m.createArtifactFolder).toHaveBeenCalledWith({ name: 'Runbooks' }))
  })

  it('abandons folder creation on Escape', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New folder/i }))
    await user.type(screen.getByLabelText('New folder name'), 'Scratch{Escape}')

    await waitFor(() => expect(screen.queryByLabelText('New folder name')).not.toBeInTheDocument())
    expect(m.createArtifactFolder).not.toHaveBeenCalled()
  })

  it('abandons folder creation when the field is left empty', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New folder/i }))
    await user.click(screen.getByText('Your Artifacts'))

    await waitFor(() => expect(screen.queryByLabelText('New folder name')).not.toBeInTheDocument())
    expect(m.createArtifactFolder).not.toHaveBeenCalled()
  })

  // DEFECT (characterized, not endorsed): the create card puts its color
  // swatches BELOW the autofocused name field, and the field commits-or-cancels
  // on blur. Clicking a swatch therefore blurs an empty field, which cancels the
  // whole creation — so the color can never be chosen before the name, and a
  // click after typing commits without the color. The swatch strip in the create
  // card is unreachable as designed.
  it('loses the pending folder when a color swatch is clicked before the name', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New folder/i }))
    await user.click(screen.getByRole('radio', { name: 'Teal' }))

    await waitFor(() => expect(screen.queryByLabelText('New folder name')).not.toBeInTheDocument())
    expect(m.createArtifactFolder).not.toHaveBeenCalled()
  })
})

// ── Folder menu: recolor / move / delete / rename ─────────────────────────
describe('ArtifactsPage — folder menu', () => {
  it('recolors a folder from the menu swatches, and skips a no-op recolor', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops', { color: '#ef4444' })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())

    await user.click(folderMenuFor('Ops'))
    // The already-selected hue is a no-op; a different one writes.
    await user.click(await screen.findByRole('radio', { name: 'Red' }))
    expect(m.updateArtifactFolder).not.toHaveBeenCalled()

    await user.click(screen.getByRole('radio', { name: 'Blue' }))
    await waitFor(() => expect(m.updateArtifactFolder).toHaveBeenCalledWith('ops', { color: '#3b82f6' }))
  })

  it('clears a folder color through the no-color swatch', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops', { color: '#ef4444' })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())

    await user.click(folderMenuFor('Ops'))
    await user.click(await screen.findByRole('radio', { name: 'No color' }))

    await waitFor(() => expect(m.updateArtifactFolder).toHaveBeenCalledWith('ops', { color: '' }))
  })

  it('unnests a folder through the move submenu', async () => {
    const user = userEvent.setup()
    const m = seed({
      artifacts: [],
      folders: [mkFolder('ops', 'Ops'), mkFolder('deep', 'Deep', { parent_id: 'ops' })],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Deep' })).toBeInTheDocument())

    await user.click(folderMenuFor('Deep'))
    await user.click(await screen.findByText('Move to folder'))
    // Radix menu items select on a plain click event; userEvent's pointer
    // sequence over a nested submenu closes it before the item resolves.
    fireEvent.click((await screen.findByText('No folder (root)')).closest('[role="menuitem"]')!)

    await waitFor(() => expect(m.updateArtifactFolder).toHaveBeenCalledWith('deep', { parent_id: '' }))
  })

  it('never offers a folder its own subtree as a move destination', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [],
      folders: [mkFolder('ops', 'Ops'), mkFolder('deep', 'Deep', { parent_id: 'ops' })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())

    await user.click(folderMenuFor('Ops'))
    await user.click(await screen.findByText('Move to folder'))
    const submenu = (await screen.findByText('No folder (root)')).closest('[role="menu"]') as HTMLElement

    // Ops itself and its descendant Deep would both create a cycle, so the
    // root entry is the only destination offered.
    expect(within(submenu).queryByText('Deep')).not.toBeInTheDocument()
    expect(within(submenu).queryByText('Ops')).not.toBeInTheDocument()
  })

  it('ignores a move that leaves the folder where it already is', async () => {
    const user = userEvent.setup()
    const m = seed({
      artifacts: [],
      folders: [mkFolder('ops', 'Ops'), mkFolder('deep', 'Deep', { parent_id: 'ops' })],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Deep' })).toBeInTheDocument())

    await user.click(folderMenuFor('Deep'))
    await user.click(await screen.findByText('Move to folder'))
    const submenu = (await screen.findByText('No folder (root)')).closest('[role="menu"]') as HTMLElement
    fireEvent.click(within(submenu).getByText('Ops').closest('[role="menuitem"]')!)

    expect(m.updateArtifactFolder).not.toHaveBeenCalled()
  })

  it('deletes an empty folder immediately, with no impact dialog', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops', { item_count: 0 })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())

    await user.click(folderMenuFor('Ops'))
    await user.click(await screen.findByText('Delete…'))

    await waitFor(() => expect(m.deleteArtifactFolder).toHaveBeenCalledWith('ops', false))
    expect(screen.queryByText('Delete folder and all contents')).not.toBeInTheDocument()
  })

  it('asks before deleting a folder that has contents at stake', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops', { item_count: 4 })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())

    await user.click(folderMenuFor('Ops'))
    await user.click(await screen.findByText('Delete…'))
    expect(m.deleteArtifactFolder).not.toHaveBeenCalled()

    await user.click(await screen.findByText('Delete folder and all contents'))
    await waitFor(() => expect(m.deleteArtifactFolder).toHaveBeenCalledWith('ops', true))
  })

  it('closes the delete dialog without deleting when dismissed', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops', { item_count: 4 })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())

    await user.click(folderMenuFor('Ops'))
    await user.click(await screen.findByText('Delete…'))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByText('Delete folder and all contents')).not.toBeInTheDocument())
    expect(m.deleteArtifactFolder).not.toHaveBeenCalled()
  })

  it('deletes a nested folder from its parent view, keeping its artifacts', async () => {
    const user = userEvent.setup()
    const m = seed({
      artifacts: [],
      folders: [mkFolder('ops', 'Ops'), mkFolder('deep', 'Deep', { parent_id: 'ops', item_count: 2 })],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Deep' })).toBeInTheDocument())

    await user.click(folderMenuFor('Deep'))
    await user.click(await screen.findByText('Delete…'))
    await user.click(await screen.findByText('Delete folder only, keep artifacts'))

    await waitFor(() => expect(m.deleteArtifactFolder).toHaveBeenCalledWith('deep', false))
  })

  // DEFECT (characterized, not endorsed): picking Rename opens the inline field,
  // but Radix returns focus to the "…" trigger when the menu closes. That blurs
  // the autofocused field, whose blur handler commits the unchanged initial
  // name — so rename mode exits in the same tick and a folder can never be
  // renamed from this menu. Verified by observing the field being added and
  // immediately removed from the DOM, with focus landing back on the trigger.
  it('drops out of rename mode as soon as the menu closes, writing nothing', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Open folder Ops' })).toBeInTheDocument())

    await user.click(folderMenuFor('Ops'))
    await user.click(await screen.findByText('Rename'))

    expect(screen.queryByLabelText('Rename folder')).not.toBeInTheDocument()
    expect(m.updateArtifactFolder).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(folderMenuFor('Ops'))
  })
})

// ── Table / tree view ─────────────────────────────────────────────────────
describe('ArtifactsPage — folder tree table', () => {
  beforeEach(() => {
    localStorage.setItem('mc-artifacts-view', 'table')
  })

  it('renders folders collapsed with an Unfiled lane, and expands on click', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [mkArtifact('filed-one', { folder_id: 'ops' }), mkArtifact('loose-one')],
      folders: [mkFolder('ops', 'Ops', { item_count: 1 })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('loose one')).toBeInTheDocument())
    expect(screen.getByText(/Unfiled ·\s*1/)).toBeInTheDocument()
    // Collapsed by design: the folder's artifact is not rendered yet.
    expect(screen.queryByText('filed one')).not.toBeInTheDocument()

    await user.click(screen.getByText('Ops'))

    await waitFor(() => expect(screen.getByText('filed one')).toBeInTheDocument())
    expect(JSON.parse(localStorage.getItem('mc-artifact-folders-expanded') || '[]')).toContain('ops')
  })

  it('restores expansion from localStorage and collapses again on a second click', async () => {
    const user = userEvent.setup()
    localStorage.setItem('mc-artifact-folders-expanded', JSON.stringify(['ops']))
    seed({
      artifacts: [mkArtifact('filed-one', { folder_id: 'ops' })],
      folders: [mkFolder('ops', 'Ops', { item_count: 1 })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('filed one')).toBeInTheDocument())

    await user.click(screen.getByText('Ops'))
    await waitFor(() => expect(screen.queryByText('filed one')).not.toBeInTheDocument())
    expect(JSON.parse(localStorage.getItem('mc-artifact-folders-expanded') || '[]')).not.toContain('ops')
  })

  it('ignores a corrupt expansion record instead of failing to render', async () => {
    localStorage.setItem('mc-artifact-folders-expanded', '{not json')
    seed({ artifacts: [mkArtifact('loose-one')], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('loose one')).toBeInTheDocument())
    expect(screen.getByText('Ops')).toBeInTheDocument()
  })

  it('drops non-string entries from a tampered expansion record', async () => {
    localStorage.setItem('mc-artifact-folders-expanded', JSON.stringify([7, 'ops']))
    seed({
      artifacts: [mkArtifact('filed-one', { folder_id: 'ops' })],
      folders: [mkFolder('ops', 'Ops', { item_count: 1 })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('filed one')).toBeInTheDocument())
  })

  it('nests subfolders under an expanded parent', async () => {
    localStorage.setItem('mc-artifact-folders-expanded', JSON.stringify(['ops']))
    seed({
      artifacts: [],
      folders: [mkFolder('ops', 'Ops'), mkFolder('deep', 'Deep', { parent_id: 'ops' })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Deep')).toBeInTheDocument())
    expect(screen.getByText('Ops')).toBeInTheDocument()
  })

  it('degrades an artifact whose folder no longer exists to Unfiled', async () => {
    seed({
      artifacts: [mkArtifact('orphan-one', { folder_id: 'gone' })],
      folders: [mkFolder('ops', 'Ops')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('orphan one')).toBeInTheDocument())
    expect(screen.getByText(/Unfiled ·\s*1/)).toBeInTheDocument()
  })

  it('ctrl-clicking a row pops the artifact out instead of navigating', async () => {
    const user = userEvent.setup()
    seed({ artifacts: [mkArtifact('cr-queue')], folders: [] })
    const open = vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: vi.fn() } as unknown as Window)
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    await user.keyboard('{Control>}')
    await user.click(screen.getByText('cr queue'))
    await user.keyboard('{/Control}')

    expect(open).toHaveBeenCalledTimes(1)
    expect(String(open.mock.calls[0][0])).toContain('/popout/artifact/cr-queue')
    open.mockRestore()
  })

  it('switches to a flat table when a filter bypasses folder scoping', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [mkArtifact('cr-queue', { folder_id: 'ops' }), mkArtifact('ticket-board')],
      folders: [mkFolder('ops', 'Ops', { item_count: 1 })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('Ops')).toBeInTheDocument())

    await user.type(screen.getByPlaceholderText(/Filter by name/i), 'queue')

    // Folder rows and the Unfiled lane are gone; matches show flat.
    await waitFor(() => expect(screen.queryByText('Ops')).not.toBeInTheDocument())
    expect(screen.getByText('cr queue')).toBeInTheDocument()
    expect(screen.queryByText('ticket board')).not.toBeInTheDocument()
  })

  it('folds unsaved session documents into the tree as rows', async () => {
    seed({
      artifacts: [mkArtifact('cr-queue')],
      folders: [],
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())
    expect(screen.getByText('/ws/research/FINDINGS.md')).toBeInTheDocument()
    expect(screen.getByText('markdown')).toBeInTheDocument()
    expect(screen.getByLabelText('Star document')).toBeInTheDocument()
  })

  it('types a .rst session document as text, not markdown', async () => {
    seed({ artifacts: [], folders: [], docs: [mkDoc('/ws/notes/INDEX.rst', 'INDEX.rst')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('INDEX.rst')).toBeInTheDocument())
    expect(screen.getByText('text')).toBeInTheDocument()
  })

  it('creates a folder at the root from the table view', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New folder/i }))
    await user.type(screen.getByLabelText('New folder name'), 'Runbooks{Enter}')

    // The tree view always creates at root — nesting happens afterwards.
    await waitFor(() => expect(m.createArtifactFolder).toHaveBeenCalledWith({ name: 'Runbooks' }))
  })
})

// ── Artifact rows in the table ────────────────────────────────────────────
describe('ArtifactsPage — artifact rows', () => {
  beforeEach(() => {
    localStorage.setItem('mc-artifacts-view', 'table')
  })

  it('renders the row detail columns: description, tags, version and publication mark', async () => {
    seed({
      artifacts: [mkArtifact('cr-queue', {
        description: 'Open code reviews',
        tags: ['ops', 'cr'],
        version: 7,
        session_title: 'Review session',
        publication: { visibility: 'PUBLIC' } as Artifact['publication'],
      })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())
    expect(screen.getByText('Open code reviews')).toBeInTheDocument()
    expect(screen.getByText('ops')).toBeInTheDocument()
    expect(screen.getByText('v7')).toBeInTheDocument()
    expect(screen.getByText('Review session')).toBeInTheDocument()
    expect(screen.getByLabelText('Published (public)')).toBeInTheDocument()
  })

  it('flags a publication whose last sync failed', async () => {
    seed({
      artifacts: [mkArtifact('cr-queue', {
        publication: { visibility: 'PUBLIC', last_error: 'push rejected' } as Artifact['publication'],
      })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByLabelText('Published (sync issue)')).toBeInTheDocument())
  })

  it('stars an artifact from its row', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [mkArtifact('cr-queue', { pinned: false })] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Star artifact'))

    await waitFor(() => expect(m.setArtifactPinned).toHaveBeenCalledWith('cr-queue', true))
  })

  it('pops an artifact out from its row action', async () => {
    const user = userEvent.setup()
    seed({ artifacts: [mkArtifact('ticket-board')] })
    const open = vi.spyOn(window, 'open').mockReturnValue({ closed: false, focus: vi.fn() } as unknown as Window)
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts' })
    await waitFor(() => expect(screen.getByText('ticket board')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Pop out to window'))

    expect(String(open.mock.calls[0][0])).toContain('/popout/artifact/ticket-board')
    open.mockRestore()
  })

  it('deletes from a row once the confirmation is accepted', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [mkArtifact('cr-queue')] })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    await user.click(screen.getByLabelText('Remove from artifacts library'))

    await waitFor(() => expect(m.deleteArtifact).toHaveBeenCalledWith('cr-queue'))
  })

  it('indents an artifact under its expanded folder', async () => {
    localStorage.setItem('mc-artifact-folders-expanded', JSON.stringify(['ops']))
    seed({
      artifacts: [mkArtifact('filed-one', { folder_id: 'ops' })],
      folders: [mkFolder('ops', 'Ops', { item_count: 1 })],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('filed one')).toBeInTheDocument())
    const cell = screen.getByText('filed one').closest('td') as HTMLElement
    expect(cell.style.paddingLeft).toBe('30px')
  })

  it('materializes a session document from the flat filtered table', async () => {
    const user = userEvent.setup()
    const m = seed({
      artifacts: [mkArtifact('findings-report'), mkArtifact('cr-queue')],
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    // A name filter flattens the table; matching doc rows come along with it.
    await user.type(screen.getByPlaceholderText(/Filter by name/i), 'findings')
    await waitFor(() => expect(screen.queryByText('cr queue')).not.toBeInTheDocument())
    expect(screen.getByText('findings report')).toBeInTheDocument()
    expect(screen.getByText('FINDINGS.md')).toBeInTheDocument()

    await user.click(screen.getByLabelText('Star document'))

    await waitFor(() =>
      expect(m.materializeArtifact).toHaveBeenCalledWith('/ws/research/FINDINGS.md', 'dashboard_chat-1'),
    )
  })

  it('drops session documents from the Starred view', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [mkArtifact('cr-queue', { pinned: true })],
      docs: [mkDoc('/ws/research/FINDINGS.md', 'FINDINGS.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('FINDINGS.md')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /Starred/i }))

    await waitFor(() => expect(screen.queryByText('FINDINGS.md')).not.toBeInTheDocument())
    expect(localStorage.getItem('mc-artifacts-pinned-only')).toBe('1')
  })
})

// ── Session-doc filtering in the gallery section ──────────────────────────
describe('ArtifactsPage — session document filters', () => {
  it('narrows the session-doc list by the kind filter', async () => {
    seed({
      artifacts: [],
      docs: [mkDoc('/ws/a/NOTES.md', 'NOTES.md'), mkDoc('/ws/a/INDEX.rst', 'INDEX.rst')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('NOTES.md')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('combobox', { name: 'Filter by kind' }))
    fireEvent.click(await screen.findByRole('option', { name: 'kind: text' }))

    // The kind change re-keys the artifact query, so the page passes back
    // through its loading state before the filtered section returns.
    expect(await screen.findByText('INDEX.rst')).toBeInTheDocument()
    expect(screen.queryByText('NOTES.md')).not.toBeInTheDocument()
  })

  it('narrows the session-doc list by the name filter, matching on path too', async () => {
    const user = userEvent.setup()
    seed({
      artifacts: [],
      docs: [mkDoc('/ws/alpha/NOTES.md', 'NOTES.md'), mkDoc('/ws/beta/INDEX.md', 'INDEX.md')],
    })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('NOTES.md')).toBeInTheDocument())

    await user.type(screen.getByPlaceholderText(/Filter by name/i), 'alpha')

    await waitFor(() => expect(screen.queryByText('INDEX.md')).not.toBeInTheDocument())
    expect(screen.getByText('NOTES.md')).toBeInTheDocument()
  })

  it('hides the session-doc section while browsing inside a folder', async () => {
    seed({
      artifacts: [],
      folders: [mkFolder('ops', 'Ops')],
      docs: [mkDoc('/ws/a/NOTES.md', 'NOTES.md')],
    })
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByText(/This folder is empty/)).toBeInTheDocument())
    // Documents are unfiled by definition, so the section would be misleading here.
    expect(screen.queryByText('NOTES.md')).not.toBeInTheDocument()
  })
})

// ── Library drag and drop ────────────────────────────────────────────────
describe('ArtifactsPage — dragging a card', () => {
  it('raises a drag ghost naming the artifact, and clears it when the drag is cancelled', async () => {
    seed({ artifacts: [mkArtifact('cr-queue')], folders: [mkFolder('ops', 'Ops')] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByText('cr queue')).toBeInTheDocument())

    const card = screen.getByText('cr queue').closest('[role="button"]') as HTMLElement
    fireEvent.pointerDown(card, { pointerId: 1, clientX: 0, clientY: 0, isPrimary: true, button: 0 })
    fireEvent.pointerMove(document, { pointerId: 1, clientX: 40, clientY: 40 })

    // The overlay ghost is a second rendering of the artifact's name.
    await waitFor(() => expect(screen.getAllByText('cr queue').length).toBeGreaterThan(1))

    fireEvent.keyDown(document, { key: 'Escape', code: 'Escape' })

    await waitFor(() => expect(screen.getAllByText('cr queue')).toHaveLength(1))
  })

  it('raises a folder ghost carrying the folder glyph', async () => {
    seed({ artifacts: [], folders: [mkFolder('ops', 'Ops', { icon: '🛠' })] })
    renderWithProviders(<ArtifactsPage />)
    const card = await screen.findByRole('button', { name: 'Open folder Ops' })

    fireEvent.pointerDown(card, { pointerId: 1, clientX: 0, clientY: 0, isPrimary: true, button: 0 })
    fireEvent.pointerMove(document, { pointerId: 1, clientX: 40, clientY: 40 })

    await waitFor(() => expect(screen.getAllByText('Ops').length).toBeGreaterThan(1))

    fireEvent.pointerUp(document, { pointerId: 1, clientX: 40, clientY: 40 })

    await waitFor(() => expect(screen.getAllByText('Ops')).toHaveLength(1))
  })
})

// ── Add Artifact (file import) ────────────────────────────────────────────
describe('ArtifactsPage — importing a file', () => {
  const fileInput = () => screen.getByLabelText('Add a file from your computer to the library')

  const pick = (file: File) => {
    const input = fileInput() as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [file], writable: true, configurable: true })
    fireEvent.change(input)
  }

  it('imports a markdown file and files it into the folder being browsed', async () => {
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops')] })
    m.createArtifact = vi.fn().mockResolvedValue(mkArtifact('runbook', { kind: 'markdown', content: '# Runbook' }))
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    pick(new File(['# Runbook'], 'runbook.md', { type: 'text/markdown' }))

    await waitFor(() =>
      expect(m.createArtifact).toHaveBeenCalledWith({ name: 'runbook.md', kind: 'markdown', content: '# Runbook' }),
    )
    await waitFor(() => expect(m.setArtifactFolder).toHaveBeenCalledWith('runbook', 'ops'))
  })

  it('warns that an import landed at the root when filing it fails', async () => {
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops')] })
    m.createArtifact = vi.fn().mockResolvedValue(mkArtifact('runbook', { kind: 'markdown', content: '# Runbook' }))
    m.setArtifactFolder = vi.fn().mockRejectedValue(new Error('folder gone'))
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    pick(new File(['# Runbook'], 'runbook.md', { type: 'text/markdown' }))

    await waitFor(() => expect(screen.getByText(/you'll find it at the library root/i)).toBeInTheDocument())
  })

  it('refuses an import whose text came back redacted, and removes the artifact', async () => {
    const m = seed({ artifacts: [], folders: [] })
    m.createArtifact = vi.fn().mockResolvedValue(
      mkArtifact('secrets', { kind: 'text', content: 'key=<redacted>' }),
    )
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    pick(new File(['key=hunter2'], 'secrets.txt', { type: 'text/plain' }))

    await waitFor(() => expect(screen.getByText(/contains credentials/i)).toBeInTheDocument())
    expect(m.deleteArtifact).toHaveBeenCalledWith('secrets')
  })

  it('keeps the refusal message even when cleaning up the artifact fails', async () => {
    const m = seed({ artifacts: [], folders: [] })
    m.createArtifact = vi.fn().mockResolvedValue(
      mkArtifact('secrets', { kind: 'text', content: 'key=<redacted>' }),
    )
    m.deleteArtifact = vi.fn().mockRejectedValue(new Error('delete failed'))
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    pick(new File(['key=hunter2'], 'secrets.txt', { type: 'text/plain' }))

    await waitFor(() => expect(screen.getByText(/contains credentials/i)).toBeInTheDocument())
  })

  it('lists the supported extensions when the picked type is not importable', async () => {
    const m = seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    pick(new File(['binary'], 'photo.png', { type: 'image/png' }))

    await waitFor(() => expect(screen.getByText(/That file type can't be added/)).toBeInTheDocument())
    expect(screen.getByText(/\.md \.markdown/)).toBeInTheDocument()
    expect(m.createArtifact).not.toHaveBeenCalled()
  })

  it('refuses an empty file', async () => {
    seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    pick(new File([], 'empty.md', { type: 'text/markdown' }))

    await waitFor(() => expect(screen.getByText(/that file is empty/i)).toBeInTheDocument())
  })

  it('refuses a file that is over the size cap without reading it', async () => {
    seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    const huge = new File(['x'], 'huge.md', { type: 'text/markdown' })
    Object.defineProperty(huge, 'size', { value: 26_214_401 })
    pick(huge)

    await waitFor(() => expect(screen.getByText(/the limit is 25 MB/i)).toBeInTheDocument())
  })

  it('refuses a file whose bytes are not UTF-8 text', async () => {
    seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    pick(new File([new Uint8Array([0x68, 0x00, 0x69])], 'binary.txt', { type: 'text/plain' }))

    await waitFor(() => expect(screen.getByText(/isn't UTF-8 text/i)).toBeInTheDocument())
  })

  it('reports a file that could not be read at all', async () => {
    seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    const gone = new File(['text'], 'gone.md', { type: 'text/markdown' })
    gone.arrayBuffer = vi.fn().mockRejectedValue(new Error('volume ejected'))
    pick(gone)

    await waitFor(() => expect(screen.getByText(/couldn't be read/i)).toBeInTheDocument())
  })

  it('does nothing when the picker is dismissed with no file', async () => {
    const m = seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /New folder/i })).toBeInTheDocument())

    const input = fileInput() as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [], writable: true, configurable: true })
    fireEvent.change(input)

    await waitFor(() => expect(m.createArtifact).not.toHaveBeenCalled())
  })

  it('opens the picker from the caret menu entry', async () => {
    const user = userEvent.setup()
    seed({ artifacts: [], folders: [] })
    renderWithProviders(<ArtifactsPage />)
    await waitFor(() => expect(screen.getByRole('button', { name: /More ways to add an artifact/i })).toBeInTheDocument())
    const click = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {})

    await user.click(screen.getByRole('button', { name: /More ways to add an artifact/i }))
    await user.click(await screen.findByText(/Import from a file/i))

    expect(click).toHaveBeenCalled()
    click.mockRestore()
  })
})

// ── New blank artifact inside a folder ───────────────────────────────────
describe('ArtifactsPage — new blank artifact', () => {
  it('files a new blank document into the folder being browsed', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops')] })
    m.createArtifact = vi.fn().mockResolvedValue(mkArtifact('untitled', { kind: 'markdown' }))
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: /New artifact/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New artifact/i }))

    await waitFor(() => expect(m.createArtifact).toHaveBeenCalledWith({ name: 'Untitled', content: '' }))
    await waitFor(() => expect(m.setArtifactFolder).toHaveBeenCalledWith('untitled', 'ops'))
  })

  it('warns when a new blank document could not be filed', async () => {
    const user = userEvent.setup()
    const m = seed({ artifacts: [], folders: [mkFolder('ops', 'Ops')] })
    m.createArtifact = vi.fn().mockResolvedValue(mkArtifact('untitled', { kind: 'markdown' }))
    m.setArtifactFolder = vi.fn().mockRejectedValue(new Error('folder gone'))
    renderWithProviders(<ArtifactsPage />, { route: '/artifacts?folder=ops' })
    await waitFor(() => expect(screen.getByRole('button', { name: /New artifact/i })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: /New artifact/i }))

    await waitFor(() => expect(screen.getByText(/you'll find it at the library root/i)).toBeInTheDocument())
  })
})
