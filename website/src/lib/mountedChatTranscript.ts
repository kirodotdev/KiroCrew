/** Whether a ChatPage is rendering the active slot's transcript in this window.
 *
 * `chat.activeSlot` keeps its value after the user leaves the chat routes, so
 * on an app page it names a session nobody sees. An app's ChatPanel, Papyrus's
 * co-author panel and the artifact companion mount ChatPage on a non-chat
 * route, and there the active slot IS on screen. Each mounted transcript holds
 * one count, so the unread gate can tell the two cases apart.
 */

let mounted = 0

/** Count one mounted transcript; the returned release uncounts it once. */
export function holdChatTranscriptMount(): () => void {
  mounted += 1
  let released = false
  return () => {
    if (released) return
    released = true
    mounted -= 1
  }
}

export function isChatTranscriptMounted(): boolean {
  return mounted > 0
}
