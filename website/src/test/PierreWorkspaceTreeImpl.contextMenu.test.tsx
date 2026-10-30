/**
 * PierreWorkspaceTreeImpl — the row context menu: its triggers and placement,
 * Download, the keyboard contract and the app-contributed rows.
 *
 * One topic of the wrapper suite whose conventions
 * `PierreWorkspaceTreeImpl.test.tsx` describes; the shared fixtures live in
 * `PierreWorkspaceTreeImplTestUtils.tsx`, and the `vi.mock` block is the
 * suite's own, word for word.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { store } from '../store'

vi.mock('@pierre/trees/react', async () => await import('./__mocks__/pierreTreesReact'))

vi.mock('../api/client', () => ({
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
    // The contributed-row seam reads `['apps']` as a cache subscriber (never fetches)
    // and dispatches an activation through `invokeFileMenuItem`.
    listApps: vi.fn(),
    invokeFileMenuItem: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('../components/AppIcon', () => ({ default: () => null }))
import { api } from '../api/client'
import { treeMock } from './__mocks__/pierreTreesReact'
import type { MenuItem, MenuContext } from './__mocks__/pierreTreesReact'
import {
  ROOT,
  mkTree,
  mkStatus,
  renderTree,
  waitForTree,
} from './PierreWorkspaceTreeImplTestUtils'

beforeEach(() => {
  treeMock.reset()
  vi.mocked(api.projectTree).mockResolvedValue(mkTree())
  vi.mocked(api.projectGitStatus).mockResolvedValue(mkStatus([]))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('PierreWorkspaceTreeImpl — row context menu', () => {
  // Baseline composition the wrapper hands `useFileTree`: the `<FileTree>`
  // renderContextMenu wiring forces `enabled: true` on top of this, but the
  // trigger config here is what survives.
  it('enables both right-click and the hover button as context-menu triggers', async () => {
    renderTree()
    await waitForTree()
    expect(treeMock.last().options).toMatchObject({
      composition: { contextMenu: { triggerMode: 'both', buttonVisibility: 'when-needed' } },
    })
  })

  it('always wires renderContextMenu, since a file row always has Download', async () => {
    // A file row is downloadable on its own, so the menu is wired
    // unconditionally. The per-node component still renders nothing on a node
    // with no action (asserted in the Download suite).
    const { update } = renderTree()
    await waitForTree()
    expect(typeof treeMock.fileTreeProps.at(-1)!.renderContextMenu).toBe('function')

    update({ onAddToContext: vi.fn() })
    expect(typeof treeMock.fileTreeProps.at(-1)!.renderContextMenu).toBe('function')
  })

  const openMenu = (item: MenuItem, anchorRect?: Partial<DOMRect>, anchorElement?: HTMLElement) => {
    const close = vi.fn()
    const context: MenuContext = {
      anchorElement: anchorElement ?? document.createElement('div'),
      anchorRect: { ...document.createElement('div').getBoundingClientRect(), ...anchorRect } as DOMRect,
      close,
      restoreFocus: vi.fn(),
    }
    const node = treeMock.fileTreeProps.at(-1)!.renderContextMenu!(item, context)
    return { close, ...render(<>{node}</>) }
  }

  it('offers a single Add to chat action on a file, wired to the absolute path', async () => {
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    const { close } = openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })

    // Row click already opens a file, so the menu carries no Open duplicate.
    expect(screen.queryByRole('menuitem', { name: 'Open' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`, 'file')
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('focuses the first item on open so the keyboard path can activate it', async () => {
    // Pierre focuses the tree ROW when it opens the menu, never this slotted
    // content, so without an explicit focus a Shift+F10 user would sit on the
    // row: the item's own Enter/Space handler could never fire.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    const menuitem = screen.getByRole('menuitem', { name: 'Add to chat' })
    expect(menuitem).toHaveFocus()

    // And the focused item actually activates on Enter.
    fireEvent.keyDown(menuitem, { key: 'Enter' })
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`, 'file')
  })

  it('portals the menu to document.body, outside the clipping tree root (#10100)', async () => {
    // Pierre's default slot placement hangs the menu in a width-0 slot at the
    // row's trailing edge INSIDE the tree root, whose `overflow: hidden` plus
    // this app's zero inline padding clipped it to a sliver flush against the
    // panel's right border at every panel width -- an unreachable "..." menu.
    // The portal (marked with the library's documented
    // `data-file-tree-context-menu-root` attribute so outside-click and Escape
    // still treat it as inside) is what escapes that clipping boundary.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    const menu = screen.getByRole('menu')
    expect(menu.parentElement).toBe(document.body)
    expect(menu).toHaveAttribute('data-file-tree-context-menu-root', 'true')
    // Fixed positioning is what places it from the open context's anchorRect
    // instead of the slot's in-flow (clipped) position.
    expect(menu.className).toContain('fixed')
  })

  it("dismisses on a scroll inside the tree's own root, not on an outside scroll", async () => {
    // The portaled menu is position: fixed, so if the virtualized tree scrolls
    // under it the menu would hover an unrelated row while still acting on the
    // original node. The tree renders in a SHADOW ROOT and scroll is a
    // non-composed event, so the dismiss listener sits capture-phase on the
    // anchor's own root: it sees every scroll container inside the tree and
    // nothing outside it -- a chat transcript auto-scrolling beside the rail
    // must NOT snatch a just-opened menu.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    const host = document.createElement('div')
    document.body.appendChild(host)
    try {
      const shadow = host.attachShadow({ mode: 'open' })
      const scroller = document.createElement('div')
      const anchor = document.createElement('div')
      scroller.appendChild(anchor)
      shadow.appendChild(scroller)

      const { close } = openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' }, undefined, anchor)
      fireEvent.scroll(document.body) // outside the tree: keep the menu
      expect(close).not.toHaveBeenCalled()
      fireEvent.scroll(scroller) // the tree's own scroller: rows moved, dismiss
      expect(close).toHaveBeenCalledTimes(1)
    } finally {
      host.remove()
    }
  })

  it('clamps into the viewport and flips above when the bottom would overflow', async () => {
    // jsdom rects are zeros by default, so stub the menu measurement and hand
    // the open context a bottom-right anchor: the horizontal clamp and the
    // vertical flip are exactly the branches the clipped-slot defect was about.
    const rect = { width: 176, height: 200, top: 0, bottom: 200, left: 0, right: 176, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
    const spy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue(rect)
    try {
      const onAddToContext = vi.fn()
      renderTree({ onAddToContext })
      await waitForTree()

      // window.innerWidth = 1024, innerHeight = 768 in jsdom.
      openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' }, { width: 18, height: 28, left: 998, right: 1016, top: 700, bottom: 728 })
      const menu = screen.getByRole('menu')
      await waitFor(() => expect(menu.style.left).toBe('840px')) // 1016 - 176, at the 1024-176-8 clamp
      expect(menu.style.top).toBe('498px') // flipped above: 700 - 200 - 2
    } finally {
      spy.mockRestore()
    }
  })

  it('reports a directory as a dir add', async () => {
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    const { close } = openMenu({ kind: 'directory', name: 'a', path: 'src/a' })

    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a`, 'dir')
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('normalizes a native-Windows mixed-separator path to forward slashes', async () => {
    // On native Windows the tree root is backslash-separated while Pierre paths
    // are POSIX, so a raw join is `C:\repo\project/src/a/b.ts`. Left mixed, the
    // mention host's makeRelative cannot relativize it (absolute token +
    // duplicate attachment); the reported path must be forward-slash throughout.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ root: 'C:\\repo\\project' }))
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith('C:/repo/project/src/a/b.ts', 'file')
  })

  it('does not double a separator when the project root is a Windows drive root', async () => {
    // A drive-root project (`D:\`) normalizes to `D:/`, which ALREADY ends in
    // a separator. Left un-stripped, the join produces `D://item.path` -- a
    // double slash the mention host's makeRelative prefix-strip then consumes
    // only ONE of, leaving a stray leading `/` on the relativized result.
    vi.mocked(api.projectTree).mockResolvedValue(mkTree({ root: 'D:\\' }))
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith('D:/src/a/b.ts', 'file')
  })

  it('leaves a POSIX filename containing a backslash untouched', async () => {
    // `\` is a legal character in a POSIX filename. Only a Windows-shaped ROOT
    // is separator-normalized; the row's own path must pass through verbatim,
    // or the staged mention would point at a nonexistent nested path.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()

    openMenu({ kind: 'file', name: 'weird\\name.txt', path: 'src/weird\\name.txt' })
    fireEvent.click(screen.getByRole('menuitem', { name: 'Add to chat' }))
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/weird\\name.txt`, 'file')
  })
})

describe('PierreWorkspaceTreeImpl — row context menu Download', () => {
  // Download streams the file's bytes through /api/file-download — the SAME
  // endpoint the viewer's Download uses — then hands the blob to the browser as
  // a save-to-disk. These tests pin: the row is file-only, the fetch hits that
  // endpoint, a failure is surfaced, and the menu offers Download even with no
  // host.
  const openMenu = (item: MenuItem) => {
    const close = vi.fn()
    const context: MenuContext = {
      anchorElement: document.createElement('div'),
      anchorRect: document.createElement('div').getBoundingClientRect(),
      close,
      restoreFocus: vi.fn(),
    }
    const node = treeMock.fileTreeProps.at(-1)!.renderContextMenu!(item, context)
    return { close, ...render(<>{node}</>) }
  }

  /** Stub fetch, URL.createObjectURL/revoke and the anchor click so the
   *  download can be observed without a real network or DOM navigation. */
  function captureDownload(response: { ok: boolean; status?: number }) {
    const names: string[] = []
    const origFetch = global.fetch
    const origCreate = URL.createObjectURL
    const origRevoke = URL.revokeObjectURL
    const body = { error: 'refused' }
    const mkRes = () => ({
      ok: response.ok,
      status: response.status ?? (response.ok ? 200 : 400),
      statusText: response.ok ? 'OK' : 'Bad Request',
      blob: async () => new Blob(['bytes'], { type: 'application/octet-stream' }),
      json: async () => body,
      clone() { return mkRes() },
    })
    const fetchMock = vi.fn().mockImplementation(async () => mkRes())
    global.fetch = fetchMock as unknown as typeof fetch
    URL.createObjectURL = vi.fn(() => 'blob:dl') as typeof URL.createObjectURL
    URL.revokeObjectURL = vi.fn() as typeof URL.revokeObjectURL
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function mockClick(this: HTMLAnchorElement) { names.push(this.download) })
    return {
      fetchMock, names,
      restore: () => {
        clickSpy.mockRestore()
        global.fetch = origFetch
        URL.createObjectURL = origCreate
        URL.revokeObjectURL = origRevoke
      },
    }
  }

  it('offers Download on a file row and streams the bytes through /api/file-download', async () => {
    const cap = captureDownload({ ok: true })
    try {
      renderTree({ onAddToContext: vi.fn() })
      await waitForTree()

      openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
      fireEvent.click(screen.getByRole('menuitem', { name: 'Download' }))

      // The absolute path goes to /api/file-download; absolute paths
      // carry no resolve=1.
      await waitFor(() => expect(cap.fetchMock).toHaveBeenCalledTimes(1))
      expect(cap.fetchMock).toHaveBeenCalledWith(
        `/api/file-download?path=${encodeURIComponent(`${ROOT}/src/a/b.ts`)}`,
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      )
      // The saved file keeps its basename.
      await waitFor(() => expect(cap.names).toEqual(['b.ts']))
    } finally {
      cap.restore()
    }
  })

  it('offers no Download on a directory row', async () => {
    const cap = captureDownload({ ok: true })
    try {
      renderTree({ onAddToContext: vi.fn() })
      await waitForTree()

      openMenu({ kind: 'directory', name: 'a', path: 'src/a' })
      // The ask is file rows only, and /api/file-download serves one file.
      expect(screen.queryByRole('menuitem', { name: 'Download' })).not.toBeInTheDocument()
      expect(screen.getByRole('menuitem', { name: 'Add to chat' })).toBeInTheDocument()
    } finally {
      cap.restore()
    }
  })

  it('surfaces a 400 as the generic Download failed and saves no bytes', async () => {
    // /api/file-download answers 400 for an invalid or out-of-project path.
    const cap = captureDownload({ ok: false, status: 400 })
    try {
      renderTree({ onAddToContext: vi.fn() })
      await waitForTree()

      openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
      fireEvent.click(screen.getByRole('menuitem', { name: 'Download' }))

      const notice = await waitFor(() => screen.getByTestId('workspace-tree-action-error'))
      expect(notice).toHaveTextContent('Download failed')
      expect(cap.names).toEqual([])
    } finally {
      cap.restore()
    }
  })

  it('surfaces a non-400 failure as the generic Download failed', async () => {
    // Any other non-ok status (a 500, a gone file) is an ordinary failure the
    // user may retry.
    const cap = captureDownload({ ok: false, status: 500 })
    try {
      renderTree({ onAddToContext: vi.fn() })
      await waitForTree()

      openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
      fireEvent.click(screen.getByRole('menuitem', { name: 'Download' }))

      const notice = await waitFor(() => screen.getByTestId('workspace-tree-action-error'))
      expect(notice).toHaveTextContent('Download failed')
      expect(cap.names).toEqual([])
    } finally {
      cap.restore()
    }
  })

  it('offers Download on a file row even with no host onAddToContext', async () => {
    // A file row is downloadable on its own, so the menu is wired and focused
    // on Download with no Add-to-chat row above it to own focus entry.
    const cap = captureDownload({ ok: true })
    try {
      renderTree()
      await waitForTree()
      expect(typeof treeMock.fileTreeProps.at(-1)!.renderContextMenu).toBe('function')

      openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
      const row = screen.getByRole('menuitem', { name: 'Download' })
      expect(row).toHaveFocus()
      fireEvent.keyDown(row, { key: 'Enter' })

      await waitFor(() => expect(cap.fetchMock).toHaveBeenCalledTimes(1))
    } finally {
      cap.restore()
    }
  })

  it('shows Download as the ONLY row on a file with no host onAddToContext', async () => {
    // The reachable shape on a hostless render site (e.g. the Members DM file
    // tree, which mounts the panel with no onAddToContext): the file row menu
    // holds Download alone -- no Add to chat above it. This is shipped UI, not a
    // defensive dead path, so the single-row Download layout must render.
    const cap = captureDownload({ ok: true })
    try {
      renderTree()
      await waitForTree()

      openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
      const items = screen.getAllByRole('menuitem')
      expect(items).toHaveLength(1)
      expect(items[0]).toHaveTextContent('Download')
      expect(screen.queryByRole('menuitem', { name: 'Add to chat' })).not.toBeInTheDocument()
    } finally {
      cap.restore()
    }
  })

  it('renders no menu on a directory row with no host and no app rows', async () => {
    // The wiring is now unconditional, but a directory node with nothing to
    // offer must still render nothing rather than an empty popup.
    renderTree()
    await waitForTree()

    const { node } = (() => {
      const context: MenuContext = {
        anchorElement: document.createElement('div'),
        anchorRect: document.createElement('div').getBoundingClientRect(),
        close: vi.fn(),
        restoreFocus: vi.fn(),
      }
      return { node: treeMock.fileTreeProps.at(-1)!.renderContextMenu!({ kind: 'directory', name: 'a', path: 'src/a' }, context) }
    })()
    render(<>{node}</>)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
  })
})

