/**
 * Workspace fullscreen paints over the docked terminal, so an open terminal is
 * out of sight while fullscreen is on. Every terminal entry (the workspace
 * title-row toggle, the nav rail row and the `terminal` chord) reads it through
 * these two helpers, so they agree on what the user can see.
 */

/** The docked terminal is open and not covered by workspace fullscreen. */
export function isTerminalShown(open: boolean, workspaceFullscreen: boolean): boolean {
  return open && !workspaceFullscreen
}

/**
 * One activation of a terminal entry. Fullscreen is left first, so the docked
 * terminal is never opened behind it. A terminal that was open beneath
 * fullscreen is revealed by leaving it, without a toggle, which would close the
 * panel the user meant to bring back. A popped-out terminal lives in its own
 * window, which the entry focuses.
 */
export function activateTerminalEntry({ open, workspaceFullscreen, poppedOut, exitFullscreen, focusPopout, toggle }: {
  open: boolean
  workspaceFullscreen: boolean
  poppedOut: boolean
  exitFullscreen?: () => void
  focusPopout: () => void
  toggle: () => void
}): void {
  exitFullscreen?.()
  if (poppedOut) focusPopout()
  else if (!(open && workspaceFullscreen)) toggle()
}
