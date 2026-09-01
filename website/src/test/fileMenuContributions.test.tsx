/**
 * contributes.fileMenuItems — the declarative file-menu contribution point.
 *
 * Four things are pinned here, because each one is a place a contributed row has
 * already been able to disappear or misbehave without a test noticing:
 *
 *  - the RESOLVER, which is the only thing standing between an untrusted manifest and
 *    three host menus (enabled-only, caps, endpoint allowlist, skip-with-warn);
 *  - the `when` predicate that drives visibility on all three surfaces;
 *  - the per-surface FILTER, so a row declared for one menu cannot appear in another;
 *  - the RENDER of each surface, including the empty cases -- "the registry is empty so
 *    the build is inert" is the claim this seam makes, and it is only true if something
 *    asserts the menus render nothing.
 */
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const invokeApi = vi.fn().mockResolvedValue({})
const listApps = vi.fn()
vi.mock('../api/client', () => ({
  api: {
    invokeFileMenuItem: (...a: unknown[]) => invokeApi(...a),
    listApps: (...a: unknown[]) => listApps(...a),
  },
}))
// AppIcon pulls in theme/dompurify; icon glyph resolution is not under test here.
vi.mock('../components/AppIcon', () => ({ default: () => null }))

import {
  contributedFileMenuItems,
  fileMenuItemMatches,
  visibleFileMenuItems,
  useFileMenuItems,
  FolderRowActions,
  type ContributedFileMenuItem,
  type FileMenuAppRecord,
} from '../apps/fileMenuContributions'

function item(over: Partial<ContributedFileMenuItem> = {}): ContributedFileMenuItem {
  return {
    id: 'send',
    app: 'doc-store',
    label: 'Send to store',
    icon: 'Package',
    endpoint: '/api/apps/doc-store/send',
    surfaces: ['folder-row'],
    when: { extensions: [], kinds: [] },
    ...over,
  }
}

const DECL = {
  id: 'send',
  label: 'Send to store',
  icon: 'Package',
  endpoint: '/api/apps/doc-store/send',
  surfaces: ['file-overflow', 'tree-context', 'folder-row'],
}

function app(over: Record<string, unknown> = {}, decls: unknown = [DECL]): FileMenuAppRecord {
  return {
    name: 'doc-store',
    enabled: true,
    manifest: { contributes: { fileMenuItems: decls } },
    ...over,
  } as FileMenuAppRecord
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

describe('contributedFileMenuItems — resolving untrusted manifest data', () => {
  it('is empty for no apps, so a stock build contributes nothing', () => {
    expect(contributedFileMenuItems([])).toEqual([])
  })

  it('reads a well-formed declaration', () => {
    const [row] = contributedFileMenuItems([app()])
    expect(row).toMatchObject({ id: 'send', app: 'doc-store', label: 'Send to store' })
  })

  it('ignores a DISABLED app — the enable switch would otherwise be a lie', () => {
    expect(contributedFileMenuItems([app({ enabled: false })])).toEqual([])
  })

  it('skips a row whose endpoint escapes the app namespace', () => {
    for (const endpoint of [
      '/api/shutdown',
      '/api/apps/other/send',
      '/api/apps/doc-store-evil/send',
      '/api/apps/doc-store/../../shutdown',
      '/api/apps/doc-store/%2e%2e/%2e%2e/shutdown',
    ]) {
      expect(contributedFileMenuItems([app({}, [{ ...DECL, endpoint }])])).toEqual([])
    }
  })

  it('skips malformed rows without throwing, so one bad app cannot break the menus', () => {
    expect(contributedFileMenuItems([app({}, 'not-an-array')])).toEqual([])
    expect(contributedFileMenuItems([app({}, [null, 3, 'x'])])).toEqual([])
    expect(contributedFileMenuItems([app({}, [{ ...DECL, id: 'Bad_Id' }])])).toEqual([])
    expect(contributedFileMenuItems([app({}, [{ ...DECL, label: '' }])])).toEqual([])
    expect(contributedFileMenuItems([app({}, [{ ...DECL, label: 'x'.repeat(121) }])])).toEqual([])
    expect(contributedFileMenuItems([app({}, [{ ...DECL, surfaces: 'file-overflow' }])])).toEqual([])
    expect(contributedFileMenuItems([app({}, [{ ...DECL, surfaces: ['nope'] }])])).toEqual([])
    expect(contributedFileMenuItems([app({ name: '' })])).toEqual([])
  })

  it('caps rows per app, mirroring the manifest so neither side truncates alone', () => {
    const many = Array.from({ length: 15 }, (_, n) => ({ ...DECL, id: `row-${n}` }))
    expect(contributedFileMenuItems([app({}, many)])).toHaveLength(10)
  })

  it('drops a duplicate id within one app', () => {
    expect(contributedFileMenuItems([app({}, [DECL, { ...DECL }])])).toHaveLength(1)
  })

  it('normalizes when.extensions and ignores a non-array when field', () => {
    const [row] = contributedFileMenuItems([
      app({}, [{ ...DECL, when: { extensions: ['.MD', 'Py'], kinds: ['file'] } }]),
    ])
    expect(row.when).toEqual({ extensions: ['md', 'py'], kinds: ['file'] })
    const [loose] = contributedFileMenuItems([app({}, [{ ...DECL, when: { extensions: 'md' } }])])
    expect(loose.when).toEqual({ extensions: [], kinds: [] })
  })

  it('lets two apps use the same row id', () => {
    const rows = contributedFileMenuItems([
      app(),
      app({ name: 'other' }, [{ ...DECL, endpoint: '/api/apps/other/send' }]),
    ])
    expect(rows.map(r => `${r.app}:${r.id}`)).toEqual(['doc-store:send', 'other:send'])
  })
})

describe('useFileMenuItems — per-surface filter, without fetching', () => {
  const twoSurfaces = [
    { ...DECL, id: 'only-overflow', surfaces: ['file-overflow'] },
    { ...DECL, id: 'only-tree', surfaces: ['tree-context'] },
  ]

  function renderHookWith(surface: 'file-overflow' | 'tree-context' | 'folder-row', decls: unknown) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(['apps'], [app({}, decls)])
    const seen: string[][] = []
    function Probe() {
      seen.push(useFileMenuItems(surface).map(r => r.id))
      return null
    }
    render(
      <QueryClientProvider client={qc}>
        <Probe />
      </QueryClientProvider>,
    )
    return seen.at(-1)!
  }

  it('returns only rows declaring the requested surface', () => {
    expect(renderHookWith('file-overflow', twoSurfaces)).toEqual(['only-overflow'])
    expect(renderHookWith('tree-context', twoSurfaces)).toEqual(['only-tree'])
    expect(renderHookWith('folder-row', twoSurfaces)).toEqual([])
  })

  it('never issues a request — the rows ride on the existing apps query', () => {
    renderHookWith('file-overflow', [DECL])
    expect(listApps).not.toHaveBeenCalled()
  })

  it('is empty when the apps cache is cold, so the menus stay inert', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    let rows: ContributedFileMenuItem[] = [item()]
    function Probe() {
      rows = useFileMenuItems('file-overflow')
      return null
    }
    render(
      <QueryClientProvider client={qc}>
        <Probe />
      </QueryClientProvider>,
    )
    expect(rows).toEqual([])
  })
})

