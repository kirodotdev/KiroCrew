import { useChatSetting } from './useChatSetting'

// Single source for the composer-spellcheck preference. Every composer the user
// types into (main chat and the side panel) reads it here so one toggle governs
// them all, and it stays live because the Settings row dispatches
// `mc-config-changed` on save. When false the composer input carries
// `spellCheck={false}` and the browser draws no red misspelled-word underlines.

export const useComposerSpellcheck = () => useChatSetting('spellcheck')
