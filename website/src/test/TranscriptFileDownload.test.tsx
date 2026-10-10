import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, render } from '@testing-library/react'
import DiffBlock from '../components/DiffBlock'
import FilePathMenu from '../components/FilePathMenu'
import Clickable from '../components/Clickable'
import { downloadBlob } from '../utils/download'

vi.mock('../pierre', () => ({ PierrePatch: () => null }))
vi.mock('../utils/download', () => ({ downloadBlob: vi.fn() }))
vi.mock('../hooks/useGatewayPlatform', () => ({ useGatewayPlatform: () => 'windows' }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ directLocal: false }) }))

const patch = (path: string) => `--- ${path}\n+++ ${path}\n@@ -1 +1 @@\n-old\n+new`
const openDiffMenu = () => fireEvent.keyDown(screen.getByRole('button', { name: 'More options' }), { key: 'Enter' })
const download = () => screen.getByRole('menuitem', { name: /^Download/ })

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})
afterEach(() => vi.unstubAllGlobals())

describe('transcript file downloads', () => {
  it.each([true, false])('keeps header control counts within their pre-download budget (open: %s)', withOpen => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true }))
    render(<DiffBlock code={patch('/tmp/report.csv')} complete onFileOpen={withOpen ? vi.fn() : undefined} />)
    expect(screen.getAllByRole('button')).toHaveLength(withOpen ? 3 : 2)
    expect(screen.queryByRole('button', { name: 'Switch to unified view' }) !== null).toBe(withOpen)
    openDiffMenu()
    expect(screen.queryByRole('menuitem', { name: 'Switch to unified view' }) !== null).toBe(!withOpen)
    if (!withOpen) {
      const layout = screen.getByRole('menuitem', { name: 'Switch to unified view' })
      expect(layout).toHaveTextContent('Switch to unified view')
      fireEvent.click(layout)
      expect(screen.getByRole('menuitem', { name: 'Switch to split view' })).toHaveTextContent('Switch to split view')
    }
  })

  it('downloads binary bytes from a completed diff through the existing endpoint', async () => {
    const bytes = new Blob([new Uint8Array([0, 255, 128, 65])])
    const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => bytes })
    vi.stubGlobal('fetch', fetch)
    render(<DiffBlock code={patch('/tmp/report final.pdf')} complete />)
    openDiffMenu()
    fireEvent.keyDown(download(), { key: 'Enter' })
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledWith(bytes, 'report final.pdf'))
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/file-download?path=%2Ftmp%2Freport%20final.pdf')
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    await waitFor(() => expect(screen.getByRole('button', { name: 'More options' })).toHaveFocus())
  })

  it.each([
    ['/project/src/a/index.ts', '/project/src/b/index.ts', '/project/src/a/index.ts', '/project/src/b/index.ts'],
    ['/project/src/nested/unique.ts', '/project/src/other.ts', 'unique.ts', 'other.ts'],
    ['C:\\work\\index.ts', 'D:\\work\\index.ts', 'C:\\work\\index.ts', 'D:\\work\\index.ts'],
    ['/home/me/we\\ird.ts', '/home/me/other.ts', 'we\\ird.ts', 'other.ts'],
  ])('distinguishes menu targets %s and %s without changing path semantics', (a, b, labelA, labelB) => {
    render(<DiffBlock code={`${patch(a)}\n${patch(b)}`} complete />)
    openDiffMenu()
    expect(screen.getByRole('menuitem', { name: `Download · ${labelA}` })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: `Download · ${labelB}` })).toBeInTheDocument()
  })

  it('names each file of a multi-file diff and downloads the selected target', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['new']) })
    vi.stubGlobal('fetch', fetch)
    render(<DiffBlock code={`${patch('/project/src/a.csv')}\n${patch('/project/src/b.csv')}`} complete />)
    openDiffMenu()
    expect(screen.getByRole('menuitem', { name: 'Download · a.csv' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Download · b.csv' }))
    await waitFor(() => expect(downloadBlob).toHaveBeenCalled())
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/file-download?path=%2Fproject%2Fsrc%2Fb.csv')
  })

  it.each([
    ['deleted', '--- a/gone.csv\n+++ /dev/null\n@@ -1 +0,0 @@\n-old', true],
    ['ambiguous', '--- a/tmp/data.csv\n+++ b/tmp/data.csv\n@@ -1 +1 @@\n-old\n+new', true],
    ['unsafe', patch('../private/report.csv'), true],
    ['quoted traversal', '--- "a/\\056\\056/private/report.csv"\n+++ "b/\\056\\056/private/report.csv"\n@@ -1 +1 @@\n-old\n+new', true],
    ['quoted sensitive path', '--- "a/\\056env"\n+++ "b/\\056env"\n@@ -1 +1 @@\n-old\n+new', true],
    ['unfinished', patch('/tmp/report.csv'), false],
    ['headerless', '-old\n+new', true],
  ] as const)('offers no download for a %s patch', (_reason, code, complete) => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    render(<DiffBlock code={code} complete={complete} />)
    openDiffMenu()
    expect(screen.queryByRole('menuitem', { name: /^Download/ })).not.toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it.each([undefined, 'src/a.csv', '/other/b.csv', '/other/prefixsrc/a.csv'])('does not download an uncorroborated relative file using the gateway checkout (hint: %s)', pathHint => {
    const fetch = vi.fn()
    vi.stubGlobal('fetch', fetch)
    render(<DiffBlock code={patch('src/a.csv')} pathHint={pathHint} complete />)
    openDiffMenu()
    expect(screen.queryByRole('menuitem', { name: /^Download/ })).not.toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('uses a matching absolute hint only for its section, without guessing a sibling checkout', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['session file']) })
    vi.stubGlobal('fetch', fetch)
    render(<DiffBlock code={`${patch('src/a.csv')}\n${patch('src/b.csv')}`} pathHint="/session-worktree/src/b.csv" complete />)
    openDiffMenu()
    expect(screen.getAllByRole('menuitem', { name: /^Download/ })).toHaveLength(1)
    fireEvent.click(screen.getByRole('menuitem', { name: 'Download · b.csv' }))
    await waitFor(() => expect(downloadBlob).toHaveBeenCalled())
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/file-download?path=%2Fsession-worktree%2Fsrc%2Fb.csv')
  })

  it('uses a corroborated absolute path instead of guessing a rootless one', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['new']) })
    vi.stubGlobal('fetch', fetch)
    render(<DiffBlock code={patch('b/tmp/data.csv')} pathHint="/tmp/data.csv" complete />)
    openDiffMenu()
    fireEvent.click(download())
    await waitFor(() => expect(downloadBlob).toHaveBeenCalled())
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/file-download?path=%2Ftmp%2Fdata.csv')
  })

  it.each([
    ['quoted Unicode', '--- "a/src/caf\\303\\251.csv"\n+++ "b/src/caf\\303\\251.csv"\n@@ -1 +1 @@\n-old\n+new', 'src/café.csv'],
    ['rename', 'diff --git a/old.csv b/new.csv\nsimilarity index 100%\nrename from old.csv\nrename to new.csv', 'new.csv'],
    ['quoted binary', 'diff --git "a/src/report final.pdf" "b/src/report final.pdf"\nBinary files differ', 'src/report final.pdf'],
    ['added file', '--- /dev/null\n+++ b/src/new.csv\n@@ -0,0 +1 @@\n+new', 'src/new.csv'],
    ['reserved URL characters', patch('/tmp/report #1 & 2.csv'), '/tmp/report #1 & 2.csv'],
  ])('downloads the current target of a %s patch', async (_reason, code, path) => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => new Blob(['current']) })
    vi.stubGlobal('fetch', fetch)
    const target = path.startsWith('/') ? path : '/session-worktree/' + path
    render(<DiffBlock code={code} pathHint={target} complete />)
    openDiffMenu()
    fireEvent.click(download())
    await waitFor(() => expect(downloadBlob).toHaveBeenCalled())
    const url = new URL(fetch.mock.calls[0][0], 'http://localhost')
    expect(url.searchParams.get('path')).toBe(target)
    expect(url.searchParams.has('resolve')).toBe(false)
  })

  it('keeps deleted sections out of a mixed patch and deduplicates repeated files', () => {
    render(<DiffBlock code={`${patch('/project/src/keep.csv')}\n--- a/gone.csv\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n${patch('/project/src/keep.csv')}`} complete />)
    openDiffMenu()
    expect(screen.getAllByRole('menuitem', { name: /^Download/ })).toHaveLength(1)
    expect(download()).toHaveAttribute('title', 'Download current file on disk: /project/src/keep.csv')
  })

  it('does not mistake a quoted rootless absolute path for a project-relative path', () => {
    render(<DiffBlock code={'--- "a/tmp/my report.csv"\n+++ "b/tmp/my report.csv"\n@@ -1 +1 @@\n-old\n+new'} complete />)
    openDiffMenu()
    expect(screen.queryByRole('menuitem', { name: /^Download/ })).not.toBeInTheDocument()
  })

  it('recovers from a network failure through a keyboard retry', async () => {
    const bytes = new Blob(['current'])
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce({ ok: true, blob: async () => bytes })
    vi.stubGlobal('fetch', fetch)
    render(<DiffBlock code={patch('/tmp/report.csv')} complete />)
    openDiffMenu()
    fireEvent.keyDown(download(), { key: 'Enter' })
    expect(await screen.findByText('Download failed')).toBeInTheDocument()
    expect(downloadBlob).not.toHaveBeenCalled()
    fireEvent.keyDown(download(), { key: 'Enter' })
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledExactlyOnceWith(bytes, 'report.csv'))
    expect(screen.queryByText('Download failed')).not.toBeInTheDocument()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('offers downloads on remote file chips and preserves Windows filenames', async () => {
    const bytes = new Blob(['report'])
    const fetch = vi.fn().mockResolvedValue({ ok: true, blob: async () => bytes })
    vi.stubGlobal('fetch', fetch)
    render(<FilePathMenu filePath={'C:\\work\\report.csv'} kind="file"><Clickable>report.csv</Clickable></FilePathMenu>)
    fireEvent.contextMenu(screen.getByText('report.csv'))
    fireEvent.click(download())
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledWith(bytes, 'report.csv'))
    expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/file-download?path=C%3A%5Cwork%5Creport.csv')
  })

  it('does not offer file download for a directory chip', () => {
    render(<FilePathMenu filePath="/tmp/reports" kind="dir"><span>reports</span></FilePathMenu>)
    fireEvent.contextMenu(screen.getByText('reports'))
    expect(screen.getByRole('menuitem', { name: 'Copy path' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /^Download/ })).not.toBeInTheDocument()
  })

  it.each(['F10', 'ContextMenu'])('opens a file chip menu from the keyboard with %s', async key => {
    const bytes = new Blob(['report'])
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, blob: async () => bytes }))
    render(<FilePathMenu filePath="/tmp/report.csv" kind="file"><Clickable>report.csv</Clickable></FilePathMenu>)
    const chip = screen.getByText('report.csv')
    expect(chip).toHaveAttribute('tabindex', '0')
    chip.focus()
    expect(fireEvent.keyDown(chip, { key, shiftKey: key === 'F10', cancelable: true })).toBe(false)
    fireEvent.keyDown(download(), { key: 'Enter' })
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledWith(bytes, 'report.csv'))
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    await waitFor(() => expect(chip).toHaveFocus())
  })

  it.each([
    [400, 'content_redacted', /Download blocked:.*credential scan/],
    [404, undefined, 'Download failed'],
    [403, undefined, 'Download failed'],
  ])('keeps a %s refusal visible in the menu without saving bytes', async (status, code, message) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ code }), { status })))
    render(<FilePathMenu filePath="/tmp/report.csv" kind="file"><Clickable>report.csv</Clickable></FilePathMenu>)
    fireEvent.contextMenu(screen.getByText('report.csv'))
    fireEvent.click(download())
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /Ask the agent/ })).toHaveAttribute('aria-describedby')
    expect(downloadBlob).not.toHaveBeenCalled()
  })

  it.each(['diff', 'chip'])('prevents duplicate selections while a %s transfer is pending', async surface => {
    let finish!: (value: { ok: boolean; blob: () => Promise<Blob> }) => void
    const fetch = vi.fn(() => new Promise(resolve => { finish = resolve }))
    vi.stubGlobal('fetch', fetch)
    if (surface === 'diff') {
      render(<DiffBlock code={patch('/tmp/report.csv')} complete />)
      openDiffMenu()
    } else {
      render(<FilePathMenu filePath="/tmp/report.csv"><Clickable>report.csv</Clickable></FilePathMenu>)
      fireEvent.contextMenu(screen.getByText('report.csv'))
    }
    fireEvent.click(download())
    fireEvent.click(download())
    expect(download()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText(/Downloading…/)).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(1)
    finish({ ok: true, blob: async () => new Blob(['report']) })
    await waitFor(() => expect(screen.queryByRole('menu')).not.toBeInTheDocument())
    expect(screen.queryByText(/Downloading…/)).not.toBeInTheDocument()
  })

  it('lets a user dismiss a failed download without closing the menu', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    render(<DiffBlock code={patch('/tmp/report.csv')} complete />)
    openDiffMenu()
    fireEvent.click(download())
    expect(await screen.findByText('Download failed')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('Download failed')).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /Ask the agent/ })).not.toBeInTheDocument()
    expect(download()).toBeInTheDocument()
  })

  it('does not carry one file\'s error into a replacement file', async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce({ ok: true, blob: async () => new Blob(['replacement']) })
    vi.stubGlobal('fetch', fetch)
    const { rerender } = render(<FilePathMenu filePath="/tmp/old.csv"><span>file</span></FilePathMenu>)
    fireEvent.contextMenu(screen.getByText('file'))
    fireEvent.click(download())
    expect(await screen.findByText('Download failed')).toBeInTheDocument()
    rerender(<FilePathMenu filePath="/tmp/new.csv"><span>file</span></FilePathMenu>)
    expect(screen.queryByText('Download failed')).not.toBeInTheDocument()
    fireEvent.click(download())
    await waitFor(() => expect(downloadBlob).toHaveBeenCalled())
    expect(fetch).toHaveBeenLastCalledWith('/api/file-download?path=%2Ftmp%2Fnew.csv')
  })
})
