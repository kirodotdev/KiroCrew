/**
 * What a text selection hands to Quote / Ask / Copy once a link is unfurled.
 *
 * The rule these lock in: the quoted text carries the URL the MODEL wrote, not
 * the title the endpoint fetched. Unfurling replaces the visible URL with the
 * page title, so a selection built from rendered characters loses the URL
 * outright — verified in a real browser across two inline chips.
 *
 * The first implementation did that substitution by searching
 * `Selection.toString()` for each element's rendered text, and review found
 * three reachable defects in it. All three are locked in below as CASE A/B/D,
 * because each one is invisible from the case that was actually browser-tested:
 *
 *   A. A title that also appears as literal prose earlier in the selection made
 *      `indexOf` rewrite the PROSE and leave the chip showing its title.
 *   B. A block card's real paragraph breaks were absorbed as if they were the
 *      spurious newlines a chip injects, merging three paragraphs into one line.
 *   D. A card never matched at all: its three block spans put newlines in
 *      `toString()` that its `textContent` does not have, so `indexOf` returned
 *      −1 and the URL was silently never substituted.
 *
 * The current implementation replaces each node BY POSITION during a DOM walk,
 * so there is no text to match and none of the three can recur.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { selectionTextFrom } from '../components/SelectionToolbar'

/**
 * Attach a parsed fixture and return its host.
 *
 * Built with `createContextualFragment` + `replaceChildren` rather than an
 * assignment to innerHTML, which the repo's frontend-security rule blocks
 * across `src/**` — tests are not excluded, and these are the APIs it names.
 */
function mountFixture(html: string): HTMLElement {
  const host = document.createElement('div')
  host.appendChild(document.createRange().createContextualFragment(html))
  document.body.appendChild(host)
  return host
}

/** Select everything inside `html`, attached so `getComputedStyle` is real. */
function selectAll(html: string): Selection {
  const host = mountFixture(html)
  const range = document.createRange()
  range.selectNodeContents(host)
  const sel = window.getSelection()!
  sel.removeAllRanges()
  sel.addRange(range)
  return sel
}

/** A Selection whose `toString()` is fixed, with a real range for the DOM walk. */
function selectionWithText(html: string, rendered: string): Selection {
  const sel = selectAll(html)
  return {
    toString: () => rendered,
    getRangeAt: (i: number) => sel.getRangeAt(i),
    rangeCount: sel.rangeCount,
  } as unknown as Selection
}

/** The chip's real shape: anchor + copy button as siblings in a container. */
const chip = (title: string, url: string) =>
  `<span><a data-unfurl-url="${url}"><span aria-hidden="true"></span>` +
  `<span>${title}</span></a><button aria-label="Copy URL"></button></span>`

/** The card's real shape: title, description and domain as block spans. */
const card = (title: string, desc: string, domain: string, url: string) =>
  `<span><a data-unfurl-url="${url}"><span>${title}</span>` +
  `<span>${desc}</span><span>${domain}</span></a><button></button></span>`

afterEach(() => {
  document.body.replaceChildren()
  window.getSelection()?.removeAllRanges()
})

