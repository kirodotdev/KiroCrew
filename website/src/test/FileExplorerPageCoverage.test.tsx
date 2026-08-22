/**
 * Coverage tests for the file-explorer page container.
 *
 * `fileExplorerComponents.test.tsx` already covers initialization (root
 * selection from `health`) and the presentational children. This file drives
 * the container's *interaction* surface, which that file leaves cold: tab
 * open/close/activate/rename, saved-state restore and persistence, root
 * navigation, the resolve mutation's four outcomes, download/reload, the
 * keyboard shortcuts, the git-status derivation, and the pane resizer.
 *
 * Same harness as that file: mock the api client and the context-menu
 * primitive, wrap in Redux + QueryClient + MemoryRouter, drive real events.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('@radix-ui/react-context-menu', async () => await import('./__mocks__/@radix-ui/react-context-menu'))
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))
import { render, screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'

// TabStrip observes its scroller for scroll-fade detection.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver

vi.mock('../apps/file-explorer/api', () => ({
  fileExplorerApi: {
    health: vi.fn(),
    tree: vi.fn(),
    read: vi.fn(),
    search: vi.fn(),
    gitStatus: vi.fn(),
    resolve: vi.fn(),
    complete: vi.fn(),
  },
}))

// MarkdownRenderer transitively loads highlight.js + mermaid; FileViewer also
// consumes its BasePathCtx for markdown, so the stub must export both.
vi.mock('../components/MarkdownRenderer', async () => {
  const React = await import('react')
  return {
    default: ({ content }: { content: string }) =>
      React.createElement('pre', { 'data-testid': 'md-renderer' }, content),
    BasePathCtx: React.createContext(''),
  }
})

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

// FileViewer's overflow gates Open/Reveal on directLocal (the browser is on the
// gateway machine). Mock it true so the reveal-menu tests can see those items;
// the remote-hidden case is covered in FilePathMenu.test.tsx.
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Kiro Crew', avatar: '/logo.png', directLocal: true }),
}))

import { fileExplorerApi } from '../apps/file-explorer/api'
import { api } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { i18nT } from '../i18n/t'
import { STORAGE_KEY } from '../apps/file-explorer/constants'
import FileExplorerPage from '../apps/file-explorer/FileExplorerPage'
import type { TreeEntry, FileMeta, GitInfo } from '../apps/file-explorer/types'

const ROOT = '/home/user'

const ENTRIES: TreeEntry[] = [
  { name: 'src', path: '/home/user/src', type: 'dir', children: [
    { name: 'index.ts', path: '/home/user/src/index.ts', type: 'file' },
  ] },
  { name: 'notes.txt', path: '/home/user/notes.txt', type: 'file', size: 12, mtime: 1700000000 },
  { name: 'other.txt', path: '/home/user/other.txt', type: 'file', size: 20, mtime: 1700000000 },
]

const base = (p: string): FileMeta => ({
  size: 12, mtime: 1700000000, mime: 'text/plain', encoding: 'utf-8',
  content: `body of ${p}`,
})

/** Give every endpoint a safe resolved value so no query can reject unhandled. */
function stubApi() {
  vi.mocked(fileExplorerApi.health).mockResolvedValue({ allowedRoots: [ROOT], home: ROOT })
  vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: ENTRIES })
  vi.mocked(fileExplorerApi.read).mockImplementation((p: string) => Promise.resolve(base(p)))
  vi.mocked(fileExplorerApi.search).mockResolvedValue({ results: [], engine: 'rg', truncated: false })
  vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue(null)
  vi.mocked(fileExplorerApi.resolve).mockResolvedValue({ exists: true, type: 'dir' })
  vi.mocked(fileExplorerApi.complete).mockResolvedValue({ entries: [] })
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const store = createTestStore()
  const utils = render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={['/file-explorer']}>
          <FileExplorerPage />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  return { ...utils, store, qc }
}

/** Seed the persisted-state key the page restores from on mount. */
function seedSaved(state: Record<string, unknown>) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
}

const treeBox = () => within(document.querySelector('.mc-fe-tree') as HTMLElement)
const tabBox = () => within(document.querySelector('.mc-fe-tabs') as HTMLElement)
const viewerName = () => document.querySelector('.mc-fe-viewer-filename')?.textContent
const leftPx = () => (document.querySelector('.mc-fe-left') as HTMLElement).style.width

