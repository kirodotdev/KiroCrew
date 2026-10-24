import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// The real editor is a lazy Shiki/Pierre chunk; a textarea stands in for it so
// the panel's buffer, dirty and save handling are what is under test.
vi.mock('../pierre', () => ({
  PierreEditor: function Editor({ file, onChange, onSave }: { file: { contents: string }; onChange: (s: string) => void; onSave?: () => void }) {
    const [value, setValue] = React.useState(file.contents)
    React.useEffect(() => setValue(file.contents), [file])
    return (
      <textarea
        aria-label="editor"
        value={value}
        onChange={(e) => { setValue(e.target.value); onChange(e.target.value) }}
        onKeyDown={(e) => { if (e.key === 's' && e.metaKey) onSave?.() }}
      />
    )
  },
  PierrePatch: ({ patch }: { patch: string }) => <pre data-testid="patch">{patch}</pre>,
}))

import RemoteFilesPanel from '../pages/chat/RemoteFilesPanel'
import RemoteGitPanel, { parsePorcelain } from '../pages/chat/RemoteGitPanel'
import { relativeTime } from '../components/GitPanel'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { copyToClipboard } from '../utils/clipboard'
import { __resetNavSeamForTests, consumeChatHandoff, installSoftNavigate, setGatewayConnected } from '../utils/errorReport'
import '../i18n'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

const clients: QueryClient[] = []

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(qc)
  return render(ui, { wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> })
}

afterEach(() => { clients.splice(0).forEach(client => client.clear()) })

const ROOT = { root: '/workspace/w', truncated: false }
const CODER = 'Coder MicroVM'
const panel = (slot = 'chat-1', name = 'Remote') => <RemoteFilesPanel slot={slot} name={name} workdir="/workspace/w" />
const save = () => screen.getByRole('button', { name: 'Save' })
const editor = () => screen.getByLabelText('editor')
const openedEditor = () => screen.findByLabelText('editor')
const fileChanged = () => new ApiError(409, 'changed', JSON.stringify({ code: 'file_changed' }))
async function open(u: ReturnType<typeof userEvent.setup>, ...names: RegExp[]) {
  for (const name of names) await u.click(await screen.findByRole('treeitem', { name }))
}
function unloadBlocked() {
  const exit = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(exit)
  return exit.defaultPrevented
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
    profile: 'dev', provider: 'coder', profiles: [{ id: 'dev', name: 'Coder', provider: 'coder', workdir: '/workspace/w' }],
  })
})

/** The placement query resolves, the browser mounts, then its listing query
 * rejects: two awaited requests before a listing alert can render. */
const CHAINED_LISTING_TIMEOUT_MS = 5_000

