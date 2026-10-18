/**
 * MarkdownRenderer: the mcwidget and leaked <tool_use> strips over the message
 * source, both of which leave inline code alone, and the softBreaks option that
 * decides when a single newline renders as a line break.
 */
import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

// The same branding stub as MarkdownRenderer.test.tsx: a direct-local session.
vi.mock('../hooks/useBranding', () => ({
  useBranding: () => ({ botName: 'Test', avatar: '', directLocal: true }),
}))

describe('MarkdownRenderer mcwidget strip is inline-code-aware', () => {
  it('preserves prose when an unclosed widget tag appears inside an inline-code span', () => {
    // The `<mcwidget[\s\S]*$` alternative must not eat from the literal opening
    // tag (inside backticks) to end-of-block, or it drops the rest of the prose.
    const content = 'In a chat: ask the agent to emit any `<mcwidget>` (e.g. "render a CR queue widget"), then click Bookmark.'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('emit any')
    expect(text).toContain('render a CR queue widget')
    expect(text).toContain('click Bookmark')
  })

  it('preserves a balanced inline-code mention of a widget tag pair', () => {
    const content = 'Use `<mcwidget>hello</mcwidget>` to embed HTML.'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('to embed HTML')
  })

  it('preserves real prose AFTER a backtick-wrapped tag mention earlier in the block', () => {
    const content = [
      '- Sidebar shows Artifacts',
      '- In a chat: ask the agent to emit any `<mcwidget>` (e.g. "render a CR queue widget")',
      '- Navigate to /artifacts',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('Sidebar shows Artifacts')
    expect(text).toContain('render a CR queue widget')
    expect(text).toContain('Navigate to /artifacts')
  })
})

describe('MarkdownRenderer strips leaked <tool_use> protocol markup', () => {
  it('strips a complete <tool_use>...</tool_use> block and renders surrounding markdown', () => {
    // When the agent leaks the full Anthropic tool_use wrapper as text, the
    // unknown <tool_use> element would otherwise trap the JSON body (including
    // escaped \n literals) into a single paragraph, dropping all the headers and
    // rating callouts. The strip pass removes the wrapper and its body so the
    // surrounding prose renders normally.
    const content = [
      "I'll generate the review.",
      '',
      '<tool_use> {"tool_calls": [{"tool_name": "write_file", "parameters": {"file_path": "/tmp/x.md", "content": "### Heading\\n\\n**Rating:** Mixed"}}]} </tool_use>',
      '',
      'Review saved.',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain("I'll generate the review.")
    expect(text).toContain('Review saved.')
    // Tag itself and its JSON body must NOT leak through
    expect(text).not.toContain('tool_calls')
    expect(text).not.toContain('write_file')
    expect(text).not.toContain('<tool_use>')
    // getElementsByTagName (not querySelector('tool_use')): happy-dom parses the
    // arg as a CSS selector and rejects the bare `tool_use` tag name as invalid,
    // whereas getElementsByTagName takes a literal tag name on every engine.
    expect(container.getElementsByTagName('tool_use')).toHaveLength(0)
  })

  it('strips an unclosed <tool_use> opener (mid-stream)', () => {
    // During streaming the closing tag may not have arrived yet. The strip
    // regex falls through to the `<tool_use[\s\S]*$` alternative and removes
    // everything from the opener to end of block.
    const content = 'Working on it…\n\n<tool_use> {"tool_calls": [{"tool_name": "write_'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('Working on it')
    expect(text).not.toContain('tool_calls')
    expect(text).not.toContain('write_')
  })

  it('strips multiple <tool_use> blocks in the same message', () => {
    const content = [
      'First action:',
      '<tool_use>{"a": 1}</tool_use>',
      'Second action:',
      '<tool_use>{"b": 2}</tool_use>',
      'Done.',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('First action:')
    expect(text).toContain('Second action:')
    expect(text).toContain('Done.')
    expect(text).not.toContain('"a"')
    expect(text).not.toContain('"b"')
  })

  it('preserves <tool_use> mentions inside inline-code spans', () => {
    // Author documenting the protocol in prose: e.g. `<tool_use>` should
    // remain visible. The strip pass uses the same maskInlineCode helper as
    // the widget strip, so backtick-wrapped tag mentions are not removed.
    const content = 'When the agent emits a literal `<tool_use>` tag, the dashboard now strips it.'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('<tool_use>')
    expect(text).toContain('the dashboard now strips it.')
  })

  it('does NOT strip <tool_use> mentions inside fenced code blocks', () => {
    // When the agent is documenting the protocol in a code block, the tags
    // are real content — the fence makes the markdown renderer treat them
    // as literal text and the strip pass operates on markdown blocks only,
    // not extracted code blocks. Regression guard for documentation messages.
    const content = '```\n<tool_use>{"x": 1}</tool_use>\n```'
    const { container } = render(<MarkdownRenderer content={content} />)
    const text = container.textContent || ''
    expect(text).toContain('<tool_use>')
    expect(text).toContain('"x"')
  })
})

describe('MarkdownRenderer softBreaks', () => {
  it('converts a soft line break to <br> when softBreaks is set', () => {
    const { container } = render(<MarkdownRenderer content={'line one\nline two'} softBreaks />)
    expect(container.querySelectorAll('br').length).toBe(1)
    expect(container.textContent).toContain('line one')
    expect(container.textContent).toContain('line two')
  })

  it('drops the redundant <br> between two attached images (blocks already break)', () => {
    // Each image renders as its own block (span.block.my-2); a <br> between
    // them adds an empty line box and blocks margin collapse, inflating the
    // gap between two attached screenshots from ~8px to ~37px.
    const { container } = render(<MarkdownRenderer
      content={'shots\n\n![a](/api/file-raw?path=a.png)\n![b](/api/file-raw?path=b.png)'} softBreaks />)
    expect(container.querySelectorAll('img').length).toBe(2)
    expect(container.querySelectorAll('br').length).toBe(0)
  })

  it('keeps the <br> between an image and following TEXT (only image-adjacent breaks drop)', () => {
    const { container } = render(<MarkdownRenderer
      content={'line one\nline two\n\n![a](https://x.test/a.png)'} softBreaks />)
    // the text-to-text break survives; none render adjacent to the image
    expect(container.querySelectorAll('br').length).toBe(1)
  })

  it('collapses a soft line break by default (no softBreaks, no <br>)', () => {
    const { container } = render(<MarkdownRenderer content={'line one\nline two'} />)
    expect(container.querySelector('br')).toBeNull()
  })

  it('does not inject <br> between loose list items — block spacing stays normal', () => {
    // A blank line between items makes a "loose" list. The soft-break plugin
    // must only touch soft breaks inside text; block separators (parsed as
    // distinct blocks) stay untouched, so list items keep normal spacing and
    // no literal blank line is rendered between them.
    const { container } = render(<MarkdownRenderer content={'1. first\n\n2. second'} softBreaks />)
    expect(container.querySelectorAll('ol > li').length).toBe(2)
    expect(container.querySelector('br')).toBeNull()
  })

  it('preserves multiple soft breaks in a paragraph as multiple <br> when softBreaks is set', () => {
    const { container } = render(<MarkdownRenderer content={'a\nb\nc'} softBreaks />)
    expect(container.querySelectorAll('br').length).toBe(2)
  })
})
