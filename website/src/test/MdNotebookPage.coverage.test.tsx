/**
 * `MdNotebookPage` — the Notes app shell.
 *
 * The page owns every stateful decision the Notes app makes: which vault is
 * active, which note is open, when a debounced edit reaches disk, and what to do
 * when a save is refused because the file moved underneath it. None of that was
 * covered — only `NoteRow`'s delete affordance had a test — so this file pins the
 * behaviour a user can observe: the three boot states, opening and editing a
 * note, the save-guard conflict banner and both of its exits, sync (success,
 * merge conflict, local-only vault, keyboard shortcut), search, the sort/view
 * menu, vault switching, row actions (pin, rename, duplicate, drag-to-file,
 * trash) and the stale-backend warning.
 *
 * The API module is mocked wholesale: the real client fetches through the
 * gateway proxy, and these tests are about what the page does with the replies,
 * not how they are transported.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { SAVE_DEBOUNCE_MS } from '../apps/md-notebook/constants'
import type { Note, Vault } from '../apps/md-notebook/types'

// Every capability the page requires. Kept as a literal rather than imported so
// a feature silently dropped from the page's own list shows up as a failing
// stale-backend test instead of passing by construction.
const ALL_FEATURES = [
  'createdAt',
  'attach',
  'changes',
  'saveGuard',
  'forget',
  'pat',
  'newNote',
  'move',
  'duplicate',
  'localOnly',
  'autoCommit',
  'trash',
  'trashOpen',
  'knowledge',
  'pickFolder',
]

const mockApi = {
  health: vi.fn(),
  listVaults: vi.fn(),
  cloneVault: vi.fn(),
  attachVault: vi.fn(),
  forgetVault: vi.fn(),
  setVaultKnowledge: vi.fn(),
  setPat: vi.fn(),
  pickFolder: vi.fn(),
  listNotes: vi.fn(),
  readNote: vi.fn(),
  saveNote: vi.fn(),
  deleteNote: vi.fn(),
  newNote: vi.fn(),
  duplicateNote: vi.fn(),
  moveNote: vi.fn(),
  sync: vi.fn(),
  commit: vi.fn(),
  openTrash: vi.fn(),
  search: vi.fn(),
  changes: vi.fn(),
}

vi.mock('../apps/md-notebook/api', async () => {
  const actual = await vi.importActual<typeof import('../apps/md-notebook/api')>(
    '../apps/md-notebook/api',
  )
  return { ...actual, notesApi: mockApi }
})

function vault(over: Partial<Vault> = {}): Vault {
  return {
    id: 'v1',
    name: 'Notebook',
    repo: 'you/notes',
    branch: 'main',
    localPath: '/home/u/notes',
    readOnly: false,
    ...over,
  }
}

// Fixed timestamps: the row's relative-time label is derived from these, and a
// `Date.now()` fixture would make that label straddle a bucket boundary.
const T = new Date('2026-01-15T10:00:00Z').getTime()

function baseNotes(): Note[] {
  return [
    { path: 'One.md', title: 'One', modifiedAt: T, createdAt: T, syncStatus: 'synced' },
    {
      path: 'folder/Two.md',
      title: 'Two',
      modifiedAt: T + 1000,
      createdAt: T + 1000,
      syncStatus: 'pending',
    },
  ]
}

/** The listing the backend currently reports, so a delete can actually remove one. */
let notesState: Note[] = []

const DOC = {
  path: 'One.md',
  content: '# Hello\n\nBody text',
  mtime: 7,
  meta: { frontmatter: {}, tags: [], links: [] },
  backlinks: [{ sourcePath: 'folder/Two.md', line: 3, context: 'see [[One]]' }],
}

async function renderPage() {
  const { default: MdNotebookPage } = await import('../apps/md-notebook/MdNotebookPage')
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MdNotebookPage />
    </QueryClientProvider>,
  )
}

/** Render, wait for the panel, then open `One.md`. */
async function renderWithOpenNote() {
  const view = await renderPage()
  await userEvent.click(await screen.findByRole('button', { name: 'One' }))
  await screen.findByText('Body text')
  return view
}

/** The row element for a note, so its hover actions can be scoped to it. */
function row(title: string): HTMLElement {
  return screen.getByRole('button', { name: title })
}

