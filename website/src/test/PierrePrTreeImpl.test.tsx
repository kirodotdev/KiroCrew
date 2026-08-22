/**
 * PierrePrTreeImpl — the PR change-set tree's own logic around `@pierre/trees`.
 *
 * Same convention as PierreWorkspaceTreeImpl.test.tsx: the trees runtime is
 * replaced by the recording fake in `./__mocks__/pierreTreesReact`, and every
 * assertion is about what THIS wrapper does — how props project onto the model
 * (paths, lanes, viewed decorations), how the viewport echo drives selection
 * without re-firing the click callback, and how a real row selection reports
 * an open. The lazy boundary in `pierre/tree.tsx` is covered at the end,
 * mirroring PierreWorkspaceTree.lazy.test.tsx.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'

vi.mock('@pierre/trees/react', async () => await import('./__mocks__/pierreTreesReact'))

import { PierrePrTreeImpl } from '../pierre/PierrePrTreeImpl'
import { PierrePrTree } from '../pierre/tree'
import { PR_TREE_VIEWED_ICON, PR_TREE_VIEWED_SPRITE, PR_TREE_VIEWED_CSS } from '../pierre/config'
import { treeMock } from './__mocks__/pierreTreesReact'

const FILES = [
  { path: 'src/a.ts', status: 'modified' },
  { path: 'src/b.ts', status: 'added' },
  { path: 'docs/readme.md', status: 'removed' },
] as const

type Props = Parameters<typeof PierrePrTreeImpl>[0]

const NO_VIEWED: ReadonlySet<string> = new Set()

function renderTree(props: Partial<Props> = {}) {
  const base: Props = { files: FILES, viewedPaths: NO_VIEWED, ...props }
  const view = render(<PierrePrTreeImpl {...base} />)
  return {
    ...view,
    update: (next: Partial<Props> = {}) => view.rerender(<PierrePrTreeImpl {...base} {...next} />),
  }
}

type DecorationRenderer = (args: { row: { kind: string; path: string } }) =>
  | { icon: { name: string }; title: string }
  | null

beforeEach(() => {
  treeMock.reset()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('PierrePrTreeImpl', () => {
  it('creates the model as an all-open, unsearchable working set carrying the viewed sprite and CSS', () => {
    renderTree()
    const options = treeMock.last().options
    expect(options.initialExpansion).toBe('open')
    expect(options.flattenEmptyDirectories).toBe(true)
    expect(options.search).toBe(false)
    expect(options.icons).toEqual({ spriteSheet: PR_TREE_VIEWED_SPRITE, set: 'complete' })
    expect(options.unsafeCSS).toBe(PR_TREE_VIEWED_CSS)
    expect(screen.getByTestId('file-tree')).toBeInTheDocument()
  })

  it('feeds the change set as paths and provider statuses as git lanes', () => {
    renderTree()
    const model = treeMock.last()
    expect(model.calls.resetPaths).toEqual([['src/a.ts', 'src/b.ts', 'docs/readme.md']])
    expect(model.calls.gitStatus.at(-1)).toEqual([
      { path: 'src/a.ts', status: 'modified' },
      { path: 'src/b.ts', status: 'added' },
      { path: 'docs/readme.md', status: 'deleted' },
    ])
  })

  it("maps provider tokens onto Pierre's lane vocabulary, defaulting unknowns to modified", () => {
    renderTree({
      files: [
        { path: 'n.ts', status: 'new' },
        { path: 'd.ts', status: 'deleted' },
        { path: 'r.ts', status: 'renamed' },
        { path: 'u.ts', status: 'churned' },
        { path: 'x.ts' },
      ],
    })
    expect(treeMock.last().calls.gitStatus.at(-1)).toEqual([
      { path: 'n.ts', status: 'added' },
      { path: 'd.ts', status: 'deleted' },
      { path: 'r.ts', status: 'renamed' },
      { path: 'u.ts', status: 'modified' },
      { path: 'x.ts', status: 'modified' },
    ])
  })

  it('re-feeds paths only when the set actually changes', () => {
    const { update } = renderTree()
    const model = treeMock.last()
    // Fresh array, same contents: no reset.
    update({ files: FILES.map(f => ({ ...f })) })
    expect(model.calls.resetPaths).toHaveLength(1)
    // Contents changed: reset with the new set.
    update({ files: [{ path: 'only.ts', status: 'added' }] })
    expect(model.calls.resetPaths).toHaveLength(2)
    expect(model.calls.resetPaths.at(-1)).toEqual(['only.ts'])
  })

  it('decorates viewed files with the check sprite, through the live set (no remount)', () => {
    const { update } = renderTree()
    const renderDecoration = treeMock.last().options.renderRowDecoration as DecorationRenderer
    expect(renderDecoration({ row: { kind: 'file', path: 'src/a.ts' } })).toBeNull()

    update({ viewedPaths: new Set(['src/a.ts']) })
    const viewed = renderDecoration({ row: { kind: 'file', path: 'src/a.ts' } })
    expect(viewed?.icon.name).toBe(PR_TREE_VIEWED_ICON)
    expect(viewed?.title).toBeTruthy()
    // Directories and unviewed files stay undecorated.
    expect(renderDecoration({ row: { kind: 'directory', path: 'src' } })).toBeNull()
    expect(renderDecoration({ row: { kind: 'file', path: 'src/b.ts' } })).toBeNull()
  })

  it('repaints the tree when the viewed set changes, by re-asserting the icon config', () => {
    const { update } = renderTree()
    const model = treeMock.last()
    const before = model.calls.setIcons.length
    update({ viewedPaths: new Set(['src/a.ts']) })
    expect(model.calls.setIcons.length).toBe(before + 1)
  })

  it('echoes the viewport file as the focused, sole selection without firing the click callback', () => {
    const onFileClick = vi.fn()
    const { update } = renderTree({ onFileClick })
    const model = treeMock.last()
    act(() => model.simulateSelection('src/a.ts'))
    expect(onFileClick).toHaveBeenCalledTimes(1)
    onFileClick.mockClear()

    update({ currentPath: 'src/b.ts' })
    expect(model.calls.focusPath.at(-1)).toBe('src/b.ts')
    expect(model.calls.select.at(-1)).toBe('src/b.ts')
    // The previous selection is dropped so the tree shows exactly one row.
    expect(model.calls.deselect).toContain('src/a.ts')
    expect(onFileClick).not.toHaveBeenCalled()
  })

  it('reports a single selected file as an open', () => {
    const onFileClick = vi.fn()
    renderTree({ onFileClick })
    act(() => treeMock.last().simulateSelection('src/b.ts'))
    expect(onFileClick).toHaveBeenCalledWith('src/b.ts')
  })

  it('ignores directory focus, multi-selection, and re-clicks on the current file', () => {
    const onFileClick = vi.fn()
    const { update } = renderTree({ onFileClick, currentPath: 'src/a.ts' })
    const model = treeMock.last()
    act(() => model.simulateSelection('src'))
    act(() => model.simulateSelection('src/b.ts', ['src/a.ts', 'src/b.ts']))
    // Clicking the file the viewport is already on is not a navigation.
    act(() => model.simulateSelection('src/a.ts'))
    expect(onFileClick).not.toHaveBeenCalled()
    // ...but the ref keeps up with prop changes: after the current file moves
    // on, the same click IS a navigation again.
    update({ currentPath: 'src/b.ts' })
    act(() => model.simulateSelection('src/a.ts'))
    expect(onFileClick).toHaveBeenCalledWith('src/a.ts')
  })

  it('unsubscribes from the model on unmount', () => {
    const { unmount } = renderTree()
    const model = treeMock.last()
    expect(model.subscriberCount()).toBe(1)
    unmount()
    expect(model.subscriberCount()).toBe(0)
    expect(model.calls.unsubscribes).toBe(1)
  })
})

describe('PierrePrTree (lazy boundary)', () => {
  it('shows the tree skeleton while the chunk loads, then hands the props to the impl', async () => {
    render(
      <PierrePrTree
        files={[{ path: 'a.ts', status: 'added' }]}
        viewedPaths={new Set(['a.ts'])}
      />,
    )
    // The impl arrives via React.lazy; the shimmer covers the wait.
    await waitFor(() => expect(screen.getByTestId('file-tree')).toBeInTheDocument())
    const model = treeMock.last()
    expect(model.calls.resetPaths).toEqual([['a.ts']])
    const renderDecoration = model.options.renderRowDecoration as DecorationRenderer
    expect(renderDecoration({ row: { kind: 'file', path: 'a.ts' } })?.icon.name).toBe(PR_TREE_VIEWED_ICON)
  })
})
