import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import ProjectNotesSection from '../components/ProjectNotesSection'

// The component reaches the gateway only through apiTransport; mock it so each
// test drives the three routes (folder->notes read, item delete, promote).
const get = vi.fn()
const del = vi.fn()
const post = vi.fn()
vi.mock('../api/apiTransport', () => ({
  apiTransport: {
    get: (url: string) => get(url),
    del: (url: string) => del(url),
    post: (url: string, body?: object) => post(url, body),
    j: (r: { _json: unknown }) => Promise.resolve(r._json),
  },
}))

function okJson(json: unknown) {
  return { ok: true, status: 200, _json: json }
}

beforeEach(() => {
  get.mockReset()
  del.mockReset()
  post.mockReset()
})

it('lists a project\'s notes and shows the empty state when there are none', async () => {
  get.mockResolvedValueOnce(okJson({ notes: [], namespace: 'project:root' }))
  render(<ProjectNotesSection folderId="f1" steeringDirs={[]} />)
  await screen.findByTestId('folder-config-notes-empty')
  expect(get).toHaveBeenCalledWith('/api/knowledge/project-notes?folder_id=f1')
})

it('renders notes and deletes one through the generic item route', async () => {
  get.mockResolvedValueOnce(
    okJson({ notes: [{ id: 'n1', text: 'prefer small PRs', tags: [], created_at: '' }], namespace: 'project:root' }),
  )
  del.mockResolvedValueOnce({ ok: true, status: 200 })
  render(<ProjectNotesSection folderId="f1" steeringDirs={[]} />)
  await screen.findByTestId('folder-config-note-n1')
  expect(screen.getByText('prefer small PRs')).toBeTruthy()

  fireEvent.click(screen.getByTestId('folder-config-note-delete-n1'))
  await waitFor(() => expect(del).toHaveBeenCalledWith('/api/knowledge/items/n1'))
  // The row is dropped from the list after a successful delete.
  await waitFor(() => expect(screen.queryByTestId('folder-config-note-n1')).toBeNull())
})

it('promote is disabled with no steering dirs and explains why', async () => {
  get.mockResolvedValueOnce(
    okJson({ notes: [{ id: 'n1', text: 'a learning', tags: [], created_at: '' }], namespace: 'project:root' }),
  )
  render(<ProjectNotesSection folderId="f1" steeringDirs={[]} />)
  await screen.findByTestId('folder-config-note-n1')
  const promote = screen.getByTestId('folder-config-note-promote-n1') as HTMLButtonElement
  expect(promote.disabled).toBe(true)
  expect(screen.getByText(/Add a steering directory/i)).toBeTruthy()
})

it('promotes a note into a chosen declared steering dir', async () => {
  get.mockResolvedValueOnce(
    okJson({ notes: [{ id: 'n1', text: 'a learning', tags: [], created_at: '' }], namespace: 'project:root' }),
  )
  post.mockResolvedValueOnce({ ok: true, status: 200 })
  render(<ProjectNotesSection folderId="f1" steeringDirs={['/repo/.kiro/steering']} />)
  await screen.findByTestId('folder-config-note-n1')

  // Opening the promote picker reveals the confirm control; the single declared
  // dir is preselected, so confirm sends it straight through.
  fireEvent.click(screen.getByTestId('folder-config-note-promote-n1'))
  const row = await screen.findByTestId('folder-config-note-promote-row-n1')
  fireEvent.click(within(row).getByTestId('folder-config-note-promote-confirm-n1'))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith('/api/knowledge/project-notes/n1/promote', {
      folder_id: 'f1',
      steering_dir: '/repo/.kiro/steering',
    }),
  )
})

it('surfaces a load failure with a retry that re-reads', async () => {
  get.mockResolvedValueOnce({ ok: false, status: 500 })
  render(<ProjectNotesSection folderId="f1" steeringDirs={[]} />)
  await screen.findByTestId('folder-config-notes-load-error')
  get.mockResolvedValueOnce(okJson({ notes: [], namespace: 'project:root' }))
  fireEvent.click(screen.getByTestId('folder-config-notes-retry'))
  await screen.findByTestId('folder-config-notes-empty')
})
