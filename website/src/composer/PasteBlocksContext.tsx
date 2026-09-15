import { createContext, useContext } from 'react'

/**
 * Bridge between the atomic `PasteBlockNode`s living in the Lexical tree and the
 * preview/remove behaviour owned by `PillsPlugin`.
 *
 * A decorated `PasteBlockChip` reads this to open the preview popover or remove
 * its block. The node itself carries id/seq/lines/content (so undo re-insert is
 * self-contained) and the plugin writes edits back through the node directly;
 * this context supplies only the surrounding app behaviour a bare node cannot.
 */
export interface PasteBlocksContextValue {
  /** Open the click-to-edit preview anchored to the chip element. */
  openPreview(seq: number, anchor: HTMLElement): void
  /** Remove the block for a seq (the ✕ on the chip). */
  removeBlock(seq: number): void
}

const noop = () => {}

/**
 * Default is inert so a `PasteBlockChip` rendered outside a provider (e.g. a
 * unit test that mounts the node in isolation) does not throw — it simply has
 * no app behaviour wired.
 */
export const PasteBlocksContext = createContext<PasteBlocksContextValue>({
  openPreview: noop,
  removeBlock: noop,
})

export function usePasteBlocks(): PasteBlocksContextValue {
  return useContext(PasteBlocksContext)
}
