import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
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
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import '../i18n'

const clients: QueryClient[] = []

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(qc)
  return render(ui, { wrapper: ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider> })
}

afterEach(() => { clients.splice(0).forEach(client => client.clear()) })

const ROOT = { root: '/workspace/w', truncated: false }

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
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    expect(await screen.findByText('Files on Coder MicroVM')).toBeInTheDocument()
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    expect(read).toHaveBeenCalledWith('chat-1', 'src/main.py')
    expect(await screen.findByLabelText('editor')).toHaveValue('print("hi")\n')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('saves an edit with the opened text as the expected base', async () => {
    mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    await u.type(await screen.findByLabelText('editor'), '# edit')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    expect(write).toHaveBeenCalledWith('chat-1', 'src/main.py', 'print("hi")\n# edit', 'print("hi")\n')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())
  })

  it('keeps text typed while a save is pending dirty and saves it against the new base', async () => {
    mockTree()
    let finish!: (result: { ok: boolean; path: string }) => void
    const pending = new Promise<{ ok: boolean; path: string }>((resolve) => { finish = resolve })
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockReturnValueOnce(pending)
      .mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    await u.type(await screen.findByLabelText('editor'), '# saved')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1))
    await u.type(screen.getByLabelText('editor'), '\n# newer')
    await act(async () => finish({ ok: true, path: 'src/main.py' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled())
    expect(screen.getByLabelText('editor')).toHaveValue('print("hi")\n# saved\n# newer')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    expect(write).toHaveBeenLastCalledWith('chat-1', 'src/main.py', 'print("hi")\n# saved\n# newer', 'print("hi")\n# saved')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())
  })

  it('reports a stale save instead of overwriting, and keeps the edit', async () => {
    mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(
      new ApiError(409, 'changed', JSON.stringify({ code: 'file_changed' })),
    )
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    await u.type(await screen.findByLabelText('editor'), 'x')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/changed in the session environment since you opened it/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('never offers to save a truncated file', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockResolvedValue({ ...ROOT, path: '.', entries: [{ name: 'big.log', dir: false }] })
    vi.spyOn(api, 'chatSlotEnvironmentRead').mockResolvedValue({ ...ROOT, path: 'big.log', content: 'x', binary: false, truncated: true })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /big\.log/ }))
    expect(await screen.findByText(/showing the first 256 KB/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('says the workspace is stopped instead of a generic failure', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'stopped', JSON.stringify({ error: 'stopped', code: 'workspace_stopped' })),
    )
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/workspace/w" />)
    expect(await screen.findByText(/The session environment is stopped/)).toBeInTheDocument()
  })
  it('retains session-owned drafts and the unload guard across tab, route and chat switches', async () => {
    mockTree().mockImplementation(async slot => ({ ...ROOT, path: 'README.md', content: slot, binary: false }))
    const u = userEvent.setup()
    const panel = (slot: string) => <RemoteFilesPanel slot={slot} name="Remote" workdir="/workspace/w" />
    const view = wrap(panel('chat-a'))
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    await u.type(await screen.findByLabelText('editor'), ' draft-a')
    view.rerender(<div>Terminal</div>)
    const exit = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(exit)
    expect(exit.defaultPrevented).toBe(true)
    view.rerender(panel('chat-a'))
    expect(await screen.findByLabelText('editor')).toHaveValue('chat-a draft-a')
    view.rerender(panel('chat-b'))
    expect(screen.queryByLabelText('editor')).toBeNull()
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    expect(await screen.findByLabelText('editor')).toHaveValue('chat-b')
    await u.type(screen.getByLabelText('editor'), ' draft-b')
    view.rerender(panel('chat-a'))
    expect(await screen.findByLabelText('editor')).toHaveValue('chat-a draft-a')
    view.rerender(panel('chat-b'))
    expect(await screen.findByLabelText('editor')).toHaveValue('chat-b draft-b')
  })

  it('keeps the original expected text after a refresh observes an agent edit', async () => {
    const read = mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(
      new ApiError(409, 'changed', JSON.stringify({ code: 'file_changed' })),
    )
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Remote" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    await u.type(await screen.findByLabelText('editor'), '# draft')
    read.mockResolvedValue({ ...ROOT, path: 'README.md', content: '# agent', binary: false })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    expect(screen.getByLabelText('editor')).toHaveValue('print("hi")\n# draft')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    expect(write).toHaveBeenCalledWith('chat-1', 'README.md', 'print("hi")\n# draft', 'print("hi")\n')
    expect(await screen.findByText(/changed in the session environment since you opened it/)).toBeInTheDocument()
  })

  it('applies an in-flight save to its original session after a chat switch', async () => {
    mockTree().mockImplementation(async slot => ({ ...ROOT, path: 'README.md', content: slot, binary: false }))
    let finish!: (result: { ok: boolean; path: string }) => void
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    const u = userEvent.setup()
    const panel = (slot: string) => <RemoteFilesPanel slot={slot} name="Remote" workdir="/workspace/w" />
    const view = wrap(panel('chat-a'))
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    await u.type(await screen.findByLabelText('editor'), ' saved')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    expect(write).toHaveBeenCalledWith('chat-a', 'README.md', 'chat-a saved', 'chat-a')
    view.rerender(panel('chat-b'))
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    await u.type(await screen.findByLabelText('editor'), ' draft-b')
    await act(async () => finish({ ok: true, path: 'README.md' }))
    expect(screen.getByLabelText('editor')).toHaveValue('chat-b draft-b')
    view.rerender(panel('chat-a'))
    expect(await screen.findByLabelText('editor')).toHaveValue('chat-a saved')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('disarms the unload guard only after the last dirty draft is saved', async () => {
    mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'README.md' })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Remote" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    await u.type(await screen.findByLabelText('editor'), ' draft')
    const dirtyExit = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(dirtyExit)
    expect(dirtyExit.defaultPrevented).toBe(true)
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    await u.type(await screen.findByLabelText('editor'), ' second draft')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())
    const remainingExit = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(remainingExit)
    expect(remainingExit.defaultPrevented).toBe(true)
    await u.click(screen.getByRole('treeitem', { name: /README/ }))
    expect(await screen.findByLabelText('editor')).toHaveValue('print("hi")\n draft')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())
    const cleanExit = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(cleanExit)
    expect(cleanExit.defaultPrevented).toBe(false)
  })

  it('does not enable editing or saving undecodable guest content', async () => {
    mockTree().mockResolvedValue({ ...ROOT, path: 'README.md', content: '', binary: true })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Remote" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    expect(await screen.findByText(/binary/i)).toBeInTheDocument()
    expect(screen.queryByLabelText('editor')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('only replaces the stale baseline after an explicitly confirmed discard', async () => {
    const read = mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'README.md' })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Remote" workdir="/workspace/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /README/ }))
    await u.type(await screen.findByLabelText('editor'), '# draft')
    read.mockResolvedValue({ ...ROOT, path: 'README.md', content: '# agent', binary: false })
    await u.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
    await u.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: 'Cancel', exact: true }))
    expect(screen.getByLabelText('editor')).toHaveValue('print("hi")\n# draft')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    await u.click(screen.getByRole('button', { name: 'Discard changes' }))
    const dialog = await screen.findByRole('dialog')
    await u.click(within(dialog).getByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.getByLabelText('editor')).toHaveValue('# agent'))
    await u.type(screen.getByLabelText('editor'), ' merged')
    await u.click(screen.getByRole('button', { name: 'Save', exact: true }))
    expect(write).toHaveBeenCalledWith('chat-1', 'README.md', '# agent merged', '# agent')
  })

})