describe('fileMenuItemMatches — declarative when predicate', () => {
  it('admits any node when when is empty', () => {
    expect(fileMenuItemMatches(item(), { path: 'a/b.md', kind: 'file' })).toBe(true)
    expect(fileMenuItemMatches(item(), { path: 'a/dir', kind: 'dir' })).toBe(true)
  })

  it('filters by kind', () => {
    const row = item({ when: { extensions: [], kinds: ['file'] } })
    expect(fileMenuItemMatches(row, { path: 'a/b.md', kind: 'file' })).toBe(true)
    expect(fileMenuItemMatches(row, { path: 'a/dir', kind: 'dir' })).toBe(false)
  })

  it('filters by extension; a dotless name and a dotfile have none', () => {
    const row = item({ when: { extensions: ['md'], kinds: [] } })
    expect(fileMenuItemMatches(row, { path: 'a/b.md', kind: 'file' })).toBe(true)
    expect(fileMenuItemMatches(row, { path: 'a/b.MD', kind: 'file' })).toBe(true)
    expect(fileMenuItemMatches(row, { path: 'a/b.py', kind: 'file' })).toBe(false)
    expect(fileMenuItemMatches(row, { path: 'Makefile', kind: 'file' })).toBe(false)
    expect(fileMenuItemMatches(row, { path: 'a/.md', kind: 'file' })).toBe(false)
    // A dot in a PARENT directory is not the file's extension.
    expect(fileMenuItemMatches(row, { path: 'a.md/notes', kind: 'file' })).toBe(false)
  })

  it('ANDs kind and extension', () => {
    const row = item({ when: { extensions: ['md'], kinds: ['file'] } })
    expect(fileMenuItemMatches(row, { path: 'a/b.md', kind: 'file' })).toBe(true)
    expect(fileMenuItemMatches(row, { path: 'a/b.md', kind: 'dir' })).toBe(false)
  })

  it('visibleFileMenuItems drops non-matching rows', () => {
    const items = [
      item({ id: 'a', when: { extensions: ['md'], kinds: [] } }),
      item({ id: 'b', when: { extensions: ['py'], kinds: [] } }),
    ]
    expect(visibleFileMenuItems(items, { path: 'x.md', kind: 'file' }).map(i => i.id)).toEqual(['a'])
  })
})

describe('FolderRowActions — folder-row surface', () => {
  it('renders nothing when no row matches', () => {
    const { container } = render(<FolderRowActions items={[]} node={{ path: 'x', kind: 'file' }} />)
    expect(container.querySelector('button')).toBeNull()
  })

  it('renders one button per row and POSTs the PATH, never the content', () => {
    const rowActivate = vi.fn()
    // The parent row's handler is a REACT onClick on a wrapping div, not a native
    // `addEventListener` on the container: React delegates from its own root, so a
    // native container listener sits BELOW that root and the button's synthetic
    // `stopPropagation()` cannot reach it — the assertion would pass whether or not
    // the click was actually stopped. A plain div carries no `role="button"`, so
    // what `getAllByRole('button')` counts is unchanged.
    render(
      <div onClick={rowActivate}>
        <FolderRowActions
          items={[item(), item({ id: 'second', label: 'Second' })]}
          node={{ path: 'notes/x.md', kind: 'file' }}
        />
      </div>,
    )
    expect(screen.getAllByRole('button')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Send to store' }))
    expect(invokeApi).toHaveBeenCalledWith(expect.objectContaining({ id: 'send' }), {
      surface: 'folder-row',
      path: 'notes/x.md',
      kind: 'file',
    })
    // The dispatched context carries no file content.
    expect(invokeApi.mock.calls[0][1]).not.toHaveProperty('content')
    // Click is stopped, so the row's own onActivate never fires.
    expect(rowActivate).not.toHaveBeenCalled()
  })
})
