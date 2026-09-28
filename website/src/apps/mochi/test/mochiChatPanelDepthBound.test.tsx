/**
 * Mochi's chat bubbles must not blow the call stack on deeply nested markdown.
 *
 * `ChatPanel` runs its own react-markdown pipeline (`MD_REMARK` / `MD_REHYPE`,
 * with `rehype-raw` admitting embedded HTML) over the same untrusted message
 * content the dashboard renders. The depth bound is shared with the core
 * renderer as two pipeline plugins plus the leading-indent cap, so the panel
 * inherits the same guarantee -- and because nothing rewrites message text
 * beyond leading whitespace, `<mcwidget>` payloads reach `WidgetFrame` intact.
 */
import React from 'react'
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Bubble } from '../src/renderer/ChatPanel'

afterEach(cleanup)

function assistantMessage(content: string) {
  return { id: 'm1', role: 'assistant' as const, content, timestamp: 1 }
}

describe('mochi ChatPanel depth bound', () => {
  it('renders nesting below the bound as real structure', () => {
    const { container } = render(
      <Bubble animate={false} message={assistantMessage('>'.repeat(40) + ' payload-text')} />,
    )
    expect(container.textContent).toContain('payload-text')
    expect(container.querySelectorAll('blockquote').length).toBeGreaterThanOrEqual(40)
  })

  it('survives a 10,000-deep blockquote run', () => {
    const { container } = render(
      <Bubble animate={false} message={assistantMessage('>'.repeat(10_000) + ' payload-text')} />,
    )
    expect(container.textContent).toContain('payload-text')
  })

  it('survives a deep raw-HTML run, including SVG foreign-content voids', () => {
    for (const body of ['<div>\n' + '<div>'.repeat(3_000), '<svg>\n' + '<base>'.repeat(3_000)]) {
      const { container } = render(<Bubble animate={false} message={assistantMessage(body + 'payload-text')} />)
      expect(container.textContent).toContain('payload-text')
      cleanup()
    }
  })

  it('survives a deep nested list', () => {
    const lines: string[] = []
    for (let i = 0; i < 400; i++) lines.push(' '.repeat(i * 2) + '- item')
    const { container } = render(<Bubble animate={false} message={assistantMessage(lines.join('\n'))} />)
    expect(container.textContent).toContain('item')
  })

  it('hands a deeply nested widget body to WidgetFrame untouched', () => {
    // The bound acts on the parsed markdown tree, never on the message text
    // (beyond leading whitespace), so the widget extracted before parsing is
    // byte-identical -- the corruption a pre-parse text clamp caused here.
    // Deep nesting AND a <pre> line indented past the leading-indent cap: the
    // cap applies to text segments only, so neither is touched here.
    const nested = '<div>'.repeat(150) + 'chart' + '</div>'.repeat(150)
    const pre = '<pre>\n' + ' '.repeat(300) + 'aligned\n</pre>'
    const html = `<html><body>${nested}${pre}</body></html>`
    const { container } = render(
      <Bubble animate={false} message={assistantMessage(`<mcwidget title="chart">\n${html}\n</mcwidget>`)} />,
    )
    const frame = container.querySelector('iframe')
    expect(frame).not.toBeNull()
    const srcdoc = frame!.getAttribute('srcdoc') ?? ''
    expect(srcdoc).toContain(nested)
    expect(srcdoc).toContain(' '.repeat(300) + 'aligned')
  })
})
