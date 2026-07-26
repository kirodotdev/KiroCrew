import { describe, it, expect } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import MarkdownRenderer, { Lightbox, dispatchLightbox } from '../components/MarkdownRenderer'

type LightboxDetail = { images: { src: string; alt: string }[]; index: number }

describe('MarkdownRenderer list indentation', () => {
  it('renders ul with pl-8 and marker:text-muted', () => {
    const { container } = render(<MarkdownRenderer content={'- a\n- b'} />)
    const ul = container.querySelector('ul')
    expect(ul).not.toBeNull()
    expect(ul!.className).toContain('pl-8')
    expect(ul!.className).toContain('marker:text-muted')
  })

  it('renders ol with pl-8 and marker:text-muted', () => {
    const { container } = render(<MarkdownRenderer content={'1. a\n2. b'} />)
    const ol = container.querySelector('ol')
    expect(ol).not.toBeNull()
    expect(ol!.className).toContain('pl-8')
    expect(ol!.className).toContain('marker:text-muted')
  })
})

describe('MarkdownRenderer streaming caret', () => {
  it('appends an inline streaming caret after the trailing text while streaming', () => {
    const { container } = render(<MarkdownRenderer content={'Hello world'} streaming glow />)
    const caret = container.querySelector('.streaming-caret')
    expect(caret).not.toBeNull()
    // Inline placement: the caret lives inside the paragraph (same line as the
    // last word), not as a bare block-level sibling of the root container.
    expect(caret!.closest('p')).not.toBeNull()
  })

  it('does not render a caret when not streaming', () => {
    const { container } = render(<MarkdownRenderer content={'Hello world'} />)
    expect(container.querySelector('.streaming-caret')).toBeNull()
  })

  it('places the caret AFTER a trailing inline code span (not before it)', () => {
    const { container } = render(<MarkdownRenderer content={'Hello `world`'} streaming glow />)
    const code = container.querySelector('code')
    const caret = container.querySelector('.streaming-caret')
    expect(code).not.toBeNull()
    expect(caret).not.toBeNull()
    // The caret must follow the <code> element in document order.
    expect(!!(code!.compareDocumentPosition(caret!) & Node.DOCUMENT_POSITION_FOLLOWING)).toBe(true)
  })
})

describe('MarkdownRenderer dollar-sign handling (currency vs math)', () => {
  it('treats single-$ currency as plain text, not inline math', () => {
    // Regression for: chat messages like `$9.99` accidentally parsed as
    // inline math spanning multiple $ signs, crashing KaTeX + React commit.
    const { container } = render(
      <MarkdownRenderer content={'Product A = $9.99 and Product B = $19.95'} />
    )
    // No KaTeX math span should be produced
    expect(container.querySelector('.katex')).toBeNull()
    // The raw dollar amounts should still appear as text
    expect(container.textContent).toContain('$9.99')
    expect(container.textContent).toContain('$19.95')
  })

  it('does not treat currency + em-dash + en-dash as math (prior crash trigger)', () => {
    // En-dash (U+2013) inside a would-be math block triggered KaTeX strict
    // warning -> bad HTML -> React commit crash ("String contains an invalid
    // character"). With singleDollarTextMath=false, this should render cleanly.
    const content = 'Total — see line items 1 – 3: $10.00 plus $5.00 tax'
    const { container } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelector('.katex')).toBeNull()
    expect(container.textContent).toContain('$10.00')
    expect(container.textContent).toContain('$5.00')
  })

  it('still renders $$...$$ display math via KaTeX', () => {
    // Regression guard: disabling singleDollarTextMath must NOT break real math.
    const { container } = render(<MarkdownRenderer content={'$$a^2 + b^2 = c^2$$'} />)
    // Display math produces a .katex-display wrapper or at least a .katex span
    const katex = container.querySelector('.katex, .katex-display')
    expect(katex).not.toBeNull()
  })
})

