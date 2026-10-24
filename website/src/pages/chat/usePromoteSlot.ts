import { useCallback, useRef, useState, type ReactNode } from 'react'
import { store, type AppDispatch } from '../../store'
import { captureForkOrigin, forkMayOpen, forkOpenSettled, forkOpenStarted, promoteSlot, requestSlotFolderOpen, switchSlot } from '../../store/chatSlice'
import { useConfirm } from '../../components/ConfirmDialog'
import { i18nT } from '../../i18n/t'

/**
 * "Keep this chat": copies an incognito/temporary session into a new persistent
 * one after the user confirms, then opens the copy unless the user moved to
 * another session meanwhile (`forkMayOpen`). Like every fork that may open its
 * child, it holds `pendingForkOpens` until the copy is active and opens the
 * copy's folder path. `promoting` is true from the
 * confirmation until the keep settles, so the menu item can be disabled and a
 * double click cannot make two copies.
 */
export function usePromoteSlot({ activeSlot, memoryMode, dispatch, showActionError }: {
  activeSlot: string | null
  memoryMode: string | undefined
  dispatch: AppDispatch
  showActionError: (message: string) => void
}): { promote: () => Promise<void>; promoting: boolean; promoteConfirmDialog: ReactNode } {
  const { confirm, confirmDialog } = useConfirm()
  const [promoting, setPromoting] = useState(false)
  const inFlightRef = useRef(false)

  const promote = useCallback(async () => {
    if (!activeSlot || inFlightRef.current) return
    inFlightRef.current = true
    try {
      const confirmed = await confirm({
        title: i18nT('pages.chatPage.keep_this_chat_confirm_title'),
        body: i18nT(
          memoryMode === 'temporary'
            ? 'pages.chatPage.keep_this_chat_confirm_body_temporary'
            : 'pages.chatPage.keep_this_chat_confirm_body',
        ),
        confirmLabel: i18nT('pages.chatPage.keep_this_chat'),
        danger: false,
        primary: true,
      })
      if (!confirmed) return
      const origin = captureForkOrigin(store.getState())
      setPromoting(true)
      // Held from the confirmation until the copy is active or the keep failed,
      // as every fork that may open its child holds it, so the chat page does not
      // open another session if this one leaves the list meanwhile.
      let released = false
      const release = () => { if (!released) { released = true; dispatch(forkOpenSettled()) } }
      dispatch(forkOpenStarted())
      try {
        const result = await dispatch(promoteSlot({ slot: activeSlot })).unwrap()
        if (result.ok) {
          if (forkMayOpen(store.getState(), origin)) {
            const switching = dispatch(switchSlot({ key: result.key, announceOnMissing: true }))
            // The copy is filed in the source's folder, which can be collapsed:
            // open that path so the copy's row shows.
            dispatch(requestSlotFolderOpen({ key: result.key, folder_id: result.folder_id }))
            release()
            await switching
          }
        } else showActionError(i18nT('pages.chatPage.keep_this_chat_failed'))
      } catch (e) {
        const code = (e as { code?: unknown } | null)?.code
        showActionError(i18nT(code === 'fork_corpus_too_large'
          ? 'pages.chatPage.keep_this_chat_too_long'
          : 'pages.chatPage.keep_this_chat_failed'))
      } finally {
        release()
        setPromoting(false)
      }
    } finally {
      inFlightRef.current = false
    }
  }, [activeSlot, memoryMode, confirm, dispatch, showActionError])

  return { promote, promoting, promoteConfirmDialog: confirmDialog }
}
