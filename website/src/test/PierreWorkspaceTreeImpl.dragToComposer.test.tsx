/**
 * Dragging a row out of the REAL `@pierre/trees` widget carries the
 * dashboard's tree-entry payload, which is how the composer tells a tree drag
 * from an OS file drag or a text drag. Mounted against the real widget (shadow
 * DOM rows, its own drag handling) so a library bump that stops rows being
 * draggable, or drops `data-item-path`, turns this red.
 */
import { act, fireEvent, render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({
  api: {
    projectTree: vi.fn(),
    projectGitStatus: vi.fn(),
  },
}))
vi.mock('../components/AppIcon', () => ({ default: () => null }))

import { api } from '../api/client'
import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { TREE_ENTRY_DRAG_TYPE, TREE_ENTRY_REFUSED_TYPE, decodeTreeEntry } from '../lib/treeEntryDrag'

const ROOT = '/repo/project'
const ROW = '[data-type="item"][data-item-path]'

type TreePayload = Awaited<ReturnType<typeof api.projectTree>>
type StatusPayload = Awaited<ReturnType<typeof api.projectGitStatus>>

const originalMatchMedia = window.matchMedia
function pointer(fine: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: query === '(pointer: fine)' ? fine : false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

async function mount(props: Partial<Parameters<typeof PierreWorkspaceTreeImpl>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const view = render(<PierreWorkspaceTreeImpl projectDir={ROOT} {...props} />, { wrapper })
  const shadow = await waitFor(() => {
    const host = view.container.querySelector<HTMLElement>('file-tree-container')
    expect(host?.shadowRoot?.querySelector(ROW)).toBeTruthy()
    return host!.shadowRoot as ShadowRoot
  })
  const row = (path: string) => shadow.querySelector<HTMLElement>(`[data-type="item"][data-item-path="${path}"]`)!
  return { view, shadow, row }
}

/** Fire a composed `dragstart` on a row and return what was written to it. */
function dragStart(el: HTMLElement) {
  const store: Record<string, string> = {}
  const dataTransfer = {
    setData: (type: string, value: string) => { store[type] = value },
    getData: (type: string) => store[type] ?? '',
    setDragImage: () => {},
    get types() { return Object.keys(store) },
    effectAllowed: 'uninitialized',
    dropEffect: 'none',
  }
  const event = new Event('dragstart', { bubbles: true, composed: true, cancelable: true })
  Object.defineProperty(event, 'dataTransfer', { value: dataTransfer })
  Object.defineProperty(event, 'clientX', { value: 0 })
  Object.defineProperty(event, 'clientY', { value: 0 })
  act(() => { el.dispatchEvent(event) })
  return { store, dataTransfer }
}

beforeEach(() => {
  pointer(true)
  vi.mocked(api.projectTree).mockResolvedValue({
    root: ROOT,
    paths: ['README.md', 'src/a.ts', 'My Docs/x.md'],
    directories: ['src/', 'My Docs/'],
    truncatedDirectories: [],
    hiddenOnlyDirectories: [],
    unreadableDirectories: [],
    repo: true,
  } as TreePayload)
  vi.mocked(api.projectGitStatus).mockResolvedValue({ repo: true, repoRoot: '/repo', files: [] } as StatusPayload)
})

afterEach(() => {
  window.matchMedia = originalMatchMedia
  vi.clearAllMocks()
})

describe('dragging a tree row toward the composer', () => {
  it('marks a file row drag with the tree-entry payload and allows a copy', async () => {
    const t = await mount({ onAddToContext: vi.fn() })
    const readme = t.row('README.md')
    expect(readme.getAttribute('draggable')).toBe('true')
    const { store, dataTransfer } = dragStart(readme)
    expect(decodeTreeEntry(store[TREE_ENTRY_DRAG_TYPE])).toEqual({ path: `${ROOT}/README.md`, kind: 'file' })
    expect(dataTransfer.effectAllowed).toBe('copyMove')
  })

  it('marks a folder row drag as a directory', async () => {
    const t = await mount({ onAddToContext: vi.fn() })
    const { store } = dragStart(t.row('src/'))
    expect(decodeTreeEntry(store[TREE_ENTRY_DRAG_TYPE])).toEqual({ path: `${ROOT}/src`, kind: 'dir' })
  })

  it('shows "Add to chat" disabled, with the reason, for a folder the composer cannot mention', async () => {
    const onAddToContext = vi.fn()
    const t = await mount({ onAddToContext })
    const openMenu = async (path: string) => {
      const row = t.row(path)
      await act(async () => { row.focus(); fireEvent.focus(row) })
      await act(async () => { fireEvent.keyDown(row, { key: 'F10', shiftKey: true }) })
      return waitFor(() => {
        const menu = document.querySelector<HTMLElement>('[data-file-tree-context-menu-root]')
        expect(menu?.textContent).toBeTruthy()
        return menu as HTMLElement
      })
    }
    const closeMenu = async (path: string) => {
      await act(async () => { fireEvent.keyDown(t.row(path), { key: 'Escape' }) })
      await waitFor(() => expect(document.querySelector('[data-file-tree-context-menu-root]')).toBeNull())
    }
    // A folder that can be mentioned keeps the live row.
    let menu = await openMenu('src/')
    expect(menu.querySelector('[data-testid="file-tree-add-to-chat-refused"]')).toBeNull()
    expect(menu.textContent).toContain('Add to chat')
    await closeMenu('src/')
    // `My Docs/`: the menu still opens, the row is disabled and says why, and
    // activating it adds nothing.
    menu = await openMenu('My Docs/')
    const refused = menu.querySelector<HTMLElement>('[data-testid="file-tree-add-to-chat-refused"]')
    expect(refused?.getAttribute('aria-disabled')).toBe('true')
    expect(refused?.textContent).toContain("This folder can't be added: its path contains a space or @.")
    await act(async () => {
      fireEvent.click(refused as HTMLElement)
      fireEvent.keyDown(refused as HTMLElement, { key: 'Enter' })
    })
    expect(onAddToContext).not.toHaveBeenCalled()
  })

  it('marks a folder whose name has a space as one the composer cannot mention', async () => {
    const t = await mount({ onAddToContext: vi.fn() })
    const { store } = dragStart(t.row('My Docs/'))
    expect(decodeTreeEntry(store[TREE_ENTRY_DRAG_TYPE])).toEqual({ path: `${ROOT}/My Docs`, kind: 'dir' })
    expect(store[TREE_ENTRY_REFUSED_TYPE]).toBe('1')
    expect(dragStart(t.row('src/')).store[TREE_ENTRY_REFUSED_TYPE]).toBeUndefined()
  })

  it('does not open the dragged file in the viewer, during or after the drag', async () => {
    const onFileOpen = vi.fn()
    const t = await mount({ onAddToContext: vi.fn(), onFileOpen })
    const readme = t.row('README.md')
    dragStart(readme)
    act(() => { readme.dispatchEvent(new Event('dragend', { bubbles: true, composed: true })) })
    await act(async () => { await new Promise(r => setTimeout(r, 10)) })
    expect(onFileOpen).not.toHaveBeenCalled()
    // A plain click afterwards still opens as before.
    act(() => { readme.click() })
    await waitFor(() => expect(onFileOpen).toHaveBeenCalledWith(`${ROOT}/README.md`))
  })

  it('keeps rows undraggable where the host has no "Add to chat"', async () => {
    const t = await mount()
    expect(t.row('README.md').getAttribute('draggable')).not.toBe('true')
    const { store } = dragStart(t.row('README.md'))
    expect(store[TREE_ENTRY_DRAG_TYPE]).toBeUndefined()
  })

  it('keeps rows undraggable on a touch-first device', async () => {
    pointer(false)
    const t = await mount({ onAddToContext: vi.fn() })
    expect(t.row('README.md').getAttribute('draggable')).not.toBe('true')
  })
})
