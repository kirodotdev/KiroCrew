import { describe, it, expect, afterEach } from 'vitest'
import {
  copySelectionAsMarkdown,
  nodeToMarkdown,
  nodeToSafeHtml,
  selectionToCopy,
} from '../components/markdown/selectionMarkdown'

/** Build a detached root from HTML with no whitespace text between tags,
 *  the way React renders it. */
function mount(html: string): HTMLDivElement {
  const root = document.createElement('div')
  root.appendChild(document.createRange().createContextualFragment(html))
  document.body.appendChild(root)
  return root
}

afterEach(() => { document.body.replaceChildren() })

/** Origin of the test page: a relative link resolves against it in both flavours. */
const PAGE = window.location.origin

function md(html: string): string | null {
  return nodeToMarkdown(mount(html))
}

function textNode(root: Element, needle: string): Text {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if ((n.nodeValue ?? '').includes(needle)) return n as Text
  }
  throw new Error(`no text node contains ${needle}`)
}

/** A range from `startText`'s node at `startOffset` to `endText`'s node at
 *  `endOffset`, located by the first text node that contains each string. */
function rangeOver(root: Element, start: [string, number], end: [string, number]): Range {
  const range = document.createRange()
  range.setStart(textNode(root, start[0]), start[1])
  range.setEnd(textNode(root, end[0]), end[1])
  return range
}

describe('nodeToMarkdown: flatten path', () => {
  it.each([
    ['underline', '<p><u>under</u> it</p>', 'under it'],
    ['a key', '<p>press <kbd>Esc</kbd></p>', 'press Esc'],
    ['a list', '<ul><li>one</li><li>two</li></ul>', 'one\n\ntwo'],
    ['a table', '<table><tr><td>a</td><td>b</td></tr><tr><td>c</td></tr></table>', 'a b\n\nc'],
    ['a code block', '<div class="code-block"><pre><code>let a = 1\nlet b = 2</code></pre></div>', 'let a = 1\nlet b = 2'],
  ])('writes %s as its text', (_name, html, expected) => {
    expect(md(html)).toBe(expected)
  })

  it('copies markdown characters in prose literally', () => {
    expect(md('<p>2*3 and _this_ and `x` and [1] with <strong>format</strong></p>'))
      .toBe('2*3 and _this_ and `x` and [1] with **format**')
    expect(md('<p># not a heading</p>')).toBe('# not a heading')
  })

  it('writes a link with an unsafe destination as its label', () => {
    expect(md('<p><a href="javascript:alert(1)">click</a> <strong>x</strong></p>')).toBe('click **x**')
    expect(md('<p><a href="vscode://file/a.ts">the file</a></p>')).toBe('the file')
  })

  it('writes emphasis removed by CSS as plain text', () => {
    expect(md('<p><b style="font-weight:400">plain</b> <em style="font-style:normal">also</em> <del style="text-decoration:none">too</del></p>'))
      .toBe('plain also too')
  })

  it('omits hidden text', () => {
    const root = mount('<p><strong>x</strong> a<span style="display:none">gone</span><span aria-hidden="true">gone</span><span class="hidden" style="display:none">gone</span> b</p>')
    expect(selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)?.markdown).toBe('**x** a b')
  })

  it('keeps a longer fence around inline code whose flattened children hold backticks', () => {
    expect(md('<p><code>a`b<u>c</u></code></p>')).toBe('``a`bc``')
  })

  it('writes a word-break opportunity as nothing', () => {
    expect(md('<p><strong>see</strong> goal/<wbr>worker</p>')).toBe('**see** goal/worker')
  })

  it('converts deeply nested emphasis beside a bang in linear time', () => {
    const depth = 40
    const nested = '<b>'.repeat(depth) + 'x' + '</b>'.repeat(depth)
    const started = performance.now()
    expect(md(`<p>Done! ${nested} tail</p>`)).toBe('Done! **x** tail')
    expect(performance.now() - started).toBeLessThan(1000)
  })
})

