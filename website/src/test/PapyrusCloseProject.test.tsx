/**
 * Leaving the workspace must not silently destroy an unsaved edit.
 *
 * The editor buffer lives ONLY in React state until a save lands, so resetting it
 * without flushing does not merely "forget" the work — it destroys it, with
 * nothing on disk to recover from. The toolbar advertises "Editing {file} —
 * unsaved" immediately next to the button that leaves, which is what makes a
 * silent discard so surprising. `openFile` already flushes before switching
 * files; `closeProject` must behave the same way, or the two ways of navigating
 * away from a file disagree.
 *
 * This lives in its own file for the same reason `ArtifactDetailPage.dirtyDelete`
 * does: reaching `dirty === true` needs an editor that emits `onChange`, and the
 * real one is Monaco, which renders no accessible input under jsdom. PapyrusEditor
 * is therefore mocked down to a textarea wired to `onChange` — the subject under
 * test is the page's flush-on-leave guard, not the editor.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PapyrusPage from '../apps/papyrus/PapyrusPage'
import { renderWithProviders } from './helpers'
import { readFileSync } from 'node:fs'
import { papyrusApi } from '../apps/papyrus/api'

vi.mock('../apps/papyrus/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../apps/papyrus/api')>()),
  papyrusApi: {
    health: vi.fn(),
    listProjects: vi.fn(),
    getProject: vi.fn(),
    listFiles: vi.fn(),
    readFile: vi.fn(),
    saveFile: vi.fn(),
    createFile: vi.fn(),
    deleteFile: vi.fn(),
    setMainFile: vi.fn(),
    compile: vi.fn(),
    gitStatus: vi.fn(),
    gitCommit: vi.fn(),
    gitPush: vi.fn(),
    gitPull: vi.fn(),
    deleteProject: vi.fn(),
    createProject: vi.fn(),
    cloneProject: vi.fn(),
  },
}))

// Replace ONLY the editor: Monaco has no accessible input under jsdom, and the
// buffer can't be made dirty without one. forwardRef because the page attaches a
// `PapyrusEditorHandle` ref for jump-to-line; a plain function component would
// warn and drop it.
vi.mock('../apps/papyrus/PapyrusEditor', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<
      { jumpToLine: (line: number) => void; focus: () => void },
      { value: string; onChange: (v: string) => void }
    >(({ value, onChange }, ref) => {
      useImperativeHandle(ref, () => ({ jumpToLine: () => {}, focus: () => {} }))
      return <textarea aria-label="editor" value={value} onChange={e => onChange(e.target.value)} />
    }),
  }
})

// The PDF pane fetches a blob URL; irrelevant here and noisy under jsdom.
vi.mock('../apps/papyrus/PdfPreview', () => ({ default: () => <div data-testid="pdf" /> }))

const api = vi.mocked(papyrusApi)

const PapyrusPageSource = readFileSync(
  'src/apps/papyrus/PapyrusPage.tsx',
  'utf-8',
)

const PROJECT = 'thesis'
const MAIN = 'main.tex'

/** Open the workspace on a project with one dirty-able file. */
async function openWorkspace() {
  const user = userEvent.setup()
  renderWithProviders(<PapyrusPage />)

  // ProjectList first; click through into the workspace.
  const card = await screen.findByText(PROJECT)
  await user.click(card)
  await screen.findByTestId('papyrus-workspace')
  return user
}

/** Type into the mocked editor so the page's `dirty` flag flips. */
async function makeDirty(text: string) {
  const editor = await screen.findByLabelText('editor')
  fireEvent.change(editor, { target: { value: text } })
  return editor
}

describe('Papyrus closeProject', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.health.mockResolvedValue({ status: 'ok', compiler: '/usr/bin/pdflatex', git: true })
    api.listProjects.mockResolvedValue({
      projects: [{ name: PROJECT, modified: 0, has_pdf: false }],
    })
    api.getProject.mockResolvedValue({
      name: PROJECT, main_file: MAIN, files: [MAIN], has_pdf: false,
    })
    api.listFiles.mockResolvedValue({ files: [MAIN] })
    api.readFile.mockResolvedValue({ path: MAIN, content: '\\documentclass{article}' })
    api.saveFile.mockResolvedValue({ ok: true, path: MAIN })
    api.gitStatus.mockResolvedValue({ is_git: false })
  })

  it('flushes an unsaved buffer before leaving the workspace', async () => {
    // The regression: this button reset the buffer with no save, so the edit was
    // gone with no way back.
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% precious unsaved edit')

    await user.click(screen.getByRole('button', { name: /papers/i }))

    await waitFor(() =>
      expect(api.saveFile).toHaveBeenCalledWith(
        PROJECT, MAIN, '\\documentclass{article}\n% precious unsaved edit',
      ),
    )
    // ...and only then does it actually leave.
    await waitFor(() => expect(screen.queryByTestId('papyrus-workspace')).not.toBeInTheDocument())
  })

  it('stays in the workspace when the flush fails, rather than discarding the edit', async () => {
    // Tearing down on a failed save would destroy exactly the work the flush
    // exists to protect, so a write error must keep the buffer on screen.
    api.saveFile.mockRejectedValue(new Error('disk full'))
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% unsaved and unsavable')

    await user.click(screen.getByRole('button', { name: /papers/i }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(screen.getByTestId('papyrus-workspace')).toBeInTheDocument()
    expect(await screen.findByText(/disk full/)).toBeInTheDocument()
  })

  it('does not write when the buffer is clean', async () => {
    // Leaving without editing must not manufacture a commit-worthy file write.
    const user = await openWorkspace()
    await user.click(screen.getByRole('button', { name: /papers/i }))

    await waitFor(() => expect(screen.queryByTestId('papyrus-workspace')).not.toBeInTheDocument())
    expect(api.saveFile).not.toHaveBeenCalled()
  })
})

