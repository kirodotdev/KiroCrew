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
import { readSource } from './readSource'
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

const PapyrusPageSource = readSource('src/apps/papyrus/PapyrusPage.tsx')

const PROJECT = 'thesis'
const MAIN = 'main.tex'

/** Open the workspace on a project with one dirty-able file.
 *
 * `queryDefaults` mirrors the SHIPPED client's `staleTime: 30_000`
 * (`api/queryClient.ts`). The bare test client uses React Query's `staleTime: 0`,
 * under which a cached entry is never considered fresh — so a `fetchQuery` that
 * wrongly serves the cache in production re-fetches happily in a test, and the
 * cache-freshness assertion below would pass with its fix reverted. */
async function openWorkspace() {
  const user = userEvent.setup()
  renderWithProviders(<PapyrusPage />, { queryDefaults: { staleTime: 30_000 } })

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
    // Without the flush this button would reset the buffer with no save,
    // leaving the edit gone with no way back.
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
 * `reloadOpenFile` must not `setDirty(false)` and then overwrite the buffer, which
 * would step around the very guard the passive adopt effect uses to protect unsaved
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
    // `flushBuffer` must not clear `dirty` as soon as the request resolves, or it
    // declares keystrokes typed DURING the save as saved when only the snapshot was —
    // the caller then transitions and loses them.
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
    // The dirty flag is easy to clear unconditionally after an await at any of the
    // three write call sites (close, create/pull, save-and-compile + push). This
    // asserts the structural guarantee: `flushBuffer` is the ONLY place that
    // calls the save mutation, so a new transition cannot reintroduce that.
    const src = PapyrusPageSource
    const calls = src.match(/saveMutation\.mutateAsync/g) ?? []
    expect(calls.length, 'saveMutation.mutateAsync must be called exactly once, inside flushBuffer').toBe(1)
    const flush = src.match(/const flushBuffer[\s\S]*?\n  \}, \[[^\]]*\]\)/)
    expect(flush![0]).toContain('saveMutation.mutateAsync')
  })

  it('does not overwrite text typed while the refresh read was in flight', async () => {
    // `reloadOpenFile` awaits `fetchQuery`, and the editor stays live across that
    // await. Without a post-await guard the fetched content would replace whatever
    // the user typed during the round trip — the same "a save completes and clobbers
    // in-flight typing" family as the flush bugs above, in the other direction.
    let releaseRead: (v: { path: string; content: string }) => void = () => {}
    const readInFlight = new Promise<{ path: string; content: string }>(resolve => {
      releaseRead = resolve
    })
    const user = await openWorkspace()

    // From here on, the refresh read hangs until we release it.
    api.readFile.mockReturnValueOnce(readInFlight as never)
    await user.click(screen.getByRole('button', { name: /pull/i }))
    await waitFor(() => expect(api.gitPull).toHaveBeenCalled())

    // The user keeps typing while the read is still outstanding.
    await makeDirty('\\documentclass{article}\n% typed DURING the refresh read')
    releaseRead({ path: MAIN, content: 'CONTENT FROM THE SERVER' })

    await waitFor(() => expect(api.readFile).toHaveBeenCalled())
    // The keystrokes survive; the stale response is dropped.
    expect(await screen.findByLabelText('editor')).toHaveValue(
      '\\documentclass{article}\n% typed DURING the refresh read',
    )
  })

  it('re-reads from disk after a pull instead of serving the seeded cache', async () => {
    // The interaction of two individually-correct decisions. `fileQuery` is
    // `staleTime: Infinity` (so a background refetch cannot clobber unsaved
    // typing) and `saveMutation.onSuccess` seeds that exact key with the text it
    // just wrote (so reopening a file does not re-adopt pre-save content). Given
    // both, `reloadOpenFile`'s `fetchQuery` finds a FRESH entry and returns the
    // cached pre-pull text without reading the disk at all — so the merge is
    // discarded and the next save writes the old text back over upstream's side.
    //
    // Requires the dirty-save first: that is what seeds the cache. Without it the
    // key is already stale and the bug is invisible, which is why the pull test
    // below passes either way.
    api.readFile.mockResolvedValue({ path: MAIN, content: 'MERGED FROM UPSTREAM' })
    const user = await openWorkspace()
    await makeDirty('\\documentclass{article}\n% mine')

    await user.click(screen.getByRole('button', { name: /pull/i }))

    await waitFor(() => expect(api.gitPull).toHaveBeenCalled())
    // The post-pull read actually happened...
    await waitFor(() => expect(api.readFile).toHaveBeenCalled())
    // ...and the editor shows the merged file, not the buffer the save seeded.
    expect(await screen.findByLabelText('editor')).toHaveValue('MERGED FROM UPSTREAM')
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

describe('Papyrus editor is read-only while the shown text is not the file', () => {
  // `currentFile` switches the moment the user picks a file (or a pull starts
  // rewriting it) but `buffer` only catches up when the fetch lands. A keystroke in
  // that window attaches the PREVIOUS file's text to the NEW path — which makes the
  // fetched content look "dirty" and get rejected, so the save writes the wrong text
  // over the selected file.
  //
  // Source contract: observing the read-only flag take effect needs a real Monaco
  // instance, and the editor is mocked here as a plain textarea. Asserting the
  // wiring is honest about what it checks.

  it('derives staleness from the query path, not a loading flag', () => {
    // `isFetching` is already false when the cache serves a DIFFERENT file's entry
    // synchronously, so a loading flag would leave the window open.
    expect(PapyrusPageSource).toContain('fileQuery.data?.path !== currentFile')
  })

  it('passes both windows to the editor as readOnly', () => {
    const prop = PapyrusPageSource.match(/readOnly=\{([^}]*)\}/)
    expect(prop, 'the editor is not given a readOnly prop').not.toBeNull()
    expect(prop![1]).toContain('contentIsStale')
    expect(prop![1]).toContain('pullMutation.isPending')
  })

  it('covers EVERY mutation that clears the dirty flag, not just the ones found so far', () => {
    // The class, not the instances. Any mutation that ends its `onSuccess` with
    // `setDirty(false)` + `setCurrentFile` opens the same window — e.g. deleting the
    // open file while typing drops those keystrokes AND suppresses the
    // unsaved-changes prompt.
    //
    // Rather than name today's mutations, this derives the requirement: any mutation
    // whose success handler clears `dirty` must appear in `readOnly`. A new one added
    // later fails here instead of shipping the bug again.
    const prop = PapyrusPageSource.match(/readOnly=\{([^}]*)\}/)![1]
    const clearsDirty = [...PapyrusPageSource.matchAll(
      /const (\w+Mutation) = useMutation\(\{([\s\S]*?)\n  \}\)/g,
    )]
      .filter(([, , body]) => /onSuccess[\s\S]*setDirty\(false\)/.test(body))
      .map(([, name]) => name)

    expect(clearsDirty.length, 'no dirty-clearing mutation found — the regex has drifted')
      .toBeGreaterThan(0)
    for (const name of clearsDirty) {
      expect(prop, `${name} clears the dirty flag but is not in the readOnly condition`)
        .toContain(`${name}.isPending`)
    }
  })

  // That the editor HONOURS the flag — rather than accepting and ignoring it — is
  // asserted behaviourally in PapyrusEditorSeed.test.tsx, which mocks only Pierre
  // and renders the real PapyrusEditor. It cannot live here: this file replaces
  // PapyrusEditor with a textarea so the buffer can be made dirty at all.
})

