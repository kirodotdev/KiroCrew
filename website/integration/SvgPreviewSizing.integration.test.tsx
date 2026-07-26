// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../src/components/MarkdownRenderer'

/**
 * Regression: SVGs authored with only a `viewBox` (no width/height) have no
 * intrinsic size and collapse to ~0px under max-w/max-h-only CSS, so a message
 * with several SVGs appeared to render only the one that declared width/height
 * (see the app-store / hero-dark / hero-light upload report). ImgWithFallback
 * now gives SVG previews a definite width basis so they stay visible.
 */
describe('MarkdownRenderer SVG preview sizing', () => {
  it('gives every SVG image a definite width so viewBox-only SVGs do not collapse', () => {
    const paths = ['/u/app-store.svg', '/u/hero-dark.svg', '/u/hero-light.svg']
    const content = paths.map(p => `![image](${p})`).join('\n')
    const { container } = render(<MarkdownRenderer content={content} softBreaks />)
    const imgs = container.querySelectorAll('img')
    expect(imgs.length).toBe(3)
    imgs.forEach(img => {
      expect((img as HTMLImageElement).style.width).toBe('240px')
    })
  })

  it('does not force a fixed width on raster images (keeps intrinsic sizing)', () => {
    const { container } = render(<MarkdownRenderer content={'![image](/u/photo.png)'} softBreaks />)
    const img = container.querySelector('img') as HTMLImageElement
    expect(img).toBeTruthy()
    expect(img.style.width).toBe('')
  })
})
