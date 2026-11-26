import { refreshUnfoldedForm } from '../lib/touchLayoutForm'

/**
 * Whether the chat page's side panel is mounted, in either host. The one
 * writer is `ChatSidePanels` (pages/chat/page/ChatSidePanels.tsx), which
 * renders both hosts from the same `sidePanelMounted` boolean and publishes it
 * from a layout effect, cleared on unmount. `recordUnfoldedForm`
 * (lib/touchLayoutForm.ts) reads it: the panel's host holds on a wake that
 * switched no form only while a panel is mounted, since only a mounted panel
 * has frames a host move would reload. So the setter owns the release: when
 * the last panel unmounts it refreshes the unfolded form, and a host held for
 * that panel follows the current form at the next open.
 */
let mounted = false

/** Called by ChatSidePanels only. */
export function setSidePanelMounted(next: boolean) {
  const released = mounted && !next
  mounted = next
  if (released) refreshUnfoldedForm()
}

export function isSidePanelMounted(): boolean {
  return mounted
}

/** Test seam: restore the module default between cases. */
export function __resetSidePanelMounted() {
  mounted = false
}
