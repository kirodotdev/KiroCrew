/**
 * `MdNotebookPage` — second pass, aimed at the paths the first coverage file
 * left cold.
 *
 * The existing suite pins the happy paths: boot, open, edit, sync, search, the
 * sort menu and the delete confirmation. What it never reaches is everything
 * that hangs off the Settings PAGE (every persisted preference, forgetting a
 * vault, storing a GitHub credential, knowledge indexing), the branches where a
 * mutation has to give way to an unresolved save, the editor's own keyboard
 * mechanics (Tab nesting, Enter splitting a block, Escape cancelling one,
 * frontmatter offsetting), and the panel's pointer drag.
 *
 * Those are the behaviours here. The API module is mocked wholesale for the same
 * reason the first file does it: the real client talks to the gateway, and what
 * is under test is what the page does with a reply.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render,
  screen,
  waitFor,
  within,
  fireEvent,
  createEvent,
  act,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { CHANGES_POLL_MS, SAVE_DEBOUNCE_MS } from '../apps/md-notebook/constants'
import type { Note, Vault } from '../apps/md-notebook/types'

/**
 * Declared rather than imported from the page, so a capability quietly dropped
 * from its own list shows up as a failure instead of passing by construction.
 */
const FEATURES = [
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

const api = {
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
  return { ...actual, notesApi: api }
})

/** A fixed clock: the row's relative-time label is derived from these. */
const AT = new Date('2026-03-02T09:00:00Z').getTime()

function aVault(over: Partial<Vault> = {}): Vault {
  return {
    id: 'v1',
    name: 'Notebook',
    repo: 'you/notes',
    branch: 'trunk',
    localPath: '/home/u/notes',
    readOnly: false,
    ...over,
  }
}

const SECOND_VAULT = aVault({ id: 'v2', name: 'Archive', localPath: '/home/u/archive' })

function twoNotes(): Note[] {
  return [
    { path: 'One.md', title: 'One', modifiedAt: AT, createdAt: AT, syncStatus: 'synced' },
    {
      path: 'folder/Two.md',
      title: 'Two',
      modifiedAt: AT + 500,
      createdAt: AT + 500,
      syncStatus: 'synced',
    },
  ]
}

const BODY = '# Hello\n\nBody text'

function doc(over: { path?: string; content?: string; mtime?: number } = {}) {
  return {
    path: 'One.md',
    content: BODY,
    mtime: 4,
    meta: { frontmatter: {}, tags: [], links: [] },
    backlinks: [],
    ...over,
  }
}

/** A save the page will never see resolve, so an in-flight state can be held. */
function pending<T>(): Promise<T> {
  return new Promise<T>(() => undefined)
}

/** A promise whose resolution this test controls, for ordering two reads. */
function deferred<T>(): { promise: Promise<T>; settle: (value: T) => void } {
  let settle!: (value: T) => void
  const promise = new Promise<T>(resolve => {
    settle = resolve
  })
  return { promise, settle }
}

/** An ESTALE rejection, the shape the save guard recognises. */
function staleRejection() {
  return Object.assign(new Error('stale'), {
    body: { code: 'ESTALE', mtime: 42, disk: 'the file on disk' },
  })
}

async function mount() {
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

/** Mount and open `One.md`, so the pane holds a note. */
async function mountWithNote() {
  const view = await mount()
  await userEvent.click(await screen.findByRole('button', { name: 'One' }))
  await screen.findByText('Body text')
  return view
}

/** Mount, open `One.md`, and leave an unsaved edit in the raw editor. */
async function mountDirty(text = '# Hello\n\nedited in the buffer') {
  const view = await mountWithNote()
  await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Markdown source' }), {
    target: { value: text },
  })
  return view
}

function noteRow(title: string): HTMLElement {
  return screen.getByRole('button', { name: title })
}

/**
 * Click a row's hover action. `fireEvent` because the action bar is
 * `pointer-events: none` until the row is hovered, which `userEvent` honours and
 * there is no layout here to hover against.
 */
function rowAction(title: string, action: string): void {
  fireEvent.click(within(noteRow(title)).getByRole('button', { name: action }))
}

/** Walk the delete confirmation for a row through to its Delete button. */
async function confirmDelete(title: string): Promise<void> {
  rowAction(title, 'Delete note')
  await userEvent.click(await screen.findByRole('button', { name: 'Delete' }))
}