describe('RemoteFilesPanel', () => {
  function mockTree() {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockImplementation(async (_slot, path) =>
      path === '.'
        ? { ...ROOT, path: '.', entries: [{ name: 'src', dir: true }, { name: 'README.md', dir: false }] }
        : { ...ROOT, path: 'src', entries: [{ name: 'main.py', dir: false }] },
    )
    return vi.spyOn(api, 'chatSlotEnvironmentRead').mockResolvedValue({
      ...ROOT, path: 'src/main.py', content: 'print("hi")\n', binary: false,
    })
  }

  it('lists the remote root, expands folders lazily and opens a file in the editor', async () => {
    const read = mockTree()
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    expect(await screen.findByText('Files on Coder MicroVM')).toBeInTheDocument()
    await open(u, /src/, /main\.py/)
    expect(read).toHaveBeenCalledWith('chat-1', 'src/main.py')
    expect(await openedEditor()).toHaveValue('print("hi")\n')
    expect(save()).toBeDisabled()
  })

  it('labels the terminal button with visible text', async () => {
    mockTree()
    const onOpenTerminal = vi.fn()
    wrap(<RemoteFilesPanel slot="chat-1" name="Remote" workdir="/workspace/w" onOpenTerminal={onOpenTerminal} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Terminal' }))
    expect(onOpenTerminal).toHaveBeenCalledOnce()
  })

  it('saves an edit with the opened text as the expected base', async () => {
    mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), '# edit')
    await u.click(save())
    expect(write).toHaveBeenCalledWith('chat-1', 'src/main.py', 'print("hi")\n# edit', 'print("hi")\n', '/workspace/w')
    await waitFor(() => expect(save()).toBeDisabled())
  })

  it('keeps text typed while a save is pending dirty and saves it against the new base', async () => {
    mockTree()
    let finish!: (result: { ok: boolean; path: string }) => void
    const pending = new Promise<{ ok: boolean; path: string }>((resolve) => { finish = resolve })
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockReturnValueOnce(pending)
      .mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), '# saved')
    await u.click(save())
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
    await u.type(editor(), '\n# newer')
    await act(async () => finish({ ok: true, path: 'src/main.py' }))
    await waitFor(() => expect(save()).toBeEnabled())
    expect(editor()).toHaveValue('print("hi")\n# saved\n# newer')
    await u.click(save())
    expect(write).toHaveBeenLastCalledWith('chat-1', 'src/main.py', 'print("hi")\n# saved\n# newer', 'print("hi")\n# saved', '/workspace/w')
    await waitFor(() => expect(save()).toBeDisabled())
  })

  it('shows a save conflict only on the file whose save failed', async () => {
    mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(fileChanged())
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), 'x')
    await u.click(save())
    expect(await screen.findByText(/changed in the workspace, so it was not saved/)).toBeInTheDocument()
    // Another file: its buffer is not the one in conflict, so no recovery controls act on it.
    await open(u, /README\.md/)
    await u.type(await openedEditor(), 'y')
    expect(screen.queryByText(/changed in the workspace, so it was not saved/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Copy my edits/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /drops my edits/ })).toBeNull()
    // Back on the conflicted file, its recovery is still offered and copies its own edits.
    await open(u, /main\.py/)
    expect(await screen.findByText(/changed in the workspace, so it was not saved/)).toBeInTheDocument()
    const copy = vi.mocked(copyToClipboard).mockResolvedValue(true)
    await u.click(screen.getByRole('button', { name: 'Copy my edits' }))
    expect(copy).toHaveBeenCalledWith('print("hi")\nx')
    expect(write).toHaveBeenCalledTimes(1)
  })

  it('saves a draft against the working folder it was made under, and reports a moved folder', async () => {
    mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite')
      .mockRejectedValue(new ApiError(409, 'moved', JSON.stringify({ code: 'root_changed' })))
    const u = userEvent.setup()
    const view = wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), 'x')
    // Settings points the profile at another folder while the draft is open.
    view.rerender(<RemoteFilesPanel slot="chat-1" name={CODER} workdir="/workspace/other" />)
    // The new folder's read loads first; the draft keeps the folder it was made under.
    await u.click(await screen.findByRole('button', { name: 'Save' }))
    expect(write).toHaveBeenCalledWith('chat-1', 'src/main.py', 'print("hi")\nx', 'print("hi")\n', '/workspace/w')
    expect(await screen.findByText(/working folder changed since you opened this file/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy my edits' })).toBeInTheDocument()
    // One way to drop the edits while the conflict is up, not two.
    expect(screen.getByRole('button', { name: 'Load workspace version (drops my edits)' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Discard my edits' })).toBeNull()
    expect(editor()).toHaveValue('print("hi")\nx')
  })

  it('reads a file again from the new working folder before it can be edited', async () => {
    const read = mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    const view = wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await openedEditor()
    const reads = read.mock.calls.length
    // Opened but not edited, then the profile is pointed at another folder: the
    // old folder's cached read must not seed an edit saved to the new one.
    view.rerender(<RemoteFilesPanel slot="chat-1" name={CODER} workdir="/workspace/other" />)
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(reads))
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    await u.type(await openedEditor(), 'x')
    await u.click(save())
    expect(write).toHaveBeenCalledWith('chat-1', 'src/main.py', 'print("hi")\nx', 'print("hi")\n', '/workspace/other')
  })

  it('keeps the saved text when a refresh that read the old text lands after the save', async () => {
    const read = mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), '# saved')
    let release!: () => void
    const held = new Promise<void>((resolve) => { release = resolve })
    read.mockImplementation(async () => {
      await held
      return { ...ROOT, path: 'src/main.py', content: 'print("hi")\n', binary: false }
    })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await u.click(save())
    await waitFor(() => expect(save()).toBeDisabled())
    await act(async () => { release() })
    // Wait until every read has settled, so a late one had its chance to land.
    const qc = clients[clients.length - 1]
    await waitFor(() => expect(qc.isFetching()).toBe(0))
    await waitFor(() => expect(read).toHaveBeenCalled())
    expect(editor()).toHaveValue('print("hi")\n# saved')
    expect(save()).toBeDisabled()
  })

  it('reports a stale save instead of overwriting, and keeps the edit', async () => {
    mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(fileChanged())
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), 'x')
    await u.click(save())
    expect(await screen.findByText(/changed in the workspace, so it was not saved/)).toBeInTheDocument()
    expect(screen.getByText(/Your edits are still here/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    expect(save()).toBeEnabled()
    // A refused clipboard says so, with no hand-off: the editor holds the only copy.
    vi.mocked(copyToClipboard).mockResolvedValueOnce(false)
    await u.click(screen.getByRole('button', { name: 'Copy my edits' }))
    const failed = await screen.findByText(/Could not copy your edits/)
    expect(within(failed.closest('[role="alert"]') as HTMLElement).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
    // "Copied" waits for the clipboard to confirm.
    const copy = vi.mocked(copyToClipboard).mockResolvedValue(true)
    await u.click(screen.getByRole('button', { name: 'Copy my edits' }))
    expect(copy).toHaveBeenCalledWith('print("hi")\nx')
    expect(await screen.findByRole('button', { name: 'Copied!' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy my edits' })).toBeNull()
    expect(screen.queryByText(/Could not copy your edits/)).toBeNull()
  })

  it('loads the newer workspace copy from the conflict notice, after confirming the discard', async () => {
    const read = mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(fileChanged())
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), 'x')
    await u.click(save())
    await screen.findByText(/changed in the workspace, so it was not saved/)
    read.mockResolvedValue({ ...ROOT, path: 'src/main.py', content: 'print("newer")\n', binary: false })
    const calls = read.mock.calls.length
    await u.click(screen.getByRole('button', { name: 'Load workspace version (drops my edits)' }))
    const dialog = await screen.findByRole('dialog')
    await u.click(within(dialog).getByRole('button', { name: 'Discard my edits' }))
    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(calls))
    await waitFor(() => expect(editor()).toHaveValue('print("newer")\n'))
    expect(screen.queryByText(/changed in the workspace, so it was not saved/)).toBeNull()
  })

  it('never offers to save a truncated file', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockResolvedValue({ ...ROOT, path: '.', entries: [{ name: 'big.log', dir: false }] })
    vi.spyOn(api, 'chatSlotEnvironmentRead').mockResolvedValue({ ...ROOT, path: 'big.log', content: 'x', binary: false, truncated: true })
    const u = userEvent.setup()
    wrap(panel('chat-1', CODER))
    await open(u, /big\.log/)
    expect(await screen.findByText(/showing the first 256 KB/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('says the workspace is stopped instead of a generic failure', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'stopped', JSON.stringify({ error: 'stopped', code: 'workspace_stopped' })),
    )
    wrap(panel('chat-1', CODER))
    expect(await screen.findByText(/The workspace is stopped/)).toBeInTheDocument()
    // The hand-off stays: drafts live in the query cache, so leaving loses nothing.
    expect(screen.getByRole('button', { name: /Ask the agent/ })).toBeInTheDocument()
  })

  it('shows a refused cloud browse as an error with the hand-off, and no refresh, tree, viewer or terminal', async () => {
    const list = vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'cloud', JSON.stringify({ error: 'cloud', code: 'cloud_sandbox' })),
    )
    wrap(<RemoteFilesPanel slot="chat-1" name="Kiro Cloud" workdir="" onOpenTerminal={() => {}} />)
    // A rejected request is an error (errors-use-error-notice); only a recorded
    // cloud placement is the neutral notice. Placement -> browser -> listing chain.
    const notice = await screen.findByRole('alert', {}, { timeout: CHAINED_LISTING_TIMEOUT_MS })
    // Its own sentence says what failed, so it never reads as the neutral notice.
    expect(notice).toHaveTextContent("Couldn't open this chat's files")
    expect(notice).not.toHaveTextContent('This chat runs in a Kiro cloud sandbox')
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
    expect(screen.queryByText(/Select a file to view it/)).toBeNull()
    expect(screen.queryByRole('tree')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Open a terminal in this workspace' })).toBeNull()
    // The panel and the root tree share one listing request.
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('keeps a real listing failure as an error with the hand-off', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(new ApiError(500, 'boom', '{}'))
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    // Placement -> browser -> listing chain.
    const failure = await screen.findByRole('alert', {}, { timeout: CHAINED_LISTING_TIMEOUT_MS })
    expect(failure).toHaveTextContent('Could not read the workspace')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(screen.getByText(/Select a file to view it/)).toBeInTheDocument()
  })
  it('retains session-owned drafts and the unload guard across tab, route and chat switches', async () => {
    mockTree().mockImplementation(async slot => ({ ...ROOT, path: 'README.md', content: slot, binary: false }))
    const u = userEvent.setup()
    const view = wrap(panel('chat-a'))
    await open(u, /README/)
    await u.type(await openedEditor(), ' draft-a')
    view.rerender(<div>Terminal</div>)
    expect(unloadBlocked()).toBe(true)
    view.rerender(panel('chat-a'))
    expect(await openedEditor()).toHaveValue('chat-a draft-a')
    view.rerender(panel('chat-b'))
    expect(screen.queryByLabelText('editor')).toBeNull()
    await open(u, /README/)
    expect(await openedEditor()).toHaveValue('chat-b')
    await u.type(editor(), ' draft-b')
    view.rerender(panel('chat-a'))
    expect(await openedEditor()).toHaveValue('chat-a draft-a')
    view.rerender(panel('chat-b'))
    expect(await openedEditor()).toHaveValue('chat-b draft-b')
  })

  it('keeps the original expected text after a refresh observes an agent edit', async () => {
    const read = mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(fileChanged())
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    await u.type(await openedEditor(), '# draft')
    read.mockResolvedValue({ ...ROOT, path: 'README.md', content: '# agent', binary: false })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(editor()).toHaveValue('print("hi")\n# draft')
    await u.click(save())
    expect(write).toHaveBeenCalledWith('chat-1', 'README.md', 'print("hi")\n# draft', 'print("hi")\n', '/workspace/w')
    expect(await screen.findByText(/changed in the workspace, so it was not saved/)).toBeInTheDocument()
  })

  it('applies an in-flight save to its original session after a chat switch', async () => {
    mockTree().mockImplementation(async slot => ({ ...ROOT, path: 'README.md', content: slot, binary: false }))
    let finish!: (result: { ok: boolean; path: string }) => void
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const u = userEvent.setup()
    const view = wrap(panel('chat-a'))
    await open(u, /README/)
    await u.type(await openedEditor(), ' saved')
    await u.click(save())
    expect(write).toHaveBeenCalledWith('chat-a', 'README.md', 'chat-a saved', 'chat-a', '/workspace/w')
    view.rerender(panel('chat-b'))
    await open(u, /README/)
    await u.type(await openedEditor(), ' draft-b')
    await act(async () => finish({ ok: true, path: 'README.md' }))
    expect(editor()).toHaveValue('chat-b draft-b')
    view.rerender(panel('chat-a'))
    expect(await openedEditor()).toHaveValue('chat-a saved')
    expect(save()).toBeDisabled()
  })

  it('disarms the unload guard only after the last dirty draft is saved', async () => {
    mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'README.md' })
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    await u.type(await openedEditor(), ' draft')
    expect(unloadBlocked()).toBe(true)
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), ' second draft')
    await u.click(save())
    await waitFor(() => expect(save()).toBeDisabled())
    expect(unloadBlocked()).toBe(true)
    await u.click(screen.getByRole('treeitem', { name: /README/ }))
    expect(await openedEditor()).toHaveValue('print("hi")\n draft')
    await u.click(save())
    await waitFor(() => expect(save()).toBeDisabled())
    expect(unloadBlocked()).toBe(false)
  })

  it('does not enable editing or saving undecodable guest content', async () => {
    mockTree().mockResolvedValue({ ...ROOT, path: 'README.md', content: '', binary: true })
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    expect(await screen.findByText(/binary/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('editor')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('only replaces the stale baseline after an explicitly confirmed discard', async () => {
    const read = mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'README.md' })
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /README/)
    await u.type(await openedEditor(), '# draft')
    read.mockResolvedValue({ ...ROOT, path: 'README.md', content: '# agent', binary: false })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    await u.click(screen.getByRole('button', { name: 'Discard my edits' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: 'Cancel', exact: true }))
    expect(editor()).toHaveValue('print("hi")\n# draft')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await u.click(screen.getByRole('button', { name: 'Discard my edits' }))
    const dialog = await screen.findByRole('dialog')
    await u.click(within(dialog).getByRole('button', { name: 'Discard my edits' }))
    await waitFor(() => expect(editor()).toHaveValue('# agent'))
    await u.type(editor(), ' merged')
    await u.click(screen.getByRole('button', { name: 'Save', exact: true }))
    expect(write).toHaveBeenCalledWith('chat-1', 'README.md', '# agent merged', '# agent', '/workspace/w')
  })

  it('keeps unsaved edits reachable when a refresh can no longer read the file', async () => {
    const read = mockTree()
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), '# kept')
    expect(unloadBlocked()).toBe(true)
    // The agent deleted the file: the refresh's read is refused.
    read.mockRejectedValue(new ApiError(400, 'refused', JSON.stringify({ code: 'path_refused' })))
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByLabelText('editor')).toBeNull())
    const copy = vi.mocked(copyToClipboard).mockResolvedValue(true)
    await u.click(await screen.findByRole('button', { name: 'Copy my edits' }))
    expect(copy).toHaveBeenCalledWith('print("hi")\n# kept')
    expect(await screen.findByRole('button', { name: 'Copied!' })).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: 'Discard my edits' }))
    const dialog = await screen.findByRole('dialog')
    await u.click(within(dialog).getByRole('button', { name: 'Discard my edits' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Discard my edits' })).toBeNull())
    expect(unloadBlocked()).toBe(false)
  })

})

