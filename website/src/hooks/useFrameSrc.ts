import { useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { withBase } from '../lib/basePath'

/** The `src` to render on a frame that shows single-use sandbox-doc URLs.
 *
 * A sandbox-doc URL is served ONCE (handlers/sandbox_doc.py), so a frame must
 * never load the same one twice. Changing a mounted frame's `src` attribute
 * breaks that: the browser records the old URL as a history entry of the TOP
 * page, and the reader's next Back renavigates the frame onto it. The spent URL
 * then answers with the gateway's plain `404: Not Found`, painted where the
 * document was. Any re-mint does this — a live content update, a theme change,
 * a retry — so a frame that follows a document updated on a timer collected one
 * dead entry per update.
 *
 * So the attribute is set ONCE, with the first URL this frame shows, and every
 * later URL navigates the frame with `location.replace`, which swaps the
 * document in place and records nothing. The previous document stays on screen
 * until the new one commits, exactly as an attribute change kept it, so the
 * swap costs no flash. The hook starts over when `url` goes null. It does not
 * suit a frame KEYED on something else: a keyed remount would be created with
 * the first, already spent, URL.
 */
export function useFrameSrc(
  frameRef: RefObject<HTMLIFrameElement | null>,
  url: string | null | undefined,
): string | undefined {
  const target = url || null
  const [pinned, setPinned] = useState<string | null>(target)
  // Derived during render rather than in an effect, so the frame is created
  // with the right attribute in the same commit that first has a URL.
  if (target === null && pinned !== null) setPinned(null)
  if (target !== null && pinned === null) setPinned(target)
  const shownRef = useRef<string | null>(null)
  const frameElRef = useRef<HTMLIFrameElement | null>(null)

  // Runs on url changes only, so a caller must render its frame in the same
  // commit that first has a url (every current caller mounts it on `url`).
  useLayoutEffect(() => {
    const frame = frameRef.current
    if (!target || !frame) {
      shownRef.current = null
      frameElRef.current = null
      return
    }
    // A frame this hook has not seen yet was created with `pinned`. Not read
    // back from the attribute: under a sub-path build the DOM shim rebases it
    // (lib/basePath.ts), so it never equals the minted url.
    if (frame !== frameElRef.current || shownRef.current === null) {
      frameElRef.current = frame
      shownRef.current = pinned ?? target
    }
    if (target === shownRef.current) return
    shownRef.current = target
    navigateFrameInPlace(frame, target)
  }, [frameRef, target, pinned])

  return pinned ?? target ?? undefined
}

/** Point `frame` at `url` without adding a session-history entry.
 *
 * `location.replace` is one of the few members a parent may call on a
 * cross-origin child, which these opaque-origin documents are. A frame with no
 * window is not in a document, so there is nothing to navigate; the next mount
 * takes the current url from the attribute. The DOM shim that rebases `src`
 * under a sub-path build does not see this call, so the base is added here. */
function navigateFrameInPlace(frame: HTMLIFrameElement, url: string): void {
  frame.contentWindow?.location.replace(withBase(url))
}
