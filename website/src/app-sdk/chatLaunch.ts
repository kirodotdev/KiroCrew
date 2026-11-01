/**
 * The memory-mode vocabulary an app's chat launch may request, shared by the SDK's
 * `openChat` (which writes the launch intent) and the routed chat's `?new=1` intake
 * (which claims it). Kept out of the SDK barrel: the validator is host plumbing,
 * and every barrel value is a name the vendor stub must publish to apps.
 */

/** The memory modes a launched session may request. */
export type ChatLaunchMemoryMode = 'persistent' | 'incognito' | 'temporary'

const CHAT_LAUNCH_MEMORY_MODES: ReadonlySet<string> = new Set<ChatLaunchMemoryMode>(['persistent', 'incognito', 'temporary'])

/** `mode` when it is a memory mode a launch may request, else undefined. */
export function chatLaunchMemoryMode(mode: unknown): ChatLaunchMemoryMode | undefined {
  return typeof mode === 'string' && CHAT_LAUNCH_MEMORY_MODES.has(mode) ? mode as ChatLaunchMemoryMode : undefined
}

/**
 * Whether a staged launch intent opens a NEW draft session, which the routed
 * chat's `?new=1` intake creates (and the send intake must leave alone): no target
 * session, and either an explicit `autoSend: false` or a new-session shape
 * (`memoryMode`, `sessionFolder: false`). Such a launch's message is staged as a
 * prefill, never sent automatically.
 */
export function chatLaunchOpensDraft(intent: {
  slotKey?: string
  autoSend?: boolean
  memoryMode?: unknown
  sessionFolder?: unknown
}): boolean {
  if (intent.slotKey) return false
  return intent.autoSend === false || !!chatLaunchMemoryMode(intent.memoryMode) || intent.sessionFolder === false
}
