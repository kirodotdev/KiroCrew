/**
 * Whether one element is a modal layer: the surface in front, which owns every
 * gesture that starts inside it. `role="dialog"` / `"alertdialog"`,
 * `aria-modal="true"`, or an open `<dialog>`. Read as a rule rather than a list
 * of overlays, since `src/` declares dozens of dialogs and the next one would
 * have to remember an attribute.
 *
 * The one test both swipe readers walk up with: the phone drawer
 * (useDrawerSwipe's `dragOwnedBelow`) and the desktop trackpad swipe
 * (useTrackpadSwipe's `inModalLayer`).
 */
export function isModalLayer(el: Element): boolean {
  const role = el.getAttribute('role')
  if (role === 'dialog' || role === 'alertdialog') return true
  if (el.getAttribute('aria-modal') === 'true') return true
  return el.tagName === 'DIALOG' && (el as HTMLDialogElement).open
}
