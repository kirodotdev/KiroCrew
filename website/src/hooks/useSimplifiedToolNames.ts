import { useChatConfig } from './useChatConfig'

/** The `simplifiedToolNames` chat setting as live state. A thin selector over
 *  the shared `useChatConfig` -- one liveness implementation for every setting,
 *  so this reader gains the focus reload the others have instead of keeping a
 *  second spelling of the `mc-config-changed` subscription. */
export const useSimplifiedToolNames = () => useChatConfig().simplifiedToolNames
