/**
 * State rows against the REAL `@pierre/trees` widget.
 *
 * `PierreWorkspaceTreeImpl.test.tsx` runs against `__mocks__/pierreTreesReact`,
 * so it proves the wrapper's own logic and nothing about the library: a
 * `@pierre/trees` bump could turn a status line back into a clickable "file"
 * with no red test. This file mounts the real `<FileTree>` (shadow DOM, its own
 * store, its own key handling) and drives it the way a user does -- clicks,
 * Shift+F10, right-click, arrow keys, the filter -- asserting that every
 * consumer the wrapper guards stays inert for a state row and live for a real
 * file, including a real file whose name ends in the marker character. It runs
 * in the normal frontend suite, so every push (hence every version change)
 * re-checks the contracts the wrapper leans on.
 *
 * What happy-dom cannot prove here, and the capture harness does: the
 * stylesheet's EFFECT. happy-dom does not compute a shadow-root stylesheet
 * (`getComputedStyle(row).pointerEvents` is empty) and its `:has()` matching
 * is unreliable, so the pointer-off / icon-hidden rendering is asserted by
 * computed style in `scripts/capture-files-tree-empty-folder.mjs` against a
 * real Chromium. This file pins the two halves that stylesheet rests on: the
 * CSS reaches the shadow root, and the decoration lane the selector keys on is
 * rendered by the real widget on exactly the planned rows.
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
import { PIERRE_TREE_STATE_ROW_CSS } from '../pierre/config'
import { PierreWorkspaceTreeImpl } from '../pierre/PierreWorkspaceTreeImpl'
import { STATE_ROW_ICON, STATE_ROW_MARKER } from '../pierre/treeStateRows'

const ROOT = '/repo/project'
/** A REAL file whose name ends in the marker character: must stay a file. */
const MARKER_NAMED_FILE = `x${STATE_ROW_MARKER}`

type TreePayload = Awaited<ReturnType<typeof api.projectTree>>
type StatusPayload = Awaited<ReturnType<typeof api.projectGitStatus>>

const tree = (): TreePayload => ({
  root: ROOT,
  paths: ['README.md', 'src/a/b.ts', MARKER_NAMED_FILE],
  directories: ['empty/', 'big/', '_bg/'],
  truncatedDirectories: ['big'],
  hiddenOnlyDirectories: ['_bg'],
  unreadableDirectories: [],
  repo: true,
})
const status = (): StatusPayload => ({ repo: true, repoRoot: '/repo', files: [] })

/** The shape `PIERRE_TREE_STATE_ROW_CSS` selects on (see `../pierre/config`). */
const STATE_ICON_IN_DECORATION = `[data-item-section="decoration"] [data-icon-name="${STATE_ROW_ICON}"]`
const ROW = '[data-type="item"][data-item-path]'

type Props = Parameters<typeof PierreWorkspaceTreeImpl>[0]

/** Mount the real widget, wait for its first rows, and expand the three folders. */
async function mountTree(extra: Partial<Props> = {}) {
  const onFileOpen = vi.fn()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  const props: Props = { projectDir: ROOT, onFileOpen, ...extra }
  const view = render(<PierreWorkspaceTreeImpl {...props} />, { wrapper })
  const host = await waitFor(() => {
    const el = view.container.querySelector<HTMLElement>('file-tree-container')
    expect(el?.shadowRoot?.querySelector(ROW)).toBeTruthy()
    return el as HTMLElement
  })
  const shadow = host.shadowRoot as ShadowRoot
  const rows = () => Array.from(shadow.querySelectorAll<HTMLElement>(ROW))
  const paths = () => rows().map(r => r.getAttribute('data-item-path'))
  const row = (path: string) => {
    const el = shadow.querySelector<HTMLElement>(`${ROW.slice(0, -1)}="${path}"]`)
    if (!el) throw new Error(`no row for ${JSON.stringify(path)}; rows: ${JSON.stringify(paths())}`)
    return el
  }
  const stateRows = () => rows().filter(r => r.querySelector(STATE_ICON_IN_DECORATION))
  const click = async (path: string) => {
    await act(async () => {
      fireEvent.click(row(path))
    })
  }
  const setQuery = async (searchQuery: string) => {
    await act(async () => {
      view.rerender(<PierreWorkspaceTreeImpl {...props} searchQuery={searchQuery} />)
    })
  }
  const menu = () => ({
    open: shadow.querySelector('[data-type="context-menu-anchor"] button')?.getAttribute('aria-expanded') === 'true',
    wash: shadow.querySelector('[data-type="context-menu-wash"]') != null,
    // The React binding hands the wrapper's rendered menu over as a slotted
    // light-DOM child of the host.
    slotted: view.container.querySelector('[slot="context-menu"]')?.textContent ?? null,
  })
  const focused = () => rows().find(r => r.hasAttribute('data-item-focused'))?.getAttribute('data-item-path') ?? null
  for (const folder of ['empty/', 'big/', '_bg/']) await click(folder)
  await waitFor(() => expect(stateRows()).toHaveLength(3))
  return { view, host, shadow, rows, paths, row, stateRows, click, setQuery, menu, focused, onFileOpen }
}

