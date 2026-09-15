/**
 * Build the host HTML for opening a generated widget in a browser tab.
 *
 * The widget is UNTRUSTED LLM markup. A blob: URL inherits the dashboard's
 * origin, so opening the raw widget as a TOP-LEVEL document would let it
 * read/clear the dashboard's localStorage/cookies. So the popout never hosts the
 * widget directly — it hosts the same `sandbox="allow-scripts"` (null-origin)
 * iframe WidgetFrame uses in-panel, and puts the widget inside its `srcdoc`. The
 * widget therefore stays origin-isolated in the popped-out tab too.
 *
 * This module holds ONLY machine HTML/markup (a DOCTYPE, a fixed stylesheet, an
 * iframe element) — never user-facing copy — which is why it is listed in
 * `eslint.i18n.config.js`'s no-literal-string ignore set, alongside the other
 * mochi `shared/` data/markup modules.
 */

export function buildWidgetPopoutHtml(srcdoc: string, title?: string): string {
  // The nodes that carry untrusted content are built with DOM APIs, never by
  // interpolating widget markup into an HTML-template string. The widget is
  // assigned to the iframe's `srcdoc` PROPERTY directly and the element is
  // serialized by the DOM, which escapes the attribute so the widget can't
  // break out of `srcdoc="..."`. The iframe is built DETACHED (never appended
  // to a document) so serializing it can't trigger a frame load. The host chrome
  // around it is fixed, untrusted-free markup — no dynamic HTML-string sink.
  const iframe = document.createElement('iframe')
  iframe.setAttribute('sandbox', 'allow-scripts')
  iframe.srcdoc = srcdoc

  let titleTag = ''
  if (title !== undefined) {
    const titleEl = document.createElement('title')
    titleEl.textContent = title
    titleTag = titleEl.outerHTML
  }

  return (
    '<!DOCTYPE html><html><head><meta charset="utf-8">' +
    titleTag +
    '<style>html,body{margin:0;height:100%}' +
    'iframe{border:0;width:100vw;height:100vh;display:block}</style>' +
    '</head><body>' +
    iframe.outerHTML +
    '</body></html>'
  )
}
