import { useChatSetting } from './useChatSetting'

// Single source for the split-view dimming preference. PaneDim reads it here
// so one toggle governs every split pane, and it stays live because the
// Settings row dispatches `mc-config-changed` on save.

export const useDimInactivePanes = () => useChatSetting('dimInactivePanes')
