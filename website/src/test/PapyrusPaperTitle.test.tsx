/**
 * What the paper list CALLS a paper, and the rename that overrides it.
 *
 * Asserted through the DOM: the defects this field invites (an Escape that saves
 * through its own blur, a cleared field that sends nothing) are invisible to a
 * unit call on the handler.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ProjectList from '../apps/papyrus/ProjectList'
import { renderWithProviders } from './helpers'
import { papyrusApi, type Project } from '../apps/papyrus/api'

vi.mock('../apps/papyrus/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/papyrus/api')>()),
  papyrusApi: {
    health: vi.fn(),
    listProjects: vi.fn(),
    createProject: vi.fn(),
    cloneProject: vi.fn(),
    deleteProject: vi.fn(),
    setTitle: vi.fn(),
    provisionCompiler: vi.fn(),
  },
}))

const mocked = vi.mocked(papyrusApi as unknown as {
  health: ReturnType<typeof vi.fn>
  listProjects: ReturnType<typeof vi.fn>
  createProject: ReturnType<typeof vi.fn>
  cloneProject: ReturnType<typeof vi.fn>
  setTitle: ReturnType<typeof vi.fn>
})

/** One list row. `title` is what the server resolved; `name` is the directory. */
function project(name: string, title: string): Project {
  return { name, title, modified: 1_700_000_000, has_pdf: false, generation: `gen-${name}` }
}

function mount(projects: Project[], onOpenProject: (name: string) => void = () => {}) {
  mocked.listProjects.mockResolvedValue({ projects })
  return renderWithProviders(<ProjectList onOpenProject={onOpenProject} />)
}

/** The row's own scope, so a title assertion cannot match the page header. */
async function row(displayed: string): Promise<HTMLElement> {
  return (await screen.findByText(displayed)).closest('tr') as HTMLElement
}

/** Open the rename field of the row showing `displayed`, and return the field. */
async function openRename(displayed: string): Promise<HTMLElement> {
  const tr = await row(displayed)
  // The exact name: the title itself is a button too, named after the title.
  await userEvent.click(within(tr).getByRole('button', { name: `Rename ${displayed}` }))
  return screen.getByLabelText('Paper name')
}

const closed = () => waitFor(() => expect(screen.queryByLabelText('Paper name')).toBeNull())

beforeEach(() => {
  vi.clearAllMocks()
  mocked.health.mockResolvedValue({ status: 'ok', compiler: 'pdflatex', git: true })
})

