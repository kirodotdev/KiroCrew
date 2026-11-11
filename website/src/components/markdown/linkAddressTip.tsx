import { InstantTip, keyboardFocused, useInstantTip } from '../InstantTip'
import { servedLinkPreviewClaims } from '../../linkPreview/linkPreviewRegistry'
import { safeHttpUrl } from '../../lib/safeUrl'

/** How long the pointer rests on a link before its address shows: the pause an
 *  app's link-preview card waits (`LinkPreviewLayer`), longer than a control's
 *  hint (`OPEN_DELAY_MS`), because a reader's pointer crosses links in running
 *  text far more often than it means to read where they go. */
export const LINK_ADDRESS_DELAY_MS = 300

/** The address a link's bubble shows, or undefined when it shows none. An
 *  http(s) URL passes `safeHttpUrl`, which also leaves out a userinfo URL, whose
 *  credential the bubble would otherwise print. A `mailto:` link shows whole, so
 *  its recipient and any subject or body it pre-fills are visible before it
 *  opens the mail app. In-app links (a `#heading`, a path, an editor scheme)
 *  have no address worth showing. */
export function linkAddress(href: string): string | undefined {
  if (safeHttpUrl(href)) return href
  try {
    return new URL(href).protocol === 'mailto:' ? href : undefined
  } catch {
    return undefined
  }
}

type AddressTipHandlers = Pick<React.DOMAttributes<HTMLElement>,
  'onPointerEnter' | 'onPointerDown' | 'onPointerUp' | 'onMouseEnter' | 'onMouseLeave' | 'onFocus' | 'onBlur'>

/** Whether an app's link-preview card answers a POINTER hover on `el`. The layer
 *  opens cards only under a `data-link-preview-scope`, matches the anchor's
 *  resolved `href`, and listens for the pointer alone, so this reads all three
 *  the same way and says nothing about keyboard focus. */
function previewCardClaims(el: HTMLElement): boolean {
  return el instanceof HTMLAnchorElement && el.closest('[data-link-preview-scope]') !== null
    && servedLinkPreviewClaims(el.href)
}

/**
 * The bubble that shows a link's full address (`linkAddress`) when the pointer
 * rests on the link or keyboard focus lands on it. A chat link's label is written by the
 * model, so it can name one thing and point somewhere else; the desktop app has
 * no browser status bar to show the destination, and a browser shows it in the
 * window's corner, away from the link.
 *
 * `address` undefined turns it off: no handlers, nothing renders (the hook still
 * runs, so the caller may switch it per render). On a pointer hover, a link an
 * app's link-preview card answers gets that card instead, and the hover closes a
 * bubble keyboard focus opened there: two pop-ups for one link cover each other.
 * Keyboard focus shows the address on every link, since the card layer does not
 * answer focus. Focus counts only when the browser would ring it
 * (`keyboardFocused`), so the focus a mouse click leaves on the link paints
 * nothing. No `aria-describedby`: assistive tech already reports a link's
 * destination from its `href`, and describing every link with its URL would
 * lengthen each one. The width cap keeps the bubble inside a phone-width window:
 * `InstantTip` clamps only where the bubble starts, so a bubble wider than the
 * window would run past its right edge.
 */
export function useLinkAddressTip(address: string | undefined): { handlers: AddressTipHandlers; bubble: React.ReactNode } {
  const { tip, tipHandlers } = useInstantTip({ placement: 'flow', openDelayMs: LINK_ADDRESS_DELAY_MS })
  if (!address) return { handlers: {}, bubble: null }
  const { 'aria-describedby': _describedBy, onMouseEnter, onFocus, ...rest } = tipHandlers
  void _describedBy
  return {
    handlers: {
      ...rest,
      onMouseEnter: (e) => { if (previewCardClaims(e.currentTarget)) rest.onMouseLeave(); else onMouseEnter(e) },
      onFocus: (e) => { if (keyboardFocused(e.currentTarget)) onFocus(e) },
    },
    bubble: <InstantTip tip={tip} className="w-max max-w-[min(28rem,calc(100vw_-_1rem))] break-all">{address}</InstantTip>,
  }
}