describe('MarkdownRenderer XSS sanitization', () => {
  it('strips iframe elements from markdown', () => {
    const { container } = render(
      <MarkdownRenderer content={'<iframe srcdoc="<script>alert(1)</script>"></iframe>'} />
    )
    expect(container.querySelector('iframe')).toBeNull()
  })

  it('strips script elements from markdown', () => {
    const { container } = render(
      <MarkdownRenderer content={'<script>fetch("/api/config")</script>'} />
    )
    expect(container.querySelector('script')).toBeNull()
  })

  it('strips event handler attributes', () => {
    const { container } = render(
      <MarkdownRenderer content={'<img src="x" onerror="alert(1)">'} />
    )
    const img = container.querySelector('img')
    expect(img?.getAttribute('onerror')).toBeNull()
  })

  it('strips javascript: hrefs', () => {
    const { container } = render(
      <MarkdownRenderer content={'<a href="javascript:alert(1)">click</a>'} />
    )
    const a = container.querySelector('a')
    // href is deleted entirely — either element has no href or doesn't render as <a>
    if (a) {
      expect(a.getAttribute('href')).toBeNull()
    }
    // Verify no javascript: anywhere in the output
    expect(container.innerHTML).not.toContain('javascript:')
  })

  it('preserves safe HTML elements like details/summary', () => {
    const { container } = render(
      <MarkdownRenderer content={'<details><summary>Info</summary>Content</details>'} />
    )
    expect(container.querySelector('details')).not.toBeNull()
    expect(container.querySelector('summary')).not.toBeNull()
  })

  it('preserves safe elements like kbd and mark', () => {
    const { container } = render(
      <MarkdownRenderer content={'Press <kbd>Ctrl+C</kbd> to copy'} />
    )
    expect(container.querySelector('kbd')).not.toBeNull()
  })

  it('strips javascript: with embedded control characters (bypass variant)', () => {
    const { container } = render(
      <MarkdownRenderer content={'<a href="java\tscript:alert(1)">click</a>'} />
    )
    expect(container.innerHTML).not.toContain('javascript:')
  })

  it('strips data: URI XSS payloads in href', () => {
    const { container } = render(
      <MarkdownRenderer content={'<a href="data:text/html,<script>alert(1)</script>">click</a>'} />
    )
    expect(container.innerHTML).not.toContain('data:text/html')
  })
})

describe('MarkdownRenderer GFM task-list checkboxes', () => {
  it('renders - [ ] and - [x] as checkbox inputs', () => {
    const { container } = render(
      <MarkdownRenderer content={'- [ ] unchecked\n- [x] checked'} />
    )
    const checkboxes = container.querySelectorAll('input[type="checkbox"]')
    expect(checkboxes).toHaveLength(2)
    expect((checkboxes[0] as HTMLInputElement).checked).toBe(false)
    expect((checkboxes[1] as HTMLInputElement).checked).toBe(true)
    expect((checkboxes[0] as HTMLInputElement).disabled).toBe(true)
  })

  it('still strips non-checkbox input elements (XSS safety)', () => {
    const { container } = render(
      <MarkdownRenderer content={'<input type="text" value="xss">'} />
    )
    expect(container.querySelector('input[type="text"]')).toBeNull()
  })

  it('renders task-list ul without bullet disc', () => {
    const { container } = render(
      <MarkdownRenderer content={'- [ ] foo\n- [x] bar'} />
    )
    const ul = container.querySelector('ul')
    expect(ul!.className).toContain('list-none')
    expect(ul!.className).not.toContain('list-disc')
  })
})

describe('MarkdownRenderer PATH_RE colon support', () => {
  it('renders path with colon in filename as clickable', () => {
    const { container } = render(
      <MarkdownRenderer content={'`/home/user/reports/2026-05-17T05:46.md`'} />
    )
    const code = container.querySelector('code')
    expect(code).not.toBeNull()
    expect(code!.className).toContain('cursor-pointer')
  })

  it('does NOT render URL as clickable path', () => {
    const { container } = render(
      <MarkdownRenderer content={'`https://example.com/path/file.txt`'} />
    )
    const code = container.querySelector('code')
    expect(code).not.toBeNull()
    expect(code!.className).not.toContain('cursor-pointer')
  })
})

