/**
 * PapyrusPage — the workspace half of the page, beyond the flush-on-leave guards
 * that `PapyrusCloseProject.test.tsx` already pins.
 *
 * What is exercised here is everything the toolbar and the co-author session can
 * reach: compile (both verdicts, the duration readout and the failure banner),
 * the git row (pull, commit-and-push, the branch/ahead/behind label), the file
 * tree's open / delete / set-main mutations, the co-author slot lifecycle, and
 * the conflict state a co-author edit produces when the buffer is dirty.
 *
 * The same two mocks `PapyrusCloseProject` needs apply for the same reasons:
 * Monaco renders no accessible input under jsdom, so `PapyrusEditor` is reduced
 * to a textarea (plus two buttons that call the cursor and jump handles the page
 * wires to it), and `PdfPreview` fetches a blob URL that only produces noise.
 * `CoAuthorPanel` is stubbed too — it mounts the entire ChatPage, and the subject
 * under test is the page's session handlers, not that chat.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PapyrusPage from '../apps/papyrus/PapyrusPage'
import { createTestStore, renderWithProviders } from './helpers'
import { papyrusApi } from '../apps/papyrus/api'
import { api as client } from '../api/client'
import { sseSlots } from '../store/dashboardSlice'
import { LAST_PROJECT_KEY, SLOT_KEY_PREFIX } from '../apps/papyrus/lib'
import type { ChatSlot } from '../types'

const hoisted = vi.hoisted(() => ({
  jumpToLine: vi.fn(),
  navigate: vi.fn(),
}))

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

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      createChatSlot: vi.fn(),
      chatSlotContext: vi.fn(),
      chatSlots: vi.fn(),
    },
  }
})

// `openFullChat` routes away; the assertion is that it flushed and then navigated,
// which needs the destination observable without a second route in the tree.
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => hoisted.navigate,
}))

// Monaco has no accessible input under jsdom. The two extra buttons stand in for
// the editor callbacks the page owns but Monaco would otherwise be the only caller
// of: the cursor readout and the diagnostics jump handle.
vi.mock('../apps/papyrus/PapyrusEditor', async () => {
  const { forwardRef, useImperativeHandle } = await import('react')
  return {
    default: forwardRef<
      { jumpToLine: (line: number) => void; focus: () => void },
      {
        value: string
        onChange: (v: string) => void
        onSave?: () => void
        onCursorChange?: (line: number, column: number) => void
      }
    >(({ value, onChange, onSave, onCursorChange }, ref) => {
      useImperativeHandle(ref, () => ({
        jumpToLine: hoisted.jumpToLine,
        focus: () => {},
      }))
      return (
        <>
          <textarea aria-label="editor" value={value} onChange={e => onChange(e.target.value)} />
          <button type="button" onClick={() => onCursorChange?.(12, 5)}>move caret</button>
          {/* Stands in for Monaco's Cmd+S binding, the one save path with no
              button to disable — which is why the page also keeps a ref guard. */}
          <button type="button" onClick={() => onSave?.()}>save shortcut</button>
        </>
      )
    }),
  }
})

vi.mock('../apps/papyrus/PdfPreview', () => ({ default: () => <div data-testid="pdf" /> }))

// CoAuthorPanel mounts the whole ChatPage; only the three handlers the page passes
// it are under test here.
vi.mock('../apps/papyrus/CoAuthorPanel', () => ({
  default: ({ onStartSession, onOpenFull, onClose }: {
    onStartSession: () => void
    onOpenFull: () => void
    onClose: () => void
  }) => (
    <div data-testid="co-author-panel">
      <button type="button" onClick={onStartSession}>start session</button>
      <button type="button" onClick={onOpenFull}>open full chat</button>
      <button type="button" onClick={onClose}>close panel</button>
    </div>
  ),
}))

const api = vi.mocked(papyrusApi)
const chat = vi.mocked(client)

const PROJECT = 'thesis'
const MAIN = 'main.tex'
const CHAPTER = 'chapters.tex'
const SLOT = 'papyrus-slot-1'
const BODY = '\\documentclass{article}'

