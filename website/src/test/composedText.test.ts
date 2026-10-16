/**
 * Anchored comments on fenced code. A highlighted code block renders through
 * Pierre inside an OPEN shadow root, which every light-DOM measure stops at:
 * these scenes pin the composed text space the comment layer now measures in,
 * the selection lookup that sees past the boundary, and that a body with no
 * shadow root measures exactly as before.
 */
import { describe, it, expect, afterEach } from 'vitest'
import {
  codeRangesWithin,
  composedContains,
  composedTextOffset,
  indexComposedText,
  liveSelectionRange,
  rangeForTextSpan,
} from '../utils/composedText'
import { anchorFromRange } from '../utils/selectionAnchor'
import { containedSelectionRange } from '../utils/selectionContainment'
import { rangeForAnchor } from '../hooks/useMarkdownCommentHighlights'
import { rangeFromTextOffset } from '../components/SelectionToolbar'

const CODE_LINES = ['<service name="orders">', '  <timeout unit="ms">2500</timeout>', '</service>']

/** Pierre's shape: a sprite, a style sheet, a line-number gutter, then one
 *  `[data-line]` row per source line with the tokens as spans. */
function pierreHost(lines = CODE_LINES): HTMLElement {
  const host = document.createElement('diffs-container')
  const sr = host.attachShadow({ mode: 'open' })
  sr.innerHTML = '<svg><symbol>\n  </symbol></svg><style>[data-line]{color:red}</style>'
  const pre = document.createElement('pre')
  const gutter = document.createElement('div')
  lines.forEach((_, i) => {
    const n = document.createElement('div')
    n.setAttribute('data-column-number', String(i + 1))
    n.innerHTML = `<span data-line-number-content="">${i + 1}</span>`
    gutter.appendChild(n)
  })
  pre.appendChild(gutter)
  for (const [i, line] of lines.entries()) {
    const row = document.createElement('div')
    row.setAttribute('data-line', String(i + 1))
    // Split mid-line, as the highlighter's token spans do.
    const cut = Math.min(3, line.length)
    for (const part of [line.slice(0, cut), line.slice(cut)]) {
      if (!part) continue
      const s = document.createElement('span')
      s.textContent = part
      row.appendChild(s)
    }
    pre.appendChild(row)
  }
  sr.appendChild(pre)
  return host
}

/** A markdown body: a paragraph, the code block, a paragraph. */
function body(code: HTMLElement): HTMLElement {
  const root = document.createElement('div')
  const p1 = document.createElement('p')
  p1.textContent = 'We propose this XML.'
  const p2 = document.createElement('p')
  p2.textContent = 'The timeout is a guess.'
  const block = document.createElement('div')
  block.setAttribute('data-anchorable-code', '')
  block.appendChild(code)
  root.append(p1, block, p2)
  document.body.appendChild(root)
  return root
}

/** CodeBlock's plain stand-in before highlighting lands. */
function standIn(): HTMLElement {
  const pre = document.createElement('pre')
  const c = document.createElement('code')
  c.textContent = CODE_LINES.join('\n')
  pre.appendChild(c)
  return pre
}

/** A Range over `needle` inside the shadow rows of `host`. */
function shadowRange(host: HTMLElement, needle: string): Range {
  const idx = indexComposedText(host)
  const at = idx.text.indexOf(needle)
  const r = rangeForTextSpan(idx, at, at + needle.length)
  if (!r) throw new Error(`no range for ${needle}`)
  return r
}

afterEach(() => { document.body.innerHTML = '' })

describe('indexComposedText', () => {
  it('splices the code rows into the light text, line-broken, without gutter, sprite or style', () => {
    const root = body(pierreHost())
    expect(indexComposedText(root).text).toBe(`We propose this XML.${CODE_LINES.join('\n')}The timeout is a guess.`)
  })

  it('reads the same as the plain stand-in, so an anchor survives the highlight swap', () => {
    const highlighted = indexComposedText(body(pierreHost())).text
    document.body.innerHTML = ''
    const plain = indexComposedText(body(standIn())).text
    expect(highlighted).toBe(plain)
  })

  it('is the plain textContent on a body with no shadow root (existing offsets unchanged)', () => {
    const root = body(standIn())
    expect(indexComposedText(root).text).toBe(root.textContent)
  })
})

