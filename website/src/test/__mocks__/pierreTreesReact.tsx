/**
 * Stand-in for `@pierre/trees/react`.
 *
 * The real `FileTree` renders custom elements that never upgrade in the test
 * DOM, so nothing the wrapper does through the model is observable via the
 * rendered output. This fake records every imperative call the wrapper makes,
 * keeps just enough state for the wrapper's own reads (`getSelectedPaths`,
 * `getItem`, `getFocusedItem`) to answer truthfully, and exposes a driver for
 * the single event the wrapper subscribes to — a row selection change.
 *
 * Used from a test with:
 *   vi.mock('@pierre/trees/react', async () => await import('./__mocks__/pierreTreesReact'))
 * so the registry below is the same module instance the component sees.
 */
import { useRef } from 'react'
import type { CSSProperties, ReactElement, ReactNode } from 'react'

export type StatusEntry = { path: string; status: string }

type Handle = {
  getPath: () => string
  isDirectory: () => boolean
  select: () => void
  deselect: () => void
  expand?: () => void
}

export function createFakeModel(options: Record<string, unknown>) {
  const files: string[] = []
  const dirs = new Set<string>()
  const selected = new Set<string>()
  const listeners = new Set<() => void>()
  let focused: string | null = null

  const calls = {
    resetPaths: [] as string[][],
    gitStatus: [] as StatusEntry[][],
    search: [] as Array<string | null>,
    focusPath: [] as string[],
    select: [] as string[],
    deselect: [] as string[],
    expand: [] as string[],
    unsubscribes: 0,
  }

  const known = (path: string) => files.includes(path) || dirs.has(path)

  const handle = (path: string): Handle => {
    const isDir = dirs.has(path)
    const base: Handle = {
      getPath: () => path,
      isDirectory: () => isDir,
      select: () => {
        calls.select.push(path)
        selected.add(path)
      },
      deselect: () => {
        calls.deselect.push(path)
        selected.delete(path)
      },
    }
    // Only directory handles carry `expand`; the wrapper feature-detects it.
    return isDir ? { ...base, expand: () => calls.expand.push(path) } : base
  }

  return {
    /** Options `useFileTree` was created with — the wrapper's prop mapping. */
    options,
    calls,
    resetPaths(next: readonly string[]) {
      calls.resetPaths.push([...next])
      files.splice(0, files.length, ...next)
      dirs.clear()
      for (const p of next) {
        const segments = p.split('/')
        for (let i = 1; i < segments.length; i++) dirs.add(segments.slice(0, i).join('/'))
      }
    },
    setGitStatus(entries: readonly StatusEntry[] = []) {
      calls.gitStatus.push([...entries])
    },
    setSearch(value: string | null) {
      calls.search.push(value)
    },
    focusPath(path: string) {
      calls.focusPath.push(path)
      focused = path
    },
    getFocusedItem: () => (focused && known(focused) ? handle(focused) : null),
    getSelectedPaths: () => [...selected],
    getItem: (path: string) => (known(path) ? handle(path) : null),
    subscribe(listener: () => void) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
        calls.unsubscribes++
      }
    },
    /** Drive the model as the tree would after a user selects rows. */
    simulateSelection(focusedPath: string | null, selection: string[] = focusedPath ? [focusedPath] : []) {
      focused = focusedPath
      selected.clear()
      for (const p of selection) selected.add(p)
      for (const listener of listeners) listener()
    },
    subscriberCount: () => listeners.size,
  }
}

export type FakeModel = ReturnType<typeof createFakeModel>

/** Mirrors the subset of `@pierre/trees`' context-menu API the wrapper touches:
 *  the row descriptor and the `close()` the menu calls after each action. */
export type MenuItem = { kind: 'directory' | 'file'; name: string; path: string }
export type MenuContext = {
  anchorElement: HTMLElement
  anchorRect: DOMRect
  close: (options?: { restoreFocus?: boolean }) => void
  restoreFocus: () => void
}
type FileTreeMockProps = {
  model: unknown
  className?: string
  style?: CSSProperties
  renderContextMenu?: (item: MenuItem, context: MenuContext) => ReactNode
}

export const treeMock = {
  models: [] as FakeModel[],
  fileTreeProps: [] as FileTreeMockProps[],
  reset() {
    this.models.length = 0
    this.fileTreeProps.length = 0
  },
  /** Model of the most recently mounted tree. */
  last(): FakeModel {
    const model = this.models.at(-1)
    if (!model) throw new Error('no FileTree model has been created')
    return model
  },
}

export function useFileTree(options: Record<string, unknown>): { model: FakeModel } {
  // The real hook hands back one stable model for the component's lifetime;
  // the wrapper's effects key off that identity.
  const ref = useRef<FakeModel | null>(null)
  if (!ref.current) {
    ref.current = createFakeModel(options)
    treeMock.models.push(ref.current)
  }
  return { model: ref.current }
}

export function FileTree(props: FileTreeMockProps): ReactElement {
  treeMock.fileTreeProps.push(props)
  return <div data-testid="file-tree" className={props.className} style={props.style} />
}
