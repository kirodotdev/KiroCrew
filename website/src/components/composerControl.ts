export interface ComposerSelection {
  start: number
  end: number
}

export interface ComposerControl {
  focus(): void
  getRootElement(): HTMLElement | null
  getSelection(): ComposerSelection | null
  setSelection(start: number, end?: number, options?: { focus?: boolean }): void
}
