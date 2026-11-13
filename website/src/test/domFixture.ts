/** Parse test markup into nodes without the HTML-sink properties the frontend rules refuse. */
function parse(html: string): Node[] {
  return Array.from(new DOMParser().parseFromString(html, 'text/html').body.childNodes)
}

/** Replace the children of `el` with `html`. */
export function setHtml(el: Element | ShadowRoot, html: string): void {
  el.replaceChildren(...parse(html))
}

/** Append `html` after the last child of `el`. */
export function appendHtml(el: Element, html: string): void {
  el.append(...parse(html))
}
