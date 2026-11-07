// @vitest-environment jsdom
//
// jsdom, not the suite-default happy-dom: happy-dom's parser drops an <svg>'s
// <style> and everything after it, so the test could not tell a fix from none.
// jsdom does not apply an SVG <style> to computed styles, so the check is that
// the stylesheet never reaches the page at all.
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SvgViewer } from '../components/FileRenderers'
import { ContentThumb } from '../components/library/ArtifactThumbs'

const STYLED_SVG = '<svg width="8" height="8"><style>body { color: red }</style><circle r="4" /></svg>'

describe("an SVG artifact's own stylesheet stays out of the page", () => {
  it.each([
    ['the viewer', <SvgViewer key="viewer" content={STYLED_SVG} />],
    ['the library card', <ContentThumb key="card" content={STYLED_SVG} kind="svg" />],
  ])('%s draws the shapes but not the stylesheet', (_where, svg) => {
    const { container } = render(svg)
    expect(container.querySelector('svg circle')).not.toBeNull()
    expect(container.querySelector('style')).toBeNull()
  })
})
