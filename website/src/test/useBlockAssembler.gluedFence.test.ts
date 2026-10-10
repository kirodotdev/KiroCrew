import { describe, it, expect } from 'vitest'
import { parseBlocks, isGluedProse } from '../hooks/useBlockAssembler'

// A closing fence the model glued to the prose after it (```**Summary**) used
// to keep the rest of the reply, widget included, inside the fence. On a diff
// the chat then folded that tail away behind a collapsed chip.

const DIFF_HEAD = ['```diff', '--- /dev/null', '+++ /sample.txt', '@@ -0,0 +1 @@', '+sample'].join('\n') + '\n'
const TAIL = '**Financial summary**\n\n<mcwidget title="Summary"><div>Chart</div></mcwidget>'
const GLUED = DIFF_HEAD + '```' + TAIL
const SEPARATED = DIFF_HEAD + '```\n\n' + TAIL

const shape = (raw: string, streaming = false) =>
  parseBlocks(raw, streaming).map(b => ({ type: b.type, complete: b.complete, unclosed: !!b.unclosed }))

describe('parseBlocks: closing fence glued to prose', () => {
  it('splits the reported sample into diff, summary and widget', () => {
    const blocks = parseBlocks(GLUED, false)
    expect(blocks.map(b => b.type)).toEqual(['diff', 'markdown', 'widget'])
    expect(blocks[0].content).toBe('--- /dev/null\n+++ /sample.txt\n@@ -0,0 +1 @@\n+sample')
    expect(blocks[0].unclosed).toBeUndefined()
    expect(blocks[1].content).toContain('**Financial summary**')
    expect(blocks[2].content).toBe('<div>Chart</div>')
    expect(blocks[2].language).toBe('Summary')
  })

  it('yields the same blocks as the correctly separated control', () => {
    expect(shape(GLUED)).toEqual(shape(SEPARATED))
  })

  it('closes on a digit-first glued remainder, as fixCodeFences splits it', () => {
    // ```358KB is the shape fixCodeFences already split on markdown blocks,
    // and a digit counts as a prose start, so the fence closes on it here too.
    const blocks = parseBlocks(DIFF_HEAD + '```358KB saved', false)
    expect(blocks.map(b => b.type)).toEqual(['diff', 'markdown'])
    expect(blocks[1].content).toBe('358KB saved')
  })

  it.each([
    ['**Financial summary**', true],
    ['358KB', true],
    ['Done, see above.', false],
    ['rust,ignore', false],
    ['rust,no_run', false],
    ['js=', false],
    ['"""', false],
    ["');", false],
    ['# Totals', true],
    ['完了しました', true],
    ['python', false],
    ['{r}', false],
    [' python', false],
    ['x`y', false],
  ])('isGluedProse(%j) is %s', (rest, want) => {
    expect(isGluedProse(rest)).toBe(want)
  })

  it('re-reads a widget tag glued straight to the closer', () => {
    const blocks = parseBlocks(DIFF_HEAD + '```<mcwidget title="W"><b>x</b></mcwidget>', false)
    expect(blocks.map(b => b.type)).toEqual(['diff', 'widget'])
    expect(blocks[1].content).toBe('<b>x</b>')
  })

  it('recovers a glued closer on a plain code fence too', () => {
    const blocks = parseBlocks('```js\nconst x = 1\n```**Done**, see above.', false)
    expect(blocks.map(b => b.type)).toEqual(['code', 'markdown'])
    expect(blocks[0].content).toBe('const x = 1')
    expect(blocks[1].content).toBe('**Done**, see above.')
  })

  it('needs a run at least as long as the opener', () => {
    // A three-backtick run cannot close a four-backtick fence.
    const raw = '````diff\n+a\n```**not a close**\n+b\n````\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks.map(b => b.type)).toEqual(['diff', 'markdown'])
    expect(blocks[0].content).toBe('+a\n```**not a close**\n+b')
  })

  it('closes a four-backtick fence glued with a four-backtick run', () => {
    const blocks = parseBlocks('````diff\n+a\n````**Summary**', false)
    expect(blocks.map(b => b.type)).toEqual(['diff', 'markdown'])
    expect(blocks[1].content).toBe('**Summary**')
  })

  it('keeps an info-string-shaped line as content (a literal ```python inside code)', () => {
    const raw = '```js\nconst s = 1\n```python\nmore\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks[0].type).toBe('code')
    expect(blocks[0].content).toBe('const s = 1\n```python\nmore')
    expect(blocks[1].content).toBe('after')
  })

  it('keeps an attributed info string as content', () => {
    const blocks = parseBlocks('```python\nx\n```js {1,3}\ny\n```', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].content).toBe('x\n```js {1,3}\ny')
  })

  it.each([
    ['```{r}', 'an attribute block on its own'],
    ['```js:app.js', 'a tag with a colon filename'],
    ['```ts filename=x.ts', 'a tag with a spaced attribute'],
  ])('keeps %s inside a code fence as content (%s)', (inner) => {
    const blocks = parseBlocks('```python\nx = 1\n' + inner + '\ny\n```\nafter', false)
    expect(blocks.map(b => b.type)).toEqual(['code', 'markdown'])
    expect(blocks[0].content).toBe('x = 1\n' + inner + '\ny')
  })

  it('keeps a glued-looking line inside a markdown example as a nested opener', () => {
    // A markup fence keeps its pre-existing nesting: the line counts as an
    // inner opener, so the example closes only on its matching closers.
    const raw = '```markdown\nIntro\n```**Summary**\nbody\n```\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks.map(b => b.type)).toEqual(['code', 'markdown'])
    expect(blocks[0].content).toBe('Intro\n```**Summary**\nbody\n```')
    expect(blocks[1].content).toBe('after')
  })

  it('keeps an attributed inner opener inside a markdown example as an opener', () => {
    // ```python title="app.py" is a nested opener, not glued prose: the outer
    // example must stay one block and close on its own closer.
    const raw = '```markdown\n```python title="app.py"\nprint(1)\n```\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks.map(b => b.type)).toEqual(['code', 'markdown'])
    expect(blocks[0].content).toBe('```python title="app.py"\nprint(1)\n```')
    expect(blocks[0].unclosed).toBeUndefined()
    expect(blocks[1].content).toBe('after')
  })

  it('keeps a line with more backticks (code that contains backticks)', () => {
    const raw = '```js\nconst t = ```${a}` + `b`\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks[0].content).toBe('const t = ```${a}` + `b`')
    expect(blocks[1].content).toBe('after')
  })

  it('leaves a spaced remainder alone (``` foo is an opener shape, not glue)', () => {
    const blocks = parseBlocks('```python\na\n``` b c\n```', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].content).toBe('a\n``` b c')
  })

  it('does not close the outer fence while a nested markdown example is open', () => {
    const raw = '```markdown\n```python\nprint(1)\n```**bold in example**\n```\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks[0].type).toBe('code')
    // The glued-looking line stays inside the example. The nested-fence
    // heuristic reads it as one more inner opener, so this outer fence never
    // closes: it is flagged rather than folded, and nothing is lost.
    expect(blocks).toHaveLength(1)
    expect(blocks[0].content).toContain('```**bold in example**')
    expect(blocks[0].content).toContain('after')
    expect(blocks[0].unclosed).toBe(true)
  })

  it('keeps a literal widget tag inside a properly closed code block as code', () => {
    const raw = '```html\n<mcwidget title="x"><p>y</p></mcwidget>\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks.map(b => b.type)).toEqual(['code', 'markdown'])
    expect(blocks[0].content).toContain('<mcwidget')
  })

  it('recovers at every streaming prefix once the glued text is past the tag shape', () => {
    // Each throttled streaming parse sees a prefix; once the closer line holds
    // prose the summary leaves the diff, and the final parse matches.
    for (let n = DIFF_HEAD.length + 4; n <= GLUED.length; n++) {
      const prefix = GLUED.slice(0, n)
      const blocks = parseBlocks(prefix, true)
      const diff = blocks[0]
      if (prefix.includes('Financial summary')) {
        expect(diff.content).not.toContain('Financial')
      }
    }
    expect(shape(GLUED, false)).toEqual(shape(SEPARATED, false))
  })
})

