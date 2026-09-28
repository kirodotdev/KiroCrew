import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import PdfPreview from '../apps/papyrus/PdfPreview'

// The pane must embed the PDF in an ELEMENT THE DASHBOARD'S OWN CSP ALLOWS.
//
// `dashboard/server.py`'s base CSP sets `object-src 'none'` and `frame-src
// 'self'`. An `<object>` was therefore refused by Chromium and Firefox — the
// app's headline feature ("read the rendered PDF beside your source") rendered
// as a fallback message for most users, and it survived review because WebKit
// does not enforce the directive for this case, so it worked in Safari.
//
// These tests pin the element choice rather than the styling, because that is
// the part a well-meaning refactor would undo.

const SOURCE = readFileSync(
  join(__dirname, '..', 'apps', 'papyrus', 'PdfPreview.tsx'),
  'utf-8',
)

describe('PdfPreview', () => {
  it('embeds the PDF in an iframe, which frame-src allows', () => {
    const { container } = render(<PdfPreview src="/pdf?v=1" downloadName="paper.pdf" />)
    const frame = container.querySelector('iframe')
    expect(frame).not.toBeNull()
    expect(frame).toHaveAttribute('src', '/pdf?v=1')
  })

  it('never uses <object>, which object-src \'none\' blocks', () => {
    const { container } = render(<PdfPreview src="/pdf?v=1" downloadName="paper.pdf" />)
    expect(container.querySelector('object')).toBeNull()
    expect(container.querySelector('embed')).toBeNull()
    // Also pinned against the source, so a reviewer re-adding `<object>` behind a
    // conditional (which would leave the rendered tree clean in this one state)
    // still trips. Anchored to a JSX tag at the start of a line — the file's own
    // docblock names `<object>` in prose to explain why it is gone.
    expect(SOURCE).not.toMatch(/^\s*<(object|embed)[\s>]/m)
  })

  it('keys the frame on src so a recompile is actually visible', () => {
    // Same URL + same element = served from the in-page cache, so the pane would
    // keep showing the previous build. The version counter in the URL only helps
    // if the element remounts with it.
    expect(SOURCE).toMatch(/key=\{src\}/)
  })

  it('offers the download as persistent chrome, not replaced content', () => {
    // An iframe has no fallback children, so the escape hatch has to be a real
    // sibling — and it now also serves the majority who CAN see the PDF.
    render(<PdfPreview src="/pdf?v=2" downloadName="thesis.pdf" />)
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', '/pdf?v=2')
    expect(link).toHaveAttribute('download', 'thesis.pdf')
  })

  it('shows the empty state instead of an empty frame before the first compile', () => {
    const { container } = render(<PdfPreview src={null} downloadName="paper.pdf" />)
    expect(container.querySelector('iframe')).toBeNull()
    expect(screen.getByTestId('papyrus-pdf-empty')).toBeInTheDocument()
  })
})