describe('blank lines and block edges', () => {
  /** Pierre renders a blank source line as a row holding one "\n" Text node. */
  function withBlankRow(): HTMLElement {
    const host = pierreHost(['a', 'b'])
    const rows = host.shadowRoot!.querySelectorAll('[data-line]')
    const blank = document.createElement('div')
    blank.setAttribute('data-line', 'blank')
    blank.appendChild(document.createTextNode('\n'))
    rows[0].after(blank)
    body(host)
    return host
  }

  it('counts a blank row once, as the plain stand-in does', () => {
    const host = withBlankRow()
    expect(indexComposedText(host).text).toBe('a\n\nb')
  })

  it('measures a selection boundary inside the blank row', () => {
    const host = withBlankRow()
    const idx = indexComposedText(host)
    const filler = host.shadowRoot!.querySelector('[data-line="blank"]')!.firstChild!
    expect(composedTextOffset(idx, filler, 0)).toBe(idx.text.indexOf('b'))
  })

  it('a point after the last code row ends at that row, not at the end of the document', () => {
    const host = pierreHost()
    const root = body(host)
    const pre = host.shadowRoot!.querySelector('pre')!
    const idx = indexComposedText(root)
    const first = idx.text.indexOf('</service>')
    const r = rangeForTextSpan(idx, first, first + 2)!
    r.setEnd(pre, pre.childNodes.length)
    const a = anchorFromRange(root, r)!
    expect(a.quote).toBe('</service>')
  })
})

describe('composedContains', () => {
  it('crosses a shadow boundary on the way up; Node.contains does not', () => {
    const host = pierreHost()
    const root = body(host)
    const inner = host.shadowRoot!.querySelector('[data-line] span')!.firstChild!
    expect(root.contains(inner)).toBe(false)
    expect(composedContains(root, inner)).toBe(true)
    expect(composedContains(document.createElement('div'), inner)).toBe(false)
  })
})

describe('anchoring a selection inside a code block', () => {
  it('the toolbar containment accepts it (it used to reject every one)', () => {
    const host = pierreHost()
    const root = body(host)
    const r = shadowRange(host, '2500')
    expect(containedSelectionRange(r, root)).toBe(r)
  })

  it('anchors the quote at its offset in the composed text, with prose context', () => {
    const host = pierreHost()
    const root = body(host)
    const a = anchorFromRange(root, shadowRange(host, '2500'))!
    const text = indexComposedText(root).text
    expect(a.quote).toBe('2500')
    expect(a.startOffset).toBe(text.indexOf('2500'))
    expect(a.endOffset).toBe(a.startOffset + 4)
    expect(a.prefix).toBe(text.slice(a.startOffset - 32, a.startOffset))
    expect(a.suffix.startsWith('</timeout>\n</service>')).toBe(true)
  })

  it('keeps the line break in a quote that spans two code rows', () => {
    const host = pierreHost()
    const root = body(host)
    const a = anchorFromRange(root, shadowRange(host, '"orders">\n  <timeout'))!
    expect(a.quote).toBe('"orders">\n  <timeout')
  })

  it('gives the same anchor whether the block was highlighted or still plain', () => {
    const host = pierreHost()
    const root = body(host)
    const highlighted = anchorFromRange(root, shadowRange(host, 'unit="ms"'))!
    document.body.innerHTML = ''
    const plainRoot = body(standIn())
    const code = plainRoot.querySelector('code')!.firstChild as Text
    const r = document.createRange()
    const at = code.data.indexOf('unit="ms"')
    r.setStart(code, at)
    r.setEnd(code, at + 'unit="ms"'.length)
    expect(anchorFromRange(plainRoot, r)).toEqual(highlighted)
  })

  it('leaves a prose anchor exactly as Range.toString measured it', () => {
    const root = body(pierreHost())
    const p2 = root.lastChild!.firstChild as Text
    const r = document.createRange()
    r.setStart(p2, 4)
    r.setEnd(p2, 11)
    const a = anchorFromRange(root, r)!
    expect(a.quote).toBe('timeout')
    const text = indexComposedText(root).text
    expect(a.startOffset).toBe(text.lastIndexOf('timeout'))
  })

  it('measures an element boundary point after the code block', () => {
    const root = body(pierreHost())
    const idx = indexComposedText(root)
    expect(composedTextOffset(idx, root, 2)).toBe(idx.text.indexOf('The timeout'))
    expect(composedTextOffset(idx, root, 3)).toBe(idx.text.length)
  })
})