/** A promise plus its settle handles, for testing what happens mid-flight. */
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (err: Error) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const slot = (over: Partial<ChatSlot>): ChatSlot =>
  ({ key: SLOT, title: 'Papyrus: thesis', messages: 0, running: false, ...over })

/** Render straight into the workspace: the remembered project opens on mount, so
 *  no click through ProjectList is needed for a toolbar assertion. */
function openWorkspace(store = createTestStore()) {
  localStorage.setItem(LAST_PROJECT_KEY, PROJECT)
  const rendered = renderWithProviders(<PapyrusPage />, {
    store,
    queryDefaults: { staleTime: 30_000 },
  })
  return { ...rendered, user: userEvent.setup() }
}

async function workspaceReady() {
  await screen.findByTestId('papyrus-workspace')
  await screen.findByLabelText('editor')
  // The editor mounts against `mainFile` BEFORE the open-main effect sets
  // `currentFile`, so a mounted editor does not mean the first read has been
  // issued — the read query is still disabled until then. Wait for that read,
  // or a test that takes this barrier as "settled" (clearing `readFile`, or
  // counting saves) credits the mount-time read to whatever it does next.
  await waitFor(() => expect(api.readFile).toHaveBeenCalled())
}

function makeDirty(text: string) {
  fireEvent.change(screen.getByLabelText('editor'), { target: { value: text } })
}

const compileBtn = () => screen.getByRole('button', { name: /^Compile$/ })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  api.health.mockResolvedValue({ status: 'ok', compiler: '/usr/bin/pdflatex', git: true })
  api.listProjects.mockResolvedValue({ projects: [{ name: PROJECT, modified: 0, has_pdf: false }] })
  api.getProject.mockResolvedValue({
    name: PROJECT, main_file: MAIN, files: [MAIN, CHAPTER], has_pdf: false,
  })
  api.listFiles.mockResolvedValue({ files: [MAIN, CHAPTER] })
  api.readFile.mockImplementation((_name: string, path: string) =>
    Promise.resolve({ path, content: BODY }))
  api.saveFile.mockResolvedValue({ ok: true, path: MAIN })
  api.gitStatus.mockResolvedValue({ is_git: false })
  api.compile.mockResolvedValue({ ok: true, log: '', errors: [], duration_ms: 1234 })
  // `fetchSlots` writes the payload straight into `state.slots`, so it is the bare
  // array, not an envelope.
  chat.chatSlots.mockResolvedValue([])
  chat.createChatSlot.mockResolvedValue({ key: SLOT, title: 'Papyrus: thesis' })
  chat.chatSlotContext.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Papyrus compile', () => {
  it('compiles, reveals the PDF download and reports how long it took', async () => {
    const { user } = openWorkspace()
    await workspaceReady()
    // No PDF exists yet, so the download must not be advertised.
    expect(screen.queryByRole('link', { name: /Download PDF/ })).not.toBeInTheDocument()

    await user.click(compileBtn())

    await waitFor(() => expect(api.compile).toHaveBeenCalledWith(PROJECT))
    expect(await screen.findByRole('link', { name: /Download PDF/ })).toHaveAttribute(
      'download', `${PROJECT}.pdf`,
    )
    // Localized through `fmtUnit`, so a build over a second reads as seconds.
    expect(screen.getByText('1.2s')).toBeInTheDocument()
  })

  it('reports a sub-second build in milliseconds', async () => {
    // The threshold is the whole point of `compileDurationLabel`: `48231 ms` is
    // arithmetic the reader should not have to do.
    api.compile.mockResolvedValue({ ok: true, log: '', errors: [], duration_ms: 480 })
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(compileBtn())

    expect(await screen.findByText('480ms')).toBeInTheDocument()
  })

  it('opens the log pane and counts the errors when the compile fails', async () => {
    api.compile.mockResolvedValue({
      ok: false,
      log: 'noise',
      errors: [
        { level: 'error', message: 'Undefined control sequence.', line: 7, file: null },
        { level: 'warning', message: 'Reference undefined.', line: 9, file: null },
      ],
      duration_ms: 90,
    })
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(compileBtn())

    // A failed compile forces the diagnostics open rather than leaving the user to
    // find the Log toggle.
    expect(await screen.findByRole('button', { name: 'Go to line 7' })).toBeInTheDocument()
    expect(screen.getByText('1 error')).toBeInTheDocument()
    expect(screen.getByText('1 warning')).toBeInTheDocument()
    // ...and still no PDF, because nothing was typeset.
    expect(screen.queryByRole('link', { name: /Download PDF/ })).not.toBeInTheDocument()
  })

  it('tolerates a compile result whose errors field is not an array', async () => {
    api.compile.mockResolvedValue({
      ok: false, log: 'raw log only', errors: null as never, duration_ms: 5,
    })
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(compileBtn())

    expect(await screen.findByText('raw log only')).toBeInTheDocument()
  })

  it('surfaces a thrown compile as a dismissable banner', async () => {
    api.compile.mockRejectedValue(new Error('compiler missing'))
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(compileBtn())

    const banner = await screen.findByRole('alert')
    expect(within(banner).getByText('compiler missing')).toBeInTheDocument()

    await user.click(within(banner).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('aborts the compile when the buffer cannot be flushed', async () => {
    // The compiler reads the file off disk, so compiling past a failed save would
    // typeset a revision the user has already moved past.
    api.saveFile.mockRejectedValue(new Error('disk full'))
    const { user } = openWorkspace()
    await workspaceReady()
    makeDirty(`${BODY}\n% unsaved`)

    await user.click(compileBtn())

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(api.compile).not.toHaveBeenCalled()
  })

  it('shows a busy label and refuses a second compile while one is running', async () => {
    const gate = deferred<{ ok: boolean; log: string; errors: []; duration_ms: number }>()
    api.compile.mockReturnValue(gate.promise)
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(compileBtn())
    const busy = await screen.findByRole('button', { name: /Compiling/ })
    // Disabled AND re-entry guarded: the button covers the pointer, the ref covers
    // the Cmd+S path that has no button to disable.
    expect(busy).toBeDisabled()

    gate.resolve({ ok: true, log: '', errors: [], duration_ms: 10 })
    await waitFor(() => expect(screen.getByRole('button', { name: /^Compile$/ })).toBeEnabled())
    expect(api.compile).toHaveBeenCalledTimes(1)
  })

  it('refuses a second Cmd+S while a compile is already running', async () => {
    // The keyboard path has no button to disable, which is the whole reason for the
    // re-entry ref: two quick presses must not run two compiles.
    const gate = deferred<{ ok: boolean; log: string; errors: []; duration_ms: number }>()
    api.compile.mockReturnValue(gate.promise)
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: 'save shortcut' }))
    await waitFor(() => expect(api.compile).toHaveBeenCalledTimes(1))
    await user.click(screen.getByRole('button', { name: 'save shortcut' }))

    expect(api.compile).toHaveBeenCalledTimes(1)
    gate.resolve({ ok: true, log: '', errors: [], duration_ms: 10 })
    await waitFor(() => expect(screen.getByRole('button', { name: /^Compile$/ })).toBeEnabled())
  })
})

