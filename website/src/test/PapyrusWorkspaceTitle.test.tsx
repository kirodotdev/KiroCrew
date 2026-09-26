/**
 * What the OPEN paper is called in its toolbar.
 *
 * The header used to render the project key — the directory name, which for a
 * cloned paper is the host's own id. So the list row read "MACKEREL" and the
 * header for the same paper read `6a16ddbf`, and renaming the paper changed the
 * row while the header kept the id: a rename that looked like it had done
 * nothing.
 *
 * Asserted through the DOM rather than on the resolver, because the defect is
 * purely about WHICH resolved string the header binds to — the backend already
 * returned a correct title that nothing rendered. The editor and PDF panes are
 * mocked away for the same reason `PapyrusCloseProject` mocks them: Monaco
 * renders no accessible input under jsdom and the PDF pane fetches a blob URL.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PapyrusPage from '../apps/papyrus/PapyrusPage'
import { renderWithProviders } from './helpers'
import { papyrusApi, type ProjectDetail } from '../apps/papyrus/api'

vi.mock('../apps/papyrus/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/papyrus/api')>()),
  papyrusApi: {
    health: vi.fn(),
    listProjects: vi.fn(),
    getProject: vi.fn(),
    listFiles: vi.fn(),
    readFile: vi.fn(),
    saveFile: vi.fn(),
    setMainFile: vi.fn(),
    setTitle: vi.fn(),
    compile: vi.fn(),
    gitStatus: vi.fn(),
  },
}))

vi.mock('../apps/papyrus/PapyrusEditor', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<{ jumpToLine: (line: number) => void; focus: () => void }, unknown>(
      (_props, ref) => {
        useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }))
        return <textarea aria-label="editor" />
      },
    ),
  }
})

vi.mock('../apps/papyrus/PdfPreview', () => ({ default: () => <div data-testid="pdf" /> }))

const api = vi.mocked(papyrusApi)

const DIR = '6a16ddbf3be8905e101a7042'
const MAIN = 'main.tex'

/** Open the workspace on the one paper, with `title` as the server resolved it. */
async function openWorkspace(title: string) {
  api.getProject.mockResolvedValue({
    name: DIR, title, main_file: MAIN, files: [MAIN], has_pdf: false,
  })
  const user = userEvent.setup()
  renderWithProviders(<PapyrusPage />)
  await user.click(await screen.findByText(title || DIR))
  return within(await screen.findByTestId('papyrus-workspace'))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  api.health.mockResolvedValue({ status: 'ok', compiler: '/usr/bin/pdflatex', git: true })
  api.listProjects.mockResolvedValue({
    projects: [{ name: DIR, title: 'MACKEREL', modified: 0, has_pdf: false }],
  })
  api.listFiles.mockResolvedValue({ files: [MAIN] })
  api.readFile.mockResolvedValue({ path: MAIN, content: '\\documentclass{article}' })
  api.gitStatus.mockResolvedValue({ is_git: false })
})

describe('the toolbar of an open paper', () => {
  it('names the paper by its title, not by the directory key', async () => {
    const toolbar = await openWorkspace('MACKEREL')
    expect(toolbar.getByText('MACKEREL')).toBeInTheDocument()
    expect(toolbar.queryByText(DIR)).toBeNull()
  })

  it('falls back to the directory when the server sends no title', async () => {
    // An older backend has no `title` field; a header must never read "undefined".
    api.listProjects.mockResolvedValue({
      projects: [{ name: DIR, title: DIR, modified: 0, has_pdf: false }],
    })
    api.getProject.mockResolvedValue(
      { name: DIR, main_file: MAIN, files: [MAIN], has_pdf: false } as ProjectDetail,
    )
    const user = userEvent.setup()
    renderWithProviders(<PapyrusPage />)
    await user.click(await screen.findByText(DIR))
    const toolbar = within(await screen.findByTestId('papyrus-workspace'))
    expect(toolbar.getByText(DIR)).toBeInTheDocument()
    expect(toolbar.queryByText(/undefined/)).toBeNull()
  })

  it('shows a rename without a page reload', async () => {
    /**
     * The list and the workspace hold SEPARATE cache entries for the same paper,
     * so a rename that refreshed only the list left the paper opening under its
     * old name until the window was reloaded.
     *
     * `queryDefaults` mirrors the SHIPPED client's finite `staleTime`: under the
     * bare test client's `staleTime: 0` nothing is ever fresh, so a missing
     * invalidation would re-fetch anyway and this test would pass with its fix
     * reverted.
     */
    const server = (title: string) => {
      api.listProjects.mockResolvedValue({
        projects: [{ name: DIR, title, modified: 0, has_pdf: false }],
      })
      api.getProject.mockResolvedValue({
        name: DIR, title, main_file: MAIN, files: [MAIN], has_pdf: false,
      })
    }
    api.setTitle.mockImplementation(async (_name: string, title: string) => {
      server(title)
      return { ok: true, title }
    })

    server('Old name')
    const user = userEvent.setup()
    renderWithProviders(<PapyrusPage />, { queryDefaults: { staleTime: 30_000 } })

    // Open it once, so the workspace's own cache entry is populated and stale.
    await user.click(await screen.findByText('Old name'))
    expect(
      within(await screen.findByTestId('papyrus-workspace')).getByText('Old name'),
    ).toBeInTheDocument()

    // Back to the list, rename there.
    await user.click(screen.getByRole('button', { name: /papers/i }))
    const row = (await screen.findByText('Old name')).closest('tr') as HTMLElement
    await user.click(within(row).getByRole('button', { name: 'Rename Old name' }))
    const field = screen.getByLabelText('Paper name')
    await user.clear(field)
    await user.type(field, 'New name{Enter}')

    // Re-open: the header must carry the new name, with no reload in between.
    await user.click(await screen.findByText('New name'))
    const toolbar = within(await screen.findByTestId('papyrus-workspace'))
    await waitFor(() => expect(toolbar.getByText('New name')).toBeInTheDocument())
    expect(toolbar.queryByText('Old name')).toBeNull()
  })
})
