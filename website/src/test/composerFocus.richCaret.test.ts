/**
 * The Lexical composer (Style Markdown While Typing) is a contenteditable div,
 * not a textarea. A bare `focus()` on it leaves the caret at offset 0, so after
 * a pre-fill (quote-to-compose, an `@`-file mention, launch intake) the next
 * character would be typed in FRONT of the draft. The textarea path keeps the
 * caret after the text, and the rich composer must match it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { focusComposer, revealComposer } from '../pages/chat/composerFocus'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))

const flushFrame = async () => {
  await Promise.resolve()
  await new Promise<void>(r => requestAnimationFrame(() => r()))
  await Promise.resolve()
}

let rich: HTMLDivElement

beforeEach(() => {
  rich = document.createElement('div')
  rich.setAttribute('data-composer-input', '')
  rich.setAttribute('contenteditable', 'true')
  rich.tabIndex = 0
  const p = document.createElement('p')
  p.textContent = '> quoted text'
  rich.appendChild(p)
  document.body.appendChild(rich)
  window.getSelection()?.removeAllRanges()
})
afterEach(() => { rich.remove() })

/** True when the document caret is collapsed at the very end of `el`. */
function caretAtEnd(el: HTMLElement): boolean {
  const sel = window.getSelection()
  if (!sel || sel.rangeCount === 0 || !sel.isCollapsed) return false
  const end = document.createRange()
  end.selectNodeContents(el)
  end.collapse(false)
  return sel.getRangeAt(0).compareBoundaryPoints(Range.START_TO_START, end) === 0
}

describe('focusing the rich composer', () => {
  it('revealComposer puts the caret after a pre-filled draft', async () => {
    revealComposer()
    await flushFrame()
    expect(document.activeElement).toBe(rich)
    expect(caretAtEnd(rich)).toBe(true)
  })

  it('focusComposer puts the caret after the draft too', async () => {
    focusComposer()
    await flushFrame()
    expect(document.activeElement).toBe(rich)
    expect(caretAtEnd(rich)).toBe(true)
  })
})
