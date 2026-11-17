import { usePersistedBool } from './usePersistedBool'

/**
 * Whether a two-finger trackpad swipe opens and closes the desktop side panel
 * (Settings → Chat → Side panel). On by default. Read by the chat page's
 * swipe (`useChatPanelTrackpadSwipe`) and the Mates page's
 * (`useMembersPageTrackpadSwipe`); off, both leave every sideways wheel to the
 * browser, and the panel button and shortcut work as before.
 *
 * Browser-local and outside the uiPrefs host backup on purpose: the trackpad
 * belongs to this device, not to the owner's other browsers.
 */
export const SIDE_PANEL_SWIPE_KEY = 'mc-side-panel-trackpad-swipe'

export function useSidePanelSwipe() {
  return usePersistedBool(SIDE_PANEL_SWIPE_KEY, true)
}