/** The context-menu mock restores focus on a double rAF after a select. */
const flushRaf = () =>
  act(async () => {
    await new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))
  })

async function ready() {
  await waitFor(() => expect(document.querySelector('.mc-fe-tree')).toBeInTheDocument())
}

/** Open `name` from the tree and wait for the viewer to show it. */
async function openFromTree(name: string) {
  await userEvent.click(treeBox().getByText(name))
  await waitFor(() => expect(viewerName()).toBe(name))
}

/** Open the viewer's overflow menu and pick a row by its visible label. */
async function pickFromOverflow(label: string) {
  await userEvent.click(screen.getByLabelText('More options'))
  await userEvent.click(await screen.findByText(label))
}

async function openMenuOn(name: string) {
  fireEvent.contextMenu(treeBox().getByText(name))
  await waitFor(() => expect(screen.getAllByRole('menuitem').length).toBeGreaterThan(0))
}

async function clickMenuItem(label: string) {
  const item = screen.getAllByRole('menuitem').find((r) => r.textContent?.includes(label))
  expect(item).toBeTruthy()
  fireEvent.click(item as HTMLElement)
  await flushRaf()
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  stubApi()
})

afterEach(() => {
  document.body.style.cursor = ''
})

// ─── saved state ────────────────────────────────────────────────────────────

describe('FileExplorerPage saved state', () => {
  it('restores folder tabs, file tabs, active ids and pane width', async () => {
    seedSaved({
      folderTabs: [{ id: 'ft-a', rootPath: ROOT, label: 'Work', expanded: { [ROOT]: true } }],
      fileTabs: [{ id: 'of-a', path: '/home/user/notes.txt', folderId: 'ft-a' }],
      activeFolderId: 'ft-a',
      activeFileId: 'of-a',
      leftWidth: 320,
    })
    renderPage()
    await ready()
    // The saved label wins over the root's basename, and the saved file tab is
    // restored as the active file rather than an empty viewer.
    expect(tabBox().getByText('Work')).toBeInTheDocument()
    expect(tabBox().getByText('notes.txt')).toBeInTheDocument()
    await waitFor(() => expect(viewerName()).toBe('notes.txt'))
    expect(leftPx()).toBe('320px')
  })

  it('falls back to the first tab and the default width when the save omits them', async () => {
    seedSaved({ folderTabs: [{ id: 'ft-b', rootPath: ROOT, label: '' }] })
    renderPage()
    await ready()
    // No label → basename; no fileTabs/leftWidth keys → viewer empty, width 280.
    expect(tabBox().getByText('user')).toBeInTheDocument()
    expect(leftPx()).toBe('280px')
    expect(screen.getByText('Select a file to view')).toBeInTheDocument()
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith(ROOT, 2))
  })

  it('persists the live tab state after the debounce window', async () => {
    renderPage()
    await ready()
    await waitFor(() => expect(localStorage.getItem(STORAGE_KEY)).toBeTruthy(), { timeout: 3000 })
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) as string)
    expect(saved.folderTabs).toHaveLength(1)
    expect(saved.folderTabs[0].rootPath).toBe(ROOT)
    expect(saved.fileTabs).toEqual([])
    expect(saved.leftWidth).toBe(280)
  })

  it('persists open file tabs so a reload restores them', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await waitFor(() => {
      const s = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')
      expect(s.fileTabs).toHaveLength(1)
    }, { timeout: 3000 })
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) as string)
    expect(saved.fileTabs[0].path).toBe('/home/user/notes.txt')
    expect(saved.fileTabs[0].folderId).toBe(saved.folderTabs[0].id)
    expect(saved.activeFileId).toBe(saved.fileTabs[0].id)
  })
})

// ─── file tabs ──────────────────────────────────────────────────────────────

