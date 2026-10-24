/** The one-off "this new chat has no folder of its own" signal.
 *
 * `POST /api/chat/slots` answers `session_folder_notice` beside the slot when the
 * owner turned on a fresh folder per new chat (`dashboard.session_folder_root`)
 * and this chat could not get one: the root was refused or the create failed,
 * so the chat opened on the default project. The notice describes this create,
 * not the slot, so the API client takes it off the payload and announces it on
 * `window`. Every create path then reaches the App shell's notice, including
 * the ones that call the client directly instead of the `createSlot` thunk. */

export const SESSION_FOLDER_UNAVAILABLE_EVENT = 'kc-session-folder-unavailable'

/** A create that asked for a folder answered with a different `folder_id`: the
 * gateway opened the chat but could not file it there (the folder was deleted,
 * or a concurrent move placed it), so it opened outside any folder. Announced
 * on `window` for the App shell's notice, like the session folder notice. */
export const SESSION_NOT_FILED_EVENT = 'kc-session-not-filed'

/** Which create opened outside its folder: the notice names the chat and the
 * folder, because the create may come from another pane or an app, where
 * "this chat" has nothing to point at. */
export interface NotFiledDetail {
  key: string
  /** The chat's title, empty when the create had none. */
  title: string
  /** The folder the create asked for. */
  folderId: string
}

/** Announce a create that opened outside the folder it asked for. */
export function announceNotFiled(detail: NotFiledDetail): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent<NotFiledDetail>(SESSION_NOT_FILED_EVENT, { detail }))
  }
}

/** Strip the notice from a create response and announce it when present. */
export function takeSessionFolderNotice<T>(created: T): T {
  if (created && typeof created === 'object' && 'session_folder_notice' in created) {
    const { session_folder_notice: notice, ...slot } = created as T & { session_folder_notice?: unknown }
    if (notice && typeof window !== 'undefined') {
      window.dispatchEvent(new Event(SESSION_FOLDER_UNAVAILABLE_EVENT))
    }
    return slot as T
  }
  return created
}