describe('nodeToMarkdown: inline formatting', () => {
  it('writes bold, italic, strikethrough and inline code', () => {
    expect(md('<p>a <strong>bold</strong> <em>it</em> <del>gone</del> <code>x()</code></p>'))
      .toBe('a **bold** *it* ~~gone~~ `x()`')
  })

  it('treats spaced b / i / s like strong / em / del', () => {
    expect(md('<p><b>B</b> <i>I</i> <s>S</s></p>')).toBe('**B** *I* ~~S~~')
  })

  it('moves edge spaces outside the markers', () => {
    expect(md('<p>x<strong> bold </strong>y</p>')).toBe('x **bold** y')
    expect(md('<p>x<em> it</em>y</p>')).toBe('x *it*y')
  })

  it('drops empty formatting but keeps a space it held', () => {
    expect(md('<p>a<strong> </strong>b<em></em>c</p>')).toBe('a bc')
  })

  it('nests formatting of different kinds', () => {
    expect(md('<p><strong><em>very much</em></strong></p>')).toBe('***very much***')
    expect(md('<p><strong>very <em>much</em>, so</strong></p>')).toBe('**very *much*, so**')
    expect(md('<p><strong>very <em>much</em></strong></p>')).toBe('**very *much***')
    expect(md('<p><i><b>x</b></i> tail</p>')).toBe('***x*** tail')
    expect(md('<p><b><i>x</i></b> tail</p>')).toBe('***x*** tail')
  })

  it('flattens a wrapper nested inside its own kind', () => {
    expect(md('<p><em>A<em>B</em></em> x</p>')).toBe('*AB* x')
    expect(md('<p><b>A<b>B</b></b> x</p>')).toBe('**AB** x')
    expect(md('<p><b>A<i><b>B</b></i></b> x</p>')).toBe('**A*B*** x')
    expect(md('<p><b><b>x</b></b> tail</p>')).toBe('**x** tail')
    expect(md('<p><i><i>x</i></i> tail</p>')).toBe('*x* tail')
    expect(md('<p><em><em><strong>x</strong></em></em> tail</p>')).toBe('***x*** tail')
    expect(md('<p><i><b><i>x</i></b></i> tail</p>')).toBe('***x*** tail')
    expect(md('<p><i><i>x</i> y</i> tail</p>')).toBe('*x y* tail')
    expect(md('<p><i>y <i>x</i></i> tail</p>')).toBe('*y x* tail')
    expect(md('<p><del><s>x</s></del> tail</p>')).toBe('~~x~~ tail')
    expect(md('<p>lead <del><s>x</s></del> tail</p>')).toBe('lead ~~x~~ tail')
  })

  it('flattens a wrapper with a hard break on either edge', () => {
    expect(md('<p><b>bold<br></b> x</p>')).toBe('bold\\\nx')
    expect(md('<p><a href="https://x.dev">x<br></a> <b>y</b></p>')).toBe('x\\\n**y**')
    expect(md('<p><em><br>it</em> x</p>')).toBe('it x')
    expect(md('<p><b>a<br>b</b> x</p>')).toBe('**a\\\nb** x')
  })

  it('fences inline code longer than any backtick run it holds', () => {
    expect(md('<p><code>a`b</code></p>')).toBe('``a`b``')
    expect(md('<p><code>`x`</code></p>')).toBe('`` `x` ``')
  })

  it('pads a code span that begins and ends with a space', () => {
    expect(md('<p><code> a </code></p>')).toBe('`  a  `')
  })

  it('reads inline code as its visible text, without escaping', () => {
    expect(md('<p><code>a*b_c<span class="sr-only">Copied</span><svg aria-hidden="true"></svg></code></p>')).toBe('`a*b_c`')
  })

  it('flattens element children of inline code to their text', () => {
    expect(md('<p><code><u>x</u></code> <strong>now</strong></p>')).toBe('`x` **now**')
    expect(md('<p><code>a<sub>2</sub></code> <em>now</em></p>')).toBe('`a2` *now*')
    expect(md('<p><code><b>x</b></code> <del>now</del></p>')).toBe('`x` ~~now~~')
    expect(md('<p><code>a<span>b</span></code></p>')).toBe('`ab`')
  })

  it('writes a line break inside inline code as a space', () => {
    expect(md('<p><strong>x</strong> <code>printf a<br>printf b</code></p>')).toBe('**x** `printf a printf b`')
  })

  it('keeps private-use and block-marker characters in code and prose', () => {
    expect(md('<p><strong>see</strong> <code>x\uE000[Click](https://x.dev)</code></p>')).toBe('**see** `x\uE000[Click](https://x.dev)`')
    expect(md('<p><strong>see</strong> <code>x\uE001# heading</code></p>')).toBe('**see** `x\uE001# heading`')
    expect(md('<p><strong>a\uE000b</strong> c</p>')).toBe('**a\uE000b** c')
  })
})

describe('nodeToMarkdown: links', () => {
  it('writes [text](url)', () => {
    expect(md('<p>see <a href="https://example.com/a">the docs</a>.</p>')).toBe('see [the docs](https://example.com/a).')
  })

  it('keeps formatting inside the label', () => {
    expect(md('<p><a href="https://x.dev"><strong>bold</strong> link</a></p>')).toBe('[**bold** link](https://x.dev/)')
  })

  it('pastes an autolinked URL as the bare address', () => {
    expect(md('<p><a href="https://x.dev/a">https://x.dev/a</a></p>')).toBe('https://x.dev/a')
    expect(md('<p><a href="mailto:me@x.dev">me@x.dev</a></p>')).toBe('me@x.dev')
  })

  it('writes a link whose scheme the HTML flavour would drop as its label', () => {
    expect(md('<p><strong>see</strong> <a href="vscode://file/home/me/a.ts">the file</a></p>')).toBe('**see** the file')
    expect(md('<p><a href="/guide/a.md">the guide</a> <strong>x</strong></p>'))
      .toBe(`[the guide](${PAGE}/guide/a.md) **x**`)
  })

  it('resolves a relative destination against the given base, without its query or hash', () => {
    const root = mount('<p><strong>see</strong> <a href="other">o</a> <a href="#s">s</a></p>')
    expect(nodeToMarkdown(root, 'https://dash.example/chat/x?token=secret#frag'))
      .toBe('**see** [o](https://dash.example/chat/other) [s](https://dash.example/chat/x#s)')
  })

  it('writes an autolink containing a space as its label', () => {
    expect(md('<p><strong>see</strong> <a href="https://x.dev/a b">https://x.dev/a b</a></p>')).toBe('**see** https://x.dev/a b')
  })

  it('keeps a relative link whose label equals its href as a link, resolved against the page', () => {
    expect(md('<p><strong>see</strong> <a href="README.md">README.md</a></p>'))
      .toBe(`**see** [README.md](${PAGE}/README.md)`)
    expect(md('<p><a href="/guide/a.md">/guide/a.md</a></p>')).toBe(`[/guide/a.md](${PAGE}/guide/a.md)`)
  })

  it.each([
    ['a space', '/files/a b.md'],
    ['parentheses', '/files/a(1).md'],
    ['a private-use character', '/files/a\uE000.md'],
    ['a newline', 'https://x.dev/a\nb'],
    ['a control character', 'https://x.dev/a\x01b'],
    ['an entity reference', 'https://x.test/?a&copy;b'],
  ])('writes a link whose destination contains %s as its label', (_name, href) => {
    const anchor = mount('<p><strong>see</strong> <a>file</a></p>').querySelector('a')!
    anchor.setAttribute('href', href)
    expect(nodeToMarkdown(anchor.parentElement!)).toBe('**see** file')
  })

  it('keeps edge spaces outside the brackets', () => {
    expect(md('<p>a<a href="https://x.dev"> link </a>b</p>')).toBe('a [link](https://x.dev/) b')
  })

  it('escapes the square brackets of a label that holds one', () => {
    expect(md('<p><a href="https://x.dev">a]b [c]</a></p>')).toBe('[a\\]b \\[c\\]](https://x.dev/)')
    expect(md('<p><a href="https://x.dev">a [b] c</a></p>')).toBe('[a \\[b\\] c](https://x.dev/)')
  })

  it('never lets a label redirect the pasted link', () => {
    expect(md('<p><a href="https://x.dev">see](https://other.example/)</a></p>'))
      .toBe('[see\\](https://other.example/)](https://x.dev/)')
    expect(md('<p><a href="https://x.dev">[see](https://evil.example)</a></p>'))
      .toBe('[\\[see\\](https://evil.example)](https://x.dev/)')
  })

  it('keeps a backslash before a label bracket literal', () => {
    expect(md('<p><a href="https://x.dev">a\\]b</a></p>')).toBe('[a\\\\\\]b](https://x.dev/)')
  })

  it('leaves brackets inside a code span in a label unescaped', () => {
    expect(md('<p><a href="https://x.dev"><code>a[0]</code> [x]</a></p>'))
      .toBe('[`a[0]` \\[x\\]](https://x.dev/)')
  })

  it('leaves brackets outside a link label unescaped', () => {
    expect(md('<p>see [notes] and <a href="https://x.dev">docs</a></p>'))
      .toBe('see [notes] and [docs](https://x.dev/)')
  })

  it('drops a hard break at the edge of a block', () => {
    expect(md('<p><strong>bold</strong> text<br></p>')).toBe('**bold** text')
    expect(md('<p><br><strong>bold</strong> text</p>')).toBe('**bold** text')
    expect(md('<p><strong>a</strong><br>b</p>')).toBe('**a**\\\nb')
  })

  it('writes an anchor without href as its text', () => {
    expect(md('<p><a>plain</a></p>')).toBe('plain')
  })

  it('writes an exclamation mark before a link literally', () => {
    expect(md('<p><strong>x</strong> in!<a href="https://x.test/p">the docs</a></p>')).toBe('**x** in![the docs](https://x.test/p)')
    expect(md('<p><strong>x</strong> in! <a href="https://x.test/p">the docs</a></p>'))
      .toBe('**x** in! [the docs](https://x.test/p)')
  })
})

