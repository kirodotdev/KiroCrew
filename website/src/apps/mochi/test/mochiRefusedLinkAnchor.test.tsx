/**
 * A refused link destination must not render as an anchor.
 *
 * Mochi's ChatPanel markdown supplies no `urlTransform`, so react-markdown's
 * defaultUrlTransform rewrites any destination outside its scheme allowlist
 * (and an empty `[x]()` destination) to href="". An `<a href="">` still paints
 * as a live link, and "Copy Link Address" on it yields the current page URL
 * (an empty href resolves against the document). The `a` override therefore
 * degrades a refused destination to an inert <span>, keeping the label as
 * ordinary prose, while a normal http(s) destination keeps its anchor.
 */
import React from 'react'
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Bubble } from '../src/renderer/ChatPanel'

afterEach(cleanup)

function assistantMessage(content: string) {
  return { id: 'm1', role: 'assistant' as const, content, timestamp: 1 }
}

describe('mochi ChatPanel refused link destinations', () => {
  it('renders an empty [x]() destination as inert text, not an anchor', () => {
    const { container } = render(
      <Bubble animate={false} message={assistantMessage('see [the vault]()')} />,
    )
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('the vault')
  })

  it('renders a refused-scheme destination (obsidian://) as inert text', () => {
    const { container } = render(
      <Bubble
        animate={false}
        message={assistantMessage('open [my note](obsidian://vault/note)')}
      />,
    )
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('my note')
  })

  it('renders a javascript: destination as inert text', () => {
    const { container } = render(
      <Bubble
        animate={false}
        message={assistantMessage('[click me](javascript:alert(1))')}
      />,
    )
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toContain('click me')
  })

  it('keeps the anchor for a normal https destination', () => {
    const { container } = render(
      <Bubble
        animate={false}
        message={assistantMessage('see [the docs](https://example.com/docs)')}
      />,
    )
    const a = container.querySelector('a')
    expect(a).not.toBeNull()
    expect(a!.getAttribute('href')).toBe('https://example.com/docs')
    expect(a!.textContent).toBe('the docs')
  })
})
