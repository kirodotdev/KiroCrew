// @vitest-environment jsdom
import { describe, it, expect, beforeAll } from 'vitest'
import mermaid from 'mermaid'

// The backend's pako credential scrubber (src/kiro_crew/security/redaction.py,
// `_mermaid_visible_record`) scans a decoded Mermaid Live diagram through a
// "visible view": a conservative model of what Mermaid DISPLAYS for a label.
// The model is only sound while it joins at least as much text as the renderer
// shows -- a construct the renderer hides but the model keeps is a way to
// display a credential the scan never saw.
//
// A pako link is rendered by the Mermaid Live editor, which this dashboard does
// not serve and cannot pin; what the backend model rests on is EVIDENCE of how
// Mermaid's label families behave, and this file is that evidence. Each case
// renders a real flowchart with the Mermaid release this repo installs
// (`securityLevel: 'strict'`, `htmlLabels` at its default) under jsdom and pins
// the node label's `textContent`. `_PAKO_VISIBLE_VIEW_MERMAID_VERSION` in the
// backend records the release these families were last checked against; it is
// documentation, not a gate, so a Mermaid bump does not fail on the constant.
// If a case reds after a bump, a family CHANGED: re-derive the backend model
// against the new behaviour (or set the constant to `None`, which switches the
// exemption off fail-closed) before updating the expectation here.
//
// jsdom has no layout engine, so SVG measurement is stubbed; text content does
// not depend on it. CSS-driven visibility (`hidden`, `display:none`) is not
// observable in textContent at all, which is exactly why the backend model
// rejects any tag that carries an attribute instead of trying to see it.

let renders = 0

async function visibleLabel(body: string): Promise<string> {
  renders += 1
  const { svg } = await mermaid.render(`visible-label-${renders}`, `flowchart TD\n  ${body} --> B`)
  // Production renders Mermaid SVG through the browser's HTML parser. Use that
  // same parsing model, but import the validated node instead of assigning HTML.
  const parsed = new DOMParser().parseFromString(svg, 'text/html')
  const root = parsed.body.firstElementChild
  if (
    parsed.body.childElementCount !== 1 ||
    root?.localName !== 'svg' ||
    root.namespaceURI !== 'http://www.w3.org/2000/svg'
  ) {
    throw new Error(`Mermaid returned a non-SVG root: ${root?.nodeName ?? 'none'}`)
  }
  const holder = document.createElement('div')
  holder.replaceChildren(document.importNode(root, true))
  const node = holder.querySelector('g.node[id^="flowchart-A-"]') ?? holder.querySelector('g.node')
  const label = node?.querySelector('.nodeLabel')
  expect(label, `no node label rendered for ${body}`).not.toBeNull()
  return label!.textContent ?? ''
}

