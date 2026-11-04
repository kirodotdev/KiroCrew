import { useEffect } from 'react'
import { useConfirm, type ConfirmOptions } from './ConfirmDialog'
import { registerAppConfirmHost } from '../lib/appConfirm'

/**
 * Draws the dialog `askAppConfirm()` raises. `App` mounts exactly one, outside
 * its layout branches, so the dashboard, pop-out and embedded windows all have it.
 *
 * Always on the `top` layer: an ask can come from ⌘W while a full-screen
 * preview is open, and the ordinary dialog layer would paint under it.
 *
 * The dialog opens two macrotasks after the ask. Most asks come from a menu
 * item (a session's "Close session", a folder's "Delete"), and that menu is
 * still closing: Radix unmounts it in the same task and restores focus to its
 * trigger from a timer it queues then. Opening after that timer means the
 * dialog takes focus last, and records the trigger as the place to hand focus
 * back to on close. Opened sooner, the restore pulls focus out of the dialog
 * and Enter reopens the menu behind it.
 */
export default function AppConfirmHost() {
  const { confirm, confirmDialog } = useConfirm()
  useEffect(() => {
    const timers = new Set<ReturnType<typeof setTimeout>>()
    const unanswered = new Set<(ok: boolean) => void>()
    const later = (fn: () => void) => {
      const id = setTimeout(() => { timers.delete(id); fn() }, 0)
      timers.add(id)
    }
    const off = registerAppConfirmHost((opts: ConfirmOptions) => new Promise<boolean>(resolve => {
      unanswered.add(resolve)
      later(() => later(() => {
        unanswered.delete(resolve)
        void confirm({ ...opts, layer: 'top' }).then(resolve)
      }))
    }))
    return () => {
      off()
      for (const id of timers) clearTimeout(id)
      // An ask still waiting to open answers no, so its caller is released.
      for (const resolve of unanswered) resolve(false)
    }
  }, [confirm])
  return <>{confirmDialog}</>
}
