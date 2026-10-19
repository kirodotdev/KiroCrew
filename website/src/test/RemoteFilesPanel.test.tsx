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
}))

import RemoteFilesPanel from '../pages/chat/RemoteFilesPanel'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { copyToClipboard } from '../utils/clipboard'
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
})

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
    // Said on its own line, not appended to the path (which truncates).
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent("This file is larger than 256 KB, so only the first 256 KB is shown and it can't be edited here.")
    for (const el of screen.getAllByTitle('big.log')) expect(el).not.toHaveTextContent(/256 KB/)
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

  it.each([
    ['grows past the read limit', { truncated: true, binary: false }, 'This file is now too large to edit here (only the first 256 KB is shown). Copy your edits to keep them.'],
    ['becomes binary', { binary: true }, "This file is now binary and can't be edited here. Copy your edits to keep them."],
  ])('keeps unsaved edits copyable when a refresh finds the file %s', async (_case, change, why) => {
    const read = mockTree()
    const u = userEvent.setup()
    wrap(panel())
    await open(u, /src/, /main\.py/)
    await u.type(await openedEditor(), '# kept')
    read.mockResolvedValue({ ...ROOT, path: 'src/main.py', content: 'x'.repeat(16), binary: false, ...change })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByLabelText('editor')).toBeNull())
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    // Says why the editor went away and what to do with the edits.
    expect(screen.getByText(why)).toBeVisible()
    const copy = vi.mocked(copyToClipboard).mockResolvedValue(false)
    await u.click(await screen.findByRole('button', { name: 'Copy my edits' }))
    expect(copy).toHaveBeenCalledWith('print("hi")\n# kept')
    expect(await screen.findByText(/Could not copy/)).toBeInTheDocument()
    copy.mockResolvedValue(true)
    await u.click(screen.getByRole('button', { name: 'Copy my edits' }))
    expect(await screen.findByRole('button', { name: 'Copied!' })).toBeInTheDocument()
    expect(unloadBlocked()).toBe(true)
  })

})