describe('FileExplorerPage file tabs', () => {
  it('opens a file tab when a tree file is clicked', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    expect(tabBox().getByText('notes.txt')).toBeInTheDocument()
    expect(fileExplorerApi.read).toHaveBeenCalledWith('/home/user/notes.txt')
    expect(screen.getByTestId('md-renderer').textContent).toContain('body of /home/user/notes.txt')
  })

  it('reuses the existing tab when the same file is clicked twice', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await userEvent.click(treeBox().getByText('notes.txt'))
    await waitFor(() => expect(viewerName()).toBe('notes.txt'))
    // Second click takes the `existing` path: re-activate, no duplicate tab.
    expect(tabBox().getAllByText('notes.txt')).toHaveLength(1)
  })

  it('closing the active file tab activates the last remaining one', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await openFromTree('other.txt')
    const closers = screen.getAllByLabelText('Close file tab')
    expect(closers).toHaveLength(2)
    await userEvent.click(closers[1])
    await waitFor(() => expect(viewerName()).toBe('notes.txt'))
    expect(screen.getAllByLabelText('Close file tab')).toHaveLength(1)
  })

  it('closing a non-active file tab keeps the current selection', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await openFromTree('other.txt')
    await userEvent.click(screen.getAllByLabelText('Close file tab')[0])
    await waitFor(() => expect(screen.getAllByLabelText('Close file tab')).toHaveLength(1))
    expect(viewerName()).toBe('other.txt')
  })

  it('activates a file tab from the tab strip', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await openFromTree('other.txt')
    await userEvent.click(tabBox().getByText('notes.txt'))
    await waitFor(() => expect(viewerName()).toBe('notes.txt'))
  })

  it('reload re-fetches the open file', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    const before = vi.mocked(fileExplorerApi.read).mock.calls.length
    await userEvent.click(screen.getByLabelText('Reload'))
    await waitFor(() =>
      expect(vi.mocked(fileExplorerApi.read).mock.calls.length).toBeGreaterThan(before),
    )
  })
})

// ─── download ───────────────────────────────────────────────────────────────

describe('FileExplorerPage download', () => {
  /** Capture the synthesized <a> and the blob handed to createObjectURL. */
  function captureDownload() {
    const blobs: Blob[] = []
    const names: string[] = []
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    URL.createObjectURL = vi.fn((b: Blob) => { blobs.push(b); return 'blob:fe' }) as typeof URL.createObjectURL
    URL.revokeObjectURL = vi.fn() as typeof URL.revokeObjectURL
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function mockClick(this: HTMLAnchorElement) { names.push(this.download) })
    return {
      blobs, names,
      restore: () => {
        clickSpy.mockRestore()
        URL.createObjectURL = origCreate
        URL.revokeObjectURL = origRevoke
      },
    }
  }

  it('downloads a text file as a plain-text blob named after the file', async () => {
    const cap = captureDownload()
    try {
      renderPage()
      await ready()
      await openFromTree('notes.txt')
      await pickFromOverflow('Download')
      expect(cap.names).toEqual(['notes.txt'])
      expect(cap.blobs).toHaveLength(1)
      expect(cap.blobs[0].type).toBe('text/plain;charset=utf-8')
      expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:fe')
    } finally { cap.restore() }
  })

  it('decodes a base64 payload into a binary blob of the reported mime', async () => {
    const cap = captureDownload()
    try {
      vi.mocked(fileExplorerApi.tree).mockResolvedValue({
        entries: [{ name: 'shot.png', path: '/home/user/shot.png', type: 'file', size: 3 }],
      })
      vi.mocked(fileExplorerApi.read).mockResolvedValue({
        size: 3, mime: 'image/png', encoding: 'base64', content: btoa('PNG'),
      })
      renderPage()
      await ready()
      await openFromTree('shot.png')
      await pickFromOverflow('Download')
      expect(cap.names).toEqual(['shot.png'])
      // atob branch: bytes, not the base64 text, and the backend's mime.
      expect(cap.blobs[0].type).toBe('image/png')
      expect(cap.blobs[0].size).toBe(3)
    } finally { cap.restore() }
  })
})

// ─── reveal in the host file manager ────────────────────────────────────────