beforeEach(() => {
  vi.mocked(api.projectTree).mockResolvedValue(tree())
  vi.mocked(api.projectGitStatus).mockResolvedValue(status())
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('state rows against the real @pierre/trees widget', () => {
  it('renders the decoration lane the stylesheet keys on, on exactly the planned rows', async () => {
    const t = await mountTree()
    // The stylesheet reached the widget's shadow root (`unsafeCSS`).
    const css = Array.from(t.shadow.querySelectorAll('style')).map(s => s.textContent ?? '').join('\n')
    expect(css).toContain(PIERRE_TREE_STATE_ROW_CSS.trim())
    // The selector's subject is `[data-type="item"]` and its `:has()` looks for
    // the state icon inside the decoration lane: the real widget renders both.
    const planned = t.stateRows().map(r => r.getAttribute('data-item-path'))
    expect(planned.map(p => p?.split('/')[0]).sort()).toEqual(['_bg', 'big', 'empty'])
    for (const r of t.stateRows()) {
      expect(r.matches('button[data-type="item"][data-item-type="file"]')).toBe(true)
      expect(r.getAttribute('data-item-path')?.endsWith(STATE_ROW_MARKER)).toBe(true)
    }
    // Every other row -- folders, files, and the file NAMED with the marker --
    // carries no state icon, so the stylesheet cannot reach it.
    const others = t.rows().filter(r => !r.querySelector(STATE_ICON_IN_DECORATION))
    expect(others.map(r => r.getAttribute('data-item-path'))).toEqual(
      expect.arrayContaining(['README.md', MARKER_NAMED_FILE, 'empty/', 'big/', '_bg/', 'src/a/']),
    )
    expect(others).toHaveLength(t.rows().length - 3)
  })

  it('does not open or keep a selection on a clicked state row, and opens the marker-named real file', async () => {
    const t = await mountTree()
    const [stateRow] = t.stateRows().filter(r => r.getAttribute('data-item-path')?.startsWith('empty/'))
    const statePath = stateRow.getAttribute('data-item-path') as string
    await t.click(statePath)
    // Pierre selected the row on click; the wrapper's open-on-selection guard
    // deselected it and reported no open.
    await waitFor(() => expect(t.row(statePath).getAttribute('aria-selected')).toBe('false'))
    expect(t.onFileOpen).not.toHaveBeenCalled()
    // A real file opens and stays selected -- the marker-named one included.
    await t.click('README.md')
    await waitFor(() => expect(t.row('README.md').getAttribute('aria-selected')).toBe('true'))
    expect(t.onFileOpen).toHaveBeenCalledWith(`${ROOT}/README.md`)
    await t.click(MARKER_NAMED_FILE)
    await waitFor(() => expect(t.onFileOpen).toHaveBeenCalledWith(`${ROOT}/${MARKER_NAMED_FILE}`))
    expect(t.onFileOpen).toHaveBeenCalledTimes(2)
  })

  it('closes the keyboard and right-click context menu on a state row without stranding the keyboard', async () => {
    const t = await mountTree()
    const [stateRow] = t.stateRows().filter(r => r.getAttribute('data-item-path')?.startsWith('empty/'))
    const statePath = stateRow.getAttribute('data-item-path') as string
    // Control: a real file's menu opens on Shift+F10, swallows ArrowDown while
    // open (Pierre's menu-open state), and closes on Escape.
    await act(async () => {
      t.row('README.md').focus()
      fireEvent.focus(t.row('README.md'))
    })
    await act(async () => {
      fireEvent.keyDown(t.row('README.md'), { key: 'F10', shiftKey: true })
    })
    await waitFor(() => expect(t.menu()).toMatchObject({ open: true, wash: true }))
    expect(t.menu().slotted).not.toBeNull()
    await act(async () => {
      fireEvent.keyDown(t.row('README.md'), { key: 'ArrowDown' })
    })
    expect(t.focused()).toBe('README.md')
    await act(async () => {
      fireEvent.keyDown(t.row('README.md'), { key: 'Escape' })
    })
    await waitFor(() => expect(t.menu()).toMatchObject({ open: false, wash: false, slotted: null }))

    // The state row: Pierre enters its menu-open state, the wrapper renders
    // nothing and closes the request, so nothing is open once the turn settles
    // and the next arrow key still moves focus.
    await act(async () => {
      t.row(statePath).focus()
      fireEvent.focus(t.row(statePath))
    })
    await act(async () => {
      fireEvent.keyDown(t.row(statePath), { key: 'F10', shiftKey: true })
    })
    await waitFor(() => expect(t.menu()).toMatchObject({ open: false, wash: false, slotted: null }))
    await act(async () => {
      fireEvent.keyDown(t.row(statePath), { key: 'ArrowDown' })
    })
    await waitFor(() => expect(t.focused()).not.toBe(statePath))
    expect(t.focused()).not.toBeNull()
    // Right-click reaches the same renderer (the stylesheet keeps the pointer
    // off the row in a browser; the guard holds even without it).
    await act(async () => {
      fireEvent.contextMenu(t.row(statePath))
    })
    await waitFor(() => expect(t.menu()).toMatchObject({ open: false, wash: false, slotted: null }))
  })

  it('keeps the search focus and the filter results off the state rows', async () => {
    const t = await mountTree()
    // A folder match: Pierre force-expands `empty/` and focuses the first match;
    // its state row rides along beneath but the focus lands on the folder.
    await t.setQuery('emp')
    await waitFor(() => expect(t.paths()).toEqual(['empty/', expect.stringMatching(/^empty\//)]))
    expect(t.stateRows()).toHaveLength(1)
    expect(t.focused()).toBe('empty/')
    expect(t.row('empty/').tabIndex).toBe(0)
    // A label-only match: the rows are withheld, so typing the label of a
    // status line surfaces no inert result.
    await t.setQuery('not shown')
    await waitFor(() => expect(t.stateRows()).toHaveLength(0))
    expect(t.paths().some(p => p?.endsWith(STATE_ROW_MARKER) && p.includes('/'))).toBe(false)
    // A real-file match shows only that file.
    await t.setQuery('READ')
    await waitFor(() => expect(t.paths()).toEqual(['README.md']))
    // Clearing restores the rows and carries `empty/` open across the reset.
    await t.setQuery('')
    await waitFor(() => expect(t.stateRows()).toHaveLength(3))
    expect(t.row('empty/').getAttribute('aria-expanded')).toBe('true')
  })

  it('lets the truncation badge yield to the state row while the folder is open', async () => {
    const t = await mountTree()
    const badge = () => t.row('big/').querySelector('[data-item-section="decoration"]')?.textContent ?? ''
    // Expanded: the state row says it, the badge is silent.
    expect(t.stateRows().some(r => r.getAttribute('data-item-path')?.startsWith('big/'))).toBe(true)
    expect(badge()).toBe('')
    // Collapsed: the row is gone with its folder and the badge is back.
    await t.click('big/')
    await waitFor(() => expect(t.row('big/').getAttribute('aria-expanded')).toBe('false'))
    expect(t.stateRows().some(r => r.getAttribute('data-item-path')?.startsWith('big/'))).toBe(false)
    expect(badge()).toMatch(/files not shown/i)
  })
})