describe('selectionTextFrom', () => {
  it('leaves an ordinary selection untouched', () => {
    const sel = selectAll('<p>plain sentence with no links</p>')
    expect(selectionTextFrom(sel)).toBe('plain sentence with no links')
  })

  it('substitutes the model-written URL for the fetched title', () => {
    const sel = selectAll(`<p>shopping is ${chip('Shop Example. Great prices, fast delivery.', 'https://shop.example')} today</p>`)
    const out = selectionTextFrom(sel)
    expect(out).toBe('shopping is https://shop.example today')
    expect(out).not.toContain('Great prices')
  })

  it('substitutes every unfurled link in the selection, in order', () => {
    const sel = selectAll(
      `<p>${chip('First Title', 'https://a.example/1')} and ${chip('Second Title', 'https://b.example/2')}</p>`,
    )
    expect(selectionTextFrom(sel)).toBe('https://a.example/1 and https://b.example/2')
  })

  it('keeps both URLs when two links share the same fetched title', () => {
    // Distinct URLs can resolve to identical titles (a site's 404 page, say).
    const sel = selectAll(
      `<p>${chip('Same Title', 'https://a.example/x')} then ${chip('Same Title', 'https://b.example/y')}</p>`,
    )
    expect(selectionTextFrom(sel)).toBe('https://a.example/x then https://b.example/y')
  })

  it('CASE A: a title that also occurs as prose earlier does not corrupt the prose', () => {
    // The old code ran indexOf('Google') from offset 0, hit the sentence's own
    // first word, and produced "https://www.google.com is great, see Google" —
    // rewriting text the user selected and leaving the link's title in place.
    const sel = selectAll(`<p>Google is great, see ${chip('Google', 'https://www.google.com')}</p>`)
    expect(selectionTextFrom(sel)).toBe('Google is great, see https://www.google.com')
  })

  it('CASE B: a block card keeps the paragraph breaks around it', () => {
    // The old whitespace absorption ate the \n\n either side, collapsing three
    // paragraphs into "First para. https://... Second para.".
    const sel = selectAll(
      `<p>First para.</p>${card('Shop Example', 'Great prices.', 'shop.example', 'https://shop.example')}<p>Second para.</p>`,
    )
    expect(selectionTextFrom(sel)).toBe(
      'First para.\n\nhttps://shop.example\n\nSecond para.',
    )
  })

  it('CASE D: a card is substituted even though its rendered text has newlines its textContent lacks', () => {
    // A browser's toString() yields "Repo Title\nA description.\ngithub.com";
    // textContent yields the same characters with no newlines, so the old
    // indexOf never matched and the URL was silently dropped. The stubbed
    // toString here is deliberately the browser's form, newlines included.
    const sel = selectionWithText(
      `<p>see</p>${card('Repo Title', 'A description.', 'github.com', 'https://github.com/o/r')}`,
      'see\nRepo Title\nA description.\ngithub.com',
    )
    const out = selectionTextFrom(sel)
    expect(out).toBe('see\n\nhttps://github.com/o/r')
    expect(out).not.toContain('A description.')
  })

  it('ignores toString() entirely once a link is unfurled', () => {
    // Proves the walk is the source of truth: a toString() that shares nothing
    // with the DOM cannot influence the result.
    const sel = selectionWithText(
      `<p>real text ${chip('Title', 'https://real.example')}</p>`,
      'COMPLETELY UNRELATED STRING',
    )
    expect(selectionTextFrom(sel)).toBe('real text https://real.example')
  })

  it('does not invent a break around an inline chip', () => {
    const sel = selectAll(`<p>before ${chip('T', 'https://x.example')} after</p>`)
    expect(selectionTextFrom(sel)).toBe('before https://x.example after')
  })

  it('preserves whitespace inside a code block', () => {
    // Collapsing runs of whitespace is right for wrapped prose and wrong for
    // `pre`, where the indentation is the content.
    const sel = selectAll(
      `<p>see ${chip('T', 'https://x.example')}</p><pre>def f():\n    return 1</pre>`,
    )
    expect(selectionTextFrom(sel)).toContain('def f():\n    return 1')
  })

  it('collapses a wrapped paragraph\'s source newlines to single spaces', () => {
    const sel = selectAll(`<p>one\ntwo ${chip('T', 'https://x.example')}</p>`)
    expect(selectionTextFrom(sel)).toBe('one two https://x.example')
  })

  it('yields the whole URL for a partially selected link', () => {
    // Half a URL cannot be pasted; half a page title is worse.
    const host = mountFixture(
      `<p>go ${chip('Long Page Title', 'https://x.example/deep')} end</p>`
    )
    const titleNode = host.querySelector('a > span:last-child')!.firstChild!
    const range = document.createRange()
    range.setStart(host.querySelector('p')!.firstChild!, 0)
    range.setEnd(titleNode, 4)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    expect(selectionTextFrom(sel)).toBe('go https://x.example/deep')
  })

  it('does not turn an icon-only control into a paragraph break', () => {
    // The chip's copy button centres its icon with `display: grid`, which is
    // block-level — but it holds no text, and emitting boundaries around it put
    // a paragraph break directly after the URL, mid-sentence.
    const sel = selectAll(
      `<p>before ${chip('T', 'https://x.example')} after</p>`,
    )
    expect(selectionTextFrom(sel)).toBe('before https://x.example after')
  })

  it('does not leave a lone space on its own line between blocks', () => {
    // The whitespace between two block elements is markup indentation. Counted
    // as content it produced "…\n\n \n\nhttps://…" — a line holding one space.
    const sel = selectAll(
      `<p>First.</p>\n  ${card('T', 'D', 'd.example', 'https://x.example/c')}\n  <p>Second.</p>`,
    )
    expect(selectionTextFrom(sel)).toBe('First.\n\nhttps://x.example/c\n\nSecond.')
  })

  it('CASE E: a selection INSIDE a chip yields the URL, not the title', () => {
    // Double-clicking a word of the title is the most ordinary gesture on a chip,
    // and it selects inside the anchor. `cloneContents()` then reveals no
    // unfurl element — it only ever shows descendants — so the fast path used to
    // hand back the fetched title, the exact failure this function prevents.
    const host = mountFixture(
      `<p>go ${chip('Shop Title', 'https://shop.example')} end</p>`
    )
    const titleText = host.querySelector('a > span:last-child')!.firstChild!
    const range = document.createRange()
    range.setStart(titleText, 0)
    range.setEnd(titleText, 6)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    expect(selectionTextFrom(sel)).toBe('https://shop.example')
  })

  it('CASE F: selecting the whole anchor contents yields the URL', () => {
    const host = mountFixture(
      `<p>${chip('T', 'https://x.example/deep?a=1')}</p>`
    )
    const range = document.createRange()
    range.selectNodeContents(host.querySelector('a')!)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    expect(selectionTextFrom(sel)).toBe('https://x.example/deep?a=1')
  })

  it('CASE G: every range of a multi-range selection is reconstructed', () => {
    // Firefox ctrl+drag is the one way to get more than one range. Rebuilding
    // only range 0 would DROP the rest — worse than the wrong-text bug, since
    // `toString()` at least returned every selected character.
    const host = mountFixture(
      `<p id="a">first ${chip('A Title', 'https://a.example')}</p>` +
      `<p id="b">second ${chip('B Title', 'https://b.example')}</p>`
    )
    const r1 = document.createRange()
    r1.selectNodeContents(host.querySelector('#a')!)
    const r2 = document.createRange()
    r2.selectNodeContents(host.querySelector('#b')!)
    const sel = {
      rangeCount: 2,
      getRangeAt: (i: number) => (i === 0 ? r1 : r2),
      toString: () => 'first A Title second B Title',
    } as unknown as Selection
    const out = selectionTextFrom(sel)
    // Asserted EXACTLY, not by containment: the separator is a real decision, and
    // a containment-only assertion cannot see it change. Reproducing the
    // stringifier's delimiter-free concatenation would yield
    // "first https://a.examplesecond https://b.example" — the first URL welded to
    // the next chunk and no longer resolvable, which is the failure this whole
    // function exists to prevent.
    expect(out).toBe('first https://a.example\nsecond https://b.example')
    expect(out).not.toContain('A Title')
    // Every URL survives as its own pasteable token.
    expect(out.match(/https?:\/\/\S+/g)).toEqual(['https://a.example', 'https://b.example'])
  })

  it('leaves a multi-range selection with no unfurled link byte-identical', () => {
    const host = mountFixture(
      '<p id="a">plain one</p><p id="b">plain two</p>'
    )
    const r1 = document.createRange()
    r1.selectNodeContents(host.querySelector('#a')!)
    const r2 = document.createRange()
    r2.selectNodeContents(host.querySelector('#b')!)
    const sel = {
      rangeCount: 2,
      getRangeAt: (i: number) => (i === 0 ? r1 : r2),
      toString: () => 'plain one\nplain two',
    } as unknown as Selection
    expect(selectionTextFrom(sel)).toBe('plain one\nplain two')
  })

  it('falls back to the rendered text when the node carries no url', () => {
    const sel = selectAll('<p><a data-unfurl-url="">Title Only</a></p>')
    expect(selectionTextFrom(sel)).toBe('Title Only')
  })

  it('does not throw when the selection has no range', () => {
    const sel = {
      toString: () => 'text',
      getRangeAt: () => { throw new Error('no range') },
      rangeCount: 0,
    } as unknown as Selection
    expect(selectionTextFrom(sel)).toBe('text')
  })
})
