/**
 * The walker's `fileInput` method (`src/kiro_crew/browser_harness/snapshot_walker.js`),
 * which picks the `browser` tool's `upload` target: only a visible, enabled
 * `<input type=file>` passes (with whether it takes several files), and with `node`
 * the element itself comes back for
 * the helper worker's `DOM.setFileInputFiles`. The snapshot marks such an input
 * `file`.
 */
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setHtml } from './domFixture'
import { readSource } from './readSource'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readSource(join(HARNESS_DIR, 'snapshot_walker.js')).split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

interface Walker {
  fileInput: (ref: string, node?: boolean) => unknown
  snapshot: () => { nodes: Array<{ ref?: string; role: string; name: string; state?: Record<string, unknown> }> }
}

const walker = (): Walker => new Function(`return (${walkerSource()})`)() as Walker

const realElementRect = Element.prototype.getBoundingClientRect
const realComputedStyle = window.getComputedStyle.bind(window)

beforeEach(() => {
  // happy-dom lays nothing out: give every element a box and a visible style.
  Element.prototype.getBoundingClientRect = function () {
    return { x: 0, y: 0, left: 0, top: 0, width: 100, height: 20, right: 100, bottom: 20, toJSON: () => ({}) } as DOMRect
  }
  vi.spyOn(window, 'getComputedStyle').mockImplementation((el) => new Proxy(realComputedStyle(el), {
    get(style, key) {
      const value = Reflect.get(style, key)
      if (key === 'visibility') return value || 'visible'
      if (key === 'display' && (el as HTMLElement).id === 'hidden') return 'none'
      return value
    },
  }))
})

afterEach(() => {
  Element.prototype.getBoundingClientRect = realElementRect
  vi.restoreAllMocks()
  delete (window as unknown as Record<string, unknown>).__kcRefs
})

describe('fileInput', () => {
  function refsOf(): Record<string, string> {
    const w = walker()
    const out: Record<string, string> = {}
    for (const n of w.snapshot().nodes) if (n.ref) out[n.name] = n.ref
    return out
  }

  it('passes only an enabled, visible file input and marks it in the snapshot', () => {
    setHtml(
      document.body,
      '<label>CV <input id="f" type="file"></label>' +
        '<label>Name <input id="t" type="text"></label>' +
        '<label>Off <input id="d" type="file" disabled></label>' +
        '<label>Many <input id="m" type="file" multiple></label>',
    )
    const w = walker()
    const nodes = w.snapshot().nodes
    expect(nodes.find((n) => n.name === 'CV')?.state?.file).toBe(true)
    expect(nodes.find((n) => n.name === 'Name')?.state?.file).toBeUndefined()
    const refs = refsOf()
    expect(w.fileInput(refs.CV)).toEqual({ ok: true, multiple: false })
    expect(w.fileInput(refs.Many)).toEqual({ ok: true, multiple: true })
    expect(w.fileInput(refs.CV, true)).toBe(document.getElementById('f'))
    expect(w.fileInput(refs.Name)).toEqual({ ok: false, code: 'not_file_input' })
    expect(w.fileInput(refs.Name, true)).toBeNull()
    expect(w.fileInput(refs.Off)).toEqual({ ok: false, code: 'disabled' })
    expect(w.fileInput('e999')).toEqual({ ok: false, code: 'ref_stale' })
  })

  it('refuses a file input that is no longer shown', () => {
    setHtml(document.body, '<label>CV <input id="f" type="file"></label>')
    const ref = refsOf().CV
    document.getElementById('f')!.id = 'hidden'
    expect(walker().fileInput(ref)).toEqual({ ok: false, code: 'not_visible' })
  })
})