describe('Papyrus status bar and log pane', () => {
  it('toggles the log pane and marks its pressed state', async () => {
    const { user } = openWorkspace()
    await workspaceReady()
    const toggle = screen.getByRole('button', { name: /^Log$/ })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')

    await user.click(toggle)

    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(await screen.findByText('The compiler reported nothing.')).toBeInTheDocument()
  })

  it('follows the caret into the status bar', async () => {
    const { user } = openWorkspace()
    await workspaceReady()
    expect(screen.getByText('Ln 1, Col 1')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'move caret' }))

    expect(screen.getByText('Ln 12, Col 5')).toBeInTheDocument()
  })

  it('jumps the editor to a diagnostic line', async () => {
    api.compile.mockResolvedValue({
      ok: false,
      log: '',
      errors: [{ level: 'error', message: 'Undefined control sequence.', line: 7, file: null }],
      duration_ms: 3,
    })
    const { user } = openWorkspace()
    await workspaceReady()
    await user.click(compileBtn())

    await user.click(await screen.findByRole('button', { name: 'Go to line 7' }))

    expect(hoisted.jumpToLine).toHaveBeenCalledWith(7)
  })

  it('counts the words in the buffer', async () => {
    openWorkspace()
    await workspaceReady()
    makeDirty('one two three')
    expect(await screen.findByText('3 words')).toBeInTheDocument()
  })
})