describe('recorded cloud placement', () => {
  it.each(['files', 'git'])('shows neutral info and makes no browse requests for %s', async (panel) => {
    vi.mocked(api.chatSlotEnvironment).mockResolvedValue({ profile: 'removed-cloud', provider: 'kiro_cloud', profiles: [] })
    const list = vi.spyOn(api, 'chatSlotEnvironmentList')
    const read = vi.spyOn(api, 'chatSlotEnvironmentRead')
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit')
    wrap(panel === 'files'
      ? <RemoteFilesPanel slot="chat-1" name="Kiro Cloud" workdir="" onOpenTerminal={() => {}} />
      : <RemoteGitPanel slot="chat-1" name="Kiro Cloud" />)
    const notice = await screen.findByRole('status')
    expect(notice).toHaveAttribute('data-tone', 'info')
    expect(notice).toHaveTextContent('This chat runs in a Kiro cloud sandbox')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('tree')).toBeNull()
    expect(screen.queryByRole('button', { name: /terminal/i })).toBeNull()
    expect(list).not.toHaveBeenCalled()
    expect(read).not.toHaveBeenCalled()
    expect(git).not.toHaveBeenCalled()
  })

  it.each(['files', 'git'])('waits for placement before mounting %s browse', async (panel) => {
    let finish!: (v: Awaited<ReturnType<typeof api.chatSlotEnvironment>>) => void
    vi.mocked(api.chatSlotEnvironment).mockReturnValue(new Promise(resolve => { finish = resolve }))
    const list = vi.spyOn(api, 'chatSlotEnvironmentList')
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit')
    wrap(panel === 'files' ? <RemoteFilesPanel slot="chat-1" name="Cloud" workdir="" /> : <RemoteGitPanel slot="chat-1" name="Cloud" />)
    expect(await screen.findByText('Loading…')).toHaveAttribute('aria-busy', 'true')
    expect(list).not.toHaveBeenCalled()
    expect(git).not.toHaveBeenCalled()
    await act(async () => finish({ profile: 'cloud', provider: 'kiro_cloud', profiles: [] }))
    await screen.findByRole('status')
    expect(list).not.toHaveBeenCalled()
    expect(git).not.toHaveBeenCalled()
  })

  it('shows a rejected cloud Git request through ErrorNotice with the hand-off', async () => {
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit').mockRejectedValue(
      new ApiError(409, 'cloud', JSON.stringify({ error: 'cloud', code: 'cloud_sandbox' })),
    )
    wrap(<RemoteGitPanel slot="chat-1" name="Kiro Cloud" />)
    const notice = await screen.findByRole('alert')
    expect(notice).toHaveTextContent('This chat runs in a Kiro cloud sandbox')
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(git).toHaveBeenCalledTimes(1)
    expect(git).toHaveBeenCalledWith('chat-1', 'git_status')
  })

  it('re-reads a stale placement after a cloud refusal and settles on the neutral notice', async () => {
    const placement = vi.mocked(api.chatSlotEnvironment)
      .mockResolvedValueOnce({ profile: 'dev', provider: 'coder', profiles: [{ id: 'dev', name: 'Coder', provider: 'coder', workdir: '/workspace/w' }] })
      .mockResolvedValue({ profile: 'cloud', provider: 'kiro_cloud', profiles: [] })
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'cloud', JSON.stringify({ error: 'cloud', code: 'cloud_sandbox' })),
    )
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder" workdir="/workspace/w" />)
    // Placement (coder) -> browser -> listing refused -> placement re-read
    // (kiro_cloud) -> wrapper swaps to the neutral notice: three awaited requests.
    const notice = await screen.findByRole('status', {}, { timeout: STALE_PLACEMENT_TIMEOUT_MS })
    expect(notice).toHaveAttribute('data-tone', 'info')
    expect(notice).toHaveTextContent('This chat runs in a Kiro cloud sandbox')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(placement).toHaveBeenCalledTimes(2)
  })
})

