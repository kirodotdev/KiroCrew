import { useEffect } from 'react'
import { store, useAppDispatch } from '../../store'
import { refreshSlot, warmSlotCache } from '../../store/chatSlice'

/**
 * Reload a pane's slot after a Blocked link card's "Allow for this host" (or
 * its Undo) on that slot. The reply was saved with the link removed, and the
 * server shows allowed hosts' links again when it serves the slot, so the pane
 * re-reads its rows to show the link in place of the chip.
 *
 * The same pair every other re-read uses: the active slot's transcript through
 * `refreshSlot` (with the card's reply as the row to reach), any other slot's
 * through `warmSlotCache`. The chat page's own listener
 * (`useChatEventBridges`) reloads its active slot as well, so a pane showing
 * that slot on the chat page asks twice; a Bot's chat, which the chat page
 * does not host, asks once.
 */
export function useRedactionHostsReload(slotKey: string): void {
  const dispatch = useAppDispatch()
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail
      if (detail?.slot !== slotKey) return
      const messageTs: unknown = detail?.messageTs
      if (slotKey === store.getState().chat.activeSlot) {
        void dispatch(refreshSlot(typeof messageTs === 'string' && messageTs ? { key: slotKey, reachTs: messageTs } : slotKey))
      } else {
        void dispatch(warmSlotCache(typeof messageTs === 'string' && messageTs ? { key: slotKey, reachTs: messageTs } : slotKey))
      }
    }
    window.addEventListener('mc:redaction-hosts-changed', handler)
    return () => window.removeEventListener('mc:redaction-hosts-changed', handler)
  }, [dispatch, slotKey])
}