describe('a failed background refresh does not destroy the buffer', () => {
  // `projectQuery` does NOT set `refetchOnWindowFocus: false` (unlike `fileQuery`),
  // so returning to the tab refetches it. Running `setProject(null)` on ANY error
  // unmounts the workspace — and the editor buffer is the only copy of unsaved
  // typing, since Papyrus holds the working text in memory and writes on explicit
  // save. So a laptop resume, a restarting gateway or a dropped wifi connection
  // would silently discard whatever the user had typed.
  //
  // Same failure mode as the close-project guard this file was written for, reached
  // by a route nobody triggers deliberately — which makes it worse, not better.

  it('unmounts only when there is no cached project to keep working against', () => {
    // React Query keeps the last successful data alongside the error, so "has data"
    // separates a failed REFETCH from a failed initial open exactly.
    expect(PapyrusPageSource).toContain('if (!projectQuery.data) setProject(null)')
  })

  it('still surfaces the error either way', () => {
    // The fix must not turn a failed background refresh into silence — the user is
    // told, the document just is not taken away with it.
    const effect = PapyrusPageSource.match(
      /if \(!projectQuery\.isError\) return[\s\S]*?\n  \}, \[projectQuery\.isError[^\]]*\]\)/,
    )
    expect(effect, 'no error effect found — did it move?').not.toBeNull()
    expect(effect![0]).toContain('setError(')
    // `setError` before the conditional unmount, so the message is set unconditionally.
    expect(effect![0].indexOf('setError(')).toBeLessThan(
      effect![0].indexOf('setProject(null)'),
    )
  })

  it('watches data as well as the error, so a recovery re-renders', () => {
    // `projectQuery.data` has to be in the dep list: the decision now reads it, and
    // an effect that fires only on `isError` transitions would evaluate a stale copy.
    const effect = PapyrusPageSource.match(
      /if \(!projectQuery\.isError\) return[\s\S]*?\n  \}, \[(projectQuery\.isError[^\]]*)\]\)/,
    )
    expect(effect![1]).toContain('projectQuery.data')
  })
})

