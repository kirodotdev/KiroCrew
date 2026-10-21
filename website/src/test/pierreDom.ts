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
export function fillCodeBlock(host: HTMLElement, lines: string[][], firstLine = 1): void {
  const root = host.shadowRoot ?? host.attachShadow({ mode: 'open' })
  const style = document.createElement('style')
  style.textContent = '[data-line]{color:red}'
  const gutter = document.createElement('div')
  gutter.setAttribute('data-gutter', '')
  const content = document.createElement('div')
  content.setAttribute('data-content', '')
  lines.forEach((lineTokens, i) => {
    const lineNumber = firstLine + i
    const number = document.createElement('div')
    number.setAttribute('data-column-number', String(lineNumber))
    number.textContent = String(lineNumber)
    gutter.append(number)
    const line = document.createElement('div')
    line.setAttribute('data-line', String(lineNumber))
    for (const token of lineTokens) {
      const span = document.createElement('span')
      span.textContent = token
      line.append(span)
    }
    content.append(line)
  })
  root.replaceChildren(style, gutter, content)
}

export function codeBlock(lines: string[][], firstLine = 1): HTMLElement {
  const host = document.createElement('diffs-container')
  fillCodeBlock(host, lines, firstLine)
  return host
}

/** A real fenced-code unit in its plain, parked, or shown rendering state. */
export function codeBlockUnit(lines: string[][], state: 'plain' | 'parked' | 'shown' = 'shown') {
  const wrapper = document.createElement('div')
  wrapper.className = 'code-block'
  const header = document.createElement('div')
  const language = document.createElement('span')
  language.textContent = 'bash'
  const copyButton = document.createElement('button')
  copyButton.setAttribute('aria-label', 'Copy code')
  header.append(language, copyButton)
  const surface = document.createElement('div')
  surface.className = 'pierre-surface'
  wrapper.append(header, surface)

  const host = codeBlock(lines)
  const copy = document.createElement('pre')
  copy.className = 'pierre-plain'
  const code = document.createElement('code')
  code.textContent = lines.map(line => line.join('')).join('\n')
  copy.append(code)
  if (state === 'plain') surface.append(copy)
  else if (state === 'parked') surface.append(copy, parked(host))
  else surface.append(host)
  return { wrapper, host }
}

/** A multi-file `diff` fence, with one layout-neutral unit per file. */
export function multiFileDiffFence(files: string[][][], withHosts: boolean[]) {
  if (files.length !== withHosts.length) throw new Error('each diff file needs a host state')
  const wrapper = document.createElement('div')
  wrapper.className = 'diff-block'
  const surface = document.createElement('div')
  surface.className = 'pierre-surface'
  const hosts = files.map(lines => codeBlock(lines))
  const sections = files.map((lines, i) => {
    const section = document.createElement('div')
    section.className = 'contents'
    section.setAttribute('data-diff-section', String(i))
    section.append(withHosts[i] ? hosts[i] : plainCopy(lines.map(line => line.join('')).join('\n')))
    return section
  })
  surface.append(...sections)
  wrapper.append(surface)
  return { wrapper, surface, sections, hosts }
}

/** A one-file `diff` fence in its shown or plain rendering state. */
export function diffFence(lines: string[][], withHost = true) {
  const fence = multiFileDiffFence([lines], [withHost])
  return { ...fence, section: fence.sections[0], host: fence.hosts[0] }
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
 * A mouse drag over `mycli rm NAME` in one line of a plain code fallback.
 * The fallback keeps every source line in one text node beneath its `<pre>`.
 */
export function dragInPlainCode(block: HTMLElement, { lineIndex = 0, start = 0, end = 13, text }: { lineIndex?: number; start?: number; end?: number; text?: string } = {}) {
  const pre = block.matches('pre') ? block : block.querySelector('pre')
  if (!pre) throw new Error('plain code block has no pre')
  const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT)
  const node = walker.nextNode() as Text | null
  if (!node || walker.nextNode()) throw new Error('plain code fallback must hold one text node')
  const sourceLines = node.data.split('\n')
  const lineStart = sourceLines.slice(0, lineIndex).reduce((offset, line) => offset + line.length + 1, 0)
  const range = document.createRange()
  range.setStart(node, lineStart + start)
  range.setEnd(node, lineStart + end)
  const selection = {
    isCollapsed: false,
    rangeCount: 1,
    anchorNode: node,
    focusNode: node,
    toString: () => text ?? range.toString(),
    getRangeAt: () => range,
    addRange: vi.fn(),
    removeAllRanges: vi.fn(),
  }
  vi.spyOn(window, 'getSelection').mockReturnValue(selection as unknown as Selection)
  fireEvent.mouseUp(document, { clientX: 100, clientY: 50 })
  return { range, selection }
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
export function dragInCode(host: HTMLElement, { withComposedRanges = true, text, lineIndex = 0 }: { withComposedRanges?: boolean; text?: string; lineIndex?: number } = {}) {
  const [first, , third] = [...host.shadowRoot!.querySelectorAll(`[data-line]:nth-of-type(${lineIndex + 1}) span`)].map(span => span.firstChild as Text)
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
