/**
 * Mochi WidgetFrame paint contract — the compositing promotion (#8037).
 *
 * The dashboard's sandbox-doc frames were all promoted to their own compositing
 * layer (#7931) after an engine was measured laying a document out, running its
 * scripts and reporting a correct height while rasterizing nothing — a
 * correctly sized, visible frame painting an empty box, silent by construction.
 * Mochi's widget frame renders the same kind of model-authored document but
 * builds it inline via srcDoc, so it never reaches the mint and sat outside
 * that PR's scope. It needs the same promotion for the same reason.
 *
 * A test DOM computes no layout and paints nothing, so dropping the property is
 * invisible to every other assertion in this suite: the style assertion below
 * is the whole guard.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'

import { WidgetFrame } from '../src/renderer/WidgetFrame'

describe('mochi WidgetFrame paint contract', () => {
  it('gives the frame its own compositing layer so a skipped first paint cannot blank it', () => {
    const { container } = render(<WidgetFrame html="<p>promoted</p>" title="T" />)
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    expect(iframe).not.toBeNull()
    expect(iframe.style.transform).toBe('translateZ(0)')
  })

  it('keeps the promotion additive: the frame still fills its card and blocks out its height', () => {
    // The transform must join the existing inline sizing, not replace it — a
    // frame that lost `display: block` or its width would change geometry for
    // every widget, which is a bigger regression than the one being fixed.
    const { container } = render(<WidgetFrame html="<p>sized</p>" title="T" />)
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    expect(iframe.style.display).toBe('block')
    expect(iframe.style.width).toBe('100%')
    // happy-dom serializes the `border` shorthand loosely, so match the style
    // rather than the exact serialized string.
    expect(iframe.style.borderStyle).toContain('none')
    // The frame is the bottom element of a rounded, clipping card, so its
    // bottom corners come from this wrapper. Promotion moves the frame onto
    // its own layer, and rounded-clipping a composited descendant is
    // compositor-side behaviour — pin the clip so the exposure stays guarded.
    const wrapper = iframe.parentElement as HTMLElement
    expect(wrapper.style.overflow).toBe('hidden')
  })

  it('keeps the frame sandboxed and its document minted, not the raw model HTML', () => {
    // This file is the only test that mounts this component, so it also pins
    // the security invariants the promotion must not disturb: the null-origin
    // sandbox stays exactly allow-scripts, and the model HTML reaches the
    // frame only through buildSrcdoc's document (CSP'd wrapper), never raw.
    const { container } = render(<WidgetFrame html="<p>handoff</p>" title="T" />)
    const iframe = container.querySelector('iframe') as HTMLIFrameElement
    expect(iframe.getAttribute('sandbox')).toBe('allow-scripts')
    const srcdoc = iframe.getAttribute('srcdoc')!
    expect(srcdoc).not.toBe('<p>handoff</p>')
    expect(srcdoc).toContain('<p>handoff</p>')
  })
})