describe('re-anchoring a stored comment on code', () => {
  it('rangeForAnchor finds the code occurrence inside the shadow tree', () => {
    const host = pierreHost()
    const root = body(host)
    const idx = indexComposedText(root)
    const r = rangeForAnchor(idx, { id: 'c', quote: 'timeout', prefix: '<', suffix: ' unit', startOffset: idx.text.indexOf('timeout') })!
    expect(r.toString()).toBe('timeout')
    expect(r.startContainer.getRootNode()).toBe(host.shadowRoot)
  })

  it('a quote that opens the code block starts inside the code, not at the end of the prose before it', () => {
    const host = pierreHost()
    const root = body(host)
    const idx = indexComposedText(root)
    const r = rangeForAnchor(idx, { id: 'c', quote: '<service', prefix: '', suffix: '' })!
    expect(r).not.toBeNull()
    expect(r.startContainer.getRootNode()).toBe(host.shadowRoot)
    expect(r.toString()).toBe('<service')
  })

  it('refuses a span that would cross the shadow boundary (a Range cannot)', () => {
    const root = body(pierreHost())
    const idx = indexComposedText(root)
    const at = idx.text.indexOf('XML.')
    expect(rangeForTextSpan(idx, at, at + 8)).toBeNull()
  })

  it('a drag from prose to just past the block anchors and re-anchors', () => {
    const host = pierreHost()
    const root = body(host)
    const p1 = root.firstChild!.firstChild as Text
    const r = document.createRange()
    r.setStart(p1, 3)
    r.setEnd(host.parentNode!, 1)
    const a = anchorFromRange(root, r)!
    expect(a.quote.endsWith('</service>')).toBe(true)
    const back = rangeForAnchor(indexComposedText(root), { id: 'c', quote: a.quote, prefix: a.prefix, suffix: a.suffix, startOffset: a.startOffset })
    expect(back).not.toBeNull()
  })

  it('leaves other shadow-rendered code (editor, windowed views, patches) out', () => {
    const host = pierreHost()
    const root = body(host)
    const r = shadowRange(host, '2500')
    host.parentElement!.removeAttribute('data-anchorable-code')
    expect(indexComposedText(root).text).toBe('We propose this XML.The timeout is a guess.')
    const sel = { rangeCount: 1, getRangeAt: () => r, toString: () => '2500' } as unknown as Selection
    expect(liveSelectionRange(sel)).toBeNull()
  })

  it('re-anchors a block edge that a trimmed quote leaves inside indentation or trailing space', () => {
    const host = pierreHost(['  indented: true', 'last: x  '])
    const root = body(host)
    const idx = indexComposedText(root)
    // Prose into the block, ending before the last row's trailing spaces.
    const end = idx.text.indexOf('last: x') + 'last: x'.length
    const r1 = rangeForTextSpan(idx, idx.text.indexOf('XML.'), end)!
    expect(r1.endContainer.getRootNode()).toBe(document)
    expect(r1.intersectsNode(host)).toBe(true)
    // The block from its first non-space character into the prose below.
    const r2 = rangeForTextSpan(idx, idx.text.indexOf('indented'), idx.text.indexOf('The timeout') + 3)!
    expect(r2.startContainer.getRootNode()).toBe(document)
    // A span ending mid-block is still refused.
    expect(rangeForTextSpan(idx, idx.text.indexOf('XML.'), idx.text.indexOf('indented') + 3)).toBeNull()
  })

  it('a drag from mid-code past the block into whitespace anchors the code part', () => {
    const host = pierreHost()
    const root = body(host)
    root.lastChild!.remove()
    const row2 = host.shadowRoot!.querySelectorAll('[data-line]')[1].lastChild!.firstChild as Text
    const light = document.createRange()
    light.setStart(host.parentNode!, 0)
    light.setEnd(root, root.childNodes.length)
    const real = { startContainer: row2, startOffset: 2, endContainer: root, endOffset: root.childNodes.length, collapsed: false } as unknown as StaticRange
    const sel = {
      rangeCount: 1, anchorNode: host.parentNode, focusNode: root, isCollapsed: false,
      getRangeAt: () => light, toString: () => 'x', getComposedRanges: () => [real],
    } as unknown as Selection
    const live = liveSelectionRange(sel)!
    expect(live.startContainer.getRootNode()).toBe(host.shadowRoot)
    expect(anchorFromRange(root, live)!.quote.endsWith('</service>')).toBe(true)
  })

  it('a drag from prose into a block leaves out the block header label', () => {
    const host = pierreHost()
    const root = body(host)
    const surface = host.parentElement!
    const block = document.createElement('div')
    block.className = 'code-block'
    const label = document.createElement('span')
    label.textContent = 'xml'
    surface.replaceWith(block)
    block.append(label, surface)
    const p1 = root.firstChild!.firstChild as Text
    const row = host.shadowRoot!.querySelector('[data-line] span')!.firstChild as Text
    // What Chromium reports for this drag: the end ON the host, `(host, 0)`.
    const light = document.createRange()
    light.setStart(p1, 3)
    light.setEnd(host, 0)
    const real = { startContainer: p1, startOffset: 3, endContainer: row, endOffset: 2, collapsed: false } as unknown as StaticRange
    const sel = {
      rangeCount: 1, anchorNode: p1, focusNode: host, isCollapsed: false,
      getRangeAt: () => light, toString: () => 'x',
      getComposedRanges: (o: { shadowRoots: ShadowRoot[] }) => (o.shadowRoots.includes(host.shadowRoot!) ? [real] : [light]),
    } as unknown as Selection
    expect(liveSelectionRange(sel)!.toString()).toBe('propose this XML.')
  })

  it('a drag from the end of one block into the next anchors the part in the next block', () => {
    const a = pierreHost(['alpha'])
    const b = pierreHost(['omega'])
    const root = body(a)
    const blockB = document.createElement('div')
    blockB.setAttribute('data-anchorable-code', '')
    blockB.appendChild(b)
    root.insertBefore(blockB, root.lastChild)
    const ta = [...a.shadowRoot!.querySelectorAll('[data-line] span')].at(-1)!.firstChild as Text
    const tb = b.shadowRoot!.querySelector('[data-line] span')!.firstChild as Text
    const light = document.createRange()
    light.setStart(a.parentNode!, 0)
    light.setEnd(blockB, 1)
    // Starts at the very end of A's only row: A's part is empty.
    const real = { startContainer: ta, startOffset: ta.length, endContainer: tb, endOffset: 2, collapsed: false } as unknown as StaticRange
    const sel = {
      rangeCount: 1, anchorNode: a.parentNode, focusNode: blockB, isCollapsed: false,
      getRangeAt: () => light, toString: () => 'x', getComposedRanges: () => [real],
    } as unknown as Selection
    expect(liveSelectionRange(sel)!.toString()).toBe('om')
  })

  it('a whole-block range lists the block rows for painting', () => {
    const host = pierreHost()
    const root = body(host)
    const r = document.createRange()
    r.setStart(root.firstChild!.firstChild!, 0)
    r.setEnd(root.lastChild!.firstChild!, 3)
    const rows = codeRangesWithin(r).map(x => x.toString())
    expect(rows.join('')).toBe(CODE_LINES.join('').replace(/\n/g, ''))
  })

  it('a selection from one code block into another keeps only the prose between them', () => {
    const a = pierreHost(['alpha'])
    const b = pierreHost(['omega'])
    const root = body(a)
    const blockB = document.createElement('div')
    blockB.setAttribute('data-anchorable-code', '')
    blockB.appendChild(b)
    root.insertBefore(blockB, root.lastChild)
    const ta = a.shadowRoot!.querySelector('[data-line] span')!.firstChild as Text
    const tb = b.shadowRoot!.querySelector('[data-line] span')!.firstChild as Text
    const light = document.createRange()
    light.setStart(a.parentNode!, 0)
    light.setEnd(blockB, 1)
    const real = { startContainer: ta, startOffset: 1, endContainer: tb, endOffset: 2, collapsed: false } as unknown as StaticRange
    const sel = {
      rangeCount: 1, anchorNode: a.parentNode, focusNode: blockB, isCollapsed: false,
      getRangeAt: () => light, toString: () => 'x', getComposedRanges: () => [real],
    } as unknown as Selection
    // Nothing but the blocks lies between them: the part inside the block the
    // drag started in is what the comment anchors.
    expect(liveSelectionRange(sel)!.toString()).toBe('lpha')
  })

  it('rangeFromTextOffset puts a code selection back from its saved offsets', () => {
    const host = pierreHost()
    const root = body(host)
    const at = indexComposedText(root).text.indexOf('2500')
    expect(rangeFromTextOffset(root, at, 4)?.toString()).toBe('2500')
  })
})

