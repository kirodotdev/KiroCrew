import { createContext, useContext } from 'react'
import type { NodeKey } from 'lexical'

/**
 * Bridge between the atomic `PasteBlockNode`s living in the Lexical tree and the
 * preview behaviour owned by `PillsPlugin`.
 *
 * A decorated `PasteBlockChip` reads this to open the preview popover. The node
 * itself carries id/seq/lines/content (so undo re-insert is self-contained) and
 * the plugin writes edits back through the node directly; this context supplies
 * only the surrounding app behaviour a bare node cannot.
 *
 * A pill is identified by its Lexical NODE KEY, never by its seq or block id:
 * a value holding the same marker twice rehydrates as two nodes that share both
 * (`$replaceComposerValue` appends one node per occurrence, `importJSON` keeps
 * id + seq), so a seq lookup would open the wrong pill's preview or write a
 * Save into its twin.
 */
export interface PasteBlocksContextValue {
  /** Open the click-to-edit preview for the pill node, anchored to the chip element. */
  openPreview(nodeKey: NodeKey, anchor: HTMLElement): void
}

const noop = () => {}

/**
 * Default is inert so a `PasteBlockChip` rendered outside a provider (e.g. a
 * unit test that mounts the node in isolation) does not throw — it simply has
 * no app behaviour wired.
 */
export const PasteBlocksContext = createContext<PasteBlocksContextValue>({
  openPreview: noop,
})

export function usePasteBlocks(): PasteBlocksContextValue {
  return useContext(PasteBlocksContext)
}