describe('nodeToMarkdown: blocks and whitespace', () => {
  it('separates paragraphs and headings with one blank line', () => {
    expect(md('<h2>Title</h2><p>one</p><p>two</p><hr><p>three</p>')).toBe('## Title\n\none\n\ntwo\n\n---\n\nthree')
  })

  it('drops layout whitespace between blocks', () => {
    expect(md('\n  <p>one</p>\n\n\n  <p>two</p>\n')).toBe('one\n\ntwo')
  })

  it('collapses whitespace runs inside text to one space', () => {
    expect(md('<p>a   b\n c</p>')).toBe('a b c')
  })

  it('writes a line break as a CommonMark hard break without surrounding spaces', () => {
    expect(md('<p>one <br> two</p>')).toBe('one\\\ntwo')
  })

  it('writes a line break inside a heading as a space, without the hard-break backslash', () => {
    expect(md('<h2>Title<br>subtitle</h2>')).toBe('## Title subtitle')
  })

  it('writes every heading level', () => {
    expect(md('<h1>a</h1><h6>b</h6>')).toBe('# a\n\n###### b')
  })

  it('copies heading text literally', () => {
    expect(md('<h2>Learn C#</h2>')).toBe('## Learn C#')
    expect(md('<h2>Title #</h2>')).toBe('## Title #')
  })

  it('treats a div as a block boundary', () => {
    expect(md('<div>a</div><div>b</div>')).toBe('a\n\nb')
  })

  it('flattens a wrapper or heading holding a block, whose delimiters would split', () => {
    expect(md('<div><b>a<div>b</div>c</b></div>')).toBe('a\n\nb\n\nc')
    expect(md('<div><a href="https://x.test/p">a<hr>c</a></div>')).toBe('a\n\n---\n\nc')
    expect(md('<h2>a<div>b</div>c</h2>')).toBe('a\n\nb\n\nc')
  })

  it('writes a code block as a text block with its line breaks', () => {
    expect(md('<p><strong>run</strong></p><pre><code>a = 1\n\nb = 2\n</code></pre><p>done</p>')).toBe('**run**\n\na = 1\n\nb = 2\n\ndone')
  })
})

