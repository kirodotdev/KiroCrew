/**
 * How a guide's last step on a destructive control ends: never on the press
 * alone, but once the confirm that press opened has been answered.
 *
 * The watch is started by the press and lives on its own: the control that
 * was pressed usually leaves the page at once (a menu item closes its menu),
 * so nothing about it may end the wait. The owner stops it when the step it
 * belongs to changes or the guide ends.
 */

import { closestRegistered, GUIDE_CONFIRM_ATTR, registeredWithin } from '../uiLocations/targetRegistry'

/** The modals the page draws (a confirm included); the guide's own panel is never one. */
const openDialogs = (): Element[] => [...document.querySelectorAll('[role="dialog"],[role="alertdialog"],dialog[open]')]

/** The marker a removal's final control carries (`guideConfirm()`); only its registration counts. */
export { GUIDE_CONFIRM_ATTR }

/** How long a press may take to open its confirm (a preview fetched first) before the wait ends. */
export const GUIDE_CONFIRM_OPEN_MS = 10_000

/**
 * `unknown`: the page gave no signal either way (a confirm that marks no
 * control, or no confirm at all). The step stays, and Done is the person's.
 */
export type ConfirmAnswer = 'confirmed' | 'cancelled' | 'unknown'

/** Whether *el* is, or sits inside, a control React registered as a removal's final one. */
function isConfirmControl(el: Element | null | undefined): boolean {
  return closestRegistered(el, 'confirm') !== null
}

/**
 * Call *onAnswer* once the press has been answered. Only a press of a
 * registered final control (`guideConfirm()`) is ever `confirmed`: a control
 * vanishing, or a dialog closing, says nothing about what was chosen.
 *
 * A dialog that was not open at the start (*before*) opens and closes again:
 * `confirmed` when its registered confirm control was pressed, `cancelled`
 * when a dialog that showed one closed any other way (Escape, the backdrop,
 * Cancel), `unknown` when it showed none, since nothing then tells a confirm
 * from a cancel.
 *
 * *pressed* is the control pressed. When it is itself a final control, the
 * press is the answer. When it sits inside a dialog already open, that dialog
 * may confirm inline (TeamDialog's first "Delete team" swaps in "Keep team"
 * and the real delete): the wait goes on while the dialog is open, a press of
 * its final control there is `confirmed`, and the dialog closing without one
 * is `unknown`. With no host dialog, nothing opening within
 * `GUIDE_CONFIRM_OPEN_MS` is `unknown`, never `cancelled`. Returns the stop.
 */
export function watchConfirmDialog(before: ReadonlySet<Element>, onAnswer: (answer: ConfirmAnswer) => void, pressed?: Element | null): () => void {
  // The open dialog the press was made in, if any: it may confirm inline.
  const host = pressed ? [...before].find(d => d.contains(pressed)) ?? null : null
  let opened: Element | null = null
  // Whether the opened dialog was ever seen showing a final control: read
  // while it is drawn, since its controls are unregistered once it closes.
  let openedMarked = false
  let confirmPressed = false
  let finished = false
  const seeOpened = () => {
    opened ??= openDialogs().find(d => !before.has(d)) ?? null
    if (opened) {
      clearTimeout(timer)
      if (opened.isConnected && registeredWithin('confirm', opened).length > 0) openedMarked = true
    }
    return opened
  }
  const onClick = (e: MouseEvent) => {
    const t = e.target
    if (!(t instanceof Element)) return
    const dialog = seeOpened()
    if (dialog && dialog.contains(t)) {
      confirmPressed = isConfirmControl(t)
      return
    }
    // The inline confirm: the final control pressed inside the host dialog.
    if (!dialog && host && host.contains(t) && isConfirmControl(t)) finish('confirmed')
  }
  const finish = (answer: ConfirmAnswer) => {
    if (finished) return
    stop()
    onAnswer(answer)
  }
  const observer = new MutationObserver(() => {
    if (!opened) {
      if (seeOpened()) return
      // The host dialog closed with no final press: nothing says what was chosen.
      if (host && (!host.isConnected || !openDialogs().includes(host))) finish('unknown')
      return
    }
    if (!opened.isConnected || !openDialogs().includes(opened)) {
      finish(!openedMarked ? 'unknown' : confirmPressed ? 'confirmed' : 'cancelled')
    } else {
      seeOpened()
    }
  })
  // Inside a host dialog the wait lasts as long as the dialog: an inline
  // confirm opens nothing new, and its final press may come at any time.
  const timer = setTimeout(() => { if (!host && !seeOpened()) finish('unknown') }, GUIDE_CONFIRM_OPEN_MS)
  function stop() {
    finished = true
    observer.disconnect()
    clearTimeout(timer)
    document.removeEventListener('click', onClick, true)
  }
  observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['role', 'open'] })
  document.addEventListener('click', onClick, true)
  // The press itself may have opened the dialog synchronously.
  seeOpened()
  // The pressed control is the final one (a form dialog's own Delete): answered.
  if (!opened && isConfirmControl(pressed)) queueMicrotask(() => finish('confirmed'))
  return stop
}

/** The dialogs open now, taken just before a press so the one it opens can be told apart. */
export function openDialogsNow(): ReadonlySet<Element> {
  return new Set(openDialogs())
}