describe('the paper list', () => {
  it('lists a paper under its title, with the directory under it', async () => {
    const long = 'A'.repeat(120)
    const legacy = { name: 'no-title-field', modified: 1, has_pdf: false } as Project
    mount([project('6a16ddbf', 'MACKEREL'), project('my-paper', 'my-paper'), project('long', long), legacy])
    expect(within(await row('MACKEREL')).getByText('6a16ddbf')).toBeInTheDocument()
    // No title: the directory once, not twice.
    expect(within(await row('my-paper')).getAllByText('my-paper')).toHaveLength(1)
    // An older backend sends no `title`: the row still reads.
    expect(await screen.findByText('no-title-field')).toBeInTheDocument()
    // An unbroken title may wrap anywhere, so the row actions stay on a narrow screen.
    expect((await screen.findByText(long)).className).toContain('[overflow-wrap:anywhere]')
  })

  it('renames through the field: Enter and blur save, Escape and an untouched seed do not', async () => {
    mocked.setTitle.mockResolvedValue({ ok: true, title: 'Before' })
    mount([project('6a16ddbf', 'Before')])

    // Seeded with the displayed name; leaving it untouched writes nothing, so a
    // document title is never pinned as an override.
    let field = await openRename('Before')
    expect(field).toHaveValue('Before')
    await userEvent.tab()
    await closed()

    field = await openRename('Before')
    await userEvent.clear(field)
    await userEvent.type(field, 'Rejected{Escape}')
    await closed()
    expect(mocked.setTitle).not.toHaveBeenCalled()

    field = await openRename('Before')
    await userEvent.clear(field)
    await userEvent.type(field, 'After')
    await userEvent.tab()
    await waitFor(() => expect(mocked.setTitle).toHaveBeenCalledWith('6a16ddbf', 'After', 'gen-6a16ddbf'))
    await closed()

    // Clearing the field is the way back to the document's title, so it is sent.
    field = await openRename('Before')
    await userEvent.clear(field)
    await userEvent.type(field, '{Enter}')
    await waitFor(() => expect(mocked.setTitle).toHaveBeenLastCalledWith('6a16ddbf', '', 'gen-6a16ddbf'))
  })

  it('keeps the draft and the page usable when a rename fails', async () => {
    mocked.setTitle.mockRejectedValue(new Error('rename refused'))
    mount([project('6a16ddbf', 'Before'), project('other', 'Other paper')])
    const loads = () => mocked.listProjects.mock.calls.length

    const tr = await row('Before')
    const field = await openRename('Before')
    expect(within(tr).getByRole('button', { name: 'Delete 6a16ddbf' })).toBeDisabled()
    await userEvent.clear(field)
    const before = loads()
    await userEvent.type(field, 'Typed with care{Enter}')
    expect(await screen.findByText('rename refused')).toBeInTheDocument()
    expect(screen.getByLabelText('Paper name')).toHaveValue('Typed with care')
    // The agent hand-off would leave the page and lose the draft.
    expect(screen.queryByRole('button', { name: /agent/i })).toBeNull()
    // The list reloads, so a retry carries a fresh generation.
    await waitFor(() => expect(loads()).toBeGreaterThan(before))

    // Another row's pencil does not discard the draft.
    await userEvent.click(within(await row('Other paper')).getByRole('button', { name: 'Rename Other paper' }))
    expect(screen.getByLabelText('Paper name')).toHaveValue('Typed with care')

    // The paper is deleted elsewhere: the field closes and the page is usable again.
    mocked.listProjects.mockResolvedValue({ projects: [project('other', 'Other paper')] })
    await userEvent.type(screen.getByLabelText('Paper name'), '{Enter}')
    await closed()
    expect(within(await row('Other paper')).getByRole('button', { name: 'Rename Other paper' })).toBeEnabled()
  })

  it('leaves nothing to navigate away while a rename is saving', async () => {
    mocked.setTitle.mockReturnValue(new Promise(() => {}))
    const onOpen = vi.fn()
    mount([project('6a16ddbf', 'Before'), project('other', 'Other paper')], onOpen)

    const field = await openRename('Before')
    await userEvent.clear(field)
    await userEvent.type(field, 'Typed with care')
    // Clicking another paper blurs the field (which saves) but does not open it.
    await userEvent.click(screen.getByText('Other paper'))
    await waitFor(() => expect(mocked.setTitle).toHaveBeenCalledWith('6a16ddbf', 'Typed with care', 'gen-6a16ddbf'))
    expect(onOpen).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Paper name')).toBeDisabled()

    // Create and Clone open the new paper too, so they wait as well.
    await userEvent.type(screen.getByLabelText('New paper name'), 'fresh{Enter}')
    await userEvent.click(screen.getByRole('button', { name: /Create/ }))
    await userEvent.type(screen.getByLabelText('Repository URL'), 'https://example.com/r.git{Enter}')
    await userEvent.click(screen.getByRole('button', { name: /Clone/ }))
    expect(mocked.createProject).not.toHaveBeenCalled()
    expect(mocked.cloneProject).not.toHaveBeenCalled()
  })

  it('shows a saved name even when the list reload fails, and the banner closes', async () => {
    mocked.setTitle.mockResolvedValue({ ok: true, title: 'Saved name' })
    mount([project('6a16ddbf', 'Before')])
    const field = await openRename('Before')
    mocked.listProjects.mockRejectedValue(new Error('list unavailable'))
    await userEvent.clear(field)
    await userEvent.type(field, 'Saved name{Enter}')
    expect(await screen.findByText('Saved name')).toBeInTheDocument()
    expect(await screen.findByText('list unavailable', {}, { timeout: 5000 })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('list unavailable')).toBeNull()
  })
})
