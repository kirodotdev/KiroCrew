/**
 * Pins the DOM coupling `resolveUploadTargetDir` depends on, against the REAL
 * `@pierre/trees` component — no mock. The resolver reads the row attributes
 * (`data-item-path` / `data-item-type` / `data-item-parent-path`) the library
 * renders per row; both suites that exercise the resolver hand-construct those
 * attributes, so a library upgrade that renamed them would pass every existing
 * test while every row-targeted drop silently fell back to the workspace root.
 * This file mounts the real tree, asserts the attributes exist on real rows,
 * and runs the resolver against those rows — if the library drops or renames
 * the contract, THIS test fails instead of production drops going quiet.
 */
import { render, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FileTree, useFileTree } from '@pierre/trees/react'
import { resolveUploadTargetDir } from '../pages/chat/resolveUploadTargetDir'

const ROOT = '/abs/project'

function RealTree() {
  const { model } = useFileTree({
    paths: ['src/a/b.ts', 'src/a/c.ts', 'README.md'],
    initialExpansion: 'open',
    flattenEmptyDirectories: false,
    search: false,
  })
  return <FileTree model={model} style={{ height: 400 }} />
}

/** The real tree renders its rows inside the `file-tree-container` custom
 *  element's open shadow root, so queries must go through it. */
function shadowRows(container: HTMLElement): Element[] {
  const host = container.querySelector('file-tree-container')
  const root = host?.shadowRoot ?? host ?? container
  return Array.from(root.querySelectorAll('[data-item-path]'))
}

describe('resolveUploadTargetDir against the real @pierre/trees rows', () => {
  it('the real library still renders the data-item-* attributes the resolver reads', async () => {
    const { container } = render(<RealTree />)

    const rows = await waitFor(() => {
      const found = shadowRows(container)
      expect(found.length).toBeGreaterThan(0)
      return found
    })

    const byPath = new Map(rows.map(el => [el.getAttribute('data-item-path'), el]))

    // A directory row: type says folder, and the real library renders its
    // path WITH a trailing slash ('src/a/') — the form the resolver actually
    // receives in production, which the hand-constructed suites never used.
    const dirRow = byPath.get('src/a/')
    expect(dirRow, 'real tree renders a row for directory src/a/').toBeTruthy()
    expect(dirRow!.getAttribute('data-item-type')).toBe('folder')

    // A file row: type says file and the parent path names its directory.
    const fileRow = byPath.get('src/a/b.ts')
    expect(fileRow, 'real tree renders a row for file src/a/b.ts').toBeTruthy()
    expect(fileRow!.getAttribute('data-item-type')).toBe('file')
    expect(fileRow!.getAttribute('data-item-parent-path')).toBe('src/a/')
  })

  it('resolves a drop on real rows: directory row → itself, file row → its parent, container → root', async () => {
    const { container } = render(<RealTree />)

    const rows = await waitFor(() => {
      const found = shadowRows(container)
      expect(found.length).toBeGreaterThan(0)
      return found
    })
    const byPath = new Map(rows.map(el => [el.getAttribute('data-item-path'), el]))

    // The trailing slash is the library's own path form for directories; the
    // backend's path validation normalizes it, so the resolver passes it
    // through rather than second-guessing the contract.
    expect(resolveUploadTargetDir(ROOT, byPath.get('src/a/')!)).toBe(`${ROOT}/src/a/`)
    expect(resolveUploadTargetDir(ROOT, byPath.get('src/a/b.ts')!)).toBe(`${ROOT}/src/a/`)
    // A drop that misses every row (the container itself) falls back to root.
    expect(resolveUploadTargetDir(ROOT, container)).toBe(ROOT)
  })

  it('resolves a composed drop caught outside the shadow root to its inner directory row', async () => {
    const { container } = render(<RealTree />)

    const dirRow = await waitFor(() => {
      const found = shadowRows(container).find(el => el.getAttribute('data-item-path') === 'src/a/')
      expect(found).toBeTruthy()
      return found!
    })
    const host = container.querySelector('file-tree-container')
    let composedOrigin: EventTarget | undefined
    let resolvedDir: string | undefined

    container.addEventListener('drop', (event) => {
      composedOrigin = event.composedPath()[0]
      // Browsers retarget `event.target` to `host` for listeners outside this
      // shadow root; happy-dom preserves the inner target, so pass the browser's
      // retargeted value explicitly while retaining the real composed path.
      resolvedDir = resolveUploadTargetDir(ROOT, host, event.composedPath())
    }, { once: true })
    dirRow.dispatchEvent(new Event('drop', { bubbles: true, composed: true }))

    expect(composedOrigin).toBe(dirRow)
    expect(resolvedDir).toBe(`${ROOT}/src/a/`)
  })
})
