import { createContext } from 'react'

/**
 * Absolute path of the open vault's content root (its local path plus subfolder).
 *
 * Read by `NoteImage` to resolve a root-relative image source (`/images/a.png`)
 * against the vault, the convention Obsidian-style vaults use. A context rather
 * than a prop because the image is rendered by `inline()`, a plain function
 * called from a dozen block renderers, and threading a second path through every
 * one of them would touch each call site for a value only the leaf needs.
 */
export const VaultRootCtx = createContext<string | undefined>(undefined)
