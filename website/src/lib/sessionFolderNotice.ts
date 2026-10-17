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
