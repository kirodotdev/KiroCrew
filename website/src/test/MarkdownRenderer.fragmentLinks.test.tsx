// @vitest-environment happy-dom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPathKindCache } from '../hooks/usePathKind'

// A `[x](#heading)` link is an in-page jump. It used to open a new tab
// (target="_blank"), because `#frag` resolves to http: and only editor schemes
// counted as "in place"; and repeated headings shared one id, so a GitHub-style
// `#setup-1` link had nothing to land on.

const MD = '# Setup\n\nintro\n\n## Setup\n\nmore\n\n[go to the second](#setup-1)\n'

describe('MarkdownRenderer #fragment links', () => {
  const realScroll = Element.prototype.scrollIntoView
  beforeEach(() => { __resetPathKindCache() })
  afterEach(() => { Element.prototype.scrollIntoView = realScroll; vi.restoreAllMocks() })

  it('renders a #fragment link without target or rel', () => {
    const { container } = render(<MarkdownRenderer content={MD} />)
    const a = container.querySelector('a[href="#setup-1"]')!
    expect(a).not.toBeNull()
    expect(a.hasAttribute('target')).toBe(false)
    expect(a.hasAttribute('rel')).toBe(false)
  })

  it('gives headings ids and dedupes repeats with -1', () => {
    const { container } = render(<MarkdownRenderer content={MD} />)
    expect(container.querySelector('h1')!.id).toBe('setup')
    expect(container.querySelector('h2')!.id).toBe('setup-1')
  })

  it('scrolls to the heading inside its own renderer and stays on the page', () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    // A second renderer with the same headings must not be the one scrolled.
    const other = render(<MarkdownRenderer content={MD} />)
    const { container } = render(<MarkdownRenderer content={MD} />)
    const ev = new MouseEvent('click', { bubbles: true, cancelable: true })
    container.querySelector('a[href="#setup-1"]')!.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    expect(scroll).toHaveBeenCalledTimes(1)
    expect(scroll.mock.contexts[0]).toBe(container.querySelector('h2'))
    expect(scroll.mock.contexts[0]).not.toBe(other.container.querySelector('h2'))
  })
})
