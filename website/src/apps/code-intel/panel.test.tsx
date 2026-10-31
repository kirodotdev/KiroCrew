/**
 * Code-intel's Language servers tab, as the bundle the app ships: pool status with
 * restart / stop, the languages found in the project, workspace-symbol search, the
 * lazy outline tree, and the app's shortcuts. It reaches the dashboard only through
 * `@kirocrew/app-sdk`, so that is what the test stands in for.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const sdk = vi.hoisted(() => ({
  post: vi.fn<(path: string, body?: unknown) => Promise<unknown>>(),
  get: vi.fn<(path: string) => Promise<unknown>>(),
  active: true,
  host: null as null | { projectDir?: string; openFile: (path: string, opts?: { line?: number }) => void },
}))
vi.mock('@kirocrew/app-sdk', () => ({
  useAppApi: () => ({ post: sdk.post, get: sdk.get }),
  useAppInfo: () => ({ name: 'code-intel', active: sdk.active }),
  useSidePanelHost: () => sdk.host,
  useLanguageGeneration: () => 0,
  activeLocale: () => 'en',
}))

import CodeIntelPanel, { rootLabel } from './panel'
import { createCodeIntelApi, errorText } from './api'
import type { LspDetectResponse, LspServersResponse } from './api'

function wrap(children: ReactNode, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

const READY: LspServersResponse = {
  maxServers: 4,
  idleShutdownSecs: 900,
  servers: [{
    server: 'python', root: '/p/proj', state: 'ready', error: '', stderr: '', pid: 42,
    command: '/usr/bin/pyright-langserver', uptimeSecs: 30, ageSecs: 31, idleSecs: 5,
    shutdownInSecs: 895, retryInSecs: null, notices: [], folders: ['/p/common'],
  }],
}
const FAILED: LspServersResponse = {
  maxServers: 4,
  idleShutdownSecs: 900,
  servers: [{
    server: 'java', root: '/p/svc', state: 'failed', error: 'language server exited',
    stderr: 'Could not resolve module: org.eclipse.core.filesystem', pid: null, command: '',
    uptimeSecs: null, ageSecs: 3, idleSecs: null, shutdownInSecs: null, retryInSecs: 57, folders: [],
    notices: [{ level: 'warning', message: 'Run bemol in /p to write them' }],
  }],
}
const DETECT: LspDetectResponse = {
  directory: '/p',
  truncated: false,
  projects: [
    { server: 'python', root: '/p/proj', files: 2, available: true, command: '/usr/bin/pyright-langserver', install: [], refused: [], state: 'ready' },
    { server: 'rust', root: '/p/worktrees/a/crate', files: 1, available: true, command: '/usr/bin/rust-analyzer', install: [], refused: [], state: 'stopped' },
    { server: 'go', root: '/p/tool', files: 3, available: false, command: '', install: ['gopls'], refused: [], state: 'stopped' },
  ],
}

type Op = { op: string } & Record<string, unknown>
const POOL = '/api/apps/code-intel/pool'
/** Every pool call so far, by op. */
const calls = (op: string) => sdk.post.mock.calls.filter(([p, b]) => p === POOL && (b as Op).op === op)

/** Answer the pool by op; anything unlisted resolves `{ ok: true }`. */
function stubPool(servers: LspServersResponse, extra: Record<string, unknown | ((b: Op) => unknown)> = {}) {
  sdk.post.mockImplementation(async (path, body) => {
    const b = body as Op
    if (path !== POOL) throw new Error(`unexpected ${path}`)
    if (b.op in extra) { const v = extra[b.op]; return typeof v === 'function' ? (v as (b: Op) => unknown)(b) : v }
    if (b.op === 'servers') return servers
    if (b.op === 'detect') return DETECT
    return { ok: true }
  })
  sdk.get.mockImplementation(async (path) => {
    const dir = decodeURIComponent(path.replace('/api/browse-files?path=', ''))
    return {
      path: dir, parent: '/',
      dirs: dir === '/p' ? [{ name: 'proj', path: '/p/proj', mtime: 0 }, { name: '.git', path: '/p/.git', mtime: 0 }] : [],
      files: dir === '/p/proj' ? [{ name: 'lib.py', path: '/p/proj/lib.py', mtime: 0 }, { name: 'notes.txt', path: '/p/proj/notes.txt', mtime: 0 }] : [],
    }
  })
}

