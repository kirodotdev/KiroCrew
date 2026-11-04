/**
 * A relative path in a chat transcript names a file in that chat's project.
 *
 * The backend's `resolve=1` joins a relative path to the gateway-wide project
 * directory, which in the desktop app is the app bundle, so a chat's
 * `run/checkin.sh` was probed under the bundle, read as missing and never became
 * a chip. A chat transcript provides its slot's project through
 * `ChatProjectDirCtx`; chips and links join the relative text to it before they
 * probe or open.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { ChatProjectDirCtx } from '../components/markdown/contexts'
import { joinProjectRelative } from '../components/markdown/pathReferences'
import { __resetPathKindCache } from '../hooks/usePathKind'
import { probedPaths, stubPathKinds } from './pathKindStub'

const PROJECT = '/home/user/proj'
const ABS = '/home/user/proj/run/checkin.sh'

const realFetch = globalThis.fetch

beforeEach(() => {
  __resetPathKindCache()
})

afterEach(() => {
  globalThis.fetch = realFetch
})

describe('joinProjectRelative', () => {
  it('joins a relative POSIX path to the project', () => {
    expect(joinProjectRelative('run/checkin.sh', PROJECT)).toBe(ABS)
    expect(joinProjectRelative('./run/checkin.sh', PROJECT + '/')).toBe(ABS)
    expect(joinProjectRelative('run/./checkin.sh', PROJECT)).toBe(ABS)
    expect(joinProjectRelative('src', '/')).toBe('/src')
    expect(joinProjectRelative('.', PROJECT)).toBe(PROJECT)
  })

  it('leaves absolute, home and unjoinable paths unchanged', () => {
    expect(joinProjectRelative('/etc/hosts', PROJECT)).toBe('/etc/hosts')
    expect(joinProjectRelative('~/notes.md', PROJECT)).toBe('~/notes.md')
    expect(joinProjectRelative('~bob/notes.md', PROJECT)).toBe('~bob/notes.md')
    expect(joinProjectRelative('C:\\x\\y.txt', PROJECT)).toBe('C:\\x\\y.txt')
    expect(joinProjectRelative('run/checkin.sh', null)).toBe('run/checkin.sh')
    expect(joinProjectRelative('run/checkin.sh', 'relative/proj')).toBe('run/checkin.sh')
  })

  it('keeps `..` segments so the backend resolves them past symlinks', () => {
    // `link` may be a symlink: `link/..` is its target's parent, not the project,
    // so collapsing the pair here would name a different file.
    expect(joinProjectRelative('link/../README.md', PROJECT)).toBe(PROJECT + '/link/../README.md')
    expect(joinProjectRelative('a/b/../../c.md', PROJECT)).toBe(PROJECT + '/a/b/../../c.md')
  })

  it('refuses a path that climbs above the project', () => {
    expect(joinProjectRelative('../secret.txt', PROJECT)).toBeNull()
    expect(joinProjectRelative('run/../../secret.txt', PROJECT)).toBeNull()
  })

  it('keeps a backslash as a filename character in a POSIX project', () => {
    expect(joinProjectRelative('a\\b.txt', PROJECT)).toBe(PROJECT + '/a\\b.txt')
  })

  it('joins with backslashes in a Windows project', () => {
    expect(joinProjectRelative('run/checkin.sh', 'C:\\Users\\me\\proj')).toBe('C:\\Users\\me\\proj\\run\\checkin.sh')
    expect(joinProjectRelative('src\\..\\a.py', 'C:\\proj\\')).toBe('C:\\proj\\src\\..\\a.py')
    expect(joinProjectRelative('..\\a.py', 'C:\\proj')).toBeNull()
  })
})

describe('MarkdownRenderer relative path chips', () => {
  it('probes and opens a relative path inside the chat project', async () => {
    const fetchMock = stubPathKinds(p => (p === ABS ? 'file' : null))
    const onFileOpen = vi.fn()
    const { container } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'See `run/checkin.sh`.'} onFileOpen={onFileOpen} />
      </ChatProjectDirCtx.Provider>,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c as HTMLElement
    })
    expect(chip.getAttribute('data-path')).toBe(ABS)
    expect(chip.textContent).toContain('run/checkin.sh')
    expect(probedPaths(fetchMock)).toEqual([ABS])
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith(ABS)
  })

  it('keeps the written label and line of a joined path:line chip', async () => {
    stubPathKinds(p => (p === '/src/a.py' ? 'file' : null))
    const onFileOpen = vi.fn()
    const { container } = render(
      <ChatProjectDirCtx.Provider value="/">
        <MarkdownRenderer content={'See `src/a.py:12`.'} onFileOpen={onFileOpen} />
      </ChatProjectDirCtx.Provider>,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c as HTMLElement
    })
    expect(chip.textContent).toBe('src/a.py:12')
    expect(chip.getAttribute('data-path')).toBe('/src/a.py')
    fireEvent.click(chip)
    expect(onFileOpen).toHaveBeenCalledWith('/src/a.py', { line: 12 })
  })

  it('offers no chip for a relative path that climbs out of the project', async () => {
    const fetchMock = stubPathKinds(() => 'file')
    const { container } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'See `../outside.txt` and `run/checkin.sh`.'} onFileOpen={vi.fn()} />
      </ChatProjectDirCtx.Provider>,
    )
    // The in-project chip settles in the same batch the escaping path would
    // have joined, so once it is drawn the escaping one was never asked about.
    await waitFor(() => {
      expect(container.querySelectorAll('code[data-path-kind="file"]')).toHaveLength(1)
    })
    expect(container.querySelector('code[data-path-kind="file"]')?.getAttribute('data-path')).toBe(ABS)
    expect(probedPaths(fetchMock)).toEqual([ABS])
  })

  it('sends a relative path unchanged when no chat project is provided', async () => {
    const fetchMock = stubPathKinds(p => (p === 'run/checkin.sh' ? 'file' : null))
    const { container } = render(
      <MarkdownRenderer content={'See `run/checkin.sh`.'} onFileOpen={vi.fn()} />,
    )
    const chip = await waitFor(() => {
      const c = container.querySelector('code[data-path-kind="file"]')
      expect(c).not.toBeNull()
      return c as HTMLElement
    })
    expect(chip.getAttribute('data-path')).toBe('run/checkin.sh')
    expect(probedPaths(fetchMock)).toEqual(['run/checkin.sh'])
  })
})

describe('MarkdownRenderer relative diff headers', () => {
  const FENCE = '```diff\n--- a/run/checkin.sh\n+++ b/run/checkin.sh\n@@ -1 +1 @@\n-a\n+b\n```'

  it('probes and opens a relative diff header inside the chat project', async () => {
    const fetchMock = stubPathKinds(p => (p === ABS ? 'file' : null))
    const onFileOpen = vi.fn()
    const { getByRole } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={FENCE} onFileOpen={onFileOpen} />
      </ChatProjectDirCtx.Provider>,
    )
    const open = await waitFor(() => getByRole('button', { name: `Open ${ABS} in side panel` }))
    expect(probedPaths(fetchMock)).toEqual([ABS])
    fireEvent.click(open)
    expect(onFileOpen).toHaveBeenCalledWith(ABS)
  })

  it('offers no Open for a header that climbs out of the project', async () => {
    const fetchMock = stubPathKinds(() => 'file')
    const { container } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'```diff\n--- a/../x.sh\n+++ b/../x.sh\n@@ -1 +1 @@\n-a\n+b\n```\n\nSee `run/checkin.sh`.'} onFileOpen={vi.fn()} />
      </ChatProjectDirCtx.Provider>,
    )
    await waitFor(() => {
      expect(container.querySelectorAll('code[data-path-kind="file"]')).toHaveLength(1)
    })
    expect(probedPaths(fetchMock)).toEqual([ABS])
  })
})

describe('MarkdownRenderer relative and home path links', () => {
  it('opens a relative link target inside the chat project', async () => {
    const fetchMock = stubPathKinds(p => (p === ABS ? 'file' : null))
    const onFileOpen = vi.fn()
    const { getByRole } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'See [the script](run/checkin.sh).'} onFileOpen={onFileOpen} />
      </ChatProjectDirCtx.Provider>,
    )
    const link = getByRole('link', { name: 'the script' })
    await waitFor(() => expect(probedPaths(fetchMock)).toEqual([ABS]))
    // Wait for the probe's answer to land before the click is judged.
    await waitFor(() => {
      const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
      link.dispatchEvent(ev)
      expect(onFileOpen).toHaveBeenCalledWith(ABS)
    })
  })

  it('opens a home-relative link target', async () => {
    stubPathKinds(p => (p === '~/notes.md' ? 'file' : null))
    const onFileOpen = vi.fn()
    const { getByRole } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'See [notes](~/notes.md).'} onFileOpen={onFileOpen} />
      </ChatProjectDirCtx.Provider>,
    )
    const link = getByRole('link', { name: 'notes' })
    await waitFor(() => {
      link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }))
      expect(onFileOpen).toHaveBeenCalledWith('~/notes.md')
    })
  })

  it('does not probe a relative link outside a chat transcript', async () => {
    // A previewed document's relative link is relative to that document, which
    // nothing here joins to, so it must not be probed against the gateway's
    // directory. A `~/` link names the home on any host and still resolves.
    const fetchMock = stubPathKinds(() => 'file')
    const onFileOpen = vi.fn()
    const { getByRole } = render(
      <MarkdownRenderer content={'See [log](./CHANGELOG.md) and [notes](~/notes.md).'} onFileOpen={onFileOpen} />,
    )
    const notes = getByRole('link', { name: 'notes' })
    await waitFor(() => {
      notes.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }))
      expect(onFileOpen).toHaveBeenCalledWith('~/notes.md')
    })
    expect(probedPaths(fetchMock)).toEqual(['~/notes.md'])
    const log = getByRole('link', { name: 'log' })
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    log.dispatchEvent(ev)
    expect(onFileOpen).toHaveBeenCalledTimes(1)
  })

  it('leaves an unconfirmed relative link an ordinary link', async () => {
    stubPathKinds(() => null)
    const onFileOpen = vi.fn()
    const { getByRole } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'See [docs](notes/guide.md) and [route](settings).'} onFileOpen={onFileOpen} />
      </ChatProjectDirCtx.Provider>,
    )
    const link = getByRole('link', { name: 'docs' })
    await waitFor(() => expect(link.getAttribute('href')).toBe('notes/guide.md'))
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    link.dispatchEvent(ev)
    expect(onFileOpen).not.toHaveBeenCalled()
    expect(getByRole('link', { name: 'route' }).getAttribute('href')).toBe('settings')
  })
})
