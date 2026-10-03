// @vitest-environment jsdom
//
// The pipeline board card, composed through the REAL document builder.
//
// Every gate on the Python side reads the page as text: it proves the page BINDS a field
// and the flattener WRITES it. None of them proves the host puts the written value on the
// page, because the host is this TypeScript module -- it sanitizes the markup, restricts
// the CSS, strips displayed attributes, and only then walks `[data-dashboard-field]` and
// sets each element's textContent. A page that satisfies every Python gate and loses its
// bindings here renders a board of empty boxes.
//
// So this composes the card through `dashboardDocument` itself and reads the values back
// out of the parsed result. It also writes the composed documents to disk, which is what
// the screenshots in the pull request are taken from -- the same bytes, not a hand-built
// preview.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { dashboardDocument } from '../pages/chat/command-center/dashboardDocument'

// jsdom parses CSS but has no constructed-sheet replaceSync, which the builder requires.
// Adapted with jsdom's own CSSOM parser, rule by rule, so the card's real style block
// survives composition instead of being dropped by the builder's catch.
const ParsedSheet = CSSStyleSheet
vi.stubGlobal('CSSStyleSheet', class extends ParsedSheet {
  replaceSync(css: string) {
    while (this.cssRules.length) this.deleteRule(0)
    for (const rule of splitRules(css)) { try { this.insertRule(rule, this.cssRules.length) } catch { /* jsdom rejects some at-rules */ } }
  }
})

/** Top-level rules of `css`, split on brace depth so nested blocks stay whole. */
function splitRules(css: string): string[] {
  const out: string[] = []
  let depth = 0, start = 0
  for (let i = 0; i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}' && --depth === 0) { out.push(css.slice(start, i + 1).trim()); start = i + 1 }
  }
  return out.filter(Boolean)
}

const OUT = process.env.KC_CARD_RENDER_DIR || ''

// Fields carrying a NOTICE rather than a value, which are empty exactly when there is nothing to
// notice. Kept in step with the Python side's own `_NOTICE_FIELDS`, which derives them from the
// closed stat vocabulary; the parity assertion below fails if the page stops binding one.
const NOTICE_FIELDS = new Set([
  'contract_note', 'stat_items_note', 'stat_entries_note', 'stat_round_note',
])

/** The card page and two data sets, produced by the Python side and pinned here.
 *
 * Written out by `scripts/pipeline_board_card_fixture.py`, which runs the real provider
 * and the real flattener -- so these are the bytes the gateway publishes, not a sample
 * someone typed. A stale fixture fails the parity assertion below rather than passing
 * quietly, because the field set is compared against the page's own bindings. */
const fixture = JSON.parse(
  readFileSync(join(__dirname, 'fixtures/pipelineBoardCard.json'), 'utf8'),
) as { html: string; boards: Record<string, Record<string, string>> }

// One palette per mode, because the page reads its colours from the HOST's variables and
// has no palette of its own. Passing the dark values in both modes would compose two
// documents that differ only in a `color-scheme` line — so a "light mode" screenshot taken
// from it would be the dark card wearing a label, which is worse evidence than none.
const THEME: Record<'dark' | 'light', Record<string, string>> = {
  dark: {
    '--bg': '#09090b', '--text': '#fafafa', '--muted': '#a1a1aa',
    '--border': '#27272a', '--accent': '#0ea5e9', '--card': '#111113', '--danger': '#f87171',
  },
  light: {
    '--bg': '#ffffff', '--text': '#18181b', '--muted': '#71717a',
    '--border': '#e4e4e7', '--accent': '#0369a1', '--card': '#fafafa', '--danger': '#dc2626',
  },
}

function compose(data: Record<string, string>, mode: 'dark' | 'light' = 'dark'): Document {
  const markup = dashboardDocument(fixture.html, THEME[mode], mode, data)
  if (OUT) {
    const path = join(OUT, `${Object.keys(fixture.boards).find(k => fixture.boards[k] === data) ?? 'card'}.${mode}.html`)
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, markup, 'utf8')
  }
  return new DOMParser().parseFromString(markup, 'text/html')
}

/** Every style block in the composed document, joined.
 *
 * Across ALL of them rather than one index: the builder prepends its own theme block, so
 * an index picks whichever happens to be first and a reordering there would redden a
 * check about the card's CSS for a reason that has nothing to do with the card. */
function allCss(doc: Document): string {
  return Array.from(doc.querySelectorAll('style'), s => s.textContent || '').join('\n')
}

