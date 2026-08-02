import { describe, it, expect } from 'vitest'
import { parseBlocks } from '../hooks/useBlockAssembler'

describe('parseBlocks', () => {
  it('parses plain markdown', () => {
    const blocks = parseBlocks('Hello **world**', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toEqual({ type: 'markdown', content: 'Hello **world**', complete: true, startLine: 1 })
  })

  it('parses a fenced code block', () => {
    const blocks = parseBlocks('before\n```js\nconst x = 1\n```\nafter', false)
    expect(blocks).toHaveLength(3)
    expect(blocks[0].type).toBe('markdown')
    expect(blocks[1]).toEqual({ type: 'code', content: 'const x = 1', language: 'js', complete: true, startLine: 3 })
    expect(blocks[2].type).toBe('markdown')
  })

  it('detects diff content', () => {
    const blocks = parseBlocks('```\n@@ -1,3 +1,4 @@\n-old\n+new\n```', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].type).toBe('diff')
  })

  it('detects diff language hint', () => {
    const blocks = parseBlocks('```diff\n+added line\n```', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].type).toBe('diff')
    expect(blocks[0].language).toBe('diff')
  })

  it('detects mermaid blocks', () => {
    const blocks = parseBlocks('```mermaid\ngraph TD\nA-->B\n```', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].type).toBe('mermaid')
    expect(blocks[0].language).toBe('mermaid')
  })

  it('marks unclosed fence as incomplete during streaming', () => {
    const blocks = parseBlocks('```python\ndef foo():\n  pass', true)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].type).toBe('code')
    expect(blocks[0].complete).toBe(false)
  })

  it('marks unclosed fence as complete when not streaming', () => {
    const blocks = parseBlocks('```python\ndef foo():\n  pass', false)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].complete).toBe(true)
  })

  it('handles multiple code blocks', () => {
    const blocks = parseBlocks('text\n```\ncode1\n```\nmiddle\n```\ncode2\n```', false)
    expect(blocks).toHaveLength(4)
    expect(blocks.filter(b => b.type === 'code')).toHaveLength(2)
    expect(blocks.filter(b => b.type === 'markdown')).toHaveLength(2)
  })

  it('assigns correct startLine to an empty code block (no stale carry-over)', () => {
    // `codeStart` is set at opening-fence detection, so an empty fence gets the
    // line after its own opening fence rather than inheriting the prior block's
    // start line.
    // Input lines (1-based):
    //   1: ```
    //   2: code
    //   3: ```
    //   4: (blank)
    //   5: ```
    //   6: ```
    const blocks = parseBlocks('```\ncode\n```\n\n```\n```', false)
    const codes = blocks.filter(b => b.type === 'code')
    expect(codes).toHaveLength(2)
    expect(codes[0].startLine).toBe(2) // line after 1st opening fence
    expect(codes[1].startLine).toBe(6) // line after 2nd opening fence (empty block)
  })

  it('handles empty input', () => {
    const blocks = parseBlocks('', false)
    expect(blocks).toHaveLength(0)
  })

  it('handles kiro-cli diff format', () => {
    const blocks = parseBlocks('```\n+10:const x = 1\n-5:const y = 2\n```', false)
    expect(blocks[0].type).toBe('diff')
  })

  describe('widgets', () => {
    it('extracts a simple block-level widget', () => {
      const blocks = parseBlocks('<mcwidget title="Card">\n<div>hello</div>\n</mcwidget>', false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0]).toMatchObject({
        type: 'widget',
        content: '<div>hello</div>',
        language: 'Card',
        complete: true,
      })
    })

    it('splits prose before and after a widget', () => {
      const blocks = parseBlocks('intro\n\n<mcwidget>\n<div>body</div>\n</mcwidget>\n\noutro', false)
      expect(blocks).toHaveLength(3)
      expect(blocks[0].type).toBe('markdown')
      expect(blocks[0].content).toMatch(/intro/)
      expect(blocks[1].type).toBe('widget')
      expect(blocks[1].content).toBe('<div>body</div>')
      expect(blocks[2].type).toBe('markdown')
      expect(blocks[2].content).toMatch(/outro/)
    })

    it('preserves blank lines inside widget body (sibling divs)', () => {
      const blocks = parseBlocks('<mcwidget>\n<div>a</div>\n\n<div>b</div>\n</mcwidget>', false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('widget')
      expect(blocks[0].content).toBe('<div>a</div>\n\n<div>b</div>')
    })

    it('marks unclosed widget as incomplete during streaming', () => {
      const blocks = parseBlocks('<mcwidget>\n<div>partial', true)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('widget')
      expect(blocks[0].complete).toBe(false)
    })

    it('marks unclosed widget as complete when not streaming', () => {
      const blocks = parseBlocks('<mcwidget>\n<div>partial', false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('widget')
      expect(blocks[0].complete).toBe(true)
    })

    // Bug 1: widget extraction must respect inline-code context
    it('does not extract widget tags wrapped in inline backticks', () => {
      const blocks = parseBlocks('Use the `<mcwidget>hello</mcwidget>` tag to embed HTML.', false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('markdown')
      expect(blocks[0].content).toBe('Use the `<mcwidget>hello</mcwidget>` tag to embed HTML.')
    })

    it('does not extract widget tags quoted with inline code across a paragraph', () => {
      const input = 'First mention `<mcwidget>x</mcwidget>` here.\nSecond mention `<mcwidget>y</mcwidget>` there.'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('markdown')
      expect(blocks.every(b => b.type !== 'widget')).toBe(true)
    })

    it('does not extract widget tags inside a fenced code block', () => {
      const input = '```html\n<mcwidget title="Demo">\n<div>hi</div>\n</mcwidget>\n```'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].language).toBe('html')
      expect(blocks.every(b => b.type !== 'widget')).toBe(true)
    })

    it('extracts a real widget on the same line as a backtick-quoted mention', () => {
      const input = 'The `<mcwidget>` tag:\n\n<mcwidget title="Real">\n<div>real body</div>\n</mcwidget>'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(2)
      expect(blocks[0].type).toBe('markdown')
      expect(blocks[0].content).toMatch(/`<mcwidget>` tag:/)
      expect(blocks[1].type).toBe('widget')
      expect(blocks[1].language).toBe('Real')
      expect(blocks[1].content).toBe('<div>real body</div>')
    })

    // Bug 2: fences inside widget bodies must not destroy the widget
    it('preserves a widget when its body contains a fenced code block', () => {
      const input = '<mcwidget title="Example">\n<div>before</div>\n```js\nconst x = 1\n```\n<div>after</div>\n</mcwidget>'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('widget')
      expect(blocks[0].language).toBe('Example')
      expect(blocks[0].content).toContain('<div>before</div>')
      expect(blocks[0].content).toContain('```js')
      expect(blocks[0].content).toContain('const x = 1')
      expect(blocks[0].content).toContain('<div>after</div>')
      expect(blocks.every(b => b.type !== 'code')).toBe(true)
    })

    it('preserves a widget when body contains a fence with blank lines around it', () => {
      const input = '<mcwidget>\n<div>a</div>\n\n```\ncode\n```\n\n<div>b</div>\n</mcwidget>'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('widget')
      expect(blocks[0].content).toContain('<div>a</div>')
      expect(blocks[0].content).toContain('<div>b</div>')
    })

    it('close tag inside a fence inside a widget does not terminate the widget', () => {
      const input = '<mcwidget>\n<div>pre</div>\n```\n</mcwidget>\n```\n<div>post</div>\n</mcwidget>'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('widget')
      expect(blocks[0].content).toContain('<div>pre</div>')
      expect(blocks[0].content).toContain('<div>post</div>')
    })

    it('handles two adjacent widgets separated by a blank line', () => {
      const input = '<mcwidget>\n<div>a</div>\n</mcwidget>\n\n<mcwidget>\n<div>b</div>\n</mcwidget>'
      const blocks = parseBlocks(input, false)
      const widgets = blocks.filter(b => b.type === 'widget')
      expect(widgets).toHaveLength(2)
      expect(widgets[0].content).toBe('<div>a</div>')
      expect(widgets[1].content).toBe('<div>b</div>')
    })

    it('handles a single-line widget (open and close on same line)', () => {
      const blocks = parseBlocks('prefix <mcwidget title="Inline">BODY</mcwidget> suffix', false)
      const widget = blocks.find(b => b.type === 'widget')
      expect(widget).toBeDefined()
      expect(widget?.content).toBe('BODY')
      expect(widget?.language).toBe('Inline')
    })

    it('does not detect widgets inside multi-backtick inline code', () => {
      // Multi-backtick runs (``text``) can contain single backticks as content.
      const blocks = parseBlocks('See ``<mcwidget>x</mcwidget>`` for details.', false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('markdown')
    })

    it('leaves unbalanced backtick runs unmasked (widget still detected beyond them)', () => {
      // An unbalanced single backtick should not eat the rest of the line.
      const input = 'Stray ` backtick\n<mcwidget>\n<div>body</div>\n</mcwidget>'
      const blocks = parseBlocks(input, false)
      const widget = blocks.find(b => b.type === 'widget')
      expect(widget).toBeDefined()
      expect(widget?.content).toBe('<div>body</div>')
    })

    // On the trailing line of a streaming buffer, an unmatched opening
    // backtick must be treated as inline-code-still-arriving so a tag inside
    // it doesn't transition the parser to widget state mid-stream.
    describe('streaming with unmatched backticks on the trailing line', () => {
      it('does not produce a widget while a backtick-quoted tag is being streamed', () => {
        // Snapshots from a single streaming session of:
        //   "Use the `<mcwidget>` tag here."
        const stages = [
          'Use the ',
          'Use the `',
          'Use the `<',
          'Use the `<mcw',
          'Use the `<mcwidget',
          'Use the `<mcwidget>',          // <-- the snapshot that must not flip to widget state
          'Use the `<mcwidget>`',
          'Use the `<mcwidget>` tag',
          'Use the `<mcwidget>` tag here.',
        ]
        for (const s of stages) {
          const blocks = parseBlocks(s, true)
          const widgets = blocks.filter(b => b.type === 'widget')
          expect(widgets, `snapshot ${JSON.stringify(s)} produced widget(s)`).toHaveLength(0)
        }
      })

      it('on completion (streaming=false) leaves the prose intact as a single markdown block', () => {
        const blocks = parseBlocks('Use the `<mcwidget>` tag here.', false)
        expect(blocks).toHaveLength(1)
        expect(blocks[0].type).toBe('markdown')
        expect(blocks[0].content).toBe('Use the `<mcwidget>` tag here.')
      })

      it('still recognizes a real streaming widget on its own line (no preceding inline code)', () => {
        // No backtick before the tag — must transition to widget state and
        // produce a provisional widget block during streaming.
        const blocks = parseBlocks('intro\n<mcwidget>\n<div>partial', true)
        const widget = blocks.find(b => b.type === 'widget')
        expect(widget).toBeDefined()
        expect(widget?.complete).toBe(false)
      })

      it('once the closing backtick streams in, the widget tag reverts to inline code', () => {
        // Adjacent snapshots differ only by the trailing `:
        const before = parseBlocks('Use the `<mcwidget>', true)
        const after = parseBlocks('Use the `<mcwidget>`', true)
        expect(before.filter(b => b.type === 'widget')).toHaveLength(0)
        expect(after.filter(b => b.type === 'widget')).toHaveLength(0)
      })

      it('non-trailing line with stray backtick is still treated as literal (CommonMark)', () => {
        // The first line has an unmatched backtick but already has a newline
        // after it — per CommonMark inline code is line-bounded, so unbalanced
        // there means literal text. A real widget tag on the next line still
        // transitions to widget state.
        const input = 'Stray ` here\n<mcwidget>\n<div>body</div>\n</mcwidget>'
        const blocks = parseBlocks(input, true)
        const widget = blocks.find(b => b.type === 'widget')
        expect(widget).toBeDefined()
        expect(widget?.content).toBe('<div>body</div>')
      })

      it('multi-backtick run on the trailing line behaves the same as single', () => {
        // Streaming snapshot: ``<mcwidget>x  (double backtick run, unmatched)
        const blocks = parseBlocks('Mention ``<mcwidget>x', true)
        expect(blocks.filter(b => b.type === 'widget')).toHaveLength(0)
      })
    })
  })

  describe('nested code fences', () => {
    it('keeps markdown containing inner code blocks as a single code block', () => {
      // The classic bug: a markdown code block contains a python example.
      // The inner ``` should NOT close the outer fence.
      const input = '```markdown\nHere is some code:\n```python\nprint("hello")\n```\nMore text\n```'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].language).toBe('markdown')
      expect(blocks[0].content).toContain('```python')
      expect(blocks[0].content).toContain('print("hello")')
      expect(blocks[0].content).toContain('More text')
    })

    it('handles multiple nested fence pairs inside a code block', () => {
      const input = '```md\n# Title\n```js\nconst x = 1\n```\nsome text\n```python\ny = 2\n```\nend\n```'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].language).toBe('md')
      expect(blocks[0].content).toContain('```js')
      expect(blocks[0].content).toContain('```python')
      expect(blocks[0].content).toContain('end')
    })

    it('longer outer fence is not affected (CommonMark proper nesting)', () => {
      // 4-backtick outer fence with 3-backtick inner — already works per spec
      const input = '````markdown\n```python\ncode\n```\n````'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].content).toContain('```python')
      expect(blocks[0].content).toContain('code')
    })

    it('simple fence without nesting still closes normally', () => {
      // Regression guard: basic fences must still work
      const input = 'before\n```js\nconst x = 1\n```\nafter'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(3)
      expect(blocks[0].type).toBe('markdown')
      expect(blocks[1].type).toBe('code')
      expect(blocks[1].content).toBe('const x = 1')
      expect(blocks[2].type).toBe('markdown')
    })

    it('bare inner fence (no language) is treated as outer close per CommonMark', () => {
      // A bare ``` inside a code block has no language identifier, so we
      // cannot distinguish it from the outer closing fence. Per CommonMark
      // spec, it closes the outer. Only inner fences WITH a language are
      // tracked as nested opens.
      const input = '```markdown\n```\n```\n```'
      const blocks = parseBlocks(input, false)
      // Line 2 (bare ```) closes the outer → produces first code block (empty)
      // Line 3 (bare ```) opens a new fence; line 4 closes it → second code block (empty)
      expect(blocks).toHaveLength(2)
      expect(blocks[0].type).toBe('code')
      expect(blocks[1].type).toBe('code')
    })

    it('nested fences during streaming produce incomplete block', () => {
      const input = '```markdown\n```python\ncode\n```\nmore content'
      const blocks = parseBlocks(input, true)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].complete).toBe(false)
      expect(blocks[0].content).toContain('```python')
    })

    it('unbalanced inner opens do not prevent outer close', () => {
      // Only one inner open, but two potential closes — the first close
      // pairs with the inner open, the second closes the outer.
      const input = '```markdown\n```python\ncode\n```\n```'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].content).toBe('```python\ncode\n```')
    })

    it('non-markup outer fence does NOT track inner fences (python)', () => {
      // A ```python block containing a ```js line should NOT treat it as a
      // nested fence. The bare ``` closes the outer fence normally.
      const input = '```python\n# example\n```js\nconsole.log("hi")\n```\nafter text'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(2)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].language).toBe('python')
      expect(blocks[0].content).toBe('# example\n```js\nconsole.log("hi")')
      expect(blocks[1].type).toBe('markdown')
      expect(blocks[1].content).toContain('after text')
    })

    it('non-markup outer fence does NOT track inner fences (bash)', () => {
      const input = '```bash\ncat <<EOF\n```markdown\n# Title\n```\nEOF\n```\noutside'
      const blocks = parseBlocks(input, false)
      // First bare ``` on line 5 closes the outer (no depth tracking for bash)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].language).toBe('bash')
      expect(blocks[0].content).toBe('cat <<EOF\n```markdown\n# Title')
      // "EOF" is markdown between the two fences
      expect(blocks.some(b => b.type === 'markdown' && b.content.includes('EOF'))).toBe(true)
    })

    it('unbalanced inner opens cause over-consumption when depth never reaches zero', () => {
      // Two inner opens but only one bare ``` follows — depth decrements to 1
      // but never reaches 0, so the outer fence never closes. At EOF with
      // streaming=false, the unclosed block is flushed as complete.
      const input = '```markdown\n```python\n```js\ncode\n```\ntrailing'
      const blocks = parseBlocks(input, false)
      // The single bare ``` only decrements depth from 2 to 1; outer never closes.
      // Everything is consumed as one code block (over-consumption tradeoff).
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].language).toBe('markdown')
      expect(blocks[0].content).toContain('```python')
      expect(blocks[0].content).toContain('trailing')
    })

    it('no-language outer fence (bare ```) enables nesting', () => {
      // A bare ``` opening (no language) is in the nestable set (empty string)
      // since the most common LLM output pattern is ``` with no lang showing markdown.
      const input = '```\nHere:\n```python\nprint(1)\n```\nDone\n```'
      const blocks = parseBlocks(input, false)
      expect(blocks).toHaveLength(1)
      expect(blocks[0].type).toBe('code')
      expect(blocks[0].content).toContain('```python')
      expect(blocks[0].content).toContain('Done')
    })
  })
})