describe('nodeToMarkdown: literal prose', () => {
  it('keeps ordinary prose punctuation', () => {
    expect(md(`<p>Letters 123, punctuation.;:!?'&quot; - () / % with <strong>format</strong>.</p>`))
      .toBe(`Letters 123, punctuation.;:!?'" - () / % with **format**.`)
  })

  it('copies a literal tilde', () => {
    expect(md('<p>numpy~=1.24 and scipy~=1.10 with <strong>format</strong></p>')).toBe('numpy~=1.24 and scipy~=1.10 with **format**')
  })

  it.each([
    ['an asterisk', '2*3'],
    ['a backtick', '`x`'],
    ['emphasis-shaped underscores', '_this_'],
    ['snake_case', 'snake_case'],
    ['a tag opener', '<div>'],
    ['a tilde run', 'a ~~ b'],
    ['a backslash', String.raw`\#`],
    ['a control character', 'a\x01b'],
    ['an entity', '&copy;'],
    ['a reference definition', '[note]: URL'],
    ['a citation', '[1] citation'],
    ['a private-use character', 'a\uE000b'],
  ])('copies prose containing %s literally', (_name, text) => {
    const root = mount('<p><strong>safe</strong> tail</p>')
    root.querySelector('p')!.append(document.createTextNode(` ${text}`))
    expect(nodeToMarkdown(root)).toBe(`**safe** tail ${text}`)
  })

  it.each([
    ['a heading', '# heading'],
    ['a numbered list item', '1. item'],
    ['a rule', '---'],
  ])('copies %s at a prose line start literally', (_name, text) => {
    const root = mount('<p><strong>safe</strong></p>')
    root.querySelector('p')!.prepend(document.createTextNode(text))
    expect(nodeToMarkdown(root)).toBe(`${text}**safe**`)
  })

  it('copies a line after a hard break literally', () => {
    expect(md('<p>safe <strong>bold</strong><br># heading</p>')).toBe('safe **bold**\\\n# heading')
  })

  it('writes adjacent siblings without a separating space', () => {
    expect(md('<p><b>a</b><b>b</b></p>')).toBe('**a****b**')
    expect(md('<p><b>a</b> <b>b</b></p>')).toBe('**a** **b**')
    expect(md('<p><b>a</b><i>b</i></p>')).toBe('**a***b*')
  })

  it('writes punctuation on a delimiter edge as-is', () => {
    expect(md('<p>A<strong>$5</strong></p>')).toBe('A**$5**')
    expect(md('<p>A <strong>$5</strong></p>')).toBe('A **$5**')
    expect(md('<p><strong>cost$</strong>B</p>')).toBe('**cost$**B')
    expect(md('<p>A<del>$5</del></p>')).toBe('A~~$5~~')
    expect(md('<p><del>cost$</del>B</p>')).toBe('~~cost$~~B')
    expect(md('<p>A<strong><em>$5</em></strong></p>')).toBe('A***$5***')
    expect(md('<p><strong><del>cost$</del></strong>B</p>')).toBe('**~~cost$~~**B')
    expect(md('<p>a<b>"q"</b>b</p>')).toBe('a**"q"**b')
  })

  it('copies a tilde inside a strikethrough body literally', () => {
    expect(md('<p><del>a ~ b</del> tail</p>')).toBe('~~a ~ b~~ tail')
    expect(md('<p>a ~ b <del>x</del></p>')).toBe('a ~ b ~~x~~')
  })
})

describe('nodeToMarkdown: rendered math', () => {
  const equation = '<span class="katex"><span class="katex-mathml"><math><semantics><mrow><mi>x</mi><msup><mi>y</mi><mn>2</mn></msup></mrow><annotation encoding="application/x-tex">x+y^2</annotation></semantics></math></span><span class="katex-html" aria-hidden="true"><span>x</span><span>+</span><span>y2</span></span></span>'

  it('writes TeX for inline, display, partial-selection and HTML copies, with a visible fallback', () => {
    expect(md(`<p>before ${equation} after</p>`)).toBe('before $x+y^2$ after')
    expect(md(`<p>before</p><span class="katex-display">${equation}</span><p>after</p>`))
      .toBe('before\n\n$$x+y^2$$\n\nafter')
    expect(md('<p><span class="katex"><span class="katex-html" aria-hidden="true">visible formula</span></span></p>'))
      .toBe('visible formula')

    const root = mount(`<p>before ${equation} after</p>`)
    const copy = selectionToCopy(rangeOver(root, ['y2', 0], ['y2', 1]), root)
    expect(copy?.markdown).toBe('$x+y^2$')
    expect(copy?.html).toBe('x+y^2')
  })
})

