/** The "your stored start mode did not apply" signal, said once per page load.
 *
 * `POST /api/chat/slots` answers `default_mode_refused: "policy"` beside the slot
 * when the owner's stored start mode (`dashboard.default_approval_mode`) is Reads
 * and the `approval_modes` policy keeps the new chat on Normal. Like
 * `session_folder_notice` (lib/sessionFolderNotice), it describes this create,
 * not the slot, so the API client takes it off the payload and announces it on
 * `window`, where the App shell shows it. Only the first refusal of a page load is
 * announced: the policy refuses every New chat the same way, and saying so each
 * time would nag (rfc-default-approval-mode, section 7). A create that also failed
 * to get its session folder says only that: both use the shell's one notice, and
 * the folder failure is an error the person must see, so this status waits for the
 * next refused create. Run it before `takeSessionFolderNotice`, which strips the
 * field it checks. */

export const DEFAULT_MODE_REFUSED_EVENT = 'kc-default-mode-refused'

let announced = false

/** Strip the refusal from a create response and announce the first one. */
export function takeDefaultModeRefused<T>(created: T): T {
  if (created && typeof created === 'object' && 'default_mode_refused' in created) {
    const { default_mode_refused: refused, ...slot } = created as T & { default_mode_refused?: unknown }
    const folderFailed = Boolean((created as { session_folder_notice?: unknown }).session_folder_notice)
    if (refused && !announced && !folderFailed && typeof window !== 'undefined') {
      announced = true
      window.dispatchEvent(new Event(DEFAULT_MODE_REFUSED_EVENT))
    }
    return slot as T
  }
  return created
}

/** Tests only: forget that a refusal was announced. */
export function resetDefaultModeRefusedForTests(): void {
  announced = false
}
