import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, act } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import MessageErrorBoundary from '../components/MessageErrorBoundary'

describe('rehypeSanitize allowlist (React #290 fix)', () => {
  it('renders unknown bare XML tags as escaped literal text', () => {
    // This is the exact crash scenario: <dynamoDBClient> is not a known HTML
    // element, so it must render as escaped text rather than pass through to
    // React (which crashes with error #290).
    const { container } = render(
      <MarkdownRenderer content={'The client is <dynamoDBClient> and it handles requests.'} />
    )
    // Should render as literal text, not crash
    // Note: HTML parser lowercases tag names before they reach rehype
    expect(container.textContent).toContain('<dynamodbclient>')
    // Should NOT produce an actual dynamoDBClient element
    expect(container.querySelector('dynamoDBClient' as any)).toBeNull()
    expect(container.querySelector('dynamodbclient' as any)).toBeNull()
  })

  it('renders nested unknown tags with children as escaped text', () => {
    const { container } = render(
      <MarkdownRenderer content={'<myCustomTag>inner content</myCustomTag>'} />
    )
    // HTML parser lowercases tag names before they reach rehype
    expect(container.textContent).toContain('<mycustomtag>')
    expect(container.textContent).toContain('inner content')
    expect(container.textContent).toContain('</mycustomtag>')
  })

  it('still renders allowed HTML tags normally', () => {
    const { container } = render(
      <MarkdownRenderer content={'<strong>bold</strong> and <em>italic</em> and <div>block</div>'} />
    )
    expect(container.querySelector('strong')).not.toBeNull()
    expect(container.querySelector('em')).not.toBeNull()
    expect(container.querySelector('div')).not.toBeNull()
    expect(container.textContent).toContain('bold')
    expect(container.textContent).toContain('italic')
    expect(container.textContent).toContain('block')
  })

  it('GFM task-list checkboxes still render', () => {
    const md = '- [x] Done\n- [ ] Not done'
    const { container } = render(<MarkdownRenderer content={md} />)
    const checkboxes = container.querySelectorAll('input[type="checkbox"]')
    expect(checkboxes.length).toBe(2)
    expect((checkboxes[0] as HTMLInputElement).checked).toBe(true)
    expect((checkboxes[1] as HTMLInputElement).checked).toBe(false)
    // All checkboxes must be disabled (safe rendering)
    expect((checkboxes[0] as HTMLInputElement).disabled).toBe(true)
    expect((checkboxes[1] as HTMLInputElement).disabled).toBe(true)
  })

  it('$$...$$ display math still renders via KaTeX', () => {
    const { container } = render(
      <MarkdownRenderer content={'$$a^2 + b^2 = c^2$$'} />
    )
    const katex = container.querySelector('.katex, .katex-display')
    expect(katex).not.toBeNull()
  })

  it('strips dangerous attributes from allowed tags', () => {
    const { container } = render(
      <MarkdownRenderer content={'<div onclick="alert(1)">test</div>'} />
    )
    const div = container.querySelector('div')
    // The div should render (allowed tag) but without onclick
    expect(div).not.toBeNull()
    expect(container.innerHTML).not.toContain('onclick')
  })

  it('never reconstructs script tags — replaced with [unsupported:] marker', () => {
    const { container } = render(
      <MarkdownRenderer content={'<script>alert("xss")</script>'} />
    )
    expect(container.querySelector('script')).toBeNull()
    // Executable tags are NOT faithfully reconstructed even as text —
    // circuit breaker replaces them so the string is inert in any sink.
    expect(container.textContent).toContain('[unsupported: script]')
    expect(container.textContent).not.toContain('alert("xss")')
  })

  it('escapes HTML metacharacters in unknown-tag attribute values and child text', () => {
    const { container } = render(
      <MarkdownRenderer content={'<customTag data-x=\'"&gt;&lt;img src=x onerror=alert(1)&gt;\'>a &lt; b</customTag>'} />
    )
    // No live img element materialized from the attribute payload
    expect(container.querySelector('img')).toBeNull()
    // The payload survives only as entity-escaped inert text — the angle
    // brackets never appear as live markup in the DOM serialization.
    expect(container.innerHTML).not.toContain('<img')
    expect(container.innerHTML).toContain('&lt;')
  })

  it('drops dangerous-protocol attribute values when serializing unknown tags', () => {
    const { container } = render(
      <MarkdownRenderer content={'<customLink href="javascript:alert(1)">x</customLink>'} />
    )
    expect(container.textContent).not.toContain('javascript:')
  })

  it('GFM footnotes render as real elements, not escaped text', () => {
    const { container } = render(
      <MarkdownRenderer content={'Claim with a note.[^1]\n\n[^1]: The footnote definition.'} />
    )
    // remark-gfm wraps definitions in <section class="footnotes"> — must be a
    // live element (allowlisted), never literal "<section ..." text
    expect(container.querySelector('section')).not.toBeNull()
    expect(container.textContent).not.toContain('<section')
    expect(container.textContent).toContain('The footnote definition.')
  })

  it('semantic HTML5 tags render as elements', () => {
    const { container } = render(
      <MarkdownRenderer content={'<article><header>Head</header><time>2026</time></article>'} />
    )
    expect(container.querySelector('article')).not.toBeNull()
    expect(container.querySelector('header')).not.toBeNull()
    expect(container.textContent).not.toContain('<article')
  })

  it('inline-SVG child elements (tspan/stop) are allowlisted', () => {
    const { container } = render(
      <MarkdownRenderer content={'<svg><text><tspan>label</tspan></text></svg>'} />
    )
    expect(container.querySelector('tspan')).not.toBeNull()
    expect(container.textContent).not.toContain('<tspan')
  })

  it('drops non-allowlisted attributes (style) from allowed tags', () => {
    // Attribute ALLOWLIST: `style` is not permitted, so CSS-injection payloads
    // never reach the DOM even though the tag itself is allowed.
    const { container } = render(
      <MarkdownRenderer content={'<div style="background:url(javascript:alert(1))">x</div>'} />
    )
    const div = container.querySelector('div')
    expect(div).not.toBeNull()
    expect(div!.getAttribute('style')).toBeNull()
    expect(container.innerHTML).not.toContain('javascript:')
  })

  it('drops formaction and unknown handler-ish attributes', () => {
    const { container } = render(
      <MarkdownRenderer content={'<div formaction="x" onpointerdown="alert(1)">body</div>'} />
    )
    const html = container.innerHTML
    expect(html).not.toContain('formaction')
    expect(html).not.toContain('onpointerdown')
    // Tag itself still renders.
    expect(container.querySelector('div')).not.toBeNull()
    expect(container.textContent).toContain('body')
  })

  it('keeps attributes the renderer depends on (href, svg geometry)', () => {
    const { container } = render(
      <MarkdownRenderer content={'<a href="/x" title="t">link</a>'} />
    )
    expect(container.querySelector('a')!.getAttribute('href')).toBe('/x')

    const svg = render(
      <MarkdownRenderer content={'<svg><path d="M0 0" stroke="red"/></svg>'} />
    )
    const path = svg.container.querySelector('path')!
    expect(path).not.toBeNull()
    expect(path.getAttribute('d')).toBe('M0 0')
    expect(path.getAttribute('stroke')).toBe('red')
  })
})

