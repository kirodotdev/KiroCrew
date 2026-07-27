import { describe, it, expect } from 'vitest'
import { deriveWidgetSlug, effectiveWidgetSlug } from '../lib/widgetSlug'
import { parseBlocks } from '../hooks/useBlockAssembler'

describe('deriveWidgetSlug', () => {
  it('produces a 16-hex-char string', () => {
    const slug = deriveWidgetSlug('1779995123.456789', 0)
    expect(slug).toMatch(/^[0-9a-f]{16}$/)
  })

  it('is deterministic — same inputs produce the same slug', () => {
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995123.456789', 0)
    expect(a).toBe(b)
  })

  it('is sensitive to the widget index — same message, different widget = different slug', () => {
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995123.456789', 1)
    expect(a).not.toBe(b)
  })

  it('is sensitive to the message ts — different message = different slug', () => {
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995124.000000', 0)
    expect(a).not.toBe(b)
  })

  it('matches the artifact-store slug regex', () => {
    // Slug regex from artifacts.py: ^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$
    const re = /^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$/
    for (const ts of ['1', '1779995123.456789', '0', '9999999999.999999']) {
      for (const idx of [0, 1, 99]) {
        expect(deriveWidgetSlug(ts, idx)).toMatch(re)
      }
    }
  })

  it('handles unicode in the message ts deterministically', () => {
    // The hash function operates on charCodes; non-ASCII shouldn't crash.
    const slug = deriveWidgetSlug('emoji-🎉-ts', 0)
    expect(slug).toMatch(/^[0-9a-f]{16}$/)
  })

  it('has reasonable avalanche behavior — flipping one input bit changes many output bits', () => {
    // Regression guard against using the 64-bit FNV prime with Math.imul,
    // which silently truncates to multiply-by-435 and produces extremely
    // poor avalanche (code review caught this on rev 1 of). With
    // the 32-bit prime + different offset bases, near-identical seeds
    // should diverge across most output bits.
    const a = deriveWidgetSlug('1779995123.456789', 0)
    const b = deriveWidgetSlug('1779995123.456789', 1)
    let diffBits = 0
    for (let i = 0; i < 16; i++) {
      const ai = parseInt(a[i], 16)
      const bi = parseInt(b[i], 16)
      // Count the differing bits in this nibble.
      let xor = ai ^ bi
      while (xor) {
        diffBits += xor & 1
        xor >>>= 1
      }
    }
    // 64-bit hash, ideal avalanche flips ~32 bits on a 1-bit input change.
    // Demand at least 16 — comfortably above the ~3 bits the broken
    // multiply-by-435 hash produced for adjacent indices.
    expect(diffBits).toBeGreaterThanOrEqual(16)
  })
})

describe('effectiveWidgetSlug', () => {
  it('prefers an explicit slug over derived', () => {
    const result = effectiveWidgetSlug({
      explicitSlug: 'cr-queue',
      messageTs: '1779995123.456789',
      widgetIndex: 0,
    })
    expect(result).toBe('cr-queue')
  })

  it('derives from messageTs + widgetIndex when no explicit slug', () => {
    const result = effectiveWidgetSlug({
      messageTs: '1779995123.456789',
      widgetIndex: 0,
    })
    expect(result).toMatch(/^[0-9a-f]{16}$/)
    // Should match the direct call.
    expect(result).toBe(deriveWidgetSlug('1779995123.456789', 0))
  })

  it('returns null when neither explicit slug nor message context is available', () => {
    expect(effectiveWidgetSlug({})).toBeNull()
    expect(effectiveWidgetSlug({ messageTs: '1779995123.456789' })).toBeNull()
    expect(effectiveWidgetSlug({ widgetIndex: 0 })).toBeNull()
  })

  it('treats empty-string explicit slug as no slug — falls back to derived', () => {
    const result = effectiveWidgetSlug({
      explicitSlug: '',
      messageTs: '1779995123.456789',
      widgetIndex: 0,
    })
    expect(result).toMatch(/^[0-9a-f]{16}$/)
  })
})

// ── Cross-language parity ───────────────────────────────────────────────────
//
// The backend auto-registers every chat-emitted <mcwidget> as an artifact keyed
// by the SAME slug this module derives (src/kiro_crew/widget_slug.py). If the two
// implementations drift, every auto-registered artifact becomes invisible to the
// probe in WidgetFrame and the star button creates a duplicate — the exact
// save-then-refresh duplication the deterministic scheme exists to prevent.
//
// These vectors are duplicated verbatim in test/test_widget_slug.py
// (PARITY_VECTORS) and test/test_widget_parse.py (SHARED_FIXTURES). Changing one
// side without the other fails both suites, which is the point.

const SLUG_PARITY_VECTORS: [string, number, string][] = [
  ['1779995123.456789', 0, '4dc7b6b89ccdb068'],
  ['1779995123.456789', 1, '4ec7b84b9dcdb1fb'],
  ['1779995123.456789', 2, '4fc7b9de9ecdb38e'],
  ['abc', 0, '9eeb65d8bb7210c8'],
  ['', 0, '07c8788634148f16'],
  ['2026-07-27T12:00:00.000Z', 0, '3f8bf58f985f41bf'],
  ['日本語', 0, '19d0c6e579a66e35'],
  ['a\u{1f600}b', 0, 'c3c71a8239d21872'],
  ['1779995123.456789', 10, '7f6769a135cee291'],
]

