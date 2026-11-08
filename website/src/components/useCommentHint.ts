import { useCallback, useState } from 'react'
import { safeSetItem } from '../utils/safeStorage'

/** Set when a reader closes the tip. Every build before image comments wrote it
 *  for the text-only tip, so it also means "has seen the text-only wording". */
export const HINT_KEY = 'kirocrew:comment-hint-dismissed'
/** Set when a reader closes the tip that names the image gesture. */
export const IMAGE_HINT_KEY = 'kirocrew:comment-hint-image-dismissed'

/** Whether any input can drag a box (a mouse, trackpad or pen), read when the
 *  tip mounts. On touch a press scrolls and never draws a box. */
const finePointerNow = () =>
  typeof window === 'undefined' || typeof window.matchMedia !== 'function' || window.matchMedia('(any-pointer: fine)').matches

/**
 * The file viewer's one-time comment tip.
 *
 * `image`: the tip names the image gesture, only where a drag can draw a box.
 * `isNew`: the reader closed the text-only tip before, so this is the tip coming
 * back once to announce box comments, and it carries a New badge. It goes away
 * with the tip, at "Got it", so it never outlives what it explains.
 */
export function useCommentHint() {
  const [state, setState] = useState(() => {
    const image = finePointerNow()
    const closedText = localStorage.getItem(HINT_KEY) === '1'
    const closedImage = localStorage.getItem(IMAGE_HINT_KEY) === '1'
    return { image, show: image ? !closedImage : !closedText, isNew: image && closedText && !closedImage }
  })
  const { image } = state
  const dismiss = useCallback(() => {
    safeSetItem(HINT_KEY, '1')
    // A touch-only reader saw the text-only wording, so a mouse connected
    // later still gets the image tip once.
    if (image) safeSetItem(IMAGE_HINT_KEY, '1')
    setState(s => ({ ...s, show: false }))
  }, [image])
  return { ...state, dismiss }
}
