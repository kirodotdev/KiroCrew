/**
 * A hand-off from the memory page's Graph tab to its Memories tab: list exactly
 * these records (by `memory_record_facets.record_ref`) and, for a single node,
 * open its detail. `seq` makes each jump a new one, even to the same records.
 */
import { createContext, useContext } from 'react'
import type { MemoryRecordRef } from '../../../types/memoryEditing'

/** A graph box selection of any size: the list asks the server for the records in it. */
export type GraphBoxPick = { bbox: string; layout: string; kinds?: string[]; count: number }
export type MemoryJump = { refs: string[]; box?: GraphBoxPick; open?: MemoryRecordRef; seq: number }
export type JumpToMemories = (refs: string[], open?: MemoryRecordRef, box?: GraphBoxPick) => void

export const MemoryJumpContext = createContext<JumpToMemories | null>(null)

/** The page's jump, or null where the graph is shown outside the memory page. */
export const useJumpToMemories = () => useContext(MemoryJumpContext)