/**
 * Click one of a row's hover actions.
 *
 * `fireEvent`, not `userEvent`: the action bar is `pointer-events: none` until
 * the row is hovered (styles.ts), and there is no layout under test to hover
 * against — `userEvent` refuses the click on that basis alone.
 */
function clickRowAction(title: string, action: string): void {
  fireEvent.click(within(row(title)).getByRole('button', { name: action }))
}

describe('MdNotebookPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    notesState = baseNotes()
    mockApi.health.mockResolvedValue({ ok: true, features: ALL_FEATURES })
    mockApi.listVaults.mockResolvedValue({
      vaults: [vault(), vault({ id: 'v2', name: 'Second vault' })],
      hasPat: false,
      hasGhAuth: true,
    })
    mockApi.listNotes.mockImplementation(async (v: string | null) => ({
      notes: v === 'v2' ? [{ path: 'Other.md', title: 'Other', modifiedAt: T, syncStatus: 'synced' }] : notesState,
    }))
    mockApi.readNote.mockImplementation(async (_v: string | null, path: string) => ({
      ...DOC,
      path,
    }))
    mockApi.saveNote.mockResolvedValue({ ok: true, mtime: 8 })
    mockApi.deleteNote.mockImplementation(async (_v: string | null, path: string) => {
      notesState = notesState.filter(n => n.path !== path)
      return { ok: true }
    })
    mockApi.newNote.mockResolvedValue({ path: 'Untitled.md' })
    mockApi.duplicateNote.mockResolvedValue({ path: 'One 1.md' })
    mockApi.moveNote.mockResolvedValue({ ok: true, path: 'moved.md' })
    mockApi.sync.mockResolvedValue({
      result: { pushed: true, pulled: true, committed: [], conflicts: [] },
    })
    mockApi.commit.mockResolvedValue({
      result: { pushed: false, pulled: false, committed: [], conflicts: [] },
    })
    mockApi.openTrash.mockResolvedValue({ opened: true, empty: false, path: '/home/u/notes/.trash' })
    mockApi.search.mockResolvedValue({ results: [] })
    mockApi.changes.mockResolvedValue({ rev: 0, changed: [], watching: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ── boot states ───────────────────────────────────────────────────────────

  it('shows a loading line until the vault list arrives', async () => {
    // Never resolves: the point is the state BEFORE any reply, which the page
    // reaches by `vaults === null` rather than a separate flag.
    mockApi.listVaults.mockReturnValue(new Promise(() => {}))
    await renderPage()
    expect(await screen.findByText('Loading…')).toBeTruthy()
  })

  it('names the backend as unreachable when the vault list fails', async () => {
    mockApi.listVaults.mockRejectedValue(new Error('connect ECONNREFUSED'))
    await renderPage()
    expect(
      await screen.findByText(/Could not reach the Notes backend: connect ECONNREFUSED/),
    ).toBeTruthy()
  })

  it('offers the connect screen instead of an empty notebook when no vault exists', async () => {
    mockApi.listVaults.mockResolvedValue({ vaults: [], hasPat: false, hasGhAuth: false })
    await renderPage()
    // The connect screen's own tabs, not its title — the panel's menu carries an
    // identically-worded entry, so the title alone would not prove which surface
    // rendered.
    expect(await screen.findByText('Clone a repo')).toBeTruthy()
    expect(screen.getByText('Attach a folder')).toBeTruthy()
  })

  // ── listing ───────────────────────────────────────────────────────────────

  it('renders the vault name, its notes in a folder tree, and the empty-body prompt', async () => {
    await renderPage()
    expect(await screen.findByRole('button', { name: 'One' })).toBeTruthy()
    // Folders view is the default, so the nested note sits under a folder row.
    expect(screen.getByRole('button', { name: 'folder' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Two' })).toBeTruthy()
    expect(screen.getByText('Notebook')).toBeTruthy()
    expect(screen.getByText('Select a note, or create one with the + button.')).toBeTruthy()
  })

  it('surfaces a listing failure as an alert rather than an empty panel', async () => {
    mockApi.listNotes.mockRejectedValue(new Error('git index locked'))
    await renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('git index locked')
  })

  it('collapses a folder, hiding the notes inside it', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'folder' }))
    expect(screen.queryByRole('button', { name: 'Two' })).toBeNull()
    expect(screen.getByRole('button', { name: 'One' })).toBeTruthy()
  })

  it('persists a collapsed folder per vault, and clears it on re-expand', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'folder' }))
    await waitFor(() => expect(localStorage.getItem('mdnb-collapsed-v1')).toBe('["folder"]'))

    await userEvent.click(screen.getByRole('button', { name: 'folder' }))
    await waitFor(() => expect(localStorage.getItem('mdnb-collapsed-v1')).toBe('[]'))
  })

  it('restores collapsed folders on mount, so the tree keeps its shape', async () => {
    localStorage.setItem('mdnb-active-vault', '"v1"')
    localStorage.setItem('mdnb-collapsed-v1', '["folder"]')
    await renderPage()
    // The folder itself is listed; only what is inside it stays hidden.
    expect(await screen.findByRole('button', { name: 'folder' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Two' })).toBeNull()
  })

  it('keeps one vault’s collapsed folders out of another’s', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'folder' }))
    await waitFor(() => expect(localStorage.getItem('mdnb-collapsed-v1')).toBe('["folder"]'))

    await userEvent.click(screen.getByRole('button', { name: 'Switch vault' }))
    await userEvent.click(screen.getByRole('option', { name: /Second vault/ }))
    await screen.findByRole('button', { name: 'Other' })
    // The second vault has its own (absent) entry rather than inheriting one a
    // folder name it does not have would have silently collapsed.
    expect(localStorage.getItem('mdnb-collapsed-v2')).toBeNull()
    expect(localStorage.getItem('mdnb-collapsed-v1')).toBe('["folder"]')
  })

  // ── opening a note ────────────────────────────────────────────────────────

  it('opens a note on click, showing its body and its backlinks', async () => {
    await renderWithOpenNote()
    expect(mockApi.readNote).toHaveBeenCalledWith('v1', 'One.md')
    expect(screen.getByText('Hello')).toBeTruthy()
    expect(screen.getByText('Linked from 1')).toBeTruthy()
    // The open note is remembered so a reload lands back on it.
    expect(localStorage.getItem('mdnb-open-note')).toBe('"One.md"')
  })

  it('opens the source note when a backlink is clicked', async () => {
    await renderWithOpenNote()
    await userEvent.click(screen.getByRole('button', { name: 'folder/Two.md' }))
    await waitFor(() => expect(mockApi.readNote).toHaveBeenCalledWith('v1', 'folder/Two.md'))
  })

  it('restores the remembered note on mount', async () => {
    localStorage.setItem('mdnb-open-note', '"folder/Two.md"')
    await renderPage()
    await waitFor(() => expect(mockApi.readNote).toHaveBeenCalledWith('v1', 'folder/Two.md'))
  })

  it('does not restore a remembered note the vault no longer has', async () => {
    localStorage.setItem('mdnb-open-note', '"Deleted.md"')
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    expect(mockApi.readNote).not.toHaveBeenCalled()
  })

  // ── raw / rendered ────────────────────────────────────────────────────────

  it('switches between rendered and raw markdown, remembering the choice', async () => {
    await renderWithOpenNote()
    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    const ta = screen.getByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    expect(ta.value).toBe('# Hello\n\nBody text')
    expect(localStorage.getItem('mdnb-view')).toBe('"raw"')

    await userEvent.click(screen.getByRole('button', { name: 'Rendered' }))
    expect(screen.queryByRole('textbox', { name: 'Markdown source' })).toBeNull()
    expect(localStorage.getItem('mdnb-view')).toBe('"rendered"')
  })

  it('debounces an edit before persisting it, and does not save on the keystroke', async () => {
    await renderWithOpenNote()
    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    const ta = screen.getByRole('textbox', { name: 'Markdown source' })

    // Fake timers only from here: the debounce is the thing under test, and the
    // mount above needs real promise scheduling to settle first.
    vi.useFakeTimers()
    fireEvent.change(ta, { target: { value: '# Hello\n\nEdited body' } })
    expect(mockApi.saveNote).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    })
    expect(mockApi.saveNote).toHaveBeenCalledWith('v1', 'One.md', '# Hello\n\nEdited body', 7)
  })

  // ── save guard ────────────────────────────────────────────────────────────

  /** Put the page in the state where a save has been refused as stale. */
  async function reachDiskConflict() {
    await renderWithOpenNote()
    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Markdown source' }), {
      target: { value: 'mine' },
    })
    mockApi.saveNote.mockRejectedValueOnce(
      Object.assign(new Error('stale'), {
        body: { code: 'ESTALE', mtime: 99, disk: 'theirs' },
      }),
    )
    // Sync flushes the pending edit immediately, which is what trips the guard.
    await userEvent.click(screen.getByRole('button', { name: 'Sync' }))
    await screen.findByText('This note changed on disk since you opened it.')
  }

  it('offers both versions when the file changed on disk instead of clobbering either', async () => {
    await reachDiskConflict()
    expect(screen.getByRole('button', { name: 'Use the version on disk' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Keep my version' })).toBeTruthy()
    // A refused save must not be reported as a completed sync.
    expect(screen.getByRole('button', { name: 'Sync' })).toBeTruthy()
  })

  it('takes the version on disk, discarding the in-memory edit', async () => {
    await reachDiskConflict()
    await userEvent.click(screen.getByRole('button', { name: 'Use the version on disk' }))
    const ta = screen.getByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
    expect(ta.value).toBe('theirs')
    expect(screen.queryByText('This note changed on disk since you opened it.')).toBeNull()
  })

  it("keeps the editor's version, re-saving it against the fresh mtime", async () => {
    await reachDiskConflict()
    await userEvent.click(screen.getByRole('button', { name: 'Keep my version' }))
    // 99 is the mtime the backend reported with the refusal — without adopting it
    // the retry would be refused again forever.
    await waitFor(() => expect(mockApi.saveNote).toHaveBeenCalledWith('v1', 'One.md', 'mine', 99))
  })

  // ── sync ──────────────────────────────────────────────────────────────────

  it('syncs and then reports how long ago that was', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Sync' }))
    await waitFor(() => expect(mockApi.sync).toHaveBeenCalledWith('v1'))
    expect(await screen.findByRole('button', { name: 'Synced just now' })).toBeTruthy()
  })

  it('reports a merge conflict rather than claiming a successful sync', async () => {
    mockApi.sync.mockResolvedValue({
      result: {
        pushed: false,
        pulled: true,
        committed: [],
        conflicts: [{ path: 'One.md', local: 'a', remote: 'b' }],
      },
    })
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Sync' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Sync stopped on a merge conflict')
    expect(alert.textContent).toContain('One.md')
    // Still "Sync", never "Synced just now": nothing was pushed.
    expect(screen.getByRole('button', { name: 'Sync' })).toBeTruthy()
  })

  it('surfaces a sync failure as an alert', async () => {
    mockApi.sync.mockRejectedValue(new Error('remote hung up'))
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Sync' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('remote hung up')
  })

  it('labels the action for what it does on a vault with no remote, and drops the pending badge', async () => {
    mockApi.listVaults.mockResolvedValue({
      vaults: [vault({ localOnly: true })],
      hasPat: false,
      hasGhAuth: false,
    })
    await renderPage()
    expect(await screen.findByRole('button', { name: 'Save locally' })).toBeTruthy()
    // "pending" means "not yet pushed", which has no meaning without a remote.
    expect(screen.queryByText('pending')).toBeNull()
  })

  it('runs a sync from the keyboard shortcut', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    fireEvent.keyDown(window, { key: 's', metaKey: true })
    await waitFor(() => expect(mockApi.sync).toHaveBeenCalledWith('v1'))
  })

  // ── stale backend ─────────────────────────────────────────────────────────

  it('names the capabilities a stale backend is missing, and withholds delete', async () => {
    mockApi.health.mockResolvedValue({
      ok: true,
      features: ALL_FEATURES.filter(f => f !== 'trash' && f !== 'knowledge'),
    })
    await renderPage()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('running older code than this page')
    expect(alert.textContent).toContain('trash, knowledge')
    // The confirmation promises the note is restorable from `.trash`; without the
    // capability the honest UI offers no delete at all.
    expect(screen.queryByRole('button', { name: 'Delete note' })).toBeNull()
  })

  // ── search ────────────────────────────────────────────────────────────────

  it('swaps the tree for ranked results while searching, and restores it when cleared', async () => {
    mockApi.search.mockResolvedValue({
      results: [{ path: 'folder/Two.md', title: 'Two', score: 1, snippet: null }],
    })
    await renderPage()
    const box = await screen.findByRole('textbox', { name: 'Search notes' })
    await userEvent.type(box, 'two')
    // The tree is dropped the moment the query is non-empty; the ranked results
    // arrive after the 150ms debounce, so wait on the RESULT, not the absence.
    expect(await screen.findByRole('button', { name: 'Two' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'One' })).toBeNull()
    // Flat results, so the folder row is gone too.
    expect(screen.queryByRole('button', { name: 'folder' })).toBeNull()

    await userEvent.clear(box)
    expect(await screen.findByRole('button', { name: 'One' })).toBeTruthy()
  })

  it('says so when a search matches nothing', async () => {
    await renderPage()
    await userEvent.type(await screen.findByRole('textbox', { name: 'Search notes' }), 'zzz')
    expect(await screen.findByText('No matches')).toBeTruthy()
  })

  // ── sort and view menu ────────────────────────────────────────────────────

  it('switches to the flat list view and remembers it', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Sort and view' }))
    await userEvent.click(screen.getByRole('button', { name: 'Flat list' }))
    expect(localStorage.getItem('mdnb-list-view')).toBe('"list"')
    // No folder tree in flat view; the note itself is still listed.
    expect(screen.queryByRole('button', { name: 'folder' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Two' })).toBeTruthy()
  })

  it('changes the sort order, remembers it, and closes the menu', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Sort and view' }))
    await userEvent.click(screen.getByRole('button', { name: 'Modified — new to old' }))
    expect(localStorage.getItem('mdnb-sort')).toBe('"modified-desc"')
    expect(screen.queryByRole('button', { name: 'Flat list' })).toBeNull()
  })

  it('falls back to the default sort when the stored one no longer exists', async () => {
    localStorage.setItem('mdnb-sort', '"invented-order"')
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Sort and view' }))
    // A dropped sort id must not crash the list or leave nothing selected.
    expect(screen.getByRole('button', { name: 'File name — A to Z' })).toBeTruthy()
  })

  // ── vault switching ───────────────────────────────────────────────────────

  it('switches vault, clearing the editor and loading that vault’s notes', async () => {
    await renderWithOpenNote()
    await userEvent.click(screen.getByRole('button', { name: 'Switch vault' }))
    await userEvent.click(screen.getByRole('option', { name: /Second vault/ }))
    expect(await screen.findByRole('button', { name: 'Other' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'One' })).toBeNull()
    // The editor is cleared rather than left showing the old vault's note.
    expect(screen.getByText('Select a note, or create one with the + button.')).toBeTruthy()
    expect(localStorage.getItem('mdnb-active-vault')).toBe('"v2"')
  })

  it('reaches the connect screen from the vault menu', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Switch vault' }))
    await userEvent.click(screen.getByRole('button', { name: 'Connect a vault' }))
    expect(await screen.findByText('Clone a repo')).toBeTruthy()
  })

  // ── panel ─────────────────────────────────────────────────────────────────

  it('hides and shows the notes panel, remembering the choice', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'Hide notes panel' }))
    expect(screen.queryByRole('textbox', { name: 'Search notes' })).toBeNull()
    expect(localStorage.getItem('mdnb-panel-open')).toBe('false')

    await userEvent.click(screen.getByRole('button', { name: 'Show notes panel' }))
    expect(screen.getByRole('textbox', { name: 'Search notes' })).toBeTruthy()
  })

  it('ignores a stored panel width outside the drag bounds', async () => {
    localStorage.setItem('mdnb-panel-width', '5000')
    await renderPage()
    // A corrupt width must not collapse or overflow the panel — the list still
    // renders at the default width.
    expect(await screen.findByRole('button', { name: 'One' })).toBeTruthy()
  })

  // ── settings ──────────────────────────────────────────────────────────────

  it('opens Settings as a page in the note pane', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    // Two controls carry this name — the whole row and the gear pinned inside it.
    // The row is the one a user aims at.
    await userEvent.click(screen.getAllByRole('button', { name: 'Settings' })[0])
    expect(await screen.findByText('Vaults, GitHub access and sync')).toBeTruthy()
    // Settings replaces the note column, so the document controls go with it.
    expect(screen.queryByRole('button', { name: 'Markdown source' })).toBeNull()
  })

  // ── note actions ──────────────────────────────────────────────────────────

  it('creates a note at the top level and opens it', async () => {
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'New note at the top level' }))
    await waitFor(() => expect(mockApi.newNote).toHaveBeenCalledWith('v1'))
    await waitFor(() => expect(mockApi.readNote).toHaveBeenCalledWith('v1', 'Untitled.md'))
  })

  it('reports a failure to create a note', async () => {
    mockApi.newNote.mockRejectedValue(new Error('read-only vault'))
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'New note at the top level' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('read-only vault')
  })

  it('duplicates a note and opens the copy', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Duplicate note')
    await waitFor(() => expect(mockApi.duplicateNote).toHaveBeenCalledWith('v1', 'One.md'))
    await waitFor(() => expect(mockApi.readNote).toHaveBeenCalledWith('v1', 'One 1.md'))
  })

  it('pins a note, persists the pin per vault, and offers to unpin', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Pin note')
    expect(localStorage.getItem('mdnb-pinned-v1')).toBe('["One.md"]')
    expect(within(row('One')).getByRole('button', { name: 'Unpin note' })).toBeTruthy()

    clickRowAction('One', 'Unpin note')
    expect(localStorage.getItem('mdnb-pinned-v1')).toBe('[]')
  })

  it('renames a note in place, keeping its folder', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'Two' })
    clickRowAction('Two', 'Rename note')
    const field = await screen.findByRole('textbox', { name: 'Note name' })
    await userEvent.clear(field)
    await userEvent.type(field, 'Renamed{Enter}')
    await waitFor(() =>
      expect(mockApi.moveNote).toHaveBeenCalledWith('v1', 'folder/Two.md', 'folder/Renamed.md'),
    )
  })

  it('strips path separators from a rename so a title edit cannot move the note', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Rename note')
    const field = await screen.findByRole('textbox', { name: 'Note name' })
    await userEvent.clear(field)
    await userEvent.type(field, '../etc/passwd{Enter}')
    await waitFor(() =>
      expect(mockApi.moveNote).toHaveBeenCalledWith('v1', 'One.md', '..etcpasswd.md'),
    )
  })

  it('files a dragged note into the folder it is dropped on', async () => {
    await renderPage()
    const folderRow = await screen.findByRole('button', { name: 'folder' })
    fireEvent.drop(folderRow, { dataTransfer: { getData: () => 'One.md' } })
    await waitFor(() => expect(mockApi.moveNote).toHaveBeenCalledWith('v1', 'One.md', 'folder/One.md'))
  })

  it('files a note at the vault root when dropped on the list background', async () => {
    await renderPage()
    const twoRow = await screen.findByRole('button', { name: 'Two' })
    // The scrolling list is the drop target that means "outside every folder".
    const list = twoRow.parentElement!.parentElement!
    fireEvent.drop(list, { dataTransfer: { getData: () => 'folder/Two.md' } })
    await waitFor(() => expect(mockApi.moveNote).toHaveBeenCalledWith('v1', 'folder/Two.md', 'Two.md'))
  })

  // ── delete ────────────────────────────────────────────────────────────────

  it('confirms before trashing a note, then removes it from the listing', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Delete note')
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(screen.getByText(/to trash\?/)).toBeTruthy()

    await userEvent.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(mockApi.deleteNote).toHaveBeenCalledWith('v1', 'One.md'))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'One' })).toBeNull())
  })

  it('cancels the confirmation without deleting anything', async () => {
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Delete note')
    await userEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(mockApi.deleteNote).not.toHaveBeenCalled()
  })

  it('says nothing has been trashed yet rather than opening an empty folder', async () => {
    mockApi.openTrash.mockResolvedValue({ opened: false, empty: true, path: '' })
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Delete note')
    await userEvent.click(await screen.findByRole('button', { name: '.trash' }))
    expect(
      await screen.findByText(
        'Nothing has been deleted from this vault yet, so there is no .trash folder.',
      ),
    ).toBeTruthy()
    // The message is feedback, not a third choice — the dialog stays open.
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('explains when the host cannot open a folder at all', async () => {
    mockApi.openTrash.mockRejectedValue(
      Object.assign(new Error('nope'), { body: { code: 'folder_open_unsupported' } }),
    )
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Delete note')
    await userEvent.click(await screen.findByRole('button', { name: '.trash' }))
    expect(
      await screen.findByText('Opening a folder is not supported on this system.'),
    ).toBeTruthy()
  })

  it('reports a failed delete instead of leaving the row pending forever', async () => {
    mockApi.deleteNote.mockRejectedValue(new Error('permission denied'))
    await renderPage()
    await screen.findByRole('button', { name: 'One' })
    clickRowAction('One', 'Delete note')
    await userEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('permission denied')
    // The row is interactive again, not stuck dimmed.
    expect(row('One').getAttribute('aria-disabled')).toBeNull()
  })

  // ── editing a rendered block ──────────────────────────────────────────────

  it('edits a rendered block in place and writes the result back into the note', async () => {
    await renderWithOpenNote()
    await userEvent.click(screen.getByRole('button', { name: 'Body text' }))
    const editor = screen.getByRole('textbox', { name: 'Edit block' })
    fireEvent.change(editor, { target: { value: 'Rewritten body' } })
    fireEvent.blur(editor)
    // The editor closes and the rewritten source is what the preview now shows.
    expect(await screen.findByText('Rewritten body')).toBeTruthy()
    expect(screen.queryByRole('textbox', { name: 'Edit block' })).toBeNull()
  })

  it('refuses to sync while a block is being edited, so the draft is not lost', async () => {
    await renderWithOpenNote()
    await userEvent.click(screen.getByRole('button', { name: 'Body text' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit block' }), {
      target: { value: 'half-typed' },
    })
    // Via the shortcut, not the button: clicking the button blurs the editor,
    // which commits the draft first, so the button can never reach the guard.
    fireEvent.keyDown(window, { key: 's', metaKey: true })
    // A sync would flush the stale buffer and then reload the note, unmounting
    // the editor with the typed text still only in its local state.
    expect(mockApi.sync).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Edit block' })).toBeTruthy()
  })

  it('ticks a task checkbox in the rendered view', async () => {
    mockApi.readNote.mockResolvedValue({ ...DOC, content: '- [ ] water the plants' })
    await renderPage()
    await userEvent.click(await screen.findByRole('button', { name: 'One' }))
    const box = (await screen.findByRole('checkbox')) as HTMLInputElement
    expect(box.checked).toBe(false)
    fireEvent.click(box)
    await waitFor(() => expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true))
  })

  // ── background timers ─────────────────────────────────────────────────────

  /** Mount under fake timers, so the page's own intervals can be driven. */
  async function renderOnFakeTimers() {
    vi.useFakeTimers()
    const view = await renderPage()
    // Two ticks: the vault read and the note read it triggers.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    return view
  }

  it('reloads the listing when the external-change poll reports a new revision', async () => {
    mockApi.changes.mockResolvedValue({ rev: 1, changed: ['One.md'], watching: true })
    await renderOnFakeTimers()
    expect(mockApi.listNotes).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(mockApi.changes).toHaveBeenCalled()
    expect(mockApi.listNotes).toHaveBeenCalledTimes(2)
  })

  it('ignores a poll that reports the revision it already has', async () => {
    await renderOnFakeTimers()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    // Same rev: a quiet tick must stay invisible rather than re-fetching.
    expect(mockApi.listNotes).toHaveBeenCalledTimes(1)
  })

  it('commits to local history on the autosave timer without pushing', async () => {
    await renderOnFakeTimers()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000)
    })
    expect(mockApi.commit).toHaveBeenCalledWith('v1')
    expect(mockApi.sync).not.toHaveBeenCalled()
  })

  it('does not poke a read-only vault with autosave commits', async () => {
    mockApi.listVaults.mockResolvedValue({
      vaults: [vault({ readOnly: true })],
      hasPat: false,
      hasGhAuth: false,
    })
    await renderOnFakeTimers()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000)
    })
    expect(mockApi.commit).not.toHaveBeenCalled()
  })

  it('syncs on the auto-sync timer once it is enabled', async () => {
    localStorage.setItem('mdnb-auto-sync', 'true')
    localStorage.setItem('mdnb-auto-sync-mins', '1')
    await renderOnFakeTimers()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(mockApi.sync).toHaveBeenCalledWith('v1')
  })

  // NOT covered on purpose: a stored `mdnb-auto-sync-mins` of 0 is read back
  // unclamped (only the setter clamps), so the auto-sync effect schedules
  // `setInterval(…, 0)` — a tight sync loop. A test for that either pins the
  // defect or fails, so it is reported rather than written; the page clamps the
  // stored panel width and validates the stored sort id, and this value needs
  // the same treatment at load.
})