describe('nodeToMarkdown: hidden and flattened content', () => {
  it('skips screen-reader text, hidden views, icon-only buttons and unselectable rows', () => {
    expect(md('<p>a<span class="sr-only">status</span><span hidden>raw</span><button aria-label="Copy"><svg aria-hidden="true"></svg></button><span class="select-none"><button>Copy</button></span><span aria-hidden="true"><svg></svg></span>b</p>'))
      .toBe('ab')
  })

  describe('aria-hidden content is omitted', () => {
    it('omits aria-hidden text beside formatting', () => {
      const root = mount('<p>Run <code>git push<span aria-hidden="true"> --dry-run</span></code> <strong>now</strong></p>')
      expect(selectionToCopy(rangeOver(root, ['Run', 0], ['now', 3]), root)?.markdown).toBe('Run `git push` **now**')
      expect(md('<p><strong>x</strong> <span aria-hidden="true">shown</span></p>')).toBe('**x**')
    })

    it('omits aria-hidden text inside a code span', () => {
      const root = mount('<p>Run <code>git push<span aria-hidden="true"> --dry-run</span></code> now</p>')
      expect(selectionToCopy(rangeOver(root, ['git', 0], [' --dry-run', 5]), root)?.markdown).toBe('`git push`')
      expect(md('<p><code>git push<span aria-hidden="true"> --dry-run</span></code></p>')).toBe('`git push`')
    })

    it('skips a textless aria-hidden helper and converts the rest', () => {
      const root = mount('<p><strong>bold</strong><svg aria-hidden="true"></svg><span aria-hidden="true"> </span> tail</p>')
      const copy = selectionToCopy(rangeOver(root, ['bold', 0], [' tail', 5]), root)
      expect(copy?.markdown).toBe('**bold** tail')
      expect(copy?.html).toBe('<strong>bold</strong> tail')
    })
  })

  describe('a helper class is judged by what it renders, so a variant that shows it keeps its text', () => {
    // jsdom has no stylesheet, so an element with no inline style renders as
    // shown, the way `hidden sm:inline` does at desktop width; an inline
    // `display:none` stands in for the width where the variant hides it.
    it('keeps a hidden sm:inline span that renders beside formatting', () => {
      const root = mount('<p>Do <strong>not</strong><span class="hidden sm:inline"> ever</span> delete it</p>')
      expect(selectionToCopy(rangeOver(root, ['Do', 0], [' delete it', 10]), root)?.markdown).toBe('Do **not** ever delete it')
      expect(md('<p>Do <strong>not</strong><span class="hidden sm:inline"> ever</span> delete it</p>')).toBe('Do **not** ever delete it')
    })

    it('omits a hidden sm:inline span at a width where it computes to display:none', () => {
      const root = mount('<p>Do <strong>not</strong><span class="hidden sm:inline" style="display:none"> ever</span> delete it</p>')
      expect(selectionToCopy(rangeOver(root, ['Do', 0], [' delete it', 10]), root)?.markdown).toBe('Do **not** delete it')
    })

    it('keeps a hidden sm:inline span that renders inside a code span', () => {
      const root = mount('<p>Run <code>git push<span class="hidden sm:inline"> --dry-run</span></code> <strong>now</strong></p>')
      expect(selectionToCopy(rangeOver(root, ['Run', 0], ['now', 3]), root)?.markdown).toBe('Run `git push --dry-run` **now**')
      expect(md('<p><code>git push<span class="hidden sm:inline"> --dry-run</span></code></p>')).toBe('`git push --dry-run`')
    })

    it.each([
      ['sr-only md:not-sr-only'],
      ['select-none md:select-text'],
    ])('keeps a %s span, whose variant undoes the helper', (classes) => {
      const root = mount(`<p><strong>x</strong> a<span class="${classes}">payload</span> b</p>`)
      expect(selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)?.markdown).toBe('**x** apayload b')
    })

    it.each([['sr-only'], ['select-none']])('still omits a bare %s span', (classes) => {
      const root = mount(`<p><strong>x</strong> a<span class="${classes}">payload</span> b</p>`)
      expect(selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)?.markdown).toBe('**x** a b')
    })
  })

  it.each([
    ['a list', '<ul><li>a</li></ul>', 'before\n\na'],
    ['a code block', '<div class="code-block"><pre><code>x</code></pre></div>', 'before\n\nx'],
    ['a diff block', '<div class="diff-block"><span>+x</span></div>', 'before\n\n+x'],
    ['a table', '<table><tr><td>a</td></tr></table>', 'before\n\na'],
    ['a quote', '<blockquote>a</blockquote>', 'before\n\na'],
    ['an image', '<p><img alt="a" src="x.png"></p>', 'before'],
    ['math', '<p><span class="katex">x</span></p>', 'before\n\nx'],
    ['a diagram', '<div><svg></svg></div>', 'before'],
    ['subscript', '<p>H<sub>2</sub>O</p>', 'before\n\nH2O'],
    ['a collapsed diff chip', '<p><strong>see</strong></p><div><button data-diff-toggle="">src/a.ts +3 -1</button></div>', 'before\n\n**see**'],
    ['a link preview card', '<p><strong>see</strong></p><span><a href="https://x.dev"><span class="block">Title</span><span class="block">Description</span><span class="block">x.dev</span></a></span>', 'before\n\n**see**\n\n[Title Description x.dev](https://x.dev/)'],
    ['a key', '<p>press <kbd>Esc</kbd></p>', 'before\n\npress Esc'],
    ['underline', '<p><u>u</u></p>', 'before\n\nu'],
    ['a mark', '<p><strong>x</strong> <mark>critical</mark></p>', 'before\n\n**x** critical'],
  ])('writes %s as its text', (_name, html, expected) => {
    expect(md(`<p>before</p>${html}`)).toBe(expected)
  })

  it('returns null when nothing serializes', () => {
    expect(md('<p><span class="sr-only">status</span></p>')).toBeNull()
    expect(md('<div><svg></svg></div>')).toBeNull()
    expect(md('')).toBeNull()
  })

  it('keeps section siblings as separate elements in the HTML flavour', () => {
    const root = mount('<section><strong>one</strong></section><section>two</section>')
    const copy = selectionToCopy(rangeOver(root, ['one', 0], ['two', 3]), root)
    expect(copy?.html).toBe('<section><strong>one</strong></section><section>two</section>')
  })

  it('keeps block-class preview rows separate in both flavours', () => {
    const root = mount('<span><a href="https://x.dev"><span class="block">Title</span><span class="block">Description</span><span class="block">x.dev</span></a></span>')
    const copy = selectionToCopy(rangeOver(root, ['Title', 0], ['x.dev', 5]), root)
    expect(copy?.markdown).toBe('[Title Description x.dev](https://x.dev/)')
    expect(copy?.html).toBe('<a href="https://x.dev/"><div>Title</div><div>Description</div><div>x.dev</div></a>')
  })
})