beforeEach(() => {
  sdk.post.mockReset(); sdk.get.mockReset()
  sdk.active = true
  sdk.host = { projectDir: '/p', openFile: vi.fn() }
})
afterEach(() => { vi.restoreAllMocks() })

describe('CodeIntelPanel', () => {
  it('lists running servers and restarts or stops one', async () => {
    stubPool(READY)
    render(wrap(<CodeIntelPanel />))
    expect(await screen.findByText('Ready')).toBeTruthy()
    expect(screen.getByText('Up 30s · stops in 15 min if idle · pid 42')).toBeTruthy()
    // Projects opened beside the root because it references them.
    expect(screen.getByText('Also opens: common')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Restart the python server for /p/proj'))
    await waitFor(() => expect(calls('restart')[0]?.[1]).toEqual({ op: 'restart', server: 'python', root: '/p/proj', lang: 'en' }))
    fireEvent.click(screen.getByLabelText('Stop the python server for /p/proj'))
    await waitFor(() => expect(calls('stop')[0]?.[1]).toEqual({ op: 'stop', server: 'python', root: '/p/proj', lang: 'en' }))
  })

  it('orders the tab: search, outline, running servers, the rest, then the shortcuts', async () => {
    stubPool({ ...READY, servers: [READY.servers[0], FAILED.servers[0]] })
    render(wrap(<CodeIntelPanel />))
    await screen.findByText('Ready')
    const titles = ['Symbol search', 'Outline', 'Running language servers', 'Other language servers', 'Shortcuts']
    const at = titles.map(t => screen.getByText(t))
    for (let i = 1; i < at.length; i++) {
      expect(at[i - 1].compareDocumentPosition(at[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    }
    // The failed java start sits under the rest, after the running python server.
    expect(at[3].compareDocumentPosition(screen.getByText('Failed')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText('Ready').compareDocumentPosition(at[3]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The chords live in Settings, which shows the ones in force after a rebind.
    expect(screen.getByText(/Settings → Shortcuts → Editor shortcuts/)).toBeTruthy()
  })

  it('shows a failed start with its error and stderr', async () => {
    stubPool(FAILED)
    render(wrap(<CodeIntelPanel />))
    expect(await screen.findByText('Failed')).toBeTruthy()
    expect(screen.getByText('language server exited')).toBeTruthy()
    expect(screen.getByText(/Could not resolve module/)).toBeTruthy()
    expect(screen.getByText(/Retry allowed in 57s/)).toBeTruthy()
    expect(screen.getByText('Run bemol in /p to write them')).toBeTruthy()
  })

  it('lists detected languages that are not running, with a start button or an install hint', async () => {
    stubPool(READY)
    render(wrap(<CodeIntelPanel />))
    expect(await screen.findByText('Not installed. Install one of: gopls')).toBeTruthy()
    // Each project shows its root relative to the chat's project directory.
    expect(screen.getByText('crate')).toBeTruthy()
    expect(screen.getByText('worktrees/a/crate')).toBeTruthy()
    // The running python project is in the pool list, not repeated here.
    expect(screen.queryByLabelText('Start the python server for /p/proj')).toBeNull()
    expect(screen.getByText('Files: 1')).toBeTruthy()
    fireEvent.click(screen.getByLabelText('Start the rust server for /p/worktrees/a/crate'))
    await waitFor(() => expect(calls('start')[0]?.[1]).toEqual({ op: 'start', server: 'rust', root: '/p/worktrees/a/crate', lang: 'en' }))
  })

  it('searches workspace symbols and opens a result in the panel at its line', async () => {
    stubPool(READY, {
      workspace_symbols_all: {
        symbols: [
          { name: 'helper', kind: 'function', container: 'lib', path: '/p/proj/lib.py', line: 3, character: 4, server: 'python', root: '/p/proj' },
          { name: 'Stage', kind: 'enum', container: 'com.amazon.variable', path: '', uri: 'jdt://contents/v.jar/Stage.class', line: 12, character: 0, server: 'java', root: '/p/svc' },
        ],
        truncated: false, external: 0, hidden: 0, servers: 2,
        errors: [{ server: 'rust', root: '/p/crate', error: 'timed out' }],
      },
      open_class: { path: '/cache/Stage.java' },
    })
    render(wrap(<CodeIntelPanel />))
    const box = await screen.findByLabelText('Search symbols in the project')
    fireEvent.change(box, { target: { value: 'help' } })
    fireEvent.click(await screen.findByText('helper'))
    // One search across every running server, each result naming its server.
    expect(calls('workspace_symbols_all')[0]?.[1]).toEqual({ op: 'workspace_symbols_all', query: 'help', lang: 'en' })
    expect(sdk.host?.openFile).toHaveBeenCalledWith('/p/proj/lib.py', { line: 3 })
    expect(screen.getByText('rust (crate) did not answer: timed out')).toBeTruthy()
    // A library class is written out when opened, then opened like a file.
    fireEvent.click(screen.getByText('Stage'))
    await waitFor(() => expect(sdk.host?.openFile).toHaveBeenCalledWith('/cache/Stage.java', { line: 12 }))
    expect(calls('open_class')[0]?.[1]).toEqual({ op: 'open_class', server: 'java', root: '/p/svc', uri: 'jdt://contents/v.jar/Stage.class', lang: 'en' })
  })

  it('expands a source file into its outline, and only source files', async () => {
    stubPool(READY, {
      symbols: { symbols: [{ name: 'helper', kind: 'function', detail: '', container: '', depth: 0, line: 1, character: 4 }], truncated: false, server: 'python' },
    })
    render(wrap(<CodeIntelPanel />))
    // The chosen server's project is open; the project it references is listed.
    expect(await screen.findByText('lib.py')).toBeTruthy()
    expect(screen.getByText('common')).toBeTruthy()
    // Hidden folders are left out of the tree.
    expect(screen.queryByText('.git')).toBeNull()
    expect(screen.queryByLabelText('Show the outline of notes.txt')).toBeNull()
    fireEvent.click(screen.getByLabelText('Show the outline of lib.py'))
    fireEvent.click(await screen.findByTitle('helper · 1'))
    expect(sdk.host?.openFile).toHaveBeenCalledWith('/p/proj/lib.py', { line: 1 })
    expect(calls('symbols')[0]?.[1]).toEqual({ op: 'symbols', path: '/p/proj/lib.py', lang: 'en' })
  })

  it('picks the outline among running servers only, and the outline follows the pick', async () => {
    stubPool({
      ...READY,
      servers: [READY.servers[0], { ...READY.servers[0], server: 'rust', root: '/p/crate', folders: [] }, { ...FAILED.servers[0] }],
    })
    render(wrap(<CodeIntelPanel />))
    expect(await screen.findByText('lib.py')).toBeTruthy()
    const picker = screen.getByLabelText('Language server for symbol search and the outline')
    // Only the two running servers: the failed java start is not offered.
    fireEvent.click(picker)
    expect(screen.queryByRole('option', { name: /java/ })).toBeNull()
    fireEvent.click(await screen.findByRole('option', { name: /rust/ }))
    // The outline switches to the rust project's tree.
    await waitFor(() => expect(screen.queryByText('lib.py')).toBeNull())
    await waitFor(() => expect(sdk.get).toHaveBeenCalledWith(`/api/browse-files?path=${encodeURIComponent('/p/crate')}`, expect.anything()))
  })

  it('has no file tree until a server runs', async () => {
    stubPool({ ...READY, servers: [] })
    render(wrap(<CodeIntelPanel />))
    expect(await screen.findByText(/Start a language server to browse/)).toBeTruthy()
  })

  it('says why a folder could not be listed, and shows the backend sentence for a refused call', async () => {
    stubPool(READY)
    sdk.get.mockRejectedValue(new Error('Permission denied: /p/proj'))
    render(wrap(<CodeIntelPanel />))
    const notice = await screen.findByText(/Could not list this folder/)
    expect(notice).toHaveTextContent('Permission denied: /p/proj')
    // The backend writes the reader's sentence into `error`; the bundle shows it as is.
    const err = Object.assign(new Error('API 422: {...}'), { status: 422, body: JSON.stringify({ error: 'No go language server is installed.', code: 'no_server' }) })
    expect(errorText(err)).toBe('No go language server is installed.')
    expect(errorText(new Error('boom'))).toBe('Code navigation failed.')
  })

  it('says the backend is not loaded when the gateway has no route, and keeps the app\'s own 404s', () => {
    const unmounted = Object.assign(new Error('API 404'), { status: 404, body: JSON.stringify({ error: 'not found' }) })
    expect(errorText(unmounted)).toMatch(/no route for Code Intelligence/)
    const own = Object.assign(new Error('API 404'), { status: 404, body: JSON.stringify({ error: 'File not found', code: 'not_found' }) })
    expect(errorText(own)).toBe('File not found')
  })

  it('refetches only the pool after a pool operation, and nothing while hidden', async () => {
    stubPool(READY, { symbols: { symbols: [], truncated: false, server: 'python' } })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { rerender } = render(wrap(<CodeIntelPanel />, client))
    fireEvent.click(await screen.findByLabelText('Show the outline of lib.py'))
    await waitFor(() => expect(calls('symbols')).toHaveLength(1))
    const serverCalls = calls('servers').length
    fireEvent.click(screen.getByLabelText('Stop the python server for /p/proj'))
    await waitFor(() => expect(calls('stop')).toHaveLength(1))
    await waitFor(() => expect(calls('servers').length).toBeGreaterThan(serverCalls))
    // The expanded outline was not asked for again.
    expect(calls('symbols')).toHaveLength(1)
    // Hidden: a refetch of everything (what a window focus does) reaches no query of the tab.
    sdk.active = false
    rerender(wrap(<CodeIntelPanel />, client))
    const before = [calls('servers').length, calls('detect').length, sdk.get.mock.calls.length, calls('symbols').length]
    await act(async () => { await client.refetchQueries({ type: 'active' }) })
    expect([calls('servers').length, calls('detect').length, sdk.get.mock.calls.length, calls('symbols').length]).toEqual(before)
  })

  it('without a project shows the pool but no detection, and polls only while active', async () => {
    stubPool(READY)
    sdk.host = { openFile: vi.fn() }
    sdk.active = false
    const { rerender } = render(wrap(<CodeIntelPanel />))
    expect(screen.getByText(/no project directory/)).toBeTruthy()
    expect(calls('servers')).toHaveLength(0)
    sdk.active = true
    rerender(wrap(<CodeIntelPanel />))
    await waitFor(() => expect(calls('servers').length).toBeGreaterThan(0))
    expect(calls('detect')).toHaveLength(0)
  })
})

describe('rootLabel', () => {
  it('shows a root relative to the project, or whole when outside it', () => {
    expect(rootLabel('/ws/worktrees/a/src/Svc', '/ws')).toBe('worktrees/a/src/Svc')
    expect(rootLabel('/ws/worktrees/a/src/Svc', '/ws/')).toBe('worktrees/a/src/Svc')
    expect(rootLabel('/ws', '/ws')).toBe('ws')
    expect(rootLabel('/wsx/Svc', '/ws')).toBe('/wsx/Svc')
    expect(rootLabel('/ws/Svc')).toBe('/ws/Svc')
  })
})

describe('the app client', () => {
  it('sends every pool operation to the app route, and the listing to the host route', async () => {
    const post = vi.fn(async () => ({}))
    const get = vi.fn(async () => ({}))
    const api = createCodeIntelApi({ post, get } as never)
    await api.servers()
    await api.detect('/p')
    await api.symbols('/p/a.py')
    await api.workspaceSymbolsAll('x')
    await api.start('python', '/p/proj')
    await api.restart('python', '/p/proj')
    await api.stop('python', '/p/proj')
    await api.browseFiles('/p/a b')
    // Every pool call carries the dashboard's language for the sentences the backend writes.
    const lang = 'en' // the mocked SDK's activeLocale()
    expect(post.mock.calls.map(c => c[1])).toEqual([
      { op: 'servers', lang },
      { op: 'detect', path: '/p', lang },
      { op: 'symbols', path: '/p/a.py', lang },
      { op: 'workspace_symbols_all', query: 'x', lang },
      { op: 'start', server: 'python', root: '/p/proj', lang },
      { op: 'restart', server: 'python', root: '/p/proj', lang },
      { op: 'stop', server: 'python', root: '/p/proj', lang },
    ])
    expect(post.mock.calls.every(c => c[0] === POOL)).toBe(true)
    expect(get).toHaveBeenCalledWith('/api/browse-files?path=%2Fp%2Fa%20b', { signal: undefined })
  })
})