describe('deriveWidgetSlug — backend parity vectors', () => {
  for (const [messageTs, widgetIndex, expected] of SLUG_PARITY_VECTORS) {
    it(`${JSON.stringify(messageTs)} #${widgetIndex} -> ${expected}`, () => {
      expect(deriveWidgetSlug(messageTs, widgetIndex)).toBe(expected)
    })
  }
})

// Same fixtures as test/test_widget_parse.py::SHARED_FIXTURES. Asserts the
// backend's parse_widgets and this parser agree on WHICH spans are widgets and
// what index each gets — the index feeds the slug, so a disagreement silently
// mis-keys the artifact.
const PARSER_PARITY_FIXTURES: [string, string, [number, string, string, string][]][] = [
  ['single multi-line widget', 'Here you go:\n<mcwidget title="Chart">\n<div>hi</div>\n</mcwidget>\nDone.', [[0, '<div>hi</div>', 'Chart', '']]],
  ['single-line widget', '<mcwidget title="Inline"><b>x</b></mcwidget>', [[0, '<b>x</b>', 'Inline', '']]],
  ['two widgets get distinct indices', '<mcwidget title="A">1</mcwidget>\ntext\n<mcwidget title="B">2</mcwidget>', [[0, '1', 'A', ''], [1, '2', 'B', '']]],
  ['explicit slug attribute is captured', '<mcwidget title="Saved" slug="my-artifact">body</mcwidget>', [[0, 'body', 'Saved', 'my-artifact']]],
  ['attribute order is free', '<mcwidget slug="s1" title="T">body</mcwidget>', [[0, 'body', 'T', 's1']]],
  ['no title falls back to Widget', '<mcwidget>body</mcwidget>', [[0, 'body', 'Widget', '']]],
  ['backtick-quoted tag is not a widget', 'Use `<mcwidget title="X">html</mcwidget>` to render.', []],
  ['widget inside a fenced code block is not a widget', '```html\n<mcwidget title="Doc">example</mcwidget>\n```', []],
  ['fence inside a widget body keeps the body opaque', '<mcwidget title="W">\n```\n</mcwidget>\n```\nreal body\n</mcwidget>', [[0, '```\n</mcwidget>\n```\nreal body', 'W', '']]],
  // On FINAL text parseBlocks marks an unterminated widget complete, so it
  // renders and must be registered. Only a streaming partial is a placeholder.
  ['unterminated widget is still emitted', '<mcwidget title="Open">\n<div>never closed', [[0, '<div>never closed', 'Open', '']]],
  ['a documented example does not shift the real widget index', 'Example: `<mcwidget>demo</mcwidget>`\n<mcwidget title="Real">body</mcwidget>', [[0, 'body', 'Real', '']]],
  ['text after the close tag is not swallowed', '<mcwidget title="A">x</mcwidget> trailing prose', [[0, 'x', 'A', '']]],
  // REGRESSION (cross-language): JS \\w is ASCII-only, Python's is Unicode-aware.
  // A non-ASCII fence info string must NOT be treated as a fence, or the two
  // parsers return DIFFERENT widgets at index 0 — same derived slug, different
  // content, so the frontend links/pins an artifact the user never starred.
  ['non-ASCII fence info string is not a fence', '\u4ee5\u4e0b\u306e\u3088\u3046\u306b\u66f8\u304d\u307e\u3059:\n```\u4f8b\n<mcwidget title="\u30b5\u30f3\u30d7\u30eb">demo</mcwidget>\n```\n\u5b9f\u969b\u306e\u7d50\u679c:\n<mcwidget title="\u30b0\u30e9\u30d5">REAL-CHART</mcwidget>', [[0, 'demo', '\u30b5\u30f3\u30d7\u30eb', '']]],
  ['content before the close tag on the closing line is kept', '<mcwidget title="A">\n<div>one</div>\n<div>two</div></mcwidget>', [[0, '<div>one</div>\n<div>two</div>', 'A', '']]],
  // Nested-fence depth (fenceNestable / innerFenceDepth). A miscount ends the
  // outer fence early and promotes an inert code-block <mcwidget> to a real
  // widget, shifting every later index and mis-keying its artifact slug.
  ['nested fence in markdown does not end the outer fence early', '```markdown\n```python\nx = 1\n```\n<mcwidget title="Inert">still inside the outer fence</mcwidget>\n```\n', []],
  ['code languages skip nested-fence tracking', '```python\n# ```python\n```\n<mcwidget title="Real">after the fence</mcwidget>', [[0, 'after the fence', 'Real', '']]],
  ['a bare inner fence does not increment depth', '```markdown\n```\n<mcwidget title="Real">out</mcwidget>', [[0, 'out', 'Real', '']]],
  ['a widget inside an unclosed fence at EOF is not a widget', '```html\n<mcwidget title="Inert">never escapes the fence</mcwidget>', []],
]

describe('parseBlocks — backend parse_widgets parity', () => {
  for (const [label, raw, expected] of PARSER_PARITY_FIXTURES) {
    it(label, () => {
      const widgets: [number, string, string, string][] = []
      let n = 0
      for (const b of parseBlocks(raw, false)) {
        if (b.type !== 'widget') continue
        widgets.push([n, b.content, b.language || 'Widget', b.slug || ''])
        n++
      }
      expect(widgets).toEqual(expected)
    })
  }
})
