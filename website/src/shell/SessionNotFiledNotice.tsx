import { useEffect, useState } from 'react'
import { i18nT } from '../i18n/t'
import ErrorNotice from '../components/ErrorNotice'
import { SESSION_NOT_FILED_EVENT, type NotFiledDetail } from '../lib/sessionFolderNotice'
import { queryClient } from '../api/queryClient'
import type { ChatFolder } from '../types'

/** The App shell's report of a new chat whose filing into the folder it asked
 * for was refused, so it opened outside any folder. The API client announces
 * it for every create path (see lib/sessionFolderNotice). It is its own notice,
 * not the shared agent-switch toast: a refused request renders through
 * ErrorNotice and stays until dismissed or handed off, while the shared toast
 * keeps its six-second expiry. */
export function SessionNotFiledNotice() {
  const [shown, setShown] = useState<{ title: string; folder: string } | null>(null)
  useEffect(() => {
    const onNotFiled = (event: Event) => {
      const detail = (event as CustomEvent<NotFiledDetail | undefined>).detail
      // The folder's name comes from the shared folder list the sidebar keeps
      // cached. A folder missing from it was most likely deleted, which is why
      // the filing was refused, so the notice says its folder no longer exists.
      const folders = queryClient.getQueryData<ChatFolder[]>(['chat-folders'])
      const folder = detail?.folderId ? folders?.find(f => f.id === detail.folderId)?.name ?? '' : ''
      setShown({ title: detail?.title ?? '', folder })
    }
    window.addEventListener(SESSION_NOT_FILED_EVENT, onNotFiled)
    return () => window.removeEventListener(SESSION_NOT_FILED_EVENT, onNotFiled)
  }, [])
  if (!shown) return null
  const dismiss = () => setShown(null)
  const { title, folder } = shown
  const message = title
    ? (folder
        ? i18nT('store.chatSlice.session_not_filed', { title, folder })
        : i18nT('store.chatSlice.session_not_filed_folder_gone', { title }))
    : (folder
        ? i18nT('store.chatSlice.session_not_filed_untitled', { folder })
        : i18nT('store.chatSlice.session_not_filed_untitled_folder_gone'))
  // askAgent on: this notice is mounted at the App root, outside every page,
  // holds no input of its own and sits beside none. The hand-off navigates
  // through the installed navigator, whose leave gate asks the page first, so
  // a page with an unsaved draft vetoes it (the notice then stays). On /chat
  // it opens a fresh session, and that slot switch first stores the composer
  // text as the outgoing slot's draft.
  return (
    <div className="fixed z-[70] top-safe-offset-14 left-safe-offset-4 right-safe-offset-4 sm:left-auto sm:w-[440px] rounded-lg shadow-xl animate-rise bg-bg-elevated">
      <ErrorNotice
        message={message}
        onDismiss={dismiss}
        askAgent
        onHandoff={dismiss}
        testId="session-not-filed-notice"
      />
    </div>
  )
}
