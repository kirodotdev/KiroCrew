// @vitest-environment jsdom
//
// jsdom, not the suite-default happy-dom: happy-dom's parser drops an <svg>'s
// <style> and everything after it, so the test could not see the stylesheet.
// jsdom does not apply an SVG <style> to computed styles, so this pins where the
// stylesheet lives: inside the SVG's own shadow root, never in the page. The
// browser check (the fill renders, the page and its print stay as they were) is
// in website/playwright/artifacts.spec.ts.
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SvgViewer } from '../components/FileRenderers'
import { ContentThumb } from '../components/library/ArtifactThumbs'

const STYLED_SVG = '<svg width="8" height="8"><style>.st0 { fill: red } body { color: red !important }</style><circle class="st0" r="4" /></svg>'

describe("an SVG artifact's own stylesheet styles only that SVG", () => {
  it.each([
    ['the viewer', <SvgViewer key="viewer" content={STYLED_SVG} />],
    ['the library card', <ContentThumb key="card" content={STYLED_SVG} kind="svg" />],
  ])('%s keeps the stylesheet with the shapes, inside the shadow root', (_where, svg) => {
    const { container } = render(svg)
    const shadow = container.querySelector('[data-guide-untrusted]')?.shadowRoot
    expect(shadow?.querySelector('svg style')?.textContent).toContain('.st0')
    expect(shadow?.querySelector('svg circle.st0')).not.toBeNull()
    expect(document.querySelector('style')).toBeNull()
    expect(Array.from(document.styleSheets).flatMap(sheet => Array.from(sheet.cssRules, rule => rule.cssText))).toEqual([])
  })
})