describe('parseBlocks: lines that look glued but are code', () => {
  it('keeps a ```rust,ignore line inside a ```text fence as content', () => {
    const raw = '```text\nexample:\n```rust,ignore\nfn main() {}\n```\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks[0].content).toContain('```rust,ignore')
    expect(blocks[0].content).toContain('fn main() {}')
    expect(blocks[0].unclosed).toBeUndefined()
  })

  it('keeps a ```""" string line inside a ```python fence as code', () => {
    const raw = '```python\ndoc = \"\"\"\n```\"\"\"\nprint(doc)\n```\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks.map(b => b.type)).toEqual(['code', 'markdown'])
    expect(blocks[0].content).toContain('print(doc)')
    expect(blocks[1].content).toBe('after')
  })
})

describe('parseBlocks: glued closer inside a widget fence', () => {
  it('closes the inner fence and still ends the widget at a glued </mcwidget>', () => {
    const raw = '<mcwidget title="W">\n```js\nx()\n```</mcwidget>\n\n**After**'
    const blocks = parseBlocks(raw, false)
    expect(blocks.map(b => b.type)).toEqual(['widget', 'markdown'])
    expect(blocks[0].content).toBe('```js\nx()\n```')
    expect(blocks[1].content).toContain('**After**')
  })

  it('leaves a strict closer inside a widget unchanged', () => {
    const raw = '<mcwidget title="W">\n```js\nx()\n```\n</mcwidget>\nafter'
    const blocks = parseBlocks(raw, false)
    expect(blocks.map(b => b.type)).toEqual(['widget', 'markdown'])
    expect(blocks[0].content).toBe('```js\nx()\n```')
  })
})

describe('parseBlocks: observed closer vs finished streaming', () => {
  it('marks a finalized fence with no closer as unclosed', () => {
    const blocks = parseBlocks('```diff\n+a\n+b\nSome prose that was swallowed', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].complete).toBe(true)
    expect(blocks[0].unclosed).toBe(true)
  })

  it('does not mark a streaming fence as unclosed (it may still close)', () => {
    const blocks = parseBlocks('```diff\n+a', true)
    expect(blocks[0].complete).toBe(false)
    expect(blocks[0].unclosed).toBeUndefined()
  })

  it('does not mark a properly closed fence as unclosed', () => {
    expect(parseBlocks('```diff\n+a\n```', false)[0].unclosed).toBeUndefined()
  })

  it('keeps an info-string-shaped glue unrecovered but flags the fence', () => {
    // ```Summary is ambiguous (it has the shape of a language tag), so the
    // parser does not guess. The fence then never closes and is flagged.
    const blocks = parseBlocks('```diff\n+a\n```Summary\n\nmore text', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].unclosed).toBe(true)
  })
})

describe('parseBlocks: message boundaries', () => {
  it('parses two separate messages independently; a glue only exists inside one string', () => {
    // Separate assistant messages are rendered by separate MarkdownRenderer
    // calls, so each is parsed on its own and the closer stands alone.
    const first = parseBlocks(DIFF_HEAD + '```', false)
    const second = parseBlocks(TAIL, false)
    expect(first.map(b => b.type)).toEqual(['diff'])
    expect(first[0].unclosed).toBeUndefined()
    expect(second.map(b => b.type)).toEqual(['markdown', 'widget'])
  })

  it('treats token chunks of one message as one string with no inserted separator', () => {
    // Chunks are concatenated verbatim; splitting the closer across chunks
    // must give the same parse as the whole string.
    const chunks = [DIFF_HEAD + '``', '`**Financ', 'ial summary**\n\n<mcwidget title="Summary"><div>Chart</div></mcwidget>']
    expect(shape(chunks.join(''))).toEqual(shape(GLUED))
  })
})
