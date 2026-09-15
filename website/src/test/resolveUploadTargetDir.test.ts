import { describe, expect, it } from 'vitest'
import { resolveUploadTargetDir } from '../pages/chat/resolveUploadTargetDir'

const ROOT = '/repo/project'

/** Builds a bare DOM node carrying the same attributes `@pierre/trees`
 *  stamps on each rendered row (see its `rowAttributes.ts`), without loading
 *  the trees runtime at all. */
function row(attrs: { path: string; type: 'folder' | 'file'; parentPath?: string }): HTMLElement {
  const el = document.createElement('div')
  el.setAttribute('data-item-path', attrs.path)
  el.setAttribute('data-item-type', attrs.type)
  if (attrs.parentPath) el.setAttribute('data-item-parent-path', attrs.parentPath)
  return el
}

describe('resolveUploadTargetDir', () => {
  it('targets a directory row itself', () => {
    const el = row({ path: 'src/components', type: 'folder', parentPath: 'src' })
    expect(resolveUploadTargetDir(ROOT, el)).toBe(`${ROOT}/src/components`)
  })

  it('targets a file row\'s parent directory', () => {
    const el = row({ path: 'src/components/App.tsx', type: 'file', parentPath: 'src/components' })
    expect(resolveUploadTargetDir(ROOT, el)).toBe(`${ROOT}/src/components`)
  })

  it('falls back to root for a top-level file row (no parent-path attribute)', () => {
    const el = row({ path: 'README.md', type: 'file' })
    expect(resolveUploadTargetDir(ROOT, el)).toBe(ROOT)
  })

  it('targets root for a top-level directory row', () => {
    const el = row({ path: 'src', type: 'folder' })
    expect(resolveUploadTargetDir(ROOT, el)).toBe(`${ROOT}/src`)
  })

  it('falls back to root when the drop lands on a descendant of a row (an icon, a label span)', () => {
    const el = row({ path: 'src/components', type: 'folder', parentPath: 'src' })
    const icon = document.createElement('span')
    el.appendChild(icon)
    expect(resolveUploadTargetDir(ROOT, icon)).toBe(`${ROOT}/src/components`)
  })

  it('falls back to root when the drop matches no row at all (the header, empty tree space)', () => {
    const header = document.createElement('div')
    expect(resolveUploadTargetDir(ROOT, header)).toBe(ROOT)
  })

  it('falls back to root for a null event target', () => {
    expect(resolveUploadTargetDir(ROOT, null)).toBe(ROOT)
  })

  it('falls back to root for a non-Element EventTarget (e.g. window)', () => {
    expect(resolveUploadTargetDir(ROOT, window)).toBe(ROOT)
  })
})