/** Open the Settings page from the panel's bottom row. */
async function openSettings(): Promise<void> {
  // Two controls carry the name — the row and the gear inside it.
  await userEvent.click(screen.getAllByRole('button', { name: 'Settings' })[0])
  await screen.findByText('Vaults, GitHub access and sync')
}

/** The raw editor's textarea. */
function rawEditor(): HTMLTextAreaElement {
  return screen.getByRole('textbox', { name: 'Markdown source' }) as HTMLTextAreaElement
}

/**
 * Mount on fake timers with `One.md` restored from the stored preference, so the
 * page's own intervals can be driven without pushing user-event through them.
 */
async function mountOnFakeTimersWithNote() {
  localStorage.setItem('mdnb-open-note', '"One.md"')
  vi.useFakeTimers()
  const view = await mount()
  // Vault read, note listing, then the note itself.
  for (let i = 0; i < 3; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
  }
  return view
}

describe('MdNotebookPage — settings, guarded mutations and editor keys', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.health.mockResolvedValue({ ok: true, features: FEATURES })
    api.listVaults.mockResolvedValue({
      vaults: [aVault(), SECOND_VAULT],
      hasPat: false,
      hasGhAuth: false,
    })
    api.listNotes.mockImplementation(async (v: string | null) => ({
      notes:
        v === 'v2'
          ? [{ path: 'Archived.md', title: 'Archived', modifiedAt: AT, syncStatus: 'synced' }]
          : twoNotes(),
    }))
    api.readNote.mockImplementation(async (_v: string | null, path: string) =>
      doc({ path }),
    )
    api.saveNote.mockResolvedValue({ ok: true, mtime: 5 })
    api.deleteNote.mockResolvedValue({ ok: true })
    api.newNote.mockResolvedValue({ path: 'Untitled.md' })
    api.duplicateNote.mockResolvedValue({ path: 'One copy.md' })
    api.moveNote.mockResolvedValue({ ok: true, path: 'moved.md' })
    api.forgetVault.mockResolvedValue({ ok: true })
    api.setPat.mockResolvedValue({ hasPat: true, hasGhAuth: false })
    api.setVaultKnowledge.mockResolvedValue({ ok: true })
    api.attachVault.mockResolvedValue({
      vault: aVault({ id: 'v3', name: 'Third', localPath: '/home/u/third' }),
    })
    api.sync.mockResolvedValue({
      result: { pushed: true, pulled: true, committed: [], conflicts: [] },
    })
    api.commit.mockResolvedValue({
      result: { pushed: false, pulled: false, committed: [], conflicts: [] },
    })
    api.openTrash.mockResolvedValue({ opened: true, empty: false, path: '/home/u/notes/.trash' })
    api.search.mockResolvedValue({ results: [] })
    api.changes.mockResolvedValue({ rev: 0, changed: [], watching: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ── settings: persisted preferences ───────────────────────────────────────

  it('persists the autosave and auto-sync switches toggled in Settings', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    // Autosave ships ON, so the first click is the one that turns it off.
    await userEvent.click(screen.getByRole('switch', { name: 'Autosave to history' }))
    expect(localStorage.getItem('mdnb-auto-commit')).toBe('false')

    await userEvent.click(screen.getByRole('switch', { name: 'Auto sync' }))
    expect(localStorage.getItem('mdnb-auto-sync')).toBe('true')
  })

  it('clamps the auto-sync interval into its allowed range', async () => {
    localStorage.setItem('mdnb-auto-sync', 'true')
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    const interval = screen.getByRole('spinbutton', { name: 'Auto sync interval in minutes' })
    fireEvent.change(interval, { target: { value: '45' } })
    expect(localStorage.getItem('mdnb-auto-sync-mins')).toBe('45')

    // Above the ceiling: pinned rather than accepted.
    fireEvent.change(interval, { target: { value: '99999' } })
    expect(localStorage.getItem('mdnb-auto-sync-mins')).toBe('1440')

    // Zero is not a cadence — it falls back to the default rather than to zero.
    fireEvent.change(interval, { target: { value: '0' } })
    expect(localStorage.getItem('mdnb-auto-sync-mins')).toBe('10')
  })

  it('records a new manual-sync shortcut without that keystroke also syncing', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    await userEvent.click(screen.getByRole('button', { name: 'Manual sync shortcut' }))
    expect(screen.getByText('Press the keys you want to use — Esc to cancel.')).toBeTruthy()

    // The combination being RECORDED must not also run a sync: the page's own
    // capture handler bails out while Settings is listening.
    fireEvent.keyDown(window, { key: 'j', ctrlKey: true })
    expect(api.sync).not.toHaveBeenCalled()

    await waitFor(() =>
      expect(localStorage.getItem('mdnb-sync-shortcut')).toContain('"key":"j"'),
    )
    // Recording ended, so the stored combination now works as a shortcut.
    fireEvent.keyDown(window, { key: 'j', ctrlKey: true })
    await waitFor(() => expect(api.sync).toHaveBeenCalledWith('v1'))
  })

  // ── settings: vault administration ────────────────────────────────────────

  it('forgets the active vault, clearing the note pane and reloading the list', async () => {
    await mountWithNote()
    // After the removal the app knows only the other vault.
    api.listVaults.mockResolvedValue({
      vaults: [SECOND_VAULT],
      hasPat: false,
      hasGhAuth: false,
    })
    await openSettings()

    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Remove it' }))

    await waitFor(() => expect(api.forgetVault).toHaveBeenCalledWith('v1'))
    await waitFor(() => expect(api.listNotes).toHaveBeenCalledWith('v2'))
    expect(screen.queryByRole('button', { name: 'One' })).toBeNull()
  })

  it('reports a failure to forget a vault', async () => {
    api.forgetVault.mockRejectedValue(new Error('registry is read-only'))
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Remove it' }))
    // The banner lives in the note column, which Settings was covering.
    await userEvent.click(screen.getByRole('button', { name: 'Close settings' }))

    // Explicit timeout: the alert lands after an async save/sync round-trip,
    // and the default 1000ms findBy window is a race that only loses under
    // load -- CI ran this file in 8.5s where it takes milliseconds locally.
    const alert = await screen.findByRole('alert', { timeout: 5_000 })
    expect(alert.textContent).toContain('registry is read-only')
  })

  it('persists a pending edit before forgetting the vault that holds it', async () => {
    await mountDirty()
    await openSettings()

    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Remove it' }))

    // The unsaved buffer reached disk BEFORE the vault was dropped.
    expect(api.saveNote).toHaveBeenCalledWith(
      'v1',
      'One.md',
      '# Hello\n\nedited in the buffer',
      4,
    )
    await waitFor(() => expect(api.forgetVault).toHaveBeenCalledWith('v1'))
  })

  it('refuses to forget a vault whose pending save was rejected', async () => {
    await mountDirty()
    api.saveNote.mockRejectedValueOnce(staleRejection())
    await openSettings()

    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0])
    await userEvent.click(await screen.findByRole('button', { name: 'Remove it' }))

    // Dropping the vault would take the unreconciled edit with it.
    await waitFor(() => expect(api.saveNote).toHaveBeenCalled())
    expect(api.forgetVault).not.toHaveBeenCalled()
  })

  it('stores a GitHub credential entered in Settings', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    await userEvent.type(
      screen.getByLabelText('Access token (optional)'),
      'github_pat_example',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.setPat).toHaveBeenCalledWith('github_pat_example'))
    expect(await screen.findByRole('status')).toBeTruthy()
  })

  it('records a vault dropping out of the Kiro Crew knowledge library', async () => {
    api.listVaults.mockResolvedValue({
      vaults: [aVault({ knowledge: true })],
      hasPat: false,
      hasGhAuth: false,
    })
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    await userEvent.click(screen.getByRole('switch', { name: 'Sync to Kiro Crew knowledge' }))

    // The disabled state is persisted FIRST, before the host source is removed.
    await waitFor(() =>
      expect(api.setVaultKnowledge).toHaveBeenCalledWith('v1', false, undefined),
    )
    expect(await screen.findByText('Removed from Knowledge.')).toBeTruthy()
  })

  it('leaves Settings from its own close control', async () => {
    await mountWithNote()
    await openSettings()
    await userEvent.click(screen.getByRole('button', { name: 'Close settings' }))
    // The note column is back, controls and all.
    expect(await screen.findByRole('button', { name: 'Markdown source' })).toBeTruthy()
  })

  it('opens another vault from Settings and returns to the note pane', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    await userEvent.click(screen.getByRole('button', { name: 'Open' }))

    await waitFor(() => expect(api.listNotes).toHaveBeenCalledWith('v2'))
    await waitFor(() =>
      expect(screen.queryByText('Vaults, GitHub access and sync')).toBeNull(),
    )
  })

  it('reaches the connect screen from Settings and backs out of it', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await openSettings()

    await userEvent.click(screen.getByRole('button', { name: 'Connect a vault' }))
    expect(await screen.findByText('Clone a repo')).toBeTruthy()

    // A vault already exists, so the connect screen offers a way back.
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByRole('button', { name: 'One' })).toBeTruthy()
  })

  it('adds a vault from the connect screen and switches to it', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await userEvent.click(screen.getByRole('button', { name: 'Switch vault' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Connect a vault' }))

    await userEvent.click(await screen.findByRole('button', { name: 'Attach a folder' }))
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Local folder' }),
      '/home/u/third',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Attach folder' }))

    await waitFor(() => expect(api.listNotes).toHaveBeenCalledWith('v3'))
  })

  // ── vault switching ───────────────────────────────────────────────────────

  it('switches vault with the keyboard from the vault menu', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await userEvent.click(screen.getByRole('button', { name: 'Switch vault' }))

    const option = await screen.findByRole('option', { name: /Archive/ })
    // A key that means nothing here must not select the row.
    fireEvent.keyDown(option, { key: 'x' })
    expect(api.listNotes).not.toHaveBeenCalledWith('v2')

    fireEvent.keyDown(option, { key: 'Enter' })
    await waitFor(() => expect(api.listNotes).toHaveBeenCalledWith('v2'))
  })

  it('ignores selecting the vault that is already active', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    api.listNotes.mockClear()
    await userEvent.click(screen.getByRole('button', { name: 'Switch vault' }))
    await userEvent.click(await screen.findByRole('option', { name: /Notebook/ }))

    // Same vault: nothing is reloaded and the editor is left alone.
    expect(api.listNotes).not.toHaveBeenCalled()
  })

  it('refuses to switch vault while a rejected save is unresolved', async () => {
    await mountDirty()
    api.saveNote.mockRejectedValueOnce(staleRejection())
    await userEvent.click(screen.getByRole('button', { name: 'Switch vault' }))
    await userEvent.click(await screen.findByRole('option', { name: /Archive/ }))

    await waitFor(() => expect(api.saveNote).toHaveBeenCalled())
    // Switching clears the editor, so the unreconciled text would be gone.
    expect(api.listNotes).not.toHaveBeenCalledWith('v2')
  })

  it('refuses to open another note while a rejected save is unresolved', async () => {
    await mountDirty()
    // Persistent, not once: the guard has to hold across the retry that opening
    // another note performs before it navigates.
    api.saveNote.mockRejectedValue(staleRejection())
    await userEvent.click(screen.getByRole('button', { name: 'Sync' }))
    await screen.findByText('This note changed on disk since you opened it.')
    api.readNote.mockClear()

    await userEvent.click(noteRow('Two'))
    expect(api.readNote).not.toHaveBeenCalled()
  })

  it('surfaces a failure to read the note that was clicked', async () => {
    api.readNote.mockRejectedValue(new Error('unreadable: bad encoding'))
    await mount()
    await userEvent.click(await screen.findByRole('button', { name: 'One' }))

    const alert = await screen.findByRole('alert', { timeout: 5_000 })
    expect(alert.textContent).toContain('unreadable: bad encoding')
  })

  // ── delete ────────────────────────────────────────────────────────────────

  it('clears the pane and opens the neighbour when the open note is trashed', async () => {
    await mountWithNote()
    await confirmDelete('One')

    await waitFor(() => expect(api.deleteNote).toHaveBeenCalledWith('v1', 'One.md'))
    // Folders sort first, so the note above is where the delete lands.
    await waitFor(() =>
      expect(localStorage.getItem('mdnb-open-note')).toBe('"folder/Two.md"'),
    )
    expect(api.readNote).toHaveBeenCalledWith('v1', 'folder/Two.md')
  })

  it('refuses to trash a note whose pending save was rejected', async () => {
    await mountDirty()
    api.saveNote.mockRejectedValueOnce(staleRejection())
    await confirmDelete('One')

    expect(
      await screen.findByText('This note changed on disk since you opened it.'),
    ).toBeTruthy()
    // Trashing now would bury the on-disk version and drop the buffer with it.
    expect(api.deleteNote).not.toHaveBeenCalled()
  })

  it('refuses a second delete while the first is still in flight', async () => {
    api.deleteNote.mockReturnValue(pending())
    await mount()
    await screen.findByRole('button', { name: 'One' })

    await confirmDelete('One')
    await waitFor(() => expect(api.deleteNote).toHaveBeenCalledTimes(1))

    await confirmDelete('Two')
    // One entry tracks the pending delete, so a second would silently displace it.
    expect(api.deleteNote).toHaveBeenCalledTimes(1)
  })

  it('drops raw-editor keystrokes aimed at a note whose delete is in flight', async () => {
    api.deleteNote.mockReturnValue(pending())
    await mountWithNote()
    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    await confirmDelete('One')

    vi.useFakeTimers()
    fireEvent.change(rawEditor(), { target: { value: 'typed after the delete' } })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SAVE_DEBOUNCE_MS)
    })
    // A save here would put the note straight back on disk.
    expect(api.saveNote).not.toHaveBeenCalled()
  })

  it('does not arm the reload warning for a block edit made mid-delete', async () => {
    api.deleteNote.mockReturnValue(pending())
    await mountWithNote()
    await confirmDelete('One')

    await userEvent.click(screen.getByRole('button', { name: 'Body text' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Edit block' }), {
      target: { value: 'typed into a doomed note' },
    })

    const unload = createEvent('beforeunload', window, { cancelable: true })
    fireEvent(window, unload)
    // Nothing is pending: the keystroke was refused rather than made dirty.
    expect(unload.defaultPrevented).toBe(false)
  })

  // ── rename, move, duplicate ───────────────────────────────────────────────

  it('follows the open note and its pin through a rename', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    rowAction('One', 'Pin note')
    await userEvent.click(noteRow('One'))
    await screen.findByText('Body text')

    rowAction('One', 'Rename note')
    const field = await screen.findByRole('textbox', { name: 'Note name' })
    await userEvent.clear(field)
    await userEvent.type(field, 'Renamed{Enter}')

    await waitFor(() => expect(api.moveNote).toHaveBeenCalledWith('v1', 'One.md', 'Renamed.md'))
    // The pin is stored per vault against the path, so it has to move with it.
    await waitFor(() => expect(localStorage.getItem('mdnb-pinned-v1')).toBe('["Renamed.md"]'))
    expect(localStorage.getItem('mdnb-open-note')).toBe('"Renamed.md"')
    expect(api.readNote).toHaveBeenCalledWith('v1', 'Renamed.md')
  })

  it('ignores a rename that strips down to an empty name', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    rowAction('One', 'Rename note')
    const field = await screen.findByRole('textbox', { name: 'Note name' })
    await userEvent.clear(field)
    // Every character here is illegal in a filename, so nothing is left.
    await userEvent.type(field, '?*|{Enter}')

    expect(api.moveNote).not.toHaveBeenCalled()
  })

  it('refuses to move a note whose pending save was rejected', async () => {
    await mountDirty()
    api.saveNote.mockRejectedValueOnce(staleRejection())
    // Back to the rendered pane so the row action bar is reachable.
    await userEvent.click(screen.getByRole('button', { name: 'Rendered' }))
    rowAction('One', 'Rename note')
    const field = await screen.findByRole('textbox', { name: 'Note name' })
    await userEvent.clear(field)
    await userEvent.type(field, 'Renamed{Enter}')

    await waitFor(() => expect(api.saveNote).toHaveBeenCalled())
    // Renaming would retarget the editor without ever reconciling its content.
    expect(api.moveNote).not.toHaveBeenCalled()
  })

  it('reports a failed move rather than leaving the row renamed', async () => {
    api.moveNote.mockRejectedValue(new Error('destination exists'))
    await mount()
    await screen.findByRole('button', { name: 'One' })
    rowAction('One', 'Rename note')
    const field = await screen.findByRole('textbox', { name: 'Note name' })
    await userEvent.clear(field)
    await userEvent.type(field, 'Renamed{Enter}')

    const alert = await screen.findByRole('alert', { timeout: 5_000 })
    expect(alert.textContent).toContain('destination exists')
  })

  it('ignores a note dropped back onto the level it already sits on', async () => {
    await mount()
    await screen.findByRole('button', { name: 'Two' })
    const list = noteRow('Two').parentElement!.parentElement!

    // `One.md` is already at the vault root, so this is a no-op move.
    fireEvent.drop(list, { dataTransfer: { getData: () => 'One.md' } })
    expect(api.moveNote).not.toHaveBeenCalled()
  })

  it('renames the open note from the title in its header', async () => {
    await mountWithNote()
    await userEvent.click(screen.getByRole('button', { name: 'Click to rename this note' }))
    const title = await screen.findByRole('textbox', { name: 'Note title' })
    await userEvent.clear(title)
    await userEvent.type(title, 'Retitled{Enter}')

    await waitFor(() => expect(api.moveNote).toHaveBeenCalledWith('v1', 'One.md', 'Retitled.md'))
  })

  it('persists a pending edit before duplicating the note it belongs to', async () => {
    await mountDirty()
    await userEvent.click(screen.getByRole('button', { name: 'Rendered' }))
    rowAction('One', 'Duplicate note')

    // The backend copies what is ON DISK, so the buffer has to land first.
    await waitFor(() => expect(api.saveNote).toHaveBeenCalled())
    await waitFor(() => expect(api.duplicateNote).toHaveBeenCalledWith('v1', 'One.md'))
  })

  it('refuses to duplicate a note whose pending save was rejected', async () => {
    await mountDirty()
    api.saveNote.mockRejectedValueOnce(staleRejection())
    await userEvent.click(screen.getByRole('button', { name: 'Rendered' }))
    rowAction('One', 'Duplicate note')

    await waitFor(() => expect(api.saveNote).toHaveBeenCalled())
    expect(api.duplicateNote).not.toHaveBeenCalled()
  })

  it('reports a failed duplicate instead of silently doing nothing', async () => {
    api.duplicateNote.mockRejectedValue(new Error('disk quota exceeded'))
    await mount()
    await screen.findByRole('button', { name: 'One' })
    rowAction('One', 'Duplicate note')

    const alert = await screen.findByRole('alert', { timeout: 5_000 })
    expect(alert.textContent).toContain('disk quota exceeded')
  })

  // ── editor keys ───────────────────────────────────────────────────────────

  it('nests a list item with Tab in the raw editor', async () => {
    api.readNote.mockResolvedValue(doc({ content: '- alpha\n- beta' }))
    await mount()
    await userEvent.click(await screen.findByRole('button', { name: 'One' }))
    await waitFor(() => expect(api.readNote).toHaveBeenCalled())
    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    await waitFor(() => expect(rawEditor().value).toBe('- alpha\n- beta'))

    const area = rawEditor()
    area.setSelectionRange(3, 3)
    fireEvent.keyDown(area, { key: 'Tab' })

    await waitFor(() => expect(rawEditor().value).toBe('\t- alpha\n- beta'))
  })

  it('leaves Tab to the browser when the caret is not on a list item', async () => {
    await mountWithNote()
    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))

    const area = rawEditor()
    area.setSelectionRange(2, 2)
    fireEvent.keyDown(area, { key: 'Tab' })

    // Nothing to nest, so the keystroke keeps its native job of moving focus.
    expect(rawEditor().value).toBe(BODY)

    // An ordinary key is not the list gesture at all and falls straight through.
    fireEvent.keyDown(area, { key: 'a' })
    expect(rawEditor().value).toBe(BODY)
  })

  it('splits a block on Enter, writing both halves back into the note', async () => {
    await mountWithNote()
    await userEvent.click(screen.getByRole('button', { name: 'Body text' }))

    const editor = screen.getByRole('textbox', { name: 'Edit block' }) as HTMLTextAreaElement
    editor.setSelectionRange(4, 4)
    fireEvent.keyDown(editor, { key: 'Enter' })

    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    await waitFor(() => expect(rawEditor().value).toBe('# Hello\n\nBody\n text'))
  })

  it('cancels a block edit with Escape, leaving the note untouched', async () => {
    await mountWithNote()
    await userEvent.click(screen.getByRole('button', { name: 'Body text' }))
    const editor = screen.getByRole('textbox', { name: 'Edit block' })
    fireEvent.change(editor, { target: { value: 'discard me' } })
    fireEvent.keyDown(editor, { key: 'Escape' })

    await waitFor(() =>
      expect(screen.queryByRole('textbox', { name: 'Edit block' })).toBeNull(),
    )
    expect(screen.getByText('Body text')).toBeTruthy()
    expect(screen.queryByText('discard me')).toBeNull()
  })

  it('offsets a block edit past frontmatter instead of overwriting it', async () => {
    api.readNote.mockResolvedValue(
      doc({ content: '---\ntitle: One\n---\n\n# Hello\n\nBody text' }),
    )
    await mountWithNote()

    await userEvent.click(screen.getByRole('button', { name: 'Body text' }))
    const editor = screen.getByRole('textbox', { name: 'Edit block' })
    fireEvent.change(editor, { target: { value: 'Rewritten body' } })
    fireEvent.blur(editor)

    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    // The closing `---` survives: the preview's line numbers were body-relative.
    await waitFor(() =>
      expect(rawEditor().value).toBe('---\ntitle: One\n---\n\n# Hello\n\nRewritten body'),
    )
  })

  it('warns before a reload while an edit is still pending, and not otherwise', async () => {
    await mountWithNote()
    const clean = createEvent('beforeunload', window, { cancelable: true })
    fireEvent(window, clean)
    expect(clean.defaultPrevented).toBe(false)

    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    fireEvent.change(rawEditor(), { target: { value: 'unsaved work' } })
    // A second keystroke restarts the debounce rather than stacking a timer.
    fireEvent.change(rawEditor(), { target: { value: 'unsaved work, revised' } })

    const dirty = createEvent('beforeunload', window, { cancelable: true })
    fireEvent(window, dirty)
    // The save is debounced, so a reload inside that window would lose the text.
    expect(dirty.defaultPrevented).toBe(true)
  })

  // ── panel ─────────────────────────────────────────────────────────────────

  it('drags the notes panel wider and remembers the width', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    const handle = Array.from(document.querySelectorAll('div')).find(
      d => d.style.cursor === 'col-resize',
    )
    expect(handle).toBeTruthy()

    fireEvent.pointerDown(handle as Element, { clientX: 400 })
    fireEvent.pointerMove(window, { clientX: 460 })
    fireEvent.pointerUp(window, { clientX: 460 })

    // 260 default + 60 dragged, inside the 180–420 bounds.
    expect(localStorage.getItem('mdnb-panel-width')).toBe('320')
  })

  it('clamps a drag that overshoots the panel bounds', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })
    const handle = Array.from(document.querySelectorAll('div')).find(
      d => d.style.cursor === 'col-resize',
    )

    fireEvent.pointerDown(handle as Element, { clientX: 400 })
    fireEvent.pointerUp(window, { clientX: 4000 })

    expect(localStorage.getItem('mdnb-panel-width')).toBe('420')
  })

  it('re-expands a folder that was collapsed', async () => {
    await mount()
    const folder = await screen.findByRole('button', { name: 'folder' })
    await userEvent.click(folder)
    expect(screen.queryByRole('button', { name: 'Two' })).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'folder' }))
    expect(await screen.findByRole('button', { name: 'Two' })).toBeTruthy()
  })

  it('opens a note from the flat list view', async () => {
    localStorage.setItem('mdnb-list-view', '"list"')
    await mount()
    await userEvent.click(await screen.findByRole('button', { name: 'Two' }))
    await waitFor(() => expect(api.readNote).toHaveBeenCalledWith('v1', 'folder/Two.md'))
  })

  it('opens a note from the search results', async () => {
    api.search.mockResolvedValue({
      results: [{ path: 'folder/Two.md', title: 'Two', snippet: 'a match', score: 1 }],
    })
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await userEvent.type(screen.getByRole('textbox', { name: 'Search notes' }), 'match')

    const hit = await screen.findByRole('button', { name: 'Two' })
    await userEvent.click(hit)
    await waitFor(() => expect(api.readNote).toHaveBeenCalledWith('v1', 'folder/Two.md'))
  })

  it('keeps quiet when the search request fails', async () => {
    api.search.mockRejectedValue(new Error('index rebuilding'))
    await mount()
    await screen.findByRole('button', { name: 'One' })
    await userEvent.type(screen.getByRole('textbox', { name: 'Search notes' }), 'match')

    // A failed search is not an error banner — it just has no hits to show.
    expect(await screen.findByText('No matches')).toBeTruthy()
    await waitFor(() => expect(api.search).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('marks the list background as a move target while a note is dragged over it', async () => {
    await mount()
    await screen.findByRole('button', { name: 'Two' })
    // `Two` sits inside a folder, so two levels up is the scrolling list itself.
    const list = noteRow('Two').parentElement!.parentElement!

    const dataTransfer = { dropEffect: 'none', getData: () => 'One.md' }
    const over = createEvent.dragOver(list, { dataTransfer })
    fireEvent(list, over)

    // Preventing the default is what tells the browser a drop is accepted here;
    // the handler also stamps the transfer's `dropEffect`, which React reads off
    // its own synthetic wrapper rather than this literal.
    expect(over.defaultPrevented).toBe(true)
  })

  // ── sync and the change poll ──────────────────────────────────────────────

  it('reopens the note it was showing once a sync completes', async () => {
    await mountWithNote()
    api.readNote.mockClear()
    await userEvent.click(screen.getByRole('button', { name: 'Sync' }))

    // The sync may have pulled a newer version of the open note.
    await waitFor(() => expect(api.readNote).toHaveBeenCalledWith('v1', 'One.md'))
  })

  it('surfaces a save failure that is not a disk conflict', async () => {
    await mountDirty()
    api.saveNote.mockRejectedValueOnce(new Error('no space left on device'))
    await userEvent.click(screen.getByRole('button', { name: 'Sync' }))

    const alert = await screen.findByRole('alert', { timeout: 5_000 })
    expect(alert.textContent).toContain('no space left on device')
  })

  it('reopens the open note when the change poll reports it was modified', async () => {
    // Restored from the stored preference rather than clicked, so the whole test
    // can run on fake timers without driving user-event through them.
    localStorage.setItem('mdnb-open-note', '"One.md"')
    api.changes.mockResolvedValue({ rev: 3, changed: ['One.md'], watching: true })

    vi.useFakeTimers()
    await mount()
    for (let i = 0; i < 3; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
    }
    expect(api.readNote).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(CHANGES_POLL_MS)
    })
    expect(api.readNote).toHaveBeenCalledTimes(2)
  })

  it('persists a pending edit before the autosave commit records history', async () => {
    await mountOnFakeTimersWithNote()
    fireEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    fireEvent.change(rawEditor(), { target: { value: '# Hello\n\nlate edit' } })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000)
    })
    // The commit records what is on disk, so the buffer has to get there first.
    expect(api.saveNote).toHaveBeenCalledWith('v1', 'One.md', '# Hello\n\nlate edit', 4)
    expect(api.commit).toHaveBeenCalledWith('v1')
  })

  it('skips the autosave commit when the pending save was rejected', async () => {
    await mountOnFakeTimersWithNote()
    api.saveNote.mockRejectedValue(staleRejection())
    fireEvent.click(screen.getByRole('button', { name: 'Markdown source' }))
    fireEvent.change(rawEditor(), { target: { value: '# Hello\n\nunreconciled' } })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000)
    })
    // Committing now would record the disk version over the user's decision.
    expect(api.commit).not.toHaveBeenCalled()
  })

  it('skips the autosave commit while a block is being edited', async () => {
    await mountOnFakeTimersWithNote()
    fireEvent.click(screen.getByRole('button', { name: 'Body text' }))
    expect(screen.getByRole('textbox', { name: 'Edit block' })).toBeTruthy()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000)
    })
    // The commit reloads the note, which would unmount the editor mid-draft.
    expect(api.commit).not.toHaveBeenCalled()
  })

  it('refreshes the listing only when the autosave commit actually wrote', async () => {
    api.commit.mockResolvedValue({
      result: { pushed: false, pulled: false, committed: ['One.md'], conflicts: [] },
    })
    await mountOnFakeTimersWithNote()
    const before = api.listNotes.mock.calls.length

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60_000)
    })
    // Pending badges changed, so the rows have to be re-read.
    expect(api.listNotes.mock.calls.length).toBeGreaterThan(before)
  })

  // ── racing note reads ─────────────────────────────────────────────────────

  it('keeps keystrokes typed while another note was still loading', async () => {
    await mountWithNote()
    await userEvent.click(screen.getByRole('button', { name: 'Markdown source' }))

    const slow = deferred<ReturnType<typeof doc>>()
    api.readNote.mockReturnValue(slow.promise)
    await userEvent.click(noteRow('Two'))

    // The click has not landed yet; these keystrokes belong to the note on screen.
    fireEvent.change(rawEditor(), { target: { value: '# Hello\n\ntyped mid-flight' } })
    slow.settle(doc({ path: 'folder/Two.md' }))

    // Applying the read would have replaced that text with the other note's body.
    await waitFor(() => expect(rawEditor().value).toBe('# Hello\n\ntyped mid-flight'))
    expect(localStorage.getItem('mdnb-open-note')).toBe('"One.md"')
  })

  it('discards a note read that a later open superseded', async () => {
    await mount()
    await screen.findByRole('button', { name: 'One' })

    const slow = deferred<ReturnType<typeof doc>>()
    api.readNote.mockReturnValueOnce(slow.promise)
    await userEvent.click(noteRow('Two'))
    // A second click wins the race and lands first.
    await userEvent.click(noteRow('One'))
    await waitFor(() => expect(localStorage.getItem('mdnb-open-note')).toBe('"One.md"'))

    slow.settle(doc({ path: 'folder/Two.md', content: '# Stale read' }))
    await act(async () => {
      await Promise.resolve()
    })
    // The superseded read must not repoint the editor at the note it read.
    expect(localStorage.getItem('mdnb-open-note')).toBe('"One.md"')
    expect(screen.queryByText('Stale read')).toBeNull()
  })
})