describe('PierreWorkspaceTreeImpl — row context menu keyboard contract (#6231)', () => {
  // The DEGENERATE case of the shared `role="menu"` contract: this menu hosts
  // exactly ONE menuitem, so every focus-move assertion is vacuously true —
  // "focus lands on the next item" and "focus does not move" are the same
  // observation. What the contract still owes a keyboard user is CONSUMPTION:
  // an arrow inside an open menu must not scroll the page behind it, and a Tab
  // must not drop the user out of a menu they were just told is open (#2533).
  // These tests therefore assert on `fireEvent`'s return value — false means
  // `preventDefault()` was called, i.e. the contract claimed the key — which is
  // the only signal that distinguishes wired from unwired on a one-item menu.
  const openMenu = (item: MenuItem) => {
    const context: MenuContext = {
      anchorElement: document.createElement('div'),
      anchorRect: document.createElement('div').getBoundingClientRect(),
      close: vi.fn(),
      restoreFocus: vi.fn(),
    }
    const node = treeMock.fileTreeProps.at(-1)!.renderContextMenu!(item, context)
    return render(<>{node}</>)
  }

  /** Open the row menu and hand back its single item, focused (the
   *  component's own firstItemRef effect owns that focus entry). A DIRECTORY
   *  row is the one-item case: it carries Add to chat but no Download (which is
   *  file-only), so the degenerate single-menuitem contract still holds. */
  const openSingleItemMenu = async () => {
    renderTree({ onAddToContext: vi.fn() })
    await waitForTree()
    openMenu({ kind: 'directory', name: 'a', path: 'src/a' })
    const menuitem = screen.getByRole('menuitem', { name: 'Add to chat' })
    expect(menuitem).toHaveFocus()
    return menuitem
  }

  it.each(['ArrowDown', 'ArrowUp', 'Home', 'End'])(
    'consumes %s rather than letting it scroll the page behind the open menu',
    async key => {
      const menuitem = await openSingleItemMenu()

      // false = preventDefault() was called: the menu contract claimed the key.
      expect(fireEvent.keyDown(menuitem, { key })).toBe(false)
      // One item, so navigation is a no-op — but it is a no-op that STAYS
      // inside the menu rather than moving focus nowhere useful.
      expect(menuitem).toHaveFocus()
    },
  )

  it.each([
    ['Tab', false],
    ['Shift+Tab', true],
  ] as const)('contains %s within the menu instead of dropping focus behind it', async (_label, shiftKey) => {
    const menuitem = await openSingleItemMenu()

    // The single item is simultaneously the first and the last item, so both
    // Tab directions sit on a containment boundary and must wrap onto it.
    expect(fireEvent.keyDown(menuitem, { key: 'Tab', shiftKey })).toBe(false)
    expect(menuitem).toHaveFocus()
  })

  it('leaves a key the menu contract does not own alone', async () => {
    // Regression PIN, not new behaviour: Enter still belongs to the item's own
    // activation handler, and the contract must not swallow it on the way.
    const onAddToContext = vi.fn()
    renderTree({ onAddToContext })
    await waitForTree()
    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    const menuitem = screen.getByRole('menuitem', { name: 'Add to chat' })

    fireEvent.keyDown(menuitem, { key: 'Enter' })
    expect(onAddToContext).toHaveBeenCalledWith(`${ROOT}/src/a/b.ts`, 'file')
  })

  it('moves focus between Add to chat and Download with ArrowDown/ArrowUp', async () => {
    // The real two-item case: a FILE row with a host holds Add to chat AND
    // Download, so the arrows do actual roving focus (not the degenerate
    // one-item no-op above). ArrowDown steps to the next item and ArrowUp wraps
    // back, both consumed so neither scrolls the tree behind the open menu.
    renderTree({ onAddToContext: vi.fn() })
    await waitForTree()
    openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })

    const add = screen.getByRole('menuitem', { name: 'Add to chat' })
    const download = screen.getByRole('menuitem', { name: 'Download' })
    // Focus enters on the first row.
    expect(add).toHaveFocus()

    // ArrowDown -> Download, claimed (false = preventDefault called).
    expect(fireEvent.keyDown(add, { key: 'ArrowDown' })).toBe(false)
    expect(download).toHaveFocus()

    // ArrowUp -> back to Add to chat, claimed.
    expect(fireEvent.keyDown(download, { key: 'ArrowUp' })).toBe(false)
    expect(add).toHaveFocus()

    // ArrowUp from the first item wraps to the last (Download).
    expect(fireEvent.keyDown(add, { key: 'ArrowUp' })).toBe(false)
    expect(download).toHaveFocus()
  })
})

