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
import { i18nT } from '../../../i18n/t'

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
    expect(container.querySelector('[data-refused-link]')).toBeNull()
  })

  it('marks a refused destination with a muted cue and an accessible reason', () => {
    const { container } = render(
      <Bubble
        animate={false}
        message={assistantMessage('open [my note](obsidian://vault/note)')}
      />,
    )
    const cue = container.querySelector('[data-refused-link]') as HTMLElement | null
    expect(cue).not.toBeNull()
    const reason = i18nT('components.markdownRenderer.refused_link_destination')
    expect(cue!.getAttribute('title')).toBe(reason)
    expect(cue!.style.textDecoration).toContain('dotted')
    expect(cue!.textContent).toBe(`my note (${reason})`)
    expect(container.innerHTML).not.toContain('obsidian:')
  })

  it('marks javascript:, data: and empty destinations with the same cue', () => {
    for (const md of ['[click me](javascript:alert(1))', '[d](data:text/html,x)', '[label]()']) {
      const { container, unmount } = render(
        <Bubble animate={false} message={assistantMessage(md)} />,
      )
      expect(container.querySelector('a')).toBeNull()
      expect(container.querySelector('[data-refused-link]')).not.toBeNull()
      unmount()
    }
  })

  it('leaves named, id-only and empty anchors as plain prose without the cue', () => {
    for (const md of ['<a name="install">Install</a>', '<a id="top">Top</a>', 'x <a name="top"></a> y']) {
      const { container, unmount } = render(
        <Bubble animate={false} message={assistantMessage(md)} />,
      )
      expect(container.querySelector('a')).toBeNull()
      expect(container.querySelector('[data-refused-link]')).toBeNull()
      unmount()
    }
  })
})
