export interface ComposerSelection {
  start: number
  end: number
}

export interface ComposerControl {
  focus(): void
  getRootElement(): HTMLElement | null
  getSelection(): ComposerSelection | null
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
