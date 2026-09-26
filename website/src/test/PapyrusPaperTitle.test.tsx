/**
 * What the paper list CALLS a paper, and the rename that overrides it.
 *
 * A cloned paper's directory is named after the clone URL's last segment, which
 * for Overleaf is the project id — so the list read `6a16ddbf` where the document
 * says "MACKEREL". The row now shows the resolved title with that identifier kept
 * underneath, because the identifier is what every route, the PDF URL and the
 * paper's chat slot are keyed on.
 *
 * The rename is asserted through the DOM rather than on the handler, because the
 * two defects this interaction invites are both invisible to a unit call: an
 * Escape that saves the draft it just abandoned (the blur it causes would commit),
 * and a cleared field that sends nothing (clearing the override is the only way
 * back to the document's own title).
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
  return { name, title, modified: 1_700_000_000, has_pdf: false }
}

function mount(projects: Project[], onOpenProject: (name: string) => void = () => {}) {
  mocked.listProjects.mockResolvedValue({ projects })
  return renderWithProviders(<ProjectList onOpenProject={onOpenProject} />)
}

/** The row's own scope, so a title assertion cannot match the page header. */
async function row(displayed: string): Promise<HTMLElement> {
  const cell = await screen.findByText(displayed)
  const tr = cell.closest('tr')
  expect(tr).not.toBeNull()
  return tr as HTMLElement
}

/**
 * The rename control, by its EXACT accessible name.
 *
 * A substring match on the title is ambiguous: the title itself is a `Clickable`,
 * which carries `role="button"` with the title as its accessible name, so
 * `/MACKEREL/` matches the row's open-the-paper control too.
 */
function renameButton(tr: HTMLElement, displayed: string): HTMLElement {
  return within(tr).getByRole('button', { name: `Rename ${displayed}` })
}

beforeEach(() => {
  vi.clearAllMocks()
  mocked.health.mockResolvedValue({ status: 'ok', compiler: 'pdflatex', git: true })
  mocked.setTitle.mockResolvedValue({ ok: true, title: 'ignored' })
})

describe('the name a paper is listed under', () => {
  it('shows the resolved title, not the directory', async () => {
    mount([project('6a16ddbf', 'MACKEREL: Multi-task Classification')])
    const tr = await row('MACKEREL: Multi-task Classification')
    expect(within(tr).getByText('6a16ddbf')).toBeInTheDocument()
  })

  it('lets an unbroken title wrap, so the row actions stay on a narrow screen', async () => {
    /** Only `anywhere` lowers the cell's min-content width; measured in Chromium at 320px. */
    const long = 'A'.repeat(120)
    mount([project('6a16ddbf', long)])
    expect((await screen.findByText(long)).className).toContain('[overflow-wrap:anywhere]')
  })

  it('keeps the identifier visible under the title', async () => {
    /** It is the string you need to find the paper again on the host it came from. */
    mount([project('62f1a9c3', 'When Peers Disagree')])
    const tr = await row('When Peers Disagree')
    expect(within(tr).getByText('62f1a9c3')).toBeInTheDocument()
  })

  it('does not print the same name twice when there is no title', async () => {
    mount([project('my-paper', 'my-paper')])
    const tr = await row('my-paper')
    expect(within(tr).getAllByText('my-paper')).toHaveLength(1)
  })

  it('renders the directory when a backend sends no title at all', async () => {
    // An older backend has no `title` field; the row must still be readable.
    const legacy = { name: 'no-title-field', modified: 1, has_pdf: false } as Project
    mount([legacy])
    expect(await screen.findByText('no-title-field')).toBeInTheDocument()
  })
})