describe('Papyrus file tree mutations', () => {
  it('opens another file, flushing the outgoing buffer first', async () => {
    const { user } = openWorkspace()
    await workspaceReady()
    makeDirty(`${BODY}\n% keep me`)

    await user.click(screen.getByRole('button', { name: CHAPTER }))

    await waitFor(() =>
      expect(api.saveFile).toHaveBeenCalledWith(PROJECT, MAIN, `${BODY}\n% keep me`))
    expect(await screen.findByText(`Editing ${CHAPTER}`)).toBeInTheDocument()
  })

  it('stays on the current file when the outgoing flush fails', async () => {
    api.saveFile.mockRejectedValue(new Error('read-only volume'))
    const { user } = openWorkspace()
    await workspaceReady()
    makeDirty(`${BODY}\n% unsavable`)

    await user.click(screen.getByRole('button', { name: CHAPTER }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(screen.getByText(`Editing ${MAIN} — unsaved`)).toBeInTheDocument()
  })

  it('ignores a click on the file already open', async () => {
    const { user } = openWorkspace()
    await workspaceReady()
    api.readFile.mockClear()

    await user.click(screen.getByRole('button', { name: `${MAIN} Main` }))

    expect(api.readFile).not.toHaveBeenCalled()
  })

  it('deletes the open file after confirmation and falls back to the main document', async () => {
    // The main document has no delete control, so the open-file case is reached by
    // deleting a chapter the user is currently editing.
    api.deleteFile.mockResolvedValue({ ok: true, path: CHAPTER })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { user } = openWorkspace()
    await workspaceReady()
    await user.click(screen.getByRole('button', { name: CHAPTER }))
    await screen.findByText(`Editing ${CHAPTER}`)

    await user.click(screen.getByRole('button', { name: `Delete ${CHAPTER}` }))

    await waitFor(() => expect(api.deleteFile).toHaveBeenCalledWith(PROJECT, CHAPTER))
    expect(await screen.findByText(`Editing ${MAIN}`)).toBeInTheDocument()
  })

  it('does not delete when the confirmation is declined', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: `Delete ${CHAPTER}` }))

    expect(api.deleteFile).not.toHaveBeenCalled()
  })

  it('reports a failed delete', async () => {
    api.deleteFile.mockRejectedValue(new Error('file is locked'))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: `Delete ${CHAPTER}` }))

    expect(await screen.findByText('file is locked')).toBeInTheDocument()
  })

  it('reports a failed create without inventing a flush error', async () => {
    api.createFile.mockRejectedValue(new Error('name already taken'))
    vi.spyOn(window, 'prompt').mockReturnValue('methods.tex')
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: 'New file' }))

    expect(await screen.findByText('name already taken')).toBeInTheDocument()
  })

  it('changes the main document through the picker', async () => {
    api.setMainFile.mockResolvedValue({ ok: true, main_file: CHAPTER })
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: 'Main document' }))
    await user.click(await screen.findByRole('option', { name: CHAPTER }))

    await waitFor(() => expect(api.setMainFile).toHaveBeenCalledWith(PROJECT, CHAPTER))
  })

  it('reports a failed main-document change', async () => {
    api.setMainFile.mockRejectedValue(new Error('not a tex file'))
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: 'Main document' }))
    await user.click(await screen.findByRole('option', { name: CHAPTER }))

    expect(await screen.findByText('not a tex file')).toBeInTheDocument()
  })
})

