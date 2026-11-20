import { useChatSetting } from './useChatSetting'

// Single source for the composer's ↑/↓ prompt-history preference, read the
// same way as useComposerSpellcheck: every composer reads it here, and it stays
// live because the Settings row dispatches `mc-config-changed` on save.

export const useComposerArrowKeyHistory = () => useChatSetting('arrowKeyPromptHistory')
