import { useChatSetting } from './useChatSetting'

// Whether the desktop two-finger trackpad swipe may open and close the side
// panel (Settings → Chat → Side panel). The chat page and the Bots page read it
// here, so one switch governs both.

export const useSidePanelSwipe = () => useChatSetting('sidePanelSwipe')
