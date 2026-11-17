import { inUntrustedContent } from '../../guide/guideMarkers'

/* The app's own chrome, found by its markers. A marker is a plain DOM attribute
   (`data-chat-pane`, `data-pane-title-row`, `data-transcript-top-overlay`,
   `data-testid="pinned-prompt"`, ...), and rendered markdown keeps data
   attributes, so a message an agent or a channel wrote can carry any of them.
   A measurement that trusts a marker it finds anywhere can then be steered by
   page content: the approval card's height cap (requestRegionCap.ts) would hold
   a readable card's Allow once and Trust, and the approval chords
   (composerFocus.ts) would focus or answer through a forged row. Every marker
   lookup that decides layout or where a chord acts goes through these, which
   never return an element inside content an agent or a file wrote
   (`inUntrustedContent`, the renderer's untrusted root). */

/** Whether *el* is the app's own: not inside rendered agent or file content. */
export function isAppChrome(el: Element): boolean {
  return !inUntrustedContent(el)
}

/** Every match of *selector* in *scope* that is the app's own. */
export function chromeQueryAll<T extends Element = HTMLElement>(scope: ParentNode, selector: string): T[] {
  return [...scope.querySelectorAll<T>(selector)].filter(isAppChrome)
}

/** The nearest ancestor (or *from* itself) matching *selector* that is the
 *  app's own, skipping any match inside untrusted content. */
export function chromeClosest<T extends Element = HTMLElement>(from: Element, selector: string): T | null {
  for (let el = from.closest<T>(selector); el; el = el.parentElement?.closest<T>(selector) ?? null) {
    if (isAppChrome(el)) return el
  }
  return null
}
