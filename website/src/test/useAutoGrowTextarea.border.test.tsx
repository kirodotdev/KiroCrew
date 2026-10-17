import React, { useRef } from 'react'
import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { useAutoGrowTextarea } from '../hooks/useAutoGrowTextarea'

/* scrollHeight leaves out the border, but `height` on a border-box element
 * includes it, so a measure that writes scrollHeight back shrinks a bordered
 * box by its border on the first keystroke. happy-dom has no layout, so the
 * box is given by getters: a 1px border makes offsetHeight 2px more than
 * clientHeight, and scrollHeight is padding plus content, no border. */

function Box({ value, maxH }: { value: string; maxH: number }) {
  const ref = useRef<HTMLTextAreaElement>(null)
  useAutoGrowTextarea(ref, value, maxH)
  return <textarea ref={ref} data-testid="ta" aria-label="auto-grow probe" />
}

function layOut(el: HTMLTextAreaElement, box: { scrollHeight: number; clientHeight: number; offsetHeight: number }) {
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => box.scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => box.clientHeight })
  Object.defineProperty(el, 'offsetHeight', { configurable: true, get: () => box.offsetHeight })
  Object.defineProperty(el, 'offsetParent', { configurable: true, get: () => document.body })
}

describe('useAutoGrowTextarea — border', () => {
  it('keeps the border in the grown height, so typing does not shrink the box', () => {
    const { getByTestId, rerender } = render(<Box value="" maxH={200} />)
    const ta = getByTestId('ta') as HTMLTextAreaElement
    // A one-line box at rest: 34px tall with a 1px border, 32px of padding and line.
    layOut(ta, { scrollHeight: 32, clientHeight: 32, offsetHeight: 34 })
    rerender(<Box value="a" maxH={200} />)
    expect(ta.style.height).toBe('34px')
    expect(ta.style.overflowY).toBe('hidden')
  })

  it('scrolls a draft at the cap instead of clipping it by the border', () => {
    const { getByTestId, rerender } = render(<Box value="" maxH={160} />)
    const ta = getByTestId('ta') as HTMLTextAreaElement
    // 159px of text needs 161px with the border: capped at 160, the content box
    // is 158px, so the last line is cut off unless the box scrolls.
    layOut(ta, { scrollHeight: 159, clientHeight: 158, offsetHeight: 160 })
    rerender(<Box value="long draft" maxH={160} />)
    expect(ta.style.height).toBe('160px')
    expect(ta.style.overflowY).toBe('auto')
  })

  it('sizes the sidebar rename on open with the same measure', async () => {
    const raw = await readFile(join(__dirname, '..', 'pages', 'chat-sidebar', 'rename.ts'), 'utf8')
    const s = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
    // A copy of the measurement here is how the border fix missed this box once.
    expect(s).toMatch(/measureAutoGrowTextarea\(el, RENAME_MAX_H\)/)
    expect(s).not.toMatch(/scrollHeight/)
  })

  it('sizes the selection composer with the same measure', async () => {
    const raw = await readFile(join(__dirname, '..', 'components', 'SelectionToolbar.tsx'), 'utf8')
    const s = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1')
    expect(s).toMatch(/measureAutoGrowTextarea\(el, COMPOSER_MAX_INPUT_H\)/)
    expect(s).not.toMatch(/scrollHeight/)
  })
})