describe('nodeToSafeHtml', () => {
  const html = (h: string) => nodeToSafeHtml(mount(h), document, 'https://dash.example/chat/x')

  it('keeps formatting tags and strips attributes', () => {
    expect(html('<p class="my-1" style="color:red" onclick="x()"><strong data-x="1">b</strong></p>'))
      .toBe('<p><strong>b</strong></p>')
  })

  it('keeps list, table, code-block and quote structure', () => {
    expect(html('<ul><li>one</li><li>two</li></ul>')).toBe('<ul><li>one</li><li>two</li></ul>')
    expect(html('<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>'))
      .toBe('<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>')
    expect(html('<pre><code>a\nb</code></pre>')).toBe('<pre><code>a\nb</code></pre>')
    expect(html('<blockquote><p>q</p></blockquote>')).toBe('<blockquote><p>q</p></blockquote>')
  })

  it('drops scripts, handlers and skipped helpers', () => {
    expect(html('<p>a<script>alert(1)</script><button>Copy</button><span class="sr-only">s</span>b</p>'))
      .toBe('<p>ab</p>')
  })

  it('unwraps unknown elements but keeps their text', () => {
    expect(html('<p><span class="chip"><mark>hit</mark></span></p>')).toBe('<p>hit</p>')
  })

  it('keeps http(s) and mailto links, resolving relative ones', () => {
    expect(html('<p><a href="/artifacts/a" target="_blank">x</a></p>'))
      .toBe('<p><a href="https://dash.example/artifacts/a">x</a></p>')
    expect(html('<p><a href="mailto:me@x.dev">m</a></p>')).toBe('<p><a href="mailto:me@x.dev">m</a></p>')
  })

  it('never carries the page query or hash into a resolved link', () => {
    const withToken = (h: string) => nodeToSafeHtml(mount(h), document, 'https://dash.example/chat/x?token=secret#frag')
    expect(withToken('<p><a href="#section">s</a></p>')).toBe('<p><a href="https://dash.example/chat/x#section">s</a></p>')
    expect(withToken('<p><a href="?q=1">q</a></p>')).toBe('<p><a href="https://dash.example/chat/x?q=1">q</a></p>')
    expect(withToken('<p><a href="other">o</a></p>')).toBe('<p><a href="https://dash.example/chat/other">o</a></p>')
  })

  it('drops a javascript: href', () => {
    expect(html('<p><a href="javascript:alert(1)">x</a></p>')).toBe('<p><a>x</a></p>')
  })
})

