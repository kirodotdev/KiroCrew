import { vi } from 'vitest'
import { fireEvent } from '@testing-library/react'

/**
 * DOM shaped like the code blocks Pierre draws, for tests that search or select
 * inside them without loading the lazy Pierre chunk. The shape was measured in a
 * browser: an open shadow root on a `diffs-container` host holding a `<style>`, a
 * gutter of line numbers, and one `[data-line]` per source line with a text node
 * per token.
 */

/** A source line split into Pierre-like tokens: `mycli rm NAME` -> `mycli`, ` rm`, ` NAME`. */
export function tokens(line: string): string[] {
  return line.split(/(?= )/)
}

/** Fills `host`'s open shadow root, creating it, with `lines` (one array of tokens per source line). */
export function fillCodeBlock(host: HTMLElement, lines: string[][]): void {
  const root = host.shadowRoot ?? host.attachShadow({ mode: 'open' })
  const style = document.createElement('style')
  style.textContent = '[data-line]{color:red}'
  const gutter = document.createElement('div')
  gutter.setAttribute('data-gutter', '')
  const content = document.createElement('div')
  content.setAttribute('data-content', '')
  lines.forEach((lineTokens, i) => {
    const number = document.createElement('div')
    number.setAttribute('data-column-number', String(i + 1))
    number.textContent = String(i + 1)
    gutter.append(number)
    const line = document.createElement('div')
    line.setAttribute('data-line', String(i + 1))
    for (const token of lineTokens) {
      const span = document.createElement('span')
      span.textContent = token
      line.append(span)
    }
    content.append(line)
  })
  root.replaceChildren(style, gutter, content)
}

export function codeBlock(lines: string[][]): HTMLElement {
  const host = document.createElement('diffs-container')
  fillCodeBlock(host, lines)
  return host
}

export function para(text: string): HTMLElement {
  const p = document.createElement('p')
  p.textContent = text
  return p
}

/** A root holding `children`, attached to the document. */
export function page(...children: Node[]): HTMLElement {
  const root = document.createElement('div')
  root.append(...children)
  document.body.append(root)
  return root
}

/** What `WarmSwap` does with the highlighted copy until it paints: an `aria-hidden` box beside the plain copy. */
export function parked(host: HTMLElement): HTMLElement {
  const box = document.createElement('div')
  box.setAttribute('aria-hidden', 'true')
  box.append(host)
  return box
}

/** The plain copy `WarmSwap` shows while the highlighted one is parked. */
export function plainCopy(text: string): HTMLElement {
  const pre = document.createElement('pre')
  pre.textContent = text
  return pre
}

/**
 * A mouse drag inside a code block, from character 0 of its first token to the
 * end of its third, as Chrome reports it: `getComposedRanges` holds the real range,
 * `getRangeAt` a range collapsed on the host's parent, and the selection is
 * `isCollapsed` though `toString()` holds the text. happy-dom has no selection
 * across a shadow root, so this installs one. `withComposedRanges: false` is an
 * engine without the method; `text` overrides what the selection holds. Restore
 * `window.getSelection` with `vi.restoreAllMocks()`.
 */
export function dragInCode(host: HTMLElement, { withComposedRanges = true, text }: { withComposedRanges?: boolean; text?: string } = {}) {
  const [first, , third] = [...host.shadowRoot!.querySelectorAll('[data-line] span')].map(span => span.firstChild as Text)
  const composed = document.createRange()
  composed.setStart(first, 0)
  composed.setEnd(third, third.data.length)
  const retargeted = document.createRange()
  retargeted.setStart(host.parentNode!, [...host.parentNode!.childNodes].indexOf(host))
  retargeted.collapse(true)
  const selection = {
    isCollapsed: true,
    rangeCount: 1,
    toString: () => text ?? composed.toString(),
    getRangeAt: () => retargeted,
    ...(withComposedRanges ? { getComposedRanges: vi.fn(() => [composed]) } : {}),
    addRange: vi.fn(),
    removeAllRanges: vi.fn(),
  }
  vi.spyOn(window, 'getSelection').mockReturnValue(selection as unknown as Selection)
  fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
  return selection
}
