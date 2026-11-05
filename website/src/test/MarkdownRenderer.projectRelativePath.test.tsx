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
import { isFileLinkDest, joinProjectRelative } from '../components/markdown/pathReferences'
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

describe('isFileLinkDest', () => {
  it('reads a rooted path with an extension as a file', () => {
    expect(isFileLinkDest('/tmp/x.md')).toBe(true)
    expect(isFileLinkDest('/abs/notes.md')).toBe(true)
    expect(isFileLinkDest('/var/log/app.log')).toBe(true)
  })

  it('reads a relative or home-rooted path as a file (never a route)', () => {
    expect(isFileLinkDest('notes/guide.md')).toBe(true)
    expect(isFileLinkDest('src/a.ts')).toBe(true)
    expect(isFileLinkDest('~/notes.md')).toBe(true)
    expect(isFileLinkDest('./run/check.sh')).toBe(true)
  })

  it('leaves a rooted, extension-less destination a route', () => {
    expect(isFileLinkDest('/schedule')).toBe(false)
    expect(isFileLinkDest('/settings')).toBe(false)
    expect(isFileLinkDest('/settings/security/rules')).toBe(false)
    // An extension-less rooted directory is indistinguishable from a route by
    // shape, so it stays a route rather than risk muting a real one.
    expect(isFileLinkDest('/var/log/app')).toBe(false)
  })

  it('leaves a /api/ gateway endpoint a route even with a file extension', () => {
    // A download route such as `/api/outbox/report.txt` carries a `.txt` tail
    // but names a gateway endpoint, not a disk file, so it must keep navigating.
    expect(isFileLinkDest('/api/outbox/report.txt')).toBe(false)
    expect(isFileLinkDest('/api/file-read?path=/x.md')).toBe(false)
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

describe('MarkdownRenderer missing file links', () => {
  // A path-shaped link whose destination reads as a file and whose probe says
  // `missing` reads and behaves like the unconfirmed inline backtick chip: it is
  // muted, and a plain click is swallowed rather than following the local-path
  // href to a new browser tab at the dashboard origin -- a URL that names
  // nothing. The same path in backticks is a neutral copy chip, so a navigating
  // link here would be the one surface that leads the reader to a dead tab.
  it('does not open a dead tab for an absolute missing file link', async () => {
    const fetchMock = stubPathKinds(() => null)
    const onFileOpen = vi.fn()
    const { getByRole } = render(
      <MarkdownRenderer content={'See [the note](/tmp/x.md).'} onFileOpen={onFileOpen} />,
    )
    const link = getByRole('link', { name: 'the note' })
    // The probe runs for a shaped candidate; the muted class is the signal the
    // answer landed and the link was demoted.
    await waitFor(() => expect(link.getAttribute('class')).toContain('text-muted'))
    expect(probedPaths(fetchMock)).toEqual(['/tmp/x.md'])
    // No new-tab affordance, and a plain click is cancelled.
    expect(link.getAttribute('target')).toBeNull()
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    link.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(onFileOpen).not.toHaveBeenCalled()
    // The href is kept, so a modified click still reaches the browser.
    expect(link.getAttribute('href')).toBe('/tmp/x.md')
  })

  it('does not open a dead tab for a relative missing file link joined to the chat project', async () => {
    const fetchMock = stubPathKinds(() => null)
    const { getByRole } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'See [the note](notes/guide.md).'} onFileOpen={vi.fn()} />
      </ChatProjectDirCtx.Provider>,
    )
    const link = getByRole('link', { name: 'the note' })
    await waitFor(() => expect(link.getAttribute('class')).toContain('text-muted'))
    expect(probedPaths(fetchMock)).toEqual([`${PROJECT}/notes/guide.md`])
    expect(link.getAttribute('target')).toBeNull()
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    link.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
  })

  it('does not open a dead tab for a home-relative missing file link', async () => {
    stubPathKinds(() => null)
    const { getByRole } = render(
      <ChatProjectDirCtx.Provider value={PROJECT}>
        <MarkdownRenderer content={'See [notes](~/notes.md).'} onFileOpen={vi.fn()} />
      </ChatProjectDirCtx.Provider>,
    )
    const link = getByRole('link', { name: 'notes' })
    await waitFor(() => expect(link.getAttribute('class')).toContain('text-muted'))
    expect(link.getAttribute('target')).toBeNull()
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    link.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
  })

  it('keeps a root-relative in-app route link working when the probe says missing', async () => {
    stubPathKinds(() => null)
    const { getByRole } = render(
      <MarkdownRenderer content={'Open [rules](/settings/security/rules).'} onFileOpen={vi.fn()} />,
    )
    const link = getByRole('link', { name: 'rules' })
    // A rooted, extension-less destination is a route, not a file, so it keeps
    // its accent dress and its ordinary navigation even while the probe answers
    // missing. Settle the probe first, then assert nothing was demoted.
    await waitFor(() => expect(link.getAttribute('class')).toContain('text-accent'))
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('href')).toBe('/settings/security/rules')
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    link.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(false)
  })

  it('keeps a /api/ download link navigating even when the probe says missing', async () => {
    // `[download](/api/outbox/report.txt)` points at a gateway download route,
    // not a disk file. Its `.txt` tail must not demote it: the plain click must
    // still reach the browser so the backend handler serves the file.
    stubPathKinds(() => null)
    const { getByRole } = render(
      <MarkdownRenderer content={'Grab [the report](/api/outbox/report.txt).'} onFileOpen={vi.fn()} />,
    )
    const link = getByRole('link', { name: 'the report' })
    await waitFor(() => expect(link.getAttribute('class')).toContain('text-accent'))
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('href')).toBe('/api/outbox/report.txt')
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 })
    link.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(false)
  })

  it('opens a confirmed file link, so the demotion is the missing verdict only', async () => {
    stubPathKinds(p => (p === '/tmp/x.md' ? 'file' : null))
    const onFileOpen = vi.fn()
    const { getByRole } = render(
      <MarkdownRenderer content={'See [the note](/tmp/x.md).'} onFileOpen={onFileOpen} />,
    )
    const link = getByRole('link', { name: 'the note' })
    await waitFor(() => {
      link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }))
      expect(onFileOpen).toHaveBeenCalledWith('/tmp/x.md')
    })
    expect(link.getAttribute('class')).not.toContain('text-muted')
  })
})
