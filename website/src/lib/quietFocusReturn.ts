/**
 * Focus return without the keyboard ring after a pointer interaction.
 *
 * Radix's DropdownMenu and Select open on `pointerdown` and cancel it, so their
 * trigger never takes mouse focus. When they close they hand focus back by
 * calling the trigger's `focus()`, and Chromium shows `:focus-visible` on a
 * scripted focus unless the previous focus came from a mouse click. Every
 * pointer open, pick and close therefore left the global accent ring painted on
 * the trigger: a keyboard cue the user never asked for, often half covered by
 * the pane beside it.
 *
 * `quietNextFocus` leaves the open/close logic to Radix, including whether
 * focus goes back at all (it does not after an outside click), and changes only
 * how that focus paints: after pointer input, the trigger's next `focus()` call
 * in the current task passes `focusVisible: false`. A keyboard close (Escape,
 * Enter on an item) keeps the ring, because there it shows where focus landed.
 */

/** A key that only modifies a pointer gesture (Shift+click) and is not navigation. */
function isModifierKey(key: string): boolean {
  return key === 'Shift' || key === 'Control' || key === 'Alt' || key === 'Meta'
    || key === 'CapsLock' || key === 'Fn' || key === 'AltGraph'
}

let lastInputWasPointer = false

// Installed at import, before any overlay can open, so the first close already
// knows which input opened it. Capture phase, so a primitive's own handler
// cannot hide the input.
if (typeof window !== 'undefined') {
  window.addEventListener('pointerdown', () => { lastInputWasPointer = true }, true)
  window.addEventListener('keydown', (e) => { if (!isModifierKey(e.key)) lastInputWasPointer = false }, true)
}

/** Whether the most recent person input was a pointer press rather than a key. */
export function lastInputIsPointer(): boolean {
  return lastInputWasPointer
}

/**
 * Make *el*'s next `focus()` call, made later in this task, skip the
 * `:focus-visible` ring when the last input was a pointer. Call it from an
 * overlay's `onCloseAutoFocus` handler, which runs just before Radix's own
 * focus return. No-op for keyboard input, a missing element, or an element that
 * already carries its own `focus` property.
 */
export function quietNextFocus(el: HTMLElement | null | undefined): void {
  if (!el || !lastInputIsPointer() || Object.prototype.hasOwnProperty.call(el, 'focus')) return
  const native = el.focus
  const restore = () => { delete (el as { focus?: unknown }).focus }
  el.focus = (options?: FocusOptions) => {
    restore()
    // `focusVisible` is in the HTML spec and shipped in Chromium, Firefox and
    // Safari, but not yet in TypeScript's DOM lib.
    native.call(el, { ...options, focusVisible: false } as FocusOptions)
  }
  // Radix calls focus() synchronously after this handler; if it decides not
  // to, drop the shim before anything else can hit it.
  setTimeout(restore, 0)
}
