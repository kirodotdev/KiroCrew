import type { MemoryMode } from '../components/MemoryModeChip'

/** Memory modes from least to most private, the gateway's own order (`execution_context.MEMORY_MODES`). */
const MEMORY_MODE_ORDER: readonly MemoryMode[] = ['persistent', 'incognito', 'temporary']

/** A mode a fork may ask for in place of its parent's: always a stricter one. */
export type PrivateForkMemoryMode = Exclude<MemoryMode, 'persistent'>

/**
 * The modes a fork of a chat in `parent` mode may ask for besides keeping its own: only the
 * stricter ones, because the gateway refuses a looser one (`fork_memory_mode_looser`). A mode
 * this order does not know offers none, so an unreadable mode never offers a choice.
 */
export function stricterForkMemoryModes(parent: string | undefined): PrivateForkMemoryMode[] {
  const at = MEMORY_MODE_ORDER.indexOf((parent ?? 'persistent') as MemoryMode)
  return at < 0 ? [] : (MEMORY_MODE_ORDER.slice(at + 1) as PrivateForkMemoryMode[])
}