describe('FileExplorerPage reveal', () => {
  // `vi.spyOn` survives the file-wide `vi.clearAllMocks()`, which clears calls
  // but leaves the stub installed — so each spy is restored here rather than
  // leaking a swallowed `window.alert` into every later test.
  const spies: { mockRestore(): void }[] = []
  afterEach(() => { for (const s of spies) s.mockRestore(); spies.length = 0 })

  function spyReveal(result: { ok: boolean; copy?: string } | Error) {
    const s = vi.spyOn(api, 'revealPath')
    if (result instanceof Error) s.mockRejectedValue(result)
    else s.mockResolvedValue(result as never)
    spies.push(s)
    return s
  }

  /** Silence and capture the alert the two failure paths raise. */
  function captureAlert() {
    const s = vi.spyOn(window, 'alert').mockImplementation(() => {})
    spies.push(s)
    return s
  }

  it('hands the open file to the host file manager', async () => {
    const reveal = spyReveal({ ok: true })
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await pickFromOverflow('Show in file manager')
    expect(reveal).toHaveBeenCalledExactlyOnceWith('/home/user/notes.txt', 'reveal')
  })

  it('does not alert locally when the mocked backend resolves with a copy fallback', async () => {
    // The copy-fallback confirmation is centralized in api.revealPath itself
    // (client.ts), right next to its copyToClipboard call, so this call site
    // must not also alert — that would double-notify once the real client
    // resolves.
    const reveal = spyReveal({ ok: true, copy: '/home/user/notes.txt' })
    const alerted = captureAlert()
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await pickFromOverflow('Show in file manager')
    await waitFor(() => expect(reveal).toHaveBeenCalled())
    expect(alerted).not.toHaveBeenCalled()
  })

  it('surfaces a refusal with the shared i18n failure message', async () => {
    // The overflow funnels reveal failures through the shared FilePathMenu path,
    // which shows a neutral catalog string rather than leaking the raw server
    // message per surface.
    spyReveal(new Error('access denied'))
    const alerted = captureAlert()
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await pickFromOverflow('Show in file manager')
    await waitFor(() => expect(alerted).toHaveBeenCalledWith(i18nT('components.filePathMenu.reveal_failed')))
  })

  /** Publish a gateway platform into the cache the prerequisite gate owns. */
  function setGatewayPlatform(qc: QueryClient, platform: string) {
    act(() => { qc.setQueryData(['kiro-prerequisite'], { platform }) })
  }

  it('names Finder by name when the gateway host is macOS', async () => {
    const { qc } = renderPage()
    await ready()
    setGatewayPlatform(qc, 'darwin')
    await openFromTree('notes.txt')
    await userEvent.click(screen.getByLabelText('More options'))
    expect(await screen.findByText('Open in Finder')).toBeInTheDocument()
    expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
  })

  it('names File Explorer by name when the gateway host is Windows', async () => {
    const { qc } = renderPage()
    await ready()
    setGatewayPlatform(qc, 'win32')
    await openFromTree('notes.txt')
    await userEvent.click(screen.getByLabelText('More options'))
    expect(await screen.findByText('Open in File Explorer')).toBeInTheDocument()
    expect(screen.queryByText('Open in Finder')).not.toBeInTheDocument()
  })

  it('stays neutral when the gateway platform is withheld or unknown', async () => {
    // A non-owner dashboard user (and a probe that could not run) gets the
    // sentinel 'gateway', which must never be read as macOS.
    const { qc } = renderPage()
    await ready()
    setGatewayPlatform(qc, 'gateway')
    await openFromTree('notes.txt')
    await userEvent.click(screen.getByLabelText('More options'))
    expect(await screen.findByText('Show in file manager')).toBeInTheDocument()
    expect(screen.queryByText('Open in Finder')).not.toBeInTheDocument()
  })

  it('keeps the action row at two controls, with reveal in the overflow', async () => {
    // The row caps at two peer buttons, so a third action has to live in the
    // menu — pin the shape, not just the behaviour.
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    const row = document.querySelector('.mc-fe-viewer-actions') as HTMLElement
    expect(row.querySelectorAll('button')).toHaveLength(2)
    expect(within(row).getByLabelText('Reload')).toBeInTheDocument()
    expect(within(row).getByLabelText('More options')).toBeInTheDocument()
    expect(screen.queryByText('Show in file manager')).not.toBeInTheDocument()
  })
})

// ─── folder tabs ────────────────────────────────────────────────────────────