describe('selectionToCopy', () => {
  it('converts a selection that spans formatted text', () => {
    const root = mount('<p>Hello <strong>bold</strong> and <a href="https://x.dev">link</a> end</p>')
    const copy = selectionToCopy(rangeOver(root, ['Hello', 0], [' end', 4]), root)
    expect(copy?.markdown).toBe('Hello **bold** and [link](https://x.dev/) end')
    expect(copy?.html).toBe('Hello <strong>bold</strong> and <a href="https://x.dev/">link</a> end')
  })

  it('writes an entity-shaped link destination as its label', () => {
    const root = mount('<p><strong>see</strong> <a>link</a></p>')
    root.querySelector('a')!.setAttribute('href', 'https://x.test/?a&copy;b')
    expect(selectionToCopy(rangeOver(root, ['see', 0], ['link', 4]), root)?.markdown).toBe('**see** link')
  })

  it('converts an ordinary query string containing an ampersand', () => {
    const root = mount('<p><strong>see</strong> <a href="https://x.test/?a=1&b=2">link</a></p>')
    expect(selectionToCopy(rangeOver(root, ['see', 0], ['link', 4]), root)?.markdown)
      .toBe('**see** [link](https://x.test/?a=1&b=2)')
  })

  it('writes the same destination for a relative link in both flavours', () => {
    // Pasted into another site, a relative markdown destination would resolve
    // against that site; the HTML flavour already carries the absolute URL.
    const root = mount('<p><strong>see</strong> <a href="/settings/agents">agents</a></p>')
    const copy = selectionToCopy(rangeOver(root, ['see', 0], ['agents', 6]), root)
    const htmlHref = /href="([^"]+)"/.exec(copy?.html ?? '')?.[1]
    expect(htmlHref).toBe(`${PAGE}/settings/agents`)
    expect(copy?.markdown).toBe(`**see** [agents](${htmlHref})`)
  })

  it('writes a selected link with a private-use destination as its label', () => {
    const root = mount('<p><a href="https://x.dev/\uE001raw">selected link</a></p>')
    expect(selectionToCopy(rangeOver(root, ['selected link', 0], ['selected link', 13]), root)?.markdown).toBe('selected link')
  })

  it('restores the inline formatting a selection sits inside', () => {
    const root = mount('<p>go <a href="https://x.dev"><strong>right here</strong></a></p>')
    const copy = selectionToCopy(rangeOver(root, ['right', 0], ['right', 5]), root)
    expect(copy?.markdown).toBe('[**right**](https://x.dev/)')
  })

  it('restores the code span a selection sits inside', () => {
    const root = mount('<p>run <code>npm test</code></p>')
    expect(selectionToCopy(rangeOver(root, ['npm', 0], ['npm', 3]), root)?.markdown).toBe('`npm`')
  })

  it('writes a selection inside a code block as text with its line breaks', () => {
    const root = mount('<div class="code-block"><pre><code>let a = 1\nlet b = 2</code></pre></div>')
    expect(selectionToCopy(rangeOver(root, ['let', 2], ['let', 12]), root)?.markdown).toBe('t a = 1\nle')
  })

  it('writes a partial heading at the start of a cross-block drag as prose', () => {
    const root = mount('<h2>Long title</h2><p>body <strong>bold</strong></p>')
    const copy = selectionToCopy(rangeOver(root, ['Long', 5], ['bold', 4]), root)
    expect(copy?.markdown).toBe('title\n\nbody **bold**')
  })

  it('writes a partial heading at the end of a cross-block drag as prose', () => {
    const root = mount('<p>intro <em>it</em></p><h3>Next section</h3>')
    const copy = selectionToCopy(rangeOver(root, ['intro', 0], ['Next', 4]), root)
    expect(copy?.markdown).toBe('intro *it*\n\nNext')
  })

  it('keeps a fully covered heading at the start of a cross-block drag', () => {
    const root = mount('<h2>Title #</h2><p>body <strong>bold</strong></p>')
    const copy = selectionToCopy(rangeOver(root, ['Title', 0], ['bold', 4]), root)
    expect(copy?.markdown).toBe('## Title #\n\nbody **bold**')
  })

  it('copies a block-shaped line start literally', () => {
    const root = mount('<p>- 12 items remain, <strong>all green</strong></p>')
    expect(selectionToCopy(rangeOver(root, ['- 12', 0], ['all green', 9]), root)?.markdown).toBe('- 12 items remain, **all green**')
  })

  it('restores a heading when its whole text is selected', () => {
    const root = mount('<h2>Plain title</h2><p>after</p>')
    const copy = selectionToCopy(rangeOver(root, ['Plain', 0], ['Plain', 11]), root)
    expect(copy?.markdown).toBe('## Plain title')
  })

  it('keeps a partial heading as prose when only its text is selected', () => {
    const root = mount('<h2>Title <em>here</em></h2>')
    const copy = selectionToCopy(rangeOver(root, ['Title', 0], ['here', 2]), root)
    expect(copy?.markdown).toBe('Title *he*')
  })

  it('writes headings and paragraph breaks for a multi-block selection', () => {
    const root = mount('<h2>Title</h2><p>first</p><p>second</p>')
    const copy = selectionToCopy(rangeOver(root, ['Title', 0], ['second', 6]), root)
    expect(copy?.markdown).toBe('## Title\n\nfirst\n\nsecond')
  })

  it('writes plain prose as its text', () => {
    const root = mount('<p>just some words</p>')
    expect(selectionToCopy(rangeOver(root, ['just', 0], ['just', 9]), root)?.markdown).toBe('just some')
  })

  it('writes plain prose with markdown-like characters literally', () => {
    const root = mount('<p>SELECT * FROM t</p><p>- not a list</p>')
    expect(selectionToCopy(rangeOver(root, ['SELECT', 0], ['- not', 12]), root)?.markdown).toBe('SELECT * FROM t\n\n- not a list')
  })

  it('omits hidden helpers around plain text', () => {
    const root = mount('<p>words<span class="sr-only"><strong>x</strong></span> more</p>')
    expect(selectionToCopy(rangeOver(root, ['words', 0], [' more', 5]), root)?.markdown).toBe('words more')
  })

  it.each([
    ['bold', '<p><b style="font-weight:400">plain</b> x</p>'],
    ['italic', '<p><em style="font-style:normal">plain</em> x</p>'],
    ['strikethrough', '<p><del style="text-decoration:none">plain</del> x</p>'],
  ])('writes semantic %s removed by CSS as plain text', (_name, html) => {
    const root = mount(html)
    expect(selectionToCopy(rangeOver(root, ['plain', 0], [' x', 2]), root)?.markdown).toBe('plain x')
  })

  it.each([
    ['bold', '<p><b>plain</b> x</p>', '**plain** x'],
    ['italic', '<p><em>plain</em> x</p>', '*plain* x'],
    ['strikethrough', '<p><del>plain</del> x</p>', '~~plain~~ x'],
  ])('still converts unstyled semantic %s', (_name, html, expected) => {
    const root = mount(html)
    expect(selectionToCopy(rangeOver(root, ['plain', 0], [' x', 2]), root)?.markdown).toBe(expected)
  })

  it.each([
    ['a plain span', ''],
    ['a color-only span', ' style="color:red"'],
    ['a bold span', ' style="font-weight:700"'],
    ['an italic span', ' style="font-style:italic"'],
    ['an underlined span', ' style="text-decoration:underline"'],
    ['an uppercase span', ' style="text-transform:uppercase"'],
    ['a small-caps span', ' style="font-variant:small-caps"'],
    ['a half-opaque span', ' style="opacity:0.5"'],
  ])('writes the text of %s', (_name, attributes) => {
    const root = mount(`<p><strong>x</strong> a<span${attributes}>payload</span> b</p>`)
    expect(selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)?.markdown).toBe('**x** apayload b')
  })

  it.each([
    ['bold div', '<div style="font-weight:700">plain <em>marked</em></div>', ['plain ', 0], ['marked', 6], 'plain *marked*'],
    ['italic paragraph', '<p style="font-style:italic">a <strong>b</strong></p>', ['a ', 0], ['b', 1], 'a **b**'],
  ] as const)('converts inside a %s', (_name, html, start, end, expected) => {
    const root = mount(html)
    expect(selectionToCopy(rangeOver(root, start, end), root)?.markdown).toBe(expected)
  })

  it('still converts an inherited bold span inside strong', () => {
    const root = mount('<p><strong style="font-weight:700">x <span style="font-weight:700">payload</span></strong> b</p>')
    expect(selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)?.markdown).toBe('**x payload** b')
  })

  it.each([
    ['display:none'],
    ['visibility:hidden'],
    ['opacity:0'],
    ['font-size:0'],
    ['color:transparent'],
    ['color:rgba(0,0,0,0)'],
  ])('omits text hidden by %s', (style) => {
    const root = mount(`<p><strong>x</strong> a<span style="${style}">payload</span> b</p>`)
    const copy = selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)
    expect(copy?.markdown).toBe('**x** a b')
    expect(copy?.html).toBe('<strong>x</strong> a b')
  })

  it('keeps a hidden-class span whose computed style shows it', () => {
    const root = mount('<p><strong>x</strong> a<span class="hidden">payload</span> b</p>')
    expect(selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)?.markdown).toBe('**x** apayload b')
  })

  it('silently skips a hidden-class span whose computed style hides it', () => {
    const root = mount('<p><strong>x</strong> a<span class="hidden" style="display:none">payload</span> b</p>')
    const copy = selectionToCopy(rangeOver(root, ['x', 0], [' b', 2]), root)
    expect(copy?.markdown).toBe('**x** a b')
    expect(copy?.html).toBe('<strong>x</strong> a b')
  })

  it('writes a selection inside an element markdown cannot write as its text', () => {
    const root = mount('<p><u>under <strong>bold</strong> line</u></p>')
    expect(selectionToCopy(rangeOver(root, ['under', 0], [' line', 5]), root)?.markdown).toBe('under **bold** line')
  })

  it('writes a selection that reaches a list with the items as paragraphs', () => {
    const root = mount('<p><strong>intro</strong></p><ul><li>item</li><li>more</li></ul>')
    expect(selectionToCopy(rangeOver(root, ['intro', 0], ['more', 4]), root)?.markdown).toBe('**intro**\n\nitem\n\nmore')
  })

  it('returns null for a selection that leaves the root', () => {
    const root = mount('<p><strong>inside</strong></p>')
    const outside = mount('<p>outside</p>')
    const range = document.createRange()
    range.setStart(root.querySelector('strong')!.firstChild!, 0)
    range.setEnd(outside.firstChild!.firstChild!, 3)
    expect(selectionToCopy(range, root)).toBeNull()
  })

  it.each([
    ['image', '<div><img src="x.png" alt=""></div>'],
    ['SVG', '<div><svg></svg></div>'],
  ])('converts a selection that drags past the root into an %s-only sibling', (_name, sibling) => {
    const outer = mount(`<div class="bubble"><p>see <strong>this</strong></p></div>${sibling}`)
    const root = outer.querySelector('.bubble')!
    const range = document.createRange()
    range.setStart(textNode(root, 'see'), 0)
    range.setEnd(outer, 2)
    expect(selectionToCopy(range, root)?.markdown).toBe('see **this**')
  })

  it('converts a selection wholly inside an emphasised span', () => {
    const root = mount('<p><span style="font-weight:700">Do not <em>ever</em> run this</span></p>')
    expect(selectionToCopy(rangeOver(root, ['ever', 0], ['ever', 4]), root)?.markdown).toBe('*ever*')
  })

  it('converts a triple-click selection of the last block that ends just past the root', () => {
    const outer = mount('<div class="bubble"><p>first</p><p>see <strong>this</strong></p></div><p>next message</p>')
    const root = outer.querySelector('.bubble')!
    const range = document.createRange()
    range.setStart(textNode(root, 'see'), 0)
    // A multi-click selection of a container's last block ends at the start of
    // the next block, which lives in the container's parent.
    range.setEnd(outer, 1)
    expect(selectionToCopy(range, root)?.markdown).toBe('see **this**')
  })

  it('converts a triple-click overshoot into a next block whose wrapper carries a label', () => {
    const outer = mount('<div class="bubble"><p>see <strong>this</strong></p></div><div aria-label="Next message"><p>next</p></div>')
    const root = outer.querySelector('.bubble')!
    const range = document.createRange()
    range.setStart(textNode(root, 'see'), 0)
    range.setEnd(outer.querySelector('[aria-label] p')!, 0)
    expect(selectionToCopy(range, root)?.markdown).toBe('see **this**')
  })

  it('returns null for a selection that reaches a shadow-root host', () => {
    const root = mount('<p><strong>intro</strong></p><div class="host"></div><p>after</p>')
    root.querySelector('.host')!.attachShadow({ mode: 'open' }).textContent = 'hidden patch'
    expect(selectionToCopy(rangeOver(root, ['intro', 0], ['after', 5]), root)).toBeNull()
  })

  it('restores the link around a selection inside one row of a link preview card', () => {
    const root = mount('<p><strong>see</strong></p><span><a href="https://x.dev"><span class="block">Title <strong>here</strong></span><span class="block">Description</span></a></span>')
    expect(selectionToCopy(rangeOver(root, ['Title', 0], ['here', 4]), root)?.markdown).toBe('[Title **here**](https://x.dev/)')
  })

  it('writes a bare autolink beside markdown-like text literally', () => {
    const root = mount('<p>2*3 at <a href="https://x.dev">https://x.dev</a></p>')
    expect(selectionToCopy(rangeOver(root, ['2*3', 0], ['https', 13]), root)?.markdown).toBe('2*3 at https://x.dev')
  })

  it('returns null for a collapsed range', () => {
    const root = mount('<p><strong>x</strong></p>')
    const range = document.createRange()
    range.setStart(root.querySelector('strong')!.firstChild!, 0)
    expect(selectionToCopy(range, root)).toBeNull()
  })

  it('returns null when only hidden content is selected', () => {
    const root = mount('<p>a<span class="sr-only">status text</span>b</p>')
    expect(selectionToCopy(rangeOver(root, ['status', 0], ['status', 6]), root)).toBeNull()
  })
})