describe('Lightbox keyboard navigation', () => {
  function open(images: { src: string; alt?: string }[], index = 0) {
    window.dispatchEvent(new CustomEvent('lightbox', {
      detail: { images: images.map(i => ({ src: i.src, alt: i.alt ?? '' })), index },
    }))
  }

  it('renders nothing initially', () => {
    const { container } = render(<Lightbox />)
    expect(container.firstChild).toBeNull()
  })

  it('closes on Escape', async () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'a.png', alt: 'a' }]))
    expect(container.querySelector('img')).not.toBeNull()
    act(() => { fireEvent.keyDown(window, { key: 'Escape' }) })
    expect(container.firstChild).toBeNull()
  })

  it('ArrowRight advances index, ArrowLeft retreats, both clamp at the ends', () => {
    const { container } = render(<Lightbox />)
    act(() => open([
      { src: 'a.png', alt: 'a' },
      { src: 'b.png', alt: 'b' },
      { src: 'c.png', alt: 'c' },
    ], 0))
    expect(container.querySelector('img')!.getAttribute('src')).toBe('a.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('b.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('c.png')
    // Clamp at end
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('c.png')
    // Walk back
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('b.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('a.png')
    // Clamp at start
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('a.png')
  })

  it('arrow keys are no-ops with a single image', () => {
    const { container } = render(<Lightbox />)
    act(() => open([{ src: 'only.png', alt: 'only' }]))
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    act(() => { fireEvent.keyDown(window, { key: 'ArrowLeft' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('only.png')
  })

  it('keyboard events are ignored when the viewer is closed', () => {
    const { container } = render(<Lightbox />)
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(container.firstChild).toBeNull()
  })

  it('accepts the legacy { src, alt } payload as a single-image set', () => {
    const { container } = render(<Lightbox />)
    act(() => {
      window.dispatchEvent(new CustomEvent('lightbox', { detail: { src: 'legacy.png', alt: 'legacy' } }))
    })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('legacy.png')
    act(() => { fireEvent.keyDown(window, { key: 'ArrowRight' }) })
    expect(container.querySelector('img')!.getAttribute('src')).toBe('legacy.png')
  })

  it('dispatchLightbox reports all sibling images and the clicked index', () => {
    const events: LightboxDetail[] = []
    const spy = (e: Event) => events.push((e as CustomEvent).detail)
    window.addEventListener('lightbox', spy)
    const root = document.createElement('div')
    root.setAttribute('data-image-scope', '')
    const a = document.createElement('img'); a.src = 'https://x.invalid/a.png'; a.alt = 'a'; a.setAttribute('data-lightbox-image', '')
    const b = document.createElement('img'); b.src = 'https://x.invalid/b.png'; b.alt = 'b'; b.setAttribute('data-lightbox-image', '')
    const c = document.createElement('img'); c.src = 'https://x.invalid/c.png'; c.alt = 'c'; c.setAttribute('data-lightbox-image', '')
    root.append(a, b, c)
    document.body.appendChild(root)
    try {
      dispatchLightbox(b)
      expect(events).toHaveLength(1)
      expect(events[0].images.map(i => i.src)).toEqual([
        'https://x.invalid/a.png',
        'https://x.invalid/b.png',
        'https://x.invalid/c.png',
      ])
      expect(events[0].index).toBe(1)
    } finally {
      document.body.removeChild(root)
      window.removeEventListener('lightbox', spy)
    }
  })

  it('dispatchLightbox falls back to a single-image payload when no scope ancestor is present', () => {
    const events: LightboxDetail[] = []
    const spy = (e: Event) => events.push((e as CustomEvent).detail)
    window.addEventListener('lightbox', spy)
    const orphan = document.createElement('img'); orphan.src = 'https://x.invalid/lone.png'; orphan.alt = 'lone'
    document.body.appendChild(orphan)
    try {
      dispatchLightbox(orphan)
      expect(events[0].images).toEqual([{ src: 'https://x.invalid/lone.png', alt: 'lone' }])
      expect(events[0].index).toBe(0)
    } finally {
      document.body.removeChild(orphan)
      window.removeEventListener('lightbox', spy)
    }
  })
})

describe('MarkdownRenderer mcwidget strip is inline-code-aware', () => {
  it('preserves prose when an unclosed widget tag appears inside an inline-code span', () => {
    // Pre-fix bug: <mcwidget[\s\S]*$ alternative ate from the literal opening
    // tag (inside backticks) to end-of-block, dropping the rest of the prose.
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
    // The smoking-gun scenario from the bug screenshot.
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
    // Repro from chat where the agent leaked the full Anthropic tool_use
    // wrapper as text. Pre-fix: the unknown <tool_use> element trapped the
    // JSON body (including escaped \n literals) into a single paragraph,
    // dropping all the headers and rating callouts. Post-fix: the wrapper
    // and its body are gone, the surrounding prose renders normally.
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