describe('FileExplorerPage folder tabs', () => {
  it('adds a workspace tab rooted at the current path', async () => {
    renderPage()
    await ready()
    await userEvent.click(screen.getByLabelText('New workspace tab'))
    await waitFor(() => expect(tabBox().getAllByText('user')).toHaveLength(2))
  })

  it('closing the active folder tab activates the remaining one', async () => {
    renderPage()
    await ready()
    await userEvent.click(screen.getByLabelText('New workspace tab'))
    await waitFor(() => expect(tabBox().getAllByText('user')).toHaveLength(2))
    // The second tab is active after creation; closing it falls back to the first.
    await userEvent.click(screen.getAllByLabelText('Close workspace tab')[1])
    await waitFor(() => expect(tabBox().getAllByText('user')).toHaveLength(1))
    expect(document.querySelector('.mc-fe-tab-folder.is-active')).toBeInTheDocument()
  })

  it('closing a non-active folder tab keeps the active one selected', async () => {
    renderPage()
    await ready()
    await userEvent.click(screen.getByLabelText('New workspace tab'))
    await waitFor(() => expect(tabBox().getAllByText('user')).toHaveLength(2))
    const activeBefore = document.querySelectorAll('.mc-fe-tab-folder')[1]
    await userEvent.click(screen.getAllByLabelText('Close workspace tab')[0])
    await waitFor(() => expect(tabBox().getAllByText('user')).toHaveLength(1))
    expect(document.querySelector('.mc-fe-tab-folder')).toBe(activeBefore)
    expect(document.querySelector('.mc-fe-tab-folder')).toHaveClass('is-active')
  })

  it('closing the last folder tab replaces it with a fresh root tab', async () => {
    renderPage()
    await ready()
    await userEvent.click(screen.getByLabelText('Close workspace tab'))
    // No tabs would leave nothing to render, so the page substitutes '/'.
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/', 2))
    expect(document.querySelectorAll('.mc-fe-tab-folder')).toHaveLength(1)
  })

  it('closing a folder tab discards the file tabs that belonged to it', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    expect(screen.getAllByLabelText('Close file tab')).toHaveLength(1)
    await userEvent.click(screen.getByLabelText('Close workspace tab'))
    await waitFor(() => expect(screen.queryByLabelText('Close file tab')).not.toBeInTheDocument())
    expect(screen.getByText('Select a file to view')).toBeInTheDocument()
  })

  it('activating a folder tab clears the file selection', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await userEvent.click(document.querySelector('.mc-fe-tab-folder') as HTMLElement)
    // The file tab survives; only the viewer selection is dropped.
    await waitFor(() => expect(screen.getByText('Select a file to view')).toBeInTheDocument())
    expect(screen.getAllByLabelText('Close file tab')).toHaveLength(1)
  })

  it('renames a folder tab from a double-click', async () => {
    renderPage()
    await ready()
    fireEvent.doubleClick(document.querySelector('.mc-fe-tab-folder') as HTMLElement)
    const input = screen.getByLabelText('Rename workspace tab')
    await userEvent.clear(input)
    await userEvent.type(input, '  Scratch  {Enter}')
    // The label is trimmed and replaces the basename.
    await waitFor(() => expect(tabBox().getByText('Scratch')).toBeInTheDocument())
    expect(tabBox().queryByText('user')).not.toBeInTheDocument()
  })
})

// ─── tree + root navigation ─────────────────────────────────────────────────