describe('PierreWorkspaceTreeImpl — app-contributed context rows', () => {
  const DECL = {
    id: 'send',
    label: 'Send to store',
    icon: 'Package',
    endpoint: '/api/apps/doc-store/send',
    surfaces: ['tree-context'],
  }
  const appsWith = (over: Record<string, unknown> = {}) => [
    { name: 'doc-store', enabled: true, manifest: { contributes: { fileMenuItems: [DECL] } }, ...over },
  ]

  /** Seed the shared `['apps']` cache the seam subscribes to (it never fetches). */
  const seed = (qc: QueryClient, apps: unknown) => act(() => { qc.setQueryData(['apps'], apps) })

  const openMenu = (item: MenuItem) => {
    const close = vi.fn()
    const context: MenuContext = {
      anchorElement: document.createElement('div'),
      anchorRect: document.createElement('div').getBoundingClientRect(),
      close,
      restoreFocus: vi.fn(),
    }
    const render_ = treeMock.fileTreeProps.at(-1)!.renderContextMenu
    return { close, node: render_ ? render_(item, context) : null }
  }

  it('reaches an app row with no host onAddToContext, on a node with no built-in row', async () => {
    // A directory node has no host row and no Download, so before an app
    // contributes, its menu renders nothing; a contributed row is what makes it
    // reachable. (A file node is always reachable now — it has Download — so a
    // directory is the node that isolates app-row reachability.)
    const { qc, update } = renderTree()
    await waitForTree()

    const dir = { kind: 'directory', name: 'a', path: 'src/a' } as const
    const before = openMenu(dir)
    render(<>{before.node}</>)
    expect(screen.queryByRole('menuitem')).toBeNull()

    seed(qc, appsWith())
    update()
    const after = openMenu(dir)
    render(<>{after.node}</>)
    expect(screen.getByRole('menuitem', { name: /^Send to store\b/ })).toBeInTheDocument()
  })

  it('renders the app row, POSTs the path and root, and never the file content', async () => {
    const { qc, update } = renderTree({ onAddToContext: vi.fn() })
    await waitForTree()
    seed(qc, appsWith())
    update()

    const { close, node } = openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    render(<>{node}</>)

    // The dispatcher reads the owning slot from the store, so name one for this case.
    store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-pierre' })

    fireEvent.click(screen.getByRole('menuitem', { name: /^Send to store\b/ }))
    expect(api.invokeFileMenuItem).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'send', app: 'doc-store' }),
      { surface: 'tree-context', path: `${ROOT}/src/a/b.ts`, kind: 'file', root: ROOT },
      // The owning slot, so a restricted slot's dispatch is gated here too. Asserted on
      // every surface: the `dashboard:ui` placeholder it replaces fails open.
      'dashboard:slot-pierre',
    )
    expect(vi.mocked(api.invokeFileMenuItem).mock.calls[0][1]).not.toHaveProperty('content')
    expect(close).toHaveBeenCalledTimes(1)
  })

  it('renders NOTHING rather than an empty popup when no row survives `when`', async () => {
    // The gate counts registered rows; `when` then filters per node. A row scoped to
    // markdown files does not match a directory node, which also has no Download
    // (file-only) and no host row — so the menu is an empty bordered box with no
    // menuitem for the focus effect to land on.
    const { qc, update } = renderTree()
    await waitForTree()
    seed(qc, appsWith({
      manifest: { contributes: { fileMenuItems: [{ ...DECL, when: { extensions: ['md'] } }] } },
    }))
    update()

    const { node } = openMenu({ kind: 'directory', name: 'a', path: 'src/a' })
    // Assert on the RENDERED output, not on `node`: Pierre's slot always receives a
    // `<TreeContextMenu/>` element, and the component's own empty-menu guard is what
    // renders nothing — so an element-identity check would pass whatever it renders.
    render(<>{node}</>)
    expect(screen.queryByRole('menu')).toBeNull()
    expect(screen.queryAllByRole('menuitem')).toHaveLength(0)
  })

  it('focuses the first app row when there is no built-in row to focus', async () => {
    // `firstItemRef` hangs off the built-in rows (Add to chat, Download); on a
    // directory node with no host neither exists, and the querySelector fallback
    // is what gives an app-only menu a focus target.
    const { qc, update } = renderTree()
    await waitForTree()
    seed(qc, appsWith())
    update()

    const { node } = openMenu({ kind: 'directory', name: 'a', path: 'src/a' })
    render(<>{node}</>)
    const row = screen.getByRole('menuitem', { name: /^Send to store\b/ })
    expect(row).toHaveFocus()
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(api.invokeFileMenuItem).toHaveBeenCalled()
  })

  it('contributes nothing from a disabled app', async () => {
    const { qc, update } = renderTree()
    await waitForTree()
    seed(qc, appsWith({ enabled: false }))
    update()
    // A directory node has no built-in row, so a disabled app contributing
    // nothing leaves the menu empty.
    const { node } = openMenu({ kind: 'directory', name: 'a', path: 'src/a' })
    render(<>{node}</>)
    expect(screen.queryByRole('menuitem')).toBeNull()
  })

  it('surfaces a rejected dispatch on the TREE, which outlives the menu', async () => {
    // `errors-use-error-notice`. Activating a row closes the context menu, so the notice
    // cannot live inside it: the state and the ErrorNotice belong to the tree.
    vi.mocked(api.invokeFileMenuItem).mockRejectedValueOnce(new Error('endpoint refused'))
    const { qc, update } = renderTree()
    await waitForTree()
    seed(qc, appsWith())
    update()

    const { node } = openMenu({ kind: 'file', name: 'b.ts', path: 'src/a/b.ts' })
    render(<>{node}</>)
    fireEvent.click(screen.getByRole('menuitem', { name: /^Send to store\b/ }))

    const notice = await waitFor(() => screen.getByTestId('workspace-tree-action-error'))
    expect(notice).toHaveTextContent('endpoint refused')
  })
})