describe('Papyrus git row', () => {
  beforeEach(() => {
    api.gitStatus.mockResolvedValue({
      is_git: true, branch: 'legacy-default', has_remote: true, ahead: 2, behind: 1,
    })
    api.gitPull.mockResolvedValue({ ok: true, output: 'up to date', stashed: false })
    api.gitCommit.mockResolvedValue({ ok: true, output: 'committed' })
    api.gitPush.mockResolvedValue({ ok: true, output: 'pushed' })
  })

  it('shows the branch with its ahead and behind counts', async () => {
    openWorkspace()
    await workspaceReady()

    const label = await screen.findByTitle('Current branch')
    expect(label).toHaveTextContent('legacy-default')
    expect(label).toHaveTextContent('+2')
    expect(label).toHaveTextContent('-1')
  })

  it('hides the pull button when the paper has no remote', async () => {
    api.gitStatus.mockResolvedValue({ is_git: true, branch: 'legacy-default', has_remote: false })
    openWorkspace()
    await workspaceReady()

    await screen.findByRole('button', { name: /Commit & Push/ })
    expect(screen.queryByRole('button', { name: /^Pull$/ })).not.toBeInTheDocument()
  })

  it('pulls, then re-reads the open file', async () => {
    const { user } = openWorkspace()
    await workspaceReady()
    api.readFile.mockResolvedValue({ path: MAIN, content: `${BODY}\n% from upstream` })

    await user.click(await screen.findByRole('button', { name: /^Pull$/ }))

    await waitFor(() => expect(api.gitPull).toHaveBeenCalledWith(PROJECT))
    await waitFor(() =>
      expect(screen.getByLabelText('editor')).toHaveValue(`${BODY}\n% from upstream`))
  })

  it('reports a failed pull', async () => {
    api.gitPull.mockRejectedValue(new Error('rebase conflict'))
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(await screen.findByRole('button', { name: /^Pull$/ }))

    expect(await screen.findByText('rebase conflict')).toBeInTheDocument()
  })

  it('commits with the typed message before pushing', async () => {
    // The button stages every change in the paper, so the message has to be the
    // user's, not a canned one.
    vi.spyOn(window, 'prompt').mockReturnValue('  Tighten the abstract  ')
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(await screen.findByRole('button', { name: /Commit & Push/ }))

    await waitFor(() =>
      expect(api.gitCommit).toHaveBeenCalledWith(PROJECT, 'Tighten the abstract'))
    await waitFor(() => expect(api.gitPush).toHaveBeenCalledWith(PROJECT))
  })

  it('forwards an empty message so the backend picks the default', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('')
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(await screen.findByRole('button', { name: /Commit & Push/ }))

    await waitFor(() => expect(api.gitCommit).toHaveBeenCalledWith(PROJECT, ''))
  })

  it('aborts the push when the prompt is cancelled', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue(null)
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(await screen.findByRole('button', { name: /Commit & Push/ }))

    expect(api.gitCommit).not.toHaveBeenCalled()
    expect(api.gitPush).not.toHaveBeenCalled()
  })

  it('reports a failed push', async () => {
    api.gitPush.mockRejectedValue(new Error('remote rejected'))
    vi.spyOn(window, 'prompt').mockReturnValue('wip')
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(await screen.findByRole('button', { name: /Commit & Push/ }))

    expect(await screen.findByText('remote rejected')).toBeInTheDocument()
  })

  it('does not push, and shows the real write error, when the flush fails', async () => {
    // The flush sentinel is internal plumbing: the save error is what the user
    // needs, so the abort must not overwrite it.
    api.saveFile.mockRejectedValue(new Error('disk full'))
    vi.spyOn(window, 'prompt').mockReturnValue('wip')
    const { user } = openWorkspace()
    await workspaceReady()
    makeDirty(`${BODY}\n% unsaved`)

    await user.click(await screen.findByRole('button', { name: /Commit & Push/ }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(api.gitCommit).not.toHaveBeenCalled()
    expect(await screen.findByText('disk full')).toBeInTheDocument()
    expect(screen.queryByText(/buffer flush failed/)).not.toBeInTheDocument()
  })
})