describe('liveSelectionRange', () => {
  /** What Chromium reports for a drag inside a shadow tree: one collapsed
   *  range at the host's light-DOM parent, the code still in toString(). */
  function retargeted(host: HTMLElement, real: Range | null, composed = true): Selection {
    const parent = host.parentElement!
    const collapsed = document.createRange()
    collapsed.setStart(parent, 0)
    const sel = {
      rangeCount: 1,
      anchorNode: parent,
      focusNode: parent,
      isCollapsed: true,
      getRangeAt: () => collapsed,
      toString: () => real?.toString() ?? '',
    } as unknown as Selection
    if (composed) {
      Object.assign(sel, {
        getComposedRanges: (opts: { shadowRoots: ShadowRoot[] }) => (
          real && opts.shadowRoots.includes(host.shadowRoot!) ? [real] : [collapsed]
        ),
      })
    }
    return sel
  }

  it('returns an ordinary selection range untouched', () => {
    const root = body(standIn())
    const r = document.createRange()
    r.selectNodeContents(root.firstChild!)
    const sel = { rangeCount: 1, getRangeAt: () => r, toString: () => r.toString() } as unknown as Selection
    expect(liveSelectionRange(sel)).toBe(r)
  })

  it('a selection crossing the edge of a code block anchors only its part outside the block', () => {
    const host = pierreHost()
    const root = body(host)
    const p2 = root.lastChild!.firstChild as Text
    const lastRow = [...host.shadowRoot!.querySelectorAll('[data-line] span')].at(-1)!.firstChild as Text
    // Chromium hands the light-DOM range back covering the whole host...
    const light = document.createRange()
    light.setStart(host.parentNode!, 0)
    light.setEnd(p2, 3)
    // ...while the composed range starts inside the last row.
    const real = { startContainer: lastRow, startOffset: 0, endContainer: p2, endOffset: 3, collapsed: false } as unknown as StaticRange
    const sel = {
      rangeCount: 1, anchorNode: host.parentNode, focusNode: p2, isCollapsed: false,
      getRangeAt: () => light, toString: () => 'x',
      getComposedRanges: () => [real],
    } as unknown as Selection
    const live = liveSelectionRange(sel)!
    const a = anchorFromRange(root, live)!
    expect(live.startContainer.getRootNode()).toBe(document)
    expect(a.quote).toBe('The')
  })

  it('sees through a retargeted selection to the code it covers', () => {
    const host = pierreHost()
    body(host)
    const real = shadowRange(host, '2500')
    const live = liveSelectionRange(retargeted(host, real))!
    expect(live.toString()).toBe('2500')
    expect(live.startContainer.getRootNode()).toBe(host.shadowRoot)
  })

  it('is null for an empty selection, and where the engine has no composed ranges', () => {
    const host = pierreHost()
    body(host)
    expect(liveSelectionRange(null)).toBeNull()
    expect(liveSelectionRange(retargeted(host, null))).toBeNull()
    expect(liveSelectionRange(retargeted(host, shadowRange(host, '2500'), false))).toBeNull()
  })
})
