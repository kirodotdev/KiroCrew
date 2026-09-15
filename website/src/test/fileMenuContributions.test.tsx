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
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it, vi, beforeEach } from 'vitest'

// The folder-row surface collapses its rows behind a DropdownMenu; the shared stub makes
// the content render on a plain trigger click instead of Radix's pointer-event gating.
vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

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
// The dispatcher reads the owning slot from the store at dispatch time; a mutable
// binding lets a test set it per case without rendering a Provider.
let activeSlot: string | undefined = 'slot-7'
vi.mock('../store', () => ({
  store: { getState: () => ({ chat: { activeSlot } }) },
}))

import {
  contributedFileMenuItems,
  fileMenuItemMatches,
  visibleFileMenuItems,
  useFileMenuItems,
  invokeFileMenuItem,
  FolderRowActions,
  FileMenuItemLabel,
  CORE_APP_ROUTE_SEGMENTS,
  type ContributedFileMenuItem,
  type FileMenuAppRecord,
} from '../apps/fileMenuContributions'
import { RESERVED_APP_PATH_SEGMENTS } from '../apps/coreAppRoutes'

function item(over: Partial<ContributedFileMenuItem> = {}): ContributedFileMenuItem {
  return {
    id: 'send',
    app: 'doc-store',
    // The resolver always sets this; a hand-built fixture must too, or the row renders
    // its host-owned attribution around an empty identifier.
    appLabel: 'doc-store',
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

  it('returns [] for a NON-ARRAY apps payload instead of throwing', () => {
    // This resolver reads the shared `['apps']` cache, whose value is whatever another
    // observer's fetch put there, and `normalizeInstalledApps` passes a non-array
    // payload straight through. It runs inside a `useMemo` during render, so iterating
    // one would throw `apps is not iterable` and take the host page down -- on a menu
    // the reader merely opened. Both sibling resolvers on that key guard the same shape.
    for (const bad of [{ apps: [] }, {}, 'x', 0, true] as unknown[]) {
      expect(contributedFileMenuItems(bad as never), JSON.stringify(bad)).toEqual([])
    }
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

  it('skips a row naming a CORE route inside the app own namespace', () => {
    // The prefix test alone is not enough: core mounts the lifecycle handlers in the
    // app's own namespace, so `/api/apps/doc-store/uninstall` would POST to core's
    // uninstall with the reader's session on a row they thought belonged to the app.
    for (const endpoint of [
      '/api/apps/doc-store/uninstall',
      '/api/apps/doc-store/uninstall/preview',
      '/api/apps/doc-store/uninstall?confirm=1',
      '/api/apps/doc-store/uninstall#x',
      // A `.` segment (and a doubled slash) is resolved away by the URL parser before
      // the request is routed, so it must not read as an app route here either.
      '/api/apps/doc-store/./uninstall',
      '/api/apps/doc-store//uninstall',
      '/api/apps/doc-store/disable',
      '/api/apps/doc-store/token',
      '/api/apps/doc-store/_jobs/active',
    ]) {
      expect(contributedFileMenuItems([app({}, [{ ...DECL, endpoint }])])).toEqual([])
    }
  })

  it('skips a row hiding a CORE route behind a control character', () => {
    // The browser's URL parser STRIPS U+0009/000A/000D as `fetch` builds the request, so
    // `'uninstall\n'` is not the reserved `'uninstall'` to a naive segment test and the
    // POST still lands on core's uninstall handler with the reader's session. Every C0
    // control, DEL and space is refused, not only the three stripped today.
    for (const ch of ['\t', '\n', '\r', '\u0000', '\u000b', '\u001f', '\u007f', ' ']) {
      for (const endpoint of [
        `/api/apps/doc-store/uninstall${ch}`,
        `/api/apps/doc-store/${ch}uninstall`,
        `/api/apps/doc-store/send${ch}`,
      ]) {
        expect(contributedFileMenuItems([app({}, [{ ...DECL, endpoint }])]), endpoint).toEqual([])
      }
    }
    // …and percent-encoded, which is why the check runs after `decodeURIComponent`.
    for (const endpoint of ['/api/apps/doc-store/uninstall%0a', '/api/apps/doc-store/uninstall%09']) {
      expect(contributedFileMenuItems([app({}, [{ ...DECL, endpoint }])]), endpoint).toEqual([])
    }
  })

  it('skips a row hiding a CORE route behind a backslash', () => {
    // `\` is a PATH SEPARATOR to the URL parser for http(s) but not to a naive segment
    // split, so `/api/apps/doc-store/.\uninstall` reads as one opaque segment here and
    // the browser then normalizes it onto core's uninstall route.
    for (const endpoint of [
      '/api/apps/doc-store/.\\uninstall',
      '/api/apps/doc-store/.\\token',
      '/api/apps/doc-store\\uninstall',
      '/api/apps/doc-store/x\\..\\uninstall',
      '/api/apps/doc-store/%5cuninstall',
      '/api/apps/doc-store/%2e%5cuninstall',
    ]) {
      expect(contributedFileMenuItems([app({}, [{ ...DECL, endpoint }])]), endpoint).toEqual([])
    }
  })

  it('admits a real endpoint and refuses characters nobody enumerated', () => {
    // The validator is a character ALLOWLIST, mirroring the Python side: the refusals
    // above then hold for a character a future parser rewrites without anyone adding it.
    for (const endpoint of [
      '/api/apps/doc-store/send-item',
      '/api/apps/doc-store/send_item',
      '/api/apps/doc-store/v1.2/send',
      '/api/apps/doc-store/send?path=/a/b&kind=file',
    ]) {
      expect(contributedFileMenuItems([app({}, [{ ...DECL, endpoint }])]), endpoint).toHaveLength(1)
    }
    for (const endpoint of [
      '/api/apps/doc-store/send"x',
      '/api/apps/doc-store/send<x',
      '/api/apps/doc-store/send|x',
      '/api/apps/doc-store/send{x}',
      '/api/apps/doc-store/send^x',
      '/api/apps/doc-store/send\u00a0x',
      '/api/apps/doc-store/send\u2028x',
    ]) {
      expect(contributedFileMenuItems([app({}, [{ ...DECL, endpoint }])]), endpoint).toEqual([])
    }
  })

  it('attributes every contributed row to the app that owns it', () => {
    // `label` is app-owned and never checked against the core vocabulary, so an app can
    // name its row exactly like a core action and sit one separator below the real one.
    // The attribution is what lets a reader see that clicking it hands the file's path to
    // a third party, so it must be present, non-empty, and never the label alone.
    //
    // It comes from `name`, NEVER `displayName`: `displayName` is free text the app
    // chooses, so as provenance it can claim to be anything the host is called.
    const rows = contributedFileMenuItems([
      { name: 'doc-store', displayName: 'Kiro Crew', enabled: true, manifest: { contributes: { fileMenuItems: [{ ...DECL, label: 'Download' }] } } },
    ])
    expect(rows).toHaveLength(1)
    expect(rows[0].appLabel).toBe('doc-store')
    expect(rows[0].appLabel).not.toBe('Kiro Crew')

    // Every shape of displayName is ignored, including the ones that used to win: a
    // friendly name, whitespace, and a zero-width character that survives `.trim()`.
    for (const displayName of ['Doc Store', '   ', '\u200b', '\u034f', undefined]) {
      expect(contributedFileMenuItems([app({ displayName })])[0].appLabel).toBe('doc-store')
    }

    // An over-long NAME clips instead of dropping the row: `KEBAB_RE` bounds the
    // alphabet and not the length, so refusing here would drop every row of an app
    // whose own fields were all fine.
    const long = contributedFileMenuItems([
      { name: 'd'.repeat(500), enabled: true, manifest: { contributes: { fileMenuItems: [{ ...DECL, endpoint: `/api/apps/${'d'.repeat(500)}/send` }] } } },
    ])
    expect(long).toHaveLength(1)
    expect(long[0].appLabel.length).toBeGreaterThan(0)
    expect(long[0].appLabel.length).toBeLessThan(500)
  })

  it('still admits an app route whose name merely STARTS with a reserved word', () => {
    // The reservation is per SEGMENT. Refusing a prefix would take routes the app owns.
    const rows = contributedFileMenuItems([
      app({}, [{ ...DECL, endpoint: '/api/apps/doc-store/uninstall-helper' }]),
    ])
    expect(rows).toHaveLength(1)
  })

  it('mirrors the reserved-segment list the Python allowlist owns', () => {
    // Two copies of one security control drift apart invisibly, and the drift only shows
    // up as something being let through. Read the owning module rather than restating it.
    const py = readFileSync(
      resolve(__dirname, '../../../src/kiro_crew/apps/manifest.py'),
      'utf8',
    )
    const block = /CORE_APP_ROUTE_SEGMENTS = frozenset\(\s*\{([^}]*)\}/.exec(py)
    expect(block, 'CORE_APP_ROUTE_SEGMENTS not found in apps/manifest.py').not.toBeNull()
    const pythonSegments = [...block![1].matchAll(/"([^"]+)"/g)].map(m => m[1])
    expect(pythonSegments.length).toBeGreaterThan(0)
    expect([...CORE_APP_ROUTE_SEGMENTS].sort()).toEqual(pythonSegments.sort())
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
  const onError = vi.fn()

  /**
   * The parent row's handler is a REACT onClick on a wrapping div, not a native
   * `addEventListener` on the container: React delegates from its own root, so a native
   * container listener sits BELOW that root and a synthetic `stopPropagation()` cannot
   * reach it — the assertion would pass whether or not the click was actually stopped.
   * `role="presentation"` keeps the wrapper out of the accessibility tree, so what
   * `getAllByRole('button')` counts is unchanged; the real row (FolderPanel's `Row`)
   * carries `role="button"` and its own Enter/Space handler, which this probe stands in
   * for rather than reproduces.
   */
  function renderRow(items: ContributedFileMenuItem[]) {
    const rowActivate = vi.fn()
    render(
      <div role="presentation" onClick={rowActivate}>
        <FolderRowActions items={items} node={{ path: 'notes/x.md', kind: 'file' }} onError={onError} />
      </div>,
    )
    return { rowActivate }
  }

  it('renders nothing when no row matches', () => {
    const { container } = render(
      <FolderRowActions items={[]} node={{ path: 'x', kind: 'file' }} onError={onError} />,
    )
    expect(container.querySelector('button')).toBeNull()
  })

  it('collapses every contributed row behind ONE trigger, whatever the count', () => {
    // `max-two-buttons-per-row`: the count is an app's to choose, so three declaring
    // apps would otherwise put three unranked icon buttons in a listing row.
    renderRow([item(), item({ id: 'second' }), item({ id: 'third' })])
    expect(screen.getAllByRole('button')).toHaveLength(1)
    expect(screen.queryByRole('menuitem')).toBeNull()
  })

  it('opens the menu and POSTs the PATH, never the content', () => {
    const { rowActivate } = renderRow([item(), item({ id: 'second', label: 'Second' })])
    fireEvent.click(screen.getByRole('button'))
    expect(screen.getAllByRole('menuitem')).toHaveLength(2)

    fireEvent.click(screen.getByRole('menuitem', { name: /^Send to store\b/ }))
    expect(invokeApi).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'send' }),
      { surface: 'folder-row', path: 'notes/x.md', kind: 'file' },
      'dashboard:slot-7',
    )
    // The dispatched context carries no file content.
    expect(invokeApi.mock.calls[0][1]).not.toHaveProperty('content')
    // Opening and selecting are stopped, so the row's own onActivate never fires.
    expect(rowActivate).not.toHaveBeenCalled()
  })

  it('sends the OWNING SLOT as the session key, not the dashboard:ui placeholder', () => {
    // `post`'s shared `dashboard:ui` default satisfies the server's `if sk:` gate but
    // names no actual session, so a restricted (incognito) slot would not be recognised
    // as restricted and the row's write would be allowed through. The slot is read at
    // dispatch time, which is why it is asserted on the CALL rather than on a prop.
    activeSlot = 'incognito-3'
    const report = vi.fn()
    invokeFileMenuItem(item(), { surface: 'folder-row', path: 'x', kind: 'file' }, report)
    expect(invokeApi).toHaveBeenCalledWith(
      expect.anything(),
      expect.anything(),
      'dashboard:incognito-3',
    )
    expect(invokeApi.mock.calls[0][2]).not.toBe('dashboard:ui')
  })

  it('omits the session key entirely when no slot is active', () => {
    // Not `dashboard:` with an empty tail, which would name a session that cannot
    // exist; omitting it lets the transport apply its own documented default.
    activeSlot = undefined
    invokeFileMenuItem(item(), { surface: 'folder-row', path: 'x', kind: 'file' }, vi.fn())
    expect(invokeApi.mock.calls[0][2]).toBeUndefined()
  })

  it('refuses a reserved app name, which is a shared core route rather than a namespace', () => {
    // `/api/apps/registry/install` and `/api/apps/registries/refresh` are literal routes
    // mounted before the `/api/apps/{name}` catch-all, and `install`/`refresh` are not
    // per-app lifecycle segments, so CORE_APP_ROUTE_SEGMENTS does not cover them.
    for (const name of RESERVED_APP_PATH_SEGMENTS) {
      for (const tail of ['install', 'refresh', 'send']) {
        expect(contributedFileMenuItems([
          app({ name }, [{ ...DECL, endpoint: `/api/apps/${name}/${tail}` }]),
        ])).toEqual([])
      }
    }
  })

  it('leaves an app whose name merely contains a reserved word alone', () => {
    expect(contributedFileMenuItems([
      app({ name: 'my-registry' }, [{ ...DECL, endpoint: '/api/apps/my-registry/send' }]),
    ])).toHaveLength(1)
  })

  it('attributes a row to the app name when displayName is invisible', () => {
    // Not a unit re-test of `attributionLabel`: this pins that the CONSUMER routes
    // through it, since the row is what a reader sees and a blank attribution here is
    // indistinguishable from a builtin row.
    for (const displayName of ['   ', '\u200b', '\ufeff', '\u3164']) {
      const rows = contributedFileMenuItems([app({ displayName })])
      expect(rows).toHaveLength(1)
      expect(rows[0].appLabel).toBe('doc-store')
    }
  })

  it('frames the identifier in host-owned copy so a plausible app name cannot pass as core', () => {
    // The identifier alone is unspoofable as a host WORD (`KEBAB_RE` allows no space and
    // no capital), but nothing reserves `kiro-crew`, and a bare slug still reads as core.
    // The surrounding word comes from the catalog, so it is core's and translated.
    const rows = contributedFileMenuItems([
      { name: 'kiro-crew', displayName: 'Kiro Crew', enabled: true, manifest: { contributes: { fileMenuItems: [{ ...DECL, endpoint: '/api/apps/kiro-crew/send' }] } } },
    ])
    expect(rows).toHaveLength(1)
    render(<>{FileMenuItemLabel({ item: rows[0] })}</>)
    // The attribution is announced, not decorative: it is the security-relevant part.
    expect(screen.getByText(/kiro-crew/)).toBeInTheDocument()
    // And it is framed rather than bare, so the row does not read as a core action.
    expect(screen.getByText(/kiro-crew/).textContent).not.toBe('kiro-crew')
  })

  it('reports a rejected dispatch to the host instead of the console', async () => {
    // `errors-use-error-notice`: the menu closes on select, so a console-only failure
    // leaves the reader with a row that silently did nothing.
    invokeApi.mockRejectedValueOnce(new Error('endpoint refused'))
    renderRow([item()])
    fireEvent.click(screen.getByRole('button'))
    fireEvent.click(screen.getByRole('menuitem', { name: /^Send to store\b/ }))
    await vi.waitFor(() => expect(onError).toHaveBeenCalledWith('endpoint refused'))
  })

  it('reports a non-Error rejection rather than an empty notice', async () => {
    invokeApi.mockRejectedValueOnce('boom')
    const report = vi.fn()
    invokeFileMenuItem(item(), { surface: 'folder-row', path: 'x', kind: 'file' }, report)
    await vi.waitFor(() => expect(report).toHaveBeenCalledWith('boom'))
  })
})
