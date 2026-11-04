import type { ConfirmOptions } from '../components/ConfirmDialog'

/**
 * App-wide in-app confirmation, for callers that are not components and so
 * cannot render `useConfirm()`'s dialog themselves: a hook's callback, a
 * keyboard-shortcut handler. `AppConfirmHost` (mounted once by `App`, in every
 * window layout) registers itself here and draws the dialog.
 *
 * The browser's native `confirm()` is not an option here. In the desktop app it
 * is an OS dialog that freezes the renderer while open, and once it closes the
 * window can stop taking input: the keyboard on Windows, the whole window on
 * macOS. The in-app dialog never leaves the page.
 *
 * With no host mounted the answer is "no": the action is not taken, and no
 * native dialog is raised as a fallback.
 */
type Asker = (opts: ConfirmOptions) => Promise<boolean>

let host: Asker | null = null

/** Called by `AppConfirmHost`. Returns the unregister function. */
export function registerAppConfirmHost(asker: Asker): () => void {
  host = asker
  return () => {
    if (host === asker) host = null
  }
}

/** Resolves `true` only when the user presses the dialog's confirm button. */
export function askAppConfirm(opts: ConfirmOptions): Promise<boolean> {
  return host ? host(opts) : Promise.resolve(false)
}