function boundText(doc: Document): Record<string, string> {
  const out: Record<string, string> = {}
  for (const el of doc.body.querySelectorAll('[data-dashboard-field]')) {
    out[el.getAttribute('data-dashboard-field') || ''] = el.textContent || ''
  }
  return out
}

describe('the pipeline board card through the real document builder', () => {
  it('puts every written value on the page and leaves no bound element empty', () => {
    const data = fixture.boards.full
    const shown = boundText(compose(data))
    // Parity, re-asserted on the COMPOSED document: the Python gate compares the page
    // source against the flattener, and this compares what survived composition.
    expect(Object.keys(shown).sort()).toEqual(Object.keys(data).sort())
    for (const [field, value] of Object.entries(data)) {
      expect(shown[field], `field ${field}`).toBe(value)
      // The NOTICE fields are the exemption: a notice says something ABOUT a value, so an empty
      // one means there is nothing to notice. Every other field holds something a reader COUNTS,
      // where a blank cannot be told from a zero.
      if (!NOTICE_FIELDS.has(field)) expect(shown[field].trim(), `field ${field} rendered blank`).not.toBe('')
    }
    expect(shown.contract_note, 'a healthy board should not lecture about contracts').toBe('')
    // A gloss the publisher DID write must still be shown, or "empty when silent" could be
    // satisfied by never showing one.
    expect(shown.stat_items_note).not.toBe('')
  })

  it('renders words rather than a blank for a hostile payload', () => {
    // A published value that cannot be read as text. On a page of counts a blank is
    // indistinguishable from a real zero, and the reader cannot recover either.
    const shown = boundText(compose(fixture.boards.hostile))
    for (const [field, value] of Object.entries(shown)) {
      if (!NOTICE_FIELDS.has(field)) expect(value.trim(), `field ${field} rendered blank`).not.toBe('')
    }
    expect(shown.lede).toBe('could not be read')
    expect(shown.contract_note).toContain('could not be read')
    // THREE states, not two: nobody filled it is a different fact from it cannot be read.
    expect(shown.since).toBe('first entry not recorded \u00b7 nothing on this card is a link')
  })

  it('leaves no value blank on any board the generator produces', () => {
    // EVERY board, not just the two the other cases use. The generator emits the empty,
    // stale, version-mismatch and row-overflow states as well, and a reader meets those on a
    // real dashboard -- so each one is composed here, which is also what writes its document
    // out for the screenshots. A state that is only asserted and never rendered is a state
    // nobody has looked at.
    for (const name of Object.keys(fixture.boards)) {
      const shown = boundText(compose(fixture.boards[name]))
      for (const [field, value] of Object.entries(shown)) {
        if (NOTICE_FIELDS.has(field)) continue
        expect(value, `${name}.${field} is blank`).not.toBe('')
      }
    }
  })

  it('renders the states a reader only meets when something is off', () => {
    const empty = boundText(compose(fixture.boards.empty))
    expect(empty.progress_legend).toContain('no items on the board in round 3')
    expect(empty.column_0_rows).toBe('no items')

    const stale = boundText(compose(fixture.boards.stale))
    expect(stale.meta_when).toContain('stale')

    const mismatch = boundText(compose(fixture.boards.mismatch))
    expect(mismatch.contract_note).toContain('different version of Kiro Crew')

    const overflow = boundText(compose(fixture.boards.overflow))
    expect(overflow.column_0_rows).toMatch(/\+\d+ more items \(of \d+\)/)
    expect(overflow.column_0_rows).toContain('[trimmed]')
  })

  it('keeps the newlines a column of rows is written with', () => {
    // The rows of one column are ONE text field, because a board's item count is not
    // bounded and the host drops a card over 24 fields whole. They are legible only if
    // the newlines survive composition and the page declares pre-line for them.
    const shown = boundText(compose(fixture.boards.full))
    expect(shown.column_0_rows.split('\n').length).toBeGreaterThan(1)
    const doc = compose(fixture.boards.full)
    expect(doc.querySelector('[data-dashboard-field="column_0_rows"]')!.className).toContain('rows')
    // Whitespace-insensitive: the builder round-trips the block through the CSSOM, which
    // reserializes `white-space:pre-line` with a space after the colon. Matching the
    // authored spelling would fail on a page whose CSS is entirely intact.
    expect(allCss(doc)).toMatch(/white-space:\s*pre-wrap/)
    // pre-WRAP, not pre-line. `pre-line` collapses white space and drops it next to a forced
    // break, so the flattener's four-space indent on an action line never rendered and the
    // sub-line sat flush with the item row it belongs under -- the column's only visual
    // hierarchy, silently absent. This asserts the indent is in the text AND that the style
    // preserving it is the one declared.
    expect(shown.column_0_rows).toMatch(/\n {4}\S/)
    expect(allCss(doc)).not.toMatch(/white-space:\s*pre-line/)
  })

  it('promises nothing clickable, in the page or in the host chrome around it', () => {
    // A reader said the rows "read as plausibly clickable" and would try clicking an item number.
    // Nothing on the card responds, and nothing can: it is inert by contract. So the whole
    // COMPOSED document -- the page plus the theme and chrome the host prepends -- must carry no
    // hover rule and no pointer cursor, or the host would sharpen a promise the card cannot keep.
    // Asserted on the composition rather than on the page alone, because the affordance that
    // would mislead a reader is the one they can SEE, whoever wrote it.
    const doc = compose(fixture.boards.full)
    const css = Array.from(doc.querySelectorAll('style'), s => s.textContent || '').join('\n')
    expect(css).not.toContain(':hover')
    expect(css).not.toMatch(/cursor\s*:\s*pointer/)
    // And no element carries one inline either.
    const inline = Array.from(doc.querySelectorAll('[style]'), e => e.getAttribute('style') || '')
    expect(inline.filter(v => /cursor/.test(v))).toEqual([])
  })

  it('gives the legend no colour that promises a link', () => {
    // The card declares zero interactive elements and the host strips anything shaped like one,
    // so accent-coloured text is a promise the page cannot keep: a reader took the blue for a
    // link, followed it, and nothing happened.
    const doc = compose(fixture.boards.full)
    const legend = doc.querySelector('[data-dashboard-field="progress_legend"]')!
    expect(legend.className).not.toContain('mark')
    // Scoped to the CARD's own block: the host always forwards `--accent` in the theme block it
    // prepends, so a document-wide search would redden over the host doing its job. What must
    // be absent is the page READING it.
    const page = Array.from(doc.querySelectorAll('style'), s => s.textContent || '')
      .filter(css => css.includes('grid-template-columns')).join('\n')
    expect(page, 'the card must declare its own style block').not.toBe('')
    expect(page).not.toContain('var(--accent')
  })

  it('ships no script, no control and nothing pointing outside the frame', () => {
    const doc = compose(fixture.boards.full)
    for (const tag of ['script', 'form', 'input', 'button', 'iframe', 'object', 'embed']) {
      expect(doc.querySelectorAll(tag).length, tag).toBe(0)
    }
    for (const el of doc.querySelectorAll('*')) {
      for (const attr of Array.from(el.attributes)) {
        if (attr.localName === 'href') expect(attr.value.startsWith('#')).toBe(true)
        expect(attr.localName).not.toBe('src')
        // Attributes the browser shows as text that the backend's scan does not read.
        for (const shown of ['alt', 'title', 'start', 'value']) expect(attr.localName).not.toBe(shown)
      }
    }
  })

  it('declares the strict card CSP, with no image or font source', () => {
    const csp = compose(fixture.boards.full).querySelector('meta[http-equiv]')!.getAttribute('content')!
    expect(csp).toContain("script-src 'none'")
    expect(csp).toContain("img-src 'none'")
    expect(csp).toContain("font-src 'none'")
    expect(csp).toContain("connect-src 'none'")
  })

  it('keeps the layout after the host restricts its CSS', () => {
    // The builder DELETES content/list-style/text-overflow in card mode. The page must not
    // depend on them -- and it must keep the properties the layout does rest on, so this
    // fails both when the page reaches for a deleted property and when restriction has
    // eaten the whole style block.
    const css = allCss(compose(fixture.boards.full))
    expect(css).toMatch(/grid-template-columns/)
    expect(css).toMatch(/white-space:\s*pre-wrap/)
    expect(css).not.toMatch(/(^|[^-\w])content\s*:/)
    expect(css).not.toMatch(/(^|[^-\w])text-overflow\s*:/)
  })

  it('takes its colours from the host in both modes', () => {
    // The page declares no palette: every colour is var(--text) / var(--muted) /
    // var(--border) / var(--accent) with a fallback. So light mode is the host's own
    // variables arriving, and this asserts they do -- a card that quietly kept a dark
    // fallback would be unreadable on a light dashboard and nothing else would say so.
    const doc = compose(fixture.boards.full, 'light')
    const host = allCss(doc)
    expect(host).toContain('color-scheme:light')
    for (const [name, value] of Object.entries(THEME.light)) {
      expect(host, `host var ${name}`).toContain(`${name}:${value}`)
    }
    expect(host).not.toContain(THEME.dark['--bg'])
    expect(boundText(doc).meta_name).toBe(fixture.boards.full.meta_name)
  })
})
