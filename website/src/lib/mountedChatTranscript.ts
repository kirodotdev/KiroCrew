/** Whether a ChatPage is rendering the active slot's transcript in this window.
 *
 * `chat.activeSlot` keeps its value after the user leaves the chat routes, so
 * on an app page it names a session nobody sees. An app's ChatPanel, Papyrus's
 * co-author panel and the artifact companion mount ChatPage on a non-chat
 * route, and there the active slot IS on screen. Each mounted transcript holds
 * one entry, so the unread gate can tell the two cases apart.
 *
 * A transcript counts only while its root element can be seen: an app that
 * mounts a ChatPanel inside a `display: none` container (to load it ahead of
 * time) does not put its session on screen. Visibility is read when the gate
 * asks, so a panel the app reveals or hides later is counted as it is then.
 */

interface TranscriptHold { el: Element | null }

const holds = new Set<TranscriptHold>()

/** Whether *el* renders a box. Before the root element exists the transcript
 *  counts as shown; a browser without `checkVisibility` counts it as shown. */
const isRendered = (el: Element | null): boolean => {
  if (!el) return true
  if (!el.isConnected) return false
  return typeof el.checkVisibility === 'function' ? el.checkVisibility() : true
}

let readRevealed: () => void = () => {}

/** The focus relay binds how a just-shown transcript's active chat is read. */
export function bindTranscriptRevealRead(read: () => void): void {
  readRevealed = read
}

/** Count one mounted transcript, rooted at *el* when known; the returned
 *  release uncounts it once. Each time *el* goes from hidden to shown (its
 *  box resizes from nothing), the bound reveal read runs, so it reads the chat. */
export function holdChatTranscriptMount(el: Element | null = null): () => void {
  const hold: TranscriptHold = { el }
  holds.add(hold)
  let shown = isRendered(el)
  const observer = el && typeof ResizeObserver === 'function'
    ? new ResizeObserver(() => {
      const now = isRendered(el)
      if (now && !shown) readRevealed()
      shown = now
    })
    : null
  if (el) observer?.observe(el)
  return () => { holds.delete(hold); observer?.disconnect() }
}

export function isChatTranscriptMounted(): boolean {
  for (const hold of holds) if (isRendered(hold.el)) return true
  return false
}
