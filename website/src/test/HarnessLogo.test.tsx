/**
 * The per-harness brand marks on Settings > Agent Harness
 * (`components/HarnessLogo.tsx`).
 *
 * Pinned here: every harness this build ships draws its OWN mark (no two share a
 * mask source, except Kiro's two harnesses which share the Kiro ghost), each mark
 * is a one-colour mask over `currentColor` so it follows the theme, each is
 * decorative, and a harness with no mark keeps the generic glyph.
 *
 * The mask is read from React's own serialization (`renderToStaticMarkup`):
 * jsdom drops the `mask-*` longhands, see KiroGhostMark.test.tsx.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { HarnessLogo } from '../components/HarnessLogo'

const BRANDED = ['claude', 'codex', 'deepseek', 'goose', 'opencode', 'pi'] as const

const maskOf = (id: string): string => {
  const html = renderToStaticMarkup(<HarnessLogo id={id} />)
  return /mask-image:url\(&quot;(.*?)&quot;\)/.exec(html)?.[1] ?? ''
}

describe('HarnessLogo', () => {
  it.each(BRANDED)('draws %s with its own mark, tinted by currentColor', id => {
    const { getByTestId } = render(<HarnessLogo id={id} />)
    const el = getByTestId(`harness-logo-${id}`)
    expect(el.style.backgroundColor).toBe('currentcolor')
    expect(el.getAttribute('aria-hidden')).toBe('true')
    expect(el.style.width).toBe('14px')
    expect(el.style.height).toBe('14px')
    expect(maskOf(id)).not.toBe('')
  })

  it('gives every branded harness a different mark', () => {
    const masks = BRANDED.map(maskOf)
    expect(new Set(masks).size).toBe(BRANDED.length)
  })

  it('draws the Kiro ghost for both Kiro CLI and KAS', () => {
    for (const id of ['kiro', 'kas']) {
      const { getByTestId, unmount } = render(<HarnessLogo id={id} />)
      expect(getByTestId('kiro-ghost-mark').getAttribute('aria-hidden')).toBe('true')
      unmount()
    }
    expect(maskOf('kiro')).toBe(maskOf('kas'))
    expect(BRANDED.map(maskOf)).not.toContain(maskOf('kiro'))
  })

  it.each(['zephyr', 'plugin-agent', 'constructor', ''])(
    'keeps the generic mark for %j, a harness with no brand mark here',
    id => {
      const { getByTestId, queryByTestId } = render(<HarnessLogo id={id} />)
      expect(getByTestId('harness-logo-fallback').getAttribute('aria-hidden')).toBe('true')
      expect(queryByTestId(`harness-logo-${id}`)).toBeNull()
      expect(queryByTestId('kiro-ghost-mark')).toBeNull()
    },
  )

  it('honours a size other than the list default', () => {
    const { getByTestId } = render(<HarnessLogo id="codex" size={20} />)
    expect(getByTestId('harness-logo-codex').style.width).toBe('20px')
  })
})