describe('FileExplorerPage tree and root navigation', () => {
  it('toggles a directory open and closed', async () => {
    renderPage()
    await ready()
    expect(treeBox().queryByText('index.ts')).not.toBeInTheDocument()
    await userEvent.click(treeBox().getByText('src'))
    await waitFor(() => expect(treeBox().getByText('index.ts')).toBeInTheDocument())
    await userEvent.click(treeBox().getByText('src'))
    await waitFor(() => expect(treeBox().queryByText('index.ts')).not.toBeInTheDocument())
  })

  it('a breadcrumb click re-roots the workspace and drops its file tabs', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    await userEvent.click(within(document.querySelector('.mc-fe-breadcrumbs') as HTMLElement).getByText('home'))
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home', 2))
    expect(screen.queryByLabelText('Close file tab')).not.toBeInTheDocument()
  })

  it('re-rooting to the path already open is a no-op', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    const calls = vi.mocked(fileExplorerApi.tree).mock.calls.length
    // The last breadcrumb segment IS the current root.
    await userEvent.click(within(document.querySelector('.mc-fe-breadcrumbs') as HTMLElement).getByText('user'))
    expect(vi.mocked(fileExplorerApi.tree).mock.calls.length).toBe(calls)
    // A real re-root would have cleared the file tab; it is still here.
    expect(screen.getAllByLabelText('Close file tab')).toHaveLength(1)
  })

  it('context menu "Open" opens the right-clicked file', async () => {
    renderPage()
    await ready()
    await openMenuOn('notes.txt')
    await clickMenuItem('Open')
    await waitFor(() => expect(viewerName()).toBe('notes.txt'))
  })

  it('context menu "Open as workspace root" re-roots to the directory', async () => {
    renderPage()
    await ready()
    await openMenuOn('src')
    await clickMenuItem('Open as workspace root')
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home/user/src', 2))
  })

  it('context menu "Reveal parent" re-roots to the parent directory', async () => {
    renderPage()
    await ready()
    // Reveal must target a NESTED file: the parent of a root-level file is the
    // root already, which re-rooting short-circuits.
    await userEvent.click(treeBox().getByText('src'))
    await waitFor(() => expect(treeBox().getByText('index.ts')).toBeInTheDocument())
    await openMenuOn('index.ts')
    await clickMenuItem('Reveal parent')
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home/user/src', 2))
  })

  it('revealing the parent of a root-level file changes nothing', async () => {
    renderPage()
    await ready()
    const calls = vi.mocked(fileExplorerApi.tree).mock.calls.length
    await openMenuOn('notes.txt')
    await clickMenuItem('Reveal parent')
    expect(vi.mocked(fileExplorerApi.tree).mock.calls.length).toBe(calls)
  })

  it('context menu "Copy path" copies the node path', async () => {
    renderPage()
    await ready()
    await openMenuOn('notes.txt')
    await clickMenuItem('Copy path')
    expect(copyToClipboard).toHaveBeenCalledWith('/home/user/notes.txt')
  })

  it('offers folder wording and no "Open" row for a directory', async () => {
    renderPage()
    await ready()
    await openMenuOn('src')
    const labels = screen.getAllByRole('menuitem').map((r) => r.textContent)
    expect(labels.some((l) => l?.includes('Chat about this folder'))).toBe(true)
    expect(labels.some((l) => l?.trim() === 'Open')).toBe(false)
  })
})

// ─── path resolve ───────────────────────────────────────────────────────────

describe('FileExplorerPage path resolution', () => {
  /** Type a path into the path bar and commit it, which runs the resolve mutation. */
  async function navigateTo(path: string) {
    await userEvent.click(screen.getByTitle('Click to edit path'))
    const input = screen.getByLabelText('Folder path')
    await userEvent.clear(input)
    await userEvent.type(input, `${path}{Enter}`)
  }

  it('re-roots when the path resolves to a directory', async () => {
    renderPage()
    await ready()
    vi.mocked(fileExplorerApi.resolve).mockResolvedValue({ exists: true, type: 'dir' })
    await navigateTo('/home/user/src')
    await waitFor(() => expect(fileExplorerApi.resolve).toHaveBeenCalledWith('/home/user/src'))
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home/user/src', 2))
  })

  it('re-roots to the parent and opens the file when the path is a file', async () => {
    renderPage()
    await ready()
    vi.mocked(fileExplorerApi.resolve).mockResolvedValue({ exists: true, type: 'file' })
    await navigateTo('/home/user/src/index.ts')
    // Parent becomes the root, and the file itself is opened in a tab.
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home/user/src', 2))
    await waitFor(() => expect(viewerName()).toBe('index.ts'))
  })

  it('keeps the file in place when it already sits in the open root', async () => {
    renderPage()
    await ready()
    vi.mocked(fileExplorerApi.resolve).mockResolvedValue({ exists: true, type: 'file' })
    const calls = vi.mocked(fileExplorerApi.tree).mock.calls.length
    await navigateTo('/home/user/notes.txt')
    await waitFor(() => expect(viewerName()).toBe('notes.txt'))
    // Parent === current root, so no re-root and no extra tree fetch.
    expect(vi.mocked(fileExplorerApi.tree).mock.calls.length).toBe(calls)
  })

  it('still re-roots when the path does not exist', async () => {
    renderPage()
    await ready()
    vi.mocked(fileExplorerApi.resolve).mockResolvedValue({ exists: false, type: '' })
    await navigateTo('/home/user/ghost')
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home/user/ghost', 2))
  })

  it('still re-roots when the resolve request fails', async () => {
    renderPage()
    await ready()
    vi.mocked(fileExplorerApi.resolve).mockRejectedValue(new Error('offline'))
    await navigateTo('/home/user/src')
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/home/user/src', 2))
  })
})