describe('RemoteGitPanel', () => {
  it('parses NUL porcelain with literal filenames and rename/copy source records', () => {
    expect(parsePorcelain('## main...origin/main\0 M space name.py\0?? é.txt\0R  new\nname.py\0old.py\0 C copied.py\0source.py\0 M arrow -> name\0')).toEqual({
      branch: 'main...origin/main',
      rows: [
        { code: ' M', path: 'space name.py' }, { code: '??', path: 'é.txt' },
        { code: 'R ', path: 'new\nname.py' }, { code: ' C', path: 'copied.py' },
        { code: ' M', path: 'arrow -> name' },
      ],
    })
  })

  it.each(['a.py', 'space name.py', 'é.py', 'arrow -> name.py'])('shows history and requests the literal diff path %s', async (filename) => {
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op, path) => {
      if (op === 'git_status') return { root: '/w', git: true, output: `## main\0 M ${filename}\0` }
      if (op === 'git_log') return { root: '/w', git: true, output: 'abc123\tme\t2 hours ago\tfirst commit' }
      return { root: '/w', git: true, output: `diff --git a/${path} b/${path}\n` }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    expect(await screen.findByText('main')).toBeInTheDocument()
    // Status enables history; file selection then enables the diff query.
    expect(await screen.findByText('first commit', {}, { timeout: 5000 })).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: `M ${filename}` }))
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
    await u.click(await screen.findByRole('button', { name: '?? new.txt' }))
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
    expect(failure).toHaveTextContent('Could not read the session environment')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
  })

  it.each(['git_status', 'git_log', 'git_diff'] as const)('shows %s subprocess failures through ErrorNotice', async (failureOp) => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op) => {
      if (op === failureOp) return { root: '/w', git: false, code: 'git_failed', error_text: 'git is not installed' }
      return { root: '/w', git: true, output: op === 'git_status' ? '## main\0 M a.py\0' : '' }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    if (failureOp === 'git_diff') await u.click(await screen.findByRole('button', { name: /a\.py/ }))
    // Status enables history; selecting a status row enables diff. Either chain may fail.
    const failure = await screen.findByRole('alert', {}, { timeout: 5000 })
    expect(failure).toHaveTextContent('git is not installed')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(screen.queryByText(/not a Git repository/)).not.toBeInTheDocument()
    expect(screen.queryByText(/No changes to show/)).not.toBeInTheDocument()
  })

  it('says when the workdir is not a repository', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockResolvedValue({ root: '/w', git: false })
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    expect(await screen.findByText(/not a Git repository/)).toBeInTheDocument()
  })
})