describe('what the installed Mermaid displays for a label (backend visible-view model facts)', () => {
  beforeAll(() => {
    const svgProto = SVGElement.prototype as unknown as Record<string, unknown>
    svgProto.getBBox = () => ({ x: 0, y: 0, width: 100, height: 20 })
    svgProto.getComputedTextLength = () => 100
    Element.prototype.getBoundingClientRect = () =>
      ({ x: 0, y: 0, width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20, toJSON: () => ({}) }) as DOMRect
    // Mermaid bypasses installed KaTeX when jsdom lacks this browser constructor.
    // Supplying the capability signal exercises Mermaid's real KaTeX import and
    // generated MathML; no renderer output is mocked.
    Object.defineProperty(window, 'MathMLElement', { configurable: true, value: HTMLElement })
    mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' })
  })

  it("decodes Mermaid's own #word; entity family into label text", async () => {
    // The model decodes `#69;` to `E` the same way; both see the joined key.
    expect(await visibleLabel('A[AKIAIOSFODNN7EXAMPL#69;]')).toBe('AKIAIOSFODNN7EXAMPLE')
    expect(await visibleLabel('A[#quot;quoted#quot;]')).toBe('"quoted"')
  }, 30_000)

  it('displays an HTML character reference with its ampersand kept', async () => {
    // The model decodes `&#69;` to `E` and so joins MORE than is displayed --
    // the fail-closed direction. A named reference is displayed decoded.
    expect(await visibleLabel('A[AKIAIOSFODNN7EXAMPL&#69;]')).toBe('AKIAIOSFODNN7EXAMPL&E')
    expect(await visibleLabel('A[Fish &amp; Chips]')).toBe('Fish & Chips')
  }, 30_000)

  it('applies emphasis only inside markdown-string labels', async () => {
    // Plain labels show the delimiters literally; a markdown string hides them
    // and displays the joined key. The model removes them in both cases, which
    // joins at least as much text as either form displays.
    expect(await visibleLabel('A[AKIA**IOSF**ODNN7EXAMPLE]')).toBe('AKIA**IOSF**ODNN7EXAMPLE')
    expect(await visibleLabel('A["`AKIA**IOSF**ODNN7EXAMPLE`"]')).toBe('AKIAIOSFODNN7EXAMPLE')
  }, 30_000)

  it('applies CommonMark backslash escapes only inside markdown-string labels', async () => {
    // Plain labels keep both characters. Markdown strings drop the backslash
    // before ASCII punctuation and display that punctuation literally, so an
    // escaped emphasis delimiter remains visible rather than opening markup.
    expect(await visibleLabel('A[ghp\\_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA]')).toBe(
      'ghp\\_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    )
    expect(await visibleLabel('A["`ghp\\_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`"]')).toBe(
      'ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    )
    expect(await visibleLabel('A["`\\*star \\~tilde \\!bang slash\\\\pair`"]')).toBe(
      '*star ~tilde !bang slash\\pair',
    )
  }, 30_000)

  it('renders complete same-line double-dollar spans through KaTeX', async () => {
    // Mermaid's family is `$$...$$` on one line, independent of the KaTeX body.
    // The backend rejects the complete family instead of modeling commands that
    // can hide source text. Single and incomplete dollars remain literal;
    // CommonMark-escaped dollars are consumed before Mermaid's KaTeX check and
    // therefore join the same renderer family.
    expect(await visibleLabel('A["AKIAIOSF$$\\phantom{}$$ODNN7EXAMPLE"]')).toBe(
      'AKIAIOSFODNN7EXAMPLE',
    )
    expect(await visibleLabel('A["before$$x$$after"]')).toBe('beforexafter')
    expect(await visibleLabel('A["before$single$after"]')).toBe('before$single$after')
    expect(await visibleLabel('A["before$$open"]')).toBe('before$$open')
    expect(await visibleLabel('A["`before\\$\\$x\\$\\$after`"]')).toBe('beforexafter')
  }, 30_000)

  it('keeps Markdown link syntax literal in the installed renderer', async () => {
    // Mermaid 11.16.1 keeps this syntax today. The backend still rejects the
    // family because Mermaid Live is independently deployed and a renderer that
    // hides the empty link would join the surrounding credential.
    expect(await visibleLabel('A["`AKIAIOSFODNN7EXAMPL[](x)E`"]')).toBe(
      'AKIAIOSFODNN7EXAMPL[](x)E',
    )
  }, 30_000)

  it('displays the content of attribute-free phrasing tags and joins across <br>', async () => {
    // The only markup the model omits: what is inside is what is displayed.
    expect(await visibleLabel('A[AKIAIOSF<span>-</span>ODNN7EXAMPLE]')).toBe('AKIAIOSF-ODNN7EXAMPLE')
    expect(await visibleLabel('A[<b>Deploy</b><br/>ready]')).toBe('Deployready')
  }, 30_000)

  it("removes some elements together with their content, which no text model can see", async () => {
    // This is why the model REJECTS any other tag-like construct instead of
    // omitting it: omitting `<style>`'s tags would leave the `-` in the view
    // while the rendered label displays the joined key.
    expect(await visibleLabel('A[AKIAIOSF<style>-</style>ODNN7EXAMPLE]')).toBe('AKIAIOSFODNN7EXAMPLE')
    expect(await visibleLabel('A[AKIAIOSF<script>-</script>ODNN7EXAMPLE]')).toBe('AKIAIOSFODNN7EXAMPLE')
  }, 30_000)
})
