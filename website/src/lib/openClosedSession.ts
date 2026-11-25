import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import type { AppDispatch } from '../store'
import { resumeFromHistory } from '../store/chatSlice'

/** Recheck a closed session on a click. A failed lookup is not proof of deletion. */
export async function openClosedSession(
  { key, title }: { key: string; title: string },
  dispatch: AppDispatch,
  showActionError: (message: string) => void,
): Promise<'opened' | 'gone' | 'failed'> {
  let row: Awaited<ReturnType<typeof api.sessionMeta>>
  try {
    row = await api.sessionMeta(key)
  } catch {
    return 'failed'
  }
  if (!row) {
    showActionError(i18nT('store.chatSlice.session_gone_open_failed_named', { name: title }))
    return 'gone'
  }
  // The resume payload keeps its raw title (the key when untitled), never the label.
  void dispatch(resumeFromHistory({ key: row.key, title: row.title || row.key }))
  return 'opened'
}