describe('a co-author refresh does not save over the agent edits it is showing', () => {
  // `reloadOpenFile` flushes the buffer before re-reading. For `pull` that is right
  // (the user's text predates the merge). For the co-author refresh it is backwards:
  // the AGENT just edited this file, so the browser buffer is the stale copy — and
  // flushing would save it over the agent's changes, then read the file back and
  // display the overwrite as if it were the agent's result, destroying the work the
  // refresh exists to show.

  it('the co-author refresh asks reloadOpenFile not to flush', () => {
    expect(PapyrusPageSource).toContain('await reloadOpenFile(false)')
  })

  it('pull still flushes, because its buffer predates the merge', () => {
    // The default must stay flush-on-dirty: `pull` relies on it for idempotence
    // (it flushes before the rebase and reloads after).
    expect(PapyrusPageSource).toMatch(/reloadOpenFile = useCallback\(async \(flushWhenDirty = true\)/)
    const pull = PapyrusPageSource.match(/onSuccess: async \(\) => \{[\s\S]*?invalidateGit[\s\S]*?\n    \}/)
    expect(pull, 'no pull onSuccess found').not.toBeNull()
    expect(pull![0]).toContain('await reloadOpenFile()')
  })

  it('a dirty buffer is left alone rather than clobbered in either direction', () => {
    // The no-flush path must ALSO not adopt the disk text, or it would discard the
    // user's typing instead of the agent's edits — the same bug mirrored.
    const fn = PapyrusPageSource.match(
      /const reloadOpenFile = useCallback\(async \(flushWhenDirty = true\)[\s\S]*?\n    const readFrom/,
    )
    expect(fn, 'reloadOpenFile prologue not found').not.toBeNull()
    // `return false` now: the function reports whether it ADOPTED the disk copy.
    expect(fn![0]).toMatch(/else if \(dirtyRef\.current\) \{[\s\S]*?return false\n/)
  })
})

describe('a failed conflict reload keeps the overwrite guard up', () => {
  // `resolveConflict` has to clear the guard BEFORE reloading — `reloadOpenFile`
  // refuses to adopt while the buffer is dirty. But leaving it cleared when the reload
  // fails is exactly what the guard exists to prevent: the editor still shows the stale
  // buffer and it would now be writable, so the next save overwrites the co-author's
  // version — the same overwrite, reached by a failed recovery instead of an edit.

  const resolver = PapyrusPageSource.match(
    /const resolveConflict = useCallback\(async \(\) => \{[\s\S]*?\n  \}, \[reloadOpenFile\]\)/,
  )

  it('restores both flags when the reload does not adopt', () => {
    expect(resolver, 'resolveConflict not found').not.toBeNull()
    expect(resolver![0]).toContain('conflictFileRef.current = conflicted')
    // `dirty` too: without it the buffer is "clean" and only the derived read-only
    // state holds it, so a later render could let typing through.
    expect(resolver![0]).toContain('dirtyRef.current = true')
  })

  it('treats a throw and a no-adopt return the same way', () => {
    // Two distinct failure modes: `fetchQuery` rejecting, and `reloadOpenFile`
    // returning early without adopting (a stale response, or a buffer that went dirty
    // mid-flight). Both must restore the guard.
    expect(resolver![0]).toMatch(/adopted = await reloadOpenFile\(false\)/)
    expect(resolver![0]).toMatch(/catch \{[\s\S]*?adopted = false/)
    expect(resolver![0]).toContain('if (!adopted) {')
  })

  it('reloadOpenFile reports adoption rather than absence of error', () => {
    // The distinction the guard depends on.
    expect(PapyrusPageSource).toMatch(
      /reloadOpenFile = useCallback\(async \(flushWhenDirty = true\): Promise<boolean>/,
    )
  })
})

describe('an unresolved co-author conflict blocks the save', () => {
  // Refusing to adopt the disk text is only half the story. Nothing has CHANGED about
  // the buffer, so the user's next Cmd+S would write it straight over the agent's
  // version — the clobber is postponed, not prevented, and this time it happens
  // silently with no refresh to blame it on.

  it('records the divergence instead of just returning', () => {
    const fn = PapyrusPageSource.match(
      /const reloadOpenFile = useCallback\(async \(flushWhenDirty = true\)[\s\S]*?\n    const readFrom/,
    )
    expect(fn, 'reloadOpenFile prologue not found').not.toBeNull()
    expect(fn![0]).toContain('setConflictFile(currentFile)')
  })

  it('refuses the write at flushBuffer, the one save chokepoint', () => {
    // Cmd+S, compile, pull, close and switching files all go through `flushBuffer`,
    // so refusing there covers every one instead of each remembering to check.
    const flush = PapyrusPageSource.match(
      /const flushBuffer = useCallback\(async \(\): Promise<boolean> => \{[\s\S]*?\n    const written =/,
    )
    expect(flush, 'flushBuffer prologue not found').not.toBeNull()
    expect(flush![0]).toContain('conflictFileRef.current === bufferFileRef.current')
    expect(flush![0]).toContain('return false')
  })

  it('reads the conflict from a ref, not state', () => {
    // Same reason `dirty` is mirrored: `flushBuffer` runs inside async chains that
    // captured an earlier render, and a conflict recorded during the same chain is
    // invisible to a state read.
    expect(PapyrusPageSource).toContain('conflictFileRef.current = conflictFile')
  })

  it('is visible and has an exit', () => {
    // A silent read-only editor whose saves fail would be worse than the overwrite.
    expect(PapyrusPageSource).toContain('apps.papyrus.workspace.co_author_conflict')
    expect(PapyrusPageSource).toContain('onClick={resolveConflict}')
    expect(PapyrusPageSource).toMatch(/readOnly=\{[^}]*hasConflict[^}]*\}/)
  })

  it('resolving clears the flag before reloading, so it cannot re-arm', () => {
    // `reloadOpenFile` refuses to adopt while the buffer is dirty, and its no-flush
    // branch would otherwise re-record the very conflict being resolved.
    const resolver = PapyrusPageSource.match(
      /const resolveConflict = useCallback\(async \(\) => \{[\s\S]*?\n  \}, \[reloadOpenFile\]\)/,
    )
    expect(resolver, 'resolveConflict not found').not.toBeNull()
    expect(resolver![0].indexOf('conflictFileRef.current = null')).toBeLessThan(
      resolver![0].indexOf('await reloadOpenFile'),
    )
    expect(resolver![0]).toContain('dirtyRef.current = false')
  })
})

describe('the conflict guard is not window-dependent', () => {
  // The `dirtyRef` PRE-check records a conflict when the buffer is already dirty. But a
  // keystroke landing DURING the fetch hits the post-await guard instead, which returns
  // without adopting — so typing before the fetch is protected while typing during it
  // is not, and the next save would silently overwrite the agent unless the post-await
  // guard records the conflict too.

  it('records the conflict at the post-await guard too', () => {
    const fn = PapyrusPageSource.match(
      /const reloadOpenFile = useCallback\(async \(flushWhenDirty = true\)[\s\S]*?\n  \}, \[project, currentFile, flushBuffer, queryClient\]\)/,
    )
    expect(fn, 'reloadOpenFile not found').not.toBeNull()
    expect(fn![0]).toContain('if (!flushWhenDirty && dirtyRef.current && bufferFileRef.current === readFrom)')
  })

  it('does not mark a newly-opened file as conflicted', () => {
    // A mid-flight file SWITCH also reaches that guard. Recording against the new
    // file would block saves on a document that never diverged.
    const fn = PapyrusPageSource.match(
      /if \(bufferFileRef\.current !== readFrom \|\| dirtyRef\.current\) \{[\s\S]*?\n      return false/,
    )
    expect(fn![0]).toContain('bufferFileRef.current === readFrom')
  })

  it('only the no-flush path records — pull already saved the buffer', () => {
    // `flushWhenDirty: true` (pull) wrote the user's text to disk first, so a dirty
    // buffer afterwards is not a divergence and must not block saves.
    const fn = PapyrusPageSource.match(
      /if \(bufferFileRef\.current !== readFrom \|\| dirtyRef\.current\) \{[\s\S]*?\n      return false/,
    )
    expect(fn![0]).toContain('!flushWhenDirty')
  })

  it('the editor is read-only while a file create is in flight', () => {
    // `createFileMutation` flushes, awaits the create, then SWITCHES `currentFile` —
    // so a keystroke in that window attaches to a buffer the switch abandons.
    const prop = PapyrusPageSource.match(/readOnly=\{([\s\S]*?)\}\n/)
    expect(prop, 'no readOnly prop found').not.toBeNull()
    expect(prop![1]).toContain('createFileMutation.isPending')
  })
})

describe('leaving for the full chat page flushes first', () => {
  // Navigating away UNMOUNTS this page, and the editor buffer lives only in memory
  // until a save lands — so routing out without flushing does not "forget" the work, it
  // destroys it. Exactly what `closeProject` is careful about, reached by a different
  // button.

  it('awaits the flush before navigating', () => {
    const handler = PapyrusPageSource.match(
      /const openFullChat = useCallback\(async \(\) => \{[\s\S]*?\n  \}, \[flushBuffer, navigate, slotKey\]\)/,
    )
    expect(handler, 'openFullChat not found').not.toBeNull()
    // Abort on a failed flush rather than trading the user's text for a chat view.
    expect(handler![0]).toContain('if (!(await flushBuffer())) return')
    expect(handler![0].indexOf('flushBuffer')).toBeLessThan(
      handler![0].indexOf('navigate('),
    )
  })

  it('the panel is wired to the guarded handler, not a bare navigate', () => {
    expect(PapyrusPageSource).toContain('onOpenFull={openFullChat}')
    expect(PapyrusPageSource).not.toMatch(/onOpenFull=\{\(\) => navigate\(/)
  })
})

describe('the browser cannot silently discard the buffer', () => {
  // Every IN-APP exit flushes (`closeProject`, `openFile`, `openFullChat`,
  // `createFileMutation`), but none of them runs on ⌘R or a tab close: React never
  // unmounts in a way we can await, so `beforeunload` is the only hook the platform
  // offers.

  it('warns rather than trying to save', () => {
    // `beforeunload` cannot await, so a flush started there is not guaranteed to reach
    // disk — a half-written file would be worse than the prompt. Letting the user cancel
    // and press Cmd+S is honest about what the platform can do.
    const guard = PapyrusPageSource.match(
      /useEffect\(\(\) => \{\n    if \(!dirty\) return[\s\S]*?\n  \}, \[dirty\]\)/,
    )
    expect(guard, 'no beforeunload guard found').not.toBeNull()
    expect(guard![0]).toContain("window.addEventListener('beforeunload', warn)")
    expect(guard![0]).not.toContain('flushBuffer')
  })

  it('arms both the modern and legacy signals', () => {
    // Browsers disagree about which one opens the dialog; the string is ignored by all.
    const guard = PapyrusPageSource.match(
      /useEffect\(\(\) => \{\n    if \(!dirty\) return[\s\S]*?\n  \}, \[dirty\]\)/,
    )
    expect(guard![0]).toContain('event.preventDefault()')
    expect(guard![0]).toContain("event.returnValue = ''")
  })

  it('is registered only while dirty, and removed after', () => {
    // A permanent listener would prompt on every ordinary reload.
    const guard = PapyrusPageSource.match(
      /useEffect\(\(\) => \{\n    if \(!dirty\) return[\s\S]*?\n  \}, \[dirty\]\)/,
    )
    expect(guard![0]).toContain('if (!dirty) return')
    expect(guard![0]).toContain("removeEventListener('beforeunload', warn)")
  })
})
