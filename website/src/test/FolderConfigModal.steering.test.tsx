import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import FolderConfigModal from '../components/FolderConfigModal'
import { api } from '../api/client'
import { render, folder, AGENTS, open } from './folderConfigModal.testHelpers'

// Split out of FolderConfigModal.test.tsx (file-line-cap) — see
// folderConfigModal.testHelpers.tsx for the shared render/open/fixtures this
// file and its siblings all use.
//
// vi.mock factories are hoisted above all imports, so the factory body is
// inlined here rather than imported from the helper module.
vi.mock('../api/client', () => ({
  api: {
    recentProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    favoriteProjects: vi.fn().mockResolvedValue({ dirs: [] }),
    browseDirs: vi.fn().mockResolvedValue({ path: '/', parent: '', dirs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
  },
}))

beforeEach(() => {
  vi.clearAllMocks()
  // A test that sets a persistent roster (mockResolvedValue / mockImplementation
  // on kirocrewAgents) would otherwise leak it into the next test. Re-assert
  // the default empty roster each time; this overrides a leaked implementation
  // without mockReset()'ing the other api.* factory mocks the suite relies on.
  ;(api.kirocrewAgents as unknown as ReturnType<typeof vi.fn>)
    .mockResolvedValue({ agents: [], default_agent: '' })
})

describe('steering directories', () => {
  // The "Add directory" button opens the SAME ProjectPicker the project-dir
  // Browse button uses, routed to push into an ordered array. The mock picker
  // (recentProjects/browseDirs above) resolves the Browse tab at path '/', so
  // clicking its "Select" button commits a path we can assert on.
  const addDir = async (path: string) => {
    fireEvent.click(screen.getByTestId('folder-config-steering-add'))
    const { api } = await import('../api/client')
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())
    // Type an absolute path into the picker's combobox and commit it.
    const combo = await screen.findByRole('combobox', { name: /project directory path/i })
    fireEvent.change(combo, { target: { value: path } })
    fireEvent.mouseDown(screen.getByText('Select'))
  }

  it('renders no directory rows for a folder with none', () => {
    open()
    expect(screen.queryByTestId('folder-config-steering-dirs')).toBeNull()
    expect(screen.getByTestId('folder-config-steering-add')).toBeTruthy()
  })

  it('prefills existing steering dirs in edit mode', () => {
    const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a', '/std/b'] })
    open({ mode: 'edit', folder: f, folders: [f] })
    const list = screen.getByTestId('folder-config-steering-dirs')
    expect(list.textContent).toContain('/std/a')
    expect(list.textContent).toContain('/std/b')
  })

  it('adds a directory via the picker and submits it under steering_dirs', async () => {
    const { onSubmit } = open()
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Standards' } })
    await addDir('/org/standards')
    await waitFor(() =>
      expect(screen.getByTestId('folder-config-steering-dir-0').textContent).toContain('/org/standards'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      steeringDirs: ['/org/standards'],
      touched: expect.arrayContaining(['steeringDirs']),
    }))
  })

  it('removes a directory row', async () => {
    const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a', '/std/b'] })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    fireEvent.click(screen.getByTestId('folder-config-steering-dir-remove-0'))
    // /std/a removed, /std/b shifts to index 0.
    await waitFor(() =>
      expect(screen.getByTestId('folder-config-steering-dir-0').textContent).toContain('/std/b'))
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      steeringDirs: ['/std/b'],
      touched: expect.arrayContaining(['steeringDirs']),
    }))
  })

  it('clearing every directory submits an empty array (PATCH [] clears)', async () => {
    const f = folder('f1', { name: 'Payments', steering_dirs: ['/only'] })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    fireEvent.click(screen.getByTestId('folder-config-steering-dir-remove-0'))
    await waitFor(() => expect(screen.queryByTestId('folder-config-steering-dirs')).toBeNull())
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
      steeringDirs: [],
      touched: expect.arrayContaining(['steeringDirs']),
    }))
  })

  it('does not report steeringDirs in touched when unchanged', async () => {
    const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FolderConfigModal open={true} mode="edit" folder={f} folders={[f]}
        installedAgents={AGENTS} onClose={vi.fn()} onSubmit={onSubmit} onRetryTags={vi.fn()} />
    )
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].touched).not.toContain('steeringDirs')
  })

  it('shows ancestor steering dirs read-only, accumulative and root-first', () => {
    // A parent folder's dirs are always in effect for a child; the modal
    // shows them read-only so the effective set is visible without letting
    // the child edit the parent's contribution.
    const folders = [
      folder('org', { name: 'Org', steering_dirs: ['/org/standards'] }),
      folder('repo', { name: 'Repo', parent_id: 'org' }),
    ]
    open({ folders, parentId: 'repo' })
    const inherited = screen.getByTestId('folder-config-steering-inherited')
    expect(inherited.textContent).toContain('/org/standards')
    // Read-only: no editable control (remove button) inside the inherited group.
    expect(inherited.querySelector('button')).toBeNull()
  })

  it('does not list an ancestor owned by another principal as inherited', () => {
    // The backend never delivers an app-owned ancestor's dirs to a
    // person-owned child's chats, so the modal must not present them as
    // inherited. A folder created from this dashboard is the person's.
    const folders = [
      folder('app-root', { name: 'Radar', owner_app: 'radar', steering_dirs: ['/radar/rules'] }),
      folder('org', { name: 'Org', parent_id: 'app-root', steering_dirs: ['/org/standards'] }),
    ]
    open({ folders, parentId: 'org' })
    const inherited = screen.getByTestId('folder-config-steering-inherited')
    expect(inherited.textContent).toContain('/org/standards')
    expect(inherited.textContent).not.toContain('/radar/rules')
  })

  it('lists an ancestor owned by the SAME principal as the folder being edited', () => {
    const parent = folder('app-root', { name: 'Radar', owner_app: 'radar', steering_dirs: ['/radar/rules'] })
    const child = folder('app-child', { name: 'Radar child', parent_id: 'app-root', owner_app: 'radar' })
    render(
      <FolderConfigModal open={true} mode="edit" folder={child} folders={[parent, child]}
        installedAgents={AGENTS} onClose={vi.fn()} onSubmit={vi.fn()} onRetryTags={vi.fn()} />
    )
    expect(screen.getByTestId('folder-config-steering-inherited').textContent).toContain('/radar/rules')
  })

  it('shows no inherited group when every ancestor with dirs belongs to another principal', () => {
    const folders = [folder('app-root', { name: 'Radar', owner_app: 'radar', steering_dirs: ['/radar/rules'] })]
    open({ folders, parentId: 'app-root' })
    expect(screen.queryByTestId('folder-config-steering-inherited')).toBeNull()
  })

  it('adding a steering dir arms the dismiss guard', async () => {
    const { onClose } = open()
    await addDir('/org/standards')
    await waitFor(() =>
      expect(screen.getByTestId('folder-config-steering-dir-0')).toBeTruthy())
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).not.toHaveBeenCalled()
  })

  it('closing the picker without a selection leaves the list unchanged', async () => {
    // Req 8.2: a cancelled pick must neither add a row nor mark the field
    // touched. Escape on the picker's path field is its no-selection close.
    const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    fireEvent.click(screen.getByTestId('folder-config-steering-add'))
    const { api } = await import('../api/client')
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalled())
    const combo = await screen.findByRole('combobox', { name: /project directory path/i })
    fireEvent.change(combo, { target: { value: '/never/picked' } })
    fireEvent.keyDown(combo, { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByRole('combobox', { name: /project directory path/i })).toBeNull())
    const list = screen.getByTestId('folder-config-steering-dirs')
    expect(list.textContent).toContain('/std/a')
    expect(list.textContent).not.toContain('/never/picked')
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    expect(onSubmit.mock.calls[0][0].steeringDirs).toEqual(['/std/a'])
    expect(onSubmit.mock.calls[0][0].touched).not.toContain('steeringDirs')
  })

  it('surfaces the server steering_dirs_invalid message in the error area', async () => {
    // Req 8.5: the server's own wording reaches the modal so the user learns
    // WHICH directory was refused, not just that the save failed.
    const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    onSubmit.mockRejectedValueOnce(
      new Error('Steering directory must be an existing directory: /std/a'))
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    const err = await screen.findByTestId('folder-config-error')
    expect(err.textContent).toContain('Steering directory must be an existing directory: /std/a')
  })

  it('falls back to the generic save-failed string when the rejection carries no message', async () => {
    // Req 8.5: a bare rejection (no message) must still tell the user the
    // save did not land, via the modal's generic string.
    const f = folder('f1', { name: 'Payments', steering_dirs: ['/std/a'] })
    const { onSubmit } = open({ mode: 'edit', folder: f, folders: [f] })
    onSubmit.mockRejectedValueOnce(new Error(''))
    fireEvent.change(screen.getByTestId('folder-config-name'), { target: { value: 'Renamed' } })
    fireEvent.click(screen.getByTestId('folder-config-submit'))
    const err = await screen.findByTestId('folder-config-error')
    expect(err.textContent).toContain('Could not save the folder.')
  })
})