// ─── search and keyboard shortcuts ──────────────────────────────────────────

describe('FileExplorerPage shortcuts and search', () => {
  const chord = (key: string) => fireEvent.keyDown(window, { key, ctrlKey: true })

  it('toggles search with the command chord and closes it with Escape', async () => {
    renderPage()
    await ready()
    act(() => { chord('f') })
    await waitFor(() => expect(screen.getByPlaceholderText(/Search in/)).toBeInTheDocument())
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    await waitFor(() => expect(screen.queryByPlaceholderText(/Search in/)).not.toBeInTheDocument())
  })

  it('ignores Escape when search is already closed', async () => {
    renderPage()
    await ready()
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(screen.queryByPlaceholderText(/Search in/)).not.toBeInTheDocument()
    expect(screen.getByText('Select a file to view')).toBeInTheDocument()
  })

  it('opens a new workspace tab from the command chord', async () => {
    renderPage()
    await ready()
    act(() => { chord('t') })
    await waitFor(() => expect(tabBox().getAllByText('user')).toHaveLength(2))
  })

  it('closes the open file with the command chord', async () => {
    renderPage()
    await ready()
    await openFromTree('notes.txt')
    act(() => { chord('w') })
    await waitFor(() => expect(screen.queryByLabelText('Close file tab')).not.toBeInTheDocument())
  })

  it('closes the workspace tab with the command chord when no file is open', async () => {
    renderPage()
    await ready()
    act(() => { chord('w') })
    // Last tab closed → substituted with a '/' root.
    await waitFor(() => expect(fileExplorerApi.tree).toHaveBeenCalledWith('/', 2))
  })

  it('leaves unmodified keys alone', async () => {
    renderPage()
    await ready()
    act(() => { fireEvent.keyDown(window, { key: 'f' }) })
    expect(screen.queryByPlaceholderText(/Search in/)).not.toBeInTheDocument()
  })

  it('jumping to a search hit opens the file and closes the panel', async () => {
    vi.mocked(fileExplorerApi.search).mockResolvedValue({
      results: [{ file: '/home/user/other.txt', line: 3, col: 1, preview: 'needle here' }],
      engine: 'rg', truncated: false,
    })
    renderPage()
    await ready()
    act(() => { chord('f') })
    await userEvent.type(screen.getByPlaceholderText(/Search in/), 'needle')
    await waitFor(() => expect(screen.getByText('needle here')).toBeInTheDocument())
    await userEvent.click(screen.getByText('needle here'))
    // Panel closes and the hit is opened in the viewer.
    await waitFor(() => expect(screen.queryByPlaceholderText(/Search in/)).not.toBeInTheDocument())
    expect(viewerName()).toBe('other.txt')
  })
})

// ─── git status ─────────────────────────────────────────────────────────────

