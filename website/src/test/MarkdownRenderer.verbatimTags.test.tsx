import { describe, it, expect, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import MarkdownRenderer, { remarkVerbatimUnknownTags } from '../components/MarkdownRenderer'
import { registerAutolinkRules, resetAutolinkRulesForTest } from '../utils/autolinkRules'

/** No live sink: a diverted tag is a React text node, so a protocol string in it
 * is displayed, never parsed. Asserts that rather than the string's absence. */
function expectInert(container: HTMLElement) {
  const strip = (v: string) => v.replace(/[\s\u0000-\u001f]/g, '').toLowerCase()
  const els = Array.from(container.querySelectorAll('*'))
  for (const tag of ['script', 'iframe', 'object', 'embed', 'a', 'img']) {
    expect(container.querySelectorAll(tag)).toHaveLength(0)
  }
  expect(els.filter(el => Array.from(el.attributes)
    .some(a => /^on/i.test(a.name)))).toHaveLength(0)
  expect(els.filter(el => Array.from(el.attributes)
    .some(a => /^(javascript|vbscript):/.test(strip(a.value))))).toHaveLength(0)
}

/**
 * Unknown (non-allowlisted) tags used as prose placeholders must render
 * VERBATIM — same case, same spacing, and with no closing tag the author
 * never typed.
 *
 * Regression: escapedNodeTree() rebuilt the tag from the post-parse hast
 * node, so it inherited every normalisation parse5 applied:
 *   - tagName was already lowercased      -> <Widget-id>  became <widget-id>
 *   - bare attributes became value ''     -> <a b c>  became <a b="" c="">
 *   - a close tag was appended uncondi-   -> unclosed placeholders grew a
 *     tionally, and parse5 had nested        stack of </...> at the very end
 *     the unclosed elements
 * The displayed bubble was therefore not a faithful copy of what the user
 * typed, which made copy-paste out of chat lossy.
 */
describe('a token inside a verbatim placeholder is not autolinked', () => {
  afterEach(() => resetAutolinkRulesForTest())

  it('leaves a registered token inside an unknown tag literal', () => {
    registerAutolinkRules([
      { id: 'vb', pattern: /\bTICKET-\d+\b/g, href: 'https://example.invalid/{match}' },
    ])
    const { container } = render(<MarkdownRenderer content={'<custom>TICKET-1</custom>'} />)
    expect(container.querySelectorAll('a')).toHaveLength(0)
    expect(container.textContent).toContain('<custom>TICKET-1</custom>')
  })

  it('still autolinks the same token outside a placeholder', () => {
    registerAutolinkRules([
      { id: 'vb2', pattern: /\bTICKET-\d+\b/g, href: 'https://example.invalid/{match}' },
    ])
    const { container } = render(<MarkdownRenderer content={'see TICKET-1 please'} />)
    expect(container.querySelectorAll('a')).toHaveLength(1)
  })
})

describe('verbatim rendering of unknown tags used as prose placeholders', () => {
  it('preserves original case and injects no closing tag', () => {
    const { container } = render(<MarkdownRenderer content={'config has <Widget-id>.json so it loads default'} />)
    expect(container.textContent).toContain('<Widget-id>.json')
    expect(container.textContent).not.toContain('<widget-id>')
    expect(container.textContent).not.toContain('</Widget-id>')
    expect(container.textContent).not.toContain('</widget-id>')
  })

  it('does not turn bare words inside a placeholder into empty attributes', () => {
    const { container } = render(<MarkdownRenderer content={'like <Widget-id>.<some optional variant id>.json instead'} />)
    expect(container.textContent).toContain('<some optional variant id>')
    expect(container.textContent).not.toContain('=""')
  })

  it('reproduces a multi-placeholder message with no appended close-tag stack', () => {
    // Two unclosed placeholders in one paragraph append a run of closing tags.
    const content = 'For ex. if the config has <Widget-id>.json it will load the default, '
      + 'but if it has like <Widget-id>.<some optional variant id>.json it will '
      + 'load that one instead of the default'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('<Widget-id>.json')
    expect(text).toContain('<some optional variant id>')
    // The exact corruption users saw appended to the end of their own message.
    expect(text).not.toContain('</some>')
    expect(text).not.toContain('</widget-id>')
    expect(text).not.toContain('=""')
  })

  it('keeps a closing tag the author actually typed, in its original case', () => {
    const { container } = render(<MarkdownRenderer content={'<myCustomTag>inner content</myCustomTag>'} />)
    const text = container.textContent || ''
    expect(text).toContain('<myCustomTag>')
    expect(text).toContain('inner content')
    expect(text).toContain('</myCustomTag>')
  })

  it('still renders no element for the unknown tag (React #290 guard holds)', () => {
    const { container } = render(<MarkdownRenderer content={'The client is <dynamoDBClient> and it handles requests.'} />)
    expect(container.querySelector('dynamoDBClient' as never)).toBeNull()
    expect(container.querySelector('dynamodbclient' as never)).toBeNull()
    expect(container.textContent).toContain('<dynamoDBClient>')
  })

  it('renders a block-level lone placeholder with no escaped-tag wrapper', () => {
    // A placeholder alone in a block takes a different mdast position from an
    // inline one and emits a bare text node rather than a wrapper span.
    const { container } = render(<MarkdownRenderer content={'<Widget-id>'} />)
    expect(container.querySelector('span.escaped-tag')).toBeNull()
    expect(container.querySelector('Widget-id' as never)).toBeNull()
    expect(container.textContent).toContain('<Widget-id>')
  })

  it('still collapses executable tags to an unsupported marker', () => {
    const { container } = render(<MarkdownRenderer content={'before <script>alert(1)</script> after'} />)
    const text = container.textContent || ''
    expect(container.querySelector('script')).toBeNull()
    expect(text).not.toContain('alert(1)')
    expect(text).toContain('[unsupported: script]')
  })

  it('leaves allowlisted inline HTML working', () => {
    const { container } = render(<MarkdownRenderer content={'<strong>bold</strong> and <em>italic</em>'} />)
    expect(container.querySelector('strong')).not.toBeNull()
    expect(container.querySelector('em')).not.toBeNull()
    expect(container.textContent).toContain('bold')
  })

  it('does not escape tag-like text inside code spans or fences', () => {
    const { container } = render(<MarkdownRenderer content={'inline `<Widget-id>` and\n\n```\n<Widget-id>.json\n```\n'} />)
    const text = container.textContent || ''
    expect(text).toContain('<Widget-id>')
    expect(text).not.toContain('&lt;')
  })

  it('renders a placeholder containing a protocol-like word verbatim', () => {
    // `data:` is an ordinary English word plus a colon, so a bare word inside a
    // placeholder must not be read as a dangerous attribute VALUE.
    const { container } = render(<MarkdownRenderer content={'paste <Some data: Thing> below'} />)
    const text = container.textContent || ''
    expect(text).toContain('<Some data: Thing>')
    expect(text).not.toContain('<some data:')
    expect(text).not.toContain('=""')
    expect(text).not.toContain('</some>')
  })

  it('keeps fidelity uniform across neighbouring placeholders in one message', () => {
    const { container } = render(<MarkdownRenderer content={'<Alpha-id> then <Beta data: id> then <Gamma-id>'} />)
    const text = container.textContent || ''
    expect(text).toContain('<Alpha-id>')
    expect(text).toContain('<Beta data: id>')
    expect(text).toContain('<Gamma-id>')
  })

  it('shows a protocol-bearing placeholder verbatim and inert', () => {
    const { container } = render(<MarkdownRenderer content={'<customLink href="javascript:alert(1)">x</customLink>'} />)
    expect(container.textContent).toContain('<customLink href="javascript:alert(1)">')
    expectInert(container)
  })

  it('keeps a paired unknown container literal, including nested allowed HTML', () => {
    const { container } = render(<MarkdownRenderer content={'<customBlock>see <b>this</b> ok</customBlock>'} />)
    const text = container.textContent || ''
    expect(text).toContain('<customBlock>')
    expect(text).toContain('<b>this</b>')
    expect(text).toContain('</customBlock>')
    expect(container.querySelectorAll('b')).toHaveLength(0)
  })

  it('still collapses an executable tag nested in a paired container', () => {
    const { container } = render(<MarkdownRenderer content={'<customBlock>a <script>alert(1)</script> b</customBlock>'} />)
    const text = container.textContent || ''
    expect(container.querySelectorAll('script')).toHaveLength(0)
    expect(text).not.toContain('alert(1)')
    expect(text).toContain('[unsupported: script]')
  })

  it('shows a protocol-bearing anchor inside a paired container verbatim and inert', () => {
    const { container } = render(<MarkdownRenderer content={'<customBlock>a <a href="javascript:alert(1)">x</a> b</customBlock>'} />)
    expect(container.textContent).toContain('<a href="javascript:alert(1)">')
    expectInert(container)
  })

  it('shows an unquoted protocol value verbatim and inert', () => {
    const { container } = render(<MarkdownRenderer content={'<customLink href=javascript:alert(1)>x</customLink>'} />)
    expect(container.textContent).toContain('<customLink href=javascript:alert(1)>')
    expectInert(container)
  })

  it('renders a double-quoted attribute value containing > verbatim', () => {
    const { container } = render(<MarkdownRenderer content={'Compare <Foo bar="a>b"> here.'} />)
    expect(container.textContent).toBe('Compare <Foo bar="a>b"> here.')
    expect(container.textContent).not.toContain('<foo')
    expect(container.textContent).not.toContain('</foo>')
  })

  it('renders a single-quoted attribute value containing > verbatim', () => {
    const { container } = render(<MarkdownRenderer content={"Compare <Foo bar='a>b'> here."} />)
    expect(container.textContent).toBe("Compare <Foo bar='a>b'> here.")
    expect(container.textContent).not.toContain('bar="a>b"')
    expect(container.textContent).not.toContain('</foo>')
  })

  it('renders an attribute value that is exactly > verbatim', () => {
    const { container } = render(<MarkdownRenderer content={'Compare <Foo bar=">"> here.'} />)
    expect(container.textContent).toBe('Compare <Foo bar=">"> here.')
    expect(container.textContent).not.toContain('</foo>')
  })

  it('keeps the trailing prose outside the placeholder', () => {
    // The lossy path rebuilt a close tag, which pulled the rest of the sentence
    // inside the escaped-tag span instead of leaving it as sibling text.
    const { container } = render(<MarkdownRenderer content={'Compare <Foo bar="a>b"> here.'} />)
    const span = container.querySelector('span.escaped-tag')
    expect(span?.textContent ?? '').not.toContain('here.')
  })

  it('still drops an HTML comment rather than rendering it verbatim', () => {
    const { container } = render(<MarkdownRenderer content={'before <!-- note --> after'} />)
    expect(container.textContent).toBe('before  after')
    expect(container.textContent).not.toContain('note')
  })

  it('still drops a doctype rather than rendering it verbatim', () => {
    const { container } = render(<MarkdownRenderer content={'<!DOCTYPE html>'} />)
    expect(container.textContent).toBe('')
    expect(container.textContent).not.toContain('DOCTYPE')
  })

  it('still renders a multi-tag block as live allowlisted elements', () => {
    const { container } = render(<MarkdownRenderer content={'<div><span>hi</span></div>'} />)
    expect(container.querySelector('span')).not.toBeNull()
    expect(container.textContent).toBe('hi')
    expect(container.textContent).not.toContain('<span>')
  })

  it('renders a bare attribute containing a slash verbatim', () => {
    // remark does not tokenize this as HTML at all, so it never reaches the
    // matcher; the guard is that no future change routes it to the lossy path.
    const { container } = render(<MarkdownRenderer content={'Compare <Foo a/b> here.'} />)
    expect(container.textContent).toBe('Compare <Foo a/b> here.')
    expect(container.textContent).not.toContain('=""')
    expect(container.textContent).not.toContain('</foo>')
  })

  it('renders a path-like bare attribute verbatim', () => {
    const { container } = render(<MarkdownRenderer content={'See <some path/to/file here> now.'} />)
    expect(container.textContent).toBe('See <some path/to/file here> now.')
    expect(container.textContent).not.toContain('=""')
  })

  it('keeps a self-closing placeholder verbatim', () => {
    const { container } = render(<MarkdownRenderer content={'A <Foo /> and <Foo a="b"/> b.'} />)
    expect(container.textContent).toBe('A <Foo /> and <Foo a="b"/> b.')
    expect(container.textContent).not.toContain('<foo')
    expect(container.textContent).not.toContain('</Foo>')
  })
})

/**
 * The pass is exported and wired by every surface that admits raw HTML, so it is
 * tested as a unit rather than only through one component: a second consumer
 * (the mochi chat panel) composes it into its own remark list.
 */
describe('remarkVerbatimUnknownTags as a shared unit', () => {
  const run = (src: string) => {
    const tree = unified().use(remarkParse).use(remarkVerbatimUnknownTags).runSync(
      unified().use(remarkParse).parse(src)
    ) as { children: { children?: { type: string; value?: string }[] }[] }
    return tree.children[0]?.children ?? []
  }

  it('converts an unknown single tag from an html node to a text node', () => {
    const kids = run('a <Widget-id> b')
    expect(kids.some(k => k.type === 'html')).toBe(false)
    expect(kids.some(k => k.type === 'text' && k.value === '<Widget-id>')).toBe(true)
  })

  it('converts a tag carrying a dangerous attribute value to a text node', () => {
    const kids = run('a <customLink href="javascript:alert(1)"> b')
    expect(kids.some(k => k.type === 'html')).toBe(false)
    expect(kids.some(k => k.type === 'text'
      && k.value === '<customLink href="javascript:alert(1)">')).toBe(true)
  })

  it('converts a placeholder whose bare word merely looks like a protocol', () => {
    const kids = run('a <Some data: Thing> b')
    expect(kids.some(k => k.type === 'text' && k.value === '<Some data: Thing>')).toBe(true)
  })

  it('receives the whole tag as one node when an attribute value holds >', () => {
    // The tokenizer does not split at the interior >, so the diverter sees the
    // complete tag and only the match test decides which path it takes.
    const kids = run('a <Foo bar="a>b"> b')
    expect(kids.filter(k => k.type === 'html')).toHaveLength(0)
    expect(kids.some(k => k.type === 'text' && k.value === '<Foo bar="a>b">')).toBe(true)
  })

  it('converts a single-quoted attribute value containing > to a text node', () => {
    const kids = run("a <Foo bar='a>b'> b")
    expect(kids.some(k => k.type === 'text' && k.value === "<Foo bar='a>b'>")).toBe(true)
  })

  it('converts an attribute value that is exactly > to a text node', () => {
    const kids = run('a <Foo bar=">"> b')
    expect(kids.some(k => k.type === 'text' && k.value === '<Foo bar=">">')).toBe(true)
  })

  it('leaves a comment, a doctype and a multi-tag block as html nodes', () => {
    for (const src of ['<!-- note -->', '<!DOCTYPE html>', '<div><span>hi</span></div>']) {
      const tree = unified().use(remarkParse).use(remarkVerbatimUnknownTags).runSync(
        unified().use(remarkParse).parse(src)
      ) as { children: { type: string; value?: string }[] }
      expect(tree.children.some(k => k.type === 'html' && k.value === src)).toBe(true)
    }
  })
})