describe('renaming a paper', () => {
  it('seeds the field with what the row currently displays', async () => {
    mount([project('6a16ddbf', 'MACKEREL: Multi-task Classification')])
    const tr = await row('MACKEREL: Multi-task Classification')
    await userEvent.click(renameButton(tr, 'MACKEREL: Multi-task Classification'))
    expect(screen.getByLabelText('Paper name')).toHaveValue(
      'MACKEREL: Multi-task Classification',
    )
  })

  it('saves the typed name on Enter', async () => {
    mount([project('6a16ddbf', 'Multi-task Classification for Keyword Expansion')])
    const tr = await row('Multi-task Classification for Keyword Expansion')
    await userEvent.click(
      renameButton(tr, 'Multi-task Classification for Keyword Expansion'),
    )
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'MACKEREL{Enter}')
    await waitFor(() => expect(mocked.setTitle).toHaveBeenCalledWith('6a16ddbf', 'MACKEREL'))
  })

  it('sends an empty title when the field is cleared, to restore the document title', async () => {
    /** Clearing the override is the ONLY way back, so a blank submit must be sent. */
    mount([project('6a16ddbf', 'A name someone set')])
    const tr = await row('A name someone set')
    await userEvent.click(renameButton(tr, 'A name someone set'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, '{Enter}')
    await waitFor(() => expect(mocked.setTitle).toHaveBeenCalledWith('6a16ddbf', ''))
  })

  it('discards the draft on Escape', async () => {
    /**
     * Escape must not save, and the row commits on BLUR so a click-away is not
     * lost — two rules that collide if the abandoned field's teardown ever emits
     * a blur. Asserting no call is the only way to see that collision.
     */
    mount([project('6a16ddbf', 'Keep this one')])
    const tr = await row('Keep this one')
    await userEvent.click(renameButton(tr, 'Keep this one'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Rejected{Escape}')
    await waitFor(() => expect(screen.queryByLabelText('Paper name')).toBeNull())
    expect(mocked.setTitle).not.toHaveBeenCalled()
  })

  it('saves on click-away, so a rename is not silently lost', async () => {
    mount([project('6a16ddbf', 'Before')])
    const tr = await row('Before')
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'After')
    await userEvent.tab()
    await waitFor(() => expect(mocked.setTitle).toHaveBeenCalledWith('6a16ddbf', 'After'))
  })

  it('writes nothing when the field is left untouched', async () => {
    /**
     * The field opens seeded with the displayed name — usually the document's own
     * `\title{}`. Saving that on blur would pin it as a user override, and later
     * edits to the document's title would silently stop showing.
     */
    mount([project('6a16ddbf', 'Declared in the document')])
    const tr = await row('Declared in the document')
    await userEvent.click(renameButton(tr, 'Declared in the document'))
    await userEvent.tab()
    await waitFor(() => expect(screen.queryByLabelText('Paper name')).toBeNull())
    expect(mocked.setTitle).not.toHaveBeenCalled()
  })

  it('keeps the typed name when the rename fails', async () => {
    mocked.setTitle.mockRejectedValue(new Error('write refused'))
    mount([project('6a16ddbf', 'Before')])
    const tr = await row('Before')
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Typed with care{Enter}')
    expect(await screen.findByText('write refused')).toBeInTheDocument()
    expect(screen.getByLabelText('Paper name')).toHaveValue('Typed with care')
  })

  it('offers no hand-off to the agent while the failed rename is still open', async () => {
    /** The hand-off leaves the page, which would throw away the draft just kept. */
    mocked.setTitle.mockRejectedValue(new Error('write refused'))
    mount([project('6a16ddbf', 'Before')])
    const tr = await row('Before')
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Typed with care{Enter}')
    expect(await screen.findByText('write refused')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /agent/i })).toBeNull()
  })

  it('does not open another paper while a rename is still being saved', async () => {
    /** Leaving would unmount the list, and a save that then failed would lose the draft. */
    mocked.setTitle.mockReturnValue(new Promise(() => {}))
    const onOpen = vi.fn()
    mount([project('6a16ddbf', 'Before'), project('other', 'Other paper')], onOpen)
    const tr = await row('Before')
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Typed with care')
    await userEvent.click(screen.getByText('Other paper'))
    await waitFor(() => expect(mocked.setTitle).toHaveBeenCalledWith('6a16ddbf', 'Typed with care'))
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('does not create or clone a paper while a rename is still being saved', async () => {
    /** Both open the new paper on success, which unmounts the list like a row click. */
    mocked.setTitle.mockReturnValue(new Promise(() => {}))
    mount([project('6a16ddbf', 'Before')])
    const tr = await row('Before')
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Typed with care')
    await userEvent.type(screen.getByLabelText('New paper name'), 'fresh{Enter}')
    await userEvent.click(screen.getByRole('button', { name: /Create/ }))
    await userEvent.type(screen.getByLabelText('Repository URL'), 'https://example.com/r.git{Enter}')
    await userEvent.click(screen.getByRole('button', { name: /Clone/ }))
    await waitFor(() => expect(mocked.setTitle).toHaveBeenCalledWith('6a16ddbf', 'Typed with care'))
    expect(mocked.createProject).not.toHaveBeenCalled()
    expect(mocked.cloneProject).not.toHaveBeenCalled()
  })

  it('keeps a failed draft when another row\'s pencil is clicked', async () => {
    mocked.setTitle.mockRejectedValue(new Error('rename refused'))
    mount([project('6a16ddbf', 'Before'), project('other', 'Other paper')])
    const tr = await row('Before')
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Typed with care')
    await userEvent.click(renameButton(await row('Other paper'), 'Other paper'))
    expect(screen.getByLabelText('Paper name')).toHaveValue('Typed with care')
  })

  it('shows the saved name even when the list refetch fails', async () => {
    mocked.setTitle.mockResolvedValue({ ok: true, title: 'Saved name' })
    mount([project('6a16ddbf', 'Before')])
    const tr = await row('Before')
    mocked.listProjects.mockRejectedValue(new Error('list unavailable'))
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Saved name{Enter}')
    expect(await screen.findByText('Saved name')).toBeInTheDocument()
    expect(await screen.findByText('list unavailable')).toBeInTheDocument()
  })

  it('locks the field while the rename is being saved', async () => {
    mocked.setTitle.mockReturnValue(new Promise(() => {}))
    mount([project('6a16ddbf', 'Before')])
    const tr = await row('Before')
    await userEvent.click(renameButton(tr, 'Before'))
    const field = screen.getByLabelText('Paper name')
    await userEvent.clear(field)
    await userEvent.type(field, 'Saving{Enter}')
    await waitFor(() => expect(screen.getByLabelText('Paper name')).toBeDisabled())
  })
})