describe('FileExplorerPage git status', () => {
  it('shows the branch and change count for the open root', async () => {
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue({
      repoRoot: ROOT, branch: 'feat/tabs', statuses: { 'notes.txt': 'M', 'other.txt': '??' },
    })
    renderPage()
    await ready()
    await waitFor(() => expect(screen.getByText('feat/tabs')).toBeInTheDocument())
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('queries git status for nested repos discovered in the tree', async () => {
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({
      entries: [
        { name: 'vendor', path: '/home/user/vendor', type: 'dir', children: [
          { name: 'lib', path: '/home/user/vendor/lib', type: 'dir', isGitRoot: true, children: [] },
        ] },
        { name: 'app', path: '/home/user/app', type: 'dir', isGitRoot: true, children: [] },
      ],
    })
    const byPath: Record<string, GitInfo> = {
      '/home/user/app': { repoRoot: '/home/user/app', branch: 'app-main', statuses: {} },
      '/home/user/vendor/lib': { repoRoot: '/home/user/vendor/lib', branch: 'lib-main', statuses: {} },
    }
    vi.mocked(fileExplorerApi.gitStatus).mockImplementation((p: string) =>
      Promise.resolve(byPath[p] ?? null))
    renderPage()
    await ready()
    // The walk recurses into children, so BOTH nested roots get a query.
    await waitFor(() => expect(fileExplorerApi.gitStatus).toHaveBeenCalledWith('/home/user/app'))
    await waitFor(() => expect(fileExplorerApi.gitStatus).toHaveBeenCalledWith('/home/user/vendor/lib'))
  })

  it('falls back to the enclosing repo when the open folder is not itself a root', async () => {
    const sub = '/home/user/project/sub'
    seedSaved({ folderTabs: [{ id: 'ft-s', rootPath: sub, label: '', expanded: { [sub]: true } }] })
    vi.mocked(fileExplorerApi.tree).mockResolvedValue({ entries: [] })
    // The backend reports the ancestor repo, which is not the open path.
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue({
      repoRoot: '/home/user', branch: 'enclosing', statuses: { 'project/sub/a.txt': 'M' },
    })
    renderPage()
    await waitFor(() => expect(screen.getByText('enclosing')).toBeInTheDocument())
  })

  it('shows no branch when the folder is outside any repo', async () => {
    vi.mocked(fileExplorerApi.gitStatus).mockResolvedValue({
      repoRoot: '/somewhere/else', branch: 'unrelated', statuses: {},
    })
    renderPage()
    await ready()
    await waitFor(() => expect(fileExplorerApi.gitStatus).toHaveBeenCalled())
    expect(screen.queryByText('unrelated')).not.toBeInTheDocument()
  })
})

// ─── resizer ────────────────────────────────────────────────────────────────

describe('FileExplorerPage pane resizer', () => {
  const handle = () => screen.getByLabelText('Resize panel')

  it('widens the left pane as the handle is dragged', async () => {
    renderPage()
    await ready()
    expect(leftPx()).toBe('280px')
    fireEvent.pointerDown(handle(), { clientX: 300, pointerId: 1 })
    // threshold 0 → the drag commits on pointer-down and sets the resize cursor.
    expect(document.body.style.cursor).toBe('col-resize')
    act(() => { fireEvent.pointerMove(handle(), { clientX: 380, pointerId: 1 }) })
    expect(leftPx()).toBe('360px')
    act(() => { fireEvent.pointerUp(handle(), { clientX: 380, pointerId: 1 }) })
    expect(document.body.style.cursor).toBe('')
  })

  it('clamps the pane to its minimum and maximum width', async () => {
    renderPage()
    await ready()
    fireEvent.pointerDown(handle(), { clientX: 300, pointerId: 1 })
    act(() => { fireEvent.pointerMove(handle(), { clientX: -5000, pointerId: 1 }) })
    expect(leftPx()).toBe('180px')
    act(() => { fireEvent.pointerMove(handle(), { clientX: 5000, pointerId: 1 }) })
    expect(leftPx()).toBe('640px')
    act(() => { fireEvent.pointerUp(handle(), { clientX: 5000, pointerId: 1 }) })
  })

  it('clears the global resize cursor if the pane unmounts mid-drag', async () => {
    const { unmount } = renderPage()
    await ready()
    fireEvent.pointerDown(handle(), { clientX: 300, pointerId: 1 })
    expect(document.body.style.cursor).toBe('col-resize')
    // onEnd can never fire now, so the unmount guard is the only cleanup.
    unmount()
    expect(document.body.style.cursor).toBe('')
  })
})

// ─── backend banner ─────────────────────────────────────────────────────────

describe('FileExplorerPage backend banner', () => {
  it('surfaces a health failure that happens after the page has initialized', async () => {
    const { qc } = renderPage()
    await ready()
    vi.mocked(fileExplorerApi.health).mockRejectedValue(new Error('connection refused'))
    await act(async () => {
      await qc.refetchQueries({ queryKey: ['file-explorer', 'health'] }).catch(() => {})
    })
    // Cached data keeps the page initialized, so the banner is reachable.
    await waitFor(() => expect(screen.getByText(/Backend not reachable/)).toBeInTheDocument())
    expect(screen.getByText(/connection refused/)).toBeInTheDocument()
  })
})