describe('copySelectionAsMarkdown', () => {
  function fakeEvent() {
    const data = new Map<string, string>()
    return {
      data,
      event: {
        clipboardData: { setData: (type: string, value: string) => { data.set(type, value) } } as unknown as DataTransfer,
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true },
      },
    }
  }

  function select(range: Range) {
    const sel = document.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
  }

  it('writes both flavours and cancels the browser copy', () => {
    const root = mount('<p>a <strong>b</strong></p>')
    select(rangeOver(root, ['a ', 0], ['b', 1]))
    const { data, event } = fakeEvent()
    expect(copySelectionAsMarkdown(event, root)).toBe(true)
    expect(event.defaultPrevented).toBe(true)
    expect(data.get('text/plain')).toBe('a **b**')
    expect(data.get('text/html')).toBe('a <strong>b</strong>')
  })

  it('does nothing when another handler already took the copy', () => {
    const root = mount('<p>a <strong>b</strong></p>')
    select(rangeOver(root, ['a ', 0], ['b', 1]))
    const { data, event } = fakeEvent()
    event.defaultPrevented = true
    expect(copySelectionAsMarkdown(event, root)).toBe(false)
    expect(data.size).toBe(0)
  })

  it('does nothing when nothing serializes', () => {
    const root = mount('<p>a<span class="sr-only">status text</span>b</p>')
    select(rangeOver(root, ['status', 0], ['status', 6]))
    const { data, event } = fakeEvent()
    expect(copySelectionAsMarkdown(event, root)).toBe(false)
    expect(event.defaultPrevented).toBe(false)
    expect(data.size).toBe(0)
  })
})
