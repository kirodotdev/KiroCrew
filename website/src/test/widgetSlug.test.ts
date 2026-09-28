import { describe, it, expect } from 'vitest'
import { deriveWidgetBodySlug, effectiveWidgetSlug } from '../lib/widgetSlug'
import { parseBlocks } from '../hooks/useBlockAssembler'

describe('effectiveWidgetSlug', () => {
  it('prefers an explicit slug over derived', () => {
    const result = effectiveWidgetSlug({
      explicitSlug: 'cr-queue',
      messageTs: '1779995123.456789',
      body: '<div>Hello</div>',
    })
    expect(result).toBe('cr-queue')
  })

  it('derives from messageTs + body when no explicit slug', () => {
    const result = effectiveWidgetSlug({
      messageTs: '1779995123.456789',
      body: '<div>Hello</div>',
    })
    expect(result).toMatch(/^[0-9a-f]{16}$/)
    expect(result).toBe(deriveWidgetBodySlug('1779995123.456789', '<div>Hello</div>'))
  })

  it('returns null without both timestamp and body', () => {
    expect(effectiveWidgetSlug({})).toBeNull()
    expect(effectiveWidgetSlug({ messageTs: '1779995123.456789' })).toBeNull()
    expect(effectiveWidgetSlug({ body: '<div>Hello</div>' })).toBeNull()
  })

  it('treats empty-string explicit slug as no slug — falls back to derived', () => {
    const result = effectiveWidgetSlug({
      explicitSlug: '',
      messageTs: '1779995123.456789',
      body: '<div>Hello</div>',
    })
    expect(result).toMatch(/^[0-9a-f]{16}$/)
  })
})

const BODY_SLUG_PARITY_VECTORS: [string, string, string][] = [
  ['1779995123.456789', '<div>Hello</div>', '2f1bd76ebf6f069e'],
  ['1779995123.456789', '<p>日本語</p>', 'ecbde055a9fcc025'],
  ['1779995123.456789', '<p>\u{1f600}</p>', 'bc39b62700f9e297'],
]

describe('deriveWidgetBodySlug — backend parity vectors', () => {
  for (const [messageTs, body, expected] of BODY_SLUG_PARITY_VECTORS) {
    it(`${JSON.stringify(messageTs)} + ${JSON.stringify(body)} -> ${expected}`, () => {
      expect(deriveWidgetBodySlug(messageTs, body)).toBe(expected)
    })
  }

  it('distinguishes different bodies in the same message', () => {
    expect(deriveWidgetBodySlug('ts', 'body-a')).not.toBe(deriveWidgetBodySlug('ts', 'body-b'))
  })

  it('distinguishes the same body in different messages', () => {
    expect(deriveWidgetBodySlug('ts-a', 'body')).not.toBe(deriveWidgetBodySlug('ts-b', 'body'))
  })

  it('distinguishes ordinal-like body values', () => {
    expect(deriveWidgetBodySlug('ts', '0')).not.toBe(deriveWidgetBodySlug('ts', '1'))
  })
})

// Same fixtures as test/test_widget_parse.py::SHARED_FIXTURES. Asserts the
// backend's parse_widgets and this parser agree on WHICH spans are widgets and
// what index each gets. Body-keyed identity prevents cross-binding, while parser
// parity ensures every rendered widget has a registered artifact.
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
  // Cross-language parity on a non-ASCII info string. Both parsers apply the
  // CommonMark rule (any non-backtick info string opens a fence), so ```例 is a
  // fence on BOTH sides: the example inside it is inert code and the real
  // widget is index 0. If either side regressed to its own `\w` (JS ASCII-only,
  // Python Unicode-aware) they would return DIFFERENT widget bodies. The body
  // fingerprints differ, so the frontend probe misses instead of linking or
  // pinning an artifact for different content. test_widget_parse.py holds the
  // twin.
  ['non-ASCII fence info string is a fence on both sides', '\u4ee5\u4e0b\u306e\u3088\u3046\u306b\u66f8\u304d\u307e\u3059:\n```\u4f8b\n<mcwidget title="\u30b5\u30f3\u30d7\u30eb">demo</mcwidget>\n```\n\u5b9f\u969b\u306e\u7d50\u679c:\n<mcwidget title="\u30b0\u30e9\u30d5">REAL-CHART</mcwidget>', [[0, 'REAL-CHART', '\u30b0\u30e9\u30d5', '']]],
  // Punctuated / hyphenated / attributed info strings are fences too, so the
  // widget that follows them is index 0 on both sides.
  ['hyphenated fence info string is a fence on both sides', '```error-report\n<mcwidget title="Inert">in code</mcwidget>\n```\n<mcwidget title="Real">out</mcwidget>', [[0, 'out', 'Real', '']]],
  ['leading whitespace before the tag keeps the tag on both sides', '``` python\n```js\n<mcwidget title="Inert">in code</mcwidget>\n```\n<mcwidget title="Real">out</mcwidget>', [[0, 'out', 'Real', '']]],
  ['attributed fence info string is a fence on both sides', '```js {1,3}\n<mcwidget title="Inert">in code</mcwidget>\n```\n<mcwidget title="Real">out</mcwidget>', [[0, 'out', 'Real', '']]],
  ['a backtick in the info string is not a fence on either side', '``` `x`\n<mcwidget title="Real">first</mcwidget>\nprose\n<mcwidget title="Second">second</mcwidget>', [[0, 'first', 'Real', ''], [1, 'second', 'Second', '']]],
  ['content before the close tag on the closing line is kept', '<mcwidget title="A">\n<div>one</div>\n<div>two</div></mcwidget>', [[0, '<div>one</div>\n<div>two</div>', 'A', '']]],
  // Nested-fence depth (fenceNestable / innerFenceDepth). A miscount ends the
  // outer fence early and promotes an inert code-block <mcwidget> to a real
  // widget. Body-keyed identity prevents artifact rebinding, while parser parity
  // still guards which spans receive registered artifacts.
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
