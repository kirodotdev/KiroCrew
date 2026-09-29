export interface ComposerSelection {
  start: number
  end: number
}

export interface ComposerControl {
  focus(): void
  getRootElement(): HTMLElement | null
  getSelection(): ComposerSelection | null
  replaceText?(text: string): void
  setSelection(start: number, end?: number, options?: { focus?: boolean }): void
  /** Where a drop at a viewport point would land: the text offset and the
   *  viewport box of an insertion caret there, or null when it cannot be told
   *  (outside the editor, or no caret hit test in this browser). Optional: an
   *  editor without it leaves a drop at the caret and draws no drop caret.
   *  `adjust` is the host's clamp, applied before the caret is measured. */
  dropTargetAtPoint?(clientX: number, clientY: number, adjust?: (text: string, offset: number) => number): ComposerDropTarget | null
}

export interface ComposerDropTarget {
  offset: number
  /** Absent when the caret's position cannot be measured. */
  caret?: { left: number; top: number; height: number }
}

/**
 * The control the Lexical composer hangs on its editable root (as `__composer`)
 * for callers that reach the composer through the DOM hook
 * (`[data-composer-input]`) rather than a ref: SideChat's Select-to-Ask seed
 * nudge, and the test drivers in `test/helpers`. Offsets are in the composer's
 * string value (the same token string the host receives via `onChange`), so a
 * caret placed at `getValue().length` is the end of the visible text. The plain
 * `<textarea>` attaches none — its native API is the control there.
 */
export interface ComposerRootHandle extends ComposerControl {
  getValue(): string
  insertText(text: string): void
}

type HandleCarrier = Element & { __composer?: ComposerRootHandle }

/** The handle on a composer's editable root, or null for a textarea / unmounted editor. */
export function composerHandleOf(el: Element | null | undefined): ComposerRootHandle | null {
  return (el as HandleCarrier | null | undefined)?.__composer ?? null
}