describe('Papyrus co-author session', () => {
  it('creates a slot the first time the panel is opened', async () => {
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: /Co-author/ }))

    expect(await screen.findByTestId('co-author-panel')).toBeInTheDocument()
    await waitFor(() => expect(chat.createChatSlot).toHaveBeenCalled())
    // The paper's identity is handed to the agent silently, not typed by the user.
    await waitFor(() => expect(chat.chatSlotContext).toHaveBeenCalledWith(
      SLOT,
      expect.stringContaining(PROJECT),
      { source: 'papyrus-co-author', ephemeral: true },
    ))
    // ...and remembered, so reopening the paper reuses it.
    expect(localStorage.getItem(SLOT_KEY_PREFIX + PROJECT)).toBe(SLOT)
  })

  it('reuses the remembered slot instead of minting another', async () => {
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: /Co-author/ }))

    expect(await screen.findByTestId('co-author-panel')).toBeInTheDocument()
    expect(chat.createChatSlot).not.toHaveBeenCalled()
  })

  it('reports a slot that could not be created', async () => {
    chat.createChatSlot.mockRejectedValue(new Error('slot limit reached'))
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: /Co-author/ }))

    expect(await screen.findByText('slot limit reached')).toBeInTheDocument()
  })

  it('does not mint a second slot while the first create is in flight', async () => {
    // A duplicate slot would append onto a second history file for the same paper.
    const gate = deferred<{ key: string; title: string }>()
    chat.createChatSlot.mockReturnValue(gate.promise)
    const { user } = openWorkspace()
    await workspaceReady()
    await user.click(screen.getByRole('button', { name: /Co-author/ }))
    await screen.findByTestId('co-author-panel')

    await user.click(screen.getByRole('button', { name: 'start session' }))

    expect(chat.createChatSlot).toHaveBeenCalledTimes(1)
    gate.resolve({ key: SLOT, title: 'Papyrus: thesis' })
    await waitFor(() => expect(chat.chatSlotContext).toHaveBeenCalled())
  })

  it('keeps the session when the silent context push fails', async () => {
    // The context is a convenience for the agent, not something the user asked
    // for — failing it must not tear down a working session or raise a banner.
    chat.chatSlotContext.mockRejectedValue(new Error('context rejected'))
    const { user } = openWorkspace()
    await workspaceReady()

    await user.click(screen.getByRole('button', { name: /Co-author/ }))

    await waitFor(() => expect(localStorage.getItem(SLOT_KEY_PREFIX + PROJECT)).toBe(SLOT))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('closes the panel again', async () => {
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const { user } = openWorkspace()
    await workspaceReady()
    await user.click(screen.getByRole('button', { name: /Co-author/ }))
    await screen.findByTestId('co-author-panel')

    await user.click(screen.getByRole('button', { name: 'close panel' }))

    await waitFor(() =>
      expect(screen.queryByTestId('co-author-panel')).not.toBeInTheDocument())
  })

  it('flushes the buffer before routing to the full chat page', async () => {
    // Navigating unmounts this page, and the buffer lives only in memory until a
    // save lands.
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const { user } = openWorkspace()
    await workspaceReady()
    await user.click(screen.getByRole('button', { name: /Co-author/ }))
    await screen.findByTestId('co-author-panel')
    makeDirty(`${BODY}\n% keep me`)

    await user.click(screen.getByRole('button', { name: 'open full chat' }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(hoisted.navigate).toHaveBeenCalledWith(`/chat?sid=${encodeURIComponent(SLOT)}`)
  })

  it('stays put when the pre-navigation flush fails', async () => {
    api.saveFile.mockRejectedValue(new Error('disk full'))
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const { user } = openWorkspace()
    await workspaceReady()
    await user.click(screen.getByRole('button', { name: /Co-author/ }))
    await screen.findByTestId('co-author-panel')
    makeDirty(`${BODY}\n% unsavable`)

    await user.click(screen.getByRole('button', { name: 'open full chat' }))

    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    expect(hoisted.navigate).not.toHaveBeenCalled()
  })
})

/**
 * The co-author edits the paper on disk, so the pane the user is watching is stale
 * the moment the agent's turn ends. The refresh must adopt the agent's version
 * without ever saving the browser's copy over it.
 */
describe('Papyrus co-author refresh', () => {
  /** Drive the busy -> idle transition the refresh is keyed on. */
  async function finishAgentTurn() {
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    const handle = openWorkspace(store)
    await workspaceReady()
    store.dispatch(sseSlots([slot({ orchestrating: false })]))
    return handle
  }

  it('adopts the agent version and recompiles when the buffer is clean', async () => {
    api.readFile.mockResolvedValue({ path: MAIN, content: `${BODY}\n% written by the agent` })

    await finishAgentTurn()

    await waitFor(() =>
      expect(screen.getByLabelText('editor')).toHaveValue(`${BODY}\n% written by the agent`))
    await waitFor(() => expect(api.compile).toHaveBeenCalledWith(PROJECT))
    // Never the other direction: the browser copy was the stale one.
    expect(api.saveFile).not.toHaveBeenCalled()
  })

  it('stays quiet when the refresh itself fails', async () => {
    // Blaming the user for the agent's turn would be worse than a stale pane.
    api.readFile.mockRejectedValue(new Error('gateway restarting'))

    await finishAgentTurn()

    // The refresh genuinely ran — it just failed silently.
    await waitFor(() => expect(api.readFile.mock.calls.length).toBeGreaterThan(1))
    expect(api.compile).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('records a conflict rather than clobbering unsaved typing', async () => {
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    openWorkspace(store)
    await workspaceReady()
    makeDirty(`${BODY}\n% mine, unsaved`)

    store.dispatch(sseSlots([slot({ orchestrating: false })]))

    expect(await screen.findByText('Co-author changed this file')).toBeInTheDocument()
    // Neither side lost: disk keeps the agent's version, the editor keeps the user's.
    expect(screen.getByLabelText('editor')).toHaveValue(`${BODY}\n% mine, unsaved`)
    expect(api.saveFile).not.toHaveBeenCalled()
  })

  it('refuses every save while the conflict is unresolved', async () => {
    // The block is at the flush chokepoint, so compile is refused too — otherwise
    // the divergence is only postponed to the next Cmd+S.
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    const { user } = openWorkspace(store)
    await workspaceReady()
    makeDirty(`${BODY}\n% mine, unsaved`)
    store.dispatch(sseSlots([slot({ orchestrating: false })]))
    await screen.findByText('Co-author changed this file')
    api.compile.mockClear()

    await user.click(compileBtn())

    await waitFor(() => expect(api.saveFile).not.toHaveBeenCalled())
    expect(api.compile).not.toHaveBeenCalled()
  })

  it('records a conflict for typing that lands DURING the refresh fetch', async () => {
    // Without this the guard was window-dependent: typing before the fetch was
    // protected, typing during it was not, and the next save overwrote the agent.
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    openWorkspace(store)
    await workspaceReady()
    const reads = api.readFile.mock.calls.length
    const gate = deferred<{ path: string; content: string }>()
    api.readFile.mockReturnValue(gate.promise)

    store.dispatch(sseSlots([slot({ orchestrating: false })]))
    // The buffer is clean here, so the refresh gets past the pre-check and starts
    // the read; the keystroke only arrives while that read is in flight.
    await waitFor(() => expect(api.readFile.mock.calls.length).toBeGreaterThan(reads))
    makeDirty(`${BODY}\n% typed mid-fetch`)
    gate.resolve({ path: MAIN, content: `${BODY}\n% the agent version` })

    expect(await screen.findByText('Co-author changed this file')).toBeInTheDocument()
    expect(screen.getByLabelText('editor')).toHaveValue(`${BODY}\n% typed mid-fetch`)
  })

  it('discards the buffer and loads the agent version when the user confirms', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    const { user } = openWorkspace(store)
    await workspaceReady()
    makeDirty(`${BODY}\n% mine, unsaved`)
    store.dispatch(sseSlots([slot({ orchestrating: false })]))
    await screen.findByText('Co-author changed this file')
    api.readFile.mockResolvedValue({ path: MAIN, content: `${BODY}\n% the agent version` })

    await user.click(screen.getByRole('button', { name: /Discard my edits and reload/ }))

    await waitFor(() =>
      expect(screen.getByLabelText('editor')).toHaveValue(`${BODY}\n% the agent version`))
    await waitFor(() =>
      expect(screen.queryByText('Co-author changed this file')).not.toBeInTheDocument())
  })

  it('keeps the buffer when the discard confirmation is declined', async () => {
    // This is the one action in the app that destroys typing with no undo.
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    const { user } = openWorkspace(store)
    await workspaceReady()
    makeDirty(`${BODY}\n% mine, unsaved`)
    store.dispatch(sseSlots([slot({ orchestrating: false })]))
    await screen.findByText('Co-author changed this file')
    api.readFile.mockClear()

    await user.click(screen.getByRole('button', { name: /Discard my edits and reload/ }))

    expect(screen.getByLabelText('editor')).toHaveValue(`${BODY}\n% mine, unsaved`)
    expect(screen.getByText('Co-author changed this file')).toBeInTheDocument()
    expect(api.readFile).not.toHaveBeenCalled()
  })

  it('restores the conflict guard when the reload fails', async () => {
    // Leaving the guard down after a failed recovery reaches the exact overwrite
    // the guard exists to stop.
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    const { user } = openWorkspace(store)
    await workspaceReady()
    makeDirty(`${BODY}\n% mine, unsaved`)
    store.dispatch(sseSlots([slot({ orchestrating: false })]))
    await screen.findByText('Co-author changed this file')
    api.readFile.mockRejectedValue(new Error('still unreachable'))

    await user.click(screen.getByRole('button', { name: /Discard my edits and reload/ }))

    await waitFor(() => expect(api.readFile).toHaveBeenCalled())
    expect(screen.getByText('Co-author changed this file')).toBeInTheDocument()
    // The buffer is dirty again too, so a later render cannot let typing through.
    expect(screen.getByText(`Editing ${MAIN} — unsaved`)).toBeInTheDocument()
  })
})

describe('Papyrus project errors', () => {
  it('returns to the paper list when the project cannot be opened at all', async () => {
    api.getProject.mockRejectedValue(new Error('paper deleted'))
    const { user } = openWorkspace()

    const banner = await screen.findByRole('alert')
    expect(within(banner).getByText('paper deleted')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByTestId('papyrus-workspace')).not.toBeInTheDocument())

    await user.click(within(banner).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the workspace mounted when a background refresh fails', async () => {
    // The buffer is the only copy of unsaved typing, so a wifi blip must not take
    // the document with it. The refresh is driven by the co-author's turn ending,
    // which invalidates the project query — a real background refetch, not a
    // click the user made.
    localStorage.setItem(SLOT_KEY_PREFIX + PROJECT, SLOT)
    const store = createTestStore()
    store.dispatch(sseSlots([slot({ orchestrating: true })]))
    openWorkspace(store)
    await workspaceReady()
    makeDirty(`${BODY}\n% mid-paragraph`)
    api.getProject.mockRejectedValue(new Error('gateway restarting'))

    store.dispatch(sseSlots([slot({ orchestrating: false })]))

    await waitFor(() => expect(api.getProject.mock.calls.length).toBeGreaterThan(1))
    // The failure IS reported — it just no longer takes the document with it.
    expect(await screen.findByText('gateway restarting')).toBeInTheDocument()
    expect(screen.getByTestId('papyrus-workspace')).toBeInTheDocument()
    expect(screen.getByLabelText('editor')).toHaveValue(`${BODY}\n% mid-paragraph`)
  })
})

describe('Papyrus unload warning', () => {
  it('warns before the browser discards an unsaved buffer', async () => {
    // The in-app exits all flush, but none of them runs on Cmd+R or a tab close.
    openWorkspace()
    await workspaceReady()
    makeDirty(`${BODY}\n% unsaved`)

    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)

    expect(event.defaultPrevented).toBe(true)
  })

  it('does not interfere with an ordinary reload', async () => {
    openWorkspace()
    await workspaceReady()

    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)

    expect(event.defaultPrevented).toBe(false)
  })
})

describe('Papyrus mid-save typing', () => {
  it('keeps the buffer dirty when a keystroke lands during the save', async () => {
    // Clearing `dirty` on the snapshot would declare the newer keystrokes saved,
    // and the caller would then leave and discard them.
    const gate = deferred<{ ok: boolean; path: string }>()
    api.saveFile.mockReturnValue(gate.promise)
    const { user } = openWorkspace()
    await workspaceReady()
    makeDirty(`${BODY}\n% first`)

    await user.click(screen.getByRole('button', { name: /Papers/ }))
    await waitFor(() => expect(api.saveFile).toHaveBeenCalled())
    makeDirty(`${BODY}\n% first and second`)
    gate.resolve({ ok: true, path: MAIN })

    // The workspace must NOT tear down: the second edit never reached disk.
    await waitFor(() =>
      expect(screen.getByText(`Editing ${MAIN} — unsaved`)).toBeInTheDocument())
    expect(screen.getByTestId('papyrus-workspace')).toBeInTheDocument()
  })
})