/**
 * A BACKGROUND refresh is the nastier half of the same bug: the user did not ask
 * for it, so losing the buffer to a finishing git pull (or the co-author's turn
 * ending) is even less recoverable than losing it to a button they clicked.
 *
 * `reloadOpenFile` used to `setDirty(false)` and then overwrite the buffer, which
 * stepped around the very guard the passive adopt effect uses to protect unsaved
 * typing.
 */
describe('Papyrus reloadOpenFile', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    api.health.mockResolvedValue({ status: 'ok', compiler: '/usr/bin/pdflatex', git: true })
    api.listProjects.mockResolvedValue({
      projects: [{ name: PROJECT, modified: 0, has_pdf: false }],
    })
    api.getProject.mockResolvedValue({
      name: PROJECT, main_file: MAIN, files: [MAIN], has_pdf: false,
    })
    api.listFiles.mockResolvedValue({ files: [MAIN] })
    api.readFile.mockResolvedValue({ path: MAIN, content: '\\documentclass{article}' })
    api.saveFile.mockResolvedValue({ ok: true, path: MAIN })
    // A git repo with a remote, so the toolbar offers Pull.
    api.gitStatus.mockResolvedValue({
      is_git: true, branch: 'main', dirty: false, has_remote: true,
      ahead: 0, behind: 1, changes: [], recent_commits: [],
    })
    api.gitPull.mockResolvedValue({ ok: true, output: 'Fast-forward', stashed: false })
    api.compile.mockResolvedValue({ ok: true, log: '', errors: [], duration_ms: 10 })
  })

  it('flushes an unsaved buffer before a pull replaces it', async () => {
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% typed while the pull was in flight')

    await user.click(screen.getByRole('button', { name: /pull/i }))

    await waitFor(() =>
      expect(api.saveFile).toHaveBeenCalledWith(
        PROJECT, MAIN, '\\documentclass{article}\n% typed while the pull was in flight',
      ),
    )
  })

  it('does not replace the buffer when the flush fails', async () => {
    // Aborting the reload is the point: showing disk content the user never saw,
    // in place of the edit that could not be written, would be the worse outcome.
    api.saveFile.mockRejectedValue(new Error('disk full'))
    api.readFile.mockResolvedValue({ path: MAIN, content: 'REPLACED FROM DISK' })
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% unsaved and unsavable')

    await user.click(screen.getByRole('button', { name: /pull/i }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(await screen.findByLabelText('editor')).toHaveValue(
      '\\documentclass{article}\n% unsaved and unsavable',
    )
  })

  it('flushes before a create switches the open file away', async () => {
    // `createFile`'s onSuccess sets `currentFile` to the new file, abandoning the
    // outgoing buffer — so the flush has to happen before the create, not after.
    api.createFile.mockResolvedValue({ ok: true, path: 'chapter2.tex' })
    vi.stubGlobal('prompt', vi.fn(() => 'chapter2.tex'))
    await openWorkspace()
    await makeDirty('\\documentclass{article}\n% about to create a new file')

    fireEvent.click(screen.getByRole('button', { name: /new file/i }))

    await waitFor(() =>
      expect(api.saveFile).toHaveBeenCalledWith(
        PROJECT, MAIN, '\\documentclass{article}\n% about to create a new file',
      ),
    )
  })

  it('does not create the file when the flush fails', async () => {
    // Trading the user's unsaved text for a new empty file is the worst outcome.
    api.saveFile.mockRejectedValue(new Error('disk full'))
    api.createFile.mockResolvedValue({ ok: true, path: 'chapter2.tex' })
    vi.stubGlobal('prompt', vi.fn(() => 'chapter2.tex'))
    await openWorkspace()
    await makeDirty('\\documentclass{article}\n% unsaveable')

    fireEvent.click(screen.getByRole('button', { name: /new file/i }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(api.createFile).not.toHaveBeenCalled()
  })

  it('flushes BEFORE the pull rewrites the file on disk', async () => {
    // Saving after a rebase would push the pre-pull buffer over upstream's version.
    const order: string[] = []
    api.saveFile.mockImplementation(async () => {
      order.push('save')
      return { ok: true, path: MAIN }
    })
    api.gitPull.mockImplementation(async () => {
      order.push('pull')
      return { ok: true, output: 'Fast-forward', stashed: false }
    })
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% typed before the pull')

    await user.click(screen.getByRole('button', { name: /pull/i }))

    await waitFor(() => expect(order).toContain('pull'))
    expect(order[0]).toBe('save')
  })

  it('does not report success when the buffer changed during the save', async () => {
    // `flushBuffer` used to clear `dirty` as soon as the request resolved, declaring
    // keystrokes typed DURING the save as saved when only the snapshot was — the
    // caller then transitioned and lost them.
    //
    // Asserted on the source rather than through the UI: reaching the race through a
    // handler requires the post-await render to have re-created the callback, and
    // every UI path I tried passed identically with the guard removed — i.e. proved
    // nothing. The contract that actually matters is small and local, so it is
    // checked where it lives. (A behavioural test would need the editor to be the
    // real Monaco; noted rather than faked.)
    const src = PapyrusPageSource
    const flush = src.match(/const flushBuffer[\s\S]*?\n  \}, \[[^\]]*\]\)/)
    expect(flush, 'flushBuffer not found — did it move?').toBeTruthy()
    const body = flush![0]
    // It snapshots what it writes...
    expect(body).toMatch(/const written = bufferRef\.current/)
    // ...and refuses to report success if the live buffer has moved on.
    expect(body).toMatch(/bufferRef\.current !== written/)
    expect(body).toMatch(/bufferFileRef\.current !== writtenTo/)
    // The refusal must come BEFORE the flag is cleared.
    expect(body.indexOf('!== written')).toBeLessThan(body.indexOf('dirtyRef.current = false'))
  })

  it('every write goes through flushBuffer — no direct saveMutation call', async () => {
    // The bug recurred at three separate call sites (close, create/pull, then
    // save-and-compile + push), each clearing `dirty` unconditionally after its own
    // await. This asserts the structural fix: `flushBuffer` is the ONLY place that
    // calls the save mutation, so a new transition cannot reintroduce it.
    const src = PapyrusPageSource
    const calls = src.match(/saveMutation\.mutateAsync/g) ?? []
    expect(calls.length, 'saveMutation.mutateAsync must be called exactly once, inside flushBuffer').toBe(1)
    const flush = src.match(/const flushBuffer[\s\S]*?\n  \}, \[[^\]]*\]\)/)
    expect(flush![0]).toContain('saveMutation.mutateAsync')
  })

  it('does not write the stale buffer back after a successful pull', async () => {
    // The regression this catches is subtle and was introduced BY the pre-pull
    // flush: `flushBuffer` left `dirty` set, so `reloadOpenFile` in onSuccess
    // flushed AGAIN — writing the now-stale pre-pull buffer over the merged file
    // and silently discarding upstream's side of a clean disjoint merge.
    const order: string[] = []
    api.saveFile.mockImplementation(async () => {
      order.push('save')
      return { ok: true, path: MAIN }
    })
    api.gitPull.mockImplementation(async () => {
      order.push('pull')
      return { ok: true, output: 'Fast-forward', stashed: false }
    })
    api.readFile.mockResolvedValue({ path: MAIN, content: 'MERGED FROM UPSTREAM' })
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% mine')

    await user.click(screen.getByRole('button', { name: /pull/i }))

    await waitFor(() => expect(order).toContain('pull'))
    // Exactly ONE save, and it happened before the pull.
    expect(order).toEqual(['save', 'pull'])
    // ...and the editor shows what the pull produced, not the pre-pull buffer.
    expect(await screen.findByLabelText('editor')).toHaveValue('MERGED FROM UPSTREAM')
  })

  it('does not pull when the flush fails', async () => {
    api.saveFile.mockRejectedValue(new Error('disk full'))
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% unsaveable')

    await user.click(screen.getByRole('button', { name: /pull/i }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(api.gitPull).not.toHaveBeenCalled()
  })

  it('still refreshes the buffer when nothing was unsaved', async () => {
    // The feature itself must survive the fix: a pull that rewrites the file has
    // to show up in the editor.
    api.readFile.mockResolvedValue({ path: MAIN, content: 'PULLED FROM REMOTE' })
    const user = await openWorkspace()

    await user.click(screen.getByRole('button', { name: /pull/i }))

    await waitFor(() => expect(api.gitPull).toHaveBeenCalled())
    expect(await screen.findByLabelText('editor')).toHaveValue('PULLED FROM REMOTE')
    expect(api.saveFile).not.toHaveBeenCalled()
  })
})