/** Placement read, browser listing refusal, then the placement re-read the
 * refusal triggers: three awaited requests before the neutral notice renders. */
const STALE_PLACEMENT_TIMEOUT_MS = 5_000


describe('RemoteGitPanel', () => {
  // Each hand-off stages a draft in the composer; nothing is sent by the click.
  const DRAFT_TITLE = 'Opens the chat with this request drafted. Nothing is sent until you send it.'

  it('parses NUL porcelain with literal filenames and rename/copy source records', () => {
    expect(parsePorcelain('## main...origin/main\0 M space name.py\0?? é.txt\0R  new\nname.py\0old.py\0 C copied.py\0source.py\0 M arrow -> name\0')).toEqual({
      branch: 'main', ahead: 0, behind: 0,
      rows: [
        { code: ' M', path: 'space name.py' }, { code: '??', path: 'é.txt' },
        { code: 'R ', path: 'new\nname.py', from: 'old.py' }, { code: ' C', path: 'copied.py', from: 'source.py' },
        { code: ' M', path: 'arrow -> name' },
      ],
    })
  })

  it('shows a NUL rename record with spaces as old → new and diffs it from the old path', async () => {
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op, path) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0R  docs/new name.md\0docs/old name.md\0 C copy of.md\0orig inal.md\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: `diff --git a/${path} b/${path}\n` }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    const renamed = await screen.findByRole('button', { name: 'Renamed from docs/old name.md to docs/new name.md' })
    expect(renamed).toHaveTextContent('docs/old name.md → docs/new name.md')
    const copied = screen.getByRole('button', { name: 'Copied from orig inal.md to copy of.md' })
    expect(copied).toHaveTextContent('orig inal.md → copy of.md')
    await u.click(renamed)
    expect(await screen.findByTestId('patch', {}, { timeout: 5000 })).toHaveTextContent('diff --git a/docs/new name.md')
    expect(git).toHaveBeenCalledWith('chat-1', 'git_diff', 'docs/new name.md', 'docs/old name.md')
    expect(git).not.toHaveBeenCalledWith('chat-1', 'git_diff', 'docs/old name.md')
    // A copy's source still exists, so its diff is the copy's own.
    await u.click(copied)
    await waitFor(() => expect(git).toHaveBeenCalledWith('chat-1', 'git_diff', 'copy of.md'))
  })

  it('shows a mode-only diff as its header, not as no difference', async () => {
    const patch = 'diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n'
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M run.sh\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: patch }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    await u.click(await screen.findByRole('button', { name: 'Modified run.sh' }))
    expect((await screen.findByTestId('patch', {}, { timeout: 5000 })).textContent).toBe(patch)
    expect(screen.queryByText(/no difference from the last commit/i)).not.toBeInTheDocument()
  })

  it('says a clean working tree has no changes since the last commit', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) =>
      op === 'git_status' ? { root: '/w', git: true, output: '## main\0' } : { root: '/w', git: true, output: '' })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    expect(await screen.findByText('No changes since the last commit.')).toBeInTheDocument()
  })

  it('carries no open file or diff into the next chat session', async () => {
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (slot, op, path) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M shared.py\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: `diff --git a/${path} b/${path}\n+from ${slot}\n` }
    })
    const u = userEvent.setup()
    const { rerender } = wrap(<RemoteGitPanel slot="chat-a" name="Remote" />)
    await u.click(await screen.findByRole('button', { name: 'Modified shared.py' }))
    expect(await screen.findByTestId('patch', {}, { timeout: 5000 })).toHaveTextContent('+from chat-a')
    const opened = screen.getByRole('button', { name: 'Modified shared.py' })
    expect(opened).toHaveAttribute('aria-expanded', 'true')
    expect(opened).toHaveAttribute('aria-controls', screen.getByTestId('remote-git-diff').id)
    rerender(<RemoteGitPanel slot="chat-b" name="Remote" />)
    const row = await screen.findByRole('button', { name: 'Modified shared.py' })
    await waitFor(() => expect(git).toHaveBeenCalledWith('chat-b', 'git_status'))
    expect(row).toHaveAttribute('aria-expanded', 'false')
    expect(row).not.toHaveAttribute('aria-controls')
    expect(screen.queryByTestId('remote-git-diff')).not.toBeInTheDocument()
    expect(screen.queryByTestId('patch')).not.toBeInTheDocument()
    expect(git).not.toHaveBeenCalledWith('chat-b', 'git_diff', 'shared.py')
  })

  it.each([
    ['main...origin/main [ahead 1, behind 2]', { branch: 'main', ahead: 1, behind: 2 }],
    ['feat/x...origin/feat/x [behind 3]', { branch: 'feat/x', ahead: 0, behind: 3 }],
    ['main...origin/main [gone]', { branch: 'main', ahead: null, behind: null }],
    ['main', { branch: 'main', ahead: null, behind: null }],
    ['No commits yet on main', { branch: 'main', ahead: null, behind: null }],
    // Verbatim from git 2.47 on an unborn branch whose fetched upstream exists.
    ['No commits yet on trunk...origin/trunk [gone]', { branch: 'trunk', ahead: null, behind: null }],
  ])('reads the branch line %s as the local panel shows it', (line, expected) => {
    expect(parsePorcelain(`## ${line}\0`)).toEqual({ ...expected, rows: [] })
  })

  it('shows the branch with an ahead/behind pill and one status letter per row', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main...origin/main [ahead 1]\0?? new.txt\0 M a.py\0' }
      return { root: '/w', git: true, output: '' }
    })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    expect(await screen.findByText('main')).toBeInTheDocument()
    expect(screen.getByText(/↑1/)).toHaveTextContent('↑1 ↓0')
    expect(screen.queryByText(/origin|ahead|\?\?/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Untracked new.txt' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Modified a.py' })).toBeInTheDocument()
    expect(screen.queryByText('No upstream')).not.toBeInTheDocument()
  })

  it.each([
    ['a branch with no upstream', '## main\0 M a.py\0', 'main'],
    ['an upstream that is gone', '## main...origin/main [gone]\0 M a.py\0', 'main'],
    ['a branch with no commits yet', '## No commits yet on main\0?? a.py\0', 'main'],
    ['a branch with no commits yet and an upstream', '## No commits yet on trunk...origin/trunk [gone]\0?? a.py\0', 'trunk'],
  ])('says there is nothing to compare for %s', async (_label, output, branch) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) =>
      op === 'git_status' ? { root: '/w', git: true, output } : { root: '/w', git: true, output: '' })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    const pill = await screen.findByText('No upstream')
    expect(screen.getByTitle('Current branch (read-only)').textContent).toBe(branch)
    expect(pill).toHaveAttribute('title', 'This branch has no upstream branch, so there is nothing to compare it with.')
    expect(pill.closest('button')).toBeNull()
    expect(pill.className).not.toMatch(/\b(border|rounded|bg-)/)
    expect(screen.queryByText(/↑|↓/)).not.toBeInTheDocument()
  })

  it.each([
    ['nothing to push or pull', '## main...origin/main\0', '↑0 ↓0', '0 commits not pushed, 0 commits not pulled (read-only)'],
    ['commits to push and pull', '## main...origin/main [ahead 2, behind 1]\0', '↑2 ↓1', '2 commits not pushed, 1 commit not pulled (read-only)'],
  ])('shows the commit counts for %s as read-only text, not a control', async (_label, output, shown, title) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) =>
      op === 'git_status' ? { root: '/w', git: true, output } : { root: '/w', git: true, output: '' })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    const counts = await screen.findByTitle(title)
    expect(counts).toHaveTextContent(shown)
    expect(counts.closest('button')).toBeNull()
    expect(counts.className).not.toMatch(/\b(border|rounded|bg-)/)
    expect(counts).toHaveClass('text-muted')
  })

  it('labels the branch, the ahead/behind counts and every status letter', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') {
        return {
          root: '/w', git: true,
          output: '## feat/x...origin/feat/x [ahead 2, behind 1]\0 M m.py\0A  a.py\0R  r.py\0old.py\0C  c.py\0src.py\0 D d.py\0?? u.py\0UU x.py\0',
        }
      }
      return { root: '/w', git: true, output: '' }
    })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    const branch = await screen.findByText('feat/x')
    expect(branch).toHaveAttribute('title', 'Current branch (read-only)')
    expect(branch.closest('button')).toBeNull()
    const label = '2 commits not pushed, 1 commit not pulled'
    expect(screen.getByText(label)).toHaveClass('sr-only')
    expect(screen.getByTitle(`${label} (read-only)`)).toHaveTextContent('↑2 ↓1')
    for (const [letter, title, file, colour, name] of [
      ['M', 'Modified', 'm.py', 'text-warn', 'Modified m.py'], ['A', 'Added', 'a.py', 'text-ok', 'Added a.py'],
      ['R', 'Renamed', 'r.py', 'text-muted', 'Renamed from old.py to r.py'],
      ['C', 'Copied', 'c.py', 'text-muted', 'Copied from src.py to c.py'], ['D', 'Deleted', 'd.py', 'text-danger', 'Deleted d.py'],
      ['?', 'Untracked', 'u.py', 'text-info', 'Untracked u.py'], ['U', 'Other change', 'x.py', 'text-muted', 'Other change x.py'],
    ]) {
      const row = screen.getByRole('button', { name })
      expect(row).toHaveTextContent(file)
      expect(within(row).getByTitle(title)).toHaveTextContent(letter)
      expect(within(row).getByTitle(title)).toHaveClass(colour)
    }
  })

  it('reads newline status from a guest worker without NUL status support', () => {
    expect(parsePorcelain('## main...origin/main [behind 2]\n M space name.py\n?? new.txt\nR  old.py -> new.py\n')).toEqual({
      branch: 'main', ahead: 0, behind: 2,
      rows: [
        { code: ' M', path: 'space name.py' }, { code: '??', path: 'new.txt' },
        { code: 'R ', path: 'new.py', from: 'old.py' },
      ],
    })
  })

  it('says when a diff is too large and shows only its mode lines', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M big.txt\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      // An over-cap file: the gateway sends its headers and no hunks.
      return { root: '/w', git: true, output: 'diff --git a/big.txt b/big.txt\nold mode 100644\nnew mode 100755\n', truncated: true }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    await u.click(await screen.findByRole('button', { name: 'Modified big.txt' }))
    const note = await screen.findByRole('note', {}, { timeout: 5000 })
    expect(note).toHaveTextContent('This diff is too large to show. Only its mode or rename lines appear, if any.')
    expect(note).not.toHaveTextContent('first part')
  })

  it('shows no cut-off notice for a complete diff', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M a.txt\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: 'diff --git a/a.txt b/a.txt\n+all\n', truncated: false }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    await u.click(await screen.findByRole('button', { name: 'Modified a.txt' }))
    expect(await screen.findByTestId('patch', {}, { timeout: 5000 })).toHaveTextContent('+all')
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it.each(['a.py', 'space name.py', 'é.py', 'arrow -> name.py'])('shows history and requests the literal diff path %s', async (filename) => {
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op, path) => {
      if (op === 'git_status') return { root: '/w', git: true, output: `## main\0 M ${filename}\0` }
      if (op === 'git_log') return { root: '/w', git: true, output: 'abc123\tme\t1760850000\tfirst commit' }
      return { root: '/w', git: true, output: `diff --git a/${path} b/${path}\n` }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    expect(await screen.findByText('main')).toBeInTheDocument()
    // Status enables history; file selection then enables the diff query.
    expect(await screen.findByText('first commit', {}, { timeout: 5000 })).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: `Modified ${filename}` }))
    expect(await screen.findByTestId('patch', {}, { timeout: 5000 })).toHaveTextContent(`diff --git a/${filename}`)
    expect(git).toHaveBeenCalledWith('chat-1', 'git_diff', filename)
  })

  it('renders the addition patch for an untracked row', async () => {
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0?? new.txt\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: 'diff --git a/new.txt b/new.txt\n+entirely new content\n' }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    await u.click(await screen.findByRole('button', { name: 'Untracked new.txt' }))
    // The status row selection enables the addition-diff query.
    expect(await screen.findByTestId('patch', {}, { timeout: 5000 })).toHaveTextContent('+entirely new content')
    expect(git).toHaveBeenCalledWith('chat-1', 'git_diff', 'new.txt')
    expect(screen.queryByText('No changes in this file.')).not.toBeInTheDocument()
  })

  it('surfaces a failed history query with an agent hand-off', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_log') throw new Error('history unavailable')
      return { root: '/w', git: true, output: '## main\0' }
    })
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    // History loads after the status response enables its query.
    const failure = await screen.findByRole('alert', {}, { timeout: 5000 })
    expect(within(failure).getByText('History could not be read.').tagName).toBe('STRONG')
    expect(failure).toHaveTextContent('Could not read the workspace')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
  })

  it.each([
    ['git_status', 'The changes could not be read.'],
    ['git_log', 'History could not be read.'],
    ['git_diff', "This file's diff could not be read."],
  ] as const)('leads a %s git failure with a product sentence and keeps git text as the detail', async (failureOp, lead) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === failureOp) return { root: '/w', git: false, code: 'git_failed', error_text: 'fatal: unable to read tree 9c1e2d4' }
      return { root: '/w', git: true, output: op === 'git_status' ? '## main\0 M a.py\0' : '' }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    if (failureOp === 'git_diff') await u.click(await screen.findByRole('button', { name: /a\.py/ }))
    const failure = await screen.findByRole('alert', {}, { timeout: 5000 })
    const title = within(failure).getByText(lead)
    expect(title.tagName).toBe('STRONG')
    const detail = within(failure).getByText('fatal: unable to read tree 9c1e2d4')
    expect(detail).toHaveClass('font-mono')
    expect(title.compareDocumentPosition(detail) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
  })

  it('keeps the stopped-environment sentence as the whole notice', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockRejectedValue(
      new ApiError(409, 'stopped', JSON.stringify({ error: 'stopped', code: 'workspace_stopped' })),
    )
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    const failure = await screen.findByRole('alert', {}, { timeout: 5000 })
    expect(failure).toHaveTextContent('The workspace is stopped. Send a message to start it, then refresh.')
    expect(failure.querySelector('strong')).toBeNull()
  })

  it('says a listed file with an empty diff is listed as changed, not unchanged', async () => {
    let clean = false
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: clean ? '## main\0' : '## main\0 M src/config.json\0' }
      return { root: '/w', git: true, output: '' }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    await u.click(await screen.findByRole('button', { name: 'Modified src/config.json' }))
    expect(await screen.findByText(
      'Git listed this as changed, but there is no difference from the last commit now. It may have changed back since the list was read; refresh to update.',
      {}, { timeout: 5000 },
    )).toBeInTheDocument()
    expect(screen.queryByText('No changes in this file.')).not.toBeInTheDocument()
    // Refreshed clean: the selection is no longer a listed change.
    clean = true
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('No changes in this file.', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(screen.queryByText(/Git listed this as changed/)).not.toBeInTheDocument()
  })

  it('says a cut-off diff with an empty shown part was cut off, not unchanged', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M big.txt\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: '', truncated: true }
    })
    setGatewayConnected(true)
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    await u.click(await screen.findByRole('button', { name: 'Modified big.txt' }))
    const note = await screen.findByRole('note', {}, { timeout: 5000 })
    expect(note).toHaveTextContent('This diff is too large to show.')
    expect(within(note).getByRole('button', { name: 'Draft a request to the agent for the full diff' })).toBeVisible()
    expect(screen.queryByText(/Git listed this as changed/)).not.toBeInTheDocument()
    expect(screen.queryByText('No changes in this file.')).not.toBeInTheDocument()
    expect(screen.queryByTestId('patch')).not.toBeInTheDocument()
  })

  it('offers to ask the agent for the full diff of a cut-off file', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M data/big file.csv\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: 'diff --git a/x b/x\n', truncated: true }
    })
    const navigate = vi.fn()
    installSoftNavigate(navigate)
    setGatewayConnected(true)
    try {
      const u = userEvent.setup()
      wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
      await u.click(await screen.findByRole('button', { name: 'Modified data/big file.csv' }))
      const note = await screen.findByRole('note', {}, { timeout: 5000 })
      const ask = within(note).getByRole('button', { name: 'Draft a request to the agent for the full diff' })
      expect(ask).toHaveAttribute('title', DRAFT_TITLE)
      expect(ask).toBeVisible()
      expect(ask.textContent).toMatch(/^Draft\b/)
      await u.click(ask)
      expect(consumeChatHandoff()).toBe(
        'Show me the full diff of data/big file.csv against the last commit in the workspace Coder MicroVM. It was too large for the Git panel to show.',
      )
      expect(navigate).toHaveBeenCalledWith('/chat')
    } finally {
      __resetNavSeamForTests()
    }
  })

  it('hides the full-diff hand-off while the gateway is unreachable', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M big.txt\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output: 'diff --git a/x b/x\n', truncated: true }
    })
    setGatewayConnected(false)
    try {
      const u = userEvent.setup()
      wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
      await u.click(await screen.findByRole('button', { name: 'Modified big.txt' }))
      const note = await screen.findByRole('note', {}, { timeout: 5000 })
      expect(within(note).queryByRole('button')).toBeNull()
    } finally {
      setGatewayConnected(true)
    }
  })

  it.each([
    ['a record cut mid-path', '## main\0 M a.py\0?? notes/half-writ', ['a.py']],
    ['a rename cut before its source', '## main\0 M a.py\0R  new.py\0old/pa', ['a.py', 'new.py']],
    ['a cut that lands on a separator', '## main\0 M a.py\0 M b.py\0', ['a.py', 'b.py']],
    ['newline records from an older worker', '## main\n M a.py\n?? half', ['a.py']],
  ])('drops the partial final record of cut-off status output: %s', (_label, output, paths) => {
    expect(parsePorcelain(output, true).rows.map((row) => row.path)).toEqual(paths)
  })

  it('keeps the final record of complete status output', () => {
    expect(parsePorcelain('## main\0 M a.py\0?? b.py', false).rows.map((row) => row.path)).toEqual(['a.py', 'b.py'])
  })

  it('says a cut-off change list is incomplete and its count a lower bound', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M a.py\0?? notes/half-writ', truncated: true }
      return { root: '/w', git: true, output: '' }
    })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    expect(await screen.findByRole('button', { name: 'Modified a.py' })).toBeInTheDocument()
    expect(screen.queryByText(/half-writ/)).not.toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('Some changes are not shown.')
    expect(screen.getByText('1 or more')).toBeInTheDocument()
  })

  it('shows no incomplete-list notice for complete status output', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M a.py\0', truncated: false }
      return { root: '/w', git: true, output: '' }
    })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    expect(await screen.findByRole('button', { name: 'Modified a.py' })).toBeInTheDocument()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it.each([
    ['git_status', 'git is not installed', 'The changes could not be read.'],
    ['git_log', 'git is not installed', 'History could not be read.'],
    ['git_diff', 'git is not installed', "This file's diff could not be read."],
    ['git_status', '', 'The changes could not be read.'],
    ['git_log', '', 'History could not be read.'],
    ['git_diff', '', "This file's diff could not be read."],
  ] as const)('shows %s subprocess failures (%j) through ErrorNotice', async (failureOp, errorText, lead) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === failureOp) return { root: '/w', git: false, code: 'git_failed', error_text: errorText }
      return { root: '/w', git: true, output: op === 'git_status' ? '## main\0 M a.py\0' : '' }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    if (failureOp === 'git_diff') await u.click(await screen.findByRole('button', { name: /a\.py/ }))
    // Status enables history; selecting a status row enables diff. Either chain may fail.
    const failure = await screen.findByRole('alert', {}, { timeout: 5000 })
    expect(failure).toHaveTextContent(lead)
    if (errorText) expect(failure).toHaveTextContent(errorText)
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(screen.queryByText(/not a Git repository/)).not.toBeInTheDocument()
    expect(screen.queryByText('No changes since the last commit.')).not.toBeInTheDocument()
    expect(screen.queryByText('No changes in this file.')).not.toBeInTheDocument()
    expect(screen.queryByText(/Git listed this as changed/)).not.toBeInTheDocument()
    expect(screen.queryByTestId('patch')).not.toBeInTheDocument()
  })

  it('says when the workdir is not a repository', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockResolvedValue({ root: '/w', git: false })
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    expect(await screen.findByText(/not a Git repository/)).toBeInTheDocument()
  })

  function header(label: string) {
    return screen.getAllByTestId('panel-section-header').find((el) => el.textContent?.startsWith(label))!
  }

  it.each([
    [true, 'Changes1 or more'],
    [false, 'Changes2'],
  ])('marks the change count as a lower bound only when the list was cut (truncated %s)', async (truncated, text) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M a.py\0?? half', truncated }
      return { root: '/w', git: true, output: '' }
    })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    expect(await screen.findByRole('button', { name: 'Modified a.py' })).toBeInTheDocument()
    expect(header('Changes').textContent).toBe(text)
  })

  it('shows no History count when the log could not be read, and the count once it was', async () => {
    let failLog = true
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_log') {
        return failLog
          ? { root: '/w', git: false, code: 'git_failed', error_text: 'fatal: bad object HEAD' }
          : { root: '/w', git: true, output: 'abc\tme\t1760850000\tfirst\ndef\tme\t1760850000\tsecond' }
      }
      return { root: '/w', git: true, output: '## main\0' }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    await screen.findByRole('alert', {}, { timeout: 5000 })
    expect(header('History').textContent).toBe('History')
    failLog = false
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('second', {}, { timeout: 5000 })).toBeInTheDocument()
    expect(header('History').textContent).toBe('History2')
  })

  it('spells out the status of the open row only', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M a.py\0?? b.py\0' }
      return { root: '/w', git: true, output: 'diff --git a/a.py b/a.py\n+x\n' }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    const modified = await screen.findByRole('button', { name: 'Modified a.py' })
    const untracked = screen.getByRole('button', { name: 'Untracked b.py' })
    const visible = (row: HTMLElement) => Array.from(row.querySelectorAll('span[aria-hidden="true"]')).map((el) => el.textContent)
    const chevron = (row: HTMLElement) => row.querySelector('svg.lucide-chevron-right') ? 'closed' : row.querySelector('svg.lucide-chevron-down') ? 'open' : 'none'
    expect(visible(modified)).toEqual(['M'])
    expect([chevron(modified), chevron(untracked)]).toEqual(['closed', 'closed'])
    await u.click(modified)
    expect(visible(modified)).toEqual(['M', 'Modified'])
    expect(visible(untracked)).toEqual(['?'])
    expect([chevron(modified), chevron(untracked)]).toEqual(['open', 'closed'])
    expect(modified.lastElementChild?.tagName.toLowerCase()).toBe('svg')
    // The accessible name is unchanged by the visible label.
    expect(screen.getByRole('button', { name: 'Modified a.py' })).toBe(modified)
  })

  it.each([
    [
      'cut-off change list',
      { root: '/w', git: true, output: '## main\0 M a.py\0', truncated: true },
      /Some changes are not shown/,
      'Draft a request to the agent for the full list',
      'Show me the full list of changes in the workspace Coder MicroVM. The Git panel could only read part of it.',
    ],
    [
      'non-repository workdir',
      { root: '/w', git: false },
      /not a Git repository/,
      'Draft a request to the agent to clone or create a repository',
      'The working directory of the workspace Coder MicroVM is not a Git repository. Help me clone a repository into it or create a new one.',
    ],
  ])('hands the %s to the agent, and only while the gateway is reachable', async (_label, reply, shown, button, prompt) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) =>
      op === 'git_status' ? reply : { root: '/w', git: true, output: '' })
    const navigate = vi.fn()
    installSoftNavigate(navigate)
    setGatewayConnected(true)
    try {
      const u = userEvent.setup()
      const { unmount } = wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
      const ask = await screen.findByRole('button', { name: button }, { timeout: 5000 })
      expect(ask).toHaveAttribute('title', DRAFT_TITLE)
      expect(ask.textContent).toMatch(/^Draft\b/)
      await u.click(ask)
      expect(consumeChatHandoff()).toBe(prompt)
      expect(navigate).toHaveBeenCalledWith('/chat')
      unmount()
      setGatewayConnected(false)
      wrap(<RemoteGitPanel slot="chat-2" name="Coder MicroVM" />)
      expect(await screen.findByText(shown, {}, { timeout: 5000 })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: button })).not.toBeInTheDocument()
    } finally {
      setGatewayConnected(true)
      __resetNavSeamForTests()
    }
  })

  it('reads an earlier worker\'s code-less not-a-repository reply as a non-repository', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockResolvedValue({
      root: '/w', git: false, error_text: 'fatal: not a git repository (or any of the parent directories): .git',
    })
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    expect(await screen.findByText('The working directory is not a Git repository.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it.each([
    ['an earlier worker, git missing', { git: false, error_text: 'git is not installed' }],
    ['an earlier worker, another git failure', { git: false, error_text: "fatal: detected dubious ownership in repository at '/w'" }],
    ['git_failed with not-a-repository text', { git: false, code: 'git_failed', error_text: 'fatal: not a git repository: /w/.git' }],
    ['git_failed with text', { git: false, code: 'git_failed', error_text: 'fatal: unable to read tree 9c1e2d4' }],
    ['git_failed without text', { git: false, code: 'git_failed', error_text: '' }],
  ] as const)('shows %s as a failure, not a non-repository', async (_label, reply) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockResolvedValue({ root: '/w', ...reply })
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    const failure = await screen.findByRole('alert', {}, { timeout: 5000 })
    expect(failure).toHaveTextContent('The changes could not be read.')
    expect(screen.queryByText('The working directory is not a Git repository.')).not.toBeInTheDocument()
  })

  it.each([
    ['a patch', 'diff --git a/x b/x\n+x\n', false, () => screen.getByTestId('patch')],
    ['a cut-off notice', 'diff --git a/x b/x\n+x\n', true, () => screen.getByRole('note')],
    ['the empty-diff notice', '', false, () => screen.getByText(/Git listed this as changed/)],
  ])('opens %s directly under the selected row, before the next row', async (_label, output, truncated, shown) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\0 M a.py\0 M b.py\0 M c.py\0' }
      if (op === 'git_log') return { root: '/w', git: true, output: '' }
      return { root: '/w', git: true, output, truncated }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    const rowA = await screen.findByRole('button', { name: 'Modified a.py' })
    const rowB = screen.getByRole('button', { name: 'Modified b.py' })
    const rowC = screen.getByRole('button', { name: 'Modified c.py' })
    const between = (el: Element, before: Element, after: Element) =>
      !!(before.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)
      && !!(el.compareDocumentPosition(after) & Node.DOCUMENT_POSITION_FOLLOWING)
    await u.click(rowA)
    await waitFor(() => shown(), { timeout: 5000 })
    expect(screen.getAllByTestId('remote-git-diff')).toHaveLength(1)
    expect(screen.getByTestId('remote-git-diff')).toContainElement(shown() as HTMLElement)
    expect(between(screen.getByTestId('remote-git-diff'), rowA, rowB)).toBe(true)
    await u.click(rowB)
    await waitFor(() => shown(), { timeout: 5000 })
    expect(between(screen.getByTestId('remote-git-diff'), rowB, rowC)).toBe(true)
    expect(screen.getByTestId('remote-git-diff')).toContainElement(shown() as HTMLElement)
  })

  it.each([
    ['out-of-range seconds', '9999999999999'],
    ['more than 16 digits', '99999999999999999999'],
    ['numeric overflow', '9'.repeat(400)],
  ])('shows an unrepresentable commit date verbatim in History: %s', async (_label, when) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) =>
      op === 'git_log'
        ? { root: '/w', git: true, output: `abc\tme\t${when}\tfirst` }
        : { root: '/w', git: true, output: '## main\0' })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    // Status enables the history query; wait for its commit row to render.
    const subject = await screen.findByText('first', {}, { timeout: 5000 })
    expect(subject.nextElementSibling!.textContent).toBe(when)
    expect(header('History').textContent).toBe('History1')
    expect(screen.getByTestId('remote-git-panel')).toBeInTheDocument()
  })

  it('formats a commit date sent as unix seconds as the local Git panel does', async () => {
    const now = Date.UTC(2026, 9, 19, 12, 0, 0)
    vi.spyOn(Date, 'now').mockReturnValue(now)
    const twoHoursAgo = now / 1000 - 2 * 3600
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) =>
      op === 'git_log'
        ? { root: '/w', git: true, output: `abc\tme\t${twoHoursAgo}\tfirst` }
        : { root: '/w', git: true, output: '## main\0' })
    wrap(<RemoteGitPanel slot="chat-1" name="Remote" />)
    const subject = await screen.findByText('first', {}, { timeout: 5000 })
    const when = subject.nextElementSibling!
    expect(when.textContent).toBe(relativeTime(new Date(twoHoursAgo * 1000).toISOString()))
    expect(when.textContent).toBe('2h')
    expect(screen.queryByText(String(twoHoursAgo))).not.toBeInTheDocument()
  })
})