describe('MessageErrorBoundary (per-message containment)', () => {
  // A component that always throws on render
  function CrashingChild(): JSX.Element {
    throw new Error('Simulated render crash')
  }

  it('renders children normally when no error', () => {
    const { container } = render(
      <MessageErrorBoundary rawContent="test">
        <div data-testid="child">Hello</div>
      </MessageErrorBoundary>
    )
    expect(container.textContent).toContain('Hello')
  })

  it('shows fallback when child throws', () => {
    // Suppress console.error from ErrorBoundary
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = render(
      <MessageErrorBoundary rawContent="raw text here">
        <CrashingChild />
      </MessageErrorBoundary>
    )
    expect(container.textContent).toContain('Message failed to render')
    spy.mockRestore()
  })

  it('shows raw text toggle button in fallback', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container, getByText } = render(
      <MessageErrorBoundary rawContent="the raw markdown content">
        <CrashingChild />
      </MessageErrorBoundary>
    )
    const btn = getByText('view raw')
    expect(btn).not.toBeNull()
    act(() => { fireEvent.click(btn) })
    expect(container.textContent).toContain('the raw markdown content')
    spy.mockRestore()
  })

  it('recovers when content changes after a transient crash (streaming)', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    // Frame 1: intermediate streaming frame crashes -> fallback latches
    const { container, rerender } = render(
      <MessageErrorBoundary rawContent="partial <foo">
        <CrashingChild />
      </MessageErrorBoundary>
    )
    expect(container.textContent).toContain('Message failed to render')
    // Frame 2: stream continues, content changes and now renders fine ->
    // boundary must reset instead of staying latched in the error state
    rerender(
      <MessageErrorBoundary rawContent="complete content">
        <div>recovered body</div>
      </MessageErrorBoundary>
    )
    expect(container.textContent).toContain('recovered body')
    expect(container.textContent).not.toContain('Message failed to render')
    spy.mockRestore()
  })
})
